"""T43: Тесты ротации лент и журналов (board.aml, outbox-*.aml, events.jsonl, llm_jobs.json).

Работает на временной копии .agent-sync; боевая доска и серверы не затрагиваются.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_sync as S  # noqa: E402
import aml_bus as B  # noqa: E402
import board_frames as F  # noqa: E402
import llm_pool as P  # noqa: E402
import nam  # noqa: E402
import rotation as R  # noqa: E402


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sync = self.tmp / ".agent-sync"
        for d in ("inbox", "history", "archive", "frames"):
            (sync / d).mkdir(parents=True)
        (sync / "log.md").write_text("# log\n", encoding="utf-8")
        (sync / "events.jsonl").write_text("", encoding="utf-8")
        task = lambda i, st, a, c, **kw: {"id": i, "title": i, "status": st, "assignee": a, "critic": c,
                                          "priority": 1, "notes": "", **kw}
        (sync / "board.json").write_text(json.dumps({"project": "aml-nam", "tasks": [
            task("T43", "in_progress", "antigravity", "claude"),
            task("T44", "review", "antigravity", "codex"),
        ]}), encoding="utf-8")
        agent = lambda role, default_critic: {
            "role": role, "heartbeat": S.iso(), "state": "active", "limit": 80, "weekly": 60,
            "fallback": [], "default_critic": default_critic}
        (sync / "agents.json").write_text(json.dumps({
            "policy": {"pause_below": 10, "resume_at": 15, "stale_after_min": 30, "offline_after_min": 60,
                       "critic_order": ["codex", "claude", "antigravity"]},
            "agents": {
                "claude": agent("curator", "codex"),
                "codex": agent("critic", "claude"),
                "antigravity": agent("executor", "codex"),
            }}), encoding="utf-8")
        for agent_name in ["antigravity", "claude", "codex"]:
            (sync / "inbox" / f"{agent_name}.md").write_text(f"# Почта для {agent_name}\n", encoding="utf-8")

        self.patches = {
            S: dict(SYNC=sync, BOARD=sync / "board.json", AGENTS=sync / "agents.json", LOG=sync / "log.md",
                    INBOX=sync / "inbox", EVENTS=sync / "events.jsonl", LOCK=sync / ".lock",
                    ARCHIVE=sync / "archive", HISTORY=sync / "history", ROOT=self.tmp),
            P: dict(JOBS=sync / "llm_jobs.json"),
        }
        for mod, attrs in self.patches.items():
            for k, v in attrs.items():
                p = mock.patch.object(mod, k, v)
                p.start()
                self.addCleanup(p.stop)
        lock_p = mock.patch.object(P.JobsLock, "path", sync / ".jobs.lock")
        lock_p.start()
        self.addCleanup(lock_p.stop)

        self.rx = F.receiver()
        self.t = 1_790_300_000_000

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_board_aml_rotation_creates_archive_and_starts_with_keyframe(self):
        """Ротация board.aml архивирует ленту, а новая лента сразу начинается с полного STATE."""
        # 1. Публикуем начальный STATE и DELTA
        self.t += 60_000
        self.assertEqual(F.publish(self.t, self.rx), "state")
        b = S.load(S.BOARD)
        b["tasks"][0]["priority"] = 2
        S.save(S.BOARD, b)
        self.t += 60_000
        self.assertEqual(F.publish(self.t, self.rx), "delta")

        feed = S.SYNC / "frames" / "board.aml"
        self.assertTrue(feed.exists())
        old_content = feed.read_text(encoding="utf-8")
        self.assertIn("|STATE|", old_content)
        self.assertIn("|DELTA|", old_content)

        # 2. Ротируем board.aml
        archive_path = R.rotate_board_aml(force=True, verify=False, now_ms=self.t)
        self.assertIsNotNone(archive_path)
        self.assertTrue(archive_path.exists())
        self.assertEqual(archive_path.read_text(encoding="utf-8"), old_content)

        # 3. Новая лента создана и содержит ровно один фрейм — полный STATE (KEYFRAME s:2)
        new_feed = feed.read_text(encoding="utf-8").strip()
        frames = new_feed.split("\n\n")
        self.assertEqual(len(frames), 1, "В новой ленте ровно 1 фрейм")
        self.assertIn("|STATE|", frames[0])
        self.assertIn("|s:2|", frames[0])

        # 4. Счётчик seq монотонно вырос (был 2 в дельте, стал 3 в новом снимке)
        last_frame = F.AMLCodec.decode(frames[0])
        self.assertEqual(last_frame["seq"], 3)
        self.assertEqual(last_frame["body"]["rev"], 3)

        # 5. Непрерывный Receiver принимает новый STATE и последующую DELTA
        self.rx.accept(json.dumps(last_frame), F.SENDER, self.t)
        b["tasks"][0]["priority"] = 3
        S.save(S.BOARD, b)
        self.t += 60_000
        self.assertEqual(F.publish(self.t, self.rx), "delta")

    def test_board_reconcile_handles_rotated_feed(self):
        """_reconcile форсирует STATE, если лента была ротирована/очищена или отстаёт."""
        self.t += 60_000
        F.publish(self.t, self.rx)
        feed = S.SYNC / "frames" / "board.aml"
        state_fp = S.SYNC / "frames" / "board_state.json"
        st = S.load(state_fp)

        # Ситуация А: лента пуста (0 байт после ротации) при rev > 0
        feed.write_text("", encoding="utf-8")
        self.assertTrue(F._reconcile(feed, st), "Пустая лента при rev > 0 требует STATE")

        # Ситуация Б: лента содержит seq меньший, чем st['seq'] (ротирована/усечена)
        wire = F.AMLCodec.encode(F.envelope(1, self.t, "state", {"world": F.WORLD, "rev": 1, "entities": []}))
        feed.write_text(wire + "\n\n", encoding="utf-8")
        st["seq"] = 5
        self.assertTrue(F._reconcile(feed, st), "Лента с отстающим seq требует STATE")
        self.assertEqual(st["rows"], {}, "rows сброшены для чистого снимка")

    def test_outbox_rotation_and_offsets(self):
        """Ротация outbox архивирует прочитанное, сбрасывает offset в 0 и сохраняет seq."""
        # 1. Отправляем 2 сообщения
        B.send("antigravity", "query", "T43", "status")
        B.send("antigravity", "query", "T44", "status")
        outbox = S.SYNC / "frames" / "outbox-antigravity.aml"
        self.assertTrue(outbox.exists())

        # 2. Приёмник съедает оба сообщения
        results = B.receive()
        self.assertEqual(len(results), 2)
        st = B.load_state()
        self.assertGreater(st["offsets"].get("outbox-antigravity.aml", 0), 0)
        self.assertEqual(st["seq_out"].get("antigravity"), 2)
        self.assertEqual(st["seq_in"].get("antigravity"), 2)

        # 3. Ротируем outbox
        archived = R.rotate_outbox_aml("antigravity", force=True)
        self.assertEqual(len(archived), 1)
        self.assertTrue(archived[0].exists())

        # 4. Проверяем состояние: offsets сброшен в 0, seq сохранены
        st_after = B.load_state()
        self.assertEqual(st_after["offsets"].get("outbox-antigravity.aml"), 0)
        self.assertEqual(st_after["seq_out"].get("antigravity"), 2)
        self.assertEqual(st_after["seq_in"].get("antigravity"), 2)
        self.assertEqual(outbox.read_bytes(), b"", "Прочитанный файл очищен")

        # 5. Новое сообщение отправляется и принимается без сбоев
        B.send("antigravity", "query", "T43", "why")
        self.assertEqual(st_after["seq_out"].get("antigravity"), 2)
        new_res = B.receive()
        self.assertEqual(len(new_res), 1)
        self.assertIn("accept", new_res[0])
        st_final = B.load_state()
        self.assertEqual(st_final["seq_out"].get("antigravity"), 3)
        self.assertGreater(st_final["offsets"].get("outbox-antigravity.aml"), 0)

    def test_outbox_rotation_preserves_unread_tail(self):
        """Недописанный или ещё не прочитанный хвост остаётся в outbox после ротации."""
        wire = B.send("antigravity", "query", "T43", "status")
        B.receive()  # первое прочитано

        # Дописываем второй фрейм, но без closing newline (хвост ждёт окончания записи)
        outbox = S.SYNC / "frames" / "outbox-antigravity.aml"
        st = B.load_state()
        first_len = st["offsets"]["outbox-antigravity.aml"]

        unread_piece = "!AML:2|QUERY|aml-nam|antigravity→agent_sync|2"
        with outbox.open("a", encoding="utf-8") as f:
            f.write(unread_piece)

        # Ротируем: прочитанный префикс уходит в архив, unread_piece остаётся
        archived = R.rotate_outbox_aml("antigravity", force=True)
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(archived[0].read_bytes()), first_len)

        # Активный outbox теперь содержит только unread_piece, а offset = 0
        self.assertEqual(outbox.read_text(encoding="utf-8"), unread_piece)
        st_rot = B.load_state()
        self.assertEqual(st_rot["offsets"]["outbox-antigravity.aml"], 0)

    def test_outbox_receive_resets_offset_if_file_shrunk(self):
        """Приёмник receive() сбрасывает offset в 0, если файл был усечён/ротирован снаружи."""
        outbox = S.SYNC / "frames" / "outbox-antigravity.aml"
        st = B.load_state()
        st["offsets"]["outbox-antigravity.aml"] = 10000  # смещение больше фактического размера
        S.save(B._state_path(), st)

        wire = B.send("antigravity", "query", "T43", "status")
        self.assertLess(outbox.stat().st_size, 10000)

        # receive не должен зависнуть или пропустить: сбрасывает offset в 0 и читает фрейм
        res = B.receive()
        self.assertEqual(len(res), 1)
        self.assertIn("accept", res[0])
        st_after = B.load_state()
        self.assertLess(st_after["offsets"]["outbox-antigravity.aml"], 10000)
        self.assertGreater(st_after["offsets"]["outbox-antigravity.aml"], 0)

    def test_events_rotation(self):
        """Ротация events.jsonl архивирует старые события и оставляет последние keep."""
        for i in range(25):
            S.event("test", index=i)

        events_fp = S.EVENTS
        self.assertEqual(len(events_fp.read_text(encoding="utf-8").splitlines()), 25)

        # Ротируем с сохранением последних 5
        archived = R.rotate_events(keep=5, force=True)
        self.assertIsNotNone(archived)
        self.assertTrue(archived.exists())

        archived_lines = archived.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(archived_lines), 20)
        self.assertIn('"index": 0', archived_lines[0])
        self.assertIn('"index": 19', archived_lines[-1])

        remaining_lines = events_fp.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(remaining_lines), 5)
        self.assertIn('"index": 20', remaining_lines[0])
        self.assertIn('"index": 24', remaining_lines[-1])

        # state_json читает корректно
        st = S.state_json()
        self.assertEqual(len(st["events"]), 5)

    def test_llm_jobs_rotation_preserves_all_incomplete_jobs(self):
        """Незавершённые задания (pending, assigned, running, delivering) НИКОГДА не удаляются."""
        jobs = [
            {"id": "J1", "kind": "ask", "state": "done", "created": "2026-09-24T01:00:00"},
            {"id": "J2", "kind": "ask", "state": "failed", "created": "2026-09-24T01:01:00"},
            {"id": "J3", "kind": "ask", "state": "pending", "created": "2026-09-24T01:02:00"},
            {"id": "J4", "kind": "ask", "state": "assigned", "created": "2026-09-24T01:03:00"},
            {"id": "J5", "kind": "ask", "state": "running", "created": "2026-09-24T01:04:00"},
            {"id": "J6", "kind": "ask", "state": "delivering", "created": "2026-09-24T01:05:00"},
            {"id": "J7", "kind": "ask", "state": "done", "created": "2026-09-24T01:06:00"},
            {"id": "J8", "kind": "ask", "state": "done", "created": "2026-09-24T01:07:00"},
            {"id": "J9", "kind": "ask", "state": "pending", "created": "2026-09-24T01:08:00"},
        ]
        P.save_jobs(jobs)

        # Ротируем с сохранением 1 завершённого задания
        archived = R.rotate_llm_jobs(keep_finished=1, force=True)
        self.assertIsNotNone(archived)
        self.assertTrue(archived.exists())

        archived_data = json.loads(archived.read_text(encoding="utf-8"))
        archived_ids = [j["id"] for j in archived_data["jobs"]]
        # J1, J2, J7 ушли в архив (J8 — самый свежий done, оставлен)
        self.assertEqual(set(archived_ids), {"J1", "J2", "J7"})

        active_jobs = P.load_jobs()
        active_ids = [j["id"] for j in active_jobs]

        # Все незавершённые задания (J3, J4, J5, J6, J9) ОБЯЗАНЫ остаться
        for inc_id in ["J3", "J4", "J5", "J6", "J9"]:
            self.assertIn(inc_id, active_ids, f"Незавершённое задание {inc_id} не должно удаляться!")
        self.assertIn("J8", active_ids, "Свежее завершённое задание J8 сохранено")
        self.assertEqual(len(active_jobs), 6)

    def test_rotate_all_and_cli(self):
        """rotate_all и CLI корректно отрабатывают со всеми флагами."""
        # 1. dry-run
        dry = R.rotate_all(force=True, dry_run=True)
        self.assertIn("board", dry)
        self.assertIn("outbox", dry)
        self.assertIn("events", dry)

        # 2. force rotate_all
        res = R.rotate_all(force=True)
        self.assertIsInstance(res, dict)

        # 3. CLI --dry-run
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(R.main(["--dry-run"]), 0)
        self.assertIn("без изменений", out.getvalue())

    def test_f1_send_serialized_with_rotation_no_frame_lost(self):
        """F1: send() сериализован под S.Lock() с ротацией outbox — ни один фрейм не теряется."""
        import threading

        sent_count = 10
        errors = []

        def sender_worker():
            try:
                for i in range(sent_count):
                    B.send("antigravity", "query", "T43", "status")
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        def rotator_worker():
            try:
                for _ in range(5):
                    R.rotate_outbox_aml("antigravity", force=True)
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        t_send = threading.Thread(target=sender_worker)
        t_rot = threading.Thread(target=rotator_worker)

        t_send.start()
        t_rot.start()
        t_send.join()
        t_rot.join()

        self.assertEqual(errors, [])
        # Все отправленные фреймы либо уже в архиве (если receive съел), либо в outbox
        # Запускаем receive() для всех оставшихся
        B.receive()
        st = B.load_state()
        self.assertEqual(st["seq_out"].get("antigravity"), sent_count)
        self.assertEqual(st["seq_in"].get("antigravity"), sent_count)

    def test_f2_force_rotation_never_archives_unread_frames(self):
        """F2: force=True при off == 0 (или упавшем receive) не отправляет в архив непрочитанное."""
        outbox = S.SYNC / "frames" / "outbox-antigravity.aml"
        B.send("antigravity", "query", "T43", "status")
        B.send("antigravity", "query", "T44", "why")

        # Симулируем ситуацию, когда receive() упал или не запускался (off == 0)
        st = B.load_state()
        self.assertEqual(st.get("offsets", {}).get("outbox-antigravity.aml", 0), 0)

        # Вызываем rotate_outbox_aml с упавшим receive
        with mock.patch.object(B, "receive", side_effect=RuntimeError("temporary receiver error")):
            archived = R.rotate_outbox_aml("antigravity", force=True)

        # Непрочитанные фреймы НЕ уходят в архив!
        self.assertEqual(archived, [], "При off == 0 ничего не должно архивироваться")
        self.assertGreater(outbox.stat().st_size, 0, "Outbox обязан сохранить непрочитанные фреймы")

        # Когда receive() восстанавливается, оба фрейма успешно принимаются
        res = B.receive()
        self.assertEqual(len(res), 2)
        self.assertIn("accept", res[0])
        self.assertIn("accept", res[1])

        # Теперь off > 0, повторная ротация успешно архивирует прочитанное
        archived_after = R.rotate_outbox_aml("antigravity", force=True)
        self.assertEqual(len(archived_after), 1)
        self.assertEqual(outbox.read_bytes(), b"", "Прочитанный файл очищен")

    def test_f3_event_reentrant_lock_and_safe_rotation(self):
        """F3: event() под реентерабельным Lock() и безопасная ротация events.jsonl."""
        # 1. Проверяем реентерабельность Lock: event() вызывается изнутри with Lock() (как в rebalance)
        with S.Lock():
            S.event("reentrant_test", val=42)
            with S.Lock():
                S.event("nested_reentrant", val=43)

        lines = S.EVENTS.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

        # 2. Ротация events.jsonl при параллельных событиях
        for i in range(20):
            S.event("bulk", num=i)

        arch = R.rotate_events(keep=5, force=True)
        self.assertIsNotNone(arch)
        remaining = S.EVENTS.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(remaining), 5)


if __name__ == "__main__":
    unittest.main()
