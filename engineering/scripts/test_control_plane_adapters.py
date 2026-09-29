"""Imported host adapters: program-mode prompts and recorded digests (no host needed)."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import control_plane_host as host

ADAPTERS = Path(__file__).resolve().parents[1] / "adapters" / "imported"
CURSOR = Path(__file__).resolve().parents[1] / "adapters" / "cursor"
BASE = {"repository": "o/r", "task_id": "T-1", "task_revision": "r1", "builder_id": "GLM",
        "launch_request_id": "a" * 24, "attempt_id": 1}


def load(name):
    directory = CURSOR if (CURSOR / name).exists() else ADAPTERS
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(directory / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


NAMES = ("astra-devin-adapter", "astra-grok-adapter", "astra-glm-adapter", "astra-cursor-adapter")
REVIEWER = {**BASE, "schema_version": 2, "role": "REVIEWER", "pr_number": 7, "head_sha": "b" * 40,
            "review_request_id": "c" * 24, "review_nonce": "d" * 32, "owner_lane": "DEVIN"}
WRITER = {**BASE, "schema_version": 2, "role": "WRITER", "owner_lane": "GLM", "delivery_nonce": "e" * 32}


def run_signer(path, *args):
    return subprocess.run([sys.executable, "-I", str(path), *args], capture_output=True, text=True, check=False)


def host_row(packet):
    return {"packet": json.dumps(packet), "role": packet.get("role", "WRITER")}


class AdapterPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def session(self, name, label):
        path = Path(self.temp.name) / f"{name}-{label}"
        path.mkdir(mode=0o700)
        return path

    def test_reviewer_prompt_is_read_only_and_uses_the_private_signer(self):
        for name in NAMES:
            with self.subTest(adapter=name):
                adapter = load(name)
                signer = adapter.write_signer(self.session(name, "review"), REVIEWER)
                prompt = adapter.build_prompt(REVIEWER, signer)
                self.assertIn("read-only", prompt)
                self.assertIn("Do not commit, push", prompt)
                self.assertIn(f"/usr/bin/python3 -I {signer} <PASS|PASS_WITH_NOTES|FAIL|DECISION_REQUIRED> <A1|A2> <A1|A2|A3>", prompt)
                self.assertIn(f"/usr/bin/python3 -I {signer} BLOCKED", prompt)
                self.assertNotIn("d" * 32, prompt)  # the key stays in the signer file
                self.assertNotIn("ASTRA_DELIVERY_V1", prompt)

    def test_writer_prompt_carries_the_signed_delivery_protocol(self):
        for name in NAMES:
            with self.subTest(adapter=name):
                adapter = load(name)
                signer = adapter.write_signer(self.session(name, "writer"), WRITER)
                prompt = adapter.build_prompt(WRITER, signer)
                self.assertIn(f"/usr/bin/python3 -I {signer} <pr number> <40-hex head sha>", prompt)
                self.assertIn("branch astra/t-1", prompt)
                self.assertNotIn("If blocked, post DECISION_REQUIRED", prompt)  # only signed blockers count
                self.assertIn(f"/usr/bin/python3 -I {signer} <DECISION_REQUIRED|BLOCKED|STALLED>", prompt)
                self.assertNotIn("e" * 32, prompt)
                self.assertIn("Never merge", prompt)
                legacy = adapter.build_prompt({**BASE, "schema_version": 1})
                self.assertIn("ASTRA_DELIVERY_V1 pr=<number> head=<40-hex head sha>", legacy)

    def test_signer_output_verifies_on_the_host(self):
        for name in NAMES:
            with self.subTest(adapter=name):
                adapter = load(name)
                review_signer = adapter.write_signer(self.session(name, "r"), REVIEWER)
                self.assertEqual(review_signer.stat().st_mode & 0o777, 0o600)
                line = run_signer(review_signer, "FAIL", "A2", "A3", "YES").stdout.strip()
                self.assertEqual(host.verify_pin(host_row(REVIEWER), line),
                                 {"kind": "REVIEW", "review": "c" * 24, "head": "b" * 40, "verdict": "FAIL",
                                  "depth": "A2", "required": "A3", "contract_change": "YES"})
                self.assertNotEqual(run_signer(review_signer, "MAYBE", "A1", "A1", "NO").returncode, 0)
                self.assertNotEqual(run_signer(review_signer, "PASS", "A1", "NO").returncode, 0)  # required missing
                delivery_signer = adapter.write_signer(self.session(name, "w"), WRITER)
                line = run_signer(delivery_signer, "12", "f" * 40).stdout.strip()
                self.assertEqual(host.verify_pin(host_row(WRITER), line), {"kind": "DELIVERY", "pr": 12, "head": "f" * 40})
                self.assertNotEqual(run_signer(delivery_signer, "12", "HEAD").returncode, 0)
                for signer, packet in ((review_signer, REVIEWER), (delivery_signer, WRITER)):
                    blocked = run_signer(signer, "STALLED").stdout.strip()
                    self.assertEqual(host.verify_pin(host_row(packet), blocked), {"kind": "BLOCKER", "blocker": "STALLED"})
                    self.assertNotEqual(run_signer(signer, "GIVE_UP").returncode, 0)
                # A reviewer escalates only through the verdict; a writer may block on a decision.
                self.assertNotEqual(run_signer(review_signer, "DECISION_REQUIRED").returncode, 0)
                decision = run_signer(delivery_signer, "DECISION_REQUIRED").stdout.strip()
                self.assertEqual(host.verify_pin(host_row(WRITER), decision)["blocker"], "DECISION_REQUIRED")
                # Another session's key does not verify.
                with self.assertRaises(host.HostError):
                    host.verify_pin(host_row({**WRITER, "delivery_nonce": "0" * 32}), line)

    def test_signer_is_not_written_without_a_valid_nonce(self):
        for name in NAMES:
            adapter = load(name)
            for packet in ({**BASE, "schema_version": 1}, {**REVIEWER, "review_nonce": "x" * 32},
                           {**REVIEWER, "head_sha": "HEAD"}, {**WRITER, "delivery_nonce": None}):
                with self.subTest(adapter=name, packet=packet):
                    self.assertIsNone(adapter.write_signer(self.session(name, str(id(packet))), packet))

    def test_reviewer_without_a_nonce_never_starts_a_session(self):
        for name in NAMES:
            with self.subTest(adapter=name):
                adapter = load(name)
                root = Path(self.temp.name) / f"{name}-launch"
                root.mkdir()
                packet = {key: value for key, value in REVIEWER.items() if key != "review_nonce"}
                ready = None
                if name == "astra-cursor-adapter":
                    packet["builder_id"] = "CURSOR"
                    ready = ({"model": "m", "cli": "/x"}, None)
                if hasattr(adapter, "cli_auth_ok"):
                    auth = patch.object(adapter, "cli_auth_ok", return_value=True)
                    auth.start()
                    self.addCleanup(auth.stop)
                with patch.object(adapter, "STATE_ROOT", root / "state"), \
                     patch.object(adapter, "WORKTREE_ROOT", root), \
                     patch.object(adapter, "prerequisites", return_value=ready), \
                     patch.object(adapter.subprocess, "Popen", side_effect=AssertionError("supervisor started")), \
                     patch("sys.stdin", io.StringIO(json.dumps(packet))), \
                     patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    adapter.launch()
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["outcome"], "FAILED_PRESTART")
                self.assertIn("review_nonce", result["reason"])

    def test_recorded_digests_match_the_reviewed_sources(self):
        for directory in (ADAPTERS, CURSOR):
            for line in (directory / "SHA256SUMS").read_text().splitlines():
                digest, name = line.split()
                with self.subTest(file=name):
                    self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), digest)


    def test_all_program_lanes_share_one_signer_and_census_implementation(self):
        def shared(name):
            text = (CURSOR if (CURSOR / name).exists() else ADAPTERS).joinpath(name).read_text()
            return (text[text.index('SIGNER_TEMPLATE = """'):text.index("def build_prompt(")],
                    text[text.index("def census("):text.index("def main(")])
        self.assertEqual(len({shared(name) for name in NAMES}), 1)

    def test_every_wrapper_routes_the_quiescence_probe_to_its_own_lane(self):
        for wrapper, user in (("astra-builder-devin", "astra-builder-devin"),
                              ("astra-builder-grok-build", "astra-builder-grokbuild"),
                              ("astra-builder-glm", "astra-builder-glm"), ("astra-builder-cursor", "astra-builder-cursor")):
            text = ((CURSOR if (CURSOR / wrapper).exists() else ADAPTERS) / wrapper).read_text()
            with self.subTest(wrapper=wrapper):
                self.assertIn(f'exec /usr/bin/sudo -n -u {user} -- "$INNER" --quiescence', text)

    def fake_proc(self, entries):
        proc = Path(self.temp.name) / f"proc{len(list(Path(self.temp.name).iterdir()))}"
        proc.mkdir()
        for pid, uids in entries.items():
            (proc / str(pid)).mkdir()
            (proc / str(pid) / "status").write_text(f"Name:\tx\nUid:\t{uids}\n")
        (proc / "self").mkdir()
        return proc

    def test_census_freezes_lists_and_thaws_the_lane(self):
        me = os.getpid()
        proc = self.fake_proc({me: "1030 1030 1030 1030", 51: "1030 1030 1030 1030", 52: "0 0 0 0",
                               53: "1040 1030 1040 1040"})
        for name in NAMES:
            adapter = load(name)
            signals = []
            with self.subTest(adapter=name), patch.object(adapter.os, "getuid", return_value=1030), \
                 patch.object(adapter.os, "geteuid", return_value=1030), \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(adapter.census(kill=lambda pid, sig: signals.append((pid, sig)), proc=str(proc)), 0)
                self.assertEqual(json.loads(stdout.getvalue()), {"status": "OK", "live": [51, 53]})
                self.assertEqual(signals, [(-1, adapter.signal.SIGSTOP), (-1, adapter.signal.SIGCONT)])

    def test_census_thaws_even_when_the_listing_fails_and_never_runs_as_root(self):
        adapter = load("astra-grok-adapter")
        proc = self.fake_proc({61: "garbage"})
        signals = []
        with patch.object(adapter.os, "getuid", return_value=1040), patch.object(adapter.os, "geteuid", return_value=1040), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(adapter.census(kill=lambda pid, sig: signals.append(sig), proc=str(proc)), 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "FAIL")
        self.assertEqual(signals, [adapter.signal.SIGSTOP, adapter.signal.SIGCONT])
        with patch.object(adapter.os, "getuid", return_value=0), patch.object(adapter.os, "geteuid", return_value=0), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(adapter.census(kill=lambda pid, sig: self.fail("root must never signal -1")), 1)


