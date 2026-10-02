#!/usr/bin/env python3
"""rotation — ротация лент и журналов кластера aml-nam (T43).

Только stdlib.
Цели ротации в .agent-sync/:
  1. frames/board.aml       — лента снимков и дельт доски; после ротации
                              новая лента обязана начинаться с полного STATE (KEYFRAME s:2),
                              счётчики seq/rev растут монотонно.
  2. frames/outbox-*.aml    — ленты исходящих фреймов агентов; перед ротацией
                              приёмник съедает завершённые фреймы, прочитанный префикс
                              уходит в архив, недописанный хвост остаётся, смещения
                              bus_state.json offsets сбрасываются в 0, seq_out/seq_in
                              не сбрасываются.
  3. events.jsonl           — журнал событий; старые события уходят в архив,
                              последние N событий сохраняются для дашборда.
  4. llm_jobs.json          — очередь локальных моделей; НЕЗАВЕРШЁННЫЕ задания
                              (pending, assigned, running, delivering) НИКОГДА
                              не удаляются; архивируются только завершённые.

Все архивные файлы сохраняются в .agent-sync/archive/:
  board-<YYYYMMDD-HHMMSS>.aml
  outbox-<peer>-<YYYYMMDD-HHMMSS>.aml
  events-<YYYYMMDD-HHMMSS>.jsonl
  llm_jobs-<YYYYMMDD-HHMMSS>.json

Использование:
  python scripts/rotation.py                        # ротация при превышении 1 МБ
  python scripts/rotation.py --max-bytes 500000     # кастомный порог
  python scripts/rotation.py --force                # принудительная ротация
  python scripts/rotation.py --dry-run              # диагностика без изменений
  python scripts/rotation.py --target board         # ротация конкретной цели
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agent_sync as S  # noqa: E402
import aml_bus as B  # noqa: E402
import board_frames as F  # noqa: E402
import llm_pool as P  # noqa: E402

DEFAULT_MAX_BYTES = 1_000_000  # 1 МБ по умолчанию (T43)
INCOMPLETE_STATES = {"pending", "assigned", "running", "delivering"}


def _archive_path(archive_dir: Path, prefix: str, ext: str) -> Path:
    """Генерирует уникальное имя файла архива в archive_dir."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(S.TZ).strftime("%Y%m%d-%H%M%S")
    base = f"{prefix}-{ts}"
    candidate = archive_dir / f"{base}.{ext}"
    counter = 1
    while candidate.exists():
        candidate = archive_dir / f"{base}-{counter}.{ext}"
        counter += 1
    return candidate


def rotate_board_aml(max_bytes: int = DEFAULT_MAX_BYTES, force: bool = False,
                     verify: bool = True, now_ms: int | None = None) -> Path | None:
    """Ротация ленты board.aml в .agent-sync/archive/.

    1. Если размер >= max_bytes (или force=True), старая лента архивируется.
    2. В новую board.aml сразу пишется полный снимок (KEYFRAME / STATE s:2).
    3. Счётчики seq/rev в board_state.json продолжают монотонный рост.
    """
    feed = S.SYNC / "frames" / "board.aml"
    if not feed.exists():
        return None
    size = feed.stat().st_size
    if not force and size < max_bytes:
        return None

    with F._PUB_LOCK:
        archive_fp = _archive_path(S.ARCHIVE, "board", "aml")
        try:
            feed.replace(archive_fp)
        except OSError:
            data = feed.read_bytes()
            archive_fp.write_bytes(data)
            feed.unlink(missing_ok=True)

        # Новая лента обязана начинаться с полного снимка STATE (KEYFRAME s:2)
        F.publish(now_ms=now_ms, verify=verify, force_state=True)
        return archive_fp


