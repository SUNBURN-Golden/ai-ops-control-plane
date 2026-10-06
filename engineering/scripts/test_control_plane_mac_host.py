"""Mac migration contracts: host selection, transport uncertainty and no model calls."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'engineering/mac_host'


def module(name):
    spec = importlib.util.spec_from_file_location('mac_' + name, PACKAGE / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


relay, host = module('relay'), module('host')


def request(operation='lanes', **fields):
    return dict(schema_version=1, request_id='request-1',
                repository='BeautifulMind-JT/ZARI', operation=operation, **fields)


class FakeGitHub:
    def __init__(self, failure=None, response=None):
        self.calls = []
        self.failure = failure
        self.response = response or (200, {'workflow_run_id': 123,
            'html_url': relay.WEB + '/actions/runs/123', 'run_url': relay.API + '/actions/runs/123'})

    def call(self, *args):
        self.calls.append(args)
        if self.failure:
            raise self.failure
        return self.response


class RelaySchemaTests(unittest.TestCase):
    def test_every_supported_request_matches_workflow_inputs(self):
        cases = [request(), request('preflight', builder_id='CURSOR'),
                 request('materialize', args={'program': 'p', 'node': 'n', 'plan_commit': 'a' * 40}),
                 request('start', issue_number=42, args={'program': 'p', 'node': 'n', 'plan_commit': 'a' * 40}),
                 request('review', issue_number=42, args={'slot': 2}),
                 request('merge-check', issue_number=42, args={'pr_number': 53}),
                 request('astra-audit', issue_number=42, args={'pr_number': 53, 'head': 'a' * 40}),
                 request('astra-consult', issue_number=42, args={'question_comment_id': 123})]
        for path in [ROOT / '.github/workflows/control-plane-runtime.yml',
                     ROOT / 'engineering/.github/workflows/control-plane-runtime.yml']:
            workflow = path.read_text()
            inputs = set(re.findall(r'^      ([a-z0-9_]+):$', workflow, re.M))
            for case in cases:
                with self.subTest(path=path, operation=case['operation']):
                    body = relay.prepare(case)['body']
                    expected = set(body['inputs'])
                    if path == ROOT / 'engineering/.github/workflows/control-plane-runtime.yml':
                        expected.discard('target_repository')  # product-local source copy
                    self.assertLessEqual(expected, inputs)
                    self.assertIn('          - ' + case['operation'] + '\n', workflow)
                    self.assertNotIn('execution_host',body['inputs'])
                    self.assertNotIn('expected_runner_name',body['inputs'])

    def test_rejects_ambiguous_or_injected_requests(self):
        base = request()
        for changes in ({'runner_name': 'x\n$(id)'}, {'repository': []}, {'operation': 'merge'},
                        {'operation': 'shell'}, {'schema_version': True}, {'request_id': '../x'},
                        {'token': 'secret'}, {'args': {'shell': 'whoami'}}, {'issue_number': 1}):
            with self.subTest(changes=changes), self.assertRaises(relay.RelayError):
                relay.prepare({**base, **changes})
        with self.assertRaises(relay.RelayError):
            relay.prepare(request('preflight', builder_id=[]))

    def test_rejects_duplicate_keys_and_non_json_constants(self):
        for raw in ('{"operation":"lanes","operation":"start"}', '{"x":NaN}', ' ' * 65537):
            with self.assertRaises(relay.RelayError):
                relay.strict_json(raw)

    def test_audit_requires_exact_head_and_real_integer_pr(self):
        for head, pr in [('main', 53), ('a' * 40, True), ('a' * 39, 53)]:
            with self.assertRaises(relay.RelayError):
                relay.prepare(request('astra-audit', issue_number=42, args={'head': head, 'pr_number': pr}))

    def test_submit_preview_does_not_create_state_or_use_network(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(relay, 'Github', side_effect=AssertionError('network')):
            source = Path(d) / 'request.json'; source.write_text(json.dumps(request()))
            state = Path(d) / 'state'
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = relay.main(['submit', '--request', str(source), '--state-dir', str(state)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())['state'], 'PREVIEW_ONLY')
            self.assertFalse(state.exists())

    def test_redirect_refused_without_forwarding_credentials(self):
        with self.assertRaises(relay.RelayError):
            relay.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid/')


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / 'receipts'
        self.journal = relay.Journal(self.directory)

    def tearDown(self):
        self.journal.close(); self.tmp.cleanup()

    def test_duplicate_submit_returns_same_receipt_with_one_network_write(self):
        api = FakeGitHub()
        first = relay.submit(request(), self.journal, api)
        second = relay.submit(request(), self.journal, api)
        self.assertEqual(first['state'], 'SUBMITTED')
        self.assertEqual(second['run_id'], 123)
        self.assertTrue(second['reused'])
        self.assertEqual(len(api.calls), 1)

    def test_lost_response_is_unknown_and_not_replayed_after_restart(self):
        api = FakeGitHub(failure=TimeoutError('sensitive-token-value'))
        first = relay.submit(request(), self.journal, api)
        self.journal.close(); self.journal = relay.Journal(self.directory)
        second = relay.submit(request(), self.journal, api)
        self.assertEqual(first['state'], 'UNKNOWN')
        self.assertTrue(second['reused'])
        self.assertEqual(len(api.calls), 1)
        self.assertNotIn('sensitive-token-value', json.dumps(first))

    def test_process_loss_after_reservation_does_not_post(self):
        self.journal.reserve(relay.prepare(request()))
        self.journal.close(); self.journal = relay.Journal(self.directory)
        api = FakeGitHub()
        result = relay.submit(request(), self.journal, api)
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertEqual(api.calls, [])

    def test_request_id_reuse_with_new_payload_is_refused(self):
        api = FakeGitHub(); relay.submit(request(), self.journal, api)
        with self.assertRaises(relay.RelayError):
            relay.submit(request('preflight'), self.journal, api)
        self.assertEqual(len(api.calls), 1)

    def test_two_callers_reserve_once(self):
        barrier = threading.Barrier(2); outcomes = []; errors = []
        def caller():
            try:
                journal = relay.Journal(self.directory)
                barrier.wait(timeout=5)
                outcomes.append(journal.reserve(relay.prepare(request()))[0])
                journal.close()
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=caller) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(sorted(outcomes), [False, True])

    def test_foreign_run_receipt_is_unknown(self):
        api = FakeGitHub(response=(200, {'workflow_run_id': 123, 'html_url': 'https://evil.invalid/',
                                       'run_url': relay.API + '/actions/runs/123'}))
        self.assertEqual(relay.submit(request(), self.journal, api)['state'], 'UNKNOWN')

    def test_204_is_accepted_without_claiming_started_or_done(self):
        api = FakeGitHub(response=(204, None))
        result = relay.submit(request(), self.journal, api)
        self.assertEqual(result['state'], 'SUBMITTED')
        self.assertNotIn('run_id', result)
        self.assertEqual(relay.refresh(result, api)['observation'], 'NO_CONFIRMED_RUN_ID')
        self.assertEqual(len(api.calls), 1)

    def test_status_reads_once_and_workflow_success_is_not_task_done(self):
        receipt = relay.submit(request(), self.journal, FakeGitHub())
        run = {'id': 123, 'html_url': receipt['run_url'], 'path': '.github/workflows/' + relay.WORKFLOW,
               'event': 'workflow_dispatch', 'head_branch': 'main', 'status': 'completed', 'conclusion': 'success'}
        api = FakeGitHub(response=(200, run))
        result = relay.refresh(receipt, api)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(result['task_completion'], 'NOT_CHECKED')
        run['path'] = '.github/workflows/other.yml'
        with self.assertRaises(relay.RelayError): relay.refresh(receipt, api)

    def test_receipt_file_symlink_refused(self):
        other = Path(self.tmp.name) / 'elsewhere'; other.mkdir(mode=0o700)
        target = Path(self.tmp.name) / 'target'; target.write_text('unchanged')
        (other / 'requests.sqlite3').symlink_to(target)
        with self.assertRaises(relay.RelayError): relay.Journal(other)
        self.assertEqual(target.read_text(), 'unchanged')


class RunnerBindingTests(unittest.TestCase):
    def test_mac_routing_is_absent_without_shared_admission(self):
        for path in [ROOT / '.github/workflows/control-plane-runtime.yml',
                     ROOT / 'engineering/.github/workflows/control-plane-runtime.yml']:
            text=path.read_text()
            selector = next(line for line in text.splitlines() if line.startswith('    runs-on:'))
            self.assertEqual(selector,'    runs-on: [self-hosted, astra-control-plane]')
            self.assertNotIn('execution_host',text)
            self.assertNotIn('aiops-macbook',text)

    def test_even_an_exact_dispatcher_runner_name_cannot_send_mac_work(self):
        api=FakeGitHub()
        for name in ('aiops-macbook-01','qualified-looking-runner'):
            with tempfile.TemporaryDirectory() as folder:
                journal=relay.Journal(Path(folder)/'receipts')
                try:
                    with self.assertRaisesRegex(relay.RelayError,'SHARED_ADMISSION_AUTHORITY_REQUIRED'):
                        relay.submit(request(execution_host='macbook',runner_name=name),journal,api)
                    self.assertEqual(journal.db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
                    self.assertEqual(api.calls,[])
                finally:journal.close()


class MacHostTests(unittest.TestCase):
    def test_rosetta_does_not_choose_an_intel_guest(self):
        with mock.patch.object(host.platform, 'system', return_value='Darwin'), \
             mock.patch.object(host.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(host, 'probe', side_effect=['1', str(16 * 1024 ** 3)]):
            result = host.inventory()
        self.assertEqual(result['suggested_guest_architecture'], 'aarch64')
        self.assertTrue(result['python_under_rosetta'])
        self.assertEqual(result['memory_gib'], 16)
        self.assertNotIn('username', result)

    def test_verified_local_image_produces_native_vm_without_shares_or_ports(self):
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / 'debian.qcow2'; image.write_bytes(b'test image')
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            for machine, arch, backend in [('arm64', 'aarch64', 'vz'), ('x86_64', 'x86_64', 'qemu')]:
                result = host.vm_config(image, digest, arch, system='Darwin', machine=machine)
                self.assertEqual(result['vmType'], backend)
                self.assertTrue(result['plain'])
                self.assertEqual(result['mounts'], [])
                self.assertEqual(result['portForwards'], [])
                self.assertFalse(result['ssh']['forwardAgent'])

    def test_wrong_image_digest_or_foreign_architecture_refused(self):
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / 'image'; image.write_bytes(b'image')
            for sha, arch in [('0' * 64, 'aarch64'), (hashlib.sha256(b'image').hexdigest(), 'x86_64')]:
                with self.assertRaises(host.HostError):
                    host.vm_config(image, sha, arch, system='Darwin', machine='arm64')

    def test_image_symlink_and_resource_type_refused(self):
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / 'image'; image.write_bytes(b'image')
            link = Path(d) / 'link'; link.symlink_to(image)
            sha = hashlib.sha256(b'image').hexdigest()
            with self.assertRaises(OSError):
                host.vm_config(link, sha, 'aarch64', system='Darwin', machine='arm64')
            with self.assertRaises(host.HostError):
                host.vm_config(image, sha, 'aarch64', cpus=True, system='Darwin', machine='arm64')

    def test_guest_check_never_claims_runtime_qualified(self):
        with mock.patch.object(host.shutil, 'which', return_value='/bin/tool'):
            result = host.guest_check()
        self.assertEqual(result['runtime_qualification'], 'NOT_CHECKED')
        self.assertEqual(result['lane_qualification'], 'NOT_CHECKED')
        self.assertFalse(result['model_invoked'])


if __name__ == '__main__':
    unittest.main()
