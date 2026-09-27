import hashlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import control_plane_codex_relay as relay
import control_plane_flow as flow


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'codex'
        self.binary.write_text('test binary')
        for d in ('home', 'state'):
            (self.root/d).mkdir(mode=0o700)
        self.rid = 'a'*64
        self.session = 'https://chatgpt.com/c/test-session'
        self.policy = dict(schema_version=1, enabled=True, model='gpt-6-sol', reasoning_effort='ultra',
            billing='CHATGPT_SUBSCRIPTION_ONLY', live_acceptance='PASS',
            evidence_pointer='https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/19',
            claim_url='https://control.example/astra/claim', codex_binary=str(self.binary),
            codex_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            state_directory=str(self.root/'state'), codex_home=str(self.root/'home'),
            work_sessions={self.rid:self.session})
        self.policy['model_catalog'] = dict(
            source='codex app-server model/list', observed_at='2026-09-27T11:28:12Z',
            codex_sha256=self.policy['codex_sha256'], codex_home=self.policy['codex_home'],
            data=[dict(model='gpt-6-sol', hidden=False,
                       supportedReasoningEfforts=[dict(reasoningEffort='ultra')])], nextCursor=None)
        self.action = dict(request_id=self.rid, kind='AUDIT', identity='designated-astra',
            designation='https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/19',
            task_pointer='https://github.com/BeautifulMind-JT/ZARI/issues/11',
            subject=dict(repository='BeautifulMind-JT/ZARI',head='b'*40,revision='1'))
        self.claims = self.sends = 0

    def claim(self, *args):
        self.claims += 1
        return dict(request_id=self.rid, start_allowed=True, state='CLAIMED', action=self.action)

    def send(self, *args):
        self.sends += 1
        return dict(request_id=self.rid,work_session=self.session,observation='WAITING',result_pointer=None)

    def run_relay(self, **kw):
        return relay.deliver(self.policy, self.rid, b's'*32,
            claim_fn=kw.get('claim_fn', self.claim), send_fn=kw.get('send_fn', self.send))

    def test_duplicate_never_claims_or_sends_twice(self):
        self.assertEqual(self.run_relay()['audit_result'], 'NOT_GRANTED')
        self.assertFalse(self.run_relay()['sent'])
        self.assertEqual((self.claims,self.sends),(1,1))

    def test_concurrent_deliveries_get_only_one_send(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.run_relay(), range(2)))
        self.assertEqual((self.claims,self.sends),(1,1))
        self.assertEqual(sum(r.get('sent') is False for r in results),1)

    def test_missing_credential_has_no_claim_or_tombstone(self):
        with self.assertRaises(relay.RelayError):
            relay.deliver(self.policy,self.rid,b'',self.claim,self.send)
        self.assertFalse((self.root/'state'/'relay.sqlite3').exists())

    def test_response_loss_preserves_unknown_without_second_post(self):
        def lost(*args):
            self.claims += 1
            raise TimeoutError('secret error must not leak')
        self.assertEqual(self.run_relay(claim_fn=lost)['state'],'UNKNOWN')
        self.assertEqual(self.run_relay()['state'],'UNKNOWN')
        self.assertEqual((self.claims,self.sends),(1,0))

    def test_browser_crash_or_response_loss_never_resends(self):
        def lost(*args):
            self.sends += 1
            raise subprocess.TimeoutExpired(['codex'],180)
        self.assertEqual(self.run_relay(send_fn=lost)['state'],'UNKNOWN')
        self.run_relay()
        self.assertEqual((self.claims,self.sends),(1,1))

    def test_central_denial_after_local_disk_loss_still_blocks(self):
        def denied(*args):
            return dict(request_id=self.rid,start_allowed=False,state='CLAIMED')
        self.assertEqual(self.run_relay(claim_fn=denied)['state'],'CLAIM_DENIED')
        self.assertEqual(self.sends,0)

    def test_wrong_claim_identity_never_sends(self):
        def wrong(*args):
            return dict(request_id='c'*64,start_allowed=True,state='CLAIMED',action=self.action)
        self.assertEqual(self.run_relay(claim_fn=wrong)['state'],'UNKNOWN')
        self.assertEqual(self.sends,0)

    def test_real_central_decision_contract_delivers(self):
        snapshot = dict(repository='BeautifulMind-JT/ZARI', task_id='ZARI-DECISION',
            revision='1', head='b'*40, base='c'*40, policy_revision='d'*40,
            task_digest='e'*64, task_pointer=self.action['task_pointer'])
        self.action = flow.request(snapshot,'DECISION','ASTRA','N/A')
        self.rid = self.action['request_id']
        self.policy['work_sessions'] = {self.rid:self.session}
        self.assertEqual(self.run_relay()['state'],'OBSERVATION_ONLY')
        self.assertEqual((self.claims,self.sends),(1,1))

    def test_audit_still_requires_designation_and_decision_requires_astra(self):
        for changes in (dict(kind='AUDIT',designation='N/A'),
                        dict(kind='DECISION',identity='unconfigured',designation='N/A')):
            with self.subTest(changes=changes), self.assertRaises(relay.RelayError):
                relay.validate_action(dict(request_id=self.rid,start_allowed=True,
                    state='CLAIMED',action=dict(self.action,**changes)),self.rid)

    def test_browser_unknown_and_blocked_remain_fenced_and_non_success(self):
        for status in ('UNKNOWN','BLOCKED'):
            with self.subTest(status=status):
                self.rid = ('d' if status == 'UNKNOWN' else 'e')*64
                self.session = 'https://chatgpt.com/c/'+status
                self.action['request_id'] = self.rid
                self.policy['work_sessions'] = {self.rid:self.session}
                def observed(*args):
                    return dict(self.send(),observation=status)
                result = self.run_relay(send_fn=observed)
                self.assertEqual(result['state'],status)
                self.assertEqual(result['audit_result'],'NOT_GRANTED')
                self.assertFalse(result['automatic_resume'])
                self.assertFalse(self.run_relay()['sent'])
                policy = self.root/'policy.json'
                policy.write_text(json.dumps(self.policy)); policy.chmod(0o600)
                with patch('sys.argv',['relay','deliver','--policy',str(policy),
                                       '--request-id',self.rid]), \
                     patch.dict(os.environ,{'ASTRA_FLOW_ASTRA_CONSUMER_SECRET':'s'*32}), \
                     patch('builtins.print'):
                    old_umask = os.umask(0o077)
                    try: self.assertEqual(relay.main(),2)
                    finally: os.umask(old_umask)
        self.assertEqual((self.claims,self.sends),(2,2))

    def test_real_sqlite_reservation_survives_process_interruption(self):
        def crash(*args):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_relay(claim_fn=crash)
        self.assertEqual(self.run_relay()['state'],'CLAIMING')
        self.assertEqual(self.sends,0)

    def test_policy_changes_fail_closed_before_side_effect(self):
        for changes in (dict(enabled=False),dict(live_acceptance='PENDING'),
                        dict(model='auto'),dict(codex_sha256='0'*64),
                        dict(billing='API'),dict(claim_url='http://host/astra/claim'),
                        dict(claim_url='https://user:secret@host/astra/claim'),
                        dict(work_sessions={self.rid:'https://chatgpt.com.evil/c/x'}),
                        dict(work_sessions={self.rid:self.session,'b'*64:self.session})):
            with self.subTest(changes=changes), self.assertRaises(relay.RelayError):
                relay.deliver(dict(self.policy,**changes), self.rid,b's'*32,self.claim,self.send)
        self.assertEqual((self.claims,self.sends),(0,0))

    def test_explicit_catalog_model_is_forwarded_without_default_or_upgrade(self):
        self.policy['model'] = 'gpt-5.6-terra'
        self.policy['model_catalog']['data'].append(dict(
            model='gpt-5.6-terra', hidden=False, isDefault=False,
            upgrade='gpt-6-astra', supportedReasoningEfforts=[dict(reasoningEffort='ultra')]))
        self.policy['model_catalog']['data'][0]['isDefault'] = True
        def fake_run(command, **kwargs):
            self.assertEqual(command[command.index('--model')+1], 'gpt-5.6-terra')
            self.assertEqual(command[command.index('-c')+1], 'model_reasoning_effort="ultra"')
            Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(self.send()))
            return subprocess.CompletedProcess(command, 0)
        with patch.object(relay.subprocess, 'run', side_effect=fake_run) as run:
            self.assertEqual(self.run_relay(send_fn=relay.run_codex)['state'], 'OBSERVATION_ONLY')
        self.assertEqual((self.claims, self.sends, run.call_count), (1, 1, 1))

    def test_unlisted_or_placeholder_model_cannot_claim_or_reserve(self):
        for model in ('unlisted-model', 'auto', 'default', 'CONFIG_REQUIRED', '', None):
            with self.subTest(model=model), self.assertRaises(relay.RelayError):
                relay.deliver(dict(self.policy, model=model), self.rid, b's'*32, self.claim, self.send)
        self.assertEqual((self.claims, self.sends), (0, 0))
        self.assertFalse((self.root/'state'/'relay.sqlite3').exists())

    def test_catalog_must_match_environment_and_be_complete(self):
        original = self.policy['model_catalog']
        bad_catalogs = [None, {}, dict(original, source='example'),
                        dict(original, codex_sha256='0'*64),
                        dict(original, codex_home=str(self.root/'another-home')),
                        dict(original, observed_at='PENDING'),
                        dict(original, observed_at='2026-09-27T11:28:12'),
                        dict(original, nextCursor='another-page'),
                        {k:v for k,v in original.items() if k != 'nextCursor'},
                        dict(original, data=[None])]
        for catalog in bad_catalogs:
            with self.subTest(catalog=catalog), self.assertRaises(relay.RelayError):
                relay.deliver(dict(self.policy, model_catalog=catalog), self.rid,
                              b's'*32, self.claim, self.send)
        self.assertEqual((self.claims, self.sends), (0, 0))
        self.assertFalse((self.root/'state'/'relay.sqlite3').exists())

    def test_hidden_ambiguous_or_unsupported_effort_models_are_rejected(self):
        entry = self.policy['model_catalog']['data'][0]
        cases = [[], [dict(entry, hidden=True)], [dict(entry, hidden=None)], [entry, entry],
                 [dict(entry, supportedReasoningEfforts=[dict(reasoningEffort='medium')])],
                 [dict(entry, supportedReasoningEfforts=None)],
                 [dict(entry, supportedReasoningEfforts=[dict(reasoningEffort='low')])]]
        for entries in cases:
            policy = copy.deepcopy(self.policy)
            policy['model_catalog']['data'] = entries
            with self.subTest(entries=entries), self.assertRaises(relay.RelayError):
                relay.deliver(policy, self.rid, b's'*32, self.claim, self.send)
        self.assertEqual((self.claims, self.sends), (0, 0))
        self.assertFalse((self.root/'state'/'relay.sqlite3').exists())

    def test_prompt_preserves_exact_action_and_denies_semantic_authority(self):
        prompt=relay.browser_prompt(self.action,self.session)
        self.assertIn(relay.canonical(self.action),prompt)
        for text in ('never Astra or the User','Submit the exact request once',
                     'UNKNOWN','medium reasoning','never paraphrase','Do not repeatedly inspect'):
            self.assertIn(text,prompt)

    def allow_fallback(self):
        self.policy['fallback'] = dict(model='gpt-5.6-sol',reasoning_effort='xhigh')
        self.policy['model_catalog']['data'].append(dict(model='gpt-5.6-sol',hidden=False,
            supportedReasoningEfforts=[dict(reasoningEffort='xhigh')]))

    def test_approved_fallback_is_selected_before_claim_and_persisted(self):
        self.allow_fallback()
        self.policy['model_catalog']['data'].pop(0)
        def fake_run(command,**kwargs):
            self.assertEqual(command[command.index('--model')+1],'gpt-5.6-sol')
            self.assertEqual(command[command.index('-c')+1],'model_reasoning_effort="xhigh"')
            with sqlite3.connect(self.root/'state'/'relay.sqlite3') as db:
                self.assertEqual(db.execute('SELECT model,effort FROM request_models').fetchone(),
                                 ('gpt-5.6-sol','xhigh'))
            Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(self.send()))
            return subprocess.CompletedProcess(command,0)
        with patch.object(relay.subprocess,'run',side_effect=fake_run):
            result=self.run_relay(send_fn=relay.run_codex)
        self.assertEqual((result['selected_model'],result['selected_effort']),('gpt-5.6-sol','xhigh'))
        self.assertEqual((self.claims,self.sends),(1,1))

    def test_primary_preferred_and_post_claim_failure_never_switches(self):
        self.allow_fallback()
        def lost(p,*args):
            self.assertEqual((p['model'],p['reasoning_effort']),('gpt-6-sol','ultra'))
            self.sends+=1
            raise TimeoutError()
        self.assertEqual(self.run_relay(send_fn=lost)['state'],'UNKNOWN')
        self.policy['model_catalog']['data'].pop(0)
        self.assertEqual(self.run_relay()['state'],'UNKNOWN')
        self.assertEqual((self.claims,self.sends),(1,1))

    def test_fallback_requires_exact_authorization_and_supported_effort(self):
        self.allow_fallback()
        original=copy.deepcopy(self.policy)
        for change in ('wrong_pair','unsupported','ambiguous'):
            self.policy=copy.deepcopy(original)
            if change=='wrong_pair': self.policy['fallback']['reasoning_effort']='high'
            if change=='unsupported':
                self.policy['model_catalog']['data'].pop(0)
                self.policy['model_catalog']['data'][0]['supportedReasoningEfforts']=[]
            if change=='ambiguous': self.policy['model_catalog']['data'].append(
                copy.deepcopy(self.policy['model_catalog']['data'][0]))
            with self.subTest(change=change),self.assertRaises(relay.RelayError): self.run_relay()
        self.assertEqual((self.claims,self.sends),(0,0))
        self.assertFalse((self.root/'state'/'relay.sqlite3').exists())

    def test_missing_primary_effort_can_select_only_approved_alternative(self):
        self.allow_fallback()
        self.policy['model_catalog']['data'][0]['supportedReasoningEfforts']=[dict(reasoningEffort='low')]
        self.assertEqual(relay.validate_model(self.policy),self.policy['fallback'])

    def test_central_hmac_wire_and_redirect_rejection(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n): return b'{"start_allowed":false}'
        with patch.object(relay.urllib.request,'build_opener') as build, patch.object(relay.time,'time',return_value=42):
            build.return_value.open.return_value=Response()
            relay.claim(self.policy,self.rid,self.session,b's'*32)
            request=build.return_value.open.call_args.args[0]
            import hmac
            self.assertEqual(request.get_header('X-astra-signature'),
                hmac.new(b's'*32,b'42.'+request.data,hashlib.sha256).hexdigest())
            self.assertNotIn(b's'*32,request.data)
            self.assertEqual(build.return_value.open.call_count,1)
        with self.assertRaises(relay.RelayError): relay.NoRedirect().redirect_request()

    def test_codex_child_has_no_gateway_or_api_credentials_and_only_returns_observation(self):
        root=self.root/'request';root.mkdir()
        def fake_run(command,**kwargs):
            self.assertEqual(kwargs['env']['HOME'],self.policy['codex_home'])
            self.assertNotIn('ASTRA_FLOW_ASTRA_CONSUMER_SECRET',kwargs['env'])
            self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
            self.assertNotIn('GITHUB_TOKEN',kwargs['env'])
            self.assertIn('--ephemeral',command)
            self.assertIn('read-only',command)
            self.assertNotIn('danger-full-access',command)
            (root/'receipt.json').write_text(json.dumps(self.send()))
            return subprocess.CompletedProcess(command,0)
        with patch.dict(os.environ,{'ASTRA_FLOW_ASTRA_CONSUMER_SECRET':'never-copy','OPENAI_API_KEY':'no-api'}), \
             patch.object(relay.subprocess,'run',side_effect=fake_run):
            self.assertEqual(relay.run_codex(self.policy,self.action,self.session,root)['observation'],'WAITING')

    def test_cross_request_browser_output_rejected(self):
        root=self.root/'request';root.mkdir()
        (root/'receipt.json').write_text(json.dumps(dict(request_id='c'*64,
            work_session=self.session,observation='RESULT_POINTER',result_pointer=None)))
        with patch.object(relay.subprocess,'run',return_value=subprocess.CompletedProcess([],0)), \
             self.assertRaises(relay.RelayError):
            relay.run_codex(self.policy,self.action,self.session,root)


if __name__ == '__main__': unittest.main()
