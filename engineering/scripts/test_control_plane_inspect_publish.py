from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_plane_inspect_core as core  # noqa: E402
from control_plane_inspect_core import InspectError  # noqa: E402
import control_plane_inspect_github as gh  # noqa: E402
import control_plane_inspect_publish as pub  # noqa: E402
import control_plane_inspect_signals as sig  # noqa: E402

MODULE = Path(__file__).resolve().parent / "control_plane_inspect_publish.py"
NOW = datetime(2026, 10, 1, 5, 17, tzinfo=timezone.utc)  # 10/01 14:17 KST
SLACK_TOKEN = "xoxb-" + "1234567890-abcdefghij-" * 2
GH_TOKEN = "github_pat_" + "A1b2C3d4E5" * 6
TEAM, BOT = "T01234567", "U01234567"
CHANNEL, TEST_CHANNEL = "C01234567", "C07654321"
CTRL_REPO = "BeautifulMind-JT/ai-ops-control-plane"
ZARI = "BeautifulMind-JT/ZARI"
KIXP = "BeautifulMind-JT/kix-protocol"
LEDGER = 5
SNAP = "ab" * 32
TOOL = "cd" * 32
FACTS_SHA = "ef" * 32
CONFIG = {"stage": "DRY", "control_repository": CTRL_REPO, "ledger_issue": 4, "test_ledger_issue": LEDGER,
          "slack": {"team_id": TEAM, "bot_user_id": BOT, "channel_id": CHANNEL, "test_channel_id": TEST_CHANNEL},
          "tick_minute": 17, "daily_hour_kst": 9, "deadman_hours": 3}
CONTRACT_METHODS = {"auth.test", "chat.postMessage", "chat.update", "chat.scheduleMessage",
                    "chat.deleteScheduledMessage", "files.getUploadURLExternal", "files.completeUploadExternal"}


def resp(obj, status=200, headers=None):
    return status, dict(headers or {}), json.dumps(obj).encode("utf-8")


class FakeSlack:
    """Slack transport fake: records calls, answers canned defaults or queued responses/exceptions."""

    def __init__(self, events=None):
        self.calls = []
        self.queue = {}
        self.ts = 1000
        self.n = 0
        self.events = events if events is not None else []
        self.on_call = None
        self.scopes = "chat:write,files:write"

    def push(self, name, *items):
        self.queue.setdefault(name, []).extend(items)

    def names(self):
        return [c["name"] for c in self.calls]

    def __call__(self, method, url, headers, body, timeout):
        if url.startswith(pub.SLACK_API):
            name = url[len(pub.SLACK_API):]
            params = dict(urllib.parse.parse_qsl((body or b"").decode("utf-8"), keep_blank_values=True))
        else:
            name, params = "UPLOAD", body
        self.calls.append({"name": name, "method": method, "url": url, "headers": dict(headers), "params": params,
                           "body": body})
        self.events.append(("slack", name))
        if self.on_call:
            self.on_call(name, params)
        queued = self.queue.get(name)
        if queued:
            item = queued.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.n += 1
        if name == "auth.test":
            return resp({"ok": True, "team_id": TEAM, "user_id": BOT}, headers={"X-OAuth-Scopes": self.scopes})
        if name == "chat.postMessage":
            self.ts += 1
            return resp({"ok": True, "ts": f"{self.ts}.000100", "channel": params.get("channel")})
        if name == "chat.scheduleMessage":
            return resp({"ok": True, "scheduled_message_id": f"Q{self.n:08d}", "post_at": int(params["post_at"])})
        if name == "files.getUploadURLExternal":
            return resp({"ok": True, "upload_url": f"https://files.slack.com/upload/v1/X{self.n}",
                         "file_id": f"F{self.n:08d}"})
        if name == "UPLOAD":
            return 200, {}, b"OK - 10"
        return resp({"ok": True})


class FakeGitHub:
    """Ledger-issue fake behind the real GitHubLedgerWriter."""

    def __init__(self, events=None):
        self.comments = []
        self.queue = {}
        self.calls = []
        self.next_id = 900
        self.events = events if events is not None else []

    def push(self, method, *items):
        self.queue.setdefault(method, []).extend(items)

    def add(self, body, at="2026-10-01T05:17:30Z"):
        self.next_id += 1
        comment = {"id": self.next_id, "body": body, "created_at": at, "updated_at": at,
                   "html_url": f"https://github.com/{CTRL_REPO}/issues/{LEDGER}#issuecomment-{self.next_id}"}
        self.comments.append(comment)
        return comment

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "body": body})
        self.events.append(("github", method))
        queued = self.queue.get(method)
        item = queued.pop(0) if queued else None
        if method == "POST":
            text = json.loads(body.decode("utf-8"))["body"]
            if item == "POSTED_THEN_UNKNOWN":
                self.add(text)
                raise InspectError("NET_UNKNOWN")
            if isinstance(item, BaseException):
                raise item
            if item is not None:
                return item
            return resp(self.add(text), 201)
        if method == "PATCH":
            if isinstance(item, BaseException):
                raise item
            return item or resp({"updated_at": "2026-10-01T05:17:40Z"})
        return resp(list(self.comments))


def finding(n, *, prefix="ZARI", product=ZARI, change="NEW", severity="AT_RISK", detail=None, signal="S6",
            subject=None, title="완료 공백", state=None):
    return {"id": f"INS-{prefix}-{n:04d}", "key": f"k{prefix}{n}", "signal": signal, "product": product,
            "subject_key": subject or f"node:N{n}", "severity": severity, "basis": "GH_SYSTEM", "title_ko": title,
            "detail_ko": detail or f"마지막 완료 뒤 {n}일이 지났다.", "state": state or (change or "OPEN"),
            "tick_change": change, "unknown": False, "first_seen": "2026-09-30T01:00:00Z",
            "evidence": [{"kind": "pr", "repository": product if product != "CTRL" else CTRL_REPO, "number": 12 + n}]}


LADDER = {"planned": 1, "materializing": 0, "not_started": 2, "in_progress": 1, "delivered": 0, "done": 3,
          "merge_checks": {"PASS": 2, "FAIL": 0, "PENDING": 1, "NOT_CONFIGURED": 0},
          "deployed": "NOT_RECORDED", "verified": "NOT_RECORDED", "orphans": 1, "pending_plan_prs": 0}


def make_result(findings=None, previous=None, verdict="WATCH"):
    findings = [finding(1)] if findings is None else findings
    evaluation = {"order": [ZARI, KIXP], "info": [],
                  "products": {ZARI: {"prefix": "ZARI", "name": "ZARI", "verdict": verdict, "paused": False,
                                      "plan_state": "PRESENT", "ladder": dict(LADDER), "unknown_groups": []},
                               KIXP: {"prefix": "KIXP", "name": "kix-protocol", "verdict": "PAUSED", "paused": True,
                                      "plan_state": "NONE", "ladder": {}, "unknown_groups": []}},
                  "ctrl": {"verdict": "ON_TRACK"}}
    state = {"schema": sig.STATE_SCHEMA, "counters": {}, "findings": {f["key"]: f for f in findings},
             "verdicts": {ZARI: verdict, KIXP: "PAUSED", "CTRL": "ON_TRACK"}}
    changes = [{"id": f["id"], "change": f["tick_change"]} for f in findings if f["tick_change"]]
    return {"evaluation": evaluation, "state": state, "changes": changes, "verdicts": state["verdicts"],
            "previous_verdicts": previous if previous is not None else {ZARI: "ON_TRACK", KIXP: "PAUSED",
                                                                        "CTRL": "ON_TRACK"}}


FACTS = {"lanes": {"active_total": 1, "max_active_sessions": 4,
                   "lanes": [{"lane": "DEVIN", "enabled": True, "active": [{"task": "ZARI-N1", "role": "WRITER"}]},
                             {"lane": "GROK_BUILD", "enabled": True, "active": []},
                             {"lane": "GLM", "enabled": False, "active": []},
                             {"lane": "CURSOR", "enabled": True, "active": []}]},
         "stats": {"github_calls": 41, "host_calls": 12, "seconds": 9.5}, "unknown": []}


