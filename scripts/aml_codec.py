"""AML v2.0 (Agent Machine Language) Reference Bijective Codec & Delta Engine.

High-density, BPE-aligned machine language for AI-to-AI (A2A) communication.
Complies with NeroAI Eye architecture specifications.
"""

from __future__ import annotations

import copy
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

ACTION_MAP = {
    "ADV": "ADVANCE",
    "ATK": "ASSAULT",
    "HLD": "HOLD",
    "RET": "RETREAT",
    "REC": "RECON",
    "FIRE": "REQUEST_FIRE",
    "BRD": "BOARD",
    "DIS": "DISEMBARK",
    "REP": "REPAIR",
    "ROE": "SET_ROE",
    "SEC": "SET_SECTOR",
    "RPT": "REPORT",
    "CAN": "CANCEL",
    "SUPP": "REQUEST_SUPPORT",
    "INTEL": "SHARE_INTEL",
    "NEG": "NEGOTIATE",
    "DES": "DESIGNATE",
    "AUTO": "AUTONOMY",
    # Cluster profile (payload schema s:2, T37): task/agent coordination.
    "RSG": "REASSIGN",
    "PAU": "PAUSE",
    "RSM": "RESUME",
    "RTN": "RETURN",
    "APV": "APPROVE"
}

REV_ACTION_MAP = {v: k for k, v in ACTION_MAP.items()}

KIND_MAP = {
    "grp": "group",
    "unt": "unit",
    "veh": "vehicle",
    "cnt": "contact"
}

REV_KIND_MAP = {v: k for k, v in KIND_MAP.items()}

# AML's compact role codes vs. NAM/1's full-word role enum
# (nam.schema.json $defs.entity.properties.role). These never matched before
# this fix: every "INF"/"REC"/"MEC"/"ARM"/"UNK" entity produced by the AML
# encoder failed NAM/1 schema validation the moment it was decoded and
# re-checked -- nothing in the original test suite ever ran that check.
ROLE_MAP = {
    "INF": "INFANTRY",
    "REC": "RECON",
    "MEC": "MECHANIZED",
    "ARM": "ARMORED",
    "UNK": "UNKNOWN"
}

REV_ROLE_MAP = {v: k for k, v in ROLE_MAP.items()}

# ---- Cluster profile (T37) ----------------------------------------------
# The field vocabulary above (groups, coordinates, ammo) describes an Arma
# battlefield. An agent cluster exchanges tasks and agents instead. Those rows
# ride in the same STATE/DELTA/PROP frames but only under payload schema
# CLUSTER_SCHEMA ("|s:2", NAM/2), and the two vocabularies never mix in one
# frame -- see _check_profile().
CLUSTER_SCHEMA = 2
CLUSTER_KINDS = ("task", "agent")
CLUSTER_ACTIONS = ("REASSIGN", "PAUSE", "RESUME", "RETURN", "APPROVE")
TASK_STATUS_MAP = {
    "BKL": "backlog",
    "INP": "in_progress",
    "REV": "review",
    "APR": "approval",
    "DON": "done",
    "CLS": "closed"
}
REV_TASK_STATUS_MAP = {v: k for k, v in TASK_STATUS_MAP.items()}
AGENT_STATE_MAP = {
    "ACT": "active",
    "STL": "stale",
    "WAT": "watching",
    "OFF": "offline"
}
REV_AGENT_STATE_MAP = {v: k for k, v in AGENT_STATE_MAP.items()}


class AMLError(ValueError):
    """Protocol or encoding error in AML payload."""
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AMLError(message)


def pack_metric(val: Optional[float]) -> str:
    """Pack float 0.0..1.0 or None to 0..100 or -"""
    if val is None:
        return "-"
    _require(type(val) in (int, float) and math.isfinite(val) and 0 <= val <= 1, "Invalid metric")
    percent = round(val * 100)
    _require(abs(val - percent / 100) <= 1e-12, "Metric precision loss; use NAM JSON")
    return str(percent)


def unpack_metric(s: str) -> Optional[float]:
    """Unpack 0..100 or - to float 0.0..1.0 or None"""
    if s == "-":
        return None
    _require(re.fullmatch(r"[0-9]+", s) is not None and 0 <= int(s) <= 100, "Invalid metric")
    return int(s) / 100


