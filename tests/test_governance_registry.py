"""Structural checks for the governance registries; no network access."""
import json
import os
import pathlib
import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML explicitly
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOT_STATUSES = {"ACTIVE", "CREATED_NOT_VALIDATED", "PLANNED", "DISABLED", "DEPRECATED"}
AUDIT_VALUES = {"정상", "수정필요", "통합후보", "오류"}
# A level or ascending range from APPROVAL_MATRIX (L0-L3), optionally scoped by a qualifier.
APPROVAL_RE = re.compile(r"L([0-3])(?:-L([0-3]))?(?:-[a-z]+)?")
REPO_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def load_yaml(relative):
    if yaml is None:
        if os.environ.get("CI"):
            raise AssertionError("PyYAML is required in CI")
        raise unittest.SkipTest("PyYAML not installed")
    with open(ROOT / relative, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class BotRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_yaml("bots/registry.yaml")
        self.bots = self.registry["bots"]
        self.names = [bot["name"] for bot in self.bots]

    def test_identities_are_unique(self):
        ids = [bot["bot_id"] for bot in self.bots]
        for values in (ids, self.names):
            self.assertTrue(all(isinstance(v, str) and v for v in values))
            self.assertEqual(len(values), len(set(values)))
        channels = [channel["name"] for channel in self.registry["channels"]]
        self.assertEqual(len(channels), len(set(channels)))
        self.assertFalse(set(channels) & set(self.names), "a channel is not a bot")

    def test_status_audit_and_onedrive_values(self):
        for bot in self.bots:
            with self.subTest(bot=bot["name"]):
                self.assertIn(bot["status"], BOT_STATUSES)
                self.assertIn(bot["audit"], AUDIT_VALUES)
                # SYSTEM_CONSTITUTION hard rule 2: OneDrive is READ ONLY for every bot.
                self.assertEqual(bot["onedrive"], "READ_ONLY")

    def test_approval_levels_use_matrix_levels(self):
        for bot in self.bots:
            with self.subTest(bot=bot["name"], level=bot["approval_level"]):
                match = APPROVAL_RE.fullmatch(bot["approval_level"])
                self.assertIsNotNone(match)
                low, high = match.group(1), match.group(2)
                self.assertTrue(high is None or int(low) < int(high))

    def test_parents_and_handoffs_resolve_without_cycles(self):
        by_name = {bot["name"]: bot for bot in self.bots}
        for bot in self.bots:
            with self.subTest(bot=bot["name"]):
                self.assertTrue(bot["parent"] is None or bot["parent"] in by_name)
                for target in bot.get("handoff_targets") or []:
                    self.assertIn(target, by_name)
                seen, current = set(), bot
                while current["parent"] is not None:
                    self.assertNotIn(current["name"], seen, "parent cycle")
                    seen.add(current["name"])
                    current = by_name[current["parent"]]
        self.assertEqual(sum(bot["parent"] is None for bot in self.bots), 1)

    def test_repositories_and_central_targets_are_registered(self):
        repos = set()
        for bot in self.bots:
            if "repo" in bot:
                with self.subTest(bot=bot["name"]):
                    self.assertRegex(bot["repo"], REPO_RE)
                repos.add(bot["repo"])
        profiles = json.loads((ROOT / "engineering/.github/control-plane/projects.json").read_text())
        for target in profiles:
            with self.subTest(target=target):
                self.assertIn(f"https://github.com/{target}", repos)


class RoutineRegistryTests(unittest.TestCase):
    def test_routines_have_unique_ids_and_known_owners(self):
        names = {bot["name"] for bot in load_yaml("bots/registry.yaml")["bots"]}
        routines = load_yaml("routines/registry.yaml")["routines"]
        ids = [routine["id"] for routine in routines]
        self.assertEqual(len(ids), len(set(ids)))
        for routine in routines:
            with self.subTest(routine=routine["id"]):
                self.assertIn(routine["owner"], names)
                self.assertIn(routine["status"], BOT_STATUSES)
                self.assertTrue(isinstance(routine["schedule"], str) and routine["schedule"].strip())


class TaskSchemaTests(unittest.TestCase):
    def test_schema_is_an_object_schema_with_declared_required_fields(self):
        schema = json.loads((ROOT / "schemas/task.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["type"], "object")
        self.assertTrue(schema["required"])
        self.assertLessEqual(set(schema["required"]), set(schema["properties"]))


if __name__ == "__main__":
    unittest.main()
