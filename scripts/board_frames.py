#!/usr/bin/env python3
"""board_frames — доска и агенты как STATE/DELTA профиля кластера s:2 (T38, фаза 2 транспорта).

agent_sync (единственный источник — длинные фреймы пишет программа, не модель)
раз в минуту снимает доску: задачи → task-строки, агенты → agent-строки.
Первый снимок и каждый KEYFRAME_EVERY-й — полный STATE, остальные — DELTA
только с изменёнными строками (+#…) и удалёнными (-#…). Каждый фрейм перед
записью проходит AMLCodec.encode (без потерь), validate_frame и приёмник
Receiver с согласованным профилем s:2 — тем же, что будет у потребителя.

Лента: .agent-sync/frames/board.aml; состояние и счётчики экономии против
JSON того же сообщения — .agent-sync/frames/board_state.json.

  PY scripts/board_frames.py publish     # один снимок (serve делает сам)
  PY scripts/board_frames.py stats       # экономия байт/токенов с начала ленты
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_sync as S  # noqa: E402
import nam  # noqa: E402
from aml_codec import CLUSTER_SCHEMA, AMLCodec  # noqa: E402
from frame_validation import validate_frame  # noqa: E402

SESSION, SENDER, RECIPIENT, WORLD = "aml-nam", "agent_sync", "board", "board"
KEYFRAME_EVERY = 30          # полный снимок раз в ~30 публикаций (≈ полчаса)
STATUS = {"claude": "approval"}                     # статус приёмки на доске → словарь s:2
AGENT_STATE = {"active": "active", "stale": "stale", "watching": "watching", "offline": "offline"}


def _paths() -> tuple[Path, Path]:
    d = S.SYNC / "frames"
    d.mkdir(exist_ok=True)
    return d / "board.aml", d / "board_state.json"


def snapshot(now_ms: int) -> dict:
    """Текущие строки s:2 по id: задачи и облачные агенты."""
    s = S.state_json()
    rows = {}
    for t in s["tasks"]:
        st = STATUS.get(t["status"], t["status"])
        if st not in ("backlog", "in_progress", "review", "approval", "done", "closed"):
            continue
        rows[t["id"]] = {"id": t["id"], "kind": "task", "status": st,
                         "assignee": t.get("assignee") or "none", "critic": t.get("critic") or "none",
                         "priority": max(0, min(99, t["priority"])), "observed_ms": now_ms,
                         "depends": list(t.get("depends") or [])}
    for name, a in s["agents"].items():
        row = {"id": name, "kind": "agent", "state": AGENT_STATE.get(a["computed"]["state"], "offline"),
               "limit": a.get("limit"), "weekly": a.get("weekly"), "observed_ms": now_ms}
        task = a.get("task")
        if task and task != "-" and task.lower() != "none":
            row["task"] = task
        rows[name] = row
    return rows


def _same(a: dict, b: dict) -> bool:
    strip = lambda r: {k: v for k, v in r.items() if k != "observed_ms"}
    return strip(a) == strip(b)


def envelope(seq: int, now_ms: int, kind: str, body: dict) -> dict:
    return {"v": "NAM/2", "id": f"m{seq}", "session": SESSION, "sender": SENDER, "recipient": RECIPIENT,
            "seq": seq, "sent_ms": now_ms, "ttl_ms": 60000, "kind": kind, "schema": CLUSTER_SCHEMA, "body": body}


def receiver() -> nam.Receiver:
    """Приёмник потребителя: профиль s:2 согласован через hello."""
    rx = nam.Receiver(SESSION, RECIPIENT, {SENDER: {"hello", "state", "delta"}}, schemas=[CLUSTER_SCHEMA])
    hello = {"v": "NAM/2", "id": "m0", "session": SESSION, "sender": SENDER, "recipient": RECIPIENT, "seq": 0,
             "sent_ms": int(time.time() * 1000), "ttl_ms": 60000, "kind": "hello",
             "body": {"versions": ["NAM/2"], "actions": [], "schemas": [CLUSTER_SCHEMA]}}
    rx.accept(nam.encode(hello), SENDER, hello["sent_ms"])
    return rx


def _tokens(text: str) -> int | None:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return None


_RX: nam.Receiver | None = None     # контрольный подписчик рабочего цикла (serve), T38 F3
_PUB_LOCK = threading.RLock()       # плановая публикация, KEYFRAME и ротация (T43) — reentrant


def _reconcile(feed: Path, st: dict) -> bool:
    """Сверка ленты и состояния (T38 F2, T43). Возвращает True, если нужен полный STATE.

    Порядок публикации — запись в ленту, затем board_state.json. Если второе
    упало, в ленте есть фрейм новее состояния: берём seq/rev из ленты и
    публикуем полный снимок (его rev больше, непрерывный Receiver его примет).
    Недописанный хвост ленты (сбой посреди записи) обрезается до последнего
    целого фрейма, чтобы следующий фрейм не склеился с мусором.
    При ротации ленты (пустая/новая лента или seq ленты отстаёт от состояния)
    также форсируется полный STATE."""
    if not feed.exists() or feed.stat().st_size == 0:
        if st.get("rev", 0) > 0:
            st["rows"] = {}
            return True
        return False
    with feed.open("rb+") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 65536))
        tail = f.read()
        base = size - len(tail)
        end = max(tail.rfind(b"\n\n"), tail.rfind(b"\r\n\r\n"))
        step = 4 if end >= 0 and tail[end:end + 4] == b"\r\n\r\n" else 2
        whole = base + end + step if end >= 0 else base
        if whole < size and (end >= 0 or base == 0):
            f.truncate(whole)
            tail = tail[:max(end + step, 0)]
    frames = [x for x in tail.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n") if x.startswith("!AML:2|")]
    if not frames:
        if st.get("rev", 0) > 0:
            st["rows"] = {}
            return True
        return False
    last = AMLCodec.decode(frames[-1])
    if last["seq"] > st["seq"]:
        st["seq"], st["rev"], st["rows"] = last["seq"], last["body"]["rev"], {}
        return True
    if last["seq"] < st["seq"]:
        st["rows"] = {}
        return True
    return False


def publish(now_ms: int | None = None, rx: nam.Receiver | None = None, verify: bool = False,
            force_state: bool = False) -> str | None:
    """Публикует STATE или DELTA, если доска изменилась. Возвращает вид фрейма или None.

    rx — приёмник, которому фрейм обязан подойти до записи в ленту (тесты);
    verify=True — то же с постоянным подписчиком рабочего цикла (serve);
    force_state=True — полный снимок вне очереди (QUERY ?KEYFRAME, T42)."""
    with _PUB_LOCK:
        return _publish(now_ms, rx, verify, force_state)


def _publish(now_ms, rx, verify, force_state) -> str | None:
    global _RX
    now_ms = now_ms or int(time.time() * 1000)
    feed, state_fp = _paths()
    st = S.load(state_fp) if state_fp.exists() else {"seq": 0, "rev": 0, "rows": {}, "stats": {}}
    force = _reconcile(feed, st) or force_state
    if verify and _RX is None:
        _RX, force = receiver(), True      # новый подписчик понимает только полный снимок
    rx = _RX if verify else rx
    rows = snapshot(now_ms)
    old = st["rows"]
    changed = [r for i, r in rows.items() if i not in old or not _same(r, old[i])]
    removed = [i for i in old if i not in rows]
    if st["rev"] and not changed and not removed and not force:
        return None
    seq, rev = st["seq"] + 1, st["rev"] + 1
    if force or not st["rev"] or rev % KEYFRAME_EVERY == 0 or len(rows) > 128:
        kind, body = "state", {"world": WORLD, "rev": rev, "entities": list(rows.values())}
    else:
        kind, body = "delta", {"world": WORLD, "base": st["rev"], "rev": rev,
                               "upsert": changed, "updates": [], "remove": removed}
    msg = envelope(seq, now_ms, kind, body)
    wire = AMLCodec.encode(msg)                        # бросит, если что-то не выразимо без потерь
    as_json = nam.encode(json.loads(json.dumps(msg)))
    validate_frame(wire)
    if rx is not None:                                 # фрейм обязан подойти подписчику до записи
        try:
            rx.accept(json.dumps(AMLCodec.decode(wire)), SENDER, now_ms)
        except nam.ProtocolError:
            if verify:
                _RX = None                             # рассинхрон — следующий проход начнёт с STATE
            raise
    with feed.open("a", encoding="utf-8", newline="\n") as f:
        f.write(wire + "\n\n")
    stats = st.get("stats", {})
    for key, a, j in (("bytes", len(wire.encode()), len(as_json.encode())),
                      ("tokens", _tokens(wire), _tokens(as_json))):
        if a is not None and j is not None:
            stats[key + "_aml"] = stats.get(key + "_aml", 0) + a
            stats[key + "_json"] = stats.get(key + "_json", 0) + j
    stats[kind] = stats.get(kind, 0) + 1
    S.save(state_fp, {"seq": seq, "rev": rev, "rows": rows, "stats": stats})
    return kind


def stats_text() -> str:
    _, state_fp = _paths()
    st = S.load(state_fp).get("stats", {}) if state_fp.exists() else {}
    out = [f"STATE: {st.get('state', 0)}, DELTA: {st.get('delta', 0)}"]
    for key in ("bytes", "tokens"):
        a, j = st.get(key + "_aml"), st.get(key + "_json")
        if a and j:
            out.append(f"{key}: AML {a} против JSON {j} (−{round(100 - 100 * a / j)} %)")
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="board_frames")
    p.add_argument("cmd", choices=["publish", "stats"])
    a = p.parse_args(argv)
    print(publish() or "без изменений" if a.cmd == "publish" else stats_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
