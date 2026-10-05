"""Temporary Mac task lineage/inspection/CI contracts; no real provider or GitHub."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'mac_app'))
import agents
import aiops
import common
import core
import gitops
import install
import mac_authority
import mac_pipeline
import native_transfer
import test_control_plane_mac_authority as fixtures


def report(head='',status='complete'):
    return {'status':status,'summary':'Synthetic fixture outcome, not live product validation.',
            'question':'Restore the assigned sandbox before continuing.' if status=='needs_user' else '',
            'plan':None,'findings':[] if status=='complete' else ['fixture rework required'],
            'checks':['fixture check'],'reviewed_head':head,'covered_tasks':['P-SDK-0']}


def private_outcome(folder,attempt,provider_report,*,profile=None,sid='fixture-builder',error=None):
    """A real short-lived Python child proves only fixture process termination."""
    child=subprocess.Popen([sys.executable,'-c','pass'],start_new_session=True)
    child.wait(timeout=10)
    profile=profile or {'provider':'codex','model':''}
    receipt={'attempt_id':attempt['id'],'binding':attempt['binding'],'provider_started':True,
             'process_group_quiescent':True,'exit_code':child.returncode,'report':provider_report,'error':error}
    if provider_report is not None:
        receipt['provider_evidence']={'provider':profile['provider'],'model_requested':profile['model'],
            'harness':agents.CATALOG[profile['provider']]['harness'],'session_id':sid}
    common.atomic_json(folder / 'provider-process.json',{'attempt_id':attempt['id'],'binding':attempt['binding'],
                       'pid':child.pid,'pgid':child.pid})
    common.atomic_json(folder / 'provider-exit.json',{'attempt_id':attempt['id'],'binding':attempt['binding'],
                       'pid':child.pid,'exit_code':child.returncode})
    common.atomic_json(folder / 'receipt.json',receipt)
    return receipt


class FixtureRepositories(gitops.Repositories):
    def __init__(self,directory):
        super().__init__(directory); self.current='c'*40; self.dirty=False; self.checkpoints=0; self.published=0
        self.fail_checkpoint=False; self.merge_ready=True; self.ci_state='passed'
        self.dispatches=[]
        self.remote_draft=True;self.remote_merged=False;self.readied=0;self.merge_count=0
    def checkpoint(self,job):
        self.checkpoints+=1
        if self.fail_checkpoint: raise common.AppError('FIXTURE_CHECKPOINT_FAILED')
        return self.current
    def head(self,job): return self.current
    def clean(self,job): return not self.dirty
    def assert_binding(self,job): pass
    def assert_scope(self,job): pass
    def prepare_native(self,job):
        return {'operation':'fixture host fetch','base_sha':job['base_sha'],'plan_blob':job['program_scope']['blob']}
    def synchronize_base(self,job): return {'changed':False}
    def publish(self,job): self.published+=1; return 'https://github.com/owner/kix/pull/2'
    def candidate_workflows(self,job): return ['ktx-kernel.yml','protocol.yml']
    def dispatch_candidate_workflow(self,job,workflow): self.dispatches.append((job['head'],workflow))
    def merge_candidate(self,job):return {'draft':self.remote_draft,'merged':self.remote_merged,'merge_head':'d'*40,'number':2}
    def user_ready(self,job):self.readied+=1;self.remote_draft=False
    def user_merge(self,job):self.merge_count+=1;self.remote_merged=True;return self.merge_candidate(job)
    def checks(self,job):
        return {'state':self.ci_state,'checks':[{'name':'fixture actual-API substitute','status':'SUCCESS'}],
                'head':job['head'],'source':'GITHUB_ACTIONS_API'}
    def merged(self,job):
        if not self.merge_ready: raise common.AppError('MAC_HOST_POST_MERGE_CI_REQUIRED')
        return {'reviewed_head':job['head'],'merge_head':'d'*40,'default_head':'d'*40,
                'post_merge_ci':{'state':'passed','head':'d'*40},'source':'AUTHENTICATED_GITHUB_READ','pr_url':job['pr_url']}


class MacPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=core.Store(Path(self.temp.name) / 'app'); self.addCleanup(self.store.close)
        self.source=mac_authority.LocalSource(self.store); self.worker=fixtures.FixtureWorker(self.store)
        self.controller=native_transfer.Controller(self.store,self.source,self.worker)
        self.latest=fixtures.snapshot()
        self.reader=patch('mac_authority.handoff.inspect_repository',side_effect=lambda repo:copy.deepcopy(self.latest))
        self.reader.start(); self.addCleanup(self.reader.stop)
        self.source.initialize({'mode':'MAC','decision':'fixture-only explicit owner mode'})
        self.source.register(self.latest)
        self.repos=FixtureRepositories(self.store.directory / 'workspaces')
        self.engine=core.Engine(self.store,self.repos,self.controller)
        notifier=patch.object(self.engine,'notify')
        notifier.start(); self.addCleanup(notifier.stop)
        self.app=object.__new__(aiops.Application); self.app.store=self.store; self.app.engine=self.engine; self.app.canonical=self.controller
    def end_builder(self,*,status='complete',error=None):
        request={'request_id':'1'*32,'binding':self.source.select('owner/kix','KIX-P-SDK-0')}
        record=self.controller.start(request)
        folder=self.store.directory / 'native' / record['request_id'] / record['attempt']['id']
        common.private_directory(folder / 'checkout')
        self.worker.result=private_outcome(folder,record['attempt'],report(status=status) if error is None else None,
                                           error=error,sid='fixture-builder')
        self.controller.tick(); self.assertEqual(self.controller.get(request['request_id'])['state'],'TERMINAL')
        return record
    def seed(self,**options):
        record=self.end_builder(**options); self.assertTrue(self.engine.pipeline.deliver())
        job=self.store.jobs()[0]; self.assertEqual(job['native_lineage']['request_id'],record['request_id'])
        return job
    def review_evidence(self,job,role,aid,sid):
        profile=job['settings']['roles'][role]
        binding=common.digest({'job':job['id'],'attempt':aid,'head':job['head'],'role':role,
                              'profile':profile,'plan':job['plan'],'task':None})
        attempt={'id':aid,'binding':binding}
        folder=common.private_directory(common.private_directory(self.store.directory / 'jobs' / job['id']) / aid)
        request={'attempt_id':aid,'binding':binding,'profile':profile,'role':role,
                 'host_directory':str(self.store.directory.resolve()),'checkout':str(self.repos.path(job).resolve())}
        common.atomic_json(folder / 'request.json',request)
        receipt=private_outcome(folder,attempt,report(job['head']),profile=profile,sid=sid)
        return {'head':job['head'],'attempt':aid,'profile':profile,'report':receipt['report'],
                'provider_evidence':receipt['provider_evidence']}
    def ready(self,job=None):
        job=job or self.seed()
        review=self.review_evidence(job,'reviewer','2'*32,'fixture-reviewer')
        supervision=self.review_evidence(job,'supervisor','3'*32,'fixture-supervisor')
        return self.store.update(job['id'],state='ready',phase='verifying',review=review,supervision=supervision,
                                 pr_url='https://github.com/owner/kix/pull/2',ci=self.repos.checks(job),
                                 candidate_published_head=job['head'],candidate_ci_requested=job['head'])
    def test_native_completion_seeds_one_original_node_and_checkout_without_planning(self):
        job=self.seed()
        self.assertEqual(job['phase'],'reviewing'); self.assertEqual(job['plan']['tasks'][0]['id'],'P-SDK-0')
        self.assertEqual(job['plan']['tasks'][0]['instructions'],'Original complete SDK scope')
        self.assertEqual(job['program_scope']['count'],2)
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'DELIVERING')
        self.assertFalse(self.engine.pipeline.deliver()); self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(self.worker.launched,1)
    def test_failure_reworks_the_same_owner_and_stop_remains_paused(self):
        job=self.seed(error='USER_STOPPED')
        self.assertEqual(job['state'],'paused'); self.assertTrue(job['pause_requested'])
        self.assertEqual(job['settings']['roles']['builder'],job['native_lineage']['binding'] and self.store.settings()['roles']['builder'])
        self.assertEqual(self.worker.launched,1)
    def test_ordinary_failure_uses_the_existing_same_owner_rework_loop(self):
        job=self.seed(status='fail')
        self.assertEqual(job['state'],'building'); self.assertTrue(job['correcting'])
        self.assertEqual(job['feedback'],['fixture rework required'])
    def test_native_needs_user_exit_zero_does_not_automatically_retry_or_lose_question(self):
        job=self.seed(status='needs_user')
        self.assertEqual(job['state'],'needs_user'); self.assertIsNone(job['attempt'])
        self.assertEqual(job['question'],report(status='needs_user')['question'])
        self.assertEqual(job['feedback'],report(status='needs_user')['findings'])
        self.assertEqual(job['built_tasks'],[]);self.assertEqual(job['task_index'],0)
        self.assertEqual(self.engine.describe(job)['health']['implementation_count'],0)
        with patch.object(self.engine,'launch') as launch,patch.object(self.engine,'keep_awake'):
            self.engine.tick(); self.engine.tick()
        launch.assert_not_called(); self.assertEqual(self.store.get(job['id'])['calls'],1)
        self.assertEqual(self.worker.launched,1); self.assertEqual(self.repos.published,0)
        with self.assertRaises(common.AppError):self.app.action(job['id'],'resume',{})
    def test_login_failure_waits_for_user_instead_of_starting_another_provider(self):
        job=self.seed(error='PROVIDER_LOGIN_REQUIRED')
        self.assertEqual(job['state'],'needs_user'); self.assertEqual(job['provider_error'],'PROVIDER_LOGIN_REQUIRED')
        self.assertEqual(self.worker.launched,1)
    def test_native_sdk_policy_protocol_permission_and_timeout_wait_without_retry(self):
        job=self.seed(error='MAC_CODEX_PROFILE_UNVERIFIED')
        for code in ('MAC_CODEX_PROFILE_UNVERIFIED','MAC_CODEX_PROTOCOL_UNVERIFIED',
                     'MAC_CODEX_PERMISSION_REQUIRED','MAC_CODEX_TURN_TIMEOUT'):
            self.engine.operational_failure(self.store.get(job['id']),code)
            current=self.store.get(job['id'])
            self.assertEqual(current['state'],'needs_user');self.assertEqual(current['provider_error'],code)
            self.assertIsNone(current['attempt']);self.assertIsNone(current['blocker']['retry_at'])
        with patch.object(self.engine,'launch') as launch,patch.object(self.engine,'keep_awake'):
            self.engine.tick();self.engine.tick()
        launch.assert_not_called();self.assertEqual(self.store.get(job['id'])['calls'],1)
        self.assertEqual(self.worker.launched,1);self.assertEqual(self.repos.published,0)
    def test_login_recovery_requires_a_real_identified_builder_before_inspection(self):
        job=self.seed(error='PROVIDER_LOGIN_REQUIRED')
        self.assertEqual(job['builder_sessions'],[])
        self.app.action(job['id'],'resume',{})
        job=self.store.get(job['id'])
        with patch('core.agents.command',return_value=['fixture-not-executed']), patch('core.subprocess.Popen'):
            self.engine.launch(job,'builder')
        job=self.store.get(job['id']); attempt=job['attempt']
        private_outcome(self.store.directory / 'jobs' / job['id'] / attempt['id'],attempt,
                        report(job['head']),sid='fixture-authenticated-builder')
        self.engine.observe(job); job=self.ready(self.store.get(job['id']))
        self.assertEqual(job['builder_sessions'],['fixture-authenticated-builder'])
        self.app.action(job['id'],'accept',{})
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'INSPECTED')
        self.assertEqual(self.worker.launched,1)
    def test_failed_builder_report_identity_also_excludes_that_session_from_review(self):
        job=self.seed(status='fail')
        with patch('core.agents.command',return_value=['fixture-not-executed']), patch('core.subprocess.Popen'):
            self.engine.launch(job,'builder')
        job=self.store.get(job['id']); attempt=job['attempt']
        private_outcome(self.store.directory / 'jobs' / job['id'] / attempt['id'],attempt,
                        report(job['head'],status='fail'),sid='fixture-failed-builder')
        self.engine.observe(job); job=self.ready(self.store.get(job['id']))
        self.assertEqual(job['builder_sessions'],['fixture-builder','fixture-failed-builder'])
        self.store.update(job['id'],supervision=self.review_evidence(job,'supervisor','4'*32,'fixture-failed-builder'))
        with self.assertRaisesRegex(common.AppError,'INDEPENDENT_REVIEW_REQUIRED'): self.app.action(job['id'],'accept',{})
    def test_checkpoint_failure_preserves_lineage_and_requires_bounded_retry(self):
        self.end_builder(); self.repos.fail_checkpoint=True; self.engine.pipeline.deliver()
        self.assertEqual(self.store.jobs(),[]); self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'DELIVERY_BLOCKED')
        self.repos.fail_checkpoint=False
        self.engine.pipeline.retry_delivery('owner/kix','KIX-P-SDK-0'); self.engine.pipeline.deliver()
        self.assertEqual(len(self.store.jobs()),1); self.assertEqual(self.worker.launched,1)
    def test_missing_native_exit_proof_holds_delivery_without_crashing_the_loop(self):
        record=self.end_builder()
        proof=self.store.directory / 'native' / record['request_id'] / record['attempt']['id'] / 'provider-exit.json'
        proof.unlink()
        self.assertTrue(self.engine.pipeline.deliver())
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'DELIVERY_BLOCKED')
        self.assertEqual(self.store.jobs(),[]); self.assertFalse(self.engine.pipeline.deliver())
        self.assertEqual(self.worker.launched,1)
    def test_current_head_private_independent_reviews_and_live_ci_allow_inspection_only(self):
        job=self.ready(); accepted=self.app.action(job['id'],'accept',{})
        self.assertEqual(accepted['state'],'accepted'); self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'INSPECTED')
        with self.assertRaisesRegex(common.AppError,'DEPENDENCY_GATE_REQUIRED'):
            self.source.call('read',self.source.select('owner/kix','KIX-NEXT'))
    def test_same_actual_reviewer_session_or_builder_session_is_not_independent(self):
        job=self.ready()
        evidence=self.review_evidence(job,'supervisor','4'*32,'fixture-builder')
        self.store.update(job['id'],supervision=evidence)
        with self.assertRaisesRegex(common.AppError,'INDEPENDENT_REVIEW_REQUIRED'): self.app.action(job['id'],'accept',{})
        self.assertEqual(self.store.get(job['id'])['state'],'ready')
    def test_caller_supplied_pass_without_private_worker_receipts_is_rejected(self):
        job=self.ready(); path=self.store.directory / 'jobs' / job['id'] / job['review']['attempt'] / 'receipt.json'
        path.unlink()
        with self.assertRaisesRegex(common.AppError,'PRIVATE_RECEIPT_REQUIRED'):
            self.app.action(job['id'],'accept',{'status':'PASS'})
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'DELIVERING')
    def test_ci_without_exact_head_or_without_actual_actions_source_is_rejected(self):
        job=self.ready(); job=self.store.update(job['id'],ci={'state':'passed','checks':['caller string'],'head':'e'*40,'source':'MODEL'})
        with self.assertRaisesRegex(common.AppError,'LIVE_CI_REQUIRED'): self.engine.pipeline.validate_inspection(job)
    def test_new_head_invalidates_both_existing_reviews(self):
        job=self.ready(); self.repos.current='e'*40
        with self.assertRaises(common.AppError) as error: self.app.action(job['id'],'accept',{})
        self.assertEqual(error.exception.code,'INSPECTION_CHANGED')
        current=self.store.get(job['id']); self.assertIsNone(current['review']); self.assertEqual(current['phase'],'building')
    def test_builder_profile_cannot_change_while_review_profile_can_be_reconfigured(self):
        job=self.seed(error='USER_STOPPED'); settings=self.store.settings()
        settings['roles']['builder']={'provider':'claude','model':''}; self.store.set_settings(settings)
        with self.assertRaisesRegex(common.AppError,'OWNER_PROFILE_IMMUTABLE'): self.store.action(job['id'],'reconfigure')
    def test_inspected_node_requires_authenticated_merge_and_post_merge_ci(self):
        job=self.ready(); self.app.action(job['id'],'accept',{}); self.repos.merge_ready=False
        with self.assertRaisesRegex(common.AppError,'POST_MERGE_CI_REQUIRED'):
            self.engine.pipeline.reconcile_accepted('owner/kix','KIX-P-SDK-0')
        self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'INSPECTED')
        self.repos.merge_ready=True
        proof=self.engine.pipeline.reconcile_accepted('owner/kix','KIX-P-SDK-0')
        self.assertEqual(proof['state'],'ACCEPTED'); self.assertEqual(proof['merge_head'],'d'*40)
    def test_authorized_base_advance_preserves_original_scope_revision_and_owner(self):
        job=self.ready(); self.app.action(job['id'],'accept',{}); self.engine.pipeline.reconcile_accepted('owner/kix','KIX-P-SDK-0')
        before=self.source.select('owner/kix','KIX-NEXT'); self.latest['source']['head']='d'*40
        rows=self.source.advance_base({'repository':'owner/kix','decision':'fixture verified merged dependency'})
        after=self.source.select('owner/kix','KIX-NEXT')
        for field in ('task_id','task_revision','owner_lane','plan_blob','dependencies','canonical_task_pointer'): self.assertEqual(before[field],after[field])
        self.assertEqual(after['plan_commit'],'d'*40); self.assertEqual(rows[0]['state'],'ACCEPTED')
        self.assertEqual(self.source.call('read',after).document['status'],'OBSERVED')
    def test_advance_does_not_skip_an_unknown_attempt_or_uninspected_delivery(self):
        self.seed(); self.latest['source']['head']='d'*40
        with self.assertRaises(common.AppError): self.source.advance_base({'repository':'owner/kix','decision':'fixture'})
    def test_update_cannot_replace_runtime_after_builder_exit_before_node_completion(self):
        self.seed()
        with self.assertRaises(common.AppError) as error: install.idle_database(self.store.directory)
        self.assertEqual(error.exception.code,'UPDATE_BUSY')
    def test_update_cannot_bypass_a_native_reservation_with_no_delivery_job(self):
        self.controller.start({'request_id':'1'*32,'binding':self.source.select('owner/kix','KIX-P-SDK-0')})
        self.assertEqual(self.store.jobs(),[])
        with self.assertRaises(common.AppError) as error: install.idle_database(self.store.directory)
        self.assertEqual(error.exception.code,'UPDATE_BUSY')
        self.assertEqual(self.controller.get('1'*32)['state'],'RUNNING')
    def test_native_result_without_a_change_cannot_skip_pr_and_actual_ci(self):
        job=self.ready(); self.repos.current=job['base_sha']
        job=self.store.update(job['id'],head=job['base_sha'],phase='publishing')
        with self.assertRaisesRegex(common.AppError,'DELIVERABLE_REQUIRED'): self.engine.step(job)
        self.assertEqual(self.repos.published,0)
    def test_existing_engine_moves_native_delivery_through_both_reviews_pr_ci_and_user_inspection(self):
        job=self.seed()
        for role in ('reviewer','supervisor'):
            if role=='supervisor':
                job=self.store.get(job['id']); self.assertEqual(job['phase'],'publishing')
                self.engine.step(job); job=self.store.get(job['id']); self.assertEqual(job['phase'],'verifying')
                self.assertIsNone(job['supervision']); self.assertNotEqual(job['state'],'ready')
                self.repos.ci_state='pending'
                self.engine.step(job); self.assertEqual(len(self.repos.dispatches),2)
                self.repos.ci_state='passed'
                self.engine.step(self.store.get(job['id']))
                self.assertEqual(self.store.get(job['id'])['phase'],'supervising')
            job=self.store.get(job['id'])
            with patch('core.agents.command',return_value=['fixture-not-executed']), patch('core.subprocess.Popen'):
                self.engine.launch(job,role)
            launched=self.store.get(job['id']); attempt=launched['attempt']; self.assertEqual(attempt['role'],role)
            folder=self.store.directory / 'jobs' / job['id'] / attempt['id']
            private_outcome(folder,attempt,report(job['head']),profile=job['settings']['roles'][role],sid='fixture-'+role)
            self.engine.observe(launched)
        job=self.store.get(job['id']); self.assertEqual(job['phase'],'verifying')
        self.engine.step(job); job=self.store.get(job['id']); self.assertEqual(job['state'],'ready')
        self.assertEqual(self.repos.published,1); self.assertEqual(self.worker.launched,1)
        self.app.action(job['id'],'accept',{}); self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'INSPECTED')

    def test_old_supervisor_question_resumes_candidate_without_repeating_reviewer(self):
        job=self.seed(); evidence=self.review_evidence(job,'reviewer','2'*32,'fixture-reviewer')
        terminal={'status':'needs_user','attempt':'prior-supervisor-receipt-preserved'}
        job=self.store.update(job['id'],state='needs_user',phase='supervising',review=evidence,
                              question='Supply actual hosted CI.',last_terminal=terminal)
        self.app.action(job['id'],'resume',{'answer':'Prepare the same Draft candidate through the app.'})
        with patch.object(self.engine,'launch') as launch:
            self.engine.step(self.store.get(job['id']))
        launch.assert_not_called(); current=self.store.get(job['id'])
        self.assertEqual(current['phase'],'publishing');self.assertEqual(current['review'],evidence)
        self.assertEqual(current['last_terminal'],terminal);self.assertEqual(current['calls'],job['calls'])
        self.assertEqual(current['user_answers'][-1]['question'],'Supply actual hosted CI.')

    def test_pending_ci_holds_supervisor_and_dispatches_each_workflow_once(self):
        job=self.ready();job=self.store.update(job['id'],state='verifying',supervision=None,candidate_ci_requested=None)
        self.repos.ci_state='pending'
        with patch.object(self.engine,'launch') as launch:
            self.engine.step(job);self.engine.step(self.store.get(job['id']))
        launch.assert_not_called();self.assertEqual(len(self.repos.dispatches),2)
        self.assertEqual(self.store.get(job['id'])['state'],'verifying')
        with self.assertRaises(common.AppError):self.app.action(job['id'],'accept',{})

    def test_unknown_dispatch_is_preserved_and_never_retransmitted(self):
        job=self.ready();job=self.store.update(job['id'],state='verifying',supervision=None,candidate_ci_requested=None)
        self.repos.ci_state='pending'
        with patch.object(self.repos,'dispatch_candidate_workflow',side_effect=TimeoutError):
            with self.assertRaises(common.AppError) as first:self.engine.step(job)
        self.assertEqual(first.exception.code,'MAC_HOST_CI_DISPATCH_UNKNOWN')
        current=self.store.get(job['id']);self.assertEqual(current['candidate_ci_dispatches'][0]['state'],'UNKNOWN')
        with patch.object(self.repos,'dispatch_candidate_workflow') as dispatch:
            with self.assertRaises(common.AppError) as second:self.engine.step(current)
        self.assertEqual(second.exception.code,'MAC_HOST_CI_DISPATCH_UNKNOWN')
        dispatch.assert_not_called()

    def test_declared_astra_gate_cannot_be_removed_by_job_or_model_text(self):
        job=self.ready(); bound=job['native_lineage']['binding']; original=self.source.program(bound)
        for floor,gate,expected in [('A3','NONE','ARCHITECTURE'),('A3','RELEASE','RELEASE'),('A2','MILESTONE','MILESTONE')]:
            program=copy.deepcopy(original)
            node=next(n for n in program['scope']['nodes'] if n['id']==bound['node'])
            node.update(audit_floor=floor,astra_gate=gate)
            job['audit_requirement']={'required':False,'audit_receipt':'model claimed PASS'}
            with patch.object(self.source,'program',return_value=program):
                requirement=self.engine.pipeline.audit_requirement(job)
                self.assertTrue(requirement['required']);self.assertEqual(requirement['astra_gate'],expected)
                with self.assertRaisesRegex(common.AppError,'ASTRA_AUDIT_REQUIRED'):
                    self.engine.pipeline.validate_inspection(job,refresh=False)

    def test_human_merge_requires_actual_inspection_exact_head_and_explicit_approval(self):
        job=self.ready()
        for value in ({},{'head':job['head'],'approval':'Explicit fixture User approval'}):
            with self.assertRaises(common.AppError):self.app.action(job['id'],'merge',value)
        self.app.action(job['id'],'accept',{})
        with self.assertRaises(common.AppError):self.app.action(job['id'],'merge',{'head':'e'*40,'approval':'wrong head'})
        self.assertEqual((self.repos.readied,self.repos.merge_count),(0,0))

    def test_approved_ready_waits_for_ci_and_merge_is_ordinary_and_idempotent(self):
        job=self.ready();self.app.action(job['id'],'accept',{})
        value={'head':job['head'],'approval':'Human explicitly approved this PR and head; fixture only.'}
        self.repos.ci_state='pending'
        result=self.app.action(job['id'],'merge',value)
        self.assertEqual(result['user_merge']['state'],'READY');self.assertEqual(self.repos.readied,1)
        self.assertEqual(self.repos.merge_count,0);self.assertEqual(self.source.tasks('owner/kix')[0]['state'],'INSPECTED')
        self.repos.ci_state='passed';result=self.app.action(job['id'],'merge',value)
        self.assertEqual(result['user_merge']['state'],'MERGED');self.assertEqual(self.repos.merge_count,1)
        self.app.action(job['id'],'merge',value)
        self.assertEqual(self.repos.merge_count,1);self.assertEqual(result['user_merge']['approval'],value)

    def test_uncertain_merge_holds_without_retransmission_and_approval_is_immutable(self):
        job=self.ready();self.app.action(job['id'],'accept',{})
        value={'head':job['head'],'approval':'Specific human fixture approval.'}
        with patch.object(self.repos,'user_merge',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):self.app.action(job['id'],'merge',value)
        current=self.store.get(job['id']);self.assertEqual(current['user_merge']['state'],'MERGE_SUBMITTING')
        with patch.object(self.repos,'user_merge') as merge:
            with self.assertRaises(common.AppError):self.app.action(job['id'],'merge',value)
            merge.assert_not_called()
        with self.assertRaises(common.AppError):self.app.action(job['id'],'merge',{**value,'approval':'Another approval'})
    def test_native_rework_keeps_the_original_node_id_and_full_spec(self):
        job=self.seed(status='fail')
        with patch('core.agents.command',return_value=['fixture-not-executed']), patch('core.subprocess.Popen'):
            self.engine.launch(job,'builder')
        attempt=self.store.get(job['id'])['attempt']
        self.assertEqual(attempt['task']['id'],'P-SDK-0')
        self.assertIn('Original complete SDK scope',attempt['task']['instructions'])
        self.assertEqual(self.worker.launched,1)


class CandidateDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repos=gitops.Repositories(Path(self.temp.name)/'workspaces')
        self.job={'repository':'BeautifulMind-JT/kix-protocol','head':'c'*40,'branch':'aiops/native-fixture',
                  'base_branch':'main','pr_url':'https://github.com/BeautifulMind-JT/kix-protocol/pull/9'}
        self.pull={'state':'open','draft':True,'auto_merge':None,
                   'head':{'sha':'c'*40,'ref':self.job['branch']},'base':{'ref':'main'}}
        self.ref={'object':{'sha':'c'*40}}
        self.workflow={'path':'.github/workflows/ktx-kernel.yml','state':'active'}
    def api(self,path):
        return self.pull if '/pulls/' in path else self.ref if '/git/ref/' in path else self.workflow
    def test_dispatch_is_only_product_verification_of_bound_draft_head(self):
        with patch.object(self.repos,'candidate_workflows',return_value=['ktx-kernel.yml']),\
             patch.object(self.repos,'assert_binding'),patch.object(self.repos,'head',return_value='c'*40),\
             patch.object(self.repos,'clean',return_value=True),patch('handoff.api',side_effect=self.api),\
             patch('gitops.execute') as execute:
            self.repos.dispatch_candidate_workflow(self.job,'ktx-kernel.yml')
        argv=execute.call_args.args[0]
        self.assertEqual(argv,['gh','api','--method','POST',
            'repos/BeautifulMind-JT/kix-protocol/actions/workflows/ktx-kernel.yml/dispatches',
            '-f','ref=aiops/native-fixture'])
    def test_ready_auto_merge_or_changed_remote_head_cannot_be_dispatched(self):
        for change in ('ready','auto_merge','head'):
            self.pull['draft']=change!='ready';self.pull['auto_merge']={} if change=='auto_merge' else None
            self.ref['object']['sha']='d'*40 if change=='head' else 'c'*40
            with patch.object(self.repos,'candidate_workflows',return_value=['ktx-kernel.yml']),\
                 patch.object(self.repos,'assert_binding'),patch.object(self.repos,'head',return_value='c'*40),\
                 patch.object(self.repos,'clean',return_value=True),patch('handoff.api',side_effect=self.api),\
                 patch('gitops.execute') as execute:
                with self.assertRaises(common.AppError):self.repos.dispatch_candidate_workflow(self.job,'ktx-kernel.yml')
                execute.assert_not_called()
    def test_control_plane_vm_or_unadmitted_repository_workflows_are_not_routes(self):
        with patch('gitops.execute') as execute:
            with self.assertRaises(common.AppError):self.repos.dispatch_candidate_workflow(self.job,'control-plane-runtime.yml')
            self.job['repository']='other/repo'
            with self.assertRaises(common.AppError):self.repos.candidate_workflows(self.job)
            execute.assert_not_called()

    def test_user_merge_is_expected_head_guarded_ordinary_merge_without_admin_bypass(self):
        candidate={'merged':False,'draft':False,'number':9,'merge_head':None}
        merged={**candidate,'merged':True,'merge_head':'d'*40}
        with patch.object(self.repos,'merge_candidate',side_effect=[candidate,merged]),\
             patch('gitops.execute',return_value=common.encoded({'merged':True,'sha':'d'*40})) as execute:
            result=self.repos.user_merge(self.job)
        self.assertTrue(result['merged'])
        self.assertEqual(execute.call_args.args[0],['gh','api','--method','PUT',
            'repos/BeautifulMind-JT/kix-protocol/pulls/9/merge','-f','sha='+self.job['head'],'-f','merge_method=merge'])
    def test_changed_head_or_base_holds_before_ready_or_merge(self):
        self.job['base_sha']='b'*40
        self.pull['base']['sha']='b'*40
        for field in ('head','base'):
            self.pull['head']['sha']='e'*40 if field=='head' else self.job['head']
            self.pull['base']['sha']='e'*40 if field=='base' else self.job['base_sha']
            with patch('handoff.api',return_value=self.pull),patch('gitops.execute') as execute:
                with self.assertRaises(common.AppError):self.repos.user_ready(self.job)
                execute.assert_not_called()


class HostedCITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repos=gitops.Repositories(Path(self.temp.name) / 'workspaces')
        self.job={'repository':'owner/kix','head':'c'*40,'native_lineage':{}}
        self.check={'head_sha':'c'*40,'name':'verified CI','status':'completed','conclusion':'success',
                    'app':{'slug':'github-actions'},'details_url':'https://github.com/owner/kix/actions/runs/7/job/8'}
        self.run={'head_sha':'c'*40,'repository':{'full_name':'owner/kix'},'event':'workflow_dispatch',
                  'name':'verified workflow','status':'completed','conclusion':'success'}
    def api(self,path): return {'total_count':1,'check_runs':[self.check]} if 'check-runs' in path else self.run
    def test_actual_actions_metadata_is_bound_to_exact_commit_and_run(self):
        with patch('handoff.api',side_effect=self.api): result=self.repos.hosted_checks(self.job,'c'*40)
        self.assertEqual(result['state'],'passed'); self.assertEqual(result['head'],'c'*40)
        self.assertEqual(result['checks'][0]['run_id'],7)
    def test_plain_status_or_another_app_cannot_become_mac_ci(self):
        self.check['app']['slug']='untrusted-status-app'
        with patch('handoff.api',side_effect=self.api): result=self.repos.hosted_checks(self.job,'c'*40)
        self.assertEqual(result['state'],'pending')
    def test_run_for_another_commit_or_repository_is_not_ci(self):
        self.run['head_sha']='e'*40
        with patch('handoff.api',side_effect=self.api), self.assertRaisesRegex(common.AppError,'CI_OBSERVATION_UNVERIFIED'):
            self.repos.hosted_checks(self.job,'c'*40)
    def test_skipped_failed_or_queued_check_is_not_passed(self):
        for conclusion,state in [('skipped','pending'),('failure','failed'),(None,'pending')]:
            self.check['conclusion']=conclusion
            with patch('handoff.api',side_effect=self.api): result=self.repos.hosted_checks(self.job,'c'*40)
            self.assertEqual(result['state'],state)
    def test_incomplete_check_run_page_cannot_drop_a_failed_check(self):
        with patch('handoff.api',return_value={'total_count':101,'check_runs':[self.check]}), self.assertRaises(common.AppError):
            self.repos.hosted_checks(self.job,'c'*40)
    def test_successful_job_in_incomplete_or_failed_workflow_is_not_pass(self):
        for status,conclusion,expected in [('in_progress',None,'pending'),('completed','failure','failed')]:
            self.run.update(status=status,conclusion=conclusion)
            with patch('handoff.api',side_effect=self.api):result=self.repos.hosted_checks(self.job,'c'*40)
            self.assertEqual(result['state'],expected)
    def test_draft_skipped_check_is_missing_evidence_not_code_failure(self):
        self.run['event']='pull_request';self.check['conclusion']='skipped'
        with patch('handoff.api',side_effect=self.api):result=self.repos.hosted_checks(self.job,'c'*40)
        self.assertEqual(result['state'],'pending');self.assertEqual(result['checks'],[])
    def test_post_merge_uses_actual_configured_post_merge_checks(self):
        self.check['name']='protocol'
        config={'owner/kix':{'program_required_checks':['protocol','kernel'],'program_post_merge_required_checks':['protocol']}}
        with patch('gitops.read_json',return_value=config),patch('handoff.api',side_effect=self.api):
            self.assertEqual(self.repos.hosted_checks(self.job,'c'*40)['state'],'pending')
            self.assertEqual(self.repos.hosted_checks(self.job,'c'*40,post_merge=True)['state'],'passed')
    def test_codex_native_thread_identity_is_extracted_from_its_cli_event(self):
        folder=common.private_directory(Path(self.temp.name) / 'receipt')
        common.atomic_json(folder / 'last-message.json',report())
        (folder / 'stdout.log').write_text('{"type":"thread.started","thread_id":"fixture-thread-id"}\n')
        result=agents.completion({'provider':'codex','model':''},folder)
        self.assertEqual(result['session_id'],'codex-cli:fixture-thread-id')


if __name__=='__main__': unittest.main()
