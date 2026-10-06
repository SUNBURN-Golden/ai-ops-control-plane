"""Temporary completion/HTTP fixtures only; never live product qualification."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac_app'))
import common
import core
import gitops
import mac_generation
import test_control_plane_mac_authority as authority
import test_control_plane_mac_generation as generation
import test_control_plane_mac_pipeline as delivery

VERIFY_REMOTE=gitops.Repositories.dependency_completion


class DependencyRepositories(delivery.FixtureRepositories):
    def hosted_checks(self,job,head,*,post_merge=False):
        return {'state':self.ci_state,'head':head,'source':'GITHUB_ACTIONS_API',
                'checks':[{'name':name,'status':'SUCCESS','head':head,'run_id':run,
                           'url':f'https://github.com/owner/kix/actions/runs/{run}/job/{run*10}',
                           'event':'workflow_dispatch','workflow_name':name+' verification'}
                          for name,run in [('protocol',11),('kernel',22)]]}
    def dependency_completion(self,job,plan_commit,*,current=True):
        return VERIFY_REMOTE(self,job,plan_commit,current=current)


class AcceptedDependencyTests(unittest.TestCase):
    value=generation.MacGenerationTests.value
    adopt=generation.MacGenerationTests.adopt
    request=generation.MacGenerationTests.request
    blocked_delivery=generation.MacGenerationTests.blocked_delivery
    inspected_delivery=generation.MacGenerationTests.inspected_delivery

    def setUp(self):
        generation.MacGenerationTests.setUp(self)
        nodes=common.parse_json(self.latest['source']['raw_program'])['nodes']
        nodes.append({'id':'NEXT','title':'Dependent document','spec':'Preserve the exact original dependent spec',
                      'audit_floor':'A2','astra_gate':'NONE','depends_on':['CONFORMANCE']})
        if self._testMethodName=='test_missing_second_dependency_leaves_no_partial_generation':
            nodes.append({'id':'MISSING','title':'Missing','spec':'No completion',
                          'audit_floor':'A2','astra_gate':'NONE','depends_on':[]})
            nodes[1]['depends_on'].append('MISSING')
        self.latest=authority.snapshot(tasks=self.latest['tasks'],nodes=nodes)
        self.repo_patch=patch.object(delivery,'FixtureRepositories',DependencyRepositories)
        self.repo_patch.start();self.addCleanup(self.repo_patch.stop)
        self.job=self.inspected_delivery()
        self.repos=DependencyRepositories(self.store.directory/'workspaces')
        self.engine=core.Engine(self.store,self.repos,self.controller)
        self.bound=self.job['native_lineage']['binding']
        self.engine.pipeline.reconcile_accepted('owner/kix',self.bound['task_id'])
        self.latest['source']['head']='d'*40
        self.api_values={}
        for name,run in [('protocol',11),('kernel',22)]:
            self.api_values[f'repos/owner/kix/actions/runs/{run}']={
                'id':run,'head_sha':'c'*40,'repository':{'full_name':'owner/kix'},
                'status':'completed','conclusion':'success','event':'workflow_dispatch',
                'name':name+' verification','run_attempt':1}
            self.api_values[f'repos/owner/kix/actions/jobs/{run*10}']={
                'id':run*10,'run_id':run,'head_sha':'c'*40,'name':name,'status':'completed','conclusion':'success',
                'steps':[{'number':1,'name':'Fixture required verification','status':'completed','conclusion':'success'}]}
        self.git_values={('merge-base','d'*40,'d'*40):'d'*40,('rev-parse','d'*40+'^1'):'a'*40,
                         ('rev-parse','d'*40+'^{tree}'):'e'*40,('rev-parse','c'*40+'^{tree}'):'e'*40}
        self.api_patch=patch('handoff.api',side_effect=lambda target:copy.deepcopy(self.api_values[target]))
        self.api_patch.start();self.addCleanup(self.api_patch.stop)
        self.git_patch=patch('gitops.git',side_effect=lambda checkout,*args:self.git_values[args])
        self.git_patch.start();self.addCleanup(self.git_patch.stop)
        self.factory=patch('gitops.Repositories',side_effect=lambda directory:self.repos)
        self.factory.start();self.addCleanup(self.factory.stop)

    def child(self,**changes):return self.source.generation(self.value(2,node='NEXT',**changes))
    def unchanged_failure(self):
        before=list(self.store.db.iterdump());launched=self.worker.launched
        with self.assertRaises(common.AppError):self.child()
        self.assertEqual(list(self.store.db.iterdump()),before)
        self.assertEqual(self.worker.launched,launched)

    def test_real_fixture_completion_opens_child_keeps_exact_graph_and_all_old_records(self):
        before={name:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+name)]
                for name in ('jobs','events','mac_host_deliveries','mac_host_attempts')}
        parent=self.source.tasks('owner/kix')[0]
        bound=self.child();self.assertEqual(bound['dependencies'],['CONFORMANCE'])
        view=self.source.call('read',bound).document
        self.assertEqual(view['work']['task']['depends_on'],['CONFORMANCE'])
        self.assertEqual(view['dependencies'][0]['task_id'],self.bound['task_id'])
        self.assertEqual(view['dependencies'][0]['state'],'ACCEPTED')
        self.assertEqual(view['work']['dependency_evidence'],view['dependencies'])
        for name,rows in before.items():
            self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM '+name)],rows)
        self.assertEqual(self.source.tasks('owner/kix')[0],parent)
        self.assertEqual(self.controller.start(self.request(bound,30))['state'],'RUNNING')

    def test_replay_is_read_only_and_does_not_refresh_or_repin_frozen_evidence(self):
        bound=self.child();before=list(self.store.db.iterdump())
        with patch('handoff.inspect_repository',side_effect=AssertionError('Replay cannot read a new source')):
            self.assertEqual(self.child(),bound)
        self.assertEqual(list(self.store.db.iterdump()),before)
        with self.assertRaisesRegex(common.AppError,'IMMUTABLE'):self.child(decision='Changed approval')

    def test_only_normal_accepted_state_satisfies_dependency(self):
        for state in ['READY','DELIVERING','INSPECTED','REWORK_REQUIRED','UNKNOWN']:
            self.store.db.execute('UPDATE mac_host_tasks SET state=?',(state,))
            self.unchanged_failure()
        self.store.db.execute("UPDATE mac_host_tasks SET state='ACCEPTED'")
        self.store.db.execute("UPDATE mac_host_deliveries SET state='INSPECTED'")
        self.unchanged_failure()

    def test_missing_private_native_or_review_receipt_is_not_a_completion(self):
        lineage=self.job['native_lineage']
        folders=[self.store.directory/'native'/lineage['request_id']/lineage['attempt_id'],
                 self.store.directory/'jobs'/self.job['id']/self.job['review']['attempt']]
        for folder in folders:
            path=folder/'receipt.json';saved=path.read_bytes();path.unlink()
            self.unchanged_failure();path.write_bytes(saved);path.chmod(0o600)

    def test_forged_inspection_or_native_origin_cannot_be_consumed(self):
        row=self.store.db.execute('SELECT request,document FROM mac_host_deliveries').fetchone()
        original=common.parse_json(row['document'])
        for mutate in [lambda d:d['inspection'].update(ci_sha256='f'*64),
                       lambda d:d.update(terminal_sha256='f'*64),
                       lambda d:d['completion'].update(source='MODEL_PASS')]:
            changed=copy.deepcopy(original);mutate(changed)
            self.store.db.execute('UPDATE mac_host_deliveries SET document=?',(common.encoded(changed),))
            self.unchanged_failure()

    def test_different_original_revision_or_plan_blob_is_not_inherited(self):
        row=self.store.db.execute('SELECT work FROM mac_host_tasks').fetchone()
        work=common.parse_json(row[0]);work['original_task']['task_revision']='f'*64
        self.store.db.execute('UPDATE mac_host_tasks SET work=?',(common.encoded(work),))
        self.unchanged_failure()

    def test_different_original_plan_is_not_inherited(self):
        plan=common.parse_json(self.latest['source']['raw_program'])
        plan['nodes'][0]['spec']='Different original revision'
        self.latest=authority.snapshot(nodes=plan['nodes']);self.latest['source']['head']='d'*40
        self.unchanged_failure()

    def test_missing_second_dependency_leaves_no_partial_generation(self):
        self.assertEqual(self.latest['source']['blob'],self.bound['plan_blob'])
        self.assertEqual(self.repos.dependency_completion(self.job,'d'*40)['reviewed_head'],self.job['head'])
        self.unchanged_failure()

    def test_completed_merge_with_wrong_tree_or_ancestry_is_rejected(self):
        for key in [('merge-base','d'*40,'d'*40),('rev-parse','d'*40+'^{tree}'),('rev-parse','d'*40+'^1')]:
            saved=self.git_values[key];self.git_values[key]='f'*40
            self.unchanged_failure();self.git_values[key]=saved

    def test_stale_target_head_is_rejected_before_ownership(self):
        self.latest['source']['head']='f'*40
        self.unchanged_failure()

    def test_wrong_head_workflow_job_or_partial_CI_is_rejected(self):
        run='repos/owner/kix/actions/runs/11';job='repos/owner/kix/actions/jobs/110'
        for target,key,value in [(run,'head_sha','f'*40),(run,'name','Unrelated workflow'),
                                 (run,'status','in_progress'),(job,'head_sha','f'*40),
                                 (job,'id',999),(job,'run_id',99),(job,'conclusion','failure'),(job,'steps',[])]:
            saved=self.api_values[target][key];self.api_values[target][key]=value
            self.unchanged_failure();self.api_values[target][key]=saved
        self.repos.merge_ready=False;self.unchanged_failure()

    def test_completion_changed_during_live_read_is_rejected_before_any_new_record(self):
        before={table:self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                for table in ['mac_host_generations','mac_host_tasks','mac_host_attempts','jobs']}
        original=self.repos.dependency_completion
        def concurrent_change(*args,**kwargs):
            proof=original(*args,**kwargs)
            self.store.update(self.job['id'],head='f'*40)
            return proof
        with patch.object(self.repos,'dependency_completion',side_effect=concurrent_change):
            with self.assertRaisesRegex(common.AppError,'DEPENDENCY_COMPLETION_CHANGED'):self.child()
        self.assertEqual(before,{table:self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                                for table in before})
        self.assertEqual(self.worker.launched,1)

    def test_changed_parent_after_admission_blocks_start_without_second_writer(self):
        bound=self.child();launched=self.worker.launched
        self.store.update(self.job['id'],head='f'*40)
        with self.assertRaisesRegex(common.AppError,'DEPENDENCY_COMPLETION_CHANGED'):
            self.controller.start(self.request(bound,31))
        self.assertEqual(self.worker.launched,launched)
        self.assertIsNone(self.controller.get(f'{31:032x}')['attempt'])

    def test_forged_frozen_child_proof_and_A3_cannot_bypass_normal_gates(self):
        bound=self.child();row=self.store.db.execute('SELECT work FROM mac_host_tasks WHERE task=?',(bound['task_id'],)).fetchone()
        work=common.parse_json(row[0]);work['dependency_evidence'][0]['state']='MODEL_PASS'
        self.store.db.execute('UPDATE mac_host_tasks SET work=? WHERE task=?',(common.encoded(work),bound['task_id']))
        with self.assertRaisesRegex(common.AppError,'BINDING_INVALID'):self.source.call('read',bound)
        plan=common.parse_json(self.latest['source']['raw_program']);plan['nodes'][1]['audit_floor']='A3'
        self.latest=authority.snapshot(nodes=plan['nodes']);self.latest['source']['head']='d'*40
        with self.assertRaisesRegex(common.AppError,'ASTRA_GATE_REQUIRED'):self.child(generation_id=f'{3:032x}')


if __name__=='__main__':unittest.main()
