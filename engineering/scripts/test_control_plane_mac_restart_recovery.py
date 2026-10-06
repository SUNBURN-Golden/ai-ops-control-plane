"""Candidate operator recovery: real boot proof, failure only, no force override."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import common
import core

spec = importlib.util.spec_from_file_location('mac_recovery', APP.parent / 'tools/mac_recover_after_restart.py')
recovery = importlib.util.module_from_spec(spec); spec.loader.exec_module(recovery)
OLD = '11111111-1111-1111-1111-111111111111'
NEW = '22222222-2222-2222-2222-222222222222'
DECISION = 'https://github.com/example/product/pull/1#issuecomment-123'


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.host_patch = mock.patch.object(recovery, 'host_id', return_value='same-mac')
        self.host_patch.start(); self.addCleanup(self.host_patch.stop)
        self.boot_patch = mock.patch.object(recovery, 'boot_time', return_value=200)
        self.boot_patch.start(); self.addCleanup(self.boot_patch.stop)
        self.repos_patch = mock.patch.object(recovery, 'Repositories', autospec=True)
        self.repos = self.repos_patch.start().return_value; self.addCleanup(self.repos_patch.stop)
        self.repos.head.return_value = 'b' * 40; self.repos.clean.return_value = True
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = core.Store(self.root / 'state')
        job = self.store.create({'repository': 'example/product', 'goal': 'Keep original scope', 'request_id': 'request-001'})
        self.attempt = {'id': 'a' * 32, 'binding': 'bound', 'head': 'b' * 40, 'role': 'planner', 'started': 20}
        self.job = self.store.update(job['id'], state='unknown', phase='planning', pause_requested=True, attempt=self.attempt, calls=1)
        folder = self.store.directory / 'jobs' / job['id'] / self.attempt['id']
        folder.mkdir(parents=True, mode=0o700); self.folder = folder
        common.atomic_json(folder / 'request.json', {'attempt_id': self.attempt['id'], 'binding': 'bound'})
        (folder / 'last-message.json').write_text('{"status":"complete"}')
        self.witness_path = self.root / 'witness.json'
        with mock.patch.object(recovery, 'boot_id', return_value=OLD), mock.patch.object(recovery, 'boot_time', return_value=10), mock.patch.object(recovery.time, 'time', return_value=100):
            self.witness = recovery.prepare(self.store.directory, job['id'], self.witness_path)
        self.approved_hash = common.digest(self.witness)

    def tearDown(self): self.store.close(); self.temp.cleanup()

    def test_prepare_preserves_entire_job_and_never_replaces_witness(self):
        self.assertEqual(self.store.get(self.job['id']), self.job)
        self.assertEqual(self.witness_path.stat().st_mode & 0o777, 0o600)
        with mock.patch.object(recovery, 'boot_id', return_value=OLD), mock.patch.object(recovery, 'boot_time', return_value=10), self.assertRaises(FileExistsError):
            recovery.prepare(self.store.directory, self.job['id'], self.witness_path)

    def test_same_boot_refuses_before_service_mutation(self):
        with mock.patch.object(recovery, 'boot_id', return_value=OLD), mock.patch.object(recovery.install, 'launchctl') as launch:
            with self.assertRaisesRegex(common.AppError, 'HOST_RESTART_REQUIRED'):
                recovery.apply(self.store.directory, self.witness_path, DECISION, self.approved_hash)
            launch.assert_not_called()
        self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_changed_boot_records_abortion_without_using_model_report(self):
        old_files = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        lock = core.service_lock(self.store.directory)
        try: result = recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)
        finally: os.close(lock)
        self.assertEqual(result['state'], 'paused'); self.assertIsNone(result['attempt'])
        self.assertEqual(result['last_terminal']['status'], 'aborted')
        self.assertEqual(result['last_terminal']['error'], 'HOST_RESTARTED_WITHOUT_RECEIPT')
        self.assertEqual(result['calls'], 1); self.assertEqual(result['goal'], self.job['goal'])
        self.assertIsNone(result['plan'])
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, old_files)
        self.assertEqual(self.store.events(result['id'])[-1]['kind'], 'host_restart_recovery')
        # A replay cannot release any later reservation or launch a model.
        with self.assertRaisesRegex(common.AppError, 'PAUSED_UNKNOWN_REQUIRED'):
            recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)
        self.assertEqual(self.store.get(result['id']), result)

    def test_late_receipt_prevents_recovery(self):
        common.atomic_json(self.folder / 'receipt.json', {})
        with self.assertRaisesRegex(common.AppError, 'TERMINAL_RECEIPT_AVAILABLE'):
            recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)
        self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_changed_job_or_request_refuses_and_rolls_back(self):
        self.store.update(self.job['id'], calls=2)
        with self.assertRaisesRegex(common.AppError, 'RECOVERY_WITNESS_MISMATCH'):
            recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)
        self.assertIsNotNone(self.store.get(self.job['id'])['attempt'])
        self.store.update(self.job['id'], calls=1, updated=self.job['updated'])
        common.atomic_json(self.folder / 'request.json', {'attempt_id': self.attempt['id'], 'binding': 'wrong'})
        with self.assertRaisesRegex(common.AppError, 'REQUEST_BINDING_MISMATCH'):
            recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)

    def test_wrong_host_directory_or_user_refuses(self):
        for field, value in [('state', '/wrong'), ('uid', os.getuid() + 1), ('attempt_id', 'c' * 32), ('binding', 'wrong')]:
            bad = dict(self.witness, **{field: value})
            with self.subTest(field=field), self.assertRaises(common.AppError):
                recovery.reconcile(self.store, bad, NEW, DECISION, common.digest(bad))
        self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_witness_copied_to_another_mac_cannot_release_original_owner(self):
        with mock.patch.object(recovery, 'host_id', return_value='different-mac'):
            with self.assertRaisesRegex(common.AppError, 'RECOVERY_HOST_MISMATCH'):
                recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)
        self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_unrelated_nonterminal_job_prevents_recovery(self):
        other = self.store.create({'repository': 'example/other', 'goal': 'Other scope', 'request_id': 'request-002'})
        for state in ('queued', 'paused', 'ready', 'unknown'):
            self.store.update(other['id'], state=state)
            with self.subTest(state=state), self.assertRaisesRegex(common.AppError, 'OTHER_JOB_NOT_TERMINAL'):
                recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)
        self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_missing_decision_pointer_cannot_touch_service(self):
        with mock.patch.object(recovery.install, 'launchctl') as launch:
            with self.assertRaisesRegex(common.AppError, 'USER_DECISION_POINTER_REQUIRED'):
                recovery.apply(self.store.directory, self.witness_path, '', self.approved_hash)
            launch.assert_not_called()

    def test_mutated_witness_cannot_use_original_approved_hash(self):
        bad = dict(self.witness, boot_id=NEW)
        with self.assertRaisesRegex(common.AppError, 'APPROVED_WITNESS_MISMATCH'):
            recovery.reconcile(self.store, bad, OLD, DECISION, self.approved_hash)
        self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_forged_boot_id_still_fails_independent_kernel_boot_time(self):
        bad = dict(self.witness, boot_id=NEW)
        with mock.patch.object(recovery, 'boot_time', return_value=10):
            with self.assertRaisesRegex(common.AppError, 'HOST_RESTART_REQUIRED'):
                recovery.reconcile(self.store, bad, OLD, DECISION, common.digest(bad))
        self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_read_only_checkout_changes_retain_unknown_and_entire_reservation(self):
        for head, clean in [('c' * 40, True), ('b' * 40, False)]:
            self.repos.head.return_value = head; self.repos.clean.return_value = clean
            with self.subTest(head=head, clean=clean), self.assertRaisesRegex(common.AppError, 'READ_ONLY_ROLE_MODIFIED_CHECKOUT'):
                recovery.reconcile(self.store, self.witness, NEW, DECISION, self.approved_hash)
            self.assertEqual(self.store.get(self.job['id']), self.job)

    def test_readonly_precheck_refuses_missing_shm_without_creating_it(self):
        state = self.root / 'wal-only'; state.mkdir(mode=0o700)
        for name in ('app.sqlite3', 'app.sqlite3-wal'):
            path = state / name; path.touch(mode=0o600)
        with mock.patch.object(recovery.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(common.AppError, 'INSTALLATION_STATE_UNVERIFIED'):
                recovery.jobs_readonly(state)
            connect.assert_not_called()
        self.assertFalse((state / 'app.sqlite3-shm').exists())

    def test_readonly_precheck_refuses_linked_sidecar(self):
        state = self.root / 'linked'; state.mkdir(mode=0o700)
        (state / 'app.sqlite3').touch(mode=0o600)
        (state / 'app.sqlite3-wal').symlink_to(self.witness_path)
        with mock.patch.object(recovery.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(common.AppError, 'UNSAFE_INSTALLED_FILE'):
                recovery.jobs_readonly(state)
            connect.assert_not_called()


if __name__ == '__main__': unittest.main()