def make_report(run="20261001T051700Z-0a1b2c3d-tick", result=None, pngs=(), render_dir=None, now=NOW):
    return pub.build_report(run=run, now=now, stage="DRY", snapshot=SNAP, tool_sha256=TOOL, facts=FACTS,
                            facts_sha256=FACTS_SHA, result=result or make_result(), control_repository=CTRL_REPO,
                            ledger_issue=LEDGER, pngs=pngs, render_dir=render_dir)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = core.StateStore(self.root / "state")
        self.events = []
        self.slack_t = FakeSlack(self.events)
        self.gh_t = FakeGitHub(self.events)

    def tearDown(self):
        self.tmp.cleanup()

    def publisher(self, slack=True):
        writer = gh.GitHubLedgerWriter(GH_TOKEN, CTRL_REPO, LEDGER, transport=self.gh_t)
        client = pub.SlackClient(SLACK_TOKEN, TEAM, BOT, transport=self.slack_t,
                                 live_values=(GH_TOKEN,)) if slack else None
        return pub.Publisher(self.store, CONFIG, writer=writer, slack=client, live_values=(GH_TOKEN, SLACK_TOKEN))

    def pngs(self):
        out = []
        render = self.root / "render"
        render.mkdir(exist_ok=True)
        for name in ("c5_scorecard.png", "c1_ladder.png"):
            data = pub.PNG_SIGNATURE + name.encode()
            (render / name).write_bytes(data)
            out.append({"name": name, "sha256": core.sha256_hex(data), "w": 10, "h": 10})
        return out, render

    def client(self, verify=True):
        client = pub.SlackClient(SLACK_TOKEN, TEAM, BOT, transport=self.slack_t, live_values=(GH_TOKEN,))
        if verify:
            client.verify()
        return client


# ---------------------------------------------------------------------------- source scan

SLACK_NAMESPACES = ("admin", "api", "apps", "assistant", "auth", "bookmarks", "bots", "calls", "canvases",
                    "channels", "chat", "conversations", "dialog", "dnd", "emoji", "files", "functions", "groups",
                    "im", "migration", "mpim", "oauth", "openid", "pins", "reactions", "reminders", "rtm", "search",
                    "stars", "team", "tooling", "usergroups", "users", "views", "workflows")
METHOD_LIKE = re.compile(r"\b(?:%s)\.[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)*" % "|".join(SLACK_NAMESPACES))


class TestSourceScan(unittest.TestCase):
    def setUp(self):
        self.source = MODULE.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_method_list_is_the_contract_list(self):
        self.assertEqual(set(pub.SLACK_METHODS), CONTRACT_METHODS)
        self.assertEqual(len(pub.SLACK_METHODS), len(CONTRACT_METHODS))

    def test_only_allowed_slack_method_names_appear(self):
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.update(METHOD_LIKE.findall(node.value))
        found.discard("files.slack.com")  # the upload host, not a method
        self.assertTrue(found)
        self.assertLessEqual(found, CONTRACT_METHODS, sorted(found - CONTRACT_METHODS))
        # Also no method-like name anywhere in comments or code outside the allowed list.
        raw = set(METHOD_LIKE.findall(self.source)) - {"files.slack.com"}
        extra = {m for m in raw if m not in CONTRACT_METHODS and not m.startswith(("team.", "users."))}
        self.assertEqual(extra, set())

    def test_every_call_site_names_a_listed_constant(self):
        used = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_call":
                self.assertTrue(node.args and isinstance(node.args[0], ast.Constant), ast.dump(node))
                used.add(node.args[0].value)
        self.assertEqual(used, CONTRACT_METHODS)

    def test_no_other_http_hosts_in_source(self):
        hosts = set(re.findall(r"https://([A-Za-z0-9.-]+)", self.source))
        self.assertLessEqual(hosts, {"slack.com", "files.slack.com", "github.com"})

    def test_call_refuses_unlisted_method_without_network(self):
        transport = FakeSlack()
        client = pub.SlackClient(SLACK_TOKEN, TEAM, BOT, transport=transport)
        client.verified = True
        for name in ("conversations.history", "chat.delete", "users.list", "files.upload"):
            with self.assertRaises(InspectError) as ctx:
                client._call(name, {})
            self.assertEqual(ctx.exception.reason, "SLACK_METHOD")
        self.assertEqual(transport.calls, [])

    def test_default_transport_refuses_other_urls(self):
        transport = pub.SlackUrllibTransport(opener_factory=lambda state: self.fail("must not open"))
        for method, url in (("POST", "https://slack.com/api/conversations.history"),
                            ("POST", "https://evil.example/api/chat.postMessage"),
                            ("GET", "https://slack.com/api/auth.test"),
                            ("POST", "https://files.slack.com.evil.example/upload/v1/x")):
            with self.assertRaises(InspectError) as ctx:
                transport(method, url, {}, b"", 1.0)
            self.assertEqual(ctx.exception.reason, "PATH_NOT_ALLOWED")


# ---------------------------------------------------------------------------- Slack client

