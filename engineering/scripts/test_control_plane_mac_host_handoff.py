"""Fixture-only Mac/legacy integration using the actual program runtime and ledger."""
import concurrent.futures
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import aiops
import common
import handoff
import host_observation as observation
import test_control_plane_program as program_fixture
import test_control_plane_mac_host as relay_fixture

relay = relay_fixture.relay


class HostHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = program_fixture.ProgramModeTests()
        self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.issue = self.f.materialized()

    def record(self, nodes=('n1',), head=program_fixture.PLAN1):
        import re
        tasks = []
        for node in nodes:
            status = self.f.host.ledger.materialize_status('zari', node)
            issue = self.f.gh.issues[status['issue']]
            owners = re.findall(r'^BUILDER_ID: ([A-Z_]+)$', issue['body'], re.MULTILINE)
            tasks.append({'program': 'zari', 'node': node, 'number': issue['number'], 'state': issue['state'],
                          'materialization_request_id': status['request'], 'declared_owners': owners})
        snap = {'repository': program_fixture.REPO, 'source': {'program': 'zari', 'head': head,
                'blob': 'b' * 40, 'node_ids': list(nodes), 'node_count': len(nodes)}, 'tasks': tasks, 'task_scope': 'all'}
        return {'id': 'a' * 16, 'kind': 'legacy_plan_handoff', 'repository': program_fixture.REPO,
                'snapshot': snap, 'snapshot_sha256': common.digest(snap), 'blockers':
                [{'code': 'HOST_AUTHORITY_UNOBSERVED'}, {'code': 'HANDOFF_ADMISSION_NOT_AVAILABLE'}]}

    def evidence(self, record=None):
        return observation.capture_fixture(record or self.record(), self.f.host, clock=lambda: 100)

    def assess(self, evidence=None, record=None):
        record = record or self.record()
        return observation.assess(record, evidence or self.evidence(record), now=100)

    def response(self, evidence, command):
        return next(x['result'] for x in evidence['responses'] if x['args'][0] == command)

    def preview(self):
        record = self.record()
        return observation.preview_start(record, self.evidence(record), 'n1', 'handoff-integration-001', now=100)

    def journal(self, name):
        journal = relay.Journal(self.f.file(name)); self.addCleanup(journal.close); return journal

    def workflow_api(self, *, lose_response=False):
        f = self.f
        class WorkflowFixture:
            def __init__(self): self.calls = 0; self.results = []
            def call(self, method, endpoint, body):
                self.calls += 1
                assert method == 'POST' and endpoint.endswith('/control-plane-runtime.yml/dispatches')
                inputs = body['inputs']; args = json.loads(inputs['program_args'])
                assert inputs['operation'] == 'start'
                assert inputs['target_repository'] == program_fixture.REPO
                # The unchanged main workflow selects the existing host itself;
                # relay cannot send undeclared routing or admission inputs.
                assert set(inputs) == {'target_repository', 'operation', 'program_args', 'issue_number'}
                # Same start, dispatch, finalize and protected ledger code as the
                # runtime fixture; no second admission implementation in the test.
                self.results.append(f.launch_writer(int(inputs['issue_number']), args['node'], args['plan_commit']))
                if lose_response: raise TimeoutError('simulated lost response')
                return 200, {'workflow_run_id': 7, 'html_url': relay.WEB + '/actions/runs/7',
                             'run_url': relay.API + '/actions/runs/7'}
        return WorkflowFixture()

    def test_all_read_calls_match_existing_sudoers_and_do_not_modify_ledger(self):
        before = hashlib.sha256(self.f.host.path.read_bytes()).hexdigest()
        plan = observation.query_plan(self.record())
        self.assertEqual({q[0] for q in plan['queries']}, {'materialize-list', 'materialize-status', 'task-status', 'status'})
        self.assertTrue(all(program_fixture.sudoers_allows(q) for q in plan['queries']))
        self.assertTrue(self.assess()['observations_consistent'])
        self.assertEqual(hashlib.sha256(self.f.host.path.read_bytes()).hexdigest(), before)
        with self.assertRaisesRegex(common.AppError, 'HOST_TRANSPORT_NOT_CONFIGURED'):
            observation.capture_fixture(self.record(), None)

    def test_clear_fixture_is_not_execution_authority(self):
        result = self.assess()
        self.assertFalse(result['execution_allowed']); self.assertEqual(result['source'], 'fixture')
        self.assertEqual(result['native_mac_admission'], 'UNSUPPORTED')
        self.assertIsNone(result['owners']['n1'])

    def test_descendant_plan_is_previewed_then_validated_by_existing_runtime(self):
        self.f.gh.contents[program_fixture.PLAN2] = program_fixture.plan()
        self.f.gh.compare[(program_fixture.PLAN1, program_fixture.PLAN2)] = 'ahead'
        record = self.record(head=program_fixture.PLAN2)
        preview = observation.preview_start(record, self.evidence(record), 'n1', 'plan-advance-001', now=100)
        self.assertFalse(preview['execution_allowed'])
        self.assertEqual(preview['assessment']['required_plan_advances']['n1'], {
            'recorded': program_fixture.PLAN1, 'requested': program_fixture.PLAN2,
            'validation': 'REQUIRED_BY_EXISTING_PROGRAM_START'})
        self.assertEqual(self.f.host.ledger.materialize_status('zari', 'n1')['plan_commit'], program_fixture.PLAN1)
        api = self.workflow_api(); relay.submit(preview['request'], self.journal('advance'), api)
        self.assertEqual(api.results[0]['status'], 'PREPARED')
        self.assertEqual(self.f.host.ledger.materialize_status('zari', 'n1')['plan_commit'], program_fixture.PLAN2)

    def test_preview_cannot_authorize_non_descendant_plan(self):
        self.f.gh.contents[program_fixture.PLAN2] = program_fixture.plan()
        self.f.gh.compare[(program_fixture.PLAN1, program_fixture.PLAN2)] = 'behind'
        record = self.record(head=program_fixture.PLAN2)
        preview = observation.preview_start(record, self.evidence(record), 'n1', 'stale-plan-001', now=100)
        request = preview['request']
        with self.assertRaisesRegex(program_fixture.cp.ControlPlaneError, 'STALE_PLAN'):
            self.f.launch_writer(request['issue_number'], request['args']['node'], request['args']['plan_commit'])
        self.assertEqual(self.f.rows(), [])

    def test_closed_previous_node_does_not_block_open_node_but_cannot_be_started(self):
        self.f.gh.contents[program_fixture.PLAN1] = program_fixture.plan([
            program_fixture.node('n1'), program_fixture.node('n2')])
        second = self.f.materialized('n2')
        self.f.gh.issues[self.issue]['state'] = 'closed'
        record = self.record(('n1', 'n2')); evidence = self.evidence(record)
        preview = observation.preview_start(record, evidence, 'n2', 'next-node-001', now=100)
        self.assertEqual(preview['request']['issue_number'], second)
        with self.assertRaisesRegex(common.AppError, 'HOST_CANONICAL_ISSUE_NOT_OPEN'):
            observation.preview_start(record, evidence, 'n1', 'closed-node-001', now=100)
        self.assertEqual(self.f.launch_writer(second, 'n2')['status'], 'PREPARED')

    def test_historical_attempt_cannot_replace_canonical_issue_or_owner(self):
        record = self.record()
        old = dict(record['snapshot']['tasks'][0], number=29, state='closed',
                   materialization_request_id='e' * 24, declared_owners=['CURSOR'])
        for tasks in ([old, record['snapshot']['tasks'][0]], [record['snapshot']['tasks'][0], old]):
            record['snapshot']['tasks'] = tasks; record['snapshot_sha256'] = common.digest(record['snapshot'])
            preview = observation.preview_start(record, self.evidence(record), 'n1', 'history-001', now=100)
            self.assertEqual(preview['request']['issue_number'], self.issue)
            self.assertIsNone(preview['owner_lane'])

    def test_old_open_only_snapshot_requires_new_preparation(self):
        record = self.record(); record['snapshot'].pop('task_scope')
        with self.assertRaisesRegex(common.AppError, 'HANDOFF_TASK_HISTORY_INCOMPLETE'):
            self.assess(record=record)

    def test_case_alias_collection_reaches_exact_protected_host_repository(self):
        import test_control_plane_mac_handoff as collection_fixture
        values = collection_fixture.responses(data=program_fixture.plan())
        values[0]['full_name'] = program_fixture.REPO
        values[1]['sha'] = program_fixture.PLAN1
        values[3] = [[self.f.gh.issues[self.issue]]]
        with mock.patch.object(handoff, 'execute', side_effect=[json.dumps(x) for x in values]):
            snap = handoff.inspect_repository(program_fixture.REPO.lower())
        record = self.record(); record.update(repository=snap['repository'], snapshot=snap,
                                              snapshot_sha256=common.digest(snap), blockers=snap['blockers'])
        self.assertEqual(self.assess(record=record)['observations_consistent'], True)

    def test_first_writer_owner_is_retained_after_verified_release(self):
        self.f.released_writer(self.issue)
        result = self.assess(); preview = self.preview()
        self.assertTrue(result['observations_consistent']); self.assertEqual(result['owners']['n1'], 'DEVIN')
        self.assertFalse(preview['execution_allowed']); self.assertEqual(preview['owner_lane'], 'DEVIN')
        self.assertNotIn('builder_id', preview['request'])

    def fail_before_start(self, live=()):
        packet_file, result_file = self.f.file('failed-packet.json'), self.f.file('failed-result.json')
        program_fixture.prog.start(self.issue, 'zari', 'n1', program_fixture.PLAN1, packet_file,
                                   preflight=lambda lane: True)
        packet = json.loads(packet_file.read_text())
        def fail(packet, policy):
            return subprocess.CompletedProcess([], 0, json.dumps(program_fixture.host.result_for(
                packet, 'FAILED_PRESTART', reason='fixture prestart failure')))
        with mock.patch.object(self.f.host, 'confirm', side_effect=fail), \
             mock.patch.object(program_fixture.host, 'lane_quiescence', return_value=list(live)):
            program_fixture.cp.launch_dispatch(packet_file, result_file)
        return program_fixture.cp.finalize_dispatch(self.issue, result_file, packet['launch_request_id'])

    def test_verified_failed_prestart_declaration_is_not_an_owner_and_can_retry(self):
        self.assertEqual(self.fail_before_start(), 'FAILED_PRESTART')
        preview = self.preview()
        self.assertIsNone(preview['owner_lane']); self.assertFalse(preview['execution_allowed'])
        api = self.workflow_api(); relay.submit(preview['request'], self.journal('retry-failed'), api)
        self.assertEqual(api.results[0]['status'], 'PREPARED')
        self.assertEqual([r['state'] for r in self.f.rows()], ['FAILED_PRESTART', 'CONFIRMED'])

    def test_owner_declaration_without_matching_failed_attempt_remains_blocked(self):
        record = self.record(); record['snapshot']['tasks'][0]['declared_owners'] = ['DEVIN']
        self.assertIn('HOST_OWNER_PROJECTION_MISMATCH', {b['code'] for b in self.assess(record=record)['blockers']})
        self.fail_before_start()
        record = self.record(); record['snapshot']['tasks'][0]['declared_owners'] = ['CURSOR']
        self.assertIn('HOST_OWNER_PROJECTION_MISMATCH', {b['code'] for b in self.assess(record=record)['blockers']})

    def test_unproven_prestart_failure_stays_unknown_and_cannot_retry(self):
        self.assertEqual(self.fail_before_start(live=(9876,)), 'UNKNOWN')
        self.assertIn('HOST_EXECUTION_UNRESOLVED', {b['code'] for b in self.assess()['blockers']})
        with self.assertRaisesRegex(common.AppError, 'HOST_OBSERVATION_BLOCKED'): self.preview()

    def test_active_confirmed_writer_and_reviewer_hold_observation(self):
        writer = self.f.launch_writer(self.issue)
        self.assertIn('HOST_EXECUTION_UNRESOLVED', {b['code'] for b in self.assess()['blockers']})
        program_fixture.prog.reap(self.issue, writer['launch_request_id'], self.f.deliver(self.issue, writer))
        self.f.launch_review(self.issue)
        self.assertIn('HOST_EXECUTION_UNRESOLVED', {b['code'] for b in self.assess()['blockers']})

    def test_submitting_unknown_and_unverified_reconciliation_remain_held(self):
        for state in ('SUBMITTING', 'UNKNOWN', 'RECONCILED'):
            with self.subTest(state=state):
                writer = self.f.launch_writer(self.issue) if not self.f.rows() else None
                db = self.f.host.ledger.connect()
                try:
                    db.execute('UPDATE launches SET state=?, evidence=?',
                               (state, json.dumps({'resolution': 'NO_SESSION_CONFIRMED'}) if state == 'RECONCILED' else None))
                finally: db.close()
                result = self.assess()
                self.assertFalse(result['observations_consistent'])

    def test_owner_history_cannot_change_to_another_lane(self):
        self.f.released_writer(self.issue)
        packet = {'schema_version': 2, 'role': 'WRITER', 'owner_lane': 'CURSOR', 'builder_id': 'CURSOR',
                  'repository': program_fixture.REPO, 'task_id': 'ZARI-N1', 'task_revision': 'p' + program_fixture.PLAN1[:12] + '-CURSOR',
                  'launch_request_id': 'c' * 24, 'attempt_id': 2}
        self.f.host.ledger.reserve(packet, self.f.host.policy)
        self.assertIn('HOST_OWNER_HISTORY_CONFLICT', {b['code'] for b in self.assess()['blockers']})

    def test_request_issue_plan_and_repository_mismatches_are_not_accepted(self):
        for field, value in (('request', 'c' * 24), ('issue', 999), ('plan_commit', 'c' * 40), ('repository', 'other/repo')):
            with self.subTest(field=field):
                evidence = self.evidence(); self.response(evidence, 'materialize-status')[field] = value
                try: result = self.assess(evidence)
                except common.AppError: continue
                self.assertFalse(result['observations_consistent'])

    def test_missing_or_hidden_materializations_cannot_disappear_with_issue_projection(self):
        evidence = self.evidence()
        self.response(evidence, 'materialize-status').update(status='NOT_FOUND')
        self.assertIn('HOST_MATERIALIZATION_MISSING', {b['code'] for b in self.assess(evidence)['blockers']})
        self.f.host.ledger.materialize_begin('zari', 'orphan', program_fixture.REPO, program_fixture.PLAN1, self.f.host.policy)
        self.assertIn('HOST_UNPROJECTED_NODE', {b['code'] for b in self.assess()['blockers']})

    def test_unresolved_materialization_cannot_be_treated_as_unstarted(self):
        evidence = self.evidence()
        self.response(evidence, 'materialize-status')['status'] = 'UNKNOWN'
        self.assertIn('HOST_MATERIALIZATION_UNRESOLVED', {b['code'] for b in self.assess(evidence)['blockers']})

    def test_stale_foreign_missing_duplicate_and_extra_queries_fail_closed(self):
        variants = []
        value = self.evidence(); value['observed_at'] = 20; value['started_at'] = 20; variants.append(value)
        value = self.evidence(); value['snapshot_sha256'] = 'd' * 64; variants.append(value)
        value = self.evidence(); value['source'] = 'authenticated'; variants.append(value)
        value = self.evidence(); value['responses'].pop(); variants.append(value)
        value = self.evidence(); value['responses'].append(copy.deepcopy(value['responses'][0])); variants.append(value)
        value = self.evidence(); value['responses'].append({'args': ['launch'], 'result': {}}); variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(common.AppError): self.assess(value)

    def test_individual_launch_and_lane_races_are_detected(self):
        self.f.launch_writer(self.issue); evidence = self.evidence()
        evidence['responses'][-1]['result']['state'] = 'UNKNOWN'
        self.assertIn('HOST_LAUNCH_OBSERVATION_CHANGED', {b['code'] for b in self.assess(evidence)['blockers']})
        evidence = self.evidence()
        board = next(x['result'] for x in evidence['responses'] if x['args'] == ['status', '--lanes'])
        for lane in board['lanes']: lane['active'] = []
        board['active_total'] = 0
        self.assertIn('HOST_LANE_OBSERVATION_CHANGED', {b['code'] for b in self.assess(evidence)['blockers']})

    def test_local_unknown_hold_is_preserved_even_with_empty_host(self):
        record = self.record(); record['blockers'].append({'code': 'LOCAL_EXECUTION_UNRESOLVED', 'job_id': 'b' * 16})
        result = self.assess(record=record)
        self.assertIn('LOCAL_EXECUTION_UNRESOLVED', {b['code'] for b in result['blockers']})
        with self.assertRaisesRegex(common.AppError, 'HOST_OBSERVATION_BLOCKED'):
            observation.preview_start(record, self.evidence(record), 'n1', 'preview-001', now=100)

    def test_preview_routes_existing_host_without_lane_or_model_override(self):
        preview = self.preview(); prepared = relay.prepare(preview['request'])
        self.assertEqual(preview['request']['execution_host'], 'current')
        self.assertEqual(prepared['body']['inputs'], {
            'target_repository': program_fixture.REPO, 'operation': 'start',
            'program_args': relay.canonical(preview['request']['args']), 'issue_number': str(self.issue)})
        self.assertEqual(prepared['body']['ref'], 'main')
        for extra in ({'runner_name': 'different-host'}, {'execution_host': 'native-mac'}, {'builder_id': 'CURSOR'}):
            with self.subTest(extra=extra), self.assertRaises(relay.RelayError): relay.prepare(dict(preview['request'], **extra))
        self.assertEqual(self.f.rows(), [])

    def test_macbook_handoff_cannot_reserve_or_dispatch_without_shared_admission(self):
        request = dict(self.preview()['request'], execution_host='macbook', runner_name='aiops-macbook')
        journal = self.journal('unqualified-macbook'); api = self.workflow_api()
        with self.assertRaisesRegex(relay.RelayError, '^SHARED_ADMISSION_AUTHORITY_REQUIRED$'):
            relay.submit(request, journal, api)
        self.assertEqual(api.calls, 0)
        self.assertEqual(api.results, [])
        self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
        self.assertEqual(self.f.rows(), [])

    def test_fixture_preview_to_actual_program_and_shared_ledger_admits_one_writer(self):
        request = self.preview()['request']; api = self.workflow_api(); journal = self.journal('first')
        first = relay.submit(request, journal, api); second = relay.submit(request, journal, api)
        self.assertEqual(first['state'], 'SUBMITTED'); self.assertTrue(second['reused'])
        self.assertEqual(api.calls, 1); self.assertEqual(len(self.f.rows()), 1)
        self.assertEqual(self.f.rows()[0]['state'], 'CONFIRMED')

    def test_second_client_cannot_start_another_writer_from_stale_clear_snapshot(self):
        request = self.preview()['request']; api = self.workflow_api()
        relay.submit(request, self.journal('first'), api)
        relay.submit(dict(request, request_id='independent-client-002'), self.journal('second'), api)
        self.assertEqual(api.calls, 2); self.assertEqual(api.results[1]['status'], 'TASK_ACTIVE')
        self.assertEqual(len(self.f.rows()), 1)

    def test_lost_dispatch_response_is_unknown_and_never_automatically_resent(self):
        request = self.preview()['request']; api = self.workflow_api(lose_response=True); journal = self.journal('lost')
        first = relay.submit(request, journal, api); second = relay.submit(request, journal, api)
        self.assertEqual(first['state'], 'UNKNOWN'); self.assertEqual(second['state'], 'UNKNOWN')
        self.assertEqual(api.calls, 1); self.assertEqual(len(self.f.rows()), 1)

    def test_existing_owner_is_used_on_real_program_resume_in_fixture(self):
        self.f.released_writer(self.issue)
        api = self.workflow_api(); relay.submit(self.preview()['request'], self.journal('resume'), api)
        self.assertEqual(len(self.f.rows()), 2)
        self.assertEqual({r['lane'] for r in self.f.rows()}, {'DEVIN'})

    def test_shared_ledger_atomically_fences_competing_clients_after_idle_reads(self):
        barrier = threading.Barrier(2)
        def compete(index):
            packet = {'schema_version': 2, 'role': 'WRITER', 'repository': program_fixture.REPO,
                      'task_id': 'ZARI-N1', 'task_revision': 'r1', 'builder_id': ('DEVIN', 'CURSOR')[index],
                      'owner_lane': ('DEVIN', 'CURSOR')[index], 'launch_request_id': str(index + 1) * 24, 'attempt_id': 1}
            barrier.wait(timeout=5)
            return self.f.host.ledger.reserve(packet, self.f.host.policy)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, (0, 1)))
        self.assertEqual(sum(admitted for admitted, _ in results), 1)
        self.assertEqual(sum(row['state'] in observation.ACTIVE for row in self.f.rows()), 1)

    def test_cli_fixture_check_has_no_mutation_or_dispatch(self):
        record = self.record(); evidence = self.evidence(record)
        import time
        evidence['started_at'] = evidence['observed_at'] = time.time()
        file = self.f.file('fixture.json'); file.write_text(json.dumps(evidence))
        with mock.patch.object(aiops, 'client', return_value=record) as client:
            self.assertEqual(aiops.main(['handoff', 'check-fixture', record['id'], '--fixture', str(file)]), 0)
        self.assertEqual(client.call_args.args[1], '/api/handoffs/' + record['id'])
        self.assertEqual(len(client.call_args.args), 2); self.assertEqual(self.f.rows(), [])

    def test_malformed_host_enum_and_claimed_authority_are_rejected(self):
        self.f.launch_writer(self.issue)
        for field in ('state', 'lane', 'role'):
            evidence = self.evidence(); self.response(evidence, 'task-status')['rows'][0][field] = []
            with self.subTest(field=field), self.assertRaises(common.AppError): self.assess(evidence)
        evidence = self.evidence(); evidence['execution_allowed'] = True
        with self.assertRaises(common.AppError): self.assess(evidence)


if __name__ == '__main__': unittest.main()
