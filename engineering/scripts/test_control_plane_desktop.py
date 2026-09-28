"""Offline screen protocol tests. No provider/UI/live-acceptance assertions."""
import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import control_plane_local_host as local
import test_control_plane_local_host as host_fixture


class DesktopTests(unittest.TestCase):
    setUp = host_fixture.IndependentHostTests.setUp

    def configure(self):
        self.policy["execution_transport"] = "APP_SCREEN"
        pair = dict(model="gpt-5.6-sol", effort="max")
        self.policy["desktop"] = {"WORK": dict(
            execution_mode="APP_SCREEN", host_id=self.policy["host_id"],
            instance_id=self.policy["instance_id"], operations=["WORK_DIAGNOSTIC"],
            isolation_evidence=self.fixture.review, coordinator=pair, supported_coordinators=[pair],
            target=dict(app="ChatGPT Work", account="test-account", session=self.fixture.action["work_session"],
                        model="GPT-6 Astra", effort="medium"))}
        # A real desktop transport does not depend on the Codex CLI relay policy.
        self.policy["relay"] = None

    def prepared(self):
        self.configure()
        return self.host.prepare_ui()

    def observed(self, prepared, outcome="ANSWER"):
        value = dict(action_id=prepared["action_id"], target=prepared["target"], outcome=outcome)
        if outcome == "ANSWER":
            value["diagnostic_result"] = self.fixture.answer()
        return value

    def send(self, prepared):
        return self.host.send_ui(dict(action_id=prepared["action_id"], target=prepared["target"], input_ready=True))

    def test_work_screen_roundtrip_without_cli_no_approval(self):
        with patch.object(local.subprocess, "run", side_effect=AssertionError("no CLI")):
            p = self.prepared()
            self.assertFalse(p["send_allowed"])
            self.assertTrue(self.send(p)["send_allowed"])
            value = self.observed(p)
            self.assertEqual(self.host.collect_ui(value)["state"], "DIAGNOSTIC_RECORDED")
            self.assertTrue(self.host.collect_ui(value)["duplicate"])
        self.assertEqual(len(self.fixture.posts), 2)
        self.assertEqual(self.host.collect_ui(value)["grants"], [])
        self.assertFalse(self.host.prepare_ui()["send_allowed"])

    def test_duplicate_and_concurrent_send_permit_once(self):
        p = self.prepared()
        def send(_):
            try:
                return self.send(p)["send_allowed"]
            except local.flow.FlowError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(send, range(2))), 1)
        self.assertFalse(send(None))

    def test_lost_permit_response_keeps_unknown_after_restart(self):
        p = self.prepared()
        self.send(p)  # caller loses this response; no actual UI effect assumed
        self.host = local.Host(self.policy, self.api)
        self.assertEqual(self.host.prepare_ui()["state"], "UNKNOWN")
        with self.assertRaises(Exception):
            self.send(p)

    def test_result_before_send_rejected(self):
        p = self.prepared()
        with self.assertRaises(Exception): self.host.collect_ui(self.observed(p))

    def test_wrong_target_fields_block_send_and_result(self):
        p = self.prepared()
        for field in p["target"]:
            bad = copy.deepcopy(p)
            bad["target"][field] += "-wrong"
            with self.assertRaises(Exception): self.send(bad)
        self.send(p)
        for field in p["target"]:
            value = self.observed(p)
            value["target"] = dict(value["target"], **{field: "wrong"})
            with self.assertRaises(Exception): self.host.collect_ui(value)

    def test_changed_policy_no_model_fallback_after_claim(self):
        p = self.prepared()
        self.policy["desktop"]["WORK"]["coordinator"]["effort"] = "xhigh"
        with self.assertRaises(Exception): self.send(p)

    def test_stale_and_wrong_identity_result(self):
        p = self.prepared(); self.send(p)
        for field in ("identity", "work_session", "request_id"):
            value = self.observed(p)
            value["diagnostic_result"][field] = "wrong"
            with self.assertRaises(Exception): self.host.collect_ui(value)
        for field in ("head", "revision"):
            value = self.observed(p)
            value["diagnostic_result"]["subject"] = dict(value["diagnostic_result"]["subject"], **{field: "wrong"})
            with self.assertRaises(Exception): self.host.collect_ui(value)

    def test_waiting_observe_existing_no_new_send(self):
        p = self.prepared(); self.send(p)
        self.assertEqual(self.host.collect_ui(self.observed(p, "WAITING"))["state"], "WAITING")
        with self.assertRaises(Exception): self.send(p)
        self.assertEqual(self.host.collect_ui(self.observed(p))["state"], "DIAGNOSTIC_RECORDED")

    def test_lost_shared_claim_response_no_send(self):
        self.configure(); self.api.fail_after = "PATCH"
        with self.assertRaises(Exception): self.host.prepare_ui()
        self.api.fail_after = None
        self.assertEqual(self.host.prepare_ui()["state"], "UNKNOWN")
        self.assertEqual(len(self.fixture.posts), 0)

    def test_crash_during_collection_cannot_republish(self):
        p = self.prepared(); self.send(p)
        with self.host.store.transaction() as db:
            db.execute("UPDATE host_actions SET state='RESULT_SUBMITTING' WHERE id=?", (p["action_id"],))
        with self.assertRaises(Exception): self.host.collect_ui(self.observed(p))
        with self.assertRaises(Exception): self.send(p)

    def test_no_isolation_evidence_no_claim(self):
        self.configure(); self.policy["desktop"]["WORK"].pop("isolation_evidence")
        with self.assertRaises(Exception): self.host.prepare_ui()
        self.assertFalse(self.api.refs)

    def test_mac_entrypoint_uses_screen_and_never_falls_back(self):
        self.configure()
        with patch.object(local.relay, "run_codex", side_effect=AssertionError("no CLI fallback")):
            self.assertEqual(self.host.work()["state"], "UI_READY")
        with self.assertRaises(Exception): self.host.work(collect=True)

    def test_new_mac_install_defaults_screen_but_disabled(self):
        with patch.object(local.sys, "platform", "darwin"):
            other = self.parent / "mac2"
            local.initialize(other, "other")
        policy = local.flow.decode((other / "policy.json").read_text())
        self.assertEqual(policy["execution_transport"], "APP_SCREEN")
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["desktop"], {})

    def test_builder_and_read_only_review_screen_routes(self):
        self.configure()
        self.assignment["operations"] = ["BUILDER", "REVIEW"]
        packet = dict(builder_id="GLM", operation="REVIEW", repository=self.assignment["repository"],
                      issue=19, task_id=self.assignment["task_id"], host_id=self.policy["host_id"],
                      instance_id=self.policy["instance_id"], revision="r1", head=self.fixture.current_sha,
                      pr=123, read_only=True,
                      ui_target=dict(app="ZCode", account="subscription-test", session="review-session", model="GLM", effort="max"))
        config = dict(self.policy["desktop"]["WORK"], target=packet["ui_target"], operations=["REVIEW"],
                      read_only_enforced=True)
        self.policy["desktop"]["GLM"] = config
        original = self.api.fallback
        def fallback(method, path, body=None, **kwargs):
            if path.endswith("pulls/123"):
                return dict(state="open", head=dict(sha=self.fixture.current_sha))
            return original(method, path, body, **kwargs)
        self.api.fallback = fallback
        with patch.object(self.host, "assignment", return_value=self.assignment), \
             patch.object(self.host, "record", return_value=({"packet": packet}, self.assignment["pointer"])), \
             patch.object(local.subprocess, "run", side_effect=AssertionError("no CLI")):
            config["read_only_enforced"] = False
            with self.assertRaises(Exception): self.host.run_adapter(self.binding)
            self.assertFalse(self.api.refs)
            config["read_only_enforced"] = True
            p = self.host.run_adapter(self.binding)
            self.send(p)
            result = self.host.collect_ui(self.observed(p, "SESSION_OBSERVED"))
            self.assertEqual(result["state"], "SESSION_OBSERVED")
            self.assertEqual(result["grants"], [])
            with self.assertRaises(Exception): self.send(p)

    def test_stale_source_before_send_stays_fenced(self):
        p = self.prepared()
        self.fixture.current_sha = "b" * 40
        with self.assertRaises(Exception): self.send(p)
        self.assertFalse(self.host.prepare_ui()["send_allowed"])

    def test_publication_loss_not_reapplied(self):
        p = self.prepared(); self.send(p)
        self.fixture.loss_after_publish = True
        with self.assertRaises(Exception): self.host.collect_ui(self.observed(p))
        self.fixture.loss_after_publish = False
        with self.assertRaises(Exception): self.host.collect_ui(self.observed(p))
        self.assertEqual(len(self.fixture.posts), 2)
        with self.assertRaises(Exception): self.send(p)


if __name__ == "__main__":
    unittest.main()
