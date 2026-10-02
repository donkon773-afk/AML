"""T35: транспорт AML в кластере — outbox → validate_frame → Receiver → полномочия §4.1 → доска.

Работает на временной копии .agent-sync; боевая доска и серверы не затрагиваются.
"""
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


class BusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sync = self.tmp / ".agent-sync"
        if (ROOT / ".agent-sync").exists():
            shutil.copytree(ROOT / ".agent-sync", sync,
                            ignore=shutil.ignore_patterns(".lock", ".jobs.lock", "*.tmp", "frames"))
        else:
            from tests.sync_fixture import init_test_sync
            init_test_sync(sync)
        for k, v in dict(SYNC=sync, BOARD=sync / "board.json", AGENTS=sync / "agents.json", LOG=sync / "log.md",
                         INBOX=sync / "inbox", EVENTS=sync / "events.jsonl", LOCK=sync / ".lock",
                         HISTORY=sync / "history", ARCHIVE=sync / "archive", ROOT=self.tmp).items():
            p = mock.patch.object(S, k, v)
            p.start()
            self.addCleanup(p.stop)
        board = S.load(S.BOARD)
        board["tasks"] = [
            dict(id="T90", title="исполнение", status="in_progress", assignee="antigravity", critic="codex", notes=""),
            dict(id="T91", title="приёмка", status="claude", assignee="codex", critic="claude", notes=""),
        ]
        S.save(S.BOARD, board)
        self.log_before = S.LOG.read_text(encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self, tid):
        return next(t for t in S.load(S.BOARD)["tasks"] if t["id"] == tid)["status"]

    def test_full_cycle_executor_critic_curator(self):
        B.send("antigravity", "handoff", "T90", "COMPLETE",
               mock.Mock(art=None, check=["verify.py:PASS:105+14"], sum="готово", next="critic: codex", rs=None))
        self.assertTrue(B.receive()[0].startswith("accept"))
        self.assertEqual(self.status("T90"), "review")
        self.assertIn("ждёт твоей критики", (S.INBOX / "codex.md").read_text(encoding="utf-8"))
        self.assertTrue((S.HISTORY / "T90.md").exists())
        B.send("codex", "res", "T90", "ACC")
        B.receive()
        self.assertEqual(self.status("T90"), "claude")
        B.send("claude", "res", "T90", "ACC")
        B.receive()
        self.assertEqual(self.status("T90"), "done")

    def test_critic_return_and_curator_return(self):
        B.send("antigravity", "handoff", "T90", "COMPLETE")
        B.send("codex", "res", "T90", "REJ", mock.Mock(art=None, check=None, sum=None, next=None, rs="CRITIC_RETURN"))
        B.receive()
        self.assertEqual(self.status("T90"), "in_progress")
        B.send("claude", "res", "T91", "REJ")
        B.receive()
        self.assertEqual(self.status("T91"), "in_progress")

    def test_role_is_taken_from_board_not_from_frame(self):
        B.send("codex", "handoff", "T90", "COMPLETE")        # codex — не исполнитель T90
        B.send("antigravity", "res", "T91", "ACC")           # antigravity — не куратор
        res = B.receive()
        self.assertTrue(all(r.startswith("reject") and "UNAUTHORIZED" in r for r in res), res)
        self.assertEqual((self.status("T90"), self.status("T91")), ("in_progress", "claude"))
        self.assertIn("отклонён", (S.INBOX / "codex.md").read_text(encoding="utf-8"))

    def test_local_model_verdict_never_changes_board(self):
        S.save(S.BOARD, {**S.load(S.BOARD), "tasks": [
            dict(id="T90", title="x", status="review", assignee="antigravity", critic="codex", notes="")]})
        B.send("gemma", "res", "T90", "ACC")
        self.assertIn("совет модели", B.receive()[0])
        self.assertEqual(self.status("T90"), "review")
        B.send("gemma", "handoff", "T90", "COMPLETE")        # модели HANDOFF запрещён матрицей
        self.assertIn("UNAUTHORIZED", B.receive()[0])

    def test_replay_expired_and_spoofed_frames_rejected(self):
        wire = B.send("codex", "query", "T90", "status")
        box = B.frames_dir() / "outbox-codex.aml"
        B.receive()
        with box.open("a", encoding="utf-8") as f:
            f.write(wire + "\n\n")                            # тот же фрейм ещё раз
        self.assertIn("REPLAY", B.receive()[0])
        B.send("codex", "query", "T90", "status")
        self.assertIn("EXPIRED", B.receive(now_ms=int(time.time() * 1000) + 61_000)[0])
        spoof = B.send("claude", "res", "T91", "ACC")
        (B.frames_dir() / "outbox-claude.aml").write_text("", encoding="utf-8")
        with (B.frames_dir() / "outbox-antigravity.aml").open("a", encoding="utf-8") as f:
            f.write(spoof + "\n\n")                           # фрейм claude в outbox antigravity
        self.assertIn("UNAUTHORIZED", B.receive()[0])
        self.assertEqual(self.status("T91"), "claude")

    def test_partially_written_frame_waits_for_its_end(self):  # T35 F1
        wire = B.send("antigravity", "handoff", "T90", "COMPLETE",
                      mock.Mock(art=None, check=["verify.py:PASS:ok"], sum="готово", next="", rs=None))
        box = B.frames_dir() / "outbox-antigravity.aml"
        head, body = wire.split("\n", 1)
        box.write_text(head + "\n", encoding="utf-8", newline="\n")         # записан только заголовок
        self.assertEqual(B.receive(), [], "недописанный фрейм не трогаем")
        with box.open("a", encoding="utf-8", newline="\n") as f:
            f.write(body + "\n\n")                                          # дописали тело и конец
        self.assertTrue(B.receive()[0].startswith("accept"))
        self.assertEqual(self.status("T90"), "review")

    def test_keyframe_query_publishes_full_board_state(self):  # T42
        import board_frames as F
        with mock.patch.object(F, "_RX", None):
            B.send("codex", "query", "ALL", "keyframe")
            res = B.receive()
        self.assertTrue(res[0].startswith("accept") and "KEYFRAME" in res[0], res)
        feed = (B.frames_dir() / "board.aml").read_text(encoding="utf-8")
        self.assertIn("|STATE|", feed.splitlines()[0])
        self.assertIn("KEYFRAME", (S.INBOX / "codex.md").read_text(encoding="utf-8"))

    def test_receiver_and_rotation_are_serialised(self):  # T43 F4 (сценарий codex)
        import threading
        import rotation as R
        B.send("antigravity", "query", "T90", "status")
        B.receive()
        B.send("antigravity", "query", "T90", "why")
        inside, release = threading.Event(), threading.Event()
        real_apply = B.apply

        calls = {"n": 0}

        def slow_apply(msg):
            calls["n"] += 1
            if calls["n"] == 1:                  # задержан только первый приёмник посреди прохода
                inside.set()
                release.wait(5)
            return real_apply(msg)
        with mock.patch.object(B, "apply", side_effect=slow_apply):
            rx = threading.Thread(target=B.receive)
            rx.start()
            inside.wait(5)
            rot = threading.Thread(target=lambda: R.rotate_outbox_aml("antigravity", force=True))
            rot.start()
            rot.join(2)       # без сериализации ротация успевает обнулить смещение здесь
            release.set()     # и задержанный приёмник затем записал бы старое смещение
            rx.join(10)
            rot.join(10)
        for i in range(3):
            B.send("antigravity", "query", "T90", "status")
        res = B.receive()
        self.assertEqual(len(res), 3, res)
        self.assertTrue(all(r.startswith("accept") for r in res), res)

    def test_query_answers_sender_without_board_change(self):
        before = S.BOARD.read_bytes()
        B.send("claude", "query", "T90", "why")
        B.receive()
        self.assertEqual(S.BOARD.read_bytes(), before)
        self.assertIn("T90 [in_progress]", (S.INBOX / "claude.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
