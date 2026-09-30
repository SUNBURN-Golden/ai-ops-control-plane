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

    def test_stored_user_required_blocks_builder_resume_even_without_labels(self):
        issue, _, _ = self.reviewed_delivery()
        self.r.gh.issues[issue]["labels"] = []
        with patch.object(prog, "fable_program", return_value={"status": "BLOCKED"}):
            with self.assertRaisesRegex(prog.ProgramError, "USER_REQUIRED"):
                self.r.launch_writer(issue)

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
