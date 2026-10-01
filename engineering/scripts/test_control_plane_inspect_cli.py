"""Tests for the inspector CLI, daemon and tick (control_plane_inspect, contract §3.9).

Everything runs under a temporary ``--root`` with injected fakes: an in-memory GitHub (ETags,
ledger issue writes), a fake host runner, a fake Slack, a fake clock and a render stub. No
network, no sudo, no real /etc, /var, /opt or /proc, no sleeping and no real signals.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import control_plane as cp  # noqa: E402
import control_plane_inspect as cli  # noqa: E402
import control_plane_inspect_charts as charts  # noqa: E402
import control_plane_inspect_core as core  # noqa: E402
from control_plane_inspect_core import InspectError  # noqa: E402
import control_plane_inspect_facts as facts  # noqa: E402
import control_plane_inspect_publish as pub  # noqa: E402

NOW = datetime(2026, 10, 1, 5, 17, tzinfo=timezone.utc)  # 10/01 14:17 KST
CTRL = "BeautifulMind-JT/ai-ops-control-plane"
ZARI = "BeautifulMind-JT/ZARI"
LEDGER = 5  # test_ledger_issue (stage DRY)
TEAM, BOT, CHANNEL, TEST_CHANNEL = "T01234567", "U01234567", "C01234567", "C07654321"
GH_READ = "github_pat_" + "R1" * 20
GH_LEDGER = "github_pat_" + "L2" * 20
SLACK = "xoxb-" + "1234567890-abcdefghij-" * 2
SECRETS = {"gh-read": GH_READ, "gh-ledger": GH_LEDGER, "slack": SLACK}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:40]


def b64(obj: Any) -> Dict[str, Any]:
    return {"encoding": "base64", "content": base64.b64encode(json.dumps(obj).encode()).decode()}


def tiny_png(width: int = 4, height: int = 3) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xfc\xfc\xfb" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def config(**over: Any) -> Dict[str, Any]:
    cfg = {"schema": "AIOPS_INSPECT_CONFIG_V1", "stage": "DRY", "control_repository": CTRL,
           "targets": [{"repository": ZARI, "prefix": "ZARI"}], "ledger_issue": 4, "test_ledger_issue": LEDGER,
           "slack": {"team_id": TEAM, "bot_user_id": BOT, "channel_id": CHANNEL, "test_channel_id": TEST_CHANNEL},
           "user_login": "BeautifulMind-JT", "host_helper": "/opt/astra/bin/astra-host-control",
           "ledger_account": "aiops-inspect-ledger", "render_account": "aiops-plot", "tick_minute": 17,
           "daily_hour_kst": 9, "deadman_hours": 3, "contract_pairs": [], "thresholds": {}}
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------------------- fakes

class World:
    """In-memory GitHub (control repo + ZARI + the ledger issue) and host ledger."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.ctrl_main = sha("ctrl-main")
        self.head = sha("zari-head")
        self.plan_commit = sha("plan")
        self.plan = {"schema_version": 1, "program": "zari", "repository": ZARI, "project": "ZARI",
                     "approval_pointer": "https://github.com/x/y/issues/1",
                     "authoritative_doc_pointers": "docs/A.md",
                     "nodes": [{"id": "N1", "title": "One", "spec": "s", "depends_on": []},
                               {"id": "N2", "title": "Two", "spec": "s", "depends_on": ["N1"]}]}
        merged = now - timedelta(days=2)
        self.pr11 = {"number": 11, "state": "closed", "merged_at": iso(merged), "merge_commit_sha": sha("m11"),
                     "head": {"sha": sha("h11")}, "base": {"ref": "main"}, "updated_at": iso(merged),
                     "title": "free text"}
        self.pulls = [self.pr11]
        self.pull_files = {11: [{"filename": "src/a.py", "status": "modified"}]}
        self.issues = [{"number": 101, "state": "closed", "state_reason": "completed",
                        "labels": [{"name": "aiops-task"}], "closed_at": iso(merged), "updated_at": iso(merged)}]
        self.ledger: List[Dict[str, Any]] = []      # comments on the ledger issue
        self.next_id = 900
        self.body: Optional[str] = None
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []
        self.post_mode: Optional[str] = None       # "UNKNOWN_HIDDEN": store hidden, raise NET_UNKNOWN
        self.lanes = {"status": "OK", "max_active_sessions": 4, "active_total": 0,
                      "lanes": [{"lane": lane, "enabled": True, "active": []}
                                for lane in ("DEVIN", "GROK_BUILD", "GLM", "CURSOR")]}
        self.writer_row = {"launch_request_id": "a" * 24, "state": "RECONCILED", "role": "WRITER", "lane": "DEVIN",
                           "repository": ZARI, "task": "ZARI-N1", "attempt_id": 1, "task_revision": "p-x",
                           "owner_lane": "DEVIN", "reserved_at": iso(merged - timedelta(hours=5)),
                           "reserved_ts": (merged - timedelta(hours=5)).timestamp(),
                           "resolution": cp.VERIFIED_RELEASE,
                           "pin": {"kind": "DELIVERY", "pr": 11, "head": self.pr11["head"]["sha"]},
                           "released_ts": (merged - timedelta(hours=1)).timestamp(),
                           "released_at": iso(merged - timedelta(hours=1))}
        self.host_argv: List[List[str]] = []

    # -- GitHub

    def counts(self, method: str) -> int:
        return sum(1 for m, _, _ in self.requests if m == method)

    def route(self, path: str, q: Dict[str, str]) -> Tuple[int, Any]:
        if path == f"/repos/{CTRL}":
            return 200, {"default_branch": "main", "full_name": CTRL}
        if path == f"/repos/{CTRL}/branches/main":
            return 200, {"commit": {"sha": self.ctrl_main}}
        if path == f"/repos/{CTRL}/contents/{facts.CTRL_PROJECTS_PATH}":
            return 200, b64({ZARI: {"project": "ZARI", "program_required_checks": ["bridge"]}})
        if path == f"/repos/{CTRL}/contents/{facts.CTRL_ACTIVATION_PATH}":
            return 200, b64({"runtime_enabled": True, "activated_runtime_sha": sha("act")})
        if path == f"/repos/{CTRL}/actions/workflows/control-plane-runtime.yml/runs":
            return 200, {"workflow_runs": [{"id": 77, "head_sha": sha("r"), "conclusion": "success",
                                            "created_at": iso(self.now - timedelta(days=1))}]}
        if path == f"/repos/{CTRL}/issues/{LEDGER}/comments":
            since = q.get("since", "")
            return 200, [c for c in self.ledger if c["visible"] and c["updated_at"] >= since]
        if path.startswith(f"/repos/{CTRL}/issues/comments/"):
            cid = int(path.rsplit("/", 1)[1])
            found = [c for c in self.ledger if c["id"] == cid]
            return (200, found[0]) if found else (404, None)
        base = f"/repos/{ZARI}"
        if path == base:
            return 200, {"default_branch": "main", "full_name": ZARI}
        rest = path[len(base):] if path.startswith(base + "/") else None
        if rest is None:
            return 404, None
        if rest == "/branches/main":
            return 200, {"commit": {"sha": self.head}}
        if rest == "/issues":
            return 200, self.issues
        if rest == "/pulls":
            return 200, [] if q.get("page") not in (None, "1") else self.pulls
        if rest == "/issues/comments":
            return 200, []
        if rest == "/commits" and "path" in q:
            return 200, [{"sha": self.plan_commit, "commit": {"committer": {"date": iso(self.now - timedelta(days=10))}}}]
        if rest == "/commits":
            return 200, []
        if rest == "/contents/.aiops/program.json" and q.get("ref") == self.plan_commit:
            return 200, b64(self.plan)
        parts = rest.strip("/").split("/")
        if parts[0] == "pulls" and len(parts) == 2 and parts[1] == "11":
            return 200, dict(self.pr11, merged=True)
        if parts[0] == "pulls" and len(parts) == 3 and parts[2] == "files":
            return 200, self.pull_files.get(int(parts[1]), []) if q.get("page") in (None, "1") else []
        if parts[0] == "commits" and len(parts) == 3 and parts[2] == "check-runs":
            runs = [{"id": 7, "name": "bridge", "status": "completed", "conclusion": "success",
                     "started_at": iso(self.now - timedelta(days=2)), "completed_at": iso(self.now - timedelta(days=2)),
                     "app": {"id": 15368, "slug": "github-actions"}}] if parts[1] == sha("m11") else []
            return 200, {"total_count": len(runs), "check_runs": runs if q.get("page") in (None, "1") else []}
        if parts[0] == "commits" and len(parts) == 3 and parts[2] == "pulls":
            return 200, []
        return 404, None

    def transport(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
                  timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.requests.append((method, parts.path, q))
        if method == "POST":
            assert parts.path == f"/repos/{CTRL}/issues/{LEDGER}/comments", parts.path
            text = json.loads(body.decode("utf-8"))["body"]
            self.next_id += 1
            at = iso(self.now)
            comment = {"id": self.next_id, "body": text, "created_at": at, "updated_at": at, "visible": True,
                       "html_url": f"https://github.com/{CTRL}/issues/{LEDGER}#issuecomment-{self.next_id}"}
            self.ledger.append(comment)
            if self.post_mode == "UNKNOWN_HIDDEN":
                comment["visible"] = False
                raise InspectError("NET_UNKNOWN")
            return 201, {}, json.dumps(comment).encode()
        if method == "PATCH":
            assert parts.path == f"/repos/{CTRL}/issues/{LEDGER}", parts.path
            self.body = json.loads(body.decode("utf-8"))["body"]
            return 200, {}, json.dumps({"updated_at": iso(self.now)}).encode()
        assert method == "GET", method
        status, data = self.route(parts.path, q)
        if status != 200:
            return status, {}, b'{"message":"Not Found"}'
        raw = json.dumps(data, sort_keys=True).encode()
        etag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
        if headers.get("If-None-Match") == etag:
            return 304, {"etag": etag}, b""
        return 200, {"etag": etag, "github-authentication-token-expiration": "2026-10-09 00:00:00 UTC"}, raw

    # -- host

    def runner(self, command: List[str], **kw: Any) -> subprocess.CompletedProcess:
        assert command[:5] == ["/usr/bin/sudo", "-n", "-u", "astra-control", "/opt/astra/bin/astra-host-control"]
        assert kw["user"] == "aiops-inspect-ledger" and kw["extra_groups"] == []
        argv = command[5:]
        self.host_argv.append(argv)
        if argv[0] == "status":
            out: Dict[str, Any] = self.lanes
        elif argv[0] == "materialize-status":
            program, node = argv[2], argv[4]
            if node == "N1":
                out = {"status": "CREATED", "program": program, "node": node, "issue": 101, "attempt": 1,
                       "repository": ZARI, "plan_commit": self.plan_commit, "request": "b" * 24}
            else:
                out = {"status": "NOT_FOUND", "program": program, "node": node}
        elif argv[0] == "task-status":
            rows = [self.writer_row] if argv[4] == "ZARI-N1" else []
            out = {"status": "OK", "repository": argv[2], "task": argv[4], "rows": rows}
        else:  # pragma: no cover
            raise AssertionError(argv)
        return subprocess.CompletedProcess(command, 0, (json.dumps(out) + "\n").encode(), b"")


class FakeSlack:
    """Slack transport fake answering every allowed method."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.ts = 1000
        self.n = 0
        self.scopes = "chat:write,files:write"

    def names(self) -> List[str]:
        return [c["name"] for c in self.calls]

    def texts(self, name: str) -> List[str]:
        return [c["params"].get("text", "") for c in self.calls if c["name"] == name]

    def __call__(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
                 timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        if url.startswith(pub.SLACK_API):
            name = url[len(pub.SLACK_API):]
            params = dict(urllib.parse.parse_qsl((body or b"").decode("utf-8"), keep_blank_values=True))
        else:
            name, params = "UPLOAD", {}
        self.calls.append({"name": name, "params": params, "url": url})
        self.n += 1

        def ok(obj: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
            return 200, dict(extra or {}), json.dumps(dict(obj, ok=True)).encode()
        if name == "auth.test":
            return ok({"team_id": TEAM, "user_id": BOT}, {"X-OAuth-Scopes": self.scopes})
        if name == "chat.postMessage":
            self.ts += 1
            return ok({"ts": f"{self.ts}.000100", "channel": params.get("channel")})
        if name == "chat.scheduleMessage":
            return ok({"scheduled_message_id": f"Q{self.n:08d}", "post_at": int(params["post_at"])})
        if name == "files.getUploadURLExternal":
            return ok({"upload_url": f"https://files.slack.com/upload/v1/X{self.n}", "file_id": f"F{self.n:08d}"})
        if name == "UPLOAD":
            return 200, {}, b"OK"
        return ok({})


class RenderStub:
    """Validates the chart document strictly and writes two tiny PNGs (stands in for the render child)."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Path, Path, str]] = []
        self.mode = "OK"

    def __call__(self, ctx: Any, data_path: Path, out_dir: Path, account: str, font: Optional[str]) -> Dict[str, Any]:
        self.calls.append((data_path, out_dir, account))
        charts.load_chart_data(data_path)  # signals -> charts schema integration
        if self.mode != "OK":
            return {"status": self.mode}
        pngs = []
        for name in ("c5_scorecard.png", "c1_ladder.png"):
            data = tiny_png()
            (out_dir / name).write_bytes(data)
            pngs.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "w": 4, "h": 3})
        if self.mode == "OK" and getattr(self, "tamper", False):
            (out_dir / "c1_ladder.png").write_bytes(b"\x89PNG\r\n\x1a\nnot a png")
        return {"status": "OK", "pngs": pngs, "font": "Stub"}


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.t = now
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.t += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------- base

