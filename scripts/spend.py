#!/usr/bin/env python3
"""spend — фактический расход агентов aml-nam по задачам (T45).

Только stdlib. Два независимых источника:
  1. % лимита — по пульсам в events.jsonl (+ archive/events-*.jsonl): падение
     остатка окна 5 ч и недельного между соседними замерами агента. Работает
     для всех трёх агентов (antigravity — только то, что он сам сообщает).
  2. токены — из локальных журналов: Claude Code (~/.claude/projects/**.jsonl,
     usage каждого ответа, дубли по message.id отбрасываются) и Codex
     (~/.codex/sessions rollout-*.jsonl, приращения total_token_usage).
     Берутся только журналы, где встречается маркер проекта ("aml-nam").

Привязка к задаче: запись относится к задаче из последнего пульса агента,
если с того пульса прошло не больше window_min; иначе — «вне пульса».
Подписки общие для всех проектов, поэтому это оценка, а не счёт.

$ считается по ценам API Claude (platform.claude.com/docs/en/about-claude/pricing,
сверено 2026-09-24); для Codex — только если цены заданы в policy.prices.
«Claude-эквивалент» для токенов Codex — грубая оценка «сколько стоило бы, если
бы ту же работу делал Claude» по ценам reference-модели.
"""
from __future__ import annotations

import bisect
import datetime as dt
import glob
import json
import os
from pathlib import Path

MARKER = "aml-nam"

# $ за 1 млн токенов: in, запись кэша 5 мин, запись кэша 1 ч, чтение кэша, out.
CLAUDE_PRICES = {
    "fable-5-1": (10, 12.5, 20, 0.25, 50),
    "fable-5": (10, 12.5, 20, 1, 50),
    "opus-5-5": (4, 5, 8, 0.20, 20),
    "opus-5": (5, 6.25, 10, 0.5, 25),
    "opus-4": (5, 6.25, 10, 0.5, 25),
    "sonnet-5": (2, 2.5, 4, 0.20, 10),
    "sonnet-4": (3, 3.75, 6, 0.3, 15),
    "haiku-4-5": (1, 1.25, 2, 0.1, 5),
}
REFERENCE_MODEL = "opus-5-5"


def _ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _task(v) -> str | None:
    return v if isinstance(v, str) and v.strip() not in ("", "-") else None


def claude_price(model: str | None) -> tuple | None:
    """Самый длинный совпавший префикс: opus-5-5 раньше opus-5."""
    m = (model or "").lower().removeprefix("claude-").split("[")[0]
    best = max((k for k in CLAUDE_PRICES if m.startswith(k)), key=len, default=None)
    return CLAUDE_PRICES.get(best)


# ---------- события и пульсы ----------

def load_events(events_fp: Path, archive_dir: Path) -> list[dict]:
    files = sorted(glob.glob(str(archive_dir / "events-*.jsonl"))) + [str(events_fp)]
    seen, out = set(), []
    for fp in files:
        if not os.path.exists(fp):
            continue
        for line in Path(fp).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line in seen:
                continue
            seen.add(line)
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = _ts(rec.get("ts"))
            if t:
                rec["_t"] = t
                out.append(rec)
    out.sort(key=lambda r: r["_t"])
    return out


class Timeline:
    """Какой задачей занят агент в момент t (по последнему пульсу)."""

    def __init__(self, events: list[dict], window_min: int):
        self.window = dt.timedelta(minutes=window_min)
        self.beats: dict[str, tuple[list, list]] = {}
        for e in events:
            if e.get("kind") == "heartbeat" and e.get("agent"):
                ts, tasks = self.beats.setdefault(e["agent"], ([], []))
                ts.append(e["_t"])
                tasks.append(_task(e.get("task")))

    def task_at(self, agent: str, t: dt.datetime) -> str | None:
        ts, tasks = self.beats.get(agent, ([], []))
        i = bisect.bisect_right(ts, t) - 1
        if i < 0 or t - ts[i] > self.window:
            return None
        return tasks[i]


