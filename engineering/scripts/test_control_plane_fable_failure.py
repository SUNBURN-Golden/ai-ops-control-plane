"""Typed failure and durable host evidence regressions; no model or network calls."""
import copy
import io
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_plane_fable as fable
from test_control_plane_fable import (BLOCKED, SESSION, SUBSCRIPTION_WARNING_286,
                                    cli_output, verdict, mock_root_evidence)

NOW_MS = 1790757120000  # 2026-09-30 17:32 KST
RESET_SECONDS = 1790765400  # 2026-09-30 19:50 KST
EXECUTION = {"model_attempted": True, "process_state": "EXITED", "process_group_state": "ABSENT",
             "exit_code": 1, "timed_out": False, "interrupted": False}


def failed_events(*, machine_reset=False):
    info = {**BLOCKED, "status": "rejected" if machine_reset else "allowed"}
    if machine_reset:
        info.update(rateLimitType="five_hour", resetsAt=RESET_SECONDS)
    # The historical host report proves these fields, not a raw captured reset
    # schema. This is a reconstruction, deliberately not called a captured log.
    return [{"type": "system", "subtype": "init", "session_id": SESSION},
            {"type": "rate_limit_event", "session_id": SESSION, "uuid": "event-00000001",
             "rate_limit_info": info},
            {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
             "session_id": SESSION, "result": "five_hour limit; reset today 19:50 KST; seven_day tomorrow 10:00 KST"}]


def raw_of(events):
    return b"\n".join(json.dumps(event).encode() for event in events) + b"\n"


def parse(events, **kwargs):
    return fable.failure_of(raw_of(events), version=fable.SUPPORTED_CLAUDE_VERSION,
                            execution=EXECUTION, observed_at_epoch_ms=NOW_MS, **kwargs)