class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = core.Paths.from_root(self.root)
        self.world = World()
        self.slack = FakeSlack()
        self.render = RenderStub()
        self.clock = Clock()
        self.kills: List[Tuple[int, int]] = []
        self.deps = cli.Deps(now=self.clock.now, monotonic=self.clock.monotonic, sleep=self.clock.sleep,
                             github_transport=self.world.transport, slack_transport=self.slack,
                             host_runner=self.world.runner, render=self.render,
                             getpass=lambda prompt: "", getpwnam=self.getpwnam,
                             kill=self.no_kill, killpg=self.no_killpg, proc_root=str(self.root / "proc"),
                             euid=lambda: 0, install_signals=False)
        self.write_config(config())
        for kind, value in SECRETS.items():
            self.write_secret(kind, value)

    # -- fixtures

    @staticmethod
    def getpwnam(name: str) -> Any:
        if name not in ("aiops-inspect-ledger", "aiops-plot"):
            raise KeyError(name)
        uid = 990 if name == "aiops-plot" else 991
        return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=uid, pw_shell="/usr/sbin/nologin", pw_dir="/nonexistent")

    def no_kill(self, pid: int, sig: int) -> None:
        self.kills.append((pid, sig))

    def no_killpg(self, pgid: int, sig: int) -> None:
        self.kills.append((-pgid, sig))

    def write_config(self, cfg: Any) -> None:
        self.paths.etc.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_text(json.dumps(cfg))
        os.chmod(str(self.paths.config), 0o600)

    def write_secret(self, kind: str, value: str) -> None:
        path = self.paths.secret(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n")
        os.chmod(str(path), 0o600)

    @property
    def store(self) -> core.StateStore:
        return core.StateStore(self.paths.state)

    # -- running commands

    def cmd(self, *argv: str) -> Tuple[int, Dict[str, Any], str]:
        out = io.StringIO()
        code = cli.main(["--root", str(self.root)] + list(argv), deps=self.deps, stream=out)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1, text)
        for secret in SECRETS.values():
            self.assertNotIn(secret, text)
        return code, json.loads(text), text

    def tick(self) -> Dict[str, Any]:
        code, result, _ = self.cmd("tick")
        self.assertEqual(code, 0, result)
        return result

    def journals(self) -> List[Dict[str, Any]]:
        publisher = pub.Publisher(self.store, config())
        return [publisher.load(r) for r in publisher.journal_runs()]


