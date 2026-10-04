"""Offline Mac installer transactions against real temporary files and SQLite."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import common
import core
spec = importlib.util.spec_from_file_location('mac_transactional_installer', APP / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / 'Mac User'
        self.home.mkdir(mode=0o700)
        self.app = self.home / 'Applications/AIOPS.app'
        self.state = self.home / 'Library/Application Support/AIOPS'
        self.plist = self.home / 'Library/LaunchAgents/local.aiops.mac.plist'
        self.calls = []; self.loaded = False; self.lock = None
        self.fail_bootstrap = False; self.after_bootout = None
        self.pid = None
        self.foreign_arguments = None; self.on_failed_bootstrap = None

    def tearDown(self):
        if self.lock is not None: os.close(self.lock)
        self.temp.cleanup()

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertNotIn('sudo', argv)
        if argv[:2] == ['/bin/launchctl', 'print']:
            if not self.loaded:
                return subprocess.CompletedProcess(argv, 113, '', 'Could not find service local.aiops.mac in domain')
            arguments = self.foreign_arguments or plistlib.loads(self.plist.read_bytes())['ProgramArguments']
            text = 'arguments = {\n' + '\n'.join('\t' + x for x in arguments) + '\n}\n'
            if self.pid is not None: text += 'pid = ' + str(self.pid) + '\n'
            return subprocess.CompletedProcess(argv, 0, text, '')
        if argv[0] == '/bin/ps':
            config = plistlib.loads(self.plist.read_bytes())
            return subprocess.CompletedProcess(argv, 0, ' '.join(config['ProgramArguments']) + '\n', '')
        if argv[:2] == ['/bin/launchctl', 'bootout']:
            self.assertEqual(argv[2], 'gui/' + str(os.getuid()) + '/local.aiops.mac')
            self.loaded = False
            if self.lock is not None: os.close(self.lock); self.lock = None
            if self.after_bootout is not None:
                callback = self.after_bootout; self.after_bootout = None; callback()
            return subprocess.CompletedProcess(argv, 0, '', '')
        if argv[:2] == ['/bin/launchctl', 'bootstrap']:
            if self.fail_bootstrap:
                self.fail_bootstrap = False
                if self.on_failed_bootstrap is not None: self.on_failed_bootstrap()
                raise subprocess.CalledProcessError(5, argv, stderr='simulated bootstrap failure')
            self.loaded = True
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.fail('Unexpected host command: ' + repr(argv))

    @contextlib.contextmanager
    def platform(self):
        with mock.patch.object(installer.sys, 'platform', 'darwin'), \
             mock.patch.object(installer.Path, 'home', return_value=self.home), \
             mock.patch.object(installer.shutil, 'which', return_value='/test/tool'), \
             mock.patch.object(installer.subprocess, 'run', side_effect=self.command), \
             contextlib.redirect_stdout(io.StringIO()):
            yield

    def install(self, **kwargs):
        with self.platform(): return installer.install(self.app, self.state, **kwargs)

    def job(self, state='accepted', attempt=None):
        store = core.Store(self.state)
        try:
            job = store.create({'repository': 'example/product', 'request_id': 'install-job-001'})
            store.update(job['id'], state=state, attempt=attempt)
            return job['id']
        finally: store.close()

    def test_fresh_install_is_complete_staged_versioned_and_space_safe(self):
        self.assertEqual(self.install(), self.app)
        config = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(config['ProgramArguments'][1], str(self.app / 'Contents/Resources/aiops.py'))
        self.assertEqual(config['ProgramArguments'][3], str(self.state))
        self.assertEqual(installer.installed_binding(self.app, self.state, self.plist), config)
        manifest = common.read_json(self.app / 'Contents/Resources/install-manifest.json')
        self.assertEqual(manifest['version'], common.VERSION)
        self.assertIn('Contents/Resources/core.py', manifest['files'])
        self.assertIn('Contents/Resources/handoff.py', manifest['files'])
        self.assertIn('Contents/Resources/host_observation.py', manifest['files'])
        self.assertFalse((self.state / 'app.sqlite3').exists())
        self.assertFalse((self.state / 'desktop-token').exists())
        self.assertEqual(config['EnvironmentVariables']['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual([x[1] for x in self.calls], ['print', 'print', 'bootstrap'])
        self.assertEqual(list(self.app.parent.glob('.aiops-stage-*')), [])
        subprocess.run(['bash', '-n', str(self.app / 'Contents/MacOS/AIOPS')], check=True)

    def test_existing_install_requires_explicit_update(self):
        self.install(); self.calls.clear()
        with self.assertRaisesRegex(common.AppError, '이미 설치'):
            self.install()
        self.assertEqual(self.calls, [])

    def test_failed_fresh_bootstrap_removes_partial_app_and_plist(self):
        self.fail_bootstrap = True
        with self.assertRaises(subprocess.CalledProcessError): self.install()
        self.assertFalse(self.app.exists()); self.assertFalse(self.plist.exists())
        self.assertFalse((self.state / 'app.sqlite3').exists())
        self.assertEqual(list(self.app.parent.glob('.aiops-stage-*')), [])

    def test_update_preserves_database_tokens_and_settings(self):
        self.install(); self.job()
        token = self.state / 'desktop-token'; token.write_text('test-token-kept'); token.chmod(0o600)
        database = (self.state / 'app.sqlite3').read_bytes()
        self.lock = core.service_lock(self.state)
        self.install(update=True)
        self.assertEqual((self.state / 'app.sqlite3').read_bytes(), database)
        self.assertEqual(token.read_text(), 'test-token-kept')
        self.assertTrue(self.loaded)
        self.assertEqual(list(self.app.parent.glob('.aiops-stage-*')), [])
        installer.installed_binding(self.app, self.state, self.plist)

    def test_large_accepted_job_uses_the_runtime_record_limit(self):
        self.install(); key = self.job()
        store = core.Store(self.state)
        store.update(key, feedback=['completed evidence ' + 'x' * 4194304]); store.close()
        before = (self.state / 'app.sqlite3').read_bytes()
        self.install(update=True)
        self.assertEqual((self.state / 'app.sqlite3').read_bytes(), before)

    def test_busy_jobs_are_rejected_before_any_bootout(self):
        self.install()
        key = self.job('ready')
        for state, attempt in [('ready', None), ('paused', None), ('queued', None), ('blocked_unknown', None), ('accepted', {'id': 'inflight'})]:
            with self.subTest(state=state, attempt=attempt):
                store = core.Store(self.state)
                store.update(key, state=state, attempt=attempt); store.close()
                self.calls.clear()
                with self.assertRaisesRegex(common.AppError, '작업을 완료'):
                    self.install(update=True)
                self.assertEqual(self.calls, [])
                self.assertTrue(self.loaded)

    def test_race_after_bootout_is_rechecked_under_lock_and_old_service_restarted(self):
        self.install(); key = self.job()
        before = self.plist.read_bytes()
        self.lock = core.service_lock(self.state)
        def enqueue():
            store = core.Store(self.state)
            store.update(key, state='queued'); store.close()
        self.after_bootout = enqueue
        self.calls.clear()
        with self.assertRaisesRegex(common.AppError, '작업을 완료'): self.install(update=True)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertTrue(self.loaded)
        self.assertEqual([x[1] for x in self.calls], ['print', 'bootout', 'bootstrap'])

    def test_failed_update_rolls_back_old_bundle_and_plist(self):
        self.install(); self.job()
        before = installer.file_hashes(self.app); old_plist = self.plist.read_bytes()
        self.lock = core.service_lock(self.state)
        self.fail_bootstrap = True
        with mock.patch.object(installer, 'VERSION', '99.0.0'):
            with self.assertRaises(subprocess.CalledProcessError): self.install(update=True)
        self.assertEqual(installer.file_hashes(self.app), before)
        self.assertEqual(self.plist.read_bytes(), old_plist)
        self.assertTrue(self.loaded)
        self.assertEqual(list(self.app.parent.glob('.aiops-stage-*')), [])

    def test_stopped_owned_install_updates_without_bootout(self):
        self.install(); self.loaded = False; self.calls.clear()
        self.install(update=True)
        self.assertEqual([x[1] for x in self.calls], ['print', 'print', 'bootstrap'])

    def test_external_destination_is_refused_before_writes(self):
        with self.platform():
            with self.assertRaisesRegex(common.AppError, 'PER_USER_PATH_REQUIRED'):
                installer.install(Path(self.temp.name) / 'AIOPS.app', self.state)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls, [])

    def test_parent_traversal_cannot_escape_the_per_user_directory(self):
        with self.platform():
            with self.assertRaisesRegex(common.AppError, 'UNSAFE_INSTALL_PATH'):
                installer.install(self.home / '../escaped.app', self.state)
        self.assertFalse((self.home.parent / 'escaped.app').exists())

    def test_symlink_ancestor_is_refused_before_writes(self):
        external = Path(self.temp.name) / 'external'; external.mkdir()
        (self.home / 'Applications').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(common.AppError, 'UNSAFE_INSTALL_PATH'): self.install()
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_manifest_detects_modified_or_extra_installed_content(self):
        self.install()
        path = self.app / 'Contents/Resources/aiops.py'
        path.write_text(path.read_text() + '\n# untracked modification\n')
        self.calls.clear()
        with self.assertRaisesRegex(common.AppError, 'INSTALLATION_MANIFEST_MISMATCH'): self.install(update=True)
        self.assertEqual(self.calls, [])

    def test_foreign_launch_agent_binding_is_refused_without_stopping_it(self):
        self.install()
        config = plistlib.loads(self.plist.read_bytes()); config['Label'] = 'another.service'
        self.plist.write_bytes(plistlib.dumps(config)); self.calls.clear()
        with self.assertRaisesRegex(common.AppError, 'INSTALLATION_BINDING_MISMATCH'): self.install(update=True)
        self.assertEqual(self.calls, [])

    def test_symlinked_private_database_is_not_opened_or_replaced(self):
        self.install()
        other = self.home / 'db-target'; other.write_bytes(b'unchanged'); other.chmod(0o600)
        (self.state / 'app.sqlite3').symlink_to(other)
        self.calls.clear()
        with self.assertRaisesRegex(common.AppError, 'UNSAFE_INSTALLED_FILE'): self.install(update=True)
        self.assertEqual(other.read_bytes(), b'unchanged'); self.assertEqual(self.calls, [])

    def test_corrupt_database_is_not_reinitialized(self):
        self.install()
        database = self.state / 'app.sqlite3'; database.write_bytes(b'not a database'); database.chmod(0o600)
        self.calls.clear()
        with self.assertRaisesRegex(common.AppError, 'INSTALLATION_STATE_UNVERIFIED'): self.install(update=True)
        self.assertEqual(database.read_bytes(), b'not a database'); self.assertEqual(self.calls, [])

    def test_unknown_launchctl_error_is_not_assumed_absent(self):
        self.install(); self.loaded = False
        with self.platform(), mock.patch.object(installer, 'launchctl', return_value=subprocess.CompletedProcess([], 1, '', 'Permission denied')):
            with self.assertRaisesRegex(common.AppError, 'INSTALLER_SERVICE_STATE_UNKNOWN'):
                installer.install(self.app, self.state, update=True)

    def test_live_pid_binding_accepts_space_paths_and_only_stops_owned_label(self):
        self.install(); self.job(); self.pid = 12345
        common.atomic_json(self.state / 'endpoint.json', {'pid': self.pid, 'origin': 'http://127.0.0.1:43120'})
        self.lock = core.service_lock(self.state)
        self.calls.clear(); self.install(update=True)
        self.assertEqual([x[1] for x in self.calls], ['print', '-p', 'bootout', 'print', 'bootstrap'])

    def test_mismatched_live_pid_is_refused_without_bootout(self):
        self.install(); self.job(); self.pid = 12345
        common.atomic_json(self.state / 'endpoint.json', {'pid': 54321})
        self.calls.clear()
        with self.assertRaisesRegex(common.AppError, 'INSTALLATION_BINDING_MISMATCH'): self.install(update=True)
        self.assertEqual([x[1] for x in self.calls], ['print'])

    def test_framework_python_process_representation_preserves_exact_service_argv(self):
        self.install(); self.job(); self.pid = 12345
        common.atomic_json(self.state / 'endpoint.json', {'pid': self.pid})
        config = plistlib.loads(self.plist.read_bytes())
        runtime = '/Library/Frameworks/Python.framework/Versions/3.10/Resources/Python.app/Contents/MacOS/Python'
        native_command = ' '.join([runtime, *config['ProgramArguments'][1:]])
        command = self.command
        def host(argv, **kwargs):
            if argv[0] == '/bin/ps': return subprocess.CompletedProcess(argv, 0, native_command + '\n', '')
            return command(argv, **kwargs)
        with self.platform(), mock.patch.object(installer.subprocess, 'run', side_effect=host), \
                mock.patch.object(installer, 'interpreter_process_path', return_value=runtime) as probe:
            self.assertTrue(installer.loaded_service('gui/' + str(os.getuid()), config, self.state))
            probe.assert_called_once_with(config['ProgramArguments'][0])
            for suffix in (' extra', '--wrong-data-dir'):
                native_command = ' '.join([runtime, *config['ProgramArguments'][1:]]) + suffix
                with self.assertRaisesRegex(common.AppError, 'INSTALLATION_BINDING_MISMATCH'):
                    installer.loaded_service('gui/' + str(os.getuid()), config, self.state)
        self.assertNotIn('bootout', [x[1] for x in self.calls])

    def test_interpreter_probe_is_isolated_fixed_and_rejects_invalid_output(self):
        with mock.patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '/framework/Python\n', '')) as run:
            self.assertEqual(installer.interpreter_process_path('/fixed/python'), '/framework/Python')
            args, kwargs = run.call_args
            self.assertEqual(args[0][:4], ['/fixed/python', '-I', '-S', '-c'])
            self.assertEqual(kwargs['env'], {'PATH': '/usr/bin:/bin'})
            self.assertEqual(kwargs['timeout'], 5)
        for code, output in ((1, '/framework/Python'), (0, 'relative'), (0, '/first\n/second'), (0, '')):
            with mock.patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, output, '')):
                with self.assertRaisesRegex(common.AppError, 'INSTALLATION_BINDING_MISMATCH'):
                    installer.interpreter_process_path('/fixed/python')

    def test_plain_python_cli_does_not_modify_installed_bundle(self):
        self.install()
        original = installer.file_hashes(self.app)
        env = dict(os.environ); env.pop('PYTHONDONTWRITEBYTECODE', None)
        env.pop('PYTHONPYCACHEPREFIX', None)
        result = subprocess.run([sys.executable, str(self.app / 'Contents/Resources/aiops.py'), '--help'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.app / 'Contents/Resources/__pycache__').exists())
        self.assertEqual(installer.file_hashes(self.app), original)
        installer.installed_binding(self.app, self.state, self.plist)

    def test_an_unregistered_live_service_cannot_be_overwritten(self):
        self.install(); self.loaded = False; self.lock = core.service_lock(self.state)
        old = installer.file_hashes(self.app); self.calls.clear()
        with self.assertRaisesRegex(common.AppError, 'SERVICE_ALREADY_RUNNING'): self.install(update=True)
        self.assertEqual(installer.file_hashes(self.app), old)
        self.assertEqual([x[1] for x in self.calls], ['print'])

    def test_install_command_forwards_update_arguments(self):
        self.assertIn('python3 install.py "$@"', (APP / 'Install.command').read_text())
        subprocess.run(['bash', '-n', str(APP / 'Install.command')], check=True)

    def test_first_install_refuses_loaded_label_without_a_plist(self):
        self.loaded = True; self.foreign_arguments = ['/usr/bin/another-app']
        with self.assertRaisesRegex(common.AppError, 'EXISTING_LAUNCH_AGENT'): self.install()
        self.assertFalse(self.app.exists()); self.assertFalse(self.plist.exists())
        self.assertEqual([x[1] for x in self.calls], ['print'])

    def test_fixed_installer_lock_serializes_different_data_directories(self):
        folder = self.home / 'Library/Application Support/AIOPS Installer'
        common.private_directory(folder)
        lock = folder / 'installer.lock'
        import fcntl
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.platform():
                with self.assertRaisesRegex(common.AppError, 'INSTALLER_ALREADY_RUNNING'):
                    installer.install(self.app, self.home / 'Another Data')
            self.assertEqual(self.calls, []); self.assertFalse(self.app.exists())
        finally: os.close(fd)

    def test_failed_bootstrap_collision_never_boots_out_foreign_registration(self):
        self.fail_bootstrap = True
        def foreign_registration():
            self.loaded = True; self.foreign_arguments = ['/usr/bin/another-app']
        self.on_failed_bootstrap = foreign_registration
        with self.assertRaisesRegex(common.AppError, '서비스의 설치 binding'): self.install()
        self.assertTrue(self.loaded); self.assertTrue(self.app.exists()); self.assertTrue(self.plist.exists())
        self.assertNotIn('bootout', [x[1] for x in self.calls])

    def test_legacy_owned_python_cache_is_allowed_without_executing_it(self):
        self.install()
        (self.app / 'Contents/Resources/install-manifest.json').unlink()
        cache = self.app / 'Contents/Resources/__pycache__'; cache.mkdir(mode=0o700)
        (cache / 'core.cpython-310.pyc').write_bytes(b'opaque legacy bytecode')
        self.install(update=True)
        self.assertFalse(cache.exists())
        self.assertTrue((self.app / 'Contents/Resources/install-manifest.json').exists())

    def test_legacy_cache_for_an_unknown_module_is_refused(self):
        self.install()
        (self.app / 'Contents/Resources/install-manifest.json').unlink()
        cache = self.app / 'Contents/Resources/__pycache__'; cache.mkdir(mode=0o700)
        (cache / 'external.cpython-310.pyc').write_bytes(b'unknown')
        self.calls.clear()
        with self.assertRaisesRegex(common.AppError, 'UNEXPECTED_INSTALLED_CONTENT'): self.install(update=True)
        self.assertEqual(self.calls, [])

    def test_linked_launchd_logs_are_refused_before_bootstrap(self):
        for update in (False, True):
            for name, kind in [('service.log', 'symlink'), ('service-errors.log', 'hardlink')]:
                with self.subTest(update=update, name=name, kind=kind), tempfile.TemporaryDirectory() as d:
                    home = Path(d) / 'User'; home.mkdir(mode=0o700)
                    original = (self.home, self.app, self.state, self.plist, self.loaded)
                    self.home = home; self.app = home / 'Applications/AIOPS.app'
                    self.state = home / 'Library/Application Support/AIOPS'
                    self.plist = home / 'Library/LaunchAgents/local.aiops.mac.plist'; self.loaded = False
                    try:
                        if update: self.install()
                        else: common.private_directory(self.state)
                        log = self.state / name
                        if log.exists(): log.unlink()
                        target = home / 'unrelated'; target.write_text('preserve'); target.chmod(0o600)
                        if kind == 'symlink': log.symlink_to(target)
                        else: os.link(target, log)
                        self.calls.clear()
                        with self.assertRaisesRegex(common.AppError, 'UNSAFE_INSTALLED_FILE'):
                            self.install(update=update)
                        self.assertEqual(target.read_text(), 'preserve')
                        self.assertNotIn('bootstrap', [x[1] for x in self.calls])
                        self.assertNotIn('bootout', [x[1] for x in self.calls])
                    finally:
                        self.home, self.app, self.state, self.plist, self.loaded = original

    def test_readable_owned_legacy_logs_are_kept_without_truncation(self):
        self.install()
        for name in ('service.log', 'service-errors.log'):
            path = self.state / name; path.write_text('old diagnostics'); path.chmod(0o644)
        self.install(update=True)
        self.assertTrue(all((self.state / name).read_text() == 'old diagnostics' for name in ('service.log', 'service-errors.log')))

    def test_hardlinked_installer_lock_is_refused_before_host_commands(self):
        folder = self.home / 'Library/Application Support/AIOPS Installer'; common.private_directory(folder)
        target = self.home / 'unrelated'; target.write_text('unchanged'); target.chmod(0o600)
        os.link(target, folder / 'installer.lock')
        with self.assertRaisesRegex(common.AppError, 'UNSAFE_INSTALLED_FILE'): self.install()
        self.assertEqual(self.calls, []); self.assertEqual(target.read_text(), 'unchanged')


if __name__ == '__main__': unittest.main()
