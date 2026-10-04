"""Offline handoff preparation regressions; no real GitHub/provider/host writes."""
import base64
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import agents
import aiops
import common
import core
import gitops
import handoff
import test_control_plane_mac_app as legacy

REPO = 'example/product'
REQUEST = {'repository': REPO, 'request_id': 'handoff-test-001'}


def program(count=1):
    return {'schema_version': 1, 'program': 'product', 'repository': REPO,
            'approval_pointer': 'docs/DECISION.md', 'authoritative_doc_pointers': 'docs/REQUIREMENTS.md',
            'nodes': [{'id': 'node-' + str(i), 'title': 'Original requirement ' + str(i),
                       'spec': '원래 요구사항 전체를 보존합니다.\nNo deployment.',
                       'depends_on': ['node-' + str(i - 1)] if i else [],
                       'audit_floor': 'A3', 'astra_gate': 'RELEASE', 'user_merge': True}
                      for i in range(count)]}


def issue(number=111, node='node-0', suffix=''):
    return {'number': number, 'state': 'open', 'body':
            '<!-- ASTRA_TASK_KEY_V1 program=product node=' + node + ' request=' + 'a' * 24 + ' -->\n'
            'Task envelope is written by the control plane when a lane is assigned. Do not dispatch.\n' + suffix}


def responses(pages=None, data=None):
    raw = json.dumps(data or program(), ensure_ascii=False, indent=2) + '\n'
    binary = raw.encode()
    blob = hashlib.sha1(b'blob ' + str(len(binary)).encode() + b'\0' + binary).hexdigest()
    return [{'full_name': REPO, 'default_branch': 'main', 'archived': False}, {'sha': 'b' * 40},
            {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(binary).decode(), 'sha': blob},
            pages if pages is not None else [[issue()]]]


def snapshot():
    with mock.patch.object(handoff, 'execute', side_effect=[json.dumps(x) for x in responses()]):
        return handoff.inspect_repository(REPO)


