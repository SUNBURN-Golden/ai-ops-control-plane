"""Adversarial tests of the protected program Astra bridge, without providers."""
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import control_plane_fable as fable
import control_plane_program as prog
import control_plane_program_astra as bridge
import test_control_plane_program as runtime
from test_control_plane_fable import verdict
import test_control_plane_fable as fable_tests


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        trust = patch.object(bridge, "protected"); trust.start(); self.addCleanup(trust.stop)
        self.store = bridge.Receipts(self.tmp.name, "f" * 64)
        self.binding = {"repository": runtime.REPO, "issue": 31, "head": runtime.HEAD,
                        "writer_launch": "1" * 24}

    def result(self, result="PASS", **extra):
        return {"status": "POSTED", "result": result, "program_binding": self.binding,
                "comment_url": "https://github.com/result", "run": "test-run", **extra}

    def test_exact_receipt_is_deduplicated_without_model_reexecution(self):
        calls = []
        def invoke():
            calls.append(1); return self.result()
        first = self.store.execute("audit", self.binding, invoke)
        self.assertEqual(self.store.execute("audit", self.binding, invoke), first)
        self.assertEqual(calls, [1])

    def test_crash_or_lost_result_never_resubmits(self):
        self.store.write("audit", self.binding, {"status": "RUNNING"})
        self.assertEqual(self.store.execute("audit", self.binding, lambda: self.fail())['status'], "UNKNOWN")

    def test_failed_invocation_is_fenced_and_never_retried(self):
        def failure():
            raise fable.FableError("OVERAGE_NOT_BLOCKED")
        with self.assertRaises(fable.FableError):
            self.store.execute("audit", self.binding, failure)
        self.assertEqual(self.store.execute("audit", self.binding, lambda: self.fail())["status"], "ERROR")

    def test_stale_head_attempt_and_tool_do_not_transfer_receipts(self):
        self.store.execute("audit", self.binding, self.result)
        for changes in ({"head": "b" * 40}, {"writer_launch": "2" * 24}, {"issue": 32}):
            self.assertEqual(self.store.read("audit", {**self.binding, **changes})["status"], "MISSING")
        newer = bridge.Receipts(self.tmp.name, "e" * 64)
        self.assertEqual(newer.read("audit", self.binding)["status"], "MISSING")

    def test_one_global_model_session_and_busy_is_not_an_admitted_retry(self):
        lock = os.open(Path(self.tmp.name) / "model.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.store.execute("audit", self.binding, lambda: self.fail())["status"], "BUSY")
            self.assertEqual(self.store.read("audit", self.binding)["status"], "MISSING")
        finally:
            os.close(lock)

    def test_user_required_cannot_be_removed_by_editing_github_labels(self):
        self.store.execute("consult", self.binding, lambda: self.result("USER_REQUIRED"))
        self.assertEqual(self.store.decision_status(self.binding)["status"], "BLOCKED")
        self.assertEqual(self.store.decision_status({**self.binding, "writer_launch": "2" * 24})["status"], "CLEAR")

    def test_unresolved_consultation_blocks_resume(self):
        self.store.write("consult", self.binding, {"status": "RUNNING"})
        self.assertEqual(self.store.decision_status(self.binding)["status"], "BLOCKED")

    def test_audit_user_required_remains_a_resume_blocker_across_tool_upgrade(self):
        self.store.execute("audit", self.binding, lambda: self.result("DECISION_REQUIRED", scope_result="USER_REQUIRED"))
        upgraded = bridge.Receipts(self.tmp.name, "b" * 64)
        self.assertEqual(upgraded.decision_status(self.binding)["status"], "BLOCKED")

    def test_unbound_posted_result_is_not_accepted(self):
        with self.assertRaises(bridge.BridgeError):
            self.store.execute("audit", self.binding,
                               lambda: {"status": "POSTED", "program_binding": {"head": "bad"}})


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.r = runtime.ProgramModeTests(); self.r.setUp(); self.addCleanup(self.r.doCleanups)
        self.r.cfg["deployment_enabled"] = True
        self.r.gh.contents[runtime.PLAN1] = runtime.plan([runtime.node(floor="A3", astra_auto_merge=True)])

    def reviewed_delivery(self, change="NO"):
        issue, writer = self.r.released_writer()
        self.r.reviewed(issue, slot=1, depth="A2", change=change)
        self.r.reviewed(issue, slot=2, depth="A2", change=change)
        binding, *_ = bridge.request_context(self.r.gh, self.r.cfg, issue, 7, runtime.HEAD)
        return issue, writer, binding

    def receipt(self, base_binding, **extra):
        return {"status": "POSTED", "result": "PASS", "scope_result": "WITHIN_APPROVED_PLAN",
                "contract_change": "NO",
                "binding": base_binding, "program_binding": base_binding, "gate": "ARCHITECTURE",
                "verified_depth": "A3", "comment_url": "https://github.com/root-receipt", **extra}

    def test_ordinary_a3_requires_exact_protected_scope_receipt(self):
        issue, writer, binding = self.reviewed_delivery(change="NO")
        with patch.object(prog, "fable_program", return_value=self.receipt(binding)):
            self.assertTrue(prog.merge_check(issue, 7)["ready"])
        for changes in ({"scope_result": "USER_REQUIRED"}, {"result": "FAIL"}, {"status": "UNKNOWN"},
                        {"verified_depth": "A2"}, {"binding": {**binding, "plan_commit": runtime.PLAN2}},
                        {"program_binding": {**binding, "writer_launch": "3" * 24}}):
            with self.subTest(changes=changes), patch.object(prog, "fable_program", return_value=self.receipt(binding, **changes)):
                self.assertFalse(prog.merge_check(issue, 7)["ready"])

    def test_contract_change_never_uses_a_scope_receipt_as_user_decision(self):
        issue, _, binding = self.reviewed_delivery(change="YES")
        with patch.object(prog, "fable_program", return_value=self.receipt(binding)):
            answer = prog.merge_check(issue, 7)
        self.assertFalse(answer["ready"])
        self.assertIn("contract change", " ".join(answer["reasons"]))
        self.assertFalse(answer["astra_audit_allowed"])

    def test_user_reserved_node_never_uses_the_automatic_gate(self):
        self.r.gh.contents[runtime.PLAN1] = runtime.plan([runtime.node(floor="A3", user_merge=True)])
        issue, _ = self.r.released_writer()
        self.r.reviewed(issue, slot=1, depth="A2"); self.r.reviewed(issue, slot=2, depth="A2")
        with patch.object(prog, "fable_program", side_effect=AssertionError("must not call Fable")):
            self.assertFalse(prog.merge_check(issue, 7)["ready"])
            self.assertIn("reserves this node", self.r.reasons(issue))

    def test_pending_approval_never_materializes_or_dispatches(self):
        self.r.gh.contents[runtime.PLAN1]["approval_pointer"] = "PENDING_APPROVAL_DO_NOT_DISPATCH"
        with self.assertRaisesRegex(prog.ProgramError, "pending"):
            self.r.materialized()

    def test_missing_review_or_moved_head_never_gets_an_audit_context(self):
        issue, _ = self.r.released_writer()
        with self.assertRaisesRegex(bridge.BridgeError, "reviews"):
            bridge.request_context(self.r.gh, self.r.cfg, issue, 7, runtime.HEAD)
        self.r.reviewed(issue, slot=1, depth="A2"); self.r.reviewed(issue, slot=2, depth="A2")
        self.r.gh.pulls[7]["head"]["sha"] = "b" * 40
        with self.assertRaises(prog.ProgramError):
            bridge.request_context(self.r.gh, self.r.cfg, issue, 7, runtime.HEAD)

    def test_plan_flag_cannot_reserve_and_delegate_the_same_merge(self):
        for fields in ({"user_merge": True, "astra_auto_merge": True}, {"astra_auto_merge": "true"}):
            with self.assertRaises(prog.ProgramError):
                prog.validate_plan(runtime.plan([runtime.node(**fields)]), self.r.cfg)

    def test_current_failed_ci_still_blocks_a_passing_astra_receipt(self):
        issue, _, binding = self.reviewed_delivery()
        self.r.gh.checks[runtime.HEAD][0]["conclusion"] = "failure"
        with patch.object(prog, "fable_program", return_value=self.receipt(binding)):
            self.assertFalse(prog.merge_check(issue, 7)["ready"])

    def test_quota_readmission_reuses_actual_current_ci_predicate(self):
        issue, _, _ = self.reviewed_delivery()
        context = bridge.request_context(self.r.gh, self.r.cfg, issue, 7, runtime.HEAD)
        bridge.require_quota_context("audit", self.r.gh, self.r.cfg, context)
        self.r.gh.checks[runtime.HEAD][0]["conclusion"] = "failure"
        with self.assertRaisesRegex(bridge.BridgeError, "verification"):
            bridge.require_quota_context("audit", self.r.gh, self.r.cfg, context)

    def test_quota_retry_never_settles_contract_change_or_user_release(self):
        issue, _, _ = self.reviewed_delivery(change="YES")
        context = bridge.request_context(self.r.gh, self.r.cfg, issue, 7, runtime.HEAD)
        with self.assertRaisesRegex(bridge.BridgeError, "contract-change"):
            bridge.require_quota_context("audit", self.r.gh, self.r.cfg, context)
        for change in ({"user_merge": True}, {"astra_gate": "RELEASE"}):
            updated = list(context); updated[3] = {**context[3], **change}
            with self.assertRaisesRegex(bridge.BridgeError, "User-only or RELEASE"):
                bridge.require_quota_context("audit", self.r.gh, self.r.cfg, tuple(updated))

    def test_stored_user_required_blocks_builder_resume_even_without_labels(self):
        issue, _, _ = self.reviewed_delivery()
        self.r.gh.issues[issue]["labels"] = []
        with patch.object(prog, "fable_program", return_value={"status": "BLOCKED"}):
            with self.assertRaisesRegex(prog.ProgramError, "USER_REQUIRED"):
                self.r.launch_writer(issue)

    def protected_status_store(self):
        root = Path(self.r.temp.name) / "program"
        root.mkdir(mode=0o700, exist_ok=True)
        ctx = SimpleNamespace(runs_dir=Path(self.r.temp.name), tool_sha256="a" * 64)
        return ctx, bridge.Receipts(root, "a" * 64)

    def status_via_bridge(self, ctx, issue, **fields):
        return bridge.run(ctx, fable, {"repository": runtime.REPO, "issue": issue,
                          "operation": "decision-status", "github_token": "token", **fields})

    def bind_user_plan_revision(self, issue, binding):
        proposed = self.r.gh.contents[runtime.PLAN2]
        proposed["approval_pointer"] = f"https://github.com/{runtime.REPO}/pull/48"
        old = prog.load_plan(self.r.gh, self.r.cfg, runtime.PLAN1)
        new = prog.load_plan(self.r.gh, self.r.cfg, runtime.PLAN2)
        proposed["superseding_decisions"] = [{"schema_version": 1, "repository": runtime.REPO,
            "program": "zari", "node": "n1", "issue": issue, "writer_launch": binding["writer_launch"],
            "previous_plan_commit": runtime.PLAN1,
            "previous_definition_sha256": prog.node_definition_sha256(old["nodes"][0]),
            "definition_sha256": prog.node_definition_sha256(new["nodes"][0]),
            "decision_pointer": proposed["approval_pointer"]}]
        self.r.gh.pulls[48] = {"state": "closed", "merged": True, "changed_files": 1,
             "merge_commit_sha": runtime.PLAN2, "head": {"sha": runtime.PLAN2,
             "ref": "user-plan-revision", "repo": {"full_name": runtime.REPO}}, "base": {"ref": "main"}}
        self.r.gh.changed_files[48] = [{"filename": prog.PLAN_PATH}]

    def test_own_issue_pointer_and_whitespace_cannot_clear_semantic_decision(self):
        issue, _, binding = self.reviewed_delivery()
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        with patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            ctx, store = self.protected_status_store()
            store.write("audit", binding, {"status": "POSTED", "result": "DECISION_REQUIRED",
                        "scope_result": "USER_REQUIRED", "program_binding": binding})
            for mode in ("own-issue", "whitespace", "wrong-digest", "unmerged", "delivery-branch", "not-plan", "merge-resolution"):
                self.r.gh.contents[runtime.PLAN2] = json.loads(json.dumps(self.r.gh.contents[runtime.PLAN1]))
                self.r.gh.contents[runtime.PLAN2]["nodes"][0]["spec"] = "Actually changed definition."
                self.bind_user_plan_revision(issue, binding)
                if mode == "own-issue":
                    self.r.gh.contents[runtime.PLAN2]["approval_pointer"] = self.r.gh.issues[issue]["html_url"]
                elif mode == "whitespace":
                    self.r.gh.contents[runtime.PLAN2]["nodes"][0] = {**self.r.gh.contents[runtime.PLAN1]["nodes"][0],
                            "title": self.r.gh.contents[runtime.PLAN1]["nodes"][0]["title"] + " "}
                elif mode == "wrong-digest":
                    self.r.gh.contents[runtime.PLAN2]["superseding_decisions"][0]["definition_sha256"] = "f" * 64
                elif mode == "unmerged": self.r.gh.pulls[48]["merged"] = False
                elif mode == "delivery-branch": self.r.gh.pulls[48]["head"]["ref"] = "astra/zari-n1"
                elif mode == "not-plan": self.r.gh.changed_files[48] = [{"filename": "src/code.py"}]
                else:
                    merge = "3" * 40
                    self.r.gh.latest_plan_commit = runtime.PLAN2
                    self.r.gh.pulls[48]["merge_commit_sha"] = merge
                    self.r.gh.contents[merge] = runtime.plan([runtime.node(floor="A3", spec="Unadopted conflict resolution.")])
                    self.r.gh.compare[(runtime.PLAN2, merge)] = "ahead"
                with self.subTest(mode=mode):
                    self.assertEqual(self.status_via_bridge(ctx, issue, plan_commit=runtime.PLAN2)["status"], "BLOCKED")

    def test_decision_binds_actual_normal_merge_even_when_latest_path_commit_is_pr_head(self):
        issue, _, binding = self.reviewed_delivery()
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node(floor="A3", spec="Approved new scope.")])
        self.bind_user_plan_revision(issue, binding)
        merge = "3" * 40
        self.r.gh.contents[merge] = json.loads(json.dumps(self.r.gh.contents[runtime.PLAN2]))
        self.r.gh.latest_plan_commit = runtime.PLAN2
        self.r.gh.pulls[48]["merge_commit_sha"] = merge
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        self.r.gh.compare[(runtime.PLAN2, merge)] = "ahead"
        self.r.gh.compare[(merge, runtime.PLAN2)] = "behind"
        revised, *_ = bridge.resolve_request(self.r.gh, self.r.cfg, issue, "decision-status",
                                             {"plan_commit": runtime.PLAN2})
        self.assertEqual(revised["plan_commit"], runtime.PLAN2)

    def test_renamed_or_removed_blocked_node_retains_host_identity(self):
        issue, _, binding = self.reviewed_delivery()
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node("renamed", floor="A3")])
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        # Erasing an editable projection cannot erase the protected canonical node.
        del self.r.gh.issues[issue]
        with self.assertRaisesRegex(prog.ProgramError, "removes or renames"):
            prog.materialize("zari", "renamed", runtime.PLAN2)
        self.assertEqual(len(self.r.host.ledger.materialize_list("zari", runtime.REPO, self.r.host.policy)["rows"]), 1)

    def test_program_key_rename_cannot_hide_existing_canonical_fence(self):
        issue, _, _ = self.reviewed_delivery()
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node("renamed", floor="A3")])
        self.r.gh.contents[runtime.PLAN2]["program"] = "renamed-program"
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        with self.assertRaisesRegex(runtime.cp.ControlPlaneError, "existing canonical program"):
            prog.materialize("renamed-program", "renamed", runtime.PLAN2)

    def test_unresolved_execution_blocks_revised_plan_even_if_delegation_removed(self):
        issue, _, binding = self.reviewed_delivery()
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node(floor="A3", spec="Approved revised scope.")])
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        old_body = self.r.gh.issues[issue]["body"]
        with patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            ctx, store = self.protected_status_store()
            store.write("audit", binding, {"status": "UNKNOWN"})
            def status(operation, actual_issue, **fields):
                self.assertEqual((operation, actual_issue), ("decision-status", issue))
                return self.status_via_bridge(ctx, actual_issue, **fields)
            with patch.object(prog, "fable_program", side_effect=status) as query:
                with self.assertRaisesRegex(prog.ProgramError, "unresolved"):
                    self.r.launch_writer(issue, commit=runtime.PLAN2)
                query.assert_called_once_with("decision-status", issue, plan_commit=runtime.PLAN2)
        self.assertEqual(self.r.host.ledger.materialize_status("zari", "n1")["plan_commit"], runtime.PLAN1)
        self.assertEqual(self.r.gh.issues[issue]["body"], old_body)
        self.assertEqual(len(prog.writer_rows(prog.task_rows(self.r.cfg, "ZARI-N1"))), 1)

    def test_approved_revision_clears_exact_semantic_decision_without_execution_bypass(self):
        issue, _, binding = self.reviewed_delivery()
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node(floor="A3", astra_auto_merge=True,
                                                                  spec="User-approved revised scope.")])
        self.bind_user_plan_revision(issue, binding)
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        with patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            ctx, store = self.protected_status_store()
            store.write("audit", binding, {"status": "POSTED", "result": "DECISION_REQUIRED",
                        "scope_result": "USER_REQUIRED", "program_binding": binding})
            self.assertEqual(self.status_via_bridge(ctx, issue)["status"], "BLOCKED")
            self.assertEqual(self.status_via_bridge(ctx, issue, plan_commit=runtime.PLAN2)["status"], "CLEAR")
            with patch.object(prog, "fable_program", side_effect=lambda _op, n, **fields:
                              self.status_via_bridge(ctx, n, **fields)):
                answer = prog.start(issue, "zari", "n1", runtime.PLAN2,
                                    self.r.file("p2.json"), preflight=lambda _: True)
        self.assertEqual(answer["status"], "PREPARED")
        self.assertEqual(self.r.host.ledger.materialize_status("zari", "n1")["plan_commit"], runtime.PLAN2)

    def test_unrelated_commit_or_spec_edit_without_new_decision_cannot_clear_user_required(self):
        issue, _, binding = self.reviewed_delivery()
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        with patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            ctx, store = self.protected_status_store()
            store.write("audit", binding, {"status": "POSTED", "result": "DECISION_REQUIRED",
                        "scope_result": "USER_REQUIRED", "program_binding": binding})
            for changed_spec in (False, True):
                self.r.gh.contents[runtime.PLAN2] = json.loads(json.dumps(self.r.gh.contents[runtime.PLAN1]))
                if changed_spec: self.r.gh.contents[runtime.PLAN2]["nodes"][0]["spec"] = "Unapproved changed scope."
                with self.subTest(changed_spec=changed_spec):
                    self.assertEqual(self.status_via_bridge(ctx, issue, plan_commit=runtime.PLAN2)["status"], "BLOCKED")
                    with patch.object(prog, "fable_program", side_effect=lambda _op, n, **fields:
                                      self.status_via_bridge(ctx, n, **fields)):
                        with self.assertRaisesRegex(prog.ProgramError, "USER_REQUIRED"):
                            self.r.launch_writer(issue, commit=runtime.PLAN2)
            self.r.gh.contents[runtime.PLAN2]["approval_pointer"] = "different text is not a User decision"
            self.assertEqual(self.status_via_bridge(ctx, issue, plan_commit=runtime.PLAN2)["status"], "BLOCKED")
            # Same-scope settlement is not manufactured by a pointer change;
            # it needs an independently adopted decision reconciler.
            self.r.gh.contents[runtime.PLAN2] = json.loads(json.dumps(self.r.gh.contents[runtime.PLAN1]))
            self.r.gh.contents[runtime.PLAN2]["approval_pointer"] = \
                "https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/48#issuecomment-124"
            self.assertEqual(self.status_via_bridge(ctx, issue, plan_commit=runtime.PLAN2)["status"], "BLOCKED")
        self.assertEqual(self.r.host.ledger.materialize_status("zari", "n1")["plan_commit"], runtime.PLAN1)

    def test_nondelegated_user_only_consultation_still_fences_cross_plan_resume(self):
        self.r.gh.contents[runtime.PLAN1] = runtime.plan([runtime.node(floor="A3", user_merge=True)])
        issue, _ = self.r.released_writer()
        binding, *_ = bridge.request_context(self.r.gh, self.r.cfg, issue, decision_only=True)
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node(floor="A3", user_merge=True,
                                                                  spec="Revised User-only scope.")])
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        with patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            ctx, store = self.protected_status_store()
            store.write("consult", binding, {"status": "UNKNOWN"})
            with patch.object(prog, "fable_program", side_effect=lambda _op, n, **fields:
                              self.status_via_bridge(ctx, n, **fields)):
                with self.assertRaisesRegex(prog.ProgramError, "unresolved"):
                    self.r.launch_writer(issue, commit=runtime.PLAN2)
        self.assertEqual(self.r.host.ledger.materialize_status("zari", "n1")["plan_commit"], runtime.PLAN1)

    def test_advanced_host_plan_after_prepare_exception_can_retry_terminal_writer(self):
        issue, _ = self.r.released_writer()
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node(floor="A3", spec="Revised scope.")])
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        with patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            ctx, _ = self.protected_status_store()
            with patch.object(prog, "fable_program", side_effect=lambda _op, n, **fields:
                              self.status_via_bridge(ctx, n, **fields)):
                with patch.object(runtime.cp, "prepare_dispatch", side_effect=runtime.cp.ControlPlaneError("prepare failed")):
                    with self.assertRaisesRegex(runtime.cp.ControlPlaneError, "prepare failed"):
                        prog.start(issue, "zari", "n1", runtime.PLAN2, self.r.file("p2.json"), preflight=lambda _: True)
                self.assertEqual(self.r.host.ledger.materialize_status("zari", "n1")["plan_commit"], runtime.PLAN2)
                binding, *_ = bridge.request_context(self.r.gh, self.r.cfg, issue, decision_only=True)
                self.assertEqual(binding["plan_commit"], runtime.PLAN1)
                self.assertEqual(prog.start(issue, "zari", "n1", runtime.PLAN2,
                                           self.r.file("retry.json"), preflight=lambda _: True)["status"], "PREPARED")

    def test_advanced_host_plan_after_failed_prestart_can_retry_terminal_writer(self):
        issue, _ = self.r.released_writer()
        self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node(floor="A3", spec="Revised scope.")])
        self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
        with patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            ctx, _ = self.protected_status_store()
            with patch.object(prog, "fable_program", side_effect=lambda _op, n, **fields:
                              self.status_via_bridge(ctx, n, **fields)):
                prog.start(issue, "zari", "n1", runtime.PLAN2, self.r.file("p2.json"), preflight=lambda _: True)
                packet = json.loads(self.r.file("p2.json").read_text())
                admitted, _ = self.r.host.ledger.reserve(packet, self.r.host.policy)
                self.assertTrue(admitted)
                self.r.host.ledger.finalize(packet, {"outcome": "FAILED_PRESTART", "reason": "fixture provider did not start"})
                result = runtime.host.result_for(packet, "FAILED_PRESTART", "fixture provider did not start")
                self.r.file("failed.json").write_text(json.dumps(result))
                runtime.cp.finalize_dispatch(issue, self.r.file("failed.json"), packet["launch_request_id"])
                binding, *_ = bridge.request_context(self.r.gh, self.r.cfg, issue, decision_only=True)
                self.assertEqual(binding["plan_commit"], runtime.PLAN1)
                self.assertEqual(prog.start(issue, "zari", "n1", runtime.PLAN2,
                                           self.r.file("retry.json"), preflight=lambda _: True)["status"], "PREPARED")

    def test_proposed_plan_status_rejects_unapproved_unmerged_stale_or_different_scope(self):
        issue, _, _ = self.reviewed_delivery()
        for mode in ("pending", "unmerged", "stale", "different-program", "missing-node"):
            self.r.gh.contents[runtime.PLAN2] = runtime.plan([runtime.node(floor="A3", astra_auto_merge=True)])
            self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "ahead"
            self.r.gh.unmerged.clear()
            if mode == "pending": self.r.gh.contents[runtime.PLAN2]["approval_pointer"] = "PENDING"
            elif mode == "unmerged": self.r.gh.unmerged.add(runtime.PLAN2)
            elif mode == "stale": self.r.gh.compare[(runtime.PLAN1, runtime.PLAN2)] = "diverged"
            elif mode == "different-program": self.r.gh.contents[runtime.PLAN2]["program"] = "other"
            else: self.r.gh.contents[runtime.PLAN2]["nodes"][0]["id"] = "other"
            with self.subTest(mode=mode), self.assertRaises((prog.ProgramError, bridge.BridgeError)):
                bridge.resolve_request(self.r.gh, self.r.cfg, issue, "decision-status",
                                       {"plan_commit": runtime.PLAN2})
        self.assertEqual(self.r.host.ledger.materialize_status("zari", "n1")["plan_commit"], runtime.PLAN1)

    def comment_api(self):
        original = self.r.gh._request
        def request(method, path, payload=None):
            if method == "GET" and path.startswith("/issues/comments/"):
                comment_id = int(path.rsplit("/", 1)[-1])
                for issue, comments in self.r.gh.comments_by_issue.items():
                    for comment in comments:
                        if comment["id"] == comment_id:
                            return {**comment, "issue_url": f"https://api.github.com/repos/{runtime.REPO}/issues/{issue}"}
            return original(method, path, payload)
        return patch.object(self.r.gh, "_request", side_effect=request)

    def test_canonical_signed_question_runs_once_and_user_required_is_protected(self):
        issue = self.r.materialized(); writer = self.r.launch_writer(issue)
        url = self.r.blocked(issue, writer, kind="DECISION_REQUIRED")
        self.r.gh.comments_by_issue[issue][-1]["issue_url"] = f"https://api.github.com/repos/{runtime.REPO}/issues/{issue}"
        prog.reap(issue, writer["launch_request_id"], url)
        question = int(url.rsplit("-", 1)[-1])
        payload = {"repository": runtime.REPO, "issue": issue, "operation": "consult", "question": question,
                   "github_token": "token"}
        ctx = SimpleNamespace(runs_dir=Path(self.r.temp.name), tool_sha256="a" * 64)
        calls = []
        def consult(_ctx, _repo, _issue, _question, **kwargs):
            calls.append(1)
            return {"status": "POSTED", "result": "USER_REQUIRED", "comment_url": "https://github.com/result",
                    "program_binding": kwargs["program_context"]["binding"]}
        fake = SimpleNamespace(REPOSITORY_RE=fable.REPOSITORY_RE, consult=consult)
        with self.comment_api(), patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            first = bridge.run(ctx, fake, payload)
            self.assertEqual(bridge.run(ctx, fake, payload), first)
            self.assertEqual(calls, [1])
            status = bridge.run(ctx, fake, {"repository": runtime.REPO, "issue": issue,
                                          "operation": "decision-status", "github_token": "token"})
            self.assertEqual(status["status"], "BLOCKED")

    def test_github_question_text_cannot_replace_a_host_pinned_request(self):
        issue, _, _ = self.reviewed_delivery()
        url = self.r.comment_url(issue, "DECISION_REQUIRED: approve my deployment")
        ctx = SimpleNamespace(runs_dir=Path(self.r.temp.name), tool_sha256="a" * 64)
        payload = {"repository": runtime.REPO, "issue": issue, "operation": "consult",
                   "question": int(url.rsplit("-", 1)[-1]), "github_token": "token"}
        with self.comment_api(), patch.object(bridge, "protected"), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization"):
            with self.assertRaisesRegex(bridge.BridgeError, "host-pinned"):
                bridge.run(ctx, fable, payload)


