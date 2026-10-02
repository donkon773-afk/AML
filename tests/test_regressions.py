"""Concrete counterexamples from the review; independent schema and JS checks."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from aml_codec import AMLCodec, AMLError
import nam
from frame_validation import validate_frame
from grammar_probe import probe
from jsonschema import Draft202012Validator
import llm_client

NOW = 1000000


def entity():
    return dict(id='A11', kind='group', pos=[1250,3420,0], observed_ms=NOW,
                role='INFANTRY', count=20, hp=1, ammo=0.9, suppression=0)


def frame(kind='state', body=None, seq=1, sent=NOW, sender='observer'):
    if body is None:
        body=dict(world='Altis',rev=1,entities=[entity()])
    return dict(v='NAM/1',id=f'm{seq}',session='demo',sender=sender,recipient='gw',
                seq=seq,sent_ms=sent,ttl_ms=30000,kind=kind,body=body)


def delta(update, seq=2, sent=NOW+2000):
    return frame('delta',dict(world='Altis',base=seq-1,rev=seq,upsert=[],updates=[update],remove=[]),seq,sent)


def js(requests):
    run=subprocess.run(['node',str(ROOT/'tests/codec_bridge.js')],input=json.dumps(requests),
                       capture_output=True,text=True,encoding='utf-8',check=True)
    return json.loads(run.stdout)


class RepairRegressionTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        llm_client.reset_grammar_support_cache()
        self.addCleanup(llm_client.reset_grammar_support_cache)

    def tearDown(self):
        llm_client.reset_grammar_support_cache()
        super().tearDown()

    def roundtrip(self, msg):
        Draft202012Validator(nam.SCHEMA).validate(msg)
        wire=AMLCodec.encode(msg)
        decoded=validate_frame(wire)
        Draft202012Validator(nam.SCHEMA).validate(decoded)
        foreign=js([dict(op='encode',value=msg),dict(op='decode',value=wire)])
        self.assertTrue(all(x['ok'] for x in foreign), foreign)
        self.assertEqual(foreign[0]['wire'],wire)
        self.assertEqual(foreign[0]['message'],decoded)
        self.assertEqual(foreign[1]['message'],decoded)
        return decoded

    def both_reject(self, msg):
        with self.assertRaises(AMLError): AMLCodec.encode(msg)
        self.assertFalse(js([dict(op='encode',value=msg)])[0]['ok'])

    def test_partial_role_count_survive_and_change_receiver(self):
        msg=self.roundtrip(delta(dict(id='A11',role='RECON',count=8)))
        self.assertEqual(msg['body']['updates'],[dict(id='A11',role='RECON',count=8)])
        rx=nam.Receiver('demo','gw',{'observer':['state','delta']})
        rx.accept(nam.encode(frame()),'observer',NOW)
        rx.accept(nam.encode(msg),'observer',NOW+2000)
        self.assertEqual(rx.states['observer']['entities']['A11']['count'],8)
        self.assertEqual(rx.states['observer']['entities']['A11']['role'],'RECON')

    def test_null_partial_is_explicitly_rejected_not_silently_lost(self):
        self.both_reject(delta(dict(id='A11',ammo=None)))

    def test_null_full_upsert_clears_previous_value(self):
        e=entity();e.update(ammo=None,observed_ms=NOW+2000)
        msg=frame('delta',dict(world='Altis',base=1,rev=2,upsert=[e],remove=[]),2,NOW+2000)
        decoded=self.roundtrip(msg)
        rx=nam.Receiver('demo','gw',{'observer':['state','delta']})
        rx.accept(nam.encode(frame()),'observer',NOW)
        rx.accept(nam.encode(decoded),'observer',NOW+2000)
        self.assertIsNone(rx.states['observer']['entities']['A11']['ammo'])
        self.assertEqual(rx.states['observer']['entities']['A11']['observed_ms'],NOW+2000)

    def test_empty_partial_rejected_atomically(self):
        rx=nam.Receiver('demo','gw',{'observer':['state','delta']})
        rx.accept(nam.encode(frame()),'observer',NOW)
        before=copy.deepcopy(rx.__dict__)
        with self.assertRaises(nam.ProtocolError):
            rx.accept(json.dumps(delta(dict(id='A11'),sent=NOW+40000)),'observer',NOW+40000)
        self.assertEqual(rx.__dict__,before)
        self.both_reject(delta(dict(id='A11')))

    def test_partial_cannot_refresh_stale_target_or_evidence(self):
        rx=nam.Receiver('demo','gw',{'observer':['state','delta'],'planner':['proposal']})
        rx.accept(nam.encode(frame()),'observer',NOW)
        d=self.roundtrip(delta(dict(id='A11',count=8),sent=NOW+40000))
        rx.accept(nam.encode(d),'observer',NOW+40000)
        self.assertEqual(rx.states['observer']['entities']['A11']['observed_ms'],NOW)
        p=frame('proposal',dict(world='Altis',rev=2,state_sender='observer',
                    actions=[dict(op='op1',target='A11',type='HOLD')],
                    reason=dict(code='CONTACT',evidence=['A11'])),1,NOW+40000,'planner')
        with self.assertRaisesRegex(nam.ProtocolError,'stale target'):
            rx.accept(nam.encode(p),'planner',NOW+40000)

    def test_all_result_statuses_reasons_and_empty_operations(self):
        for status in ('ACCEPTED','EXECUTED','REJECTED','FAILED'):
            for ops in ([],['op1','op2']):
                for reason in (None,dict(code='LOW_AMMO',evidence=['A11'])):
                    body=dict(related='m77',status=status,operations=ops)
                    if reason is not None: body['reason']=reason
                    with self.subTest(status=status,ops=ops,reason=reason):
                        self.assertEqual(self.roundtrip(frame('result',body))['body'],body)

    def test_error_and_explain_query_match_schema(self):
        for kind,body in [('error',dict(related='m7',code='RESYNC')),
                          ('query',dict(request='EXPLAIN',related='m7'))]:
            self.assertEqual(self.roundtrip(frame(kind,body))['body'],body)

    def test_schema_incompatible_legacy_explanation_rejected(self):
        self.both_reject(frame('query',dict(request='EXPLAIN',related='m7',topic='AMMO')))
        self.both_reject(frame('result',dict(status='EXPLAINED',related='m7',reason=dict(code='UNKNOWN',evidence=[]))))

    def test_unsupported_projection_fields_rejected(self):
        for key,value in [('side','west'),('pos',[1250.5,3420.5,-0.5]),('hp',0.925)]:
            msg=frame();msg['body']['entities'][0][key]=value
            with self.subTest(key=key): self.both_reject(msg)
        msg=frame();msg['id']='custom-id';self.both_reject(msg)
        msg=frame();msg['v']='NAM/2';self.both_reject(msg)

    def test_nam1_cannot_be_promoted_by_extensions(self):
        msg=frame();msg['schema']=17;self.both_reject(msg)

    def test_supported_nam2_roundtrip(self):
        msg=frame();msg.update(v='NAM/2',schema=17,trace_id='trace-1')
        self.assertEqual(self.roundtrip(msg),msg)

    def test_integer_coordinates_and_percent_metrics_match(self):
        for x,hp in [(1250,0.92),(-1251,0.93),(0,0),(-100000,1)]:
            msg=frame();msg['body']['entities'][0].update(pos=[x,1,0],hp=hp)
            self.assertEqual(self.roundtrip(msg),msg)

    def test_no_unadvertised_schema_is_implicitly_trusted(self):
        rx=nam.Receiver('demo','gw',{'observer':['hello']})
        h=frame('hello',dict(versions=['NAM/2'],actions=[],schemas=[99]));h['v']='NAM/2'
        with self.assertRaisesRegex(nam.ProtocolError,'no common payload schema'):
            rx.accept(nam.encode(h),'observer',NOW)

    def test_full_validator_rejects_prefix_truncation_and_prose(self):
        wire=AMLCodec.encode(frame())
        for bad in [wire+'\nHELLO WORLD',wire.rsplit(':t',1)[0],'!AML:2|',
                    wire.replace('%H100A90S0','%H999A90S0')]:
            with self.subTest(bad=bad):
                with self.assertRaises(Exception): validate_frame(bad)

    def test_bad_result_without_related_rejected(self):
        wire='!AML:2|RES|demo|observer→gw|1|1000000|30000|*ACC:#op1'
        with self.assertRaises(Exception): validate_frame(wire)
        self.assertFalse(js([dict(op='decode',value=wire)])[0]['ok'])

    def test_probe_catches_ignored_grammar_and_prefix_only(self):
        self.assertFalse(probe(lambda prompt,g: 'HELLO WORLD')[0])
        self.assertFalse(probe(lambda prompt,g: 'HELLO WORLD' if g is None else '!AML:2|')[0])
        self.assertFalse(probe(lambda prompt,g: 'uncooperative')[0])

    def test_probe_marker_is_only_in_grammar(self):
        grammars=[]
        def generated(prompt,g):
            if g is None: return 'HELLO WORLD'
            marker=g.split('"')[1]
            self.assertNotIn(marker,prompt);grammars.append(g)
            return marker
        self.assertTrue(probe(generated)[0])
        self.assertEqual(len(set(grammars)),2)

    def test_http_truncation_is_not_accepted(self):
        import llm_client
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'choices':[{'finish_reason':'length','message':{'content':'!AML:2|'}}]}).encode()
        with patch('urllib.request.urlopen',return_value=response):
            with self.assertRaises(AMLError): llm_client.ask('x')

    def test_missing_envelope_cannot_synthesize_time_or_identity(self):
        for key in ('sent_ms','id','seq','session'):
            msg=frame();del msg[key]
            with self.subTest(key=key): self.both_reject(msg)

    def test_sdk_stop_reason_from_stats(self):
        import importlib.util
        from types import SimpleNamespace
        result=SimpleNamespace(stats=SimpleNamespace(stop_reason='eosFound'),parsed='HELLO WORLD')
        model=SimpleNamespace(respond=lambda *args,**kwargs: result)
        chat=SimpleNamespace(add_user_message=lambda x: None)
        sdk=SimpleNamespace(llm=lambda x:model,Chat=lambda x:chat)
        with patch.dict(sys.modules,{'lmstudio':sdk}):
            spec=importlib.util.spec_from_file_location('sdk_under_test',ROOT/'scripts/llm_client_sdk.py')
            client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
            self.assertEqual(client.ask('fake','x',probe_mode=True),'HELLO WORLD')
            for stop in ('maxPredictedTokensReached','contextLengthReached','failed',None):
                result.stats.stop_reason=stop
                with self.subTest(stop=stop):
                    with self.assertRaises(AMLError): client.ask('fake','x',probe_mode=True)

    def test_sdk_strip_reasoning_in_probe_mode(self):
        import importlib.util
        from types import SimpleNamespace
        result = SimpleNamespace(stats=SimpleNamespace(stop_reason='eosFound'), parsed='HELLO WORLD')
        model = SimpleNamespace(respond=lambda *args, **kwargs: result)
        chat = SimpleNamespace(add_user_message=lambda x: None)
        sdk = SimpleNamespace(llm=lambda x: model, Chat=lambda x: chat)
        with patch.dict(sys.modules, {'lmstudio': sdk}):
            spec = importlib.util.spec_from_file_location('sdk_under_test', ROOT / 'scripts/llm_client_sdk.py')
            client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(client)
            marker = '__LM_STUDIO_INTERNAL_LSEP_SYNTHETIC_REASONING_END_abcdef__'
            result.parsed = 'HELLO WORLD' + marker
            self.assertEqual(client.ask('fake', 'x', probe_mode=True), 'HELLO WORLD')
            result.parsed = 'internal reasoning' + marker + 'HELLO WORLD'
            self.assertEqual(client.ask('fake', 'x', probe_mode=True), 'HELLO WORLD')

    def test_sdk_selftest_forwards_cli_limits(self):
        import importlib.util
        import io
        calls = []
        spec = importlib.util.spec_from_file_location('sdk_selftest', ROOT / 'scripts/llm_client_sdk.py')
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        client.ask = lambda *args, **kwargs: calls.append((args, kwargs)) or 'HELLO WORLD'
        def fake_probe(generate):
            generate('control', None)
            generate('probe', 'root ::= "AML_PROBE"')
            return True, 'PASS'
        with patch('sys.stderr', new_callable=io.StringIO), patch('sys.stdout', new_callable=io.StringIO):
            with patch('grammar_probe.probe', fake_probe):
                self.assertTrue(client.selftest('fake', max_tokens=777, no_think=True))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[1]['max_tokens'] == 777 for c in calls))
        self.assertTrue(all(c[1]['no_think'] is True for c in calls))

    def test_http_grammar_support_is_fail_closed(self):
        import llm_client
        llm_client.reset_grammar_support_cache()
        with patch.object(llm_client, 'grammar_selftest', return_value=False):
            with self.assertRaisesRegex(AMLError, 'did not demonstrate'):
                llm_client.require_grammar_support('fake-fail')
        llm_client.reset_grammar_support_cache()
        with patch.object(llm_client, 'grammar_selftest', return_value=True):
            llm_client.require_grammar_support('fake-pass')

    def test_http_mock_server_ignoring_grammar_fails_fast(self):
        import llm_client
        from unittest.mock import MagicMock
        import io

        llm_client.reset_grammar_support_cache()
        # Handler simulating LM Studio's behavior: ignores payload['grammar']
        def fake_urlopen_ignoring_grammar(req, timeout=None):
            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.read.return_value = json.dumps({
                'choices': [{'finish_reason': 'stop', 'message': {'content': 'HELLO WORLD'}}]
            }).encode('utf-8')
            return resp

        with patch('sys.stdout', new_callable=io.StringIO):
            with patch('urllib.request.urlopen', side_effect=fake_urlopen_ignoring_grammar):
                with self.assertRaisesRegex(AMLError, 'did not demonstrate'):
                    llm_client.require_grammar_support('fake-model')

            llm_client.reset_grammar_support_cache()
            # Handler simulating a server that DOES enforce grammar
            def fake_urlopen_enforcing_grammar(req, timeout=None):
                body = json.loads(req.data.decode('utf-8'))
                grammar = body.get('grammar')
                if grammar and 'root ::=' in grammar:
                    marker = grammar.split('"')[1]
                    content = marker
                else:
                    content = 'HELLO WORLD'
                resp = MagicMock()
                resp.__enter__.return_value = resp
                resp.read.return_value = json.dumps({
                    'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]
                }).encode('utf-8')
                return resp

            with patch('urllib.request.urlopen', side_effect=fake_urlopen_enforcing_grammar):
                llm_client.require_grammar_support('fake-model')

    def test_http_ask_direct_fails_closed_when_grammar_ignored(self):
        import llm_client
        from unittest.mock import MagicMock
        import io

        llm_client.reset_grammar_support_cache()
        calls = []

        def fake_urlopen_ignoring_grammar(req, timeout=None):
            body = json.loads(req.data.decode('utf-8'))
            calls.append(body)
            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.read.return_value = json.dumps({
                'choices': [{'finish_reason': 'stop', 'message': {'content': 'HELLO WORLD'}}]
            }).encode('utf-8')
            return resp

        with patch('sys.stdout', new_callable=io.StringIO):
            with patch('urllib.request.urlopen', side_effect=fake_urlopen_ignoring_grammar):
                with self.assertRaisesRegex(AMLError, 'did not demonstrate grammar enforcement'):
                    llm_client.ask('TARGET', grammar=True)

        # Fail closed: 1 control + 1 probe (fails on first marker) = 2 calls; TARGET never sent
        self.assertLessEqual(len(calls), 3)
        self.assertFalse(any(c.get('messages', [{}])[-1].get('content') == 'TARGET' for c in calls))

    def test_http_main_ask_call_count_and_order(self):
        import llm_client
        from unittest.mock import MagicMock
        import io

        llm_client.reset_grammar_support_cache()
        recorded_bodies = []
        target_frame = (
            '!AML:2|STATE|s1|observer\u2192gw|1|1000000|30000|w:Altis|r:1\n'
            '#A11:grp:INF:20:@1250,3420:%H100A90S0:t1000000:C"\u0410\u043b\u044c\u0444\u0430 1-1"'
        )

        def fake_urlopen_enforcing_grammar(req, timeout=None):
            body = json.loads(req.data.decode('utf-8'))
            recorded_bodies.append(body)
            grammar = body.get('grammar')
            if grammar and 'AML_PROBE_' in grammar:
                content = grammar.split('"')[1]
            elif grammar:
                content = target_frame
            else:
                content = 'HELLO WORLD'

            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.read.return_value = json.dumps({
                'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]
            }).encode('utf-8')
            return resp

        with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            with patch('urllib.request.urlopen', side_effect=fake_urlopen_enforcing_grammar):
                with patch.object(sys, 'argv', ['llm_client.py', '--ask', 'Send me state']):
                    llm_client.main()

        # Check call count: 1 control + 2 probe markers + 1 target = 4 calls total
        self.assertEqual(len(recorded_bodies), 4)

        # Check order of calls
        # 0: control (no grammar)
        self.assertNotIn('grammar', recorded_bodies[0])
        self.assertIn('HELLO WORLD', recorded_bodies[0]['messages'][-1]['content'])
        # 1 & 2: probe markers
        self.assertIn('AML_PROBE_', recorded_bodies[1]['grammar'])
        self.assertIn('AML_PROBE_', recorded_bodies[2]['grammar'])
        # 3: target request
        self.assertEqual(recorded_bodies[3]['messages'][-1]['content'], 'Send me state')
        self.assertIn('state-msg', recorded_bodies[3]['grammar'])

        # Second request in the same process must hit cache and make exactly 1 call (no re-probing)
        with patch('urllib.request.urlopen', side_effect=fake_urlopen_enforcing_grammar):
            second_res = llm_client.ask('Second query', grammar=True)
            self.assertEqual(second_res, target_frame)

        self.assertEqual(len(recorded_bodies), 5)
        self.assertEqual(recorded_bodies[4]['messages'][-1]['content'], 'Second query')

    def test_http_client_main_cli_fail_fast(self):
        import llm_client
        import io
        llm_client.reset_grammar_support_cache()
        with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            with patch.object(sys, 'argv', ['llm_client.py', '--ask', 'test']):
                with patch.object(llm_client, 'grammar_selftest', return_value=False):
                    with self.assertRaises(SystemExit) as cm:
                        llm_client.main()
                    self.assertEqual(cm.exception.code, 1)

            llm_client.reset_grammar_support_cache()
            with patch.object(sys, 'argv', ['llm_client.py', '--ask', 'test', '--no-grammar']):
                with patch.object(llm_client, 'ask', return_value='!AML:2|STATE|...'):
                    with patch('frame_validation.validate_frame', return_value={'ok': True}):
                        llm_client.main()

    def test_http_grammar_cache_isolation_and_order(self):
        """F4 regression check: Ensure grammar cache does not leak across test cases

        Verifies:
        1. Cache starts clean.
        2. Populating cache in one test method is wiped before next method runs.
        3. Subsequent test does not inherit stale positive cache (no fake-pass).
        4. Reverse order execution also maintains strict isolation.
        5. Crashed/errored test cases still trigger cleanup leaving cache empty.
        """
        # Baseline: Current test started with empty cache thanks to setUp
        self.assertEqual(len(llm_client._GRAMMAR_SUPPORT_CACHE), 0)

        class MockTestCase(unittest.TestCase):
            def setUp(self):
                super().setUp()
                llm_client.reset_grammar_support_cache()
                self.addCleanup(llm_client.reset_grammar_support_cache)

            def tearDown(self):
                llm_client.reset_grammar_support_cache()
                super().tearDown()

            def test_a_populate_cache(self):
                self.assertEqual(len(llm_client._GRAMMAR_SUPPORT_CACHE), 0)
                with patch.object(llm_client, 'grammar_selftest', return_value=True):
                    llm_client.require_grammar_support('model-isolation-check')
                self.assertEqual(len(llm_client._GRAMMAR_SUPPORT_CACHE), 1)

            def test_b_verify_clean_reprobe(self):
                # Must start empty even though test_a_populate_cache ran right before
                self.assertEqual(len(llm_client._GRAMMAR_SUPPORT_CACHE), 0)
                # If cache leaked, require_grammar_support would return True without checking probe.
                # Since cache is isolated, returning False from selftest MUST raise AMLError!
                with patch.object(llm_client, 'grammar_selftest', return_value=False):
                    with self.assertRaisesRegex(AMLError, 'did not demonstrate'):
                        llm_client.require_grammar_support('model-isolation-check')

            def test_c_crash_during_execution(self):
                with patch.object(llm_client, 'grammar_selftest', return_value=True):
                    llm_client.require_grammar_support('model-crashed-test')
                raise RuntimeError("simulated test crash")

        # Forward execution order: a -> b
        suite_fwd = unittest.TestSuite([
            MockTestCase('test_a_populate_cache'),
            MockTestCase('test_b_verify_clean_reprobe'),
        ])
        res_fwd = unittest.TestResult()
        suite_fwd.run(res_fwd)
        self.assertTrue(res_fwd.wasSuccessful(), f"Forward order failed: {res_fwd.failures}, {res_fwd.errors}")
        self.assertEqual(len(llm_client._GRAMMAR_SUPPORT_CACHE), 0)

        # Reverse execution order: b -> a -> b
        suite_rev = unittest.TestSuite([
            MockTestCase('test_b_verify_clean_reprobe'),
            MockTestCase('test_a_populate_cache'),
            MockTestCase('test_b_verify_clean_reprobe'),
        ])
        res_rev = unittest.TestResult()
        suite_rev.run(res_rev)
        self.assertTrue(res_rev.wasSuccessful(), f"Reverse order failed: {res_rev.failures}, {res_rev.errors}")
        self.assertEqual(len(llm_client._GRAMMAR_SUPPORT_CACHE), 0)

        # Crash resilience: test_c crashes, but addCleanup/tearDown must still clear cache
        suite_crash = unittest.TestSuite([
            MockTestCase('test_c_crash_during_execution'),
            MockTestCase('test_b_verify_clean_reprobe'),
        ])
        res_crash = unittest.TestResult()
        suite_crash.run(res_crash)
        # Exactly 1 error expected (from test_c crash)
        self.assertEqual(len(res_crash.errors), 1)
        # test_b must still have passed cleanly after the crash
        self.assertEqual(len(res_crash.failures), 0)
        self.assertEqual(len(llm_client._GRAMMAR_SUPPORT_CACHE), 0)


if __name__=='__main__': unittest.main()
