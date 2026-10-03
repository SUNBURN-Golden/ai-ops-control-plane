"""Local startup failure cleanup and direct loopback client regressions."""
import copy
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import agents
import aiops
import common
import core


def result():
    return {'status': 'complete', 'summary': 'Verified.', 'question': '', 'plan': None,
            'findings': [], 'checks': ['Executed required tests.'], 'reviewed_head': 'a' * 40,
            'covered_tasks': ['feature']}


class StartupTests(unittest.TestCase):
    def test_initial_discovery_never_runs_unused_cli_or_network_authentication(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(agents.shutil, 'which', return_value='/local/tool'), \
             mock.patch.object(agents.subprocess, 'run', side_effect=AssertionError('Startup must not probe')):
            app = aiops.Application(Path(directory) / 'state')
            try:
                self.assertTrue(app.doctor['gh']['installed'])
                self.assertFalse(app.doctor['github_authenticated'])
                self.assertEqual(app.doctor['github_authentication'], 'not_checked')
                self.assertTrue(all(app.doctor[key]['authentication'] == 'not_checked' for key in agents.CATALOG))
            finally: app.store.close()

    def test_application_constructor_closes_database_on_token_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(Path(directory) / 'state')
            with mock.patch.object(aiops, 'Store', return_value=store), \
                 mock.patch.object(aiops, 'token', side_effect=common.AppError('TOKEN_FAILURE')):
                with self.assertRaisesRegex(common.AppError, 'TOKEN_FAILURE'): aiops.Application(store.directory)
            with self.assertRaisesRegex(Exception, 'closed'): store.db.execute('SELECT 1')

    def test_constructor_failure_releases_real_service_lock_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            with mock.patch.object(aiops, 'Application', side_effect=common.AppError('INIT_FAILED')):
                with self.assertRaisesRegex(common.AppError, 'INIT_FAILED'): aiops.serve(state, 8765)
            lock = core.service_lock(state)
            os.close(lock)

    def test_bind_failure_closes_store_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            app = aiops.Application(Path(directory) / 'state')
            with mock.patch.object(aiops, 'Application', return_value=app), \
                 mock.patch.object(aiops, 'ThreadingHTTPServer', side_effect=OSError('address occupied')):
                with self.assertRaisesRegex(OSError, 'address occupied'): aiops.serve(app.store.directory, 8765)
            lock = core.service_lock(app.store.directory)
            os.close(lock)
            with self.assertRaisesRegex(Exception, 'closed'): app.store.db.execute('SELECT 1')

    def test_endpoint_write_failure_closes_server_before_thread_is_started(self):
        with tempfile.TemporaryDirectory() as directory:
            server = mock.Mock()
            app = aiops.Application(Path(directory) / 'state')
            with mock.patch.object(aiops, 'Application', return_value=app), \
                 mock.patch.object(aiops, 'ThreadingHTTPServer', return_value=server), \
                 mock.patch.object(aiops, 'atomic_json', side_effect=OSError('disk full')), \
                 mock.patch.object(aiops.threading, 'Thread') as thread:
                with self.assertRaisesRegex(OSError, 'disk full'): aiops.serve(app.store.directory, 8765)
            server.server_close.assert_called_once()
            thread.assert_not_called()
            os.close(core.service_lock(app.store.directory))

    def test_process_exit_keeps_lock_until_old_engine_step_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            source = '''
import sys, threading, time
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import aiops
class Engine:
    stopping = threading.Event()
    wake = threading.Event()
    def run(self):
        time.sleep(6)
        (Path(sys.argv[2]) / 'operation-finished').write_text('done')
class Server:
    def serve_forever(self, **kwargs): pass
    def server_close(self): pass
app = aiops.Application(Path(sys.argv[2]), engine=Engine())
with mock.patch.object(aiops, 'Application', return_value=app), mock.patch.object(aiops, 'ThreadingHTTPServer', return_value=Server()):
    aiops.serve(app.store.directory, 8765)
print('serve-returned', flush=True)
'''
            process = subprocess.Popen([sys.executable, '-c', source, str(APP), str(state)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'serve-returned')
                self.assertIsNone(process.poll())
                with self.assertRaisesRegex(common.AppError, 'SERVICE_ALREADY_RUNNING'): core.service_lock(state)
                output, errors = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, errors)
                self.assertEqual((state / 'operation-finished').read_text(), 'done')
                os.close(core.service_lock(state))
            finally:
                if process.poll() is None: process.kill(); process.communicate()


class ReportBoundsTests(unittest.TestCase):
    def test_all_native_wrappers_reject_oversized_report_fields(self):
        for changes in ({'summary': 'x' * 16001}, {'question': 'x' * 16001},
                        {'checks': ['x' * 4001]}, {'findings': ['x'] * 257},
                        {'covered_tasks': ['feature', 'feature']}, {'covered_tasks': ['../escape']},
                        {'reviewed_head': 'a' * 41}):
            with self.subTest(changes=list(changes)), self.assertRaises(common.AppError):
                agents.validate_report(dict(result(), **changes))

    def test_aggregate_report_limit_applies_even_to_structured_native_output(self):
        value = result()
        value.update(checks=['x' * 4000] * 256, findings=['x' * 4000] * 256,
                     plan={'summary': 'x' * 100000})
        with self.assertRaisesRegex(common.AppError, 'AGENT_REPORT_TOO_LARGE'): agents.validate_report(value)

    def test_valid_full_task_coverage_and_large_review_are_accepted(self):
        value = result()
        value.update(covered_tasks=[f'TASK-{n:03}' for n in range(256)], checks=['x' * 4000] * 100)
        self.assertEqual(agents.validate_report(value), value)


class DirectClientTests(unittest.TestCase):
    def test_browser_open_revalidates_endpoint_after_successful_probe(self):
        with mock.patch.object(aiops, 'client', return_value={}), \
             mock.patch.object(aiops, 'read_json', return_value={'origin': 'https://external.invalid'}), \
             mock.patch.object(aiops, 'token') as token, \
             mock.patch.object(aiops.webbrowser, 'open') as browser, \
             mock.patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(aiops.main(['open']), 2)
        token.assert_not_called()
        browser.assert_not_called()

    def test_malformed_or_out_of_range_endpoint_is_not_a_client_target(self):
        for value in (None, [], {'origin': None}, {'origin': 'http://127.0.0.1:99999'},
                      {'origin': 'http://127.0.0.1:0999'}, {'origin': 'http://localhost:8765'}):
            with self.subTest(value=value), mock.patch.object(aiops, 'read_json', return_value=value):
                with self.assertRaisesRegex(common.AppError, 'INVALID_LOCAL_ENDPOINT'): aiops.local_origin('/unused')

    def test_client_disables_environment_proxy_before_bearer_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = common.private_directory(Path(directory) / 'state')
            common.atomic_json(state / 'endpoint.json', {'origin': 'http://127.0.0.1:8765'})
            aiops.token(state / 'relay-token')
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'[]'
            opener = mock.Mock()
            opener.open.return_value = response
            actual_build = aiops.urllib.request.build_opener
            seen = []
            def build(*handlers):
                built = actual_build(*handlers)
                # A configured HTTP_PROXY must not install a proxy method on
                # the direct opener; ProxyHandler({}) overrides global policy.
                self.assertFalse(any(type(h) is aiops.urllib.request.ProxyHandler for h in built.handlers))
                seen.extend(handlers)
                return opener
            with mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://proxy.invalid:3128',
                                             'http_proxy': 'http://proxy.invalid:3128',
                                             'NO_PROXY': '', 'no_proxy': ''}), \
                 mock.patch.object(aiops.urllib.request, 'build_opener', side_effect=build):
                self.assertEqual(aiops.client(state, '/api/jobs'), [])
            self.assertEqual(next(h for h in seen if isinstance(h, aiops.urllib.request.ProxyHandler)).proxies, {})
            self.assertTrue(opener.open.call_args.args[0].full_url.startswith('http://127.0.0.1:8765/'))


if __name__ == '__main__': unittest.main()