def limit_spend(events: list[dict], timeline: Timeline, since: dt.datetime) -> list[dict]:
    """Падения остатка % между соседними замерами агента; рост = сброс окна, не расход."""
    last: dict[str, dict] = {}
    out = []
    for e in events:
        if e.get("kind") not in ("heartbeat", "limits") or not e.get("agent"):
            continue
        a = e["agent"]
        prev = last.get(a)
        if prev and e["_t"] >= since:
            # долгий промежуток без замеров: неизвестно, на что ушёл лимит
            task = timeline.task_at(a, prev["_t"]) if e["_t"] - prev["_t"] <= timeline.window else None
            for key, name in (("limit", "pct5h"), ("weekly", "pctweek")):
                p, c = prev.get(key), e.get(key)
                if isinstance(p, (int, float)) and isinstance(c, (int, float)) and c < p:
                    out.append({"t": e["_t"], "agent": a, "task": task, name: p - c})
        merged = dict(prev or {})
        merged.update({k: v for k, v in e.items() if k in ("limit", "weekly") and v is not None})
        merged["_t"] = e["_t"]
        last[a] = merged
    return out


# ---------- токены из журналов ----------

def _recent(pattern: str, since: dt.datetime) -> list[str]:
    cutoff = since.timestamp()
    return [fp for fp in glob.glob(pattern, recursive=True) if os.path.getmtime(fp) >= cutoff]


def claude_tokens(projects_dir: Path, since: dt.datetime, marker: str = MARKER) -> list[dict]:
    seen, out = set(), []
    for fp in _recent(str(projects_dir / "**" / "*.jsonl"), since):
        text = Path(fp).read_text(encoding="utf-8", errors="replace")
        if marker not in text:
            continue
        for line in text.splitlines():
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message")
            if rec.get("type") != "assistant" or not isinstance(msg, dict) or not msg.get("usage"):
                continue
            key = msg.get("id") or rec.get("requestId") or rec.get("uuid")
            t = _ts(rec.get("timestamp"))
            if key in seen or not t or t < since:
                continue
            seen.add(key)
            u = msg["usage"]
            cc = u.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens")
            w5m = cc.get("ephemeral_5m_input_tokens")
            wall = u.get("cache_creation_input_tokens") or 0
            if w1h is None and w5m is None:
                w5m, w1h = wall, 0
            r = {"t": t, "agent": "claude", "model": msg.get("model"),
                 "in": u.get("input_tokens") or 0, "w5m": w5m or 0, "w1h": w1h or 0,
                 "read": u.get("cache_read_input_tokens") or 0, "out": u.get("output_tokens") or 0}
            r["usd"] = claude_usd(r)
            out.append(r)
    return out


def claude_usd(r: dict, model: str | None = None) -> float | None:
    p = claude_price(model or r.get("model"))
    if not p:
        return None
    return (r["in"] * p[0] + r.get("w5m", 0) * p[1] + r.get("w1h", 0) * p[2]
            + r["read"] * p[3] + r["out"] * p[4]) / 1e6


def codex_tokens(sessions_dir: Path, since: dt.datetime, marker: str = MARKER,
                 prices: dict | None = None) -> list[dict]:
    out = []
    for fp in _recent(str(sessions_dir / "**" / "rollout-*.jsonl"), since):
        text = Path(fp).read_text(encoding="utf-8", errors="replace")
        if marker not in text:
            continue
        model, prev = None, None
        for line in text.splitlines():
            if '"model"' in line and '"turn_context"' in line:
                try:
                    model = (json.loads(line).get("payload") or {}).get("model") or model
                except ValueError:
                    pass
            if '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            tot = (((rec.get("payload") or {}).get("info")) or {}).get("total_token_usage")
            if not tot:
                continue
            cur = (tot.get("input_tokens") or 0, tot.get("cached_input_tokens") or 0,
                   tot.get("output_tokens") or 0)
            if prev is None or any(c < p for c, p in zip(cur, prev)):
                d = cur                           # счётчик файла начинается с нуля (или сброшен)
            else:
                d = tuple(c - p for c, p in zip(cur, prev))
            prev = cur
            t = _ts(rec.get("timestamp"))
            if not t or t < since or not any(d):
                continue
            r = {"t": t, "agent": "codex", "model": model, "in": max(d[0] - d[1], 0),
                 "w5m": 0, "w1h": 0, "read": d[1], "out": d[2]}
            p = (prices or {}).get(model or "")
            r["usd"] = ((r["in"] * p["in"] + r["read"] * p.get("read", p["in"]) + r["out"] * p["out"]) / 1e6
                        if p else None)
            r["claude_eq"] = claude_usd(r, REFERENCE_MODEL)
            out.append(r)
    return out


# ---------- сводка ----------