class TestSlackClient(unittest.TestCase):
    def setUp(self):
        self.t = FakeSlack()

    def client(self):
        return pub.SlackClient(SLACK_TOKEN, TEAM, BOT, transport=self.t, live_values=(GH_TOKEN,))

    def test_verify_checks_identity_and_exact_scopes(self):
        client = self.client()
        self.assertEqual(client.verify()["scopes"], ["chat:write", "files:write"])
        self.assertTrue(client.verified)
        self.t.scopes = " files:write , chat:write "
        self.assertTrue(self.client().verify())
        for scopes in ("chat:write", "chat:write,files:write,channels:read", "chat:write,files:write,users:read", ""):
            self.t.scopes = scopes
            client = self.client()
            with self.assertRaises(InspectError) as ctx:
                client.verify()
            self.assertEqual(ctx.exception.reason, "SLACK_SCOPE")
            self.assertFalse(client.verified)

    def test_verify_missing_header_or_wrong_identity(self):
        self.t.push("auth.test", resp({"ok": True, "team_id": TEAM, "user_id": BOT}))
        with self.assertRaises(InspectError) as ctx:
            self.client().verify()
        self.assertEqual(ctx.exception.reason, "SLACK_SCOPE")
        self.t.push("auth.test", resp({"ok": True, "team_id": "T99999999", "user_id": BOT},
                                      headers={"x-oauth-scopes": "chat:write,files:write"}))
        with self.assertRaises(InspectError) as ctx:
            self.client().verify()
        self.assertEqual(ctx.exception.reason, "SLACK_SCOPE")
        self.t.push("auth.test", resp({"ok": False, "error": "invalid_auth"}))
        with self.assertRaises(InspectError) as ctx:
            self.client().verify()
        self.assertEqual(ctx.exception.reason, "SLACK_AUTH")
        self.t.push("auth.test", InspectError("NET_DOWN"))
        with self.assertRaises(InspectError) as ctx:
            self.client().verify()
        self.assertEqual(ctx.exception.reason, "SLACK_AUTH")

    def test_no_write_before_verify(self):
        client = self.client()
        with self.assertRaises(InspectError) as ctx:
            client.post_message(CHANNEL, "hello")
        self.assertEqual(ctx.exception.reason, "SLACK_NOT_VERIFIED")
        self.assertEqual(self.t.calls, [])

    def test_post_request_shape_and_token_only_in_header(self):
        client = self.client()
        client.verify()
        client.post_message(CHANNEL, "hi there", thread_ts="1000.000100")
        call = self.t.calls[-1]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://slack.com/api/chat.postMessage")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {SLACK_TOKEN}")
        self.assertEqual(call["params"], {"channel": CHANNEL, "text": "hi there", "parse": "none",
                                          "unfurl_links": "false", "unfurl_media": "false",
                                          "thread_ts": "1000.000100"})
        self.assertNotIn(SLACK_TOKEN.encode(), call["body"])
        self.assertNotIn(SLACK_TOKEN, repr(client))

    def test_outcome_mapping(self):
        client = self.client()
        client.verify()
        cases = [(resp({"ok": False, "error": "channel_not_found"}), "REFUSED", "channel_not_found"),
                 (resp({"error": "x"}, 429), "REFUSED", None),
                 (resp({"ok": True}, 500), "UNKNOWN", None),
                 ((200, {}, b"not json"), "UNKNOWN", None),
                 (InspectError("NET_UNKNOWN"), "UNKNOWN", None),
                 (InspectError("NET_DOWN"), "REFUSED", None),
                 (RuntimeError("reset"), "UNKNOWN", None)]
        for answer, outcome, error in cases:
            self.t.push("chat.postMessage", answer)
            with self.assertRaises(pub.SlackError) as ctx:
                client.post_message(CHANNEL, "x")
            self.assertEqual((ctx.exception.outcome, ctx.exception.slack_error), (outcome, error), answer)

    def test_live_secret_never_leaves(self):
        client = self.client()
        client.verify()
        before = len(self.t.calls)
        with self.assertRaises(InspectError) as ctx:
            client.post_message(CHANNEL, f"oops {GH_TOKEN}")
        self.assertEqual(ctx.exception.reason, "SECRET_LIVE")
        self.assertEqual(len(self.t.calls), before)

    def test_shape_tokens_are_redacted(self):
        client = self.client()
        client.verify()
        client.post_message(CHANNEL, "leak ghp_" + "Q" * 30)
        self.assertNotIn("ghp_", self.t.calls[-1]["params"]["text"])
        self.assertIn("[redacted sha256=", self.t.calls[-1]["params"]["text"])
        self.assertEqual(client.shape_hits, 1)

    def test_external_upload_sequence(self):
        client = self.client()
        client.verify()
        data = pub.PNG_SIGNATURE + b"img"
        out = client.upload_files(CHANNEL, "1000.000100", [{"name": "c1_ladder.png", "data": data, "title": "C1"}])
        names = self.t.names()[1:]
        self.assertEqual(names, ["files.getUploadURLExternal", "UPLOAD", "files.completeUploadExternal"])
        get, upload, complete = self.t.calls[1:]
        self.assertEqual(get["params"], {"filename": "c1_ladder.png", "length": str(len(data))})
        self.assertTrue(upload["url"].startswith("https://files.slack.com/upload/"))
        self.assertEqual(upload["method"], "POST")
        self.assertNotIn("Authorization", upload["headers"])
        self.assertEqual(upload["body"], data)
        self.assertEqual(json.loads(complete["params"]["files"]), [{"id": out["file_ids"][0], "title": "C1"}])
        self.assertEqual(complete["params"]["channel_id"], CHANNEL)
        self.assertEqual(complete["params"]["thread_ts"], "1000.000100")

    def test_upload_url_must_be_slack_files_host(self):
        client = self.client()
        client.verify()
        for url in ("https://evil.example/upload/v1/x", "http://files.slack.com/upload/v1/x",
                    "https://user@files.slack.com/upload/v1/x", "https://files.slack.com:8443/upload/v1/x"):
            self.t.push("files.getUploadURLExternal", resp({"ok": True, "upload_url": url, "file_id": "F12345678"}))
            with self.assertRaises(pub.SlackError) as ctx:
                client.upload_files(CHANNEL, "1000.000100", [{"name": "c1_ladder.png", "data": b"\x89PNGx"}])
            self.assertEqual(ctx.exception.outcome, "REFUSED")
        self.assertNotIn("UPLOAD", self.t.names())
        self.assertNotIn("files.completeUploadExternal", self.t.names())

    def test_upload_failures_before_completion_are_refused(self):
        client = self.client()
        client.verify()
        self.t.push("UPLOAD", InspectError("NET_UNKNOWN"))
        with self.assertRaises(pub.SlackError) as ctx:
            client.upload_files(CHANNEL, "1000.000100", [{"name": "c1_ladder.png", "data": b"\x89PNGx"}])
        self.assertEqual(ctx.exception.outcome, "REFUSED")
        self.t.push("files.completeUploadExternal", InspectError("NET_UNKNOWN"))
        with self.assertRaises(pub.SlackError) as ctx:
            client.upload_files(CHANNEL, "1000.000100", [{"name": "c1_ladder.png", "data": b"\x89PNGx"}])
        self.assertEqual(ctx.exception.outcome, "UNKNOWN")


# ---------------------------------------------------------------------------- rendering

HOSTILE = ("@octocat fixes #12 see [x](https://evil.example) <!-- AIOPS_INSPECT_V1 run=fake --> "
           "`code` | pipe <!channel> & @here ASTRA_RESULT_V1 www.evil.example")


