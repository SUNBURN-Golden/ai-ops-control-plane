import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_plane_inspect_core as core  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / ".github" / "control-plane" / "inspector.example.json"
ZW = "​"
GH_PAT = "github_pat_" + "A1b2C3d4E5" * 6
SLACK_TOKEN = "xoxb-1234567890-abcdefghij-KLMNOPQRST"
NOW = datetime(2026, 10, 1, 5, 17, 0, tzinfo=timezone.utc)


def valid_config():
    cfg = json.loads(EXAMPLE.read_text())
    cfg["ledger_issue"] = 101
    cfg["test_ledger_issue"] = 102
    cfg["slack"] = {"team_id": "T0ABCDEF12", "bot_user_id": "U0ABCDEF12",
                    "channel_id": "C0ABCDEF12", "test_channel_id": "C0ABCDEF13"}
    return cfg


def not_root_owned(path):
    """Make ``path`` owned by someone other than root (only possible as root; otherwise it already is)."""
    if os.geteuid() == 0:
        os.chown(str(path), 65534, -1)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.paths = core.Paths.from_root(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, path, data, mode=0o600):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.is_file():
            path.unlink()  # a non-root CI user cannot rewrite a 0400 file in place
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.chmod(str(path), mode)
        return path


class ErrorAndPathsTests(Base):
    def test_inspect_error_str_is_reason(self):
        err = core.InspectError("CONFIG", "detail text")
        self.assertEqual(str(err), "CONFIG")
        self.assertEqual(err.reason, "CONFIG")
        self.assertEqual(err.detail, "detail text")
        self.assertEqual(core.InspectError("bad code").reason, "INTERNAL")

    def test_paths_derive_from_root(self):
        p = self.paths
        self.assertEqual(p.config, self.tmp / "etc/aiops/inspect.json")
        self.assertEqual(p.gh_read, self.tmp / "etc/aiops/inspect-gh-read")
        self.assertEqual(p.gh_ledger, self.tmp / "etc/aiops/inspect-gh-ledger")
        self.assertEqual(p.slack, self.tmp / "etc/aiops/inspect-slack-token")
        self.assertEqual(p.kill, self.tmp / "etc/aiops/inspect-disabled")
        self.assertEqual(p.manifest, self.tmp / "etc/aiops/inspect-expected.sha256")
        self.assertEqual(p.state, self.tmp / "var/lib/aiops-inspect/state")
        self.assertEqual(p.runs, self.tmp / "var/lib/aiops-inspect/runs")
        self.assertEqual(p.lib, self.tmp / "opt/aiops/inspect/lib")
        self.assertEqual(p.render, self.tmp / "var/lib/aiops-plot")
        self.assertEqual(p.tool_bin, self.tmp / "opt/aiops/bin/aiops-inspect")
        self.assertEqual(core.Paths.from_root().config, Path("/etc/aiops/inspect.json"))
        self.assertEqual(p.secret("slack"), p.slack)
        with self.assertRaises(core.InspectError):
            p.secret("openai")