class FailureParserTests(unittest.TestCase):
    def test_subscription_warning_never_synthesizes_billing_block_or_quota_eligibility(self):
        events = failed_events(machine_reset=True)
        events[1]["rate_limit_info"] = dict(SUBSCRIPTION_WARNING_286)
        record = parse(events)
        self.assertEqual(record["error_code"], "MODEL_EXECUTION_FAILED")
        self.assertEqual(record["terminal_evidence"], "VERIFIED")
        self.assertEqual(record["extra_usage"], {"status": "allowed_warning", "isUsingOverage": False})
        self.assertIsNone(record["reset_at_epoch_ms"])
        self.assertIsNone(record["limit_type"])
        events.insert(1, {"type": "rate_limit_event", "uuid": "event-extra-00001", "session_id": SESSION,
                          "rate_limit_info": {**BLOCKED, "status": "rejected", "rateLimitType": "five_hour",
                                              "resetsAt": RESET_SECONDS}})
        self.assertNotEqual(parse(events)["error_code"], "MODEL_RATE_LIMIT")

    def test_wrapper_stop_requires_its_own_kill_and_proves_no_quota_on_any_cli_version(self):
        for code, raw in (("WRAPPER_TIMEOUT", b'{}\n'), ("STREAM_INVALID", b'not-json\n'),
                          ("OUTPUT_LIMIT", b'x' * 33)):
            with self.subTest(code=code), patch.object(fable, "MAX_OUTPUT", 32):
                guard = (fable.wrapper_stop(code, raw, timeout_seconds=fable.TIMEOUT_SECONDS)
                         if code == "WRAPPER_TIMEOUT" else fable.read_stream(io.BytesIO(raw))[1])
                execution = {**EXECUTION, "exit_code": -signal.SIGKILL, "guard_stop": guard,
                             "guard_kill_sent": True, "timed_out": code == "WRAPPER_TIMEOUT"}
                record = fable.failure_of(raw, version="2.1.286 (Claude Code)", execution=execution)
                self.assertEqual(record["error_code"], code)
                self.assertEqual(record["terminal_evidence"], "VERIFIED")
                self.assertEqual(record["adapter_profile"], fable.WRAPPER_STOP_PROFILE)
                self.assertIsNone(record["reset_at_epoch_ms"])
                self.assertIsNone(record["extra_usage"])
                for change in ({"guard_kill_sent": False}, {"guard_kill_sent": 1},
                               {"process_group_state": "UNKNOWN"}, {"exit_code": 1},
                               {"interrupted": True}, {"timed_out": code != "WRAPPER_TIMEOUT"},
                               {"guard_stop": {**guard, "raw_output_sha256": "0" * 64}}):
                    unproved = fable.failure_of(raw, version="2.1.286 (Claude Code)",
                                                execution={**execution, **change})
                    self.assertEqual(unproved["terminal_evidence"], "UNKNOWN")

    def test_1732_reconstruction_preserves_429_without_guessing_text_reset(self):
        record = parse(failed_events())
        self.assertEqual(record["error_code"], "MODEL_EXECUTION_FAILED")
        self.assertEqual(record["terminal_evidence"], "VERIFIED")
        self.assertEqual(record["api_error_status"], 429)
        self.assertIsNone(record["limit_type"])
        self.assertIsNone(record["reset_at_epoch_ms"])
        self.assertEqual(record["extra_usage"], {"overageStatus": "rejected", "isUsingOverage": False})

    def test_machine_epoch_window_is_typed_only_after_rejected_window(self):
        record = parse(failed_events(machine_reset=True))
        self.assertEqual(record["error_code"], "MODEL_RATE_LIMIT")
        self.assertEqual(record["reset_at_epoch_ms"], RESET_SECONDS * 1000)
        self.assertEqual(record["limit_type"], "five_hour")
        self.assertEqual(record["adapter_profile"], fable.FAILURE_PROFILE)

    def test_version_is_exact_and_unsupported_stays_unverified(self):
        for version in ("", "2.1.284 (Claude Code)", "2.1.286 (Claude Code)", "2.1.285"):
            with self.subTest(version=version):
                record = fable.failure_of(raw_of(failed_events(machine_reset=True)), version=version,
                                          execution=EXECUTION, observed_at_epoch_ms=NOW_MS)
                self.assertEqual(record["adapter_profile"], "UNSUPPORTED")
                self.assertNotEqual(record["terminal_evidence"], "VERIFIED")
                self.assertIsNone(record["reset_at_epoch_ms"])

    def test_is_error_requires_genuine_boolean(self):
        for value in ("true", "false", 1, 0, None, {}, []):
            events = failed_events(machine_reset=True)
            events[-1]["is_error"] = value
            with self.subTest(value=value):
                record = parse(events)
                self.assertEqual(record["error_code"], "RESULT_INVALID")
                self.assertNotEqual(record["terminal_evidence"], "VERIFIED")

    def test_status_and_epoch_require_genuine_integers(self):
        for field, value in (("api_error_status", "429"), ("api_error_status", True),
                             ("resetsAt", "1790765400"), ("resetsAt", 1790765400.0),
                             ("resetsAt", True), ("resetsAt", 1790765400000)):
            events = failed_events(machine_reset=True)
            target = events[-1] if field == "api_error_status" else events[1]["rate_limit_info"]
            target[field] = value
            with self.subTest(field=field, value=value):
                self.assertNotEqual(parse(events)["error_code"], "MODEL_RATE_LIMIT")

    def test_unknown_result_subtype_or_bad_result_field_is_not_a_supported_quota(self):
        for fields in ({"subtype": "new_unsupported_error"}, {"subtype": None}, {"result": {"status": "PASS"}}):
            events = failed_events(machine_reset=True)
            events[-1].update(fields)
            with self.subTest(fields=fields):
                self.assertNotEqual(parse(events)["error_code"], "MODEL_RATE_LIMIT")

    def test_unknown_window_expired_or_excessive_reset_stays_blocked(self):
        for window, reset in (("seven_day_opus", RESET_SECONDS), ("five_hour", NOW_MS // 1000),
                              ("five_hour", NOW_MS // 1000 + fable.RESET_HORIZON_SECONDS + 1)):
            events = failed_events(machine_reset=True)
            events[1]["rate_limit_info"].update(rateLimitType=window, resetsAt=reset)
            self.assertEqual(parse(events)["error_code"], "MODEL_EXECUTION_FAILED")

    def test_missing_mixed_duplicate_or_nonterminal_result_is_not_verified(self):
        base = failed_events(machine_reset=True)
        good_result = {**base[-1], "is_error": False, "structured_output": {"result": "PASS"}}
        variants = (base[:-1], base + [copy.deepcopy(base[-1])], base[:-1] + [good_result, base[-1]],
                    base + [{"type": "assistant", "session_id": SESSION}])
        for events in variants:
            with self.subTest(events=events):
                record = parse(events)
                self.assertEqual(record["error_code"], "RESULT_INVALID")
                self.assertNotEqual(record["terminal_evidence"], "VERIFIED")

    def test_failure_containing_structured_success_is_not_verified(self):
        events = failed_events(machine_reset=True)
        events[-1]["structured_output"] = {"result": "PASS"}
        self.assertNotEqual(parse(events)["terminal_evidence"], "VERIFIED")

    def test_cross_session_missing_uuid_or_duplicate_event_is_not_verified(self):
        for change in ("session", "uuid", "duplicate"):
            events = failed_events(machine_reset=True)
            if change == "session":
                events[1]["session_id"] = "other-session-00001"
            elif change == "uuid":
                del events[1]["uuid"]
            else:
                events.insert(2, copy.deepcopy(events[1]))
            with self.subTest(change=change):
                self.assertNotEqual(parse(events)["terminal_evidence"], "VERIFIED")

    def test_missing_overage_and_any_contradiction_blocks_policy(self):
        for change in ({"overageStatus": "allowed"}, {"isUsingOverage": True},
                       {"isUsingOverage": "false"}, {"overageStatus": None}):
            events = failed_events(machine_reset=True)
            events[1]["rate_limit_info"].update(change)
            with self.subTest(change=change):
                record = parse(events)
                expected = ("OVERAGE_NOT_BLOCKED" if change.get("isUsingOverage") is True
                            or change.get("overageStatus") == "allowed" else "OVERAGE_UNVERIFIED")
                self.assertEqual(record["error_code"], expected)
                self.assertIsNone(record["reset_at_epoch_ms"])
        self.assertEqual(parse([failed_events()[0], failed_events()[-1]])["error_code"], "OVERAGE_UNVERIFIED")

    def test_guard_stop_is_operator_settlement_evidence_and_never_quota_evidence(self):
        for change, expected in (({"overageStatus": None}, "OVERAGE_UNVERIFIED"),
                                 ({"isUsingOverage": True}, "OVERAGE_NOT_BLOCKED")):
            events = failed_events(machine_reset=True)
            events[1]["rate_limit_info"].update(change)
            # A success or quota result buffered after the guard is not read.
            raw, guard = fable.read_stream(io.BytesIO(raw_of(events)))
            execution = {**EXECUTION, "exit_code": -signal.SIGKILL, "guard_stop": guard}
            record = fable.failure_of(raw, version=fable.SUPPORTED_CLAUDE_VERSION,
                                      execution=execution, observed_at_epoch_ms=NOW_MS)
            with self.subTest(change=change):
                self.assertEqual(record["error_code"], expected)
                self.assertEqual(record["terminal_evidence"], "VERIFIED")
                self.assertIsNone(record["reset_at_epoch_ms"])
                self.assertIsNone(record["limit_type"])
                self.assertIsNone(record["extra_usage"])
                self.assertEqual(len(fable.events_of(raw)), 2)
                for alteration in ({"process_group_state": "PRESENT"}, {"exit_code": 1},
                                   {"timed_out": True}, {"interrupted": True},
                                   {"guard_stop": {**guard, "event_sha256": "0" * 64}},
                                   {"guard_stop": {**guard, "event_index": True}}):
                    unproved = fable.failure_of(raw, version=fable.SUPPORTED_CLAUDE_VERSION,
                                                execution={**execution, **alteration})
                    self.assertEqual(unproved["terminal_evidence"], "UNKNOWN")

    def test_informational_weekly_event_does_not_override_rejected_five_hour(self):
        events = failed_events(machine_reset=True)
        events.insert(1, {"type": "rate_limit_event", "uuid": "event-weekly-00001", "session_id": SESSION,
                          "rate_limit_info": {**BLOCKED, "status": "allowed", "rateLimitType": "seven_day",
                                              "resetsAt": RESET_SECONDS + 86400}})
        self.assertEqual(parse(events)["limit_type"], "five_hour")
        self.assertEqual(parse(events)["reset_at_epoch_ms"], RESET_SECONDS * 1000)

    def test_conflicting_rejected_windows_or_later_allowed_event_do_not_schedule(self):
        for later in ("rejected", "allowed"):
            events = failed_events(machine_reset=True)
            events.insert(2, {"type": "rate_limit_event", "uuid": "event-weekly-00001", "session_id": SESSION,
                              "rate_limit_info": {**BLOCKED, "status": later, "rateLimitType": "seven_day",
                                                  "resetsAt": RESET_SECONDS + 86400}})
            self.assertNotEqual(parse(events)["error_code"], "MODEL_RATE_LIMIT")

    def test_malformed_duplicate_keys_or_nonfinite_json_is_invalid(self):
        for raw in (b'{"type":"result","is_error":false,"is_error":true}',
                    b'{"type":"result","n":NaN}', b'not-json', b'[]', b'\xff'):
            with self.subTest(raw=raw):
                record = fable.failure_of(raw, version=fable.SUPPORTED_CLAUDE_VERSION, execution=EXECUTION)
                self.assertEqual(record["error_code"], "RESULT_INVALID")

    def test_process_ambiguity_timeout_interruption_or_signal_never_verifies(self):
        for change in ({"process_group_state": "PRESENT"}, {"process_state": "UNKNOWN"},
                       {"model_attempted": False}, {"timed_out": True}, {"interrupted": True},
                       {"exit_code": -9}, {"exit_code": True}):
            with self.subTest(change=change):
                record = fable.failure_of(raw_of(failed_events(machine_reset=True)),
                                          version=fable.SUPPORTED_CLAUDE_VERSION,
                                          execution={**EXECUTION, **change}, observed_at_epoch_ms=NOW_MS)
                self.assertNotEqual(record["terminal_evidence"], "VERIFIED")

    def test_free_text_and_unknown_fields_never_enter_projection(self):
        events = failed_events(machine_reset=True)
        secret = "gho_" + "S" * 36
        events[-1]["result"] = secret
        events[1]["rate_limit_info"]["overageDisabledReason"] = secret
        record = parse(events)
        self.assertNotIn(secret, json.dumps(record))
        self.assertEqual(record["error_code"], "MODEL_RATE_LIMIT")

    def test_success_output_preserves_existing_dictionary(self):
        data = fable.model_output(cli_output(verdict()))
        self.assertEqual(data["structured_output"], verdict())
        self.assertEqual(data["overage"], {"overageStatus": "rejected", "overageDisabledReason": "org_level_disabled"})

    def test_malformed_stream_stops_before_more_model_output(self):
        for first in (b'{"type":"rate_limit_event","type":"assistant"}\n', b'not-json\n'):
            raw, stop = fable.read_stream(io.BytesIO(first + b'{"type":"assistant"}\n'))
            self.assertTrue(stop)
            self.assertEqual(raw, first)

    def test_stream_bound_limits_even_a_single_unbounded_line(self):
        with patch.object(fable, "MAX_OUTPUT", 10):
            raw, stop = fable.read_stream(io.BytesIO(b"x" * 1000))
        self.assertTrue(stop)
        self.assertEqual(len(raw), 11)


class ProcessTests(unittest.TestCase):
    def runner(self):
        runner = fable.Runner(1001, 1001, "/nonexistent", "/fake/claude", "not-a-real-token")
        runner.unreadable = lambda work: None
        return runner

    def process(self, *, broken=False, output=None):
        class Proc:
            pid = 4321
            returncode = None
            stdin = io.BytesIO()
            stdout = io.BytesIO(output or raw_of(failed_events(machine_reset=True)))
            calls = []
            def wait(self, timeout=None):
                self.calls.append(("wait", timeout))
                self.returncode = 1
                return 1
            def poll(self):
                return self.returncode
        proc = Proc()
        if broken:
            class BrokenInput:
                def write(self, value):
                    raise BrokenPipeError("transport interrupted")
            proc.stdin = BrokenInput()
        return proc

    def test_normal_terminal_model_records_waited_dead_process_group(self):
        runner, proc = self.runner(), self.process()
        with patch.object(fable.subprocess, "Popen", return_value=proc), \
                patch.object(fable.threading, "Timer"), \
                patch.object(fable.os, "killpg", side_effect=ProcessLookupError):
            code, raw, stderr = runner(Path("/fake/work"), "system", {}, "prompt")
        self.assertEqual(code, 1)
        self.assertEqual(proc.calls, [("wait", None)])
        self.assertEqual(runner.last_execution, EXECUTION)
        self.assertEqual(raw, raw_of(failed_events(machine_reset=True)))

    def test_transport_exception_kills_and_waits_the_group_before_escape(self):
        runner, proc, signals = self.runner(), self.process(broken=True), []
        def killpg(pid, sig):
            signals.append(sig)
            if sig == 0:
                raise ProcessLookupError
        with patch.object(fable.subprocess, "Popen", return_value=proc), \
                patch.object(fable.threading, "Timer"), patch.object(fable.os, "killpg", killpg):
            with self.assertRaises(BrokenPipeError):
                runner(Path("/fake/work"), "system", {}, "prompt")
        self.assertEqual(signals, [signal.SIGKILL, 0])
        self.assertEqual(proc.calls, [("wait", 30)])
        self.assertTrue(runner.last_execution["interrupted"])
        self.assertEqual(runner.last_execution["process_group_state"], "ABSENT")

    def test_surviving_group_is_killed_and_remains_ambiguous(self):
        runner, proc, signals = self.runner(), self.process(), []
        with patch.object(fable.subprocess, "Popen", return_value=proc), \
                patch.object(fable.threading, "Timer"), \
                patch.object(fable.os, "killpg", side_effect=lambda pid, sig: signals.append(sig)):
            runner(Path("/fake/work"), "system", {}, "prompt")
        self.assertEqual(signals, [0, signal.SIGKILL])
        self.assertEqual(runner.last_execution["process_group_state"], "PRESENT")
        self.assertNotEqual(fable.failure_of(runner.last_output, version=fable.SUPPORTED_CLAUDE_VERSION,
                                             execution=runner.last_execution)["terminal_evidence"], "VERIFIED")

    def test_missing_overage_stops_the_real_runner_flow_before_terminal_result(self):
        events = failed_events(machine_reset=True)
        del events[1]["rate_limit_info"]["overageStatus"]
        runner, proc, signals = self.runner(), self.process(output=raw_of(events)), []
        def killpg(pid, sig):
            signals.append(sig)
            if sig == 0:
                raise ProcessLookupError
        def wait(timeout=None):
            proc.calls.append(("wait", timeout))
            proc.returncode = -signal.SIGKILL
        proc.wait = wait
        with patch.object(fable.subprocess, "Popen", return_value=proc), \
                patch.object(fable.threading, "Timer"), patch.object(fable.os, "killpg", killpg):
            runner(Path("/fake/work"), "system", {}, "prompt")
        self.assertEqual(signals, [signal.SIGKILL, 0])
        self.assertEqual(len(fable.events_of(runner.last_output)), 2)
        self.assertEqual(runner.last_execution["guard_stop"]["error_code"], "OVERAGE_UNVERIFIED")
        record = fable.failure_of(runner.last_output, version=fable.SUPPORTED_CLAUDE_VERSION,
                                  execution=runner.last_execution)
        self.assertEqual(record["terminal_evidence"], "VERIFIED")

    def test_wrapper_stream_stop_runner_records_actual_kill_before_verification(self):
        for raw, expected in ((b'not-json\n', "STREAM_INVALID"), (b'x' * 1000, "OUTPUT_LIMIT")):
            runner, proc, signals = self.runner(), self.process(output=raw), []
            def killpg(pid, sig):
                signals.append(sig)
                if sig == 0:
                    raise ProcessLookupError
            def wait(timeout=None):
                proc.returncode = -signal.SIGKILL
            proc.wait = wait
            with self.subTest(expected=expected), patch.object(fable, "MAX_OUTPUT", 32), \
                    patch.object(fable.subprocess, "Popen", return_value=proc), \
                    patch.object(fable.threading, "Timer"), patch.object(fable.os, "killpg", killpg):
                runner(Path("/fake/work"), "system", {}, "prompt")
                self.assertEqual(signals, [signal.SIGKILL, 0])
                self.assertTrue(runner.last_execution["guard_kill_sent"])
                record = fable.failure_of(runner.last_output, version="2.1.286 (Claude Code)",
                                          execution=runner.last_execution)
                self.assertEqual(record["error_code"], expected)
                self.assertEqual(record["terminal_evidence"], "VERIFIED")

    def test_timeout_runner_seals_only_a_completed_timeout_kill(self):
        runner, proc, signals = self.runner(), self.process(output=b'{}\n'), []
        class ImmediateTimer:
            def __init__(self, seconds, function, args):
                self.function, self.args = function, args
            def start(self): self.function(*self.args)
            def cancel(self): pass
            def join(self): pass
        def killpg(pid, sig):
            signals.append(sig)
            if sig == 0: raise ProcessLookupError
        def wait(timeout=None): proc.returncode = -signal.SIGKILL
        proc.wait = wait
        with patch.object(fable.subprocess, "Popen", return_value=proc), \
                patch.object(fable.threading, "Timer", ImmediateTimer), patch.object(fable.os, "killpg", killpg):
            runner(Path("/fake/work"), "system", {}, "prompt")
        self.assertEqual(signals, [signal.SIGKILL, 0])
        record = fable.failure_of(runner.last_output, version="2.1.286 (Claude Code)",
                                  execution=runner.last_execution)
        self.assertEqual(record["error_code"], "WRAPPER_TIMEOUT")
        self.assertEqual(record["terminal_evidence"], "VERIFIED")

    def test_late_timeout_does_not_replace_successful_stream_guard_kill(self):
        for raw, expected in ((b'not-json\n', "STREAM_INVALID"), (b'x' * 1000, "OUTPUT_LIMIT")):
            runner, proc, signals = self.runner(), self.process(output=raw), []
            class LateTimer:
                def __init__(self, seconds, function, args):
                    self.function, self.args = function, args
                    proc.timeout_callback = lambda: self.function(*self.args)
                def start(self): pass
                def cancel(self): pass
                def join(self): pass
            def killpg(pid, sig):
                signals.append(sig)
                if sig == 0 or signals.count(signal.SIGKILL) > 1:
                    raise ProcessLookupError
            def wait(timeout=None):
                # The guard already killed the group. A previously started
                # deadline callback reaches killpg before timer.join completes.
                proc.timeout_callback()
                proc.returncode = -signal.SIGKILL
            proc.wait = wait
            with self.subTest(expected=expected), patch.object(fable, "MAX_OUTPUT", 32), \
                    patch.object(fable.subprocess, "Popen", return_value=proc), \
                    patch.object(fable.threading, "Timer", LateTimer), patch.object(fable.os, "killpg", killpg):
                runner(Path("/fake/work"), "system", {}, "prompt")
                self.assertEqual(signals, [signal.SIGKILL, signal.SIGKILL, 0])
                record = fable.failure_of(runner.last_output, version="2.1.286 (Claude Code)",
                                          execution=runner.last_execution)
                self.assertEqual(record["error_code"], expected)
                self.assertEqual(record["terminal_evidence"], "VERIFIED")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)
        mock_root_evidence(self, self.runs)
        # /tmp is shared only for these test fixtures. Production traverses all
        # ancestors; tests still check the fixture root itself with real lstat.
        self.chain_patch = patch.object(fable, "protected_run_chain", self.check_test_root)
        self.chain_patch.start()
        self.binding = {"repository": "BeautifulMind-JT/ZARI", "node": "sp009", "head": "a" * 40}

    def tearDown(self):
        self.chain_patch.stop()
        self.tmp.cleanup()

    def check_test_root(self, path):
        info = os.lstat(path)
        if not __import__("stat").S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise fable.FableError("failure runs directory chain is not protected")

    def context(self, output=None, execution=None, github=None):
        class FakeRunner:
            last_execution = copy.deepcopy(execution or EXECUTION)
            def __call__(self, *args):
                return self.last_execution["exit_code"], output or raw_of(failed_events(machine_reset=True)), b""
        ctx = fable.Context(github, FakeRunner(), self.runs, None, ("secret-oauth-test",), "a" * 64)
        ctx.claude_version = fable.SUPPORTED_CLAUDE_VERSION
        return ctx

    def fail_run(self, ctx):
        run_id, run = ctx.new_run("audit", "BeautifulMind-JT/ZARI", 37)
        fable.make_dir(run / "work" / "audit")
        fable.write(run / "work" / "audit" / "program_scope.json", json.dumps({"binding": self.binding}))
        with patch.object(fable.time, "time", return_value=NOW_MS / 1000):
            with self.assertRaises(fable.FableError) as caught:
                ctx.run_model(run, "system", {}, "prompt", lambda value: value)
        return run_id, run, caught.exception.failure

    def test_sealed_terminal_evidence_round_trip_is_bound_and_private(self):
        ctx = self.context()
        run_id, run, record = self.fail_run(ctx)
        verified = fable.verify_failure_evidence(ctx, run_id)
        self.assertEqual(verified, record)
        self.assertEqual(verified["program_binding"], self.binding)
        self.assertEqual(verified["kind"], "audit")
        self.assertIs(ctx.last_failure, record)
        for name in ("model-attempt.json", "claude-output.jsonl", "claude-stderr.txt", "failure-evidence.json"):
            self.assertEqual((run / name).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            fable.sealed_file(run / "failure-evidence.json", b"overwrite")

    def test_sealed_protective_stop_is_verified_without_extra_usage_or_retry_authority(self):
        events = failed_events(machine_reset=True)
        del events[1]["rate_limit_info"]["overageStatus"]
        raw, guard = fable.read_stream(io.BytesIO(raw_of(events)))
        ctx = self.context(output=raw, execution={**EXECUTION, "exit_code": -signal.SIGKILL,
                                                  "guard_stop": guard})
        run_id, run, record = self.fail_run(ctx)
        self.assertEqual(record["error_code"], "OVERAGE_UNVERIFIED")
        self.assertEqual(fable.verify_failure_evidence(ctx, run_id), record)
        self.assertEqual((run / "guard-stop.json").stat().st_mode & 0o777, 0o600)
        self.assertIsNone(record["extra_usage"])
        self.assertIsNone(record["reset_at_epoch_ms"])
        (run / "guard-stop.json").write_bytes(b"{}")
        with self.assertRaisesRegex(fable.FableError, "protective stop"):
            fable.verify_failure_evidence(ctx, run_id)

    def test_guard_archive_to_operator_settlement_preserves_admission_and_blocks_quota_retry(self):
        import control_plane_program_receipts as journal
        from control_plane_program_quota import Quota
        events = failed_events(machine_reset=True)
        del events[1]["rate_limit_info"]["overageStatus"]
        raw, guard = fable.read_stream(io.BytesIO(raw_of(events)))
        ctx = self.context(output=raw, execution={**EXECUTION, "exit_code": -signal.SIGKILL,
                                                  "guard_stop": guard})
        store = journal.Receipts(self.runs, "a" * 64, trust=lambda *args, **kwargs: None)
        def invoke():
            _, _, failure = self.fail_run(ctx)
            raise fable.FableError("protective stop", failure=failure)
        with self.assertRaises(fable.FableError):
            store.execute("audit", self.binding, invoke)
        first = store.read("audit", self.binding)
        original = store._path(first["admission"], "outcome").read_bytes()
        verify = lambda run: fable.verify_failure_evidence(ctx, run)
        def forbidden_call(*args):
            self.fail("protective stop granted an automatic model retry")
        quota = Quota(store, verify, forbidden_call, forbidden_call, clock_ms=lambda: NOW_MS)
        self.assertEqual(quota.resume("audit", self.binding, forbidden_call)["status"], "BLOCKED_ERROR")
        self.assertEqual(list(quota.root.iterdir()), [])
        settled = store.reconcile(first["admission"], first["state_version"], verify)
        self.assertEqual(settled["status"], "RECONCILED_FAILED")
        self.assertEqual(store._path(first["admission"], "outcome").read_bytes(), original)
        self.assertEqual(store.read("audit", self.binding)["status"], "ERROR")
        self.assertEqual(store.read("audit", self.binding)["settlement"], "TERMINAL_FAILED")
        self.assertEqual(quota.resume("audit", self.binding, forbidden_call)["status"], "BLOCKED_ERROR")

    def test_each_wrapper_stop_roundtrips_settles_without_same_binding_reexecution_or_quota(self):
        import control_plane_program_receipts as journal
        from control_plane_program_quota import Quota
        for code, raw in (("WRAPPER_TIMEOUT", b'{}\n'), ("STREAM_INVALID", b'not-json\n'),
                          ("STREAM_INVALID", b'{"type":"assistant","type":"result"}\n'),
                          ("OUTPUT_LIMIT", b'x' * 65)):
            with self.subTest(code=code, raw=raw), patch.object(fable, "MAX_OUTPUT", 64):
                guard = (fable.wrapper_stop(code, raw, timeout_seconds=fable.TIMEOUT_SECONDS)
                         if code == "WRAPPER_TIMEOUT" else fable.read_stream(io.BytesIO(raw))[1])
                ctx = self.context(output=raw, execution={**EXECUTION, "exit_code": -signal.SIGKILL,
                    "guard_stop": guard, "guard_kill_sent": True, "timed_out": code == "WRAPPER_TIMEOUT"})
                ctx.claude_version = "2.1.286 (Claude Code)"
                store_root = self.runs / ("store-" + fable.sha256_bytes(raw + code.encode())[:8])
                store_root.mkdir(mode=0o700)
                store = journal.Receipts(store_root, "a" * 64, trust=lambda *args, **kwargs: None)
                def invoke():
                    _, _, failure = self.fail_run(ctx)
                    raise fable.FableError("wrapper stop", failure=failure)
                with self.assertRaises(fable.FableError): store.execute("audit", self.binding, invoke)
                first = store.read("audit", self.binding)
                self.assertEqual(first["failure"]["error_code"], code)
                original = store._path(first["admission"], "outcome").read_bytes()
                verify = lambda run: fable.verify_failure_evidence(ctx, run)
                def forbidden(*args): self.fail("self-stop retried the model")
                quota = Quota(store, verify, forbidden, forbidden, clock_ms=lambda: NOW_MS)
                self.assertEqual(quota.resume("audit", self.binding, forbidden)["status"], "BLOCKED_ERROR")
                self.assertEqual(list(quota.root.iterdir()), [])
                settled = store.reconcile(first["admission"], first["state_version"], verify)
                self.assertEqual(settled["status"], "RECONCILED_FAILED")
                self.assertEqual(store._path(first["admission"], "outcome").read_bytes(), original)
                self.assertEqual(store.execute("audit", self.binding, forbidden)["status"], "ERROR")
                self.assertEqual(quota.resume("audit", self.binding, forbidden)["status"], "BLOCKED_ERROR")
                changed = {**self.binding, "head": "b" * 40}
                self.assertEqual(store.read("audit", changed)["status"], "MISSING")

    def test_tampered_archive_or_projection_cannot_settle(self):
        for target in ("claude-output.jsonl", "claude-stderr.txt", "failure-evidence.json", "model-attempt.json"):
            with self.subTest(target=target):
                ctx = self.context()
                run_id, run, record = self.fail_run(ctx)
                (run / target).write_bytes(b"{}")
                with self.assertRaises((fable.FableError, ValueError, KeyError)):
                    fable.verify_failure_evidence(ctx, run_id)

    def test_nonobject_or_boolean_version_record_is_a_controlled_error(self):
        for target, value in (("model-attempt.json", []), ("failure-evidence.json", []),
                              ("model-attempt.json", {"schema_version": True}),
                              ("failure-evidence.json", {"schema": fable.FAILURE_SCHEMA, "schema_version": True})):
            with self.subTest(target=target, value=value):
                ctx = self.context()
                run_id, run, record = self.fail_run(ctx)
                (run / target).write_bytes(fable.canonical_bytes(value))
                with self.assertRaises(fable.FableError):
                    fable.verify_failure_evidence(ctx, run_id)

    def test_publication_markers_refuse_terminal_failure_settlement(self):
        for marker in ("publish-intent.json", "publish-response.json", "comment.md", "run.json"):
            with self.subTest(marker=marker):
                ctx = self.context()
                run_id, run, _ = self.fail_run(ctx)
                (run / marker).write_text("{}")
                with self.assertRaisesRegex(fable.FableError, "publication"):
                    fable.verify_failure_evidence(ctx, run_id)

    def test_unprotected_or_symlink_archive_cannot_settle(self):
        for mode in ("readable", "symlink", "hardlink"):
            ctx = self.context()
            run_id, run, _ = self.fail_run(ctx)
            path = run / "claude-output.jsonl"
            if mode == "readable":
                path.chmod(0o644)
            elif mode == "symlink":
                data = path.read_bytes()
                path.unlink()
                target = run / "replacement.jsonl"
                target.write_bytes(data)
                target.chmod(0o600)
                path.symlink_to(target)
            else:
                os.link(path, run / "linked-output.jsonl")
            with self.subTest(mode=mode), self.assertRaises(fable.FableError):
                fable.verify_failure_evidence(ctx, run_id)

    def test_path_caller_or_ambiguous_run_id_cannot_select_archive(self):
        ctx = self.context()
        run_id, run, _ = self.fail_run(ctx)
        for value in (str(run), "../" + run_id, "a" * 64, None):
            with self.subTest(value=value), self.assertRaises(fable.FableError):
                fable.verify_failure_evidence(ctx, value)
        (self.runs / (run_id + "-another-run")).mkdir()
        with self.assertRaisesRegex(fable.FableError, "ambiguous"):
            fable.verify_failure_evidence(ctx, run_id)

    def test_archive_failure_is_unknown_and_never_has_terminal_authority(self):
        ctx = self.context()
        run_id, run = ctx.new_run("preflight", "x/self", 0)
        original = fable.sealed_file
        def fail_output(path, data):
            if path.name == "claude-output.jsonl":
                raise OSError("disk full")
            return original(path, data)
        with patch.object(fable, "sealed_file", fail_output), self.assertRaises(fable.FableError) as caught:
            ctx.run_model(run, "system", {}, "prompt", lambda value: value)
        self.assertEqual(caught.exception.failure["error_code"], "UNKNOWN")
        self.assertEqual(caught.exception.failure["archive_state"], "UNKNOWN")
        self.assertFalse((run / "failure-evidence.json").exists())

    def test_post_intent_precedes_post_and_transport_failure_remains_unknown(self):
        ctx = self.context()
        run_id, run = ctx.new_run("audit", "BeautifulMind-JT/ZARI", 37)
        class BrokenGitHub:
            def post(self, *args):
                self.intent = json.loads((run / "publish-intent.json").read_text())
                raise fable.FableError("connection reset after possible commit")
        ctx.github = BrokenGitHub()
        with self.assertRaisesRegex(fable.FableError, "publication") as caught:
            ctx.finish(run, {"run": run_id, "program_binding": self.binding}, "BeautifulMind-JT/ZARI", 37, "comment")
        self.assertEqual(ctx.github.intent["publication_state"], "ATTEMPTED")
        self.assertEqual(caught.exception.failure["publication_state"], "UNKNOWN")
        self.assertFalse((run / "publish-response.json").exists())

    def test_tuple_only_runner_cannot_supply_process_proof(self):
        from test_control_plane_fable import FakeRunner
        ctx = fable.Context(None, FakeRunner(output=raw_of(failed_events(machine_reset=True))),
                            self.runs, None, (), "a" * 64)
        ctx.claude_version = fable.SUPPORTED_CLAUDE_VERSION
        run_id, run, record = self.fail_run(ctx)
        self.assertNotEqual(record["terminal_evidence"], "VERIFIED")
        with self.assertRaises(fable.FableError):
            fable.verify_failure_evidence(ctx, run_id)


if __name__ == "__main__":
    unittest.main()