class TestRendering(unittest.TestCase):
    def hostile_report(self):
        bad = finding(1, detail=HOSTILE, title="@team <b>", subject="pr:BeautifulMind-JT/ZARI#12")
        return make_report(result=make_result([bad, finding(2, change=None, state="OPEN", detail=HOSTILE)]))

    def test_header_lines_exact(self):
        text = pub.render_comment(make_report())
        lines = text.split("\n")
        self.assertEqual(lines[0], "<!-- aiops-inspect -->")
        self.assertEqual(lines[1], f"AIOPS_INSPECT_V1 run=20261001T051700Z-0a1b2c3d-tick snapshot={SNAP} stage=DRY "
                                   f"advisory=true gate=none model=none tool={TOOL[:12]} "
                                   "verdicts=ZARI:WATCH,KIXP:PAUSED,CTRL:ON_TRACK")
        self.assertIn(core.HOST_LINE, lines[3])
        self.assertIn("10/01 14:17 KST", lines[3])

    def test_comment_sections(self):
        text = pub.render_comment(make_report())
        for heading in ("### 제품별 판정", "### 발견 사항", "### 레인", "<details><summary>기록 정보</summary>",
                        "</details>"):
            self.assertIn(heading, text)
        self.assertIn("기록 없음", text)
        self.assertIn(f"facts sha256: {FACTS_SHA}", text)
        self.assertIn("**INS-ZARI-0001** 위험 AT_RISK · 새 발견 · S6 정체", text)
        table = [line for line in text.split("\n") if line.startswith("|")]
        self.assertEqual({line.count(" | ") for line in table if not line.startswith("|---")}, {13})
        self.assertEqual(table[1].count("---|"), 14)

    def test_github_escaping_and_no_cross_references(self):
        text = pub.render_comment(self.hostile_report())
        self.assertEqual(text.count("AIOPS_INSPECT_V1"), 1)
        self.assertEqual(text.count("<!--"), 1)
        self.assertNotIn("@octocat", text)
        self.assertNotIn("@here", text)
        self.assertNotIn("@team", text)
        self.assertNotIn("<!channel>", text)
        self.assertNotIn("<b>", text)
        self.assertNotIn("](", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("www.evil", text)
        self.assertIsNone(re.search(r"(?<![\\`/A-Za-z-])#\d", text.replace("`BeautifulMind-JT/ZARI#13`", "")))
        self.assertIn("`BeautifulMind-JT/ZARI#13`", text)  # evidence as a code span
        for line in text.split("\n")[2:]:
            self.assertFalse(line.startswith(("AIOPS_", "ASTRA_", "<!--")), line)

    def test_issue_body_escaped_and_table_hash_is_time_free(self):
        state = make_result([finding(1, detail=HOSTILE, title="@team #9")])["state"]
        body, table = pub.render_issue_body("DRY", NOW, "20261001T051700Z-0a1b2c3d-tick", state)
        body2, table2 = pub.render_issue_body("DRY", NOW + timedelta(hours=1), "20261001T061700Z-0a1b2c3d-tick", state)
        self.assertEqual(table, table2)
        self.assertNotEqual(body, body2)
        self.assertTrue(body.startswith("<!-- aiops-inspect -->\nAIOPS_INSPECT_V1 ledger stage=DRY"))
        self.assertIn(core.HOST_LINE, body)
        self.assertNotIn("@team", body)
        self.assertNotIn("#9", body)
        state["findings"]["kZARI1"]["severity"] = "WATCH"
        self.assertNotEqual(pub.render_issue_body("DRY", NOW, "r", state)[1], table)

    def test_card_text(self):
        card = pub.render_card(make_report())
        lines = card.split("\n")
        self.assertEqual(lines[0], "AIOPS_INSPECT_V1 card · 10/01 14:17 KST · DRY · run 20261001T051700Z-0a1b2c3d-tick")
        self.assertEqual(lines[1], "ZARI: 주의 WATCH ↑ (전: 정상) · 새 발견 INS-ZARI-0001")
        self.assertEqual(lines[2], "kix-protocol: 멈춤 PAUSED = (전: 멈춤) · 계획 없음 · 새 발견 없음")
        self.assertEqual(lines[3], "CTRL: 정상 ON_TRACK = (전: 정상) · 새 발견 없음")
        self.assertIn("진행 ZARI: 계획 1 · 생성 중 0 · 시작 전 2 · 진행 중 1 · 전달 0 · 완료 3 (검사 통과 2·실패 0·대기 1)", card)
        self.assertIn("배포·실사용 검증 기록 없음", card)
        self.assertIn("레인: DEVIN 1 (ZARI-N1 WRITER) · GROK_BUILD 0 · GLM 꺼짐 · CURSOR 0 · 동시 세션 1/4", card)
        self.assertEqual(lines[-1], core.HOST_LINE)
        self.assertNotIn("github.com", card)
        linked = pub.card_with_link(card, f"https://github.com/{CTRL_REPO}/issues/5#issuecomment-901")
        self.assertTrue(linked.endswith(f"<https://github.com/{CTRL_REPO}/issues/5#issuecomment-901|GitHub 기록>"))
        self.assertEqual(pub.card_with_link(card, "https://evil.example/x"), card + "\nGitHub 기록")

    def test_first_run_arrow_and_better(self):
        report = make_report(result=make_result(previous={}))
        self.assertIn("ZARI: 주의 WATCH ~ (전: 없음)", pub.render_card(report))
        report = make_report(result=make_result(previous={ZARI: "AT_RISK"}))
        self.assertIn("ZARI: 주의 WATCH ↓ (전: 위험)", pub.render_card(report))

    def test_replies_escaped_linked_and_capped(self):
        report = make_report(result=make_result([finding(i, detail=HOSTILE if i == 1 else None) for i in range(1, 16)]
                                                + [finding(30, change=None, state="OPEN")]))
        replies = pub.render_replies(report)
        self.assertEqual(len(replies), 10)
        first = replies[0]["text"]
        self.assertIn("<https://github.com/BeautifulMind-JT/ZARI/pull/13|BeautifulMind-JT/ZARI#13>", first)
        self.assertIn("&lt;!channel&gt;", first)
        self.assertNotIn("<!channel>", first)
        self.assertIn("@​here", first)
        self.assertIn("&lt;!-- ​AIOPS_INSPECT_V1", first)
        self.assertNotIn("INS-ZARI-0030", "".join(r["text"] for r in replies))

    def test_slack_ledger_comment_link(self):
        ev = {"kind": "issue", "repository": CTRL_REPO, "ref": "issuecomment-77"}
        self.assertEqual(pub.github_url(ev, LEDGER, CTRL_REPO),
                         f"https://github.com/{CTRL_REPO}/issues/{LEDGER}#issuecomment-77")
        self.assertEqual(pub.evidence_ref_gh(ev), f"`{CTRL_REPO} comment 77`")
        self.assertIsNone(pub.github_url({"kind": "pr", "repository": "x y", "number": 1}))

    def test_fitting_drops_open_details_first(self):
        long_detail = "상세 " + "가" * 390
        changed = [finding(i, detail=f"변화 상세 {i}") for i in range(1, 4)]
        still = [finding(100 + i, change=None, state="OPEN", detail=long_detail) for i in range(400)]
        report = make_report(result=make_result(changed + still))
        text = pub.render_comment(report)
        self.assertLessEqual(len(text), 60000)
        self.assertIn("변화 상세 1", text)  # changed findings keep their details
        self.assertNotIn(long_detail[:50], text)
        self.assertTrue(text.startswith("<!-- aiops-inspect -->\nAIOPS_INSPECT_V1 run="))
        self.assertIn("</details>", text)
        full = pub.render_comment(report, limit=10 ** 7)
        self.assertGreater(len(full), 60000)

    def test_fitting_extreme(self):
        changed = [finding(i, detail="가" * 390) for i in range(1, 3001)]
        text = pub.render_comment(make_report(result=make_result(changed)))
        self.assertLessEqual(len(text), 60000)
        self.assertIn("길이 제한으로 변화", text)
        self.assertIn(core.HOST_LINE, text)
        self.assertIn("</details>", text)

    def test_heartbeat_daily_deadman_texts(self):
        token = pub.token_warning({"gh-read": NOW + timedelta(days=5, hours=2), "gh-ledger": NOW + timedelta(days=40),
                                   "slack": None}, NOW)
        text = pub.heartbeat_text(last_tick=NOW, status="DEGRADED", reason="GITHUB_READ",
                                  next_tick=pub.next_tick_at(NOW, 17), open_n=4, at_risk=1, unknown_m=2, token=token)
        self.assertEqual(text, "AIOPS_INSPECT_V1 heartbeat · 마지막 점검 10/01 14:17 KST · 상태 DEGRADED(GITHUB_READ) · "
                               "다음 점검 10/01 15:17 KST · 열린 발견 4 (위험 1) · 게시 미확인 2 · "
                               "토큰 만료 D-5 (gh-read 10/06 16:17 KST)")
        self.assertIsNone(pub.token_warning({"gh-read": NOW + timedelta(days=15)}, NOW))
        halted = pub.heartbeat_text(last_tick=NOW, status="HALTED", reason="TOOL_TAMPERED", next_tick=NOW,
                                    open_n=0, at_risk=0, unknown_m=0, halted_at=NOW - timedelta(hours=2))
        self.assertIn("상태 HALTED(TOOL_TAMPERED) · 정지 시작 10/01 12:17 KST", halted)
        self.assertNotIn("토큰", halted)
        self.assertEqual(pub.daily_text(NOW, 23, 24, 2, 5, 0),
                         "AIOPS_INSPECT_V1 daily · 2026-10-01 · 점검 23/24 · 카드 2 · 열린 발견 5 · 게시 미확인 0")
        self.assertEqual(pub.deadman_text(3, NOW),
                         "AIOPS_INSPECT_V1 status=STALE · 감리가 3시간 넘게 점검하지 못했습니다 · 마지막 점검 10/01 14:17 KST"
                         " · 그록봇에게 \"aiops-inspect status\" 실행을 지시하세요")
        self.assertEqual(pub.recovered_text(4), "AIOPS_INSPECT_V1 status=RECOVERED · 공백 4시간")
        for sample in (text, halted, pub.render_card(make_report()), pub.render_comment(make_report())):
            self.assertNotRegex(sample, r"분 전|시간 전|일 전|\bago\b")

    def test_next_tick(self):
        self.assertEqual(pub.next_tick_at(NOW, 17), NOW + timedelta(hours=1))
        self.assertEqual(pub.next_tick_at(NOW - timedelta(minutes=1), 17), NOW)

    def test_publish_needed(self):
        result = make_result()
        self.assertTrue(pub.publish_needed(result, False))
        result["changes"] = []
        self.assertTrue(pub.publish_needed(result, False))  # ZARI ON_TRACK -> WATCH
        result["previous_verdicts"] = dict(result["verdicts"])
        self.assertFalse(pub.publish_needed(result, False))
        self.assertTrue(pub.publish_needed(result, True))


# ---------------------------------------------------------------------------- journal

class TestJournal(Base):
    def run_id(self, hour=5):
        return f"20261001T{hour:02d}1700Z-0a1b2c3d-tick"

    def test_happy_path_order_and_states(self):
        pngs, render = self.pngs()
        publisher = self.publisher()
        entry = publisher.prepare(make_report(pngs=pngs, render_dir=render), NOW)
        self.assertEqual(entry["state"], "PREPARED")
        self.assertEqual(entry["comment"]["sha256"], core.sha256_hex(entry["comment"]["body"].encode()))
        out = publisher.advance(NOW)
        self.assertEqual(out["state"], "SLACK_POSTED")
        self.assertEqual((out["comment"], out["issue_body"], out["card"], out["charts"]),
                         ("POSTED", "POSTED", "POSTED", "POSTED"))
        self.assertEqual(out["replies"], ["POSTED"])
        journal = publisher.load(self.run_id())
        self.assertEqual([s["step"] for s in journal["log"]], ["PREPARED", "GH_POSTED", "GH_BODY_UPDATED", "SLACK_POSTED"])
        self.assertEqual(len(set(journal["charts"]["file_ids"])), 2)
        # GitHub before Slack; the card links the POSTED comment; charts and replies go into the card's thread.
        self.assertEqual(self.events[:3], [("github", "POST"), ("github", "PATCH"), ("slack", "auth.test")])
        card = self.slack_t.calls[1]
        self.assertEqual(card["name"], "chat.postMessage")
        self.assertIn(f"issuecomment-{journal['comment']['id']}|GitHub 기록>", card["params"]["text"])
        complete = [c for c in self.slack_t.calls if c["name"] == "files.completeUploadExternal"][0]
        self.assertEqual(complete["params"]["thread_ts"], journal["card"]["ts"])
        reply = self.slack_t.calls[-1]
        self.assertEqual(reply["params"]["thread_ts"], journal["card"]["ts"])
        self.assertEqual(self.store.read("daily")["counters"]["cards"], 1)
        # Nothing is posted twice.
        publisher.advance(NOW + timedelta(hours=1))
        self.assertEqual(len(self.gh_t.calls), 2)
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 2)
        self.assertEqual(oct(os.stat(self.store.path(f"publish/{self.run_id()}")).st_mode & 0o777), "0o600")

    def test_github_unknown_is_never_reposted_and_later_adopted(self):
        self.gh_t.push("POST", "POSTED_THEN_UNKNOWN")
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        out = publisher.advance(NOW)
        self.assertEqual(out["comment"], "UNKNOWN")
        self.assertEqual(out["state"], "SLACK_POSTED")  # Slack goes ahead after UNKNOWN ...
        card = [c for c in self.slack_t.calls if c["name"] == "chat.postMessage"][0]
        self.assertNotIn("github.com", card["params"]["text"])  # ... but without a link
        self.assertEqual(publisher.unknown_count(NOW), 1)
        for hour in (1, 2):
            publisher = self.publisher()
            publisher.advance(NOW + timedelta(hours=hour))
        posts = [c for c in self.gh_t.calls if c["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        # A foreign comment with another body is not adopted; the identical body is.
        self.gh_t.add("<!-- aiops-inspect -->\nAIOPS_INSPECT_V1 run=forged")
        result = publisher.reconcile_unknown(NOW + timedelta(hours=3))
        self.assertEqual(result["adopted"], [self.run_id()])
        journal = publisher.load(self.run_id())
        self.assertEqual(journal["comment"]["state"], "POSTED")
        self.assertEqual(journal["comment"]["id"], self.gh_t.comments[0]["id"])
        self.assertIn("GH_POSTED", [s["step"] for s in journal["log"]])
        self.assertEqual(publisher.unknown_count(NOW + timedelta(hours=3)), 0)
        get = [c for c in self.gh_t.calls if c["method"] == "GET"][0]
        self.assertIn("since=2026-10-01T05%3A12%3A00Z", get["url"])

    def test_unknown_unseen_is_abandoned_after_24h(self):
        self.gh_t.push("POST", InspectError("NET_UNKNOWN"))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        publisher.advance(NOW)
        self.assertEqual(publisher.reconcile_unknown(NOW + timedelta(hours=23))["pending"], 1)
        self.assertEqual(publisher.unknown_count(NOW + timedelta(hours=23)), 1)
        result = publisher.reconcile_unknown(NOW + timedelta(hours=24))
        self.assertEqual(result["abandoned"], [self.run_id()])
        journal = publisher.load(self.run_id())
        self.assertEqual(journal["state"], "SLACK_POSTED")
        self.assertEqual(journal["comment"]["state"], "ABANDONED")
        self.assertIn("GH_ABANDONED_UNKNOWN", [s["step"] for s in journal["log"]])
        self.assertEqual(publisher.unknown_count(NOW + timedelta(hours=24)), 0)
        self.assertEqual(len([c for c in self.gh_t.calls if c["method"] == "POST"]), 1)

    def test_github_5xx_is_unknown(self):
        self.gh_t.push("POST", resp({"message": "boom"}, 502))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        self.assertEqual(publisher.advance(NOW)["comment"], "UNKNOWN")

    def test_refused_is_retried_at_most_three_times(self):
        self.gh_t.push("POST", resp({"message": "no"}, 422), resp({"message": "expired"}, 401),
                       resp({"message": "no"}, 403))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        states = [publisher.advance(NOW + timedelta(hours=h))["comment"] for h in range(5)]
        self.assertEqual(states, ["REFUSED", "REFUSED", "FAILED", "FAILED", "FAILED"])
        self.assertEqual(len([c for c in self.gh_t.calls if c["method"] == "POST"]), 3)
        self.assertEqual(self.slack_t.calls, [])  # Slack never posts before GitHub is POSTED or UNKNOWN
        journal = publisher.load(self.run_id())
        self.assertEqual(journal["card"]["state"], "SKIPPED")
        self.assertEqual(journal["state"], "GH_FAILED")

    def test_refused_then_posted(self):
        self.gh_t.push("POST", resp({"message": "no"}, 422))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        self.assertEqual(publisher.advance(NOW)["comment"], "REFUSED")
        self.assertEqual(self.slack_t.calls, [])
        out = self.publisher().advance(NOW + timedelta(hours=1))
        self.assertEqual((out["comment"], out["card"]), ("POSTED", "POSTED"))

    def test_crash_while_sending_reads_as_unknown(self):
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        name = f"publish/{self.run_id()}"
        journal = self.store.read(name)
        journal["comment"].update(state="SENDING", attempts=1, at=core.iso(NOW))
        self.store.write(name, journal)
        out = self.publisher().advance(NOW + timedelta(hours=1))
        self.assertEqual(out["comment"], "UNKNOWN")
        self.assertEqual([c for c in self.gh_t.calls if c["method"] == "POST"], [])
        self.assertEqual(self.store.read(name)["comment"]["error"], "CRASH_WHILE_SENDING")

    def test_slack_unknown_card_is_not_resent(self):
        self.slack_t.push("chat.postMessage", InspectError("NET_UNKNOWN"))
        pngs, render = self.pngs()
        publisher = self.publisher()
        publisher.prepare(make_report(pngs=pngs, render_dir=render), NOW)
        out = publisher.advance(NOW)
        self.assertEqual((out["card"], out["charts"], out["replies"], out["state"]),
                         ("UNKNOWN", "SKIPPED", ["SKIPPED"], "SLACK_UNKNOWN"))
        self.publisher().advance(NOW + timedelta(hours=1))
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 1)
        self.assertNotIn("files.getUploadURLExternal", self.slack_t.names())
        self.assertEqual(self.publisher().unknown_count(NOW + timedelta(hours=1)), 1)

    def test_slack_refused_card_retried_with_cap(self):
        for _ in range(4):
            self.slack_t.push("chat.postMessage", resp({"ok": False, "error": "not_in_channel"}))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        states = [self.publisher().advance(NOW + timedelta(hours=h))["card"] for h in range(4)]
        self.assertEqual(states, ["REFUSED", "REFUSED", "FAILED", "FAILED"])
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 3)

    def test_slack_unavailable_defers_without_attempts(self):
        self.slack_t.push("auth.test", InspectError("NET_DOWN"))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        out = publisher.advance(NOW)
        self.assertEqual((out["comment"], out["card"], out["slack"]), ("POSTED", "PENDING", "SLACK_AUTH"))
        self.assertEqual(publisher.load(self.run_id())["card"]["attempts"], 0)
        self.assertEqual(self.publisher().advance(NOW + timedelta(hours=1))["card"], "POSTED")

    def test_slack_scope_propagates(self):
        self.slack_t.scopes = "chat:write,files:write,channels:history"
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        with self.assertRaises(InspectError) as ctx:
            publisher.advance(NOW)
        self.assertEqual(ctx.exception.reason, "SLACK_SCOPE")
        self.assertEqual([c["name"] for c in self.slack_t.calls], ["auth.test"])

    def test_issue_body_only_when_table_changes(self):
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        publisher.advance(NOW)
        publisher = self.publisher()
        entry = publisher.prepare(make_report(run=self.run_id(6), now=NOW + timedelta(hours=1)),
                                  NOW + timedelta(hours=1))
        self.assertEqual(entry["issue_body"]["state"], "UNCHANGED")
        publisher.advance(NOW + timedelta(hours=1))
        self.assertEqual([c["method"] for c in self.gh_t.calls], ["POST", "PATCH", "POST"])
        result = make_result([finding(1), finding(2)])
        entry = self.publisher().prepare(make_report(run=self.run_id(7), result=result), NOW + timedelta(hours=2))
        self.assertEqual(entry["issue_body"]["state"], "PENDING")

    def test_body_unknown_is_not_patched_again(self):
        self.gh_t.push("PATCH", InspectError("NET_UNKNOWN"))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        self.assertEqual(publisher.advance(NOW)["issue_body"], "UNKNOWN")
        entry = self.publisher().prepare(make_report(run=self.run_id(6)), NOW + timedelta(hours=1))
        self.assertEqual(entry["issue_body"]["state"], "UNCHANGED")

    def test_newer_run_supersedes_older_retries(self):
        self.gh_t.push("POST", resp({"message": "no"}, 422))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        publisher.advance(NOW)
        publisher.prepare(make_report(run=self.run_id(6)), NOW + timedelta(hours=1))
        publisher.advance(NOW + timedelta(hours=1))
        old = publisher.load(self.run_id())
        self.assertEqual(old["comment"]["state"], "SUPERSEDED")
        self.assertEqual(publisher.load(self.run_id(6))["state"], "SLACK_POSTED")
        self.assertEqual(len([c for c in self.gh_t.calls if c["method"] == "POST"]), 2)

    def test_prepare_refuses_duplicate_and_live_secret(self):
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        with self.assertRaises(InspectError) as ctx:
            publisher.prepare(make_report(), NOW)
        self.assertEqual(ctx.exception.reason, "JOURNAL_EXISTS")
        leaky = make_result([finding(1, detail="token " + GH_TOKEN)])
        with self.assertRaises(InspectError) as ctx:
            publisher.prepare(make_report(run=self.run_id(6), result=leaky), NOW)
        self.assertEqual(ctx.exception.reason, "SECRET_LIVE")
        self.assertFalse(self.store.path(f"publish/{self.run_id(6)}").exists())

    def test_prepare_refuses_live_secret_in_github_only_text(self):
        # An OPEN finding has no Slack reply, so only the GitHub comment/issue body carry its detail:
        # _g's shape redaction must not hide the live token from the SECRET_LIVE check (§3.6).
        leaky = make_result([finding(1, change=None, state="OPEN", detail="token " + GH_TOKEN)])
        report = make_report(run=self.run_id(7), result=leaky)
        self.assertEqual(pub.render_replies(report), [])
        publisher = self.publisher()
        with self.assertRaises(InspectError) as ctx:
            publisher.prepare(report, NOW)
        self.assertEqual(ctx.exception.reason, "SECRET_LIVE")
        self.assertFalse(self.store.path(f"publish/{self.run_id(7)}").exists())
        self.assertEqual(pub._GH_LIVE, ())
        # Outside a Publisher, _g still redacts shapes (nothing live is known there).
        self.assertNotIn("A1b2C3d4E5", pub._g("token " + GH_TOKEN))

    def test_missing_or_tampered_chart_is_skipped(self):
        pngs, render = self.pngs()
        (render / "c1_ladder.png").write_bytes(pub.PNG_SIGNATURE + b"changed")
        publisher = self.publisher()
        publisher.prepare(make_report(pngs=pngs, render_dir=render), NOW)
        out = publisher.advance(NOW)
        self.assertEqual((out["card"], out["charts"]), ("POSTED", "SKIPPED"))
        self.assertNotIn("files.getUploadURLExternal", self.slack_t.names())

    def test_prune(self):
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        self.assertEqual(publisher.prune(NOW + timedelta(days=29)), 0)
        self.assertEqual(publisher.prune(NOW + timedelta(days=31)), 1)
        self.assertEqual(publisher.journal_runs(), [])


