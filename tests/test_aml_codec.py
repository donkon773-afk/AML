"""Unit tests for AML v2.0 Codec (Python)."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from aml_codec import AMLCodec, AMLError
import nam


class AMLCodecTests(unittest.TestCase):
    def test_state_keyframe_roundtrip(self):
        original = {
            "v": "NAM/1",
            "id": "m1",
            "session": "test_sess",
            "sender": "obs",
            "recipient": "gw",
            "seq": 1,
            "sent_ms": 1000000,
            "ttl_ms": 30000,
            "kind": "state",
            "body": {
                "world": "Altis",
                "rev": 1,
                "entities": [
                    {
                        "id": "A11",
                        "kind": "group",
                        "role": "INF",
                        "count": 20,
                        "pos": [1250, 3420, 0],
                        "hp": 1.0,
                        "ammo": 0.9,
                        "suppression": 0.0,
                        "observed_ms": 999500,
                        "callsign": "Альфа 1-1"
                    },
                    {
                        "id": "E01",
                        "kind": "contact",
                        "role": "ARM",
                        "count": 1,
                        "pos": [1450, 3600, 15],
                        "hp": 0.85,
                        "ammo": None,
                        "suppression": None,
                        "observed_ms": 998000,
                        "source": "A11"
                    }
                ]
            }
        }

        aml = AMLCodec.encode(original)
        self.assertTrue(aml.startswith("!AML:2|STATE|test_sess|obs→gw|1|1000000|30000|w:Altis|r:1"))
        self.assertIn('#A11:grp:INF:20:@1250,3420:%H100A90S0:t999500:C"Альфа 1-1"', aml)
        self.assertIn("#E01:cnt:ARM:1:@1450,3600,15:%H85A-S-:t998000:src:A11", aml)

        decoded = AMLCodec.decode(aml)
        self.assertEqual(decoded["session"], "test_sess")
        self.assertEqual(decoded["sender"], "obs")
        self.assertEqual(decoded["recipient"], "gw")
        self.assertEqual(decoded["body"]["world"], "Altis")
        self.assertEqual(decoded["body"]["rev"], 1)
        self.assertEqual(len(decoded["body"]["entities"]), 2)

        e1 = decoded["body"]["entities"][0]
        self.assertEqual(e1["id"], "A11")
        self.assertEqual(e1["kind"], "group")
        # AML's short role code "INF" maps to NAM/1's schema enum "INFANTRY" --
        # they are not the same string, and nam.schema.json only accepts the
        # full word (see ROLE_MAP).
        self.assertEqual(e1["role"], "INFANTRY")
        self.assertEqual(e1["count"], 20)
        self.assertEqual(e1["pos"], [1250, 3420, 0])
        self.assertEqual(e1["hp"], 1.0)
        self.assertEqual(e1["ammo"], 0.9)
        self.assertEqual(e1["suppression"], 0.0)
        self.assertEqual(e1["callsign"], "Альфа 1-1")
        # Regression: observed_ms must survive the wire, not be re-stamped at
        # decode time -- NAM/1 staleness checks depend on the original value.
        self.assertEqual(e1["observed_ms"], 999500)

        e2 = decoded["body"]["entities"][1]
        self.assertEqual(e2["id"], "E01")
        self.assertEqual(e2["kind"], "contact")
        self.assertEqual(e2["hp"], 0.85)
        self.assertIsNone(e2["ammo"])
        self.assertEqual(e2["pos"], [1450, 3600, 15])
        self.assertEqual(e2["source"], "A11")
        self.assertEqual(e2["observed_ms"], 998000)

    def test_entity_missing_observed_ms_is_rejected_at_encode(self):
        # Silently stamping 'now' at decode time used to defeat every
        # freshness/replay check in NAM/1. Now it's a hard encode-time error.
        msg = {
            "v": "NAM/1", "id": "m1", "session": "s", "sender": "obs", "recipient": "gw",
            "seq": 1, "sent_ms": 1000000, "ttl_ms": 30000, "kind": "state",
            "body": {"world": "Altis", "rev": 1, "entities": [
                {"id": "A11", "kind": "group", "role": "INF", "count": 20, "pos": [0, 0, 0]}
            ]}
        }
        with self.assertRaises(AMLError):
            AMLCodec.encode(msg)

    def test_entity_row_missing_observed_token_is_rejected_at_decode(self):
        # Guards against feeding pre-fix (or hand-crafted) wire text that
        # omits the mandatory 't<ms>' token.
        legacy_row = "!AML:2|STATE|s|obs→gw|1|1000000|30000|w:Altis|r:1\n#A11:grp:INF:20:@1250,3420:%H100A90S0"
        with self.assertRaises(AMLError):
            AMLCodec.decode(legacy_row)

    def test_delta_mutations_and_state_reconstruction(self):
        base_state = {
            "v": "NAM/1",
            "id": "m1",
            "session": "s1",
            "sender": "obs",
            "recipient": "gw",
            "seq": 1,
            "sent_ms": 1000000,
            "ttl_ms": 30000,
            "kind": "state",
            "body": {
                "world": "Altis",
                "rev": 1,
                "entities": [
                    {"id": "A11", "kind": "group", "role": "INF", "count": 20, "pos": [1250, 3420, 0], "hp": 1.0, "ammo": 0.9, "suppression": 0.0, "observed_ms": 1000000},
                    {"id": "E01", "kind": "contact", "role": "ARM", "count": 1, "pos": [1450, 3600, 0], "hp": 1.0, "ammo": None, "suppression": None, "observed_ms": 1000000}
                ]
            }
        }

        delta_msg = {
            "v": "NAM/1",
            "id": "m2",
            "session": "s1",
            "sender": "obs",
            "recipient": "gw",
            "seq": 2,
            "sent_ms": 1002000,
            "ttl_ms": 30000,
            "kind": "delta",
            "body": {
                "world": "Altis",
                "base": 1,
                "rev": 2,
                "upsert": [
                    {"id": "E02", "kind": "contact", "role": "INF", "count": 4, "pos": [1320, 3480, 0], "hp": 1.0, "ammo": 0.8, "suppression": 0.0, "observed_ms": 1002000}
                ],
                "updates": [
                    {"id": "A11", "pos": [1265, 3435, 0], "hp": 0.92, "suppression": 0.15}
                ],
                "remove": ["E01"]
            }
        }

        aml_delta = AMLCodec.encode(delta_msg)
        self.assertIn("~#A11:@1265,3435:%H92A-S15", aml_delta)
        self.assertIn("+#E02:cnt:INF:4:@1320,3480:%H100A80S0:t1002000", aml_delta)
        self.assertIn("-#E01", aml_delta)

        decoded_delta = AMLCodec.decode(aml_delta)
        self.assertEqual(decoded_delta["body"]["base"], 1)
        self.assertEqual(decoded_delta["body"]["rev"], 2)
        self.assertEqual(len(decoded_delta["body"]["upsert"]), 1)
        self.assertEqual(len(decoded_delta["body"]["updates"]), 1)
        self.assertEqual(decoded_delta["body"]["remove"], ["E01"])

        # Regression: a decoded AML delta (including the '~' partial-update
        # channel) must be a message that the real NAM/1 validator accepts.
        # Before the schema/receiver fix this raised ProtocolError('unknown field').
        import json
        nam.decode(json.dumps(decoded_delta))

        updated_state = AMLCodec.apply_delta(base_state, decoded_delta)
        self.assertEqual(updated_state["body"]["rev"], 2)
        self.assertEqual(len(updated_state["body"]["entities"]), 2)

        a11 = next(e for e in updated_state["body"]["entities"] if e["id"] == "A11")
        self.assertEqual(a11["pos"], [1265, 3435, 0])
        self.assertEqual(a11["hp"], 0.92)
        self.assertEqual(a11["suppression"], 0.15)
        self.assertEqual(a11["ammo"], 0.9)

        self.assertIsNone(next((e for e in updated_state["body"]["entities"] if e["id"] == "E01"), None))
        e02 = next(e for e in updated_state["body"]["entities"] if e["id"] == "E02")
        self.assertEqual(e02["pos"], [1320, 3480, 0])

    def test_delta_updates_flow_through_live_nam_receiver(self):
        """End-to-end: AML delta -> decode -> nam.Receiver.accept() actually
        mutates the shadow state via the 'updates' channel (not just schema-valid)."""
        import json
        rx = nam.Receiver('s1', 'gw', {'obs': ['state', 'delta']})
        state_msg = {
            "v": "NAM/1", "id": "m1", "session": "s1", "sender": "obs", "recipient": "gw",
            "seq": 1, "sent_ms": 1000000, "ttl_ms": 30000, "kind": "state",
            "body": {"world": "Altis", "rev": 1, "entities": [
                {"id": "A11", "kind": "group", "role": "INFANTRY", "count": 20, "pos": [1250, 3420, 0],
                 "hp": 1.0, "ammo": 0.9, "suppression": 0.0, "observed_ms": 1000000}
            ]}
        }
        rx.accept(nam.encode(state_msg), 'obs', 1000000)

        delta_msg = {
            "v": "NAM/1", "id": "m2", "session": "s1", "sender": "obs", "recipient": "gw",
            "seq": 2, "sent_ms": 1002000, "ttl_ms": 30000, "kind": "delta",
            "body": {"world": "Altis", "base": 1, "rev": 2,
                     "upsert": [], "updates": [{"id": "A11", "pos": [1265, 3435, 0], "hp": 0.9}],
                     "remove": []}
        }
        aml_delta = AMLCodec.encode(delta_msg)
        decoded_delta = AMLCodec.decode(aml_delta)
        rx.accept(json.dumps(decoded_delta), 'obs', 1002000)

        self.assertEqual(rx.states['obs']['entities']['A11']['pos'], [1265, 3435, 0])
        self.assertEqual(rx.states['obs']['entities']['A11']['hp'], 0.9)
        self.assertEqual(rx.states['obs']['entities']['A11']['observed_ms'], 1000000)

    def test_proposal_actions(self):
        proposal = {
            "v": "NAM/1",
            "id": "m3",
            "session": "s1",
            "sender": "loc",
            "recipient": "gw",
            "seq": 3,
            "sent_ms": 1003000,
            "ttl_ms": 30000,
            "kind": "proposal",
            "body": {
                "world": "Altis",
                "rev": 2,
                "state_sender": "obs",
                "reason": {"code": "CONTACT", "evidence": ["E02"]},
                "actions": [
                    {"type": "ASSAULT", "target": "A11", "position": [1320, 3480, 0], "op": "op10"},
                    {"type": "SET_ROE", "target": "A11", "roe": "OPEN_FIRE", "op": "op11"},
                    {"type": "REQUEST_FIRE", "target": "A12", "position": [1320, 3480, 0], "support": "MORTAR", "op": "op12"}
                ]
            }
        }

        aml = AMLCodec.encode(proposal)
        self.assertIn("$ATK:#A11:@1320,3480:#op10", aml)
        self.assertIn("$ROE:#A11:OPEN_FIRE:#op11", aml)
        self.assertIn("$FIRE:#A12:@1320,3480:MORTAR:#op12", aml)

        decoded = AMLCodec.decode(aml)
        self.assertEqual(decoded["kind"], "proposal")
        self.assertEqual(decoded["body"]["reason"]["code"], "CONTACT")
        self.assertEqual(decoded["body"]["reason"]["evidence"], ["E02"])
        self.assertEqual(len(decoded["body"]["actions"]), 3)

    def test_proposal_action_without_op_is_rejected_at_encode(self):
        # Previously the encoder silently dropped the op, and the decoder
        # then fabricated a non-deterministic one from the wall clock.
        proposal = {
            "v": "NAM/1", "id": "m3", "session": "s1", "sender": "loc", "recipient": "gw",
            "seq": 3, "sent_ms": 1003000, "ttl_ms": 30000, "kind": "proposal",
            "body": {"world": "Altis", "rev": 2, "state_sender": "obs",
                     "reason": {"code": "CONTACT", "evidence": ["E02"]},
                     "actions": [{"type": "HOLD", "target": "A11"}]}
        }
        with self.assertRaises(AMLError):
            AMLCodec.encode(proposal)

    def test_action_row_without_op_is_rejected_at_decode(self):
        legacy = "!AML:2|PROP|s1|loc→gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]\n$HLD:#A11"
        with self.assertRaises(AMLError):
            AMLCodec.decode(legacy)

    def test_decoded_action_op_is_deterministic(self):
        proposal = {
            "v": "NAM/1", "id": "m3", "session": "s1", "sender": "loc", "recipient": "gw",
            "seq": 3, "sent_ms": 1003000, "ttl_ms": 30000, "kind": "proposal",
            "body": {"world": "Altis", "rev": 2, "state_sender": "obs",
                     "reason": {"code": "CONTACT", "evidence": ["E02"]},
                     "actions": [{"type": "HOLD", "target": "A11", "op": "op1"}]}
        }
        aml = AMLCodec.encode(proposal)
        first = AMLCodec.decode(aml)["body"]["actions"][0]["op"]
        second = AMLCodec.decode(aml)["body"]["actions"][0]["op"]
        self.assertEqual(first, "op1")
        self.assertEqual(first, second)

    def test_autonomy_action_roundtrips_and_matches_nam_enum(self):
        # AML's action vocabulary includes AUTO/AUTONOMY; NAM/1's schema
        # must accept it or every $AUTO proposal is dead on arrival.
        proposal = {
            "v": "NAM/1", "id": "m9", "session": "s1", "sender": "loc", "recipient": "gw",
            "seq": 9, "sent_ms": 1009000, "ttl_ms": 30000, "kind": "proposal",
            "body": {"world": "Altis", "rev": 2, "state_sender": "obs",
                     "reason": {"code": "STALLED", "evidence": []},
                     "actions": [{"type": "AUTONOMY", "target": "A11", "op": "op9"}]}
        }
        aml = AMLCodec.encode(proposal)
        self.assertIn("$AUTO:#A11:#op9", aml)
        decoded = AMLCodec.decode(aml)
        self.assertEqual(decoded["body"]["actions"][0]["type"], "AUTONOMY")

    def test_nam2_envelope_survives_aml_and_live_receiver(self):
        """schema/trace_id must survive the compact projection AND be accepted
        by the real NAM/2 receiver after the peer negotiated the schema."""
        import json
        rx = nam.Receiver('s1', 'gw', {'obs': ['hello', 'state']}, schemas={17})

        greeting = {
            "v": "NAM/2", "id": "m0", "session": "s1", "sender": "obs", "recipient": "gw",
            "seq": 0, "sent_ms": 1000000, "ttl_ms": 30000, "kind": "hello",
            "body": {"versions": ["NAM/2"], "actions": ["ADVANCE"], "schemas": [17]}
        }
        rx.accept(nam.encode(greeting), 'obs', 1000000)
        self.assertEqual(rx.agreed['obs'], frozenset({17}))

        state = {
            "v": "NAM/2", "id": "m1", "session": "s1", "sender": "obs", "recipient": "gw",
            "seq": 1, "sent_ms": 1000000, "ttl_ms": 30000, "kind": "state",
            "schema": 17, "trace_id": "trace.42",
            "body": {"world": "Altis", "rev": 1, "entities": [
                {"id": "A11", "kind": "group", "role": "INFANTRY", "count": 20,
                 "pos": [1250, 3420, 0], "hp": 1.0, "ammo": 0.9, "suppression": 0.0,
                 "observed_ms": 1000000}
            ]}
        }
        aml = AMLCodec.encode(state)
        self.assertIn("|s:17|tr:trace.42|", aml)

        decoded = AMLCodec.decode(aml)
        self.assertEqual(decoded["v"], "NAM/2")
        self.assertEqual(decoded["schema"], 17)
        self.assertEqual(decoded["trace_id"], "trace.42")

        rx.accept(json.dumps(decoded), 'obs', 1000000)
        self.assertEqual(rx.states['obs']['rev'], 1)

    def test_query_result(self):
        query = {
            "v": "NAM/1",
            "id": "m4",
            "session": "s1",
            "sender": "cld",
            "recipient": "loc",
            "seq": 4,
            "sent_ms": 1004000,
            "ttl_ms": 30000,
            "kind": "query",
            "body": {"request": "EXPLAIN", "related": "A11"}
        }
        aml_q = AMLCodec.encode(query)
        self.assertIn("?WHY:#A11", aml_q)
        dec_q = AMLCodec.decode(aml_q)
        self.assertEqual(dec_q["body"]["request"], "EXPLAIN")
        self.assertEqual(dec_q["body"]["related"], "A11")
        self.assertNotIn("topic", dec_q["body"])


if __name__ == "__main__":
    unittest.main()