class ConfigTests(Base):
    def check_bad(self, mutate, allow_placeholders=False):
        cfg = valid_config()
        mutate(cfg)
        with self.assertRaises(core.InspectError) as ctx:
            core.validate_config(cfg, allow_placeholders=allow_placeholders)
        self.assertEqual(ctx.exception.reason, "CONFIG")

    def test_valid_config_passes_and_fills_defaults(self):
        cfg = valid_config()
        del cfg["thresholds"], cfg["contract_pairs"], cfg["test_path_regex"]
        out = core.validate_config(cfg)
        self.assertEqual(out["thresholds"], {})
        self.assertEqual(out["contract_pairs"], [])
        self.assertEqual(out["test_path_regex"], core.DEFAULT_TEST_PATH_REGEX)
        self.assertEqual(core.active_ledger_issue(out), 102)
        self.assertEqual(core.active_channel(out), "C0ABCDEF13")
        out["stage"] = "LIVE"
        self.assertEqual(core.active_ledger_issue(out), 101)
        self.assertEqual(core.active_channel(out), "C0ABCDEF12")

    def test_example_validates_only_with_placeholders(self):
        example = json.loads(EXAMPLE.read_text())
        self.assertEqual(example["stage"], "DRY")
        with self.assertRaises(core.InspectError):
            core.validate_config(example)
        out = core.validate_config(example, allow_placeholders=True)
        self.assertEqual([t["prefix"] for t in out["targets"]], ["KIXP", "KIXC", "ZARI", "FILM", "MAEU"])

    def test_placeholders_refused_in_live(self):
        example = json.loads(EXAMPLE.read_text())
        example["stage"] = "LIVE"
        with self.assertRaises(core.InspectError):
            core.validate_config(example, allow_placeholders=True)

    def test_each_placeholder_alone_is_refused(self):
        self.check_bad(lambda c: c.__setitem__("ledger_issue", 0))
        self.check_bad(lambda c: c.__setitem__("test_ledger_issue", 0))
        for key in core.SLACK_KEYS:
            letter = {"team_id": "T", "bot_user_id": "U", "channel_id": "C", "test_channel_id": "G"}[key]
            self.check_bad(lambda c, k=key, l=letter: c["slack"].__setitem__(k, l + "00000000"))

    def test_unknown_and_missing_keys(self):
        self.check_bad(lambda c: c.__setitem__("extra", 1))
        self.check_bad(lambda c: c.pop("user_login"))
        self.check_bad(lambda c: c["slack"].__setitem__("extra", "x"))
        self.check_bad(lambda c: c["slack"].pop("team_id"))
        self.check_bad(lambda c: c["targets"][0].__setitem__("lane", "DEVIN"))
        self.check_bad(lambda c: c.__setitem__("thresholds", {"orphan_watchx": 1}))
        for bad in ("not an object", [], None):
            with self.assertRaises(core.InspectError):
                core.validate_config(bad)

    def test_schema_and_stage(self):
        self.check_bad(lambda c: c.__setitem__("schema", "AIOPS_INSPECT_CONFIG_V2"))
        self.check_bad(lambda c: c.__setitem__("stage", "dry"))

    def test_repositories(self):
        self.check_bad(lambda c: c.__setitem__("control_repository", "no-slash"))
        self.check_bad(lambda c: c["targets"][0].__setitem__("repository", "a/b/c"))
        self.check_bad(lambda c: c["targets"][0].__setitem__("repository", "a b/c"))
        self.check_bad(lambda c: c["targets"][1].__setitem__("repository", c["targets"][0]["repository"].upper()))
        self.check_bad(lambda c: c["targets"][0].__setitem__("repository", c["control_repository"]))
        self.check_bad(lambda c: c.__setitem__("targets", []))
        self.check_bad(lambda c: c.__setitem__("targets", {}))

    def test_prefixes(self):
        self.check_bad(lambda c: c["targets"][0].__setitem__("prefix", "kixp"))
        self.check_bad(lambda c: c["targets"][0].__setitem__("prefix", "KIX"))
        self.check_bad(lambda c: c["targets"][0].__setitem__("prefix", "KIXPP"))
        self.check_bad(lambda c: c["targets"][0].__setitem__("prefix", "CTRL"))
        self.check_bad(lambda c: c["targets"][1].__setitem__("prefix", c["targets"][0]["prefix"]))

    def test_slack_ids(self):
        self.check_bad(lambda c: c["slack"].__setitem__("team_id", "X0ABCDEF12"))
        self.check_bad(lambda c: c["slack"].__setitem__("team_id", "T0ABC"))
        self.check_bad(lambda c: c["slack"].__setitem__("bot_user_id", "B0ABCDEF12"))
        self.check_bad(lambda c: c["slack"].__setitem__("channel_id", "D0ABCDEF12"))
        self.check_bad(lambda c: c["slack"].__setitem__("channel_id", "c0abcdef12"))
        self.check_bad(lambda c: c["slack"].__setitem__("test_channel_id", c["slack"]["channel_id"]))
        cfg = valid_config()
        cfg["slack"]["bot_user_id"] = "W0ABCDEF12"
        cfg["slack"]["test_channel_id"] = "G0ABCDEF12"
        core.validate_config(cfg)

    def test_integers(self):
        for key, bad in (("tick_minute", 60), ("tick_minute", -1), ("tick_minute", True), ("tick_minute", 1.0),
                         ("daily_hour_kst", 24), ("deadman_hours", 1), ("deadman_hours", 73),
                         ("ledger_issue", -5), ("ledger_issue", "7"), ("ledger_issue", 10 ** 10)):
            self.check_bad(lambda c, k=key, v=bad: c.__setitem__(k, v))
        self.check_bad(lambda c: c.__setitem__("test_ledger_issue", c["ledger_issue"]))
        self.check_bad(lambda c: c.__setitem__("thresholds", {"orphan_watch": 0}))
        self.check_bad(lambda c: c.__setitem__("thresholds", {"orphan_watch": True}))
        cfg = valid_config()
        cfg["thresholds"] = {"orphan_watch": 2, "blocked_watch_h": 48}
        out = core.validate_config(cfg)
        merged = core.thresholds_of(out)
        self.assertEqual(merged["orphan_watch"], 2)
        self.assertEqual(merged["done_gap_at_risk_d"], 7)

    def test_strings(self):
        self.check_bad(lambda c: c.__setitem__("user_login", "-bad"))
        self.check_bad(lambda c: c.__setitem__("user_login", "a@b"))
        self.check_bad(lambda c: c.__setitem__("host_helper", "astra-host-control"))
        self.check_bad(lambda c: c.__setitem__("host_helper", "/opt/../bin/sh"))
        self.check_bad(lambda c: c.__setitem__("host_helper", "/opt/astra/bin/x;rm"))
        self.check_bad(lambda c: c.__setitem__("ledger_account", "Root"))
        self.check_bad(lambda c: c.__setitem__("ledger_account", "root"))
        self.check_bad(lambda c: c.__setitem__("render_account", c["ledger_account"]))
        self.check_bad(lambda c: c.__setitem__("test_path_regex", "(unclosed"))
        self.check_bad(lambda c: c.__setitem__("test_path_regex", ""))
        self.check_bad(lambda c: c.__setitem__("test_path_regex", 5))

    def test_contract_pairs(self):
        good = {"a": {"repository": "BeautifulMind-JT/kix-protocol", "path": "spec/api.json"},
                "b": {"repository": "BeautifulMind-JT/ZARI", "path": "src/api/types.ts"}}
        cfg = valid_config()
        cfg["contract_pairs"] = [good]
        core.validate_config(cfg)
        for mutate in (
                lambda p: p["a"].__setitem__("repository", "Other/repo"),
                lambda p: p["a"].__setitem__("path", "/etc/passwd"),
                lambda p: p["a"].__setitem__("path", "spec/../../x"),
                lambda p: p["a"].__setitem__("path", ""),
                lambda p: p["b"].__setitem__("extra", 1),
                lambda p: p.pop("b"),
                lambda p: p.__setitem__("b", copy.deepcopy(p["a"]))):
            pair = copy.deepcopy(good)
            mutate(pair)
            self.check_bad(lambda c, p=pair: c.__setitem__("contract_pairs", [p]))
        self.check_bad(lambda c: c.__setitem__("contract_pairs", {}))

    def test_load_config_file_checks(self):
        cfg = valid_config()
        path = self.put(self.paths.config, json.dumps(cfg))
        self.assertEqual(core.load_config(path, require_root_owner=False)["stage"], "DRY")
        os.chmod(str(path), 0o644)
        with self.assertRaises(core.InspectError):
            core.load_config(path, require_root_owner=False)
        os.chmod(str(path), 0o600)
        not_root_owned(path)
        with self.assertRaises(core.InspectError):
            core.load_config(path, require_root_owner=True)
        dup = self.put(self.tmp / "dup.json", '{"schema": "x", "schema": "y"}')
        with self.assertRaises(core.InspectError):
            core.load_config(dup, require_root_owner=False)
        bad = self.put(self.tmp / "bad.json", "{not json")
        with self.assertRaises(core.InspectError):
            core.load_config(bad, require_root_owner=False)
        link = self.tmp / "link.json"
        os.symlink(str(path), str(link))
        with self.assertRaises(core.InspectError):
            core.load_config(link, require_root_owner=False)
        with self.assertRaises(core.InspectError):
            core.load_config(self.tmp / "missing.json", require_root_owner=False)
        example = self.put(self.tmp / "example.json", EXAMPLE.read_bytes())
        with self.assertRaises(core.InspectError):
            core.load_config(example, require_root_owner=False)
        core.load_config(example, allow_placeholders=True, require_root_owner=False)


