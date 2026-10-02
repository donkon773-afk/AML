"""T39: a broken test raises the stop line, a repaired test clears it."""
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci  # noqa: E402


class QualityGateTests(unittest.TestCase):
    def test_head_isolation_stop_line_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            sample = tests / "test_sample.py"
            command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
            def git(*args):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

            git("init", "-q")
            git("config", "user.name", "CI Test")
            git("config", "user.email", "ci-test@example.invalid")
            with (mock.patch.object(ci, "ROOT", root),
                  mock.patch.object(ci, "STOP_LINE", root / ".agent-sync" / "STOP_LINE.md"),
                  mock.patch.object(ci, "LOG_DIR", root / "review"),
                  mock.patch.object(ci, "checks", return_value=[("unittest", command, 30)])):
                sample.write_text("import unittest\nclass Good(unittest.TestCase):\n"
                                  "    def test_gate(self): self.assertTrue(True)\n", encoding="utf-8")
                git("add", "tests/test_sample.py")
                git("commit", "-qm", "good")
                sample.write_text("import unittest\nclass Broken(unittest.TestCase):\n"
                                  "    def test_gate(self): self.assertTrue(False)\n", encoding="utf-8")
                self.assertEqual(ci.main(), 0, "uncommitted broken test must not stop clean HEAD")
                self.assertFalse(ci.STOP_LINE.exists())
                git("add", "tests/test_sample.py")
                git("commit", "-qm", "broken")
                self.assertEqual(ci.main(), 1)
                stop = ci.STOP_LINE.read_text(encoding="utf-8")
                self.assertIn("unittest", stop)
                self.assertIn(ci.git("rev-parse", "HEAD"), stop)
                sample.write_text("import unittest\nclass Repaired(unittest.TestCase):\n"
                                  "    def test_gate(self): self.assertTrue(True)\n", encoding="utf-8")
                git("add", "tests/test_sample.py")
                git("commit", "-qm", "repaired")
                self.assertEqual(ci.main(), 0)
                self.assertFalse(ci.STOP_LINE.exists())
                with mock.patch.object(ci, "run", side_effect=[(False, "flaky"), (True, "pass")]) as run:
                    self.assertEqual(ci.main(), 0)
                self.assertEqual(run.call_count, 2)
                self.assertIn("RETRY after first failure", (root / "review" / "T39-ci.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
