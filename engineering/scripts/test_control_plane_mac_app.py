"""Offline Mac app tests. No provider, GitHub write, merge or deployment is called."""
import copy
import http.client
import importlib.util
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import agents
import aiops
import common
import core
import gitops
import worker


def plan():
    return {'summary': 'Deliver the repository requirements.', 'sources': ['README.md'],
            'tasks': [{'id': 'feature', 'title': 'Feature', 'instructions': 'Implement the approved feature.',
                       'acceptance': ['Test passes and specified output exists.'], 'depends_on': []}]}


def report(job, role, status='complete', **changes):
    value = {'status': status, 'summary': 'Completed ' + role, 'question': '',
             'plan': plan() if role == 'planner' else None, 'findings': [], 'checks': ['Verified actual test output.'],
             'reviewed_head': job['head'] or '', 'covered_tasks': ['feature'] if role in ('reviewer', 'supervisor') else []}
    value.update(changes); return value


class FakeRepos:
    def __init__(self, directory):
        self.directory = Path(directory); self.sha = 'a' * 40; self.dirty = False
        self.sync = {'changed': False}; self.checked = {'state': 'passed', 'checks': []}; self.publishes = 0
    def path(self, job): return self.directory
    def prepare(self, job): return {'base_sha': self.sha, 'head': self.sha, 'base_branch': 'main'}
    def head(self, job): return self.sha
    def clean(self, job): return not self.dirty
    def assert_binding(self, job): pass
    def program_scope(self, job): return None
    def assert_scope(self, job): pass
    def source_pins(self, job, value): return {'README.md': '1' * 40}
    def checkpoint(self, job): self.sha = 'b' * 40; self.dirty = False; return self.sha
    def synchronize_base(self, job): return self.sync
    def publish(self, job): self.publishes += 1; return 'https://github.com/example/product/pull/1'
    def checks(self, job): return self.checked


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.directory = Path(self.temp.name) / 'private'
        self.store = core.Store(self.directory); self.repos = FakeRepos(Path(self.temp.name))
        self.engine = core.Engine(self.store, self.repos)
        # These are state-machine tests, not desktop notification/power tests.
        # Keep them offline and prevent native subprocesses on the actual Mac.
        self.engine.keep_awake = mock.Mock()
        self.engine.notify = mock.Mock()
    def tearDown(self): self.store.close(); self.temp.cleanup()
    def new(self, repo='example/product', request_id='request-001'):
        return self.store.create({'repository': repo, 'goal': 'Finish repository deliverables', 'request_id': request_id})
    def job(self): return self.store.jobs()[0]
    def launch(self):
        with mock.patch.object(agents, 'command', return_value=['fake']), mock.patch.object(core.subprocess, 'Popen') as popen:
            self.engine.step(self.job())
            self.assertEqual(popen.call_count, 1)
            return self.job()
    def finish(self, value=None, error=None):
        job = self.job(); attempt = job['attempt']
        receipt = {'attempt_id': attempt['id'], 'binding': attempt['binding'], 'exit_code': 0,
                   'report': None if error else value or report(job, attempt['role']), 'error': error, 'process_group_quiescent': True,
                   'provider_evidence': self.evidence(job, attempt['role'])}
        path = self.directory / 'jobs' / job['id'] / attempt['id'] / 'receipt.json'
        common.atomic_json(path, receipt); self.engine.step(job); return self.job()
    def evidence(self, job, role):
        profile = job['settings']['roles'][role]
        return {'provider': profile['provider'], 'harness': agents.CATALOG[profile['provider']]['harness'],
                'model_requested': profile['model'], 'session_id': None}
    def prepared(self):
        self.new(); self.engine.step(self.job()); self.launch(); self.finish()
    def built(self):
        self.prepared(); self.launch(); self.finish()
    def reviewed(self):
        self.built(); self.launch(); self.finish(); self.launch(); self.finish()

    def test_idempotent_start_and_request_conflict(self):
        first = self.new(); second = self.new(); self.assertEqual(first['id'], second['id'])
        with self.assertRaises(common.AppError):
            self.store.create({'repository':'example/product','goal':'different','request_id':'request-001'})
        self.assertEqual(len(self.store.jobs()),1)

    def test_one_repository_owner_including_paused_and_ready(self):
        job = self.new(); self.store.update(job['id'], state='paused')
        with self.assertRaises(common.AppError): self.new('Example/Product', 'request-002')
        self.store.update(job['id'], state='ready')
        with self.assertRaises(common.AppError): self.new('example/product', 'request-002')
        self.store.action(job['id'], 'accept'); self.new('example/product', 'request-002')
        self.assertEqual(len(self.store.jobs()),2)

    def test_concurrent_start_has_one_owner(self):
        result=[]
        def create(rid):
            try: result.append(self.new(request_id=rid)['id'])
            except common.AppError as exc: result.append(exc.code)
        threads=[threading.Thread(target=create,args=(f'request-{n:03}',)) for n in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(self.store.jobs()),1); self.assertIn('REPOSITORY_BUSY',result)

    def test_settings_snapshot_cannot_change_running_models(self):
        job=self.new(); value=self.store.settings();value['roles']['builder']['model']='custom-model'
        self.store.set_settings(value)
        self.assertEqual(self.store.get(job['id'])['settings']['roles']['builder']['model'],'')
        self.assertEqual(self.store.settings()['roles']['builder']['model'],'custom-model')

    def test_full_loop_requires_two_independent_exact_head_reviews_and_user(self):
        self.reviewed(); job=self.job()
        self.assertEqual(job['state'],'publishing');self.assertNotEqual(job['review']['attempt'],job['supervision']['attempt'])
        self.assertEqual(job['review']['head'],job['head']);self.assertEqual(job['supervision']['head'],job['head'])
        self.engine.step(job);self.assertEqual(self.job()['state'],'verifying')
        self.engine.step(self.job());self.assertEqual(self.job()['state'],'ready')
        self.assertEqual(self.repos.publishes,1)
        self.store.action(job['id'],'accept');self.assertEqual(self.job()['state'],'accepted')

    def test_review_failure_returns_findings_to_same_writer(self):
        self.built(); job=self.launch(); branch=job['branch'];profile=job['settings']['roles']['builder']
        self.finish(report(job,'reviewer','fail',findings=['Fix the incorrect retry behavior.']))
        job=self.job();self.assertEqual(job['state'],'building');self.assertTrue(job['correcting'])
        self.assertEqual(job['branch'],branch);self.assertEqual(job['settings']['roles']['builder'],profile)
        self.assertIsNone(job['review']);self.assertIn('retry',job['feedback'][0])
        self.launch();self.finish();self.assertEqual(self.job()['state'],'reviewing')

    def test_final_inspector_failure_clears_all_old_passes(self):
        self.built();self.launch();self.finish();job=self.launch()
        self.finish(report(job,'supervisor','fail',findings=['Missing an original deliverable.']))
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review']);self.assertIsNone(self.job()['supervision'])

    def test_stale_review_cannot_pass(self):
        self.built();job=self.launch();self.finish(report(job,'reviewer',reviewed_head='0'*40))
        self.assertEqual(self.job()['state'],'reviewing');self.assertIsNone(self.job()['review'])

    def test_omitted_tasks_or_missing_evidence_cannot_pass(self):
        for changes in ({'covered_tasks':[]},{'checks':[]},{'findings':['unresolved']}):
            with self.subTest(changes=changes):
                self.store.db.execute('DELETE FROM jobs');self.store.db.execute('DELETE FROM events')
                self.built();job=self.launch();self.finish(report(job,'reviewer',**changes))
                self.assertEqual(self.job()['state'],'building' if 'checks' in changes or 'findings' in changes else 'reviewing');self.assertIsNone(self.job()['review'])

    def test_read_only_role_modification_fences_attempt(self):
        self.built();self.launch();self.repos.dirty=True
        with self.assertRaisesRegex(common.AppError,'READ_ONLY_ROLE_MODIFIED'):self.finish()
        self.assertIsNotNone(self.job()['attempt'])

    def test_failed_read_only_provider_cannot_release_a_modified_checkout(self):
        self.built(); self.launch(); self.repos.dirty = True
        with self.assertRaisesRegex(common.AppError, 'READ_ONLY_ROLE_MODIFIED'):
            self.finish(error='PROVIDER_LOGIN_REQUIRED')
        self.assertIsNotNone(self.job()['attempt'])

    def test_provider_evidence_cannot_claim_another_harness_or_model(self):
        self.built(); job = self.launch(); attempt = job['attempt']
        path = self.directory / 'jobs' / job['id'] / attempt['id'] / 'receipt.json'
        for evidence in ({'provider': 'cursor', 'harness': 'CURSOR_CLI', 'model_requested': ''},
                         {'provider': 'codex', 'harness': 'GROK_BUILD_CLI', 'model_requested': ''},
                         {'provider': 'codex', 'harness': 'CODEX_CLI', 'model_requested': 'other'}, []):
            common.atomic_json(path, {'attempt_id': attempt['id'], 'binding': attempt['binding'], 'error': None, 'exit_code': 0,
                'report': report(job, 'reviewer'), 'process_group_quiescent': True, 'provider_evidence': evidence})
            with self.subTest(evidence=evidence), self.assertRaisesRegex(common.AppError, 'PROVIDER_PROFILE_MISMATCH'):
                self.engine.step(job)
            self.assertIsNotNone(self.job()['attempt'])

    def test_user_question_keeps_scope_and_resumes_only_with_answer(self):
        self.prepared();job=self.launch();self.finish(report(job,'builder','needs_user',question='Choose the required external account.'))
        self.assertEqual(self.job()['state'],'needs_user')
        with self.assertRaises(common.AppError):self.store.action(job['id'],'resume',{})
        self.store.action(job['id'],'resume',{'answer':'Use the previously linked account.'})
        self.assertEqual(self.job()['state'],'building');self.assertEqual(len(self.job()['user_answers']),1)

    def test_pause_waits_for_running_worker_and_does_not_cancel_it(self):
        self.prepared();job=self.launch();self.store.action(job['id'],'pause')
        self.assertIsNotNone(self.job()['attempt']);self.finish();self.engine.step(self.job())
        self.assertEqual(self.job()['state'],'paused')
        self.store.action(job['id'],'resume');self.assertEqual(self.job()['state'],'reviewing')

    def test_pause_during_preparation_cannot_release_or_reconfigure_owner(self):
        job=self.new();started=threading.Event();release=threading.Event()
        def prepare(_):
            started.set();release.wait(timeout=4)
            return {'base_sha':'a'*40,'head':'a'*40,'base_branch':'main'}
        with mock.patch.object(self.repos,'prepare',side_effect=prepare):
            thread=threading.Thread(target=self.engine.tick);thread.start();self.assertTrue(started.wait(timeout=3))
            self.store.action(job['id'],'pause')
            with self.assertRaisesRegex(common.AppError,'현재 단계'):self.store.action(job['id'],'reconfigure')
            with self.assertRaisesRegex(common.AppError,'현재 단계'):self.store.action(job['id'],'cancel')
            release.set();thread.join(timeout=5)
        self.engine.tick();self.assertEqual(self.job()['state'],'paused');self.assertIsNone(self.job()['attempt'])

    def test_provider_failure_backoff_preserves_model_and_task(self):
        self.prepared();job=self.launch();self.finish(error='PROVIDER_EXIT_1')
        current=self.job();self.assertEqual(current['state'],'waiting_provider')
        self.assertGreater(current['not_before'],time.time()+200)
        self.assertEqual(current['settings'],job['settings']);self.assertIsNone(current['attempt'])

    def test_invalid_model_requires_one_explicit_configuration_change(self):
        self.prepared();self.launch();self.finish(error='MODEL_UNAVAILABLE')
        self.assertEqual(self.job()['state'],'needs_user')
        settings=self.store.settings();settings['roles']['builder']['model']='valid-model';self.store.set_settings(settings)
        self.store.action(self.job()['id'],'reconfigure')
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['question'])
        self.assertEqual(self.job()['settings']['roles']['builder']['model'],'valid-model')

    def test_model_change_cannot_clear_contract_question_or_live_attempt(self):
        self.prepared();job=self.launch()
        with self.assertRaises(common.AppError):self.store.action(job['id'],'reconfigure')
        self.finish(report(job,'builder','needs_user',question='Contract decision required.'))
        self.store.action(job['id'],'reconfigure')
        self.assertEqual(self.job()['state'],'needs_user');self.assertEqual(self.job()['question'],'Contract decision required.')

    def test_uncertain_execution_is_never_relaunched(self):
        self.prepared();self.launch();attempt=self.job()['attempt'];attempt['started']=0
        self.store.update(self.job()['id'],attempt=attempt)
        with mock.patch.object(core.subprocess,'Popen') as popen:self.engine.tick();self.engine.tick()
        self.assertEqual(self.job()['state'],'unknown');popen.assert_not_called()
        with self.assertRaises(common.AppError):self.store.action(self.job()['id'],'resume')

    def test_restart_consumes_existing_receipt_once(self):
        self.prepared();job=self.launch();attempt=job['attempt']
        path=self.directory/'jobs'/job['id']/attempt['id']/'receipt.json'
        common.atomic_json(path,{'attempt_id':attempt['id'],'binding':attempt['binding'],'exit_code':0,'error':None,'report':report(job,'builder'),'process_group_quiescent':True,'provider_evidence':self.evidence(job,'builder')})
        self.store.close();self.store=core.Store(self.directory);self.engine=core.Engine(self.store,self.repos)
        with mock.patch.object(core.subprocess,'Popen') as popen:self.engine.step(self.job())
        popen.assert_not_called();self.assertEqual(self.job()['state'],'reviewing');self.assertEqual(self.job()['calls'],2)

    def test_unknown_writer_blocks_other_repositories(self):
        self.prepared();self.launch();first=self.job();self.store.update(first['id'],state='unknown')
        second=self.new('example/second','request-002')
        with mock.patch.object(self.engine,'step') as step:self.engine.tick()
        step.assert_not_called();self.assertEqual(self.store.get(second['id'])['state'],'queued')

    def test_mismatched_receipt_never_advances(self):
        self.prepared();job=self.launch();attempt=job['attempt']
        path=self.directory/'jobs'/job['id']/attempt['id']/'receipt.json'
        common.atomic_json(path,{'attempt_id':attempt['id'],'binding':'wrong','report':report(job,'builder'),'error':None,'exit_code':0,'process_group_quiescent':True})
        with self.assertRaisesRegex(common.AppError,'RECEIPT_BINDING_MISMATCH'):self.engine.step(job)

    def test_ci_failure_returns_to_builder_not_ready(self):
        self.reviewed();self.engine.step(self.job());self.repos.checked={'state':'failed','checks':[{'name':'tests','status':'FAILURE'}]}
        self.engine.step(self.job());self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])

    def test_new_base_requires_fresh_reviews(self):
        self.reviewed();self.repos.sync={'changed':True,'base':'c'*40,'conflicts':False}
        self.engine.step(self.job());self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])
        self.assertEqual(self.repos.publishes,0)

    def test_local_mutation_after_inspection_requires_fresh_reviews(self):
        self.reviewed();self.repos.dirty=True;self.engine.step(self.job())
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])
        self.assertEqual(self.repos.publishes,0)

    def test_final_user_rework_reopens_entire_verification(self):
        self.reviewed();self.engine.step(self.job());self.engine.step(self.job());job=self.job()
        self.store.action(job['id'],'revise',{'feedback':'Improve the empty state.'})
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])

    def test_cap_stops_before_another_model_call(self):
        self.prepared();job=self.job();settings=job['settings'];settings['max_agent_calls']=job['calls']
        self.store.update(job['id'],settings=settings)
        with mock.patch.object(core.subprocess,'Popen') as popen:self.engine.step(self.job())
        popen.assert_not_called();self.assertEqual(self.job()['state'],'paused')

    def test_increased_budget_resumes_without_changing_running_model(self):
        self.prepared();job=self.job();settings=job['settings'];settings['max_agent_calls']=job['calls']
        self.store.update(job['id'],settings=settings,state='paused')
        new=self.store.settings();new['max_agent_calls']=10;new['roles']['builder']['model']='new-next-job-model'
        self.store.set_settings(new);self.store.action(job['id'],'resume')
        self.assertEqual(self.job()['settings']['max_agent_calls'],10)
        self.assertEqual(self.job()['settings']['roles']['builder']['model'],'')

    def test_nonquiescent_terminal_receipt_cannot_free_writer(self):
        self.prepared();job=self.launch();attempt=job['attempt']
        path=self.directory/'jobs'/job['id']/attempt['id']/'receipt.json'
        common.atomic_json(path,{'attempt_id':attempt['id'],'binding':attempt['binding'],'exit_code':0,'error':None,
                                 'report':report(job,'builder'),'process_group_quiescent':False})
        with self.assertRaisesRegex(common.AppError,'종료 상태'):self.engine.step(job)
        self.assertIsNotNone(self.job()['attempt'])

    def terminal(self, job, **changes):
        attempt = job['attempt']
        receipt = {'attempt_id': attempt['id'], 'binding': attempt['binding'], 'exit_code': 0,
                   'report': report(job, attempt['role']), 'error': None, 'process_group_quiescent': True,
                   'provider_evidence': self.evidence(job, attempt['role'])}
        receipt.update(changes)
        common.atomic_json(self.engine.receipt_path(job), receipt)

    def test_late_terminal_receipt_releases_unknown_once_without_launch(self):
        self.prepared(); job = self.launch(); attempt = dict(job['attempt'], started=0)
        self.store.update(job['id'], attempt=attempt)
        with mock.patch.object(self.engine, 'notify') as notify:
            self.engine.tick(); self.engine.tick(); notify.assert_called_once()
        self.assertEqual(self.job()['state'], 'unknown')
        self.terminal(self.job())
        with mock.patch.object(core.subprocess, 'Popen') as popen:
            self.assertTrue(self.engine.tick()); popen.assert_not_called()
        current = self.job(); self.assertEqual(current['state'], 'reviewing')
        self.assertIsNone(current['attempt']); self.assertIsNone(current['blocker'])
        self.assertEqual(current['calls'], job['calls'])
        self.assertEqual(len([e for e in self.store.events(job['id']) if e['kind']=='result']), 2)

    def test_late_bad_or_live_receipt_keeps_global_fence(self):
        self.prepared(); job = self.launch(); self.store.update(job['id'], state='unknown')
        self.new('example/other', 'request-002')
        for changes in ({'binding': 'wrong'}, {'process_group_quiescent': False}):
            current = self.store.get(job['id']); self.terminal(current, **changes)
            with mock.patch.object(core.subprocess, 'Popen') as popen: self.engine.tick()
            popen.assert_not_called(); self.assertIsNotNone(self.store.get(job['id'])['attempt'])
            self.assertEqual(self.store.get(job['id'])['state'], 'unknown')

    def test_conflicting_error_and_success_or_nonzero_exit_cannot_advance(self):
        self.prepared(); job = self.launch()
        for changes in ({'error': 'PROVIDER_USAGE_LIMIT'}, {'exit_code': 1}, {'exit_code': True}, {'error': ''}):
            self.terminal(job, **changes)
            with self.subTest(changes=changes), mock.patch.object(core.subprocess, 'Popen') as popen:
                self.engine.tick(); popen.assert_not_called()
            current = self.job(); self.assertEqual(current['state'], 'unknown'); self.assertIsNotNone(current['attempt'])
            self.assertEqual(current['task_index'], 0); self.assertFalse(current['built_tasks'])

    def test_missing_provider_profile_or_malformed_receipt_fences(self):
        self.prepared(); job = self.launch()
        self.terminal(job, provider_evidence=None); self.engine.tick()
        self.assertEqual(self.job()['blocker']['code'], 'PROVIDER_PROFILE_MISMATCH')
        common.atomic_json(self.engine.receipt_path(job), [])
        self.engine.tick(); self.assertEqual(self.job()['blocker']['code'], 'INVALID_TERMINAL_RECEIPT')
        self.assertIsNotNone(self.job()['attempt'])

    def test_incomplete_builder_success_returns_to_same_task(self):
        for changes in ({'findings':['Incorrect invariant.']}, {'checks':[]}, {'checks':['  ']}, {'question':'Required approval?'}):
            self.store.db.execute('DELETE FROM jobs'); self.store.db.execute('DELETE FROM events')
            self.prepared(); job = self.launch()
            current = self.finish(report(job, 'builder', **changes))
            self.assertEqual(current['state'], 'building'); self.assertEqual(current['task_index'], 0)
            self.assertFalse(current['built_tasks']); self.assertEqual(current['settings'], job['settings'])
            self.assertTrue(current['feedback']); self.assertIsNone(current['attempt'])
            self.assertEqual(current['last_terminal']['status'],'fail')

    def test_identical_engineering_failures_backoff_without_user_cutoff(self):
        self.prepared(); delays=[]
        for _ in range(5):
            job=self.launch(); self.finish(report(job, 'builder', 'fail', findings=['Fix the same defect.']))
            current=self.job(); delays.append(current['not_before']-time.time())
            self.assertEqual(current['state'], 'building'); self.assertIsNone(current['question'])
            self.assertEqual(current['settings'], job['settings'])
        self.assertGreater(delays[-1], delays[0]*8)
        self.repos.sha='c'*40; job=self.launch(); self.finish(report(job, 'builder', 'fail', findings=['Fix the same defect.']))
        self.assertEqual(self.job()['feedback_repeats'], 0)

    def test_permanent_infrastructure_failure_stops_once_without_model_change(self):
        for error in ('PROVIDER_PERMISSION_REQUIRED', 'PROVIDER_USAGE_LIMIT'):
            self.store.db.execute('DELETE FROM jobs'); self.store.db.execute('DELETE FROM events')
            self.prepared(); job=self.launch()
            with mock.patch.object(self.engine,'notify') as notify:
                self.finish(error=error); self.engine.tick(); self.engine.tick(); notify.assert_called_once()
            current=self.job(); self.assertEqual(current['state'],'needs_user')
            self.assertEqual(current['provider_error'],error); self.assertEqual(current['settings'],job['settings'])
            self.store.action(job['id'],'resume',{})
            self.assertEqual(self.job()['state'],'building'); self.assertEqual(self.job()['settings'],job['settings'])

    def test_repeated_unknown_operational_error_holds_after_three_verified_exits(self):
        self.prepared()
        for count in range(1,4):
            self.launch(); current=self.finish(error='PROVIDER_EXIT_1')
            self.assertEqual(current['failures'],count)
            self.assertEqual(current['state'],'needs_user' if count==3 else 'waiting_provider')
        self.assertIsNone(current['attempt']); self.assertFalse(current['built_tasks'])

    def test_explicit_temporary_outage_retries_automatically_with_bounded_delay(self):
        self.prepared()
        for _ in range(5):
            job=self.launch(); current=self.finish(error='PROVIDER_TEMPORARILY_UNAVAILABLE')
            self.assertEqual(current['state'],'waiting_provider'); self.assertIsNone(current['question'])
            self.assertLessEqual(current['not_before']-time.time(),3600)
            self.assertEqual(current['settings'],job['settings'])

    def test_health_exposes_observation_and_deadline_without_log_contents(self):
        self.prepared(); job=self.launch(); folder=self.engine.receipt_path(job).parent
        (folder/'stdout.log').write_text('SECRET not a completion receipt')
        current=self.engine.describe(job); health=current['health']
        self.assertEqual(health['status'],'running'); self.assertGreater(health['output_bytes'],0)
        self.assertEqual(health['deadline_at'],job['attempt']['started']+job['attempt']['timeout_seconds'])
        self.assertNotIn('SECRET',common.encoded(current)); self.assertEqual(health['completion_kind'],'draft_delivery')
        (folder/'stdout.log').unlink(); self.assertEqual(self.engine.describe(job)['health']['status'],'quiet')

    def test_waiting_job_names_the_unknown_owner_without_starting(self):
        self.prepared(); job=self.launch(); self.store.update(job['id'],state='unknown')
        other=self.new('example/other','request-002'); health=self.engine.describe(other)['health']
        self.assertEqual(health['status'],'waiting_for_owner'); self.assertEqual(health['blocking_job_id'],job['id'])
        self.assertEqual(self.store.get(other['id'])['state'],'queued')

    def test_global_unknown_owner_takes_priority_over_retry_countdown(self):
        first=self.new(); self.store.update(first['id'],state='waiting_provider',not_before=time.time()+600)
        other=self.new('example/other','request-002')
        self.store.update(other['id'],state='unknown',attempt={'id':'unconfirmed','role':'builder','started':time.time(),'timeout_seconds':600})
        health=self.engine.describe(self.store.get(first['id']))['health']
        self.assertEqual(health['status'],'waiting_for_owner');self.assertIsNone(health['retry_at'])

    def test_ci_window_mutation_cannot_become_ready(self):
        self.reviewed(); self.engine.step(self.job()); self.repos.dirty=True
        self.engine.step(self.job()); self.assertEqual(self.job()['state'],'building')
        self.assertIsNone(self.job()['review']); self.assertIsNone(self.job()['supervision'])

    def test_failed_latest_ci_or_changed_head_cannot_be_accepted(self):
        self.reviewed(); self.engine.step(self.job()); self.engine.step(self.job()); job=self.job()
        self.repos.checked={'state':'failed','checks':[{'name':'tests','status':'FAILURE'}]}
        with self.assertRaisesRegex(common.AppError,'최신 CI'):self.engine.validate_acceptance(job)
        self.assertEqual(self.job()['state'],'building'); self.assertIsNone(self.job()['review'])

    def test_changed_base_during_ci_invalidates_all_review_evidence(self):
        self.reviewed(); self.engine.step(self.job()); self.repos.sync={'changed':True,'base':'c'*40}
        self.engine.step(self.job()); self.assertEqual(self.job()['state'],'building')
        self.assertIsNone(self.job()['review']); self.assertIsNone(self.job()['supervision'])

    def test_changed_base_after_ready_cannot_be_accepted(self):
        self.reviewed(); self.engine.step(self.job()); self.engine.step(self.job())
        self.repos.sync={'changed':True,'base':'c'*40}
        with self.assertRaisesRegex(common.AppError,'기준 브랜치'):self.engine.validate_acceptance(self.job())
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['supervision'])

    def test_program_scope_is_loaded_before_plan_and_cannot_be_omitted(self):
        import program_scope
        manifest={'schema_version':1,'program':'sample','repository':'example/product',
                  'approval_pointer':'https://github.com/example/product/issues/1','authoritative_doc_pointers':'README.md',
                  'nodes':[{'id':'001','title':'Original scope','spec':'Keep the complete original specification.'}]}
        scope=program_scope.load_scope(manifest,'example/product','1'*40)
        with mock.patch.object(self.repos,'program_scope',return_value=scope):
            self.new(); self.engine.step(self.job())
        self.assertEqual(self.job()['program_scope']['node_ids'],['001'])
        self.launch(); current=self.finish()
        self.assertEqual(current['state'],'planning'); self.assertIsNone(current['plan'])
        self.assertIn('고정된 프로그램',current['feedback'][0])
        approved={'summary':'Full program','sources':['README.md','.aiops/program.json'],
                  'tasks':[{'id':'001','title':'Original scope','instructions':manifest['nodes'][0]['spec'],
                            'acceptance':['Actual evidence'], 'depends_on':[]}]}
        job=self.launch(); current=self.finish(report(job,'planner',plan=approved))
        self.assertEqual(current['state'],'building'); self.assertEqual(current['plan']['tasks'][0]['id'],'001')


