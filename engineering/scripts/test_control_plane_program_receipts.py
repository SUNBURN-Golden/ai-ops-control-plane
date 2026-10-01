"""Execution fences and root reconciliation; no providers or host changes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import control_plane_fable as fable
import control_plane_program_astra as bridge
import control_plane_program_receipts as journal


class JournalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Production chain protection is independently covered below; temporary
        # test directories deliberately sit under a writable /tmp parent.
        self.trust = patch.object(bridge, "protected"); self.trust.start(); self.addCleanup(self.trust.stop)
        self.store = bridge.Receipts(self.root, "a" * 64)
        self.binding = {"repository": "BeautifulMind-JT/ZARI", "issue": 3,
                        "program": "zari", "node": "sp-1", "head": "b" * 40,
                        "plan_commit": "c" * 40, "task_revision": "r1", "writer_launch": "d" * 24}

    def posted(self, binding=None):
        return {"status": "POSTED", "result": "PASS", "program_binding": binding or self.binding,
                "run": "run-ok", "comment_url": "https://github.com/example"}

    def failure(self, **changes):
        return {"schema": "FABLE_FAILURE_V1", "schema_version": 1, "run": "run-1", "kind": "audit",
                "program_binding": self.binding, "terminal_evidence": "VERIFIED",
                "process_terminated": True, "publication_state": "NOT_STARTED",
                "error_code": "MODEL_EXECUTION_FAILED", **changes}

    def fail(self, evidence=None):
        def invoke():
            raise fable.FableError("session quota", failure=evidence or self.failure())
        with self.assertRaises(fable.FableError): self.store.execute("audit", self.binding, invoke)
        return self.store.read("audit", self.binding)

    def test_projection_deletion_keeps_original_failure_and_no_model_call(self):
        first = self.fail()
        (self.root / (first["admission"] + ".json")).unlink()
        self.assertEqual(self.store.read("audit", self.binding), first)
        self.assertEqual(self.store.execute("audit", self.binding, self.fail)["status"], "ERROR")

    def test_admission_crash_survives_projection_deletion(self):
        self.store.write("audit", self.binding, {"status": "RUNNING"})
        (self.root / (self.store.key("audit", self.binding) + ".json")).unlink()
        self.assertEqual(self.store.execute("audit", self.binding, lambda: self.fail())["status"], "UNKNOWN")

    def test_changed_head_plan_revision_writer_question_and_tool_cannot_bypass_ancestor(self):
        first = self.fail()
        for field, new in [("head", "e" * 40), ("plan_commit", "f" * 40), ("task_revision", "r2"),
                           ("writer_launch", "e" * 24), ("question", 88), ("issue", 44)]:
            with self.subTest(field=field):
                changed = {**self.binding, field: new}
                result = self.store.execute("audit", changed, lambda: self.fail())
                self.assertEqual(result["status"], "UNKNOWN"); self.assertEqual(result["ancestor_admission"], first["admission"])
                self.assertEqual(self.store.read("audit", changed)["status"], "UNKNOWN")
        upgraded = bridge.Receipts(self.root, "f" * 64)
        self.assertEqual(upgraded.execute("audit", self.binding, lambda: self.fail())["status"], "UNKNOWN")

    def test_other_node_repository_and_action_do_not_inherit_execution_fence(self):
        self.fail()
        for changed in ({**self.binding, "node": "sp-2"}, {**self.binding, "repository": "BeautifulMind-JT/other"}):
            self.assertEqual(self.store.execute("audit", changed, lambda: self.posted(changed))["status"], "POSTED")
        self.assertEqual(self.store.execute("consult", self.binding, self.posted)["status"], "POSTED")

    def test_old_matching_pass_cannot_bypass_newer_unresolved_execution(self):
        self.store.execute("audit", self.binding, self.posted)
        newer = {**self.binding, "head": "f" * 40}
        def failed(): raise fable.FableError("lost response")
        with self.assertRaises(fable.FableError): self.store.execute("audit", newer, failed)
        self.assertEqual(self.store.read("audit", self.binding)["status"], "UNKNOWN")
        self.assertEqual(self.store.execute("audit", self.binding, lambda: self.fail())["status"], "UNKNOWN")

    def test_reconcile_is_idempotent_preserves_outcome_and_does_not_retry_or_grant_pass(self):
        first = self.fail(); key = first["admission"]
        before = (self.store.journal / (key + ".outcome.json")).read_bytes()
        verify = lambda _: self.failure()
        result = self.store.reconcile(key, first["state_version"], verify)
        self.assertEqual(result["status"], "RECONCILED_FAILED")
        self.assertEqual(self.store.reconcile(key, first["state_version"], lambda _: self.fail()), result)
        self.assertEqual((self.store.journal / (key + ".outcome.json")).read_bytes(), before)
        self.assertEqual(self.store.execute("audit", self.binding, lambda: self.fail())["status"], "ERROR")
        changed = {**self.binding, "head": "f" * 40}
        self.assertEqual(self.store.read("audit", changed)["status"], "MISSING")
        self.assertEqual(self.store.execute("audit", changed, lambda: self.posted(changed))["status"], "POSTED")

    def test_operator_can_settle_unverified_overage_guard_without_retry_or_pass(self):
        evidence = self.failure(error_code="OVERAGE_UNVERIFIED", extra_usage=None,
                                reset_at_epoch_ms=None, limit_type=None,
                                execution={"guard_stop": {"error_code": "OVERAGE_UNVERIFIED"}})
        first = self.fail(evidence)
        original = self.store._path(first["admission"], "outcome").read_bytes()
        result = self.store.reconcile(first["admission"], first["state_version"], lambda _: evidence)
        self.assertEqual(result["status"], "RECONCILED_FAILED")
        self.assertEqual(self.store._path(first["admission"], "outcome").read_bytes(), original)
        self.assertEqual(self.store.read("audit", self.binding)["status"], "ERROR")
        self.assertEqual(self.store.execute("audit", self.binding, lambda: self.fail())["status"], "ERROR")

    def test_reconciliation_stale_version_and_unknown_process_publication_or_binding_refused(self):
        for change in ({"process_terminated": False}, {"publication_state": "ATTEMPTED"},
                       {"terminal_evidence": "UNKNOWN"}, {"error_code": "RESULT_INVALID"},
                       {"program_binding": {**self.binding, "head": "f" * 40}}, {"kind": "consult"}):
            with self.subTest(change=change):
                first = self.store.read("audit", self.binding)
                if first["status"] == "MISSING": first = self.fail()
                with self.assertRaises(journal.ReceiptError):
                    self.store.reconcile(first["admission"], first["state_version"], lambda _: self.failure(**change))
        first = self.store.read("audit", self.binding)
        with self.assertRaisesRegex(journal.ReceiptError, "stale"):
            self.store.reconcile(first["admission"], "0" * 64, lambda _: self.failure())

    def test_missing_terminal_archive_and_plain_error_do_not_reconcile(self):
        self.store.write("audit", self.binding, {"status": "ERROR", "reason": "HTTP 429 success"})
        first = self.store.read("audit", self.binding)
        with self.assertRaisesRegex(journal.ReceiptError, "unproven"):
            self.store.reconcile(first["admission"], first["state_version"], lambda _: self.fail())

    def test_journal_result_cannot_be_replaced_by_edited_projection(self):
        result = self.fail()
        (self.root / (result["admission"] + ".json")).write_text(json.dumps(self.posted()))
        self.assertEqual(self.store.read("audit", self.binding)["status"], "ERROR")
        with self.assertRaisesRegex(journal.ReceiptError, "conflicts"):
            self.store.write("audit", self.binding, self.posted())

    def test_orphan_or_partial_event_fails_closed_before_new_invocation(self):
        self.fail(); key = self.store.key("audit", self.binding)
        outcome = self.store.journal / (key + ".outcome.json")
        outcome.write_text('{"schema_version":')
        with self.assertRaises(journal.ReceiptError): self.store.execute("audit", {**self.binding, "head": "f" * 40}, lambda: self.fail())

    def test_journal_symlink_and_hardlink_refused(self):
        result = self.fail(); path = self.store.journal / (result["admission"] + ".outcome.json")
        original = path.read_bytes(); path.unlink()
        target = self.root / "target"; target.write_bytes(original); path.symlink_to(target)
        with self.assertRaises(journal.ReceiptError): self.store.read("audit", self.binding)
        path.unlink(); os.link(target, path)
        with self.assertRaisesRegex(journal.ReceiptError, "hard links"): self.store.read("audit", self.binding)

    def test_scope_and_global_locks_block_reconcile_and_new_admission(self):
        first = self.fail()
        with self.store.locked("model.lock"):
            self.assertEqual(self.store.reconcile(first["admission"], first["state_version"], lambda _: self.failure())["status"], "BUSY")
            other = {**self.binding, "node": "sp-2"}
            self.assertEqual(self.store.execute("audit", other, lambda: self.fail())["status"], "BUSY")
            self.assertEqual(self.store.read("audit", other)["status"], "MISSING")

    def test_admission_guard_rechecks_queue_under_global_lock_without_admitting(self):
        def became_due():
            with self.store.locked("model.lock") as acquired: self.assertFalse(acquired)
            return {"status": "QUEUED", "quota_attempt": "f" * 64}
        result = self.store.execute("audit", self.binding, lambda: self.fail(), admission_guard=became_due)
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(self.store.read("audit", self.binding)["status"], "MISSING")

    def test_old_flat_error_still_fences_changed_binding(self):
        key = self.store.key("audit", self.binding)
        (self.root / (key + ".json")).write_text(json.dumps({"action": "audit", "binding": self.binding,
                                                           "tool_sha256": "a" * 64, "status": "ERROR"}))
        self.assertEqual(self.store.execute("audit", {**self.binding, "head": "f" * 40}, lambda: self.fail())["status"], "UNKNOWN")
        (self.root / (key + ".json")).unlink()
        upgraded = bridge.Receipts(self.root, "f" * 64)
        self.assertEqual(upgraded.execute("audit", {**self.binding, "head": "e" * 40}, lambda: self.fail())["status"], "UNKNOWN")

    def test_operator_reconcile_is_separate_and_rejects_paths_grants_or_nonroot(self):
        payload = {"repository": self.binding["repository"], "admission": "a" * 64,
                   "expected_version": "b" * 64, "github_token": "test-token"}
        for change in ({"admission": "../escape"}, {"result": "PASS"}, {"run": "invented"}):
            with self.subTest(change=change), self.assertRaises(journal.ReceiptError):
                bridge.operator_reconcile(None, fable, {**payload, **change})
        with patch.object(bridge.os, "geteuid", return_value=99), self.assertRaises(journal.ReceiptError):
            bridge.operator_reconcile(None, fable, payload)

    def test_opened_descriptor_identity_is_checked_after_path_trust(self):
        result = self.fail()
        path = self.store.journal / (result["admission"] + ".outcome.json")
        def trust(name, directory=False, *, info=None):
            if Path(name) == path and info is not None:
                raise journal.ReceiptError("opened descriptor ownership changed")
        self.store.trust = trust
        with self.assertRaisesRegex(journal.ReceiptError, "ownership changed"):
            self.store.read("audit", self.binding)


class ChainTests(unittest.TestCase):
    def test_writable_parent_is_checked_before_any_admission(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "program"; root.mkdir(mode=0o700)
            # /tmp is writable regardless of whether this test runs as root.
            with self.assertRaises(journal.ReceiptError): bridge.Receipts(root, "a" * 64)

    def test_protected_descriptor_rejects_foreign_owner_and_writable_record(self):
        from types import SimpleNamespace
        import stat
        for uid, mode in [(91, 0o600), (0, 0o622)]:
            with self.subTest(uid=uid, mode=mode), self.assertRaises(journal.ReceiptError):
                bridge.protected("unused", info=SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid))


if __name__ == "__main__":
    unittest.main()
