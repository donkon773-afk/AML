"""T45: учёт расхода агентов — на временных журналах, живые ~/.claude и ~/.codex не читаются."""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import spend  # noqa: E402

T0 = dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))


def at(minutes: int) -> str:
    return (T0 + dt.timedelta(minutes=minutes)).isoformat()


def hb(minutes, agent, task, limit=None, weekly=None, kind="heartbeat"):
    return {"ts": at(minutes), "kind": kind, "agent": agent, "task": task, "limit": limit, "weekly": weekly}


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "archive").mkdir()

    def write_events(self, events, archived=()):
        (self.dir / "archive" / "events-20260924-000000.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in archived), encoding="utf-8")
        (self.dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return spend.load_events(self.dir / "events.jsonl", self.dir / "archive")

    def test_limit_drops_go_to_task_resets_and_long_gaps_do_not(self):
        events = self.write_events([
            hb(10, "claude", "T1", 80, 70),
            hb(20, "claude", "T1", 60, 65),       # T1: −20 % окна, −5 % недели
            hb(30, "claude", "-", 100, 64),       # сброс окна 5 ч — не расход; неделя −1 % ещё на T1
            hb(40, "claude", "T2", 90, 64),       # «-» = без задачи → «—»
            hb(200, "claude", "T2", 50, 60),      # 160 мин без замеров — неизвестно, на что
        ], archived=[hb(0, "claude", "T1", 90, 70)])  # архив ротации тоже читается: −10 % на T1
        tl = spend.Timeline(events, window_min=60)
        rows = spend.limit_spend(events, tl, T0)
        s = spend.summarize(rows, [], tl)
        self.assertEqual(s["tasks"]["T1"]["claude"]["pct5h"], 30)
        self.assertEqual(s["tasks"]["T1"]["claude"]["pctweek"], 6)
        self.assertEqual(s["tasks"]["—"]["claude"]["pct5h"], 50)
        self.assertEqual(s["agents"]["claude"]["pct5h"], 80)

    def test_codex_limits_events_use_task_from_heartbeat(self):
        events = self.write_events([
            hb(0, "codex", "T7"),
            hb(5, "codex", None, 70, 30, kind="limits"),
            hb(9, "codex", None, 64, 29, kind="limits"),
        ])
        tl = spend.Timeline(events, 60)
        s = spend.summarize(spend.limit_spend(events, tl, T0), [], tl)
        self.assertEqual((s["tasks"]["T7"]["codex"]["pct5h"], s["tasks"]["T7"]["codex"]["pctweek"]), (6, 1))

    def test_claude_tokens_dedupe_marker_and_price(self):
        proj = self.dir / "claude" / "proj"
        proj.mkdir(parents=True)
        usage = {"input_tokens": 1000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 1_000_000,
                 "output_tokens": 10_000,
                 "cache_creation": {"ephemeral_1h_input_tokens": 2000, "ephemeral_5m_input_tokens": 0}}
        line = lambda mid, model: json.dumps({"type": "assistant", "timestamp": at(15), "requestId": "r" + mid,
                                              "message": {"id": mid, "model": model, "usage": usage}})
        # одно сообщение пишется строкой на каждый блок контента — считать один раз
        (proj / "a.jsonl").write_text("\n".join([json.dumps({"type": "user", "cwd": "C:/x/aml-nam"}),
                                                  line("m1", "claude-opus-5-5"), line("m1", "claude-opus-5-5"),
                                                  line("m2", "claude-opus-5")]), encoding="utf-8")
        (proj / "other.jsonl").write_text(line("m3", "claude-opus-5-5"), encoding="utf-8")  # без маркера
        rows = spend.claude_tokens(self.dir / "claude", T0)
        self.assertEqual([r["model"] for r in rows], ["claude-opus-5-5", "claude-opus-5"])
        # opus-5-5: 1000×4 + 2000×8 (1 ч) + 1M×0.20 + 10k×20 = 0.004+0.016+0.2+0.2
        self.assertAlmostEqual(rows[0]["usd"], 0.42)
        # opus-5 (не opus-5-5): 1000×5 + 2000×10 + 1M×0.5 + 10k×25
        self.assertAlmostEqual(rows[1]["usd"], 0.775)

    def test_codex_tokens_from_cumulative_counter(self):
        sess = self.dir / "codex" / "2026" / "09" / "24"
        sess.mkdir(parents=True)
        tc = lambda m, i, c, o: json.dumps({"timestamp": at(m), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}})
        (sess / "rollout-1.jsonl").write_text("\n".join([
            json.dumps({"type": "turn_context", "payload": {"cwd": "C:/x/aml-nam", "model": "gpt-x"}}),
            tc(1, 100_000, 60_000, 1000),        # первый замер = всё с начала файла
            tc(2, 100_000, 60_000, 1000),        # повтор без приращения — пропуск
            tc(3, 300_000, 250_000, 3000),       # +200k вход, из них +190k кэш, +2k выход
        ]), encoding="utf-8")
        rows = spend.codex_tokens(self.dir / "codex", T0, prices={"gpt-x": {"in": 1, "read": 0.1, "out": 10}})
        self.assertEqual([(r["in"], r["read"], r["out"]) for r in rows], [(40_000, 60_000, 1000), (10_000, 190_000, 2000)])
        self.assertAlmostEqual(rows[0]["usd"], (40_000 * 1 + 60_000 * 0.1 + 1000 * 10) / 1e6)
        self.assertAlmostEqual(rows[0]["claude_eq"], (40_000 * 4 + 60_000 * 0.2 + 1000 * 20) / 1e6)
        self.assertIsNone(spend.codex_tokens(self.dir / "codex", T0)[0]["usd"])  # цены не заданы

    def test_tokens_attributed_by_heartbeat_and_rendered(self):
        events = self.write_events([hb(0, "claude", "T5", 90), hb(0, "antigravity", "T6", 90),
                                    hb(20, "antigravity", "T6", 85)])
        tl = spend.Timeline(events, 60)
        tok = [{"t": T0 + dt.timedelta(minutes=m), "agent": "claude", "model": "claude-sonnet-5",
                "in": 0, "w5m": 0, "w1h": 0, "read": 0, "out": 100_000, "usd": 1.0} for m in (10, 90)]
        s = spend.summarize(spend.limit_spend(events, tl, T0), tok, tl)
        self.assertEqual(s["tasks"]["T5"]["claude"]["usd"], 1.0)
        self.assertEqual(s["tasks"]["—"]["claude"]["usd"], 1.0)   # через 90 мин после пульса — вне пульса
        s["since"] = T0.isoformat()
        text = spend.render(s)
        self.assertIn("без журнала токенов (только % лимита): antigravity", text)
        self.assertIn("T6", text)


if __name__ == "__main__":
    unittest.main()
