#!/usr/bin/env python3
"""aml_bus — транспорт AML для кластера агентов, фаза 1 (T35).

Агент пишет фрейм HANDOFF / RES / QUERY / ERR в свой outbox
(.agent-sync/frames/outbox-<отправитель>.aml). Приёмник (agent_sync serve,
раз в 0,5 с — TTL фрейма 60 с) проверяет его цепочкой
validate_frame → Receiver.accept → полномочия по задаче (docs/SCENARIO-
cluster-exchange.md §4.1) и только потом меняет доску. Отказ — письмо
отправителю «ERR <код>: причина» и строка в log.md; доска не меняется.

  PY scripts/aml_bus.py send antigravity handoff T14 COMPLETE --sum "..." \
        --check "verify.py:PASS:105+14" --art review/2026-09-24/LIVE_FRAMES.md
  PY scripts/aml_bus.py send codex res T14 REJ --rs CRITIC_RETURN
  PY scripts/aml_bus.py send claude res T14 ACC
  PY scripts/aml_bus.py send claude query T18 status
  PY scripts/aml_bus.py send claude query ALL keyframe   # полный снимок доски вне очереди (T42)
  PY scripts/aml_bus.py send codex err T36 BUSY
  PY scripts/aml_bus.py receive          # один проход приёмника (serve делает сам)

Только stdlib + кодек этого репо.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_sync as S  # noqa: E402
import nam  # noqa: E402
from aml_codec import AMLCodec  # noqa: E402
from frame_validation import validate_frame  # noqa: E402

SESSION = "aml-nam"
BUS = "agent_sync"               # единственный приёмник и маршрутизатор
TTL_MS = 60_000                  # максимум схемы NAM
AGENTS_RW = {"handoff", "result", "query", "error"}
MODELS_RO = {"result", "query"}  # локальные модели только советуют

KIND = {"handoff": "HANDOFF", "res": "RES", "query": "QUERY", "err": "ERR"}
RES_STATUS = {"ACC": "ACCEPTED", "REJ": "REJECTED", "EXEC": "EXECUTED", "FAIL": "FAILED"}


def frames_dir() -> Path:
    d = S.SYNC / "frames"
    d.mkdir(exist_ok=True)
    return d


def _state_path() -> Path:
    return frames_dir() / "bus_state.json"


def load_state() -> dict:
    p = _state_path()
    return S.load(p) if p.exists() else {"seq_out": {}, "seq_in": {}, "offsets": {}}


def permissions() -> dict:
    agents = S.load(S.AGENTS)
    perms = {n: AGENTS_RW for n in agents["agents"]}
    perms.update({n: MODELS_RO for n in agents.get("llm", {})})
    return perms


# ---------- отправка ----------

def build(sender: str, kind: str, task: str, arg: str | None, opts) -> dict:
    body: dict
    if kind == "handoff":
        arts = []
        for path in opts.art or []:
            data = (S.ROOT / path).read_bytes()
            arts.append({"path": path.replace("\\", "/"), "sha256": hashlib.sha256(data).hexdigest()})
        checks = []
        for c in opts.check or []:
            name, result, evidence = (c.split(":", 2) + ["", ""])[:3]
            checks.append({"name": name, "result": result, "evidence": evidence})
        hs = (arg or "COMPLETE").upper()
        body = {"task": task, "status": hs, "summary": opts.sum or hs,   # схема: summary непустой
                "artifacts": arts, "checks": checks, "next": opts.next or ""}
    elif kind == "res":
        body = {"related": task, "status": RES_STATUS[(arg or "").upper()], "operations": [task]}
        if opts.rs:
            body["reason"] = {"code": opts.rs, "evidence": []}
    elif kind == "query":
        req = {"status": "STATUS", "why": "EXPLAIN", "keyframe": "KEYFRAME"}[(arg or "status").lower()]
        body = {"request": req, "related": "ALL" if req == "KEYFRAME" else task}
    elif kind == "err":
        body = {"code": (arg or "BUSY").upper(), "related": task}
    else:
        raise SystemExit(f"неизвестный вид: {kind}")
    return {"kind": KIND[kind], "body": body}


def send(sender: str, kind: str, task: str, arg: str | None = None, opts=None) -> str:
    opts = opts or argparse.Namespace(art=None, check=None, sum=None, next=None, rs=None)
    spec = build(sender, kind, task, arg, opts)
    std = {"HANDOFF": "handoff", "RES": "result", "QUERY": "query", "ERR": "error"}[spec["kind"]]
    now_ms = int(time.time() * 1000)
    with S.Lock():
        st = load_state()
        seq = st["seq_out"].get(sender, 0) + 1
        st["seq_out"][sender] = seq
        S.save(_state_path(), st)
        msg = {"v": "NAM/1", "id": f"m{seq}", "session": SESSION, "sender": sender, "recipient": BUS,
               "seq": seq, "sent_ms": now_ms, "ttl_ms": TTL_MS, "kind": std, "body": spec["body"]}
        if std == "handoff":
            msg["body"] = {"summary": "", "artifacts": [], "checks": [], "next": "", **msg["body"]}
        wire = AMLCodec.encode(msg)
        with (frames_dir() / f"outbox-{sender}.aml").open("a", encoding="utf-8", newline="\n") as f:
            f.write(wire + "\n\n")
    return wire


# ---------- приём ----------

def split_frames(text: str) -> list[str]:
    parts = re.split(r"(?m)^(?=!AML:2\|)", text)
    return [p.strip() for p in parts if p.strip()]


def receive(now_ms: int | None = None) -> list[str]:
    """Один проход: новые фреймы всех outbox → проверки → доска. Возвращает строки итогов.

    Весь проход — чтение состояния, файлов, применение и запись смещений — идёт
    под S.Lock (реентерабельная). Смещение — позиция в конкретной версии файла:
    без этого приёмник, начавший до ротации outbox, записывал бы старое
    смещение поверх нуля новой ленты и терял новые фреймы (T43 F4, codex)."""
    out = []
    perms = permissions()
    with S.Lock():
        st = load_state()
        for fp in sorted(frames_dir().glob("outbox-*.aml")):
            peer = fp.stem[len("outbox-"):]
            raw = fp.read_bytes()
            off = st["offsets"].get(fp.name, 0)
            if len(raw) < off:
                off = 0
                st["offsets"][fp.name] = 0
            if len(raw) <= off:
                continue
            # Фрейм считается записанным, только когда за ним пустая строка (send пишет
            # wire + "\n\n" одним вызовом). Недописанный хвост остаётся до следующего
            # прохода — иначе приёмник съел бы половину фрейма (T35 F1).
            end = max(raw.rfind(b"\n\n", off), raw.rfind(b"\r\n\r\n", off))
            if end < 0:
                continue
            end += 4 if raw[end:end + 4] == b"\r\n\r\n" else 2
            chunk = raw[off:end].decode("utf-8", "replace").replace("\r\n", "\n")   # Windows-редакторы
            st["offsets"][fp.name] = end
            for wire in split_frames(chunk):
                out.append(handle(wire, peer, perms, st, now_ms or int(time.time() * 1000)))
        S.save(_state_path(), st)
    return out


def handle(wire: str, peer: str, perms: dict, st: dict, now_ms: int) -> str:
    rx = nam.Receiver(SESSION, BUS, {p: set(k) for p, k in perms.items() if k} or {peer: set()})
    rx.sequences = dict(st["seq_in"])           # защита от повтора переживает перезапуск
    try:
        msg = rx.accept(json.dumps(validate_frame(wire)), peer, now_ms)
    except Exception as e:
        return reject(peer, "INVALID" if "syntax" in str(e).lower() else _code(e), str(e), wire)
    st["seq_in"][peer] = msg["seq"]
    try:
        return apply(msg)
    except Refused as e:
        return reject(peer, e.code, str(e), wire)


class Refused(Exception):
    def __init__(self, code: str, why: str):
        super().__init__(why)
        self.code = code


def _code(e: Exception) -> str:
    s = str(e)
    return ("EXPIRED" if "expired" in s else "REPLAY" if "replay" in s or "duplicate" in s
            else "UNAUTHORIZED" if "unauthorized" in s or "identity" in s else "INVALID")


def reject(peer: str, code: str, why: str, wire: str) -> str:
    head = wire.splitlines()[0][:160]
    line = f"ERR {code}: {why}"
    S.append_log("aml_bus", peer, f"отклонён фрейм {peer}: {line}\n`{head}`")
    box = S.INBOX / f"{peer}.md"
    if box.exists():
        with box.open("a", encoding="utf-8") as f:
            f.write(f"\n## {S.now():%Y-%m-%d %H:%M} — aml_bus — {code}\nТвой фрейм отклонён: {why}\n`{head}`\n")
    S.event("aml", text=f"{peer}: {line}"[:200])
    return f"reject {peer}: {line}"


def _task(board: dict, tid: str) -> dict:
    t = next((x for x in board["tasks"] if x["id"] == tid), None)
    if t is None:
        raise Refused("INVALID", f"задачи {tid} нет на доске")
    return t


def _mail(who: str, topic: str, text: str) -> None:
    box = S.INBOX / f"{who}.md"
    if box.exists():
        with box.open("a", encoding="utf-8") as f:
            f.write(f"\n## {S.now():%Y-%m-%d %H:%M} — aml_bus — {topic}\n{text}\n")


def apply(msg: dict) -> str:
    """Таблица полномочий и переходов §4.1: роли берутся из board.json, не из фрейма."""
    who, kind, body = msg["sender"], msg["kind"], msg["body"]
    if kind == "query" and body.get("request") == "KEYFRAME":
        # T42: потребитель, подключившийся посреди ленты доски, просит полный снимок
        import board_frames
        published = board_frames.publish(verify=True, force_state=True)
        line = f"query KEYFRAME от {who}: опубликован {published or 'ничего'}"
        _mail(who, "KEYFRAME", "Полный снимок доски (STATE s:2) опубликован в .agent-sync/frames/board.aml")
        S.append_log("aml_bus", "KEYFRAME", f"принят фрейм: {line}")
        S.event("aml", text=line)
        return "accept " + line
    models = set(S.load(S.AGENTS).get("llm", {}))
    tid = body.get("task") or body.get("related")
    effect, mail = "", []
    with S.Lock():
        board = S.load(S.BOARD)
        t = _task(board, tid)
        st, owner, critic = t.get("status"), t.get("assignee"), t.get("critic")
        note = None
        if kind == "handoff":
            hs = body["status"]
            checks = "; ".join(f"{c['name']}:{c['result']}" for c in body["checks"])
            if hs == "COMPLETE" and who == owner and st == "in_progress":
                t["status"], note, effect = "review", f"handoff COMPLETE: {body['summary']} [{checks}]", "→ review"
                mail.append((critic, f"{tid} ждёт твоей критики: {body['summary']}"))
            elif hs == "IN_PROGRESS" and who == owner and st in ("backlog", "in_progress"):
                t["status"], note, effect = "in_progress", f"handoff IN_PROGRESS: {body['summary']}", "→ in_progress"
            elif hs == "BLOCKED" and who == owner and st == "in_progress":
                note, effect = f"blocked: {body['summary']} [{checks}]", "статус не меняется"
                mail.append(("claude", f"{tid} заблокирована исполнителем: {body['summary']}"))
            elif hs == "BLOCKED" and who == critic and st == "review":
                note, effect = f"критик: {body['summary']} [{checks}]", "находки записаны"
                mail.append((owner, f"{tid}: находки критика — {checks}"))
            else:
                raise Refused("UNAUTHORIZED", f"HANDOFF {hs} от {who} при статусе {st} (исп. {owner}, критик {critic})")
            S.HISTORY.mkdir(exist_ok=True)
            with (S.HISTORY / f"{tid}.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## фрейм {S.iso()} от {who}\n```\n{AMLCodec.encode(msg)}\n```\n")
        elif kind == "result":
            if body.get("operations") and body["operations"] != [tid]:
                raise Refused("INVALID", f"ops {body['operations']} не совпадают с rel {tid}")
            verdict = body["status"]
            if who in models:
                effect = "совет модели, доска не меняется"
                mail.append(("claude", f"{who} советует по {tid}: {verdict}"))
            elif who == critic and st == "review" and verdict in ("ACCEPTED", "REJECTED"):
                t["status"] = "claude" if verdict == "ACCEPTED" else "in_progress"
                note, effect = f"critic ({who}, фрейм RES): {verdict}", f"→ {t['status']}"
                mail.append(("claude" if verdict == "ACCEPTED" else owner, f"{tid}: вердикт критика {verdict}"))
            elif who == "claude" and st == "claude" and verdict in ("ACCEPTED", "REJECTED"):
                t["status"] = "done" if verdict == "ACCEPTED" else "in_progress"
                note, effect = f"curator (фрейм RES): {verdict}", f"→ {t['status']}"
                mail.append((owner, f"{tid}: решение куратора {verdict}"))
            else:
                raise Refused("UNAUTHORIZED", f"RES {verdict} от {who} при статусе {st} (критик {critic})")
        elif kind == "query":
            tail = (t.get("notes") or "")[-400:] if body["request"] == "EXPLAIN" else ""
            mail.append((who, f"{tid} [{st}] исп. {owner}, критик {critic}: {t.get('summary', '')[:300]}"
                              + (f"\nПоследнее: {tail}" if tail else "")))
            effect = "ответ отправлен"
        elif kind == "error":
            if body["code"] == "BUSY":
                effect = "отправитель на паузе"
            else:
                effect = f"принято к сведению: {body['code']}"
        if note:
            t["notes"] = (t.get("notes", "") + " | " + note).strip(" |")
            t["updated_at"], t["updated_by"] = S.iso(), who
            S.save(S.BOARD, board)
    if kind == "error" and body["code"] == "BUSY":
        with S.Lock():
            agents = S.load(S.AGENTS)
            if who in agents["agents"]:
                agents["agents"][who]["state"] = "paused"
                S.save(S.AGENTS, agents)
    for to, text in mail:
        if to:
            _mail(to, tid, text)
    line = f"{kind} {tid} от {who}: {effect}"
    S.append_log("aml_bus", tid, f"принят фрейм: {line}")
    S.event("aml", text=line[:200])
    return "accept " + line


def main(argv=None):
    p = argparse.ArgumentParser(prog="aml_bus")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("sender"); s.add_argument("kind", choices=list(KIND)); s.add_argument("task")
    s.add_argument("arg", nargs="?")
    s.add_argument("--sum"); s.add_argument("--next"); s.add_argument("--rs")
    s.add_argument("--check", action="append"); s.add_argument("--art", action="append")
    sub.add_parser("receive")
    a = p.parse_args(argv)
    if a.cmd == "send":
        print(send(a.sender, a.kind, a.task, a.arg, a))
        print("→ приёмник обработает в течение 60 с (agent_sync serve); итог — в log.md и почте")
    else:
        for line in receive():
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
