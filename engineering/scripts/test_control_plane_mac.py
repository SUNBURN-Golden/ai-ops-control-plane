"""Offline fault tests for the isolated Mac diagnostic partition, no provider calls."""
import concurrent.futures
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

import control_plane_mac as mac


class FakeLaunchd:
    domain = "gui/501"

    def __init__(self):
        self.calls = []
        self.state = "ABSENT"
        self.failure = False

    def call(self, *args):
        self.calls.append(args)
        if self.failure:
            raise subprocess.TimeoutExpired("launchctl", 30)
        if args[0] == "bootstrap":
            self.state = "RUNNING"
        if args[0] == "bootout":
            self.state = "ABSENT"
        return subprocess.CompletedProcess(args, 0, "", "")

    def status(self, label):
        return self.state


class MacTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for directory in ("control", "workspaces", "bin"):
            (self.root / directory).mkdir(mode=0o700)
        self.runtime = mac.Runtime.__new__(mac.Runtime)
        self.runtime.root = self.root
        self.runtime.launchd = FakeLaunchd()
        self.runtime.policy = {"host_id": str(uuid.uuid4()), "intake": f"https://github.com/{mac.REPOSITORY}/issues/21",
                               "allowed_repositories": [mac.REPOSITORY], "enabled_builders": list(mac.LANES),
                               "max_active_sessions": 1, "python": "/usr/bin/python3"}
        self.runtime.ledger = mac.Ledger(self.root / "control" / "admission.sqlite")
        self.runtime.ledger.initialize()
        self.packet = self.runtime.new("ISOLATION")
        self.request = self.packet["launch_request_id"]

    def terminal(self):
        directory = self.runtime.directory(self.request)
        mac.create_file(directory / "probe.stdout", "public output\n")
        mac.create_file(directory / "receipt.json", mac.canonical({"packet": self.packet, "terminal": True,
            "child_group_gone": True, "stdout_sha256": mac.digest(directory / "probe.stdout")}))
        self.runtime.launchd.state = "EXITED"

    def test_minted_namespace_is_not_existing_task(self):
        mac.validate_request(self.packet, self.runtime.policy)
        self.assertTrue(self.packet["task_id"].startswith("macdiag:"))
        self.assertEqual(self.packet["attempt_id"], 1)

    def test_reject_imports_extra_prompts_revisions_and_attempts(self):
        for change in ({"task_id": "CP-EXISTING"}, {"prompt": "build"}, {"attempt_id": 2},
                       {"task_revision": "r2"}, {"repository": "owner/other"}, {"builder_id": "CLAUDE"}):
            with self.subTest(change=change), self.assertRaises(mac.HostError):
                mac.validate_request({**self.packet, **change}, self.runtime.policy)

    def test_foreign_host_namespace_rejected(self):
        with self.assertRaises(mac.HostError):
            mac.validate_request(self.packet, {**self.runtime.policy, "host_id": str(uuid.uuid4())})

    def test_unsafe_request_paths_rejected(self):
        for request in ("../policy", "", str(uuid.uuid4()).upper()):
            with self.subTest(request=request), self.assertRaises((mac.HostError, ValueError)):
                self.runtime.packet(request)

    def test_durable_reservation_precedes_bootstrap(self):
        original = self.runtime.launchd.call
        def checked(*args):
            self.assertEqual(self.runtime.row(self.request)["state"], "SUBMITTING")
            return original(*args)
        self.runtime.launchd.call = checked
        self.assertEqual(self.runtime.start(self.request)["outcome"], "CONFIRMED")

    def test_duplicate_never_bootstraps_twice(self):
        first = self.runtime.start(self.request)
        self.assertEqual(first, self.runtime.start(self.request))
        self.assertEqual(len(self.runtime.launchd.calls), 1)

    def test_simultaneous_duplicate_has_one_sender(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.runtime.start(self.request), range(8)))
        self.assertEqual(len(self.runtime.launchd.calls), 1)
        self.assertTrue(all(result["outcome"] in {"UNKNOWN", "CONFIRMED"} for result in results))

    def test_unknown_is_never_replayed_and_holds_capacity(self):
        self.runtime.launchd.failure = True
        result = self.runtime.start(self.request)
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.runtime.launchd.failure = False
        self.assertEqual(self.runtime.start(self.request), result)
        second = self.runtime.new("LIFETIME")
        self.assertEqual(self.runtime.start(second["launch_request_id"])["outcome"], "FAILED_PRESTART")
        self.assertEqual(len(self.runtime.launchd.calls), 1)

    def test_submitter_crash_reservation_is_not_replayed(self):
        self.runtime.ledger.reserve(self.packet, self.runtime.policy)
        self.assertEqual(self.runtime.start(self.request)["outcome"], "UNKNOWN")
        self.assertEqual(self.runtime.launchd.calls, [])

    def test_restart_reads_existing_owner_without_launch(self):
        self.runtime.start(self.request)
        self.runtime.ledger = mac.Ledger(self.runtime.ledger.path)
        count = len(self.runtime.launchd.calls)
        state = self.runtime.status(self.request)
        self.assertEqual(state["ledger_state"], "CONFIRMED")
        self.assertEqual(count, len(self.runtime.launchd.calls))

    def test_missing_ledger_fails_without_recreating(self):
        path = self.root / "control" / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            mac.Ledger(path).connect()
        self.assertFalse(path.exists())

    def test_plist_tampering_never_executes(self):
        path = self.runtime.directory(self.request) / "job.plist"
        value = plistlib.loads(path.read_bytes())
        value["ProgramArguments"] = ["/bin/sh", "-c", "false"]
        path.write_bytes(plistlib.dumps(value))
        self.assertEqual(self.runtime.start(self.request)["outcome"], "UNKNOWN")
        self.assertEqual(self.runtime.launchd.calls, [])

    def test_recovery_altered_packet_is_rejected(self):
        self.runtime.start(self.request)
        path = self.runtime.directory(self.request) / "packet.json"
        path.write_text(mac.canonical({**self.packet, "builder_id": "LIFETIME"}))
        with self.assertRaises(mac.HostError):
            self.runtime.status(self.request)

    def test_absence_alone_does_not_release_owner(self):
        self.runtime.start(self.request)
        self.runtime.launchd.state = "ABSENT"
        with self.assertRaises(mac.HostError):
            self.runtime.release(self.request, self.runtime.policy["intake"])
        self.assertEqual(self.runtime.row(self.request)["state"], "CONFIRMED")

    def test_receipt_alone_does_not_release_running_owner(self):
        self.runtime.start(self.request)
        self.terminal()
        self.runtime.launchd.state = "RUNNING"
        with self.assertRaises(mac.HostError):
            self.runtime.release(self.request, self.runtime.policy["intake"])

    def test_unknown_unit_does_not_release_owner(self):
        self.runtime.start(self.request)
        self.terminal()
        self.runtime.launchd.state = "UNKNOWN"
        with self.assertRaises(mac.HostError):
            self.runtime.release(self.request, self.runtime.policy["intake"])

    def test_terminal_release_preserves_dedupe_tombstone(self):
        previous = self.runtime.start(self.request)
        self.terminal()
        self.assertEqual(self.runtime.release(self.request, self.runtime.policy["intake"])["status"], "RECONCILED")
        self.assertEqual(self.runtime.start(self.request), previous)
        self.assertEqual([call[0] for call in self.runtime.launchd.calls], ["bootstrap", "bootout"])

    def test_report_tampering_blocks_release(self):
        self.runtime.start(self.request)
        self.terminal()
        (self.runtime.directory(self.request) / "probe.stdout").write_text("changed")
        with self.assertRaises(mac.HostError):
            self.runtime.release(self.request, self.runtime.policy["intake"])

    def test_bootout_ambiguity_keeps_reservation(self):
        self.runtime.start(self.request)
        self.terminal()
        self.runtime.launchd.failure = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.runtime.release(self.request, self.runtime.policy["intake"])
        self.assertEqual(self.runtime.row(self.request)["state"], "CONFIRMED")

    def test_worker_marker_prevents_second_execution(self):
        self.runtime.start(self.request)
        mac.create_file(self.runtime.directory(self.request) / "execution.started", "already started")
        with patch.object(mac.subprocess, "Popen") as invoke, self.assertRaises(FileExistsError):
            self.runtime.worker(self.request)
        invoke.assert_not_called()

    def test_production_policy_cannot_be_enabled(self):
        with self.assertRaises(mac.HostError):
            mac.verify_policy(self.root, {**self.runtime.policy, "production_enabled": True})

    def test_writes_refuse_existing_and_symlink_targets(self):
        target = self.root / "protected"
        mac.create_file(target, "original")
        link = self.root / "link"
        link.symlink_to(target)
        for path in (target, link):
            with self.assertRaises(FileExistsError):
                mac.create_file(path, "replacement")
        self.assertEqual(target.read_text(), "original")

    def test_credentialless_profile_is_network_deny_and_signal_self(self):
        value = mac.profile(self.root / "workspaces" / self.request, ["/public/bin"])
        self.assertIn("(deny default)", value)
        self.assertNotIn("allow network", value)
        self.assertIn("(target self)", value)
        self.assertNotIn(str(Path.home() / ".grok"), value)

    def test_adapter_digest_change_refuses_command(self):
        binary = self.root / "cli"
        binary.write_text("first")
        policy = {"adapters": {"DEVIN": {"argv": [str(binary), "--version"], "pins": {str(binary): mac.digest(binary)}}}}
        self.assertEqual(mac.command(policy, "DEVIN"), [str(binary), "--version"])
        binary.write_text("changed")
        with self.assertRaises(mac.HostError):
            mac.command(policy, "DEVIN")

    def test_prompt_command_not_accepted(self):
        with self.assertRaises(mac.HostError):
            mac.command({"adapters": {"GLM": {"pins": {}, "argv": ["node", "zcode.cjs", "--prompt", "do work"]}}}, "GLM")