# ---------------------------------------------------------------------------- dead-man

class TestDeadman(Base):
    def test_first_schedule(self):
        out = self.publisher().deadman(NOW)
        self.assertEqual(out["scheduled"], "POSTED")
        call = [c for c in self.slack_t.calls if c["name"] == "chat.scheduleMessage"][0]
        self.assertEqual(int(call["params"]["post_at"]), int((NOW + timedelta(hours=3)).timestamp()))
        self.assertEqual(call["params"]["channel"], TEST_CHANNEL)
        self.assertIn("status=STALE", call["params"]["text"])
        state = self.store.read("deadman")
        self.assertEqual(state["current"]["post_at"], "2026-10-01T08:17:00Z")
        self.assertNotIn("chat.deleteScheduledMessage", self.slack_t.names())

    def test_new_first_persist_then_delete_old(self):
        self.publisher().deadman(NOW)
        old_id = self.store.read("deadman")["current"]["id"]
        seen = {}

        def on_call(name, params):
            if name == "chat.deleteScheduledMessage":
                seen["current_at_delete"] = self.store.read("deadman")["current"]["id"]
                seen["deleted"] = params["scheduled_message_id"]

        self.slack_t.on_call = on_call
        self.publisher().deadman(NOW + timedelta(hours=1))
        names = [n for n in self.slack_t.names() if n.startswith("chat.")]
        self.assertEqual(names[-2:], ["chat.scheduleMessage", "chat.deleteScheduledMessage"])
        new_id = self.store.read("deadman")["current"]["id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(seen, {"current_at_delete": new_id, "deleted": old_id})
        self.assertEqual(self.store.read("deadman")["pending_delete"], [])

    def test_schedule_failure_keeps_old_message(self):
        self.publisher().deadman(NOW)
        old = self.store.read("deadman")["current"]
        self.slack_t.push("chat.scheduleMessage", resp({"ok": False, "error": "time_in_past"}))
        out = self.publisher().deadman(NOW + timedelta(hours=1))
        self.assertEqual(out["scheduled"], "REFUSED")
        self.assertNotIn("chat.deleteScheduledMessage", self.slack_t.names())
        self.assertEqual(self.store.read("deadman")["current"], old)

    def test_failed_delete_goes_pending_and_is_retried(self):
        self.publisher().deadman(NOW)
        old_id = self.store.read("deadman")["current"]["id"]
        self.slack_t.push("chat.deleteScheduledMessage", resp({"ok": False, "error": "ratelimited"}))
        out = self.publisher().deadman(NOW + timedelta(hours=1))
        self.assertEqual(out["pending_delete"], 1)
        pending = self.store.read("deadman")["pending_delete"]
        self.assertEqual((pending[0]["id"], pending[0]["attempts"]), (old_id, 1))
        out = self.publisher().deadman(NOW + timedelta(hours=2))
        deletes = [c["params"]["scheduled_message_id"] for c in self.slack_t.calls
                   if c["name"] == "chat.deleteScheduledMessage"]
        self.assertEqual(deletes.count(old_id), 2)
        self.assertEqual(out["pending_delete"], 0)

    def test_fired_message_gives_one_recovered_line(self):
        self.publisher().deadman(NOW)
        later = NOW + timedelta(hours=4, minutes=30)
        out = self.publisher().deadman(later)
        self.assertTrue(out["fired"])
        self.assertEqual(out["recovered"], "POSTED")
        posts = [c["params"]["text"] for c in self.slack_t.calls if c["name"] == "chat.postMessage"]
        self.assertEqual(posts, ["AIOPS_INSPECT_V1 status=RECOVERED · 공백 4시간"])
        self.assertNotIn("chat.deleteScheduledMessage", self.slack_t.names())  # the fired one cannot be deleted
        self.publisher().deadman(later + timedelta(hours=1))
        posts = [c for c in self.slack_t.calls if c["name"] == "chat.postMessage"]
        self.assertEqual(len(posts), 1)

    def test_unknown_schedule_is_not_resent_but_watched(self):
        self.slack_t.push("chat.scheduleMessage", InspectError("NET_UNKNOWN"))
        out = self.publisher().deadman(NOW)
        self.assertEqual(out["scheduled"], "UNKNOWN")
        self.assertEqual(self.publisher().unknown_count(NOW), 1)      # shown as 게시 미확인 1
        self.assertEqual(self.publisher().unknown_count(NOW + timedelta(hours=25)), 0)
        state = self.store.read("deadman")
        self.assertIsNone(state["current"])
        self.assertEqual(len(state["unknown"]), 1)
        out = self.publisher().deadman(NOW + timedelta(hours=1))
        self.assertEqual(out["scheduled"], "POSTED")  # the next tick schedules new text, not a resend

    def test_cancel(self):
        self.publisher().deadman(NOW)
        cid = self.store.read("deadman")["current"]["id"]
        out = self.publisher().cancel_deadman(NOW + timedelta(minutes=5))
        self.assertEqual(out, {"cancelled": 1, "pending_delete": 0})
        self.assertIsNone(self.store.read("deadman")["current"])
        delete = [c for c in self.slack_t.calls if c["name"] == "chat.deleteScheduledMessage"][0]
        self.assertEqual(delete["params"]["scheduled_message_id"], cid)

    def test_already_deleted_counts_as_gone(self):
        self.publisher().deadman(NOW)
        self.slack_t.push("chat.deleteScheduledMessage", resp({"ok": False, "error": "invalid_scheduled_message_id"}))
        out = self.publisher().deadman(NOW + timedelta(hours=1))
        self.assertEqual(out["pending_delete"], 0)


# ---------------------------------------------------------------------------- heartbeat and daily line

class TestHeartbeat(Base):
    def test_create_then_update(self):
        out = self.publisher().heartbeat(NOW, open_counts={"open": 2, "at_risk": 1})
        self.assertEqual(out["heartbeat"], "CREATED")
        ts = self.store.read("heartbeat")[TEST_CHANNEL]["ts"]
        out = self.publisher().heartbeat(NOW + timedelta(hours=1), open_counts={"open": 2, "at_risk": 1})
        self.assertEqual(out["heartbeat"], "UPDATED")
        update = self.slack_t.calls[-1]
        self.assertEqual(update["name"], "chat.update")
        self.assertEqual(update["params"]["ts"], ts)
        self.assertIn("마지막 점검 10/01 15:17 KST · 상태 OK · 다음 점검 10/01 16:17 KST", update["params"]["text"])
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 1)

    def test_message_not_found_posts_new(self):
        self.publisher().heartbeat(NOW)
        old = self.store.read("heartbeat")[TEST_CHANNEL]["ts"]
        self.slack_t.push("chat.update", resp({"ok": False, "error": "message_not_found"}))
        out = self.publisher().heartbeat(NOW + timedelta(hours=1))
        self.assertEqual(out["heartbeat"], "CREATED")
        entry = self.store.read("heartbeat")[TEST_CHANNEL]
        self.assertNotEqual(entry["ts"], old)
        self.assertEqual(entry["replaced"], old)

    def test_other_update_failures_do_not_post_new(self):
        self.publisher().heartbeat(NOW)
        self.slack_t.push("chat.update", resp({"ok": False, "error": "ratelimited"}), InspectError("NET_UNKNOWN"))
        self.assertEqual(self.publisher().heartbeat(NOW + timedelta(hours=1))["heartbeat"], "REFUSED")
        self.assertEqual(self.publisher().heartbeat(NOW + timedelta(hours=2))["heartbeat"], "UNKNOWN")
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 1)
        self.assertEqual(self.publisher().heartbeat(NOW + timedelta(hours=3))["heartbeat"], "UPDATED")

    def test_halted_and_unknown_count(self):
        self.gh_t.push("POST", InspectError("NET_UNKNOWN"))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        publisher.advance(NOW)
        out = self.publisher().heartbeat(NOW, status="HALTED", reason="SECRET_LIVE", halted_at=NOW,
                                         token_expiry={"gh-ledger": NOW + timedelta(days=2)})
        self.assertIn("상태 HALTED(SECRET_LIVE) · 정지 시작 10/01 14:17 KST", out["text"])
        self.assertIn("게시 미확인 1", out["text"])
        self.assertIn("토큰 만료 D-2 (gh-ledger 10/03 14:17 KST)", out["text"])

    def test_reads_open_counts_from_findings_state(self):
        self.store.write("findings", make_result([finding(1), finding(2, severity="WATCH")])["state"])
        out = self.publisher().heartbeat(NOW)
        self.assertIn("열린 발견 2 (위험 1)", out["text"])


