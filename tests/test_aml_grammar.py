"""Grammar/codec consistency check.

scripts/aml_grammar.lark is the structural twin of docs/aml_v2.gbnf (the
file actually handed to llama.cpp/LM Studio for grammar-constrained
decoding). This test proves the grammar is not fiction: every AML frame
the real encoder produces must parse, and known-bad frames -- including
the exact defects fixed in the codec (missing 't<ms>', role-code mismatch,
missing 'op') -- must be rejected.

docs/aml_v2.gbnf itself could not be exercised directly in this sandbox
(no llama.cpp/llama-cpp-python binary available offline). Before relying
on it in production, do one live smoke test in LM Studio/llama.cpp:
load the grammar and confirm Qwen can still generate a STATE frame under
it. If that ever diverges from this test, the .lark and .gbnf files have
drifted -- fix .lark first, rerun this test, then mirror the fix in .gbnf.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from aml_codec import AMLCodec
from lark import Lark
from lark.exceptions import UnexpectedInput

GRAMMAR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "aml_grammar.lark"


class AMLGrammarConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = Lark(GRAMMAR_PATH.read_text(), start="start", parser="earley")

    def parse_ok(self, text):
        self.parser.parse(text)  # raises on failure

    def parse_rejected(self, text):
        with self.assertRaises(UnexpectedInput):
            self.parser.parse(text)

    def test_every_message_kind_the_codec_produces_is_grammar_valid(self):
        fixtures = [
            {"kind": "state", "sent_ms": 1000000, "body": {"world": "Altis", "rev": 1, "entities": [
                {"id": "A11", "kind": "group", "role": "INFANTRY", "count": 20, "pos": [1250, 3420, 0],
                 "hp": 1.0, "ammo": 0.9, "suppression": 0.0, "observed_ms": 999500, "callsign": "Alpha 1-1"},
                {"id": "E01", "kind": "contact", "role": "ARMORED", "count": 1, "pos": [1450, 3600, 15],
                 "hp": 0.85, "ammo": None, "suppression": None, "observed_ms": 998000, "source": "A11"},
            ]}},
            {"kind": "delta", "sent_ms": 1002000, "body": {"world": "Altis", "base": 1, "rev": 2,
                "upsert": [{"id": "E02", "kind": "contact", "role": "INFANTRY", "count": 4, "pos": [1320, 3480, 0],
                            "hp": 1.0, "ammo": 0.8, "suppression": 0.0, "observed_ms": 1002000}],
                "updates": [{"id": "A11", "pos": [1265, 3435, 0], "hp": 0.92, "suppression": 0.15,
                             "role": "MECHANIZED", "count": 18}],
                "remove": ["E01"]}},
            {"kind": "proposal", "sent_ms": 1003000, "body": {"world": "Altis", "rev": 2, "state_sender": "obs",
                "reason": {"code": "CONTACT", "evidence": ["E02"]},
                "actions": [{"type": "ASSAULT", "target": "A11", "position": [1320, 3480, 0], "op": "op10"},
                            {"type": "SET_ROE", "target": "A11", "roe": "OPEN_FIRE", "op": "op11"},
                            {"type": "AUTONOMY", "target": "A11", "op": "op12"}]}},
            {"kind": "query", "sent_ms": 1004000,
                "body": {"request": "EXPLAIN", "related": "A11"}},
            {"kind": "result", "sent_ms": 1004200,
                "body": {"status": "ACCEPTED", "operations": [], "related": "A11", "reason": {"code": "LOW_AMMO", "evidence": ["E02"]}}},
            {"kind": "error", "sent_ms": 1006000, "body": {"code": "RESYNC", "related": "m2"}},
            {"kind": "handoff", "sent_ms": 1007000, "body": {
                "task": "AML_DEV", "status": "COMPLETE", "summary": "Fixed observed_ms and role mapping",
                "artifacts": [{"path": "scripts/aml_codec.js", "sha256": "a" * 64}],
                "checks": [{"name": "unit_tests", "result": "PASS", "evidence": "44 green"}],
                "next": "Write GBNF grammar"}},
        ]
        base = {"v": "NAM/1", "id": "m1", "session": "s1", "sender": "obs", "recipient": "gw",
                "seq": 1, "ttl_ms": 30000}
        for i, extra in enumerate(fixtures, start=1):
            msg = {**base, "seq": i, "id": f"m{i}", **extra}
            wire = AMLCodec.encode(msg)
            with self.subTest(kind=extra["kind"]):
                self.parse_ok(wire)
                import nam
                nam.encode(AMLCodec.decode(wire))

    def test_grammar_accepts_nam2_envelope_fields(self):
        route = "s1|obs\u2192gw|1|1000000|30000"
        self.parse_ok(f"!AML:2|STATE|{route}|s:17|tr:trace.42|w:Altis|r:1")
        self.parse_ok(f"!AML:2|STATE|{route}|tr:trace.42|w:Altis|r:1")
        self.parse_ok(f"!AML:2|STATE|{route}|s:17|w:Altis|r:1")
        # order is fixed: schema before trace, both before the per-kind fields
        self.parse_rejected(f"!AML:2|STATE|{route}|tr:trace.42|s:17|w:Altis|r:1")
        self.parse_rejected(f"!AML:2|STATE|{route}|w:Altis|s:17|r:1")

    def test_repetition_bounds_mirror_the_schema(self):
        """Unbounded repetition let a grammar-constrained model loop forever:
        'one more entity row' was always a legal continuation, so generation
        never had to stop. The bounds now match nam.schema.json."""
        base = "!AML:2|STATE|s1|obs\u2192gw|1|1000000|30000|w:Altis|r:1"
        row = "\n#A11:grp:INF:20:@1250,3420:%H100A90S0:t1"
        self.parse_ok(base + row * 128)          # entities maxItems: 128
        self.parse_rejected(base + row * 129)

        prop = ("!AML:2|PROP|s1|loc\u2192gw|3|1|30000|w:Altis|r:2"
                "|by:obs|rs:CONTACT[#E02]")
        act = "\n$ATK:#A11:@1,2:#op1"
        self.parse_rejected(prop)                 # actions minItems: 1
        self.parse_ok(prop + act * 3)             # actions maxItems: 3
        self.parse_rejected(prop + act * 4)

    def test_grammar_rejects_the_exact_defects_that_were_fixed(self):
        route = "s1|obs\u2192gw|1|1000000|30000"
        self.parse_rejected(f"!AML:2|STATE|{route}|w:Altis|r:1\n#A11:grp:INF:20:@1250,3420:%H100A90S0")  # no t<ms>
        self.parse_rejected(f"!AML:2|STATE|{route}|w:Altis|r:1\n#A11:grp:INFANTRY:20:@1250,3420:%H100A90S0:t1")  # full-word role
        self.parse_rejected("!AML:2|PROP|s1|loc\u2192gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]\n$HLD:#A11")  # no op
        self.parse_rejected("!AML:2|PROP|s1|loc\u2192gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]\n$NUKE:#A11:#op1")  # unknown action
        self.parse_rejected(f"!AML:1|STATE|{route}|w:Altis|r:1")  # wrong protocol version
        self.parse_rejected(f"!AML:2|STATE|{route}|w:Altis|r:1\n#A1:1:grp:INF:20:@1250,3420:%H100A90S0:t1")  # ':' in id


if __name__ == "__main__":
    unittest.main()
