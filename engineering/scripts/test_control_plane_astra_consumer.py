"""Real SQLite receiver races and authenticated ingress; no LLM calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
import control_plane_flow_gateway as g

f = g.flow


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'flow.db'
        self.store = f.Store(self.path, initialize=True); self.store.initialize_consumers()
        self.subject = dict(repository='owner/repo', task_id='T1', revision='1', head='a'*40,
                            base='b'*40, policy_revision='c'*64, task_digest='d'*64)
        self.action = f.request(dict(self.subject, task_pointer='https://github.com/owner/repo/issues/1'),
                                'AUDIT', 'auditor', 'https://github.com/owner/repo/issues/1#issuecomment-2')
        self.deliver(self.action)
        self.ports = Mock(); self.ports.is_current.return_value = True
        self.policy = {'enabled': True, 'astra_consumer': {'enabled': True, 'identity': 'auditor'}}
        self.secret = b's'*32
        self.app = g.Ingress(self.store, self.ports, self.policy, b's'*32, b'g'*32,
                             executor=Mock(), consumer_secret=self.secret)

    def deliver(self, action):
        projection = dict(action, request_id=f.digest(['projection', action['request_id']]))
        self.store.send_once(projection, lambda a: dict(accepted=True, request_id=a['request_id'],
            pointer='https://github.com/owner/repo/issues/1#issuecomment-3'), lambda a: True)
        self.store.send_once(action, lambda a: dict(accepted=True, request_id=a['request_id']), lambda a: True)

    def headers(self, raw, stamp=None):
        stamp = str(int(time.time())) if stamp is None else str(stamp)
        return {'x-astra-timestamp': stamp, 'x-astra-signature': hmac.new(
            self.secret, stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()}

    def claim(self, session='audit-session'):
        raw = f.canonical({'request_id': self.action['request_id'], 'session_id': session}).encode()
        return self.app.claim_consumer(raw, self.headers(raw))

    def test_concurrent_deliveries_start_one_consumer(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.claim('session-'+str(i)), range(12)))
        self.assertEqual(sum(r['start_allowed'] for r in results), 1)

    def test_lost_response_same_or_new_session_never_restarts(self):
        self.claim()
        self.assertFalse(self.claim()['start_allowed'])
        self.assertFalse(self.claim('different')['start_allowed'])
        self.assertNotIn('action', self.claim())

    def test_unknown_sender_or_stale_designation_does_not_claim(self):
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET state='UNKNOWN' WHERE id=?", (self.action['request_id'],))
        with self.assertRaises(f.FlowError): self.claim()

    def test_stale_head_or_revoked_designation_is_rejected(self):
        self.ports.is_current.return_value = False
        with self.assertRaises(f.FlowError): self.claim()
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM astra_claims').fetchone()[0], 0)

    def test_unresolved_old_head_blocks_replacement(self):
        self.claim()
        newer = f.request(dict(self.subject, head='e'*40, task_pointer=self.action['task_pointer']),
                          'AUDIT', 'auditor', self.action['designation'])
        self.deliver(newer)
        with self.assertRaises(f.FlowError): self.store.claim_astra(newer, 'auditor', 'new')

    def test_reconcile_requires_owner_fence_evidence_and_grants_no_pass(self):
        self.claim(); key = self.action['request_id']; ev = self.action['task_pointer']+'#issuecomment-7'
        for kwargs in ({}, {'consumer_fenced':False}):
            with self.assertRaises(f.FlowError): self.store.reconcile_astra(key, 'auditor', 'audit-session', ev, **kwargs)
        with self.assertRaises(f.FlowError):
            self.store.reconcile_astra(key, 'auditor', 'wrong-session', ev, consumer_fenced=True)
        result = self.store.reconcile_astra(key, 'auditor', 'audit-session', ev, consumer_fenced=True)
        self.assertEqual(result['audit_result'], 'NOT_GRANTED')
        self.assertFalse(self.claim()['start_allowed'])

    def test_authentication_timestamp_identity_and_signature_fail_closed(self):
        raw = f.canonical(dict(request_id=self.action['request_id'], session_id='s')).encode()
        for headers in ({}, self.headers(raw, 1), dict(self.headers(raw), **{'x-astra-signature':'0'*64})):
            with self.assertRaises(f.FlowError): self.app.claim_consumer(raw, headers)
        self.policy['astra_consumer']['identity'] = 'author'
        with self.assertRaises(f.FlowError): self.claim()

    def test_projection_verified_before_claim(self):
        self.ports.projected_pointer.side_effect = f.FlowError('edited projection')
        with self.assertRaises(f.FlowError): self.claim()

    def test_request_cannot_supply_its_own_actor(self):
        raw = f.canonical(dict(request_id=self.action['request_id'], session_id='s', identity='auditor')).encode()
        with self.assertRaises(f.FlowError): self.app.claim_consumer(raw, self.headers(raw))

    def test_wsgi_claim_is_synchronous_and_does_not_dispatch_builder(self):
        raw = f.canonical(dict(request_id=self.action['request_id'], session_id='s')).encode()
        env = {'REQUEST_METHOD':'POST', 'PATH_INFO':'/astra/claim', 'CONTENT_LENGTH':str(len(raw)),
               'wsgi.input':io.BytesIO(raw)}
        env.update({'HTTP_'+k.upper().replace('-','_'):v for k,v in self.headers(raw).items()})
        start = Mock(); result = f.decode(b''.join(self.app(env, start)))
        self.assertTrue(result['start_allowed']); self.app.executor.submit.assert_not_called()

    def test_consumer_disabled_by_default(self):
        self.policy.pop('astra_consumer')
        with self.assertRaises(f.FlowError): self.claim()

    def test_unmigrated_ledger_is_not_silently_modified_by_request(self):
        with self.store.transaction() as db: db.execute('DROP TABLE astra_claims')
        raw = f.canonical(dict(request_id=self.action['request_id'], session_id='s')).encode()
        env = {'REQUEST_METHOD':'POST', 'PATH_INFO':'/astra/claim', 'CONTENT_LENGTH':str(len(raw)),
               'wsgi.input':io.BytesIO(raw)}
        env.update({'HTTP_'+k.upper().replace('-','_'):v for k,v in self.headers(raw).items()})
        start=Mock(); self.app(env,start)
        self.assertEqual(start.call_args.args[0], '503 Service Unavailable')


if __name__ == '__main__': unittest.main()
