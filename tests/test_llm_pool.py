"""T18: llm_pool — раздача заданий локальным моделям и перенос с выпавших.

Все сетевые вызовы подменены: тесты не трогают LM Studio и боевую доску
(.agent-sync копируется во временную папку).
"""
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
import llm_pool as P  # noqa: E402

LLM = {"gemma": {"model": "g", "endpoint": "http://mac:1234", "speed": 16, "host": "mac",
                 "status": {"online": True}},
       "gptoss": {"model": "o", "endpoint": "http://pc:1234", "speed": 3.5, "host": "pc",
                  "status": {"online": True}}}


def job(i, state="pending", llm=None, attempts=0):
    return {"id": f"J{i}", "kind": "ask", "by": "codex", "prompt": "x", "state": state,
            "llm": llm, "attempts": attempts, "reply_to": ["codex"]}


class AssignTests(unittest.TestCase):
    def test_faster_model_gets_more_but_slow_one_is_used_under_load(self):
        jobs = [job(i) for i in range(6)]
        P.assign(jobs, LLM)
        got = [j["llm"] for j in jobs]
        self.assertGreater(got.count("gemma"), got.count("gptoss"))
        self.assertIn("gptoss", got, "при длинной очереди медленная модель тоже получает работу")

    def test_jobs_of_dropped_model_move_to_online_ones(self):
        jobs = [job(1, "running", "gemma"), job(2, "assigned", "gemma")]
        llm = {**LLM, "gemma": {**LLM["gemma"], "status": {"online": False}}}
        moves = P.assign(jobs, llm)
        self.assertEqual({j["llm"] for j in jobs}, {"gptoss"})
        self.assertTrue(any("не в сети" in m for m in moves))

    def test_nobody_online_jobs_wait(self):
        jobs = [job(1, "assigned", "gemma")]
        off = {n: {**c, "status": {"online": False}} for n, c in LLM.items()}
        P.assign(jobs, off)
        self.assertEqual((jobs[0]["state"], jobs[0]["llm"]), ("pending", None))

    def test_retry_limit(self):
        jobs = [job(1, attempts=P.MAX_ATTEMPTS)]
        P.assign(jobs, LLM)
        self.assertEqual(jobs[0]["state"], "failed")


class RunJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sync = self.tmp / ".agent-sync"
        if (ROOT / ".agent-sync").exists():
            shutil.copytree(ROOT / ".agent-sync", sync,
                            ignore=shutil.ignore_patterns(".lock", ".jobs.lock", "*.tmp"))
        else:
            from tests.sync_fixture import init_test_sync
            init_test_sync(sync)
        patches = {S: dict(SYNC=sync, BOARD=sync / "board.json", AGENTS=sync / "agents.json",
                           LOG=sync / "log.md", INBOX=sync / "inbox", EVENTS=sync / "events.jsonl",
                           LOCK=sync / ".lock", ROOT=self.tmp),
                   P: dict(JOBS=sync / "llm_jobs.json", REVIEW=self.tmp / "review")}
        for mod, attrs in patches.items():
            for k, v in attrs.items():
                p = mock.patch.object(mod, k, v)
                p.start()
                self.addCleanup(p.stop)
        lock = mock.patch.object(P.JobsLock, "path", sync / ".jobs.lock")
        lock.start()
        self.addCleanup(lock.stop)
        agents = S.load(S.AGENTS)
        agents["llm"] = {k: dict(v) for k, v in LLM.items()}
        S.save(S.AGENTS, agents)
        P.save_jobs([job(1, "assigned", "gemma")])
        self.log_before = S.LOG.read_text(encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_success_is_delivered_and_log_only_appended(self):
        reply = {"choices": [{"message": {"content": "<think>x</think>Совет: добавить тест"},
                              "finish_reason": "stop"}], "usage": {"completion_tokens": 10}}
        with mock.patch.object(P, "http_json", return_value=reply):
            P.run_job("J1", "gemma")
        j = P.load_jobs()[0]
        self.assertEqual(j["state"], "done")
        log = S.LOG.read_text(encoding="utf-8")
        self.assertTrue(log.startswith(self.log_before), "log.md только дописывается")
        added = log[len(self.log_before):]
        self.assertIn("Совет: добавить тест", added)
        self.assertNotIn("<think>", added, "рассуждения модели в лог не попадают")
        self.assertIn("gemma", (S.INBOX / "codex.md").read_text(encoding="utf-8"))
        self.assertEqual(len(list((self.tmp / "review").rglob("J1-gemma-*.md"))), 1)

    def test_truncated_answer_is_marked(self):
        reply = {"choices": [{"message": {"content": "обрыв"}, "finish_reason": "length"}], "usage": {}}
        with mock.patch.object(P, "http_json", return_value=reply):
            P.run_job("J1", "gemma")
        out = next((self.tmp / "review").rglob("J1-gemma-*.md")).read_text(encoding="utf-8")
        self.assertIn("ОБРЕЗАН", out)

    def test_server_error_returns_job_to_queue(self):
        with mock.patch.object(P, "http_json", side_effect=RuntimeError("HTTP 500: Model is unloaded.")):
            P.run_job("J1", "gemma")
        j = P.load_jobs()[0]
        self.assertEqual((j["state"], j["llm"], j["attempts"]), ("pending", None, 1))
        self.assertIn("unloaded", j["error"])
        self.assertEqual(S.LOG.read_text(encoding="utf-8"), self.log_before)

    def test_job_ids_unique_within_one_millisecond(self):  # T18 F2
        with mock.patch.object(P.time, "time", return_value=1790211600.123):
            ids = {P.new_job("ask", "codex", prompt=str(i))["id"] for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_late_answer_after_reassignment_is_dropped(self):  # T18 F1
        def slow_reply(*_a, **_kw):
            # пока gemma «думает», пул переназначает задание на qwen9b
            with P.JobsLock():
                jobs = P.load_jobs()
                P.assign(jobs, {**LLM, "gemma": {**LLM["gemma"], "status": {"online": False}}})
                P.save_jobs(jobs)
            return {"choices": [{"message": {"content": "старый ответ"}, "finish_reason": "stop"}], "usage": {}}
        with mock.patch.object(P, "http_json", side_effect=slow_reply):
            P.run_job("J1", "gemma")
        j = P.load_jobs()[0]
        self.assertEqual((j["llm"], j["state"]), ("gptoss", "assigned"))
        self.assertEqual(S.LOG.read_text(encoding="utf-8"), self.log_before, "чужой ответ не доставлен")
        reply = {"choices": [{"message": {"content": "новый ответ"}, "finish_reason": "stop"}], "usage": {}}
        with mock.patch.object(P, "http_json", return_value=reply):
            P.run_job("J1", "gptoss")
        added = S.LOG.read_text(encoding="utf-8")[len(self.log_before):]
        self.assertEqual(added.count("новый ответ"), 1)
        self.assertNotIn("старый ответ", added)

    def test_failed_delivery_is_retried_without_duplicates(self):  # T18 F3
        reply = {"choices": [{"message": {"content": "совет F3"}, "finish_reason": "stop"}], "usage": {}}
        broken = self.tmp / "inbox-broken"
        broken.write_text("не папка", encoding="utf-8")   # запись в почту упадёт с OSError
        with mock.patch.object(P, "http_json", return_value=reply), mock.patch.object(S, "INBOX", broken):
            P.run_job("J1", "gemma")
        j = P.load_jobs()[0]
        self.assertEqual((j["state"], j["answer"]), ("delivering", "совет F3"), "ответ не потерян")
        self.assertEqual(j["delivered"], ["report", "log"])
        self.assertTrue(P.finish_delivery("J1"))                  # следующий проход, почта исправна
        j = P.load_jobs()[0]
        self.assertEqual(j["state"], "done")
        added = S.LOG.read_text(encoding="utf-8")[len(self.log_before):]
        self.assertEqual(added.count("совет F3"), 1, "лог не дублируется при повторе")
        self.assertIn("совет F3", (S.INBOX / "codex.md").read_text(encoding="utf-8"))
        self.assertEqual(len(list((self.tmp / "review").rglob("J1-gemma-*.md"))), 1)

    def test_workers_never_touch_the_board(self):
        before = S.BOARD.read_bytes()
        reply = {"choices": [{"message": {"content": "critic2: accept"}, "finish_reason": "stop"}], "usage": {}}
        with mock.patch.object(P, "http_json", return_value=reply):
            P.run_job("J1", "gemma")
        self.assertEqual(S.BOARD.read_bytes(), before)

    def test_stop_sequence_passed_to_request(self):
        reply = {"choices": [{"message": {"content": "ответ со стопом"}, "finish_reason": "stop"}], "usage": {}}
        captured_body = {}

        def fake_http_json(url, body=None, timeout=10):
            nonlocal captured_body
            captured_body = body
            return reply

        # Обновляем задание J1 со стоп-последовательностью
        with P.JobsLock():
            jobs = P.load_jobs()
            jobs[0]["stop"] = ["\n"]
            P.save_jobs(jobs)

        with mock.patch.object(P, "http_json", side_effect=fake_http_json):
            P.run_job("J1", "gemma")

        self.assertIn("stop", captured_body)
        self.assertEqual(captured_body["stop"], ["\n"])


if __name__ == "__main__":
    unittest.main()