class RepositoryTests(unittest.TestCase):
    def inspect(self, values):
        with mock.patch.object(handoff, 'execute', side_effect=[json.dumps(x) for x in values]) as command:
            result = handoff.inspect_repository(REPO)
        return result, command

    def test_original_73_nodes_specs_and_gates_remain_exactly_pinned(self):
        values = responses(data=program(73)); result, command = self.inspect(values)
        raw = base64.b64decode(values[2]['content']).decode()
        self.assertEqual(result['source']['raw_program'], raw)
        self.assertEqual(result['source']['node_count'], 73)
        self.assertEqual(json.loads(raw), program(73))
        self.assertEqual(result['source']['blob'], values[2]['sha'])
        self.assertEqual(result['tasks'][0]['classification'], 'registered_plan_projection')
        self.assertFalse(result['execution_allowed'])
        self.assertEqual(command.call_count, 4)
        for call in command.call_args_list:
            self.assertEqual(call.args[0][:4], ['gh', 'api', '--method', 'GET'])
        self.assertIn('?ref=' + 'b' * 40, command.call_args_list[2].args[0][-1])
        self.assertEqual(command.call_args_list[3].args[0][4:6], ['--paginate', '--slurp'])
        self.assertIn('state=all', command.call_args_list[3].args[0][-1])
        self.assertEqual(result['task_scope'], 'all')

    def test_pagination_observes_owner_on_second_page(self):
        first = [issue(i + 1, 'node-' + str(i)) for i in range(100)]
        result, _ = self.inspect(responses([first, [issue(101, 'node-100', 'BUILDER_ID: DEVIN\nLAUNCH_STATE: CONFIRMED\n')]], program(101)))
        self.assertEqual(result['task_count'], 101)
        self.assertIn({'code': 'LEGACY_OWNER_DECLARED', 'issue': 101}, result['blockers'])
        self.assertIn({'code': 'LEGACY_LAUNCH_UNRESOLVED', 'issue': 101}, result['blockers'])

    def test_missing_owner_empty_or_released_projection_never_grants_authority(self):
        for pages in ([[]], [[issue()]], [[issue(suffix='LAUNCH_STATE: RELEASED\n')]]):
            with self.subTest(pages=pages):
                result, _ = self.inspect(responses(pages))
                self.assertFalse(result['execution_allowed'])
                self.assertEqual(result['host_authority'], 'unobserved')
                codes = {x['code'] for x in result['blockers']}
                self.assertTrue({'HOST_AUTHORITY_UNOBSERVED', 'HANDOFF_ADMISSION_NOT_AVAILABLE'} <= codes)

    def test_all_unresolved_launch_states_are_conservative_holds(self):
        for state in handoff.UNRESOLVED:
            with self.subTest(state=state):
                result, _ = self.inspect(responses([[issue(suffix='LAUNCH_STATE: ' + state + '\n')]]))
                self.assertIn({'code': 'LEGACY_LAUNCH_UNRESOLVED', 'issue': 111}, result['blockers'])

    def test_ambiguous_missing_duplicate_and_foreign_task_keys_are_preserved_as_blockers(self):
        unknown = issue(1); unknown['body'] = 'Do not dispatch.'
        double = issue(2); double['body'] += '\n' + issue()['body']
        result, _ = self.inspect(responses([[unknown, double, issue(3), issue(4), issue(5, 'foreign')]]))
        codes = {x['code'] for x in result['blockers']}
        self.assertTrue({'LEGACY_TASK_KEY_UNRESOLVED', 'LEGACY_DUPLICATE_TASK', 'LEGACY_PLAN_BINDING_MISMATCH'} <= codes)
        self.assertEqual(result['task_count'], 5)
        self.assertNotIn('body', result['tasks'][0])

    def test_invalid_metadata_head_blob_pages_and_issue_data_fail_closed(self):
        variants = []
        for metadata in ({'full_name': None}, {'full_name': 'other/repo'}, {'full_name': REPO, 'archived': True}):
            value = responses(); value[0] = metadata; variants.append(value)
        value = responses(); value[1]['sha'] = 'main'; variants.append(value)
        for changes in ({'sha': 'c' * 40}, {'encoding': 'none'}, {'content': 'not base64'}):
            value = responses(); value[2].update(changes); variants.append(value)
        for pages in ([], {}, [None], [[issue(), issue()]], [[{'number': True, 'body': '', 'state': 'open'}]], [[dict(issue(), state='unknown')]]):
            variants.append(responses(pages))
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(common.AppError): self.inspect(value)

    def test_closed_history_is_preserved_without_open_duplicate_or_legacy_holds(self):
        old = dict(issue(110, suffix='BUILDER_ID: DEVIN\nLAUNCH_STATE: CONFIRMED\n'), state='closed')
        legacy_closed = dict(issue(109), state='closed', body='Old completed registration.')
        result, _ = self.inspect(responses([[old, legacy_closed, issue()]]))
        self.assertEqual([t['state'] for t in result['tasks']], ['closed', 'closed', 'open'])
        self.assertEqual({b['code'] for b in result['blockers']},
                         {'HOST_AUTHORITY_UNOBSERVED', 'HANDOFF_ADMISSION_NOT_AVAILABLE'})
        self.assertEqual({b['code'] for b in result['historical_notes']},
                         {'LEGACY_OWNER_DECLARED', 'LEGACY_LAUNCH_UNRESOLVED', 'LEGACY_TASK_KEY_UNRESOLVED'})

    def test_github_canonical_repository_is_used_after_metadata_verification(self):
        with mock.patch.object(handoff, 'execute', side_effect=[json.dumps(x) for x in responses()]) as command:
            result = handoff.inspect_repository(REPO.upper())
        self.assertEqual(result['repository'], REPO)
        self.assertEqual(command.call_args_list[0].args[0][-1], 'repos/' + REPO.upper())
        self.assertTrue(all(c.args[0][-1].startswith('repos/' + REPO + '/') for c in command.call_args_list[1:]))

    def test_unsupported_or_pending_program_is_not_silently_rewritten(self):
        for field, value in (('schema_version', 2), ('approval_pointer', 'PENDING')):
            data = program(); data[field] = value
            with self.subTest(field=field), self.assertRaises(common.AppError): self.inspect(responses(data=data))

    def test_request_rejects_host_evidence_force_urls_and_invalid_ids(self):
        for value in (dict(REQUEST, force=True), dict(REQUEST, execution_allowed=True),
                      dict(REQUEST, host_receipt={}), dict(REQUEST, repository='https://evil.invalid/example/product'),
                      dict(REQUEST, source_job_id='../secrets'), dict(REQUEST, request_id='short')):
            with self.subTest(value=value), self.assertRaises(common.AppError): handoff.request(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with mock.patch.object(agents, 'availability', return_value={}):
            self.app = aiops.Application(Path(self.temp.name) / 'state')
        self.store = self.app.store; self.observation = snapshot()

    def tearDown(self): self.store.close(); self.temp.cleanup()

    def prepare(self, value=None):
        with mock.patch.object(handoff, 'inspect_repository', return_value=self.observation):
            return self.app.prepare_handoff(value or REQUEST)

    def test_prepare_is_separate_from_queue_and_does_not_wake_workers(self):
        settings = self.store.settings()
        with mock.patch.object(self.app.engine, 'wake') as wake:
            result = self.prepare()
        wake.set.assert_not_called()
        self.assertEqual(result['state'], 'prepared'); self.assertFalse(result['execution_allowed'])
        self.assertEqual(self.store.jobs(), []); self.assertEqual(self.store.settings(), settings)
        with mock.patch.object(self.app.engine, 'keep_awake'), mock.patch.object(self.app.engine, 'step') as step:
            self.assertFalse(self.app.engine.tick()); step.assert_not_called()
        with self.assertRaisesRegex(common.AppError, 'JOB_NOT_FOUND'): self.store.action(result['id'], 'resume')
        self.assertNotIn('raw_program', result['snapshot']['source'])
        self.assertEqual(self.store.get_handoff(result['id'])['snapshot'], self.observation)

    def test_same_request_returns_original_snapshot_without_network_or_new_record(self):
        first = self.prepare()
        with mock.patch.object(handoff, 'inspect_repository', side_effect=AssertionError('No refresh')):
            second = self.app.prepare_handoff(dict(REQUEST, repository=REPO.upper()))
        self.assertEqual(first, second); self.assertEqual(len(self.store.handoffs()), 1)
        with self.assertRaisesRegex(common.AppError, 'REQUEST_ID_CONFLICT'):
            self.app.prepare_handoff(dict(REQUEST, repository='example/other'))
        with self.assertRaisesRegex(common.AppError, 'REQUEST_ID_CONFLICT'):
            self.app.prepare_handoff(dict(REQUEST, source_job_id='a' * 16))

    def test_new_record_uses_canonical_repository_and_retries_keep_original_record(self):
        first = self.prepare(dict(REQUEST, repository=REPO.upper()))
        self.assertEqual(first['repository'], REPO)
        self.assertEqual(first['snapshot']['repository'], REPO)
        self.assertEqual(first, self.prepare(REQUEST))

    def test_prepare_preserves_jobs_failures_settings_and_every_event(self):
        job = self.store.create({'repository': REPO, 'request_id': 'existing-job-001'})
        job = self.store.update(job['id'], state='cancelled', failures=3,
                                blocker={'code': 'EXISTING_AIOPS_OWNER'}, failure_fingerprint='saved',
                                plan={'original': 'do not shorten'}, feedback=['original'], last_terminal={'error': 'original'})
        for i in range(505): self.store.event(job['id'], 'old_failure', 'Preserved event ' + str(i))
        before = list(self.store.db.iterdump())
        result = self.prepare(dict(REQUEST, source_job_id=job['id']))
        self.assertEqual(self.store.get(job['id']), job)
        self.assertEqual(result['source_jobs'][0]['event_count'], 506)
        self.assertEqual(result['source_jobs'][0]['document_sha256'], common.digest(job))
        after = list(self.store.db.iterdump())
        self.assertEqual([x for x in before if not x.startswith('INSERT INTO "handoffs"')],
                         [x for x in after if not x.startswith('INSERT INTO "handoffs"')])

    def test_all_nonterminal_states_and_attempts_remain_blocked_and_unchanged(self):
        job = self.store.create({'repository': REPO, 'request_id': 'existing-job-001'})
        for i, state in enumerate((*core.ACTIVE, 'paused', 'needs_user', 'ready', 'unknown', 'cancelled')):
            with self.subTest(state=state):
                attempt = {'id': 'bound-attempt'} if state in ('unknown', 'cancelled') else None
                before = self.store.update(job['id'], state=state, attempt=attempt)
                result = self.prepare(dict(REQUEST, request_id='state-check-' + str(i)))
                codes = {x['code'] for x in result['blockers']}
                self.assertFalse(result['execution_allowed']); self.assertEqual(self.store.get(job['id']), before)
                if state != 'cancelled': self.assertIn('LOCAL_JOB_NOT_TERMINAL', codes)
                if attempt: self.assertIn('LOCAL_EXECUTION_UNRESOLVED', codes)

    def test_source_job_must_exist_and_match_repository_before_any_fetch(self):
        job = self.store.create({'repository': 'example/other', 'request_id': 'existing-job-001'})
        with mock.patch.object(handoff, 'inspect_repository') as inspect:
            for key in (job['id'], 'f' * 16):
                with self.assertRaises(common.AppError): self.app.prepare_handoff(dict(REQUEST, source_job_id=key))
            inspect.assert_not_called()
        self.assertEqual(self.store.handoffs(), [])

    def test_inspect_has_no_persistent_mutations(self):
        before = list(self.store.db.iterdump())
        with mock.patch.object(handoff, 'inspect_repository', return_value=copy.deepcopy(self.observation)):
            result = self.app.inspect_handoff({'repository': REPO})
        self.assertFalse(result['execution_allowed']); self.assertEqual(list(self.store.db.iterdump()), before)

    def test_network_failure_leaves_no_partial_record_or_job(self):
        with mock.patch.object(handoff, 'inspect_repository', side_effect=common.AppError('COMMAND_TIMEOUT')):
            with self.assertRaisesRegex(common.AppError, 'COMMAND_TIMEOUT'): self.app.prepare_handoff(REQUEST)
        self.assertEqual(self.store.handoffs(), []); self.assertEqual(self.store.jobs(), [])

    def test_concurrent_same_request_creates_only_one_record(self):
        barrier = threading.Barrier(2); results, errors = [], []
        def observe(_): barrier.wait(timeout=5); return self.observation
        def prepare():
            try: results.append(self.app.prepare_handoff(REQUEST))
            except Exception as exc: errors.append(exc)
        with mock.patch.object(handoff, 'inspect_repository', side_effect=observe):
            threads = [threading.Thread(target=prepare) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=10)
        self.assertEqual(errors, []); self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1]); self.assertEqual(len(self.store.handoffs()), 1)

    def test_local_state_is_rechecked_after_remote_observation(self):
        job = self.store.create({'repository': REPO, 'request_id': 'existing-job-001'})
        self.store.update(job['id'], state='cancelled')
        def observe(_):
            self.store.update(job['id'], state='unknown', attempt={'id': 'late-attempt'})
            return self.observation
        with mock.patch.object(handoff, 'inspect_repository', side_effect=observe): result = self.app.prepare_handoff(REQUEST)
        self.assertIn({'code': 'LOCAL_EXECUTION_UNRESOLVED', 'job_id': job['id']}, result['blockers'])

    def test_reopen_preserves_immutable_record(self):
        first = self.prepare(); path = self.store.directory
        self.store.close(); self.store = core.Store(path); self.app.store = self.store
        self.assertEqual(handoff.summary(self.store.get_handoff(first['id'])), first)

    def test_invalid_record_rolls_back_and_leaves_store_usable(self):
        bad = dict(self.observation, execution_allowed=True)
        with self.assertRaisesRegex(common.AppError, 'INVALID_HANDOFF_SNAPSHOT'): self.store.create_handoff(REQUEST, bad)
        self.assertEqual(self.store.handoffs(), []); self.prepare()

    def test_existing_repository_guard_cannot_be_bypassed_by_prepared_record(self):
        self.prepare(); job = {'id': 'a' * 16, 'repository': REPO, 'branch': 'aiops/test'}
        with mock.patch.object(gitops, 'gh', side_effect=[json.dumps({'defaultBranchRef': {'name': 'main'}}),
                json.dumps([{'number': 111, 'url': 'https://github.com/' + REPO + '/issues/111'}])]), \
                mock.patch.object(gitops, 'git') as git:
            with self.assertRaises(common.AppError) as raised: self.app.engine.repos.prepare(job)
        self.assertEqual(raised.exception.code, 'EXISTING_AIOPS_OWNER'); git.assert_not_called()