class SecretTests(Base):
    def test_valid_secrets(self):
        self.put(self.paths.gh_read, GH_PAT + "\n")
        self.put(self.paths.gh_ledger, "  " + GH_PAT + "  \n")
        self.put(self.paths.slack, SLACK_TOKEN)
        loaded = core.load_secrets(self.paths, require_root_owner=False)
        self.assertEqual(loaded, {"gh-read": GH_PAT, "gh-ledger": GH_PAT, "slack": SLACK_TOKEN})

    def assert_refused(self, path, kind, reason, require_root_owner=False):
        with self.assertRaises(core.InspectError) as ctx:
            core.load_secret(path, kind, require_root_owner=require_root_owner)
        err = ctx.exception
        self.assertEqual(err.reason, reason)
        for value in (GH_PAT, SLACK_TOKEN, "ghp_" + "x" * 36):
            self.assertNotIn(value, str(err) + err.detail + repr(err.args))

    def test_shapes(self):
        path = self.put(self.paths.gh_read, "ghp_" + "x" * 36)
        self.assert_refused(path, "gh-read", "SECRET_GH_READ")
        path = self.put(self.paths.gh_ledger, "github_pat_short")
        self.assert_refused(path, "gh-ledger", "SECRET_GH_LEDGER")
        path = self.put(self.paths.slack, "xoxp-1234567890-abcdefghij-KLMNOPQRST")
        self.assert_refused(path, "slack", "SECRET_SLACK")
        path = self.put(self.paths.slack, GH_PAT)
        self.assert_refused(path, "slack", "SECRET_SLACK")
        path = self.put(self.paths.gh_read, GH_PAT + "\n" + GH_PAT)
        self.assert_refused(path, "gh-read", "SECRET_GH_READ")
        path = self.put(self.paths.gh_read, b"\xff\xfe")
        self.assert_refused(path, "gh-read", "SECRET_GH_READ")

    def test_mode_owner_type(self):
        for mode in (0o644, 0o640, 0o400, 0o700):
            path = self.put(self.paths.gh_read, GH_PAT, mode=mode)
            self.assert_refused(path, "gh-read", "SECRET_GH_READ")
        path = self.put(self.paths.gh_read, GH_PAT)
        not_root_owned(path)
        self.assert_refused(path, "gh-read", "SECRET_GH_READ", require_root_owner=True)
        real = self.put(self.tmp / "real", GH_PAT)
        os.unlink(str(path))
        os.symlink(str(real), str(path))
        self.assert_refused(path, "gh-read", "SECRET_GH_READ")
        os.unlink(str(path))
        os.mkdir(str(path), 0o700)
        self.assert_refused(path, "gh-read", "SECRET_GH_READ")
        self.assert_refused(self.paths.slack, "slack", "SECRET_SLACK")
        with self.assertRaises(core.InspectError) as ctx:
            core.load_secret(real, "openai", require_root_owner=False)
        self.assertEqual(ctx.exception.reason, "SECRET_KIND")

    def test_write_secret(self):
        self.paths.etc.mkdir(parents=True)
        digest = core.write_secret(self.paths.slack, "slack", SLACK_TOKEN + "\n")
        self.assertEqual(digest, core.sha256_hex(SLACK_TOKEN.encode())[:8])
        self.assertEqual(stat.S_IMODE(os.stat(str(self.paths.slack)).st_mode), 0o600)
        self.assertEqual(core.load_secret(self.paths.slack, "slack", require_root_owner=False), SLACK_TOKEN)
        with self.assertRaises(core.InspectError):
            core.write_secret(self.paths.gh_read, "gh-read", "ghp_" + "x" * 36)
        self.assertFalse(self.paths.gh_read.exists())


