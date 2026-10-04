"""Candidate contract tests in temporary ledgers; no account, model, VM or install."""
import concurrent.futures
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import common
import core
import native_transfer as mac
import control_plane_host as host

REPO = 'owner/kix'


class FixtureWorker:
    """No subprocess; these counters are synthetic, never real product sessions."""
    def __init__(self):
        self.prepared = self.launched = 0
        self.terminal = None
        self.interrupt = False

    def prepare(self, binding, work, request, attempt):
        self.prepared += 1
        value = {'id': attempt, 'head': binding['plan_commit'], 'profile': work['profile']}
        value['binding'] = common.digest({'request_id': request, 'canonical': binding, **value})
        return value

    def launch(self, record):
        self.launched += 1
        if self.interrupt: raise OSError('fixture interrupted launch')

    def receipt(self, record):
        if self.terminal is None: return None
        return {'attempt_id': record['attempt']['id'], 'binding': record['attempt']['binding'],
                'exit_code': 1, 'report': None, 'error': 'FIXTURE_PRESTART_FAILURE',
                'provider_started': False, 'process_group_quiescent': True, **self.terminal}


class FixtureSource:
    # Injection exists only inside these tests. configured_source() always
    # returns UnsupportedSource; fixture replies cannot enter the production API.
    production_qualified = True
    source_host, target_host = 'legacy-source', 'mac-target'

    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = []
        self.lost = None
        self.raw = False
        self.wrong_channel = False
        self.changed_scope = False

    def call(self, operation, payload):
        self.calls.append(operation)
        value = self.ledger.read(payload) if operation == 'read' else self.ledger.reserve(payload) if operation == 'reserve' else self.ledger.transition(operation, payload)
        if self.lost == operation:
            self.lost = None
            raise TimeoutError('fixture lost reply after durable source commit')
        if self.raw: return value
        if self.changed_scope and operation == 'read': value['work'] = {'invented': 'scope'}
        return mac.SourceReply('other-source' if self.wrong_channel else self.source_host, self.target_host, value)


class NativeTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = core.Store(root / 'mac'); self.addCleanup(self.store.close)
        self.now = 1000
        self.ledger = host.Ledger(root / 'source.sqlite', clock=lambda: self.now); self.ledger.initialize()
        self.work = {'task_id': 'KIX-P-SDK-0', 'task_revision': 'r1', 'plan_commit': 'a'*40,
                     'profile': {'provider':'devin', 'model':''},
                     'task': {'instructions':'frozen approved SDK scope', 'depends_on': []},
                     'original_plan': {'node':'p-sdk-0', 'approved_scope':'unchanged'}}
        self.binding = {'repository': REPO, 'task_id': self.work['task_id'], 'task_revision':'r1', 'issue':94,
                        'program':'kix', 'node':'p-sdk-0', 'materialization_request_id':'',
                        'plan_commit':'a'*40, 'plan_blob':'b'*40, 'dependencies':[], 'owner_lane':'DEVIN',
                        'source_host':'legacy-source', 'target_host':'mac-target', 'work_sha256':host.native_digest(self.work)}
        self.policy = {'control_uid':1010, 'runner_uid':1020,
                       'builder_uids':{'DEVIN':1030,'GROK_BUILD':1040,'GLM':1050},
                       'allowed_repositories':[REPO], 'enabled_builders':['DEVIN','GLM'],
                       'max_active_sessions':2, 'max_launches_per_24h':None, 'ledger_path':str(self.ledger.path),
                       'wrapper_paths':{k:host.WRAPPERS[k] for k in ('DEVIN','GROK_BUILD','GLM')},
                       'boundary_evidence_pointer':'https://github.com/owner/ops/issues/1'}
        materialized = self.ledger.materialize_begin('kix','p-sdk-0',REPO,'a'*40,self.policy)
        self.ledger.materialize_finish('kix','p-sdk-0',materialized['request'],'CREATED',94)
        self.binding['materialization_request_id'] = materialized['request']
        self.policy['native_transfer'] = {'qualified':True, 'source_host':'legacy-source',
            'authorization':'https://github.com/owner/ops/issues/2', 'targets':{'mac-target':1060},
            'contracts':[{'binding':self.binding, 'work':self.work, 'gate_evidence':'https://github.com/owner/ops/issues/3'}]}
        self.native = host.NativeTransferLedger(self.ledger,self.policy,quiescence=lambda lane,policy:[])
        self.native.initialize()
        self.source, self.worker = FixtureSource(self.native), FixtureWorker()
        self.mac = mac.Controller(self.store,self.source,self.worker)

    def request(self, index=1): return {'request_id':f'{index:032x}', 'binding':copy.deepcopy(self.binding)}

    def packet(self, task=None, lane='DEVIN', request='legacy-request'):
        return {'schema_version':1, 'repository':REPO, 'task_id':task or self.binding['task_id'],
                'task_revision':'r1', 'builder_id':lane, 'launch_request_id':request, 'attempt_id':1}

    def payload(self, index=1):
        value = self.request(index)
        value['attempt'] = self.worker.prepare(self.binding,self.work,value['request_id'],f'{index+100:032x}')
        return value

    def test_production_factory_and_unqualified_policy_never_authorize_a_fixture(self):
        controller = mac.Controller(self.store,worker=self.worker)
        with self.assertRaisesRegex(common.AppError,'NATIVE_SOURCE_UNSUPPORTED'): controller.start(self.request())
        self.assertIsNone(controller.get(self.request()['request_id'])); self.assertEqual(self.worker.prepared,0)
        self.assertFalse(controller.capability()['supported'])
        self.policy['native_transfer']['qualified'] = False
        with self.assertRaises(host.HostError): self.native.read(self.binding)

    def test_bound_start_terminal_and_replay_never_launch_twice(self):
        value = self.mac.start(self.request()); self.assertEqual(value['state'],'RUNNING')
        self.assertEqual(self.worker.launched,1)
        self.assertEqual(self.mac.start(self.request()),value)
        self.assertEqual(self.source.calls,['read','reserve','claim'])
        self.worker.terminal = {}; self.mac.tick()
        ended = self.mac.get(value['request_id']); self.assertEqual(ended['state'],'TERMINAL')
        self.assertEqual(ended['receipt']['state'],'TERMINAL')
        self.assertEqual(self.mac.start(self.request()),ended); self.assertEqual(self.worker.launched,1)
        admitted,result = self.ledger.reserve(self.packet(),self.policy)
        self.assertFalse(admitted); self.assertIn('source cannot reclaim',result['reason'])
        second = self.mac.start(self.request(2)); self.assertEqual(second['binding'],value['binding'])
        self.assertEqual(self.worker.launched,2) # two sequential fixture attempts, no real sessions

    def test_lost_reserve_keeps_both_fences_without_expiry_or_polling(self):
        self.source.lost = 'reserve'
        with self.assertRaises(TimeoutError): self.mac.start(self.request())
        record = self.mac.get(self.request()['request_id']); self.assertEqual(record['state'],'UNKNOWN')
        before = list(self.source.calls)
        self.assertEqual(self.mac.start(self.request())['state'],'UNKNOWN'); self.mac.tick()
        self.assertEqual(self.source.calls,before)
        with self.assertRaises(common.AppError): self.mac.start(self.request(2))
        with self.assertRaises(common.AppError): self.store.create({'repository':'owner/other','request_id':'ordinary-request','goal':'new'})
        self.now += 86400*100
        with self.assertRaises(host.HostError): self.native.reserve(self.payload(2))

    def test_lost_claim_does_not_create_a_worker(self):
        self.source.lost='claim'
        with self.assertRaises(TimeoutError): self.mac.start(self.request())
        self.assertEqual(self.worker.launched,0); self.assertEqual(self.mac.get(self.request()['request_id'])['state'],'UNKNOWN')
        row = self.native.transition('status',{'request_id':self.request()['request_id'],'attempt':self.mac.get(self.request()['request_id'])['attempt']})
        self.assertEqual(row['state'],'CLAIMED')

    def test_interrupted_spawn_and_restart_cannot_replay_admission(self):
        self.worker.interrupt=True
        with self.assertRaises(OSError): self.mac.start(self.request())
        renewed=mac.Controller(self.store,self.source,self.worker)
        self.assertEqual(renewed.start(self.request())['state'],'UNKNOWN'); renewed.tick()
        self.assertEqual(self.worker.launched,1)

    def test_terminal_quiescence_and_binding_mismatch_never_release_source(self):
        record=self.mac.start(self.request()); self.worker.terminal={'process_group_quiescent':False}
        self.mac.tick(); self.assertEqual(self.mac.get(record['request_id'])['state'],'UNKNOWN')
        with self.assertRaises(common.AppError): self.mac.reconcile(record['request_id'])
        view=self.native.transition('status',{'request_id':record['request_id'],'attempt':record['attempt']})
        self.assertEqual(view['state'],'CLAIMED')

    def test_lost_terminal_reply_recovers_by_read_without_resend_or_new_worker(self):
        record=self.mac.start(self.request()); self.worker.terminal={}; self.source.lost='finish'
        self.mac.tick(); self.assertEqual(self.mac.get(record['request_id'])['state'],'UNKNOWN')
        final=self.mac.reconcile(record['request_id']); self.assertEqual(final['state'],'TERMINAL')
        self.assertEqual(self.source.calls,['read','reserve','claim','finish','status']); self.assertEqual(self.worker.launched,1)
        with self.assertRaises(host.HostError): self.native.transition('claim',{'request_id':record['request_id'],'attempt':record['attempt']})
        changed=copy.deepcopy(final['terminal']); changed['result_sha256']='f'*64
        with self.assertRaises(host.HostError): self.native.transition('finish',{'request_id':record['request_id'],'attempt':record['attempt'],'terminal':changed})

    def test_untrusted_body_and_wrong_authenticated_channel_fail_before_worker_prepare(self):
        self.source.raw=True
        with self.assertRaises(common.AppError): self.mac.start(self.request())
        self.assertEqual(self.worker.prepared,0); self.assertEqual(self.worker.launched,0)

    def test_wrong_channel_fail_before_worker_prepare(self):
        self.source.wrong_channel=True
        with self.assertRaises(common.AppError): self.mac.start(self.request())
        self.assertEqual(self.worker.prepared,0)

    def test_unpinned_work_and_revision_or_plan_changes_fail_closed(self):
        for field,value in (('task_revision','r2'),('plan_commit','c'*40),('owner_lane','GLM'),('target_host','other-mac')):
            other=copy.deepcopy(self.binding); other[field]=value
            with self.assertRaises(host.HostError): self.native.read(other)
        self.source.changed_scope=True
        with self.assertRaises(common.AppError): self.mac.start(self.request())
        self.assertEqual(self.worker.prepared,0)

    def test_source_unknown_and_wrong_terminal_session_are_not_transfer_evidence(self):
        self.ledger.reserve(self.packet(),self.policy)
        with self.assertRaises(host.HostError): self.native.read(self.binding)
        with self.assertRaises(host.HostError): self.native.reserve(self.payload())
        self.assertEqual(self.ledger.status('legacy-request')['state'],'SUBMITTING')

    def test_first_owner_and_materialized_identity_survive_terminal_history(self):
        value=self.packet(lane='GLM')
        self.ledger.reserve(value,self.policy)
        self.ledger.finalize(value,host.result_for(value,'CONFIRMED',session_id='fixture-session'))
        self.ledger.reconcile(value['launch_request_id'],'fixture-session','https://github.com/owner/ops/issues/4')
        with self.assertRaises(host.HostError): self.native.read(self.binding)

    def test_concurrent_source_duplicate_is_one_durable_reservation(self):
        payload=self.payload()
        def reserve(_):
            try: return self.native.reserve(copy.deepcopy(payload))
            except host.HostError as exc:
                self.assertIn('in flight',str(exc)); return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=[r for r in pool.map(reserve,range(12)) if r is not None]
        self.assertTrue(results); self.assertTrue(all(r==results[0] for r in results))
        with self.ledger.connect() as db: self.assertEqual(len(host.native_rows(db)),1)
        admitted,result=self.ledger.reserve(self.packet(task='OTHER'),self.policy)
        self.assertFalse(admitted); self.assertIn('native transfer',result['reason'])
        self.assertEqual(self.ledger.lanes(self.policy)['active_total'],1)

    def test_parallel_mac_replays_do_not_mark_the_active_admission_interrupted(self):
        original=self.worker.prepare
        seen=[]
        def prepare(*args):
            self.mac.tick()
            seen.append(self.mac.start(self.request())['state'])
            return original(*args)
        self.worker.prepare=prepare
        self.assertEqual(self.mac.start(self.request())['state'],'RUNNING')
        self.assertEqual(seen,['PREPARING']); self.assertEqual(self.worker.launched,1)

    def test_interrupted_claim_is_not_auto_completed_after_mac_restart(self):
        value=self.mac.start(self.request())
        self.mac.save(value,'STARTING')
        renewed=mac.Controller(self.store,self.source,self.worker); before=list(self.source.calls)
        renewed.tick()
        self.assertEqual(renewed.get(value['request_id'])['state'],'UNKNOWN')
        self.assertEqual(before,self.source.calls); self.assertEqual(self.worker.launched,1)

    def test_late_bound_quiescent_receipt_after_restart_can_record_termination(self):
        value=self.mac.start(self.request()); self.mac.save(value,'STARTING',worker_started=None)
        renewed=mac.Controller(self.store,self.source,self.worker); renewed.tick()
        self.assertEqual(renewed.get(value['request_id'])['state'],'UNKNOWN')
        self.worker.terminal={}; renewed.tick()
        self.assertEqual(renewed.get(value['request_id'])['state'],'TERMINAL')
        self.assertEqual(self.worker.launched,1)

    def test_malformed_terminal_binding_and_finish_before_claim_are_rejected(self):
        payload=self.payload(); self.native.reserve(payload)
        terminal={'attempt_id':payload['attempt']['id'],'binding':payload['attempt']['binding'],'head':payload['attempt']['head'],
                  'exit_code':0,'process_group_quiescent':True,'result_sha256':'e'*64,'session_id':'fixture-session'}
        with self.assertRaises(host.HostError): self.native.transition('finish',{'request_id':payload['request_id'],'attempt':payload['attempt'],'terminal':terminal})
        self.native.transition('claim',{'request_id':payload['request_id'],'attempt':payload['attempt']})
        terminal['binding']='f'*64
        with self.assertRaises(host.HostError): self.native.transition('finish',{'request_id':payload['request_id'],'attempt':payload['attempt'],'terminal':terminal})

    def test_snapshot_change_and_source_quiescence_fail_before_worker_start(self):
        self.native.quiescence=lambda lane,policy:[123]
        with self.assertRaises(host.HostError): self.mac.start(self.request())
        self.assertEqual(self.worker.launched,0)
        with self.ledger.connect() as db: self.assertEqual(len(host.native_rows(db)),0)

    def test_transferred_plan_cannot_silently_advance_on_source(self):
        self.native.reserve(self.payload())
        with self.assertRaises(host.HostError): self.ledger.materialize_plan('kix','p-sdk-0','a'*40,'d'*40)
        self.assertEqual(self.ledger.materialize_status('kix','p-sdk-0')['plan_commit'],'a'*40)

    def test_unverified_dependency_and_profile_never_grant_admission(self):
        self.binding['dependencies']=[{'task_id':'DEP','task_revision':'r1','launch_request_id':'d'*24,
            'head_sha':'d'*40,'gate_evidence':'https://github.com/owner/ops/issues/5'}]
        with self.assertRaises(host.HostError): self.native.read(self.binding)
        self.binding['dependencies']=[]
        payload=self.payload(); payload['attempt']['profile']={'provider':'glm','model':''}
        with self.assertRaises(host.HostError): self.native.reserve(payload)

    def test_ordinary_queued_work_and_existing_transport_unknown_block_native(self):
        self.store.create({'repository':'owner/other','goal':'existing','request_id':'existing-request'})
        with self.assertRaisesRegex(common.AppError,'NATIVE_LOCAL_WORK_BUSY'): self.mac.start(self.request())
        self.assertEqual(self.source.calls,[]); self.assertEqual(self.worker.launched,0)

    def test_binding_replay_conflict_is_not_a_new_attempt(self):
        self.mac.start(self.request()); other=self.request(); other['binding']['task_revision']='r2'
        with self.assertRaises(common.AppError): self.mac.start(other)
        self.assertEqual(self.worker.launched,1)

    def test_native_fixed_entry_requires_registered_target_caller(self):
        with patch.object(host,'load_host_policy',return_value=self.policy), patch.dict(os.environ,{'SUDO_UID':'1020'}), \
                patch.object(sys,'stdin',io.StringIO(json.dumps(self.binding))), patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(host.main(['native-transfer-read']),2)
        with patch.object(host,'load_host_policy',return_value=self.policy), patch.dict(os.environ,{'SUDO_UID':'1060'}), \
                patch.object(sys,'stdin',io.StringIO(json.dumps(self.binding))), patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(host.main(['native-transfer-read']),0)


if __name__=='__main__': unittest.main()
