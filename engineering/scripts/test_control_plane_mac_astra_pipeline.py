"""A3 app transitions with synthetic GitHub and actual private fixture receipts."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac_app'))
import aiops
import common
import core
import mac_astra_receipt as receipts
import mac_authority
import native_transfer
import test_control_plane_mac_authority as authority
import test_control_plane_mac_pipeline as delivery
from test_control_plane_mac_astra_receipt import audit_comment


class AstraPipelineTests(unittest.TestCase):
    end_builder=delivery.MacPipelineTests.end_builder
    seed=delivery.MacPipelineTests.seed
    review_evidence=delivery.MacPipelineTests.review_evidence

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=core.Store(Path(self.temp.name)/'app');self.addCleanup(self.store.close)
        self.source=mac_authority.LocalSource(self.store);self.worker=authority.FixtureWorker(self.store)
        self.controller=native_transfer.Controller(self.store,self.source,self.worker)
        self.latest=authority.snapshot(nodes=[{'id':'P-SDK-0','title':'Original node','spec':'Original unchanged scope',
                                              'audit_floor':'A3','astra_gate':'NONE'}])
        self.addCleanup(patch.stopall)
        patch('mac_authority.handoff.inspect_repository',side_effect=lambda repo, **kwargs:copy.deepcopy(self.latest)).start()
        patch('mac_astra_receipt.decision_evidence',return_value=copy.deepcopy(receipts.DECISION)).start()
        self.source.initialize({'mode':'MAC','decision':'Synthetic fixture authority'});self.source.register(self.latest)
        self.repos=delivery.FixtureRepositories(self.store.directory/'workspaces')
        self.engine=core.Engine(self.store,self.repos,self.controller);patch.object(self.engine,'notify').start()
        self.app=object.__new__(aiops.Application);self.app.store=self.store;self.app.engine=self.engine;self.app.canonical=self.controller
        self.comments=[];self.deleted=False;self.reads=0
        patch('handoff.api',side_effect=self.api).start()

    def api(self,path,paginate=False):
        self.reads+=1;job=self.store.jobs()[0]
        if path.endswith('/pulls/2'):
            return {'number':2,'html_url':job['pr_url'],'head':{'sha':self.repos.current,'ref':job['branch'],
                    'repo':{'full_name':job['repository']}},'base':{'repo':{'full_name':job['repository']}}}
        if '/issues/2/comments?' in path:return [copy.deepcopy(self.comments)]
        if '/issues/comments/' in path:
            if self.deleted:raise common.AppError('GITHUB_NOT_FOUND')
            number=int(path.rsplit('/',1)[1]);found=[c for c in self.comments if c['id']==number]
            if not found:raise common.AppError('GITHUB_NOT_FOUND')
            return copy.deepcopy(found[0])
        raise AssertionError(path)

    def candidate(self):
        job=self.seed();review=self.review_evidence(job,'reviewer','2'*32,'fixture-reviewer')
        return self.store.update(job['id'],state='verifying',phase='verifying',review=review,
            pr_url='https://github.com/owner/kix/pull/2',candidate_published_head=job['head'],
            candidate_ci_requested=job['head'],ci=self.repos.checks(job))

    def audited(self):
        job=self.candidate()
        with self.assertRaisesRegex(common.AppError,'ASTRA_AUDIT_REQUIRED'):self.engine.step(job)
        request=self.store.get(job['id'])['audit_requirement'];self.comments=[audit_comment(request)]
        self.engine.step(self.store.get(job['id']))
        current=self.store.get(job['id']);self.assertEqual(current['phase'],'supervising')
        self.assertEqual(current['audit_requirement']['audit_receipt']['comment']['comment_id'],123)
        return current

    def inspected(self):
        job=self.audited()
        with patch('core.agents.command',return_value=['fixture-not-executed']),patch('core.subprocess.Popen'):
            self.engine.launch(job,'supervisor')
        job=self.store.get(job['id']);attempt=job['attempt'];folder=self.store.directory/'jobs'/job['id']/attempt['id']
        delivery.private_outcome(folder,attempt,delivery.report(job['head']),profile=job['settings']['roles']['supervisor'],sid='fixture-supervisor')
        self.engine.observe(job);self.engine.step(self.store.get(job['id']))
        job=self.store.get(job['id']);self.assertEqual(job['state'],'ready')
        self.engine.pipeline.validate_inspection(job,refresh=False)
        return job

    def test_admission_audit_supervision_and_inspection_remain_separate_gates(self):
        job=self.inspected();before=self.reads
        self.app.action(job['id'],'accept',{})
        self.assertGreater(self.reads,before)
        self.assertEqual(self.store.get(job['id'])['state'],'accepted');self.assertEqual(self.repos.merge_count,0)
        self.assertEqual(self.worker.launched,1)

    def test_live_reverification_preserves_private_job_digest_for_dependencies(self):
        job=self.inspected();self.app.action(job['id'],'accept',{})
        before=self.store.get(job['id']);reads=self.reads
        self.engine.pipeline.validate_inspection(before,refresh=False)
        self.assertGreater(self.reads,reads)
        self.assertEqual(self.store.get(job['id']),before)

    def test_pending_to_passed_ci_uses_current_host_evidence_for_audit_request(self):
        job=self.candidate();self.repos.ci_state='pending'
        self.engine.step(job);job=self.store.get(job['id'])
        self.assertEqual(job['ci']['state'],'pending')
        self.repos.ci_state='passed'
        with self.assertRaisesRegex(common.AppError,'ASTRA_AUDIT_REQUIRED'):self.engine.step(job)
        current=self.store.get(job['id'])
        self.assertEqual(current['ci']['state'],'passed')
        self.assertIsNotNone(current['audit_requirement']['request_recorded_at'])

    def test_constructing_pipeline_does_not_migrate_read_only_source_ledgers(self):
        from mac_pipeline import Pipeline
        with patch('mac_astra_receipt.Journal') as journal:
            pipeline=Pipeline(self.store,self.source,self.repos)
            journal.assert_not_called()
            self.assertIsNone(pipeline._astra)

    def test_changed_or_deleted_audit_blocks_supervisor_start_before_any_model(self):
        job=self.audited();self.deleted=True
        with patch('core.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(common.AppError,'RECEIPT_UNVERIFIED'):self.engine.launch(job,'supervisor')
        spawn.assert_not_called();self.assertIsNone(self.store.get(job['id'])['attempt'])

    def test_changed_or_deleted_audit_blocks_acceptance_and_user_merge(self):
        job=self.inspected();self.deleted=True
        with self.assertRaises(common.AppError):self.app.action(job['id'],'accept',{})
        self.deleted=False;self.app.action(job['id'],'accept',{});self.deleted=True
        with self.assertRaises(common.AppError):self.app.action(job['id'],'merge',{'head':job['head'],'approval':'Synthetic User approval'})
        self.assertEqual(self.repos.merge_count,0);self.assertEqual(self.repos.readied,0)

    def test_conflicting_same_head_fail_cannot_be_hidden_by_prior_pass(self):
        job=self.audited();self.comments.append(audit_comment(job['audit_requirement'],cid=124,result='FAIL'))
        with self.assertRaisesRegex(common.AppError,'AUDIT_CONFLICT'):self.engine.pipeline.validate_audit(job)

    def test_rework_review_at_same_head_cannot_reuse_existing_receipt(self):
        job=self.audited();review=self.review_evidence(job,'reviewer','4'*32,'fixture-new-reviewer')
        job=self.store.update(job['id'],review=review)
        with self.assertRaisesRegex(common.AppError,'RECEIPT_REPLAY'):self.engine.pipeline.validate_audit(job)


if __name__=='__main__':unittest.main()
