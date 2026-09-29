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
from unittest.mock import patch

import control_plane_host as host

ADAPTERS = Path(__file__).resolve().parents[1] / "adapters" / "imported"
BASE = {"repository": "o/r", "task_id": "T-1", "task_revision": "r1", "builder_id": "GLM",
        "launch_request_id": "a" * 24, "attempt_id": 1}


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ADAPTERS / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


NAMES = ("astra-devin-adapter", "astra-grok-adapter", "astra-glm-adapter")
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
                self.assertIn(f"/usr/bin/python3 -I {signer} <PASS|PASS_WITH_NOTES|FAIL|DECISION_REQUIRED>", prompt)
                self.assertNotIn("d" * 32, prompt)  # the key stays in the signer file
                self.assertNotIn("ASTRA_DELIVERY_V1", prompt)

    def test_writer_prompt_carries_the_signed_delivery_protocol(self):
        for name in NAMES:
            with self.subTest(adapter=name):
                adapter = load(name)
                signer = adapter.write_signer(self.session(name, "writer"), WRITER)
                prompt = adapter.build_prompt(WRITER, signer)
                self.assertIn(f"/usr/bin/python3 -I {signer} <pr number> <40-hex head sha>", prompt)
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
                line = run_signer(review_signer, "FAIL", "A2", "YES").stdout.strip()
                self.assertEqual(host.verify_pin(host_row(REVIEWER), line),
                                 {"kind": "REVIEW", "review": "c" * 24, "head": "b" * 40, "verdict": "FAIL",
                                  "depth": "A2", "contract_change": "YES"})
                self.assertNotEqual(run_signer(review_signer, "MAYBE", "A1", "NO").returncode, 0)
                delivery_signer = adapter.write_signer(self.session(name, "w"), WRITER)
                line = run_signer(delivery_signer, "12", "f" * 40).stdout.strip()
                self.assertEqual(host.verify_pin(host_row(WRITER), line), {"kind": "DELIVERY", "pr": 12, "head": "f" * 40})
                self.assertNotEqual(run_signer(delivery_signer, "12", "HEAD").returncode, 0)
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
                if hasattr(adapter, "cli_auth_ok"):
                    auth = patch.object(adapter, "cli_auth_ok", return_value=True)
                    auth.start()
                    self.addCleanup(auth.stop)
                with patch.object(adapter, "STATE_ROOT", root / "state"), \
                     patch.object(adapter, "WORKTREE_ROOT", root), \
                     patch.object(adapter, "prerequisites", return_value=None), \
                     patch.object(adapter.subprocess, "Popen", side_effect=AssertionError("supervisor started")), \
                     patch("sys.stdin", io.StringIO(json.dumps(packet))), \
                     patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    adapter.launch()
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["outcome"], "FAILED_PRESTART")
                self.assertIn("review_nonce", result["reason"])

    def test_recorded_digests_match_the_reviewed_sources(self):
        for line in (ADAPTERS / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split()
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ADAPTERS / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
