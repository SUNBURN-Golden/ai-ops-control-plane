"""Offline Issue 19 qualification: real SQLite, fake GitHub/Slack/browser, no live credentials."""
import copy
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

import control_plane_flow_gateway as gateway
import control_plane_codex_relay as relay

flow = gateway.flow


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = flow.Store(self.root / 'flow.sqlite', initialize=True)
        self.store.initialize_consumers()
        self.action = flow.diagnostic_request('diag-r1', 'a'*40, 'diagnostic-consumer',
            'https://chatgpt.com/c/dedicated-diagnostic', flow.DIAGNOSTIC_ISSUE + '#issuecomment-1')
        self.review = f'https://github.com/{flow.DIAGNOSTIC_REPO}/pull/20#issuecomment-2'
        self.authority_body = '<!-- ASTRA_DIAGNOSTIC_AUTHORIZATION_V1 -->\n' + flow.canonical(
            dict(active=True, request=self.action, source_review_pointer=self.review))
        self.policy = dict(enabled=False, deployment_audit='PENDING', user_actors=['user'],
            projection_actor='mechanical', diagnostic=dict(enabled=True, source_review='PASS',
            source_review_pointer=self.review, credential_expires_at=int(time.time())+3600,
            request=self.action, slack_channel='CDIAGNOSTIC', authorization=dict(actor='user', comment_id=1,
            sha256=hashlib.sha256(self.authority_body.encode()).hexdigest())))
        self.secret = b'dedicated-diagnostic-secret-12345'
        self.api = Mock()
        self.api.call.side_effect = self.api_call
        self.posts = []
        self.comments = {}
        self.current_sha = self.action['subject']['head']
        self.loss_after_publish = False
        self.ports = gateway.GithubPorts(self.api, self.policy)
        self.executor = Mock()
        self.app = gateway.Ingress(self.store, self.ports, self.policy, b's'*32, b'g'*32,
            executor=self.executor, consumer_secret=b'o'*32, diagnostic_secret=self.secret)
        for directory in ('codex-home', 'relay-state'):
            (self.root/directory).mkdir(mode=0o700)
        binary = self.root/'fake-codex'; binary.write_text('never executed')
        self.relay_policy = dict(schema_version=1, enabled=True, mode='DIAGNOSTIC',
            diagnostic_source_review='PASS', source_review_pointer=self.review,
            diagnostic_request=self.action, live_acceptance='PENDING',
            model='gpt-5.6-sol', reasoning_effort='max', billing='CHATGPT_SUBSCRIPTION_ONLY',
            claim_url='https://authority.test/astra/diagnostic/claim', codex_binary=str(binary),
            codex_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            codex_home=str(self.root/'codex-home'), state_directory=str(self.root/'relay-state'),
            work_sessions={self.action['request_id']:self.action['work_session']})
        self.relay_policy['model_catalog'] = dict(source='codex app-server model/list',
            observed_at='2026-09-28T00:00:00Z', codex_sha256=self.relay_policy['codex_sha256'],
            codex_home=self.relay_policy['codex_home'], nextCursor=None, data=[dict(model='gpt-5.6-sol',
            hidden=False, supportedReasoningEfforts=[dict(reasoningEffort=e) for e in ('max','xhigh')])])
        self.sends = 0

    def comment(self, identifier, body, actor='mechanical'):
        return dict(id=identifier, body=body, user=dict(login=actor),
            issue_url=f'https://api.github.com/repos/{flow.DIAGNOSTIC_REPO}/issues/19',
            html_url=flow.DIAGNOSTIC_ISSUE + f'#issuecomment-{identifier}')

    def api_call(self, method, path, body=None, **kwargs):
        prefix = f'repos/{flow.DIAGNOSTIC_REPO}/'
        if method == 'GET':
            if path == prefix+'issues/comments/1':
                return self.comment(1, self.authority_body, 'user')
            if path == prefix+'issues/19':
                return dict(state='open')
            if path == prefix+'git/ref/heads/main':
                return dict(object=dict(sha=self.current_sha))
            if path.startswith(prefix+'issues/comments/'):
                return self.comments[int(path.rsplit('/',1)[1])]
        if method == 'POST' and path == prefix+'issues/19/comments':
            identifier = 100+len(self.posts)
            self.posts.append((path, body))
            comment = self.comment(identifier, body['body'])
            self.comments[identifier] = comment
            if self.loss_after_publish:
                raise TimeoutError('fixture: response lost after durable GitHub effect')
            return comment
        if method == 'POST' and path == 'chat.postMessage' and kwargs.get('slack') is True:
            self.posts.append((path, body))
            return dict(ok=True, channel=body['channel'], ts='1790000000.123456')
        raise AssertionError('Unexpected external capability: ' + method + ' ' + path)

    def request(self, path, value, secret=None, stamp=None):
        raw = flow.canonical(value).encode()
        stamp = str(int(time.time())) if stamp is None else stamp
        signature = hmac.new(self.secret if secret is None else secret,
            path.encode()+b'\n'+stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
        return self.wsgi(path, raw, {'X-Astra-Timestamp':stamp, 'X-Astra-Signature':signature})

    def wsgi(self, path, raw, headers):
        environ = dict(REQUEST_METHOD='POST', PATH_INFO=path, CONTENT_LENGTH=str(len(raw)))
        environ['wsgi.input'] = io.BytesIO(raw)
        environ.update({'HTTP_'+k.upper().replace('-','_'):v for k,v in headers.items()})
        start = Mock()
        result = flow.decode(b''.join(self.app(environ, start)))
        return start.call_args.args[0], result

    def claim_value(self):
        return dict(request_id=self.action['request_id'], session_id=self.action['work_session'])

    def claim(self):
        return self.request('/astra/diagnostic/claim', self.claim_value())

    def answer(self):
        return dict(kind='DIAGNOSTIC_RESULT', request_id=self.action['request_id'],
            subject=copy.deepcopy(self.action['subject']), identity=self.action['identity'],
            work_session=self.action['work_session'], observation=dict(answer='Keep UNKNOWN; never resend.',
            evidence_pointers=[f"https://github.com/{flow.DIAGNOSTIC_REPO}/blob/{self.current_sha}/{flow.DIAGNOSTIC_DOCUMENT}#L1-L3"],
            displayed_model='GPT-6 Astra', displayed_effort='medium', internal_model_id=None))

    def send(self, *args):
        self.sends += 1
        with sqlite3.connect(self.root/'relay-state/relay.sqlite3') as db:
            self.assertEqual(db.execute('SELECT state FROM requests').fetchone()[0], 'SUBMITTING')
        return dict(request_id=self.action['request_id'], work_session=self.action['work_session'],
                    observation='ANSWER', diagnostic_result=self.answer())

    def open_http(self, request, timeout):
        # Exercise real client HMAC/path construction against WSGI without a socket.
        status, result = self.wsgi(urlsplit(request.full_url).path, request.data, dict(request.header_items()))
        if status != '200 OK':
            raise HTTPError(request.full_url, int(status[:3]), 'fixture rejection', {}, None)
        return io.BytesIO(flow.canonical(result).encode())

    def run_relay(self, **kwargs):
        with patch.object(relay.urllib.request, 'build_opener', return_value=Mock(open=self.open_http)):
            return relay.deliver(self.relay_policy, self.action['request_id'], self.secret,
                                 send_fn=kwargs.pop('send_fn', self.send), **kwargs)

    def prepare(self):
        self.assertEqual(self.app.prepare_diagnostic()['state'], 'CONFIRMED')

    def test_first_diagnostic_complete_pipeline_without_live_pass_or_global_enable(self):
        self.prepare()
        result = self.run_relay()
        self.assertEqual(result['state'], 'DIAGNOSTIC_RECORDED')
        self.assertEqual(result['audit_result'], 'NOT_GRANTED')
        self.assertFalse(result['automatic_resume'])
        self.assertFalse(self.policy['enabled'])
        self.assertEqual(self.relay_policy['live_acceptance'], 'PENDING')
        self.assertEqual(self.sends, 1)
        self.assertEqual(len(self.posts), 3)  # projection, Slack, diagnostic evidence
        record = flow.decode(self.posts[-1][1]['body'].split('\n',1)[1])
        self.assertEqual(record['kind'], 'DIAGNOSTIC_RESULT')
        self.assertEqual(record['grants'], [])
        self.assertEqual(record['diagnostic_request_id'], self.action['request_id'])
        self.assertIn('NOT_PROVIDER_ATTESTATION', record['provenance'])
        self.assertIsNone(record['observation']['internal_model_id'])
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'], 'CLAIMED')
        self.executor.submit.assert_not_called()

    def test_operational_live_gate_unchanged(self):
        ordinary = dict(self.relay_policy, mode='OPERATIONAL', claim_url='https://authority.test/astra/claim')
        with self.assertRaises(relay.RelayError): relay.validate_policy(ordinary)
        for path in ('/astra/claim','/github/events','/slack/commands','/astra/diagnostic/prepare','/dispatch','/merge'):
            self.assertEqual(self.request(path, self.claim_value())[0], '403 Forbidden')
        self.assertEqual(self.posts, [])

    def test_diagnostic_cannot_be_promoted_or_change_fixed_scope(self):
        for key, value in (('kind','AUDIT'),('capabilities',['MERGE']),('document','other.md'),
                           ('question','approve'),('read_only',False),('task_pointer',flow.DIAGNOSTIC_ISSUE+'0')):
            changed = copy.deepcopy(self.action); changed[key] = value
            with self.subTest(key=key), self.assertRaises(flow.FlowError): flow.validate_diagnostic(changed)
        with self.assertRaises(flow.FlowError):
            flow.request(dict(self.action['subject'], task_pointer=flow.DIAGNOSTIC_ISSUE), 'DIAGNOSTIC', 'x', 'N/A')
        with self.assertRaises(flow.FlowError):
            flow.verify_result(self.answer(), dict(self.action,kind='AUDIT'), [], [])
        receipt = dict(request_id=self.action['request_id'], start_allowed=True, state='CLAIMED', action=self.action)
        with self.assertRaises(relay.RelayError): relay.validate_action(receipt, self.action['request_id'])

    def test_missing_dedicated_auth_or_source_review_prevents_preparation(self):
        for key, value in (('enabled',False),('source_review','PENDING'),('credential_expires_at',0)):
            prior = self.policy['diagnostic'][key]; self.policy['diagnostic'][key] = value
            with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
            self.policy['diagnostic'][key] = prior
        for secret in (b'', self.app.consumer_secret):
            self.app.diagnostic_secret = secret
            with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
        self.assertEqual(self.posts, [])

    def test_authority_edit_wrong_actor_wrong_issue_and_stale_main_rejected(self):
        self.current_sha = 'b'*40
        with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
        self.current_sha = self.action['subject']['head']
        self.authority_body += ' '
        with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
        self.authority_body = self.authority_body[:-1]
        self.policy['diagnostic']['authorization']['actor'] = 'attacker'
        with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
        self.assertEqual(self.posts, [])

    def test_projection_and_slack_are_real_preconditions(self):
        self.assertEqual(self.claim()[0], '403 Forbidden')
        self.prepare()
        projection = self.comments[100]
        projection['user']['login'] = 'attacker'
        self.assertEqual(self.claim()[0], '403 Forbidden')
        projection['user']['login'] = 'mechanical'
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET state='UNKNOWN' WHERE id=?", (self.action['request_id'],))
        self.assertEqual(self.claim()[0], '403 Forbidden')

    def test_concurrent_prepare_and_claim_send_only_once(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: self.app.prepare_diagnostic(), range(5)))
        # A losing preparation may see SUBMITTING as UNKNOWN; never publish again.
        self.assertEqual(len(self.posts), 2)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.claim(), range(12)))
        self.assertEqual(sum(result.get('start_allowed') is True for _,result in results), 1)
        self.assertEqual(len(self.posts), 2)

    def test_wrong_claim_session_request_signature_and_operation_denied(self):
        self.prepare()
        for value in (dict(self.claim_value(), session_id='https://chatgpt.com/c/other'),
                      dict(self.claim_value(), request_id='b'*64), dict(self.claim_value(), identity='attacker')):
            self.assertEqual(self.request('/astra/diagnostic/claim', value)[0], '403 Forbidden')
        for secret in (b'wrong', self.app.consumer_secret):
            self.assertEqual(self.request('/astra/diagnostic/claim', self.claim_value(), secret=secret)[0], '403 Forbidden')
        self.assertEqual(self.request('/astra/diagnostic/claim',self.claim_value(),stamp='1')[0], '403 Forbidden')
        raw = flow.canonical(self.claim_value()).encode(); stamp = str(int(time.time()))
        legacy = hmac.new(self.secret, stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
        self.assertEqual(self.wsgi('/astra/diagnostic/claim', raw,
            {'X-Astra-Timestamp':stamp,'X-Astra-Signature':legacy})[0], '403 Forbidden')

    def test_no_result_without_claim(self):
        self.prepare()
        self.assertEqual(self.request('/astra/diagnostic/result', self.answer())[0], '403 Forbidden')
        self.assertEqual(len(self.posts), 2)

    def test_stale_wrong_consumer_conversation_and_extra_authority_results_rejected(self):
        self.prepare(); self.claim()
        mutations = [('request_id','b'*64), ('identity','other'),('work_session','https://chatgpt.com/c/other'),
                     ('kind','AUDIT_RESULT'),('result','PASS'),('grants',['merge'])]
        for key,value in mutations:
            result = self.answer(); result[key] = value
            self.assertEqual(self.request('/astra/diagnostic/result',result)[0], '403 Forbidden', key)
        for field in ('head','revision','task_id','policy_revision'):
            result = self.answer(); result['subject'][field] = 'stale'
            self.assertEqual(self.request('/astra/diagnostic/result', result)[0], '403 Forbidden')
        for key,value in (('displayed_model','GPT-6 Sol'),('displayed_effort','high'),
                          ('internal_model_id','gpt-6-astra'),('evidence_pointers',['https://evil.test/'])):
            result = self.answer(); result['observation'][key] = value
            self.assertEqual(self.request('/astra/diagnostic/result',result)[0], '403 Forbidden')
        self.assertEqual(len(self.posts), 2)

    def test_duplicate_results_applied_once_and_conflicting_answer_rejected(self):
        self.prepare(); self.claim()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.request('/astra/diagnostic/result',self.answer()), range(12)))
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(sum(v.get('sent') is True for _,v in results), 1)
        different = self.answer(); different['observation']['answer'] = 'Changed answer'
        self.assertEqual(self.request('/astra/diagnostic/result',different)[0], '403 Forbidden')
        self.assertEqual(len(self.posts), 3)

    def test_expired_credential_and_changed_head_do_not_release_claim(self):
        self.prepare(); self.claim()
        self.policy['diagnostic']['credential_expires_at'] = 1
        self.assertEqual(self.request('/astra/diagnostic/result',self.answer())[0], '403 Forbidden')
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'], 'CLAIMED')
        self.policy['diagnostic']['credential_expires_at'] = int(time.time())+3600
        self.current_sha = 'c'*40
        self.assertEqual(self.request('/astra/diagnostic/result',self.answer())[0], '403 Forbidden')
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'], 'CLAIMED')

    def test_unknown_result_publication_never_reposts_or_releases(self):
        self.prepare(); self.claim(); self.loss_after_publish = True
        first = self.request('/astra/diagnostic/result',self.answer())[1]
        second = self.request('/astra/diagnostic/result',self.answer())[1]
        self.assertEqual((first['state'],second['state']), ('UNKNOWN','UNKNOWN'))
        self.assertEqual(len(self.posts),3)
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'],'CLAIMED')

    def test_claim_response_loss_never_resends_or_switches(self):
        self.prepare()
        def lost(*args):
            self.assertTrue(self.claim()[1]['start_allowed'])
            raise TimeoutError('response lost')
        first = self.run_relay(claim_fn=lost)
        self.assertEqual(first['state'],'UNKNOWN')
        self.assertEqual(self.run_relay()['state'],'UNKNOWN')
        self.assertFalse(self.claim()[1]['start_allowed'])
        self.assertEqual(self.sends,0)
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'],'CLAIMED')

    def test_unknown_result_blocks_even_operator_consumer_reconciliation(self):
        self.prepare(); self.claim(); self.loss_after_publish = True
        self.assertEqual(self.request('/astra/diagnostic/result', self.answer())[1]['state'], 'UNKNOWN')
        with self.assertRaises(flow.FlowError):
            self.store.reconcile_astra(self.action['request_id'], self.action['identity'],
                self.action['work_session'], self.comments[102]['html_url'], consumer_fenced=True)
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'], 'CLAIMED')
        self.assertEqual(len(self.posts), 3)

    def test_pending_result_fences_replacement_even_if_consumer_was_already_reconciled(self):
        self.prepare(); self.claim()
        self.request('/astra/diagnostic/result', self.answer())
        self.store.reconcile_astra(self.action['request_id'], self.action['identity'],
            self.action['work_session'], self.comments[102]['html_url'], consumer_fenced=True)
        result_id = flow.digest(['diagnostic-result', self.action['request_id']])
        replacement = flow.diagnostic_request('diag-r2', 'b'*40, self.action['identity'],
            'https://chatgpt.com/c/other', self.action['designation'])
        ordinary = flow.request(dict(self.action['subject'], task_pointer=flow.DIAGNOSTIC_ISSUE),
                                'DISPATCH', 'builder', 'N/A')
        # Simulate a pre-fix ledger/crash marker, not a supported live state mutation.
        for state, valid in (('NOT_STARTED', 1), ('SUBMITTING', 1), ('UNKNOWN', 1), ('UNKNOWN', 0)):
            with self.store.transaction() as db:
                db.execute('UPDATE outbox SET state=?,valid=? WHERE id=?', (state, valid, result_id))
            for candidate in (replacement, ordinary):
                with self.subTest(state=state, valid=valid, kind=candidate['kind']), self.assertRaises(flow.FlowError):
                    self.store.reserve(candidate)
        self.assertEqual(len(self.posts), 3)

    def test_result_send_marker_rechecks_claim_atomically_after_earlier_validation(self):
        self.prepare(); self.claim()
        self.store.diagnostic_claim(self.action)  # collector's earlier authority read
        self.store.reconcile_astra(self.action['request_id'], self.action['identity'],
            self.action['work_session'], self.action['designation'], consumer_fenced=True)
        send = Mock()
        result = flow.diagnostic_result(self.action, self.answer())
        # Even an earlier/successful current check cannot admit an ended claim.
        with self.assertRaises(flow.FlowError):
            self.store.send_once(result, send, lambda _: True)
        send.assert_not_called()
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?',
                             (result['request_id'],)).fetchone()[0], 'NOT_STARTED')

    def test_result_reservation_and_submitting_marker_each_fence_reconciliation(self):
        self.prepare(); self.claim()
        result = flow.diagnostic_result(self.action, self.answer())
        def try_reconcile():
            with self.assertRaises(flow.FlowError):
                self.store.reconcile_astra(self.action['request_id'], self.action['identity'],
                    self.action['work_session'], self.action['designation'], consumer_fenced=True)
        def current(_):
            try_reconcile()
            return True
        def send(action):
            try_reconcile()
            return dict(accepted=True, request_id=action['request_id'])
        self.assertEqual(self.store.send_once(result, send, current)['state'], 'CONFIRMED')
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'], 'CLAIMED')

    def test_send_response_loss_and_process_interruption_are_unknown(self):
        self.prepare()
        def interrupted(*args):
            self.sends += 1
            raise KeyboardInterrupt()
        self.assertEqual(self.run_relay(send_fn=interrupted)['state'],'UNKNOWN')
        self.assertEqual(self.run_relay()['state'],'UNKNOWN')
        self.assertEqual(self.sends,1)
        self.assertEqual(self.store.diagnostic_claim(self.action)['state'],'CLAIMED')

    def test_result_response_loss_is_not_reapplied_by_relay(self):
        self.prepare()
        def lost(p, operation, result, secret):
            response = self.request('/astra/diagnostic/result',result)
            self.assertEqual(response[1]['state'],'CONFIRMED')
            raise TimeoutError('collector response lost')
        self.assertEqual(self.run_relay(submit_fn=lost)['state'],'UNKNOWN')
        self.assertEqual(self.run_relay()['state'],'UNKNOWN')
        self.assertEqual((self.sends,len(self.posts)),(1,3))

    def test_waiting_collection_is_explicit_read_only_and_keeps_recorded_model(self):
        self.prepare()
        def waiting(*args):
            value = self.send(*args); value.update(observation='WAITING',diagnostic_result=None)
            return value
        self.assertEqual(self.run_relay(send_fn=waiting)['state'],'WAITING')
        observations = []
        def observe(p, action, session, root):
            observations.append(p)
            self.assertTrue(p['observe_only'])
            self.assertIn('Never send any message',relay.diagnostic_prompt(action,True))
            return dict(request_id=action['request_id'],work_session=session,observation='ANSWER',diagnostic_result=self.answer())
        with patch.object(relay.urllib.request,'build_opener',return_value=Mock(open=self.open_http)):
            result = relay.collect_existing(self.relay_policy,self.action['request_id'],self.secret,observe_fn=observe)
            self.assertEqual(result['state'],'DIAGNOSTIC_RECORDED')
            again = relay.collect_existing(self.relay_policy,self.action['request_id'],self.secret,observe_fn=observe)
        self.assertEqual(again['state'],'DIAGNOSTIC_RECORDED')
        self.assertEqual(len(observations),1)
        self.assertEqual(self.sends,1)
        self.assertEqual(observations[0]['reasoning_effort'],'max')

    def test_local_crash_markers_are_reported_unknown_without_relaunch(self):
        self.prepare()
        self.run_relay()
        for state in ('CLAIMING','SUBMITTING','RESULT_SUBMITTING','OBSERVING'):
            with sqlite3.connect(self.root/'relay-state/relay.sqlite3') as db:
                db.execute('UPDATE requests SET state=?',(state,))
            result = self.run_relay()
            self.assertEqual((result['state'],result['durable_state']),('UNKNOWN',state))
        self.assertEqual(self.sends,1)

    def test_unresolved_ordinary_request_blocks_diagnostic_before_projection(self):
        prior = flow.request(dict(self.action['subject'],task_pointer=flow.DIAGNOSTIC_ISSUE), 'AUDIT', 'auditor', self.review)
        self.store.reserve(prior)
        with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
        self.assertEqual(self.posts,[])

    def test_diagnostic_blocks_new_revision_and_operational_dispatch(self):
        self.prepare(); self.claim()
        replacement = flow.diagnostic_request('diag-r2','b'*40,self.action['identity'],
            'https://chatgpt.com/c/other',self.action['designation'])
        with self.assertRaises(flow.FlowError): self.store.reserve(replacement)
        ordinary = flow.request(dict(self.action['subject'],task_pointer=flow.DIAGNOSTIC_ISSUE), 'DISPATCH', 'builder', 'N/A')
        with self.assertRaises(flow.FlowError): self.store.reserve(ordinary)
        self.store.invalidate(self.action['request_id'])
        with self.assertRaises(flow.FlowError): self.store.reserve(ordinary)

    def test_child_does_not_receive_consumer_or_github_secrets(self):
        root = self.root/'child'; root.mkdir()
        value = dict(request_id=self.action['request_id'], work_session=self.action['work_session'],
                     observation='ANSWER',diagnostic_result=self.answer())
        def fake_run(command,**kwargs):
            self.assertEqual(set(kwargs['env']),{'PATH','HOME','CODEX_HOME','LANG'})
            self.assertNotIn(self.secret.decode(),kwargs['input'])
            self.assertIn('read-only',command)
            self.assertIn('No GitHub mutation',kwargs['input'])
            (root/'receipt.json').write_text(json.dumps(value))
            return subprocess.CompletedProcess(command,0)
        with patch.dict(os.environ,ASTRA_FLOW_DIAGNOSTIC_CONSUMER_SECRET=self.secret.decode(),GITHUB_TOKEN='fake-fixture'), \
             patch.object(relay.subprocess,'run',side_effect=fake_run):
            receipt = relay.run_codex(self.relay_policy,self.action,self.action['work_session'],root)
        self.assertEqual(receipt['observation'],'ANSWER')

    def test_operational_credential_cannot_claim_diagnostic_even_when_operations_enabled(self):
        self.prepare()
        self.policy.update(enabled=True, astra_consumer=dict(enabled=True,identity=self.action['identity']))
        raw = flow.canonical(self.claim_value()).encode(); stamp = str(int(time.time()))
        for secret in (self.secret, self.app.consumer_secret):
            signature = hmac.new(secret,stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
            status,_ = self.wsgi('/astra/claim',raw,{'X-Astra-Timestamp':stamp,'X-Astra-Signature':signature})
            self.assertEqual(status,'403 Forbidden')
        self.assertTrue(self.claim()[1]['start_allowed'])

    def test_prepare_publication_crash_is_unknown_and_never_reposted(self):
        original = self.api.call.side_effect
        def killed(method,path,body=None,**kwargs):
            result = original(method,path,body,**kwargs)
            if method == 'POST': raise KeyboardInterrupt()
            return result
        self.api.call.side_effect = killed
        with self.assertRaises(KeyboardInterrupt): self.app.prepare_diagnostic()
        self.api.call.side_effect = original
        self.assertEqual(self.app.prepare_diagnostic()['state'],'PROJECTION_UNKNOWN')
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.claim()[0],'403 Forbidden')

    def test_unknown_slack_notification_never_retried(self):
        original = self.api.call.side_effect
        def lost(method,path,body=None,**kwargs):
            result = original(method,path,body,**kwargs)
            if path == 'chat.postMessage': raise TimeoutError()
            return result
        self.api.call.side_effect = lost
        self.assertEqual(self.app.prepare_diagnostic()['state'],'UNKNOWN')
        self.assertEqual(self.app.prepare_diagnostic()['state'],'UNKNOWN')
        self.assertEqual(len(self.posts),2)
        self.assertEqual(self.claim()[0],'403 Forbidden')

    def test_existing_graph_owner_and_pending_graph_implementation_block_diagnostic(self):
        with self.store.transaction() as db:
            db.execute('CREATE TABLE graph_owners (task TEXT PRIMARY KEY, request TEXT, session TEXT)')
            db.execute('INSERT INTO graph_owners VALUES (?,?,?)',
                (flow.canonical([flow.DIAGNOSTIC_REPO,'CP-LOCAL-001']),'prior','existing-writer'))
        with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
        self.assertEqual(len(self.posts),0)
        with self.store.transaction() as db: db.execute('DELETE FROM graph_owners')
        prior = dict(self.action,kind='IMPLEMENT',request_id='b'*64)
        self.store.reserve(prior)
        with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()

    def test_reconciled_consumer_never_grants_second_start(self):
        self.prepare(); self.claim()
        self.request('/astra/diagnostic/result',self.answer())
        self.store.reconcile_astra(self.action['request_id'],self.action['identity'],self.action['work_session'],
            self.comments[102]['html_url'],consumer_fenced=True)
        self.assertFalse(self.claim()[1]['start_allowed'])
        self.assertEqual(self.request('/astra/diagnostic/result',self.answer())[0],'403 Forbidden')
        self.assertEqual(len(self.posts),3)

    def test_missing_consumer_schema_is_not_created_on_diagnostic_admission(self):
        with self.store.transaction() as db: db.execute('DROP TABLE astra_claims')
        with self.assertRaises(flow.FlowError): self.app.prepare_diagnostic()
        self.assertEqual(len(self.posts),0)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='astra_claims'").fetchone())


if __name__ == '__main__':
    unittest.main()
