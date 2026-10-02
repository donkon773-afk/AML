#!/usr/bin/env python3
"""agent_sync — координация трёх агентов aml-nam (claude / codex / antigravity).

Только stdlib. Один источник правды — файлы в .agent-sync/:
  board.json   задачи (T<N>), поля: id, title, status, assignee, critic, home,
               depends, summary, notes, updated_at, updated_by
  agents.json  реестр агентов: роль, запасные, пороги, пульс, лимит
  log.md       только дописывать; history/<id>.md — архив длинных notes

Команды (запуск из корня репо, python из AGENTS.md):
  status                        сводка агентов и доски (≤25 строк)
  brief <agent>                 сжатый бриф для агента: его задачи, очередь
                                критики, непрочитанная почта, хвост лога
  heartbeat <agent> [--limit N] [--weekly N] [--resets ISO] [--task T]
                    [--state active|watching|paused] [--note TEXT]
  ack <agent>                   отметить почту прочитанной
  rebalance                     собрать лимиты, поставить на паузу / вернуть,
                                перераспределить задачи (идемпотентно)
  watch <agent> [--every S]     фоновое наблюдение: блокирует, пока лимит
                                агента < resume_at; выход 0 = можно работать
  set <T> key=value ...         изменить поля задачи (перечитывает файл)
  compact                       длинные notes → history/, ротация log.md
  spend [--hours 24|--since ISO] [--json] [--no-tokens]
                                фактический расход агентов по задачам: % лимитов
                                по пульсам + токены Claude/Codex, ≈$ (T45)
  serve [--port 5758]           дашборд + /api/state + авто-rebalance раз в 60 с
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / ".agent-sync"
BOARD = SYNC / "board.json"
AGENTS = SYNC / "agents.json"
LOG = SYNC / "log.md"
INBOX = SYNC / "inbox"
HISTORY = SYNC / "history"
ARCHIVE = SYNC / "archive"
EVENTS = SYNC / "events.jsonl"
DECISIONS = SYNC / "decisions.md"       # вопросы пользователю — очередь на утро
STOP_LINE = SYNC / "STOP_LINE.md"       # verify.py красный на HEAD — чинить до любых задач
LOCK = SYNC / ".lock"
READ_MARK = "<!-- read "
BUS_POLL_S = 0.5                        # опрос outbox шины AML: p95 outbox→log ≤ 2 с (SCENARIO §5)
OPEN = ("backlog", "in_progress")
TZ = dt.timezone(dt.timedelta(hours=3))
# serve работает под pythonw (без консоли): каждый дочерний git/python/npm без
# этого флага открывает на Windows своё окно, и оно мигает на экране.
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}
# Для дочернего Python берём консольный python.exe: с NO_WINDOW он получает скрытую
# консоль, и её наследуют все его потомки (git, npm, node, тесты ci.py). Под pythonw
# у ребёнка консоли нет, и каждый его потомок снова открыл бы своё окно.
CONSOLE_PY = str(Path(sys.executable).with_name("python.exe")) \
    if Path(sys.executable).name.lower() == "pythonw.exe" else sys.executable

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def now() -> dt.datetime:
    return dt.datetime.now(TZ).replace(microsecond=0)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).isoformat()


def parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=TZ)
    except ValueError:
        return None


# ---------- файлы с блокировкой ----------

_THREAD_LOCK = threading.RLock()
_LOCK_DEPTH = 0
_LOCK_FD = None


class Lock:
    def __enter__(self):
        global _LOCK_DEPTH, _LOCK_FD
        _THREAD_LOCK.acquire()
        if _LOCK_DEPTH == 0:
            for _ in range(100):
                try:
                    self.fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    _LOCK_FD = self.fd
                    _LOCK_DEPTH = 1
                    return self
                except (FileExistsError, PermissionError):
                    try:  # зависшая блокировка старше 30 с
                        if time.time() - LOCK.stat().st_mtime > 30:
                            LOCK.unlink()
                    except OSError:
                        pass
                    time.sleep(0.05)
            _THREAD_LOCK.release()
            raise SystemExit("agent_sync: .agent-sync/.lock занят")
        else:
            _LOCK_DEPTH += 1
            self.fd = _LOCK_FD
            return self

    def __exit__(self, *exc):
        global _LOCK_DEPTH, _LOCK_FD
        try:
            _LOCK_DEPTH -= 1
            if _LOCK_DEPTH == 0:
                if _LOCK_FD is not None:
                    try:
                        os.close(_LOCK_FD)
                    except OSError:
                        pass
                    _LOCK_FD = None
                try:
                    LOCK.unlink()
                except OSError:
                    pass
        finally:
            _THREAD_LOCK.release()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Windows: os.replace падает с PermissionError, пока файл открыт на чтение
    # другим процессом (дашборд, brief другого агента) — повторяем до ~5 с.
    for attempt in range(100):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.05)


def append_log(agent: str, topic: str, text: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n## {now():%Y-%m-%d %H:%M} — {agent} — {topic}\n{text.strip()}\n")


def event(kind: str, **kw) -> None:
    with Lock():
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso(), "kind": kind, **kw}, ensure_ascii=False) + "\n")


# ---------- источники лимитов ----------

def codex_limits() -> dict | None:
    """Последний rate_limits из rollout-файлов Codex (~/.codex/sessions)."""
    base = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    # Codex дописывает и старые сессии, поэтому mtime файла не говорит о
    # свежести лимитов: берём запись с самым поздним timestamp среди недавних.
    files = sorted(glob.glob(str(base / "*" / "*" / "*" / "rollout-*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:8]
    best = None
    for fp in files:
        with open(fp, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rl = (rec.get("payload") or {}).get("rate_limits") or {}
            out, t = {}, time.time()
            for key, name in (("primary", "limit"), ("secondary", "weekly")):
                w = rl.get(key) or {}
                if "used_percent" not in w:
                    continue
                reset = w.get("resets_at")
                used = 0.0 if reset and reset < t else float(w["used_percent"])
                out[name] = round(100 - used)
                if reset:
                    out[name + "_resets"] = iso(dt.datetime.fromtimestamp(reset, TZ))
            if out:
                out["seen"] = rec.get("timestamp") or ""
                out["source"] = "codex rollout"
                if best is None or out["seen"] > best["seen"]:
                    best = out
            break  # в файле нужна только последняя запись
    return best


GLOG = re.compile(r"([IWEF])(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})")
QUOTA = re.compile(r"RESOURCE_EXHAUSTED|quota|rate.?limit|\b429\b", re.I)


def antigravity_signal() -> dict | None:
    """Косвенный сигнал: свежие ошибки квоты в language_server.log."""
    fp = Path(os.environ.get("APPDATA", "")) / "Antigravity" / "logs" / "language_server.log"
    if not fp.exists():
        return None
    with open(fp, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 200_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    last_quota = None
    for line in lines:
        m = GLOG.search(line)
        if m and m.group(1) in "WEF" and QUOTA.search(line):
            y = now().year
            last_quota = dt.datetime(y, int(m.group(2)), int(m.group(3)), int(m.group(4)),
                                     int(m.group(5)), int(m.group(6)), tzinfo=TZ)
    alive = dt.datetime.fromtimestamp(fp.stat().st_mtime, TZ)
    return {"app_alive": iso(alive.replace(microsecond=0)),
            "quota_error": iso(last_quota) if last_quota else None}


# ---------- состояние агентов ----------

def effective(agents: dict, name: str) -> dict:
    """Вычисляет state агента: active / watching / paused / stale / offline."""
    a = agents["agents"][name]
    cfg = agents["policy"]
    hb = parse(a.get("heartbeat"))
    age = (now() - hb).total_seconds() / 60 if hb else None
    lim = a.get("limit")
    wk = a.get("weekly")
    low = min(x for x in (lim, wk, 100) if x is not None)
    if a.get("state") == "paused" or low < cfg["pause_below"]:
        state = "watching"  # пауза держится до resume_at (гистерезис), см. collect()
    elif age is None or age > cfg["offline_after_min"]:
        state = "offline"
    elif age > cfg["stale_after_min"]:
        state = "stale"
    else:
        state = "active"
    return {"state": state, "age_min": None if age is None else round(age), "low": low}


def collect(agents: dict) -> list[str]:
    """Подтягивает автоматические источники лимитов; возвращает изменения."""
    changes = []
    cx = codex_limits()
    a = agents["agents"]["codex"]
    if cx:
        if any(k in cx and cx[k] != a.get(k) for k in ("limit", "weekly")):
            # замер для учёта расхода (spend.py): codex не шлёт --limit сам
            event("limits", agent="codex", limit=cx.get("limit"), weekly=cx.get("weekly"))
        for k in ("limit", "weekly", "limit_resets", "weekly_resets"):
            if k in cx:
                a[k] = cx[k]
        a["limit_source"] = cx["source"]
        a["limit_seen"] = cx.get("seen")
    ag = antigravity_signal()
    g = agents["agents"]["antigravity"]
    if ag:
        g["app_alive"] = ag["app_alive"]
        qe = parse(ag["quota_error"])
        hb = parse(g.get("heartbeat"))
        if qe and (not hb or qe > hb) and g.get("limit", 100) > 0:
            g["limit"] = 0
            g["limit_source"] = "antigravity log: quota error " + ag["quota_error"]
            changes.append(f"antigravity: ошибка квоты в логе {ag['quota_error']}")
    # пауза/возврат по порогам
    pol = agents["policy"]
    for name, a in agents["agents"].items():
        low = min(x for x in (a.get("limit"), a.get("weekly"), 100) if x is not None)
        if a.get("state") != "paused" and low < pol["pause_below"]:
            a["state"] = "paused"
            a["paused_at"] = iso()
            changes.append(f"{name}: лимит {low}% < {pol['pause_below']}% → пауза, фоновое наблюдение")
        elif a.get("state") == "paused" and low >= pol["resume_at"]:
            a["state"] = "active"
            a["resumed_at"] = iso()
            changes.append(f"{name}: лимит {low}% ≥ {pol['resume_at']}% → возврат в работу")
    return changes


def available(agents: dict, name: str) -> bool:
    return effective(agents, name)["state"] in ("active", "stale")


def can_take(agents: dict, name: str) -> bool:
    """Может ли агент получать задачи на исполнение. Недельный остаток ниже
    policy.critic_only_weekly_below (AUTONOMY §3) — только критика."""
    wk = agents["agents"][name].get("weekly")
    floor = agents["policy"].get("critic_only_weekly_below", 25)
    return available(agents, name) and (wk is None or wk >= floor)


def pick(agents: dict, candidates: list[str], exclude: set[str], take: bool = False) -> str | None:
    ok = can_take if take else available
    for c in candidates:
        if c not in exclude and ok(agents, c):
            return c
    return None


def rebalance(verbose: bool = True) -> list[str]:
    with Lock():
        agents = load(AGENTS)
        board = load(BOARD)
        moves = collect(agents)
        for t in board["tasks"]:
            st, who = t.get("status"), t.get("assignee")
            home = t.get("home") or who
            critic = t.get("critic") or agents["agents"].get(home, {}).get("default_critic", "codex")
            # 1) вернуть задачу хозяину, если он вернулся и заместитель её не начал
            if st == "backlog" and t.get("home") and t["home"] != who and can_take(agents, t["home"]):
                moves.append(f"{t['id']}: {who} → {t['home']} (хозяин вернулся)")
                t["assignee"] = t.pop("home")
                who = t["assignee"]
            # 2) исполнитель недоступен → запасной
            if st in OPEN and who in agents["agents"] and not available(agents, who):
                sub = pick(agents, agents["agents"][who]["fallback"], {who, critic}, take=True)
                if sub:
                    t.setdefault("home", who)
                    t["assignee"] = sub
                    moves.append(f"{t['id']}: {who} → {sub} ({effective(agents, who)['state']})")
                    who = sub
            # 3) критик недоступен или совпал с исполнителем → запасной критик;
            #    назначенный критик вернулся → задача снова у него (critic_home)
            if st in ("review", *OPEN):
                # авторы задачи — исполнитель и исходный хозяин (home): их работу они не критикуют
                authors = {who, t.get("home")} - {None}
                ch = t.get("critic_home")
                if ch and ch not in authors and available(agents, ch):
                    if ch != critic:
                        moves.append(f"{t['id']}: критик {critic} → {ch} (назначенный критик вернулся)")
                    critic = ch
                    t.pop("critic_home")
                elif critic in authors or (st == "review" and not available(agents, critic)):
                    alt = pick(agents, agents["policy"]["critic_order"], authors)
                    if alt and alt != critic:
                        t.setdefault("critic_home", critic)
                        moves.append(f"{t['id']}: критик {critic} → {alt}")
                        critic = alt
                    elif not alt and critic in authors and ch and ch not in authors:
                        moves.append(f"{t['id']}: критик {critic} — автор задачи → ждём {ch}")
                        critic = ch          # лучше подождать критика, чем проверять самому себя
                        t.pop("critic_home")
            t["critic"] = critic
        if moves:
            for name in agents["agents"]:
                agents["agents"][name]["computed"] = effective(agents, name)
            save(AGENTS, agents)
            board["updated_at"] = iso()
            save(BOARD, board)
            for m in moves:
                event("rebalance", text=m)
            append_log("agent_sync", "rebalance", "\n".join("- " + m for m in moves))
        else:
            save(AGENTS, agents)
    if verbose:
        print("\n".join(moves) if moves else "rebalance: без изменений")
    return moves


# ---------- представления ----------

def prio(t: dict) -> int:
    """Приоритет задачи как число: set пишет строки, старые записи — числа."""
    try:
        return int(t.get("priority"))
    except (TypeError, ValueError):
        return 9


def short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def unread(agent: str) -> str:
    fp = INBOX / f"{agent}.md"
    if not fp.exists():
        return ""
    txt = fp.read_text(encoding="utf-8")
    i = txt.rfind(READ_MARK)
    if i >= 0:
        txt = txt[txt.find("\n", i) + 1:]
    return txt.strip()


def log_tail(n: int) -> list[str]:
    heads = [l for l in LOG.read_text(encoding="utf-8").splitlines() if l.startswith("## ")]
    return heads[-n:]


# ---------- готовность проекта ----------

# Доля готовности задачи по статусу: работа засчитывается частично, пока не принята.
STATUS_DONE = {"backlog": 0.0, "in_progress": 0.3, "review": 0.7, "claude": 0.9, "done": 1.0, "closed": 1.0}


def readiness(tasks: list, milestones: list) -> dict:
    """Взвешенная готовность: сумма вес*доля / сумма весов по задачам с полем weight."""
    out = []
    for m in milestones:
        own = [t for t in tasks if t.get("milestone") == m["id"] and t.get("weight")]
        w = sum(t["weight"] for t in own)
        got = sum(t["weight"] * STATUS_DONE.get(t.get("status"), 0) for t in own)
        out.append({**m, "weight": w, "pct": round(100 * got / w) if w else 0,
                    "tasks": [t["id"] for t in own]})
    total = sum(m["weight"] for m in out)
    pct = round(sum(m["weight"] * m["pct"] for m in out) / total) if total else 0
    return {"pct": pct, "milestones": out}


def state_json() -> dict:
    agents = load(AGENTS)
    board = load(BOARD)
    for name in agents["agents"]:
        agents["agents"][name]["computed"] = effective(agents, name)
        agents["agents"][name]["unread"] = len(re.findall(r"^## ", unread(name), re.M))
    events = []
    if EVENTS.exists():
        events = [json.loads(l) for l in EVENTS.read_text(encoding="utf-8").splitlines()[-30:] if l.strip()]
    tasks = []
    for t in board["tasks"]:
        tasks.append({k: t.get(k) for k in ("id", "title", "status", "assignee", "critic", "home",
                                             "depends", "updated_at", "updated_by", "milestone", "weight")}
                     | {"priority": prio(t), "summary": t.get("summary") or short(t.get("notes", ""), 300)})
    jobs_fp = SYNC / "llm_jobs.json"
    jobs = load(jobs_fp)["jobs"][-25:] if jobs_fp.exists() else []
    return {"now": iso(), "project": board.get("project"), "agents": agents["agents"],
            "policy": agents["policy"], "tasks": tasks, "log": log_tail(25), "events": events,
            "llm": agents.get("llm", {}), "jobs": jobs,
            "readiness": readiness(board["tasks"], board.get("milestones", [])),
            "decisions": len(re.findall(r"^## ", DECISIONS.read_text(encoding="utf-8"), re.M))
            if DECISIONS.exists() else 0,
            "stop_line": STOP_LINE.read_text(encoding="utf-8").strip() if STOP_LINE.exists() else ""}


def cmd_status(_):
    s = state_json()
    r = s["readiness"]
    print(f"Готовность проекта: {r['pct']}%  (" + ", ".join(f"{m['title']} {m['pct']}%" for m in r["milestones"]) + ")")
    for n, a in s["agents"].items():
        c = a["computed"]
        lim = "?" if a.get("limit") is None else f"{a['limit']}%"
        wk = "" if a.get("weekly") is None else f" нед {a['weekly']}%"
        print(f"{n:12} {c['state']:8} лимит {lim}{wk}  пульс {c['age_min']} мин  задача {a.get('task') or '-'}  почта {a['unread']}")
    for t in s["tasks"]:
        if t["status"] not in ("done", "closed"):
            print(f"{t['id']:4} {t['status']:11} {t['assignee'] or '-':11} критик {t['critic'] or '-':11} {short(t['title'], 70)}")


def cmd_brief(args):
    """Сжатый контекст: вместо чтения board.json/log.md целиком."""
    s = state_json()
    me = args.agent
    a = s["agents"][me]
    c = a["computed"]
    out = [f"# brief {me} {s['now']} — роль: {a['role']}; состояние {c['state']}, лимит {a.get('limit', '?')}%"]
    if c["state"] == "watching":
        out.append("! ЛИМИТ НИЖЕ ПОРОГА: не бери работу. Запусти `agent_sync.py watch "
                   f"{me}` и продолжай после выхода 0.")
    mine = [t for t in s["tasks"] if t["assignee"] == me and t["status"] in OPEN]
    rev = [t for t in s["tasks"] if t["critic"] == me and t["status"] == "review"]
    acc = [t for t in s["tasks"] if t["status"] == "claude" and me == "claude"]
    done = {t["id"] for t in s["tasks"] if t["status"] in ("done", "closed")}
    for title, lst in (("Мои задачи", mine), ("Жду моей критики", rev), ("Жду приёмки", acc)):
        if lst:
            out.append(f"## {title}")
            for t in sorted(lst, key=lambda t: (prio(t), t["id"])):
                deps = [d for d in (t.get("depends") or []) if d not in done]
                blk = f" [ждёт {','.join(deps)}]" if deps else ""
                out.append(f"- {t['id']} [{t['status']}]{blk} {t['title']}\n  {short(t['summary'], args.width)}")
    mail = unread(me)
    if mail:
        out.append("## Почта (непрочитанное, `ack` после чтения)")
        out.append(short(mail, 1500) if len(mail) > 1500 else mail)
    out.append("## Лог (последние заголовки)")
    out += s["log"][-6:]
    others = [f"{n}:{x['computed']['state']}/{x.get('limit', '?')}%" for n, x in s["agents"].items() if n != me]
    out.append("Команда: " + ", ".join(others))
    print("\n".join(out))


def cmd_heartbeat(args):
    with Lock():
        agents = load(AGENTS)
        a = agents["agents"][args.agent]
        a["heartbeat"] = iso()
        for k in ("limit", "weekly"):
            v = getattr(args, k)
            if v is not None:
                a[k] = int(v)
                a["limit_source"] = "self-report"
        if args.resets:
            a["limit_resets"] = args.resets
        if args.task is not None:
            a["task"] = args.task or None
        if args.state:
            a["state"] = args.state
        if args.note is not None:
            a["note"] = short(args.note, 200)
        save(AGENTS, agents)
    a = agents["agents"][args.agent]
    event("heartbeat", agent=args.agent, limit=a.get("limit"), weekly=a.get("weekly"), task=a.get("task"))
    rebalance(verbose=False)
    print(f"heartbeat {args.agent}: {effective(load(AGENTS), args.agent)}")


def cmd_ack(args):
    fp = INBOX / f"{args.agent}.md"
    with fp.open("a", encoding="utf-8") as f:
        f.write(f"\n{READ_MARK}{iso()} -->\n")
    print("ok")


def actionable(s: dict, me: str) -> list:
    """Что агенту делать сейчас, по убыванию важности (ночной протокол, docs/AUTONOMY.md)."""
    done = {t["id"] for t in s["tasks"] if t["status"] in ("done", "closed")}
    tasks = sorted(s["tasks"], key=lambda t: (t["priority"], t["id"]))
    items = []
    if s["stop_line"]:
        items.append("СТОП-ЛИНИЯ (чинить первым): " + short(s["stop_line"], 300))
    items += [f"критика {t['id']}: {t['title']}" for t in tasks if t["status"] == "review" and t["critic"] == me]
    if me == "claude":
        items += [f"приёмка {t['id']}: {t['title']}" for t in tasks if t["status"] == "claude"]
    if unread(me):
        items.append("почта: есть непрочитанное (brief, затем ack)")
    items += [f"доделать {t['id']}: {t['title']}" for t in tasks
              if t["status"] == "in_progress" and t["assignee"] == me]
    items += [f"взять {t['id']}: {t['title']}" for t in tasks
              if t["status"] == "backlog" and t["assignee"] == me
              and all(d in done for d in (t.get("depends") or []))]
    return items


def cmd_next(args):
    """Следующее дело агента. С --wait блокирует (токены не тратятся), пока дело не появится."""
    deadline = time.time() + (args.wait or 0)
    while True:
        s = state_json()
        if s["agents"][args.agent].get("state") == "paused":
            print(f"{args.agent}: пауза по лимиту — запусти `agent_sync.py watch {args.agent}`")
            return 4
        items = actionable(s, args.agent)
        if items:
            print("\n".join(items[:5]))
            return 0
        if time.time() >= deadline:
            print(f"{args.agent}: дел нет" + (f" (ждал {args.wait} с)" if args.wait else ""))
            return 3
        time.sleep(30)


def cmd_decide(args):
    """Вопрос пользователю: в очередь решений на утро; работа идёт дальше по другим задачам."""
    with DECISIONS.open("a", encoding="utf-8") as f:
        f.write(f"\n## {now():%Y-%m-%d %H:%M} — {args.agent} — {args.task or 'общее'}\n{args.question.strip()}\n")
    event("decision", text=f"{args.agent}: {short(args.question, 120)}")
    print("вопрос записан в .agent-sync/decisions.md")


def cmd_report(args):
    """Утренний отчёт: что сделано с --since, готовность, вопросы пользователю."""
    import subprocess
    since = args.since or (now() - dt.timedelta(hours=10)).isoformat()
    s = state_json()
    r = s["readiness"]
    lines = [f"# Отчёт автономной работы — {now():%Y-%m-%d %H:%M}", "",
             f"Период: с {since}. **Готовность проекта: {r['pct']}%**", ""]
    lines += [f"- {m['title']}: {m['pct']}% ({', '.join(m['tasks'])})" for m in r["milestones"]]
    changed = [t for t in load(BOARD)["tasks"] if (t.get("updated_at") or "") >= since]
    lines += ["", "## Задачи, изменённые за период", ""]
    lines += [f"- {t['id']} [{t['status']}] {t['assignee']}: {t['title']}" for t in changed] or ["- нет"]
    log = subprocess.run(["git", "log", f"--since={since}", "--format=- %h %an: %s"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8", **NO_WINDOW).stdout.strip()
    lines += ["", "## Коммиты", "", log or "- нет"]
    jobs = [j for j in s["jobs"] if (j.get("finished") or "") >= since]
    lines += ["", f"## Локальные модели: выполнено заданий {len(jobs)}", ""]
    lines += [f"- {j['id']} {j['kind']} {j.get('task') or ''} — {j['llm']}, {j.get('secs')} с" for j in jobs]
    lines += ["", "## Ждёт решения пользователя", "",
              DECISIONS.read_text(encoding="utf-8").strip() if DECISIONS.exists() else "- нет"]
    try:
        lines += ["", "## Расход агентов (T45)", "", "```", spend_text(parse(since)), "```"]
    except Exception as e:  # отчёт не падает из-за чужих журналов
        lines += ["", f"Расход агентов: ошибка {e}"]
    lines += ["", "## Агенты сейчас", ""]
    lines += [f"- {n}: {a['computed']['state']}, лимит {a.get('limit', '?')}%" for n, a in s["agents"].items()]
    fp = ROOT / "review" / f"{now():%Y-%m-%d}" / "NIGHT_REPORT.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(fp.relative_to(ROOT).as_posix())


def spend_summary(since: dt.datetime, tokens: bool = True) -> dict:
    import spend
    pol = load(AGENTS)["policy"]
    return spend.collect(EVENTS, ARCHIVE, since, window_min=pol.get("offline_after_min", 60),
                         tokens=tokens, prices=pol.get("prices"))


def spend_text(since: dt.datetime) -> str:
    import spend
    return spend.render(spend_summary(since))


def cmd_spend(args):
    """Фактический расход агентов: % лимитов по пульсам + токены из журналов Claude/Codex (T45)."""
    since = parse(args.since) if args.since else now() - dt.timedelta(hours=args.hours)
    if since is None:
        raise SystemExit(f"spend: не разобрал --since {args.since!r}")
    if args.json:
        s = spend_summary(since, tokens=not args.no_tokens)
        print(json.dumps(s, ensure_ascii=False, indent=1, default=str))
    else:
        import spend
        print(spend.render(spend_summary(since, tokens=not args.no_tokens), top=args.top))


def cmd_watch(args):
    """Пауза + фоновое наблюдение: ждёт, пока лимит агента не восстановится."""
    pol = load(AGENTS)["policy"]
    while True:
        rebalance(verbose=False)
        agents = load(AGENTS)
        a = agents["agents"][args.agent]
        low = effective(agents, args.agent)["low"]
        if a.get("state") != "paused" and low >= pol["resume_at"]:
            print(f"watch {args.agent}: лимит {low}% — можно работать")
            return 0
        resets = a.get("limit_resets") or "?"
        print(f"watch {args.agent}: лимит {low}% (< {pol['resume_at']}%), сброс {resets}; жду {args.every} с", flush=True)
        if args.once:
            return 3
        time.sleep(args.every)


def cmd_set(args):
    with Lock():
        board = load(BOARD)
        t = next((x for x in board["tasks"] if x["id"] == args.task), None)
        if t is None:
            t = {"id": args.task, "status": "backlog", "notes": ""}
            board["tasks"].append(t)
        for kv in args.pairs:
            k, _, v = kv.partition("=")
            if k == "notes+":
                t["notes"] = (t.get("notes", "") + " | " + v).strip(" |")
            elif k == "depends":
                t[k] = [x for x in v.split(",") if x]
            elif k in ("priority", "weight"):  # readiness() складывает weight: строка ломает дашборд
                t[k] = int(v)
            else:
                t[k] = v
        t["updated_at"] = iso()
        t["updated_by"] = args.by
        board["updated_at"] = iso()
        save(BOARD, board)
    event("task", task=args.task, by=args.by, change=" ".join(args.pairs)[:200])
    print(f"{args.task}: {t.get('status')} → {t.get('assignee')}")


def cmd_compact(args):
    """Экономия токенов: длинные notes в history/, старый лог в archive/."""
    HISTORY.mkdir(exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)
    moved = 0
    with Lock():
        board = load(BOARD)
        for t in board["tasks"]:
            notes = t.get("notes") or ""
            if len(notes) > args.max_notes:
                with (HISTORY / f"{t['id']}.md").open("a", encoding="utf-8") as f:
                    f.write(f"\n## архив notes {iso()}\n{notes}\n")
                keep = notes.split(" | ")[-1]
                t["notes"] = f"(история: .agent-sync/history/{t['id']}.md) " + short(keep, args.max_notes)
                moved += 1
        save(BOARD, board)
        text = LOG.read_text(encoding="utf-8")
        parts = re.split(r"(?m)^(?=## )", text)
        head, entries = (parts[0], parts[1:]) if not parts[0].startswith("## ") else ("", parts)
        rotated = 0
        if len(text.encode("utf-8")) > args.max_log and len(entries) > args.keep:
            old, new = entries[:-args.keep], entries[-args.keep:]
            fp = ARCHIVE / f"log-{now():%Y%m%d-%H%M}.md"
            fp.write_text("".join(old), encoding="utf-8")
            LOG.write_text(head + f"(ранние записи: .agent-sync/archive/{fp.name})\n\n" + "".join(new), encoding="utf-8")
            rotated = len(old)
    print(f"compact: notes в history — {moved}, записей лога в архив — {rotated}")
    try:
        import rotation
        rotation.rotate_all()
    except Exception as e:
        print("compact rotation:", e, file=sys.stderr)


def cmd_rotate(args):
    """Ротация лент и журналов кластера (T43)."""
    import rotation
    res = rotation.rotate_all(max_bytes=args.max_bytes, force=args.force, dry_run=args.dry_run)
    for k, v in res.items():
        if v:
            print(f"{k}: {', '.join(v)}")
    if not any(res.values()):
        print("rotate: все файлы в пределах лимита")


# ---------- сервер дашборда ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/.agent-sync/dashboard/index.html")
            self.end_headers()
            return
        if path == "/api/task":
            # полная карточка задачи для окна «история» на дашборде (T40)
            from urllib.parse import parse_qs, urlsplit
            tid = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
            t = next((x for x in load(BOARD)["tasks"] if x["id"] == tid), None)
            hist = HISTORY / f"{tid}.md"
            body = json.dumps({"task": t, "history": hist.read_text(encoding="utf-8")[-20000:]
                               if t and hist.exists() else ""}, ensure_ascii=False).encode()
            self.send_response(200 if t else 404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            body = json.dumps(state_json(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if not path.startswith("/.agent-sync/") and not path.startswith("/review/") and not path.startswith("/docs/"):
            self.send_error(403)
            return
        super().do_GET()


def cmd_serve(args):
    def loop():
        tick = 0
        while True:
            tick += 1
            try:
                rebalance(verbose=False)
            except Exception as e:  # сервер не падает из-за одного сбоя
                print("rebalance:", e, file=sys.stderr)
            try:
                import board_frames  # доска как STATE/DELTA s:2 (T38)
                board_frames.publish(verify=True)   # постоянный подписчик Receiver (T38 F3)
            except Exception as e:
                print("board_frames:", e, file=sys.stderr)
            if tick % 5 == 0:
                try:
                    import rotation
                    rotation.rotate_all()
                except Exception as e:
                    print("rotation:", e, file=sys.stderr)
            time.sleep(60)
    def llm_loop():
        import llm_pool  # локальные модели: пробы, раздача заданий, перенос с выпавших (T18)
        while True:
            try:
                llm_pool.tick()
            except Exception as e:
                print("llm_pool:", e, file=sys.stderr)
            time.sleep(30)
    def ci_loop():
        while True:
            try:
                result = subprocess.run([CONSOLE_PY, str(ROOT / "scripts" / "ci.py")],
                                        cwd=ROOT, timeout=600, **NO_WINDOW)
                if result.returncode:
                    print(f"ci: exit {result.returncode}; see .agent-sync/STOP_LINE.md", file=sys.stderr)
            except Exception as e:
                print("ci:", e, file=sys.stderr)
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True).start()
    def bus_loop():
        import aml_bus  # транспорт AML: outbox-фреймы → Receiver → полномочия → доска (T35)
        while True:
            try:
                aml_bus.receive()
            except Exception as e:
                print("aml_bus:", e, file=sys.stderr)
            time.sleep(BUS_POLL_S)
    threading.Thread(target=llm_loop, daemon=True).start()
    threading.Thread(target=bus_loop, daemon=True).start()
    threading.Thread(target=ci_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"http://127.0.0.1:{args.port}/.agent-sync/dashboard/index.html")
    srv.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent_sync")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    n = sub.add_parser("next"); n.add_argument("agent"); n.add_argument("--wait", type=int, default=0); n.set_defaults(fn=cmd_next)
    d = sub.add_parser("decide"); d.add_argument("agent"); d.add_argument("question"); d.add_argument("--task"); d.set_defaults(fn=cmd_decide)
    r = sub.add_parser("report"); r.add_argument("--since"); r.set_defaults(fn=cmd_report)
    b = sub.add_parser("brief"); b.add_argument("agent"); b.add_argument("--width", type=int, default=400); b.set_defaults(fn=cmd_brief)
    h = sub.add_parser("heartbeat"); h.add_argument("agent")
    for k in ("limit", "weekly"):
        h.add_argument("--" + k, type=int)
    for k in ("resets", "task", "state", "note"):
        h.add_argument("--" + k)
    h.set_defaults(fn=cmd_heartbeat)
    a = sub.add_parser("ack"); a.add_argument("agent"); a.set_defaults(fn=cmd_ack)
    sub.add_parser("rebalance").set_defaults(fn=lambda _: rebalance())
    w = sub.add_parser("watch"); w.add_argument("agent"); w.add_argument("--every", type=int, default=300); w.add_argument("--once", action="store_true"); w.set_defaults(fn=cmd_watch)
    s = sub.add_parser("set"); s.add_argument("task"); s.add_argument("pairs", nargs="+"); s.add_argument("--by", default="claude"); s.set_defaults(fn=cmd_set)
    c = sub.add_parser("compact"); c.add_argument("--max-notes", type=int, default=900); c.add_argument("--max-log", type=int, default=30000); c.add_argument("--keep", type=int, default=20); c.set_defaults(fn=cmd_compact)
    rot = sub.add_parser("rotate"); rot.add_argument("--max-bytes", type=int, default=1_000_000); rot.add_argument("--force", action="store_true"); rot.add_argument("--dry-run", action="store_true"); rot.set_defaults(fn=cmd_rotate)
    sp = sub.add_parser("spend"); sp.add_argument("--since"); sp.add_argument("--hours", type=float, default=24)
    sp.add_argument("--top", type=int, default=15); sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-tokens", action="store_true"); sp.set_defaults(fn=cmd_spend)
    v = sub.add_parser("serve"); v.add_argument("--port", type=int, default=5758); v.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
