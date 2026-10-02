"""Unit tests for scripts/benchmark_live_frames.py (T14).

All tests run completely offline with mocked data and no network calls.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_live_frames as bmark
from nam import Receiver


class BenchmarkLiveFramesTests(unittest.TestCase):
    def setUp(self):
        self.dataset = bmark.build_dataset()
        self.permissions = bmark.PERMISSIONS
        self.receiver = Receiver(bmark.SESSION, "claude", self.permissions)

    def test_dataset_size_and_structure(self):
        """Verify dataset contains ≥20 samples for each required frame type."""
        for kind in ("HANDOFF", "RES", "QUERY", "ERR", "PROP"):
            self.assertIn(kind, self.dataset)
            items = self.dataset[kind]
            self.assertGreaterEqual(len(items), 20, f"Kind {kind} must have at least 20 items")
            for item in items:
                self.assertEqual(item["kind"], kind)
                self.assertIn("prompt", item)
                self.assertTrue(len(item["prompt"]) > 10)

    def test_percentile_calculation(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        self.assertEqual(bmark.percentile(vals, 50), 5.5)
        self.assertAlmostEqual(bmark.percentile(vals, 95), 9.55, places=2)
        self.assertEqual(bmark.percentile([], 50), 0.0)

    def test_evaluate_frame_strict_success(self):
        # A valid RES frame
        raw = "!AML:2|RES|aml-nam|codex\u2192claude|1|1790200000000|60000|rel:T13|*ACC:#T13"
        expected = {
            "kind": "RES",
            "task": "T13",
            "sender": "codex",
            "recipient": "claude",
            "status": "ACCEPTED"
        }
        res = bmark.evaluate_frame(raw, expected, self.receiver)
        self.assertTrue(res["strict"])
        self.assertTrue(res["lenient"])
        self.assertTrue(res["accept"])
        self.assertTrue(res["semantic"])
        self.assertIsNone(res["error"])

    def test_evaluate_frame_lenient_markdown_wrapped(self):
        # A valid frame wrapped in markdown prose and code fences
        frame = "!AML:2|RES|aml-nam|codex\u2192claude|1|1790200000000|60000|rel:T13|*ACC:#T13"
        raw = f"Here is the requested frame:\n```aml\n{frame}\n```\nHope it helps!"
        expected = {
            "kind": "RES",
            "task": "T13",
            "sender": "codex",
            "recipient": "claude",
            "status": "ACCEPTED"
        }
        res = bmark.evaluate_frame(raw, expected, self.receiver)
        self.assertFalse(res["strict"], "Markdown wrapper should fail strict validation")
        self.assertTrue(res["lenient"], "Lenient extraction should succeed")
        self.assertTrue(res["accept"])
        self.assertTrue(res["semantic"])

    def test_evaluate_frame_semantic_mismatch(self):
        # Frame refers to T14 when prompt expected T13
        raw = "!AML:2|RES|aml-nam|codex\u2192claude|1|1790200000000|60000|rel:T14|*ACC:#T14"
        expected = {
            "kind": "RES",
            "task": "T13",
            "sender": "codex",
            "recipient": "claude",
            "status": "ACCEPTED"
        }
        res = bmark.evaluate_frame(raw, expected, self.receiver)
        self.assertTrue(res["strict"])
        self.assertTrue(res["accept"])
        self.assertFalse(res["semantic"], "Task mismatch should set semantic to False")

    def test_evaluate_frame_receiver_unauthorized_rejection(self):
        # antigravity is not allowed to send 'result' in PERMISSIONS
        raw = "!AML:2|RES|aml-nam|antigravity\u2192claude|1|1790200000000|60000|rel:T13|*ACC:#T13"
        expected = {
            "kind": "RES",
            "task": "T13",
            "sender": "antigravity",
            "recipient": "claude",
            "status": "ACCEPTED"
        }
        res = bmark.evaluate_frame(raw, expected, self.receiver)
        self.assertTrue(res["strict"])
        self.assertFalse(res["accept"], "Unauthorized sender should be rejected by receiver")
        self.assertIn("unauthorized", res["error"])

    def test_generate_markdown_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "LIVE_FRAMES.md"
            mock_results = {
                "test_model": {
                    "model": "test_model",
                    "kinds": {
                        "RES": {
                            "total": 5,
                            "strict": 5,
                            "lenient": 5,
                            "accept": 5,
                            "semantic": 5,
                            "latencies_sec": [1.0, 1.2, 1.1, 1.5, 2.0],
                            "tokens": [50, 50, 50, 50, 50],
                            "errors": []
                        }
                    }
                }
            }
            bmark.generate_markdown_report(mock_results, report_path)
            self.assertTrue(report_path.exists())
            content = report_path.read_text(encoding="utf-8")
            self.assertIn("LIVE_FRAMES.md", content)
            self.assertIn("test_model", content)
            self.assertIn("100.0%", content)

    def test_query_gemma_http_stop_parameter(self):
        import io
        from unittest import mock
        captured_data = {}

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                body = {
                    "choices": [{"message": {"content": "!AML:2|RES|..."}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 10}
                }
                return json.dumps(body).encode("utf-8")

        def fake_urlopen(req, timeout=30):
            nonlocal captured_data
            captured_data = json.loads(req.data.decode("utf-8"))
            return DummyResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            bmark.query_gemma_http("тестовый промпт", stop=["\n"])

        self.assertIn("stop", captured_data)
        self.assertEqual(captured_data["stop"], ["\n"])

    def test_run_benchmark_kinds_filter(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            with mock.patch.object(bmark, "query_gemma_http", return_value=("!AML:2|RES|aml-nam|codex\u2192claude|1|1790200000000|60000|rel:T13|*ACC:#T13", 0.5, 20, 40.0)):
                res = bmark.run_benchmark(models=["gemma"], samples_per_kind=2, kinds=["RES"], out_dir=out_dir, report_name="T44-test.md")
                self.assertIn("gemma", res)
                self.assertEqual(list(res["gemma"]["kinds"].keys()), ["RES"])
                self.assertTrue((out_dir / "T44-test.md").exists())

    def test_generate_markdown_report_t44_customization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "T44-gemma-res.md"
            mock_results = {
                "gemma": {
                    "model": "gemma",
                    "kinds": {
                        "RES": {
                            "total": 20,
                            "strict": 20,
                            "lenient": 20,
                            "accept": 20,
                            "semantic": 20,
                            "latencies_sec": [3.5] * 20,
                            "tokens": [50] * 20,
                            "errors": []
                        }
                    }
                }
            }
            bmark.generate_markdown_report(mock_results, report_path)
            self.assertTrue(report_path.exists())
            content = report_path.read_text(encoding="utf-8")
            self.assertIn("T44-gemma-res.md", content)
            self.assertIn("Gemma RES", content)
            self.assertIn("12/20 (60.0%)", content)
            self.assertIn("20/20 (100.0%)", content)
            self.assertIn("Отказов не зафиксировано: все 20 проверок успешны", content)
            self.assertNotIn("| `qwen9b` |", content)
            self.assertNotIn("| `gemma` | **HANDOFF** |", content)

    def test_generate_markdown_report_t44_partial_failure(self):
        """F2 codex: отчёт T44 динамически отражает частичный успех и список ошибок."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "T44-gemma-res.md"
            mock_results = {
                "gemma": {
                    "model": "gemma",
                    "kinds": {
                        "RES": {
                            "total": 2,
                            "strict": 1,
                            "lenient": 1,
                            "accept": 1,
                            "semantic": 1,
                            "latencies_sec": [1.0, 2.0],
                            "tokens": [30, 40],
                            "errors": ["syntax_error: invalid character"]
                        }
                    }
                }
            }
            bmark.generate_markdown_report(mock_results, report_path)
            content = report_path.read_text(encoding="utf-8")
            self.assertIn("1/2 (50.0%)", content)
            self.assertIn("p50 = 1.50 с", content)
            self.assertIn("p95 = 1.95 с", content)
            self.assertIn("syntax_error: invalid character", content)
            self.assertNotIn("20/20 (100.0%)", content)
            self.assertNotIn("Отказов и сбоев валидации не зафиксировано", content)

    def test_t44_verdicts_follow_numbers_without_errors(self):
        """F3 codex: пустой errors при 50 % и p95 > 60 с — никаких «соблюдён» / «100 %»."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "T44-gemma-res.md"
            res = {"total": 2, "strict": 1, "lenient": 1, "accept": 1, "semantic": 1,
                   "latencies_sec": [61.0, 62.0], "tokens": [30, 40], "errors": []}
            bmark.generate_markdown_report({"gemma": {"model": "gemma", "kinds": {"RES": res}}}, report_path)
            content = report_path.read_text(encoding="utf-8")
            self.assertIn("p95 = 61.95 с", content)
            self.assertIn("порог p95 ≤ 60 с **НЕ выполнен**", content)
            self.assertIn("1/2 (50.0%)** со стоп-последовательностью `['\\n']` — порог **НЕ выполнен**", content)
            self.assertNotIn("соблюдён", content)
            self.assertNotIn("100% успешных", content)
            self.assertIn("успешны не все проверки", content)

    def test_t44_verdicts_positive_when_all_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "T44-gemma-res.md"
            res = {"total": 20, "strict": 20, "lenient": 20, "accept": 20, "semantic": 20,
                   "latencies_sec": [3.0] * 20, "tokens": [30] * 20, "errors": []}
            bmark.generate_markdown_report({"gemma": {"model": "gemma", "kinds": {"RES": res}}}, report_path)
            content = report_path.read_text(encoding="utf-8")
            self.assertNotIn("НЕ выполнен", content)
            self.assertIn("все 20 проверок успешны", content)


if __name__ == "__main__":
    unittest.main()