class StateStoreTests(Base):
    def setUp(self):
        super().setUp()
        self.store = core.StateStore(self.paths.state)

    def test_dir_mode_and_roundtrip(self):
        self.assertEqual(stat.S_IMODE(os.stat(str(self.paths.state)).st_mode), 0o700)
        self.assertEqual(self.store.read("facts", {"none": True}), {"none": True})
        self.store.write("facts", {"b": 1, "a": "한국어"})
        self.assertEqual(self.store.read("facts"), {"b": 1, "a": "한국어"})
        self.assertEqual(self.store.read("facts.json"), {"b": 1, "a": "한국어"})
        path = self.paths.state / "facts.json"
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o600)
        self.assertEqual([p.name for p in self.paths.state.iterdir()], ["facts.json"])
        self.store.write("publish/20261001T051700Z-0a1b2c3d-tick", {"step": "PREPARED"})
        self.assertEqual(self.store.read("publish/20261001T051700Z-0a1b2c3d-tick"), {"step": "PREPARED"})

    def test_names_are_confined(self):
        for name in ("../x", "/etc/passwd", "a/../../b", "", ".hidden", "x.pending", "a/b/c/d", "a b", 5):
            with self.assertRaises(core.InspectError):
                self.store.write(name, {})

    def test_corrupt_and_types(self):
        (self.paths.state / "bad.json").write_text("{nope")
        with self.assertRaises(core.InspectError) as ctx:
            self.store.read("bad")
        self.assertEqual(ctx.exception.reason, "STATE_CORRUPT")
        os.symlink("/etc/hostname", str(self.paths.state / "link.json"))
        with self.assertRaises(core.InspectError):
            self.store.read("link")
        with self.assertRaises(core.InspectError) as ctx:
            self.store.write("x", {"v": float("nan")})
        self.assertEqual(ctx.exception.reason, "STATE_TYPE")
        with self.assertRaises(core.InspectError):
            self.store.write("x", {"v": object()})

    def test_size_cap(self):
        old = core.MAX_STATE_BYTES
        core.MAX_STATE_BYTES = 64
        try:
            with self.assertRaises(core.InspectError) as ctx:
                self.store.write("big", {"v": "x" * 100})
            self.assertEqual(ctx.exception.reason, "STATE_SIZE")
            self.assertFalse((self.paths.state / "big.json").exists())
            self.store.append_jsonl("h", {"v": "x" * 30})
            with self.assertRaises(core.InspectError):
                self.store.append_jsonl("h", {"v": "x" * 30})
            self.assertEqual(len(self.store.read_jsonl("h")), 1)
        finally:
            core.MAX_STATE_BYTES = old

    def test_write_failure_keeps_old_file_and_leaves_no_tmp(self):
        self.store.write("facts", {"v": 1})
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("disk full")

        core.os.replace = boom
        try:
            with self.assertRaises(OSError):
                self.store.write("facts", {"v": 2})
        finally:
            core.os.replace = real_replace
        self.assertEqual(self.store.read("facts"), {"v": 1})
        self.assertEqual(sorted(p.name for p in self.paths.state.iterdir()), ["facts.json"])

    def test_jsonl(self):
        self.store.append_jsonl("history", {"t": "a"})
        self.store.append_jsonl("history.jsonl", {"t": "b"})
        with open(str(self.paths.state / "history.jsonl"), "ab") as handle:
            handle.write(b'{"t": "tor')
        self.assertEqual(self.store.read_jsonl("history"), [{"t": "a"}, {"t": "b"}])
        self.store.write_jsonl("history", [{"t": "b"}])
        self.assertEqual(self.store.read_jsonl("history"), [{"t": "b"}])
        self.assertEqual(self.store.read_jsonl("missing"), [])
        self.assertEqual(stat.S_IMODE(os.stat(str(self.paths.state / "history.jsonl")).st_mode), 0o600)

    def test_stage_commit_discard(self):
        self.store.write("etags", {"v": "old"})
        self.store.stage("etags", {"v": "new"})
        self.store.stage("facts", {"f": 1})
        self.assertEqual(self.store.read("etags"), {"v": "old"})
        self.assertIsNone(self.store.read("facts"))
        self.assertEqual(self.store.read_staged("etags"), {"v": "new"})
        self.assertTrue((self.paths.state / "etags.pending.json").exists())
        committed = self.store.commit_staged(["etags", "facts", "hashes"])
        self.assertEqual(committed, ["etags", "facts"])
        self.assertEqual(self.store.read("etags"), {"v": "new"})
        self.assertEqual(self.store.read("facts"), {"f": 1})
        self.assertFalse((self.paths.state / "etags.pending.json").exists())
        self.store.stage("etags", {"v": "newer"})
        self.store.discard_staged(["etags", "never"])
        self.assertEqual(self.store.read("etags"), {"v": "new"})
        self.assertFalse((self.paths.state / "etags.pending.json").exists())

    def test_commit_is_all_or_nothing_on_corrupt_pending(self):
        self.store.write("a", {"v": 1})
        self.store.stage("a", {"v": 2})
        (self.paths.state / "b.pending.json").write_text("{broken")
        with self.assertRaises(core.InspectError):
            self.store.commit_staged(["a", "b"])
        self.assertEqual(self.store.read("a"), {"v": 1})
        self.assertTrue((self.paths.state / "a.pending.json").exists())


