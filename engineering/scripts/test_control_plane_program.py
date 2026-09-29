"""Program-mode runtime tests against a fake GitHub and the real host ledger (no network)."""
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import control_plane as cp
import control_plane_host as host
import control_plane_program as prog

REPO = "BeautifulMind-JT/ZARI"
ACTOR = "BeautifulMind-JT"
PLAN1 = "1" * 40
PLAN2 = "2" * 40
HEAD = "a" * 40


def plan(nodes=None):
    return {"schema_version": 1, "program": "zari", "repository": REPO, "project": "ZARI",
            "approval_pointer": "https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/37",
            "authoritative_doc_pointers": "docs/DEVIN_EXECUTION_PLAN.md",
            "nodes": nodes or [{"id": "n1", "title": "Bench re-measure", "spec": "Objective: re-measure.",
                                "audit_floor": "A1"}]}


class FakeGitHub:
    def __init__(self):
        self.issues, self.comments_by_issue, self.pulls, self.reviews, self.checks = {}, {}, {}, {}, {}
        self.contents, self.compare = {}, {}
        self.next_issue, self.next_comment = 30, 5000
        self.posts = 0
        self.fail_post = None      # "before" (not created) or "after" (created, response lost)
        self.fail_update = False

    # control_plane.GithubApi surface
    def issue(self, number):
        return self.issues[number]

    def comments(self, number):
        return [dict(c) for c in self.comments_by_issue.get(number, [])]

    def create_comment(self, number, body):
        self.next_comment += 1
        comment = {"id": self.next_comment, "body": body, "user": {"login": ACTOR}}
        self.comments_by_issue.setdefault(number, []).append(comment)
        return dict(comment)

    def update_comment(self, comment_id, body):
        if self.fail_update:
            raise cp.ControlPlaneError("GitHub API PATCH unavailable")
        for comments in self.comments_by_issue.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return dict(comment)
        raise cp.ControlPlaneError("no such comment")

    def delete_comment(self, comment_id):
        raise AssertionError("unexpected delete")

    def new_issue(self, title, body, labels=("aiops-task",)):
        self.next_issue += 1
        number = self.next_issue
        self.issues[number] = {"number": number, "title": title, "body": body, "state": "open",
                               "html_url": f"https://github.com/{REPO}/issues/{number}",
                               "user": {"login": ACTOR}, "labels": [{"name": l} for l in labels]}
        return number

    def _request(self, method, path, payload=None):
        path_only = path.split("?")[0]
        if method == "POST" and path_only == "/issues":
            self.posts += 1
            if self.fail_post == "before":
                raise cp.ControlPlaneError("GitHub API POST /issues unavailable")
            number = self.new_issue(payload["title"], payload["body"], payload["labels"])
            if self.fail_post == "after":
                raise cp.ControlPlaneError("GitHub API POST /issues unavailable")
            return {"number": number}
        if method == "GET" and path_only == "/issues":
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return [dict(i) for i in self.issues.values()] if page == 1 else []
        match = re.fullmatch(r"/issues/(\d+)", path_only)
        if match and method == "PATCH":
            self.issues[int(match.group(1))]["body"] = payload["body"]
            return {}
        if method == "GET" and path_only.startswith("/contents/"):
            ref = re.search(r"ref=([0-9a-f]{40})", path).group(1)
            return {"content": base64.b64encode(json.dumps(self.contents[ref]).encode()).decode()}
        match = re.fullmatch(r"/compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})", path_only)
        if match:
            return {"status": self.compare[(match.group(1), match.group(2))]}
        match = re.fullmatch(r"/pulls/(\d+)", path_only)
        if match:
            return dict(self.pulls[int(match.group(1))])
        match = re.fullmatch(r"/pulls/(\d+)/reviews", path_only)
        if match:
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return list(self.reviews.get(int(match.group(1)), [])) if page == 1 else []
        match = re.fullmatch(r"/commits/([0-9a-f]{40})/check-runs", path_only)
        if match:
            return {"check_runs": self.checks.get(match.group(1), [])}
        raise AssertionError(f"unrouted {method} {path}")