class HandoffHttpTests(unittest.TestCase):
    setUp = legacy.HttpTests.setUp
    tearDown = legacy.HttpTests.tearDown
    request = legacy.HttpTests.request

    def test_authenticated_relay_can_prepare_and_read_but_cannot_execute(self):
        auth = {'Authorization': 'Bearer ' + self.app.relay_token}
        self.assertEqual(self.request('/api/handoffs')[0], 401)
        self.assertEqual(self.request('/api/handoffs', 'POST', REQUEST)[0], 401)
        with mock.patch.object(handoff, 'inspect_repository', return_value=snapshot()):
            status, _, raw = self.request('/api/handoffs', 'POST', REQUEST, auth)
            self.assertEqual(status, 201); record = json.loads(raw)
            self.assertEqual(self.request('/api/handoffs/inspect', 'POST', {'repository': REPO}, auth)[0], 200)
        self.assertEqual(self.request('/api/handoffs', headers=auth)[0], 200)
        self.assertEqual(json.loads(self.request('/api/handoffs/' + record['id'], headers=auth)[2]), record)
        self.assertEqual(self.request('/api/handoffs/' + record['id'] + '/host-plan', headers=auth)[0], 200)
        self.assertEqual(self.request('/api/handoffs/' + record['id'] + '/host-plan')[0], 401)
        self.assertEqual(self.request('/api/handoffs/' + record['id'] + '/resume', 'POST', {}, auth)[0], 404)
        owner = {'Authorization': 'Bearer ' + self.app.owner_token}
        self.assertEqual(self.request('/api/jobs/' + record['id'] + '/resume', 'POST', {}, owner)[0], 409)
        self.assertEqual(self.app.store.jobs(), [])

    def test_force_receipts_and_traversal_are_rejected_before_remote_calls(self):
        auth = {'Authorization': 'Bearer ' + self.app.relay_token}
        with mock.patch.object(handoff, 'inspect_repository') as remote:
            for extra in ({'force': True}, {'host_receipt': {'owner': None}}, {'execution_allowed': True}):
                self.assertEqual(self.request('/api/handoffs', 'POST', dict(REQUEST, **extra), auth)[0], 409)
            remote.assert_not_called()
        self.assertEqual(self.request('/api/handoffs/..', headers=auth)[0], 400)
        self.assertEqual(self.request('/api/handoffs/../../desktop-token', headers=auth)[0], 404)


