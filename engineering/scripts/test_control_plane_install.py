#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import control_plane_install as ci

SHA = "edbeea2966fea1f574c20c6cf65f9adc84652472"
OTHER_SHA = "6d543a7c4540f42d099294d9da0679a9548b1817"

DISABLED_ACTIVATION = {
    "schema_version": 1,
    "user_activation_approval": "NOT_APPROVED",
    "user_activation_approval_pointer": None,
    "implementation_audit": "PENDING",
    "implementation_audit_pointer": None,
    "runner_preflight": "PENDING",
    "runner_preflight_pointer": None,
    "runtime_enabled": False,
    "activated_runtime_sha": "PENDING",
}

HOST_POLICY = {
    "control_uid": 998,
    "runner_uid": 997,
    "builder_uids": {"DEVIN": 996, "GROK_BUILD": 995, "GLM": 994},
    "allowed_repositories": ["BeautifulMind-JT/kix-protocol"],
    "enabled_builders": ["DEVIN", "GROK_BUILD", "GLM"],
    "max_active_sessions": 1,
    "max_launches_per_24h": None,
    "ledger_path": "/var/lib/astra/control/admission.sqlite3",
    "wrapper_paths": dict(ci.__dict__.get("WRAPPERS", {}) or {
        "DEVIN": "/opt/astra/bin/astra-builder-devin",
        "GROK_BUILD": "/opt/astra/bin/astra-builder-grok-build",
        "GLM": "/opt/astra/bin/astra-builder-glm",
    }),
    "boundary_evidence_pointer": "https://github.com/BeautifulMind-JT/kix-protocol/pull/32",
}


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.policy = root / "control-plane-host.json"
        self.pin = root / "RUNTIME_SOURCE_SHA.txt"
        self.activation = root / "activation.json"
        self.policy.write_text(json.dumps(HOST_POLICY), encoding="utf-8")
        self.pin.write_text(OTHER_SHA + "\n", encoding="utf-8")
        self.activation.write_text(json.dumps(DISABLED_ACTIVATION), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()


class TestDisabledGate(InstallCase):
    def test_rejects_bad_sha(self):
        for bad in ("", "abc", "EDBEEA2966FEA1F574C20C6CF65F9ADC84652472", SHA + "0", "g" * 40):
            with self.assertRaises(ci.InstallError):
                ci.build_plan(bad, self.policy, self.pin, self.activation)

    def test_rejects_enabled_activation(self):
        enabled = dict(DISABLED_ACTIVATION, runtime_enabled=True,
                       user_activation_approval="APPROVED",
                       implementation_audit="PASS", runner_preflight="PASS")
        self.activation.write_text(json.dumps(enabled), encoding="utf-8")
        with self.assertRaises(ci.InstallError):
            ci.build_plan(SHA, self.policy, self.pin, self.activation)

    def test_rejects_inherited_kix_evidence(self):
        inherited = dict(DISABLED_ACTIVATION, implementation_audit="PASS")
        self.activation.write_text(json.dumps(inherited), encoding="utf-8")
        with self.assertRaises(ci.InstallError):
            ci.build_plan(SHA, self.policy, self.pin, self.activation)


class TestPlan(InstallCase):
    def test_plan_is_disabled_and_non_mutating(self):
        before = self.policy.read_text()
        plan = ci.build_plan(SHA, self.policy, self.pin, self.activation)
        self.assertEqual(plan["status"], "DISABLED_INSTALL_PLAN")
        self.assertEqual(plan["control_source_sha"], SHA)
        self.assertFalse(plan["runtime_enabled"])
        self.assertEqual(plan["unchanged"]["allowed_repositories"],
                         ["BeautifulMind-JT/kix-protocol"])
        self.assertEqual(self.policy.read_text(), before)
        self.assertEqual(self.pin.read_text().strip(), OTHER_SHA)


class TestApplyAndVerify(InstallCase):
    def test_apply_pins_and_preserves_policy(self):
        result = ci.apply(SHA, self.policy, self.pin, self.activation)
        self.assertEqual(result["status"], "INSTALLED_DISABLED")
        self.assertEqual(self.pin.read_text().strip(), SHA)
        policy = json.loads(self.policy.read_text())
        self.assertEqual(policy["control_repository"], ci.CONTROL_REPOSITORY)
        self.assertEqual(policy["control_source_sha"], SHA)
        self.assertIs(policy["control_runtime_enabled"], False)
        # Consumer allowlist, builder set, ledger and wrappers are untouched.
        for key in HOST_POLICY:
            self.assertEqual(policy[key], HOST_POLICY[key])

    def test_apply_is_idempotent(self):
        ci.apply(SHA, self.policy, self.pin, self.activation)
        result = ci.apply(SHA, self.policy, self.pin, self.activation)
        self.assertEqual(result["status"], "INSTALLED_DISABLED")

    def test_verify_reports_failures(self):
        report = ci.verify(SHA, self.policy, self.pin, self.activation)
        self.assertEqual(report["status"], "FAIL")
        by_name = {c["check"]: c["ok"] for c in report["checks"]}
        self.assertTrue(by_name["activation_disabled"])
        self.assertFalse(by_name["source_pin"])
        self.assertFalse(by_name["policy_control_source_sha"])

    def test_verify_passes_after_apply(self):
        ci.apply(SHA, self.policy, self.pin, self.activation)
        report = ci.verify(SHA, self.policy, self.pin, self.activation)
        self.assertEqual(report["status"], "PASS")

    def test_cli_apply_requires_confirmation(self):
        rc = ci.main(["apply", "--sha", SHA, "--policy-path", str(self.policy),
                      "--pin-path", str(self.pin), "--activation-path", str(self.activation)])
        self.assertEqual(rc, 2)
        self.assertEqual(self.pin.read_text().strip(), OTHER_SHA)


if __name__ == "__main__":
    unittest.main()