def pack_coords(pos: List[Union[int, float]]) -> str:
    """Format position list to @x,y or @x,y,z"""
    _require(isinstance(pos, list) and len(pos) == 3, "Expected three coordinates")
    _require(all(type(x) in (int, float) and math.isfinite(x) and x == int(x) and abs(x) <= 100000 for x in pos),
             "Coordinate precision/range unsupported; use NAM JSON")
    x, y, z = map(int, pos)
    return f"@{x},{y},{z}" if z else f"@{x},{y}"


def unpack_coords(s: str) -> List[int]:
    """Parse @x,y[,z] to [x, y, z]"""
    _require(s.startswith("@"), f"Invalid coordinate format: {s}")
    parts = s[1:].split(",")
    _require(len(parts) in (2, 3) and all(re.fullmatch(r"-?[0-9]+", p) for p in parts), f"Malformed coordinates: {s}")
    try:
        coords = [int(p) for p in parts]
    except ValueError as exc:
        raise AMLError(f"Non-numeric coordinates: {s}") from exc
    if len(coords) == 2:
        coords.append(0)
    return coords


def pack_metrics(hp: Optional[float], ammo: Optional[float], supp: Optional[float]) -> str:
    return f"%H{pack_metric(hp)}A{pack_metric(ammo)}S{pack_metric(supp)}"


def unpack_metrics(s: str) -> Dict[str, Optional[float]]:
    _require(isinstance(s, str), "Invalid metrics")
    m = re.match(r"^%H(-|\d+)A(-|\d+)S(-|\d+)$", s)
    _require(m is not None, "Invalid metrics")
    return {
        "hp": unpack_metric(m.group(1)),
        "ammo": unpack_metric(m.group(2)),
        "suppression": unpack_metric(m.group(3))
    }


def pack_observed(observed_ms: Optional[int]) -> str:
    """Pack the mandatory observation timestamp as a single 't<ms>' token.

    NAM/1 requires observed_ms on every entity (staleness/replay checks depend
    on it). AML must carry it on the wire instead of letting the decoder
    fabricate 'now' -- a fabricated timestamp silently defeats every
    freshness check downstream.
    """
    _require(observed_ms is not None, "Entity is missing required 'observed_ms'")
    _require(type(observed_ms) is int and 0 <= observed_ms <= 9007199254740991, "Invalid observation time")
    return f"t{observed_ms}"


def unpack_observed(token: str) -> int:
    _require(token.startswith("t") and token[1:].lstrip("-").isdigit(),
              f"Missing/invalid observation timestamp token: {token!r}")
    return int(token[1:])


def pack_limit(val: Optional[int]) -> str:
    """Remaining limit percent 0..100, or '-' when the agent cannot report it."""
    if val is None:
        return "-"
    _require(type(val) is int and 0 <= val <= 100, "Invalid limit percent")
    return str(val)


def unpack_limit(s: str) -> Optional[int]:
    if s == "-":
        return None
    _require(re.fullmatch(r"[0-9]+", s) is not None and 0 <= int(s) <= 100, "Invalid limit percent")
    return int(s)


def encode_cluster_row(ent: Dict[str, Any], prefix: str) -> str:
    """#T13:task:INP:antigravity:codex:P1:t<ms>[:dep:#T17,#T18]
    #codex:agent:ACT:L72:W45:t<ms>[:tsk:#T34]"""
    observed = pack_observed(ent.get("observed_ms"))
    if ent["kind"] == "task":
        _require(ent.get("status") in REV_TASK_STATUS_MAP, "Invalid task status")
        _require(type(ent.get("priority")) is int and 0 <= ent["priority"] <= 99, "Invalid task priority")
        row = (f"{prefix}{ent['id']}:task:{REV_TASK_STATUS_MAP[ent['status']]}:"
               f"{ent['assignee']}:{ent['critic']}:P{ent['priority']}:{observed}")
        if ent.get("depends"):
            row += ":dep:" + ",".join(f"#{d}" for d in ent["depends"])
        return row
    _require(ent.get("state") in REV_AGENT_STATE_MAP, "Invalid agent state")
    row = (f"{prefix}{ent['id']}:agent:{REV_AGENT_STATE_MAP[ent['state']]}:"
           f"L{pack_limit(ent.get('limit'))}:W{pack_limit(ent.get('weekly'))}:{observed}")
    if ent.get("task"):
        row += f":tsk:#{ent['task']}"
    return row


