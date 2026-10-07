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
        self.bundle_decision=patch('mac_bundle.decision',return_value=copy.deepcopy(mac_bundle.DECISION))
        self.bundle_decision.start();self.addCleanup(self.bundle_decision.stop)
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
    def test_bundle_namespace_alias_requires_live_existing_identity_contract(self):
        from program_scope import load_scope
        raw=common.parse_json(self.latest['source']['raw_program']);raw['repository']='BeautifulMind-JT/kix-protocol'
        blob=self.latest['source']['blob']
        with patch('repository_identity.same_repository',return_value=False):
            with self.assertRaises(common.AppError):load_scope(raw,self.latest['repository'],blob,bundle_repository_alias=True)
        with patch('repository_identity.same_repository',return_value=True):
            scope=load_scope(raw,self.latest['repository'],blob,bundle_repository_alias=True)
            self.assertEqual(scope['source_repository'],raw['repository'])
            self.assertEqual(scope['repository'],self.latest['repository'])
            self.assertEqual(scope['nodes'],raw['nodes'])
            with self.assertRaises(common.AppError):load_scope(raw,self.latest['repository'],blob)
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
    def test_candidate_defers_dependency_audit_observations_to_admission(self):
        value=self.candidate();value['bundle']['nodes']=['catalogue','examples']
        before=list(self.store.db.iterdump())
        with patch('mac_generation.dependency_evidence',side_effect=AssertionError('read-only candidate cannot consume audit')):
            candidate=mac_bundle.prepare(self.source,value)
        self.assertIn({'node':'sdk','code':'MAC_BUNDLE_DEPENDENCY_REVALIDATION_REQUIRED'},candidate['blockers'])
        self.assertEqual(before,list(self.store.db.iterdump()))
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

    def delivered(self, *, complete=True):
        import test_control_plane_mac_pipeline as delivery
        bound=self.adopt();record=self.controller.start({'binding':bound,'request_id':'2'*32})
        folder=self.store.directory/'native'/record['request_id']/record['attempt']['id']
        checkout=common.private_directory(folder/'checkout')
        common.atomic_json(folder/'request.json',{'attempt_id':record['attempt']['id'],
            'binding':record['attempt']['binding'],'host_directory':str(self.store.directory.resolve()),'checkout':str(checkout)})
        report=delivery.report()
        if complete:
            report.update(covered_tasks=self.spec['nodes'],checks=['node:'+n+': fixture evidence' for n in self.spec['nodes']])
        self.worker.result=delivery.private_outcome(folder,record['attempt'],report)
        self.controller.tick()
        self.assertEqual(self.controller.get('2'*32)['state'],'TERMINAL')
        repos=delivery.FixtureRepositories(self.store.directory/'workspaces')
        engine=core.Engine(self.store,repos,self.controller)
        self.assertTrue(engine.pipeline.deliver())
        return engine,self.store.jobs()[0]
    def test_complete_bundle_delivery_keeps_one_lineage_and_all_review_members(self):
        engine,job=self.delivered()
        self.assertEqual(job['state'],'reviewing')
        self.assertEqual(job['built_tasks'],self.spec['nodes'])
        self.assertEqual([n['id'] for n in job['plan']['tasks']],self.spec['nodes'])
        self.assertEqual(job['plan']['tasks'][1]['depends_on'],['sdk'])
        engine.pipeline.assert_job(job,refresh=True)
        self.assertEqual(len(self.store.jobs()),1)
        self.assertIn('one writer/branch/PR',agents.prompt(job,'reviewer',job['head']))
    def test_partial_complete_report_is_rework_not_accepted_or_review_ready(self):
        engine,job=self.delivered(complete=False)
        self.assertEqual(job['state'],'building');self.assertTrue(job['correcting'])
        self.assertEqual(job['built_tasks'],[])
        task=engine.builder_task(job)
        for member in job['bundle']['nodes']:self.assertIn(member['spec'],task['instructions'])
        self.assertEqual(task['id'],self.spec['id'])
    def test_reviewer_cannot_pass_only_one_member_or_change_member_scope(self):
        import test_control_plane_mac_pipeline as delivery
        engine,job=self.delivered()
        with patch('core.agents.command',return_value=['not-executed']),patch('core.subprocess.Popen'):
            engine.launch(job,'reviewer')
        current=self.store.get(job['id']);attempt=current['attempt']
        delivery.private_outcome(self.store.directory/'jobs'/job['id']/attempt['id'],attempt,
            delivery.report(current['head']),profile=current['settings']['roles']['reviewer'],sid='independent-fixture')
        engine.observe(current)
        self.assertEqual(self.store.get(job['id'])['phase'],'building')
        self.assertIsNone(self.store.get(job['id'])['review'])
        changed=copy.deepcopy(job);changed['plan']['tasks'][1]['instructions']='weakened'
        with self.assertRaisesRegex(common.AppError,'DELIVERY_BINDING_MISMATCH'):engine.pipeline.assert_job(changed,refresh=False)
    def test_same_identity_legacy_namespace_unknown_still_blocks(self):
        generation.MacGenerationTests.journal(self)
        self.source.annotate_receipt({'request_id':'legacy-request','payload_sha256':'b'*64,
            'repository':'BeautifulMind-JT/kix-protocol','task_id':'KIX-SDK','decision':'fixture scope only'})
        with self.assertRaisesRegex(common.AppError,'OWNER_UNRESOLVED'):self.adopt()
    def test_edited_bundle_authority_stops_start(self):
        bound=self.adopt()
        with patch('mac_bundle.decision',side_effect=common.AppError('MAC_BUNDLE_DECISION_UNVERIFIED')):
            with self.assertRaisesRegex(common.AppError,'DECISION_UNVERIFIED'):
                self.controller.start({'binding':bound,'request_id':'2'*32})
        self.assertEqual(self.worker.launched,0)