def rotate_outbox_aml(peer: str | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
                      force: bool = False) -> list[Path]:
    """Ротация лент outbox-*.aml в .agent-sync/archive/.

    1. Перед ротацией выполняется aml_bus.receive(), чтобы съесть все завершённые фреймы.
    2. Прочитанный префикс уходит в archive/outbox-<peer>-<ts>.aml.
    3. Недописанный/непрочитанный хвост остаётся в outbox-<peer>.aml.
    4. bus_state.json offsets сбрасываются в 0 для ротированного файла.
    5. seq_out и seq_in НЕ сбрасываются (защита от повтора и монотонность сохраняются).
    """
    frames_dir = S.SYNC / "frames"
    if not frames_dir.exists():
        return []

    # Сначала принимаем все готовые фреймы
    try:
        B.receive()
    except Exception:
        pass

    targets = [frames_dir / f"outbox-{peer}.aml"] if peer else sorted(frames_dir.glob("outbox-*.aml"))
    archived = []

    with S.Lock():
        st = B.load_state()
        offsets = st.setdefault("offsets", {})

        for fp in targets:
            if not fp.exists():
                continue
            size = fp.stat().st_size
            if not force and size < max_bytes:
                continue

            p_name = fp.stem[len("outbox-"):]
            raw = fp.read_bytes()
            off = offsets.get(fp.name, 0)
            if off > len(raw):
                off = 0

            # F2: архивировать можно ТОЛЬКО уже прочитанную часть raw[:off].
            # Непрочитанный хвост (raw[off:]) ОБЯЗАН остаться в outbox,
            # даже при force=True, иначе фреймы будут утеряны до приёма receive().
            if off <= 0:
                continue

            to_archive = raw[:off]
            to_keep = raw[off:]

            if not to_archive:
                continue

            archive_fp = _archive_path(S.ARCHIVE, f"outbox-{p_name}", "aml")
            archive_fp.write_bytes(to_archive)

            # Сохраняем оставшийся хвост (если был) обратно в outbox
            if to_keep:
                tmp_fp = fp.with_suffix(".aml.tmp")
                tmp_fp.write_bytes(to_keep)
                try:
                    tmp_fp.replace(fp)
                except OSError:
                    fp.write_bytes(to_keep)
                    tmp_fp.unlink(missing_ok=True)
            else:
                fp.write_bytes(b"")

            offsets[fp.name] = 0
            archived.append(archive_fp)

        S.save(B._state_path(), st)

    return archived


def rotate_events(max_bytes: int = DEFAULT_MAX_BYTES, keep: int = 50,
                  force: bool = False) -> Path | None:
    """Ротация журнала events.jsonl в .agent-sync/archive/.

    1. Если размер >= max_bytes (или force=True), старые события уходят в архив.
    2. Последние `keep` событий остаются в активном events.jsonl для дашборда.
    """
    events_fp = S.EVENTS
    if not events_fp.exists():
        return None
    size = events_fp.stat().st_size
    if not force and size < max_bytes:
        return None

    with S.Lock():
        raw_text = events_fp.read_text(encoding="utf-8")
        lines = [l for l in raw_text.splitlines() if l.strip()]
        if not lines:
            return None
        if len(lines) <= keep and not force:
            return None

        if len(lines) > keep:
            old_lines = lines[:-keep]
            new_lines = lines[-keep:]
        else:
            old_lines = lines
            new_lines = []

        archive_fp = _archive_path(S.ARCHIVE, "events", "jsonl")
        archive_fp.write_text("\n".join(old_lines) + "\n", encoding="utf-8")

        tmp_fp = events_fp.with_suffix(".jsonl.tmp")
        tmp_fp.write_text(("\n".join(new_lines) + "\n") if new_lines else "", encoding="utf-8")
        try:
            tmp_fp.replace(events_fp)
        except OSError:
            events_fp.write_text(("\n".join(new_lines) + "\n") if new_lines else "", encoding="utf-8")
            tmp_fp.unlink(missing_ok=True)
        return archive_fp


def rotate_llm_jobs(max_bytes: int = DEFAULT_MAX_BYTES, keep_finished: int = 25,
                    force: bool = False) -> Path | None:
    """Ротация очереди и истории llm_jobs.json в .agent-sync/archive/.

    КРИТИЧЕСКИЙ ИНВАРИАНТ T43:
    Незавершённые задания (pending, assigned, running, delivering) НИКОГДА
    не удаляются. Архивируются только завершённые (done, failed).
    """
    jobs_fp = S.SYNC / "llm_jobs.json"
    if not jobs_fp.exists():
        return None
    size = jobs_fp.stat().st_size
    if not force and size < max_bytes:
        return None

    with P.JobsLock():
        data = S.load(jobs_fp) if jobs_fp.exists() else {}
        jobs = data.get("jobs", [])
        if not jobs:
            return None

        incomplete = [j for j in jobs if j.get("state") in INCOMPLETE_STATES]
        finished = [j for j in jobs if j.get("state") not in INCOMPLETE_STATES]

        if len(finished) <= keep_finished and not force:
            return None

        if len(finished) > keep_finished:
            to_archive = finished[:-keep_finished]
            kept_finished = finished[-keep_finished:]
        elif force and finished:
            to_archive = finished
            kept_finished = []
        else:
            return None

        archive_fp = _archive_path(S.ARCHIVE, "llm_jobs", "json")
        archive_data = {
            "archived_at": S.iso(),
            "count": len(to_archive),
            "jobs": to_archive,
        }
        S.save(archive_fp, archive_data)

        # Сохраняем ВСЕ незавершённые задания + последние kept_finished завершённых
        kept_ids = set(id(j) for j in (incomplete + kept_finished))
        remaining = [j for j in jobs if id(j) in kept_ids]
        S.save(jobs_fp, {"jobs": remaining})
        return archive_fp


