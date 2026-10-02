"""T33: coordination tests use a temporary .agent-sync, never the live board."""

import argparse
import contextlib
import importlib.util
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "agent_sync.py"
spec = importlib.util.spec_from_file_location("agent_sync_under_test", SOURCE)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def agent(limit=80, *, fallback=(), critic="codex"):
    return {"role": "test agent", "heartbeat": sync.iso(), "state": "active", "limit": limit,
            "fallback": list(fallback), "default_critic": critic}


class AgentSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / ".agent-sync"
        root.mkdir()
        self.root = root
        paths = {name: root / file for name, file in {
            "SYNC": ".", "BOARD": "board.json", "AGENTS": "agents.json",
            "LOG": "log.md", "EVENTS": "events.jsonl", "LOCK": ".lock",
            "INBOX": "inbox", "HISTORY": "history", "ARCHIVE": "archive",
        }.items()}
        for name, value in paths.items():
            p = patch.object(sync, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("INBOX", "HISTORY", "ARCHIVE"):
            getattr(sync, name).mkdir()
        sync.LOG.write_text("# test log\n", encoding="utf-8")
        sync.EVENTS.write_text("", encoding="utf-8")
        self.agents = {"policy": {"pause_below": 10, "resume_at": 15,
                                   "stale_after_min": 30, "offline_after_min": 60,
                                   "critic_order": ["codex", "claude", "antigravity"]},
                       "agents": {"codex": agent(7, fallback=["claude", "antigravity"], critic="claude"),
                                  "claude": agent(80, fallback=["antigravity", "codex"]),
                                  "antigravity": agent(80, fallback=["claude", "codex"])}}
        sync.save(sync.AGENTS, self.agents)
        self.board = {"project": "aml-nam", "tasks": [
            {"id": "T1", "title": "queued", "status": "backlog", "assignee": "codex",
             "critic": "antigravity", "priority": 1, "notes": ""},
            {"id": "T2", "title": "started", "status": "in_progress", "assignee": "codex",
             "critic": "antigravity", "priority": 2, "notes": ""},
            {"id": "T3", "title": "review", "status": "review", "assignee": "antigravity",
             "critic": "codex", "priority": "2", "notes": ""},
        ]}
        sync.save(sync.BOARD, self.board)
        for name in ("codex_limits", "antigravity_signal"):
            p = patch.object(sync, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def task(self, tid):
        return next(t for t in sync.load(sync.BOARD)["tasks"] if t["id"] == tid)

    def test_pause_rebalance_resume_and_critic_return(self):
        sync.rebalance(verbose=False)
        self.assertEqual(sync.load(sync.AGENTS)["agents"]["codex"]["state"], "paused")
        self.assertEqual(self.task("T1")["assignee"], "claude")
        self.assertEqual(self.task("T1")["home"], "codex")
        self.assertEqual(self.task("T2")["assignee"], "claude")
        self.assertEqual(self.task("T3")["critic"], "claude")
        self.assertEqual(self.task("T3")["critic_home"], "codex")
        self.assertTrue(all(t["critic"] != t["assignee"] for t in sync.load(sync.BOARD)["tasks"]))

        agents = sync.load(sync.AGENTS)
        agents["agents"]["codex"]["limit"] = 15
        sync.save(sync.AGENTS, agents)
        sync.rebalance(verbose=False)
        self.assertEqual(sync.load(sync.AGENTS)["agents"]["codex"]["state"], "active")
        self.assertEqual(self.task("T1")["assignee"], "codex")  # backlog returns home
        self.assertEqual(self.task("T2")["assignee"], "claude")  # started work stays with substitute
        self.assertEqual(self.task("T3")["critic"], "codex")
        self.assertNotIn("critic_home", self.task("T3"))
        self.assertEqual(sync.rebalance(verbose=False), [])  # no second move

    def test_original_owner_never_critiques_substituted_task(self):
        """Задачу автора (home) доделал заместитель — автор её критиком не становится."""
        board = sync.load(sync.BOARD)
        board["tasks"] = [dict(id="T90", title="x", status="review", assignee="claude", home="antigravity",
                               critic="antigravity", critic_home="codex", notes="")]
        sync.save(sync.BOARD, board)
        agents = sync.load(sync.AGENTS)
        agents["agents"]["codex"]["heartbeat"] = (sync.now() - sync.dt.timedelta(minutes=120)).isoformat()
        for n in ("claude", "antigravity"):
            agents["agents"][n].update(heartbeat=sync.iso(), state="active", limit=80, weekly=80)
        sync.save(sync.AGENTS, agents)
        sync.rebalance(verbose=False)
        self.assertEqual(sync.load(sync.BOARD)["tasks"][0]["critic"], "codex")

    def test_low_weekly_limit_means_critic_only(self):
        """AUTONOMY §3: недельный остаток < 25 % — задач на исполнение не получает, критиковать может."""
        board = sync.load(sync.BOARD)
        board["tasks"] = [dict(id="T91", title="x", status="backlog", assignee="antigravity", home="codex",
                               critic="claude", notes="")]
        sync.save(sync.BOARD, board)
        agents = sync.load(sync.AGENTS)
        agents["agents"]["codex"].update(limit=70, weekly=20, heartbeat=sync.iso(), state="active")
        sync.save(sync.AGENTS, agents)
        sync.rebalance(verbose=False)
        self.assertEqual(self.task("T91")["assignee"], "antigravity", "не возвращается хозяину с 20 % недели")
        self.assertTrue(sync.available(sync.load(sync.AGENTS), "codex"), "критиковать может")

    def test_brief_sorts_mixed_priority_types(self):
        agents = sync.load(sync.AGENTS)
        agents["agents"]["codex"]["limit"] = 50
        sync.save(sync.AGENTS, agents)
        board = sync.load(sync.BOARD)
        board["tasks"].append({"id": "T4", "title": "first review", "status": "review",
                               "assignee": "claude", "critic": "codex", "priority": 1, "notes": ""})
        sync.save(sync.BOARD, board)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sync.cmd_brief(argparse.Namespace(agent="codex", width=100))
        text = out.getvalue()
        self.assertLess(text.index("T4 [review]"), text.index("T3 [review]"))

    def test_set_is_serialized_and_keeps_both_notes(self):
        errors = []

        def write(value):
            try:
                sync.cmd_set(argparse.Namespace(task="T1", pairs=["notes+=" + value], by="codex"))
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=write, args=(str(i),)) for i in range(12)]
        with patch.object(sync, "print", lambda *_: None, create=True):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        notes = self.task("T1")["notes"].split(" | ")
        self.assertEqual(set(notes), {str(i) for i in range(12)})

    def test_compact_preserves_full_notes_in_history(self):
        board = sync.load(sync.BOARD)
        full = "first: " + "x" * 100 + " | latest result"
        board["tasks"][0]["notes"] = full
        sync.save(sync.BOARD, board)
        sync.LOG.write_text("# header\n\n" + "".join(f"## entry {i}\nbody\n" for i in range(5)), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            sync.cmd_compact(argparse.Namespace(max_notes=50, max_log=40, keep=2))
        self.assertIn(full, (sync.HISTORY / "T1.md").read_text(encoding="utf-8"))
        self.assertIn("latest result", self.task("T1")["notes"])
        archives = list(sync.ARCHIVE.glob("log-*.md"))
        self.assertEqual(len(archives), 1)
        self.assertIn("entry 0", archives[0].read_text(encoding="utf-8"))
        self.assertIn("entry 4", sync.LOG.read_text(encoding="utf-8"))

    def test_set_weight_is_int_so_readiness_works(self):
        """T45: `set weight=3` писал строку, readiness() падал на sum(int + str) — дашборд и status ломались."""
        self.board["milestones"] = [{"id": "M1", "title": "m"}]
        sync.save(sync.BOARD, self.board)
        with contextlib.redirect_stdout(io.StringIO()):
            sync.cmd_set(argparse.Namespace(task="T1", pairs=["weight=3", "milestone=M1"], by="claude"))
            sync.cmd_set(argparse.Namespace(task="T2", pairs=["weight=2", "milestone=M1"], by="claude"))
        self.assertEqual(self.task("T1")["weight"], 3)
        self.assertEqual(sync.state_json()["readiness"]["milestones"][0]["weight"], 5)

    def test_heartbeat_event_records_weekly_and_codex_limit_changes(self):
        """T45: spend.py считает расход по замерам — пульс несёт недельный остаток, лимиты codex пишутся событием."""
        with contextlib.redirect_stdout(io.StringIO()):
            sync.cmd_heartbeat(argparse.Namespace(agent="claude", limit=60, weekly=70, resets=None,
                                                  task="T2", state=None, note=None))
        cx = {"limit": 50, "weekly": 40, "source": "codex rollout", "seen": "x"}
        with patch.object(sync, "codex_limits", return_value=cx):
            sync.rebalance(verbose=False)
            sync.rebalance(verbose=False)  # без изменений — второго события нет
        events = [sync.json.loads(l) for l in sync.EVENTS.read_text(encoding="utf-8").splitlines() if l]
        hb = next(e for e in events if e["kind"] == "heartbeat")
        self.assertEqual((hb["limit"], hb["weekly"], hb["task"]), (60, 70, "T2"))
        limits = [e for e in events if e["kind"] == "limits"]
        self.assertEqual(len(limits), 1)
        self.assertEqual((limits[0]["agent"], limits[0]["limit"], limits[0]["weekly"]), ("codex", 50, 40))

    def test_watch_once_returns_3_until_recovery(self):
        sync.rebalance(verbose=False)
        args = argparse.Namespace(agent="codex", every=0, once=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.cmd_watch(args), 3)
        agents = sync.load(sync.AGENTS)
        agents["agents"]["codex"]["limit"] = 15
        sync.save(sync.AGENTS, agents)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.cmd_watch(args), 0)


if __name__ == "__main__":
    unittest.main()