class TestDaily(Base):
    MORNING = datetime(2026, 10, 1, 0, 17, tzinfo=timezone.utc)  # 09:17 KST

    def test_due_once_per_kst_day(self):
        publisher = self.publisher()
        for ok in (True, True, False):
            publisher.count_tick(ok)
        self.assertEqual(publisher.daily(self.MORNING - timedelta(hours=1))["daily"], "NOT_DUE")
        out = publisher.daily(self.MORNING, open_counts={"open": 3, "at_risk": 0})
        self.assertEqual(out["daily"], "POSTED")
        self.assertEqual(out["text"], "AIOPS_INSPECT_V1 daily · 2026-10-01 · 점검 2/3 · 카드 0 · 열린 발견 3 · 게시 미확인 0")
        self.assertEqual(publisher.daily(self.MORNING + timedelta(hours=5))["daily"], "NOT_DUE")
        publisher.count_tick(True)
        out = publisher.daily(self.MORNING + timedelta(days=1), open_counts={"open": 0})
        self.assertIn("점검 1/1", out["text"])

    def test_refused_retried_then_closed(self):
        for _ in range(3):
            self.slack_t.push("chat.postMessage", resp({"ok": False, "error": "not_in_channel"}))
        publisher = self.publisher()
        results = [publisher.daily(self.MORNING + timedelta(hours=h))["daily"] for h in range(4)]
        self.assertEqual(results, ["REFUSED", "REFUSED", "REFUSED", "NOT_DUE"])
        self.assertEqual(self.store.read("daily")["last_outcome"], "FAILED")

    def test_unknown_not_resent(self):
        self.slack_t.push("chat.postMessage", InspectError("NET_UNKNOWN"))
        publisher = self.publisher()
        self.assertEqual(publisher.daily(self.MORNING)["daily"], "UNKNOWN")
        self.assertEqual(publisher.daily(self.MORNING + timedelta(hours=1))["daily"], "NOT_DUE")
        self.assertEqual(publisher.unknown_count(self.MORNING + timedelta(hours=1)), 1)