# ---------------------------------------------------------------------------- the end-to-end test (§3.9)

class EndToEndTests(Base):
    def test_four_ticks(self) -> None:
        world, slack = self.world, self.slack
        # Tick 1: first run posts the ledger comment, body, card (+charts, replies), heartbeat, daily, dead-man.
        first = self.tick()
        self.assertEqual((first["status"], first["t1"], first["published"]), ("OK", "DONE", True))
        self.assertEqual(first["render"], "OK")
        self.assertEqual(world.counts("POST"), 1)
        self.assertEqual(world.counts("PATCH"), 1)
        comment = world.ledger[0]["body"]
        self.assertTrue(comment.startswith("<!-- aiops-inspect -->\nAIOPS_INSPECT_V1 run="))
        self.assertIn("stage=DRY advisory=true gate=none model=none tool=", comment)
        names = slack.names()
        self.assertEqual(names[0], "auth.test")
        self.assertIn("files.completeUploadExternal", names)
        self.assertEqual(names.count("UPLOAD"), 2)
        self.assertEqual(names[-1], "chat.scheduleMessage")
        posts = slack.texts("chat.postMessage")
        self.assertTrue(any(core.HOST_LINE in t for t in posts))                    # the card
        self.assertTrue(any(t.startswith("AIOPS_INSPECT_V1 heartbeat") for t in posts))
        self.assertTrue(any(t.startswith("AIOPS_INSPECT_V1 daily") for t in posts))
        self.assertTrue(slack.texts("chat.scheduleMessage")[0].startswith("AIOPS_INSPECT_V1 status=STALE"))
        self.assertEqual(self.journals()[-1]["state"], "SLACK_POSTED")
        self.assertEqual(self.journals()[-1]["comment"]["state"], pub.POSTED)
        self.assertIsNotNone(self.store.read("findings"))
        self.assertTrue(any("charts" in str(p) for p in (self.paths.runs / first["run"]).iterdir()))

        # Tick 2: nothing changed -> T1 skipped; only the heartbeat edit and the dead-man reschedule.
        self.clock.t = NOW + timedelta(hours=1)
        world.now = self.clock.t
        slack.calls.clear()
        before = (world.counts("POST"), world.counts("PATCH"))
        second = self.tick()
        self.assertEqual((second["t1"], second["published"]), ("SKIPPED", False))
        self.assertEqual(second["t1_reason"], "UNCHANGED")
        self.assertEqual(slack.names(), ["auth.test", "chat.update", "chat.scheduleMessage",
                                         "chat.deleteScheduledMessage"])
        self.assertEqual((world.counts("POST"), world.counts("PATCH")), before)

        # Tick 3: an orphan merge changes the facts; the ledger POST ends NET_UNKNOWN -> GH_UNKNOWN.
        self.clock.t = NOW + timedelta(hours=2)
        world.now = self.clock.t
        world.pulls.insert(0, {"number": 13, "state": "closed", "merged_at": iso(self.clock.t - timedelta(minutes=30)),
                               "merge_commit_sha": sha("m13"), "head": {"sha": sha("h13")}, "base": {"ref": "main"},
                               "updated_at": iso(self.clock.t - timedelta(minutes=30)), "title": "x"})
        world.pull_files[13] = [{"filename": "src/orphan.py", "status": "added"}]
        world.post_mode = "UNKNOWN_HIDDEN"
        slack.calls.clear()
        third = self.tick()
        self.assertEqual((third["t1"], third["published"]), ("DONE", True))
        self.assertEqual(world.counts("POST"), 2)
        entry = self.journals()[-1]
        self.assertEqual(entry["comment"]["state"], pub.UNKNOWN)
        self.assertIn("GH_UNKNOWN", [step["step"] for step in entry["log"]])
        card = [t for t in slack.texts("chat.postMessage") if core.HOST_LINE in t]
        self.assertEqual(len(card), 1)
        self.assertNotIn("issuecomment-", card[0])  # linked only when POSTED
        heartbeat = [t for t in slack.texts("chat.update") if "heartbeat" in t][-1]
        self.assertIn("게시 미확인 1", heartbeat)
        code, status, _ = self.cmd("status")
        self.assertEqual(status["unknown_posts"], 1)
        self.assertEqual(status["findings"], {"WATCH": 2, "AT_RISK": 0, "unknown": 0})
        found = {(f["id"], f["signal"]) for f in self.store.read("findings")["findings"].values()}
        self.assertEqual(found, {("INS-ZARI-0001", "S1"), ("INS-CTRL-0001", "S7")})  # orphan merge; idle lanes

        # Tick 4: the comment is now listed -> adopted as POSTED; never reposted.
        self.clock.t = NOW + timedelta(hours=3)
        world.now = self.clock.t
        world.post_mode = None
        world.ledger[-1]["visible"] = True
        fourth = self.tick()
        self.assertEqual(fourth["reconciled"]["adopted"], 1)
        self.assertEqual(world.counts("POST"), 2)
        entry = self.journals()[-1]
        self.assertEqual(entry["comment"]["state"], pub.POSTED)
        self.assertEqual(entry["comment"]["id"], world.ledger[-1]["id"])
        self.assertEqual(fourth["t1"], "SKIPPED")
        # No secret in any GitHub or Slack payload.
        everything = json.dumps([c["params"] for c in slack.calls]) + json.dumps(world.ledger)
        for secret in SECRETS.values():
            self.assertNotIn(secret, everything)

    def test_font_missing_posts_text_only(self) -> None:
        self.render.mode = "FONT_MISSING"
        result = self.tick()
        self.assertEqual((result["published"], result["render"]), (True, "FONT_MISSING"))
        self.assertNotIn("files.getUploadURLExternal", self.slack.names())
        self.assertIn("그림 없음 (FONT\\_MISSING)", self.world.ledger[0]["body"])

    def test_tampered_png_is_not_published(self) -> None:
        self.render.tamper = True
        result = self.tick()
        self.assertEqual(result["render"], "PNG_INVALID")
        self.assertNotIn("UPLOAD", self.slack.names())

    def test_t1_failure_is_handled_and_degrades_after_three(self) -> None:
        def broken(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Any:
            if "/contents/" in url or "/pulls" in url:
                return 429, {"x-ratelimit-remaining": "0"}, b"{}"
            return self.world.transport(method, url, headers, body, timeout)
        self.deps.github_transport = broken
        for hour in range(3):
            self.clock.t = NOW + timedelta(hours=hour)
            result = self.tick()
            self.assertEqual(result["t1"], "FAILED")
            self.assertEqual(result["t1_reason"], "GITHUB_RATE_LIMIT")
        self.assertEqual(result["heartbeat"], "DEGRADED(GITHUB_READ)")
        self.assertIn("상태 DEGRADED(GITHUB_READ)", self.slack.texts("chat.update")[-1])
        self.assertTrue(self.store.read("recheck")["t1_dirty"])
        self.assertIsNone(core.kill_state(self.paths))  # handled failures never halt
        self.assertEqual(self.world.counts("POST"), 0)


# ---------------------------------------------------------------------------- locks, kill switch, errors

class LockAndKillTests(Base):
    def test_tick_lock_busy(self) -> None:
        lock = cli.Lock(cli.Ctx(self.root, self.deps).lock_path("tick.lock"))
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        for argv in (["tick"], ["probe"], ["preflight"], ["resume", "--pointer", "x"]):
            code, result, _ = self.cmd(*argv)
            self.assertEqual((code, result), (0, {"status": "BUSY"}), argv)
        code, result, _ = self.cmd("status")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(self.world.requests, [])

    def test_kill_switch_halts_with_heartbeat_only(self) -> None:
        core.write_kill(self.paths, "SECRET_LIVE", "r", NOW - timedelta(hours=1))
        result = self.tick()
        self.assertEqual((result["status"], result["reason"]), ("HALTED", "SECRET_LIVE"))
        self.assertEqual(self.world.requests, [])                   # no GitHub at all
        names = self.slack.names()
        self.assertNotIn("chat.scheduleMessage", names)              # no dead-man while halted
        heartbeat = [t for t in self.slack.texts("chat.postMessage") if "heartbeat" in t][0]
        self.assertIn("상태 HALTED(SECRET_LIVE)", heartbeat)
        self.assertIn("정지 시작 10/01 13:17 KST", heartbeat)

    def test_slack_scope_engages_kill_switch(self) -> None:
        self.slack.scopes = "chat:write,files:write,channels:read"
        code, result, _ = self.cmd("tick")
        self.assertEqual((code, result["reason"]), (1, "SLACK_SCOPE"))
        self.assertEqual(core.kill_state(self.paths)["reason"], "SLACK_SCOPE")
        self.assertEqual(self.world.requests, [])  # auth.test runs before any GitHub read or write

    def test_secret_live_engages_kill_switch(self) -> None:
        original = facts.probe

        def leaky(*args: Any, **kw: Any) -> Any:
            raise InspectError("SECRET_LIVE", "output contained a live secret")
        facts.probe = leaky
        self.addCleanup(setattr, facts, "probe", original)
        code, result, _ = self.cmd("tick")
        self.assertEqual((code, result["reason"]), (1, "SECRET_LIVE"))
        self.assertEqual(core.kill_state(self.paths)["reason"], "SECRET_LIVE")
        # The heartbeat still reports HALTED and the dead-man is still rescheduled (Gate 0 passed).
        self.assertIn("상태 HALTED(SECRET_LIVE)", self.slack.texts("chat.postMessage")[0])
        self.assertIn("chat.scheduleMessage", self.slack.names())

    def test_tool_tampered_engages_kill_switch(self) -> None:
        lib = self.paths.lib
        lib.mkdir(parents=True)
        for path in core.own_files(self.paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        lines = [f"{'0' * 64}  {p}" for p in core.own_files(self.paths)]
        self.paths.manifest.write_text("\n".join(lines) + "\n")
        out = io.StringIO()
        ctx_cls = cli.Ctx

        def checking_ctx(root: Any, deps: Any = None) -> Any:
            return ctx_cls(root, deps, installed=False, check_manifest=True)
        cli.Ctx = checking_ctx  # type: ignore[assignment]
        self.addCleanup(setattr, cli, "Ctx", ctx_cls)
        code = cli.main(["--root", str(self.root), "tick"], deps=self.deps, stream=out)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["reason"], "TOOL_TAMPERED")
        self.assertEqual(core.kill_state(self.paths)["reason"], "TOOL_TAMPERED")
        self.assertEqual(self.world.requests, [])

    def test_three_errors_in_24h_engage_kill_switch_and_resume_resets(self) -> None:
        self.write_config({"schema": "nope"})
        for hour in range(3):
            self.clock.t = NOW + timedelta(hours=hour)
            code, result, _ = self.cmd("tick")
            self.assertEqual((code, result["reason"]), (1, "CONFIG"))
        self.assertEqual(core.kill_state(self.paths)["reason"], cli.ERROR_KILL_REASON)
        self.write_config(config())
        pointer = f"https://github.com/{CTRL}/issues/{LEDGER}#issuecomment-901"
        code, result, _ = self.cmd("resume", "--pointer", pointer)
        self.assertEqual((code, result["status"], result["previous"]["reason"]), (0, "RESUMED", "ERROR_REPEATED"))
        self.assertIsNone(core.kill_state(self.paths))
        self.assertIn({"resume": pointer, "at": iso(self.clock.t)}, self.store.read_jsonl("history"))
        # One more error after the resume does not halt again.
        self.write_config({"schema": "nope"})
        self.cmd("tick")
        self.assertIsNone(core.kill_state(self.paths))

    def test_errors_older_than_24h_do_not_count(self) -> None:
        self.write_config({"schema": "nope"})
        for hours in (0, 13, 26):
            self.clock.t = NOW + timedelta(hours=hours)
            self.cmd("tick")
        self.assertIsNone(core.kill_state(self.paths))


class ResumeAndSecretTests(Base):
    def test_resume_pointer_validation(self) -> None:
        core.write_kill(self.paths, "SLACK_SCOPE", "r", NOW)
        bad = [f"http://github.com/{CTRL}/issues/5#issuecomment-1",
               f"https://github.com/BeautifulMind-JT/ZARI/issues/5#issuecomment-1",
               f"https://github.com/{CTRL}/issues/5",
               f"https://github.com/{CTRL}/issues/0#issuecomment-1",
               f"https://github.com/{CTRL}/issues/5#issuecomment-1 ",
               f"https://github.com/{CTRL}/pull/5#issuecomment-1"]
        for pointer in bad:
            code, result, _ = self.cmd("resume", "--pointer", pointer)
            self.assertEqual((code, result["reason"]), (1, "POINTER"), pointer)
            self.assertIsNotNone(core.kill_state(self.paths))
        code, result, _ = self.cmd("resume", "--pointer", f"https://github.com/{CTRL}/issues/5#issuecomment-12")
        self.assertEqual(result["status"], "RESUMED")
        code, result, _ = self.cmd("resume", "--pointer", f"https://github.com/{CTRL}/issues/5#issuecomment-12")
        self.assertEqual(result["status"], "NOT_HALTED")

    def test_set_secret_stores_0600_and_prints_only_a_digest(self) -> None:
        value = "github_pat_" + "N3" * 20
        self.deps.getpass = lambda prompt: value + "\n"
        code, result, text = self.cmd("set-secret", "gh-read")
        self.assertEqual(result, {"status": "STORED", "kind": "gh-read",
                                  "sha256_8": hashlib.sha256(value.encode()).hexdigest()[:8]})
        self.assertNotIn(value, text)
        self.assertEqual(os.stat(str(self.paths.gh_read)).st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.gh_read.read_text().strip(), value)

    def test_set_secret_refuses_wrong_shape_without_echo(self) -> None:
        value = "ghp_" + "C" * 36  # classic token
        self.deps.getpass = lambda prompt: value
        code, result, text = self.cmd("set-secret", "gh-ledger")
        self.assertEqual((code, result["reason"]), (1, "SECRET_GH_LEDGER"))
        self.assertNotIn(value, text)
        self.assertEqual(self.paths.gh_ledger.read_text().strip(), GH_LEDGER)


# ---------------------------------------------------------------------------- arguments and output

class OutputTests(Base):
    def test_argument_errors_print_one_line_and_exit_2(self) -> None:
        for argv in ([], ["--help"], ["nope"], ["set-secret", "other"], ["resume"], ["tick", "--x"], ["-h"]):
            out = io.StringIO()
            code = cli.main(argv, deps=self.deps, stream=out)
            self.assertEqual(code, 2, argv)
            self.assertEqual(json.loads(out.getvalue()), {"status": "ERROR", "reason": "ARGS"})

    def test_unexpected_exception_is_internal_with_class_name_only(self) -> None:
        def explode(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Any:
            raise RuntimeError("boom " + GH_READ)
        self.deps.github_transport = explode
        code, result, text = self.cmd("probe")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "ERROR")
        self.assertNotIn("boom", text)

    def test_not_root_refused_when_installed(self) -> None:
        deps = cli.Deps(euid=lambda: 1000)
        ctx = cli.Ctx("/", deps)
        with self.assertRaises(InspectError) as caught:
            cli.dispatch(ctx, cli.build_parser().parse_args(["status"]))
        self.assertEqual(caught.exception.reason, "NOT_ROOT")

    def test_status_reads_local_files_only(self) -> None:
        self.tick()
        self.world.requests.clear()
        self.slack.calls.clear()
        code, result, _ = self.cmd("status")
        self.assertEqual((code, result["status"], result["config"], result["manifest"]), (0, "OK", "OK", "SKIPPED"))
        self.assertEqual(self.world.requests, [])
        self.assertEqual(self.slack.calls, [])
        self.assertFalse(result["daemon"]["running"])
        self.assertEqual(result["last_tick"]["t1"], "DONE")
        self.assertEqual(result["heartbeat"]["status"], "OK")
        self.assertIn("gh-read", result["tokens"])
        self.assertIsNone(result["kill"])


class LauncherTests(unittest.TestCase):
    def test_lib_dir(self) -> None:
        self.assertEqual(cli._lib_dir("/opt/aiops/bin/aiops-inspect"), "/opt/aiops/inspect/lib")
        self.assertEqual(cli._lib_dir(str(HERE / "control_plane_inspect.py")), str(HERE))
        self.assertIn(str(HERE), sys.path)

    def test_isolated_subprocess_prints_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, "-I", str(HERE / "control_plane_inspect.py"), "--root", tmp,
                                   "status"], capture_output=True, text=True, timeout=120, cwd="/",
                                  env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.count("\n"), 1)
        result = json.loads(done.stdout)
        self.assertEqual((result["status"], result["config"]), ("OK", "CONFIG"))
        self.assertEqual(done.stderr, "")

    def test_isolated_subprocess_args_error(self) -> None:
        done = subprocess.run([sys.executable, "-I", str(HERE / "control_plane_inspect.py"), "bogus"],
                              capture_output=True, text=True, timeout=120, cwd="/")
        self.assertEqual((done.returncode, json.loads(done.stdout)["reason"]), (2, "ARGS"))

    def test_source_never_signals_outside_injected_kill(self) -> None:
        source = (HERE / "control_plane_inspect.py").read_text()
        self.assertNotIn("kill(-1", source)
        self.assertNotIn("os.kill(", source.replace("kill: Callable[[int, int], None] = os.kill", ""))
        self.assertEqual(source.count("os.killpg("), 0)