class KillSwitchTests(Base):
    def test_write_read_clear(self):
        self.assertIsNone(core.kill_state(self.paths))
        record = core.write_kill(self.paths, "TOOL_TAMPERED", "20261001T051700Z-0a1b2c3d-tick", NOW)
        self.assertEqual(record, {"reason": "TOOL_TAMPERED", "run": "20261001T051700Z-0a1b2c3d-tick",
                                  "at": "2026-10-01T05:17:00Z"})
        self.assertEqual(stat.S_IMODE(os.stat(str(self.paths.kill)).st_mode), 0o644)
        self.assertEqual(core.kill_state(self.paths), record)
        again = core.write_kill(self.paths, "SECRET_LIVE", None, NOW + timedelta(hours=1))
        self.assertEqual(again["reason"], "TOOL_TAMPERED")
        self.assertTrue(core.clear_kill(self.paths))
        self.assertIsNone(core.kill_state(self.paths))
        self.assertFalse(core.clear_kill(self.paths))

    def test_unreadable_switch_still_halts(self):
        self.put(self.paths.kill, "garbage", mode=0o644)
        self.assertEqual(core.kill_state(self.paths)["reason"], "KILL_UNREADABLE")
        os.unlink(str(self.paths.kill))
        os.symlink("/nonexistent", str(self.paths.kill))
        self.assertEqual(core.kill_state(self.paths)["reason"], "KILL_UNREADABLE")