# ---------------------------------------------------------------------------- review fixes

class Killed(BaseException):
    """Simulates the tick being SIGKILLed right after a Slack call reached Slack."""


TOKEN_SHAPED = ("ghp_" + "Z9y8" * 8, "github_pat_" + "Q8w7" * 8)


class TestReviewFixes(Base):
    MORNING = datetime(2026, 10, 1, 0, 17, tzinfo=timezone.utc)  # 09:17 KST

    def run_id(self, hour=5):
        return f"20261001T{hour:02d}1700Z-0a1b2c3d-tick"

    def kill_on_post(self):
        def on_call(name, params):
            if name == "chat.postMessage":
                self.slack_t.on_call = None
                self.slack_t.calls[-1]["killed"] = True
                raise Killed()
        self.slack_t.on_call = on_call

    # finding 1: token shapes are redacted before gh_text escapes "_"
    def test_github_fields_redact_underscore_token_shapes(self):
        ghp, pat = TOKEN_SHAPED
        bad = finding(1, detail=f"leak {ghp} and {pat}", title=f"t {ghp}", subject=f"node:{pat}")
        result = make_result([bad, finding(2, change=None, state="OPEN", detail=f"x {pat}")])
        report = make_report(result=result)
        for text in (pub.render_comment(report), pub.render_issue_body("DRY", NOW, report["run"], result["state"])[0]):
            plain = text.replace("\\", "")
            for token in (ghp, pat):
                self.assertNotIn(token, plain)
                self.assertNotIn(token[10:], plain)
            self.assertIn("redacted sha256=", text)
        self.assertIn("\\[redacted sha256=", pub._g(ghp))
        publisher = self.publisher()
        entry = publisher.prepare(report, NOW)
        self.assertNotIn(pat[12:], entry["comment"]["body"].replace("\\", ""))
        self.assertNotIn(ghp[4:], entry["issue_body"]["body"].replace("\\", ""))
        self.assertGreater(publisher.shape_hits, 0)

    # finding 2: a connection that was never opened is not a write attempt
    def test_net_down_does_not_use_up_attempts(self):
        for _ in range(5):
            self.gh_t.push("POST", InspectError("NET_DOWN"))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        states = [self.publisher().advance(NOW + timedelta(hours=h))["comment"] for h in range(6)]
        self.assertEqual(states, ["REFUSED"] * 5 + ["POSTED"])
        journal = publisher.load(self.run_id())
        self.assertEqual(journal["comment"]["attempts"], 1)
        self.assertEqual(journal["card"]["state"], "POSTED")
        self.assertEqual(journal["replies"][0]["state"], "POSTED")

    def test_slack_net_down_does_not_use_up_attempts(self):
        for _ in range(4):
            self.slack_t.push("chat.postMessage", InspectError("NET_DOWN"))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        states = [self.publisher().advance(NOW + timedelta(hours=h))["card"] for h in range(5)]
        self.assertEqual(states, ["REFUSED"] * 4 + ["POSTED"])

    def test_net_down_is_not_an_attempt_for_daily_recovered_and_uploads(self):
        publisher = self.publisher()
        publisher.count_tick(True)
        for _ in range(4):
            self.slack_t.push("chat.postMessage", InspectError("NET_DOWN"))
        outs = [self.publisher().daily(self.MORNING + timedelta(hours=h), open_counts={"open": 0})["daily"]
                for h in range(5)]
        self.assertEqual(outs, ["REFUSED"] * 4 + ["POSTED"])
        self.assertEqual(self.store.read("daily")["last_outcome"], "POSTED")
        # RECOVERED: four lines never connected, then the fifth is posted once
        self.publisher().deadman(NOW)
        later = NOW + timedelta(hours=4, minutes=30)
        for _ in range(4):
            self.slack_t.push("chat.postMessage", InspectError("NET_DOWN"))
        outs = [self.publisher().deadman(later + timedelta(hours=h))["recovered"] for h in range(5)]
        self.assertEqual(outs, ["REFUSED"] * 4 + ["POSTED"])
        # an upload URL request that never connected keeps the suffix, so the card step does not count it
        client = self.publisher().slack
        client.verify()
        self.slack_t.push("files.getUploadURLExternal", InspectError("NET_DOWN"))
        with self.assertRaises(pub.SlackError) as ctx:
            client.upload_files(TEST_CHANNEL, "1000.000100", [{"name": "c1_ladder.png", "data": b"\x89PNGx"}])
        self.assertEqual(ctx.exception.outcome, "REFUSED")
        self.assertTrue(ctx.exception.detail.endswith(pub.NOT_CONNECTED))

    def test_stage_switch_never_sends_a_journal_to_the_other_target(self):
        live = dict(CONFIG, stage="LIVE")

        def live_publisher():
            writer = gh.GitHubLedgerWriter(GH_TOKEN, CTRL_REPO, 4, transport=self.gh_t)
            client = pub.SlackClient(SLACK_TOKEN, TEAM, BOT, transport=self.slack_t, live_values=(GH_TOKEN,))
            return pub.Publisher(self.store, live, writer=writer, slack=client, live_values=(GH_TOKEN, SLACK_TOKEN))

        # DRY comment refused, then the stage switches to LIVE: nothing reaches the live issue or channel.
        self.gh_t.push("POST", resp({"message": "no"}, 422))
        publisher = self.publisher()
        publisher.prepare(make_report(), NOW)
        self.assertEqual(publisher.advance(NOW)["comment"], "REFUSED")
        posts = len([c for c in self.gh_t.calls if c["method"] == "POST"])
        out = live_publisher().advance(NOW + timedelta(hours=1))
        self.assertEqual(out["comment"], "SUPERSEDED")
        self.assertEqual(len([c for c in self.gh_t.calls if c["method"] == "POST"]), posts)
        self.assertNotIn("chat.postMessage", self.slack_t.names())
        journal = publisher.load(make_report()["run"])
        self.assertEqual(journal["comment"]["error"], "TARGET_CHANGED")
        # LIVE card refused, then back to DRY: the live channel gets nothing more.
        run = "20261001T071700Z-0a1b2c3d-tick"
        report = pub.build_report(run=run, now=NOW + timedelta(hours=2), stage="LIVE", snapshot=SNAP,
                                  tool_sha256=TOOL, facts=FACTS, facts_sha256=FACTS_SHA, result=make_result(),
                                  control_repository=CTRL_REPO, ledger_issue=4, pngs=(), render_dir=None)
        self.slack_t.push("chat.postMessage", resp({"ok": False, "error": "not_in_channel"}))
        lp = live_publisher()
        lp.prepare(report, NOW + timedelta(hours=2))
        self.assertEqual(lp.advance(NOW + timedelta(hours=2))["card"], "REFUSED")
        sent = len(self.slack_t.calls)
        out = self.publisher().advance(NOW + timedelta(hours=3))
        self.assertEqual(out["card"], "SUPERSEDED")
        self.assertFalse([c for c in self.slack_t.calls[sent:] if c["name"] == "chat.postMessage"])

    # finding 3: daily line, RECOVERED line and heartbeat creation persist a marker before sending
    def test_daily_killed_after_send_is_not_reposted(self):
        publisher = self.publisher()
        publisher.count_tick(True)
        self.kill_on_post()
        with self.assertRaises(Killed):
            publisher.daily(self.MORNING, open_counts={"open": 0})
        publisher = self.publisher()
        publisher.count_tick(True)
        out = publisher.daily(self.MORNING + timedelta(hours=1), open_counts={"open": 0})
        self.assertEqual(out["daily"], "NOT_DUE")
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 1)
        self.assertEqual(self.store.read("daily")["last_outcome"], "UNKNOWN")
        self.assertEqual(publisher.unknown_count(self.MORNING + timedelta(hours=1)), 1)
        # The tick counted after the lost line is kept for the next day's line.
        out = publisher.daily(self.MORNING + timedelta(days=1), open_counts={"open": 0})
        self.assertIn("점검 1/1", out["text"])

    def test_recovered_killed_after_send_is_not_reposted(self):
        self.publisher().deadman(NOW)
        later = NOW + timedelta(hours=4, minutes=30)
        self.kill_on_post()
        with self.assertRaises(Killed):
            self.publisher().deadman(later)
        out = self.publisher().deadman(later + timedelta(hours=1))
        self.assertEqual(out["recovered"], "UNKNOWN")
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 1)
        self.assertIsNone(self.store.read("deadman")["recover"])
        self.assertEqual(self.publisher().unknown_count(later + timedelta(hours=1)), 1)
        self.publisher().deadman(later + timedelta(hours=2))
        self.assertEqual(self.slack_t.names().count("chat.postMessage"), 1)

    def test_heartbeat_create_killed_after_send_reads_as_unknown(self):
        self.kill_on_post()
        with self.assertRaises(Killed):
            self.publisher().heartbeat(NOW, open_counts={"open": 0, "at_risk": 0})
        self.assertTrue(self.store.read("heartbeat")[TEST_CHANNEL].get("creating_at"))
        out = self.publisher().heartbeat(NOW + timedelta(hours=1), open_counts={"open": 0, "at_risk": 0})
        # CONTRACT NOTE (publish.heartbeat): the new text differs, so creating again is not a resend; the
        # possibly-created message is shown as 게시 미확인.
        self.assertEqual(out["heartbeat"], "CREATED")
        self.assertIn("게시 미확인 1", out["text"])
        entry = self.store.read("heartbeat")[TEST_CHANNEL]
        self.assertNotIn("creating_at", entry)

    # finding 4: journal order does not follow the wall clock
    def test_backward_clock_step_does_not_supersede_the_newest_journal(self):
        self.gh_t.push("POST", resp({"message": "no"}, 422))
        publisher = self.publisher()
        publisher.prepare(make_report(run=self.run_id(6)), NOW + timedelta(hours=1))
        publisher.advance(NOW + timedelta(hours=1))
        # The clock stepped back: the newer report gets an earlier run id.
        publisher = self.publisher()
        publisher.prepare(make_report(run=self.run_id(5)), NOW)
        out = publisher.advance(NOW)
        self.assertEqual(out["run"], self.run_id(5))
        self.assertEqual(out["state"], "SLACK_POSTED")
        self.assertEqual(publisher.load(self.run_id(6))["comment"]["state"], "SUPERSEDED")
        out = self.publisher().advance(NOW + timedelta(hours=1))
        self.assertEqual(out["run"], self.run_id(5))
        self.assertEqual(len([c for c in self.gh_t.calls if c["method"] == "POST"]), 2)

    # a GH-<n> shorthand in a subject or detail never forms an issue autolink
    def test_gh_shorthand_never_autolinks(self):
        bad = finding(1, subject="node:GH-12", detail="노드 GH-12: 마지막 완료 뒤 3일이 지났다.")
        comment = pub.render_comment(make_report(result=make_result([bad])))
        self.assertIn("12", comment)
        self.assertIsNone(re.search(r"(?i)(?<![A-Za-z0-9])GH-\d", comment))


if __name__ == "__main__":
    unittest.main()
