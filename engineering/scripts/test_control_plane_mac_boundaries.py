"""Record/request size boundaries preserve large valid scopes and single ownership."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import agents
import common
import core
import program_scope
import worker


def large_valid_fields():
    nodes = [{'id': f'n{index}', 'title': f'Node {index}', 'spec': 's' * 14900,
              'depends_on': []} for index in range(128)]
    manifest = {'schema_version': 1, 'program': 'example', 'repository': 'example/product',
                'approval_pointer': 'durable-user-pointer', 'authoritative_doc_pointers': 'README.md',
                'nodes': nodes}
    scope = program_scope.load_scope(manifest, 'example/product', 'a' * 40)
    plan = {'summary': 'Every original local node.', 'sources': [program_scope.PATH],
            'tasks': [{'id': node['id'], 'title': node['title'], 'instructions': node['spec'],
                       'acceptance': ['Executed fixture verification.'], 'depends_on': []}
                      for node in nodes]}
    common.validate_plan(plan)
    program_scope.validate_coverage(plan, scope)
    report = {'status': 'complete', 'summary': 'Reviewed fixture.', 'question': '', 'plan': None,
              'findings': [], 'checks': ['e' * 4000 for _ in range(100)],
              'reviewed_head': 'b' * 40, 'covered_tasks': [node['id'] for node in nodes]}
    agents.validate_report(report)
    return scope, plan, {'head': 'b' * 40, 'attempt': 'review-session',
                         'profile': {'provider': 'codex', 'model': ''}, 'report': report}


class LocalRepos:
    def execution_admission(self, job): return {'mode': 'native'}
    def __init__(self, path): self.directory = Path(path)
    def path(self, job): return self.directory
    def assert_binding(self, job): pass
    def assert_scope(self, job): pass
    def head(self, job): return 'a' * 40
    def clean(self, job): return True


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'private'
        self.store = core.Store(self.directory)
        self.request = {'repository': 'example/product', 'request_id': 'boundary-job-001'}
        self.job = self.store.create(self.request)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_individually_valid_scope_plan_and_review_roundtrip_above_old_4mb_limit(self):
        scope, plan, review = large_valid_fields()
        for value in (scope, plan, review['report']):
            self.assertLess(len(common.encoded(value).encode()), common.REPORT_LIMIT)
        stored = self.store.update(self.job['id'], state='paused', program_scope=scope,
                                   plan=plan, review=review)
        self.assertGreater(len(common.encoded(stored).encode()), 4194304)
        self.assertEqual(self.store.get(self.job['id'])['plan'], plan)
        self.assertEqual(self.store.jobs()[0]['program_scope'], scope)
        self.assertEqual(self.store.existing_request(self.request)['review'], review)
        self.assertEqual(self.store.create(self.request)['id'], self.job['id'])
        self.store.close()
        self.store = core.Store(self.directory)
        self.assertEqual(self.store.get(self.job['id'])['review'], review)
        engine = core.Engine(self.store, LocalRepos(self.temp.name))
        self.assertFalse(engine.tick())

    def test_oversized_aggregate_update_does_not_mutate_persisted_record(self):
        before = self.store.get(self.job['id'])
        answers = [{'question': 'Fixture question.', 'answer': 'a' * 16000} for _ in range(1000)]
        with self.assertRaises(common.AppError) as caught:
            self.store.update(self.job['id'], state='building', user_answers=answers)
        self.assertEqual(caught.exception.code, 'JOB_RECORD_TOO_LARGE')
        self.assertEqual(self.store.get(self.job['id']), before)

    def test_create_storage_failure_rolls_back_job_and_created_event(self):
        request = {'repository': 'example/second', 'request_id': 'boundary-job-002'}
        with mock.patch.object(core.Store, 'document', side_effect=common.AppError('JOB_RECORD_TOO_LARGE')):
            with self.assertRaises(common.AppError): self.store.create(request)
        self.assertIsNone(self.store.existing_request(request))
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)

    def test_control_reserve_allows_unknown_fence_without_removing_authoritative_content(self):
        answers = [{'question': 'Fixture question.', 'answer': 'a' * 16000} for _ in range(978)]
        attempt = {'id': 'one', 'head': 'a' * 40, 'role': 'builder', 'started': 1,
                   'timeout_seconds': 60, 'task': {'id': 'feature', 'title': 'Feature'}}
        stored = self.store.update(self.job['id'], state='building', phase='building',
                                   attempt=attempt, user_answers=answers)
        self.assertGreater(len(common.encoded(stored).encode()),
                           common.JOB_RECORD_LIMIT - common.JOB_CONTROL_RESERVE - 100000)
        engine = core.Engine(self.store, LocalRepos(self.temp.name))
        engine.fence(stored, 'WORKER_OUTCOME_UNKNOWN')
        fenced = self.store.get(self.job['id'])
        self.assertEqual(fenced['state'], 'unknown')
        self.assertEqual(fenced['user_answers'], answers)
        self.assertEqual(fenced['attempt'], attempt)
        self.assertEqual(self.store.events(self.job['id'])[-1]['kind'], 'execution_unknown')

    def test_invalid_or_oversized_event_is_rejected_before_insert(self):
        count = len(self.store.events(self.job['id']))
        for key, kind in ((self.job['id'], 'k' * 81), ('x' * common.EVENT_RECORD_LIMIT, 'test')):
            with self.subTest(kind=kind), self.assertRaises(common.AppError):
                self.store.event(key, kind, 'A bounded diagnostic.')
        self.assertEqual(len(self.store.events(self.job['id'])), count)

    def test_compact_projection_preserves_ui_acceptance_and_heads_without_duplicate_specs(self):
        scope, plan, review = large_valid_fields()
        stored = self.store.update(self.job['id'], state='paused', program_scope=scope,
                                   plan=plan, review=review,
                                   user_answers=[{'question': 'q', 'answer': 'a'}])
        engine = core.Engine(self.store, LocalRepos(self.temp.name))
        projected = engine.describe(stored, compact=True)
        self.assertEqual(projected['projection'], 'summary')
        self.assertEqual(projected['plan']['tasks'][0]['acceptance'], plan['tasks'][0]['acceptance'])
        self.assertNotIn('instructions', projected['plan']['tasks'][0])
        self.assertNotIn('spec', projected['program_scope']['nodes'][0])
        self.assertNotIn('report', projected['review'])
        self.assertEqual(projected['review']['head'], review['head'])
        self.assertEqual(projected['review']['check_count'], 100)
        self.assertEqual(projected['answer_count'], 1)
        self.assertNotIn('user_answers', projected)
        self.assertLess(len(common.encoded(projected).encode()), 100000)
        self.assertEqual(self.store.get(self.job['id'])['plan'], plan)
        self.assertEqual(self.store.get(self.job['id'])['review'], review)

    def test_serialized_request_limit_is_checked_before_reservation_and_spawn(self):
        current = self.store.update(self.job['id'], state='planning', phase='planning',
                                    base_sha='a' * 40, head='a' * 40)
        engine = core.Engine(self.store, LocalRepos(self.temp.name))
        # JSON escaping is counted: the prompt's character count alone fits.
        with mock.patch.object(core, 'WORKER_REQUEST_LIMIT', 1024), \
             mock.patch.object(agents, 'command', return_value=['fake']), \
             mock.patch.object(agents, 'prompt', return_value='\\' * 700), \
             mock.patch.object(core.subprocess, 'Popen') as popen:
            with self.assertRaises(common.AppError) as caught:
                engine.launch(current, 'planner')
        self.assertEqual(caught.exception.code, 'WORKER_REQUEST_TOO_LARGE')
        popen.assert_not_called()
        self.assertIsNone(self.store.get(self.job['id'])['attempt'])
        self.assertEqual(self.store.get(self.job['id'])['calls'], 0)
        self.assertFalse(any((self.directory / 'jobs').rglob('request.json')))

    def test_worker_reads_request_above_4mb_and_records_real_terminal_receipt(self):
        folder = Path(self.temp.name)
        attempt = folder / 'attempt'; attempt.mkdir()
        executable = folder / 'codex'
        executable.write_text('#!' + sys.executable + '\n' +
            'import json,sys\nfrom pathlib import Path\n' +
            'received=sys.stdin.read()\n' +
            'result={"status":"complete","summary":str(len(received)),"question":"",' +
            '"plan":None,"findings":[],"checks":["Read the complete fixture input."],' +
            '"reviewed_head":"' + 'a' * 40 + '","covered_tasks":["feature"]}\n' +
            'Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text(json.dumps(result))\n')
        executable.chmod(0o755)
        prompt = '\\' * (4194304 + 10)
        request = {'attempt_id': 'one', 'binding': 'bound-input',
                   'profile': {'provider': 'codex', 'model': ''}, 'role': 'reviewer',
                   'checkout': str(folder), 'prompt': prompt, 'timeout_seconds': 10}
        common.atomic_json(attempt / 'request.json', request)
        common.atomic_json(attempt / 'schema.json', agents.SCHEMA)
        self.assertGreater((attempt / 'request.json').stat().st_size, 4194304)
        self.assertLess((attempt / 'request.json').stat().st_size, common.WORKER_REQUEST_LIMIT)
        with mock.patch.dict(os.environ, {'PATH': str(folder) + os.pathsep + os.environ['PATH']}):
            worker.run(attempt)
        receipt = common.read_json(attempt / 'receipt.json')
        self.assertIsNone(receipt['error'])
        self.assertEqual(receipt['report']['summary'], str(len(prompt)))
        self.assertTrue(receipt['provider_started'])
        self.assertTrue(receipt['process_group_quiescent'])
        self.assertEqual(receipt['binding'], 'bound-input')

    def test_exact_json_limit_reserves_room_for_atomic_file_newline(self):
        current = self.store.update(self.job['id'], state='planning', phase='planning',
                                    base_sha='a' * 40, head='a' * 40)
        engine = core.Engine(self.store, LocalRepos(self.temp.name))
        requests = []
        def capture(value):
            if isinstance(value, dict) and 'prompt' in value: requests.append(value)
            return common.encoded(value)
        with mock.patch.object(core.uuid, 'uuid4', return_value=mock.Mock(hex='b' * 32)), \
             mock.patch.object(agents, 'command', return_value=['fake']), \
             mock.patch.object(agents, 'prompt', return_value='Bounded prompt'), \
             mock.patch.object(core, 'encoded', side_effect=capture), \
             mock.patch.object(core.subprocess, 'Popen') as popen:
            with mock.patch.object(core, 'WORKER_REQUEST_LIMIT', 1), self.assertRaises(common.AppError):
                engine.launch(current, 'planner')
            limit = len(common.encoded(requests[-1]).encode())
            with mock.patch.object(core, 'WORKER_REQUEST_LIMIT', limit), self.assertRaises(common.AppError):
                engine.launch(current, 'planner')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        popen.assert_not_called()
        self.assertIsNone(self.store.get(self.job['id'])['attempt'])
        self.assertFalse(any((self.directory / 'jobs').rglob('request.json')))


if __name__ == '__main__':
    unittest.main()
