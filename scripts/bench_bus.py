#!/usr/bin/env python3
"""Замер задержки приёмника шины AML «запись в outbox → запись в log.md» (SCENARIO §5, порог p95 ≤ 2 с).

Работает на временной копии .agent-sync: отправители пишут фреймы в случайные
моменты, приёмник крутится тем же циклом, что в agent_sync serve (опрос
BUS_POLL_S). Задержка = момент окончания receive() для фрейма − sent_ms.
Боевая доска и серверы не затрагиваются.

  PY scripts/bench_bus.py [--n 60] [--out review/<дата>/T38-bus-latency.txt]
"""
from __future__ import annotations

import argparse
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_sync as S  # noqa: E402
import aml_bus as B  # noqa: E402


def isolate() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="aml-bench-")) / ".agent-sync"
    shutil.copytree(S.SYNC, tmp, ignore=shutil.ignore_patterns("frames", ".lock", ".jobs.lock", "*.tmp"))
    for k in ("BOARD", "AGENTS", "LOG", "INBOX", "EVENTS", "HISTORY"):
        setattr(S, k, tmp / getattr(S, k).name)
    S.LOCK, S.SYNC = tmp / ".lock", tmp
    return tmp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    tmp = isolate()
    done_at: list[tuple[str, float]] = []
    stop = threading.Event()

    def receiver():
        while not stop.is_set():
            for line in B.receive():
                done_at.append((line, time.time()))
            time.sleep(S.BUS_POLL_S)

    th = threading.Thread(target=receiver, daemon=True)
    th.start()
    sent = []
    senders = ["claude", "codex", "antigravity"]
    for i in range(a.n):
        time.sleep(random.uniform(0.05, 0.6))
        who = senders[i % 3]
        wire = B.send(who, "query", "T38", "status")
        sent.append((who, int(wire.split("|")[5]) / 1000))
    deadline = time.time() + 10
    while len(done_at) < a.n and time.time() < deadline:
        time.sleep(0.1)
    stop.set()
    th.join(timeout=5)
    # сопоставление по порядку: приёмник обрабатывает outbox каждого отправителя по порядку
    by_sender = {w: sorted(t for l, t in done_at if f" от {w}:" in l) for w in senders}
    lat = []
    for w in senders:
        mine = [s for who, s in sent if who == w]
        lat += [d - s for s, d in zip(mine, by_sender[w])]
    lat.sort()
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else float("nan")
    text = (f"Шина AML: {len(lat)}/{a.n} фреймов обработано, опрос {S.BUS_POLL_S} с\n"
            f"p50 {statistics.median(lat):.3f} с, p95 {p95:.3f} с, max {lat[-1]:.3f} с "
            f"(порог §5: p95 ≤ 2 с — {'выполнен' if p95 <= 2 else 'НЕ выполнен'})\n")
    print(text, end="")
    if a.out:
        out = S.ROOT / a.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    shutil.rmtree(tmp.parent, ignore_errors=True)
    return 0 if len(lat) == a.n and p95 <= 2 else 1


if __name__ == "__main__":
    sys.exit(main())