class ContractTests(unittest.TestCase):
    def test_validates_repository_and_injection(self):
        self.assertEqual(common.repository('https://github.com/owner/repo.git'),'owner/repo')
        for value in ('file:///tmp/repo','x/y;touch /tmp/x','x/../../y','-x/y',[],None):
            with self.subTest(value=value),self.assertRaises(common.AppError):common.repository(value)

    def test_invalid_settings_do_not_enable_shell_or_other_provider(self):
        for changes in ({'provider':'shell','model':'x'},{'provider':'codex','model':'x\n--dangerous'},{'provider':'claude','model':'--flag'}):
            value=copy.deepcopy(common.DEFAULTS);value['roles']['builder']=changes
            with self.assertRaises(common.AppError):common.validate_settings(value)

    def test_plan_rejects_cycles_missing_acceptance_and_external_sources(self):
        for change in ('cycle','empty','source'):
            value=plan()
            if change=='cycle':value['tasks'][0]['depends_on']=['feature']
            elif change=='empty':value['tasks'][0]['acceptance']=[]
            else:value['sources']=['../../secret']
            with self.assertRaises(common.AppError):common.validate_plan(value)

    def test_duplicate_keys_and_nonfinite_json_refused(self):
        for value in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'):
            with self.assertRaises(common.AppError):common.parse_json(value)

    def test_provider_roles_have_fixed_commands_and_separate_read_only_sessions(self):
        with mock.patch.object(agents.shutil,'which',side_effect=lambda name:'/bin/'+name):
            for role in common.ROLES:
                argv=agents.command({'provider':'codex','model':'custom-model'},role,Path('/tmp/attempt'))
                self.assertIn('--ephemeral',argv);self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',argv)
                self.assertIn('workspace-write' if role=='builder' else 'read-only',argv)
                claude=agents.command({'provider':'claude','model':'my-model'},role,Path('/tmp/attempt'))
                self.assertIn('--bare',claude);self.assertNotIn('bypassPermissions',claude)
                if role!='builder':self.assertEqual(claude[claude.index('--tools')+1],'Read,Glob,Grep')
                self.assertFalse(json.loads(claude[claude.index('--settings')+1])['sandbox']['allowUnsandboxedCommands'])

    def test_tokens_and_api_fallback_are_not_in_worker_environment(self):
        with mock.patch.dict(os.environ,{'GH_TOKEN':'secret','OPENAI_API_KEY':'secret','ANTHROPIC_API_KEY':'secret','AWS_SECRET_ACCESS_KEY':'secret','PYTHONPATH':'bad'}):
            value=agents.environment()
        for key in ('GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','PYTHONPATH'):self.assertNotIn(key,value)

    def test_double_worker_claim_cannot_start_provider(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);common.atomic_json(folder/'request.json',{})
            (folder/'claimed').write_text('old')
            with mock.patch.object(worker.subprocess,'Popen') as popen,self.assertRaises(FileExistsError):worker.run(folder)
            popen.assert_not_called()

    def test_real_worker_subprocess_writes_terminal_bound_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);attempt=folder/'attempt';attempt.mkdir();executable=folder/'codex'
            output=report({'head':'a'*40},'reviewer')
            script='#!'+sys.executable+'\nimport json,sys,os\nfrom pathlib import Path\n'
            script+="assert 'GH_TOKEN' not in os.environ\n"
            script+="sys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1]).write_text("+repr(json.dumps(output))+')\n'
            executable.write_text(script);executable.chmod(0o755)
            common.atomic_json(attempt/'schema.json',agents.SCHEMA)
            common.atomic_json(attempt/'request.json',{'attempt_id':'one','binding':'binding','profile':{'provider':'codex','model':'custom'},
                'role':'reviewer','checkout':str(folder),'prompt':'review','timeout_seconds':10})
            with mock.patch.dict(os.environ,{'PATH':str(folder)+os.pathsep+os.environ['PATH'],'GH_TOKEN':'not-a-real-token'}):
                worker.run(attempt)
            receipt=common.read_json(attempt/'receipt.json')
            self.assertIsNone(receipt['error']);self.assertEqual(receipt['report'],output)
            self.assertEqual(receipt['binding'],'binding');self.assertTrue(receipt['process_group_quiescent'])

    def test_terminal_cli_error_classification_never_exposes_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            for message,code in [('model x does not exist TOKEN','MODEL_UNAVAILABLE'),('Please log in TOKEN','PROVIDER_LOGIN_REQUIRED'),('Unexpected argument --schema TOKEN','CLI_SETUP_REQUIRED'),('429 quota exceeded TOKEN','PROVIDER_USAGE_LIMIT')]:
                (Path(d)/'stderr.log').write_text(message)
                self.assertEqual(worker.failure_code(d,1),code)

    def test_service_has_single_process_lock(self):
        with tempfile.TemporaryDirectory() as d:
            fd=core.service_lock(d)
            try:
                with self.assertRaises(common.AppError):core.service_lock(d)
            finally:os.close(fd)

    def test_symlink_state_and_world_readable_token_refused(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);target=folder/'target';target.write_text('a'*40);target.chmod(0o600)
            link=folder/'link';link.symlink_to(target)
            with self.assertRaises(common.AppError):aiops.token(link)
            target.chmod(0o644)
            with self.assertRaises(common.AppError):aiops.token(target)


