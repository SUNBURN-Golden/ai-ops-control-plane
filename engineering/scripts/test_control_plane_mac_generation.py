"""Isolated fixture generation contracts, not actual product/account evidence."""
import concurrent.futures
import copy
import hashlib
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


def decision_fixture():
    """Authenticated-comment shape fixture; not live host qualification."""
    return {'id': 6008874154, 'html_url': mac_generation.HOST_DECISION['url'],
            'issue_url': "https://api.github.com/repos/BeautifulMind-JT/ai-ops-control-plane/issues/77",
            'user': {'id': 263336091, 'login': "BeautifulMind-JT", 'type': 'User'},
            'body': "## 결정 기록 (JunTae Park, 2026-10-06 12:42 KST)\n\nA3 감사(https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/77#issuecomment-6008601206)의 F1·F2에 대한 소유자 결정이다.\n\n**D-2026-10-06-MAC-HOST: MacBook(`aiops-macbook` 러너)을 Linux 보호 호스트와 함께 정식 실행 호스트로 인정한다.**\n\n조건(재감사 기준):\n1. **F1 단일 원장:** 두 호스트는 같은 작업에 대해 하나의 승인(admission) 기록만 가진다. 맥 러너로 보내는 경로는 공유 승인 설계(어느 호스트가 작업을 잡았는지 한 곳에서 판정, 중복 승인 차단)를 갖춘 뒤에만 활성화한다. dispatcher가 넘긴 이름만 비교하는 러너 확인은 증거로 인정하지 않는다.\n2. **F2 단일 소유자:** 한 작업은 한 기록, 한 소유자다. MAC-* 작업이 원래 프로그램 노드를 넘겨받으려면 이 결정 기록을 참조해야 하고, Linux 호스트가 열린 이슈를 이미 소유 중이면 넘겨받기를 거절한다(명시적 이관 절차 없이 이중 소유 금지). 자유 텍스트 `decision` 필드만으로는 승인 근거가 될 수 없다.\n3. **F3:** PR 설명은 main 대비 실제 범위(맥 앱 전체, VM 도구, 워크플로 2개, README)와 정확한 CI·리뷰 증거 링크로 고친다.\n\n위 조건을 반영한 새 HEAD에서 A3를 다시 받는다.\n"}


class MacGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=core.Store(Path(self.temp.name)/'app'); self.addCleanup(self.store.close)
        self.source=mac_authority.LocalSource(self.store); self.worker=fixtures.FixtureWorker(self.store)
        self.controller=native_transfer.Controller(self.store,self.source,self.worker)
        self.latest=fixtures.snapshot(tasks=[{'number':108,'program':'KIX','node':'CONFORMANCE','state':'closed',
                            'declared_owners':['CURSOR'],'body_sha256':'c'*64}],
            nodes=[{'id':'CONFORMANCE','title':'Local conformance','spec':'Exact original technical spec; do not alter',
                    'audit_floor':'A2','astra_gate':'NONE','astra_auto_merge':True}])
        self.reader=patch('mac_authority.handoff.inspect_repository',side_effect=lambda repo:copy.deepcopy(self.latest))
        self.reader.start();self.addCleanup(self.reader.stop)
        self.decision_reader=patch('handoff.api',return_value=decision_fixture())
        self.decision_reader.start();self.addCleanup(self.decision_reader.stop)
        self.source.initialize({'mode':'MAC','decision':'Explicit fixture Mac mode, not legacy quiescence'})
    def value(self,index=1,**changes):
        return {'repository':'owner/kix','node':'CONFORMANCE','generation_id':f'{index:032x}',
                'decision':mac_generation.HOST_DECISION['url'],
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
        self.assertEqual(work['generation_decision'],mac_generation.HOST_DECISION['url'])
        self.assertEqual(work['generation_decision_evidence'],mac_generation.HOST_DECISION)
    def test_free_text_or_another_comment_is_not_durable_user_authority(self):
        before=list(self.store.db.iterdump())
        for pointer in ('User approves Mac','https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/77#issuecomment-1'):
            with self.assertRaisesRegex(common.AppError,'DURABLE_DECISION_REQUIRED'):
                self.source.generation(self.value(decision=pointer))
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
    def test_decision_requires_the_pinned_comment_actor_body_and_issue(self):
        changes=[{'id':1},{'html_url':'https://example.invalid/approval'},
                 {'issue_url':'https://api.github.com/repos/owner/kix/issues/77'},
                 {'user':{'id':1,'login':'BeautifulMind-JT','type':'User'}},
                 {'user':{'id':mac_generation.HOST_DECISION['actor_id'],'login':'another','type':'User'}},
                 {'body':decision_fixture()['body']+'\nEdited approval'}]
        before=list(self.store.db.iterdump())
        for change in changes:
            with patch('handoff.api',return_value={**decision_fixture(),**change}):
                with self.assertRaisesRegex(common.AppError,'DURABLE_DECISION_UNVERIFIED'):self.adopt()
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
    def test_open_linux_canonical_owner_is_not_acknowledged_as_an_adoption(self):
        before=list(self.store.db.iterdump());self.latest['tasks'][0]['state']='open'
        with self.assertRaisesRegex(common.AppError,'LINUX_OWNER_UNRESOLVED'):self.adopt()
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
    def test_ownerless_or_unkeyed_open_issue_also_fails_closed(self):
        self.latest['tasks'][0]['state']='open';self.latest['tasks'][0].pop('declared_owners')
        with self.assertRaisesRegex(common.AppError,'LINUX_OWNER_UNRESOLVED'):self.adopt()
        self.latest['tasks'][0].pop('node')
        with self.assertRaisesRegex(common.AppError,'LINUX_OWNER_UNRESOLVED'):self.adopt()
        self.assertEqual(self.source.tasks('owner/kix'),[])
    def test_new_linux_owner_before_start_blocks_without_a_native_reservation(self):
        bound=self.adopt();before=list(self.store.db.iterdump())
        self.latest['tasks'][0]['state']='open'
        with self.assertRaisesRegex(common.AppError,'LINUX_OWNER_UNRESOLVED'):
            self.controller.start(self.request(bound))
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
    def test_owner_appearing_after_checkout_preparation_blocks_claim_and_worker(self):
        bound=self.adopt();prepare=self.worker.prepare
        def changed(*args):
            attempt=prepare(*args);self.latest['tasks'][0]['state']='open';return attempt
        with patch.object(self.worker,'prepare',side_effect=changed):
            with self.assertRaisesRegex(common.AppError,'LINUX_OWNER_UNRESOLVED'):
                self.controller.start(self.request(bound))
        self.assertEqual(self.worker.launched,0)
    def test_edited_decision_after_adoption_blocks_start_without_repinning(self):
        bound=self.adopt();before=list(self.store.db.iterdump())
        with patch('handoff.api',return_value={**decision_fixture(),'body':'Changed decision'}):
            with self.assertRaisesRegex(common.AppError,'DURABLE_DECISION_UNVERIFIED'):
                self.controller.start(self.request(bound))
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
    def test_old_free_text_generation_cannot_gain_authority_on_update(self):
        bound=self.adopt();row=self.store.db.execute('SELECT document FROM mac_host_generations').fetchone()
        old=common.parse_json(row[0]);old.pop('decision_evidence')
        self.store.db.execute('UPDATE mac_host_generations SET document=?',(common.encoded(old),))
        before=list(self.store.db.iterdump())
        with self.assertRaisesRegex(common.AppError,'DURABLE_DECISION_REQUIRED'):
            self.controller.start(self.request(bound))
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
    def test_concurrent_different_generations_cannot_own_one_original_node(self):
        def adopt(index):
            try:self.adopt(index);return 'ADOPTED'
            except common.AppError as exc:return exc.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(adopt,[1,2]))
        self.assertEqual(results.count('ADOPTED'),1)
        self.assertIn('MAC_GENERATION_ORIGINAL_TASK_ALREADY_OWNED',results)
        self.assertEqual(len(self.source.tasks('owner/kix')),1);self.assertEqual(self.worker.launched,0)
    def test_register_after_generation_cannot_add_a_second_original_record(self):
        self.adopt();before=list(self.store.db.iterdump())
        with self.assertRaisesRegex(common.AppError,'ORIGINAL_TASK_ALREADY_OWNED'):
            self.source.register(self.latest)
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
    def test_concurrent_register_and_generation_admit_only_one_original_record(self):
        def admit(kind):
            try:
                self.adopt() if kind=='generation' else self.source.register(self.latest)
                return 'ADMITTED'
            except common.AppError as exc:return exc.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(admit,['generation','register']))
        self.assertEqual(results.count('ADMITTED'),1)
        self.assertEqual(sum('ORIGINAL_TASK_ALREADY_OWNED' in v for v in results),1)
        self.assertEqual(len(self.source.tasks('owner/kix')),1);self.assertEqual(self.worker.launched,0)
    def test_case_only_plan_change_cannot_duplicate_the_canonical_original(self):
        self.adopt()
        raw=common.parse_json(self.latest['source']['raw_program'])
        raw['program']=raw['program'].lower()
        for node in raw['nodes']:node['id']=node['id'].lower()
        data=common.encoded(raw);self.latest['source']['raw_program']=data
        self.latest['source']['blob']=hashlib.sha1(b'blob '+str(len(data.encode())).encode()+b'\0'+data.encode()).hexdigest()
        self.latest['source']['head']='d'*40;before=list(self.store.db.iterdump())
        with self.assertRaisesRegex(common.AppError,'ORIGINAL_TASK_ALREADY_OWNED'):
            self.source.generation(self.value(2,node='conformance'))
        with self.assertRaisesRegex(common.AppError,'ORIGINAL_TASK_ALREADY_OWNED'):
            self.source.register(self.latest)
        self.assertEqual(list(self.store.db.iterdump()),before);self.assertEqual(self.worker.launched,0)
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
        with self.assertRaisesRegex(common.AppError,'ORIGINAL_TASK_ALREADY_OWNED'):self.adopt()
        self.assertEqual(self.worker.launched,0)
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
        first=self.adopt()
        with self.assertRaisesRegex(common.AppError,'ORIGINAL_TASK_ALREADY_OWNED'):self.adopt(2)
        def start(item):
            try:return self.controller.start(self.request(item[1],item[0]))['state']
            except common.AppError:return 'FENCED'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(start,[(21,first),(22,first)]))
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
    def inspected_delivery(self,*,accept=True):
        job,_=self.blocked_delivery();repos=delivery.FixtureRepositories(self.store.directory/'workspaces')
        engine=core.Engine(self.store,repos,self.controller)
        with patch.object(engine,'notify'):
            self.store.action(job['id'],'resume',{'answer':'Fixture-only recovery'})
            for role in ('builder','reviewer','supervisor'):
                current=self.store.get(job['id'])
                if role=='supervisor':
                    engine.step(current);engine.step(self.store.get(job['id']))
                    current=self.store.get(job['id'])
                with patch('core.agents.command',return_value=['fixture-not-executed']),patch('core.subprocess.Popen'):
                    engine.launch(current,role)
                current=self.store.get(job['id']);attempt=current['attempt']
                result=delivery.report(current['head']);result['covered_tasks']=['CONFORMANCE']
                delivery.private_outcome(self.store.directory/'jobs'/job['id']/attempt['id'],attempt,result,
                    profile=current['settings']['roles'][role],sid='fixture-inspected-'+role)
                engine.observe(current)
            engine.step(self.store.get(job['id']))
            app=object.__new__(aiops.Application);app.store=self.store;app.engine=engine;app.canonical=self.controller
            if accept:app.action(job['id'],'accept',{})
        return self.store.get(job['id'])
    def ready_git_result(self,args,**kwargs):
        if args[-2:]==['rev-parse','HEAD']:return subprocess.CompletedProcess(args,0,'c'*40+'\n','')
        if args[-2:]==['status','--porcelain']:return subprocess.CompletedProcess(args,0,'','')
        raise AssertionError('Unexpected updater command')
    def test_ready_generation_update_proves_reviews_and_quiescence_without_accepting(self):
        job=self.inspected_delivery(accept=False);before=list(self.store.db.iterdump())
        self.assertEqual(job['state'],'ready')
        with patch('install.subprocess.run',side_effect=self.ready_git_result):install.idle_database(self.store.directory)
        self.assertEqual(list(self.store.db.iterdump()),before)
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'DELIVERING')
        self.assertEqual(self.store.get(job['id'])['state'],'ready')
    def test_ready_label_without_original_private_review_or_clean_head_cannot_enable_update(self):
        job=self.inspected_delivery(accept=False)
        with patch('install.subprocess.run',return_value=subprocess.CompletedProcess([],0,'e'*40,'')):
            with self.assertRaises(common.AppError):install.idle_database(self.store.directory)
        folder=self.store.directory/'jobs'/job['id']/job['review']['attempt']
        (folder/'receipt.json').unlink()
        with patch('install.subprocess.run',side_effect=self.ready_git_result):
            with self.assertRaises(common.AppError):install.idle_database(self.store.directory)
    def test_update_of_inspected_generation_requires_all_private_proof_and_keeps_inspection(self):
        job=self.inspected_delivery();before=list(self.store.db.iterdump())
        install.idle_database(self.store.directory)
        self.assertEqual(list(self.store.db.iterdump()),before)
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'INSPECTED')
        self.assertEqual(self.store.get(job['id'])['accepted_head'],job['head'])
    def test_inspected_label_without_matching_recorded_inspection_cannot_enable_update(self):
        job=self.inspected_delivery();self.store.update(job['id'],accepted_head='e'*40)
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
            with patch('gitops.git'),patch('gitops.gh',side_effect=[common.encoded({'nameWithOwner':'owner/kix','isPrivate':True}),common.encoded(prior)]) as gh:
                with self.assertRaisesRegex(common.AppError,'PUBLICATION_POLICY_REQUIRED'):gitops.Repositories.publish(repos,job)
                self.assertTrue(all('merge' not in c.args and '--auto' not in c.args for c in gh.call_args_list))
    def test_new_pr_uses_draft_without_legacy_marker_or_automatic_merge(self):
        repos,job=self.publication_job()
        with patch('gitops.git'),patch('gitops.gh',side_effect=[common.encoded({'nameWithOwner':'owner/kix','isPrivate':True}),'[]','https://github.com/owner/kix/pull/2']) as gh:
            self.assertEqual(gitops.Repositories.publish(repos,job),'https://github.com/owner/kix/pull/2')
            self.assertIn('--draft',gh.call_args_list[-1].args)
            self.assertTrue(all('merge' not in c.args and '--auto' not in c.args for c in gh.call_args_list))
        body=(self.store.directory/'jobs'/job['id']/'pr-body.md').read_text()
        self.assertIn(job['native_lineage']['binding']['generation_id'],body)
        self.assertNotIn('ASTRA_TASK_KEY_V1',body);self.assertNotIn('TASK ENVELOPE v4',body)
    def test_candidate_publication_requires_exact_private_target_before_push(self):
        repos,job=self.publication_job()
        for metadata in ({'nameWithOwner':'owner/kix','isPrivate':False},
                         {'nameWithOwner':'different/repo','isPrivate':True},[]):
            with patch('gitops.git') as git,patch('gitops.gh',return_value=common.encoded(metadata)):
                with self.assertRaises(common.AppError) as caught:gitops.Repositories.publish(repos,job)
                self.assertEqual(caught.exception.code,'MAC_HOST_CANDIDATE_PUBLICATION_SCOPE_REQUIRED')
                git.assert_not_called()
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