# ---------------------------------------------------------------------------- probe, preflight, render

class ProbeAndPreflightTests(Base):
    def test_probe_uses_scratch_state_only(self) -> None:
        code, result, _ = self.cmd("probe")
        self.assertEqual((code, result["status"]), (0, "OK"))
        self.assertEqual(result["products"][ZARI]["plan"], "PRESENT")
        self.assertRegex(result["snapshot"], r"^[0-9a-f]{64}$")
        self.assertFalse((self.paths.state / "etags.json").exists())
        self.assertFalse((self.paths.state / "facts.json").exists())
        self.assertFalse((self.paths.runs / result["run"] / "probe-state" / "facts.json").exists())
        self.assertTrue((self.paths.runs / result["run"] / "probe-state").is_dir())
        self.assertEqual(self.world.counts("POST") + self.world.counts("PATCH"), 0)
        self.assertEqual(self.slack.calls, [])

    def test_probe_refused_when_halted(self) -> None:
        core.write_kill(self.paths, "SLACK_SCOPE", None, NOW)
        code, result, _ = self.cmd("probe")
        self.assertEqual(result["status"], "HALTED")
        self.assertEqual(self.world.requests, [])

    def test_preflight_pass_without_writes(self) -> None:
        code, result, _ = self.cmd("preflight")
        self.assertEqual(result["status"], "PASS", result)
        names = {c["name"] for c in result["checks"]}
        self.assertTrue({"config", "kill_switch", "ledger_account", "render_account", "secret_slack",
                         "slack_auth", f"github_read:{ZARI}", f"github_read:{CTRL}", "github_ledger_read",
                         "host_lanes", "render", f"github_checks:{ZARI}"} <= names)
        self.assertNotIn(f"github_checks:{CTRL}", names)
        self.assertEqual(self.world.counts("POST") + self.world.counts("PATCH"), 0)
        self.assertEqual(self.slack.names(), ["auth.test"])
        self.assertEqual(self.world.host_argv, [["status", "--lanes"]])

    def test_preflight_fail_cases(self) -> None:
        os.unlink(str(self.paths.slack))
        self.slack.calls.clear()

        def login_shell(name: str) -> Any:
            entry = self.getpwnam(name)
            return SimpleNamespace(**dict(vars(entry), pw_shell="/bin/bash"))
        self.deps.getpwnam = login_shell
        self.render.mode = "FONT_MISSING"
        code, result, _ = self.cmd("preflight")
        self.assertEqual((code, result["status"]), (0, "FAIL"))
        failed = {c["name"]: c["detail"] for c in result["checks"] if not c["ok"]}
        self.assertEqual(failed["secret_slack"], "SECRET_SLACK")
        self.assertEqual(failed["ledger_account"], "ACCOUNT_SHELL")
        self.assertEqual(failed["render"], "FONT_MISSING")
        render = [c for c in result["checks"] if c["name"] == "render"][0]
        self.assertFalse(render["required"])
        self.assertNotIn("slack_auth", {c["name"] for c in result["checks"]})
        self.assertEqual(self.slack.calls, [])

    def test_empty_chart_doc_is_valid(self) -> None:
        charts.validate_chart_data(cli.empty_chart_doc(NOW))

    def test_launch_render_refuses_root_account(self) -> None:
        ctx = cli.Ctx(self.root, cli.Deps(getpwnam=lambda n: SimpleNamespace(pw_uid=0, pw_gid=0)))
        self.assertEqual(cli.launch_render(ctx, self.root / "d.json", self.root, "aiops-plot")["reason"],
                         "RENDER_ACCOUNT")
        ctx = cli.Ctx(self.root, cli.Deps(getpwnam=self.getpwnam))
        self.assertEqual(cli.launch_render(ctx, self.root / "d.json", self.root, "nobody-here")["reason"],
                         "RENDER_ACCOUNT")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root to switch to an account")
    def test_launch_render_real_child_as_unprivileged_account(self) -> None:
        import pwd
        try:
            nobody = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no nobody account")
        base = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(base)], check=False))
        os.chmod(str(base), 0o755)
        out = base / "run"
        out.mkdir(mode=0o700)
        os.chown(str(out), nobody.pw_uid, nobody.pw_gid)
        data = out / "charts.json"
        data.write_text(json.dumps(cli.empty_chart_doc(NOW)))
        os.chmod(str(data), 0o644)
        ctx = cli.Ctx(base, cli.Deps())
        result = cli.launch_render(ctx, data, out, "nobody")
        self.assertIn(result.get("status"), ("OK", "FONT_MISSING", "ERROR"), result)
        if result["status"] == "ERROR":
            self.assertIn(result.get("reason"), ("MATPLOTLIB_MISSING", "RENDER", "IO", "OUT_DIR", "RENDER_OUTPUT"))
        self.assertEqual(cli.read_children(ctx)["render"], [])


