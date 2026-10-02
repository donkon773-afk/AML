#!/usr/bin/env python3
"""llm_pool — локальные модели кластера как советующие работники (T18).

Только stdlib. Модели описаны в .agent-sync/agents.json → "llm":
  qwen9b  qwen3.5-9b Q4_K_M на этом ПК (LM Studio 127.0.0.1:1234; заменил gpt-oss-20b:
          3,5 → 29,7 ток/с, модель целиком в VRAM RTX 3060)
  gemma   gemma-4-12b-coder на MacBook (LM Studio по tailnet)

Работа — очередь .agent-sync/llm_jobs.json:
  review  второе мнение по коммиту задачи в статусе review (создаётся само)
  ask     поручение от облачного агента: `llm_pool.py ask <кто> "<текст>" [--task T]`
          — экономит лимиты claude/codex/antigravity на черновой работе.

Распределение: задание получает модель в сети с наименьшей очередью (при
равенстве — более быстрая). Модель выпала (не отвечает / выгружена) —
её незавершённые задания возвращаются в очередь и уходят тем, кто в сети;
в сети никого — ждут. Результат: review/<дата>/llm/<job>.md, короткая запись
в log.md и письмо заказчику. Модели только советуют: board.json не меняют,
код не трогают, не коммитят (docs/SCENARIO-cluster-exchange.md §4.1).

  python scripts/llm_pool.py status
  python scripts/llm_pool.py ask codex "Предложи тесты для rebalance" --task T33
  python scripts/llm_pool.py --once        # один проход: пробы, раздача, выполнение
  python scripts/llm_pool.py --loop 30     # то же в цикле (serve делает это сам)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_sync as S  # noqa: E402

JOBS = S.SYNC / "llm_jobs.json"
REVIEW = S.ROOT / "review"
MAX_ATTEMPTS = 3
_running: dict[str, threading.Thread] = {}      # модель → поток с её текущим заданием


class JobsLock:
    """Файловая блокировка очереди: её пишут и serve, и CLI `ask` из других процессов."""
    path = S.SYNC / ".jobs.lock"

    def __enter__(self):
        for _ in range(200):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 30:
                        self.path.unlink()
                except OSError:
                    pass
                time.sleep(0.05)
        raise SystemExit("llm_pool: .jobs.lock занят")

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------- конфигурация и пробы ----------

def pool() -> dict:
    return S.load(S.AGENTS).get("llm", {})


def http_json(url: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:  # LM Studio отдаёт причину в теле
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("error")
        except Exception:
            detail = None
        raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}") from e


def probe(cfg: dict) -> dict:
    """Модель в сети = сервер отвечает и нужная модель загружена."""
    t0 = time.time()
    try:
        models = http_json(cfg["endpoint"] + "/api/v0/models", timeout=5)["data"]
    except Exception as e:
        return {"online": False, "why": f"нет связи: {str(e)[:120]}", "probe_ms": None}
    m = next((x for x in models if x.get("id") == cfg["model"]), None)
    ms = round((time.time() - t0) * 1000)
    if m is None:
        return {"online": False, "why": "модели нет на сервере", "probe_ms": ms}
    if m.get("state") not in (None, "loaded"):
        return {"online": False, "why": f"модель {m.get('state')}", "probe_ms": ms}
    return {"online": True, "why": "", "probe_ms": ms, "ctx": m.get("max_context_length")}


def refresh() -> dict:
    """Пробует все модели, пишет статус в agents.json → llm.<имя>.status."""
    with S.Lock():
        agents = S.load(S.AGENTS)
        for name, cfg in agents.get("llm", {}).items():
            st = probe(cfg)
            prev = cfg.get("status", {})
            if prev.get("online") != st["online"]:
                S.event("llm", text=f"{name}: {'в сети' if st['online'] else 'не в сети — ' + st['why']}")
            cfg["status"] = {**prev, **st, "checked": S.iso()}
        S.save(S.AGENTS, agents)
    return agents["llm"]


# ---------- очередь ----------

def load_jobs() -> list:
    return S.load(JOBS)["jobs"] if JOBS.exists() else []


def save_jobs(jobs: list) -> None:
    # Незавершённые задания (T43) никогда не удаляются при ограничении очереди
    incomplete = [j for j in jobs if j.get("state") in ("pending", "assigned", "running", "delivering")]
    finished = [j for j in jobs if j.get("state") not in ("pending", "assigned", "running", "delivering")]
    keep_finished = max(0, 300 - len(incomplete))
    kept_ids = set(id(j) for j in (finished[-keep_finished:] + incomplete))
    S.save(JOBS, {"jobs": [j for j in jobs if id(j) in kept_ids]})


_IS_COMMIT: dict[str, bool] = {}   # хэш → есть ли такой коммит; git не дёргается каждые 30 с


def _is_commit(h: str) -> bool:
    if h not in _IS_COMMIT:
        _IS_COMMIT[h] = subprocess.run(["git", "cat-file", "-e", h + "^{commit}"], cwd=S.ROOT,
                                       capture_output=True, **S.NO_WINDOW).returncode == 0
    return _IS_COMMIT[h]


def commit_of(task: dict) -> str | None:
    """Последний коммит, упомянутый в notes/summary задачи и существующий в репо."""
    text = f"{task.get('notes', '')} {task.get('summary', '')}"
    for h in reversed(re.findall(r"\b[0-9a-f]{7,40}\b", text)):
        if _is_commit(h):
            return h
    return None


def enqueue_reviews(jobs: list) -> int:
    """Каждая задача в review с коммитом получает одно второе мнение на коммит."""
    seen = {(j.get("task"), j.get("commit")) for j in jobs if j["kind"] == "review"}
    added = 0
    for t in S.load(S.BOARD)["tasks"]:
        if t.get("status") != "review":
            continue
        c = commit_of(t)
        if c and (t["id"], c) not in seen:
            jobs.append(new_job("review", "agent_sync", task=t["id"], commit=c,
                                reply_to=sorted({"claude", t.get("assignee") or "claude"})))
            added += 1
    return added


def new_job(kind: str, by: str, **kw) -> dict:
    # время + случайный хвост: два задания в одну миллисекунду не совпадут (T18 F2)
    return {"id": f"J{int(time.time() * 1000) % 10**10}-{secrets.token_hex(3)}", "kind": kind, "by": by,
            "state": "pending", "llm": None, "lease": 0, "attempts": 0, "created": S.iso(), **kw}


def assign(jobs: list, llm: dict) -> list[str]:
    """Раздаёт ожидающие задания моделям в сети; снимает с выпавших."""
    moves = []
    online = [n for n, c in llm.items() if c.get("status", {}).get("online")]
    for j in jobs:
        if j["state"] in ("assigned", "running") and j["llm"] not in online:
            moves.append(f"{j['id']}: {j['llm']} не в сети → в очередь")
            j.update(state="pending", llm=None, lease=j.get("lease", 0) + 1)   # старый ответ станет чужим
    load = {n: sum(1 for j in jobs if j["llm"] == n and j["state"] in ("assigned", "running")) for n in online}
    for j in jobs:
        if j["state"] != "pending" or not online:
            continue
        if j["attempts"] >= MAX_ATTEMPTS:
            j["state"] = "failed"
            continue
        # ожидаемое время очереди: (задания + 1) / скорость; измеренная tok_s важнее паспортной
        best = min(online, key=lambda n: ((load[n] + 1) / (llm[n].get("status", {}).get("tok_s") or llm[n].get("speed", 1)), n))
        j.update(state="assigned", llm=best, lease=j.get("lease", 0) + 1)
        load[best] += 1
        moves.append(f"{j['id']} ({j['kind']} {j.get('task') or ''}) → {best}")
    return moves


# ---------- выполнение ----------

def build_prompt(job: dict, cfg: dict) -> list:
    sys_msg = (f"Ты — советующий участник кластера агентов (локальная модель {cfg['model']}). "
               "Отвечай по-русски, кратко и проверяемо. Ты не меняешь доску и код — только советуешь.")
    if job["kind"] == "review":
        task = next((t for t in S.load(S.BOARD)["tasks"] if t["id"] == job["task"]), {})
        diff = subprocess.run(["git", "show", "--stat", "--patch", job["commit"]], cwd=S.ROOT, **S.NO_WINDOW,
                              capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        limit = cfg.get("max_diff_chars", 12000)
        cut = f"\n[диф обрезан до {limit} из {len(diff)} символов]" if len(diff) > limit else ""
        user = (f"Задача {job['task']}: {task.get('title', '')}\nЦель: {task.get('summary', '')}\n\n"
                f"Коммит {job['commit']}:\n{diff[:limit]}{cut}\n\n"
                "Первая строка строго: `critic2: accept` или `critic2: return` или `critic2: inconclusive`.\n"
                "Затем не больше 8 пунктов: что сломано, что не проверено, команда для воспроизведения.")
    else:
        ctx = ""
        if job.get("task"):
            task = next((t for t in S.load(S.BOARD)["tasks"] if t["id"] == job["task"]), {})
            ctx = f"Контекст задачи {job['task']}: {task.get('title', '')}. {task.get('summary', '')}\n\n"
        user = ctx + job["prompt"]
    return [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}]


def _owned(job: dict | None, name: str, lease: int) -> bool:
    return job is not None and job.get("llm") == name and job.get("lease", 0) == lease


def run_job(job_id: str, name: str) -> None:
    """Выполняет задание. Результат сохраняется и доставляется, только если
    модель всё ещё владеет заданием (тот же lease): задание, переназначенное
    при выпадении модели, не доставляется дважды (T18 F1)."""
    cfg = pool()[name]
    with JobsLock():
        jobs = load_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None or job.get("llm") != name or job["state"] != "assigned":
            return
        lease = job.get("lease", 0)
        job.update(state="running", started=S.iso(), attempts=job["attempts"] + 1)
        save_jobs(jobs)
    t0 = time.time()
    try:
        body = {"model": cfg["model"], "messages": build_prompt(job, cfg), "temperature": 0.2,
                "max_tokens": cfg.get("max_tokens", 1500), **cfg.get("extra", {})}
        if job.get("stop"):
            body["stop"] = job["stop"]
        resp = http_json(cfg["endpoint"] + "/v1/chat/completions", body, timeout=cfg.get("timeout", 300))
        choice = resp["choices"][0]
        text = re.sub(r"<think>.*?</think>", "", choice["message"].get("content") or "", flags=re.S).strip()
        if job["kind"] == "review" and not re.match(r"`?critic2: (accept|return|inconclusive)", text):
            text = "critic2: inconclusive (модель не соблюла формат первой строки)\n" + text
        secs = time.time() - t0
        toks = (resp.get("usage") or {}).get("completion_tokens") or 0
        result = {"state": "delivering", "answer": text, "finished": S.iso(), "secs": round(secs, 1),
                  "tok_s": round(toks / secs, 1) if secs else None,
                  "truncated": choice.get("finish_reason") == "length"}
    except Exception as e:
        text, result = None, {"state": "pending", "llm": None, "error": str(e)[:200]}  # отдадим другой модели
    with JobsLock():
        jobs = load_jobs()
        cur = next((j for j in jobs if j["id"] == job_id), None)
        owned = _owned(cur, name, lease)
        if owned:
            if result["state"] == "pending":
                result["lease"] = lease + 1
            cur.update(result)
            save_jobs(jobs)
    if not owned:
        S.event("llm", text=f"{job_id}: ответ {name} отброшен — задание уже переназначено")
        return
    if result["state"] != "delivering":
        S.event("llm", text=f"{job_id}: {name} сбой — {result['error'][:120]}; задание вернулось в очередь")
        return
    if finish_delivery(job_id):
        with S.Lock():
            agents = S.load(S.AGENTS)
            st = agents["llm"][name].setdefault("status", {})
            st.update(last_job=job_id, last_secs=result["secs"], tok_s=result["tok_s"],
                      done=st.get("done", 0) + 1)
            S.save(S.AGENTS, agents)


def report_rel(job: dict, name: str) -> str:
    """Путь отчёта модели от корня репо; день — по времени ответа, чтобы повтор доставки писал тот же файл."""
    day = (job.get("finished") or S.iso())[:10]
    return (REVIEW.relative_to(S.ROOT) / day / "llm" /
            f"{job['id']}-{name}-{job.get('task') or job['kind']}.md").as_posix()


def finish_delivery(job_id: str) -> bool:
    """Доставляет сохранённый ответ; при сбое задание остаётся в delivering и
    дожимается следующим tick(). Шаги отмечаются в job["delivered"], поэтому
    повтор не дублирует уже сделанное (T18 F3). Гарантия — «хотя бы один раз»:
    при падении процесса ровно между записью шага и его отметкой этот один
    шаг повторится; потерять ответ так нельзя."""
    with JobsLock():
        job = next((j for j in load_jobs() if j["id"] == job_id), None)
    if job is None or job["state"] != "delivering":
        return False
    cfg = pool().get(job["llm"]) or {"model": job["llm"], "host": ""}

    def mark(step):
        with JobsLock():
            jobs = load_jobs()
            cur = next(j for j in jobs if j["id"] == job_id)
            cur.setdefault("delivered", []).append(step)
            save_jobs(jobs)
        job.setdefault("delivered", []).append(step)
    try:
        deliver(job, job["llm"], cfg, job["answer"], job, mark)
    except Exception as e:
        with JobsLock():
            jobs = load_jobs()
            cur = next(j for j in jobs if j["id"] == job_id)
            cur.update(deliver_error=str(e)[:200], deliver_tries=cur.get("deliver_tries", 0) + 1)
            save_jobs(jobs)
        S.event("llm", text=f"{job_id}: доставка не удалась ({str(e)[:80]}), повтор в следующем проходе")
        return False
    with JobsLock():
        jobs = load_jobs()
        cur = next(j for j in jobs if j["id"] == job_id)
        cur.update(state="done", report=report_rel(cur, cur["llm"]))
        cur.pop("deliver_error", None)
        save_jobs(jobs)
    return True


def deliver(job: dict, name: str, cfg: dict, text: str, result: dict, mark=lambda step: None) -> None:
    done = set(job.get("delivered") or [])
    fp = S.ROOT / report_rel(job, name)
    fp.parent.mkdir(parents=True, exist_ok=True)
    head = (f"# {job['kind']} {job.get('task') or ''} — {name}\n"
            f"Источник: локальная модель `{cfg['model']}` ({cfg.get('host', '')}); совет, доску не меняет.\n"
            f"Заказчик: {job['by']}; {result['secs']} с, {result['tok_s']} ток/с"
            f"{'; ОТВЕТ ОБРЕЗАН по max_tokens' if result['truncated'] else ''}.\n\n")
    if job["kind"] == "ask":
        head += f"## Запрос\n{job['prompt']}\n\n## Ответ\n"
    if "report" not in done:
        fp.write_text(head + text + "\n", encoding="utf-8")
        mark("report")
    rel = fp.relative_to(S.ROOT).as_posix()
    first = text.splitlines()[0][:160] if text else "(пустой ответ)"
    topic = job.get("task") or job["kind"]
    if "log" not in done:
        S.append_log(name, topic, f"{first}\nИсточник: локальная модель {cfg['model']} (совет). Полностью: {rel}")
        mark("log")
    for who in job.get("reply_to") or [job["by"]]:
        box = S.INBOX / f"{who}.md"
        if f"inbox:{who}" not in done and (box.exists() or who in S.load(S.AGENTS)["agents"]):
            with box.open("a", encoding="utf-8") as f:
                f.write(f"\n## {S.now():%Y-%m-%d %H:%M} — {name} — {topic}\n{first}\nПолностью: {rel}\n")
            mark(f"inbox:{who}")
    S.event("llm", text=f"{job['id']} {topic}: {name} ответил за {result['secs']} с — {first[:60]}")


def tick() -> list[str]:
    """Один проход: пробы → новые review-задания → раздача → запуск."""
    llm = refresh()
    with JobsLock():
        jobs = load_jobs()
        added = enqueue_reviews(jobs)
        moves = assign(jobs, llm)
        save_jobs(jobs)
    for j in jobs:
        if j["state"] == "delivering" and not any(
                t.is_alive() and t.name == f"llm-{j['llm']}" for t in _running.values()):
            finish_delivery(j["id"])
    for name in llm:
        th = _running.get(name)
        if th and th.is_alive():
            continue
        nxt = next((j for j in jobs if j["llm"] == name and j["state"] == "assigned"), None)
        if nxt:
            th = threading.Thread(target=run_job, args=(nxt["id"], name), daemon=True, name=f"llm-{name}")
            _running[name] = th
            th.start()
    if moves:
        S.event("llm", text="; ".join(moves)[:300])
    return ([f"новых review: {added}"] if added else []) + moves


def state() -> dict:
    return {"llm": pool(), "jobs": load_jobs()[-25:]}


def main(argv=None):
    p = argparse.ArgumentParser(prog="llm_pool")
    p.add_argument("cmd", nargs="?", choices=["status", "ask"])
    p.add_argument("who", nargs="?")
    p.add_argument("prompt", nargs="?")
    p.add_argument("--task")
    p.add_argument("--stop", nargs="*", default=None, help="Стоп-последовательности (напр. '\\n')")
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", type=int)
    a = p.parse_args(argv)
    if a.cmd == "ask":
        if not (a.who and a.prompt):
            p.error("ask <кто> \"<текст>\"")
        with JobsLock():
            jobs = load_jobs()
            job = new_job("ask", a.who, prompt=a.prompt, task=a.task, reply_to=[a.who], stop=a.stop)
            jobs.append(job)
            save_jobs(jobs)
        print(f"{job['id']}: в очереди; ответ придёт в inbox/{a.who}.md")
        return 0
    if a.cmd == "status":
        for n, c in refresh().items():
            st = c.get("status", {})
            print(f"{n:8} {'в сети' if st.get('online') else 'нет: ' + st.get('why', '?'):40} {c['host']}")
        for j in load_jobs()[-10:]:
            print(f"{j['id']} {j['kind']:6} {j.get('task') or '-':5} {j['state']:9} {j.get('llm') or '-'}")
        return 0
    if not any(c.get("status", {}).get("online") for c in refresh().values()) and a.once:
        print("llm_pool: ни одна модель не в сети", file=sys.stderr)
        return 2
    while True:
        for m in tick():
            print(m)
        if not a.loop:
            for th in list(_running.values()):
                th.join()
            return 0
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
