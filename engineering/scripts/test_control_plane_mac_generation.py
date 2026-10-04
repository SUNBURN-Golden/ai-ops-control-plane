"""Isolated fixture generation contracts, not actual product/account evidence."""
import concurrent.futures
import copy
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'mac_app'))
import aiops
import agents
import common
import core
import gitops
import install
import mac_authority
import mac_generation
import mac_sandbox
import native_transfer
import transport_guard
import test_control_plane_mac_authority as fixtures
import test_control_plane_mac_pipeline as delivery


class MacGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=core.Store(Path(self.temp.name)/'app'); self.addCleanup(self.store.close)
        self.source=mac_authority.LocalSource(self.store); self.worker=fixtures.FixtureWorker(self.store)
        self.controller=native_transfer.Controller(self.store,self.source,self.worker)
        self.latest=fixtures.snapshot(tasks=[{'number':108,'program':'KIX','node':'CONFORMANCE','state':'open',
                            'declared_owners':['CURSOR'],'body_sha256':'c'*64}],
            nodes=[{'id':'CONFORMANCE','title':'Local conformance','spec':'Exact original technical spec; do not alter',
                    'audit_floor':'A2','astra_gate':'NONE','astra_auto_merge':True}])
        self.reader=patch('mac_authority.handoff.inspect_repository',side_effect=lambda repo:copy.deepcopy(self.latest))
        self.reader.start();self.addCleanup(self.reader.stop)
        self.source.initialize({'mode':'MAC','decision':'Explicit fixture Mac mode, not legacy quiescence'})
    def value(self,index=1,**changes):
        return {'repository':'owner/kix','node':'CONFORMANCE','generation_id':f'{index:032x}',
                'decision':'User approved a distinct isolated Mac generation with draft-only publication, not legacy terminal',
                'plan_commit':self.latest['source']['head'],'plan_blob':self.latest['source']['blob'],**changes}
    def adopt(self,index=1): return self.source.generation(self.value(index))
    def request(self,bound,index=20): return {'binding':bound,'request_id':f'{index:032x}'}
    def journal(self): return fixtures.MacAuthorityTests.journal(self)
    def test_explicit_owner_request_required_and_injected_scope_or_policy_rejected(self):
        app=object.__new__(aiops.Application);app.canonical=self.controller
        for changes in ({'snapshot':self.latest},{'auto_merge':True},{'decision':''},{'generation_id':'old-task'}):
            with self.assertRaises(common.AppError):app.host_action('generation',self.value(**changes))
        self.assertEqual(self.source.tasks('owner/kix'),[])
    def test_new_identity_revision_exact_spec_and_original_auto_merge_only_provenance(self):
        bound=self.adopt();view=self.source.call('read',bound).document;work=view['work']
        self.assertNotEqual(bound['task_id'],'KIX-CONFORMANCE')
        self.assertNotEqual(bound['task_revision'],work['original_task']['task_revision'])
        self.assertEqual(work['task'],common.parse_json(self.latest['source']['raw_program'])['nodes'][0])
        self.assertTrue(work['task']['astra_auto_merge']);self.assertEqual(work['generation_policy'],mac_generation.POLICY)
        self.assertFalse(work['generation_policy']['auto_merge']);self.assertTrue(work['original_task']['provenance_only'])
        self.assertEqual(native_transfer.binding(bound),bound)
        self.assertFalse(self.source.meta()['legacy_terminal_verified'])
    def test_generation_replay_is_read_only_and_cannot_repin_or_change_decision(self):
        value=self.value();bound=self.source.generation(value)
        before=[tuple(r) for r in self.store.db.execute('SELECT * FROM mac_host_generations')]
        self.latest['source']['head']='d'*40
        self.assertEqual(self.source.generation(value),bound)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM mac_host_generations')],before)
        with self.assertRaisesRegex(common.AppError,'IMMUTABLE'):self.source.generation(dict(value,decision='changed'))
        with self.assertRaisesRegex(common.AppError,'PROGRAM_REVISION_CHANGED'):self.controller.start(self.request(bound))
    def test_stale_plan_blob_or_head_cannot_create_a_generation(self):
        for name in ('plan_commit','plan_blob'):
            with self.assertRaisesRegex(common.AppError,'PROGRAM_REVISION_CHANGED'):
                self.source.generation(self.value(**{name:'f'*40}))
        self.assertEqual(self.source.tasks('owner/kix'),[])
    def test_a3_and_architecture_gates_still_block_adoption(self):
        for changes in ({'audit_floor':'A3'},{'astra_gate':'ARCHITECTURE'}):
            self.latest=fixtures.snapshot(nodes=[{'id':'CONFORMANCE','title':'Gated','spec':'Unchanged architecture scope',**changes}])
            with self.assertRaisesRegex(common.AppError,'ASTRA_GATE_REQUIRED'):self.adopt()
        self.assertEqual(self.worker.launched,0)
    def test_dependency_is_not_inherited_from_a_legacy_done_declaration(self):
        self.latest=fixtures.snapshot(tasks=[{'number':94,'program':'KIX','node':'P-SDK-0','state':'closed'}])
        value=self.value(node='NEXT')
        with self.assertRaisesRegex(common.AppError,'DEPENDENCY_GATE_REQUIRED'):self.source.generation(value)
    def test_old_unknown_jobs_events_settings_and_external_claims_are_preserved(self):
        job=self.store.create({'repository':'owner/old','request_id':'old-event','goal':'old scope'})
        self.store.update(job['id'],state='cancelled')
        self.source.register(self.latest);old=self.source.select('owner/kix','KIX-CONFORMANCE')
        journal=self.journal();before=journal.read_bytes();jobs=self.store.jobs()
        events=[tuple(r) for r in self.store.db.execute('SELECT * FROM events')];settings=self.store.settings()
        claims=[tuple(r) for r in self.store.db.execute('SELECT * FROM mac_host_external')]
        bound=self.adopt();record=self.controller.start(self.request(bound))
        self.assertEqual(record['state'],'RUNNING');self.assertEqual(self.worker.launched,1)
        self.assertEqual(journal.read_bytes(),before);self.assertEqual(self.store.jobs(),jobs)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM events')],events)
        self.assertEqual(self.store.settings(),settings)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM mac_host_external')],claims)
        self.assertEqual(transport_guard.unresolved(self.store.directory)[0]['state'],'UNKNOWN')
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_EXECUTION_UNRESOLVED'):self.source.check_external(old)
    def test_unacknowledged_new_receipt_blocks_even_a_previously_adopted_generation(self):
        journal=self.journal();bound=self.adopt()
        with sqlite3.connect(journal) as db:
            receipt={'request_id':'new-external','payload_sha256':'d'*64,'state':'UNKNOWN','task_completion':'NOT_CHECKED'}
            db.execute('INSERT INTO requests VALUES (?,?,?,?)',('new-external','d'*64,'UNKNOWN',common.encoded(receipt)))
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_RECEIPT_UNRESOLVED'):self.controller.start(self.request(bound))
        self.assertEqual(self.worker.launched,0)
    def test_changed_external_scope_is_append_only_and_blocks_start(self):
        bound=self.adopt();self.latest['tasks'][0]['body_sha256']='d'*64
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_CHANGE_UNRESOLVED'):self.controller.start(self.request(bound))
        self.latest['tasks'][0]['body_sha256']='c'*64
        with self.assertRaisesRegex(common.AppError,'EXTERNAL_CHANGE_UNRESOLVED'):self.controller.start(self.request(bound))
        self.assertEqual(self.worker.launched,0)
    def test_old_task_revision_or_generation_cannot_be_substituted(self):
        bound=self.adopt()
        for changes in ({'task_id':'KIX-CONFORMANCE'},{'task_revision':'e'*64},{'generation_id':'2'*32}):
            value=self.request({**bound,**changes})
            with self.assertRaises(common.AppError):self.controller.start(value)
        self.assertEqual(self.worker.launched,0)
    def test_two_requests_and_generations_keep_one_global_writer(self):
        first=self.adopt();second=self.adopt(2)
        def start(item):
            try:return self.controller.start(self.request(item[1],item[0]))['state']
            except common.AppError:return 'FENCED'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(start,[(21,first),(22,second)]))
        self.assertEqual(results.count('RUNNING'),1);self.assertEqual(self.worker.launched,1)
        with self.assertRaisesRegex(common.AppError,'WORK_BUSY'):self.adopt(3)
    def test_same_start_after_unknown_launch_does_not_start_another_writer(self):
        bound=self.adopt();self.worker.unknown=True;value=self.request(bound)
        with self.assertRaises(OSError):self.controller.start(value)
        self.assertEqual(self.controller.start(value)['state'],'UNKNOWN');self.assertEqual(self.worker.launched,1)
    def test_immutable_policy_tampering_is_rejected(self):
        bound=self.adopt();row=self.store.db.execute('SELECT document FROM mac_host_generations').fetchone()
        document=common.parse_json(row[0]);document['policy']['auto_merge']=True
        self.store.db.execute('UPDATE mac_host_generations SET document=?',(common.encoded(document),))
        with self.assertRaisesRegex(common.AppError,'BINDING_INVALID'):self.source.call('read',bound)
    def test_terminal_finish_replay_does_not_reset_delivery_or_inspection_state(self):
        bound=self.adopt();value=self.request(bound);record=self.controller.start(value);self.worker.finish(record)
        self.controller.tick();ended=self.controller.get(value['request_id'])
        row=self.store.db.execute('SELECT terminal FROM mac_host_attempts').fetchone();terminal=common.parse_json(row[0])
        for state in ('DELIVERING','INSPECTED','ACCEPTED'):
            self.store.db.execute('UPDATE mac_host_tasks SET state=?',(state,))
            reply=self.source.call('finish',{'request_id':value['request_id'],'attempt':record['attempt'],'terminal':terminal})
            self.assertEqual(reply.document['state'],'TERMINAL');self.assertEqual(self.source.tasks('owner/kix')[0]['state'],state)
        self.assertEqual(self.controller.start(value),ended);self.assertEqual(self.worker.launched,1)
    def test_delivery_uses_new_lineage_same_frozen_node_and_draft_policy(self):
        bound=self.adopt();record=self.controller.start(self.request(bound));folder=self.store.directory/'native'/record['request_id']/record['attempt']['id']
        common.private_directory(folder/'checkout')
        self.worker.result=delivery.private_outcome(folder,record['attempt'],delivery.report(),sid='fixture-generation-builder')
        self.controller.tick();repos=delivery.FixtureRepositories(self.store.directory/'workspaces')
        engine=core.Engine(self.store,repos,self.controller)
        self.assertTrue(engine.pipeline.deliver());job=self.store.jobs()[0]
        self.assertEqual(job['native_lineage']['binding'],bound);self.assertEqual(job['plan']['tasks'][0]['id'],'CONFORMANCE')
        self.assertEqual(job['generation_policy'],mac_generation.POLICY);self.assertEqual(job['phase'],'reviewing')
        self.assertEqual(job['branch'],'aiops/native-'+record['request_id'][:16])
        engine.pipeline.assert_job(job,refresh=True)
        job['generation_policy']['auto_merge']=True
        with self.assertRaisesRegex(common.AppError,'PUBLICATION_POLICY_REQUIRED'):engine.pipeline.assert_job(job,refresh=False)
        self.assertIn('new isolated Mac generation',agents.prompt(self.store.jobs()[0],'reviewer',job['head']))
    def blocked_delivery(self):
        bound=self.adopt();record=self.controller.start(self.request(bound))
        folder=self.store.directory/'native'/record['request_id']/record['attempt']['id']
        checkout=common.private_directory(folder/'checkout')
        common.atomic_json(folder/'request.json',{'attempt_id':record['attempt']['id'],
            'binding':record['attempt']['binding'],'host_directory':str(self.store.directory.resolve()),'checkout':str(checkout.resolve())})
        self.worker.result=delivery.private_outcome(folder,record['attempt'],delivery.report(status='needs_user'))
        self.controller.tick();engine=core.Engine(self.store,delivery.FixtureRepositories(self.store.directory/'workspaces'),self.controller)
        self.assertTrue(engine.pipeline.deliver())
        return self.store.jobs()[0],folder
    def test_update_preserves_quiescent_needs_user_generation_and_failure_without_state_mutation(self):
        job,_=self.blocked_delivery();before=list(self.store.db.iterdump())
        with patch('core.Store',side_effect=AssertionError('A read-only precheck cannot open a Store')):
            install.idle_database(self.store.directory)
        self.assertEqual(list(self.store.db.iterdump()),before)
        self.assertEqual(self.store.get(job['id'])['state'],'needs_user')
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'DELIVERING')
        self.store.update(job['id'],state='paused');install.idle_database(self.store.directory)
        self.assertEqual(self.store.get(job['id'])['state'],'paused')
    def test_quiescent_update_rejects_lost_or_changed_private_proof_and_immutable_binding(self):
        job,folder=self.blocked_delivery()
        path=folder/'receipt.json';original=path.read_bytes();path.unlink()
        with self.assertRaisesRegex(common.AppError,'작업을 완료'):install.idle_database(self.store.directory)
        path.write_bytes(original);path.chmod(0o600)
        value=common.read_json(path);value['process_group_quiescent']=False;common.atomic_json(path,value)
        with self.assertRaises(common.AppError):install.idle_database(self.store.directory)
        path.write_bytes(original);path.chmod(0o600)
        self.store.update(job['id'],generation_policy={**mac_generation.POLICY,'auto_merge':True})
        with self.assertRaises(common.AppError):install.idle_database(self.store.directory)
    def test_quiescent_update_cannot_bypass_an_extra_attempt_or_actual_live_child(self):
        job,folder=self.blocked_delivery()
        extra=common.private_directory(self.store.directory/'jobs'/job['id']/('9'*32))
        with self.assertRaises(common.AppError):install.idle_database(self.store.directory)
        extra.rmdir()
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.5)'],start_new_session=True)
        try:
            common.atomic_json(folder/'running.json',{'pid':child.pid})
            with self.assertRaises(common.AppError):install.idle_database(self.store.directory)
        finally:child.wait(timeout=5)
        install.idle_database(self.store.directory)
    def test_completed_owned_native_wrapper_is_reaped_even_without_pending_work(self):
        job,_=self.blocked_delivery()
        worker=native_transfer.NativeWorker(self.store.directory,self.store.settings)
        child=subprocess.Popen([sys.executable,'-c','pass'],start_new_session=True)
        self.addCleanup(child.wait,timeout=5)
        worker.children['owned-fixture']=child;self.controller.worker=worker
        engine=core.Engine(self.store,delivery.FixtureRepositories(self.store.directory/'workspaces'),self.controller)
        with patch.object(engine,'keep_awake'):
            deadline=time.monotonic()+5
            while child.returncode is None and time.monotonic()<deadline:
                engine.tick();time.sleep(.01)
        self.assertEqual(child.returncode,0)
        self.assertEqual(self.store.get(job['id'])['calls'],1)
        self.assertEqual(self.store.get(job['id'])['state'],'needs_user')
        install.idle_database(self.store.directory)
    def test_publication_rejects_policy_tamper_before_any_git_or_github_write(self):
        repos=gitops.Repositories(self.store.directory/'workspaces')
        job={'native_lineage':{'binding':{'generation_id':'1'*32}},'generation_policy':{**mac_generation.POLICY,'auto_merge':True}}
        with patch('gitops.git') as git,patch('gitops.gh') as gh:
            with self.assertRaisesRegex(common.AppError,'PUBLICATION_POLICY_REQUIRED'):repos.publish(job)
            git.assert_not_called();gh.assert_not_called()
    def publication_job(self):
        bound=self.adopt();repos=delivery.FixtureRepositories(self.store.directory/'workspaces')
        job=self.store.new_document('8'*16,'publication-fixture','owner/kix','Exact source task')
        job.update(native_lineage={'binding':bound,'request_id':'6'*32,'attempt_id':'7'*32},
            generation_policy=copy.deepcopy(mac_generation.POLICY),head=repos.current,base_sha='a'*40,
            base_branch='main',branch='aiops/native-'+('6'*16),plan={'tasks':[]})
        common.private_directory(self.store.directory/'jobs'/job['id'])
        return repos,job
    def test_existing_ready_or_auto_merge_pr_is_rejected_without_a_merge_command(self):
        repos,job=self.publication_job()
        for draft,auto in ((False,None),(True,{'enabledBy':{'login':'fixture'}})):
            prior=[{'url':'https://github.com/owner/kix/pull/2','state':'OPEN','headRefOid':job['head'],
                    'isDraft':draft,'autoMergeRequest':auto}]
            with patch('gitops.git'),patch('gitops.gh',return_value=common.encoded(prior)) as gh:
                with self.assertRaisesRegex(common.AppError,'PUBLICATION_POLICY_REQUIRED'):gitops.Repositories.publish(repos,job)
                self.assertTrue(all('merge' not in c.args and '--auto' not in c.args for c in gh.call_args_list))
    def test_new_pr_uses_draft_without_legacy_marker_or_automatic_merge(self):
        repos,job=self.publication_job()
        with patch('gitops.git'),patch('gitops.gh',side_effect=['[]','https://github.com/owner/kix/pull/2']) as gh:
            self.assertEqual(gitops.Repositories.publish(repos,job),'https://github.com/owner/kix/pull/2')
            self.assertIn('--draft',gh.call_args_list[1].args)
            self.assertTrue(all('merge' not in c.args and '--auto' not in c.args for c in gh.call_args_list))
        body=(self.store.directory/'jobs'/job['id']/'pr-body.md').read_text()
        self.assertIn(job['native_lineage']['binding']['generation_id'],body)
        self.assertNotIn('ASTRA_TASK_KEY_V1',body);self.assertNotIn('TASK ENVELOPE v4',body)
    def test_isolated_native_checkout_never_reuses_legacy_branch_or_folder(self):
        bound=self.adopt();work=self.source.call('read',bound).document['work']
        native=native_transfer.NativeWorker(self.store.directory,self.store.settings);calls=[]
        def git(checkout,*args,**kwargs):
            calls.append((checkout,args))
            if args[0]=='clone':Path(args[-1]).mkdir()
            if args[0]=='symbolic-ref':return 'origin/main'
            if args[0]=='rev-parse':return bound['plan_blob'] if ':' in args[1] else bound['plan_commit']
            return ''
        with patch('native_transfer.git',side_effect=git),patch('gitops.git',side_effect=git),patch('native_transfer.agents.command',return_value=['fixture']):
            native.prepare(bound,work,'3'*32,'4'*32)
        self.assertTrue(any(args[:2]==('checkout','-b') and args[2]=='aiops/native-'+('3'*16) for _,args in calls))
        self.assertTrue(all('astra/' not in str(args) for _,args in calls))
        self.assertIn('provenance only',common.read_json(self.store.directory/'native'/('3'*32)/('4'*32)/'request.json')['prompt'])
    @unittest.skipUnless(sys.platform=='darwin','actual Mac old scope isolation')
    def test_real_seatbelt_rejects_writes_to_old_scope_and_authority_but_allows_new_checkout(self):
        state=self.store.directory;folder=common.private_directory(state/'native'/'new'/'attempt');checkout=common.private_directory(folder/'checkout')
        old=common.private_directory(state/'workspaces'/'legacy');oldfile=old/'old.py';oldfile.write_text('preserve')
        script='''from pathlib import Path
import sys
old,checkout=map(Path,sys.argv[1:])
try:old.write_text('modified legacy')
except PermissionError:pass
else:raise SystemExit(2)
(checkout/'new.py').write_text('fixture new scope only')
'''
        argv=mac_sandbox.command([sys.executable,'-c',script,str(oldfile),str(checkout)],state,folder,checkout)
        result=subprocess.run(argv,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode()[-1000:]);self.assertEqual(oldfile.read_text(),'preserve')
        self.assertEqual((checkout/'new.py').read_text(),'fixture new scope only')


if __name__=='__main__':unittest.main()
