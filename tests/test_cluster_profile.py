"""T37: cluster profile (payload schema s:2) -- task/agent rows and cluster actions.

Covers the whole chain the field profile already has: codec round-trip in
Python and JS, the Lark grammar, the independent JSON Schema validator, and
Receiver semantics (who may be reassigned what). Also pins that the two
vocabularies never share a frame.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jsonschema import Draft202012Validator  # noqa: E402
from lark import Lark  # noqa: E402

import nam  # noqa: E402
from aml_codec import AMLCodec, AMLError  # noqa: E402

NOW = 1_790_200_000_000
PERMS = {"agent_sync": {"hello", "state", "delta", "proposal"}, "claude": {"proposal", "result"}}


def frame(kind, body, seq=1, sent=NOW, sender="agent_sync", schema=2):
    msg = dict(v="NAM/2", id=f"m{seq}", session="aml-nam", sender=sender, recipient="claude",
               seq=seq, sent_ms=sent, ttl_ms=60000, kind=kind, body=body)
    if schema is not None:
        msg["schema"] = schema
    return msg


def task(tid, status="in_progress", assignee="antigravity", critic="codex", priority=1, depends=None, at=NOW):
    return dict(id=tid, kind="task", status=status, assignee=assignee, critic=critic,
                priority=priority, observed_ms=at, depends=depends or [])


def agent(aid, state="active", limit=80, weekly=50, task_id=None, at=NOW):
    ent = dict(id=aid, kind="agent", state=state, limit=limit, weekly=weekly, observed_ms=at)
    if task_id:
        ent["task"] = task_id
    return ent


def board_state(rev=4, sent=NOW):
    return frame("state", dict(world="board", rev=rev, entities=[
        task("T13", depends=["T17", "T18"]),
        task("T34", status="backlog", assignee="codex", critic="claude", priority=2),
        agent("codex", state="watching", limit=7, weekly=45, task_id="T34"),
        agent("claude", limit=80, weekly=95),
        agent("antigravity", state="active", limit=None, weekly=None),
    ]), sent=sent)


def prop(actions, code="LIMIT", evidence=("codex",), seq=2, sent=NOW + 1000):
    return frame("proposal", dict(state_sender="agent_sync", world="board", rev=4,
                                  reason=dict(code=code, evidence=list(evidence)), actions=actions),
                 seq=seq, sent=sent)


def js(requests):
    run = subprocess.run(["node", str(ROOT / "tests" / "codec_bridge.js")], input=json.dumps(requests),
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(run.stdout)


class ClusterCodecTests(unittest.TestCase):
    parser = Lark((ROOT / "scripts" / "aml_grammar.lark").read_text(encoding="utf-8"), parser="earley")

    def roundtrip(self, msg):
        Draft202012Validator(nam.SCHEMA).validate(msg)
        wire = AMLCodec.encode(msg)
        self.parser.parse(wire)
        back = AMLCodec.decode(wire)
        nam.encode(back)
        [js_res] = js([{"op": "encode", "value": msg}])
        self.assertTrue(js_res["ok"], js_res.get("error"))
        self.assertEqual(js_res["wire"], wire, "Python and JS must emit identical AML")
        self.assertEqual(js_res["message"], back)
        return wire, back

    def test_state_rows_roundtrip_and_are_compact(self):
        wire, back = self.roundtrip(board_state())
        self.assertIn("#T13:task:INP:antigravity:codex:P1:t", wire)
        self.assertIn(":dep:#T17,#T18", wire)
        self.assertIn("#codex:agent:WAT:L7:W45:t", wire)
        self.assertIn("#antigravity:agent:ACT:L-:W-:t", wire)
        self.assertEqual(back["body"]["entities"][2]["task"], "T34")
        self.assertLess(len(wire.encode()), len(nam.encode(back).encode()) * 0.6)

    def test_delta_upsert_and_remove(self):
        msg = frame("delta", dict(world="board", base=4, rev=5, upsert=[task("T13", status="review")],
                                  updates=[], remove=["T34"]), seq=2)
        wire, _ = self.roundtrip(msg)
        self.assertIn("+#T13:task:REV:", wire)
        self.assertIn("-#T34", wire)

    def test_cluster_actions_roundtrip(self):
        msg = prop([dict(op="op1", type="REASSIGN", target="T34", to="claude"),
                    dict(op="op2", type="PAUSE", target="codex")])
        wire, back = self.roundtrip(msg)
        self.assertIn("$RSG:#T34:claude:#op1", wire)
        self.assertIn("$PAU:#codex:#op2", wire)
        self.assertEqual(back["body"]["actions"][0]["to"], "claude")

    def test_vocabularies_never_mix(self):
        field = dict(id="A11", kind="group", role="INFANTRY", count=20, pos=[1, 2, 0],
                     hp=1.0, ammo=None, suppression=None, observed_ms=NOW)
        mixed = frame("state", dict(world="board", rev=1, entities=[task("T1"), field]))
        with self.assertRaises(AMLError):
            AMLCodec.encode(mixed)
        no_schema = frame("state", dict(world="board", rev=1, entities=[task("T1")]), schema=None)
        no_schema["v"] = "NAM/1"
        with self.assertRaises(AMLError):
            AMLCodec.encode(no_schema)
        with self.assertRaises(AMLError):  # field action under s:2
            AMLCodec.encode(prop([dict(op="op1", type="HOLD", target="T13")]))
        for res in js([{"op": "encode", "value": mixed}, {"op": "encode", "value": no_schema}]):
            self.assertFalse(res["ok"])

    def test_malformed_rows_rejected_by_grammar_and_codec(self):
        head = "!AML:2|STATE|aml-nam|agent_sync→claude|1|1|60000|s:2|w:board|r:1\n"
        for row in ("#T1:task:WIP:a:b:P1:t1",        # unknown status
                    "#T1:task:INP:a:b:P100:t1",      # priority > 99
                    "#T1:task:INP:a:b:P1",           # no observation time
                    "#c:agent:ACT:L101:W5:t1",       # limit > 100 (codec)
                    "#c:agent:ACT:72:45:t1"):        # missing L/W markers
            with self.subTest(row=row):
                with self.assertRaises(Exception):
                    self.parser.parse(head + row)
                    AMLCodec.decode(head + row)


class ClusterReceiverTests(unittest.TestCase):
    def setUp(self):
        self.rx = nam.Receiver("aml-nam", "claude", PERMS, schemas=[2])
        hello = frame("hello", dict(versions=["NAM/2"], actions=["REASSIGN", "PAUSE"], schemas=[2]),
                      seq=0, sent=NOW - 1000)
        self.rx.accept(nam.encode(hello), "agent_sync", NOW - 500)
        self.rx.accept(nam.encode(AMLCodec.decode(AMLCodec.encode(board_state()))), "agent_sync", NOW + 500)

    def accept(self, msg, at=NOW + 1500):
        return self.rx.accept(nam.encode(AMLCodec.decode(AMLCodec.encode(msg))), "agent_sync", at)

    def test_reassign_to_available_non_critic_is_accepted(self):
        self.accept(prop([dict(op="op1", type="REASSIGN", target="T34", to="antigravity")]))

    def test_reassign_to_paused_agent_is_rejected(self):
        with self.assertRaisesRegex(nam.ProtocolError, "unavailable agent"):
            self.accept(prop([dict(op="op1", type="REASSIGN", target="T13", to="codex")]))

    def test_executor_never_becomes_critic(self):
        with self.assertRaisesRegex(nam.ProtocolError, "cannot be the critic"):
            self.accept(prop([dict(op="op1", type="REASSIGN", target="T34", to="claude")]))

    def test_action_must_target_matching_kind(self):
        with self.assertRaisesRegex(nam.ProtocolError, "invalid action target"):
            self.accept(prop([dict(op="op1", type="PAUSE", target="T13")]))
        with self.assertRaisesRegex(nam.ProtocolError, "invalid action target"):
            self.accept(prop([dict(op="op1", type="APPROVE", target="claude")]), at=NOW + 1600)

    def test_unnegotiated_schema_is_rejected(self):
        rx = nam.Receiver("aml-nam", "claude", PERMS, schemas=[2])
        with self.assertRaisesRegex(nam.ProtocolError, "unnegotiated payload schema"):
            rx.accept(nam.encode(AMLCodec.decode(AMLCodec.encode(board_state()))), "agent_sync", NOW + 500)


if __name__ == "__main__":
    unittest.main()
