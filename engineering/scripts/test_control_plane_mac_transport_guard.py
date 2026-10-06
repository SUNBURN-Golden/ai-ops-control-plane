"""Unresolved transport delivery cannot become a new native model execution."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import common
import core
import transport_guard
import test_control_plane_mac_app as legacy


def journal(directory, state='UNKNOWN'):
    folder = directory / 'relay'; folder.mkdir(mode=0o700)
    path = folder / 'requests.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE requests (id TEXT PRIMARY KEY,digest TEXT,state TEXT,receipt TEXT,created INTEGER)')
    request_id = 'existing-kix-request-001'; digest = 'a' * 64
    receipt = {'request_id': request_id, 'payload_sha256': digest, 'state': state, 'task_completion': 'NOT_CHECKED'}
    db.execute('INSERT INTO requests VALUES (?,?,?,?,?)', (request_id, digest, state, json.dumps(receipt), 0))
    db.commit(); db.close(); path.chmod(0o600)
    return path


class JournalTests(unittest.TestCase):
    def test_unknown_and_submitted_receipts_are_preserved_and_never_authorize_execution(self):
        for state in ('UNKNOWN', 'SUBMITTED'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as d:
                directory = Path(d); path = journal(directory, state); before = path.read_bytes()
                self.assertEqual(transport_guard.unresolved(directory), [{'request_id': 'existing-kix-request-001', 'state': state}])
                with self.assertRaises(common.AppError) as caught: transport_guard.require_clear(directory)
                self.assertEqual(caught.exception.code, 'TRANSPORT_EXECUTION_UNRESOLVED')
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ['requests.sqlite3'])

    def test_missing_empty_corrupt_and_unsafe_journals_fail_conservatively(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            self.assertEqual(transport_guard.unresolved(directory), [])
            path = journal(directory); path.write_bytes(b'broken database')
            with self.assertRaises(common.AppError) as caught: transport_guard.unresolved(directory)
            self.assertEqual(caught.exception.code, 'TRANSPORT_JOURNAL_UNVERIFIED')
            path.unlink(); path.symlink_to(directory / 'unrelated')
            with self.assertRaises(common.AppError): transport_guard.unresolved(directory)

    def test_unknown_state_or_mismatched_identity_is_not_a_clear_receipt(self):
        for mutation in ["UPDATE requests SET state='RESOLVED'", "UPDATE requests SET digest='b'"]:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                directory = Path(d); path = journal(directory)
                with sqlite3.connect(path) as db: db.execute(mutation)
                with self.assertRaises(common.AppError) as caught: transport_guard.unresolved(directory)
                self.assertEqual(caught.exception.code, 'TRANSPORT_JOURNAL_UNVERIFIED')

    def test_new_request_ids_do_not_bypass_unknown_and_existing_replays_are_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            store = core.Store(Path(d))
            try:
                request = {'repository': 'example/product', 'request_id': 'existing-native-request'}
                existing = store.create(request); path = journal(store.directory); before = path.read_bytes()
                self.assertEqual(store.create(request)['id'], existing['id'])
                for repo in ('example/product', 'example/other'):
                    with self.assertRaises(common.AppError): store.create({'repository': repo, 'request_id': 'new-request-' + repo.split('/')[1]})
                self.assertEqual(len(store.jobs()), 1); self.assertEqual(path.read_bytes(), before)
            finally: store.close()


class RuntimeTests(unittest.TestCase):
    setUp = legacy.AppTests.setUp
    tearDown = legacy.AppTests.tearDown
    new = legacy.AppTests.new

    def test_existing_queued_job_is_held_without_worker_calls_or_automatic_retries(self):
        job = self.new(); self.store.update(job['id'], phase='planning', state='planning')
        journal(self.store.directory)
        with mock.patch.object(self.engine, '_launch') as launch:
            self.engine.tick()
        current = self.store.get(job['id'])
        launch.assert_not_called(); self.assertEqual(current['calls'], 0); self.assertIsNone(current['attempt'])
        self.assertEqual(current['state'], 'needs_user'); self.assertEqual(current['not_before'], 0)
        self.assertEqual(current['blocker']['code'], 'TRANSPORT_EXECUTION_UNRESOLVED')

    def test_receipt_appearing_during_remote_observation_is_rechecked_before_launch(self):
        job = self.new(); self.store.update(job['id'], phase='planning', state='planning')
        def observation(_):
            journal(self.store.directory); return {'mode': 'native'}
        with mock.patch.object(self.engine.repos, 'execution_admission', side_effect=observation), \
                mock.patch.object(self.engine, '_launch') as launch:
            with self.assertRaises(common.AppError): self.engine.launch(job, 'planner')
        launch.assert_not_called(); self.assertEqual(self.store.get(job['id'])['calls'], 0)


if __name__ == '__main__': unittest.main()
