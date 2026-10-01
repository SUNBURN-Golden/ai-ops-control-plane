"""Tests for the program inspector facts (control_plane_inspect_facts, contract §3.4).

The real GitHubReader and HostReader run against a fake GitHub transport (an in-memory world
with ETags, pagination and fault injection) and a fake host runner. State lives in a temporary
directory. No network, no sudo, no real /etc or /var.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
import unittest.mock
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import control_plane as cp
import control_plane_inspect_core as core
import control_plane_inspect_facts as facts_mod
import control_plane_inspect_github as gh
import control_plane_inspect_host as hostmod

NOW = datetime(2026, 10, 1, 5, 17, tzinfo=timezone.utc)
CTRL = "BeautifulMind-JT/ai-ops-control-plane"
ZARI = "BeautifulMind-JT/ZARI"
KIXP = "BeautifulMind-JT/kix-protocol"
TOKEN = "github_pat_" + "A" * 40
LEAK = "ghp_" + "Z" * 36


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:40]


def req(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def b64(obj: Any) -> Dict[str, Any]:
    data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()   # bytes = a raw file body
    return {"encoding": "base64", "content": base64.b64encode(data).decode()}


def config(**over: Any) -> Dict[str, Any]:
    cfg = {"schema": "AIOPS_INSPECT_CONFIG_V1", "stage": "DRY", "control_repository": CTRL,
           "targets": [{"repository": ZARI, "prefix": "ZARI"}, {"repository": KIXP, "prefix": "KIXP"}],
           "daily_hour_kst": 9, "tick_minute": 17, "thresholds": {}}
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------------------- fake world

def plan_doc(repo: str, project: str, nodes: List[Dict[str, Any]], program: str = "zari") -> Dict[str, Any]:
    return {"schema_version": 1, "program": program, "repository": repo, "project": project,
            "approval_pointer": "https://github.com/x/y/issues/1", "authoritative_doc_pointers": "docs/A.md",
            "nodes": nodes}


def node(nid: str, deps: Optional[List[str]] = None, title: str = "Title") -> Dict[str, Any]:
    return {"id": nid, "title": title, "spec": "spec text", "depends_on": deps or []}


def writer_row(task: str, *, state: str = "RECONCILED", pin: Optional[Dict[str, Any]] = None,
               lane: str = "DEVIN", seed: str = "w", created: float = 1759200000.0) -> Dict[str, Any]:
    row = {"launch_request_id": req(task + seed), "state": state, "role": "WRITER", "lane": lane,
           "repository": ZARI, "task": task, "attempt_id": 1, "task_revision": "p-x", "owner_lane": lane,
           "reserved_at": iso(datetime.fromtimestamp(created, tz=timezone.utc)), "reserved_ts": created}
    if state == "RECONCILED":
        row.update(resolution=cp.VERIFIED_RELEASE, pin=pin, released_ts=created + 3600,
                   released_at=iso(datetime.fromtimestamp(created + 3600, tz=timezone.utc)))
    return row


class World:
    """In-memory GitHub + host with ETags, pagination and fault injection."""

    def __init__(self) -> None:
        self.ctrl_main = sha("ctrl-main")
        self.projects = {ZARI: {"project": "ZARI", "program_required_checks": ["bridge"]},
                         KIXP: {"project": "KIX", "program_required_checks": ["protocol", "kernel"]}}
        self.activation = {"runtime_enabled": True, "activated_runtime_sha": sha("activated")}
        self.runtime_runs = [{"id": 900 + i, "head_sha": sha(f"run{i}"), "conclusion": "success",
                              "created_at": iso(ago(hours=i))} for i in range(5)]
        self.repos: Dict[str, Dict[str, Any]] = {}
        for repo in (ZARI, KIXP):
            self.repos[repo] = {"default_branch": "main", "head": sha(repo + "head"), "issues": [], "pulls": [],
                                "comments": [], "plan_commits": [], "plan_content": {}, "branch_commits": [],
                                "pull_detail": {}, "pull_files": {}, "check_runs": {}, "commit_pulls": {},
                                "events": {}, "issue_detail": {}}
        self.ledger_comments: Dict[int, Dict[str, Any]] = {}
        self.fail: Dict[str, int] = {}          # path prefix -> HTTP status
        self.requests: List[Tuple[str, Dict[str, str], Optional[str]]] = []
        # host
        self.lanes = {"status": "OK", "max_active_sessions": 4, "active_total": 0,
                      "lanes": [{"lane": lane, "enabled": True, "active": []}
                                for lane in ("DEVIN", "GROK_BUILD", "GLM", "CURSOR")]}
        self.materializations: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.rows: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.host_mode = "ok"                   # ok | timeout | refuse
        self.host_argv: List[List[str]] = []

    # -- GitHub

    def route(self, path: str, q: Dict[str, str]) -> Tuple[int, Any]:
        if path == f"/repos/{CTRL}/branches/main":
            return 200, {"commit": {"sha": self.ctrl_main}}
        if path == f"/repos/{CTRL}/contents/{facts_mod.CTRL_PROJECTS_PATH}":
            return 200, b64(self.projects)
        if path == f"/repos/{CTRL}/contents/{facts_mod.CTRL_ACTIVATION_PATH}":
            return 200, b64(self.activation)
        if path == f"/repos/{CTRL}/actions/workflows/control-plane-runtime.yml/runs":
            return 200, {"workflow_runs": self.runtime_runs}
        if path.startswith(f"/repos/{CTRL}/issues/comments/"):
            cid = int(path.rsplit("/", 1)[1])
            return (200, self.ledger_comments[cid]) if cid in self.ledger_comments else (404, None)
        for repo, r in self.repos.items():
            base = f"/repos/{repo}"
            if not (path == base or path.startswith(base + "/")):
                continue
            rest = path[len(base):]
            if rest == "":
                return 200, {"default_branch": r["default_branch"], "full_name": repo}
            if rest == f"/branches/{r['default_branch']}":
                return 200, {"commit": {"sha": r["head"]}}
            if rest == "/issues":
                return 200, sorted(r["issues"], key=lambda i: i.get("updated_at", ""), reverse=True)[:50]
            if rest == "/pulls":
                return 200, self.page(sorted(r["pulls"], key=lambda p: p.get("updated_at", ""), reverse=True), q)
            if rest == "/issues/comments":
                since = q.get("since")
                items = [c for c in r["comments"] if not since or c["updated_at"] >= since]
                return 200, self.page(items, q)
            if rest == "/commits" and "path" in q:
                items = r["plan_commits"]
                if "since" in q:
                    items = [c for c in items if c["commit"]["committer"]["date"] >= q["since"]]
                return 200, self.page(items, q)
            if rest == "/commits":
                items = [c for c in r["branch_commits"] if c["commit"]["committer"]["date"] >= q.get("since", "")]
                return 200, self.page(items, q)
            if rest == "/contents/.aiops/program.json":
                content = r["plan_content"].get(q.get("ref"))
                return (200, b64(content)) if content is not None else (404, None)
            parts = rest.strip("/").split("/")
            if parts[0] == "pulls" and len(parts) == 2:
                pr = r["pull_detail"].get(int(parts[1]))
                return (200, pr) if pr else (404, None)
            if parts[0] == "pulls" and len(parts) == 3 and parts[2] == "files":
                return 200, self.page(r["pull_files"].get(int(parts[1]), []), q)
            if parts[0] == "commits" and len(parts) == 3 and parts[2] == "check-runs":
                runs = r["check_runs"].get(parts[1], [])
                return 200, {"total_count": len(runs), "check_runs": self.page(runs, q)}
            if parts[0] == "commits" and len(parts) == 3 and parts[2] == "pulls":
                return 200, r["commit_pulls"].get(parts[1], [])
            if parts[0] == "issues" and len(parts) == 3 and parts[2] == "events":
                return 200, self.page(r["events"].get(int(parts[1]), []), q)
            if parts[0] == "issues" and len(parts) == 2:
                issue = r["issue_detail"].get(int(parts[1]))
                return (200, issue) if issue else (404, None)
        return 404, None

    @staticmethod
    def page(items: List[Any], q: Dict[str, str]) -> List[Any]:
        per = int(q.get("per_page", 30))
        page = int(q.get("page", 1))
        return items[(page - 1) * per: page * per]

    def transport(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
                  timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        assert method == "GET", method
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.requests.append((parts.path, q, headers.get("If-None-Match")))
        for prefix, status in self.fail.items():
            if parts.path.startswith(prefix):
                extra = {"x-ratelimit-remaining": "0"} if status == 429 else {}
                return status, extra, b'{"message":"fail"}'
        status, data = self.route(parts.path, q)
        if status != 200:
            return status, {}, b'{"message":"Not Found"}'
        raw = json.dumps(data, sort_keys=True).encode()
        etag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
        if headers.get("If-None-Match") == etag:
            return 304, {"etag": etag}, b""
        return 200, {"etag": etag}, raw

    # -- host

    def runner(self, command: List[str], **kw: Any) -> subprocess.CompletedProcess:
        assert command[:5] == ["/usr/bin/sudo", "-n", "-u", "astra-control", "/opt/astra/bin/astra-host-control"]
        assert kw["user"] == "aiops-inspect-ledger" and kw["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        argv = command[5:]
        self.host_argv.append(argv)
        if self.host_mode == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        if self.host_mode == "refuse":
            out = {"status": "ERROR", "reason": "refused"}
        elif argv[0] == "status":
            out = self.lanes
        elif argv[0] == "materialize-status":
            program, nid = argv[2], argv[4]
            out = self.materializations.get((program, nid), {"status": "NOT_FOUND", "program": program, "node": nid})
        elif argv[0] == "task-status":
            repo, task = argv[2], argv[4]
            out = {"status": "OK", "repository": repo, "task": task, "rows": self.rows.get((repo, task), [])}
        else:  # pragma: no cover
            raise AssertionError(argv)
        return subprocess.CompletedProcess(command, 0, (json.dumps(out) + "\n").encode(), b"")

    # -- readers

    def reader(self) -> gh.GitHubReader:
        return gh.GitHubReader(TOKEN, [CTRL, ZARI, KIXP], transport=self.transport)

    def host(self) -> hostmod.HostReader:
        return hostmod.HostReader(runner=self.runner)

    # -- builders

    def add_plan(self, repo: str, plan: Any, when: datetime) -> str:
        commit = sha(repo + json.dumps(plan, sort_keys=True))
        r = self.repos[repo]
        r["plan_commits"].insert(0, {"sha": commit, "commit": {"committer": {"date": iso(when)}}})
        r["plan_content"][commit] = plan
        return commit

    def add_pull(self, repo: str, number: int, *, merged_at: Optional[datetime] = None, state: str = "closed",
                 files: Optional[List[str]] = None, updated: Optional[datetime] = None, base: str = "main",
                 detail: bool = False) -> Dict[str, Any]:
        head = sha(f"{repo}pr{number}head")
        merge = sha(f"{repo}pr{number}merge") if merged_at else None
        pull = {"number": number, "state": state, "merged_at": iso(merged_at) if merged_at else None,
                "merge_commit_sha": merge, "head": {"sha": head}, "base": {"ref": base},
                "updated_at": iso(updated or merged_at or ago(hours=1)), "title": "free text"}
        r = self.repos[repo]
        r["pulls"].append(pull)
        r["pull_files"][number] = [{"filename": f, "status": "modified", "patch": "@@"} for f in (files or ["a.py"])]
        if detail:
            r["pull_detail"][number] = dict(pull, merged=bool(merged_at))
        return pull

    def add_issue(self, repo: str, number: int, *, labels: List[str], state: str = "open",
                  state_reason: Optional[str] = None, updated: Optional[datetime] = None) -> Dict[str, Any]:
        issue = {"number": number, "state": state, "state_reason": state_reason,
                 "labels": [{"name": label} for label in labels], "closed_at": None,
                 "updated_at": iso(updated or ago(hours=2)), "title": "free", "body": "free"}
        self.repos[repo]["issues"].append(issue)
        return issue


def created(program: str, nid: str, issue: int, repo: str = ZARI) -> Dict[str, Any]:
    return {"status": "CREATED", "program": program, "node": nid, "request": req(nid), "attempt": 1,
            "issue": issue, "plan_commit": sha("plan"), "repository": repo, "sealed": {"k": "v"}}


def full_world() -> World:
    """ZARI with a PRESENT plan and nodes in every completion stage; KIXP without a plan."""
    w = World()
    nodes = [node("N1"), node("N2", ["N1"]), node("N3", ["N1"]), node("N4", ["N2"]), node("N5", ["N3"]),
             node("N6", ["N4", "N5"], title="Leak " + LEAK)]
    w.add_plan(ZARI, plan_doc(ZARI, "ZARI", nodes), ago(days=10))
    prog = "zari"
    # N1 DONE (merged pin + check runs), N2 DELIVERED, N3 IN_PROGRESS, N4 NOT_STARTED, N5 MATERIALIZING, N6 PLANNED
    pr1 = w.add_pull(ZARI, 11, merged_at=ago(days=2), files=["src/a.py", "tests/test_a.py"], detail=True)
    w.add_pull(ZARI, 12, state="open", files=["src/b.py"], detail=True, updated=ago(hours=3))
    w.materializations[(prog, "N1")] = created(prog, "N1", 101)
    w.materializations[(prog, "N2")] = created(prog, "N2", 102)
    w.materializations[(prog, "N3")] = created(prog, "N3", 103)
    w.materializations[(prog, "N4")] = created(prog, "N4", 104)
    w.materializations[(prog, "N5")] = {"status": "SUBMITTING", "program": prog, "node": "N5"}
    w.rows[(ZARI, "ZARI-N1")] = [writer_row("ZARI-N1", pin={"kind": "DELIVERY", "pr": 11, "head": pr1["head"]["sha"]})]
    w.rows[(ZARI, "ZARI-N2")] = [writer_row("ZARI-N2", pin={"kind": "DELIVERY", "pr": 12,
                                                             "head": sha(f"{ZARI}pr12head")})]
    w.rows[(ZARI, "ZARI-N3")] = [writer_row("ZARI-N3", state="CONFIRMED")]
    w.repos[ZARI]["check_runs"][pr1["merge_commit_sha"]] = [
        {"id": 5, "name": "bridge", "status": "completed", "conclusion": "failure", "started_at": iso(ago(days=2)),
         "completed_at": iso(ago(days=2)), "app": {"id": 15368, "slug": "github-actions"}},
        {"id": 7, "name": "bridge", "status": "completed", "conclusion": "success", "started_at": iso(ago(days=2)),
         "completed_at": iso(ago(days=2)), "app": {"id": 15368, "slug": "github-actions"}}]
    for number in (101, 102, 103, 104):
        w.add_issue(ZARI, number, labels=["aiops-task"])
    return w


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = core.StateStore(Path(self.tmp.name) / "state")

    def tick(self, w: World, now: datetime = NOW, cfg: Optional[Dict[str, Any]] = None,
             budgets: Optional[facts_mod.Budgets] = None, **kw: Any) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        cfg = cfg or config()
        reader, host = w.reader(), w.host()
        probe = facts_mod.probe(reader, host, cfg, self.state, now)
        run, _ = facts_mod.should_run_t1(probe, self.state, now)
        if not run:
            facts_mod.commit_unchanged(self.state)
            return probe, None
        doc = facts_mod.collect(reader, host, cfg, self.state, now, budgets, probe_result=probe, **kw)
        facts_mod.commit_t1(self.state, doc, now, cfg)
        return probe, doc


# ---------------------------------------------------------------------------- T0 and the early-exit rule

class ProbeTests(Base):
    def test_first_probe_changed_and_staged_only(self) -> None:
        w = full_world()
        probe = facts_mod.probe(w.reader(), w.host(), config(), self.state, NOW)
        self.assertTrue(probe["changed"])
        self.assertIn(f"issues:{ZARI}", probe["changed_keys"])
        self.assertIn("host:lanes", probe["changed_keys"])
        self.assertIsNotNone(self.state.read_staged("etags"))
        self.assertIsNone(self.state.read("etags"))     # committed only after T1 or when unchanged
        self.assertEqual(probe["targets"][ZARI]["head_sha"], w.repos[ZARI]["head"])
        self.assertEqual(probe["control"]["main_sha"], w.ctrl_main)
        # The issue list request is exactly the contract's
        issue_q = [q for p, q, _ in w.requests if p == f"/repos/{ZARI}/issues"][0]
        self.assertEqual(issue_q, {"labels": "aiops-task", "state": "all", "sort": "updated", "direction": "desc",
                                   "per_page": "50"})

    def test_unchanged_after_commit_skips_t1(self) -> None:
        w = full_world()
        _, doc = self.tick(w)
        self.assertIsNotNone(doc)
        later = NOW + timedelta(hours=1)
        w.requests.clear()
        probe = facts_mod.probe(w.reader(), w.host(), config(), self.state, later)
        self.assertFalse(probe["changed"], probe["changed_keys"])
        # every conditional GET carried an ETag (the comments ETag was primed by T1)
        conditional = [r for r in w.requests if not r[0].endswith(("/repos/" + ZARI, "/repos/" + KIXP))]
        self.assertTrue(all(etag for _, _, etag in conditional), conditional)
        run, reason = facts_mod.should_run_t1(probe, self.state, later)
        self.assertEqual((run, reason), (False, "UNCHANGED"))
        committed = facts_mod.commit_unchanged(self.state)
        self.assertEqual(committed, ["etags", "t0", "repos"])
        self.assertEqual(self.state.read("etags")["tokens"], probe["etags"])

    def test_changed_issue_and_lanes(self) -> None:
        w = full_world()
        self.tick(w)
        w.repos[ZARI]["issues"][0]["labels"].append({"name": "needs-user"})
        w.lanes["lanes"][0]["active"].append({"request": req("x"), "repository": ZARI, "task": "ZARI-N3",
                                              "role": "WRITER", "state": "CONFIRMED", "created": NOW.timestamp()})
        w.lanes["active_total"] = 1
        probe = facts_mod.probe(w.reader(), w.host(), config(), self.state, NOW + timedelta(hours=1))
        self.assertTrue(probe["changed"])
        self.assertIn(f"issues:{ZARI}", probe["changed_keys"])
        self.assertIn("host:lanes", probe["changed_keys"])

    def test_early_exit_rule(self) -> None:
        w = full_world()
        self.tick(w)
        unchanged = {"changed": False}
        recheck = self.state.read("recheck")
        self.assertFalse(recheck["t1_dirty"])
        due = core.parse_iso(recheck["next_recheck_at"])
        self.assertEqual(facts_mod.should_run_t1(unchanged, self.state, due - timedelta(minutes=1)),
                         (False, "UNCHANGED"))
        self.assertEqual(facts_mod.should_run_t1(unchanged, self.state, due), (True, "RECHECK_DUE"))
        self.assertEqual(facts_mod.should_run_t1({"changed": True}, self.state, NOW), (True, "CHANGED"))
        self.state.write("recheck", dict(recheck, t1_dirty=True))
        self.assertEqual(facts_mod.should_run_t1(unchanged, self.state, NOW), (True, "DIRTY"))
        self.state.remove("recheck")
        self.assertEqual(facts_mod.should_run_t1(unchanged, self.state, NOW), (True, "DIRTY"))

    def test_steady_target_error_is_unchanged(self) -> None:
        w = full_world()
        w.fail[f"/repos/{KIXP}/issues"] = 500
        _, doc = self.tick(w)
        self.assertEqual(doc["products"][KIXP]["unknown"], list(facts_mod.PRODUCT_GROUPS))
        probe = facts_mod.probe(w.reader(), w.host(), config(), self.state, NOW + timedelta(hours=1))
        self.assertNotIn(f"err:{KIXP}", probe["changed_keys"])

    def test_rate_limit_aborts_probe_and_t1(self) -> None:
        w = full_world()
        w.fail[f"/repos/{ZARI}/pulls"] = 429
        with self.assertRaises(core.InspectError) as ctx:
            facts_mod.probe(w.reader(), w.host(), config(), self.state, NOW)
        self.assertEqual(ctx.exception.reason, "GITHUB_RATE_LIMIT")
        w.fail.clear()
        reader, host = w.reader(), w.host()
        probe = facts_mod.probe(reader, host, config(), self.state, NOW)
        w.fail[f"/repos/{ZARI}/pulls/11"] = 429       # the pinned delivery PR, read only in T1
        with self.assertRaises(core.InspectError) as ctx:
            facts_mod.collect(reader, host, config(), self.state, NOW, probe_result=probe)
        self.assertEqual(ctx.exception.reason, "GITHUB_RATE_LIMIT")


# ---------------------------------------------------------------------------- T1 failure and the since cursor

class FailureTests(Base):
    def comments_since(self, w: World, repo: str) -> Optional[str]:
        qs = [q for p, q, _ in w.requests if p == f"/repos/{repo}/issues/comments" and q.get("per_page") == "100"]
        return qs[0].get("since") if qs else None

    def test_t1_failure_keeps_previous_facts_and_etags(self) -> None:
        w = full_world()
        _, first = self.tick(w)
        etags_before = self.state.read("etags")
        facts_before = self.state.read("facts")
        w.repos[ZARI]["issues"][0]["labels"].append({"name": "blocked"})
        later = NOW + timedelta(hours=1)
        reader, host = w.reader(), w.host()
        probe = facts_mod.probe(reader, host, config(), self.state, later)
        self.assertTrue(probe["changed"])
        with self.assertRaises(core.InspectError) as ctx:
            facts_mod.collect(reader, host, config(), self.state, later,
                              facts_mod.Budgets(github_calls=3), probe_result=probe)
        self.assertEqual(ctx.exception.reason, "GITHUB_BUDGET")
        record = facts_mod.fail_t1(self.state, later, ctx.exception.reason)
        self.assertTrue(record["t1_dirty"])
        self.assertEqual(record["t1_failures"], 1)
        self.assertEqual(self.state.read("facts"), facts_before)
        self.assertEqual(self.state.read("etags"), etags_before)
        self.assertIsNone(self.state.read_staged("etags"))
        # The same change is seen again, and the dirty flag forces T1 even when unchanged
        probe2 = facts_mod.probe(reader, host, config(), self.state, later + timedelta(hours=1))
        self.assertTrue(probe2["changed"])
        self.assertEqual(facts_mod.should_run_t1({"changed": False}, self.state, later), (True, "DIRTY"))

    def test_since_cursor_fixed_between_commits(self) -> None:
        w = full_world()
        self.tick(w)
        committed_since = self.state.read("etags")["since"]
        self.assertEqual(committed_since, iso(NOW))
        self.assertEqual(self.state.read("recheck")["last_t1"], iso(NOW))
        w.requests.clear()
        t2 = NOW + timedelta(hours=1)
        reader, host = w.reader(), w.host()
        probe = facts_mod.probe(reader, host, config(), self.state, t2)
        self.assertEqual(self.comments_since(w, ZARI), committed_since)
        facts_mod.fail_t1(self.state, t2, "HOST_BUDGET")
        w.requests.clear()
        facts_mod.probe(reader, host, config(), self.state, t2 + timedelta(hours=1))
        self.assertEqual(self.comments_since(w, ZARI), committed_since)   # unchanged after a failed T1
        # A new comment after the cursor is a change
        w.repos[ZARI]["comments"].append({"id": 1, "body": "hi", "issue_url": f"https://api.github.com/repos/{ZARI}/issues/101",
                                          "updated_at": iso(t2), "created_at": iso(t2)})
        probe3 = facts_mod.probe(reader, host, config(), self.state, t2 + timedelta(hours=2))
        self.assertIn(f"comments:{ZARI}", probe3["changed_keys"])
        self.assertIsNotNone(probe)

    def test_host_down_is_unknown_not_exception(self) -> None:
        w = full_world()
        w.host_mode = "timeout"
        _, doc = self.tick(w)
        self.assertIsNone(doc["lanes"])
        self.assertIn("lanes", doc["unknown"])
        zari = doc["products"][ZARI]
        self.assertIn("host", zari["unknown"])
        self.assertIsNone(zari["nodes"]["N1"]["completion"])
        # after the first timeout no further host call is attempted in this T1
        self.assertLessEqual(len(w.host_argv), 2)

    def test_host_budget_aborts(self) -> None:
        w = full_world()
        reader, host = w.reader(), w.host()
        probe = facts_mod.probe(reader, host, config(), self.state, NOW)
        with self.assertRaises(core.InspectError) as ctx:
            facts_mod.collect(reader, host, config(), self.state, NOW, facts_mod.Budgets(host_calls=2),
                              probe_result=probe)
        self.assertEqual(ctx.exception.reason, "HOST_BUDGET")

    def test_time_budget_aborts(self) -> None:
        w = full_world()
        ticks = iter(range(0, 10 ** 6, 100))
        reader, host = w.reader(), w.host()
        probe = facts_mod.probe(reader, host, config(), self.state, NOW)
        with self.assertRaises(core.InspectError) as ctx:
            facts_mod.collect(reader, host, config(), self.state, NOW,
                              facts_mod.Budgets(seconds=480, clock=lambda: float(next(ticks))), probe_result=probe)
        self.assertEqual(ctx.exception.reason, "T1_TIMEOUT")


# ---------------------------------------------------------------------------- collection branches

class CollectTests(Base):
    def test_stages_and_merge_checks(self) -> None:
        w = full_world()
        _, doc = self.tick(w)
        self.assertEqual(doc["schema"], "AIOPS_INSPECT_FACTS_V1")
        zari = doc["products"][ZARI]
        self.assertEqual(zari["plan"]["state"], "PRESENT")
        self.assertEqual(zari["plan"]["program"], "zari")
        stages = {n: zari["nodes"][n]["completion"]["stage"] for n in zari["nodes"]}
        self.assertEqual(stages, {"N1": "DONE", "N2": "DELIVERED", "N3": "IN_PROGRESS", "N4": "NOT_STARTED",
                                  "N5": "MATERIALIZING", "N6": "PLANNED"})
        n1 = zari["nodes"]["N1"]
        self.assertEqual(n1["task_id"], "ZARI-N1")
        self.assertEqual(n1["completion"]["merge_checks"], "PASS")   # the newest bridge run (id 7) succeeded
        self.assertEqual(n1["completion"]["deployed"], "NOT_RECORDED")
        self.assertEqual(sorted(r["name"] for r in n1["merge_runs"]), ["bridge", "bridge"])
        self.assertEqual(set(n1["merge_runs"][0]), {"id", "name", "status", "conclusion", "started_at",
                                                     "completed_at", "app"})
        self.assertEqual([f["filename"] for f in n1["delivery_files"]], ["src/a.py", "tests/test_a.py"])
        self.assertNotIn("patch", n1["delivery_files"][0])
        self.assertEqual(n1["delivery_pr"]["head"]["sha"], sha(f"{ZARI}pr11head"))
        self.assertIsNone(zari["nodes"]["N2"]["merge_runs"])
        self.assertIsNone(zari["nodes"]["N6"]["rows"])
        self.assertEqual(zari["required_checks"], ["bridge"])
        self.assertEqual(zari["unknown"], [])
        self.assertEqual(doc["control"]["runtime_enabled"], True)
        self.assertEqual(len(doc["control"]["runtime_runs"]), 5)
        self.assertEqual(doc["lanes"]["source"], "HOST")
        self.assertEqual(zari["sources"]["nodes.issue"], "GH_TEXT")
        # pending post-merge checks
        w2 = full_world()
        w2.repos[ZARI]["check_runs"][sha(f"{ZARI}pr11merge")][1]["status"] = "in_progress"
        self.state = core.StateStore(Path(self.tmp.name) / "state2")
        _, doc2 = self.tick(w2)
        self.assertEqual(doc2["products"][ZARI]["nodes"]["N1"]["completion"]["merge_checks"], "PENDING")

    def test_plan_none_and_invalid(self) -> None:
        w = full_world()
        _, doc = self.tick(w)
        self.assertEqual(doc["products"][KIXP]["plan"]["state"], "NONE")
        self.assertEqual(doc["products"][KIXP]["nodes"], {})
        # INVALID: a dependency cycle never raises out of the tick
        w2 = World()
        w2.add_plan(KIXP, plan_doc(KIXP, "KIX", [node("A", ["B"]), node("B", ["A"])], program="kixp"), ago(days=1))
        w2.add_plan(ZARI, plan_doc(KIXP, "ZARI", [node("A")]), ago(days=1))   # targets another repository
        self.state = core.StateStore(Path(self.tmp.name) / "s2")
        _, doc2 = self.tick(w2)
        kixp = doc2["products"][KIXP]
        self.assertEqual(kixp["plan"]["state"], "INVALID")
        self.assertIn("cycle", kixp["plan"]["invalid_reason"])
        self.assertEqual(kixp["plan"]["plan_commits_7d"], 1)
        self.assertEqual(doc2["products"][ZARI]["plan"]["state"], "INVALID")
        self.assertEqual(w2.host_argv, [["status", "--lanes"]])           # no node reads for invalid plans
        # deleted plan file at the latest commit -> NONE; unreadable JSON -> INVALID
        w3 = World()
        commit = w3.add_plan(ZARI, None, ago(days=1))
        w3.repos[ZARI]["plan_content"].pop(commit)
        w3.repos[KIXP]["plan_commits"].insert(0, {"sha": sha("bad"), "commit": {"committer": {"date": iso(NOW)}}})
        w3.repos[KIXP]["plan_content"][sha("bad")] = "not an object"
        self.state = core.StateStore(Path(self.tmp.name) / "s3")
        _, doc3 = self.tick(w3)
        self.assertEqual(doc3["products"][ZARI]["plan"]["state"], "NONE")
        self.assertEqual(doc3["products"][KIXP]["plan"]["state"], "INVALID")

    def test_pending_plan_prs(self) -> None:
        w = full_world()
        w.add_pull(ZARI, 20, state="open", files=[".aiops/program.json", "README.md"], updated=ago(hours=1))
        w.add_pull(ZARI, 21, state="open", files=["src/x.py"], updated=ago(hours=1))
        _, doc = self.tick(w)
        pending = doc["products"][ZARI]["plan"]["pending_plan_prs"]
        self.assertEqual([p["number"] for p in pending], [20])
        self.assertEqual(pending[0]["head_sha"], sha(f"{ZARI}pr20head"))

    def test_blocked_since_from_events(self) -> None:
        w = full_world()
        issue = w.repos[ZARI]["issues"][3]   # #104, node N4
        issue["labels"] += [{"name": "needs-user"}, {"name": "blocked"}]
        w.repos[ZARI]["events"][104] = [
            {"event": "labeled", "label": {"name": "needs-user"}, "created_at": iso(ago(hours=80))},
            {"event": "unlabeled", "label": {"name": "needs-user"}, "created_at": iso(ago(hours=70))},
            {"event": "labeled", "label": {"name": "needs-user"}, "created_at": iso(ago(hours=30))},
            {"event": "labeled", "label": {"name": "blocked"}, "created_at": iso(ago(hours=26))},
            {"event": "labeled", "label": {"name": "other"}, "created_at": iso(ago(hours=90))}]
        _, doc = self.tick(w)
        n4 = doc["products"][ZARI]["nodes"]["N4"]
        self.assertEqual(n4["blocked_since"], iso(ago(hours=30)))
        self.assertEqual(n4["issue"]["labels"], ["aiops-task", "blocked", "needs-user"])
        self.assertIsNone(doc["products"][ZARI]["nodes"]["N3"]["blocked_since"])
        # events unreadable -> "events" UNKNOWN, not an exception
        w.fail[f"/repos/{ZARI}/issues/104/events"] = 502
        self.state = core.StateStore(Path(self.tmp.name) / "s2")
        _, doc2 = self.tick(w)
        self.assertIn("events", doc2["products"][ZARI]["unknown"])
        self.assertIsNone(doc2["products"][ZARI]["nodes"]["N4"]["blocked_since"])

    def test_orphans_direct_pushes_and_issue_fallback(self) -> None:
        w = full_world()
        w.add_pull(ZARI, 30, merged_at=ago(days=1), files=["src/orphan.py"])
        w.add_pull(ZARI, 31, merged_at=ago(days=1), files=[".aiops/program.json"])
        w.add_pull(ZARI, 32, merged_at=ago(days=9), files=["old.py"], updated=ago(days=9))
        w.add_pull(ZARI, 33, merged_at=ago(days=1), files=["x.py"], base="dev")
        r = w.repos[ZARI]
        merge30 = sha(f"{ZARI}pr30merge")
        r["branch_commits"] = [
            {"sha": merge30, "commit": {"committer": {"date": iso(ago(days=1))}}},
            {"sha": sha("rebased"), "commit": {"committer": {"date": iso(ago(days=2))}}},
            {"sha": sha("direct"), "commit": {"committer": {"date": iso(ago(days=3))}}}]
        r["commit_pulls"][sha("rebased")] = [{"number": 31}]
        r["commit_pulls"][sha("direct")] = []
        _, doc = self.tick(w)
        zari = doc["products"][ZARI]
        merged = {p["number"]: p for p in zari["merged_7d"]}
        self.assertEqual(sorted(merged), [11, 30, 31])
        self.assertTrue(merged[11]["pinned"])
        self.assertFalse(merged[30]["pinned"])
        self.assertFalse(merged[30]["aiops_only"])
        self.assertTrue(merged[31]["aiops_only"])
        self.assertEqual(zari["direct_pushes_7d"], [{"sha": sha("direct"), "date": iso(ago(days=3))}])
        asked = [p for p, _, _ in w.requests if p.endswith("/pulls") and "/commits/" in p]
        self.assertNotIn(f"/repos/{ZARI}/commits/{merge30}/pulls", asked)   # known merge commit is skipped

    def test_merged_pulls_paginate_back_seven_days(self) -> None:
        w = full_world()
        for n in range(100, 160):   # 60 recently updated pulls push the merged one off the first page
            w.add_pull(ZARI, n, state="open", updated=ago(minutes=n))
        w.add_pull(ZARI, 99, merged_at=ago(days=5), files=["deep.py"], updated=ago(days=5))
        _, doc = self.tick(w)
        self.assertIn(99, [p["number"] for p in doc["products"][ZARI]["merged_7d"]])
        pages = [q.get("page") for p, q, _ in w.requests if p == f"/repos/{ZARI}/pulls" and q.get("page")]
        self.assertEqual(pages, ["2"])

    def test_exceptions_count(self) -> None:
        w = full_world()
        marker = "ASTRA_CONSULT_V1 result=APPROVED_SMALL_EXCEPTION node=N2"
        url = f"https://api.github.com/repos/{ZARI}/issues/"
        w.repos[ZARI]["comments"] = [
            {"id": 1, "body": marker, "issue_url": url + "102", "updated_at": iso(ago(days=1))},
            {"id": 2, "body": "x\n" + marker, "issue_url": url + "102", "updated_at": iso(ago(days=2))},
            {"id": 3, "body": marker, "issue_url": url + "103", "updated_at": iso(ago(days=20))},
            {"id": 4, "body": "ASTRA_CONSULT_V1 result=REJECTED", "issue_url": url + "103",
             "updated_at": iso(ago(days=1))}]
        _, doc = self.tick(w)
        nodes = doc["products"][ZARI]["nodes"]
        self.assertEqual(nodes["N2"]["exceptions_14d"], 2)
        self.assertEqual(nodes["N3"]["exceptions_14d"], 0)
        self.assertEqual(nodes["N6"]["exceptions_14d"], 0)

    def test_required_check_shrink_and_baseline(self) -> None:
        w = full_world()
        self.tick(w)
        self.assertEqual(self.state.read("baseline")[KIXP], ["protocol", "kernel"])
        w.projects[KIXP]["program_required_checks"] = ["protocol"]
        w.ctrl_main = sha("ctrl-main-2")
        _, doc = self.tick(w, NOW + timedelta(hours=1))
        self.assertTrue(doc["products"][KIXP]["required_checks_shrank"])
        self.assertFalse(doc["products"][ZARI]["required_checks_shrank"])
        self.assertEqual(self.state.read("baseline")[KIXP], ["protocol", "kernel"])   # never shrinks

    def test_projects_unreadable_is_unknown(self) -> None:
        w = full_world()
        w.fail[f"/repos/{CTRL}/contents/"] = 500
        _, doc = self.tick(w)
        self.assertIn("control", doc["unknown"])
        self.assertIn("required_checks", doc["products"][ZARI]["unknown"])
        self.assertIsNone(doc["products"][ZARI]["required_checks"])
        self.assertEqual(doc["products"][ZARI]["plan"]["state"], "PRESENT")

    def test_ledger_comments(self) -> None:
        w = full_world()
        w.ledger_comments[77] = {"id": 77, "body": "posted body", "updated_at": iso(ago(hours=1))}
        _, doc = self.tick(w, ledger_ids=[77, 78, "x"])
        comments = {c["id"]: c for c in doc["ledger"]["comments"]}
        self.assertEqual(comments[77]["body_sha256"], hashlib.sha256(b"posted body").hexdigest())
        self.assertTrue(comments[78]["deleted"])
        self.assertNotIn("body", comments[77])

    def test_redaction_of_github_text(self) -> None:
        w = full_world()
        w.repos[ZARI]["issues"][0]["labels"].append({"name": "x-" + LEAK})
        w.repos[ZARI]["pull_files"][11].append({"filename": "leak/" + LEAK + ".txt", "status": "added"})
        _, doc = self.tick(w)
        text = core.canon(doc).decode()
        self.assertNotIn(LEAK, text)
        self.assertIn("[redacted sha256=", text)
        self.assertGreater(doc["stats"]["redactions"], 0)
        self.assertIn("[redacted sha256=", doc["products"][ZARI]["plan"]["nodes"][5]["title"])
        self.assertNotIn("title", doc["products"][ZARI]["nodes"]["N1"]["delivery_pr"])

    def test_caches_by_sha(self) -> None:
        w = full_world()
        self.tick(w)
        w.ctrl_main = sha("ctrl-main-2")   # force a change, nothing else
        w.requests.clear()
        self.tick(w, NOW + timedelta(hours=1))
        paths = [p for p, _, _ in w.requests]
        self.assertNotIn(f"/repos/{ZARI}/commits/{sha(f'{ZARI}pr11merge')}/check-runs", paths)
        self.assertNotIn(f"/repos/{ZARI}/pulls/11/files", paths)
        self.assertNotIn(f"/repos/{ZARI}/contents/.aiops/program.json", paths)
        cache = self.state.read("cache")
        self.assertIn(f"{ZARI}@{sha(f'{ZARI}pr11merge')}", cache["checks"])

    def test_collect_from_staged_probe_state(self) -> None:
        w = full_world()
        reader, host = w.reader(), w.host()
        probe = facts_mod.probe(reader, host, config(), self.state, NOW)
        with_probe = facts_mod.collect(reader, host, config(), self.state, NOW, probe_result=probe)
        from_state = facts_mod.collect(reader, host, config(), self.state, NOW)
        self.assertEqual(facts_mod.hashes_of(with_probe), facts_mod.hashes_of(from_state))

    def test_commit_writes_group(self) -> None:
        w = full_world()
        _, doc = self.tick(w)
        self.assertEqual(self.state.read("facts"), json.loads(json.dumps(doc)))
        hashes = self.state.read("hashes")
        self.assertEqual(hashes["snapshot"], facts_mod.hashes_of(doc)["snapshot"])
        self.assertEqual(set(hashes["products"]), {ZARI, KIXP})
        history = self.state.read_jsonl("history")
        self.assertEqual(history, [{"t": iso(NOW), "products": {ZARI: {"planned": 6, "done": 1}}}])
        recheck = self.state.read("recheck")
        self.assertEqual(recheck["t1_dirty"], False)
        self.assertEqual(recheck["last_t1"], iso(NOW))
        for name in ("etags", "t0", "repos", "cache", "facts", "recheck"):
            self.assertIsNone(self.state.read_staged(name), name)


# ---------------------------------------------------------------------------- hashing and recheck

def reorder(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: reorder(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [reorder(v) for v in obj]
    return obj


class HashTests(Base):
    def doc(self) -> Dict[str, Any]:
        w = full_world()
        w.lanes["lanes"][0]["active"] = [{"request": req("a"), "repository": ZARI, "task": "ZARI-N3", "role": "WRITER",
                                          "state": "CONFIRMED", "created": ago(hours=5).timestamp()}]
        w.lanes["active_total"] = 1
        w.repos[ZARI]["issues"][3]["labels"].append({"name": "needs-user"})
        w.repos[ZARI]["events"][104] = [{"event": "labeled", "label": {"name": "needs-user"},
                                         "created_at": iso(ago(hours=20))}]
        _, doc = self.tick(w)
        return doc

    def test_stable_across_key_order_and_volatile_fields(self) -> None:
        doc = self.doc()
        base = facts_mod.hashes_of(doc)
        self.assertEqual(facts_mod.hashes_of(reorder(doc)), base)
        noisy = copy.deepcopy(doc)
        noisy["stats"] = {"github_calls": 999, "host_calls": 5, "seconds": 3.5}
        noisy["ledger"] = {"comments": [{"id": 1, "body_sha256": "0" * 64}]}
        noisy["collected_at"] = iso(NOW + timedelta(minutes=10))       # same buckets
        n1 = noisy["products"][ZARI]["nodes"]["N1"]
        n1["delivery_pr"]["updated_at"] = iso(NOW)
        for i, run in enumerate(n1["merge_runs"]):
            run["id"] = 10 ** 6 + i
            run["started_at"] = iso(NOW)
        noisy["products"][ZARI]["plan"]["pending_plan_prs"] = [{"number": 5, "head_sha": None, "updated_at": "x"}]
        doc_pp = copy.deepcopy(doc)
        doc_pp["products"][ZARI]["plan"]["pending_plan_prs"] = [{"number": 5, "head_sha": None, "updated_at": "y"}]
        self.assertEqual(facts_mod.hashes_of(noisy)["products"][ZARI], facts_mod.hashes_of(doc_pp)["products"][ZARI])
        noisy["products"][ZARI]["plan"]["pending_plan_prs"] = []
        self.assertEqual(facts_mod.hashes_of(noisy), base)
        runtime = copy.deepcopy(doc)
        for run in runtime["control"]["runtime_runs"]:
            run["id"] += 1000
            run["created_at"] = iso(NOW)
        self.assertEqual(facts_mod.hashes_of(runtime), base)

    def test_material_change_changes_hash(self) -> None:
        doc = self.doc()
        base = facts_mod.hashes_of(doc)
        changed = copy.deepcopy(doc)
        changed["products"][ZARI]["nodes"]["N2"]["issue"]["labels"].append("blocked")
        new = facts_mod.hashes_of(changed)
        self.assertNotEqual(new["products"][ZARI], base["products"][ZARI])
        self.assertEqual(new["products"][KIXP], base["products"][KIXP])
        self.assertNotEqual(new["snapshot"], base["snapshot"])

    def test_bucket_crossings_change_hash(self) -> None:
        doc = self.doc()
        base = facts_mod.hashes_of(doc)
        # session 5h -> 7h crosses confirmed_watch_h=6
        later = copy.deepcopy(doc)
        later["collected_at"] = iso(NOW + timedelta(hours=2))
        self.assertNotEqual(facts_mod.hashes_of(later)["snapshot"], base["snapshot"])
        # blocked 20h -> 25h crosses 24h; the product hash moves
        lanes_only = copy.deepcopy(doc)
        lanes_only["lanes"]["lanes"][0]["active"] = []
        b0 = facts_mod.hashes_of(lanes_only)
        lanes_only["collected_at"] = iso(NOW + timedelta(hours=5))
        self.assertNotEqual(facts_mod.hashes_of(lanes_only)["products"][ZARI], b0["products"][ZARI])
        # threshold overrides move the buckets
        self.assertNotEqual(facts_mod.hashes_of(doc, {"thresholds": {"confirmed_watch_h": 4}})["snapshot"],
                            base["snapshot"])
        # done gap: the last DONE (2 days ago) crossing 3 days
        no_block = copy.deepcopy(lanes_only)
        no_block["collected_at"] = iso(NOW)
        no_block["products"][ZARI]["nodes"]["N4"]["blocked_since"] = None
        no_block["products"][ZARI]["nodes"]["N4"]["label_since"] = None
        h0 = facts_mod.hashes_of(no_block)["products"][ZARI]
        no_block["collected_at"] = iso(NOW + timedelta(hours=23))
        self.assertEqual(facts_mod.hashes_of(no_block)["products"][ZARI], h0)
        no_block["collected_at"] = iso(NOW + timedelta(hours=25))
        self.assertNotEqual(facts_mod.hashes_of(no_block)["products"][ZARI], h0)

    def test_facts_core_drops_volatile(self) -> None:
        doc = self.doc()
        fc = facts_mod.facts_core(doc)
        self.assertNotIn("collected_at", fc)
        self.assertNotIn("stats", fc)
        self.assertNotIn("ledger", fc)
        row = fc["lanes"]["lanes"][0]["active"][0]
        self.assertNotIn("created", row)
        self.assertEqual(row["age_bucket"], 0)
        n4 = fc["products"][ZARI]["nodes"]["N4"]
        self.assertNotIn("blocked_since", n4)
        self.assertEqual(n4["blocked_bucket"], 0)
        self.assertNotIn("id", fc["products"][ZARI]["nodes"]["N1"]["merge_runs"][0])

    def test_next_recheck_at(self) -> None:
        doc = self.doc()
        # CONFIRMED row created 5h ago crosses 6h in 1h: the earliest crossing
        self.assertEqual(facts_mod.next_recheck_at(doc, NOW, config()), NOW + timedelta(hours=1))
        quiet = copy.deepcopy(doc)
        quiet["lanes"]["lanes"][0]["active"] = []
        # blocked 20h ago -> 24h crossing in 4h
        self.assertEqual(facts_mod.next_recheck_at(quiet, NOW, config()), NOW + timedelta(hours=4))
        quiet["products"][ZARI]["nodes"]["N4"]["blocked_since"] = None
        quiet["products"][ZARI]["nodes"]["N4"]["label_since"] = None
        # next daily line: 09:00 KST on 10/02 = 00:00 UTC; done gap crossing (3d after 2 days ago) is later
        self.assertEqual(facts_mod.next_recheck_at(quiet, NOW, config()),
                         datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc))
        empty = {"schema": "AIOPS_INSPECT_FACTS_V1", "collected_at": iso(NOW), "products": {}, "lanes": None}
        at = facts_mod.next_recheck_at(empty, NOW, config(daily_hour_kst=14))
        self.assertEqual(at, datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc) + timedelta(days=1))
        self.assertLessEqual(at, NOW + timedelta(hours=24))



# ---------------------------------------------------------------------------- review fixes

class ReviewFixTests(Base):
    def run_ticks(self, w: World, hours: List[int],
                  cfg: Optional[Dict[str, Any]] = None) -> List[Optional[Dict[str, Any]]]:
        return [self.tick(w, NOW + timedelta(hours=h), cfg)[1] for h in hours]

    def test_redaction_input_is_bounded(self) -> None:
        seen: List[int] = []
        real = core.redact

        def spy(text: str, live: Any = ()) -> Any:
            seen.append(len(text))
            return real(text, live)

        with unittest.mock.patch.object(core, "redact", spy):
            red = facts_mod._Redactor()
            red.text("eyJ" * 20000, 200)
            red.text("x" * 50000, 4096)
            cut = red.text("a" * 190 + "ghp_" + "Z" * 10000, 200)
        self.assertLessEqual(max(seen), 4096 + 4096)
        self.assertLessEqual(seen[0], 200 + 4096)
        self.assertNotIn("ghp_", cut)
        self.assertNotIn("ZZZZ", cut)
        self.assertLessEqual(len(cut), 200)

    def test_read_error_in_t1_stays_dirty_and_recollects(self) -> None:
        w = full_world()
        self.tick(w)
        w.fail[f"/repos/{ZARI}/pulls/11"] = 502
        w.repos[ZARI]["issues"][0]["updated_at"] = iso(NOW)   # some change starts a T1
        _, doc = self.tick(w, NOW + timedelta(hours=1))
        self.assertEqual(doc["products"][ZARI]["unknown"], ["delivery"])
        self.assertIsNone(doc["products"][ZARI]["nodes"]["N1"]["completion"])
        recheck = self.state.read("recheck")
        self.assertTrue(recheck["t1_dirty"])
        self.assertEqual((recheck["t1_failures"], recheck["last_error"]), (1, "GITHUB_5XX"))
        del w.fail[f"/repos/{ZARI}/pulls/11"]
        _, doc2 = self.tick(w, NOW + timedelta(hours=2))
        self.assertIsNotNone(doc2)                              # collected again although nothing changed
        self.assertEqual(doc2["products"][ZARI]["nodes"]["N1"]["completion"]["stage"], "DONE")
        recheck = self.state.read("recheck")
        self.assertEqual((recheck["t1_dirty"], recheck["t1_failures"], recheck["last_error"]), (False, 0, None))

    def test_host_down_counts_consecutive_failures(self) -> None:
        w = full_world()
        w.host_mode = "timeout"
        docs = self.run_ticks(w, [0, 1, 2])
        self.assertTrue(all(d is not None for d in docs))
        recheck = self.state.read("recheck")
        self.assertTrue(recheck["t1_dirty"])
        self.assertEqual(recheck["t1_failures"], 3)
        self.assertTrue(recheck["last_error"].startswith("HOST_"), recheck["last_error"])

    def test_steady_data_gap_is_not_a_read_failure(self) -> None:
        w = full_world()
        del w.repos[ZARI]["pull_detail"][11]                    # pinned PR 404: a fact, not a read failure
        _, doc = self.tick(w)
        self.assertIn("delivery", doc["products"][ZARI]["unknown"])
        recheck = self.state.read("recheck")
        self.assertEqual((recheck["t1_dirty"], recheck["t1_failures"]), (False, 0))

    def test_non_list_page_is_a_read_failure(self) -> None:
        class Resp:
            status, json = 200, {"message": "not a list"}

        class Reader:
            calls = 0

            def get(self, path: str, params: Any) -> Any:
                self.calls += 1
                return Resp()

        run = facts_mod._Run(Reader(), None, facts_mod.Budgets(), {}, facts_mod._Redactor())
        with self.assertRaises(core.InspectError) as ctx:
            run.pages(f"/repos/{ZARI}/issues/1/comments", {"per_page": 100}, 3)
        self.assertEqual(ctx.exception.reason, "GITHUB_JSON")
        self.assertEqual(run.read_errors, {"GITHUB_JSON"})

    def test_check_runs_forbidden_is_steady_not_a_read_failure(self) -> None:
        # A read token without Checks permission (INSPECTOR.md §12.7) gets a plain 403 on check runs:
        # "checks" is UNKNOWN, but the T1 is not dirty and never counts toward DEGRADED(GITHUB_READ).
        w = full_world()
        w.fail[f"/repos/{ZARI}/commits/{sha(f'{ZARI}pr11merge')}/check-runs"] = 403
        docs = self.run_ticks(w, [0, 1, 2, 3])
        self.assertIsNotNone(docs[0])
        self.assertEqual(docs[0]["products"][ZARI]["unknown"], ["checks"])
        self.assertEqual(docs[1:], [None, None, None])          # nothing changed: no T1 every tick
        recheck = self.state.read("recheck")
        self.assertEqual((recheck["t1_dirty"], recheck["t1_failures"], recheck["last_error"]), (False, 0, None))

    def test_forbidden_elsewhere_is_still_a_read_failure(self) -> None:
        w = full_world()
        w.fail[f"/repos/{ZARI}/pulls/11"] = 403                 # only check runs have the steady 403
        _, doc = self.tick(w)
        self.assertIn("delivery", doc["products"][ZARI]["unknown"])
        recheck = self.state.read("recheck")
        self.assertEqual((recheck["t1_dirty"], recheck["t1_failures"], recheck["last_error"]), (True, 1, "GITHUB_READ"))

    def test_check_runs_cached_only_when_final(self) -> None:
        w = full_world()
        merge = sha(f"{ZARI}pr11merge")
        w.repos[ZARI]["check_runs"][merge] = [{"id": 3, "name": "lint", "status": "completed",
                                               "conclusion": "success", "app": {"id": 1, "slug": "gha"}}]
        _, doc = self.tick(w)
        self.assertEqual(doc["products"][ZARI]["nodes"]["N1"]["completion"]["merge_checks"], "PENDING")
        self.assertNotIn(f"{ZARI}@{merge}", self.state.read("cache")["checks"])
        w.repos[ZARI]["check_runs"][merge].append({"id": 4, "name": "bridge", "status": "completed",
                                                   "conclusion": "failure", "app": {"id": 1, "slug": "gha"}})
        w.ctrl_main = sha("ctrl-main-2")                        # force a T1
        _, doc = self.tick(w, NOW + timedelta(hours=1))
        self.assertEqual(doc["products"][ZARI]["nodes"]["N1"]["completion"]["merge_checks"], "FAIL")
        # a successful re-run is seen too
        w.repos[ZARI]["check_runs"][merge].append({"id": 5, "name": "bridge", "status": "completed",
                                                   "conclusion": "success", "app": {"id": 1, "slug": "gha"}})
        w.ctrl_main = sha("ctrl-main-3")
        _, doc = self.tick(w, NOW + timedelta(hours=2))
        self.assertEqual(doc["products"][ZARI]["nodes"]["N1"]["completion"]["merge_checks"], "PASS")
        self.assertIn(f"{ZARI}@{merge}", self.state.read("cache")["checks"])
        # a cached PASS no longer counts once the required list grows
        w.projects[ZARI]["program_required_checks"] = ["bridge", "e2e"]
        w.ctrl_main = sha("ctrl-main-4")
        _, doc = self.tick(w, NOW + timedelta(hours=3))
        self.assertEqual(doc["products"][ZARI]["nodes"]["N1"]["completion"]["merge_checks"], "PENDING")

    def test_pending_merge_checks_recheck_every_tick(self) -> None:
        w = full_world()
        merge = sha(f"{ZARI}pr11merge")
        w.repos[ZARI]["check_runs"][merge] = [{"id": 9, "name": "bridge", "status": "in_progress",
                                               "conclusion": None, "app": {"id": 1, "slug": "gha"}}]
        _, doc = self.tick(w)
        self.assertEqual(doc["products"][ZARI]["nodes"]["N1"]["completion"]["merge_checks"], "PENDING")
        w.repos[ZARI]["check_runs"][merge] = [{"id": 9, "name": "bridge", "status": "completed",
                                               "conclusion": "failure", "app": {"id": 1, "slug": "gha"}}]
        _, doc = self.tick(w, NOW + timedelta(hours=1))
        self.assertIsNotNone(doc)
        self.assertEqual(doc["products"][ZARI]["nodes"]["N1"]["completion"]["merge_checks"], "FAIL")
        # settled: back to the ordinary recheck time
        _, doc = self.tick(w, NOW + timedelta(hours=2))
        self.assertIsNone(doc)

    def test_idle_while_waiting_rechecks_until_the_span_is_covered(self) -> None:
        w = full_world()
        w.rows[(ZARI, "ZARI-N3")] = []                          # N3 NOT_STARTED, its only dependency is DONE
        docs = self.run_ticks(w, [0, 1, 2, 3])
        self.assertIsNotNone(docs[0])
        self.assertIsNotNone(docs[1])                           # second idle snapshot one hour later
        self.assertIsNone(docs[2])                              # two snapshots spanning 1 h: enough
        self.assertIsNone(docs[3])
        # without idle lanes nothing extra is collected
        w2 = full_world()
        w2.rows[(ZARI, "ZARI-N3")] = []
        for lane in w2.lanes["lanes"]:
            lane["enabled"] = False
        self.state = core.StateStore(Path(self.tmp.name) / "idle2")
        docs = self.run_ticks(w2, [0, 1])
        self.assertIsNone(docs[1])

    def test_truncated_exception_comments_are_unknown(self) -> None:
        w = full_world()
        marker = "ASTRA_CONSULT_V1 result=APPROVED_SMALL_EXCEPTION"
        url = f"https://api.github.com/repos/{ZARI}/issues/102"
        w.repos[ZARI]["comments"] = [{"id": i, "body": marker, "issue_url": url, "updated_at": iso(ago(hours=1))}
                                     for i in range(1, 502)]
        _, doc = self.tick(w)
        self.assertIn("comments", doc["products"][ZARI]["unknown"])

    def test_truncated_label_events_are_unknown(self) -> None:
        w = full_world()
        w.repos[ZARI]["issues"][3]["labels"].append({"name": "needs-user"})
        events = [{"event": "labeled", "label": {"name": "needs-user"}, "created_at": iso(ago(hours=80))}]
        events += [{"event": "commented", "created_at": iso(ago(hours=70))} for _ in range(300)]
        events += [{"event": "labeled", "label": {"name": "needs-user"}, "created_at": iso(ago(hours=2))}]
        w.repos[ZARI]["events"][104] = events
        _, doc = self.tick(w)
        self.assertIn("events", doc["products"][ZARI]["unknown"])
        self.assertIsNone(doc["products"][ZARI]["nodes"]["N4"]["blocked_since"])

    def test_label_since_per_blocking_label(self) -> None:
        w = full_world()
        w.repos[ZARI]["issues"][3]["labels"] += [{"name": "needs-user"}, {"name": "needs-lane-cleanup"}]
        w.repos[ZARI]["events"][104] = [
            {"event": "labeled", "label": {"name": "needs-user"}, "created_at": iso(ago(hours=72))},
            {"event": "labeled", "label": {"name": "needs-lane-cleanup"}, "created_at": iso(ago(hours=1))}]
        _, doc = self.tick(w)
        n4 = doc["products"][ZARI]["nodes"]["N4"]
        self.assertEqual(n4["blocked_since"], iso(ago(hours=72)))
        self.assertEqual(n4["label_since"], {"needs-lane-cleanup": iso(ago(hours=1)),
                                             "needs-user": iso(ago(hours=72))})
        self.assertIsNone(doc["products"][ZARI]["nodes"]["N3"]["label_since"])
        # the cleanup label's own 24 h crossing is a recheck time and moves the hash
        self.assertEqual(facts_mod.next_recheck_at(doc, NOW, config(daily_hour_kst=14)), NOW + timedelta(hours=23))
        later = copy.deepcopy(doc)
        later["collected_at"] = iso(NOW + timedelta(hours=23, minutes=30))
        self.assertNotEqual(facts_mod.hashes_of(later)["products"][ZARI],
                            facts_mod.hashes_of(doc)["products"][ZARI])
        self.assertNotIn("label_since", facts_mod.facts_core(doc)["products"][ZARI]["nodes"]["N4"])


    def test_deeply_nested_plan_is_invalid_and_collect_completes(self) -> None:
        # Hostile nesting (deeper than the recursion limit, and shallow enough to escape json but not
        # copy.deepcopy) is bad JSON: the plan is INVALID and T1 still commits.
        for depth in (2000, 500):
            w = World()
            commit = w.add_plan(KIXP, plan_doc(KIXP, "KIX", [node("A")], program="kixp"), ago(days=1))
            w.repos[KIXP]["plan_content"][commit] = (
                '{"schema_version":1,"x":' + "[" * depth + "]" * depth + "}").encode()
            self.state = core.StateStore(Path(self.tmp.name) / f"deep{depth}")
            _, doc = self.tick(w)
            self.assertIsNotNone(doc, depth)
            self.assertEqual(doc["products"][KIXP]["plan"]["state"], "INVALID", depth)
            self.assertIsNotNone(self.state.read("facts"), depth)

if __name__ == "__main__":
    unittest.main()