class ProductProfileTests(unittest.TestCase):
    def test_scoped_profile_only_changes_kix_future_builder(self):
        original=copy.deepcopy(common.DEFAULTS)
        settings={**original,'product_builders':{product_builder.REPOSITORY:product_builder.PROFILE}}
        common.validate_settings(settings)
        self.assertEqual(product_builder.resolve(settings,'SUNBURN-Golden/kix-protocol')['roles']['builder'],product_builder.PROFILE)
        self.assertEqual(product_builder.resolve(settings,'SUNBURN-Golden/kix-commerce-apps')['roles'],original['roles'])
        self.assertEqual(settings['roles'],original['roles'])
        self.assertEqual(product_builder.options(product_builder.PROFILE),{'model_reasoning_effort':'high','service_tier':'default'})
    def test_global_tuning_arbitrary_product_and_partial_options_rejected(self):
        settings=copy.deepcopy(common.DEFAULTS);settings['roles']['builder']=product_builder.PROFILE
        with self.assertRaises(common.AppError):common.validate_settings(settings)
        for value in ({'other/repo':product_builder.PROFILE},{product_builder.REPOSITORY:{**product_builder.PROFILE,'service_tier':'auto'}}):
            with self.assertRaises(common.AppError):product_builder.validate(value)
        with self.assertRaises(common.AppError):common.validate_profile({'provider':'codex','model':'gpt-6-astra','reasoning_effort':'high'})
    def test_reconfigure_resolves_product_override_without_replacing_frozen_builder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            store=core.Store(Path(temp)/'app')
            try:
                store.product_builder({product_builder.REPOSITORY:product_builder.PROFILE})
                job=store.create({'repository':product_builder.REPOSITORY,'request_id':'fixture-request','goal':'fixture'})
                settings=product_builder.resolve(store.settings(),job['repository'])
                store.update(job['id'],settings=settings,native_lineage={'fixture':True},state='waiting_provider',phase='reviewing')
                new=copy.deepcopy(common.DEFAULTS);new['roles']['reviewer']={'provider':'claude','model':'fixture'};store.set_settings(new)
                store.action(job['id'],'reconfigure',{})
                actual=store.get(job['id'])['settings']
                self.assertEqual(actual['roles']['builder'],product_builder.PROFILE)
                self.assertEqual(actual['roles']['reviewer']['provider'],'claude')
            finally:store.close()
    def test_cli_options_and_receipt_bind_requested_values(self):
        with patch('agents.executable',return_value='/fixture/codex'):
            argv=agents.command(product_builder.PROFILE,'builder',Path('/tmp/fixture'))
        self.assertIn('service_tier="default"',argv);self.assertIn('model_reasoning_effort="high"',argv)
        evidence=product_builder.evidence(product_builder.PROFILE)
        self.assertTrue(product_builder.matches(product_builder.PROFILE,evidence))
        self.assertFalse(product_builder.matches(product_builder.PROFILE,{}))

    def test_legacy_fast_mapping_and_evidence_are_not_rewritten(self):
        fast=product_builder.FAST_PROFILE
        common.validate_profile(fast)
        product_builder.validate({product_builder.REPOSITORY:fast})
        self.assertEqual(product_builder.options(fast),{'model_reasoning_effort':'high','service_tier':'priority'})
        with patch('agents.executable',return_value='/fixture/codex'):
            argv=agents.command(fast,'builder',Path('/tmp/fixture'))
        self.assertIn('service_tier="priority"',argv)
        for profile,other in ((fast,product_builder.PROFILE),(product_builder.PROFILE,fast)):
            receipt=product_builder.evidence(profile)
            self.assertEqual(receipt['service_tier_requested'],profile['service_tier'])
            self.assertTrue(product_builder.matches(profile,receipt))
            self.assertFalse(product_builder.matches(other,receipt))

    def test_future_default_setting_does_not_mutate_frozen_fast_job(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            store=core.Store(Path(temp)/'app')
            try:
                store.product_builder({product_builder.REPOSITORY:product_builder.FAST_PROFILE})
                job=store.create({'repository':product_builder.REPOSITORY,'request_id':'fixture-request','goal':'fixture'})
                frozen=product_builder.resolve(store.settings(),job['repository'])
                store.update(job['id'],settings=frozen,native_lineage={'fixture':True},state='waiting_provider',phase='reviewing')
                before=store.get(job['id'])
                settings=store.product_builder({product_builder.REPOSITORY:product_builder.PROFILE})
                self.assertEqual(settings['roles'],common.DEFAULTS['roles'])
                self.assertEqual(product_builder.resolve(settings,job['repository'])['roles']['builder'],product_builder.PROFILE)
                self.assertEqual(store.get(job['id']),before)
                with self.assertRaisesRegex(common.AppError,'OWNER_PROFILE_IMMUTABLE'):
                    store.action(job['id'],'reconfigure',{})
                self.assertEqual(store.get(job['id']),before)
            finally:store.close()

    def test_only_exact_two_profiles_are_accepted_and_results_are_copied(self):
        for profile in (product_builder.PROFILE,product_builder.FAST_PROFILE):
            for field,value in (('provider','glm'),('model','gpt-6-sol'),('reasoning_effort','low'),
                                ('service_tier','priority'),('service_tier','auto'),('service_tier',None),('extra',True)):
                candidate={**profile,field:value}
                with self.subTest(field=field,value=value),self.assertRaises(common.AppError):
                    product_builder.validate({product_builder.REPOSITORY:candidate})
                with self.assertRaises(common.AppError):product_builder.options(candidate)
            original={product_builder.REPOSITORY:copy.deepcopy(profile)}
            selected=product_builder.validate(original)
            selected[product_builder.REPOSITORY]['service_tier']='changed'
            self.assertEqual(original[product_builder.REPOSITORY],profile)
        self.assertEqual(product_builder.options({'provider':'codex','model':'gpt-6-astra'}),{})


class BundleDecisionTests(unittest.TestCase):
    def test_pinned_approval_body_actor_and_time_are_required(self):
        root=Path(__file__).resolve().parents[1]/'docs'
        meta=common.read_json(root/'MAC_KIX_BUNDLE_DECISION_20261007_RECORD.json')
        value={k:meta[k] for k in ('id','html_url','issue_url','created_at','updated_at')}
        value['user']=meta['actor'];value['body']=(root/'MAC_KIX_BUNDLE_DECISION_20261007.md').read_text()
        with patch('handoff.api',return_value=value):self.assertEqual(mac_bundle.decision(),mac_bundle.DECISION)
        for change in ({'body':value['body']+'edited'},{'updated_at':'changed'},
                       {'user':{'id':1,'login':'BeautifulMind-JT','type':'User'}},{'html_url':value['html_url'].replace('/86#','/85#')}):
            with patch('handoff.api',return_value={**value,**change}):
                with self.assertRaisesRegex(common.AppError,'DECISION_UNVERIFIED'):mac_bundle.decision()


class NativeTuningTests(unittest.TestCase):
    profile = product_builder.PROFILE
    def setUp(self):
        import test_control_plane_mac_codex_native_exec as fixture
        import codex_native_exec as native
        fixture.Fixture.setUp(self)
        self.request['profile']=copy.deepcopy(self.profile)
        self.driver=native.Driver(self.request,self.folder)
    def client(self):
        import test_control_plane_mac_codex_native_exec as fixture
        client=fixture.PreflightClient(self.driver,self.config)
        client.read['config'].update(product_builder.options(self.request['profile']))
        rpc=client.rpc
        client.models={'data':[{'model':'gpt-6-astra','supportedReasoningEfforts':[{'reasoningEffort':'high'}],
                              'serviceTiers':[{'id':'priority','name':'Fast'},{'id':'default','name':'Standard'}]}]}
        def call(method,params,timeout=20):
            if method=='model/list':client.calls.append(method);return client.models
            return rpc(method,params,timeout)
        client.rpc=call
        return client
    def preflight(self,client):
        import test_control_plane_mac_codex_native_exec as fixture
        fixture.NativePreflightTests.preflight(self,client)
    def test_actual_config_and_capabilities_bound_before_model_call(self):
        client=self.client();before=self.config.read_bytes();self.preflight(client)
        self.assertEqual(self.driver.proof['builder_options_verified'],product_builder.options(self.profile))
        self.assertEqual(self.driver.values['service_tier'],product_builder.options(self.profile)['service_tier'])
        self.assertFalse(self.driver.proof['model_turn_requested']);self.assertEqual(self.config.read_bytes(),before)
        self.assertNotIn('thread/start',client.calls)
    def test_missing_effort_or_tier_or_wrong_effective_config_fails_closed(self):
        for field in ('supportedReasoningEfforts','serviceTiers','model'):
            client=self.client();client.models['data'][0][field]=[] if field!='model' else 'another-model'
            with self.assertRaises(common.AppError):self.preflight(client)
            self.assertNotIn('account/read',client.calls)
        wrong = 'priority' if self.profile['service_tier']=='default' else 'default'
        for value in (wrong, None, 'auto', ''):
            client=self.client();client.read['config']['service_tier']=value
            with self.subTest(value=value),self.assertRaises(common.AppError):self.preflight(client)
            self.assertNotIn('model/list',client.calls)
        client=self.client();del client.read['config']['service_tier']
        with self.assertRaises(common.AppError):self.preflight(client)
        self.assertNotIn('model/list',client.calls)
    def test_other_service_tier_cannot_substitute_for_requested_tier(self):
        client=self.client()
        wanted=product_builder.options(self.profile)['service_tier']
        client.models['data'][0]['serviceTiers']=[t for t in client.models['data'][0]['serviceTiers'] if t['id']!=wanted]
        with self.assertRaises(common.AppError):self.preflight(client)
        self.assertNotIn('account/read',client.calls)


class LegacyFastNativeTuningTests(NativeTuningTests):
    profile = product_builder.FAST_PROFILE


if __name__=='__main__':unittest.main()
