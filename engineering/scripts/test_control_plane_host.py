"""Local fault/concurrency tests; no installed policy, provider or root access needed."""
import concurrent.futures
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import control_plane_host as host


def packet(index=0, **changes):
    value = {"schema_version": 1, "repository": f"owner/repo{index % 5}", "task_id": f"T-{index}",
             "task_revision": "r1", "builder_id": "DEVIN", "launch_request_id": f"request-{index}",
             "attempt_id": 1, "task_issue": index + 1}
    value.update(changes)
    return value


def reserve_process(path, policy, value):
    return host.Ledger(path).reserve(value, policy)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "admission.sqlite"
        self.now = 1_000_000
        self.ledger = host.Ledger(self.path, clock=lambda: self.now)
        self.ledger.initialize()
        self.policy = {
            "control_uid": 1010, "runner_uid": 1020,
            "builder_uids": {"DEVIN": 1030, "GROK_BUILD": 1040, "GLM": 1050},
            "allowed_repositories": [f"owner/repo{i}" for i in range(5)],
            "enabled_builders": ["DEVIN"], "max_active_sessions": 2,
            "max_launches_per_24h": None, "ledger_path": str(self.path),
            "wrapper_paths": {builder: host.WRAPPERS[builder] for builder in ("DEVIN", "GROK_BUILD", "GLM")},
            "boundary_evidence_pointer": "https://github.com/owner/ops/issues/12",
        }
        self.calls = []

    def invoke(self, value, policy):
        self.calls.append(value["launch_request_id"])
        return subprocess.CompletedProcess([], 0, json.dumps(host.result_for(value, "CONFIRMED", session_id="session-" + value["launch_request_id"])))

    def assertReleasedRefusal(self, result):
        self.assertEqual((result["outcome"], result["session_id"]), ("FAILED_PRESTART", None))
        self.assertIn("already released", result["reason"])

    def state(self, request="request-0"):
        db = sqlite3.connect(self.path)
        try:
            return db.execute("SELECT state FROM launches WHERE request=?", (request,)).fetchone()[0]
        finally:
            db.close()

    def test_concurrent_processes_share_capacity_across_five_repositories(self):
        self.policy["enabled_builders"] = ["DEVIN", "GROK_BUILD", "GLM"]
        lanes = ("DEVIN", "GROK_BUILD", "GLM")
        with concurrent.futures.ProcessPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(reserve_process, str(self.path), self.policy,
                                   packet(i, builder_id=lanes[i % 3])) for i in range(15)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(admitted for admitted, _ in results), 2)
        self.assertEqual(sum(result["outcome"] == "FAILED_PRESTART" for _, result in results), 13)

    def test_concurrent_processes_admit_one_session_per_lane(self):
        self.policy["max_active_sessions"] = 4
        with concurrent.futures.ProcessPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(reserve_process, str(self.path), self.policy, packet(i)) for i in range(12)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(admitted for admitted, _ in results), 1)
        refused = [result["reason"] for admitted, result in results if not admitted]
        self.assertTrue(all(reason == "lane busy" for reason in refused))

    def test_concurrent_duplicate_admits_only_once(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(reserve_process, str(self.path), self.policy, packet()) for _ in range(8)]
            self.assertEqual(sum(future.result(timeout=20)[0] for future in futures), 1)

    def test_duplicate_returns_cached_result_without_second_provider_call(self):
        first = host.launch(packet(), self.policy, self.ledger, self.invoke)
        second = host.launch(dict(reversed(list(packet().items()))), self.policy, self.ledger, self.invoke)
        self.assertEqual(first, second)
        self.assertEqual(self.calls, ["request-0"])
        self.assertEqual(self.state(), "CONFIRMED")

    def test_request_identity_includes_entire_packet(self):
        host.launch(packet(), self.policy, self.ledger, self.invoke)
        for changes in ({"task_issue": 99}, {"task_revision": "r2"}, {"attempt_id": 2}, {"task_id": "different"}):
            with self.subTest(changes=changes), self.assertRaises(host.HostError):
                host.launch(packet(**changes), self.policy, self.ledger, self.invoke)
        self.assertEqual(len(self.calls), 1)

    def test_new_revision_or_issue_cannot_create_second_task_writer(self):
        host.launch(packet(), self.policy, self.ledger, self.invoke)
        other = packet(5, task_id="T-0", task_revision="r2", task_issue=99)
        result = host.launch(other, self.policy, self.ledger, self.invoke)
        self.assertEqual(result["outcome"], "FAILED_PRESTART")
        self.assertIn("task already", result["reason"])
        self.assertEqual(len(self.calls), 1)

    def test_unknown_holds_slot_forever_and_duplicate_never_resends(self):
        self.policy["max_active_sessions"] = 1
        def ambiguous(value, policy):
            self.calls.append("ambiguous")
            raise subprocess.TimeoutExpired("adapter", 180)
        result = host.launch(packet(), self.policy, self.ledger, ambiguous)
        self.now += 86400 * 100
        self.assertEqual(host.launch(packet(), self.policy, self.ledger, self.invoke), result)
        self.assertEqual(host.launch(packet(1), self.policy, self.ledger, self.invoke)["outcome"], "FAILED_PRESTART")
        self.assertEqual(self.state(), "UNKNOWN")
        self.assertEqual(self.calls, ["ambiguous"])

    def test_operator_proven_no_session_releases_slot_without_replay(self):
        self.policy["max_active_sessions"] = 1
        self.ledger.reserve(packet(), self.policy)  # crash before any adapter call
        evidence = "https://github.com/owner/ops/issues/13"
        for options in ({"no_session": True}, {"no_session": True, "sender_fenced": False}):
            with self.assertRaises(host.HostError):
                self.ledger.reconcile("request-0", None, evidence, **options)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", None, "", no_session=True, sender_fenced=True)
        result = self.ledger.reconcile("request-0", None, evidence, no_session=True, sender_fenced=True)
        self.assertEqual(result["resolution"], "NO_SESSION_CONFIRMED")
        self.assertIsNone(result["session_id"])
        self.assertEqual(self.state(), "RECONCILED")
        # The first result is retained: replay never authorizes another send.
        self.assertReleasedRefusal(host.launch(packet(), self.policy, self.ledger, self.invoke))
        self.assertEqual(self.calls, [])
        self.assertEqual(host.launch(packet(1), self.policy, self.ledger, self.invoke)["outcome"], "CONFIRMED")
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT sum(admitted) FROM launches").fetchone()[0], 2)
        finally:
            db.close()

    def test_known_session_cannot_be_released_as_no_session(self):
        host.launch(packet(), self.policy, self.ledger, self.invoke)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", None, "https://github.com/owner/ops/issues/13",
                                  no_session=True, sender_fenced=True)
        self.assertEqual(self.state(), "CONFIRMED")

    def test_process_interruption_leaves_durable_submitting_record(self):
        self.policy["max_active_sessions"] = 1
        def crash(value, policy):
            self.assertEqual(self.state(), "SUBMITTING")
            raise KeyboardInterrupt("simulated process death after reservation")
        with self.assertRaises(KeyboardInterrupt):
            host.launch(packet(), self.policy, self.ledger, crash)
        restarted = host.Ledger(self.path, clock=lambda: self.now)
        self.assertEqual(host.launch(packet(), self.policy, restarted, self.invoke)["outcome"], "UNKNOWN")
        self.now += 86400 * 100
        self.assertEqual(host.launch(packet(1), self.policy, restarted, self.invoke)["outcome"], "FAILED_PRESTART")
        self.assertEqual(self.state(), "SUBMITTING")
        self.assertEqual(self.calls, [])

    def test_malformed_nonzero_and_wrong_identity_results_are_unknown(self):
        good = host.result_for(packet(), "CONFIRMED", session_id="actual-session")
        cases = [(0, "not JSON"), (1, json.dumps(good)), (0, json.dumps({**good, "task_revision": "stale"})),
                 (0, json.dumps({**good, "attempt_id": True})), (0, json.dumps({**good, "session_id": ""})),
                 (0, json.dumps({**good, "outcome": "FAILED_PRESTART", "reason": "claims a live session"})),
                 (0, json.dumps({**good, "outcome": "UNKNOWN", "session_id": {"bad": "type"}})),
                 (0, json.dumps({**good, "outcome": "FAILED_PRESTART", "session_id": None}))]
        self.policy["max_active_sessions"] = len(cases)
        for index, (code, stdout) in enumerate(cases):
            with self.subTest(index=index):
                # Each UNKNOWN keeps its lane busy, so every case gets its own ledger.
                self.path = Path(self.temp.name) / f"admission-{index}.sqlite"
                self.ledger = host.Ledger(self.path, clock=lambda: self.now)
                self.ledger.initialize()
                # Bind all other fields to this request so the malformed field is decisive.
                if index:
                    output = json.loads(stdout)
                    original = {key: val for key, val in output.items() if key not in host.IDENTITY}
                    output = {**host.result_for(packet(index), "CONFIRMED", session_id="actual-session"), **original}
                    if index == 2:
                        output["task_revision"] = "stale"
                    if index == 3:
                        output["attempt_id"] = True
                    stdout = json.dumps(output)
                result = host.launch(packet(index), self.policy, self.ledger,
                                     lambda value, policy, code=code, stdout=stdout: subprocess.CompletedProcess([], code, stdout))
                self.assertEqual(result["outcome"], "UNKNOWN")
                for field in host.IDENTITY:
                    self.assertEqual(result[field], packet(index)[field])
                self.assertEqual(self.state(f"request-{index}"), "UNKNOWN")

    def test_definite_prestart_failure_releases_slot_with_unlimited_daily_launches(self):
        self.policy.update(max_active_sessions=1, max_launches_per_24h=None)
        def fail(value, policy):
            self.calls.append(value["launch_request_id"])
            return subprocess.CompletedProcess([], 0, json.dumps(host.result_for(value, "FAILED_PRESTART", "credentials unavailable; no create request sent")))
        result = host.launch(packet(), self.policy, self.ledger, fail, quiescence=lambda lane, policy: [])
        self.assertEqual(result["outcome"], "FAILED_PRESTART")
        result = host.launch(packet(1), self.policy, self.ledger, self.invoke)
        self.assertEqual(result["outcome"], "CONFIRMED")
        self.assertEqual(self.calls, ["request-0", "request-1"])

    def test_a_prestart_failure_from_a_busy_lane_keeps_the_slot(self):
        # Astra A3 finding 1: the builder UID can write the adapter's result, so a live
        # builder could report FAILED_PRESTART and keep running.
        self.policy.update(max_active_sessions=1, max_launches_per_24h=None)
        def fake_failure(value, policy):
            return subprocess.CompletedProcess([], 0, json.dumps(host.result_for(value, "FAILED_PRESTART", "forged")))
        censuses = (lambda lane, policy: [4242], lambda lane, policy: (_ for _ in ()).throw(host.HostError("x")))
        with patch.object(host, "QUIESCENCE_GRACE", 0):
            for index, census in enumerate(censuses):
                with self.subTest(index=index):
                    result = host.launch(packet(2 * index), self.policy, self.ledger, fake_failure, quiescence=census)
                    self.assertEqual(result["outcome"], "UNKNOWN")
                    self.assertIn("not quiescent", result["reason"])
                    self.assertEqual(host.launch(packet(2 * index + 1), self.policy, self.ledger, self.invoke)["outcome"],
                                     "FAILED_PRESTART")  # the slot is still held
                    self.ledger.reconcile(f"request-{2 * index}", None, "https://github.com/owner/ops/issues/13",
                                          no_session=True, sender_fenced=True)

    def test_lane_empty_waits_briefly_for_an_exiting_supervisor(self):
        answers = iter([[77], [77], []])
        times = iter([0.0, 1.0, 2.0, 3.0])
        self.assertTrue(host.lane_empty("DEVIN", self.policy, lambda lane, policy: next(answers),
                                        clock=lambda: next(times), sleep=lambda _: None))
        times = iter([0.0, 5.0, 11.0])
        self.assertFalse(host.lane_empty("DEVIN", self.policy, lambda lane, policy: [77],
                                         clock=lambda: next(times), sleep=lambda _: None))

    def test_unlimited_daily_launch_policy_accepts_null_and_rejects_zero(self):
        self.policy["max_launches_per_24h"] = None
        host.validate_policy(self.policy)
        self.policy["max_launches_per_24h"] = 0
        with self.assertRaisesRegex(host.HostError, "null .* or a positive integer"):
            host.validate_policy(self.policy)

    def test_reconciliation_requires_session_match_and_keeps_request_deduplication(self):
        self.policy["max_active_sessions"] = 1
        original = host.launch(packet(), self.policy, self.ledger, self.invoke)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", "wrong", "https://github.com/owner/ops/issues/13")
        self.ledger.reconcile("request-0", original["session_id"], "https://github.com/owner/ops/issues/13")
        self.assertEqual(self.state(), "RECONCILED")
        # A released request is never replayed: its stored CONFIRMED names a session that is gone.
        self.assertReleasedRefusal(host.launch(packet(), self.policy, self.ledger, self.invoke))
        self.assertEqual(host.launch(packet(5, task_id="T-0"), self.policy, self.ledger, self.invoke)["outcome"], "CONFIRMED")
        self.assertEqual(len(self.calls), 2)

    def test_reconciliation_cannot_release_reservation_while_sender_is_in_flight(self):
        def still_sending(value, policy):
            self.assertEqual(self.state(), "SUBMITTING")
            with self.assertRaisesRegex(host.HostError, "still in flight"):
                self.ledger.reconcile("request-0", "session-request-0", "https://github.com/owner/ops/issues/13")
            self.assertEqual(self.state(), "SUBMITTING")
            return self.invoke(value, policy)
        result = host.launch(packet(), self.policy, self.ledger, still_sending)
        self.ledger.reconcile("request-0", result["session_id"], "https://github.com/owner/ops/issues/13")
        self.assertEqual(self.state(), "RECONCILED")

    def test_unknown_release_requires_operator_evidence_but_never_resends_request(self):
        self.ledger.reserve(packet(), self.policy)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", "", "https://github.com/owner/ops/issues/13")
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", "operator-found-session", "PENDING")
        self.ledger.reconcile("request-0", "operator-found-session", "https://github.com/owner/ops/issues/13")
        self.assertReleasedRefusal(host.launch(packet(), self.policy, self.ledger, self.invoke))
        self.assertEqual(self.calls, [])

    def test_adapter_inherits_fence_after_helper_descriptor_closes(self):
        self.ledger.reserve(packet(), self.policy)
        with self.ledger.inflight_lock() as fd:
            child = subprocess.Popen([sys.executable, "-I", "-c",
                                      "import sys; print('ready', flush=True); sys.stdin.read()"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, pass_fds=(fd,))
            self.addCleanup(lambda: child.kill() if child.poll() is None else None)
            self.assertEqual(child.stdout.readline(), "ready\n")
        # Closing the helper's descriptor models its death; the sending child lives.
        with self.assertRaisesRegex(host.HostError, "still in flight"):
            self.ledger.reconcile("request-0", "verified-session", "https://github.com/owner/ops/issues/13")
        child.communicate(timeout=5)
        self.ledger.reconcile("request-0", "verified-session", "https://github.com/owner/ops/issues/13")
        self.assertEqual(self.state(), "RECONCILED")

    def test_missing_or_corrupt_ledger_never_invokes_provider(self):
        for name, content in (("absent", None), ("corrupt", b"not a database")):
            path = Path(self.temp.name) / name
            if content is not None:
                path.write_bytes(content)
            with self.subTest(name=name), self.assertRaises((host.HostError, sqlite3.Error)):
                host.launch(packet(), self.policy, host.Ledger(path), self.invoke)
        self.assertEqual(self.calls, [])
        self.assertFalse((Path(self.temp.name) / "absent").exists())
        with self.assertRaises(FileExistsError):
            self.ledger.initialize()

    def test_adapter_receives_protected_packet_clean_environment_and_root_cwd(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "never-forward", "GITHUB_OUTPUT": "never-forward", "PYTHONPATH": "/untrusted"}):
            def run(args, **kwargs):
                self.assertEqual(args[0], host.WRAPPERS["DEVIN"])
                path = Path(args[1])
                self.assertEqual(path.parent, self.path.parent)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text()), packet())
                self.assertEqual(kwargs["cwd"], "/")
                inherited_fd = kwargs["pass_fds"][0]
                self.assertEqual(kwargs["env"], {**host.CLEAN_ENV, "ASTRA_HOST_INFLIGHT_FD": str(inherited_fd)})
                self.assertEqual(os.fstat(inherited_fd).st_uid, os.geteuid())
                self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
                return self.invoke(packet(), self.policy)
            with patch.object(host.subprocess, "run", side_effect=run):
                host.launch(packet(), self.policy, self.ledger)
        self.assertEqual(list(self.path.parent.glob("packet-*")), [])

    def test_preflight_only_calls_preflight_adapter_without_creating_reservation(self):
        report = {"status": "PASS", "builder_id": "DEVIN", "execution_mode": "REMOTE_SESSION",
                  "parallel_safe": True, "launch_contract_version": 2}
        with patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report))) as invoke:
            result = host.preflight("DEVIN", self.policy, self.ledger)
        self.assertEqual(invoke.call_args.args[0], [host.WRAPPERS["DEVIN"], "--preflight"])
        self.assertEqual(result["host_admission"], "ENFORCED")
        self.assertEqual(result["boundary_evidence_pointer"], self.policy["boundary_evidence_pointer"])
        db = self.ledger.connect()
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM launches").fetchone()[0], 0)
        finally:
            db.close()
        del report["launch_contract_version"]
        with patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report))):
            with self.assertRaises(host.HostError):
                host.preflight("DEVIN", self.policy, self.ledger)

    def test_policy_and_unix_identity_boundaries_fail_closed(self):
        host.validate_policy(self.policy)
        for changes in ({"control_uid": self.policy["runner_uid"]}, {"max_active_sessions": True},
                        {"enabled_builders": []}, {"boundary_evidence_pointer": "PENDING"},
                        {"wrapper_paths": {"DEVIN": "/tmp/attacker"}}):
            with self.subTest(changes=changes), self.assertRaises(host.HostError):
                host.validate_policy({**self.policy, **changes})
        with patch.object(host.os, "getuid", return_value=1010), patch.object(host.os, "geteuid", return_value=1010):
            with patch.dict(os.environ, {"SUDO_UID": "1020"}):
                host.authorize_identity(self.policy, "launch")
                host.authorize_identity(self.policy, "preflight")
                for command in ("init", "reconcile"):
                    with self.assertRaises(host.HostError):
                        host.authorize_identity(self.policy, command)
            with patch.dict(os.environ, {"SUDO_UID": "1030"}), self.assertRaises(host.HostError):
                host.authorize_identity(self.policy, "launch")
            with patch.dict(os.environ, {"SUDO_UID": "0"}):
                host.authorize_identity(self.policy, "reconcile")
        with patch.object(host.os, "getuid", return_value=1020), self.assertRaises(host.HostError):
            host.authorize_identity(self.policy, "launch")

    def test_status_reports_fencing_state_without_mutating_the_ledger(self):
        self.policy["max_active_sessions"] = 1
        host.launch(packet(), self.policy, self.ledger, self.invoke)
        rejected = host.launch(packet(1), self.policy, self.ledger, self.invoke)
        self.assertEqual(rejected["outcome"], "FAILED_PRESTART")
        self.assertEqual(self.ledger.status("request-1"),
                         {"status": "FOUND", "launch_request_id": "request-1", "state": "FAILED_PRESTART",
                          "role": "WRITER", "lane": "DEVIN", "repository": "owner/repo1", "task": "T-1",
                          "attempt_id": 1, "task_revision": "r1", "owner_lane": None, "reserved_ts": self.ledger.clock(),
                          "reserved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(self.ledger.clock())))})
        self.assertEqual(self.ledger.status("request-0")["state"], "CONFIRMED")
        self.assertEqual(self.ledger.status("never-sent"),
                         {"status": "NOT_FOUND", "launch_request_id": "never-sent", "state": None})
        self.ledger.reconcile("request-0", "session-request-0", "https://github.com/owner/ops/issues/13")
        self.assertEqual(self.ledger.status("request-0")["state"], "RECONCILED")
        before = self.path.read_bytes()
        for request in ("request-0", "request-1", "never-sent"):
            self.ledger.status(request)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, ["request-0"])
        for bad in ("", " ", "x\0y", "x" * 4097, None, 7):
            with self.subTest(bad=bad), self.assertRaises(host.HostError):
                self.ledger.status(bad)

    def test_never_admitted_request_is_fenced_by_a_tombstone(self):
        evidence = "https://github.com/owner/ops/issues/14"
        for options in ({}, {"no_session": True}, {"no_session": True, "sender_fenced": False}):
            with self.subTest(options=options), self.assertRaises(host.HostError):
                self.ledger.reconcile("lost-request", None, evidence, never_admitted=True, **options)
        self.assertIsNone(self.ledger.status("lost-request")["state"])
        with self.ledger.inflight_lock():
            with self.assertRaisesRegex(host.HostError, "still in flight"):
                self.ledger.reconcile("lost-request", None, evidence, no_session=True,
                                      sender_fenced=True, never_admitted=True)
        result = self.ledger.reconcile("lost-request", None, evidence, no_session=True,
                                       sender_fenced=True, never_admitted=True)
        self.assertEqual(result["resolution"], "NEVER_ADMITTED")
        self.assertEqual(self.ledger.status("lost-request")["state"], "RECONCILED")
        # A late send of the fenced request is refused before any adapter call.
        with self.assertRaises(host.HostError):
            host.launch(packet(launch_request_id="lost-request"), self.policy, self.ledger, self.invoke)
        self.assertEqual(self.calls, [])
        # The tombstone neither blocks the task's new attempt nor counts as an admitted launch.
        self.assertEqual(host.launch(packet(), self.policy, self.ledger, self.invoke)["outcome"], "CONFIRMED")
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT sum(admitted) FROM launches").fetchone()[0], 1)
        finally:
            db.close()
        with self.assertRaisesRegex(host.HostError, "in the ledger"):
            self.ledger.reconcile("request-0", None, evidence, no_session=True,
                                  sender_fenced=True, never_admitted=True)
        self.assertEqual(self.state(), "CONFIRMED")

    def test_never_admitted_cli_requires_operator_flags(self):
        base = ["reconcile", "--launch-request-id", "lost-request", "--evidence",
                "https://github.com/owner/ops/issues/14", "--no-session"]
        with patch.object(host, "load_host_policy", return_value=self.policy), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout, \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(host.main(base + ["--never-admitted"]), 2)
            self.assertEqual(host.main(base + ["--sender-fenced", "--never-admitted"]), 0)
        self.assertEqual(json.loads(stdout.getvalue().splitlines()[-1])["resolution"], "NEVER_ADMITTED")

    def test_status_is_open_to_the_runner_but_not_to_builders(self):
        with patch.object(host.os, "getuid", return_value=1010), patch.object(host.os, "geteuid", return_value=1010):
            with patch.dict(os.environ, {"SUDO_UID": "1020"}):
                host.authorize_identity(self.policy, "status")
            for caller in ("1030", "1040", "1050", "1010"):
                with self.subTest(caller=caller), patch.dict(os.environ, {"SUDO_UID": caller}), \
                     self.assertRaises(host.HostError):
                    host.authorize_identity(self.policy, "status")

    def test_status_command_prints_one_canonical_object(self):
        with patch.object(host, "load_host_policy", return_value=self.policy), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(host.main(["status", "--launch-request-id", "never-sent"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()),
                         {"status": "NOT_FOUND", "launch_request_id": "never-sent", "state": None})

    def test_unsafe_files_and_duplicate_json_keys_are_rejected(self):
        path = Path(self.temp.name) / "protected"
        path.write_text("x")
        path.chmod(0o600)
        host.protected_leaf(path, os.getuid(), mode=0o600)
        path.chmod(0o666)
        with self.assertRaises(host.HostError):
            host.protected_leaf(path, os.getuid())
        path.chmod(0o600)
        link = path.with_name("symlink")
        link.symlink_to(path)
        with self.assertRaises(host.HostError):
            host.protected_leaf(link, os.getuid())
        os.link(path, path.with_name("hardlink"))
        with self.assertRaises(host.HostError):
            host.protected_leaf(path, os.getuid())
        with self.assertRaises(host.HostError):
            host.parse_json('{"outcome":"CONFIRMED","outcome":"FAILED_PRESTART"}')


LANES = ("DEVIN", "GROK_BUILD", "GLM", "CURSOR")
HEAD = "a" * 40
EVIDENCE = "https://github.com/owner/repo0/pull/7#issuecomment-1"


def writer(index=0, lane="DEVIN", **changes):
    value = packet(index, schema_version=2, role="WRITER", builder_id=lane, owner_lane=lane,
                   launch_request_id=f"{index:024x}")
    value.update(changes)
    return value


def reviewer(index=0, lane="GROK_BUILD", owner="DEVIN", review=1, **changes):
    value = packet(index, schema_version=2, role="REVIEWER", builder_id=lane, owner_lane=owner,
                   review_request_id=f"{review:024x}", head_sha=HEAD,
                   launch_request_id=f"{review + 0x100:024x}")
    value.update(changes)
    return value


class ProgramModeHostTests(unittest.TestCase):
    """Ledger v2: role reservations, one session per lane, verified reap, materialization."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "admission.sqlite"
        self.now = 1_000_000
        self.ledger = host.Ledger(self.path, clock=lambda: self.now)
        self.ledger.initialize()
        self.policy = {
            "control_uid": 1010, "runner_uid": 1020,
            "builder_uids": {"DEVIN": 1030, "GROK_BUILD": 1040, "GLM": 1050, "CURSOR": 1060},
            "allowed_repositories": [f"owner/repo{i}" for i in range(5)],
            "enabled_builders": list(LANES), "max_active_sessions": 4,
            "max_launches_per_24h": None, "ledger_path": str(self.path),
            "wrapper_paths": dict(host.WRAPPERS),
            "boundary_evidence_pointer": "https://github.com/owner/ops/issues/12",
        }

    def confirm(self, value, policy):
        return subprocess.CompletedProcess([], 0, json.dumps(
            host.result_for(value, "CONFIRMED", session_id="session-" + value["launch_request_id"])))

    def launch(self, value):
        return host.launch(value, self.policy, self.ledger, self.confirm)

    def reap(self, value, pids=()):
        return self.ledger.reap(value["launch_request_id"], EVIDENCE, self.policy,
                                quiescence=lambda lane, policy: list(pids))

    def launch_and_reap(self, value):
        self.launch(value)
        return self.reap(value)

    def test_f1_four_busy_writers_release_before_merge_so_reviews_can_run(self):
        writers = [writer(i, lane) for i, lane in enumerate(LANES)]
        self.assertTrue(all(self.launch(w)["outcome"] == "CONFIRMED" for w in writers))
        blocked = self.launch(reviewer(0, "GROK_BUILD", "DEVIN"))
        self.assertEqual(blocked["outcome"], "FAILED_PRESTART")
        for w in writers:  # each writer posted its deliverable and its lane UID is quiescent
            self.assertEqual(self.reap(w)["resolution"], "SESSION_TERMINAL_VERIFIED")
        for index, (owner, lane) in enumerate(zip(LANES, LANES[1:] + LANES[:1])):
            review = reviewer(index, lane, owner, review=10 + index)
            self.assertEqual(self.launch(review)["outcome"], "CONFIRMED", (owner, lane))

    def test_f2_review_of_same_task_is_admitted_once_writer_released(self):
        w = writer()
        self.launch(w)
        early = self.launch(reviewer())
        self.assertEqual(early["reason"], "task writer session is still active or unresolved; review waits")
        self.reap(w)
        self.assertEqual(self.launch(reviewer(review=2))["outcome"], "CONFIRMED")
        second = self.launch(reviewer(lane="GLM", review=3))  # A2: second reviewer on another lane
        self.assertEqual(second["outcome"], "CONFIRMED")
        resume = self.launch(writer(attempt_id=2, launch_request_id="f" * 24))
        self.assertEqual(resume["reason"], "task has an active or unresolved review; the writer waits")

    def test_reviewer_rules_are_enforced_by_packet_and_ledger(self):
        with self.assertRaises(host.HostError):
            host.validate_packet(reviewer(lane="DEVIN", owner="DEVIN"), self.policy)
        with self.assertRaises(host.HostError):
            host.validate_packet(reviewer(head_sha="short"), self.policy)
        with self.assertRaises(host.HostError):
            host.validate_packet(writer(owner_lane="GLM"), self.policy)
        self.launch_and_reap(writer())
        self.launch(reviewer(review=5))
        duplicate = self.launch(reviewer(lane="GLM", review=5, launch_request_id="e" * 24))
        self.assertEqual(duplicate["reason"], "review request already has an active or unresolved session")

    def test_lane_is_exclusive_across_tasks_and_repositories(self):
        self.launch(writer(0, "DEVIN"))
        busy = self.launch(writer(1, "DEVIN"))
        self.assertEqual(busy["reason"], "lane busy")
        self.assertEqual(self.launch(writer(2, "GLM"))["outcome"], "CONFIRMED")
        board = self.ledger.lanes(self.policy)
        self.assertEqual(board["active_total"], 2)
        self.assertEqual({lane["lane"]: len(lane["active"]) for lane in board["lanes"]},
                         {"DEVIN": 1, "GROK_BUILD": 0, "GLM": 1, "CURSOR": 0})

    def test_reap_requires_quiescent_lane_and_is_idempotent(self):
        w = writer()
        self.launch(w)
        with self.assertRaisesRegex(host.HostError, "live process"):
            self.reap(w, pids=[4242])
        with self.assertRaises(host.HostError):
            self.ledger.reap(w["launch_request_id"], "PENDING", self.policy, quiescence=lambda *_: [])
        first = self.reap(w)
        self.assertFalse(first["repeated"])
        # A lost RELEASED projection is repaired from the stored result; the lane is not re-checked.
        again = self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy,
                                 quiescence=lambda *_: self.fail("must not re-check"))
        self.assertEqual((again["resolution"], again["repeated"]), ("SESSION_TERMINAL_VERIFIED", True))
        self.assertEqual(self.ledger.status(w["launch_request_id"])["resolution"], "SESSION_TERMINAL_VERIFIED")

    def test_reap_never_touches_unknown_or_operator_reconciled_rows(self):
        unknown = writer(1, "GLM")
        host.launch(unknown, self.policy, self.ledger,
                    lambda value, policy: subprocess.CompletedProcess([], 1, "crash"))
        with self.assertRaisesRegex(host.HostError, "operator-only"):
            self.reap(unknown)
        confirmed = writer(2, "DEVIN")
        self.launch(confirmed)
        self.ledger.reconcile(confirmed["launch_request_id"], "session-" + confirmed["launch_request_id"], EVIDENCE)
        with self.assertRaisesRegex(host.HostError, "operator"):
            self.reap(confirmed)

    def test_lane_quiescence_scans_proc_and_refuses_hidden_processes(self):
        proc = Path(self.temp.name) / "proc"
        for pid, uid in ((10, 1030), (11, 1040), (12, 0)):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (proc / "self").mkdir()
        self.assertEqual(host.live_processes(1030, str(proc)), [10])
        self.assertEqual(host.live_processes(1050, str(proc)), [])
        mountinfo = Path(self.temp.name) / "mountinfo"
        mountinfo.write_text("22 1 0:5 / /proc rw,nosuid shared:13 - proc proc rw\n")
        self.assertFalse(host.proc_hides_processes(str(mountinfo)))
        mountinfo.write_text("22 1 0:5 / /proc rw,nosuid shared:13 - proc proc rw,hidepid=2\n")
        self.assertTrue(host.proc_hides_processes(str(mountinfo)))
        self.assertTrue(host.proc_hides_processes(str(Path(self.temp.name) / "missing")))
        # A hidepid procfs mounted over a plain one hides processes: the topmost mount wins.
        mountinfo.write_text("22 1 0:5 / /proc rw shared:13 - proc proc rw\n"
                             "40 22 0:9 / /proc rw shared:20 - proc proc rw,hidepid=invisible\n")
        self.assertTrue(host.proc_hides_processes(str(mountinfo)))

    def test_unreadable_or_unparseable_status_fails_closed(self):
        proc = Path(self.temp.name) / "proc2"
        (proc / "10").mkdir(parents=True)
        (proc / "10" / "status").write_text("Name:\tx\n")  # no Uid line
        with self.assertRaisesRegex(host.HostError, "unverifiable"):
            host.live_processes(1030, str(proc))
        (proc / "10" / "status").write_text("Name:\tx\nUid:\t1030\t1030\t1030\t1030\n")
        with patch.object(host.Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(host.HostError, "unverifiable"):
                host.live_processes(1030, str(proc))

    def test_materialize_never_recreates_an_unresolved_request(self):
        args = ("zari", "n010", "owner/repo0", "b" * 40, self.policy)
        first = self.ledger.materialize_begin(*args)
        self.assertEqual(first["decision"], "CREATE_ALLOWED")
        # A second coordinator run while the create is in flight must not create again.
        self.assertEqual(self.ledger.materialize_begin(*args)["decision"], "UNRESOLVED")
        self.ledger.materialize_finish("zari", "n010", first["request"], "UNKNOWN", None)
        self.assertEqual(self.ledger.materialize_begin(*args)["decision"], "UNRESOLVED")
        # A later lookup found the issue carrying the same request id.
        self.ledger.materialize_finish("zari", "n010", first["request"], "CREATED", 33)
        created = self.ledger.materialize_begin(*args)
        self.assertEqual((created["decision"], created["issue"]), ("CREATED", 33))
        self.assertTrue(self.ledger.materialize_finish("zari", "n010", first["request"], "CREATED", 33)["repeated"])
        with self.assertRaises(host.HostError):
            self.ledger.materialize_finish("zari", "n010", first["request"], "CREATED", 34)

    def test_late_response_of_sealed_request_cannot_replace_current_issue(self):
        args = ("zari", "n011", "owner/repo0", "c" * 40, self.policy)
        old = self.ledger.materialize_begin(*args)
        self.ledger.materialize_finish("zari", "n011", old["request"], "UNKNOWN", None)
        with self.assertRaises(host.HostError):
            self.ledger.materialize_resolve("zari", "n011", old["request"], "PENDING")
        sealed = self.ledger.materialize_resolve("zari", "n011", old["request"], EVIDENCE)
        self.assertEqual(sealed["sealed"], [old["request"]])
        new = self.ledger.materialize_begin(*args)
        self.assertEqual((new["decision"], new["attempt"]), ("CREATE_ALLOWED", 2))
        self.assertNotEqual(new["request"], old["request"])
        self.ledger.materialize_finish("zari", "n011", new["request"], "CREATED", 40)
        with self.assertRaisesRegex(host.HostError, "sealed or stale"):
            self.ledger.materialize_finish("zari", "n011", old["request"], "CREATED", 39)
        status = self.ledger.materialize_status("zari", "n011")
        self.assertEqual((status["status"], status["issue"], status["sealed"]), ("CREATED", 40, [old["request"]]))

    def test_plan_commit_advances_only_by_compare_and_swap(self):
        self.ledger.materialize_begin("zari", "n2", "owner/repo0", "b" * 40, self.policy)
        self.assertEqual(self.ledger.materialize_plan("zari", "n2", "b" * 40, "c" * 40)["plan_commit"], "c" * 40)
        with self.assertRaisesRegex(host.HostError, "STALE_PLAN"):
            self.ledger.materialize_plan("zari", "n2", "b" * 40, "d" * 40)  # stale writer
        self.assertEqual(self.ledger.materialize_status("zari", "n2")["plan_commit"], "c" * 40)

    def test_materialize_validates_inputs(self):
        for bad in (("zari prog", "n1", "owner/repo0", "b" * 40), ("zari", "n1", "other/repo", "b" * 40),
                    ("zari", "n1", "owner/repo0", "main")):
            with self.subTest(bad=bad), self.assertRaises(host.HostError):
                self.ledger.materialize_begin(*bad, self.policy)
        self.ledger.materialize_begin("zari", "n1", "owner/repo0", "b" * 40, self.policy)
        with self.assertRaisesRegex(host.HostError, "different repository"):
            self.ledger.materialize_begin("zari", "n1", "owner/repo1", "b" * 40, self.policy)

    def test_operator_only_commands(self):
        with patch.object(host.os, "getuid", return_value=1010), patch.object(host.os, "geteuid", return_value=1010):
            with patch.dict(os.environ, {"SUDO_UID": "1020"}):
                for command in ("reap", "materialize-begin", "materialize-finish", "materialize-status", "status"):
                    host.authorize_identity(self.policy, command)
                for command in ("migrate", "materialize-resolve", "reconcile", "init"):
                    with self.subTest(command=command), self.assertRaises(host.HostError):
                        host.authorize_identity(self.policy, command)
            with patch.dict(os.environ, {"SUDO_UID": "1060"}), self.assertRaises(host.HostError):
                host.authorize_identity(self.policy, "reap")

    # ------------------------------------------------------------ signed, write-once pins

    def signed_review(self, value, verdict="PASS", depth="A1", change="NO", nonce=None, required="A1", **fields):
        fields = {"review": value["review_request_id"], "head": value["head_sha"], **fields}
        mac = host.pin_mac(nonce or value["review_nonce"],
                           ("ASTRA_REVIEW_V1", fields["review"], fields["head"], verdict, depth, required, change))
        return (f"ASTRA_REVIEW_V1 review={fields['review']} head={fields['head']} verdict={verdict} "
                f"depth={depth} required={required} contract_change={change} mac={mac}")

    def signed_delivery(self, value, pr=7, head=HEAD, nonce=None):
        mac = host.pin_mac(nonce or value["delivery_nonce"], ("ASTRA_DELIVERY_V1", value["launch_request_id"],
                                                              str(pr), head))
        return f"ASTRA_DELIVERY_V1 pr={pr} head={head} mac={mac}"

    def reaped_writer(self):
        w = writer(delivery_nonce="1" * 32)
        self.launch(w)
        return w

    def test_reap_pins_a_verified_delivery_and_review(self):
        w = self.reaped_writer()
        done = self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_delivery(w),
                                quiescence=lambda lane, policy: [])
        self.assertEqual(done["pin"], {"kind": "DELIVERY", "pr": 7, "head": HEAD})
        r = reviewer(review_nonce="2" * 32)
        self.launch(r)
        verdict = self.ledger.reap(r["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_review(r, "FAIL"),
                                   quiescence=lambda lane, policy: [])
        self.assertEqual((verdict["pin"]["kind"], verdict["pin"]["verdict"]), ("REVIEW", "FAIL"))
        rows = self.ledger.task_status("owner/repo0", "T-0")["rows"]
        self.assertEqual([(row["role"], row["pin"]["kind"]) for row in rows], [("WRITER", "DELIVERY"),
                                                                               ("REVIEWER", "REVIEW")])
        self.assertEqual(rows[1]["head_sha"], HEAD)
        # Neither status nor task-status ever discloses a signing key.
        for text in (json.dumps(rows), json.dumps(self.ledger.status(r["launch_request_id"]))):
            self.assertNotIn("1" * 32, text)
            self.assertNotIn("2" * 32, text)

    def test_forged_or_mismatched_pins_are_refused_before_release(self):
        w = self.reaped_writer()
        r = reviewer(review_nonce="2" * 32)
        cases = [
            (w, self.signed_delivery(w, nonce="9" * 32), "MAC"),                 # another session's key
            (w, self.signed_delivery(w).replace("pr=7", "pr=8"), "MAC"),          # edited after signing
            (w, "ASTRA_DELIVERY_V1 pr=7 head=" + HEAD, "exactly one ASTRA_DELIVERY_V1"),
            (w, self.signed_review(reviewer(review_nonce="2" * 32)), "writer pin"),
        ]
        for value, pin, expected in cases:
            with self.subTest(expected=expected, pin=pin[:40]), self.assertRaisesRegex(host.HostError, expected):
                self.ledger.reap(value["launch_request_id"], EVIDENCE, self.policy, pin=pin,
                                 quiescence=lambda lane, policy: [])
        self.assertEqual(self.ledger.status(w["launch_request_id"])["state"], "CONFIRMED")
        self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_delivery(w),
                         quiescence=lambda lane, policy: [])
        self.launch(r)
        for pin, expected in ((self.signed_review(r).replace("verdict=PASS", "verdict=FAIL"), "MAC"),
                              (self.signed_review(r, head="b" * 40), "review request and head"),
                              (self.signed_review(r, review="e" * 24), "review request and head")):
            with self.subTest(expected=expected), self.assertRaisesRegex(host.HostError, expected):
                self.ledger.reap(r["launch_request_id"], EVIDENCE, self.policy, pin=pin,
                                 quiescence=lambda lane, policy: [])
        self.assertEqual(self.ledger.status(r["launch_request_id"])["state"], "CONFIRMED")

    def signed_blocker(self, value, kind="BLOCKED", nonce=None, launch=None):
        launch = launch or value["launch_request_id"]
        key = nonce or value.get("review_nonce") or value["delivery_nonce"]
        return f"ASTRA_BLOCKED_V1 kind={kind} launch={launch} mac={host.pin_mac(key, ('ASTRA_BLOCKED_V1', launch, kind))}"

    def test_a_signed_blocker_releases_either_role_without_a_result(self):
        w = self.reaped_writer()
        done = self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_blocker(w, "STALLED"),
                                quiescence=lambda lane, policy: [])
        self.assertEqual(done["pin"], {"kind": "BLOCKER", "blocker": "STALLED"})
        r = reviewer(review_nonce="2" * 32)
        self.launch(r)
        done = self.ledger.reap(r["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_blocker(r),
                                quiescence=lambda lane, policy: [])
        self.assertEqual(done["pin"]["kind"], "BLOCKER")

    def test_blocker_pins_are_bound_to_the_session(self):
        w = self.reaped_writer()
        other = writer(1, "GROK_BUILD", delivery_nonce="4" * 32)
        cases = [
            (self.signed_blocker(w, nonce="9" * 32), "MAC"),                          # another session's key
            (self.signed_blocker(w, launch=other["launch_request_id"]), "this session's launch"),
            (self.signed_blocker(w).replace("kind=BLOCKED", "kind=STALLED"), "MAC"),   # edited after signing
            (None, "signed line is required"),                                         # unsigned text pins nothing
        ]
        for pin, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(host.HostError, expected):
                self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy, pin=pin,
                                 quiescence=lambda lane, policy: [])
        self.assertEqual(self.ledger.status(w["launch_request_id"])["state"], "CONFIRMED")

    def test_one_pr_is_the_delivery_of_one_task(self):
        first = self.reaped_writer()
        self.ledger.reap(first["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_delivery(first),
                         quiescence=lambda lane, policy: [])
        second = writer(1, "GROK_BUILD", repository="owner/repo0", task_id="T-other", delivery_nonce="3" * 32)
        self.launch(second)
        with self.assertRaisesRegex(host.HostError, "already the delivery of task T-0"):
            self.ledger.reap(second["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_delivery(second),
                             quiescence=lambda lane, policy: [])
        done = self.ledger.reap(second["launch_request_id"], EVIDENCE, self.policy,
                                pin=self.signed_delivery(second, pr=8), quiescence=lambda lane, policy: [])
        self.assertEqual(done["pin"]["pr"], 8)

    def test_a_packet_without_a_nonce_cannot_pin(self):
        w = writer()  # no delivery_nonce
        self.launch(w)
        with self.assertRaisesRegex(host.HostError, "no signing nonce"):
            self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy,
                             pin=self.signed_delivery(w, nonce="1" * 32), quiescence=lambda lane, policy: [])
        self.assertEqual(self.reap(w)["pin"], {"kind": "NONE"})  # released, with nothing pinned

    def test_reap_is_write_once(self):
        w = self.reaped_writer()
        pin = self.signed_delivery(w)
        self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy, pin=pin, quiescence=lambda lane, policy: [])
        again = self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy, pin=pin,
                                 quiescence=lambda lane, policy: [1])  # repeat never re-checks the lane
        self.assertEqual((again["repeated"], again["pin"]["pr"]), (True, 7))
        for evidence, other in ((EVIDENCE + "0", pin), (EVIDENCE, self.signed_delivery(w, pr=8)), (EVIDENCE, None)):
            with self.subTest(evidence=evidence, other=other), \
                 self.assertRaisesRegex(host.HostError, "write-once|signed line is required"):
                self.ledger.reap(w["launch_request_id"], evidence, self.policy, pin=other,
                                 quiescence=lambda lane, policy: [])

    def test_released_request_is_never_replayed(self):
        w = self.reaped_writer()
        self.ledger.reap(w["launch_request_id"], EVIDENCE, self.policy, pin=self.signed_delivery(w),
                         quiescence=lambda lane, policy: [])
        replay = self.launch(w)
        self.assertEqual((replay["outcome"], replay["session_id"]), ("FAILED_PRESTART", None))
        self.assertEqual(self.ledger.status(w["launch_request_id"])["state"], "RECONCILED")

    def test_task_status_validates_its_arguments(self):
        for repository, task in (("no-slash", "T"), ("o/r", ""), ("o/r", "x" * 257), ("o/r", "a\0b")):
            with self.subTest(repository=repository, task=task), self.assertRaises(host.HostError):
                self.ledger.task_status(repository, task)
        self.assertEqual(self.ledger.task_status("owner/repo0", "T-9")["rows"], [])

    def test_reap_and_task_status_commands(self):
        w = self.reaped_writer()
        document = json.dumps({"pin": self.signed_delivery(w)})
        with patch.object(host, "load_host_policy", return_value=self.policy), \
             patch.object(host, "lane_quiescence", return_value=[]), \
             patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(host.main(["reap", "--launch-request-id", w["launch_request_id"], "--evidence", EVIDENCE,
                                        "--pin-stdin"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["pin"]["kind"], "DELIVERY")
        with patch.object(host, "load_host_policy", return_value=self.policy), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(host.main(["task-status", "--repository", "owner/repo0", "--task", "T-0"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["rows"][0]["pin"]["head"], HEAD)
        with patch.object(host, "load_host_policy", return_value=self.policy), \
             patch("sys.stdin", io.StringIO('{"pin": "x", "extra": 1}')), patch("sys.stdout", new_callable=io.StringIO), \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(host.main(["reap", "--launch-request-id", w["launch_request_id"], "--evidence", EVIDENCE,
                                        "--pin-stdin"]), 2)

    # ------------------------------------------------------------ quiescence preconditions

    def test_pid_namespace_must_be_the_proc_namespace(self):
        status = Path(self.temp.name) / "status"
        for text, expected in (("NSpid:\t4242\n", True), ("NSpid:\t4242\t7\n", False), ("Name:\tx\n", False)):
            with self.subTest(text=text):
                status.write_text(text)
                self.assertIs(host.own_pid_namespace_is_procs(str(status)), expected)
        self.assertFalse(host.own_pid_namespace_is_procs(str(status) + ".missing"))

    def test_subordinate_id_ranges_block_quiescence(self):
        subuid = Path(self.temp.name) / "subuid"
        subuid.write_text("someone:100000:65536\n1030:200000:65536\n")
        self.assertTrue(host.has_subordinate_ids(1030, (str(subuid),)))
        self.assertFalse(host.has_subordinate_ids(1040, (str(subuid),)))
        self.assertFalse(host.has_subordinate_ids(1040, (str(subuid) + ".missing",)))
        with patch.object(host, "proc_hides_processes", return_value=False), \
             patch.object(host, "own_pid_namespace_is_procs", return_value=True), \
             patch.object(host, "has_subordinate_ids", return_value=True), \
             self.assertRaisesRegex(host.HostError, "subordinate"):
            host.lane_quiescence("DEVIN", self.policy)
        with patch.object(host, "proc_hides_processes", return_value=False), \
             patch.object(host, "own_pid_namespace_is_procs", return_value=False), \
             self.assertRaisesRegex(host.HostError, "PID namespace"):
            host.lane_quiescence("DEVIN", self.policy)

    def test_lane_quiescence_combines_the_outside_scan_and_the_frozen_census(self):
        with patch.object(host, "proc_hides_processes", return_value=False), \
             patch.object(host, "own_pid_namespace_is_procs", return_value=True), \
             patch.object(host, "has_subordinate_ids", return_value=False), \
             patch.object(host, "live_processes", return_value=[]):
            self.assertEqual(host.lane_quiescence("DEVIN", self.policy, census=lambda lane: [91]), [91])
            self.assertEqual(host.lane_quiescence("DEVIN", self.policy, census=lambda lane: []), [])

    def test_lane_census_runs_through_the_wrapper_and_fails_closed(self):
        cases = [(0, {"status": "OK", "live": [5]}, [5]), (0, {"status": "OK", "live": []}, [])]
        for code, report, expected in cases:
            with patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], code, json.dumps(report))) as run:
                self.assertEqual(host.lane_census("GLM"), expected)
                self.assertEqual(run.call_args.args[0], [host.WRAPPERS["GLM"], "--quiescence"])
        for code, report in ((2, {"status": "OK", "live": []}), (0, {"status": "FAIL"}), (0, {"status": "OK", "live": ["x"]}),
                             (0, {"status": "OK"})):
            with self.subTest(report=report), \
                 patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], code, json.dumps(report))), \
                 self.assertRaises(host.HostError):
                host.lane_census("GLM")

    def test_status_lanes_command(self):
        self.launch(writer())
        with patch.object(host, "load_host_policy", return_value=self.policy), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(host.main(["status", "--lanes"]), 0)
        board = json.loads(stdout.getvalue())
        self.assertEqual(board["lanes"][0]["active"][0]["role"], "WRITER")


class MigrationTests(unittest.TestCase):
    V1 = """
        CREATE TABLE launches (
            request TEXT PRIMARY KEY, repository TEXT NOT NULL, task TEXT NOT NULL,
            packet TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
            ('SUBMITTING','CONFIRMED','UNKNOWN','FAILED_PRESTART','RECONCILED')),
            result TEXT NOT NULL, admitted INTEGER NOT NULL CHECK(admitted IN (0,1)),
            created REAL NOT NULL, evidence TEXT);
        CREATE UNIQUE INDEX one_active_task ON launches(repository, task)
            WHERE state IN ('SUBMITTING','CONFIRMED','UNKNOWN');
        PRAGMA user_version=1;
    """

    def make_v1(self, rows):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "admission.sqlite"
        db = sqlite3.connect(path)
        db.executescript(self.V1)
        for request, lane, state in rows:
            body = json.dumps({"builder_id": lane}) if lane else ""
            db.execute("INSERT INTO launches VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (request, "o/r", "T-" + request, body, state, "{}", 1, 1.0))
        db.commit()
        db.close()
        return path

    def test_migrates_in_place_with_backup_and_lane_backfill(self):
        path = self.make_v1([("r1", "DEVIN", "RECONCILED"), ("r2", "GLM", "FAILED_PRESTART"),
                             ("r3", "", "RECONCILED"), ("r4", "DEVIN", "CONFIRMED")])
        ledger = host.Ledger(path, clock=lambda: 5)
        with self.assertRaises(host.HostError):
            ledger.connect()  # v1 is refused until migrated
        result = ledger.migrate(2)
        self.assertEqual(result["status"], "MIGRATED")
        self.assertTrue(Path(result["backup"]).exists())
        db = ledger.connect()
        try:
            lanes = dict(db.execute("SELECT request, lane FROM launches").fetchall())
            roles = {row[0] for row in db.execute("SELECT role FROM launches")}
        finally:
            db.close()
        self.assertEqual(lanes, {"r1": "DEVIN", "r2": "GLM", "r3": "", "r4": "DEVIN"})
        self.assertEqual(roles, {"WRITER"})
        self.assertEqual(ledger.migrate(2)["status"], "ALREADY_V2")

    def test_conflicting_active_rows_roll_back_and_leave_v1_intact(self):
        path = self.make_v1([("r1", "DEVIN", "CONFIRMED"), ("r2", "DEVIN", "UNKNOWN")])
        ledger = host.Ledger(path, clock=lambda: 6)
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.migrate(2)
        db = sqlite3.connect(path)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            columns = [row[1] for row in db.execute("PRAGMA table_info(launches)")]
        finally:
            db.close()
        self.assertNotIn("lane", columns)


if __name__ == "__main__":
    unittest.main()

