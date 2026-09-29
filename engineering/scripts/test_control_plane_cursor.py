"""Cursor source registration: no installed adapter or live provider is assumed."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import control_plane as cp
import control_plane_flow as flow
import control_plane_host as host


def policy(*, cursor=False, enabled=None):
    builders = {"DEVIN": 1030, "GROK_BUILD": 1040, "GLM": 1050}
    if cursor:
        builders["CURSOR"] = 1060
    return {
        "control_uid": 1010, "runner_uid": 1020, "builder_uids": builders,
        "allowed_repositories": ["owner/repo"],
        "enabled_builders": enabled or ["DEVIN"],
        "max_active_sessions": 1, "max_launches_per_24h": None,
        "ledger_path": "/var/lib/astra/control/admission.sqlite3",
        "wrapper_paths": {key: host.WRAPPERS[key] for key in builders},
        "boundary_evidence_pointer": "https://github.com/owner/ops/issues/1",
    }


def cursor_report(**changes):
    result = {
        "status": "PASS", "builder_id": "CURSOR", "harness": "CURSOR_CLI",
        "model": "grok-4.7", "cli_version": "fixture-version", "host_id": "fixture-host",
        "execution_mode": "PERSISTENT_SUPERVISOR", "parallel_safe": True,
        "launch_contract_version": 2, "worktree_root": "/opt/astra/worktrees/cursor",
        "runtime_sha": "a" * 40, "wrapper_sha256": "b" * 64, "binary_sha256": "c" * 64,
        "evidence_pointer": "https://github.com/owner/ops/issues/2",
        "checks": {key: "PASS" for key in (
            "cli", "authentication", "credential_isolation", "durable_session",
            "duplicate_unknown", "trusted_workflow_boundary", "quota_policy")},
    }
    result.update(changes)
    return result


def approval(report):
    return {"active": True, "pointer": "https://github.com/owner/ops/issues/3",
            "report_digest": flow.digest(report)}


class CursorRegistrationTests(unittest.TestCase):
    def test_legacy_host_policy_remains_valid_without_cursor_uid_or_wrapper(self):
        host.validate_policy(policy())
        host.validate_policy(policy(cursor=True))

    def test_enabling_unregistered_cursor_or_missing_cursor_adapter_is_rejected(self):
        with self.assertRaisesRegex(host.HostError, "registered builders"):
            host.validate_policy(policy(enabled=["CURSOR"]))
        missing = policy(cursor=True, enabled=["CURSOR"])
        del missing["wrapper_paths"]["CURSOR"]
        with self.assertRaisesRegex(host.HostError, "fixed installed adapter"):
            host.validate_policy(missing)

    def test_cursor_uid_cannot_share_a_writer_or_control_identity(self):
        for uid in (1010, 1020, 1030, 0):
            value = policy(cursor=True)
            value["builder_uids"]["CURSOR"] = uid
            with self.subTest(uid=uid), self.assertRaisesRegex(host.HostError, "distinct and non-root"):
                host.validate_policy(value)

    def test_enabled_cursor_missing_on_disk_fails_closed(self):
        value = policy(cursor=True, enabled=["CURSOR"])
        def protected(path, **_):
            if str(path) == host.WRAPPERS["CURSOR"]:
                raise FileNotFoundError("Cursor adapter not installed")
        with patch.object(host.Path, "read_text", return_value=json.dumps(value)), \
             patch.object(host, "__file__", str(host.INSTALLED_PATH)), \
             patch.object(host, "authorize_identity"), \
             patch.object(host, "protected_root_path", side_effect=protected), \
             self.assertRaisesRegex(FileNotFoundError, "not installed"):
            host.load_host_policy("preflight")

    def test_registered_cursor_is_not_automatically_enabled_for_a_product(self):
        profiles = cp.load_json(cp.CONFIG_PATH.with_name("projects.json"))
        for repo in profiles:
            with self.subTest(repo=repo), patch.dict(os.environ, {"ASTRA_TARGET_REPOSITORY": repo}):
                cfg = cp.load_config()
                self.assertIn("CURSOR", cfg["allowed_builders"])
                self.assertNotIn("CURSOR", cfg["enabled_builders"])
                with patch.object(cp, "host_call") as invoke, \
                     self.assertRaisesRegex(cp.ControlPlaneError, "builder is not enabled"):
                    cp.host_preflight("CURSOR")
                invoke.assert_not_called()


class CursorQualificationTests(unittest.TestCase):
    def test_cursor_grok_model_keeps_cursor_identity_without_production_approval(self):
        report = cursor_report()
        result = flow.qualify_lane(report, approval(report), "a" * 40)
        self.assertEqual(result["status"], "QUALIFIED_FOR_INDEPENDENT_REVIEW")
        self.assertFalse(result["production_enabled"])
        self.assertEqual(report["builder_id"], "CURSOR")

    def test_native_harness_or_unresolved_model_cannot_qualify_as_cursor(self):
        cases = [{"harness": name} for name in ("GROK_BUILD", "GLM", "OPENCODE")]
        cases += [{"model": name} for name in ("auto", "CONFIG_REQUIRED", "PENDING", "unknown", "", " model ")]
        for changes in cases:
            report = cursor_report(**changes)
            with self.subTest(changes=changes), self.assertRaises(flow.FlowError):
                flow.qualify_lane(report, approval(report), "a" * 40)

    def test_model_change_invalidates_prior_qualification_approval(self):
        report = cursor_report()
        old_approval = approval(report)
        report["model"] = "a-different-explicit-model"
        with self.assertRaisesRegex(flow.FlowError, "exact report"):
            flow.qualify_lane(report, old_approval, "a" * 40)

    def test_cursor_preflight_checks_provenance_at_host_and_central_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = host.Ledger(Path(tmp) / "admission.sqlite")
            ledger.initialize()
            value = policy(cursor=True, enabled=["CURSOR"])
            good = cursor_report()
            for report in (good, cursor_report(harness="GROK_BUILD"), cursor_report(model="auto")):
                with self.subTest(report=report["model"] + report["harness"]), \
                     patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess(
                         [], 0, json.dumps(report))) as invoke:
                    if report != good:
                        with self.assertRaisesRegex(host.HostError, "provenance"):
                            host.preflight("CURSOR", value, ledger)
                    else:
                        actual = host.preflight("CURSOR", value, ledger)
                        self.assertEqual(actual["model"], good["model"])
                    self.assertEqual(invoke.call_args.args[0], [host.WRAPPERS["CURSOR"], "--preflight"])
                returned = dict(report, host_admission="ENFORCED",
                                boundary_evidence_pointer=value["boundary_evidence_pointer"],
                                helper_sha256=hashlib.sha256(Path(host.__file__).read_bytes()).hexdigest(),
                                allowed_repositories=["owner/repo"])
                cfg = {"enabled_builders": ["CURSOR"], "repository": "owner/repo"}
                def host_reply(args, returned=returned):
                    # The central preflight also probes the read-only status command.
                    if args[0] == "status":
                        return {"launch_request_id": args[2], "state": None}
                    return returned
                with patch.object(cp, "load_config", return_value=cfg), \
                     patch.object(cp, "host_call", side_effect=host_reply):
                    if report != good:
                        with self.assertRaisesRegex(cp.ControlPlaneError, "provenance"):
                            cp.host_preflight("CURSOR")
                    else:
                        with patch("builtins.print"):
                            cp.host_preflight("CURSOR")

    def test_cursor_unknown_keeps_single_writer_and_never_relaunches(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = policy(cursor=True, enabled=["CURSOR", "DEVIN"])
            value["ledger_path"] = str(Path(tmp) / "admission.sqlite")
            ledger = host.Ledger(value["ledger_path"])
            ledger.initialize()
            packet = {"schema_version": 1, "repository": "owner/repo", "task_id": "T1",
                      "task_revision": "1", "builder_id": "CURSOR", "launch_request_id": "r1",
                      "attempt_id": 1}
            calls = []
            def lose_response(*_):
                calls.append("cursor")
                raise subprocess.TimeoutExpired("cursor", 180)
            first = host.launch(packet, value, ledger, lose_response)
            self.assertEqual(first["outcome"], "UNKNOWN")
            self.assertEqual(host.launch(copy.deepcopy(packet), value, ledger, lose_response), first)
            alternate = dict(packet, builder_id="DEVIN", launch_request_id="r2")
            self.assertEqual(host.launch(alternate, value, ledger, lose_response)["outcome"], "FAILED_PRESTART")
            self.assertEqual(calls, ["cursor"])


if __name__ == "__main__":
    unittest.main()
