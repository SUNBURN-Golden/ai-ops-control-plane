"""Real offline CLI failures preserve receipt truth without exposing log secrets."""
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
import worker


def completed_report():
    return {'status': 'complete', 'summary': 'Verified the requested assignment.',
            'question': '', 'plan': None, 'findings': [],
            'checks': ['Executed the offline CLI fixture.'],
            'reviewed_head': 'a' * 40, 'covered_tasks': ['task']}


class WorkerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'attempt'
        self.folder.mkdir()
        common.atomic_json(self.folder / 'schema.json', agents.SCHEMA)
        common.atomic_json(self.folder / 'request.json', {
            'attempt_id': 'one', 'binding': 'fixed-task-and-model',
            'profile': {'provider': 'codex', 'model': 'fixed-model'},
            'role': 'reviewer', 'checkout': str(self.root),
            'prompt': 'review the assignment', 'timeout_seconds': 10})

    def tearDown(self):
        self.temp.cleanup()

    def install_cli(self, message='', returncode=0):
        # This executable follows the actual codex adapter arguments and runs
        # under its filtered environment, rather than mocking Popen/returncode.
        source = '#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\n'
        source += "assert sys.stdin.read() == 'review the assignment'\n"
        source += "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(" + repr(json.dumps(completed_report())) + ')\n'
        source += 'print(' + repr(message + ' secret-test-marker') + ', file=sys.stderr)\n'
        source += 'raise SystemExit(' + str(returncode) + ')\n'
        binary = self.root / 'codex'
        binary.write_text(source)
        binary.chmod(0o755)

    def run_worker(self):
        with mock.patch.dict(os.environ, {'PATH': str(self.root)}):
            worker.run(self.folder)
        return common.read_json(self.folder / 'receipt.json')

    def assert_sanitized_failure(self, message, expected):
        self.install_cli(message, 7)
        receipt = self.run_worker()
        self.assertEqual(receipt['error'], expected)
        self.assertEqual(receipt['exit_code'], 7)
        self.assertTrue(receipt['provider_started'])
        self.assertTrue(receipt['process_group_quiescent'])
        # A complete-looking artifact from a failed process grants no report.
        self.assertTrue((self.folder / 'last-message.json').exists())
        self.assertIsNone(receipt['report'])
        self.assertNotIn('provider_evidence', receipt)
        self.assertNotIn('secret-test-marker', json.dumps(receipt))
        self.assertEqual(receipt['binding'], 'fixed-task-and-model')
        return receipt

    def test_sudo_password_requirement_is_terminal_sanitized_permission(self):
        self.assert_sanitized_failure('sudo: a password is required', 'PROVIDER_PERMISSION_REQUIRED')

    def test_permission_denial_does_not_use_auth_or_generic_retry(self):
        self.assert_sanitized_failure('Permission denied for protected tool', 'PROVIDER_PERMISSION_REQUIRED')

    def test_noninteractive_approval_requirement_is_a_permission_blocker(self):
        self.assert_sanitized_failure('Approval required in non-interactive mode', 'PROVIDER_PERMISSION_REQUIRED')

    def test_quota_exhaustion_takes_precedence_over_http429(self):
        self.assert_sanitized_failure('HTTP 429 quota exceeded for weekly subscription', 'PROVIDER_USAGE_LIMIT')

    def test_free_usage_exhaustion_is_a_capacity_blocker(self):
        self.assert_sanitized_failure('Free tier usage exhausted', 'PROVIDER_USAGE_LIMIT')

    def test_weekly_limit_reached_before_capacity_name_is_still_a_blocker(self):
        self.assert_sanitized_failure("You've reached your weekly usage limit", 'PROVIDER_USAGE_LIMIT')

    def test_actual_rate_limit_is_temporary(self):
        self.assert_sanitized_failure('HTTP 429: rate limit, retry later', 'PROVIDER_TEMPORARILY_UNAVAILABLE')

    def test_explicit_server_failure_is_temporary(self):
        self.assert_sanitized_failure('HTTP 503 Service Unavailable', 'PROVIDER_TEMPORARILY_UNAVAILABLE')

    def test_network_reset_is_temporary(self):
        self.assert_sanitized_failure('Connection reset by peer', 'PROVIDER_TEMPORARILY_UNAVAILABLE')

    def test_unrecognized_nonzero_exit_keeps_narrow_numeric_code(self):
        self.assert_sanitized_failure('Unexpected provider failure', 'PROVIDER_EXIT_7')

    def test_missing_cli_is_prestart_quiescent_and_has_no_completion(self):
        receipt = self.run_worker()
        self.assertEqual(receipt['error'], 'MISSING_PROVIDER')
        self.assertFalse(receipt['provider_started'])
        self.assertTrue(receipt['process_group_quiescent'])
        self.assertIsNone(receipt['exit_code'])
        self.assertIsNone(receipt['report'])
        self.assertNotIn('provider_evidence', receipt)

    def test_success_has_real_provider_start_and_completed_report(self):
        self.install_cli()
        receipt = self.run_worker()
        self.assertTrue(receipt['provider_started'])
        self.assertTrue(receipt['process_group_quiescent'])
        self.assertIsNone(receipt['error'])
        self.assertEqual(receipt['exit_code'], 0)
        self.assertEqual(receipt['report'], completed_report())
        self.assertEqual(receipt['provider_evidence']['provider'], 'codex')
        self.assertEqual(receipt['provider_evidence']['model_requested'], 'fixed-model')

    def test_exclusive_claim_still_rejects_duplicate_without_second_spawn(self):
        self.install_cli()
        receipt = self.run_worker()
        with mock.patch.object(worker.subprocess, 'Popen') as spawn:
            with self.assertRaises(FileExistsError):
                worker.run(self.folder)
        spawn.assert_not_called()
        self.assertEqual(common.read_json(self.folder / 'receipt.json'), receipt)


if __name__ == '__main__':
    unittest.main()