FAKE_CLI = r"""#!/usr/bin/python3 -I
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as out:
    out.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")
mode = open(os.path.join(here, "mode")).read().strip()
if args[:1] == ["status"]:
    print(json.dumps({"loggedIn": True, "email": "lane@example.test"}))
elif args[:1] == ["models"]:
    print("gpt-x\ngrok-4-cursor-high\nsonnet-y")
elif args[:1] == ["create-chat"]:
    if mode == "chat-fails":
        sys.exit(3)
    print("not a chat id!" if mode == "bad-chat" else "chat-abc123")
else:
    sys.exit(0)
"""


class CursorLaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = load("astra-cursor-adapter")
        self.supervisor = load("astra-cursor-supervisor")
        self.cli = self.root / "cursor-agent"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o755)
        (self.root / "mode").write_text("ok")

    def config(self, **changes):
        value = {"cli": str(self.cli), "cli_sha256": hashlib.sha256(self.cli.read_bytes()).hexdigest(),
                 "model": "grok-4-cursor-high", "auth_match": {"loggedIn": True, "email": "lane@example.test"}}
        value.update(changes)
        path = self.root / "cursor-lane.json"
        path.write_text(json.dumps(value))
        path.chmod(0o644)
        return path

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def loaded_config(self):
        with patch.object(self.adapter, "CONFIG", self.config()), \
             patch.object(self.adapter, "root_owned", return_value=True):
            return self.adapter.load_config()

    def test_config_requires_an_exact_model_digest_and_account_fields(self):
        self.assertEqual(self.loaded_config()["model"], "grok-4-cursor-high")
        bad = [{"model": "auto"}, {"model": "CONFIG_REQUIRED"}, {"model": ""}, {"cli_sha256": "PENDING"},
               {"auth_match": {}}, {"cli": "relative/agent"}]
        for changes in bad:
            with self.subTest(changes=changes), patch.object(self.adapter, "CONFIG", self.config(**changes)), \
                 patch.object(self.adapter, "root_owned", return_value=True):
                self.assertIsNone(self.adapter.load_config())
        example = Path(__file__).resolve().parents[1] / ".github/control-plane/cursor-lane.example.json"
        with patch.object(self.adapter, "CONFIG", example), patch.object(self.adapter, "root_owned", return_value=True):
            self.assertIsNone(self.adapter.load_config())  # the example never qualifies
        with patch.object(self.adapter, "CONFIG", self.config()), \
             patch.object(self.adapter, "root_owned", return_value=False):
            self.assertIsNone(self.adapter.load_config())  # an unprotected config is refused

    def test_cli_must_be_the_qualified_build(self):
        config = self.loaded_config()
        with patch.object(self.adapter, "root_owned", return_value=True):
            self.assertTrue(self.adapter.cli_ready(config))
            self.assertFalse(self.adapter.cli_ready({**config, "cli_sha256": "0" * 64}))
        with patch.object(self.adapter, "root_owned", return_value=False):
            self.assertFalse(self.adapter.cli_ready(config))

    def test_account_and_model_are_checked_against_the_installed_cli(self):
        config = self.loaded_config()
        self.assertTrue(self.adapter.cli_auth_ok(config))
        self.assertFalse(self.adapter.cli_auth_ok({**config, "auth_match": {"email": "other@example.test"}}))
        self.assertFalse(self.adapter.cli_auth_ok({**config, "model": "grok-4-cursor"}))  # a prefix is not a match

    def test_preflight_report_passes_the_host_cursor_provenance_check(self):
        config = self.loaded_config()
        with patch.object(self.adapter, "prerequisites", return_value=(config, None)), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.adapter.preflight()
        report = json.loads(stdout.getvalue())
        self.assertEqual((report["harness"], report["model"], report["builder_id"]),
                         ("CURSOR_CLI", "grok-4-cursor-high", "CURSOR"))
        policy = {"enabled_builders": ["CURSOR"], "boundary_evidence_pointer": "https://github.com/o/r/issues/1",
                  "allowed_repositories": ["o/r"]}
        with patch.object(host.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, json.dumps(report))):
            self.assertEqual(host.preflight("CURSOR", policy, MagicMock())["model"], "grok-4-cursor-high")

    def test_launch_refuses_a_packet_for_another_lane(self):
        packet = {**WRITER, "builder_id": "GLM"}
        with patch("sys.stdin", io.StringIO(json.dumps(packet))), \
             patch.object(self.adapter, "prerequisites", side_effect=AssertionError("checked a foreign packet")), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.adapter.launch()
        self.assertIn("not a CURSOR launch packet", json.loads(stdout.getvalue())["reason"])

    def job(self, prompt="Review PR #7 at head bbbb. Run /usr/bin/python3 -I /s/signer.py ..."):
        session = self.root / "session"
        session.mkdir(mode=0o700)
        worktree = self.root / "wt"
        worktree.mkdir(mode=0o700)
        (session / "prompt.txt").write_text(prompt)
        return {"launch_request_id": "a" * 24, "repository": "o/r", "worktree": str(worktree),
                "session_dir": str(session), "prompt_file": str(session / "prompt.txt"),
                "model": "grok-4-cursor-high"}

    def fake_git(self, code=0):
        git = self.root / "git"
        git.write_text(f"#!/bin/sh\nexit {code}\n")
        git.chmod(0o755)
        return str(git)

    def run_supervisor(self, job, git_code=0):
        with patch.object(self.supervisor, "GIT", self.fake_git(git_code)):
            self.assertEqual(self.supervisor.supervise(job, str(self.cli), {"PATH": "/usr/bin:/bin"}), 0)
        session = Path(job["session_dir"])
        return (json.loads((session / "result.json").read_text()),
                json.loads((session / "status.json").read_text()))

    def reset_job_dirs(self):
        for name in ("session", "wt"):
            for item in (self.root / name).glob("*"):
                item.unlink()
            (self.root / name).rmdir()

    def test_supervisor_confirms_the_chat_and_runs_the_agent_on_it(self):
        job = self.job()
        result, status = self.run_supervisor(job)
        self.assertEqual(result, {"outcome": "CONFIRMED", "session_id": "cursor-cli:chat-abc123"})
        self.assertEqual((status["state"], status["returncode"]), ("EXITED", 0))
        run = self.calls()[-1]["args"]
        self.assertEqual(run[run.index("--resume") + 1], "chat-abc123")
        self.assertEqual(run[run.index("--model") + 1], "grok-4-cursor-high")
        self.assertEqual(run[run.index("--workspace") + 1], job["worktree"])
        self.assertEqual(run[-1], Path(job["prompt_file"]).read_text())
        for name in ("result.json", "status.json"):
            self.assertEqual((Path(job["session_dir"]) / name).stat().st_mode & 0o777, 0o600)

    def test_supervisor_outcomes_before_and_after_the_provider_chat(self):
        result, _ = self.run_supervisor(self.job(), git_code=1)
        self.assertEqual(result["outcome"], "FAILED_PRESTART")  # no provider session can exist yet
        self.assertEqual(self.calls(), [])
        self.reset_job_dirs()
        job = self.job()
        Path(job["prompt_file"]).unlink()
        result, _ = self.run_supervisor(job)
        self.assertEqual(result["outcome"], "FAILED_PRESTART")  # the prompt is read before any chat exists
        self.assertEqual(self.calls(), [])
        for mode in ("chat-fails", "bad-chat"):
            with self.subTest(mode=mode):
                (self.root / "mode").write_text(mode)
                self.reset_job_dirs()
                result, _ = self.run_supervisor(self.job())
                self.assertEqual(result["outcome"], "UNKNOWN")  # a provider session may exist

    def test_setup_finishes_inside_the_adapter_wait(self):
        self.assertLess(self.supervisor.CLONE_TIMEOUT + self.supervisor.CHAT_TIMEOUT, self.adapter.RESULT_WAIT)
        text = (CURSOR / "astra-cursor-supervisor").read_text()
        self.assertIn('"--filter=blob:none"', text)

    def test_no_launch_secret_reaches_any_process_argument(self):
        packet = {**REVIEWER, "builder_id": "CURSOR"}
        session = self.root / "sig"
        session.mkdir(mode=0o700)
        signer = self.adapter.write_signer(session, packet)
        job = self.job(prompt=self.adapter.build_prompt(packet, signer))
        self.run_supervisor(job)
        arguments = json.dumps([call["args"] for call in self.calls()])
        self.assertNotIn(packet["review_nonce"], arguments)
        self.assertIn(str(signer), arguments)  # only the signer's path travels in the prompt


if __name__ == "__main__":
    unittest.main()