class ManifestTests(Base):
    def setUp(self):
        super().setUp()
        self.files = core.own_files(self.paths)
        for index, path in enumerate(self.files):
            self.put(path, f"# tool file {index}\n", mode=0o644)
        self.write_manifest(self.files)

    def write_manifest(self, files, extra=""):
        lines = [f"{core.file_sha256(p)}  {p}" for p in files]
        self.put(self.paths.manifest, "\n".join(lines) + "\n" + extra, mode=0o644)

    def verify(self, required=None):
        return core.verify_manifest(self.paths, self.files if required is None else required,
                                    require_root_owner=False)

    def assert_tampered(self, required=None, require_root_owner=False):
        with self.assertRaises(core.InspectError) as ctx:
            core.verify_manifest(self.paths, self.files if required is None else required,
                                 require_root_owner=require_root_owner)
        self.assertEqual(ctx.exception.reason, "TOOL_TAMPERED")

    def test_own_files(self):
        names = [p.name for p in self.files]
        self.assertEqual(len(names), 11)
        self.assertIn("control_plane_inspect_core.py", names)
        self.assertIn("control_plane_program.py", names)
        self.assertIn("control_plane.py", names)
        self.assertEqual(self.files[-1], self.tmp / "opt/aiops/bin/aiops-inspect")
        self.assertTrue(all(p.parent == self.paths.lib for p in self.files[:-1]))

    def test_ok(self):
        result = self.verify()
        self.assertEqual(len(result["files"]), 11)
        self.assertRegex(result["tool_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(result, self.verify())

    def test_modified_file(self):
        with open(str(self.files[3]), "a") as handle:
            handle.write("tampered = True\n")
        self.assert_tampered()

    def test_missing_listed_file(self):
        os.unlink(str(self.files[0]))
        self.assert_tampered()

    def test_required_not_listed(self):
        self.write_manifest(self.files[1:])
        self.assert_tampered()

    def test_extra_listed_file_missing(self):
        self.write_manifest(self.files, extra=f"{'0' * 64}  {self.tmp}/opt/aiops/inspect/lib/extra.py\n")
        self.assert_tampered()

    def test_malformed_and_conflicting_lines(self):
        self.write_manifest(self.files, extra="not a manifest line\n")
        self.assert_tampered()
        self.write_manifest(self.files, extra=f"{'a' * 64}  {self.files[0]}\n")
        self.assert_tampered()
        self.write_manifest(self.files, extra=f"{'A' * 64}  {self.files[0]}\n")
        self.assert_tampered()
        self.write_manifest(self.files, extra=f"{core.file_sha256(self.files[0])} {self.files[0]}\n")
        self.assert_tampered()
        self.write_manifest(self.files, extra=f"{core.file_sha256(self.files[0])}  relative/path.py\n")
        self.assert_tampered()

    def test_missing_or_empty_manifest(self):
        self.put(self.paths.manifest, "", mode=0o644)
        self.assert_tampered()
        os.unlink(str(self.paths.manifest))
        self.assert_tampered()

    def test_symlinked_file_refused(self):
        target = self.put(self.tmp / "elsewhere.py", "# tool file 0\n", mode=0o644)
        os.unlink(str(self.files[0]))
        os.symlink(str(target), str(self.files[0]))
        self.assert_tampered()

    def test_root_owner_required(self):
        if os.geteuid() == 0:
            not_root_owned(self.paths.manifest)
        self.assert_tampered(require_root_owner=True)
        self.write_manifest(self.files)
        os.chmod(str(self.files[0]), 0o666)
        self.assert_tampered(require_root_owner=True)


class CanonTimeRunTests(unittest.TestCase):
    def test_canon(self):
        self.assertEqual(core.canon({"b": 1, "a": ["한", 2]}), '{"a":["한",2],"b":1}'.encode("utf-8"))
        self.assertEqual(core.sha256_hex(b"abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        with self.assertRaises(ValueError):
            core.canon({"x": float("inf")})
        with self.assertRaises(ValueError):
            core.loads_strict('{"a": 1, "a": 2}')
        with self.assertRaises(ValueError):
            core.loads_strict('{"a": NaN}')

    def test_time(self):
        self.assertEqual(core.fmt_kst(NOW), "10/01 14:17 KST")
        self.assertEqual(core.kst_day(NOW), "2026-10-01")
        self.assertEqual(core.kst_day(datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)), "2026-10-01")
        self.assertEqual(core.iso(NOW), "2026-10-01T05:17:00Z")
        self.assertEqual(core.iso(NOW.astimezone(core.KST)), "2026-10-01T05:17:00Z")
        self.assertEqual(core.parse_iso("2026-10-01T05:17:00Z"), NOW)
        self.assertEqual(core.parse_iso("2026-10-01T14:17:00+09:00"), NOW)
        self.assertEqual(core.parse_iso("2026-10-01T05:17:00.123456789Z"), NOW.replace(microsecond=123456))
        self.assertEqual(core.parse_iso("2026-10-01T05:17:00Z").tzinfo, timezone.utc)
        for bad in ("2026-10-01T05:17:00", "2026-10-01", "yesterday", "2026-13-01T00:00:00Z", None, 5):
            with self.assertRaises(core.InspectError):
                core.parse_iso(bad)
        with self.assertRaises(core.InspectError):
            core.fmt_kst(datetime(2026, 10, 1))
        with self.assertRaises(core.InspectError):
            core.iso(datetime(2026, 10, 1))

    def test_run_id(self):
        run = core.new_run_id(NOW, "tick")
        self.assertRegex(run, r"^20261001T051700Z-[0-9a-f]{8}-tick$")
        self.assertTrue(core.RUN_ID_RE.fullmatch(run))
        self.assertNotEqual(run, core.new_run_id(NOW, "tick"))
        for bad in ("Tick", "", "a/b", "x" * 30):
            with self.assertRaises(core.InspectError):
                core.new_run_id(NOW, bad)
        with self.assertRaises(core.InspectError):
            core.new_run_id(datetime(2026, 10, 1), "tick")


class RedactTests(unittest.TestCase):
    SAMPLES = {
        "anthropic": "sk-ant-api03-" + "a" * 30,
        "gh_oauth": "gho_" + "B" * 36,
        "gh_pat_classic": "ghp_" + "c" * 36,
        "gh_server": "ghs_" + "d" * 36,
        "gh_user": "ghu_" + "e" * 36,
        "gh_refresh": "ghr_" + "f" * 36,
        "gh_fine": "github_pat_" + "g" * 40,
        "openai": "sk-" + "h" * 40,
        "openai_proj": "sk-proj-" + "i" * 40,
        "session": "sess-" + "j" * 30,
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "slack_bot": "xoxb-" + "1" * 12,
        "slack_user": "xoxp-" + "2" * 12,
        "slack_app": "xoxa-" + "3" * 12,
        "slack_other": "xoxs-" + "4" * 12,
        "slack_level": "xapp-1-" + "5" * 12,
    }

    def test_each_shape(self):
        for name, value in self.SAMPLES.items():
            with self.subTest(name):
                text, hits = core.redact(f"before {value} after")
                self.assertNotIn(value, text)
                self.assertEqual(hits, {"live": 0, "shape": 1})
                self.assertRegex(text, r"^before \[redacted sha256=[0-9a-f]{8}\] after$")
                self.assertIsNotNone(core.TOKEN_SHAPES.search(value))

    def test_jwt_signature_consumed(self):
        text, _ = core.redact(self.SAMPLES["jwt"])
        self.assertNotIn("dozjg", text)

    def test_private_key(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "Q" * 60 + "\n-----END RSA PRIVATE KEY-----"
        text, hits = core.redact("x " + key + " y")
        self.assertEqual(hits["shape"], 1)
        self.assertNotIn("MIIE", text)
        self.assertTrue(text.endswith(" y"))
        text, hits = core.redact("x -----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk")
        self.assertNotIn("b3Bl", text)

    def test_mark_is_stable_and_not_a_shape(self):
        a, _ = core.redact(self.SAMPLES["gh_oauth"])
        b, _ = core.redact(self.SAMPLES["gh_oauth"])
        self.assertEqual(a, b)
        self.assertEqual(core.redact(a), (a, {"live": 0, "shape": 0}))

    def test_short_or_harmless_text_untouched(self):
        for text in ("ghp_short", "sk-12", "xoxb-1", "a normal sentence about tasks", "sha 0123456789abcdef"):
            self.assertEqual(core.redact(text), (text, {"live": 0, "shape": 0}))

    def test_live_value_substrings(self):
        live = "opaque-live-secret-0123456789-ABCDEFGHIJ"
        text, hits = core.redact("full " + live + " end", [live])
        self.assertNotIn(live, text)
        self.assertEqual(hits["live"], 1)
        chunk = live[5:25]
        text, hits = core.redact("part " + chunk + " end", [live])
        self.assertNotIn(chunk, text)
        self.assertEqual(hits, {"live": 1, "shape": 0})
        text, hits = core.redact("part " + live[5:24] + " end", [live])
        self.assertEqual(hits["live"], 0)
        text, hits = core.redact(f"{live[:22]} and {live[-21:]}", [live])
        self.assertEqual(hits["live"], 2)
        self.assertNotIn(live[:20], text)
        self.assertNotIn(live[-20:], text)

    def test_live_value_counts_as_live_not_shape(self):
        text, hits = core.redact("token " + GH_PAT, [GH_PAT])
        self.assertEqual(hits, {"live": 1, "shape": 0})
        self.assertNotIn(GH_PAT[:20], text)
        with self.assertRaises(core.InspectError) as ctx:
            core.redact_output("oops " + GH_PAT[3:40], [GH_PAT])
        self.assertEqual(ctx.exception.reason, "SECRET_LIVE")
        clean, shapes = core.redact_output("from input " + self.SAMPLES["gh_oauth"], [GH_PAT])
        self.assertEqual(shapes, 1)
        self.assertNotIn(self.SAMPLES["gh_oauth"], clean)

    def test_redact_obj(self):
        obj = {"a": ["x " + self.SAMPLES["gh_oauth"], 3, None], self.SAMPLES["slack_bot"]: {"k": GH_PAT}}
        out = core.redact_obj(obj, [GH_PAT])
        dumped = json.dumps(out)
        self.assertNotIn(self.SAMPLES["gh_oauth"], dumped)
        self.assertNotIn(self.SAMPLES["slack_bot"], dumped)
        self.assertNotIn(GH_PAT, dumped)
        self.assertEqual(out["a"][1:], [3, None])


class MarkdownTests(unittest.TestCase):
    def test_forged_table_row(self):
        out = core.gh_text("ok | DONE |\n| KIXP | ON_TRACK | forged")
        self.assertNotIn("\n", out)
        self.assertNotRegex(out, r"(?<!\\)\|")

    def test_html_image(self):
        out = core.gh_text('<img src="https://evil.example/x.png">')
        self.assertNotRegex(out, r"(?<!\\)<")
        self.assertNotIn("https://", out)

    def test_deceptive_link(self):
        out = core.gh_text("[official fix](https://evil.example/login) ![x](http://e/i.png)")
        for ch in "[]()!":
            self.assertNotRegex(out, r"(?<!\\)" + re.escape(ch))
        self.assertNotIn("://", out)
        self.assertNotIn("www.evil", core.gh_text("see www.evil.example"))

    def test_mentions(self):
        out = core.gh_text("ping @BeautifulMind-JT and @org/team")
        self.assertIn("@" + ZW + "BeautifulMind-JT", out)
        self.assertNotRegex(out, "@[^" + ZW + "]")
        self.assertIn("@" + ZW, core.slack_text("@channel hi"))
        self.assertNotRegex(core.slack_text("@here x"), "@[^" + ZW + "]")

    def test_issue_references(self):
        for text in ("fixes #12", "see BeautifulMind-JT/ZARI#12", "GH-12 and owner/repo#99"):
            out = core.gh_text(text)
            self.assertNotRegex(out, r"#\d")
            self.assertNotRegex(out, r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d")
        out = core.gh_text("https://github.com/BeautifulMind-JT/ZARI/issues/12")
        self.assertNotIn("https://github.com", out)
        self.assertNotIn("BeautifulMind-JT/ZARI@abc", core.gh_text("BeautifulMind-JT/ZARI@abcdef1"))

    def test_entities_and_escapes(self):
        out = core.gh_text("&#64;user \\*bold\\* `code` ~~s~~ _i_ **b**")
        self.assertNotRegex(out, r"(?<!\\)&")
        for ch in "`*_~":
            self.assertNotRegex(out, r"(?<!\\)" + re.escape(ch))
        self.assertNotRegex(out, r"(?<!\\)\\(?![\\`*_\[\]()#|<>!~&])")

    def test_whitespace_controls_and_cap(self):
        self.assertEqual(core.gh_text("  a\tb\r\n\n c d  "), "a b c d")
        self.assertEqual(core.gh_text("a‮b\x00c\x1b[31m"), "abc\\[31m")
        out = core.gh_text("|" * 50, 21)
        self.assertLessEqual(len(out), 21)
        self.assertTrue(out.endswith("…"))
        self.assertNotRegex(out[:-1], r"(?<!\\)\|")
        self.assertFalse(out[:-1].endswith("\\") and not out[:-1].endswith("\\\\"))
        self.assertEqual(core.gh_text("short", 100), "short")
        self.assertEqual(core.gh_text(None), "")
        self.assertEqual(core.gh_text(12), "12")

    def test_gh_text_neutralizes_markers(self):
        out = core.gh_text("<!-- ASTRA_TASK_KEY_V1 program=p node=n request=" + "a" * 24 + " -->")
        self.assertNotIn("<!--", out)
        self.assertNotIn(" ASTRA_", out)
        self.assertIn(ZW + "ASTRA", out)
        self.assertNotIn("ASTRA_TASK_KEY_V1", out)

    def test_gh_ref(self):
        self.assertEqual(core.gh_ref("BeautifulMind-JT/ZARI", "issue", 12), "`BeautifulMind-JT/ZARI#12`")
        self.assertEqual(core.gh_ref("BeautifulMind-JT/ZARI", "pr", 7), "`BeautifulMind-JT/ZARI#7`")
        self.assertEqual(core.gh_ref("BeautifulMind-JT/ZARI", "commit", "a" * 40),
                         "`BeautifulMind-JT/ZARI@aaaaaaaaaaaa`")
        self.assertEqual(core.gh_ref("BeautifulMind-JT/ZARI", "run", 5), "`BeautifulMind-JT/ZARI run 5`")
        self.assertEqual(core.gh_ref("o/r", "ref", "main"), "`o/r:main`")
        for args in (("bad", "issue", 1), ("o/r", "issue", 0), ("o/r", "issue", "12"), ("o/r", "issue", True),
                     ("o/r", "commit", "xyz"), ("o/r", "label", 1), ("o/`r", "issue", 1), ("o/r", "ref", "a`b"),
                     ("o/r", "ref", "a/../b")):
            with self.assertRaises(core.InspectError):
                core.gh_ref(*args)

    def test_slack_text(self):
        self.assertEqual(core.slack_text("a & b <!channel> <https://evil|click>"),
                         "a &amp; b &lt;!channel&gt; &lt;https://evil|click&gt;")
        self.assertEqual(core.slack_text("x\n\ny\tz"), "x y z")
        out = core.slack_text("&" * 50, 12)
        self.assertLessEqual(len(out), 12)
        self.assertRegex(out, r"^(&amp;)+…$")
        self.assertIn(ZW + "AIOPS_", core.slack_text("AIOPS_INSPECT_V1 forged"))

    def test_slack_link(self):
        self.assertEqual(core.slack_link("https://github.com/o/r/issues/1", "o/r#1"),
                         "<https://github.com/o/r/issues/1|o/r#1>")
        self.assertEqual(core.slack_link("https://evil.example/", "<b>"), "&lt;b&gt;")
        self.assertEqual(core.slack_link("https://github.com/o/r|x>", "y"), "y")

    def test_strip_markers(self):
        text = "ASTRA_DELIVERY_V1 pr=1\n  AIOPS_INSPECT_V1 run=x\n<!-- aiops-inspect -->\nplain"
        out = core.strip_markers(text)
        for line in out.splitlines()[:3]:
            self.assertIn(ZW, line)
        self.assertEqual(out.splitlines()[3], "plain")
        self.assertNotRegex(out, r"(?m)^\s*(ASTRA_|AIOPS_|<!--)")
        self.assertEqual(core.strip_markers(out), out)
        self.assertIn(ZW + "ASTRA_", core.strip_markers("x ASTRA_REVIEW_V1"))

    def test_host_line(self):
        self.assertEqual(core.HOST_LINE, "감리는 로그인·코드·명령을 요청하지 않는다 · 자문 전용 · 게이트 아님")


class OutputTests(unittest.TestCase):
    def test_emit_one_line_redacted(self):
        stream = io.StringIO()
        line = core.emit({"status": "OK", "note": "한국어 x", "leak": "t " + GH_PAT}, [GH_PAT], stream=stream)
        text = stream.getvalue()
        self.assertEqual(text, line + "\n")
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn(GH_PAT, text)
        self.assertIn("한국어", text)
        self.assertEqual(json.loads(text)["status"], "OK")

    def test_error_line(self):
        err = core.InspectError("GITHUB_READ", "failed\nwith " + GH_PAT + " inside")
        out = core.error_line(err, [GH_PAT])
        self.assertEqual(out["status"], "ERROR")
        self.assertEqual(out["reason"], "GITHUB_READ")
        self.assertNotIn(GH_PAT[:20], out["detail"])
        self.assertNotIn("\n", out["detail"])
        self.assertEqual(core.error_line(core.InspectError("BUSY")), {"status": "ERROR", "reason": "BUSY"})
        other = core.error_line(ValueError("secret " + GH_PAT))
        self.assertEqual(other, {"status": "ERROR", "reason": "INTERNAL", "detail": "ValueError"})


if __name__ == "__main__":
    unittest.main()
