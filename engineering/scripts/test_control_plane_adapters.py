"""Imported host adapters: program-mode prompts and recorded digests (no host needed)."""
import hashlib
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

ADAPTERS = Path(__file__).resolve().parents[1] / "adapters" / "imported"
BASE = {"repository": "o/r", "task_id": "T-1", "task_revision": "r1", "builder_id": "GLM",
        "launch_request_id": "a" * 24, "attempt_id": 1}


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ADAPTERS / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class AdapterPromptTests(unittest.TestCase):
    def test_reviewer_prompt_is_read_only_and_binds_the_verdict(self):
        for name in ("astra-devin-adapter", "astra-grok-adapter", "astra-glm-adapter"):
            with self.subTest(adapter=name):
                prompt = load(name).build_prompt({**BASE, "schema_version": 2, "role": "REVIEWER",
                                                  "pr_number": 7, "head_sha": "b" * 40,
                                                  "review_request_id": "c" * 24, "owner_lane": "DEVIN"})
                self.assertIn("read-only", prompt)
                self.assertIn("Do not commit, push", prompt)
                self.assertIn(f"ASTRA_REVIEW_V1 review={'c' * 24} head={'b' * 40}", prompt)
                self.assertNotIn("ASTRA_DELIVERY_V1", prompt)

    def test_writer_prompt_carries_the_delivery_protocol(self):
        for name in ("astra-devin-adapter", "astra-grok-adapter", "astra-glm-adapter"):
            for packet in ({**BASE, "schema_version": 2, "role": "WRITER"}, {**BASE, "schema_version": 1}):
                with self.subTest(adapter=name, schema=packet["schema_version"]):
                    prompt = load(name).build_prompt(packet)
                    self.assertIn("ASTRA_DELIVERY_V1 pr=<number> head=<40-hex head sha>", prompt)
                    self.assertIn("Never merge", prompt)

    def test_recorded_digests_match_the_reviewed_sources(self):
        for line in (ADAPTERS / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split()
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ADAPTERS / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
