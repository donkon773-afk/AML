import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from nam import Receiver, ProtocolError, decode, encode

NOW = 1000000


def frame(kind='state', seq=1, sender='observer', body=None):
    if body is None:
        body = {'world': 'Altis.demo', 'rev': 1, 'entities': [
            {'id': 'A11', 'kind': 'group', 'pos': [1250, 3420, 0], 'observed_ms': NOW,
             'callsign': 'Альфа 1-1', 'ammo': None}]}
    return {'v': 'NAM/1', 'id': f'm{seq}', 'session': 'demo', 'sender': sender,
            'recipient': 'gateway', 'seq': seq, 'sent_ms': NOW, 'ttl_ms': 30000,
            'kind': kind, 'body': body}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.rx = Receiver('demo', 'gateway', {
            'observer': ['state', 'delta'], 'local': ['proposal', 'query'],
            'cloud': ['query', 'handoff']})

    def accept(self, message, peer=None, now=NOW):
        return self.rx.accept(json.dumps(message, ensure_ascii=False), peer or message['sender'], now)

    def proposal(self):
        return frame('proposal', sender='local', body={
            'state_sender': 'observer', 'world': 'Altis.demo', 'rev': 1,
            'actions': [{'op': 'op1', 'target': 'A11', 'type': 'ADVANCE', 'position': [1300, 3420, 0]}],
            'reason': {'code': 'PLAYER_ORDER', 'evidence': ['A11']}})

    def delta(self, seq=2, base=1):
        entity = copy.deepcopy(frame()['body']['entities'][0])
        entity['pos'][0] += 10
        return frame('delta', seq=seq, body={'world': 'Altis.demo', 'base': base, 'rev': base + 1, 'upsert': [entity], 'remove': []})

    def test_unicode_null_roundtrip(self):
        self.assertEqual(decode(encode(frame())), frame())

    def test_state_delta_lossless(self):
        self.accept(frame())
        self.accept(self.delta())
        self.assertEqual(self.rx.states['observer']['entities']['A11']['pos'], [1260,3420,0])
        self.assertEqual(self.rx.states['observer']['rev'], 2)

    def test_failed_delta_is_atomic_then_resync(self):
        self.accept(frame())
        before = copy.deepcopy(self.rx.__dict__)
        with self.assertRaisesRegex(ProtocolError, 'RESYNC'):
            self.accept(self.delta(base=9))
        self.assertEqual(self.rx.__dict__, before)
        update = frame(seq=8)
        update['body']['rev'] = 8
        self.accept(update)
        self.assertEqual(self.rx.states['observer']['rev'], 8)

    def test_missing_baseline(self):
        with self.assertRaisesRegex(ProtocolError, 'RESYNC'):
            self.accept(self.delta())

    def test_replay_and_duplicate_message_id(self):
        self.accept(frame())
        with self.assertRaisesRegex(ProtocolError, 'replay'):
            self.accept(frame())
        replay = frame(seq=2)
        replay['id'] = 'm1'
        with self.assertRaisesRegex(ProtocolError, 'duplicate message'):
            self.accept(replay)

    def test_peer_authentication_and_permission(self):
        with self.assertRaisesRegex(ProtocolError, 'identity'):
            self.accept(frame(), peer='cloud')
        with self.assertRaisesRegex(ProtocolError, 'unauthorized'):
            self.accept(frame(sender='cloud'))

    def test_session_and_recipient(self):
        for key in ('session', 'recipient'):
            candidate = frame()
            candidate[key] = 'other'
            with self.assertRaises(ProtocolError):
                self.accept(candidate)

    def test_expiry_and_clock_skew(self):
        with self.assertRaises(ProtocolError):
            self.accept(frame(), now=NOW+30000)
        with self.assertRaises(ProtocolError):
            self.accept(frame(), now=NOW-2001)

    def test_duplicate_keys_and_nonfinite(self):
        for raw in ('{"v":"NAM/1","v":"NAM/2"}', '{"x":NaN}', '{"x":1e999}'):
            with self.assertRaises(ProtocolError):
                decode(raw)

    def test_bounded_frames_depth_unicode(self):
        for raw in (' ' * 65537, '[' * 40 + '0' + ']' * 40, '{"x":"\\ud800"}', '{"x":"\\u0000"}'):
            with self.assertRaises(ProtocolError):
                decode(raw)

    def test_unknown_fields_and_prototype(self):
        for key in ('code', '__proto__'):
            candidate = frame()
            candidate[key] = 'compile malicious'
            with self.assertRaises(ProtocolError):
                self.accept(candidate)

    def test_types_ranges_and_version(self):
        cases = [('seq', True), ('seq', -1), ('seq', 9007199254740992), ('ttl_ms', 0), ('v', 'NAM/3')]
        for key, value in cases:
            candidate = frame()
            candidate[key] = value
            with self.assertRaises(ProtocolError):
                self.accept(candidate)

    def test_duplicate_entity_and_ambiguous_delta(self):
        candidate = frame()
        candidate['body']['entities'] *= 2
        with self.assertRaisesRegex(ProtocolError, 'duplicate entity'):
            self.accept(candidate)
        self.accept(frame())
        candidate = self.delta()
        candidate['body']['remove'] = ['A11']
        with self.assertRaisesRegex(ProtocolError, 'ambiguous'):
            self.accept(candidate)

    def test_wrong_world_and_rollback(self):
        self.accept(frame())
        for candidate in (frame(seq=2), self.delta()):
            candidate['body']['world'] = 'Tanoa.demo'
            with self.assertRaises(ProtocolError):
                self.accept(candidate)
        with self.assertRaises(ProtocolError):
            self.accept(frame(seq=3))

    def test_stale_observation(self):
        candidate = frame()
        candidate['body']['entities'][0]['observed_ms'] = NOW-30001
        with self.assertRaises(ProtocolError):
            self.accept(candidate)

    def test_valid_proposal_is_only_data(self):
        self.accept(frame())
        before = copy.deepcopy(self.rx.states)
        result = self.accept(self.proposal())
        self.assertEqual(result['kind'], 'proposal')
        self.assertEqual(self.rx.states, before)
        result['body']['actions'][0]['type'] = 'HOLD'
        self.assertEqual(self.rx.states, before)

    def test_bad_proposal_target_and_grounding(self):
        self.accept(frame())
        candidates = []
        a = self.proposal(); a['body']['actions'][0]['target'] = 'missing'; candidates.append(a)
        a = self.proposal(); a['body']['actions'][0]['position'] = [90000,90000,0]; candidates.append(a)
        a = self.proposal(); a['body']['reason']['evidence'] = ['missing']; candidates.append(a)
        a = self.proposal(); a['body']['rev'] = 0; candidates.append(a)
        for candidate in candidates:
            with self.assertRaises(ProtocolError):
                self.accept(candidate)

    def test_action_contracts(self):
        self.accept(frame())
        for change in ({'type': 'FLANK'}, {'type': 'SET_ROE'}, {'type': 'REQUEST_FIRE'}, {'code': 'execVM'}):
            candidate = self.proposal()
            candidate['body']['actions'][0].update(change)
            with self.assertRaises(ProtocolError):
                self.accept(candidate)

    def test_nam2_fields_rejected_on_nam1_frames(self):
        """A NAM/1 sender must not smuggle NAM/2 fields: the recipient would
        otherwise treat a schema ID from an un-negotiated dialect as binding."""
        for field, value in (('schema', 17), ('trace_id', 'tr1')):
            candidate = frame()
            candidate[field] = value
            with self.assertRaises(ProtocolError):
                self.accept(candidate)

    def test_nam2_trace_id_roundtrips(self):
        candidate = frame()
        candidate['v'] = 'NAM/2'
        candidate['trace_id'] = 'trace.42'
        result = self.accept(candidate)
        self.assertEqual(result['trace_id'], 'trace.42')

    def test_schema_must_be_negotiated_before_use(self):
        rx = Receiver('demo', 'gateway', {'local': ['hello', 'proposal', 'query']}, schemas={17, 23})
        early = frame('query', sender='local', body={'request': 'KEYFRAME', 'related': 'm1'})
        early['v'], early['schema'] = 'NAM/2', 17
        with self.assertRaisesRegex(ProtocolError, 'unnegotiated'):
            rx.accept(json.dumps(early), 'local', NOW)

    def test_hello_negotiates_common_schema(self):
        rx = Receiver('demo', 'gateway', {'local': ['hello', 'query']}, schemas={17, 23})
        greeting = frame('hello', sender='local',
                         body={'versions': ['NAM/2'], 'actions': ['BOARD'], 'schemas': [17, 99]})
        greeting['v'] = 'NAM/2'
        rx.accept(json.dumps(greeting), 'local', NOW)
        self.assertEqual(rx.agreed['local'], frozenset({17}))

        ok = frame('query', seq=2, sender='local', body={'request': 'KEYFRAME', 'related': 'm1'})
        ok['v'], ok['schema'] = 'NAM/2', 17
        rx.accept(json.dumps(ok), 'local', NOW)

        # 99 was offered by the peer but the host never claimed to understand it.
        bad = frame('query', seq=3, sender='local', body={'request': 'KEYFRAME', 'related': 'm1'})
        bad['v'], bad['schema'] = 'NAM/2', 99
        with self.assertRaisesRegex(ProtocolError, 'unnegotiated'):
            rx.accept(json.dumps(bad), 'local', NOW)

    def test_hello_without_common_schema_is_rejected(self):
        rx = Receiver('demo', 'gateway', {'local': ['hello']}, schemas={17})
        greeting = frame('hello', sender='local',
                         body={'versions': ['NAM/2'], 'actions': [], 'schemas': [99]})
        greeting['v'] = 'NAM/2'
        with self.assertRaisesRegex(ProtocolError, 'no common payload schema'):
            rx.accept(json.dumps(greeting), 'local', NOW)

    def test_schemas_require_nam2_dialect(self):
        rx = Receiver('demo', 'gateway', {'local': ['hello']}, schemas={17})
        greeting = frame('hello', sender='local',
                         body={'versions': ['NAM/1'], 'actions': [], 'schemas': [17]})
        with self.assertRaisesRegex(ProtocolError, 'require NAM/2'):
            rx.accept(json.dumps(greeting), 'local', NOW)

    def test_hello_query_result_error_shapes(self):
        bodies = {
            'hello': {'versions': ['NAM/1'], 'actions': ['BOARD']},
            'query': {'request': 'KEYFRAME', 'related': 'm1'},
            'result': {'related': 'm1', 'status': 'ACCEPTED', 'operations': ['op1']},
            'error': {'related': 'm1', 'code': 'RESYNC'}}
        for kind, body in bodies.items():
            self.assertEqual(decode(encode(frame(kind, body=body)))['kind'], kind)

    def test_delta_remove_then_add(self):
        self.accept(frame())
        candidate = self.delta()
        candidate['body'].update(upsert=[], remove=['A11'])
        self.accept(candidate)
        self.assertEqual(self.rx.states['observer']['entities'], {})
        self.accept(self.delta(seq=3, base=2))
        self.assertIn('A11', self.rx.states['observer']['entities'])

    def test_handoff_cross_agent_data_only_and_paths(self):
        body = {'task':'repair1','status':'IN_PROGRESS','summary':'Проверен транспорт.',
                'artifacts':[{'path':'bridge/protocol.js','sha256':'a'*64}],
                'checks':[{'name':'runtime','result':'UNVERIFIED','evidence':'Game not run'}],
                'next':'Review the scoped patch'}
        self.accept(frame('handoff', sender='cloud', body=body))
        self.assertEqual(self.rx.states, {})
        for path in ('../outside', '/absolute', 'C:/secret', 'a\\b', 'a//b'):
            bad = copy.deepcopy(body)
            bad['artifacts'][0]['path'] = path
            with self.assertRaises(ProtocolError):
                self.accept(frame('handoff', seq=2, sender='cloud', body=bad))


if __name__ == '__main__':
    unittest.main()
