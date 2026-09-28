"""Host-local dispatch route regressions; no GitHub, host helper or provider access."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import control_plane_local as local


class LocalDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.lock_dir = root / "locks"
        self.lock_dir.mkdir(mode=0o700)
        self.token_path = root / "token"
        self.token_path.write_text("ghp_exampleToken\n")
        self.token_path.chmod(0o600)
        self.policy_path = root / "local-dispatch.json"
        self.policy = {"schema_version": 1, "enabled": True, "token_path": str(self.token_path),
                       "lock_dir": str(self.lock_dir)}
        self.write_policy(self.policy)
        self.args = argparse.Namespace(
            command="dispatch", target="BeautifulMind-JT/kix-protocol", issue_number=7,
            expected_task_id="T1", expected_task_revision="1", expected_builder_id="DEVIN",
            expected_issue_body_sha256="a" * 64, expected_attempt_id=None)
        self.calls = []
        patches = [patch.object(local, "POLICY_OWNER_UID", os.geteuid()),
                   patch.object(local, "require_main_checkout"),
                   patch.object(local, "token_login", return_value="BeautifulMind-JT")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def write_policy(self, policy):
        self.policy_path.write_text(json.dumps(policy))
        self.policy_path.chmod(0o644)

    def fake_prepare(self, launch_required="true"):
        def prepare(issue, packet):
            self.calls.append(("prepare", issue, {k: os.environ.get(k) for k in local.DISPATCH_ENV}))
            with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
                handle.write(f"launch_required={launch_required}\n")
                if launch_required == "true":
                    handle.write("launch_request_id=request-1\n")
            packet.write_text("{}")
        return prepare

    def run_dispatch(self, launch_required="true", launch_error=None, final_state="CONFIRMED"):
        def launch(packet, result):
            self.calls.append(("launch",))
            if launch_error:
                raise launch_error
        def finalize(issue, result, request):
            self.calls.append(("finalize", issue, request))
            return final_state
        with patch.object(local.cp, "validate_repo", side_effect=lambda: self.calls.append(("validate",))), \
             patch.object(local.cp, "prepare_dispatch", side_effect=self.fake_prepare(launch_required)), \
             patch.object(local.cp, "launch_dispatch", side_effect=launch), \
             patch.object(local.cp, "finalize_dispatch", side_effect=finalize):
            return local.dispatch(self.args, local.load_policy(self.policy_path))

    def test_runs_workflow_sequence_with_token_identity(self):
        before = {key: os.environ.get(key) for key in local.DISPATCH_ENV}
        self.assertEqual(self.run_dispatch(), 0)
        self.assertEqual([c[0] for c in self.calls], ["validate", "prepare", "launch", "finalize"])
        env = self.calls[1][2]
        self.assertEqual(env["GITHUB_ACTOR"], "BeautifulMind-JT")
        self.assertEqual(env["GITHUB_TRIGGERING_ACTOR"], "BeautifulMind-JT")
        self.assertEqual(env["GITHUB_TOKEN"], "ghp_exampleToken")
        self.assertEqual(env["GITHUB_REPOSITORY"], "BeautifulMind-JT/ai-ops-control-plane")
        self.assertEqual(env["ASTRA_TARGET_REPOSITORY"], "BeautifulMind-JT/kix-protocol")
        self.assertEqual((env["EXPECTED_TASK_ID"], env["EXPECTED_BUILDER_ID"]), ("T1", "DEVIN"))
        self.assertIsNone(env["EXPECTED_ATTEMPT_ID"])
        self.assertEqual(self.calls[3][1:], (7, "request-1"))
        # Nothing from the dispatch leaks into the caller's environment.
        self.assertTrue(before == {key: os.environ.get(key) for key in local.DISPATCH_ENV},
                        "dispatch environment was not restored")

    def test_explicit_retry_attempt_is_forwarded(self):
        self.args.expected_attempt_id = 2
        self.run_dispatch()
        self.assertEqual(self.calls[1][2]["EXPECTED_ATTEMPT_ID"], "2")

    def test_caller_environment_cannot_inject_dispatch_values(self):
        with patch.dict(os.environ, {"GITHUB_ACTOR": "someone-else", "EXPECTED_ATTEMPT_ID": "9",
                                     "GITHUB_OUTPUT": "/tmp/elsewhere"}):
            self.run_dispatch()
            self.assertEqual(os.environ["GITHUB_ACTOR"], "someone-else")
        env = self.calls[1][2]
        self.assertEqual(env["GITHUB_ACTOR"], "BeautifulMind-JT")
        self.assertIsNone(env["EXPECTED_ATTEMPT_ID"])
        self.assertNotEqual(env["GITHUB_OUTPUT"], "/tmp/elsewhere")

    def test_no_launch_when_prepare_finds_existing_owner(self):
        self.assertEqual(self.run_dispatch(launch_required="false"), 0)
        self.assertEqual([c[0] for c in self.calls], ["validate", "prepare"])

    def test_finalize_runs_even_when_launch_raises(self):
        with self.assertRaises(OSError):
            self.run_dispatch(launch_error=OSError("helper vanished"))
        self.assertEqual([c[0] for c in self.calls], ["validate", "prepare", "launch", "finalize"])

    def test_unconfirmed_final_state_exits_nonzero(self):
        for state in ("UNKNOWN", "FAILED_PRESTART"):
            with self.subTest(state=state):
                self.calls.clear()
                self.assertEqual(self.run_dispatch(final_state=state), 3)

    def test_second_dispatch_for_same_task_is_refused_while_first_holds_lock(self):
        with local.task_lock(self.lock_dir, self.args.target, self.args.issue_number):
            with self.assertRaisesRegex(local.LocalDispatchError, "still running"):
                self.run_dispatch()
            # A different task is independent.
            with local.task_lock(self.lock_dir, self.args.target, 8):
                pass
        self.assertEqual(self.calls, [])

    def test_disabled_or_unprotected_policy_stops_before_any_call(self):
        cases = [dict(self.policy, enabled=False), dict(self.policy, schema_version=2),
                 dict(self.policy, token_path="relative/token"), dict(self.policy, lock_dir="/x/../y")]
        for policy in cases:
            with self.subTest(policy=policy):
                self.write_policy(policy)
                with self.assertRaises(local.LocalDispatchError):
                    local.load_policy(self.policy_path)
        self.write_policy(self.policy)
        self.policy_path.chmod(0o666)
        with self.assertRaisesRegex(local.LocalDispatchError, "unprotected"):
            local.load_policy(self.policy_path)
        with self.assertRaisesRegex(local.LocalDispatchError, "missing"):
            local.load_policy(self.policy_path.with_name("absent.json"))

    def test_token_file_must_be_owner_only(self):
        self.token_path.chmod(0o640)
        with self.assertRaisesRegex(local.LocalDispatchError, "mode 600"):
            self.run_dispatch()
        self.token_path.chmod(0o600)
        self.token_path.write_text("two words\n")
        with self.assertRaisesRegex(local.LocalDispatchError, "malformed"):
            self.run_dispatch()
        self.assertEqual(self.calls, [])

    def test_lock_directory_must_be_owner_only(self):
        self.lock_dir.chmod(0o755)
        with self.assertRaisesRegex(local.LocalDispatchError, "owner-only"):
            self.run_dispatch()
        self.assertEqual(self.calls, [])

    def test_checkout_must_be_main(self):
        with patch.object(local, "require_main_checkout",
                          side_effect=local.LocalDispatchError("local dispatch runs only from a checkout of main")):
            with self.assertRaisesRegex(local.LocalDispatchError, "main"):
                self.run_dispatch()
        self.assertEqual(self.calls, [])

    def test_real_main_checkout_guard_reads_branch(self):
        for code, stdout, allowed in ((0, "main\n", True), (0, "feature\n", False), (1, "", False)):
            branch = local.subprocess.CompletedProcess([], code, stdout, "")
            with self.subTest(stdout=stdout), patch.object(local.subprocess, "run", return_value=branch):
                if allowed:
                    REAL_REQUIRE_MAIN()
                else:
                    with self.assertRaises(local.LocalDispatchError):
                        REAL_REQUIRE_MAIN()

    def test_cli_maps_errors_to_exit_code_two(self):
        with patch.object(local, "POLICY_PATH", self.policy_path.with_name("absent.json")):
            self.assertEqual(local.main(["preflight", "--target", "BeautifulMind-JT/kix-protocol"]), 2)
        for bad in ("0", "-3", "x"):
            with self.subTest(issue=bad), self.assertRaises(SystemExit):
                local.main(["dispatch", "--target", "t", "--issue-number", bad, "--expected-task-id", "T",
                            "--expected-task-revision", "1", "--expected-builder-id", "DEVIN",
                            "--expected-issue-body-sha256", "a" * 64])


REAL_REQUIRE_MAIN = local.require_main_checkout


if __name__ == "__main__":
    unittest.main()