def rotate_all(max_bytes: int = DEFAULT_MAX_BYTES, force: bool = False,
               dry_run: bool = False) -> dict[str, list[str]]:
    """Проверяет все 4 цели и ротирует превысившие порог."""
    results: dict[str, list[str]] = {"board": [], "outbox": [], "events": [], "jobs": []}

    if dry_run:
        feed = S.SYNC / "frames" / "board.aml"
        if feed.exists() and (force or feed.stat().st_size >= max_bytes):
            results["board"].append(f"{feed.name} ({feed.stat().st_size} B >= {max_bytes} B)")
        frames_dir = S.SYNC / "frames"
        if frames_dir.exists():
            for fp in sorted(frames_dir.glob("outbox-*.aml")):
                if force or fp.stat().st_size >= max_bytes:
                    results["outbox"].append(f"{fp.name} ({fp.stat().st_size} B >= {max_bytes} B)")
        if S.EVENTS.exists() and (force or S.EVENTS.stat().st_size >= max_bytes):
            results["events"].append(f"events.jsonl ({S.EVENTS.stat().st_size} B >= {max_bytes} B)")
        jobs_fp = S.SYNC / "llm_jobs.json"
        if jobs_fp.exists() and (force or jobs_fp.stat().st_size >= max_bytes):
            results["jobs"].append(f"llm_jobs.json ({jobs_fp.stat().st_size} B >= {max_bytes} B)")
        return results

    b_res = rotate_board_aml(max_bytes=max_bytes, force=force)
    if b_res:
        results["board"].append(b_res.as_posix())

    o_res = rotate_outbox_aml(max_bytes=max_bytes, force=force)
    for p in o_res:
        results["outbox"].append(p.as_posix())

    e_res = rotate_events(max_bytes=max_bytes, force=force)
    if e_res:
        results["events"].append(e_res.as_posix())

    j_res = rotate_llm_jobs(max_bytes=max_bytes, force=force)
    if j_res:
        results["jobs"].append(j_res.as_posix())

    total = sum(len(v) for v in results.values())
    if total:
        summary_parts = [f"{k}: {len(v)} файл(ов)" for k, v in results.items() if v]
        summary = ", ".join(summary_parts)
        S.append_log("rotation", "rotate", f"Выполнена ротация ({summary}):\n" +
                     "\n".join(f"- {k}: {', '.join(v)}" for k, v in results.items() if v))
        S.event("rotation", rotated=results)

    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rotation", description="Ротация лент и журналов aml-nam (T43)")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Порог размера файла (байт)")
    p.add_argument("--force", action="store_true", help="Принудительная ротация без проверки размера")
    p.add_argument("--dry-run", action="store_true", help="Только показать кандидатов на ротацию")
    p.add_argument("--target", choices=["all", "board", "outbox", "events", "jobs"], default="all")
    args = p.parse_args(argv)

    if args.target == "all":
        res = rotate_all(max_bytes=args.max_bytes, force=args.force, dry_run=args.dry_run)
    elif args.target == "board":
        r = rotate_board_aml(max_bytes=args.max_bytes, force=args.force)
        res = {"board": [r.as_posix()] if r else []}
    elif args.target == "outbox":
        r = rotate_outbox_aml(max_bytes=args.max_bytes, force=args.force)
        res = {"outbox": [p.as_posix() for p in r]}
    elif args.target == "events":
        r = rotate_events(max_bytes=args.max_bytes, force=args.force)
        res = {"events": [r.as_posix()] if r else []}
    elif args.target == "jobs":
        r = rotate_llm_jobs(max_bytes=args.max_bytes, force=args.force)
        res = {"jobs": [r.as_posix()] if r else []}

    for k, v in res.items():
        if v:
            print(f"{k}: {', '.join(v)}")
        elif args.dry_run or args.target != "all":
            print(f"{k}: без изменений")

    total = sum(len(v) for v in res.values())
    if not total and args.target == "all":
        print("Ротация: все файлы в пределах лимита")
    return 0


if __name__ == "__main__":
    sys.exit(main())