class LaunchdStatusTests(unittest.TestCase):
    def test_errors_are_not_absence(self):
        launchd = mac.Launchd(501)
        for message in ("Permission denied", "Could not find domain", "Input/output error"):
            with patch.object(launchd, "call", return_value=subprocess.CompletedProcess([], 1, "", message)):
                self.assertEqual(launchd.status("test"), "UNKNOWN")

    def test_exact_service_absence(self):
        launchd = mac.Launchd(501)
        with patch.object(launchd, "call", return_value=subprocess.CompletedProcess([], 113, "", 'Could not find service "test" in domain for user gui: 501')):
            self.assertEqual(launchd.status("test"), "ABSENT")
            self.assertEqual(launchd.status("different"), "UNKNOWN")

    def test_known_states_only(self):
        launchd = mac.Launchd(501)
        for observed, expected in (("running", "RUNNING"), ("not running", "EXITED"), ("unfamiliar", "UNKNOWN")):
            with patch.object(launchd, "call", return_value=subprocess.CompletedProcess([], 0, f"\tstate = {observed}\n", "")):
                self.assertEqual(launchd.status("test"), expected)


class CleanupTests(unittest.TestCase):
    def test_delayed_descendant_exit_is_observed_before_sealing_receipt(self):
        now = [0.0]
        def sleep(delay):
            now[0] += delay
        with patch.object(mac.os, "killpg", side_effect=[None, None, ProcessLookupError()]) as probe:
            self.assertTrue(mac.wait_group_gone(123, clock=lambda: now[0], sleep=sleep))
        self.assertEqual(probe.call_count, 3)
        self.assertGreater(now[0], 0)

    def test_group_not_confirmed_absent_remains_unresolved_after_bound(self):
        now = [0.0]
        def sleep(delay):
            now[0] += delay
        with patch.object(mac.os, "killpg", return_value=None):
            self.assertFalse(mac.wait_group_gone(123, timeout=0.1, clock=lambda: now[0], sleep=sleep))
        self.assertAlmostEqual(now[0], 0.1)

    def test_permission_error_is_not_proof_of_group_absence(self):
        with patch.object(mac.os, "killpg", side_effect=PermissionError()), self.assertRaises(PermissionError):
            mac.wait_group_gone(123)


if __name__ == "__main__":
    unittest.main()
