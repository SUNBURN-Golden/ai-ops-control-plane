"""Isolated fixtures only: no product model, ledger or provider execution."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import common
import core
import agents
import mac_astra
import mac_bundle
import mac_generation
import product_builder
import native_transfer
import test_control_plane_mac_authority as authority
import test_control_plane_mac_generation as generation


class BundleTests(unittest.TestCase):
    def setUp(self):
        generation.MacGenerationTests.setUp(self)
        self.latest = authority.snapshot(repo='SUNBURN-Golden/kix-protocol', nodes=[
            {'id':'sdk','title':'SDK','spec':'Generate exact SDK bytes'},
            {'id':'catalogue','title':'Catalogue','spec':'Do not invent commands','depends_on':['sdk']},
            {'id':'examples','title':'Examples','spec':'Use actual receipt fields','depends_on':['catalogue']}])
        self.spec = {'id':'bundle-sdk','title':'SDK package','nodes':['sdk','catalogue','examples']}
        self.audit = patch('mac_astra_receipt.admission',return_value={'fixture':'not real audit'})
        self.audit.start();self.addCleanup(self.audit.stop)
    def value(self, **changes):
        return {'repository':self.latest['repository'],'node':self.spec['id'],'bundle':copy.deepcopy(self.spec),
                'generation_id':'1'*32,'decision':mac_generation.HOST_DECISION['url'],
                'plan_commit':self.latest['source']['head'],'plan_blob':self.latest['source']['blob'],**changes}
    def candidate(self):
        v=self.value();return {k:v[k] for k in ('repository','bundle','plan_commit','plan_blob')}
    def adopt(self, **changes):return self.source.generation(self.value(**changes))
    def work(self,bound):return self.source.call('read',bound).document['work']
    def test_candidate_preparation_is_read_only_even_with_existing_owner(self):
        self.latest['tasks']=[{'program':'KIX','node':'sdk','state':'open'}]
        before=list(self.store.db.iterdump())
        result=mac_bundle.prepare(self.source,self.candidate())
        self.assertEqual(result['status'],'CANDIDATE_ONLY');self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['blockers'][0]['code'],'MAC_GENERATION_LINUX_OWNER_UNRESOLVED')
        self.assertEqual(before,list(self.store.db.iterdump()));self.assertEqual(self.worker.launched,0)
    def test_bundle_preserves_originals_internal_edges_and_a3(self):
        bound=self.adopt();work=self.work(bound)
        original=common.parse_json(self.latest['source']['raw_program'])['nodes']
        self.assertEqual(work['bundle']['nodes'],original)
        self.assertEqual(bound['dependencies'],[])
        self.assertEqual(work['task']['audit_floor'],'A3')
        self.assertTrue(work['task']['user_merge']);self.assertFalse(work['task']['astra_auto_merge'])
        self.assertEqual(mac_bundle.plan_tasks(work)[2]['depends_on'],['catalogue'])
        self.assertEqual(self.source.tasks(self.latest['repository'])[0]['state'],'READY')
        self.assertEqual(native_transfer.binding(bound),bound)
    def test_release_gate_preserved(self):
        scope=self.source.program(self.adopt())['scope'];scope['nodes'][1]['astra_gate']='RELEASE'
        task,_=mac_bundle.aggregate(scope,self.spec)
        self.assertEqual(task['astra_gate'],'RELEASE')
    def test_invalid_order_duplicates_unknown_and_other_product_rejected(self):
        for nodes in (['catalogue','sdk'],['sdk','sdk'],['sdk','absent']):
            with self.assertRaises(common.AppError):self.adopt(bundle={**self.spec,'nodes':nodes})
        v=self.candidate();v['repository']='owner/kix'
        self.latest=authority.snapshot()
        with self.assertRaises(common.AppError):mac_bundle.prepare(self.source,v)
        self.assertEqual(self.worker.launched,0)
    def test_unknown_cannot_be_acknowledged_by_bundle(self):
        generation.MacGenerationTests.journal(self)
        before=list(self.store.db.iterdump())
        with self.assertRaisesRegex(common.AppError,'RECEIPT_SCOPE_UNRESOLVED'):self.adopt()
        self.assertEqual(before,list(self.store.db.iterdump()))
        self.source.annotate_receipt({'request_id':'legacy-request','payload_sha256':'b'*64,
            'repository':self.latest['repository'],'task_id':'KIX-SDK','decision':'fixture scope only'})
        with self.assertRaisesRegex(common.AppError,'OWNER_UNRESOLVED'):self.adopt()
        self.assertEqual(self.worker.launched,0)
    def test_every_member_owner_checked_before_adoption_and_start(self):
        for node in self.spec['nodes']:
            self.latest['tasks']=[{'program':'KIX','node':node,'state':'open'}]
            with self.assertRaisesRegex(common.AppError,'LINUX_OWNER_UNRESOLVED'):self.adopt()
        self.latest['tasks']=[];bound=self.adopt()
        self.latest['tasks']=[{'program':'KIX','node':'examples','state':'open'}]
        with self.assertRaisesRegex(common.AppError,'LINUX_OWNER_UNRESOLVED'):
            self.controller.start({'binding':bound,'request_id':'2'*32})
        self.assertEqual(self.worker.launched,0)
    def test_overlapping_bundle_and_single_adoption_both_refused(self):
        self.adopt()
        for value in (self.value(generation_id='2'*32,bundle={**self.spec,'nodes':['sdk','catalogue']}),
                      {k:v for k,v in self.value(generation_id='3'*32,node='sdk').items() if k!='bundle'}):
            with self.assertRaisesRegex(common.AppError,'ALREADY_OWNED'):self.source.generation(value)
        with self.assertRaisesRegex(common.AppError,'ALREADY_OWNED'):self.source.register(self.latest)
    def test_existing_single_generation_blocks_bundle(self):
        v=self.value(node='sdk');v.pop('bundle');self.source.generation(v)
        with self.assertRaisesRegex(common.AppError,'ALREADY_OWNED'):self.adopt(generation_id='2'*32)
    def test_external_dependency_is_not_silently_internalized(self):
        with self.assertRaisesRegex(common.AppError,'DEPENDENCY_GATE_REQUIRED'):
            self.adopt(bundle={**self.spec,'nodes':['catalogue','examples']})
        self.assertEqual(self.source.tasks(self.latest['repository']),[])
    def test_replay_does_not_widen_scope(self):
        bound=self.adopt();before=list(self.store.db.iterdump());self.assertEqual(self.adopt(),bound)
        self.assertEqual(before,list(self.store.db.iterdump()))
        with self.assertRaisesRegex(common.AppError,'IMMUTABLE'):
            self.adopt(bundle={**self.spec,'title':'new scope'})
    def test_completion_needs_every_member_and_actual_evidence(self):
        packet=self.work(self.adopt())['bundle']
        report={'status':'complete','covered_tasks':self.spec['nodes'],
                'checks':['node:'+n+': fixture check executed' for n in self.spec['nodes']]}
        mac_bundle.validate_report(report,packet)
        for bad in ({**report,'covered_tasks':['sdk']},{**report,'checks':['all good']},
                    {**report,'checks':['node:'+n+':' for n in self.spec['nodes']]}):
            with self.assertRaisesRegex(common.AppError,'MEMBER_EVIDENCE_REQUIRED'):mac_bundle.validate_report(bad,packet)
    def test_aggregate_audit_is_new_request_not_pr121_exception(self):
        bound=self.adopt();work=self.work(bound)
        job={'id':'newjob','repository':bound['repository'],'base_sha':bound['plan_commit'],'head':'c'*40,
             'native_lineage':{'binding':bound,'request_id':'2'*32,'attempt_id':'3'*32},'bundle':work['bundle']}
        requirement=mac_astra.audit_requirement(job,self.source.program(bound)['scope'])
        self.assertTrue(requirement['required']);self.assertEqual(requirement['audit_floor'],'A3')
        import mac_glm_audit
        self.assertFalse(mac_glm_audit.supported(requirement))
    def test_frozen_member_tampering_fails_closed(self):
        bound=self.adopt();work=self.work(bound);work['bundle']['nodes'][0]['spec']='weakened'
        self.store.db.execute('UPDATE mac_host_tasks SET work=?',(common.encoded(work),))
        with self.assertRaisesRegex(common.AppError,'BUNDLE_BINDING_INVALID'):self.source.call('read',bound)


class ProductProfileTests(unittest.TestCase):
    def test_scoped_profile_only_changes_kix_future_builder(self):
        original=copy.deepcopy(common.DEFAULTS)
        settings={**original,'product_builders':{product_builder.REPOSITORY:product_builder.PROFILE}}
        common.validate_settings(settings)
        self.assertEqual(product_builder.resolve(settings,'SUNBURN-Golden/kix-protocol')['roles']['builder'],product_builder.PROFILE)
        self.assertEqual(product_builder.resolve(settings,'SUNBURN-Golden/kix-commerce-apps')['roles'],original['roles'])
        self.assertEqual(settings['roles'],original['roles'])
        self.assertEqual(product_builder.options(product_builder.PROFILE),{'model_reasoning_effort':'high','service_tier':'priority'})
    def test_global_tuning_arbitrary_product_and_partial_options_rejected(self):
        settings=copy.deepcopy(common.DEFAULTS);settings['roles']['builder']=product_builder.PROFILE
        with self.assertRaises(common.AppError):common.validate_settings(settings)
        for value in ({'other/repo':product_builder.PROFILE},{product_builder.REPOSITORY:{**product_builder.PROFILE,'service_tier':'auto'}}):
            with self.assertRaises(common.AppError):product_builder.validate(value)
        with self.assertRaises(common.AppError):common.validate_profile({'provider':'codex','model':'gpt-6-astra','reasoning_effort':'high'})
    def test_cli_options_and_receipt_bind_requested_values(self):
        with patch('agents.executable',return_value='/fixture/codex'):
            argv=agents.command(product_builder.PROFILE,'builder',Path('/tmp/fixture'))
        self.assertIn('service_tier="priority"',argv);self.assertIn('model_reasoning_effort="high"',argv)
        evidence=product_builder.evidence(product_builder.PROFILE)
        self.assertTrue(product_builder.matches(product_builder.PROFILE,evidence))
        self.assertFalse(product_builder.matches(product_builder.PROFILE,{}))


if __name__=='__main__':unittest.main()
