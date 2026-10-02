#!/usr/bin/env python3
"""Night quality gate. Exit 0 only when every check passes."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOP_LINE = ROOT / ".agent-sync" / "STOP_LINE.md"
LOG_DIR = ROOT / "review" / dt.date.today().isoformat()


def project_python() -> str:
    candidates = (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python")
    return str(next((p for p in candidates if p.is_file()), Path(sys.executable)))


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=15)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def checks(worktree: Path) -> list[tuple[str, list[str], int]]:
    python = project_python()
    node = shutil.which("node") or "node"
    npm = shutil.which("npm") or "npm"
    js_tests = sorted(str(p.relative_to(worktree)) for p in (worktree / "tests").glob("*.test.js"))
    return [
        ("npm ci", [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], 180),
        ("verify.py", [python, "verify.py"], 180),
        ("unittest", [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], 180),
        ("node tests", [node, "--test", *js_tests], 120),
        ("cluster_examples", [python, "scripts/cluster_examples.py", "--check"], 90),
        ("npm audit", [npm, "audit", "--audit-level=high"], 120),
    ]


def run(name: str, command: list[str], timeout: int, worktree: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, cwd=worktree, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        detail = result.stdout + result.stderr
        return result.returncode == 0, f"exit {result.returncode}\n{detail}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main(recheck: int = 1) -> int:
    head = git("rev-parse", "HEAD")
    author = git("show", "-s", "--format=%an <%ae>", head)
    results = []
    temp_parent = Path(tempfile.mkdtemp(prefix="aml-ci-"))
    worktree = temp_parent / "checkout"
    attached = False
    try:
        add = subprocess.run(["git", "worktree", "add", "--detach", str(worktree), head],
                             cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        if add.returncode:
            results.append(("worktree", False, add.stdout + add.stderr))
        else:
            attached = True
            for name, command, timeout in checks(worktree):
                attempts = []
                for _ in range(2):
                    passed, detail = run(name, command, timeout, worktree)
                    attempts.append(detail)
                    if passed:
                        break
                results.append((name, passed, "\nRETRY after first failure\n".join(attempts)))
                print(f"[{'PASS' if passed else 'FAIL'}] {name}"
                      + (" (retry)" if len(attempts) == 2 else ""), flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        results.append(("worktree", False, f"{type(exc).__name__}: {exc}"))
    finally:
        try:
            removed_ok = not attached
            if attached:
                modules = worktree / "node_modules"
                if modules.exists():
                    resolved = modules.resolve()
                    if resolved.parent != worktree.resolve() or modules.is_symlink():
                        raise RuntimeError(f"Unexpected node_modules path: {resolved}")
                    shutil.rmtree(modules)
                removed = subprocess.run(["git", "worktree", "remove", str(worktree)], cwd=ROOT,
                                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                                         timeout=60)
                removed_ok = removed.returncode == 0
                if not removed_ok:
                    results.append(("worktree cleanup", False, removed.stdout + removed.stderr
                                    + f"\nPreserved: {worktree}"))
            if removed_ok:
                resolved = temp_parent.resolve()
                if resolved.parent != Path(tempfile.gettempdir()).resolve() or not resolved.name.startswith("aml-ci-"):
                    raise RuntimeError(f"Unexpected temporary path: {resolved}")
                shutil.rmtree(resolved)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            results.append(("worktree cleanup", False, f"{exc}; preserved: {temp_parent}"))

    if git("rev-parse", "HEAD") != head:
        print("[RETRY] HEAD changed during run; testing the new commit", flush=True)
        if recheck:
            return main(recheck - 1)
        head = git("rev-parse", "HEAD")
        author = git("show", "-s", "--format=%an <%ae>", head)
        results.append(("HEAD", False, "HEAD changed during two consecutive gates; rerun when commits settle."))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / "T39-ci.log"
    log.write_text("\n".join(f"## {name}: {'PASS' if ok else 'FAIL'}\n{detail}"
                             for name, ok, detail in results) + "\n", encoding="utf-8")
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        STOP_LINE.parent.mkdir(parents=True, exist_ok=True)
        STOP_LINE.write_text(f"# Quality gate failed\n\nChecks: {', '.join(failed)}\n"
                             f"HEAD: {head}\nAuthor: {author}\n"
                             f"Details: {log.relative_to(ROOT).as_posix()}\n", encoding="utf-8")
        return 1
    STOP_LINE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