def encode_entity_row(ent: Dict[str, Any], prefix: str = "#") -> str:
    """Shared row-builder for STATE entities and DELTA '+' upserts."""
    if ent.get("kind") in CLUSTER_KINDS:
        return encode_cluster_row(ent, prefix)
    eid = ent["id"]
    ekind = REV_KIND_MAP.get(ent.get("kind"), ent.get("kind", "grp"))
    role = REV_ROLE_MAP.get(ent.get("role"), ent.get("role", "UNK"))
    count = ent.get("count", 1)
    coords = pack_coords(ent.get("pos", [0, 0]))
    metrics = pack_metrics(ent.get("hp"), ent.get("ammo"), ent.get("suppression"))
    observed = pack_observed(ent.get("observed_ms"))
    row = f"{prefix}{eid}:{ekind}:{role}:{count}:{coords}:{metrics}:{observed}"
    if ent.get("callsign"):
        escaped = ent["callsign"].replace('"', '""')
        row += f':C"{escaped}"'
    if ent.get("source"):
        row += f":src:{ent['source']}"
    return row


def split_row(row: str) -> List[str]:
    parts = []
    current = []
    in_quote = False
    i = 0
    while i < len(row):
        ch = row[i]
        if ch == '"':
            if in_quote and i + 1 < len(row) and row[i + 1] == '"':
                current.append('"')
                i += 1
            else:
                in_quote = not in_quote
                current.append(ch)
        elif ch == ":" and not in_quote:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _preserved(source, decoded, path="message"):
    """No supplied field may disappear/change. Decoder defaults are documented.
    Legacy short role aliases are normalized. Float tolerance is ONLY 1e-12.
    Unrepresentable data must use NAM JSON, never silently lose precision.
    """
    if isinstance(source, dict):
        _require(isinstance(decoded, dict), f"Cannot preserve {path}; use NAM JSON")
        for key, value in source.items():
            _require(key in decoded, f"Cannot preserve {path}.{key}; use NAM JSON/full upsert")
            if key == "role":
                value = ROLE_MAP.get(value, value)
            _preserved(value, decoded[key], f"{path}.{key}")
    elif isinstance(source, list):
        _require(isinstance(decoded, list) and len(source) == len(decoded), f"Cannot preserve {path}")
        for i, value in enumerate(source):
            _preserved(value, decoded[i], f"{path}[{i}]")
    elif type(source) in (int, float) and type(decoded) in (int, float):
        _require(math.isfinite(source) and abs(source - decoded) <= 1e-12,
                 f"Precision loss at {path}; use NAM JSON")
    else:
        _require(type(source) is type(decoded) and source == decoded,
                 f"Cannot preserve {path}; use NAM JSON")


def _check_profile(msg: Dict[str, Any]) -> None:
    """Field and cluster vocabularies never share a frame; cluster needs s:2."""
    body = msg.get("body", {})
    rows = list(body.get("entities", [])) + list(body.get("upsert", []))
    acts = body.get("actions", []) if msg.get("kind") == "proposal" else []
    cluster = [r for r in rows if r.get("kind") in CLUSTER_KINDS] + \
              [a for a in acts if a.get("type") in CLUSTER_ACTIONS]
    if msg.get("schema") == CLUSTER_SCHEMA:
        _require(len(cluster) == len(rows) + len(acts),
                 "Cluster profile (s:2) admits only task/agent rows and cluster actions")
        _require(not body.get("updates"), "Cluster profile uses full '+' upserts, not partial updates")
    else:
        _require(not cluster, "task/agent rows and cluster actions require payload schema s:2")


