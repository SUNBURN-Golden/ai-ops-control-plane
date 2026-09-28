"""Offline independent-host tests: real local SQLite, serialized fake GitHub refs.
No Work send, provider launch, remote VM or live credentials.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import control_plane_local_host as local
import control_plane_ownership as own
import test_control_plane_diagnostic as diagnostic_fixture


class GitApi:
    """GitHub create-once + fast-forward enforcement, including lost responses."""
    def __init__(self, fallback=None):
        self.lock = threading.RLock()
        self.refs, self.commits = {}, {}
        self.calls = []
        self.fallback = fallback
        self.fail_after = None

    def call(self, method, path, body=None, **kwargs):
        with self.lock:
            self.calls.append((method, path))
            prefix = f"repos/{own.REPOSITORY}/git/"
            if not path.startswith(prefix):
                return self.fallback(method, path, body, **kwargs)
            suffix = path[len(prefix):]
            if suffix == "ref/heads/main":
                return self.fallback(method, path, body, **kwargs)
            if method == "POST" and suffix == "trees":
                assert body == {"tree": []}
                return {"sha": own.EMPTY_TREE}
            if method == "POST" and suffix == "commits":
                head = hashlib.sha1(own.canonical(body).encode()).hexdigest()
                self.commits[head] = dict(sha=head, tree={"sha": body["tree"]}, message=body["message"],
                                          parents=body["parents"])
                return {"sha": head}
            if method == "GET" and suffix.startswith("commits/"):
                return copy.deepcopy(self.commits[suffix[8:]])
            if method == "GET" and suffix.startswith("ref/"):
                ref = "refs/" + suffix[4:]
                if ref not in self.refs:
                    raise HTTPError(path, 404, "missing", {}, None)
            elif method == "POST" and suffix == "refs":
                ref = body["ref"]
                if ref in self.refs:
                    raise HTTPError(path, 422, "exists", {}, None)
                self.refs[ref] = body["sha"]
            elif method == "PATCH" and suffix.startswith("refs/"):
                ref = "refs/" + suffix[5:]
                assert body["force"] is False
                if self.commits[body["sha"]]["parents"] != [self.refs[ref]]:
                    raise HTTPError(path, 422, "not fast forward", {}, None)
                self.refs[ref] = body["sha"]
            else:
                return self.fallback(method, path, body, **kwargs)
            if self.fail_after == method and method in {"POST", "PATCH"}:
                raise TimeoutError("response lost after effect")
            return dict(ref=ref, object={"sha": self.refs[ref], "type": "commit"})


def assignment(host="mac", instance="11111111-1111-1111-1111-111111111111", issue=28):
    return dict(active=True, repository=own.REPOSITORY, issue=issue, task_id="HOST-TEST",
                host_id=host, instance_id=instance, admission="FRESH_TASK", operations=["WORK_DIAGNOSTIC"],
                github_issue_id=123, github_issue_created_at="2026-09-28T05:00:00Z",
                pointer=f"https://github.com/{own.REPOSITORY}/issues/{issue}#issuecomment-8")


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.api = GitApi()
        self.registry = own.Registry(self.api)
        self.assignment = assignment()

    def test_two_hosts_only_one_owner(self):
        candidates = [self.assignment, assignment("grok", "22222222-2222-2222-2222-222222222222")]
        def acquire(value):
            try: self.registry.acquire(value); return True
            except own.OwnershipError: return False
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(acquire, candidates))
        self.assertEqual(sum(result), 1)
        self.assertEqual(len(self.api.refs), 1)

    def test_revision_and_host_are_not_part_of_task_key(self):
        self.registry.acquire(self.assignment)
        for key, value in (("task_id", "RENAMED"), ("host_id", "other"), ("revision", "r2")):
            with self.assertRaises(own.OwnershipError): self.registry.acquire(dict(self.assignment, **{key:value}))
        self.assertEqual(own.task_key(own.REPOSITORY, 28), own.task_key(own.REPOSITORY.lower().replace("beautifulmind-jt", "BeautifulMind-JT"), 28))

    def test_same_owner_concurrent_identical_start_grants_once(self):
        self.registry.acquire(self.assignment)
        barrier = threading.Barrier(2)
        original = self.registry.write
        def race(*args):
            barrier.wait(timeout=2)
            return original(*args)
        self.registry.write = race
        def begin(_):
            try: return self.registry.begin(self.assignment, "a"*64, "BUILDER", {"p":1})["start_allowed"]
            except own.Uncertain: return False
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(begin, range(2)))
        self.assertEqual(sum(results), 1)

    def test_unknown_blocks_different_action_and_changed_head(self):
        self.registry.acquire(self.assignment)
        self.api.fail_after = "PATCH"
        with self.assertRaises(own.Uncertain): self.registry.begin(self.assignment, "a"*64, "BUILDER", {"head":"old"})
        self.api.fail_after = None
        self.assertFalse(self.registry.begin(self.assignment, "a"*64, "BUILDER", {"head":"old"})["start_allowed"])
        with self.assertRaises(own.OwnershipError): self.registry.begin(self.assignment, "b"*64, "BUILDER", {"head":"new"})

    def test_lost_owner_create_does_not_grant_execution(self):
        self.api.fail_after = "POST"
        with self.assertRaises(own.Uncertain): self.registry.acquire(self.assignment)
        self.assertEqual(len(self.api.refs), 1)

    def test_two_different_tasks_do_not_wait_for_old_host(self):
        self.registry.acquire(self.assignment)
        self.registry.begin(self.assignment, "a"*64, "BUILDER", {})
        second = assignment("other", "22222222-2222-2222-2222-222222222222", issue=29)
        self.registry.acquire(second)
        self.assertTrue(self.registry.begin(second, "b"*64, "BUILDER", {})["start_allowed"])

    def test_finished_retains_tombstone_and_permits_new_action(self):
        self.registry.acquire(self.assignment)
        self.registry.begin(self.assignment, "a"*64, "BUILDER", {})
        evidence = dict(sender_fenced=True, session_ended=True, publication_resolved=True,
                        pointer=self.assignment["pointer"])
        self.registry.finish(self.assignment, "a"*64, evidence)
        self.assertFalse(self.registry.begin(self.assignment, "a"*64, "BUILDER", {})["start_allowed"])
        self.assertTrue(self.registry.begin(self.assignment, "b"*64, "REVIEW", {})["start_allowed"])
        self.assertFalse(any(method == "DELETE" for method, _ in self.api.calls))

    def test_missing_terminal_evidence_cannot_clear_fence(self):
        self.registry.acquire(self.assignment)
        self.registry.begin(self.assignment, "a"*64, "BUILDER", {})
        with self.assertRaises(own.OwnershipError): self.registry.finish(self.assignment, "a"*64, {"session_ended":True})

    def test_legacy_or_handoff_is_not_fresh(self):
        for admission in ("HANDOFF", "LEGACY", None):
            with self.assertRaises(own.OwnershipError): self.registry.acquire(dict(self.assignment, admission=admission))

    def test_wrong_ref_or_control_tree_rejected(self):
        self.registry.acquire(self.assignment)
        head = next(iter(self.api.refs.values()))
        self.api.commits[head]["tree"]["sha"] = "a"*40
        with self.assertRaises(own.OwnershipError): self.registry.owned(self.assignment)


class IndependentHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.root = self.parent / "host"
        self.install = local.initialize(self.root, "mac")
        self.policy = json.loads((self.root / "policy.json").read_text())
        self.fixture = diagnostic_fixture.DiagnosticTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.assignment = assignment(instance=self.policy["instance_id"], issue=19)
        self.assignment["task_id"] = "CP-LOCAL-001"
        self.assignment_body = "<!-- ASTRA_HOST_ASSIGNMENT_V1 -->\n" + local.flow.canonical(self.assignment)
        self.binding = dict(repository=own.REPOSITORY, issue=19, comment_id=8, actor="user",
                            sha256=hashlib.sha256(self.assignment_body.encode()).hexdigest())
        self.policy.update(enabled=True, source_review="PASS", source_review_pointer=self.fixture.review,
                           user_actors=["user"], projection_actor="mechanical", assignment_binding=self.binding)
        self.policy["diagnostic"] = copy.deepcopy(self.fixture.policy["diagnostic"])
        self.policy["diagnostic"]["authorization_expires_at"] = int(time.time()) + 3600
        self.policy["relay"] = dict(self.fixture.relay_policy, authority_mode="IN_PROCESS_HOST", claim_url=None, state_directory=str(self.root/"relay"),
                                    browser_qualified=True, browser_evidence=self.fixture.review)
        def fallback(method, path, body=None, **kwargs):
            if path.endswith("issues/comments/8"):
                return self.fixture.comment(8, self.assignment_body, "user")
            if path.endswith("issues/19"):
                return dict(state="open", id=123, created_at="2026-09-28T05:00:00Z")
            return self.fixture.api_call(method, path, body, **kwargs)
        self.api = GitApi(fallback)
        self.host = local.Host(self.policy, self.api)
        self.sends = 0

    def send(self, p, action, session, root):
        self.sends += 1
        self.assertEqual(p["authority_mode"], "IN_PROCESS_HOST")
        self.assertFalse(p.get("claim_url"))
        return dict(request_id=action["request_id"], work_session=session, observation="ANSWER",
                    diagnostic_result=self.fixture.answer())

    def test_fresh_work_without_old_host_sudo_http_or_hmac(self):
        with patch.object(local.relay, "transport_post", side_effect=AssertionError("old gateway prohibited")):
            result = self.host.work(send_fn=self.send)
        self.assertEqual(result["state"], "DIAGNOSTIC_RECORDED")
        self.assertEqual(self.sends, 1)
        self.assertEqual(len(self.fixture.posts), 2)
        self.assertFalse(self.policy["relay"]["live_acceptance"] == "PASS")
        again = self.host.work(send_fn=self.send)
        self.assertFalse(again["start_allowed"])
        self.assertEqual(self.sends, 1)

    def test_direct_work_succeeds_without_slack(self):
        original = self.api.fallback
        def offline_slack(method, path, body=None, **kwargs):
            if kwargs.get("slack"):
                raise AssertionError("Slack is not a dependency")
            return original(method, path, body, **kwargs)
        self.api.fallback = offline_slack
        self.assertEqual(self.host.work(send_fn=self.send)["state"], "DIAGNOSTIC_RECORDED")
        self.assertEqual(self.sends, 1)

    def test_fresh_diagnostic_issue_not_legacy_issue_19(self):
        issue=123
        pointer=f"https://github.com/{own.REPOSITORY}/issues/{issue}"
        action=local.flow.diagnostic_request("new-r1", self.fixture.current_sha, "diagnostic-consumer",
            self.fixture.action["work_session"], pointer+"#issuecomment-1", issue=issue, task_id="FRESH-MAC")
        self.assignment.update(issue=issue, task_id="FRESH-MAC", pointer=pointer+"#issuecomment-8")
        self.assignment_body="<!-- ASTRA_HOST_ASSIGNMENT_V1 -->\n"+local.flow.canonical(self.assignment)
        self.binding.update(issue=issue,sha256=hashlib.sha256(self.assignment_body.encode()).hexdigest())
        self.fixture.action=action
        self.fixture.authority_body="<!-- ASTRA_DIAGNOSTIC_AUTHORIZATION_V1 -->\n"+local.flow.canonical(
            dict(active=True,request=action,source_review_pointer=self.fixture.review))
        self.policy["diagnostic"]["request"]=action
        self.policy["diagnostic"]["authorization"]["sha256"]=hashlib.sha256(self.fixture.authority_body.encode()).hexdigest()
        self.policy["relay"]["diagnostic_request"]=action
        old=self.api.fallback
        def routed(method,path,body=None,**kwargs):
            value=old(method,path.replace("issues/123", "issues/19"),body,**kwargs)
            if isinstance(value,dict):
                for key in ("issue_url","html_url"):
                    if key in value: value[key]=value[key].replace("issues/19", "issues/123")
            return value
        self.api.fallback=routed
        result=self.host.work(send_fn=self.send)
        self.assertEqual(result["state"],"DIAGNOSTIC_RECORDED")
        self.assertTrue(any(path.endswith("issues/123/comments") for method,path in self.api.calls if method=="POST"))
        self.assertFalse(any("issues/19" in path for _,path in self.api.calls))

    def test_known_ownership_rollback_and_deletion_rejected(self):
        self.host.work(send_fn=self.send)
        ref = next(iter(self.api.refs))
        current = self.api.refs[ref]
        self.api.refs[ref] = self.api.commits[current]["parents"][0]
        with self.assertRaises(Exception): self.host.shared(self.assignment)
        del self.api.refs[ref]
        with self.assertRaises(Exception): self.host.shared(self.assignment, acquire=True)
        self.assertEqual(len(self.api.refs), 0)

    def finish_evidence(self, action_id, *, prestart=False):
        proof = dict(active=True, host_id=self.policy["host_id"], instance_id=self.policy["instance_id"],
                     action_id=action_id, sender_fenced=True, session_ended=True, publication_resolved=True,
                     failed_prestart=prestart, downstream_never_started=prestart)
        body = "<!-- ASTRA_HOST_TERMINAL_V1 -->\n" + local.flow.canonical(proof)
        binding = dict(self.binding, comment_id=9, sha256=hashlib.sha256(body.encode()).hexdigest())
        old = self.api.fallback
        def api(method, path, data=None, **kwargs):
            if path.endswith("issues/comments/9"):
                return self.fixture.comment(9, body, "user")
            if path.endswith("issues/19"):
                return dict(state="closed", id=123, created_at="2026-09-28T05:00:00Z")
            return old(method, path, data, **kwargs)
        self.api.fallback = api
        return binding

    def test_lost_grant_can_reconcile_before_claim_even_after_issue_closed(self):
        self.api.fail_after = "PATCH"
        with self.assertRaises(Exception): self.host.work(send_fn=self.send)
        self.api.fail_after = None
        request = self.fixture.action["request_id"]
        binding = self.finish_evidence(request, prestart=True)
        self.assertEqual(self.host.finish(binding)["state"], "FINISHED")
        self.assertEqual(self.host.registry.owned(self.assignment)[1]["actions"][request]["state"], "FAILED_PRESTART")
        self.assertEqual(self.sends, 0)

    def test_lost_owner_receipt_can_record_prestart_tombstone(self):
        self.api.fail_after = "POST"
        with self.assertRaises(Exception): self.host.work(send_fn=self.send)
        self.api.fail_after = None
        request = self.fixture.action["request_id"]
        self.assertEqual(self.host.finish(self.finish_evidence(request, prestart=True))["state"], "FINISHED")
        self.assertEqual(self.host.registry.owned(self.assignment)[1]["actions"][request]["state"], "FAILED_PRESTART")

    def test_unclaimed_finish_without_never_started_evidence_rejected(self):
        self.api.fail_after = "PATCH"
        with self.assertRaises(Exception): self.host.work(send_fn=self.send)
        self.api.fail_after = None
        with self.assertRaises(Exception):
            self.host.finish(self.finish_evidence(self.fixture.action["request_id"]))

    def test_qualified_local_adapter_no_vm_and_duplicate_not_relaunched(self):
        self.assignment["operations"] = ["BUILDER", "REVIEW"]
        executable = self.root/"bin"/"adapter"
        executable.write_text("#!/bin/sh\nexit 2\n")
        executable.chmod(0o700)
        self.policy["adapters"]["DEVIN"] = dict(qualified=True, evidence_pointer=self.fixture.review,
            host_id=self.policy["host_id"], instance_id=self.policy["instance_id"], operations=["BUILDER"],
            execution_mode="PERSISTENT_SUPERVISOR", executable=str(executable),
            sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        packet = dict(builder_id="DEVIN", operation="BUILDER", repository=own.REPOSITORY, issue=19,
                      task_id=self.assignment["task_id"], host_id=self.policy["host_id"],
                      instance_id=self.policy["instance_id"], revision="r1", head=self.fixture.action["subject"]["head"])
        calls=[]
        def run(command, **kwargs):
            calls.append(command)
            self.assertEqual(set(kwargs["env"]), {"PATH", "LANG"})
            value=json.loads(kwargs["input"])
            self.assertEqual(Path(value["worktree_root"]).parent,self.root/"workspaces")
            return subprocess.CompletedProcess(command,0,json.dumps(dict(outcome="CONFIRMED",
                request_id=value["request_id"],session_id="mock-provider-session")),"")
        with patch.object(self.host,"assignment",return_value=self.assignment), patch.object(self.host,"record",return_value=({"packet":packet}, self.assignment["pointer"])), patch.object(local.subprocess,"run",side_effect=run):
            self.assertEqual(self.host.run_adapter(self.binding)["outcome"],"CONFIRMED")
            self.assertFalse(self.host.run_adapter(self.binding)["start_allowed"])
            packet["head"]="f"*40
            with self.assertRaises(Exception): self.host.run_adapter(self.binding)
        self.assertEqual(len(calls),1)

    def test_install_disabled_user_owned_and_no_original_ledger(self):
        self.assertFalse(json.loads((self.root / "policy.json").read_text())["enabled"])
        run = subprocess.run([sys.executable, str(self.root/"bin/control_plane_local_host.py"), "check",
                              "--policy", str(self.root/"policy.json")], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
        with self.assertRaises(FileExistsError): local.initialize(self.root, "other")

    def test_missing_browser_qualification_blocks_before_claim(self):
        self.policy["relay"]["browser_qualified"] = False
        with self.assertRaises(local.flow.FlowError): self.host.work(send_fn=self.send)
        self.assertEqual(self.api.refs, {})
        self.assertEqual(self.sends, 0)

    def test_shared_response_loss_fences_local_and_no_work_send(self):
        self.api.fail_after = "PATCH"
        with self.assertRaises(Exception): self.host.work(send_fn=self.send)
        result = self.host.work(send_fn=self.send)
        self.assertFalse(result["start_allowed"])
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(self.sends, 0)

    def test_deleted_local_database_not_recreated(self):
        self.host.work(send_fn=self.send)
        (self.root/"control/flow.sqlite").unlink()
        with self.assertRaises(Exception): local.Host(self.policy, self.api)
        self.assertFalse((self.root/"control/flow.sqlite").exists())

    def test_new_local_partition_cannot_adopt_previous_instance(self):
        self.host.work(send_fn=self.send)
        bad = dict(self.policy, instance_id="22222222-2222-2222-2222-222222222222")
        with self.assertRaises(local.flow.FlowError): local.Host(bad, self.api)

    def test_waiting_retains_slot_and_collection_does_not_send(self):
        def waiting(p, action, session, root):
            self.sends += 1
            return dict(request_id=action["request_id"], work_session=session, observation="WAITING", diagnostic_result=None)
        result = self.host.work(send_fn=waiting)
        self.assertEqual(result["state"], "WAITING")
        reads = []
        def observe(p, action, session, root):
            reads.append(True)
            self.assertTrue(p["observe_only"])
            return dict(request_id=action["request_id"], work_session=session, observation="ANSWER", diagnostic_result=self.fixture.answer())
        self.assertEqual(self.host.work(collect=True, send_fn=observe)["state"], "DIAGNOSTIC_RECORDED")
        self.assertEqual((len(reads),self.sends), (1,1))
        with self.assertRaises(local.flow.FlowError): self.host.work(collect=True, send_fn=observe)

    def test_relay_standalone_cannot_bypass_host_ownership(self):
        p = dict(self.policy["relay"], authority_mode="IN_PROCESS_HOST")
        with self.assertRaises(local.relay.RelayError): local.relay.deliver(p, self.fixture.action["request_id"], b"s"*32)

    def test_model_child_has_no_control_tokens(self):
        self.assertIn('"PATH"', Path(local.relay.__file__).read_text())
        root = self.parent/"receipt";root.mkdir()
        def run(command, **kwargs):
            self.assertEqual(set(kwargs["env"]), {"PATH","HOME","CODEX_HOME","LANG"})
            self.assertNotIn("ASTRA_HOST_GITHUB_TOKEN",kwargs["input"])
            (root/"receipt.json").write_text(json.dumps(self.send(self.policy["relay"],self.fixture.action,self.fixture.action["work_session"],root)))
            return subprocess.CompletedProcess(command,0)
        with patch.object(local.relay.subprocess,"run",side_effect=run):
            local.relay.run_codex(self.policy["relay"],self.fixture.action,self.fixture.action["work_session"],root)


if __name__ == "__main__":
    unittest.main()
