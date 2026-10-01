"""Real flock concurrency around injected fixed commands; no provider calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import control_plane_fable as fable
import control_plane_program_astra as bridge
import control_plane_program_receipts as journal
from test_control_plane_fable import HEAD, REPO, mock_root_evidence


class AccountLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        mock_root_evidence(self, self.root)
        for replacement in (patch.object(fable, "ACCOUNT_MODEL_LOCK", self.root / "account-model.lock"),
                            patch.object(fable, "protected_run_chain"),
                            patch.object(fable.os, "geteuid", return_value=0),
                            patch.object(fable, "load_program_support")):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.store = journal.Receipts(self.root, "a" * 64, trust=lambda *args, **kwargs: None)
        self.binding = {"repository": REPO, "program": "zari", "node": "sp-1", "head": HEAD}
        self.payload = {"operation": "audit", "repository": REPO, "issue": 1, "github_token": "test-token"}

    def test_each_model_command_contends_on_one_lease_before_context_or_admission(self):
        commands = ((["preflight"], None),
                    (["audit", "--repository", REPO, "--pr", "1", "--head", HEAD,
                      "--gate", "ARCHITECTURE", "--depth", "A3"], None),
                    (["consult", "--repository", REPO, "--issue", "1", "--comment", "2"], None),
                    (["program"], self.payload),
                    (["program"], {**self.payload, "operation": "consult"}),
                    (["program"], {**self.payload, "operation": "quota-resume"}))
        with fable.account_model_lock() as acquired:
            self.assertTrue(acquired)
            for argv, payload in commands:
                output = io.StringIO()
                with self.subTest(argv=argv, payload=payload), \
                        patch.object(fable, "production_context") as context, \
                        patch.object(bridge, "run") as run, \
                        patch.object(fable.sys, "stdin", io.StringIO(json.dumps(payload))), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(fable.main(argv), 0)
                    self.assertEqual(json.loads(output.getvalue())["status"], "BUSY")
                    context.assert_not_called()
                    run.assert_not_called()
        self.assertEqual(list(self.store.journal.glob("*.admission.json")), [])

    def test_program_lease_spans_admission_model_and_publication_without_nested_deadlock(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        returns = []
        def model():
            with fable.account_model_lock() as acquired:
                self.assertFalse(acquired)
            entered.set()
            self.assertTrue(release.wait(3))
            return {"status": "POSTED", "result": "PASS", "program_binding": self.binding}
        def program(*args):
            return self.store.execute("audit", self.binding, model)
        def run_program():
            returns.append(fable.main(["program"]))
            finished.set()
        output = io.StringIO()
        with patch.object(fable, "production_context", return_value=(object(), "test")), \
                patch.object(bridge, "run", side_effect=program), \
                patch.object(fable.sys, "stdin", io.StringIO(json.dumps(self.payload))), \
                contextlib.redirect_stdout(output):
            worker = threading.Thread(target=run_program, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.store.read("audit", self.binding)["status"], "RUNNING")
                self.assertEqual(fable.main(["preflight"]), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], "BUSY")
            finally:
                release.set()
                worker.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(returns, [0])
        self.assertEqual(self.store.read("audit", self.binding)["status"], "POSTED")
        with fable.account_model_lock() as acquired:
            self.assertTrue(acquired)

    def test_failed_manual_model_releases_lease_and_does_not_block_next_program(self):
        output = io.StringIO()
        with patch.object(fable, "production_context", return_value=(object(), "test")), \
                patch.object(fable, "preflight", side_effect=fable.FableError("guard stopped")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(fable.main(["preflight"]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "ERROR")
        with fable.account_model_lock() as acquired:
            self.assertTrue(acquired)

    def test_read_only_program_commands_remain_available_during_manual_audit(self):
        with fable.account_model_lock():
            for command, payload, method in (("program", {**self.payload, "operation": "check"}, "run"),
                                            ("program-reconcile", self.payload, "operator_reconcile")):
                output = io.StringIO()
                with self.subTest(command=command), \
                        patch.object(fable, "production_context", return_value=(object(), "test")), \
                        patch.object(bridge, method, return_value={"status": "CHECKED"}) as called, \
                        patch.object(fable.sys, "stdin", io.StringIO(json.dumps(payload))), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(fable.main([command]), 0)
                    self.assertEqual(json.loads(output.getvalue())["status"], "CHECKED")
                    called.assert_called_once()

    def test_quota_preflight_and_retry_nest_existing_journal_locks_inside_account_lease(self):
        from test_control_plane_program_quota import QuotaTests
        fixture = QuotaTests("test_one_retry_preserves_original_error_and_exact_program_binding")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fail()
        def preflight():
            with fable.account_model_lock() as acquired:
                self.assertFalse(acquired)
            with fixture.store.locked("model.lock") as acquired:
                self.assertFalse(acquired)
            return {"status": "PASS", "extra_usage": {"overageStatus": "rejected"}}
        fixture.preflight = preflight
        with fable.account_model_lock() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(fixture.q.resume("audit", fixture.binding, fixture.invoke)["status"], "POSTED")
        self.assertEqual((fixture.preflights, fixture.invocations), (1, 1))

    def test_lock_symlink_hardlink_or_nonprivate_mode_is_rejected(self):
        path = self.root / "account-model.lock"
        target = self.root / "target"
        target.write_bytes(b"")
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(OSError), fable.account_model_lock():
            self.fail("symlink was accepted")
        path.unlink()
        os.link(target, path)
        with self.assertRaisesRegex(fable.FableError, "protected"), fable.account_model_lock():
            self.fail("hardlink was accepted")
        path.unlink()
        path.write_bytes(b"")
        path.chmod(0o644)
        with self.assertRaisesRegex(fable.FableError, "protected"), fable.account_model_lock():
            self.fail("nonprivate lock was accepted")


if __name__ == "__main__":
    unittest.main()
