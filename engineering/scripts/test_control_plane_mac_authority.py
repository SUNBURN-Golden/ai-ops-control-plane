"""Mac-authoritative contracts: temporary ledgers, no account/model/VM/install."""
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'mac_app'))
import aiops
import common
import core
import mac_authority
import mac_sandbox
import native_transfer
import transport_guard
import worker


def snapshot(repo='owner/kix', tasks=None, nodes=None):
    program={'schema_version':1,'program':'KIX','repository':repo,
             'approval_pointer':'https://github.com/owner/kix/issues/1',
             'authoritative_doc_pointers':'docs/spec.md',
             'nodes':nodes or [{'id':'P-SDK-0','title':'SDK','spec':'Original complete SDK scope'},
                               {'id':'NEXT','title':'Next','spec':'Original dependent scope','depends_on':['P-SDK-0']}]}
    raw=common.encoded(program); data=raw.encode()
    return {'repository':repo,'task_scope':'all','tasks':tasks or [],'observed_at':1,
            'source':{'head':'a'*40,'branch':'main','blob':hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest(),
                      'raw_program':raw}}


class FixtureWorker:
    """A prestart fixture, never an actual provider or positive model review."""
    def __init__(self,store): self.store=store; self.launched=0; self.result=None; self.unknown=False
    def prepare(self,bound,work,rid,aid):
        attempt={'id':aid,'head':bound['plan_commit'],'profile':work['profile']}
        attempt['binding']=common.digest({'request_id':rid,'canonical':bound,**attempt})
        common.private_directory(common.private_directory(common.private_directory(self.store.directory / 'native') / rid) / aid)
        return attempt
    def launch(self,record):
        self.launched+=1
        if self.unknown: raise OSError('fixture ambiguous launch')
    def stop(self,record): self.stop_requested=True
    def receipt(self,record): return self.result
    def finish(self,record):
        self.result={'attempt_id':record['attempt']['id'],'binding':record['attempt']['binding'],
                     'provider_started':False,'process_group_quiescent':True,'exit_code':None,
                     'report':None,'error':'FIXTURE_LOGIN_REQUIRED'}
        folder=self.store.directory / 'native' / record['request_id'] / record['attempt']['id']
        common.atomic_json(folder / 'receipt.json',self.result)


class MacAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=core.Store(Path(self.temp.name) / 'app'); self.addCleanup(self.store.close)
        self.source=mac_authority.LocalSource(self.store)
        self.worker=FixtureWorker(self.store)
        self.controller=native_transfer.Controller(self.store,self.source,self.worker)
        self.latest=snapshot()
        self.observer=patch('mac_authority.handoff.inspect_repository',side_effect=lambda repo:copy.deepcopy(self.latest))
        self.observer.start(); self.addCleanup(self.observer.stop)
    def configure(self):
        self.source.initialize({'mode':'MAC','decision':'Explicit user fixture decision, not production qualification'})
        return self.source.register(self.latest)
    def request(self,index=1,task='KIX-P-SDK-0'):
        return {'request_id':f'{index:032x}','binding':self.source.select('owner/kix',task)}
    def journal(self):
        folder=common.private_directory(self.store.directory / 'relay'); path=folder / 'requests.sqlite3'
        fd=os.open(path,os.O_CREAT|os.O_WRONLY,0o600); os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE requests(id TEXT,digest TEXT,state TEXT,receipt TEXT)')
            receipt={'request_id':'legacy-request','payload_sha256':'b'*64,'state':'UNKNOWN','task_completion':'NOT_CHECKED'}
            db.execute('INSERT INTO requests VALUES (?,?,?,?)',('legacy-request','b'*64,'UNKNOWN',common.encoded(receipt)))
        return path
    def association(self,repo='owner/kix',task='KIX-P-SDK-0'):
        return {'request_id':'legacy-request','payload_sha256':'b'*64,'repository':repo,
                'task_id':task,'decision':'Explicit fixture association only; termination not verified'}
    def test_factory_remains_uninitialized_until_explicit_owner_mode(self):
        self.assertFalse(self.controller.capability()['supported'])
        with self.assertRaisesRegex(common.AppError,'EXPLICIT_MODE'):
            self.source.initialize({'mode':'VM','decision':'wrong host'})
        with self.assertRaisesRegex(common.AppError,'NOT_INITIALIZED'): self.source.register(self.latest)
        self.assertEqual(self.worker.launched,0)
    def test_initialization_replay_preserves_host_id_and_all_old_job_documents(self):
        job=self.store.create({'repository':'owner/old','request_id':'old-request','goal':'old goal'})
        self.store.update(job['id'],state='cancelled'); before=self.store.jobs()
        self.configure(); first=self.source.meta()
        self.assertEqual(self.source.initialize({'mode':'MAC','decision':'second request'}),first)
        self.assertEqual(self.store.jobs(),before)
    def test_busy_ordinary_job_blocks_mode_initialization(self):
        self.store.create({'repository':'owner/old','request_id':'old-request','goal':'old goal'})
        with self.assertRaisesRegex(common.AppError,'LOCAL_WORK_BUSY'): self.configure()
        self.assertIsNone(self.source.meta())
    def test_fresh_task_reserves_on_mac_without_protected_transport(self):
        self.configure()
        with patch('transport_guard.require_clear',side_effect=AssertionError('VM/old global gate must not be used')):
            record=self.controller.start(self.request())
        self.assertEqual(record['state'],'RUNNING'); self.assertEqual(self.worker.launched,1)
        self.assertEqual(record['binding']['authority_kind'],'MAC_LOCAL')
        self.assertTrue(record['binding']['canonical_task_pointer'].startswith('mac-host:'))
        self.assertFalse(self.source.status()['automatic_execution_enabled'])
    def test_same_request_replay_is_read_only_after_ambiguous_launch(self):
        self.configure(); self.worker.unknown=True
        with self.assertRaises(OSError): self.controller.start(self.request())
        before=self.worker.launched
        self.assertEqual(self.controller.start(self.request())['state'],'UNKNOWN')
        self.assertEqual(self.worker.launched,before)
        with self.assertRaises(common.AppError): self.controller.start(self.request(2))
    def test_two_concurrent_new_requests_admit_one_worker(self):
        self.configure()
        def start(index):
            try: return self.controller.start(self.request(index))['state']
            except common.AppError: return 'FENCED'
        with concurrent.futures.ThreadPoolExecutor(2) as pool: result=list(pool.map(start,(1,2)))
        self.assertEqual(result.count('RUNNING'),1); self.assertEqual(self.worker.launched,1)
    def test_legacy_canonical_issue_blocks_only_its_original_task(self):
        self.latest['tasks']=[{'number':94,'program':'KIX','node':'NEXT','materialization_request_id':'c'*24,
                               'declared_owners':['CURSOR'],'state':'closed'}]
        self.configure()
        self.source.check_external(self.source.select('owner/kix','KIX-P-SDK-0'))
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_EXECUTION_UNRESOLVED'):
            self.source.check_external(self.source.select('owner/kix','KIX-NEXT'))
    def test_unbound_unknown_receipt_requires_exact_origin_scope_and_is_preserved(self):
        self.configure(); journal=self.journal(); before=journal.read_bytes()
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_SCOPE_UNRESOLVED'): self.controller.start(self.request())
        association=self.association('owner/other','OTHER-TASK')
        self.source.annotate_receipt(association)
        record=self.controller.start(self.request()); self.assertEqual(record['state'],'RUNNING')
        self.assertEqual(journal.read_bytes(),before)
        self.assertEqual(transport_guard.unresolved(self.store.directory)[0]['state'],'UNKNOWN')
        changed=dict(association,task_id='KIX-P-SDK-0')
        with self.assertRaisesRegex(common.AppError,'SCOPE_IMMUTABLE'): self.source.annotate_receipt(changed)
    def test_overlapping_unknown_cannot_be_released_by_scope_annotation(self):
        self.configure(); self.journal(); self.source.annotate_receipt(self.association())
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_EXECUTION_UNRESOLVED'): self.controller.start(self.request())
        self.assertEqual(self.worker.launched,0)
    def test_wrong_receipt_digest_never_creates_association(self):
        self.configure(); self.journal()
        with self.assertRaisesRegex(common.AppError,'ORIGIN_UNVERIFIED'):
            self.source.annotate_receipt(dict(self.association(),payload_sha256='c'*64))
    def test_changed_pinned_plan_cannot_advance_task_or_start(self):
        self.configure(); self.latest['source']['head']='b'*40
        with self.assertRaisesRegex(common.AppError,'PROGRAM_REVISION_CHANGED'): self.controller.start(self.request())
        self.assertEqual(self.worker.launched,0)
    def test_new_external_projection_after_registration_blocks_start(self):
        self.configure(); self.latest['tasks']=[{'number':94,'program':'KIX','node':'P-SDK-0','state':'closed'}]
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_EXECUTION_UNRESOLVED'): self.controller.start(self.request())
        self.latest['tasks']=[]
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_EXECUTION_UNRESOLVED'): self.controller.start(self.request())
    def test_dependency_and_original_astra_gate_remain_required(self):
        self.configure()
        with self.assertRaisesRegex(common.AppError,'DEPENDENCY_GATE_REQUIRED'): self.controller.start(self.request(task='KIX-NEXT'))
        self.assertEqual(self.worker.launched,0)
        gate=snapshot(repo='owner/gated',nodes=[{'id':'A3','title':'Architecture','spec':'original gate','audit_floor':'A3'}])
        bound=self.source.register(gate)[0]['binding']
        with self.assertRaisesRegex(common.AppError,'ASTRA_GATE_REQUIRED'): self.source.call('read',bound)
    def test_terminal_fixture_releases_only_current_mac_attempt_not_old_unknowns(self):
        self.configure(); record=self.controller.start(self.request()); self.worker.finish(record)
        self.controller.tick(); ended=self.controller.get(record['request_id'])
        self.assertEqual(ended['state'],'TERMINAL'); self.assertIsNone(self.controller.pending())
        self.assertEqual(ended['error'],'FIXTURE_LOGIN_REQUIRED')
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'READY')
        self.assertEqual(self.controller.start(self.request()),ended)
        self.assertEqual(self.worker.launched,1)
    def test_forged_or_missing_private_terminal_receipt_holds_reservation(self):
        self.configure(); record=self.controller.start(self.request()); self.worker.finish(record)
        path=self.store.directory / 'native' / record['request_id'] / record['attempt']['id'] / 'receipt.json'
        path.unlink(); self.controller.tick()
        self.assertEqual(self.controller.get(record['request_id'])['state'],'UNKNOWN')
        self.assertEqual(self.source.status()['active'][0]['state'],'CLAIMED')
    def test_mode_and_registration_do_not_assert_reviews_ci_merge_or_legacy_terminal(self):
        self.configure(); meta=self.source.meta()
        self.assertFalse(meta['legacy_terminal_verified'])
        self.assertEqual({task['state'] for task in self.source.tasks('owner/kix')},{'READY'})
        self.assertNotIn('accept',dir(self.source)); self.assertEqual(self.worker.launched,0)
    def test_program_blob_mismatch_is_rejected_before_any_registration(self):
        self.source.initialize({'mode':'MAC','decision':'fixture'})
        self.latest['source']['blob']='f'*40
        with self.assertRaisesRegex(common.AppError,'PLAN_BLOB_MISMATCH'): self.source.register(self.latest)
        self.assertEqual(self.source.tasks('owner/kix'),[])
    def test_http_owner_action_reads_original_github_snapshot_not_injected_plan(self):
        app=object.__new__(aiops.Application); app.canonical=self.controller
        self.source.initialize({'mode':'MAC','decision':'fixture'})
        self.assertEqual(len(app.host_action('register',{'repository':'owner/kix'})),2)
        with self.assertRaisesRegex(common.AppError,'REQUEST_INVALID'):
            app.host_action('register',{'repository':'owner/kix','snapshot':self.latest})
    def test_stop_request_cannot_clear_a_running_or_unknown_reservation(self):
        self.configure(); record=self.controller.start(self.request())
        stopped=self.controller.stop(record['request_id'])
        self.assertTrue(stopped['stop_requested']); self.assertEqual(stopped['state'],'RUNNING')
        self.assertEqual(self.source.status()['active'][0]['state'],'CLAIMED')
        with self.assertRaises(common.AppError): self.controller.start(self.request(2))
    def test_old_task_ids_or_event_ids_cannot_change_a_canonical_binding(self):
        self.configure(); value=self.request()
        value['binding']['task_id']='KIX-NEXT'
        with self.assertRaisesRegex(common.AppError,'CANONICAL_BINDING_MISMATCH'): self.controller.start(value)
        self.assertEqual(self.worker.launched,0)


