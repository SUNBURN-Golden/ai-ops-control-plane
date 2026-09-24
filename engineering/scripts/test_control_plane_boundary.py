#!/usr/bin/env python3
"""Regression tests for scripts/control_plane_boundary.py.

These tests cover policy parsing, claim resolution, evaluation, the
fail-closed enforce path, install verification and worker-process discovery.
They do not replace the live probe-runner evidence required by the task.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_plane_boundary as cb  # noqa: E402


def write_json(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def sample_policy() -> dict:
    return {
        "schema_version": 1,
        "policy_id": "test-policy",
        "required_claims": ["env:GITHUB_RUN_ID", "event:repository.id"],
        "allow": [
            {"name": "control", "claims": {
                "env:GITHUB_REPOSITORY_ID": "1365416872",
                "env:GITHUB_REF": "refs/heads/main",
                "env:GITHUB_WORKFLOW_SHA": "abc123",
                "env:GITHUB_EVENT_NAME": "workflow_dispatch",
                "env:GITHUB_ACTOR_ID": "263336091",
                "event:repository.id": 1365416872,
                "event:repository.owner.id": 263336091,
            }},
        ],
    }


def sample_event() -> dict:
    return {
        "repository": {"id": 1365416872, "full_name": "BeautifulMind-JT/kix-protocol",
                       "owner": {"id": 263336091}},
        "sender": {"id": 263336091},
        "commits": [{"id": "deadbeef"}],
    }


def sample_env() -> dict:
    return {
        "GITHUB_RUN_ID": "777",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "control",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "BeautifulMind-JT/kix-protocol",
        "GITHUB_REPOSITORY_ID": "1365416872",
        "GITHUB_REPOSITORY_OWNER_ID": "263336091",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "cafe",
        "GITHUB_WORKFLOW_REF": "owner/repo/.github/workflows/control.yml@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": "abc123",
        "GITHUB_ACTOR": "BeautifulMind-JT",
        "GITHUB_ACTOR_ID": "263336091",
        "RUNNER_NAME": "probe",
        "HOME": "/should/not/leak",
    }


class PolicyTests(unittest.TestCase):
    def test_valid_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "p.json", sample_policy())
            policy = cb.load_policy(path)
            self.assertEqual(policy["policy_id"], "test-policy")

    def test_reject_unknown_keys_and_bad_schema(self):
        for mutate in (
            lambda p: p.update(extra=1),
            lambda p: p.update(schema_version=2),
            lambda p: p.update(allow=[]),
            lambda p: p.update(policy_id="bad id"),
            lambda p: p["allow"][0].update(claims={}),
            lambda p: p["allow"][0]["claims"].update({"env:HOME": "x"}),
            lambda p: p["allow"][0]["claims"].update({"event:..x": 1}),
            lambda p: p["allow"][0]["claims"].update({"env:GITHUB_REF": 1.5}),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                policy = sample_policy()
                mutate(policy)
                path = write_json(Path(tmp) / "p.json", policy)
                with self.assertRaises(cb.BoundaryError):
                    cb.load_policy(path)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text('{"schema_version":1,"schema_version":1,"policy_id":"x","allow":[]}')
            with self.assertRaises(cb.BoundaryError):
                cb.load_policy(path)

    def test_missing_policy_denies(self):
        with self.assertRaises(cb.BoundaryError):
            cb.load_policy(Path("/nonexistent/policy.json"))


class ClaimTests(unittest.TestCase):
    def test_env_claims_filtered_to_runner_prefixes(self):
        claims = cb.collect_claims(sample_env(), sample_event())
        self.assertEqual(claims["env:GITHUB_REF"], "refs/heads/main")
        self.assertNotIn("env:HOME", claims)
        self.assertEqual(claims["event:repository.owner.id"], 263336091)
        self.assertEqual(claims["event:commits.0.id"], "deadbeef")

    def test_event_resolution_edges(self):
        event = sample_event()
        self.assertIsNone(cb.resolve_event(event, "repository.missing"))
        self.assertIsNone(cb.resolve_event(event, "commits.5.id"))
        self.assertIsNone(cb.resolve_event(event, "repository.id.x"))
        self.assertEqual(cb.resolve_event(event, "repository.full_name"),
                         "BeautifulMind-JT/kix-protocol")

    def test_non_scalar_event_values_never_become_claims(self):
        event = {"repository": {"id": 1.5, "null": None, "list": [1], "obj": {"x": 1}}}
        claims = cb.collect_claims({}, event)
        self.assertNotIn("event:repository.id", claims)
        self.assertNotIn("event:repository.null", claims)
        self.assertEqual(claims["event:repository.list.0"], 1)
        self.assertEqual(claims["event:repository.obj.x"], 1)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.policy = sample_policy()
        self.claims = cb.collect_claims(sample_env(), sample_event())

    def assert_denied(self, claims):
        allowed, rule, reasons = cb.evaluate(self.policy, claims)
        self.assertFalse(allowed)
        self.assertIsNone(rule)
        self.assertTrue(reasons)

    def test_allow(self):
        allowed, rule, reasons = cb.evaluate(self.policy, self.claims)
        self.assertTrue(allowed)
        self.assertEqual(rule, "control")

    def test_wrong_ref_repo_actor_event_sha(self):
        for ref in ("env:GITHUB_REF", "env:GITHUB_REPOSITORY_ID", "env:GITHUB_ACTOR_ID",
                    "env:GITHUB_EVENT_NAME", "env:GITHUB_WORKFLOW_SHA",
                    "event:repository.id", "event:repository.owner.id"):
            claims = dict(self.claims)
            claims[ref] = "999999" if isinstance(claims[ref], str) else 999999
            self.assert_denied(claims)

    def test_absent_claim_never_matches(self):
        claims = dict(self.claims)
        del claims["env:GITHUB_REF"]
        self.assert_denied(claims)
        claims = dict(self.claims)
        claims["env:GITHUB_REF"] = ""
        self.assert_denied(claims)

    def test_type_strictness(self):
        claims = dict(self.claims)
        claims["event:repository.id"] = "1365416872"  # str, not int
        self.assert_denied(claims)

    def test_required_claim_missing(self):
        claims = dict(self.claims)
        del claims["env:GITHUB_RUN_ID"]
        allowed, _, reasons = cb.evaluate(self.policy, claims)
        self.assertFalse(allowed)
        self.assertIn("required claim absent", reasons[0])

    def test_list_expected(self):
        policy = sample_policy()
        policy["allow"][0]["claims"]["env:GITHUB_EVENT_NAME"] = ["push", "workflow_dispatch"]
        claims = dict(self.claims)
        claims["env:GITHUB_EVENT_NAME"] = "push"
        allowed, _, _ = cb.evaluate(policy, claims)
        self.assertTrue(allowed)


class EnforceTests(unittest.TestCase):
    """End-to-end enforce against fabricated env/event, isolated dirs."""

    def run_enforce(self, env_overrides=None, policy_obj=None, policy_sha=True,
                    event_obj=None, evidence_dir=None, policy_path=None, self_path=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ev = evidence_dir or tmp / "evidence"
            if not ev.exists():
                ev.mkdir()
            if policy_path is None:
                policy_path = tmp / "policy.json"
                write_json(policy_path, policy_obj if policy_obj is not None else sample_policy())
            sha = (hashlib.sha256(policy_path.read_bytes()).hexdigest()
                   if policy_sha and policy_path.exists() else "0" * 64)
            if event_obj is not False:
                write_json(tmp / "event.json", event_obj if event_obj is not None else sample_event())
                event_path = str(tmp / "event.json")
            else:
                event_path = "/nonexistent/event.json"
            env = dict(sample_env())
            env.update({
                cb.POLICY_ENV: str(policy_path),
                cb.POLICY_SHA_ENV: sha,
                cb.EVIDENCE_ENV: str(ev),
                "GITHUB_EVENT_PATH": event_path,
            })
            if env_overrides:
                env.update(env_overrides)
            args = argparse.Namespace(policy=None, policy_sha256=None,
                                      evidence_dir=None, self_path=self_path)
            with mock.patch.dict(os.environ, env, clear=True):
                rc = cb.cmd_enforce(args)
            records = list(ev.glob("decision-*.json"))
            record = json.loads(records[0].read_text()) if records else None
            return rc, record

    def test_allow(self):
        rc, record = self.run_enforce()
        self.assertEqual(rc, 0)
        self.assertEqual(record["decision"], "ALLOW")
        self.assertEqual(record["matched_rule"], "control")

    def test_deny_ref_mismatch(self):
        rc, record = self.run_enforce({"GITHUB_REF": "refs/heads/evil"})
        self.assertEqual(rc, cb.DENY)
        self.assertEqual(record["decision"], "DENY")
        self.assertIn("env:GITHUB_REF", record["reasons"][0])

    def test_deny_missing_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, record = self.run_enforce(policy_path=Path(tmp) / "gone.json")
        self.assertEqual(rc, cb.DENY)
        self.assertIn("unreadable", record["reasons"][0])

    def test_deny_digest_mismatch(self):
        rc, record = self.run_enforce(policy_sha=False)
        self.assertEqual(rc, cb.DENY)
        self.assertIn("sha256 mismatch", record["reasons"][0])

    def test_deny_missing_config(self):
        rc, record = self.run_enforce({cb.POLICY_SHA_ENV: ""})
        self.assertEqual(rc, cb.DENY)
        self.assertIn("configuration incomplete", record["reasons"][0])

    def test_deny_malformed_event(self):
        rc, record = self.run_enforce(event_obj=False)
        self.assertEqual(rc, cb.DENY)

    def test_deny_hook_env_mismatch(self):
        rc, record = self.run_enforce(
            env_overrides={cb.HOOK_ENV: "/evil/other.sh"},
            self_path="/boundary/control_plane_boundary_hook.sh")
        self.assertEqual(rc, cb.DENY)
        self.assertTrue(any("does not match" in r for r in record["reasons"]))

    def test_deny_self_check_without_hook_env(self):
        for value in (None, ""):
            with self.subTest(hook_env=value):
                overrides = {} if value is None else {cb.HOOK_ENV: value}
                rc, record = self.run_enforce(
                    env_overrides=overrides,
                    self_path="/boundary/control_plane_boundary_hook.sh")
                self.assertEqual(rc, cb.DENY)
                self.assertIn("not set", record["reasons"][0])

    def test_deny_noncanonical_policy_sha(self):
        for sha in ("A" * 64, "0" * 63, "0" * 65, "0g" + "0" * 62):
            with self.subTest(sha=sha):
                rc, record = self.run_enforce(env_overrides={cb.POLICY_SHA_ENV: sha})
                self.assertEqual(rc, cb.DENY)
                self.assertIn("hex digest", record["reasons"][0])

    def test_deny_non_object_and_unreadable_policy(self):
        rc, record = self.run_enforce(policy_obj=[1, 2])
        self.assertEqual(rc, cb.DENY)
        self.assertIn("policy invalid", record["reasons"][0])
        with tempfile.TemporaryDirectory() as tmp:
            rc, record = self.run_enforce(policy_path=Path(tmp), policy_sha=False)
        self.assertEqual(rc, cb.DENY)
        self.assertIn("unreadable", record["reasons"][0])

    def test_evidence_write_failure_denies_otherwise_allowed_job(self):
        # An evidence dir that cannot hold a decision record turns an
        # otherwise-ALLOW job into ERROR with no persisted ALLOW record.
        for make in ("world-writable", "not-a-directory"):
            with self.subTest(make=make):
                with tempfile.TemporaryDirectory() as tmp:
                    ev = Path(tmp) / "evidence"
                    if make == "world-writable":
                        ev.mkdir()
                        ev.chmod(0o777)
                    else:
                        ev.write_text("not a directory")
                    rc, record = self.run_enforce(evidence_dir=ev)
                self.assertEqual(rc, cb.ERROR)
                self.assertIsNone(record)

    def test_actor_mismatch_lists_claim(self):
        policy = sample_policy()
        policy["allow"][0]["claims"]["env:GITHUB_ACTOR_ID"] = "999999"
        rc, record = self.run_enforce(policy_obj=policy)
        self.assertEqual(rc, cb.DENY)
        self.assertIn("GITHUB_ACTOR_ID", record["reasons"][0])


class VerifyInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=Path.home()))
        self.hook = write_json(self.tmp / "hook.sh", {})  # placeholder replaced below
        self.hook.write_text("#!/bin/sh\ncontrol_plane_boundary.py enforce deny-job\n")
        self.hook.chmod(0o755)
        self.policy = write_json(self.tmp / "policy.json", sample_policy())
        self.policy.chmod(0o644)
        self.evaluator = self.tmp / "control_plane_boundary.py"
        self.evaluator.write_text("# trusted test evaluator\n")
        self.evaluator.chmod(0o644)
        self.hook_sha = hashlib.sha256(self.hook.read_bytes()).hexdigest()
        self.policy_sha = hashlib.sha256(self.policy.read_bytes()).hexdigest()

    def verify(self, **kw):
        evaluator_sha = kw.get("evaluator_sha256")
        if evaluator_sha is None:
            evaluator_sha = (hashlib.sha256(self.evaluator.read_bytes()).hexdigest()
                             if self.evaluator.exists() else "0" * 64)
        args = argparse.Namespace(
            evaluator_sha256=evaluator_sha,
            hook=str(kw.get("hook", self.hook)),
            policy=str(kw.get("policy", self.policy)),
            hook_sha256=kw.get("hook_sha256", self.hook_sha),
            policy_sha256=kw.get("policy_sha256", self.policy_sha),
            owner_uid=kw.get("owner_uid"),
            forbid_prefix=kw.get("forbid_prefix"),
            writable_check=kw.get("writable_check", False),
            env_file=kw.get("env_file"),
            env_forbid=kw.get("env_forbid", [cb.HOOK_ENV, cb.POLICY_ENV, cb.POLICY_SHA_ENV]),
            env_require=kw.get("env_require", {}),
        )
        return cb.cmd_verify_install(args)

    def test_pass(self):
        self.assertEqual(self.verify(), 0)

    def test_evaluator_and_parent_protection(self):
        self.evaluator.chmod(0o666)
        self.assertEqual(self.verify(), cb.DENY)
        self.evaluator.chmod(0o644)
        self.tmp.chmod(0o777)
        self.assertEqual(self.verify(), cb.DENY)
        self.tmp.chmod(0o700)
        self.assertEqual(self.verify(evaluator_sha256="0" * 64), cb.DENY)
        self.assertEqual(self.verify(evaluator_sha256=""), cb.DENY)

    def test_all_boundary_overrides_rejected(self):
        env = self.tmp / ".env"
        for key in ("ASTRA_BOUNDARY_PYTHON", "ASTRA_BOUNDARY_KILL", "ASTRA_BOUNDARY_FLUSH_SECONDS", "BASH_ENV", "LD_PRELOAD", "PYTHONPATH"):
            env.write_text(key + "=/usr/bin/true\n")
            self.assertEqual(self.verify(env_file=str(env)), cb.DENY)

    def test_env_file_must_be_protected(self):
        env = self.tmp / ".env"
        env.write_text("LANG=C\n")
        env.chmod(0o666)
        self.assertEqual(self.verify(env_file=str(env)), cb.DENY)

    def test_digest_mismatch(self):
        self.assertEqual(self.verify(hook_sha256="0" * 64), cb.DENY)
        self.assertEqual(self.verify(policy_sha256="0" * 64), cb.DENY)

    def test_writable_mode_rejected(self):
        self.policy.chmod(0o666)
        self.assertEqual(self.verify(), cb.DENY)

    def test_missing_hook_rejected(self):
        self.assertEqual(self.verify(hook=self.tmp / "gone.sh"), cb.DENY)

    def test_hook_missing_tokens_rejected(self):
        self.hook.write_text("#!/bin/sh\nexit 0\n")
        self.evaluator = self.tmp / "control_plane_boundary.py"
        self.evaluator.write_text("# trusted test evaluator\n")
        self.evaluator.chmod(0o644)
        self.hook_sha = hashlib.sha256(self.hook.read_bytes()).hexdigest()
        self.assertEqual(self.verify(), cb.DENY)

    def test_forbid_prefix(self):
        self.assertEqual(self.verify(forbid_prefix=str(self.tmp)), cb.DENY)
        self.assertEqual(self.verify(forbid_prefix="/other/dir"), 0)

    def test_env_file_present_but_omitted_denies(self):
        """A conventional runner env file next to the policy must not be skipped."""
        env = self.tmp / ".env"
        env.write_text("LANG=C\n")
        self.assertEqual(self.verify(), cb.DENY)
        self.assertEqual(self.verify(env_file=str(env)), 0)

    def test_env_file(self):
        env = self.tmp / ".env"
        env.write_text("LANG=C\n")
        self.assertEqual(self.verify(env_file=str(env)), 0)
        env.write_text(f"{cb.HOOK_ENV}=/evil.sh\n")
        self.assertEqual(self.verify(env_file=str(env)), cb.DENY)
        env.write_text(f"A=1\nA=2\n")
        self.assertEqual(self.verify(env_file=str(env)), cb.DENY)
        env.write_text("LANG=C\n")
        self.assertEqual(self.verify(env_file=str(env),
                                     env_require={cb.HOOK_ENV: str(self.hook)}), cb.DENY)

    def test_empty_env_file_argument_denies(self):
        # An explicitly empty --env-file must fail closed, not silently skip
        # both the supplied-file checks and the conventional .env check.
        self.assertEqual(self.verify(env_file=""), cb.DENY)
        (self.tmp / ".env").write_text("LANG=C\n")
        self.assertEqual(self.verify(env_file=""), cb.DENY)

    def test_env_require_satisfied_missing_and_mismatch(self):
        env = self.tmp / ".env"
        env.write_text("LANG=C\n")
        self.assertEqual(self.verify(env_file=str(env), env_require={"LANG": "C"}), 0)
        env.write_text("LANG=ko\n")
        self.assertEqual(self.verify(env_file=str(env), env_require={"LANG": "C"}), cb.DENY)
        env.write_text("TZ=UTC\n")
        self.assertEqual(self.verify(env_file=str(env), env_require={"LANG": "C"}), cb.DENY)

    def test_symlink_and_hardlink_targets_rejected(self):
        link = self.tmp / "hook-link.sh"
        link.symlink_to(self.hook)
        self.assertEqual(self.verify(hook=link), cb.DENY)
        hard = self.tmp / "policy-hard.json"
        os.link(self.policy, hard)
        try:
            self.assertEqual(self.verify(policy=hard), cb.DENY)
        finally:
            hard.unlink()

    def test_missing_evaluator_rejected(self):
        self.evaluator.unlink()
        self.assertEqual(self.verify(), cb.DENY)

    def test_group_writable_parent_rejected(self):
        self.tmp.chmod(0o770)
        try:
            self.assertEqual(self.verify(), cb.DENY)
        finally:
            self.tmp.chmod(0o700)


class CheckEnvTests(unittest.TestCase):
    """cmd_check_env: launch-environment assertions and injection denies."""

    HOOK = "/opt/astra/boundary/control_plane_boundary_hook.sh"
    POLICY = "/opt/astra/boundary/policy.json"
    SHA = "a" * 64

    def run_check_env(self, env_overrides=None, drop=()):
        env = {
            cb.HOOK_ENV: self.HOOK,
            cb.POLICY_ENV: self.POLICY,
            cb.POLICY_SHA_ENV: self.SHA,
            cb.EVIDENCE_ENV: "/var/lib/astra/boundary-evidence",
        }
        env.update(env_overrides or {})
        for key in drop:
            env.pop(key, None)
        args = argparse.Namespace(hook=self.HOOK, policy=self.POLICY,
                                  policy_sha256=self.SHA)
        with mock.patch.dict(os.environ, env, clear=True):
            return cb.cmd_check_env(args)

    def test_pass_baseline(self):
        self.assertEqual(self.run_check_env(), 0)

    def test_deny_process_env_injection(self):
        for key in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "LD_PRELOAD",
                    "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "PYTHONPATH",
                    "PYTHONSTARTUP", "PYTHONHOME", "BASH_FUNC_x%%"):
            self.assertEqual(self.run_check_env({key: "/tmp/evil"}), cb.DENY, key)

    def test_deny_boundary_overrides(self):
        for key in ("ASTRA_BOUNDARY_PYTHON", "ASTRA_BOUNDARY_KILL",
                    "ASTRA_BOUNDARY_FLUSH_SECONDS"):
            self.assertEqual(self.run_check_env({key: "1"}), cb.DENY, key)

    def test_deny_wrong_pins_and_missing_evidence(self):
        self.assertEqual(self.run_check_env({cb.HOOK_ENV: "/evil.sh"}), cb.DENY)
        self.assertEqual(self.run_check_env({cb.POLICY_ENV: "/evil.json"}), cb.DENY)
        self.assertEqual(self.run_check_env({cb.POLICY_SHA_ENV: "b" * 64}), cb.DENY)
        self.assertEqual(self.run_check_env(drop=(cb.EVIDENCE_ENV,)), cb.DENY)
        self.assertEqual(self.run_check_env({cb.EVIDENCE_ENV: ""}), cb.DENY)

    def test_deny_missing_boundary_pins(self):
        # A job-controlled environment cannot satisfy the pins by omission.
        for key in (cb.HOOK_ENV, cb.POLICY_ENV, cb.POLICY_SHA_ENV):
            with self.subTest(missing=key):
                self.assertEqual(self.run_check_env(drop=(key,)), cb.DENY)
        for key in (cb.HOOK_ENV, cb.POLICY_ENV, cb.POLICY_SHA_ENV):
            with self.subTest(empty=key):
                self.assertEqual(self.run_check_env({key: ""}), cb.DENY)


class WorkerPidTests(unittest.TestCase):
    """worker_pids / _proc_ppid against a fabricated proc tree."""

    def make_proc(self, tmp: Path, chain):
        """chain: list of (pid, comm, ppid) built from self upward."""
        for pid, comm, ppid in chain:
            d = tmp / str(pid)
            d.mkdir()
            (d / "comm").write_text(comm)
            (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0 0")

    def test_finds_worker_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # hook(pid via getppid patch) -> bash(50) -> Runner.Worker(40) -> Runner.Listener(30) -> init
            self.make_proc(tmp, [(50, "bash", 40), (40, "Runner.Worker", 30),
                                 (30, "Runner.Listener", 1)])
            with mock.patch.object(os, "getppid", return_value=50):
                self.assertEqual(cb.worker_pids(tmp), [40])

    def test_no_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.make_proc(tmp, [(50, "bash", 1)])
            with mock.patch.object(os, "getppid", return_value=50):
                self.assertEqual(cb.worker_pids(tmp), [])

    def test_proc_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.make_proc(tmp, [(40, "Runner.Worker", 30), (41, "bash", 40)])
            self.assertEqual(cb.all_worker_pids(tmp), [40])

    def test_stat_parsing(self):
        self.assertEqual(cb._proc_ppid("40 (Runner.Worker) S 30 40 40 0"), 30)
        self.assertIsNone(cb._proc_ppid("garbage"))


if __name__ == "__main__":
    unittest.main()