def _bucket() -> dict:
    return {"pct5h": 0, "pctweek": 0, "in": 0, "cache_write": 0, "read": 0, "out": 0,
            "usd": 0.0, "usd_known": False, "claude_eq": 0.0}


def summarize(limit_rows: list[dict], token_rows: list[dict], timeline: Timeline) -> dict:
    agents: dict[str, dict] = {}
    tasks: dict[str, dict[str, dict]] = {}
    for r in limit_rows:
        for b in (agents.setdefault(r["agent"], _bucket()),
                  tasks.setdefault(r["task"] or "—", {}).setdefault(r["agent"], _bucket())):
            b["pct5h"] += r.get("pct5h", 0)
            b["pctweek"] += r.get("pctweek", 0)
    for r in token_rows:
        task = timeline.task_at(r["agent"], r["t"]) or "—"
        for b in (agents.setdefault(r["agent"], _bucket()),
                  tasks.setdefault(task, {}).setdefault(r["agent"], _bucket())):
            b["in"] += r["in"]
            b["cache_write"] += r["w5m"] + r["w1h"]
            b["read"] += r["read"]
            b["out"] += r["out"]
            if r.get("usd") is not None:
                b["usd"] += r["usd"]
                b["usd_known"] = True
            b["claude_eq"] += r.get("claude_eq") or r.get("usd") or 0.0
    for group in [agents, *tasks.values()]:
        for b in group.values():
            b["usd"] = round(b["usd"], 2)
            b["claude_eq"] = round(b["claude_eq"], 2)
    total_eq = sum(b["claude_eq"] for b in agents.values())
    shares = {a: round(100 * b["claude_eq"] / total_eq) for a, b in agents.items()} if total_eq else {}
    return {"agents": agents, "tasks": tasks, "claude_eq_shares": shares}


def collect(events_fp: Path, archive_dir: Path, since: dt.datetime, window_min: int = 60,
            tokens: bool = True, claude_dir: Path | None = None, codex_dir: Path | None = None,
            prices: dict | None = None, marker: str = MARKER) -> dict:
    events = load_events(events_fp, archive_dir)
    tl = Timeline(events, window_min)
    rows = []
    if tokens:
        home = Path.home()
        rows += claude_tokens(claude_dir or home / ".claude" / "projects", since, marker)
        rows += codex_tokens(codex_dir or Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions",
                             since, marker, prices)
    s = summarize(limit_spend(events, tl, since), rows, tl)
    s["since"] = since.isoformat()
    return s


def _k(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def _line(name: str, b: dict) -> str:
    parts = [f"{name:12}", f"5ч {b['pct5h']:>3}%", f"нед {b['pctweek']:>3}%"]
    if b["in"] or b["read"] or b["out"]:
        parts.append(f"токены вход {_k(b['in'] + b['cache_write'])} кэш {_k(b['read'])} выход {_k(b['out'])}")
        if b["usd_known"]:
            parts.append(f"≈${b['usd']:.2f}")
        elif b["claude_eq"]:
            parts.append(f"(как у Claude ≈${b['claude_eq']:.2f})")
    return "  ".join(parts)


def render(s: dict, top: int = 15) -> str:
    lines = [f"Расход агентов с {s['since']} (оценка: подписки общие для всех проектов)", ""]
    for a in sorted(s["agents"]):
        lines.append(_line(a, s["agents"][a]))
    if s["claude_eq_shares"]:
        no_log = [a for a, b in s["agents"].items() if not (b["in"] or b["read"] or b["out"])]
        lines += ["", "Доля в токенах (в ценах Claude): "
                  + ", ".join(f"{a} {p}%" for a, p in sorted(s["claude_eq_shares"].items(), key=lambda x: -x[1])
                              if a not in no_log)
                  + (f"; без журнала токенов (только % лимита): {', '.join(sorted(no_log))}" if no_log else "")]
    lines.append("«—» = без задачи или вне пульса (другие проекты, долгие паузы между пульсами)")
    weight = lambda item: sum(b["pctweek"] * 10 + b["claude_eq"] for b in item[1].values())
    rows = sorted(s["tasks"].items(), key=weight, reverse=True)[:top]
    if rows:
        lines += ["", "По задачам:"]
        for task, per in rows:
            lines.append(f"{task}")
            lines += ["  " + _line(a, b) for a, b in sorted(per.items())]
    return "\n".join(lines)
