"""Regression of Fable #44 authority/entry findings; no provider or host calls."""
import contextlib
import io
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


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.r = runtime.ProgramModeTests(); self.r.setUp(); self.addCleanup(self.r.doCleanups)
        self.r.cfg["deployment_enabled"] = True

    def test_release_delegation_rejected_before_a3_normalization(self):
        for floor in ("A2", "A3"):
            with self.subTest(floor=floor), self.assertRaisesRegex(prog.ProgramError, "RELEASE"):
                prog.validate_plan(runtime.plan([runtime.node(floor=floor, astra_gate="RELEASE", astra_auto_merge=True)]),
                                   self.r.cfg)
        plan = prog.validate_plan(runtime.plan([runtime.node(floor="A3", astra_gate="RELEASE", user_merge=True)]),
                                  self.r.cfg)
        self.assertEqual(plan["nodes"][0]["astra_gate"], "RELEASE")

    def test_release_never_calls_the_automatic_receipt_path(self):
        self.r.gh.contents[runtime.PLAN1] = runtime.plan([runtime.node(floor="A3", astra_gate="RELEASE", user_merge=True)])
        issue, _ = self.r.released_writer()
        self.r.reviewed(issue, slot=1, depth="A2"); self.r.reviewed(issue, slot=2, depth="A2")
        with patch.object(prog, "fable_program", side_effect=AssertionError("no automatic gate")):
            answer = prog.merge_check(issue, 7)
        self.assertFalse(answer["ready"]); self.assertEqual(answer["astra_status"], "USER_REQUIRED")

    def test_audit_allowed_is_computed_from_all_ordinary_holds(self):
        self.r.gh.contents[runtime.PLAN1] = runtime.plan([runtime.node(floor="A3", astra_auto_merge=True)])
        issue, _ = self.r.released_writer()
        self.r.reviewed(issue, slot=1, depth="A2"); self.r.reviewed(issue, slot=2, depth="A2")
        with patch.object(prog, "fable_program", return_value={"status": "MISSING"}):
            self.assertTrue(prog.merge_check(issue, 7)["astra_audit_allowed"])
            self.r.gh.checks[runtime.HEAD][0]["conclusion"] = "failure"
            self.assertFalse(prog.merge_check(issue, 7)["astra_audit_allowed"])

    def test_read_only_status_can_check_reconciled_writer_and_edited_body(self):
        self.r.gh.contents[runtime.PLAN1] = runtime.plan([runtime.node(floor="A3", astra_auto_merge=True)])
        issue, writer = self.r.released_writer()
        rows = prog.task_rows(self.r.cfg, prog.task_id_for(runtime.plan()["program"], "n1"))
        row = next(row for row in rows if row["launch_request_id"] == writer["launch_request_id"])
        row["resolution"] = "SESSION_TERMINAL"
        self.r.gh.issues[issue]["body"] += "\nedited projection"
        with patch.object(prog, "task_rows", return_value=rows):
            binding, *_ = bridge.request_context(self.r.gh, self.r.cfg, issue, decision_only=True)
            self.assertEqual(binding["writer_launch"], writer["launch_request_id"])
            self.assertIsNone(prog.pin_of(row, "DELIVERY"))
            with self.assertRaises(bridge.BridgeError):
                bridge.request_context(self.r.gh, self.r.cfg, issue, 7, runtime.HEAD)

    def test_status_rejects_unknown_writer(self):
        self.r.gh.contents[runtime.PLAN1] = runtime.plan([runtime.node(floor="A3", astra_auto_merge=True)])
        issue, writer = self.r.released_writer()
        rows = prog.task_rows(self.r.cfg, prog.task_id_for(runtime.plan()["program"], "n1"))
        next(row for row in rows if row["launch_request_id"] == writer["launch_request_id"])["state"] = "UNKNOWN"
        with patch.object(prog, "task_rows", return_value=rows), self.assertRaises(bridge.BridgeError):
            bridge.request_context(self.r.gh, self.r.cfg, issue, decision_only=True)

    def test_direct_bridge_entry_cannot_skip_adoption_guard(self):
        ctx = SimpleNamespace(tool_sha256="a" * 64)
        payload = {"repository": runtime.REPO, "issue": 31, "operation": "decision-status", "github_token": "test"}
        with patch.object(bridge, "protected"), patch.object(prog.cp, "load_config", return_value=self.r.cfg), \
                patch.object(bridge, "installed_fingerprint", return_value="a" * 64), \
                patch.object(bridge, "require_service_authorization", side_effect=bridge.BridgeError("pending")), \
                patch.object(prog.cp, "GithubApi") as api:
            with self.assertRaisesRegex(bridge.BridgeError, "pending"):
                bridge.run(ctx, fable, payload)
            api.assert_not_called()

    def test_cancel_transport_failure_does_not_resubmit_or_echo_stderr(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "private-test-secret"}), \
                patch.object(prog.cp, "load_config", return_value=self.r.cfg), \
                patch.object(subprocess, "run", side_effect=PermissionError("private-test-secret")) as run:
            with self.assertRaisesRegex(prog.ProgramError, "may still be running") as error:
                prog.fable_program("audit", 31, pr=7, head=runtime.HEAD)
            self.assertNotIn("private-test-secret", str(error.exception)); self.assertEqual(run.call_count, 1)


class ServiceAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "program-astra-authorization.json"
        p = patch.object(prog.cp, "CONFIG_PATH", Path(self.tmp.name) / "config.json"); p.start(); self.addCleanup(p.stop)
        p = patch.object(bridge, "protected"); p.start(); self.addCleanup(p.stop)
        p = patch.object(prog.cp, "load_activation", return_value={"runtime_enabled": True,
                        "activated_runtime_sha": "c" * 40}); p.start(); self.addCleanup(p.stop)
        self.record = {"schema_version": 1, "state": "ACTIVE", "runtime_enabled": True,
                       "approved_commit": "c" * 40, "installed_fingerprint": "a" * 64,
                       "decision_pointer": "https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/46#issuecomment-1",
                       "audit_pointer": "https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/46#issuecomment-2",
                       "qualification_pointer": "https://github.com/BeautifulMind-JT/ai-ops-control-plane/actions/runs/3",
                       "qualification_sha256": "d" * 64}

    def test_missing_pending_stale_or_bad_evidence_record_holds(self):
        with self.assertRaises(bridge.BridgeError): bridge.require_service_authorization("a" * 64)
        changes = [{"state": "PENDING"}, {"runtime_enabled": False}, {"installed_fingerprint": "b" * 64},
                   {"approved_commit": "b" * 40}, {"decision_pointer": "PENDING"},
                   {"audit_pointer": "https://github.com/attacker/repo/pull/1"}, {"schema_version": True},
                   {"qualification_sha256": "PENDING"}, {"unknown": "field"}]
        for extra in changes:
            with self.subTest(extra=extra):
                self.path.write_text(json.dumps({**self.record, **extra}))
                with self.assertRaises(bridge.BridgeError): bridge.require_service_authorization("a" * 64)

    def test_active_record_and_activation_must_agree(self):
        self.path.write_text(json.dumps(self.record))
        self.assertEqual(bridge.require_service_authorization("a" * 64), self.record)
        with patch.object(prog.cp, "load_activation", return_value={"runtime_enabled": False}):
            with self.assertRaisesRegex(bridge.BridgeError, "disabled"):
                bridge.require_service_authorization("a" * 64)

    def test_duplicate_json_record_never_grants_authority(self):
        raw = json.dumps(self.record)
        self.path.write_text(raw[:-1] + ',"state":"ACTIVE"}')
        with self.assertRaisesRegex(bridge.BridgeError, "duplicate"):
            bridge.require_service_authorization("a" * 64)

    def test_installed_policy_activation_and_profile_changes_invalidate_fingerprint(self):
        root = Path(self.tmp.name); config = root / "config.json"; profiles = root / "projects.json"
        activation = root / "activation.json"; config.write_text("config"); profiles.write_text("profiles")
        activation.write_text("activation")
        for name in bridge.POLICY_PATHS:
            p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(name)
        with patch.object(prog.cp, "ROOT", root), patch.object(prog.cp, "ACTIVATION_PATH", activation):
            first = bridge.installed_fingerprint(SimpleNamespace(tool_sha256="a" * 64))
            for path in (profiles, activation, root / bridge.POLICY_PATHS[0]):
                old = path.read_text(); path.write_text(old + "changed")
                self.assertNotEqual(first, bridge.installed_fingerprint(SimpleNamespace(tool_sha256="a" * 64)))
                path.write_text(old)


