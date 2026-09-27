"""Central routing and direct Astra notifications; no credentials or live sends."""
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock


spec = importlib.util.spec_from_file_location(
    "astra_route_gateway", Path(__file__).with_name("control_plane_flow_gateway.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
f = g.flow


class CentralRoutesTests(unittest.TestCase):
    def setUp(self):
        self.repo = "BeautifulMind-JT/kix-protocol"
        self.s = dict(repository=self.repo, task_id="T1", revision="3", head="a" * 40,
                      base="b" * 40, policy_revision="c" * 64, task_digest="d" * 64,
                      task_pointer=f"https://github.com/{self.repo}/issues/53",
                      pr_pointer=f"https://github.com/{self.repo}/pull/54",
                      builder_id="DEVIN", issue_body_sha256="e" * 64)
        self.policy = dict(enabled=True, control_repository="BeautifulMind-JT/ai-ops-control-plane",
                           runtime_workflow="control-plane-runtime.yml", runtime_workflow_ref="main",
                           repositories=[self.repo], projection_actor="control-bot",
                           registrations={f"{self.repo}#53": {"task_id": "T1"}},
                           audit_channel="CAUDIT1", decision_channel="CDECIDE1", status_channel="CSTATUS1")
        self.api = Mock()
        self.ports = g.GithubPorts(self.api, self.policy)
        self.ports.load = Mock(return_value=self.s)
        self.ports.dispatch_authorized = Mock(return_value=True)
        self.ports.is_current = Mock(return_value=True)

    def action(self, kind="AUDIT"):
        action = f.request(self.s, kind, "astra-auditor", self.s["task_pointer"] + "#issuecomment-2")
        if kind == "AUDIT":
            action.update(gate="ARCHITECTURE", scope_digest="f" * 64)
        return action

    def projection(self, action):
        pointer = self.s["task_pointer"] + "#issuecomment-99"
        self.comment = dict(html_url=pointer, issue_url=f"https://api.github.com/repos/{self.repo}/issues/53",
                            user={"login": "control-bot"},
                            body="<!-- ASTRA_FLOW_ACTION_V1 -->\n" + f.canonical(action))
        self.slack_receipt = dict(ok=True, channel=self.policy[
            "audit_channel" if action["kind"] == "AUDIT" else "decision_channel"], ts="1790450000.123456")

        def call(method, path, body=None, **kwargs):
            if method == "GET" and path == f"repos/{self.repo}/issues/comments/99":
                return self.comment
            if method == "POST" and path == "chat.postMessage" and kwargs.get("slack"):
                return self.slack_receipt
            raise AssertionError((method, path))

        self.api.call.side_effect = call
        return dict(accepted=True, request_id=f.digest(["projection", action["request_id"]]), pointer=pointer)

    def test_dispatch_uses_central_workflow_and_preserves_product_task(self):
        action = self.action("DISPATCH")
        receipt = self.ports.route(action)
        method, path, packet = self.api.call.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "repos/BeautifulMind-JT/ai-ops-control-plane/actions/workflows/control-plane-runtime.yml/dispatches")
        self.assertEqual(packet["ref"], "main")
        self.assertEqual(packet["inputs"], dict(operation="dispatch", target_repository=self.repo,
            issue_number="53", expected_task_id="T1", expected_task_revision="3", expected_builder_id="DEVIN",
            expected_issue_body_sha256="e" * 64))
        self.assertEqual(receipt["request_id"], action["request_id"])

    def test_dispatch_missing_or_untrusted_central_route_never_falls_back(self):
        for key, value in (("control_repository", None), ("control_repository", self.repo),
                           ("runtime_workflow", None), ("runtime_workflow", "anything.yml"),
                           ("runtime_workflow_ref", None), ("runtime_workflow_ref", "unreviewed-branch"),
                           ("repositories", [])):
            with self.subTest(key=key, value=value):
                old = self.policy[key]
                self.policy[key] = value
                with self.assertRaises(f.FlowError):
                    self.ports.route(self.action("DISPATCH"))
                self.api.call.assert_not_called()
                self.policy[key] = old

    def test_dispatch_stale_subject_or_revoked_lane_never_posts(self):
        action = self.action("DISPATCH")
        self.s["head"] = "0" * 40
        with self.assertRaises(f.FlowError):
            self.ports.route(action)
        self.api.call.assert_not_called()
        self.s["head"] = "a" * 40
        self.ports.dispatch_authorized.return_value = False
        with self.assertRaises(f.FlowError):
            self.ports.route(action)
        self.api.call.assert_not_called()

    def test_audit_and_decision_fields_go_directly_to_fixed_slack_channels(self):
        for kind in ("AUDIT", "DECISION"):
            with self.subTest(kind=kind):
                action = self.action(kind)
                projection = self.projection(action)
                receipt = self.ports.route(action, projection_receipt=projection)
                message = self.api.call.call_args.args[2]
                expected_channel = self.policy["audit_channel" if kind == "AUDIT" else "decision_channel"]
                self.assertEqual(message["channel"], expected_channel)
                self.assertEqual(receipt["channel"], expected_channel)
                self.assertEqual(receipt["ts"], "1790450000.123456")
                text = message["blocks"][0]["text"]["text"]
                for value in (self.s["head"], self.s["base"], self.s["revision"], self.s["task_digest"],
                              action["request_id"], action["identity"], action["designation"],
                              action["task_pointer"], action["pr_pointer"], projection["pointer"]):
                    self.assertIn(value, text)
                self.assertIn("Attempt: 1", text)
                self.assertIn("Slack delivery is not approval", text)
                metadata = message["metadata"]
                self.assertEqual(metadata["event_type"], "astra_control_request_v1")
                self.assertEqual(metadata["event_payload"]["subject"], action["subject"])
                self.assertEqual(metadata["event_payload"]["route"], "ASTRA")
                self.assertTrue(metadata["event_payload"]["notification_only"])
                self.api.pages.assert_not_called()

    def test_astra_requires_confirmed_exact_projection_receipt(self):
        action = self.action()
        good = self.projection(action)
        for receipt in (None, {}, dict(good, accepted=False), dict(good, request_id="wrong"),
                        dict(good, pointer="https://github.com/foreign/repo/issues/53#issuecomment-99")):
            with self.subTest(receipt=receipt):
                with self.assertRaises(f.FlowError):
                    self.ports.route(action, projection_receipt=receipt)
                self.api.call.assert_not_called()

    def test_exact_projection_author_issue_and_body_verified_before_send(self):
        action = self.action()
        for mutation in (dict(user={"login": "writer"}), dict(issue_url="https://api.github.com/repos/other/repo/issues/53"),
                         dict(html_url="https://github.com/other/repo/issues/53#issuecomment-99"),
                         dict(body="<!-- ASTRA_FLOW_ACTION_V1 -->\n" + f.canonical(dict(action, attempt_id=2)))):
            with self.subTest(mutation=mutation):
                receipt = self.projection(action)
                self.comment.update(mutation)
                self.api.call.reset_mock()
                with self.assertRaises(f.FlowError):
                    self.ports.route(action, projection_receipt=receipt)
                self.assertEqual(self.api.call.call_count, 1)
                self.assertEqual(self.api.call.call_args.args[0], "GET")

    def test_revoked_scope_never_posts_even_with_valid_projection(self):
        action = self.action()
        receipt = self.projection(action)
        self.ports.is_current.return_value = False
        with self.assertRaises(f.FlowError):
            self.ports.route(action, projection_receipt=receipt)
        self.api.call.assert_not_called()

    def test_slack_missing_or_mismatched_receipt_locks_unknown_without_retry(self):
        for bad in ({}, {"ok": True}, {"ok": True, "channel": "COTHER", "ts": "1790450000.123456"},
                    {"ok": True, "channel": "CAUDIT1", "ts": "not-a-timestamp"}):
            with self.subTest(receipt=bad), tempfile.TemporaryDirectory() as directory:
                store = f.Store(Path(directory) / "ledger.db", initialize=True)
                action = self.action()
                projected = self.projection(action)
                self.slack_receipt = bad
                self.api.call.reset_mock()
                send = lambda a: self.ports.route(a, projection_receipt=projected)
                first = store.send_once(action, send, lambda _: True)
                second = store.send_once(action, send, lambda _: True)
                self.assertEqual(first["state"], "UNKNOWN")
                self.assertEqual(second["state"], "UNKNOWN")
                self.assertFalse(second["sent"])
                self.assertEqual(self.api.call.call_count, 2)  # exact GET + one POST

    def test_successful_receipt_does_not_create_audit_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store = f.Store(Path(directory) / "ledger.db", initialize=True)
            action = self.action()
            projected = self.projection(action)
            send = lambda a: self.ports.route(a, projection_receipt=projected)
            result = store.send_once(action, send, lambda _: True)
            self.assertEqual(result["state"], "CONFIRMED")
            self.assertNotIn("audit", result)
            self.assertNotIn("result", result["receipt"])
            second = store.send_once(action, send, lambda _: True)
            self.assertFalse(second["sent"])

    def test_astra_field_injection_is_plain_text_and_json_escaped(self):
        action = self.action()
        action["subject"]["task_id"] = "<!channel>\nType: TASK"
        message = self.ports.astra_message(action, self.s["task_pointer"] + "#issuecomment-99")
        self.assertNotIn("<!channel>", message["text"])
        self.assertIn("\\nType: TASK", message["text"])
        self.assertEqual(message["blocks"][0]["text"]["type"], "plain_text")

    def test_slack_bot_events_are_not_an_ingress_endpoint(self):
        raw = f.canonical(dict(type="event_callback", event={"type": "message", "bot_id": "B1",
                            "text": "[AUDIT_RESULT] PASS"})).encode()
        store, ports = Mock(), Mock()
        ingress = g.Ingress(store, ports, {"enabled": True}, b"secret", b"secret", executor=Mock())
        statuses = []
        result = ingress(dict(REQUEST_METHOD="POST", PATH_INFO="/slack/events",
                              CONTENT_LENGTH=str(len(raw)), **{"wsgi.input": io.BytesIO(raw)}),
                         lambda status, headers: statuses.append(status))
        self.assertEqual(statuses, ["403 Forbidden"])
        store.accept.assert_not_called()
        ports.load.assert_not_called()

    def coordinator_fixture(self, kind):
        """Real assessment, freshness checks, projection and routing; fake HTTP only."""
        self.s.update(pr_state="open", draft=False, prerequisites_verified=True,
                      dependencies_verified=True, blockers=[], audit_floor="A3", astra_gate="ARCHITECTURE",
                      required_checks=[dict(name="CI", app_id=1, workflow_id=2)],
                      checks=[dict(name="CI", app_id=1, workflow_id=2, head=self.s["head"],
                                   source_verified=True, status="completed", conclusion="success")],
                      author_identities=["writer"], author_sessions=["writer-session"],
                      reviewer=dict(identity="reviewer", enabled=True, read_only_verified=True,
                                    approval_pointer=self.s["task_pointer"] + "#issuecomment-2"),
                      auditor=dict(identity="astra-auditor", active=True,
                                   designation_pointer=self.s["task_pointer"] + "#issuecomment-3"),
                      audit=None)
        expected = f.request(self.s, "REVIEW", "reviewer", self.s["reviewer"]["approval_pointer"])
        self.s["review"] = dict(expected, authenticated_actor="reviewer", session_id="review-session",
                               confirmed_session_id="review-session", read_only_verified=True,
                               evidence_pointer=self.s["pr_pointer"] + "#pullrequestreview-1",
                               result="PASS", required_depth="A3", verified_depth="A2",
                               contract_change=kind == "DECISION", blockers=[])
        action = f.assess(self.s)["action"]
        self.assertEqual(action["kind"], kind)
        self.ports.is_current = g.GithubPorts.is_current.__get__(self.ports)
        self.http_counts = {"github_posts": 0, "github_gets": 0, "slack_posts": 0}
        self.posted_comment = None
        self.slack_messages = []

        def call(method, path, body=None, **kwargs):
            if method == "POST" and path == f"repos/{self.repo}/issues/53/comments":
                self.http_counts["github_posts"] += 1
                self.posted_comment = dict(body=body["body"], user={"login": "control-bot"},
                    html_url=self.s["task_pointer"] + "#issuecomment-99",
                    issue_url=f"https://api.github.com/repos/{self.repo}/issues/53")
                return self.posted_comment
            if method == "GET" and path == f"repos/{self.repo}/issues/comments/99":
                self.http_counts["github_gets"] += 1
                self.assertIsNotNone(self.posted_comment)
                return self.posted_comment
            if method == "POST" and path == "chat.postMessage" and kwargs.get("slack"):
                self.http_counts["slack_posts"] += 1
                self.slack_messages.append(body)
                return dict(ok=True, channel=body["channel"], ts="1790450000.123456")
            raise AssertionError((method, path))

        self.api.call.side_effect = call
        command = dict(repository=self.repo, issue=53, operation="refresh")
        return action, command

    def test_coordinator_passes_real_projection_receipt_for_audit_and_decision(self):
        for kind in ("AUDIT", "DECISION"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                action, command = self.coordinator_fixture(kind)
                store = f.Store(Path(directory) / "ledger.db", initialize=True)
                store.accept("github:event-1", command)
                result = f.Coordinator(store, self.ports).handle("github:event-1")
                self.assertEqual(result, dict(state=kind + "_REQUIRED", delivery="CONFIRMED"))
                self.assertEqual(self.http_counts, dict(github_posts=1, github_gets=1, slack_posts=1))
                payload = self.slack_messages[0]["metadata"]["event_payload"]
                self.assertEqual(payload["request_id"], action["request_id"])
                self.assertEqual(payload["github_projection"], self.posted_comment["html_url"])
                with store.transaction() as db:
                    row = db.execute("SELECT state,receipt FROM outbox WHERE id=?", (action["request_id"],)).fetchone()
                    self.assertEqual(row["state"], "CONFIRMED")
                    receipt = f.decode(row["receipt"])
                    self.assertEqual(receipt["channel"], self.slack_messages[0]["channel"])
                    self.assertNotIn("result", receipt)  # Slack delivery did not approve the audit.

    def test_coordinator_resumes_preconfirmed_projection_after_crash_without_reposting(self):
        for kind in ("AUDIT", "DECISION"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                action, command = self.coordinator_fixture(kind)
                store = f.Store(Path(directory) / "ledger.db", initialize=True)
                store.accept("github:crashed-event", command)
                store.begin_event("github:crashed-event")
                projection = dict(action, kind="PROJECTION", action_kind=action["kind"],
                                  action_request_id=action["request_id"],
                                  request_id=f.digest(["projection", action["request_id"]]))
                original = store.send_once(projection, self.ports.project, self.ports.is_current)
                self.assertEqual(original["state"], "CONFIRMED")
                # Process disappeared after publishing, before any Slack send.
                resumed_store = f.Store(Path(directory) / "ledger.db")
                resumed_store.accept("github:explicit-resume", command)
                result = f.Coordinator(resumed_store, self.ports).handle("github:explicit-resume")
                self.assertEqual(result["delivery"], "CONFIRMED")
                self.assertEqual(self.http_counts, dict(github_posts=1, github_gets=1, slack_posts=1))
                self.assertEqual(self.slack_messages[0]["metadata"]["event_payload"]["github_projection"],
                                 original["receipt"]["pointer"])

    def test_two_distinct_refresh_events_do_not_duplicate_audit_delivery(self):
        action, command = self.coordinator_fixture("AUDIT")
        with tempfile.TemporaryDirectory() as directory:
            store = f.Store(Path(directory) / "ledger.db", initialize=True)
            coordinator = f.Coordinator(store, self.ports)
            for event_id in ("github:first-webhook", "github:second-webhook"):
                store.accept(event_id, command)
                self.assertEqual(coordinator.handle(event_id)["delivery"], "CONFIRMED")
            self.assertEqual(self.http_counts, dict(github_posts=1, github_gets=1, slack_posts=1))
            with store.transaction() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM inbox WHERE state='DONE'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