class GitTests(unittest.TestCase):
    def test_repo_metadata_uses_positional_repository_and_prs_keep_repo_flag(self):
        with mock.patch.object(gitops, 'execute', return_value='{}') as run:
            gitops.gh('example/product', 'repo', 'view', '--json', 'defaultBranchRef,isArchived')
            run.assert_called_once_with(['gh', 'repo', 'view', 'example/product', '--json', 'defaultBranchRef,isArchived'])
            run.reset_mock()
            gitops.gh('example/product', 'pr', 'view', 'aiops/test', '--json', 'headRefOid')
            run.assert_called_once_with(['gh', 'pr', 'view', 'aiops/test', '--json', 'headRefOid', '--repo', 'example/product'])

    def test_real_git_scope_stays_bound_after_an_agent_commits_a_changed_manifest(self):
        import program_scope
        with tempfile.TemporaryDirectory() as d:
            repos=gitops.Repositories(d);job={'id':'scope','repository':'example/product','branch':'aiops/scope','current_task':{'title':'Feature'}}
            checkout=repos.path(job);checkout.mkdir()
            gitops.git(checkout,'init','-b',job['branch']);gitops.git(checkout,'config','user.name','Test');gitops.git(checkout,'config','user.email','test@example.invalid')
            gitops.git(checkout,'remote','add','origin','https://github.com/example/product.git')
            (checkout/'.aiops').mkdir()
            value={'schema_version':1,'program':'test','repository':job['repository'],
                   'approval_pointer':'https://github.com/example/product/issues/1','authoritative_doc_pointers':'README.md',
                   'nodes':[{'id':'001','title':'Feature','spec':'Original required output.'}]}
            target=checkout/program_scope.PATH;target.write_text(json.dumps(value));(checkout/'README.md').write_text('spec')
            gitops.git(checkout,'add','.');gitops.git(checkout,'commit','-m','baseline');job['base_sha']=repos.head(job)
            scope=repos.program_scope(job);job['program_scope']=scope
            self.assertEqual(scope['node_ids'],['001']);self.assertEqual(scope['blob'],gitops.git(checkout,'rev-parse','HEAD:.aiops/program.json'))
            repos.assert_scope(job)
            value['nodes'][0]['spec']='Weakened output.';target.write_text(json.dumps(value))
            gitops.git(checkout,'add','.');gitops.git(checkout,'commit','-m','unauthorized committed scope edit')
            with self.assertRaisesRegex(common.AppError,'원래 프로그램'):repos.assert_scope(job)
            with self.assertRaisesRegex(common.AppError,'원래 프로그램'):repos.checkpoint(job)

    def test_already_committed_authority_edits_cannot_bypass_checkpoint_guard(self):
        with tempfile.TemporaryDirectory() as d:
            repos=gitops.Repositories(d);job={'id':'scope','repository':'example/product','branch':'aiops/scope','current_task':{'title':'Feature'}}
            checkout=repos.path(job);checkout.mkdir()
            gitops.git(checkout,'init','-b',job['branch']);gitops.git(checkout,'config','user.name','Test');gitops.git(checkout,'config','user.email','test@example.invalid')
            gitops.git(checkout,'remote','add','origin','https://github.com/example/product.git')
            target=checkout/'AGENTS.md';target.write_text('Original authority')
            gitops.git(checkout,'add','.');gitops.git(checkout,'commit','-m','baseline');job['base_sha']=repos.head(job)
            target.write_text('Weakened authority');gitops.git(checkout,'add','.');gitops.git(checkout,'commit','-m','authority edit')
            self.assertTrue(repos.clean(job))
            with self.assertRaisesRegex(common.AppError,'기준 계약'):repos.checkpoint(job)

    def test_missing_repo_and_expired_login_are_actionable_without_leaking_stderr(self):
        for stderr,code in [('repository not found token-secret','REPOSITORY_ACCESS_REQUIRED'),('HTTP 401 token-secret','GITHUB_LOGIN_REQUIRED')]:
            result=subprocess.CompletedProcess(['gh'],1,'',stderr)
            with mock.patch.object(gitops.subprocess,'run',return_value=result):
                with self.assertRaises(common.AppError) as caught:gitops.execute(['gh','repo','view'])
            self.assertEqual(caught.exception.code,code);self.assertNotIn('token-secret',str(caught.exception))

    def test_required_checks_missing_or_skipped_cannot_pass(self):
        with tempfile.TemporaryDirectory() as d:
            repos=gitops.Repositories(d);job={'repository':'BeautifulMind-JT/ZARI','branch':'aiops/x','head':'a'*40}
            data={'headRefOid':job['head'],'state':'OPEN','statusCheckRollup':[]}
            with mock.patch.object(gitops,'gh',return_value=json.dumps(data)):
                self.assertEqual(repos.checks(job)['state'],'pending')
            data['statusCheckRollup']=[{'name':'bridge','status':'COMPLETED','conclusion':'SKIPPED'}]
            with mock.patch.object(gitops,'gh',return_value=json.dumps(data)):
                self.assertEqual(repos.checks(job)['state'],'failed')

    def test_real_git_checkpoint_keeps_branch_and_blocks_credential_files(self):
        with tempfile.TemporaryDirectory() as d:
            repos=gitops.Repositories(d);job={'id':'test','repository':'example/product','branch':'aiops/test','current_task':{'title':'Feature'}}
            checkout=repos.path(job);checkout.mkdir()
            gitops.git(checkout,'init','-b',job['branch']);gitops.git(checkout,'config','user.name','Test');gitops.git(checkout,'config','user.email','test@example.invalid')
            gitops.git(checkout,'remote','add','origin','https://github.com/example/product.git')
            (checkout/'README.md').write_text('spec');gitops.git(checkout,'add','.');gitops.git(checkout,'commit','-m','baseline')
            before=repos.head(job);(checkout/'app.py').write_text('print("done")\n')
            after=repos.checkpoint(job);self.assertNotEqual(before,after);self.assertTrue(repos.clean(job))
            (checkout/'.env').write_text('FAKE=not-a-secret')
            with self.assertRaisesRegex(common.AppError,'자격증명'):repos.checkpoint(job)
            self.assertEqual(repos.head(job),after)


