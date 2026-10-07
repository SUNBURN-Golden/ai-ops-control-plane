"""Offline regressions for history, preparation and provider admission boundaries."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import admission
import common
import core
import gitops
import test_control_plane_mac_app as legacy
import test_control_plane_mac_handoff as handoff_fixture

REPO = 'example/product'


def issue(number=1, state='open', body='A task registration', comments=0):
    return {'number': number, 'state': state, 'body': body, 'comments': comments}


class ObservationTests(unittest.TestCase):
    def observe(self, responses, **job):
        with mock.patch.object(gitops, 'execute', side_effect=[json.dumps(value) for value in responses]) as run:
            result = admission.observe({'repository': REPO, **job}, False, run)
        return result, run

    def test_labels_and_historical_registration_do_not_claim_an_owner(self):
        result, run = self.observe([[[issue(), issue(2, 'closed'), issue(3, body=None)]]])
        self.assertEqual(result, {'mode': 'native', 'registration_count': 3, 'historical_registration_count': 1})
        admission.require_native(result)
        self.assertIn('state=all', run.call_args.args[0][-1])
        self.assertIn('--paginate', run.call_args.args[0])

    def test_closed_terminal_owner_and_unknown_launch_are_never_release_evidence(self):
        for state in ('open', 'closed'):
            for marker in ('BUILDER_ID: CURSOR\nLAUNCH_STATE: RELEASED', 'LAUNCH_STATE: UNKNOWN',
                           'LAUNCH_STATE: FAILED_PRESTART', '<!-- ASTRA_CONTROL_RECORD_V1 -->',
                           '<!-- ASTRA_TASK_KEY_V1 malformed -->', 'TASK_ID: KIX-001'):
                with self.subTest(state=state, marker=marker):
                    result, _ = self.observe([[[issue(state=state, body=marker)]]])
                    self.assertEqual(result['mode'], 'host_required')
                    self.assertEqual(result['issue_state'], state)
                    with self.assertRaises(common.AppError) as caught: admission.require_native(result)
                    self.assertEqual(caught.exception.code, 'HOST_ADMISSION_REQUIRED')

    def test_paginated_comment_control_record_is_observed(self):
        result, run = self.observe([[[issue()], [issue(2, 'closed', comments=2)]],
                                   [[{'id': 1, 'body': 'History'}], [{'id': 2, 'body': '<!-- ASTRA_CONTROL_RECORD_V1 -->'}]]])
        self.assertEqual(result['issue'], 2)
        self.assertEqual(result['classification'], 'canonical_task_projection')
        self.assertEqual(run.call_count, 2)
        self.assertTrue(run.call_args.args[0][-1].endswith('/issues/2/comments?per_page=100'))

    def test_incomplete_or_malformed_observation_never_admits(self):
        cases = [[[]], [[{}]], [[[issue(comments=1)]], [[]]],
                 [[[issue(comments=2)]], [[{'id': 1, 'body': 'x'}, {'id': 1, 'body': 'y'}]]],
                 [[[issue(), issue()]]], [[[issue(comments=True)]]]]
        for responses in cases:
            with self.subTest(responses=responses), self.assertRaises(common.AppError): self.observe(responses)

    def test_observation_has_a_total_deadline_and_comment_request_bound(self):
        with mock.patch.object(admission.time, 'monotonic', side_effect=[0, 61]), self.assertRaises(common.AppError):
            self.observe([])
        responses = [[[issue(number=n, comments=1) for n in range(1, 34)]]]
        responses += [[[{'id': 1, 'body': 'Ordinary note'}]]] * 32
        with self.assertRaises(common.AppError) as caught: self.observe(responses)
        self.assertEqual(caught.exception.code, 'ADMISSION_OBSERVATION_UNRESOLVED')

    def test_managed_repository_and_pinned_program_require_actual_admission_even_without_issues(self):
        with mock.patch.object(gitops, 'execute') as run:
            result = admission.observe({'repository': REPO}, True, run)
            self.assertEqual(result['reason'], 'registered_project')
            scope = {'program': 'original', 'blob': 'a' * 40}
            result = admission.observe({'repository': REPO, 'program_scope': scope}, False, run)
            self.assertEqual(result['program'], 'original')
            run.assert_not_called()

    def test_observed_canonical_scope_is_not_cleared_by_later_issue_deletion(self):
        prior = admission.host_required('canonical_issue', issue=12, issue_state='closed')
        with mock.patch.object(gitops, 'execute') as run:
            self.assertEqual(admission.observe({'repository': REPO, 'admission': prior}, False, run), prior)
            run.assert_not_called()

    def test_registry_routes_case_insensitively_and_does_not_read_host_backups(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gitops, 'execute') as run:
            for repository in ('beautifulmind-jt/KIX-PROTOCOL', 'sunburn-golden/KIX-PROTOCOL'):
                result = gitops.Repositories(d).execution_admission({'repository': repository})
                self.assertEqual(result['reason'], 'registered_project'); run.assert_not_called()

    def test_older_prepared_job_still_checks_its_pinned_manifest(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gitops, 'execute') as run:
            repos = gitops.Repositories(d)
            scope = {'program': 'original', 'blob': 'b' * 40}
            with mock.patch.object(repos, 'program_scope', return_value=scope) as manifest:
                result = repos.execution_admission({'repository': REPO, 'base_sha': 'a' * 40})
            self.assertEqual(result['reason'], 'canonical_program')
            manifest.assert_called_once(); run.assert_not_called()

    def test_preparation_creates_an_isolated_branch_without_reserving_a_provider(self):
        with tempfile.TemporaryDirectory() as d:
            repos = gitops.Repositories(d)
            with mock.patch.object(gitops, 'gh', return_value=json.dumps({'defaultBranchRef': {'name': 'main'}})) as gh, \
                    mock.patch.object(gitops, 'git', return_value='a' * 40) as git:
                for key in ('a' * 16, 'b' * 16):
                    job = {'id': key, 'repository': REPO, 'branch': 'aiops/mac-' + key}
                    repos.prepare(job)
                    self.assertIn(mock.call(repos.path(job), 'checkout', '-b', job['branch']), git.call_args_list)
                    self.assertIn(mock.call(None, 'clone', '--no-local', '--single-branch', '--branch', 'main',
                                           'https://github.com/' + REPO + '.git', str(repos.path(job)), timeout=600), git.call_args_list)
                self.assertTrue(all(call.args[1:3] == ('repo', 'view') for call in gh.call_args_list))


class RuntimeTests(unittest.TestCase):
    setUp = legacy.AppTests.setUp
    tearDown = legacy.AppTests.tearDown
    new = legacy.AppTests.new
    job = legacy.AppTests.job

    def next_request(self):
        return self.store.create({'repository': REPO, 'goal': 'New independent work', 'request_id': 'next-request'})

    def test_completed_or_cancelled_local_history_preserved_and_new_branch_allowed(self):
        for state in common.TERMINAL:
            with self.subTest(state=state):
                old = self.new()
                old = self.store.update(old['id'], state=state, failure_fingerprint='old failure',
                                        blocker={'code': 'EXISTING_AIOPS_OWNER'}, user_answers=['old answer'])
                events = self.store.events(old['id'])
                new = self.store.create({'repository': REPO, 'goal': 'New independent work', 'request_id': 'next-' + state})
                self.assertNotEqual(new['branch'], old['branch'])
                self.assertNotEqual(new['id'], old['id'])
                self.assertIsNone(new['admission'])
                self.assertEqual(self.store.get(old['id']), old)
                self.assertEqual(self.store.events(old['id']), events)
                self.store.action(new['id'], 'cancel')

    def test_terminal_label_does_not_override_an_unresolved_attempt(self):
        old = self.new()
        for state in common.TERMINAL:
            for attempt in ({'id': 'unresolved'}, {}):
                with self.subTest(state=state, attempt=attempt):
                    self.store.update(old['id'], state=state, attempt=attempt)
                    with self.assertRaises(common.AppError) as caught: self.next_request()
                    self.assertEqual(caught.exception.code, 'LOCAL_EXECUTION_UNRESOLVED')
                    self.assertEqual(len(self.store.jobs()), 1)

    def test_unresolved_and_unfinished_local_jobs_keep_their_reservation(self):
        old = self.new()
        for state in ('unknown', 'planning', 'paused', 'needs_user', 'ready'):
            with self.subTest(state=state):
                self.store.update(old['id'], state=state)
                with self.assertRaises(common.AppError) as caught: self.next_request()
                self.assertEqual(caught.exception.code, 'LOCAL_EXECUTION_UNRESOLVED' if state == 'unknown' else 'REPOSITORY_BUSY')

    def test_previous_canonical_scope_survives_cancel_and_new_request(self):
        old = self.new(); proof = admission.host_required('canonical_issue', issue=12, issue_state='closed')
        self.store.update(old['id'], state='cancelled', admission=proof)
        new = self.next_request()
        self.assertEqual(new['admission'], proof)
        self.assertEqual(self.store.get(old['id'])['admission'], proof)

    def test_original_program_scope_is_kept_after_local_acceptance(self):
        old = self.new(); scope = {'program': 'original', 'blob': 'b' * 40, 'nodes': [{'id': 'original-node'}]}
        self.store.update(old['id'], state='accepted', program_scope=scope)
        new = self.next_request()
        self.assertEqual(new['admission'], admission.host_required('canonical_program', program='original', blob='b' * 40))
        self.assertEqual(self.store.get(old['id'])['program_scope'], scope)
        unrelated = self.store.create({'repository': 'example/unrelated', 'request_id': 'unrelated-request'})
        self.assertIsNone(unrelated['admission'])

    def test_execution_guard_applies_to_every_role_without_worker_or_call_reservation(self):
        self.new(); self.engine.step(self.job())
        proof = admission.host_required('registered_project')
        for role, phase in core.ROLE_STATE.items():
            self.store.update(self.job()['id'], phase=phase, state=phase)
            with mock.patch.object(self.repos, 'execution_admission', return_value=proof), \
                    mock.patch.object(core.subprocess, 'Popen') as spawn:
                self.engine.tick()
                current = self.job()
                self.assertEqual(current['state'], 'needs_user')
                self.assertEqual(current['blocker']['code'], 'HOST_ADMISSION_REQUIRED')
                self.assertEqual(current['admission'], proof)
                self.assertEqual(current['calls'], 0); self.assertIsNone(current['attempt'])
                spawn.assert_not_called()
        self.assertFalse((self.store.directory / 'jobs' / self.job()['id']).exists())

    def test_handoff_saved_during_observation_is_rechecked_before_launch(self):
        self.new(); self.engine.step(self.job())
        snapshot = handoff_fixture.snapshot()
        def observed(_):
            self.store.create_handoff(handoff_fixture.REQUEST, snapshot)
            return {'mode': 'native'}
        with mock.patch.object(self.repos, 'execution_admission', side_effect=observed), \
                mock.patch.object(core.subprocess, 'Popen') as spawn:
            self.engine.tick()
            spawn.assert_not_called()
        current = self.job()
        self.assertEqual(current['admission']['reason'], 'prepared_handoff')
        self.assertEqual(current['blocker']['code'], 'HOST_ADMISSION_REQUIRED')
        self.assertIsNone(current['attempt']); self.assertEqual(current['calls'], 0)

    def test_pause_and_cancel_remain_responsive_during_remote_observation(self):
        self.new(); self.engine.step(self.job())
        for action in ('pause', 'cancel'):
            self.store.update(self.job()['id'], state='planning', pause_requested=False)
            entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
            errors = []
            def observed(_):
                entered.set()
                if not release.wait(5): raise AssertionError('Observation not released')
                return {'mode': 'native'}
            def launch():
                try: self.engine.launch(self.job(), 'planner')
                except Exception as exc: errors.append(exc)
            def control():
                try: self.store.action(self.job()['id'], action); controlled.set()
                except Exception as exc: errors.append(exc)
            with mock.patch.object(self.repos, 'execution_admission', side_effect=observed), \
                    mock.patch.object(core.subprocess, 'Popen') as spawn:
                runner = threading.Thread(target=launch); runner.start()
                self.assertTrue(entered.wait(2))
                controller = threading.Thread(target=control); controller.start()
                try: self.assertTrue(controlled.wait(2), 'App controls blocked on GitHub read')
                finally: release.set(); runner.join(5); controller.join(5)
                spawn.assert_not_called()
            self.assertEqual(errors, [])
            self.assertEqual(self.job()['state'], 'paused' if action == 'pause' else 'cancelled')
            self.assertIsNone(self.job()['attempt'])

    def test_competing_observations_still_reserve_only_one_worker(self):
        self.new(); self.engine.step(self.job())
        barrier = threading.Barrier(2); errors = []
        def observed(_): barrier.wait(3); return {'mode': 'native'}
        def launch():
            try: self.engine.launch(self.job(), 'planner')
            except Exception as exc: errors.append(exc)
        with mock.patch.object(self.repos, 'execution_admission', side_effect=observed), \
                mock.patch.object(core.agents, 'command', return_value=['fake']), \
                mock.patch.object(core.subprocess, 'Popen') as spawn:
            threads = [threading.Thread(target=launch) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5)
            spawn.assert_called_once()
        self.assertEqual(errors, []); self.assertEqual(self.job()['calls'], 1)

    def test_cancel_during_observation_keeps_new_canonical_evidence_without_resurrecting_job(self):
        self.new(); self.engine.step(self.job())
        proof = admission.host_required('canonical_issue', issue=9)
        def observed(_):
            self.store.action(self.job()['id'], 'cancel')
            return proof
        with mock.patch.object(self.repos, 'execution_admission', side_effect=observed): self.engine.launch(self.job(), 'planner')
        self.assertEqual(self.job()['state'], 'cancelled')
        self.assertEqual(self.next_request()['admission'], proof)

    def test_observation_error_after_pause_does_not_retry_job(self):
        self.new(); self.engine.step(self.job())
        def observed(_):
            self.store.action(self.job()['id'], 'pause')
            raise common.AppError('COMMAND_TIMEOUT')
        with mock.patch.object(self.repos, 'execution_admission', side_effect=observed): self.engine.tick()
        self.assertEqual(self.job()['state'], 'paused')
        self.assertEqual(self.job()['failures'], 0)


if __name__ == '__main__': unittest.main()