class MainEntryTests(unittest.TestCase):
    def invoke(self, raw, *, run_result=None, run_error=None, trust_error=False):
        output = io.StringIO()
        ctx = SimpleNamespace(secret_values=("private-claude-test-secret",))
        root = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        with patch.object(fable.sys, "stdin", io.StringIO(raw)), patch.object(fable.sys, "path", list(fable.sys.path)), \
                patch.object(fable.os, "lstat", return_value=root), \
                patch.object(fable, "root_file", side_effect=fable.FableError("bad support") if trust_error else None) as trust, \
                patch.object(fable, "production_context", return_value=(ctx, "test")) as production, \
                patch.object(bridge, "run", return_value=run_result, side_effect=run_error) as run, \
                contextlib.redirect_stdout(output):
            rc = fable.main(["program"])
        return rc, json.loads(output.getvalue()), trust, production, run

    def test_valid_entry_uses_bounded_stdin_and_checks_three_installed_modules(self):
        token = "private-github-test-secret"; payload = {"operation": "check", "github_token": token}
        rc, result, trust, production, run = self.invoke(json.dumps(payload), run_result={"status": "MISSING"})
        self.assertEqual(rc, 0); self.assertEqual(result["status"], "MISSING"); self.assertEqual(trust.call_count, 3)
        self.assertEqual(production.call_args.kwargs["github_token"], token)
        self.assertEqual(run.call_args.args[2], payload)

    def test_oversized_or_untrusted_entry_never_reaches_production_or_bridge(self):
        for raw, bad_trust in (("x" * 65537, False),
                               (json.dumps({"github_token": "가" * 30000}, ensure_ascii=False), False),
                               (json.dumps({"github_token": "test"}), True)):
            rc, result, _, production, run = self.invoke(raw, trust_error=bad_trust)
            self.assertEqual(rc, 1); self.assertEqual(result["status"], "ERROR")
            production.assert_not_called(); run.assert_not_called()

    def test_entry_error_redacts_stdin_ctx_and_token_shaped_credentials(self):
        token = "private-github-test-secret"; shaped = "github_pat_" + "Z" * 30
        rc, result, *_ = self.invoke(json.dumps({"github_token": token}), run_error=bridge.BridgeError(
            f"policy failure {token} private-claude-test-secret {shaped}"))
        self.assertEqual(rc, 1)
        for secret in (token, "private-claude-test-secret", shaped): self.assertNotIn(secret, result["reason"])
        self.assertIn("policy failure", result["reason"])

    def test_error_receipt_preserves_reason_without_credentials(self):
        with tempfile.TemporaryDirectory() as root, patch.object(bridge, "protected"):
            store = bridge.Receipts(root, "a" * 64, ("private-test-secret",))
            def fail(): raise fable.FableError("OVERAGE_NOT_BLOCKED private-test-secret " + "gho_" + "Z" * 30)
            with self.assertRaises(fable.FableError): store.execute("audit", {}, fail)
            result = store.read("audit", {})
            self.assertIn("OVERAGE_NOT_BLOCKED", result["reason"])
            self.assertNotIn("private-test-secret", result["reason"]); self.assertNotIn("gho_", result["reason"])


if __name__ == "__main__":
    unittest.main()
