#!/usr/bin/env python3
"""T17: эталонные фреймы сценария «обмен в кластере агентов».

Строит фреймы через AMLCodec.encode, проверяет каждый validate_frame
(грамматика) и Receiver.accept (права, сессия, TTL, повтор), сравнивает
размер с эквивалентным JSON. Используется в docs/SCENARIO-cluster-exchange.md.

  python scripts/cluster_examples.py            # печать фреймов и итогов
  python scripts/cluster_examples.py --check    # только exit 0/1
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aml_codec import AMLCodec  # noqa: E402
from frame_validation import validate_frame  # noqa: E402
from nam import Receiver  # noqa: E402

SESSION = "aml-nam"
T0 = 1_790_200_000_000
SHA = "0" * 63 + "1"

# Кто какие фреймы вправе слать (NAM kind из decode()).
PERMISSIONS = {
    "antigravity": {"handoff", "query", "error"},
    "codex": {"handoff", "result", "query", "error"},
    "claude": {"handoff", "result", "query", "error"},
    "gptoss": {"result", "query"},
}


def frames():
    return [
        ("исполнитель сдаёт задачу критику", {
            "kind": "HANDOFF", "session": SESSION, "sender": "antigravity", "recipient": "codex",
            "seq": 1, "sent_ms": T0, "ttl_ms": 60_000,
            "body": {"task": "T13", "status": "COMPLETE",
                     "summary": "F4: reset кэша грамматики в setUp + addCleanup, тест изоляции",
                     "artifacts": [{"path": "tests/test_regressions.py", "sha256": SHA}],
                     "checks": [{"name": "verify.py", "result": "PASS", "evidence": "71/71 + 14/14, exit 0"}],
                     "next": "critic: codex"}}),
        ("критик возвращает с находкой", {
            "kind": "HANDOFF", "session": SESSION, "sender": "codex", "recipient": "antigravity",
            "seq": 1, "sent_ms": T0 + 600_000, "ttl_ms": 60_000,
            "body": {"task": "T13", "status": "BLOCKED",
                     "summary": "critic: return",
                     "checks": [{"name": "F5", "result": "FAIL",
                                 "evidence": "python -m pytest -p no:randomly tests/test_regressions.py оставляет ключ в кэше"}],
                     "next": "исправить F5, вернуть в review"}}),
        ("вердикт критика одним фреймом", {
            "kind": "RES", "session": SESSION, "sender": "codex", "recipient": "claude",
            "seq": 2, "sent_ms": T0 + 601_000, "ttl_ms": 60_000,
            "body": {"related": "T13", "status": "REJECTED", "operations": ["T13"],
                     "reason": {"code": "STALLED", "evidence": []}}}),
        ("куратор спрашивает статус", {
            "kind": "QUERY", "session": SESSION, "sender": "claude", "recipient": "antigravity",
            "seq": 1, "sent_ms": T0 + 700_000, "ttl_ms": 60_000,
            "body": {"request": "STATUS", "related": "T18"}}),
        ("отказ: агент на паузе по лимиту", {
            "kind": "ERR", "session": SESSION, "sender": "antigravity", "recipient": "claude",
            "seq": 2, "sent_ms": T0 + 701_000, "ttl_ms": 60_000,
            "body": {"code": "BUSY", "related": "T18"}}),
        ("локальная модель-критик (советующий вердикт)", {
            "kind": "RES", "session": SESSION, "sender": "gptoss", "recipient": "claude",
            "seq": 1, "sent_ms": T0 + 800_000, "ttl_ms": 60_000,
            "body": {"related": "T18", "status": "ACCEPTED", "operations": ["T18"]}}),
    ]


KINDS = {"HANDOFF": "handoff", "RES": "result", "QUERY": "query", "ERR": "error"}
HANDOFF_KEYS = {"summary": "", "artifacts": [], "checks": [], "next": ""}


def envelope(msg):
    """Полный конверт NAM/1: codec не придумывает id/версию сам."""
    msg = dict(msg, v="NAM/1", id=f"m{msg['seq']}", kind=KINDS[msg["kind"]])
    if msg["kind"] == "handoff":
        msg["body"] = {**HANDOFF_KEYS, **msg["body"]}
    return msg


def main():
    check_only = "--check" in sys.argv
    receivers = {r: Receiver(SESSION, r, PERMISSIONS) for r in ("claude", "codex", "antigravity")}
    ok = True
    total_aml = total_json = 0
    for title, msg in ((t, envelope(m)) for t, m in frames()):
        wire = AMLCodec.encode(msg)
        try:
            decoded = validate_frame(wire)  # грамматика + схема NAM, как в llm_client.validate_and_accept
            g = True
            receivers[msg["recipient"]].accept(json.dumps(decoded), msg["sender"], msg["sent_ms"] + 1000)
            accepted = "accept"
        except Exception as exc:  # noqa: BLE001 — печатаем причину отказа
            g = g if "decoded" in locals() else False
            accepted = f"REJECT {exc}"
            ok = False
        as_json = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        total_aml += len(wire.encode())
        total_json += len(as_json.encode())
        if not check_only:
            print(f"### {title}\n{wire}\n→ grammar={g} receiver={accepted} "
                  f"bytes AML/JSON={len(wire.encode())}/{len(as_json.encode())}\n")
    # Отрицательные случаи: приёмник обязан отказать.
    rx = Receiver(SESSION, "claude", PERMISSIONS)
    ho = envelope(dict(frames()[0][1], sender="gptoss", recipient="claude"))
    res = envelope(frames()[5][1])
    negatives = [
        ("gptoss не вправе слать HANDOFF", ho, "gptoss", ho["sent_ms"] + 1000),
        ("фрейм просрочен (TTL 60 с)", res, "gptoss", res["sent_ms"] + 61_000),
        ("подмена отправителя", res, "codex", res["sent_ms"] + 1000),
    ]
    for title, msg, peer, at in negatives:
        try:
            rx.accept(json.dumps(validate_frame(AMLCodec.encode(msg))), peer, at)
            verdict, ok = "ПРИНЯТ — ошибка", False
        except Exception as exc:  # noqa: BLE001
            verdict = f"отказ: {exc}"
        if not check_only:
            print(f"✗ {title} → {verdict}")
    rx.accept(json.dumps(validate_frame(AMLCodec.encode(res))), "gptoss", res["sent_ms"] + 1000)
    try:
        rx.accept(json.dumps(validate_frame(AMLCodec.encode(res))), "gptoss", res["sent_ms"] + 2000)
        ok = False
        if not check_only:
            print("✗ повтор того же seq → ПРИНЯТ — ошибка")
    except Exception as exc:  # noqa: BLE001
        if not check_only:
            print(f"✗ повтор того же seq → отказ: {exc}")
    if not check_only:
        print(f"ИТОГО байт AML/JSON: {total_aml}/{total_json} "
              f"(−{100 - 100 * total_aml // total_json}%); всё валидно: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