class MacSandboxTests(unittest.TestCase):
    def test_unsupported_platform_has_no_unconfined_fallback(self):
        with self.assertRaisesRegex(common.AppError,'SANDBOX_UNAVAILABLE'):
            mac_sandbox.command(['true'],'/tmp/control','/tmp/control/native/a','/tmp/control/native/a/checkout',platform='linux')
    @unittest.skipUnless(sys.platform=='darwin','actual Mac Seatbelt probe')
    def test_actual_provider_process_cannot_read_or_write_host_but_can_edit_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            state=common.private_directory(Path(root) / 'control')
            folder=common.private_directory(state / 'native' / 'request' / 'attempt')
            checkout=common.private_directory(folder / 'checkout')
            secret=state / 'desktop-token'; secret.write_text('FIXTURE_OWNER_TOKEN')
            receipt=folder / 'receipt.json'; receipt.write_text('FIXTURE_CONTROL_RECEIPT')
            (checkout / 'linked-token').symlink_to(secret)
            metadata=checkout / '.git'; metadata.mkdir(); (metadata / 'config').write_text('FIXTURE_GIT_CONFIG')
            script='''import pathlib,sys
secret,receipt,checkout=map(pathlib.Path,sys.argv[1:])
for p in (secret,receipt,checkout / "linked-token"):
    try: p.read_text()
    except PermissionError: pass
    else: raise SystemExit(2)
    try: p.write_text("forged")
    except PermissionError: pass
    else: raise SystemExit(3)
(checkout / "allowed.txt").write_text("checkout only")
try: (checkout / ".git/config").write_text("forged git authority")
except PermissionError: pass
else: raise SystemExit(4)
'''
            argv=mac_sandbox.command([sys.executable,'-c',script,str(secret),str(receipt),str(checkout)],state,folder,checkout)
            result=subprocess.run(argv,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr.decode()[-1000:])
            self.assertEqual(secret.read_text(),'FIXTURE_OWNER_TOKEN')
            self.assertEqual(receipt.read_text(),'FIXTURE_CONTROL_RECEIPT')
            self.assertEqual((checkout / 'allowed.txt').read_text(),'checkout only')
            self.assertEqual((metadata / 'config').read_text(),'FIXTURE_GIT_CONFIG')
    @unittest.skipUnless(sys.platform=='darwin','actual Mac read-only reviewer fence')
    def test_reviewer_can_read_but_cannot_edit_the_canonical_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            state=common.private_directory(Path(root) / 'control')
            folder=common.private_directory(state / 'jobs' / 'fixture' / 'review')
            checkout=common.private_directory(state / 'native' / 'fixture' / 'builder' / 'checkout')
            source=checkout / 'code.py'; source.write_text('original fixture source')
            script='''from pathlib import Path
import sys
p=Path(sys.argv[1]); assert p.read_text()=="original fixture source"
try: p.write_text("modified by reviewer")
except PermissionError: pass
else: raise SystemExit(2)
'''
            argv=mac_sandbox.command([sys.executable,'-c',script,str(source)],state,folder,checkout,writing=False)
            result=subprocess.run(argv,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr.decode()[-1000:])
            self.assertEqual(source.read_text(),'original fixture source')
    @unittest.skipUnless(sys.platform=='darwin','actual Mac native worker cancellation')
    def test_stop_is_handled_by_the_live_wrapper_and_persists_real_child_exit(self):
        with tempfile.TemporaryDirectory() as root:
            state=common.private_directory(Path(root) / 'control')
            folder=common.private_directory(state / 'native' / 'request' / 'attempt')
            checkout=common.private_directory(folder / 'checkout')
            request={'attempt_id':'fixture-attempt','binding':'fixture-binding','profile':{'provider':'grok_build','model':''},
                     'role':'builder','checkout':str(checkout),'prompt':'fixture','timeout_seconds':30,
                     'host_directory':str(state)}
            common.atomic_json(folder / 'request.json',request)
            argv=[sys.executable,'-c','import time; time.sleep(30)']
            with patch('worker.agents.command',return_value=argv), patch('worker.agents.prepare'):
                thread=threading.Thread(target=worker.run,args=(folder,)); thread.start()
                deadline=time.monotonic()+5
                while not (folder / 'provider-process.json').exists() and time.monotonic()<deadline: time.sleep(.05)
                self.assertTrue((folder / 'provider-process.json').exists())
                common.atomic_json(folder / 'stop-request.json',{'attempt_id':request['attempt_id'],'binding':request['binding']})
                thread.join(12)
                self.assertFalse(thread.is_alive())
            receipt=common.read_json(folder / 'receipt.json'); exited=common.read_json(folder / 'provider-exit.json')
            self.assertEqual(receipt['error'],'USER_STOPPED'); self.assertTrue(receipt['process_group_quiescent'])
            self.assertEqual(receipt['exit_code'],-15); self.assertEqual(exited['exit_code'],-15)
            self.assertTrue(receipt['provider_started']); self.assertIsNone(receipt['report'])


if __name__=='__main__': unittest.main()