# ---------------------------------------------------------------------------- daemon, start, stop

class FakeProc:
    def __init__(self, pid: int, clock: Clock, runtime: float, code: int = 0) -> None:
        self.pid, self.clock, self.code = pid, clock, code
        self.ends = clock.mono + runtime
        self.killed = False

    def poll(self) -> Optional[int]:
        if self.killed:
            return -9
        return self.code if self.clock.mono >= self.ends else None

    def wait(self, timeout: Optional[float] = None) -> int:
        return self.poll() if self.poll() is not None else -9


class DaemonTests(Base):
    def daemon(self, runtimes: List[Tuple[float, int]]) -> Tuple[cli.Daemon, List[List[str]]]:
        spawned: List[List[str]] = []
        procs = list(runtimes)

        def spawn(argv: List[str]) -> FakeProc:
            spawned.append(argv)
            runtime, code = procs.pop(0)
            return FakeProc(4242 + len(spawned), self.clock, runtime, code)
        self.deps.spawn_tick = spawn

        def killpg(pgid: int, sig: int) -> None:
            self.kills.append((-pgid, sig))
            if self.current is not None and pgid == self.current.pid:
                self.current.killed = True
        self.current: Optional[FakeProc] = None
        self.deps.killpg = killpg
        ctx = cli.Ctx(self.root, self.deps)
        daemon = cli.Daemon(ctx)
        original = daemon.spawn

        def tracking() -> Any:
            self.current = original()
            return self.current
        daemon.spawn = tracking  # type: ignore[assignment]
        return daemon, spawned

    def test_ticks_at_tick_minute_kst_and_failures_do_not_end_loop(self) -> None:
        self.clock.t = datetime(2026, 10, 1, 5, 20, tzinfo=timezone.utc)   # 14:20 KST
        daemon, spawned = self.daemon([(30.0, 1), (40.0, 0)])
        starts = []
        real_spawn = daemon.spawn

        def spawn_at() -> Any:
            starts.append(self.clock.t)
            return real_spawn()
        daemon.spawn = spawn_at  # type: ignore[assignment]
        daemon.loop(max_ticks=2)
        self.assertEqual([t.astimezone(core.KST).strftime("%H:%M") for t in starts], ["15:17", "16:17"])
        self.assertEqual(spawned[0][-1], "tick")
        self.assertIn("--root", spawned[0])
        self.assertEqual(self.kills, [])
        self.assertEqual(self.store.read("children"), {"tick": None, "render": []})
        self.assertEqual(self.store.read_jsonl("errors"), [])  # exit 1 is recorded by the tick itself

    def test_overrun_kills_only_recorded_groups_and_marks_degraded(self) -> None:
        daemon, _ = self.daemon([(cli.TICK_CAP_SECONDS + 600.0, 0)])
        proc_dir = self.root / "proc"
        for pgid, cmd, group in ((5001, b"python3\0-I\0/opt/aiops/inspect/lib/control_plane_inspect_charts.py\0render",
                                  5001),
                                 (5002, b"bash\0-c\0sleep 1", 5002),
                                 (5003, b"python3\0control_plane_inspect_charts.py", 1)):
            (proc_dir / str(pgid)).mkdir(parents=True)
            (proc_dir / str(pgid) / "stat").write_text(f"{pgid} (python3) S 1 {group} {group} 0")
            (proc_dir / str(pgid) / "cmdline").write_bytes(cmd)
        original = daemon.spawn

        def spawn_and_record() -> Any:
            proc = original()
            return proc
        daemon.spawn = spawn_and_record  # type: ignore[assignment]

        orig_write = core.StateStore.write

        def write(store: core.StateStore, name: str, obj: Any) -> None:
            if name == "children" and obj.get("tick"):
                obj = dict(obj, render=[{"pgid": 5001}, {"pgid": 5002}, {"pgid": 5003}])
            orig_write(store, name, obj)
        core.StateStore.write = write  # type: ignore[assignment]
        self.addCleanup(setattr, core.StateStore, "write", orig_write)
        outcome = daemon.run_tick()
        self.assertEqual(outcome["outcome"], "OVERRUN")
        self.assertEqual(self.kills, [(-self.current.pid, signal.SIGKILL), (-5001, signal.SIGKILL)])
        self.assertIsNotNone(self.store.read("overrun"))
        self.assertEqual([r["reason"] for r in self.store.read_jsonl("errors")], ["OVERRUN"])
        # The next tick shows DEGRADED(OVERRUN) once, then clears the mark.
        result = self.tick()
        self.assertEqual(result["heartbeat"], "DEGRADED(OVERRUN)")
        self.assertIsNone(self.store.read("overrun"))

    def test_sigterm_flag_kills_tick_group_and_exits(self) -> None:
        daemon, _ = self.daemon([(10_000.0, 0)])

        def sleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            if self.current is not None:
                daemon.stopping = True
        self.deps.sleep = sleep
        self.clock.t = datetime(2026, 10, 1, 6, 16, 59, tzinfo=timezone.utc)
        daemon.loop()
        self.assertEqual(self.kills, [(-self.current.pid, signal.SIGKILL)])
        self.assertEqual(daemon.ticks, 1)

    def test_tick_crash_is_recorded(self) -> None:
        daemon, _ = self.daemon([(5.0, -11)])
        daemon.run_tick()
        self.assertEqual([r["reason"] for r in self.store.read_jsonl("errors")], ["TICK_CRASH"])

    def test_cmd_daemon_holds_lock_and_pid(self) -> None:
        ctx = cli.Ctx(self.root, self.deps)
        seen = {}

        def loop(self_daemon: cli.Daemon, max_ticks: Optional[int] = None) -> None:
            seen["held"] = cli.lock_held(ctx.paths.state / "daemon.lock")
            seen["pid"] = cli.read_pid(ctx)
        original = cli.Daemon.loop
        cli.Daemon.loop = loop  # type: ignore[assignment]
        self.addCleanup(setattr, cli.Daemon, "loop", original)
        self.assertEqual(cli.cmd_daemon(ctx)["status"], "STOPPED")
        self.assertEqual(seen, {"held": True, "pid": os.getpid()})
        self.assertIsNone(cli.read_pid(ctx))
        self.assertFalse(cli.lock_held(ctx.paths.state / "daemon.lock"))