class AMLCodec:
    """AML/2 compact projection with explicit rejection of unrepresentable fields."""

    @classmethod
    def encode(cls, msg: Dict[str, Any]) -> str:
        try:
            _require(isinstance(msg, dict) and all(k in msg for k in
                     ("v", "id", "session", "sender", "recipient", "seq", "sent_ms", "ttl_ms", "kind", "body")),
                     "Complete NAM envelope required; timestamps are never invented")
            return cls._encode(msg)
        except AMLError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            raise AMLError("Invalid or unsupported AML input: " + str(exc)) from exc

    @classmethod
    def decode(cls, raw: str) -> Dict[str, Any]:
        try:
            return cls._decode(raw)
        except AMLError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            raise AMLError("Invalid AML text: " + str(exc)) from exc

    @classmethod
    def _encode(cls, msg: Dict[str, Any]) -> str:
        _require(isinstance(msg, dict), "Message must be a dictionary")
        kind = msg.get("kind", "").upper()
        session = msg.get("session", "default")
        sender = msg.get("sender", "agent")
        recipient = msg.get("recipient", "peer")
        seq = msg.get("seq", 1)
        sent_ms = msg.get("sent_ms", int(time.time() * 1000))
        ttl_ms = msg.get("ttl_ms", 30000)
        body = msg.get("body", {})

        # NAM/2 additions ride at the front of the variable header so every
        # message kind picks them up without a per-kind branch.
        header_extra = []
        if "schema" in msg:
            header_extra.append(f"s:{msg['schema']}")
        if "trace_id" in msg:
            header_extra.append(f"tr:{msg['trace_id']}")
        lines = []

        if kind == "STATE":
            if "world" in body:
                header_extra.append(f"w:{body['world']}")
            if "rev" in body:
                header_extra.append(f"r:{body['rev']}")
            header = f"!AML:2|STATE|{session}|{sender}→{recipient}|{seq}|{sent_ms}|{ttl_ms}"
            if header_extra:
                header += "|" + "|".join(header_extra)
            lines.append(header)

            for ent in body.get("entities", []):
                lines.append(encode_entity_row(ent, prefix="#"))

        elif kind == "DELTA":
            if "world" in body:
                header_extra.append(f"w:{body['world']}")
            if "base" in body:
                header_extra.append(f"b:{body['base']}")
            if "rev" in body:
                header_extra.append(f"r:{body['rev']}")
            header = f"!AML:2|DELTA|{session}|{sender}→{recipient}|{seq}|{sent_ms}|{ttl_ms}"
            if header_extra:
                header += "|" + "|".join(header_extra)
            lines.append(header)

            for ent in body.get("upsert", []):
                lines.append(encode_entity_row(ent, prefix="+#"))

            for upd in body.get("updates", []):
                row = f"~#{upd['id']}"
                if "pos" in upd:
                    row += f":{pack_coords(upd['pos'])}"
                if any(k in upd for k in ("hp", "ammo", "suppression")):
                    row += f":{pack_metrics(upd.get('hp'), upd.get('ammo'), upd.get('suppression'))}"
                if "role" in upd:
                    row += f":role:{REV_ROLE_MAP.get(upd['role'], upd['role'])}"
                if "count" in upd:
                    row += f":cnt:{upd['count']}"
                lines.append(row)

            for rem_id in body.get("remove", []):
                lines.append(f"-#{rem_id}")

        elif kind in ("PROP", "PROPOSAL"):
            if "world" in body:
                header_extra.append(f"w:{body['world']}")
            if "rev" in body:
                header_extra.append(f"r:{body['rev']}")
            if "state_sender" in body:
                header_extra.append(f"by:{body['state_sender']}")
            if "reason" in body:
                code = body["reason"].get("code", "UNKNOWN")
                ev_list = body["reason"].get("evidence", [])
                ev = f"[{','.join(f'#{e}' for e in ev_list)}]" if ev_list else ""
                header_extra.append(f"rs:{code}{ev}")
            header = f"!AML:2|PROP|{session}|{sender}→{recipient}|{seq}|{sent_ms}|{ttl_ms}"
            if header_extra:
                header += "|" + "|".join(header_extra)
            lines.append(header)

            for act in body.get("actions", []):
                _require(act.get("op"), "Proposal action must include 'op' (NAM/1 requires it; "
                                         "AML must not synthesize IDs on decode)")
                atype = REV_ACTION_MAP.get(act.get("type"), act.get("type"))
                target = f"#{act.get('target', 'ALL')}"
                row = f"${atype}:{target}"
                if "position" in act and act["position"]:
                    row += f":{pack_coords(act['position'])}"
                arg = act.get("argument") or act.get("to") or act.get("roe") or act.get("support")
                if arg:
                    row += f":{arg}"
                row += f":#{act['op']}"
                lines.append(row)

        elif kind == "QUERY":
            req = body.get("request")
            if req == "KEYFRAME":
                header_extra.append("?KEYFRAME")
            elif req == "STATUS":
                header_extra.append(f"?STATUS:#{body.get('related', 'ALL')}")
            elif req == "EXPLAIN":
                _require(not body.get("topic"), "topic is not in NAM schema; use related only")
                topic = f":{body['topic']}" if body.get("topic") else ""
                header_extra.append(f"?WHY:#{body.get('related', 'ALL')}{topic}")
            header = f"!AML:2|QUERY|{session}|{sender}→{recipient}|{seq}|{sent_ms}|{ttl_ms}"
            if header_extra:
                header += "|" + "|".join(header_extra)
            lines.append(header)

        elif kind in ("RES", "RESULT"):
            status = body.get("status")
            codes = {"ACCEPTED": "ACC", "EXECUTED": "EXEC", "REJECTED": "REJ", "FAILED": "FAIL"}
            _require(status in codes and body.get("related"), "Result needs supported status and related")
            header_extra.append(f"rel:{body['related']}")
            ops = ",".join(f"#{o}" for o in body.get("operations", []))
            header_extra.append(f"*{codes[status]}:{ops}")
            if "reason" in body:
                r = body["reason"]
                evidence = "[" + ",".join("#" + x for x in r["evidence"]) + "]" if r["evidence"] else ""
                header_extra.append(f"rs:{r['code']}{evidence}")
            lines.append(f"!AML:2|RES|{session}|{sender}→{recipient}|{seq}|{sent_ms}|{ttl_ms}|" + "|".join(header_extra))

        elif kind in ("ERR", "ERROR"):
            header_extra.append(f"err:{body.get('code', 'INVALID')}")
            if "related" in body:
                header_extra.append(f"rel:{body['related']}")
            header = f"!AML:2|ERR|{session}|{sender}→{recipient}|{seq}|{sent_ms}|{ttl_ms}"
            if header_extra:
                header += "|" + "|".join(header_extra)
            lines.append(header)

        elif kind == "HANDOFF":
            header_extra.append(f"tsk:{body.get('task', 'TASK')}")
            header_extra.append(f"st:{body.get('status', 'IN_PROGRESS')}")
            header = f"!AML:2|HANDOFF|{session}|{sender}→{recipient}|{seq}|{sent_ms}|{ttl_ms}"
            if header_extra:
                header += "|" + "|".join(header_extra)
            lines.append(header)
            if body.get("summary"):
                lines.append(f'SUM:"{body["summary"].replace(chr(34), chr(34)*2)}"')
            for art in body.get("artifacts", []):
                lines.append(f"ART:{art['path']}:{art['sha256']}")
            for chk in body.get("checks", []):
                ev = chk.get("evidence", "").replace('"', '""')
                lines.append(f'CHK:{chk["name"]}:{chk["result"]}:"{ev}"')
            if body.get("next"):
                lines.append(f'NXT:"{body["next"].replace(chr(34), chr(34)*2)}"')
        else:
            raise AMLError(f"Unsupported message kind: {kind}")

        wire = "\n".join(lines)
        _preserved(msg, cls.decode(wire))
        return wire

    @classmethod
    def _decode(cls, raw: str) -> Dict[str, Any]:
        _require(isinstance(raw, str) and raw.strip(), "Input must be non-empty string")
        _require(len(raw.encode("utf-8")) <= 65536, "Message too large")
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        _require(len(lines) > 0, "No lines in payload")

        header = lines[0]
        _require(header.startswith("!AML:2|"), "Invalid header or version mismatch")

        parts = header[7:].split("|")
        _require(len(parts) >= 6, "Malformed AML envelope")

        kind = parts[0].upper()
        _require(kind in ("STATE", "DELTA", "PROP", "QUERY", "RES", "ERR", "HANDOFF"), "Unsupported kind")
        session = parts[1]
        route = parts[2].split("→")
        _require(len(route) == 2, "Malformed sender→recipient routing")
        sender, recipient = route[0], route[1]
        _require(all(re.fullmatch(r"[0-9]+", p) for p in parts[3:6]), "Invalid envelope number")
        seq = int(parts[3])
        sent_ms = int(parts[4])
        ttl_ms = int(parts[5])

        extra_map = {}
        for param in parts[6:]:
            if param.startswith("?"):
                extra_map["QUERY"] = param[1:]
            elif param.startswith("*"):
                extra_map["RESULT"] = param[1:]
            elif ":" in param:
                k, v = param.split(":", 1)
                extra_map[k] = v

        body_lines = lines[1:]
        std_kind = {"PROP": "proposal", "RES": "result", "ERR": "error"}.get(kind, kind.lower())

        msg: Dict[str, Any] = {
            "v": "NAM/2" if ("s" in extra_map or "tr" in extra_map) else "NAM/1",
            "id": f"m{seq}",
            "session": session,
            "sender": sender,
            "recipient": recipient,
            "seq": seq,
            "sent_ms": sent_ms,
            "ttl_ms": ttl_ms,
            "kind": std_kind,
            "body": {}
        }
        if "s" in extra_map:
            msg["schema"] = int(extra_map["s"])
        if "tr" in extra_map:
            msg["trace_id"] = extra_map["tr"]

        if kind == "STATE":
            msg["body"] = {
                "world": extra_map.get("w", "world"),
                "rev": int(extra_map.get("r", 1)),
                "entities": []
            }
            for line in body_lines:
                if line.startswith("#"):
                    msg["body"]["entities"].append(cls._parse_entity(line))

        elif kind == "DELTA":
            msg["body"] = {
                "world": extra_map.get("w", "world"),
                "base": int(extra_map.get("b", 0)),
                "rev": int(extra_map.get("r", 1)),
                "upsert": [],
                "updates": [],
                "remove": []
            }
            for line in body_lines:
                if line.startswith("+#"):
                    msg["body"]["upsert"].append(cls._parse_entity(line[1:]))
                elif line.startswith("-#"):
                    msg["body"]["remove"].append(line[2:])
                elif line.startswith("~#"):
                    msg["body"]["updates"].append(cls._parse_delta_update(line))

        elif kind in ("PROP", "PROPOSAL"):
            reason_code = "UNKNOWN"
            evidence = []
            if "rs" in extra_map:
                m = re.match(r"^([A-Z_]+)(?:\[(.*)\])?$", extra_map["rs"])
                if m:
                    reason_code = m.group(1)
                    if m.group(2):
                        evidence = [e.replace("#", "").strip() for e in m.group(2).split(",") if e.strip()]

            msg["body"] = {
                "world": extra_map.get("w", "world"),
                "rev": int(extra_map.get("r", 1)),
                "state_sender": extra_map.get("by", "observer"),
                "reason": {"code": reason_code, "evidence": evidence},
                "actions": []
            }
            for line in body_lines:
                if line.startswith("$"):
                    act = cls._parse_action(line)
                    if act:
                        msg["body"]["actions"].append(act)

        elif kind == "QUERY":
            q = extra_map.get("QUERY", "")
            if q.startswith("KEYFRAME"):
                msg["body"] = {"request": "KEYFRAME", "related": "ALL"}
            elif q.startswith("STATUS"):
                rel = q.replace("STATUS:#", "").replace("STATUS:", "")
                msg["body"] = {"request": "STATUS", "related": rel or "ALL"}
            elif q.startswith("WHY"):
                sub = q[4:].split(":")
                _require(len(sub) == 1, "topic is not in NAM schema")
                msg["body"] = {
                    "request": "EXPLAIN",
                    "related": sub[0].replace("#", ""),
                }

        elif kind in ("RES", "RESULT"):
            res = extra_map.get("RESULT", "")
            match = re.fullmatch(r"(ACC|EXEC|REJ|FAIL):(.*)", res)
            _require(match is not None and bool(extra_map.get("rel")), "Result needs supported status and related")
            code, ops = match.groups()
            _require(not ops or re.fullmatch(r"#[A-Za-z0-9_.-]+(?:,#[A-Za-z0-9_.-]+)*", ops), "Invalid operations")
            msg["body"] = {"related": extra_map["rel"], "status": {"ACC": "ACCEPTED", "EXEC": "EXECUTED", "REJ": "REJECTED", "FAIL": "FAILED"}[code],
                           "operations": [x[1:] for x in ops.split(",")] if ops else []}
            if "rs" in extra_map:
                reason = re.fullmatch(r"([A-Z_]+)(?:\[(#[A-Za-z0-9_.-]+(?:,#[A-Za-z0-9_.-]+)*)\])?", extra_map["rs"])
                _require(reason is not None, "Invalid reason")
                msg["body"]["reason"] = {"code": reason[1], "evidence": [x[1:] for x in reason[2].split(",")] if reason[2] else []}

        elif kind in ("ERR", "ERROR"):
            msg["body"] = {
                "code": extra_map.get("err", "INVALID"),
                "related": extra_map.get("rel", "m0")
            }

        elif kind == "HANDOFF":
            msg["body"] = {
                "task": extra_map.get("tsk", "TASK"),
                "status": extra_map.get("st", "IN_PROGRESS"),
                "summary": "",
                "artifacts": [],
                "checks": [],
                "next": ""
            }
            for line in body_lines:
                if line.startswith('SUM:"') and line.endswith('"'):
                    msg["body"]["summary"] = line[5:-1].replace('""', '"')
                elif line.startswith("ART:"):
                    p = line[4:].split(":")
                    msg["body"]["artifacts"].append({"path": p[0], "sha256": p[1]})
                elif line.startswith("CHK:"):
                    m = re.match(r"^([^:]+):([^:]+):\"(.*)\"$", line[4:])
                    if m:
                        msg["body"]["checks"].append({
                            "name": m.group(1),
                            "result": m.group(2),
                            "evidence": m.group(3).replace('""', '"')
                        })
                elif line.startswith('NXT:"') and line.endswith('"'):
                    msg["body"]["next"] = line[5:-1].replace('""', '"')

        _check_profile(msg)
        return msg

    @classmethod
    def _parse_entity(cls, line: str) -> Dict[str, Any]:
        parts = split_row(line[1:])
        if len(parts) > 1 and parts[1] in CLUSTER_KINDS:
            return cls._parse_cluster_entity(parts, line)
        _require(len(parts) >= 7, f"Entity row missing required fields (incl. observation time): {line!r}")
        eid = parts[0]
        raw_kind = parts[1]
        ekind = KIND_MAP.get(raw_kind, raw_kind)
        role = ROLE_MAP.get(parts[2], parts[2])
        count = int(parts[3])
        pos = unpack_coords(parts[4])
        metrics = unpack_metrics(parts[5])
        observed_ms = unpack_observed(parts[6])

        ent: Dict[str, Any] = {
            "id": eid,
            "kind": ekind,
            "role": role,
            "count": count,
            "pos": pos,
            "hp": metrics["hp"],
            "ammo": metrics["ammo"],
            "suppression": metrics["suppression"],
            "observed_ms": observed_ms
        }

        i = 7
        while i < len(parts):
            p = parts[i]
            if p.startswith('C"') and p.endswith('"'):
                ent["callsign"] = p[2:-1].replace('""', '"')
            elif p == "src" and i + 1 < len(parts):
                ent["source"] = parts[i + 1]
                i += 1
            elif p.startswith("src:"):
                ent["source"] = p[4:]
            i += 1

        return ent

    @classmethod
    def _parse_cluster_entity(cls, parts: List[str], line: str) -> Dict[str, Any]:
        if parts[1] == "task":
            _require(len(parts) in (7, 9), f"Task row needs status, assignee, critic, priority, time: {line!r}")
            _require(parts[2] in TASK_STATUS_MAP, "Invalid task status")
            _require(re.fullmatch(r"P[0-9]{1,2}", parts[5]) is not None, "Invalid task priority")
            ent: Dict[str, Any] = {
                "id": parts[0], "kind": "task", "status": TASK_STATUS_MAP[parts[2]],
                "assignee": parts[3], "critic": parts[4], "priority": int(parts[5][1:]),
                "observed_ms": unpack_observed(parts[6]), "depends": []
            }
            if len(parts) == 9:
                _require(parts[7] == "dep" and re.fullmatch(r"#[A-Za-z0-9_.-]+(?:,#[A-Za-z0-9_.-]+)*", parts[8]),
                         "Invalid task dependencies")
                ent["depends"] = [d[1:] for d in parts[8].split(",")]
            return ent
        _require(len(parts) in (6, 8), f"Agent row needs state, limit, weekly, time: {line!r}")
        _require(parts[2] in AGENT_STATE_MAP, "Invalid agent state")
        _require(parts[3].startswith("L") and parts[4].startswith("W"), "Invalid agent limits")
        ent = {
            "id": parts[0], "kind": "agent", "state": AGENT_STATE_MAP[parts[2]],
            "limit": unpack_limit(parts[3][1:]), "weekly": unpack_limit(parts[4][1:]),
            "observed_ms": unpack_observed(parts[5])
        }
        if len(parts) == 8:
            _require(parts[6] == "tsk" and re.fullmatch(r"#[A-Za-z0-9_.-]+", parts[7]), "Invalid agent task")
            ent["task"] = parts[7][1:]
        return ent

    @classmethod
    def _parse_delta_update(cls, line: str) -> Dict[str, Any]:
        parts = line[2:].split(":")
        eid = parts[0]
        upd: Dict[str, Any] = {"id": eid}
        i = 1
        while i < len(parts):
            p = parts[i]
            if p.startswith("@"):
                _require("pos" not in upd, "Duplicate update field")
                upd["pos"] = unpack_coords(p)
            elif p.startswith("%"):
                m = unpack_metrics(p)
                for key, value in m.items():
                    if value is not None:
                        _require(key not in upd, "Duplicate update field")
                        upd[key] = value
            elif p in ("role", "cnt"):
                _require(i + 1 < len(parts), "Missing update value")
                key = "role" if p == "role" else "count"
                _require(key not in upd, "Duplicate update field")
                i += 1
                _require(p == "role" or re.fullmatch(r"[0-9]+", parts[i]), "Invalid count")
                upd[key] = ROLE_MAP.get(parts[i], parts[i]) if p == "role" else int(parts[i])
            else:
                raise AMLError("Unknown update token")
            i += 1
        _require(len(upd) > 1, "Empty partial update")
        return upd

    @classmethod
    def _parse_action(cls, line: str) -> Dict[str, Any]:
        parts = line[1:].split(":")
        raw_type = parts[0]
        atype = ACTION_MAP.get(raw_type, raw_type)
        target = parts[1].replace("#", "") if len(parts) > 1 else "ALL"
        act: Dict[str, Any] = {"type": atype, "target": target}

        for p in parts[2:]:
            if p.startswith("@"):
                act["position"] = unpack_coords(p)
            elif p.startswith("#"):
                act["op"] = p[1:]
            elif p in ("HOLD_FIRE", "RETURN_FIRE", "OPEN_FIRE"):
                act["roe"] = p
            elif p in ("MORTAR", "ARTILLERY", "CAS"):
                act["support"] = p
            elif atype == "REASSIGN":
                act["to"] = p
            else:
                act["argument"] = p

        _require("op" in act, f"Action row is missing required '#op' operation id: {line!r}")
        return act

    @classmethod
    def apply_delta(cls, state_msg: Dict[str, Any], delta_msg: Dict[str, Any]) -> Dict[str, Any]:
        _require(state_msg and "body" in state_msg and "entities" in state_msg["body"], "Invalid base state")
        _require(delta_msg and "body" in delta_msg, "Invalid delta message")
        _require(
            delta_msg["body"].get("base") == state_msg["body"].get("rev"),
            f"Delta base mismatch: expected {state_msg['body'].get('rev')}, got {delta_msg['body'].get('base')}"
        )

        entities_map = {ent["id"]: copy.deepcopy(ent) for ent in state_msg["body"]["entities"]}

        for rem_id in delta_msg["body"].get("remove", []):
            entities_map.pop(rem_id, None)

        for upd in delta_msg["body"].get("updates", []):
            eid = upd["id"]
            if eid in entities_map:
                cur = entities_map[eid]
                if "pos" in upd:
                    cur["pos"] = upd["pos"]
                if "hp" in upd:
                    cur["hp"] = upd["hp"]
                if "ammo" in upd:
                    cur["ammo"] = upd["ammo"]
                if "suppression" in upd:
                    cur["suppression"] = upd["suppression"]
                if "role" in upd:
                    cur["role"] = upd["role"]
                if "count" in upd:
                    cur["count"] = upd["count"]
                # Partial fields are not proof that the whole entity was observed.
                # Preserve observation time; use full upsert to refresh it.

        for up in delta_msg["body"].get("upsert", []):
            entities_map[up["id"]] = copy.deepcopy(up)

        res = copy.deepcopy(state_msg)
        res["seq"] = delta_msg.get("seq", state_msg.get("seq", 1) + 1)
        _require("sent_ms" in delta_msg, "Delta requires sent_ms")
        res["sent_ms"] = delta_msg["sent_ms"]
        res["id"] = delta_msg["id"]
        res["body"]["rev"] = delta_msg["body"].get("rev", state_msg["body"].get("rev", 1) + 1)
        res["body"]["entities"] = list(entities_map.values())
        return res