class FakeHost:
    def __init__(self, directory):
        self.path = Path(directory) / "admission.sqlite"
        self.ledger = host.Ledger(self.path)
        self.ledger.initialize()
        self.policy = {
            "control_uid": 1010, "runner_uid": 1020,
            "builder_uids": {"DEVIN": 1030, "GROK_BUILD": 1040, "GLM": 1050, "CURSOR": 1060},
            "allowed_repositories": [REPO], "enabled_builders": list(prog.LANE_ORDER),
            "max_active_sessions": 4, "max_launches_per_24h": None, "ledger_path": str(self.path),
            "wrapper_paths": dict(host.WRAPPERS),
            "boundary_evidence_pointer": "https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/35",
        }
        self.live = {}  # lane -> live pids
        self.helper_sha = hashlib.sha256((cp.ROOT / "scripts/control_plane_host.py").read_bytes()).hexdigest()

    def confirm(self, packet, policy):
        return subprocess.CompletedProcess([], 0, json.dumps(
            host.result_for(packet, "CONFIRMED", session_id="cli:" + packet["launch_request_id"])))

    def __call__(self, arguments, packet=None):
        # The real host_call turns a nonzero helper exit into ControlPlaneError.
        try:
            return self.dispatch(arguments, packet)
        except host.HostError as exc:
            raise cp.ControlPlaneError(f"host control refused: {exc}") from exc

    def dispatch(self, arguments, packet=None):
        command, flags = arguments[0], dict(zip(arguments[1::2], arguments[2::2]))
        if command == "status" and "--lanes" in arguments:
            return self.ledger.lanes(self.policy)
        if command == "status":
            return self.ledger.status(flags["--launch-request-id"])
        if command == "preflight":
            lane = flags["--builder-id"]
            return {"status": "PASS", "builder_id": lane, "execution_mode": "PERSISTENT_SUPERVISOR",
                    "parallel_safe": True, "launch_contract_version": 2, "host_admission": "ENFORCED",
                    "boundary_evidence_pointer": self.policy["boundary_evidence_pointer"],
                    "allowed_repositories": [REPO], "helper_sha256": self.helper_sha,
                    "worktree_root": f"/opt/astra/worktrees/{lane.lower()}"}
        if command == "launch":
            return host.launch(packet, self.policy, self.ledger, self.confirm)
        if command == "reap":
            return self.ledger.reap(flags["--launch-request-id"], flags["--evidence"], self.policy,
                                    quiescence=lambda lane, policy: self.live.get(lane, []))
        if command == "materialize-begin":
            return self.ledger.materialize_begin(flags["--program"], flags["--node"], flags["--repository"],
                                                 flags["--plan-commit"], self.policy)
        if command == "materialize-finish":
            issue = int(flags["--issue"]) if "--issue" in flags else None
            return self.ledger.materialize_finish(flags["--program"], flags["--node"], flags["--request"],
                                                  flags["--outcome"], issue)
        if command == "materialize-status":
            return self.ledger.materialize_status(flags["--program"], flags["--node"])
        if command == "materialize-plan":
            return self.ledger.materialize_plan(flags["--program"], flags["--node"], flags["--from"], flags["--to"])
        raise AssertionError(f"unrouted host {arguments}")


class ProgramModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gh = FakeGitHub()
        self.gh.contents[PLAN1] = plan()
        self.host = FakeHost(self.temp.name)
        self.cfg = {
            "schema_version": 1, "project": "ZARI", "repository": REPO,
            "allowed_builders": list(prog.LANE_ORDER), "enabled_builders": list(prog.LANE_ORDER),
            "control_record_actor": ACTOR, "allowed_task_actors": [ACTOR], "allowed_dispatch_actors": [ACTOR],
            "program_merge_policy": "STANDARD",
        }
        for target, value in ((cp, "load_config"), (cp, "require_runtime_enabled")):
            patcher = patch.object(target, value, return_value=self.cfg if value == "load_config" else None)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in ((cp, "GithubApi"), (cp, "host_call")):
            patcher = patch.object(target, value, self.fake_api if value == "GithubApi" else self.host)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_ACTOR": ACTOR, "GITHUB_TRIGGERING_ACTOR": ACTOR})
        env.start()
        self.addCleanup(env.stop)
        out = patch("sys.stdout")
        out.start()
        self.addCleanup(out.stop)

    def fake_api(self, repository, token):
        return self.gh

    def file(self, name):
        return Path(self.temp.name) / name

    def record(self, issue):
        comment = cp.find_control_comment(self.gh.comments(issue), ACTOR)
        return cp.parse_control_record(comment["body"])

    def materialized(self, node="n1", commit=PLAN1):
        result = prog.materialize("zari", node, commit)
        self.assertEqual(result["status"], "CREATED")
        return result["issue"]

    def launch_writer(self, issue, node="n1", commit=PLAN1):
        started = prog.start(issue, "zari", node, commit, self.file("packet.json"), preflight=lambda lane: True)
        if started["status"] != "PREPARED":
            return started
        cp.launch_dispatch(self.file("packet.json"), self.file("result.json"))
        packet = json.loads(self.file("packet.json").read_text())
        state = cp.finalize_dispatch(issue, self.file("result.json"), packet["launch_request_id"])
        self.assertEqual(state, "CONFIRMED")
        return {**started, "launch_request_id": packet["launch_request_id"]}

    def deliver(self, issue, pr=7, head=HEAD, green=True):
        self.gh.pulls[pr] = {"state": "open", "head": {"sha": head}, "mergeable_state": "clean"}
        self.gh.checks[head] = [{"status": "completed", "conclusion": "success" if green else "failure"}]
        comment = self.gh.create_comment(issue, f"ASTRA_DELIVERY_V1 pr={pr} head={head}")
        return f"https://github.com/{REPO}/issues/{issue}#issuecomment-{comment['id']}"

    def launch_review(self, issue, slot=1):
        prepared = prog.prepare_review(issue, slot, self.file("rpacket.json"), preflight=lambda lane: True)
        if prepared["status"] != "PREPARED":
            return prepared
        cp.launch_dispatch(self.file("rpacket.json"), self.file("rresult.json"))
        packet = json.loads(self.file("rpacket.json").read_text())
        self.assertEqual(prog.finalize_review(issue, self.file("rresult.json"), packet["launch_request_id"]),
                         "CONFIRMED")
        return {**prepared, "launch_request_id": packet["launch_request_id"]}

    def post_review(self, pr, review_id, verdict="PASS", depth="A1", change="NO"):
        body = (f"ASTRA_REVIEW_V1 review={review_id} head={HEAD} verdict={verdict} "
                f"depth={depth} contract_change={change}")
        self.gh.reviews.setdefault(pr, []).append({"body": body})
        return f"https://github.com/{REPO}/pull/{pr}#pullrequestreview-{len(self.gh.reviews[pr])}"

    def test_full_cycle_plan_to_ready_for_merge(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        self.assertEqual(writer["lane"], "DEVIN")
        record = self.record(issue)
        self.assertEqual((record["owner_lane"], record["launch_state"]), ("DEVIN", "CONFIRMED"))
        envelope = cp.parse_task_envelope(self.gh.issues[issue]["body"])
        self.assertEqual((envelope["BUILDER_ID"], envelope["TASK_ID"]), ("DEVIN", "ZARI-N1"))
        evidence = self.deliver(issue)
        self.assertEqual(prog.reap(issue, writer["launch_request_id"], evidence)["status"], "RELEASED")
        review = self.launch_review(issue)
        self.assertEqual(review["lane"], "GROK_BUILD")  # first idle lane, owner excluded
        review_url = self.post_review(7, review["review_request_id"])
        prog.reap(issue, review["launch_request_id"], review_url)
        check = prog.merge_check(issue, 7)
        self.assertTrue(check["ready"], check["reasons"])
        self.assertEqual(check["head"], HEAD)

    def test_merge_check_refuses_uncomputable_or_failing_gates(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        prog.reap(issue, writer["launch_request_id"], self.deliver(issue, green=False))
        review = self.launch_review(issue)
        self.post_review(7, review["review_request_id"], verdict="FAIL")
        self.cfg["program_merge_policy"] = None
        reasons = " | ".join(prog.merge_check(issue, 7)["reasons"])
        for expected in ("CI is not green", "returned FAIL", "0 of 1 required", "program_merge_policy"):
            self.assertIn(expected, reasons)

    def test_merge_check_sends_astra_gated_work_to_the_user(self):
        self.gh.contents[PLAN1] = plan([{"id": "n1", "title": "t", "spec": "s", "audit_floor": "A3"}])
        issue = self.materialized()
        self.launch_writer(issue)
        self.deliver(issue)
        self.assertIn("Astra gate ARCHITECTURE", " ".join(prog.merge_check(issue, 7)["reasons"]))

    def test_resume_keeps_owner_lane_and_waits_for_it(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        prog.reap(issue, writer["launch_request_id"], self.deliver(issue, green=False))
        # Another task now occupies DEVIN: the resume waits instead of switching lanes.
        self.host.ledger.reserve({"schema_version": 2, "role": "WRITER", "owner_lane": "DEVIN",
                                  "repository": REPO, "task_id": "OTHER", "task_revision": "r",
                                  "builder_id": "DEVIN", "launch_request_id": "9" * 24, "attempt_id": 1},
                                 self.host.policy)
        waiting = prog.start(issue, "zari", "n1", PLAN1, self.file("p2.json"), preflight=lambda lane: True)
        self.assertEqual((waiting["status"], waiting["owner_lane"]), ("NO_IDLE_LANE", "DEVIN"))
        self.host.ledger.finalize({"schema_version": 2, "role": "WRITER", "owner_lane": "DEVIN",
                                   "repository": REPO, "task_id": "OTHER", "task_revision": "r",
                                   "builder_id": "DEVIN", "launch_request_id": "9" * 24, "attempt_id": 1},
                                  {"outcome": "FAILED_PRESTART", "reason": "test"})
        resumed = self.launch_writer(issue)
        self.assertEqual((resumed["lane"], resumed["attempt"]), ("DEVIN", 2))
        record = self.record(issue)
        self.assertEqual(record["previous_attempts"][0]["fenced_by_host_state"], cp.VERIFIED_RELEASE)

    def test_owner_live_blocks_restart_until_verified_release(self):
        issue = self.materialized()
        self.launch_writer(issue)
        again = prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(again["status"], "TASK_ACTIVE")

    def test_review_waits_for_verified_writer_release(self):
        issue = self.materialized()
        self.launch_writer(issue)
        self.deliver(issue)
        with self.assertRaisesRegex(cp.ControlPlaneError, "RELEASED"):
            prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)

    def test_a2_second_reviewer_excludes_owner_and_first_reviewer(self):
        self.gh.contents[PLAN1] = plan([{"id": "n1", "title": "t", "spec": "s", "audit_floor": "A2"}])
        issue = self.materialized()
        writer = self.launch_writer(issue)
        prog.reap(issue, writer["launch_request_id"], self.deliver(issue))
        first = self.launch_review(issue, slot=1)
        second = self.launch_review(issue, slot=2)
        self.assertEqual((first["lane"], second["lane"]), ("GROK_BUILD", "GLM"))
        with self.assertRaises(cp.ControlPlaneError):
            prog.prepare_review(issue, 3, self.file("r3.json"))

    def test_lost_released_projection_is_repaired_from_host_result(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        evidence = self.deliver(issue)
        self.gh.fail_update = True
        with self.assertRaises(cp.ControlPlaneError):
            prog.reap(issue, writer["launch_request_id"], evidence)
        self.gh.fail_update = False
        self.assertEqual(self.record(issue)["launch_state"], "CONFIRMED")  # projection lost
        repaired = prog.reap(issue, writer["launch_request_id"], evidence)
        self.assertEqual((repaired["status"], repaired["repeated"]), ("RELEASED", True))
        self.assertEqual(self.record(issue)["launch_state"], "RELEASED")

    def test_reap_refuses_while_lane_uid_has_live_processes(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        self.host.live["DEVIN"] = [321]
        with self.assertRaises(cp.ControlPlaneError):
            prog.reap(issue, writer["launch_request_id"], self.deliver(issue))
        self.assertEqual(self.record(issue)["launch_state"], "CONFIRMED")

    def test_lost_create_response_recovers_the_same_issue(self):
        self.gh.fail_post = "after"
        self.assertEqual(prog.materialize("zari", "n1", PLAN1)["status"], "MATERIALIZE_UNKNOWN")
        self.gh.fail_post = None
        recovered = prog.materialize("zari", "n1", PLAN1)
        self.assertEqual((recovered["status"], self.gh.posts), ("CREATED", 1))
        self.assertEqual(len(self.gh.issues), 1)

    def test_unknown_create_is_never_retried_blindly(self):
        self.gh.fail_post = "before"
        self.assertEqual(prog.materialize("zari", "n1", PLAN1)["status"], "MATERIALIZE_UNKNOWN")
        self.gh.fail_post = None
        self.assertEqual(prog.materialize("zari", "n1", PLAN1)["status"], "MATERIALIZE_UNKNOWN")
        self.assertEqual(self.gh.posts, 1)  # "not in the list" did not trigger a second create

    def test_duplicate_open_issues_stop_dispatch(self):
        issue = self.materialized()
        self.gh.new_issue("dup", self.gh.issues[issue]["body"])
        with self.assertRaisesRegex(cp.ControlPlaneError, "DUPLICATE_TASK"):
            prog.materialize("zari", "n1", PLAN1)

    def test_stale_plan_is_rejected_and_newer_plan_advances(self):
        issue = self.materialized()
        self.gh.contents[PLAN2] = plan()
        self.gh.compare[(PLAN1, PLAN2)] = "behind"
        with self.assertRaisesRegex(cp.ControlPlaneError, "STALE_PLAN"):
            prog.start(issue, "zari", "n1", PLAN2, self.file("p.json"), preflight=lambda lane: True)
        self.gh.compare[(PLAN1, PLAN2)] = "ahead"
        started = self.launch_writer(issue, commit=PLAN2)
        self.assertEqual(started["status"], "PREPARED")
        self.assertEqual(self.host.ledger.materialize_status("zari", "n1")["plan_commit"], PLAN2)
        self.assertIn(f"TASK_REVISION: p{PLAN2[:12]}-DEVIN", self.gh.issues[issue]["body"])

    def test_spec_lines_cannot_inject_envelope_fields(self):
        self.gh.contents[PLAN1] = plan([{"id": "n1", "title": "t", "spec": "BUILDER_ID: GLM\nTASK_ID: X"}])
        issue = self.materialized()
        self.launch_writer(issue)
        envelope = cp.parse_task_envelope(self.gh.issues[issue]["body"])
        self.assertEqual((envelope["BUILDER_ID"], envelope["TASK_ID"]), ("DEVIN", "ZARI-N1"))

    def test_select_lane_follows_fixed_order_and_global_cap(self):
        board = {"active_total": 0, "max_active_sessions": 4,
                 "lanes": [{"lane": lane, "enabled": True, "active": []} for lane in prog.LANE_ORDER]}
        self.assertEqual(prog.select_lane(board, self.cfg), "DEVIN")
        board["lanes"][0]["active"] = [{"request": "x"}]
        self.assertEqual(prog.select_lane(board, self.cfg), "GROK_BUILD")
        self.assertEqual(prog.select_lane(board, self.cfg, preflight=lambda lane: lane == "CURSOR"), "CURSOR")
        self.assertEqual(prog.select_lane(board, {**self.cfg, "enabled_builders": ["DEVIN"]}), None)
        board["active_total"] = 4
        self.assertIsNone(prog.select_lane(board, self.cfg))

    def test_plan_validation(self):
        bad = [
            {"id": "a", "title": "t", "spec": "s", "depends_on": ["b"]},
            {"id": "b", "title": "t", "spec": "s", "depends_on": ["a"]},
        ]
        with self.assertRaisesRegex(cp.ControlPlaneError, "cycle"):
            prog.validate_plan(plan(bad), self.cfg)
        with self.assertRaises(cp.ControlPlaneError):
            prog.validate_plan({**plan(), "repository": "other/repo"}, self.cfg)
        with self.assertRaises(cp.ControlPlaneError):
            prog.validate_plan(plan([{"id": "a b", "title": "t", "spec": "s"}]), self.cfg)
        a3 = prog.validate_plan(plan([{"id": "a", "title": "t", "spec": "s", "audit_floor": "A3"}]), self.cfg)
        self.assertEqual(a3["nodes"][0]["astra_gate"], "ARCHITECTURE")


if __name__ == "__main__":
    unittest.main()