class InstallerTests(unittest.TestCase):
    def test_per_user_app_and_launch_agent_have_literal_space_safe_paths(self):
        spec=importlib.util.spec_from_file_location('mac_installer',APP/'install.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as d:
            test_home=Path(d)/'Test User';test_home.mkdir();destination=test_home/'Applications/AIOPS.app';data=test_home/'Library/Application Support/AIOPS'
            def launchctl(args, **kwargs):
                if args[1]=='print':return subprocess.CompletedProcess(args,113,'','Could not find service local.aiops.mac')
                self.assertEqual(args[1],'bootstrap')
                return subprocess.CompletedProcess(args,0,'','')
            with mock.patch.object(installer.sys,'platform','darwin'),mock.patch.object(installer.Path,'home',return_value=test_home),\
                 mock.patch.object(installer.shutil,'which',return_value='/usr/bin/tool'),mock.patch.object(installer.subprocess,'run',side_effect=launchctl) as run,\
                 mock.patch('sys.stdout',new_callable=io.StringIO):
                installer.install(destination,data)
            config=plistlib.loads((test_home/'Library/LaunchAgents/local.aiops.mac.plist').read_bytes())
            self.assertEqual(config['ProgramArguments'][1],str(destination/'Contents/Resources/aiops.py'))
            self.assertIn(str(data),config['ProgramArguments']);self.assertTrue(config['RunAtLoad'])
            self.assertNotIn('sudo',run.call_args.args[0]);self.assertEqual([call.args[0][1] for call in run.call_args_list],['print','print','bootstrap'])
            launcher=destination/'Contents/MacOS/AIOPS'
            subprocess.run(['bash','-n',str(launcher)],check=True)
            self.assertTrue((destination/'Contents/Resources/ui/app.js').is_file())


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.available={k:{'installed':True,'version':'test'} for k in ('git','gh','codex')}
        self.available['github_authenticated']=True
        with mock.patch.object(agents,'availability',return_value=self.available):
            self.app=aiops.Application(Path(self.temp.name)/'state')
    def tearDown(self):self.app.store.close();self.temp.cleanup()
    def test_new_start_refreshes_authentication_and_does_not_reserve_after_logout(self):
        fresh=dict(self.available,github_authenticated=False)
        with mock.patch.object(agents,'availability',return_value=fresh) as probe:
            with self.assertRaisesRegex(common.AppError,'GitHub 로그인'):
                self.app.start({'repository':'example/product','request_id':'request-001'})
        probe.assert_called_once_with(providers={'codex'},versions=False)
        self.assertFalse(self.app.store.jobs())
    def test_same_request_remains_queryable_when_connection_has_changed(self):
        value={'repository':'example/product','request_id':'request-001'}
        prior=self.app.store.create(value)
        with mock.patch.object(agents,'availability',side_effect=AssertionError('No new admission probe')):
            self.assertEqual(self.app.start(value)['id'],prior['id'])
            with self.assertRaisesRegex(common.AppError,'REQUEST_ID_CONFLICT'):
                self.app.start(dict(value,goal='Different request'))
        self.assertEqual(len(self.app.store.jobs()),1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        available={k:{'installed':True,'version':'test'} for k in ('git','gh','codex','claude')};available['github_authenticated']=True
        with mock.patch.object(agents,'availability',return_value=available):self.app=aiops.Application(Path(self.temp.name)/'state')
        self.server=aiops.ThreadingHTTPServer(('127.0.0.1',0),aiops.handler(self.app,'http://127.0.0.1:0'))
        self.port=self.server.server_port;self.origin='http://127.0.0.1:'+str(self.port)
        self.server.RequestHandlerClass=aiops.handler(self.app,self.origin)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.app.store.close();self.temp.cleanup()
    def request(self,path,method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        raw=json.dumps(body) if body is not None else None
        h={'Content-Type':'application/json'};h.update(headers or {})
        conn.request(method,path,raw,h);response=conn.getresponse();data=response.read();result=(response.status,dict(response.headers),data)
        conn.close();return result
    def test_static_page_is_real_but_private_api_requires_auth(self):
        status,headers,data=self.request('/');self.assertEqual(status,200);self.assertIn('AIOPS'.encode(),data)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(self.request('/api/state')[0],401)
    def test_host_origin_and_cross_origin_preflight_are_rejected(self):
        auth={'Authorization':'Bearer '+self.app.owner_token}
        self.assertEqual(self.request('/api/state',headers={**auth,'Host':'evil.invalid'})[0],400)
        self.assertEqual(self.request('/api/state',headers={**auth,'Origin':'https://evil.invalid'})[0],400)
        self.assertEqual(self.request('/api/jobs','OPTIONS')[0],403)
    def test_owner_session_cookie_is_httponly_and_mutations_need_same_origin(self):
        auth={'Authorization':'Bearer '+self.app.owner_token}
        status,headers,_=self.request('/api/session','POST',{},auth);self.assertEqual(status,200)
        cookie=headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        header={'Cookie':cookie.split(';')[0]}
        self.assertEqual(self.request('/api/state',headers=header)[0],200)
        self.assertEqual(self.request('/api/settings','POST',self.app.store.settings(),header)[0],401)
        header.update(Origin=self.origin,**{'X-AIOPS-Client':'desktop'})
        self.assertEqual(self.request('/api/settings','POST',self.app.store.settings(),header)[0],200)
    def test_bot_start_is_idempotent_but_cannot_accept_or_change_models(self):
        auth={'Authorization':'Bearer '+self.app.relay_token};value={'repository':'example/product','request_id':'request-001'}
        with mock.patch.object(agents,'availability',return_value=self.app.doctor):
            first=self.request('/api/jobs','POST',value,auth);second=self.request('/api/jobs','POST',value,auth)
        self.assertEqual(first[0],201);self.assertEqual(json.loads(first[2])['id'],json.loads(second[2])['id'])
        self.assertEqual(self.request('/api/settings','POST',self.app.store.settings(),auth)[0],409)
        key=json.loads(first[2])['id'];self.app.store.update(key,state='ready')
        self.assertEqual(self.request('/api/jobs/'+key+'/accept','POST',{},auth)[0],409)
        self.assertEqual(self.app.store.get(key)['state'],'ready')
    def test_traversal_route_never_serves_secrets(self):
        auth={'Authorization':'Bearer '+self.app.owner_token}
        self.assertEqual(self.request('/../desktop-token',headers=auth)[0],404)
    def test_mcp_handshake_and_tool_contract(self):
        source=io.StringIO('\n'.join(json.dumps(x) for x in [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'}])+'\n')
        out=io.StringIO();aiops.mcp(self.app.store.directory,source,out);rows=[json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(rows),2);self.assertEqual(rows[0]['result']['protocolVersion'],'2025-06-18')
        names={x['name'] for x in rows[1]['result']['tools']};self.assertEqual(names,{'aiops_start','aiops_status','aiops_list','aiops_pause'})

    def test_mcp_malformed_objects_return_errors_without_crashing(self):
        data='[]\n{"id":1,"method":"tools/call","params":{"arguments":"bad"}}\n'
        output=io.StringIO();aiops.mcp(self.app.store.directory,io.StringIO(data),output)
        rows=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows),2);self.assertTrue(all('error' in row for row in rows))


if __name__=='__main__':unittest.main()
