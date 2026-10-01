"""Program-mode runtime tests against a fake GitHub and the real host ledger (no network).

Every lane posts with the same GitHub account, so these tests treat all GitHub text as
attacker-editable and check that gates follow only host pins, the plan and live PR state.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
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


def node(node_id="n1", floor="A1", **extra):
    return {"id": node_id, "title": f"t {node_id}", "spec": "s", "audit_floor": floor, **extra}


def now_iso():
    # time.gmtime() alone reads the coarse clock, which can lag time.time() (the ledger's
    # clock) across a second boundary and make a fresh comment look older than its session.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))


SUDOERS = cp.ROOT / ".github/control-plane/sudoers-aiops-program.example"
# The runner rules that predate program mode (docs/CONTROL_PLANE_RUNTIME.md).
BASE_RUNNER_RULES = ("launch", "^status --launch-request-id [0-9a-f]{24}$",
                     "^preflight --builder-id (DEVIN|GROK_BUILD|GLM)$")


def sudoers_runner_rules(text):
    """Argument patterns of the AIOPS_PROGRAM_HOST alias, as the sudoers parser reads them."""
    body = text.split("Cmnd_Alias AIOPS_PROGRAM_HOST =", 1)[1].split("\n\n", 1)[0].replace("\\\n", " ")
    rules = []
    for entry in re.split(r"(?<!\\),", body):
        command, _, args = entry.strip().partition(" ")
        assert command == "/opt/astra/bin/astra-host-control", entry
        # A backslash before : \ , = # or blank is removed; an unescaped one of those ends the entry.
        assert not re.search(r"(?<!\\)[#:,=]", args), f"unescaped sudoers special character in {args}"
        rules.append(re.sub(r"\s+", " ", re.sub(r"\\([:\\,= \t#])", r"\1", args)).strip())
    return rules


RUNNER_RULES = (*BASE_RUNNER_RULES, *sudoers_runner_rules(SUDOERS.read_text(encoding="utf-8")))


def sudoers_allows(arguments, rules=RUNNER_RULES):
    """sudo >= 1.9.10: a ^...$ argument pattern must match the whole space-joined argument line."""
    line = " ".join(arguments)
    return any(re.search(rule, line) if rule.startswith("^") and rule.endswith("$") else rule == line
               for rule in rules)


def signed_delivery(packet, pr=7, head=HEAD):
    mac = host.pin_mac(packet["delivery_nonce"], ("ASTRA_DELIVERY_V1", packet["launch_request_id"], str(pr), head))
    return f"ASTRA_DELIVERY_V1 pr={pr} head={head} mac={mac}"


def signed_blocker(packet, kind="BLOCKED", nonce=None):
    key = nonce or packet.get("review_nonce") or packet["delivery_nonce"]
    launch = packet["launch_request_id"]
    return f"ASTRA_BLOCKED_V1 kind={kind} launch={launch} mac={host.pin_mac(key, ('ASTRA_BLOCKED_V1', launch, kind))}"


def signed_review(packet, verdict="PASS", depth="A1", change="NO", nonce=None, required="A1"):
    fields = ("ASTRA_REVIEW_V1", packet["review_request_id"], packet["head_sha"], verdict, depth, required, change)
    mac = host.pin_mac(nonce or packet["review_nonce"], fields)
    return (f"ASTRA_REVIEW_V1 review={packet['review_request_id']} head={packet['head_sha']} verdict={verdict} "
            f"depth={depth} required={required} contract_change={change} mac={mac}")


class FakeGitHub:
    def __init__(self):
        self.issues, self.comments_by_issue, self.pulls, self.reviews, self.checks = {}, {}, {}, {}, {}
        self.contents, self.compare, self.statuses = {}, {}, {}
        self.file_contents, self.workflows = {}, []
        self.unmerged = set()      # commits not on the default branch
        self.next_issue, self.next_comment, self.next_review = 30, 5000, 900
        self.posts = self.label_posts = 0
        self.fail_post = None      # "before" (not created) or "after" (created, response lost)
        self.fail_update = False
        self.drop_labels = False   # the create response omits the label
        self.merges = []

    # control_plane.GithubApi surface
    def issue(self, number):
        return json.loads(json.dumps(self.issues[number]))

    def comments(self, number):
        return [dict(c) for c in self.comments_by_issue.get(number, [])]

    def create_comment(self, number, body, login=ACTOR, created_at=None):
        self.next_comment += 1
        comment = {"id": self.next_comment, "body": body, "user": {"login": login},
                   "created_at": created_at or now_iso()}
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

    def add_review(self, pr, body, commit_id=HEAD, login=ACTOR, submitted_at=None):
        self.next_review += 1
        self.reviews.setdefault(pr, []).append({"id": self.next_review, "body": body, "commit_id": commit_id,
                                                "user": {"login": login},
                                                "submitted_at": submitted_at or now_iso()})
        return f"https://github.com/{REPO}/pull/{pr}#pullrequestreview-{self.next_review}"

    def _request(self, method, path, payload=None):
        path_only = path.split("?")[0]
        paged = re.search(r"[?&]page=(\d+)", path)
        page = int(paged.group(1)) if paged else 1
        if method == "GET" and path_only == "":
            return {"default_branch": "main"}
        if method == "POST" and path_only == "/issues":
            self.posts += 1
            if self.fail_post == "before":
                raise cp.ControlPlaneError("GitHub API POST /issues unavailable")
            number = self.new_issue(payload["title"], payload["body"], [] if self.drop_labels else payload["labels"])
            if self.fail_post == "after":
                raise cp.ControlPlaneError("GitHub API POST /issues unavailable")
            return {"number": number, "labels": self.issues[number]["labels"]}
        if method == "GET" and path_only == "/issues":
            return [dict(i) for i in self.issues.values()] if page == 1 else []
        match = re.fullmatch(r"/issues/(\d+)/labels", path_only)
        if match and method == "POST":
            self.label_posts += 1
            self.issues[int(match.group(1))]["labels"] += [{"name": l} for l in payload["labels"]]
            return []
        match = re.fullmatch(r"/issues/(\d+)", path_only)
        if match and method == "PATCH":
            self.issues[int(match.group(1))]["body"] = payload["body"]
            return {}
        match = re.fullmatch(r"/issues/comments/(\d+)", path_only)
        if match and method == "GET":
            for comments in self.comments_by_issue.values():
                for comment in comments:
                    if comment["id"] == int(match.group(1)):
                        return dict(comment)
            raise cp.ControlPlaneError("GitHub API GET comment failed: 404")
        if method == "GET" and path_only.startswith("/contents/"):
            ref = re.search(r"ref=([0-9a-f]{40})", path).group(1)
            if (path_only[len("/contents/"):], ref) in self.file_contents:
                return self.file_contents[(path_only[len("/contents/"):], ref)]
            return {"content": base64.b64encode(json.dumps(self.contents[ref]).encode()).decode()}
        if method == "GET" and path_only == "/actions/runs":
            return {"workflow_runs": self.workflows if page == 1 else []}
        match = re.fullmatch(r"/compare/main\.\.\.([0-9a-f]{40})", path_only)
        if match:
            return {"status": "ahead" if match.group(1) in self.unmerged else "behind"}
        match = re.fullmatch(r"/compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})", path_only)
        if match:
            return {"status": self.compare[(match.group(1), match.group(2))]}
        match = re.fullmatch(r"/pulls/(\d+)/merge", path_only)
        if match and method == "PUT":
            pull = self.pulls[int(match.group(1))]
            self.merges.append((int(match.group(1)), payload))
            if payload.get("sha") != pull["head"]["sha"]:
                raise cp.ControlPlaneError("GitHub API PUT merge failed: 409 Head branch was modified")
            pull.update(merged=True, state="closed")
            return {"merged": True, "sha": "f" * 40}
        match = re.fullmatch(r"/pulls/(\d+)", path_only)
        if match:
            return json.loads(json.dumps(self.pulls[int(match.group(1))]))
        match = re.fullmatch(r"/pulls/(\d+)/reviews", path_only)
        if match:
            return [dict(r) for r in self.reviews.get(int(match.group(1)), [])] if page == 1 else []
        match = re.fullmatch(r"/pulls/(\d+)/reviews/(\d+)", path_only)
        if match:
            for review in self.reviews.get(int(match.group(1)), []):
                if review["id"] == int(match.group(2)):
                    return dict(review)
            raise cp.ControlPlaneError("GitHub API GET review failed: 404")
        match = re.fullmatch(r"/commits/([0-9a-f]{40})/check-runs", path_only)
        if match:
            return {"check_runs": self.checks.get(match.group(1), []) if page == 1 else []}
        match = re.fullmatch(r"/commits/([0-9a-f]{40})/status", path_only)
        if match:
            return self.statuses.get(match.group(1), {"state": "pending", "statuses": []})
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
        # Every helper call the runtime makes must be one the installed sudoers rules admit.
        assert sudoers_allows(arguments), f"sudoers would refuse the runner: {arguments}"
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
        if command == "task-status":
            return self.ledger.task_status(flags["--repository"], flags["--task"])
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
            reap_flags = dict(zip(arguments[1:5:2], arguments[2:5:2]))
            # Mirrors the helper CLI: a pin arrives only as {"pin": ...} on stdin with --pin-stdin.
            if "--pin-stdin" in arguments:
                assert set(packet) == {"pin"} and isinstance(packet["pin"], str)
            else:
                assert packet is None
            return self.ledger.reap(reap_flags["--launch-request-id"], reap_flags["--evidence"], self.policy,
                                    pin=(packet or {}).get("pin"),
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
            "program_merge_policy": "STANDARD", "program_required_checks": ["offline"],
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
        # These provider-free fixtures have an explicitly clear protected
        # journal. Production queries root before any terminal writer resume.
        original_fable_program = prog.fable_program
        p = patch.object(prog, "fable_program", side_effect=lambda operation, issue, **fields:
                         {"status": "CLEAR"} if operation == "decision-status"
                         else original_fable_program(operation, issue, **fields))
        p.start(); self.addCleanup(p.stop)
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

    def edit_record(self, issue, change):
        comment = cp.find_control_comment(self.gh.comments(issue), ACTOR)
        record = cp.parse_control_record(comment["body"])
        change(record)
        self.gh.update_comment(comment["id"], cp.render_control_record(record))

    def materialized(self, node_id="n1", commit=PLAN1):
        result = prog.materialize("zari", node_id, commit)
        self.assertEqual(result["status"], "CREATED")
        return result["issue"]

    def launch_writer(self, issue, node_id="n1", commit=PLAN1):
        started = prog.start(issue, "zari", node_id, commit, self.file("packet.json"), preflight=lambda lane: True)
        if started["status"] != "PREPARED":
            return started
        self.assertEqual(self.file("packet.json").stat().st_mode & 0o777, 0o600)
        cp.launch_dispatch(self.file("packet.json"), self.file("result.json"))
        packet = json.loads(self.file("packet.json").read_text())
        self.assertEqual(cp.finalize_dispatch(issue, self.file("result.json"), packet["launch_request_id"]),
                         "CONFIRMED")
        return {**started, "launch_request_id": packet["launch_request_id"], "packet": packet}

    def pr(self, pr=7, sha=HEAD, ref="astra/zari-n1", **overrides):
        self.gh.pulls[pr] = {"state": "open", "draft": False, "mergeable_state": "clean", "merged": False,
                             "merge_commit_sha": "f" * 40,
                             "head": {"sha": sha, "ref": ref, "repo": {"full_name": REPO}}, "base": {"ref": "main"},
                             **overrides}

    def comment_url(self, issue, body, **comment):
        posted = self.gh.create_comment(issue, body, **comment)
        return f"https://github.com/{REPO}/issues/{issue}#issuecomment-{posted['id']}"

    def blocked(self, issue, session, kind="BLOCKED", **comment):
        return self.comment_url(issue, f"{kind}: see reason\n\n" + signed_blocker(session["packet"], kind), **comment)

    def deliver(self, issue, writer, pr=7, head=HEAD, green=True, body=None, keep_pr=False, **comment):
        if not keep_pr:
            self.pr(pr, head, ref=prog.branch_for(writer["packet"]["task_id"]))
        self.gh.checks[head] = [{"name": "offline", "status": "completed",
                                 "conclusion": "success" if green else "failure"}]
        text = body if body is not None else "Ready.\n\n" + signed_delivery(writer["packet"], pr, head)
        posted = self.gh.create_comment(issue, text, **comment)
        return f"https://github.com/{REPO}/issues/{issue}#issuecomment-{posted['id']}"

    def launch_review(self, issue, slot=1):
        if not self.file("rpacket.json").exists():
            self.file("rpacket.json").write_text("{}")
            self.file("rpacket.json").chmod(0o644)  # a pre-existing file must still end up 0600
        prepared = prog.prepare_review(issue, slot, self.file("rpacket.json"), preflight=lambda lane: True)
        if prepared["status"] != "PREPARED":
            return prepared
        packet_file = self.file("rpacket.json")
        self.assertEqual(packet_file.stat().st_mode & 0o777, 0o600)
        cp.launch_dispatch(packet_file, self.file("rresult.json"))
        packet = json.loads(packet_file.read_text())
        self.assertEqual(prog.finalize_review(issue, self.file("rresult.json"), packet["launch_request_id"]),
                         "CONFIRMED")
        return {**prepared, "launch_request_id": packet["launch_request_id"], "packet": packet}

    def post_review(self, pr, review, verdict="PASS", depth="A1", change="NO", nonce=None, line=None,
                    required="A1", **kwargs):
        body = (line or signed_review(review["packet"], verdict, depth, change, nonce, required)) + "\n\nfindings"
        return self.gh.add_review(pr, body, **kwargs)

    def released_writer(self, issue=None, node_id="n1", **deliver):
        issue = issue or self.materialized(node_id)
        writer = self.launch_writer(issue, node_id)
        released = prog.reap(issue, writer["launch_request_id"], self.deliver(issue, writer, **deliver))
        self.assertEqual(released["status"], "RELEASED")
        return issue, writer

    def reviewed(self, issue, slot=1, **verdict):
        review = self.launch_review(issue, slot)
        prog.reap(issue, review["launch_request_id"], self.post_review(7, review, **verdict))
        return review

    def reasons(self, issue, pr=7):
        return " | ".join(prog.merge_check(issue, pr)["reasons"])

    def rows(self, task="ZARI-N1"):
        return self.host.ledger.task_status(REPO, task)["rows"]

    # ------------------------------------------------------------------ full cycle

    def test_full_cycle_plan_to_ready_for_merge(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        self.assertEqual(writer["lane"], "DEVIN")
        record = self.record(issue)
        self.assertEqual((record["owner_lane"], record["launch_state"]), ("DEVIN", "CONFIRMED"))
        envelope = cp.parse_task_envelope(self.gh.issues[issue]["body"])
        self.assertEqual((envelope["BUILDER_ID"], envelope["TASK_ID"]), ("DEVIN", "ZARI-N1"))
        self.assertEqual(prog.reap(issue, writer["launch_request_id"], self.deliver(issue, writer))["status"],
                         "RELEASED")
        self.assertEqual(self.record(issue)["delivery"], {"kind": "DELIVERY", "pr": 7, "head": HEAD})
        review = self.launch_review(issue)
        self.assertEqual(review["lane"], "GROK_BUILD")  # first idle lane, writer excluded
        for secret in (review["packet"]["review_nonce"], writer["packet"]["delivery_nonce"]):
            self.assertNotIn(secret, json.dumps(self.record(issue)))
            self.assertNotIn(secret, json.dumps(self.rows()))
        prog.reap(issue, review["launch_request_id"], self.post_review(7, review))
        self.assertEqual(self.record(issue)["reviews"][0]["verdict"]["verdict"], "PASS")
        check = prog.merge_check(issue, 7)
        self.assertTrue(check["ready"], check["reasons"])
        self.assertEqual(check["head"], HEAD)

    # ------------------------------------------------------------------ merge readiness

    def test_merge_check_refuses_uncomputable_or_failing_gates(self):
        issue, _ = self.released_writer(green=False)
        self.reviewed(issue, verdict="FAIL")
        self.cfg["program_merge_policy"] = None
        reasons = self.reasons(issue)
        for expected in ("incomplete or failing", "required check 'offline' has not succeeded", "returned FAIL",
                         "0 of 1 required", "program_merge_policy"):
            self.assertIn(expected, reasons)

    def test_merge_check_requires_a_clean_same_repo_pr_against_the_default_branch(self):
        issue, _ = self.released_writer()
        self.reviewed(issue)
        self.assertTrue(prog.merge_check(issue, 7)["ready"])
        cases = [
            ({"draft": True}, "non-draft"),
            ({"mergeable_state": "unstable"}, "must be clean"),
            ({"base": {"ref": "release"}}, "default branch"),
            ({"head": {"sha": HEAD, "ref": "astra/zari-n1", "repo": {"full_name": "fork/ZARI"}}}, "this repository"),
            ({"head": {"sha": HEAD, "ref": "feature/x", "repo": {"full_name": REPO}}}, "task branch astra/zari-n1"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.pr(**overrides)
                self.assertIn(expected, self.reasons(issue))
        self.pr()
        self.gh.statuses[HEAD] = {"state": "failure", "statuses": [{"context": "ci", "state": "failure"}]}
        self.assertIn("commit statuses are failure", self.reasons(issue))
        self.gh.statuses.pop(HEAD)
        self.gh.issues[issue]["labels"].append({"name": "needs-user"})
        self.assertIn("blocker labels", self.reasons(issue))

    def test_merge_check_is_bound_to_the_pinned_delivery_head(self):
        issue, _ = self.released_writer()
        self.reviewed(issue)
        other = "b" * 40
        self.pr(sha=other)  # the writer pushed after delivering
        self.gh.checks[other] = [{"name": "offline", "status": "completed", "conclusion": "success"}]
        reasons = self.reasons(issue)
        self.assertIn("host-pinned delivery", reasons)
        self.assertIn("0 of 1 required", reasons)  # the PASS was for the delivered head only

    def test_shallow_review_does_not_satisfy_a2(self):
        self.gh.contents[PLAN1] = plan([node(floor="A2")])
        issue, _ = self.released_writer()
        self.reviewed(issue, depth="A1")
        self.reviewed(issue, slot=2, depth="A2")
        self.assertIn("1 of 2 required", self.reasons(issue))

    def test_a2_needs_two_distinct_non_writer_lanes(self):
        self.gh.contents[PLAN1] = plan([node(floor="A2")])
        issue, _ = self.released_writer()
        self.reviewed(issue, depth="A2")
        self.assertIn("1 of 2 required", self.reasons(issue))
        self.reviewed(issue, slot=2, depth="A2")
        check = prog.merge_check(issue, 7)
        self.assertTrue(check["ready"], check["reasons"])

    def test_a_verdict_from_a_writer_lane_never_counts(self):
        issue, writer = self.released_writer()
        # A reviewer launched on the writer's own lane (a caller bug: the packet lies about the owner).
        review_id = prog.review_request_id(REPO, "ZARI-N1", writer["launch_request_id"], HEAD, 1)
        packet = {**writer["packet"], "role": "REVIEWER", "builder_id": "DEVIN", "owner_lane": "GLM",
                  "review_request_id": review_id, "review_nonce": "7" * 32, "head_sha": HEAD, "pr_number": 7,
                  "launch_request_id": prog.review_launch_id(review_id, 1), "attempt_id": 1}
        packet.pop("delivery_nonce")
        self.assertEqual(host.launch(packet, self.host.policy, self.host.ledger, self.host.confirm)["outcome"],
                         "CONFIRMED")
        self.host.ledger.reap(packet["launch_request_id"], "https://github.com/o/r/pull/7#pullrequestreview-1",
                              self.host.policy, pin=signed_review(packet), quiescence=lambda lane, policy: [])
        self.assertIn("0 of 1 required", self.reasons(issue))

    def test_contract_change_goes_to_a_decision(self):
        issue, _ = self.released_writer()
        self.reviewed(issue, change="YES")
        self.assertIn("contract change", self.reasons(issue))

    def test_merge_check_sends_astra_gated_work_to_the_user(self):
        self.gh.contents[PLAN1] = plan([node(floor="A3")])
        issue, _ = self.released_writer()
        self.assertIn("Astra gate ARCHITECTURE", self.reasons(issue))

    def test_active_review_blocks_readiness(self):
        self.gh.contents[PLAN1] = plan([node(floor="A2")])
        issue, _ = self.released_writer()
        self.reviewed(issue, depth="A2")
        self.launch_review(issue, slot=2)
        self.assertIn("still active", self.reasons(issue))

    # ------------------------------------------------------------------ attacks from the re-review (R1-R7)

    def test_r1_a_released_verdict_cannot_be_flipped(self):
        issue, _ = self.released_writer()
        review = self.reviewed(issue, verdict="FAIL")
        copy = self.post_review(7, review, verdict="PASS")  # even a correctly signed second line
        with self.assertRaisesRegex(cp.ControlPlaneError, "write-once"):
            prog.reap(issue, review["launch_request_id"], copy)
        self.assertIn("returned FAIL", self.reasons(issue))

    def test_r2_an_edited_verdict_does_not_verify(self):
        issue, _ = self.released_writer()
        review = self.launch_review(issue)
        genuine = signed_review(review["packet"], "FAIL")
        url = self.post_review(7, review, line=genuine.replace("verdict=FAIL", "verdict=PASS"))  # edited before reap
        with self.assertRaisesRegex(cp.ControlPlaneError, "MAC"):
            prog.reap(issue, review["launch_request_id"], url)
        self.assertEqual(self.host.ledger.status(review["launch_request_id"])["state"], "CONFIRMED")
        self.assertIn("0 of 1 required", self.reasons(issue))

    def test_writer_cannot_sign_a_review(self):
        issue, writer = self.released_writer()
        review = self.launch_review(issue)
        # The writer knows its own delivery key, never the reviewer's.
        forged = self.post_review(7, review, nonce=writer["packet"]["delivery_nonce"])
        with self.assertRaisesRegex(cp.ControlPlaneError, "MAC"):
            prog.reap(issue, review["launch_request_id"], forged)
        self.assertIn("0 of 1 required", self.reasons(issue))

    def test_r3_a_later_delivery_cannot_repin_a_released_writer(self):
        issue, writer = self.released_writer()
        later = self.deliver(issue, writer, pr=8, head="b" * 40)
        with self.assertRaisesRegex(cp.ControlPlaneError, "write-once"):
            prog.reap(issue, writer["launch_request_id"], later)
        self.assertEqual(self.record(issue)["delivery"]["pr"], 7)

    def test_r4_issue_body_edits_do_not_change_the_gates(self):
        self.gh.contents[PLAN1] = plan([node(floor="A3")])
        issue, _ = self.released_writer()
        body = self.gh.issues[issue]["body"]
        self.gh.issues[issue]["body"] = body.replace("AUDIT_FLOOR: A3", "AUDIT_FLOOR: A0").replace(
            "ASTRA_GATE: ARCHITECTURE", "ASTRA_GATE: NONE")
        reasons = self.reasons(issue)
        self.assertIn("differs from the envelope", reasons)
        self.assertIn("0 of 2 required", reasons)       # the floor still comes from the plan
        self.assertIn("Astra gate ARCHITECTURE", reasons)
        # D2: review restores the envelope from the host record and the plan instead of wedging.
        prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)
        self.assertEqual(self.gh.issues[issue]["body"], body)
        self.assertNotIn("differs from the envelope", self.reasons(issue))

    def test_r5_one_pr_cannot_be_the_delivery_of_two_tasks(self):
        self.gh.contents[PLAN1] = plan([node("n1"), node("n2")])
        _, first_writer = self.released_writer(node_id="n1")
        second = self.materialized("n2")
        second_writer = self.launch_writer(second, "n2")
        self.assertEqual(second_writer["lane"], "DEVIN")  # DEVIN is idle again after its release
        # n1's line copied onto n2 names n1's PR, which is not on n2's branch.
        with self.assertRaisesRegex(cp.ControlPlaneError, "this task's branch astra/zari-n2"):
            prog.reap(second, second_writer["launch_request_id"],
                      self.comment_url(second, signed_delivery(first_writer["packet"])))
        # A line signed with n1's key for a PR on n2's branch does not verify under n2's session.
        self.pr(8, "b" * 40, ref="astra/zari-n2")
        with self.assertRaisesRegex(cp.ControlPlaneError, "MAC"):
            prog.reap(second, second_writer["launch_request_id"],
                      self.comment_url(second, signed_delivery(first_writer["packet"], 8, "b" * 40)))
        # n2's own writer signing n1's PR is refused: that PR comes from n1's branch (the host also refuses a
        # PR another task already pinned).
        with self.assertRaisesRegex(cp.ControlPlaneError, "this task's branch astra/zari-n2"):
            prog.reap(second, second_writer["launch_request_id"],
                      self.deliver(second, second_writer, keep_pr=True))

    def test_r6_control_record_edits_do_not_change_readiness(self):
        issue, _ = self.released_writer()
        self.edit_record(issue, lambda record: record.setdefault("reviews", []).append({
            "launch_request_id": "e" * 24, "review_request_id": "e" * 24, "slot": 1, "lane": "GLM",
            "head_sha": HEAD, "pr": 7, "state": "RELEASED", "attempt": 1,
            "verdict": {"kind": "REVIEW", "verdict": "PASS", "depth": "A2", "contract_change": "NO", "head": HEAD}}))
        self.edit_record(issue, lambda record: record.update(owner_lane="CURSOR"))
        self.assertIn("0 of 1 required", self.reasons(issue))

    def test_r7_reviewer_released_without_a_verdict_is_retried(self):
        issue, _ = self.released_writer()
        first = self.launch_review(issue)
        prog.reap(issue, first["launch_request_id"], self.blocked(issue, first))
        retry = self.launch_review(issue)
        self.assertEqual((retry["status"], retry["attempt"]), ("PREPARED", 2))
        prog.reap(issue, retry["launch_request_id"], self.post_review(7, retry))
        check = prog.merge_check(issue, 7)
        self.assertTrue(check["ready"], check["reasons"])

    def test_an_operator_reconciled_review_without_verdict_is_retried(self):
        issue, _ = self.released_writer()
        first = self.launch_review(issue)
        self.host.ledger.reconcile(first["launch_request_id"], "cli:" + first["launch_request_id"],
                                   f"https://github.com/{REPO}/issues/{issue}")
        self.assertEqual(self.launch_review(issue)["attempt"], 2)

    def test_n1_a_fail_verdict_cannot_be_discarded_to_re_roll_the_review(self):
        issue, writer = self.released_writer()
        review = self.launch_review(issue)
        self.post_review(7, review, verdict="FAIL")
        # The writer's lane (same account) breaks the genuine line, then tries to release the reviewer.
        self.gh.reviews[7][-1]["body"] = "edited away"
        for evidence in (self.comment_url(issue, "BLOCKED: re-roll please"),
                         self.comment_url(issue, signed_blocker({**review["packet"]},
                                                                nonce=writer["packet"]["delivery_nonce"]))):
            with self.subTest(evidence=evidence), self.assertRaises(cp.ControlPlaneError):
                prog.reap(issue, review["launch_request_id"], evidence)
        self.assertEqual(self.host.ledger.status(review["launch_request_id"])["state"], "CONFIRMED")
        again = prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)
        self.assertEqual(again["status"], "REVIEW_EXISTS")  # no re-roll: the session is still the slot's
        self.assertIn("0 of 1 required", self.reasons(issue))

    def test_unsigned_markers_never_release_a_session(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        for text in ("DECISION_REQUIRED: which API?", "BLOCKED", f"ASTRA_DELIVERY_V1 pr=7 head={HEAD}"):
            with self.subTest(text=text), self.assertRaisesRegex(cp.ControlPlaneError, "exactly one signed"):
                prog.reap(issue, writer["launch_request_id"], self.comment_url(issue, text))
        both = signed_delivery(writer["packet"]) + "\n" + signed_blocker(writer["packet"])
        self.pr(ref="astra/zari-n1")
        with self.assertRaisesRegex(cp.ControlPlaneError, "exactly one signed"):
            prog.reap(issue, writer["launch_request_id"], self.comment_url(issue, both))
        self.assertEqual(self.record(issue)["launch_state"], "CONFIRMED")

    def test_a0_is_promoted_to_a1_in_program_mode(self):
        # Astra A3 finding 4: no A0 qualification path exists, so A0 never skips review.
        self.gh.contents[PLAN1] = plan([node(floor="A0")])
        issue, _ = self.released_writer()
        self.assertIn("0 of 1 required", self.reasons(issue))
        self.assertIn("AUDIT_FLOOR: A1", self.gh.issues[issue]["body"])
        self.reviewed(issue)
        check = prog.merge_check(issue, 7)
        self.assertTrue(check["ready"], check["reasons"])

    def test_required_checks_must_each_succeed_on_the_head(self):
        # Astra A3 finding 5: only observed checks were examined.
        issue, _ = self.released_writer()
        self.reviewed(issue)
        self.assertTrue(prog.merge_check(issue, 7)["ready"])
        cases = [
            ([{"name": "lint", "status": "completed", "conclusion": "skipped"}], "required check 'offline'"),
            ([{"name": "offline", "status": "completed", "conclusion": "skipped"}], "required check 'offline'"),
            ([{"name": "offline", "status": "in_progress", "conclusion": None}], "incomplete or failing"),
            ([], "required check 'offline'"),
        ]
        for runs, expected in cases:
            with self.subTest(runs=runs):
                self.gh.checks[HEAD] = runs
                self.assertIn(expected, self.reasons(issue))
        self.gh.checks[HEAD] = [{"name": "offline", "status": "completed", "conclusion": "success"}]
        self.cfg.pop("program_required_checks")
        self.assertIn("required checks are not declared", self.reasons(issue))

    def test_only_the_newest_run_of_each_check_counts(self):
        # A head keeps every run of a check: the skipped run from while its PR was a draft, or
        # a failed attempt before a re-run. Branch protection reads the newest one; so do we.
        issue, _ = self.released_writer()
        self.reviewed(issue)

        def run(run_id, conclusion, status="completed", app=None):
            return {"id": run_id, "name": "offline", "status": status, "conclusion": conclusion,
                    **({"app": {"id": app}} if app else {})}

        for runs, ready in (
                ([run(1, "skipped"), run(2, "success")], True),       # draft, then ready for review
                ([run(4, "success"), run(3, "failure")], True),       # a failed attempt, then a re-run
                ([run(5, "success"), run(6, "failure")], False),      # a newer run failed
                ([run(7, "success"), run(8, None, "in_progress")], False),
                ([run(9, "success", app=1), run(10, "failure", app=2)], False)):  # another app's check
            with self.subTest(runs=runs):
                self.gh.checks[HEAD] = runs
                self.assertEqual(prog.merge_check(issue, 7)["ready"], ready, self.reasons(issue))

    def test_a_reviewers_verified_required_depth_raises_the_floor(self):
        # Astra A3 finding 6: EFFECTIVE_AUDIT_FLOOR = max(plan floor, VERIFIED_REQUIRED_DEPTH).
        issue, _ = self.released_writer()
        self.reviewed(issue, depth="A2", required="A2")
        self.assertIn("1 of 2 required independent reviews at effective floor A2", self.reasons(issue))
        self.reviewed(issue, slot=2, depth="A2", required="A2")  # the second slot opens
        check = prog.merge_check(issue, 7)
        self.assertTrue(check["ready"], check["reasons"])

    def test_a_verified_a3_requirement_adds_the_architecture_gate(self):
        issue, _ = self.released_writer()
        self.reviewed(issue, depth="A2", required="A3")
        reasons = self.reasons(issue)
        self.assertIn("Astra gate ARCHITECTURE", reasons)
        self.assertIn("effective floor A3", reasons)

    def test_a_revision_change_invalidates_the_old_delivery_and_reviews(self):
        # Astra A3 finding 3: start advanced the plan and envelope, then provider preflight failed.
        issue, _ = self.released_writer()
        self.reviewed(issue)
        self.assertTrue(prog.merge_check(issue, 7)["ready"])
        self.gh.contents[PLAN2] = plan([node(floor="A1", spec="New requirement.")])
        self.gh.compare[(PLAN1, PLAN2)] = "ahead"
        with patch.object(cp, "host_preflight", side_effect=cp.ControlPlaneError("provider preflight failed")):
            with self.assertRaisesRegex(cp.ControlPlaneError, "provider preflight failed"):
                prog.start(issue, "zari", "n1", PLAN2, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(self.host.ledger.materialize_status("zari", "n1")["plan_commit"], PLAN2)
        check = prog.merge_check(issue, 7)
        self.assertFalse(check["ready"])
        self.assertIn("older task revision", " | ".join(check["reasons"]))
        with self.assertRaisesRegex(cp.ControlPlaneError, "older task revision"):
            prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)

    def test_m1_merge_executes_only_a_computed_ready_state_pinned_to_its_head(self):
        issue, _ = self.released_writer()
        not_ready = prog.merge(issue, 7)
        self.assertEqual(not_ready["status"], "NOT_READY")  # no review yet
        self.assertEqual(self.gh.merges, [])
        self.reviewed(issue)
        merged = prog.merge(issue, 7)
        self.assertEqual((merged["status"], merged["head"]), ("MERGED", HEAD))
        self.assertEqual(self.gh.merges, [(7, {"sha": HEAD})])
        done = prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(done["status"], "DONE")

    def test_m1_merge_refuses_a_head_that_moved_after_the_check(self):
        issue, _ = self.released_writer()
        self.reviewed(issue)
        real = self.gh._request
        def moved(method, path, payload=None):
            if method == "PUT":
                self.gh.pulls[7]["head"]["sha"] = "b" * 40  # a push lands between check and merge
            return real(method, path, payload)
        with patch.object(self.gh, "_request", side_effect=moved):
            with self.assertRaisesRegex(cp.ControlPlaneError, "409"):
                prog.merge(issue, 7)
        self.assertFalse(self.gh.pulls[7]["merged"])

    def test_a_merged_task_is_never_redispatched(self):
        # Astra A3 finding 7: the delivered PR merged while the issue stayed open.
        issue, _ = self.released_writer()
        self.gh.pulls[7].update(merged=True, state="closed")
        done = prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(done["status"], "DONE")
        self.assertEqual(self.record(issue)["attempt_id"], 1)

    def test_d1_a_reviewer_escalation_is_a_verdict_never_a_retry(self):
        issue, _ = self.released_writer()
        review = self.launch_review(issue)
        with self.assertRaisesRegex(cp.ControlPlaneError, "escalates with the DECISION_REQUIRED verdict"):
            prog.reap(issue, review["launch_request_id"], self.blocked(issue, review, "DECISION_REQUIRED"))
        prog.reap(issue, review["launch_request_id"], self.post_review(7, review, verdict="DECISION_REQUIRED"))
        self.assertIn("returned DECISION_REQUIRED", self.reasons(issue))
        again = prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)
        self.assertEqual(again["status"], "REVIEW_EXISTS")  # the slot is answered, not re-rolled

    def test_review_retries_are_bounded(self):
        issue, _ = self.released_writer()
        for _ in range(prog.MAX_REVIEW_SESSIONS):
            review = self.launch_review(issue)
            prog.reap(issue, review["launch_request_id"], self.blocked(issue, review, "STALLED"))
        with self.assertRaisesRegex(cp.ControlPlaneError, "REVIEW_RETRIES_EXHAUSTED"):
            prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)

    def test_n2_a_never_admitted_tombstone_does_not_wedge_the_slot(self):
        issue, writer = self.released_writer()
        prepared = prog.prepare_review(issue, 1, self.file("lost.json"), preflight=lambda lane: True)
        lost = json.loads(self.file("lost.json").read_text())["launch_request_id"]  # the launch step never ran
        self.host.ledger.reconcile(lost, None, f"https://github.com/{REPO}/issues/{issue}", no_session=True,
                                   sender_fenced=True, never_admitted=True)
        retry = self.launch_review(issue)
        self.assertEqual((prepared["attempt"], retry["attempt"]), (1, 2))
        self.assertNotEqual(retry["launch_request_id"], lost)

    def test_f3_an_edited_record_cannot_revive_a_released_session(self):
        issue, writer = self.released_writer()
        self.edit_record(issue, lambda record: record.update(launch_state="NOT_STARTED"))
        started = prog.start(issue, "zari", "n1", PLAN1, self.file("packet.json"), preflight=lambda lane: True)
        self.assertEqual(started["status"], "PREPARED")
        cp.launch_dispatch(self.file("packet.json"), self.file("result.json"))
        packet = json.loads(self.file("packet.json").read_text())
        state = cp.finalize_dispatch(issue, self.file("result.json"), packet["launch_request_id"])
        # The same request (and so a new packet) is refused by the host: never a revived CONFIRMED.
        self.assertNotEqual(state, "CONFIRMED")
        self.assertEqual(self.host.ledger.status(writer["launch_request_id"])["state"], "RECONCILED")
        # P3-a: the host has fenced that request, so the next start resumes as a new attempt.
        resumed = self.launch_writer(issue)
        self.assertEqual((resumed["status"], resumed["attempt"], resumed["lane"]), ("PREPARED", 2, "DEVIN"))

    def test_n3_the_plan_advances_only_when_the_envelope_is_rewritten(self):
        issue, _ = self.released_writer()
        self.launch_review(issue)
        self.gh.contents[PLAN2] = plan()
        self.gh.compare[(PLAN1, PLAN2)] = "ahead"
        busy = prog.start(issue, "zari", "n1", PLAN2, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(busy["status"], "TASK_ACTIVE")
        self.assertEqual(self.host.ledger.materialize_status("zari", "n1")["plan_commit"], PLAN1)
        prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)  # body still matches

    def test_reap_ignores_an_edited_record_task_id(self):
        self.gh.contents[PLAN1] = plan([node("n1"), node("n2")])
        first = self.materialized("n1")
        self.launch_writer(first)
        second = self.materialized("n2")
        other = self.launch_writer(second, "n2")
        self.edit_record(first, lambda record: record.update(task_id="ZARI-N2"))
        with self.assertRaisesRegex(cp.ControlPlaneError, "not a host launch of this task"):
            prog.reap(first, other["launch_request_id"], self.blocked(first, other))

    # ------------------------------------------------------------------ reap evidence

    def test_review_evidence_must_be_at_the_reviewed_head_after_the_reservation(self):
        issue, _ = self.released_writer()
        review = self.launch_review(issue)
        cases = [
            (self.post_review(7, review, commit_id="c" * 40), "not submitted at the reviewed head"),
            (self.post_review(7, review, submitted_at="2000-01-01T00:00:00Z"), "predates"),
            (self.post_review(7, review, login="someone-else"), "not from the control actor"),
            (f"https://github.com/{REPO}/pull/8#pullrequestreview-1", "reviewed PR"),
            (self.gh.add_review(7, "looks fine"), "exactly one signed ASTRA_REVIEW_V1"),
        ]
        for evidence, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(cp.ControlPlaneError, expected):
                    prog.reap(issue, review["launch_request_id"], evidence)
        self.assertEqual(self.host.ledger.status(review["launch_request_id"])["state"], "CONFIRMED")

    def test_writer_evidence_must_be_a_fresh_signed_deliverable_on_this_task(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        other = self.gh.new_issue("other", "x")
        unsigned = f"ASTRA_DELIVERY_V1 pr=7 head={HEAD}"
        self.pr(9, ref="feature/other")  # a PR outside the task branch
        cases = [
            (self.deliver(issue, writer, created_at="2000-01-01T00:00:00Z"), "predates"),
            (self.deliver(issue, writer, login="someone-else"), "not from the control actor"),
            (self.deliver(other, writer), "comment URL on this task issue"),
            (f"https://github.com/{REPO}/pull/7#pullrequestreview-1", "comment URL"),
            (self.deliver(issue, writer, body=unsigned), "exactly one signed"),
            (self.deliver(issue, writer, body=signed_delivery(writer["packet"]) + "\n" +
                          signed_delivery(writer["packet"], 8)), "exactly one signed"),
            (self.comment_url(issue, signed_delivery(writer["packet"], 9)), "this task's branch"),
        ]
        for evidence, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(cp.ControlPlaneError, expected):
                    prog.reap(issue, writer["launch_request_id"], evidence)
        self.assertEqual(self.record(issue)["launch_state"], "CONFIRMED")

    def test_blocker_reap_releases_without_a_delivery(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        prog.reap(issue, writer["launch_request_id"], self.blocked(issue, writer, "DECISION_REQUIRED"))
        record = self.record(issue)
        self.assertEqual((record["launch_state"], record.get("delivery")), ("RELEASED", None))
        self.assertEqual(record["last_deliverable"]["kind"], "DECISION_REQUIRED")
        with self.assertRaisesRegex(cp.ControlPlaneError, "host-pinned ASTRA_DELIVERY_V1"):
            prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)

    def test_reap_refuses_a_launch_of_another_task(self):
        self.gh.contents[PLAN1] = plan([node("n1"), node("n2")])
        first = self.materialized("n1")
        writer = self.launch_writer(first)
        second = self.materialized("n2")
        self.launch_writer(second, "n2")
        with self.assertRaisesRegex(cp.ControlPlaneError, "not a host launch of this task"):
            prog.reap(second, writer["launch_request_id"], self.deliver(first, writer))

    def test_review_refuses_when_the_pr_moved_off_the_delivered_head(self):
        issue, _ = self.released_writer()
        self.pr(sha="b" * 40)
        with self.assertRaisesRegex(cp.ControlPlaneError, "deliver again"):
            prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)

    def test_lost_released_projection_is_repaired_from_the_host(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        evidence = self.deliver(issue, writer)
        self.gh.fail_update = True
        with self.assertRaises(cp.ControlPlaneError):
            prog.reap(issue, writer["launch_request_id"], evidence)
        self.gh.fail_update = False
        self.assertEqual(self.record(issue)["launch_state"], "CONFIRMED")  # projection lost
        self.gh.comments_by_issue[issue] = [c for c in self.gh.comments_by_issue[issue]
                                            if not evidence.endswith(f"-{c['id']}")]  # and the comment deleted
        repaired = prog.reap(issue, writer["launch_request_id"], evidence)
        self.assertEqual((repaired["status"], repaired["repeated"]), ("RELEASED", True))
        record = self.record(issue)
        self.assertEqual((record["launch_state"], record["delivery"]["head"]), ("RELEASED", HEAD))

    def test_reap_refuses_while_lane_uid_has_live_processes(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        self.host.live["DEVIN"] = [321]
        with self.assertRaises(cp.ControlPlaneError):
            prog.reap(issue, writer["launch_request_id"], self.deliver(issue, writer))
        self.assertEqual(self.record(issue)["launch_state"], "CONFIRMED")

    # ------------------------------------------------------------------ sessions and lanes

    def test_resume_keeps_owner_lane_and_waits_for_it(self):
        issue, _ = self.released_writer(green=False)
        # Another task now occupies DEVIN: the resume waits instead of switching lanes.
        other = {"schema_version": 2, "role": "WRITER", "owner_lane": "DEVIN", "repository": REPO,
                 "task_id": "OTHER", "task_revision": "r", "builder_id": "DEVIN",
                 "launch_request_id": "9" * 24, "attempt_id": 1}
        self.host.ledger.reserve(other, self.host.policy)
        waiting = prog.start(issue, "zari", "n1", PLAN1, self.file("p2.json"), preflight=lambda lane: True)
        self.assertEqual((waiting["status"], waiting["owner_lane"]), ("NO_IDLE_LANE", "DEVIN"))
        self.host.ledger.finalize(other, {"outcome": "FAILED_PRESTART", "reason": "test"})
        resumed = self.launch_writer(issue)
        self.assertEqual((resumed["lane"], resumed["attempt"]), ("DEVIN", 2))
        record = self.record(issue)
        self.assertEqual(record["previous_attempts"][0]["fenced_by_host_state"], cp.VERIFIED_RELEASE)
        self.assertNotIn("delivery", record)  # a new attempt must deliver again
        self.assertIn("host-pinned delivery", self.reasons(issue))

    def test_owner_lane_comes_from_the_host_not_the_record(self):
        issue, _ = self.released_writer(green=False)
        self.edit_record(issue, lambda record: record.update(owner_lane="GLM"))
        with self.assertRaisesRegex(cp.ControlPlaneError, "owned by lane GLM"):
            # The host says DEVIN; the edited record's own owner check then refuses the mismatch.
            prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda lane: True)

    def test_released_task_is_never_redispatched_under_the_old_request(self):
        issue, writer = self.released_writer()
        envelope = cp.parse_task_envelope(self.gh.issues[issue]["body"])
        expected = {"EXPECTED_TASK_ID": envelope["TASK_ID"], "EXPECTED_TASK_REVISION": envelope["TASK_REVISION"],
                    "EXPECTED_BUILDER_ID": envelope["BUILDER_ID"],
                    "EXPECTED_ISSUE_BODY_SHA256": prog.sha256_text(self.gh.issues[issue]["body"])}
        os.environ.pop("EXPECTED_ATTEMPT_ID", None)
        with self.assertRaisesRegex(cp.ControlPlaneError, "RELEASED task: a resume requires expected_attempt_id=2"):
            cp.prepare_dispatch(issue, self.file("manual.json"), expected)
        self.assertEqual(self.record(issue)["launch_request_id"], writer["launch_request_id"])

    def test_operator_reconciled_session_resumes_on_the_owner_lane(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        self.host.ledger.reconcile(writer["launch_request_id"], "cli:" + writer["launch_request_id"],
                                   f"https://github.com/{REPO}/issues/{issue}")
        resumed = self.launch_writer(issue)
        self.assertEqual((resumed["lane"], resumed["attempt"]), ("DEVIN", 2))
        self.assertEqual(self.record(issue)["previous_attempts"][0]["fenced_by_host_state"], "SESSION_TERMINAL")

    def test_owner_live_blocks_restart_until_verified_release(self):
        issue = self.materialized()
        self.launch_writer(issue)
        again = prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(again["status"], "TASK_ACTIVE")

    def test_live_reviewer_blocks_the_writer_resume(self):
        issue, _ = self.released_writer()
        self.launch_review(issue)
        again = prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(again["status"], "TASK_ACTIVE")

    def test_review_waits_for_verified_writer_release(self):
        issue = self.materialized()
        writer = self.launch_writer(issue)
        self.deliver(issue, writer)
        with self.assertRaisesRegex(cp.ControlPlaneError, "released current writer attempt"):
            prog.prepare_review(issue, 1, self.file("r.json"), preflight=lambda lane: True)

    def test_a2_second_reviewer_excludes_writer_and_first_reviewer(self):
        self.gh.contents[PLAN1] = plan([node(floor="A2")])
        issue, _ = self.released_writer()
        first = self.launch_review(issue, slot=1)
        second = self.launch_review(issue, slot=2)
        self.assertEqual((first["lane"], second["lane"]), ("GROK_BUILD", "GLM"))
        self.assertNotEqual(first["packet"]["review_nonce"], second["packet"]["review_nonce"])
        again = prog.prepare_review(issue, 1, self.file("r1.json"), preflight=lambda lane: True)
        self.assertEqual(again["status"], "REVIEW_EXISTS")
        with self.assertRaises(cp.ControlPlaneError):
            prog.prepare_review(issue, 3, self.file("r3.json"))

    def test_a1_task_takes_no_second_review_slot(self):
        issue, _ = self.released_writer()
        with self.assertRaisesRegex(cp.ControlPlaneError, "does not require review slot 2"):
            prog.prepare_review(issue, 2, self.file("r.json"), preflight=lambda lane: True)

    def test_cursor_is_the_last_lane_in_the_order(self):
        board = {"active_total": 0, "max_active_sessions": 4,
                 "lanes": [{"lane": lane, "enabled": True, "active": []} for lane in prog.LANE_ORDER]}
        self.assertEqual(prog.select_lane(board, self.cfg, exclude={"DEVIN", "GROK_BUILD", "GLM"}), "CURSOR")
        self.assertIsNone(prog.select_lane(board, {**self.cfg, "enabled_builders": ["DEVIN", "GROK_BUILD", "GLM"]},
                                           exclude={"DEVIN", "GROK_BUILD", "GLM"}))

    # ------------------------------------------------------------------ dependencies (F8)

    def test_dependencies_must_be_done_before_start_and_merge(self):
        self.gh.contents[PLAN1] = plan([node("n1"), node("n2", depends_on=["n1"])])
        self.released_writer(node_id="n1")
        second = self.materialized("n2")
        waiting = prog.start(second, "zari", "n2", PLAN1, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual((waiting["status"], waiting["pending"]), ("WAITING_ON_DEPENDENCIES", ["n1"]))
        self.gh.pulls[7].update(merged=True, state="closed")  # n1's delivered head merged
        started = self.launch_writer(second, "n2")
        self.assertEqual(started["status"], "PREPARED")

    def test_a_dependency_merged_at_another_head_is_not_done(self):
        self.gh.contents[PLAN1] = plan([node("n1"), node("n2", depends_on=["n1"])])
        self.released_writer(node_id="n1")
        self.gh.pulls[7].update(merged=True, state="closed",
                                head={"sha": "b" * 40, "ref": "astra/zari-n1", "repo": {"full_name": REPO}})
        second = self.materialized("n2")
        waiting = prog.start(second, "zari", "n2", PLAN1, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(waiting["status"], "WAITING_ON_DEPENDENCIES")

    def test_external_dependency_field_is_rejected_before_materialization_or_start(self):
        for external in ([], [{"repository": "BeautifulMind-JT/kix-protocol", "node": "sdk"}], None):
            with self.subTest(external=external):
                self.gh.contents[PLAN1] = plan([node(depends_on_external=external)])
                with self.assertRaisesRegex(prog.ProgramError, "not supported by active schema"):
                    prog.materialize("zari", "n1", PLAN1)
                self.assertEqual(self.gh.posts, 0)
        self.gh.contents[PLAN1] = plan()
        issue = self.materialized()
        self.gh.contents[PLAN2] = plan([node(depends_on_external=[])])
        self.gh.compare[(PLAN1, PLAN2)] = "ahead"
        with self.assertRaisesRegex(prog.ProgramError, "not supported by active schema"):
            prog.start(issue, "zari", "n1", PLAN2, self.file("packet.json"), preflight=lambda _: True)
        self.assertEqual(self.host.ledger.materialize_status("zari", "n1")["plan_commit"], PLAN1)
        self.assertFalse(self.file("packet.json").exists())

    def kix_post_merge_fixture(self):
        cfg = {**self.cfg, "repository": "BeautifulMind-JT/kix-protocol", "project": "KIX",
               "program_required_checks": ["kernel", "protocol"]}
        merge = "f" * 40
        self.pr(merged=True, state="closed", head={"sha": HEAD, "ref": "astra/zari-n1",
                "repo": {"full_name": cfg["repository"]}})
        self.gh.checks[merge] = [{"name": name, "id": suite, "check_suite": {"id": suite},
                                "status": "completed", "conclusion": "success"}
                               for name, suite in (("kernel", 100), ("protocol", 200))]
        self.gh.workflows = [{"id": suite, "workflow_id": suite, "check_suite_id": suite,
                              "event": "push", "head_branch": "main", "head_sha": merge,
                              "status": "completed", "conclusion": "success", "run_number": 1}
                             for suite in (100, 200)]
        paths = ("runtime/crates/kix-kernel/src/lib.rs", "runtime/crates/kix-kernel/tests/quarantine_capacity.rs")
        cfg["program_post_merge_locked_blobs"] = {}
        for path in paths:
            raw = ("provider-free fixture " + path).encode()
            blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            cfg["program_post_merge_locked_blobs"][path] = blob
            self.gh.file_contents[(path, merge)] = {"type": "file", "encoding": "base64", "sha": blob,
                                                   "content": base64.b64encode(raw).decode()}
        return cfg, {"kind": "DELIVERY", "pr": 7, "head": HEAD}, merge

    def test_kix_completion_needs_exact_merge_push_checks_and_locked_blobs(self):
        cfg, pin, merge = self.kix_post_merge_fixture()
        self.assertEqual(prog.delivery_completion(self.gh, cfg, pin, "ZARI-N1")["status"], "DONE")
        self.gh.checks[merge][1]["conclusion"] = "failure"
        self.assertEqual(prog.delivery_completion(self.gh, cfg, pin, "ZARI-N1")["status"], "POST_MERGE_FAILED")
        self.gh.checks[merge][1]["conclusion"] = "success"
        self.gh.workflows[1]["event"] = "pull_request"
        result = prog.delivery_completion(self.gh, cfg, pin, "ZARI-N1")
        self.assertEqual(result["status"], "MERGED_POST_VERIFY")
        self.assertIn("main/push", " ".join(result["reasons"]))

    def test_kix_post_merge_missing_stale_skipped_and_running_evidence_holds(self):
        for mode in ("missing-baseline", "old-ci-sha", "skipped", "running", "wrong-suite", "rerun-running"):
            cfg, pin, merge = self.kix_post_merge_fixture()
            if mode == "missing-baseline": cfg.pop("program_post_merge_locked_blobs")
            elif mode == "old-ci-sha": self.gh.checks[HEAD] = self.gh.checks.pop(merge)
            elif mode == "skipped": self.gh.checks[merge][1]["conclusion"] = "skipped"
            elif mode == "running": self.gh.workflows[1]["status"] = "in_progress"
            elif mode == "wrong-suite": self.gh.workflows[1]["check_suite_id"] = 999
            else: self.gh.workflows.append({**self.gh.workflows[1], "run_attempt": 2, "status": "in_progress"})
            with self.subTest(mode=mode):
                self.assertEqual(prog.delivery_completion(self.gh, cfg, pin, "ZARI-N1")["status"],
                                 "MERGED_POST_VERIFY")

    def test_kix_locked_blob_changed_or_forged_metadata_is_post_merge_failure(self):
        for mode in ("sha-metadata", "content"):
            cfg, pin, merge = self.kix_post_merge_fixture()
            path = next(iter(cfg["program_post_merge_locked_blobs"]))
            value = self.gh.file_contents[(path, merge)]
            value["sha" if mode == "sha-metadata" else "content"] = "b" * 40 if mode == "sha-metadata" \
                else base64.b64encode(b"changed locked bytes").decode()
            with self.subTest(mode=mode):
                self.assertEqual(prog.delivery_completion(self.gh, cfg, pin, "ZARI-N1")["status"], "POST_MERGE_FAILED")

    def test_merged_delivery_wrong_target_or_unknown_lineage_never_done_or_redispatched(self):
        issue, _ = self.released_writer()
        for mode in ("wrong-target", "off-main", "missing-merge-sha"):
            self.gh.pulls[7].update(merged=True, state="closed", base={"ref": "main"}, merge_commit_sha="f" * 40)
            self.gh.unmerged.clear()
            if mode == "wrong-target": self.gh.pulls[7]["base"]["ref"] = "other"
            elif mode == "off-main": self.gh.unmerged.add("f" * 40)
            else: self.gh.pulls[7]["merge_commit_sha"] = None
            with self.subTest(mode=mode):
                result = prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda _: True)
                self.assertEqual(result["status"], "MERGED_POST_VERIFY")
                self.assertEqual(self.record(issue)["attempt_id"], 1)

    def test_kix_failed_post_merge_blocks_successor_even_if_comment_claims_done(self):
        cfg, pin, merge = self.kix_post_merge_fixture()
        self.gh.checks[merge][0]["conclusion"] = "failure"
        self.gh.create_comment(31, "POST_MERGE_FAILED recorded; corrective issue #99; DONE")
        row = {"role": "WRITER", "state": "RECONCILED", "resolution": cp.VERIFIED_RELEASE, "pin": pin}
        with patch.object(prog, "host", return_value={"status": "CREATED", "repository": cfg["repository"]}), \
                patch.object(prog, "task_rows", return_value=[row]):
            self.assertEqual(prog.pending_dependencies(self.gh, cfg, plan([node("n1"), node("n2", depends_on=["n1"])]),
                                                       node("n2", depends_on=["n1"])), ["n1"])

    # ------------------------------------------------------------------ materialize and plan

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

    def test_recovery_ignores_issues_from_other_authors(self):
        self.gh.fail_post = "after"
        prog.materialize("zari", "n1", PLAN1)
        (number,) = self.gh.issues
        self.gh.issues[number]["user"] = {"login": "someone-else"}
        self.gh.fail_post = None
        self.assertEqual(prog.materialize("zari", "n1", PLAN1)["status"], "MATERIALIZE_UNKNOWN")

    def test_missing_label_is_repaired_after_create(self):
        self.gh.drop_labels = True
        issue = self.materialized()
        self.assertEqual(self.gh.label_posts, 1)
        self.assertIn({"name": "aiops-task"}, self.gh.issues[issue]["labels"])

    def test_duplicate_open_issues_stop_dispatch(self):
        issue = self.materialized()
        self.gh.new_issue("dup", self.gh.issues[issue]["body"])
        with self.assertRaisesRegex(cp.ControlPlaneError, "DUPLICATE_TASK"):
            prog.materialize("zari", "n1", PLAN1)
        with self.assertRaisesRegex(cp.ControlPlaneError, "DUPLICATE_TASK"):
            prog.start(issue, "zari", "n1", PLAN1, self.file("p.json"), preflight=lambda lane: True)

    def test_plan_must_be_on_the_default_branch(self):
        self.gh.unmerged.add(PLAN1)
        with self.assertRaisesRegex(cp.ControlPlaneError, "PLAN_NOT_MERGED"):
            prog.materialize("zari", "n1", PLAN1)
        self.gh.unmerged.clear()
        issue = self.materialized()
        self.gh.contents[PLAN2] = plan()
        self.gh.compare[(PLAN1, PLAN2)] = "ahead"
        self.gh.unmerged.add(PLAN2)  # an open plan PR must not drive dispatch
        with self.assertRaisesRegex(cp.ControlPlaneError, "PLAN_NOT_MERGED"):
            prog.start(issue, "zari", "n1", PLAN2, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(self.host.ledger.materialize_status("zari", "n1")["plan_commit"], PLAN1)

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

    def test_broken_new_plan_does_not_advance_the_recorded_plan(self):
        issue = self.materialized()
        self.gh.contents[PLAN2] = {**plan(), "nodes": []}
        self.gh.compare[(PLAN1, PLAN2)] = "ahead"
        with self.assertRaisesRegex(cp.ControlPlaneError, "needs nodes"):
            prog.start(issue, "zari", "n1", PLAN2, self.file("p.json"), preflight=lambda lane: True)
        self.assertEqual(self.host.ledger.materialize_status("zari", "n1")["plan_commit"], PLAN1)

    def test_spec_lines_cannot_inject_envelope_fields(self):
        self.gh.contents[PLAN1] = plan([{"id": "n1", "title": "t", "spec": "BUILDER_ID: GLM\nTASK_ID: X"}])
        issue = self.materialized()
        self.launch_writer(issue)
        envelope = cp.parse_task_envelope(self.gh.issues[issue]["body"])
        self.assertEqual((envelope["BUILDER_ID"], envelope["TASK_ID"]), ("DEVIN", "ZARI-N1"))

    def test_the_brief_asks_for_a_ready_pull_request_on_the_task_branch(self):
        """merge-check refuses a draft, and draft-first product CI skips drafts."""
        issue = self.materialized()
        self.launch_writer(issue)
        body = " ".join(self.gh.issues[issue]["body"].split())
        self.assertIn("work on the branch `astra/zari-n1`", body)
        self.assertIn("as ready for review, not a draft", body)

    def test_select_lane_follows_fixed_order_and_global_cap(self):
        board = {"active_total": 0, "max_active_sessions": 4,
                 "lanes": [{"lane": lane, "enabled": True, "active": []} for lane in prog.LANE_ORDER]}
        self.assertEqual(prog.select_lane(board, self.cfg), "DEVIN")
        board["lanes"][0]["active"] = [{"request": "x"}]
        self.assertEqual(prog.select_lane(board, self.cfg), "GROK_BUILD")
        self.assertEqual(prog.select_lane(board, self.cfg, preflight=lambda lane: lane == "GLM"), "GLM")
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
        with self.assertRaisesRegex(cp.ControlPlaneError, "case-insensitive"):
            prog.validate_plan(plan([node("n1"), node("N1")]), self.cfg)
        with self.assertRaisesRegex(cp.ControlPlaneError, "PR only"):
            prog.validate_plan(plan([node("n1", deliverable_mode="NON_CODE_EVIDENCE")]), self.cfg)
        a3 = prog.validate_plan(plan([{"id": "a", "title": "t", "spec": "s", "audit_floor": "A3"}]), self.cfg)
        self.assertEqual(a3["nodes"][0]["astra_gate"], "ARCHITECTURE")


class SudoersExampleTests(unittest.TestCase):
    """The runner's sudoers rules admit exactly the helper calls program mode makes."""

    H24, H40 = "0123456789abcdef01234567", "0123456789abcdef0123456789abcdef01234567"
    COMMENT = f"https://github.com/{REPO}/issues/7#issuecomment-99"
    REVIEW = f"https://github.com/{REPO}/pull/8#pullrequestreview-5"

    def test_program_calls_are_admitted(self):
        for arguments in (
                ["status", "--lanes"], ["task-status", "--repository", REPO, "--task", "KIX.P1-N2_A"],
                ["reap", "--launch-request-id", self.H24, "--evidence", self.COMMENT, "--pin-stdin"],
                ["reap", "--launch-request-id", self.H24, "--evidence", self.REVIEW, "--pin-stdin"],
                ["materialize-begin", "--program", "zari", "--node", "n1", "--repository", REPO,
                 "--plan-commit", self.H40],
                ["materialize-finish", "--program", "zari", "--node", "n1", "--request", self.H24,
                 "--outcome", "CREATED", "--issue", "12"],
                ["materialize-finish", "--program", "zari", "--node", "n1", "--request", self.H24,
                 "--outcome", "UNKNOWN"],
                ["materialize-status", "--program", "zari", "--node", "n1"],
                ["materialize-plan", "--program", "zari", "--node", "n1", "--from", self.H40, "--to", self.H40],
                ["preflight", "--builder-id", "CURSOR"]):
            with self.subTest(arguments=arguments):
                self.assertTrue(sudoers_allows(arguments, sudoers_runner_rules(SUDOERS.read_text())))

    def test_operator_commands_and_loose_forms_are_refused(self):
        for arguments in (
                ["migrate", "--to", "2"], ["init"],
                ["reconcile", "--launch-request-id", self.H24, "--evidence", self.COMMENT, "--no-session"],
                ["materialize-resolve", "--program", "zari", "--node", "n1", "--request", self.H24,
                 "--evidence", self.COMMENT, "--not-created"],
                ["reap", "--launch-request-id", self.H24, "--evidence", self.COMMENT],  # unsigned form
                ["reap", "--launch-request-id", self.H24, "--evidence", self.COMMENT, "--pin-stdin", "--x"],
                ["reap", "--launch-request-id", self.H24.upper(), "--evidence", self.COMMENT, "--pin-stdin"],
                ["reap", "--launch-request-id", self.H24, "--evidence",
                 "https://evil.test/o/r/issues/1#issuecomment-1", "--pin-stdin"],
                ["reap", "--launch-request-id", self.H24, "--evidence",
                 self.COMMENT.replace("issues/7", "pull/7"), "--pin-stdin"],
                ["status", "--lanes", "--launch-request-id", self.H24], ["status", "--lanes;id"],
                ["task-status", f"--repository={REPO}", "--task", "ZARI-N1"],
                ["task-status", "--repository", REPO, "--task", "zari-n1"],
                ["materialize-finish", "--program", "zari", "--node", "n1", "--request", self.H24,
                 "--outcome", "CREATED"],
                ["materialize-begin", "--program", "-x", "--node", "n1", "--repository", REPO,
                 "--plan-commit", self.H40],
                ["preflight", "--builder-id", "cursor"], ["launch", "--packet", "/tmp/x"]):
            with self.subTest(arguments=arguments):
                self.assertFalse(sudoers_allows(arguments))

    def test_the_parser_reads_escapes_like_sudo(self):
        rules = sudoers_runner_rules(SUDOERS.read_text())
        self.assertTrue(any("https://github[.]com/" in rule and "#issuecomment-" in rule for rule in rules))
        with self.assertRaisesRegex(AssertionError, "unescaped"):
            sudoers_runner_rules("Cmnd_Alias AIOPS_PROGRAM_HOST = \\\n"
                                 "    /opt/astra/bin/astra-host-control ^reap --evidence https://x$\n\n")


if __name__ == "__main__":
    unittest.main()
