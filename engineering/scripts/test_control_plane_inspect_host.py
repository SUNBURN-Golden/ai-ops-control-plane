from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_plane_inspect_host as ih  # noqa: E402
from control_plane_inspect_core import InspectError  # noqa: E402

SUDOERS = Path(__file__).resolve().parents[1] / ".github" / "control-plane" / "sudoers-aiops-inspector.example"
MODULE = Path(ih.__file__).resolve()
REPO = "BeautifulMind-JT/ZARI"
FORBIDDEN_VERBS = ("reap", "reconcile", "init", "migrate", "materialize-begin", "materialize-finish",
                   "materialize-resolve", "materialize-plan", "preflight", "launch")
EXPECTED_KWARGS = {"user": "aiops-inspect-ledger", "group": "aiops-inspect-ledger", "extra_groups": [],
                   "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, "stdin": subprocess.DEVNULL,
                   "capture_output": True, "timeout": 10, "check": False, "cwd": "/"}


def lanes_board():
    return {"status": "OK", "max_active_sessions": 4, "active_total": 1,
            "lanes": [{"lane": "DEVIN", "enabled": True,
                       "active": [{"request": "a" * 24, "repository": REPO, "task": "ZARI-N1", "role": "WRITER",
                                   "lane": "DEVIN", "state": "CONFIRMED", "created": 1759200000.0}]},
                      {"lane": "GLM", "enabled": False, "active": []}]}


class FakeRunner:
    """Records every call; answers from a queue of (returncode, stdout, stderr) or exceptions."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        answer = self.answers.pop(0) if self.answers else (0, b"{}", b"")
        if isinstance(answer, BaseException):
            raise answer
        code, stdout, stderr = answer
        if isinstance(stdout, (dict, list)):
            stdout = (json.dumps(stdout) + "\n").encode()
        return subprocess.CompletedProcess(command, code, stdout, stderr)


def ok(obj, code=0):
    return (code, obj, b"")


# ------------------------------------------------------------------------- sudoers parsing


def _unescape(text):
    """sudoers removes a backslash before : \\ , = # or blank; an unescaped one of those ends the entry."""
    assert not re.search(r"(?<!\\)[#:,=]", text), f"unescaped sudoers special character in {text}"
    return re.sub(r"\s+", " ", re.sub(r"\\([:\\,= \t#])", r"\1", text)).strip()


def parse_sudoers(text):
    """Return (aliases {name: [(command, args)]}, user specs [line]) of a sudoers fragment."""
    logical, buffer = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buffer and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append((buffer + line).strip())
        buffer = ""
    if buffer:
        raise AssertionError("dangling continuation line")
    aliases, specs = {}, []
    for line in logical:
        match = re.fullmatch(r"Cmnd_Alias\s+([A-Z][A-Z0-9_]*)\s*=\s*(.+)", line)
        if match:
            entries = [entry.strip() for entry in re.split(r"(?<!\\),", match.group(2))]
            commands = []
            for entry in entries:
                command, _, args = entry.partition(" ")
                commands.append((command, _unescape(args.strip())))
            aliases[match.group(1)] = commands
        else:
            specs.append(" ".join(line.split()))
    return aliases, specs


