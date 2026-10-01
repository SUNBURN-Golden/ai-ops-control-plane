"""Provider-free quota retry tests of durable admission and race boundaries."""
import tempfile
import unittest

import control_plane_program_receipts as journal
from control_plane_program_quota import Quota


class Failure(RuntimeError):
    def __init__(self, failure):
        super().__init__("bounded failure")
        self.failure = failure


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = journal.Receipts(self.tmp.name, "f" * 64, trust=lambda *args, **kwargs: None)
        self.binding = {"repository": "x/y", "program": "p", "node": "n", "issue": 1,
                        "head": "a" * 40, "writer_launch": "b" * 24, "plan_commit": "c" * 40}
        self.now = 2000000
        self.evidence = {}
        self.preflights = 0
        self.invocations = 0
        self.current = lambda action, binding: {"binding": binding, "tool_sha256": self.store.tool_sha}
        self.preflight = lambda: {"status": "PASS", "extra_usage": {"overageStatus": "rejected"}}
        self.q = Quota(self.store, lambda run: self.evidence[run], self.pf,
                       lambda action, binding: self.current(action, binding), clock_ms=lambda: self.now)

    def pf(self):
        self.preflights += 1
        return self.preflight()

    def quota_failure(self, binding=None, *, action="audit", reset=1000000, **changes):
        binding = binding or self.binding
        run = "run-" + str(len(self.evidence))
        failure = {"schema": "FABLE_FAILURE_V1", "schema_version": 1, "run": run,
                   "kind": action, "program_binding": binding, "error_code": "MODEL_RATE_LIMIT",
                   "terminal_evidence": "VERIFIED", "process_terminated": True, "model_attempted": True,
                   "archive_state": "SEALED", "publication_state": "NOT_STARTED",
                   "adapter_profile": "claude-cli-2.1.285-observed-v1", "api_error_status": 429,
                   "limit_type": "five_hour", "reset_at_epoch_ms": reset,
                   "extra_usage": {"overageStatus": "rejected", "isUsingOverage": False}, **changes}
        self.evidence[run] = failure
        self.store.write(action, binding, {"status": "ERROR", "failure": failure, "reason": "quota"})
        return self.store.key(action, binding)

    def invoke(self):
        self.invocations += 1
        return {"status": "POSTED", "result": "PASS", "scope_result": "WITHIN_APPROVED_PLAN",
                "program_binding": self.binding, "run": "retry-run", "comment_url": "https://github.com/result"}

    def test_one_retry_preserves_original_error_and_exact_program_binding(self):
        key = self.quota_failure()
        original = self.store._path(key, "outcome").read_bytes()
        first = self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(first["status"], "POSTED")
        self.assertEqual(first["program_binding"], self.binding)
        for _ in range(3):
            self.assertEqual(self.q.resume("audit", self.binding, self.invoke), first)
        self.assertEqual((self.preflights, self.invocations), (1, 1))
        self.assertEqual(self.store._path(key, "outcome").read_bytes(), original)
        self.assertEqual(self.store.read("audit", self.binding)["status"], "ERROR")
        self.assertEqual(self.store.read("audit", self.binding)["settlement"], "TERMINAL_FAILED")

    def test_reset_before_due_and_late_heartbeat(self):
        self.quota_failure(reset=3000000)
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "WAITING_QUOTA")
        self.assertEqual((self.preflights, self.invocations), (0, 0))
        self.now = 5000000
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "POSTED")

    def test_reboot_and_duplicate_events_use_one_durable_attempt(self):
        self.quota_failure(); self.q.resume("audit", self.binding, self.invoke)
        newer = Quota(self.store, lambda run: self.evidence[run], self.pf, self.current, clock_ms=lambda: self.now)
        self.assertEqual(newer.resume("audit", self.binding, self.invoke)["status"], "POSTED")
        self.assertEqual((self.preflights, self.invocations), (1, 1))

    def test_global_lock_busy_consumes_no_nonce(self):
        self.quota_failure()
        with self.store.locked("model.lock"):
            self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "BUSY")
        self.assertEqual(list(self.q.root.iterdir()), [])
        self.assertEqual((self.preflights, self.invocations), (0, 0))

    def test_model_lock_is_held_across_preflight_and_retry(self):
        self.quota_failure()
        def check():
            with self.store.locked("model.lock") as acquired:
                self.assertFalse(acquired)
            return {"status": "PASS", "extra_usage": {"overageStatus": "rejected"}}
        self.preflight = check
        def invoke():
            with self.store.locked("model.lock") as acquired:
                self.assertFalse(acquired)
            return self.invoke()
        self.assertEqual(self.q.resume("audit", self.binding, invoke)["status"], "POSTED")

    def test_invalid_and_generic_failures_never_retry(self):
        for i, changes in enumerate(({"error_code": "UNKNOWN"}, {"error_code": "MODEL_EXECUTION_FAILED"},
                                     {"api_error_status": True}, {"process_terminated": "true"},
                                     {"publication_state": "UNKNOWN"}, {"reset_at_epoch_ms": "1000000"},
                                     {"reset_at_epoch_ms": True}, {"adapter_profile": "unknown"},
                                     {"extra_usage": {"overageStatus": "rejected", "isUsingOverage": "false"}})):
            binding = {**self.binding, "node": f"invalid{i}"}
            self.quota_failure(binding, **changes)
            self.assertEqual(self.q.resume("audit", binding, self.invoke)["status"], "BLOCKED_ERROR")
        self.assertEqual((self.preflights, self.invocations), (0, 0))

    def test_verified_archive_must_exactly_match_original_envelope(self):
        self.quota_failure()
        self.q.verify = lambda run: {**self.evidence[run], "reset_at_epoch_ms": 9000000}
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "BLOCKED_ERROR")

    def test_changed_context_before_or_during_preflight_never_admits(self):
        self.quota_failure()
        self.current = lambda action, binding: {"binding": {**binding, "head": "d" * 40}, "tool_sha256": self.store.tool_sha}
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "STALE_CONTEXT")
        self.assertEqual(self.preflights, 0)
        self.current = lambda action, binding: {"binding": binding, "tool_sha256": self.store.tool_sha}
        def move():
            self.current = lambda action, binding: {"binding": binding, "tool_sha256": "e" * 64}
            return {"status": "PASS", "extra_usage": {"overageStatus": "rejected"}}
        self.preflight = move
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "STALE_CONTEXT")
        self.assertEqual(self.invocations, 0)
        self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(self.preflights, 1)

    def test_preflight_policy_failure_consumes_one_wake(self):
        self.quota_failure()
        self.preflight = lambda: {"status": "PASS", "extra_usage": {"overageStatus": "allowed"}}
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "BLOCKED_POLICY")
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "BLOCKED_POLICY")
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_completed_childless_policy_terminal_releases_scope_but_consumes_retry(self):
        self.quota_failure()
        self.preflight = lambda: {"status": "PASS", "extra_usage": {
            "status": "allowed_warning", "isUsingOverage": False}}
        first = self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(first["status"], "BLOCKED_POLICY")
        self.assertIsNone(self.q.unresolved("audit", self.binding))
        self.assertEqual(self.q.decision_status(self.binding)["status"], "CLEAR")
        fresh = {**self.binding, "head": "d" * 40, "writer_launch": "e" * 24}
        self.assertEqual(self.q.effective("audit", fresh)["status"], "MISSING")
        self.assertIsNone(self.q.fair_blocked("audit", fresh))
        completed = self.store.execute("audit", fresh, lambda: {
            "status": "POSTED", "program_binding": fresh, "result": "PASS"})
        self.assertEqual(completed["status"], "POSTED")
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke), first)
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_verified_childless_preflight_failure_releases_scope(self):
        self.quota_failure()
        evidence = {"schema": "FABLE_FAILURE_V1", "schema_version": 1,
                    "run": "preflight-run", "kind": "preflight",
                    "terminal_evidence": "VERIFIED", "process_terminated": True,
                    "archive_state": "SEALED", "publication_state": "NOT_STARTED",
                    "error_code": "OVERAGE_UNVERIFIED"}
        self.evidence[evidence["run"]] = evidence
        def failure():
            raise Failure(evidence)
        self.preflight = failure
        first = self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(first["status"], "BLOCKED_ERROR")
        self.assertIsNone(self.q.unresolved("audit", self.binding))
        self.assertEqual(self.q.decision_status(self.binding)["status"], "CLEAR")
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke), first)
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_completed_preflight_stale_context_releases_only_new_binding(self):
        self.quota_failure()
        def changed():
            self.current = lambda action, binding: {"binding": {}, "tool_sha256": self.store.tool_sha}
            return {"status": "PASS", "extra_usage": {"overageStatus": "rejected"}}
        self.preflight = changed
        first = self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(first["status"], "STALE_CONTEXT")
        self.assertEqual(first["resolution"], "CONSUMED_NO_CHILD")
        self.assertIsNone(self.q.unresolved("audit", self.binding))
        self.assertEqual(self.q.decision_status(self.binding)["status"], "CLEAR")
        self.current = lambda action, binding: {"binding": binding, "tool_sha256": self.store.tool_sha}
        fresh = {**self.binding, "head": "d" * 40}
        self.assertEqual(self.q.effective("audit", fresh)["status"], "MISSING")
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke), first)
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_verified_childless_policy_exception_releases_new_scope(self):
        self.quota_failure()
        evidence = {"schema": "FABLE_FAILURE_V1", "schema_version": 1,
                    "run": "preflight-policy", "kind": "preflight",
                    "terminal_evidence": "VERIFIED", "process_terminated": True,
                    "archive_state": "SEALED", "publication_state": "NOT_STARTED",
                    "error_code": "OVERAGE_NOT_BLOCKED"}
        self.evidence[evidence["run"]] = evidence
        def failure():
            raise Failure(evidence)
        self.preflight = failure
        result = self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(result["status"], "BLOCKED_POLICY")
        self.assertEqual(result["resolution"], "CONSUMED_NO_CHILD")
        fresh = {**self.binding, "head": "d" * 40}
        self.assertEqual(self.q.effective("audit", fresh)["status"], "MISSING")
        self.assertEqual(self.q.decision_status(fresh)["status"], "CLEAR")

    def test_unproved_preflight_exception_never_creates_completion_release(self):
        cases = ({"archive_state": "UNKNOWN"}, {"schema_version": True},
                 {"process_terminated": "true"}, {"publication_state": "UNKNOWN"},
                 {"kind": "audit"}, {"terminal_evidence": "UNKNOWN"})
        for index, change in enumerate(cases):
            with self.subTest(change=change):
                binding = {**self.binding, "node": f"preflight-unknown-{index}"}
                self.quota_failure(binding)
                evidence = {"schema": "FABLE_FAILURE_V1", "schema_version": 1,
                            "run": f"unproved-{index}", "kind": "preflight",
                            "terminal_evidence": "VERIFIED", "process_terminated": True,
                            "archive_state": "SEALED", "publication_state": "NOT_STARTED",
                            "error_code": "OVERAGE_UNVERIFIED", **change}
                self.evidence[evidence["run"]] = evidence
                def failure():
                    raise Failure(evidence)
                self.preflight = failure
                result = self.q.resume("audit", binding, self.invoke)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertNotIn("resolution", result)
                self.assertIsNotNone(self.q.unresolved("audit", binding))

    def test_malformed_preflight_return_is_unknown_not_childless_completion(self):
        self.quota_failure()
        self.preflight = lambda: {"status": "RUNNING"}
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "UNKNOWN")
        self.assertIsNotNone(self.q.unresolved("audit", self.binding))
        self.assertEqual(self.q.decision_status(self.binding)["status"], "BLOCKED")
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_legacy_childless_terminal_without_proof_remains_fenced(self):
        self.quota_failure()
        self.preflight = lambda: {"status": "PASS", "extra_usage": {"overageStatus": "allowed"}}
        result = self.q.resume("audit", self.binding, self.invoke)
        path = self.q._path(result["quota_attempt"]) / "terminal.json"
        terminal = self.store._read(path)
        del terminal["resolution"]
        del terminal["preflight_sha256"]
        path.write_text(journal.canonical(terminal))
        # Reboot/migration may read old records, but does not infer their missing
        # release proof from terminal status or an unrelated preflight digest.
        rebooted = Quota(self.store, self.q.verify, self.pf, self.current, clock_ms=lambda: self.now)
        self.assertEqual(rebooted.effective("audit", self.binding)["status"], "UNKNOWN")
        self.assertIsNotNone(rebooted.unresolved("audit", self.binding))
        self.assertEqual(rebooted.decision_status(self.binding)["status"], "BLOCKED")
        self.assertEqual(rebooted.resume("audit", self.binding, self.invoke)["status"], "UNKNOWN")
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_changed_completion_digest_cannot_release_scope(self):
        self.quota_failure()
        self.preflight = lambda: {"status": "PASS", "extra_usage": {"overageStatus": "allowed"}}
        result = self.q.resume("audit", self.binding, self.invoke)
        path = self.q._path(result["quota_attempt"]) / "terminal.json"
        terminal = self.store._read(path)
        terminal["preflight_sha256"] = "e" * 64
        path.write_text(journal.canonical(terminal))
        with self.assertRaises(journal.ReceiptError):
            self.q.decision_status({**self.binding, "head": "d" * 40})

    def test_no_child_proof_contradicted_by_posted_child_never_clears(self):
        self.quota_failure()
        self.preflight = lambda: {"status": "PASS", "extra_usage": {"overageStatus": "allowed"}}
        result = self.q.resume("audit", self.binding, self.invoke)
        ticket = next(self.q._tickets())
        child = self.q._child(ticket, create=True)
        child.write("audit", self.binding, {"status": "POSTED", "program_binding": self.binding, "result": "PASS"})
        with self.assertRaises(journal.ReceiptError):
            self.q.effective("audit", self.binding)
        with self.assertRaises(journal.ReceiptError):
            self.q.decision_status({**self.binding, "head": "d" * 40})
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_preflight_explicit_overage_use_is_rejected_even_with_rejected_status(self):
        self.quota_failure()
        self.preflight = lambda: {"status": "PASS", "extra_usage": {"overageStatus": "rejected", "isUsingOverage": True}}
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "BLOCKED_POLICY")
        self.assertEqual(self.invocations, 0)

    def test_ambiguous_preflight_error_remains_unknown_and_fenced(self):
        self.quota_failure()
        def error():
            raise RuntimeError("transport ambiguity")
        self.preflight = error
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "UNKNOWN")
        self.assertIsNotNone(self.q.unresolved("audit", {**self.binding, "head": "d" * 40}))
        self.assertEqual(self.q.decision_status(self.binding)["status"], "BLOCKED")
        self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(self.preflights, 1)

    def test_claim_crash_before_preflight_is_not_replayed(self):
        self.quota_failure()
        def crash():
            raise KeyboardInterrupt()
        self.preflight = crash
        with self.assertRaises(KeyboardInterrupt):
            self.q.resume("audit", self.binding, self.invoke)
        self.preflight = lambda: {"status": "PASS", "extra_usage": {"overageStatus": "rejected"}}
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "UNKNOWN")
        self.assertEqual((self.preflights, self.invocations), (1, 0))

    def test_second_quota_failure_cannot_generate_another_retry(self):
        self.quota_failure()
        def fail_again():
            self.invocations += 1
            raise Failure(next(iter(self.evidence.values())))
        self.assertEqual(self.q.resume("audit", self.binding, fail_again)["status"], "ERROR")
        self.assertEqual(self.q.resume("audit", self.binding, fail_again)["status"], "ERROR")
        self.assertEqual((self.preflights, self.invocations), (1, 1))
        self.assertIsNotNone(self.q.unresolved("audit", {**self.binding, "head": "d" * 40}))

    def test_earlier_due_ticket_has_priority_over_later_and_fresh_work(self):
        self.quota_failure()
        later = {**self.binding, "node": "later", "issue": 2}
        self.quota_failure(later, reset=1500000)
        self.assertEqual(self.q.resume("audit", later, self.invoke)["status"], "QUEUED")
        self.assertIsNotNone(self.q.fair_blocked("audit", {**self.binding, "node": "fresh"}))
        self.q.resume("audit", self.binding, self.invoke)
        self.assertEqual(self.q.readiness("audit", later)["status"], "DUE")

    def test_stale_earlier_ticket_does_not_starve_current_later_ticket(self):
        self.quota_failure()
        later = {**self.binding, "node": "later", "issue": 2}
        self.quota_failure(later, reset=1500000)
        self.current = lambda action, binding: {"binding": binding if binding["node"] == "later" else {},
                                                 "tool_sha256": self.store.tool_sha}
        self.assertEqual(self.q.readiness("audit", later)["status"], "DUE")

    def test_unresolved_ancestor_is_not_bypassed_by_quota_settlement(self):
        older = {**self.binding, "head": "e" * 40}
        self.store.write("audit", older, {"status": "RUNNING"})
        self.quota_failure()
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "UNKNOWN")
        self.assertEqual((self.preflights, self.invocations), (0, 0))

    def test_other_action_ambiguity_blocks_quota_and_does_not_starve_unrelated_task(self):
        self.quota_failure()
        self.store.write("consult", {**self.binding, "question": 5}, {"status": "RUNNING"})
        later = {**self.binding, "node": "later"}
        self.quota_failure(later, reset=1500000)
        self.assertEqual(self.q.readiness("audit", self.binding)["status"], "UNKNOWN")
        self.assertEqual(self.q.readiness("audit", later)["status"], "DUE")

    def test_plain_posted_and_running_receipts_keep_original_state(self):
        self.store.write("audit", self.binding, {"status": "RUNNING"})
        self.assertEqual(self.q.readiness("audit", self.binding)["status"], "RUNNING")
        other = {**self.binding, "node": "posted"}
        self.store.write("audit", other, {"status": "POSTED", "program_binding": other, "result": "PASS"})
        self.assertEqual(self.q.readiness("audit", other)["status"], "POSTED")

    def test_operator_reconciled_child_failure_does_not_make_second_automatic_attempt(self):
        self.quota_failure()
        def failure():
            self.invocations += 1
            raise Failure(next(iter(self.evidence.values())))
        first = self.q.resume("audit", self.binding, failure)
        ticket = next(self.q._tickets())
        child = self.q.operator_store(ticket["incident"], first["admission"])
        child.reconcile(first["admission"], first["state_version"], self.q.verify)
        self.assertIsNone(self.q.unresolved("audit", self.binding))
        self.assertEqual(self.q.resume("audit", self.binding, failure)["status"], "ERROR")
        self.assertEqual((self.preflights, self.invocations), (1, 1))

    def test_operator_selectors_cannot_supply_paths_or_unrelated_child(self):
        self.quota_failure(); result = self.q.resume("audit", self.binding, self.invoke)
        for incident, admission in (("../../tmp", result["admission"]),
                                    (result["quota_attempt"], "e" * 64), ("f" * 64, result["admission"])):
            with self.assertRaises(journal.ReceiptError):
                self.q.operator_store(incident, admission)

    def test_generic_operator_settlement_does_not_enable_quota(self):
        key = self.quota_failure(error_code="MODEL_EXECUTION_FAILED")
        version = self.store.read("audit", self.binding)["state_version"]
        self.store.reconcile(key, version, lambda run: self.evidence[run])
        self.assertEqual(self.q.resume("audit", self.binding, self.invoke)["status"], "BLOCKED_ERROR")

    def test_user_decision_from_retry_is_still_protected(self):
        self.quota_failure()
        def invoke():
            return {**self.invoke(), "result": "DECISION_REQUIRED", "scope_result": "USER_REQUIRED"}
        self.q.resume("audit", self.binding, invoke)
        self.assertEqual(self.q.decision_status(self.binding)["status"], "BLOCKED")

    def test_wrong_parent_binding_and_kind_are_rejected(self):
        for node, changes in (("wrongkind", {"kind": "consult"}),
                              ("wrongbinding", {"program_binding": {"issue": 999}})):
            binding = {**self.binding, "node": node}
            self.quota_failure(binding, **changes)
            self.assertEqual(self.q.resume("audit", binding, self.invoke)["status"], "BLOCKED_ERROR")

    def test_no_old_pass_transfer_after_fingerprint_change(self):
        self.quota_failure(); self.q.resume("audit", self.binding, self.invoke)
        upgraded = journal.Receipts(self.tmp.name, "e" * 64, trust=self.store.trust)
        q = Quota(upgraded, self.q.verify, self.pf, self.current, clock_ms=lambda: self.now)
        self.assertEqual(q.effective("audit", self.binding)["status"], "MISSING")

    def test_earlier_retry_pass_is_fenced_by_later_unknown_child(self):
        self.quota_failure(); self.q.resume("audit", self.binding, self.invoke)
        later = {**self.binding, "head": "d" * 40, "writer_launch": "e" * 24}
        self.quota_failure(later)
        def ambiguous():
            raise RuntimeError("transport ambiguity")
        self.q.resume("audit", later, ambiguous)
        self.assertEqual(self.q.effective("audit", self.binding)["status"], "UNKNOWN")
        self.assertEqual(self.q.readiness("audit", self.binding)["status"], "UNKNOWN")

    def test_unknown_consult_child_fences_fresh_audit_projection_but_not_unrelated_node(self):
        consult = {**self.binding, "question": 5}
        self.quota_failure(consult, action="consult")
        def ambiguous():
            raise RuntimeError("transport ambiguity")
        self.q.resume("consult", consult, ambiguous)
        fresh_audit = {**self.binding, "head": "d" * 40}
        self.assertEqual(self.q.effective("audit", fresh_audit)["status"], "UNKNOWN")
        self.assertEqual(self.q.readiness("audit", fresh_audit)["status"], "UNKNOWN")
        unrelated = {**fresh_audit, "node": "other"}
        self.assertEqual(self.q.effective("audit", unrelated)["status"], "MISSING")
        self.assertEqual(self.invocations, 0)


if __name__ == "__main__":
    unittest.main()
