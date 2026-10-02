"""Shared test fixture for .agent-sync isolation in hermetic test runs."""
import json
from pathlib import Path


def init_test_sync(sync_dir: Path) -> None:
    sync_dir.mkdir(parents=True, exist_ok=True)
    for d in ["inbox", "history", "archive", "frames"]:
        (sync_dir / d).mkdir(parents=True, exist_ok=True)
    for agent_name in ["antigravity", "claude", "codex"]:
        (sync_dir / "inbox" / f"{agent_name}.md").write_text(f"# Почта для {agent_name}\n", encoding="utf-8")
    (sync_dir / "log.md").write_text("# log\n", encoding="utf-8")
    (sync_dir / "events.jsonl").write_text("", encoding="utf-8")
    (sync_dir / "decisions.md").write_text("", encoding="utf-8")
    (sync_dir / "board.json").write_text(json.dumps({
        "project": "AML NAM",
        "tasks": [
            {"id": "T38", "title": "frames", "status": "in_progress", "assignee": "claude", "critic": "antigravity", "notes": ""},
            {"id": "T41", "title": "guide", "status": "done", "assignee": "antigravity", "critic": "codex", "notes": ""},
            {"id": "T90", "title": "исполнение", "status": "in_progress", "assignee": "antigravity", "critic": "codex", "notes": ""},
            {"id": "T91", "title": "приёмка", "status": "claude", "assignee": "codex", "critic": "claude", "notes": ""},
        ]
    }, indent=2), encoding="utf-8")
    (sync_dir / "agents.json").write_text(json.dumps({
        "policy": {
            "pause_below": 10,
            "resume_at": 15,
            "stale_after_min": 30,
            "offline_after_min": 60,
            "critic_order": ["codex", "claude", "antigravity"]
        },
        "agents": {
            "antigravity": {"role": "worker", "heartbeat": "2026-09-24T00:00:00+03:00", "state": "active", "limit": 80, "default_critic": "codex"},
            "claude": {"role": "curator", "heartbeat": "2026-09-24T00:00:00+03:00", "state": "active", "limit": 80, "default_critic": "codex"},
            "codex": {"role": "critic", "heartbeat": "2026-09-24T00:00:00+03:00", "state": "active", "limit": 80, "default_critic": "claude"},
        },
        "llm": {
            "gemma": {"model": "gemma", "endpoint": "http://127.0.0.1:1234/v1/chat/completions", "speed": 16, "host": "mac", "status": {"online": True}},
            "qwen9b": {"model": "qwen9b", "endpoint": "http://127.0.0.1:1234/v1/chat/completions", "speed": 30, "host": "pc", "status": {"online": True}}
        }
    }, indent=2), encoding="utf-8")