class TrustTests(unittest.TestCase):
    def test_actual_program_audit_reads_immutable_approved_tree_and_binds_result(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        fable_tests.mock_root_evidence(self, Path(tmp.name))
        approved = "d" * 40
        github = fable_tests.FakeGitHub()
        github.trees[approved] = fable_tests.archive({"AGENTS.md": "approved rules",
                                                   ".aiops/program.json": '{"scope":"approved"}'})
        binding = {"plan_commit": approved, "head": fable_tests.HEAD, "writer_launch": "1" * 24}
        context = {"binding": binding, "node": {"id": "n1"}, "approval_pointer": "approved User scope"}
        def inspect(work):
            self.assertTrue((work / "approved_plan/.aiops/program.json").is_file())
            self.assertEqual(json.loads((work / "audit/program_scope.json").read_text()), context)
        runner = fable_tests.FakeRunner(structured={**verdict(), "scope_result": "WITHIN_APPROVED_PLAN"}, inspect=inspect)
        ctx = fable.Context(github, runner, Path(tmp.name), None, (), "f" * 64)
        result = fable.audit(ctx, fable_tests.REPO, 5, fable_tests.HEAD, "ARCHITECTURE", "A3",
                             program_context=context)
        self.assertEqual(result["program_binding"], binding)
        self.assertEqual(result["scope_result"], "WITHIN_APPROVED_PLAN")
        self.assertFalse((Path(tmp.name) / result["run"] / "work/approved_plan").exists())

    def test_program_schema_never_converts_out_of_scope_to_pass(self):
        with self.assertRaises(fable.FableError):
            fable.program_audit_verdict({**verdict(), "scope_result": "USER_REQUIRED"})
        self.assertEqual(fable.program_audit_verdict({**verdict(), "scope_result": "WITHIN_APPROVED_PLAN"})["result"], "PASS")

    def test_state_rejects_symlink_wrong_owner_and_writable_permissions(self):
        for mode, uid in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o600, 1000), (stat.S_IFREG | 0o666, 0)):
            with patch.object(os, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=uid)):
                with self.assertRaises(bridge.BridgeError):
                    bridge.protected(Path("/protected/receipt.json"))

    def test_github_token_is_on_stdin_only_and_never_echoed_on_error(self):
        token = "private-token-for-test"
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), patch.object(prog.cp, "load_config", return_value={"repository": runtime.REPO}):
            def invoked(argv, **kwargs):
                self.assertNotIn(token, " ".join(argv))
                self.assertNotIn(token, str(kwargs["env"]))
                self.assertEqual(json.loads(kwargs["input"])["github_token"], token)
                return subprocess.CompletedProcess(argv, 1, json.dumps({"status": "ERROR", "reason": token}), token)
            with patch.object(subprocess, "run", side_effect=invoked):
                with self.assertRaises(prog.ProgramError) as error:
                    prog.fable_program("check", 31, pr=7, head=runtime.HEAD)
                self.assertNotIn(token, str(error.exception))


if __name__ == "__main__":
    unittest.main()