class BridgeTests(unittest.TestCase):
    def test_cli_commands_route_only_to_preparation_endpoints(self):
        cases = [(['inspect', '--repo', REPO], '/api/handoffs/inspect'),
                 (['prepare', '--repo', REPO, '--request-id', REQUEST['request_id']], '/api/handoffs'),
                 (['list'], '/api/handoffs'), (['status', 'a' * 16], '/api/handoffs/' + 'a' * 16),
                 (['host-plan', 'a' * 16], '/api/handoffs/' + 'a' * 16 + '/host-plan')]
        for args, endpoint in cases:
            with self.subTest(args=args), mock.patch.object(aiops, 'client', return_value={}) as client, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(aiops.main(['handoff', *args]), 0)
                self.assertEqual(client.call_args.args[1], endpoint)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): aiops.main(['handoff', 'execute'])

    def test_mcp_commands_route_only_to_preparation_endpoints(self):
        calls = [('aiops_handoff_inspect', {'repository': REPO}, '/api/handoffs/inspect'),
                 ('aiops_handoff_prepare', REQUEST, '/api/handoffs'),
                 ('aiops_handoff_list', {}, '/api/handoffs'),
                 ('aiops_handoff_host_plan', {'handoff_id': 'a' * 16}, '/api/handoffs/' + 'a' * 16 + '/host-plan'),
                 ('aiops_handoff_status', {'handoff_id': 'a' * 16}, '/api/handoffs/' + 'a' * 16)]
        for name, args, endpoint in calls:
            source = io.StringIO(json.dumps({'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}) + '\n')
            output = io.StringIO()
            with self.subTest(name=name), mock.patch.object(aiops, 'client', return_value={}) as client:
                aiops.mcp(Path('/unused'), source, output)
                self.assertIn('result', json.loads(output.getvalue()))
                self.assertEqual(client.call_args.args[1], endpoint)

    def test_mcp_rejects_extra_authority_and_invalid_path_before_client(self):
        calls = [('aiops_handoff_prepare', dict(REQUEST, force=True)),
                 ('aiops_handoff_inspect', dict(REQUEST)), ('aiops_handoff_list', {'execute': True}),
                 ('aiops_handoff_status', {'handoff_id': '../secrets'}),
                 ('aiops_handoff_status', {'handoff_id': 'a' * 16, 'resume': True})]
        for name, args in calls:
            source = io.StringIO(json.dumps({'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}) + '\n')
            output = io.StringIO()
            with self.subTest(name=name), mock.patch.object(aiops, 'client') as client:
                aiops.mcp(Path('/unused'), source, output)
                self.assertIn('error', json.loads(output.getvalue())); client.assert_not_called()


if __name__ == '__main__': unittest.main()