class SudoersParityTest(unittest.TestCase):
    def setUp(self):
        self.text = SUDOERS.read_text(encoding="utf-8")
        self.aliases, self.specs = parse_sudoers(self.text)
        self.rules = [re.compile(args) for _, args in self.aliases["AIOPS_INSPECT_READ"]]

    def test_file_has_exactly_three_reads_for_the_ledger_account_only(self):
        self.assertTrue(self.text.startswith("# /etc/sudoers.d/aiops-inspector  (root:root 0440;"))
        self.assertEqual(list(self.aliases), ["AIOPS_INSPECT_READ"])
        self.assertEqual(self.specs, ["aiops-inspect-ledger ALL=(astra-control) NOPASSWD: AIOPS_INSPECT_READ"])
        entries = self.aliases["AIOPS_INSPECT_READ"]
        self.assertEqual([command for command, _ in entries], [ih.DEFAULT_HELPER] * 3)
        self.assertEqual(tuple(args for _, args in entries), ih.SUDOERS_ARG_RULES)
        for _, args in entries:
            self.assertTrue(args.startswith("^") and args.endswith("$"), args)
            for char in ":#,=":
                self.assertNotIn(char, args)
        self.assertNotIn("NOPASSWD: ALL", self.text)

    def test_comment_explains_why_this_file_is_the_allowlist(self):
        for phrase in ("authorize_identity", "THIS FILE IS THE REAL ALLOWLIST", "never calls the helper as root",
                       "aiops-inspect-ledger", "no dot"):
            self.assertIn(phrase, self.text)

    def matches(self, argv):
        line = " ".join(argv)
        return any(rule.fullmatch(line) for rule in self.rules)

    def produced(self, method, *args):
        runner = FakeRunner(ok(lanes_board()) if method == "lanes" else ok({"status": "X"}))
        reader = ih.HostReader(runner=runner)
        try:
            getattr(reader, method)(*args)
        except InspectError as exc:
            self.assertNotEqual(exc.reason, "HOST_ARGV", args)
        self.assertEqual(len(runner.calls), 1)
        command = runner.calls[0][0]
        self.assertEqual(command[:5], ["/usr/bin/sudo", "-n", "-u", "astra-control", ih.DEFAULT_HELPER])
        return command[5:]

    def test_every_producible_argv_matches_a_sudoers_rule(self):
        rng = random.Random(7)
        alnum = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

        def word(first, rest, low, high):
            return rng.choice(first) + "".join(rng.choice(rest) for _ in range(rng.randint(low, high)))

        self.assertTrue(self.matches(self.produced("lanes")))
        for _ in range(300):
            repository = word(alnum + "_", alnum + "_.-", 0, 30) + "/" + word(alnum + "_", alnum + "_.-", 0, 30)
            task = word(upper, upper + "._-", 0, 40)
            program = word(alnum, alnum + "._-", 0, 30)
            node = word(alnum, alnum + "._-", 0, 30)
            self.assertTrue(self.matches(self.produced("task_status", repository, task)), (repository, task))
            self.assertTrue(self.matches(self.produced("materialize_status", program, node)), (program, node))

    def test_forbidden_verbs_are_refused_by_the_file_and_never_produced(self):
        samples = ["reap --launch-request-id " + "a" * 24 + " --evidence https://github.com/o/r/issues/1"
                   "#issuecomment-2 --pin-stdin", "reconcile --launch-request-id " + "a" * 24 + " --no-session",
                   "init", "migrate --to 3", "materialize-begin --program p --node n --repository o/r "
                   "--plan-commit " + "0" * 40, "materialize-resolve --program p --node n --request "
                   + "a" * 24 + " --evidence x --not-created", "preflight --builder-id CURSOR", "launch",
                   "status --launch-request-id " + "a" * 24, "task-status --repository o/r --task T --task U",
                   "status --lanes --launch-request-id x"]
        for sample in samples:
            self.assertFalse(self.matches(sample.split()), sample)
        # No string literal in the module names a forbidden verb, so no code path can build one.
        literals = {node.value for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8")))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for verb in FORBIDDEN_VERBS:
            self.assertNotIn(verb, literals)
        # Injection attempts through the public arguments never reach the runner.
        hostile = ["x reap", "N1 --node N2", "-x", "--lanes", "reap\n", "a\0b", "", " ", "N1$", "N1;init",
                   "a" * 65, "../x", 7, None]
        for value in hostile:
            for method, args in (("task_status", (REPO, value)), ("task_status", (value, "ZARI-N1")),
                                 ("materialize_status", ("zari", value)), ("materialize_status", (value, "N1"))):
                runner = FakeRunner()
                with self.subTest(method=method, args=args), self.assertRaises(InspectError) as caught:
                    getattr(ih.HostReader(runner=runner), method)(*args)
                self.assertEqual(caught.exception.reason, "HOST_ARGV")
                self.assertEqual(runner.calls, [])

    def test_public_surface_is_three_verbs(self):
        reader = ih.HostReader(runner=FakeRunner())
        public = {name for name in dir(reader) if not name.startswith("_") and callable(getattr(reader, name))}
        self.assertEqual(public, {"lanes", "task_status", "materialize_status"})
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertFalse({name for name in functions if not name.startswith("_")}
                         - {"lanes", "task_status", "materialize_status"})

    def test_builder_regex_rejects_what_sudo_would_reject(self):
        with self.assertRaises(InspectError) as caught:
            ih._allowed(["status", "--lanes", "--extra"])
        self.assertEqual(caught.exception.reason, "HOST_ARGV")
        with self.assertRaises(InspectError):
            ih._allowed(["task-status", "--repository", "o/r", "--task", "lower"])


class HostReaderTest(unittest.TestCase):
    def test_lanes_runs_exact_command_as_the_ledger_account(self):
        runner = FakeRunner(ok(lanes_board()))
        reader = ih.HostReader("/opt/astra/bin/astra-host-control", "aiops-inspect-ledger", runner=runner)
        self.assertEqual(reader.lanes(), lanes_board())
        command, kwargs = runner.calls[0]
        self.assertEqual(command, ["/usr/bin/sudo", "-n", "-u", "astra-control",
                                   "/opt/astra/bin/astra-host-control", "status", "--lanes"])
        self.assertEqual(kwargs, EXPECTED_KWARGS)
        self.assertEqual(reader.calls, 1)

    def test_task_status_and_materialize_status_argv(self):
        rows = [{"launch_request_id": "a" * 24, "state": "RECONCILED", "role": "WRITER", "lane": "DEVIN"}]
        runner = FakeRunner(ok({"status": "OK", "repository": REPO, "task": "ZARI-N1", "rows": rows}),
                            ok({"status": "NOT_FOUND", "program": "zari", "node": "N1"}),
                            ok({"status": "CREATED", "program": "zari", "node": "N2", "issue": 12,
                                "repository": REPO, "sealed": {}}))
        reader = ih.HostReader(runner=runner, timeout=7)
        self.assertEqual(reader.task_status(REPO, "ZARI-N1")["rows"], rows)
        self.assertEqual(reader.materialize_status("zari", "N1")["status"], "NOT_FOUND")
        self.assertEqual(reader.materialize_status("zari", "N2")["issue"], 12)
        self.assertEqual([call[0][5:] for call in runner.calls],
                         [["task-status", "--repository", REPO, "--task", "ZARI-N1"],
                          ["materialize-status", "--program", "zari", "--node", "N1"],
                          ["materialize-status", "--program", "zari", "--node", "N2"]])
        for _, kwargs in runner.calls:
            self.assertEqual(kwargs, {**EXPECTED_KWARGS, "timeout": 7})
        self.assertEqual(reader.calls, 3)

    def test_custom_helper_and_account_are_passed_through(self):
        runner = FakeRunner(ok(lanes_board()))
        ih.HostReader("/opt/other/helper", "inspect-reader", runner=runner).lanes()
        command, kwargs = runner.calls[0]
        self.assertEqual(command[4], "/opt/other/helper")
        self.assertEqual((kwargs["user"], kwargs["group"]), ("inspect-reader", "inspect-reader"))

    def test_constructor_refuses_root_and_bad_settings(self):
        for kwargs in ({"account": "root"}, {"account": "Bad User"}, {"helper": "relative/helper"},
                       {"helper": "/opt/../etc/x"}, {"timeout": 0}, {"timeout": True}, {"max_calls": 0},
                       {"max_calls": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(InspectError) as caught:
                ih.HostReader(runner=FakeRunner(), **kwargs)
            self.assertEqual(caught.exception.reason, "CONFIG")

    def test_last_non_empty_line_is_parsed(self):
        text = b"noise line\n" + json.dumps(lanes_board()).encode() + b"\n\n  \n"
        reader = ih.HostReader(runner=FakeRunner((0, text, b"")))
        self.assertEqual(reader.lanes()["active_total"], 1)
        reader = ih.HostReader(runner=FakeRunner((0, json.dumps(lanes_board()), "")))
        self.assertEqual(reader.lanes()["max_active_sessions"], 4)

    def test_helper_error_is_host_refused_with_redacted_reason(self):
        secret = "github_pat_" + "A1b2C3d4E5" * 6
        runner = FakeRunner(ok({"status": "ERROR", "reason": f"invalid task\nnext {secret}"}, code=2),
                            ok({"status": "ERROR", "reason": "nope"}, code=0))
        reader = ih.HostReader(runner=runner)
        with self.assertRaises(InspectError) as caught:
            reader.task_status(REPO, "ZARI-N1")
        self.assertEqual(caught.exception.reason, "HOST_REFUSED")
        self.assertEqual(caught.exception.detail, "invalid task")
        with self.assertRaises(InspectError) as caught:
            reader.lanes()
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("HOST_REFUSED", "nope"))
        self.assertNotIn(secret, str(caught.exception))

    def test_sudo_refusal_without_result_is_host_refused(self):
        secret = "xoxb-1234567890-abcdefghij-KLMNOPQRST"
        reader = ih.HostReader(runner=FakeRunner((1, b"", f"sudo: a password is required {secret}\n".encode())))
        with self.assertRaises(InspectError) as caught:
            reader.lanes()
        self.assertEqual(caught.exception.reason, "HOST_REFUSED")
        self.assertTrue(caught.exception.detail.startswith("exit 1: sudo: a password is required"))
        self.assertNotIn(secret, caught.exception.detail)
        # A non-zero exit never yields a result, even with a well-formed object on stdout.
        reader = ih.HostReader(runner=FakeRunner(ok(lanes_board(), code=3)))
        with self.assertRaises(InspectError) as caught:
            reader.lanes()
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("HOST_REFUSED", "exit 3"))

    def test_timeout_and_launch_failures(self):
        reader = ih.HostReader(runner=FakeRunner(subprocess.TimeoutExpired(["sudo"], 10)))
        with self.assertRaises(InspectError) as caught:
            reader.lanes()
        self.assertEqual(caught.exception.reason, "HOST_TIMEOUT")
        for exc in (PermissionError("not root"), KeyError("no such user"), FileNotFoundError("/usr/bin/sudo")):
            reader = ih.HostReader(runner=FakeRunner(exc))
            with self.subTest(exc=exc), self.assertRaises(InspectError) as caught:
                reader.lanes()
            self.assertEqual(caught.exception.reason, "HOST_UNAVAILABLE")
            self.assertEqual(reader.calls, 1)

    def test_budget_counts_every_attempt(self):
        runner = FakeRunner(ok(lanes_board()), subprocess.TimeoutExpired(["sudo"], 10), ok(lanes_board()))
        reader = ih.HostReader(runner=runner, max_calls=2)
        reader.lanes()
        with self.assertRaises(InspectError):
            reader.lanes()
        with self.assertRaises(InspectError) as caught:
            reader.lanes()
        self.assertEqual(caught.exception.reason, "HOST_BUDGET")
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(reader.calls, 2)
        # Argument errors are refused before they spend budget.
        reader = ih.HostReader(runner=FakeRunner(), max_calls=1)
        with self.assertRaises(InspectError):
            reader.materialize_status("zari", "bad node")
        self.assertEqual(reader.calls, 0)

    def test_output_cap_and_bad_output(self):
        cases = [(0, b"x" * (ih.MAX_OUTPUT + 1), b""), (0, b"", b""), (0, b"not json\n", b""),
                 (0, b"[1, 2]\n", b""), (0, b"\xff\xfe\n", b""), (0, b'{"a": 1, "a": 2}\n', b""),
                 (0, b'{"a": NaN}\n', b""), (0, None, None)]
        for answer in cases:
            reader = ih.HostReader(runner=FakeRunner(answer))
            with self.subTest(answer=answer[1] if answer[1] is None else answer[1][:20]), \
                    self.assertRaises(InspectError) as caught:
                reader.lanes()
            self.assertEqual(caught.exception.reason, "HOST_OUTPUT")

    def test_lanes_shape_validation(self):
        bad = []
        for change in ({"lanes": {}}, {"active_total": "1"}, {"active_total": True}, {"max_active_sessions": None},
                       {"lanes": [{"lane": "DEVIN", "enabled": True}]}, {"lanes": ["DEVIN"]},
                       {"lanes": [{"lane": "DEVIN", "enabled": "yes", "active": []}]},
                       {"lanes": [{"lane": "DEVIN", "enabled": True, "active": ["row"]}]}):
            bad.append({**lanes_board(), **change})
        missing = lanes_board()
        del missing["active_total"]
        bad.append(missing)
        for board in bad:
            reader = ih.HostReader(runner=FakeRunner(ok(board)))
            with self.subTest(board=board), self.assertRaises(InspectError) as caught:
                reader.lanes()
            self.assertEqual(caught.exception.reason, "HOST_OUTPUT")

    def test_task_status_shape_validation(self):
        good = {"status": "OK", "repository": REPO, "task": "ZARI-N1", "rows": []}
        for change in ({"repository": "other/repo"}, {"task": "ZARI-N2"}, {"rows": None}, {"rows": ["x"]},
                       {"rows": {}}):
            reader = ih.HostReader(runner=FakeRunner(ok({**good, **change})))
            with self.subTest(change=change), self.assertRaises(InspectError) as caught:
                reader.task_status(REPO, "ZARI-N1")
            self.assertEqual(caught.exception.reason, "HOST_OUTPUT")
        self.assertEqual(ih.HostReader(runner=FakeRunner(ok(good))).task_status(REPO, "ZARI-N1"), good)

    def test_materialize_status_shape_validation(self):
        for report in ({"program": "zari", "node": "N1"}, {"status": "", "program": "zari", "node": "N1"},
                       {"status": "CREATED", "program": "other", "node": "N1"},
                       {"status": "CREATED", "program": "zari", "node": "N9"}, {"status": 1}):
            reader = ih.HostReader(runner=FakeRunner(ok(report)))
            with self.subTest(report=report), self.assertRaises(InspectError) as caught:
                reader.materialize_status("zari", "N1")
            self.assertEqual(caught.exception.reason, "HOST_OUTPUT")

    def test_default_runner_is_subprocess_run(self):
        reader = ih.HostReader()
        self.assertIs(reader._runner, subprocess.run)
        with patch.object(ih.subprocess, "run", side_effect=PermissionError("not root")) as run:
            reader = ih.HostReader()
            self.assertIs(reader._runner, run)

    def test_reasons_are_host_codes(self):
        for reason in ih.HOST_REASONS:
            self.assertTrue(reason.startswith("HOST_"))
            self.assertEqual(str(InspectError(reason)), reason)


class HelperIdentityDocumentationTest(unittest.TestCase):
    """Why the sudoers file is the real allowlist: the helper accepts any other caller for every verb."""

    def test_helper_accepts_a_non_lane_caller_for_mutating_verbs(self):
        try:
            import control_plane_host as host
        except Exception as exc:  # pragma: no cover - the helper module is part of this repository
            self.skipTest(f"helper not importable: {type(exc).__name__}")
        policy = {"control_uid": 1010, "runner_uid": 1020, "builder_uids": {"DEVIN": 1030, "GLM": 1040}}
        with patch.object(host.os, "getuid", return_value=1010), patch.object(host.os, "geteuid", return_value=1010):
            with patch.dict(os.environ, {"SUDO_UID": "988"}):
                for verb in FORBIDDEN_VERBS + ("status", "task-status", "materialize-status"):
                    host.authorize_identity(policy, verb)
            with patch.dict(os.environ, {"SUDO_UID": "0"}):
                host.authorize_identity(policy, "reconcile")


if __name__ == "__main__":
    unittest.main()