class StartStopTests(Base):
    def hold_daemon(self, pid: int) -> cli.Lock:
        ctx = cli.Ctx(self.root, self.deps)
        lock = cli.Lock(ctx.lock_path("daemon.lock"))
        self.assertTrue(lock.acquire())
        (self.paths.state / "daemon.pid").write_text(f"{pid}\n")
        return lock

    def fake_proc(self, pid: int, uid: str = "0", cmdline: bytes = b"/usr/bin/python3\0-I\0/opt/aiops/bin/aiops-inspect\0daemon\0") -> Path:
        base = self.root / "proc" / str(pid)
        base.mkdir(parents=True)
        (base / "status").write_text(f"Name:\tpython3\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (base / "cmdline").write_bytes(cmdline)
        return base

    def test_start_spawns_and_reports_pid(self) -> None:
        held: List[cli.Lock] = []

        def spawn(argv: List[str]) -> int:
            self.assertEqual(argv[-1], "daemon")
            held.append(self.hold_daemon(31337))
            return 31337
        self.deps.spawn_daemon = spawn
        code, result, _ = self.cmd("start")
        self.assertEqual(result, {"status": "STARTED", "pid": 31337})
        code, result, _ = self.cmd("start")
        self.assertEqual(result["status"], "ALREADY_RUNNING")
        held[0].release()

    def test_start_fails_when_daemon_never_takes_lock(self) -> None:
        self.deps.spawn_daemon = lambda argv: 31338
        code, result, _ = self.cmd("start")
        self.assertEqual((code, result["reason"]), (1, "START_FAILED"))

    def test_stop_signals_verified_daemon_then_pauses(self) -> None:
        lock = self.hold_daemon(4321)
        proc = self.fake_proc(4321)

        def kill(pid: int, sig: int) -> None:
            self.kills.append((pid, sig))
            lock.release()
            for child in proc.iterdir():
                child.unlink()
            proc.rmdir()
        self.deps.kill = kill
        code, result, _ = self.cmd("stop")
        self.assertEqual((code, result["status"], result["heartbeat"]), (0, "STOPPED", "CREATED"))
        self.assertEqual(self.kills, [(4321, signal.SIGTERM)])
        heartbeat = [t for t in self.slack.texts("chat.postMessage") if "heartbeat" in t][0]
        self.assertIn("상태 PAUSED", heartbeat)

    def test_stop_cancels_scheduled_deadman(self) -> None:
        self.tick()
        self.slack.calls.clear()
        code, result, _ = self.cmd("stop")
        self.assertEqual(result["status"], "NOT_RUNNING")
        self.assertEqual(result["deadman_cancelled"], 1)
        self.assertIn("chat.deleteScheduledMessage", self.slack.names())
        self.assertIn("chat.update", self.slack.names())

    def test_stop_refuses_foreign_processes(self) -> None:
        cases = [("1", b"/opt/aiops/bin/aiops-inspect\0daemon\0", None),
                 ("1000", b"/opt/aiops/bin/aiops-inspect\0daemon\0", None),
                 ("0", b"/opt/aiops/bin/aiops-inspect\0tick\0", None),
                 ("0", b"/usr/bin/sleep\0daemon\0", None),
                 ("0", b"/tmp/not-aiops-inspect\0daemon\0", None)]
        for index, (uid, cmdline, _) in enumerate(cases):
            pid = 5000 + index
            lock = self.hold_daemon(pid)
            self.fake_proc(pid, uid, cmdline)
            code, result, _ = self.cmd("stop")
            self.assertEqual((code, result["reason"]), (1, "NOT_DAEMON"), cmdline)
            lock.release()
        self.assertEqual(self.kills, [])

    def test_stop_never_signals_pid_one_or_minus_one(self) -> None:
        self.fake_proc(1)
        for text in ("1", "-1", "0"):
            lock = self.hold_daemon(1)
            (self.paths.state / "daemon.pid").write_text(text + "\n")
            code, result, _ = self.cmd("stop")
            self.assertEqual((code, result["reason"]), (1, "NOT_DAEMON"), text)
            lock.release()
        self.assertEqual(self.kills, [])

    def test_stop_timeout(self) -> None:
        lock = self.hold_daemon(4400)
        self.addCleanup(lock.release)
        self.fake_proc(4400)
        code, result, _ = self.cmd("stop")
        self.assertEqual((code, result["reason"]), (1, "STOP_TIMEOUT"))
        self.assertEqual(self.kills, [(4400, signal.SIGTERM)])


if __name__ == "__main__":
    unittest.main()
