import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_sync as S  # noqa: E402
import board_frames as F  # noqa: E402
import nam  # noqa: E402


class BoardFramesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Полностью синтетическая доска: тест не зависит ни от живой доски (T38 critic
        # antigravity F1: статус живой T38 менялся и ломал ожидания), ни от наличия
        # .agent-sync в распакованном пакете выпуска.
        sync = self.tmp / ".agent-sync"
        for d in ("inbox", "history", "archive", "frames"):
            (sync / d).mkdir(parents=True)
        (sync / "log.md").write_text("# log\n", encoding="utf-8")
        (sync / "events.jsonl").write_text("", encoding="utf-8")
        task = lambda i, st, a, c, **kw: {"id": i, "title": i, "status": st, "assignee": a, "critic": c,
                                          "priority": 1, "notes": "", **kw}
        (sync / "board.json").write_text(json.dumps({"project": "aml-nam", "tasks": [
            task("T37", "done", "claude", "codex"),
            task("T38", "in_progress", "claude", "antigravity", depends=["T37"]),
            task("T41", "review", "antigravity", "codex"),
        ]}), encoding="utf-8")
        agent = lambda: {"role": "test", "heartbeat": S.iso(), "state": "active", "limit": 80, "weekly": 60,
                         "fallback": [], "default_critic": "codex"}
        (sync / "agents.json").write_text(json.dumps({
            "policy": {"pause_below": 10, "resume_at": 15, "stale_after_min": 30, "offline_after_min": 60,
                       "critic_order": ["codex", "claude", "antigravity"]},
            "agents": {"claude": agent(), "codex": agent(), "antigravity": agent()}}), encoding="utf-8")
        for k, v in dict(SYNC=sync, BOARD=sync / "board.json", AGENTS=sync / "agents.json", LOG=sync / "log.md",
                         INBOX=sync / "inbox", EVENTS=sync / "events.jsonl", LOCK=sync / ".lock",
                         DECISIONS=sync / "decisions.md", STOP_LINE=sync / "STOP_LINE.md").items():
            p = mock.patch.object(S, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.edit("T38", status="in_progress")
        self.rx = F.receiver()
        self.t = 1_790_300_000_000

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pub(self):
        self.t += 60_000
        return F.publish(self.t, self.rx)

    def edit(self, tid, **kw):
        b = S.load(S.BOARD)
        for t in b["tasks"]:
            if t["id"] == tid:
                t.update(kw)
        S.save(S.BOARD, b)

    def test_state_then_deltas_in_one_receiver_session(self):
        self.assertEqual(self.pub(), "state")
        self.assertIsNone(self.pub(), "без изменений фрейм не пишется")
        self.edit("T38", status="review")
        self.assertEqual(self.pub(), "delta")
        feed = (S.SYNC / "frames" / "board.aml").read_text(encoding="utf-8")
        last = feed.strip().split("\n\n")[-1]
        self.assertIn("|s:2|", last.splitlines()[0])
        self.assertEqual([l for l in last.splitlines()[1:] if l.startswith("+#")][0].split(":")[:3],
                         ["+#T38", "task", "REV"])
        self.assertEqual(len(last.splitlines()), 2, "в дельте только изменённая задача")

    def test_removed_task_becomes_delta_remove(self):
        self.pub()
        b = S.load(S.BOARD)
        b["tasks"] = [t for t in b["tasks"] if t["id"] != "T41"]
        S.save(S.BOARD, b)
        self.assertEqual(self.pub(), "delta")
        self.assertIn("-#T41", (S.SYNC / "frames" / "board.aml").read_text(encoding="utf-8"))

    def test_keyframe_every_n_and_savings_counted(self):
        with mock.patch.object(F, "KEYFRAME_EVERY", 3):
            kinds = []
            for i in range(5):
                self.edit("T38", priority=i % 2 + 1)
                kinds.append(self.pub())
        self.assertEqual(kinds, ["state", "delta", "state", "delta", "delta"])
        st = S.load(S.SYNC / "frames" / "board_state.json")["stats"]
        self.assertLess(st["bytes_aml"], st["bytes_json"] * 0.6)

    def test_failed_state_save_recovers_from_feed(self):  # T38 F2 (сценарий codex)
        self.assertEqual(self.pub(), "state")                       # rx принял STATE seq1
        state_fp = S.SYNC / "frames" / "board_state.json"
        state_fp.unlink()                                           # как будто save упал после append
        self.edit("T38", status="review")
        self.assertEqual(self.pub(), "state", "восстановление из ленты — полным снимком")
        feed = (S.SYNC / "frames" / "board.aml").read_text(encoding="utf-8").strip().split("\n\n")
        self.assertEqual([f.split("|")[4] for f in feed], ["1", "2"], "seq продолжается, без повтора")
        self.edit("T38", status="done")
        self.assertEqual(self.pub(), "delta")                       # дальше снова дельты, тот же rx

    def test_partial_tail_is_truncated_before_next_frame(self):  # T38 F2
        self.pub()
        feed = S.SYNC / "frames" / "board.aml"
        with feed.open("a", encoding="utf-8", newline="\n") as f:
            f.write("!AML:2|DELTA|aml-nam|agent_sync→board|9")     # сбой посреди записи
        self.edit("T38", status="review")
        self.assertEqual(self.pub(), "delta")
        frames = feed.read_text(encoding="utf-8").strip().split("\n\n")
        self.assertEqual(len(frames), 2)
        self.assertNotIn("|9\n", feed.read_text(encoding="utf-8"))

    def test_serve_mode_keeps_its_own_receiver(self):  # T38 F3
        with mock.patch.object(F, "_RX", None):
            self.t += 60_000
            self.assertEqual(F.publish(self.t, verify=True), "state")   # новый подписчик → STATE
            self.edit("T38", status="review")
            self.t += 60_000
            self.assertEqual(F.publish(self.t, verify=True), "delta")
            self.assertIsNotNone(F._RX)
            F._RX.states.clear()                                     # подписчик потерял состояние
            self.edit("T38", status="done")
            self.t += 60_000
            with self.assertRaisesRegex(nam.ProtocolError, "RESYNC"):
                F.publish(self.t, verify=True)                       # дельта не записана
            self.assertIsNone(F._RX)
            self.t += 60_000
            self.assertEqual(F.publish(self.t, verify=True), "state")   # восстановился снимком

    def test_keyframe_on_request_resyncs_a_late_consumer(self):  # T42
        self.pub()
        late = F.receiver()                                          # подключился посреди ленты
        self.edit("T38", status="review")
        with self.assertRaisesRegex(nam.ProtocolError, "RESYNC"):
            F.publish(self.t + 60_000, late)
        self.t += 60_000
        self.assertEqual(F.publish(self.t, late, force_state=True), "state", "снимок вне очереди")
        self.edit("T38", status="done")
        self.assertEqual(self.pub_with(late), "delta", "дальше обычные дельты")

    def pub_with(self, rx):
        self.t += 60_000
        return F.publish(self.t, rx)

    def test_bad_task_in_heartbeat_does_not_break_snapshot(self):  # стоп-линия 24.09: task "-"
        agents = S.load(S.AGENTS)
        agents["agents"]["claude"]["task"] = "-"
        S.save(S.AGENTS, agents)
        self.assertEqual(self.pub(), "state")
        self.assertIn("#claude:agent:ACT", (S.SYNC / "frames" / "board.aml").read_text(encoding="utf-8"))

    def test_stale_receiver_rejects_delta_without_keyframe(self):
        self.pub()
        self.edit("T38", status="review")
        fresh = F.receiver()                     # потребитель подключился посередине ленты
        with self.assertRaisesRegex(nam.ProtocolError, "RESYNC"):
            F.publish(self.t + 60_000, fresh)


if __name__ == "__main__":
    unittest.main()
