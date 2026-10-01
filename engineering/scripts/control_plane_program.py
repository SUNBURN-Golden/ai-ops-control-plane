#!/usr/bin/env python3
"""Program-mode operations for the central control plane (docs/PROGRAM_MODE.md v2).

Every operation here is mechanical: it reads GitHub and the host ledger, applies
a fixed rule and records the result. Lane choice, task creation, review launch,
session release and merge readiness are computed, never judged.

Operations (one per workflow run, all under the runtime workflow's concurrency
group and the host ledger's own transactions):

  lanes        read-only lane board from the host ledger
  materialize  plan node -> exactly one canonical task issue (unknown-state safe)
  start        assign the first idle lane (or the owner lane) and prepare a writer launch
  review       pick the first idle non-owner lane and prepare a reviewer launch
  finalize-review  record a reviewer launch outcome in the control record
  reap         release a verified-terminal session; the host pins its signed marker
  merge-check  compute DISPATCH section 18 READY_FOR_MERGE for one PR head
  merge        User decision M1: merge a computed READY_FOR_MERGE, pinned to that exact head

Authority: every lane posts to GitHub with the same account, so GitHub text (issue
bodies, the control record comment, review bodies) is never a gate input. Gates read
the host ledger (signed, write-once pins; lanes; attempts), the plan at the host-recorded
plan commit, and live PR state. The control record is a projection for people and the
coordinator.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import control_plane as cp

LANE_ORDER = ("DEVIN", "GROK_BUILD", "GLM", "CURSOR")
# Lanes whose adapters take schema v2 packets and run without a per-user service manager.
# CURSOR's systemd-free adapter (User decision M3 = b) lives in adapters/cursor/.
PROGRAM_LANES = LANE_ORDER
TASK_LABEL = "aiops-task"
PLAN_PATH = ".aiops/program.json"
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[0-9a-f]{40}")
TASK_KEY_RE = re.compile(r"<!-- ASTRA_TASK_KEY_V1 program=(\S+) node=(\S+) request=([0-9a-f]{24}) -->")
# Session-signed marker lines; the host verifies the MAC (control_plane_host.verify_pin).
DELIVERY_RE = re.compile(r"ASTRA_DELIVERY_V1 pr=[1-9][0-9]{0,9} head=[0-9a-f]{40} mac=[0-9a-f]{64}")
REVIEW_RE = re.compile(r"ASTRA_REVIEW_V1 review=([0-9a-f]{24}) head=[0-9a-f]{40} "
                       r"verdict=(?:PASS|PASS_WITH_NOTES|FAIL|DECISION_REQUIRED) depth=A[0-3] "
                       r"required=A[1-3] contract_change=(?:NO|YES) mac=[0-9a-f]{64}")
BLOCKED_RE = re.compile(r"ASTRA_BLOCKED_V1 kind=(DECISION_REQUIRED|BLOCKED|STALLED) launch=([0-9a-f]{24}) "
                        r"mac=[0-9a-f]{64}")
MAX_REVIEW_SESSIONS = 3  # per review slot and delivered head; beyond this an operator looks
AUDIT_FLOORS = ("A0", "A1", "A2", "A3")
ASTRA_GATES = ("NONE", "MILESTONE", "ARCHITECTURE", "RELEASE")
DELIVERABLE_MODES = ("PR",)  # program mode completes a node only through a host-pinned, merged PR
REQUIRED_REVIEWS = {"A0": 0, "A1": 1, "A2": 2, "A3": 2}
BLOCKING_LABELS = {"needs-user", "blocked", "decision-required"}
FABLE_PROGRAM_COMMAND = ("/usr/bin/sudo", "-n", "/opt/aiops/bin/aiops-fable", "program")


class ProgramError(cp.ControlPlaneError):
    pass


def fable_program(operation: str, issue_number: int, **fields) -> Dict[str, Any]:
    """The root-owned bridge recomputes authority; only its stdout is a receipt.

    No token in argv, env preservation, free-form shell, model choice, file path,
    comment verdict or unlimited retry. Model audits may take up to three hours;
    check is a short receipt read. An interrupted admitted request stays UNKNOWN.
    """
    cfg = cp.load_config()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ProgramError("GITHUB_TOKEN is required")
    payload = {"operation": operation, "repository": cfg["repository"], "issue": issue_number,
               "github_token": token, **fields}
    try:
        result = subprocess.run(FABLE_PROGRAM_COMMAND, input=json.dumps(payload), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                timeout=28800 if operation == "quota-resume" else 14400 if operation in ("audit", "consult") else 120)
    except subprocess.TimeoutExpired:
        raise ProgramError("program Astra request timed out; reconcile it on the host; do not resubmit") from None
    except OSError:
        raise ProgramError("program Astra transport/cancellation failed; its root request may still be running; "
                           "reconcile it on the host; do not resubmit") from None
    try:
        answer = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise ProgramError("protected program Astra bridge returned no valid JSON; do not resubmit") from None
    if result.returncode or not isinstance(answer, dict) or answer.get("status") == "ERROR":
        reason = cp.program_error_reason(answer.get("reason", "inspect its host receipt"), (token,)) \
            if isinstance(answer, dict) else "inspect its host receipt"
        raise ProgramError("protected program Astra bridge failed: " + reason)
    return answer


def astra_audit(issue_number: int, pr_number: int, head: str) -> Dict[str, Any]:
    cp.require_runtime_enabled()
    return fable_program("audit", issue_number, pr=pr_number, head=head)


def quota_operation(operation: str, issue_number: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    cp.require_runtime_enabled()
    if set(fields) - {"pr_number", "head", "question_comment_id"}:
        raise ProgramError("quota operations accept only the original delivery or question selector")
    question = fields.get("question_comment_id")
    if question is not None:
        if "pr_number" in fields or "head" in fields:
            raise ProgramError("quota target cannot mix delivery and consultation")
        return fable_program(operation, issue_number, question=question)
    return fable_program(operation, issue_number, pr=fields.get("pr_number"), head=fields.get("head"))


def astra_consult(issue_number: int, question: int) -> Dict[str, Any]:
    cp.require_runtime_enabled()
    return fable_program("consult", issue_number, question=question)


def astra_receipt_matches(receipt, expected, gate, floor):
    return isinstance(receipt, dict) and receipt.get("status") == "POSTED" \
        and receipt.get("result") in ("PASS", "PASS_WITH_NOTES") \
        and receipt.get("scope_result") == "WITHIN_APPROVED_PLAN" \
        and receipt.get("program_binding") == expected and receipt.get("binding") == expected \
        and receipt.get("gate") == gate and receipt.get("verified_depth") in AUDIT_FLOORS \
        and AUDIT_FLOORS.index(receipt["verified_depth"]) >= AUDIT_FLOORS.index(floor) \
        and isinstance(receipt.get("comment_url"), str) and bool(receipt["comment_url"].strip())


# --------------------------------------------------------------------------- GitHub


def api_for(cfg: Dict[str, Any]) -> cp.GithubApi:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ProgramError("GITHUB_TOKEN is required")
    return cp.GithubApi(cfg["repository"], token)


def paginate(api: cp.GithubApi, path: str) -> List[Dict[str, Any]]:
    result, page = [], 1
    joiner = "&" if "?" in path else "?"
    while True:
        chunk = api._request("GET", f"{path}{joiner}per_page=100&page={page}")
        if not isinstance(chunk, list):
            raise ProgramError(f"GitHub list {path} returned a non-list")
        result.extend(chunk)
        if len(chunk) < 100:
            return result
        page += 1


def task_issues(api: cp.GithubApi) -> List[Dict[str, Any]]:
    """Exhaustive REST listing (never search): open and closed issues with the task label."""
    return [item for item in paginate(api, f"/issues?labels={TASK_LABEL}&state=all") if "pull_request" not in item]


def issue_key(issue: Dict[str, Any]) -> Optional[tuple]:
    match = TASK_KEY_RE.search(issue.get("body") or "")
    return match.groups() if match else None


# --------------------------------------------------------------------------- host


def host(arguments: List[str], document: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    report = cp.host_call(arguments, document)
    if report.get("status") == "ERROR":
        raise ProgramError(f"host refused: {report.get('reason')}")
    return report


def host_lanes() -> Dict[str, Any]:
    board = host(["status", "--lanes"])
    if (not isinstance(board.get("lanes"), list) or type(board.get("active_total")) is not int
            or type(board.get("max_active_sessions")) is not int):
        raise ProgramError("host lane board is malformed")
    return board


def preflight_ok(lane: str) -> bool:
    try:
        cp.host_preflight(lane)
        return True
    except cp.ControlPlaneError:
        return False


def select_lane(board: Dict[str, Any], cfg: Dict[str, Any], exclude: Iterable[str] = (),
                preflight: Optional[Callable[[str], bool]] = None) -> Optional[str]:
    """First idle lane in the fixed order; None when no lane can take work now."""
    if board["active_total"] >= board["max_active_sessions"]:
        return None
    lanes = {entry.get("lane"): entry for entry in board["lanes"]}
    excluded = set(exclude)
    for lane in LANE_ORDER:
        entry = lanes.get(lane)
        if (lane in excluded or lane not in PROGRAM_LANES or lane not in cfg["enabled_builders"] or not entry
                or entry.get("enabled") is not True or entry.get("active")):
            continue
        if preflight is not None and not preflight(lane):
            continue
        return lane
    return None


def active_rows_for_task(board: Dict[str, Any], repository: str, task_id: str) -> List[Dict[str, Any]]:
    return [row for entry in board["lanes"] for row in entry.get("active", [])
            if row.get("repository") == repository and row.get("task") == task_id]


# --------------------------------------------------------------------------- plan


def load_plan(api: cp.GithubApi, cfg: Dict[str, Any], plan_commit: str) -> Dict[str, Any]:
    if not SHA.fullmatch(plan_commit or ""):
        raise ProgramError("plan_commit must be a 40-hex commit SHA")
    content = api._request("GET", f"/contents/{PLAN_PATH}?ref={plan_commit}")
    try:
        plan = json.loads(base64.b64decode(content["content"]).decode("utf-8"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ProgramError(f"{PLAN_PATH} at {plan_commit} is unreadable") from exc
    if isinstance(plan, dict) and "PENDING" in str(plan.get("approval_pointer", "")).upper():
        raise ProgramError("program scope/start approval is pending; do not dispatch")
    return validate_plan(plan, cfg)


def validate_plan(plan: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ProgramError("program plan must be schema_version 1")
    if not SAFE.fullmatch(str(plan.get("program", ""))):
        raise ProgramError("program key must be a safe identifier")
    if plan.get("repository") != cfg["repository"] or plan.get("project") != cfg["project"]:
        raise ProgramError("program plan targets a different repository or project")
    for field in ("approval_pointer", "authoritative_doc_pointers"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ProgramError(f"program plan needs {field}")
    nodes = plan.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ProgramError("program plan needs nodes")
    ids = [node.get("id") if isinstance(node, dict) else None for node in nodes]
    if any(not isinstance(i, str) or not SAFE.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
        raise ProgramError("node ids must be unique safe identifiers")
    if len({task_id_for(plan["program"], i) for i in ids}) != len(ids):
        raise ProgramError("node ids must stay unique as TASK_IDs (case-insensitive)")
    for node in nodes:
        if not isinstance(node.get("title"), str) or not node["title"].strip():
            raise ProgramError(f"node {node['id']} needs a title")
        if not isinstance(node.get("spec"), str) or not node["spec"].strip():
            raise ProgramError(f"node {node['id']} needs a spec")
        deps = node.get("depends_on", [])
        if not isinstance(deps, list) or any(dep not in ids or dep == node["id"] for dep in deps):
            raise ProgramError(f"node {node['id']} has invalid depends_on")
        # Schema v1 has no adopted cross-repository completion reader. Even an
        # empty field must not look like supported/consumed dependency evidence.
        if "depends_on_external" in node:
            raise ProgramError(f"node {node['id']}: depends_on_external is not supported by active schema v1; "
                               "keep cross-repository dependencies in the pending catalogue until adopted")
        node.setdefault("audit_floor", "A1")
        node.setdefault("astra_gate", "NONE")
        node.setdefault("deliverable_mode", "PR")
        if node["audit_floor"] not in AUDIT_FLOORS or node["astra_gate"] not in ASTRA_GATES:
            raise ProgramError(f"node {node['id']} has an invalid gate field")
        if node["deliverable_mode"] not in DELIVERABLE_MODES:
            raise ProgramError(f"node {node['id']}: program mode supports deliverable_mode PR only")
        for flag in ("user_merge", "astra_auto_merge"):
            if flag in node and type(node[flag]) is not bool:
                raise ProgramError(f"node {node['id']} has invalid {flag}")
        if node.get("user_merge") is True and node.get("astra_auto_merge") is True:
            raise ProgramError(f"node {node['id']}: User-only merge cannot also delegate its Astra merge")
        # Check the declared gate before A3 normalizes it to ARCHITECTURE.
        if node["astra_gate"] == "RELEASE" and node.get("astra_auto_merge") is True:
            raise ProgramError(f"node {node['id']}: RELEASE merge cannot be delegated")
        if node["audit_floor"] == "A0":
            # Program mode has no A0 qualification path (DISPATCH section 16: authorization pointer,
            # path contract, attestation), so A0 is promoted to A1 and gets its independent review.
            node["audit_floor"] = "A1"
        if node["audit_floor"] == "A3" and node["astra_gate"] != "RELEASE":
            node["astra_gate"] = "ARCHITECTURE"  # AGENTS section 8: A3 implies the architecture gate
    # Reject dependency cycles.
    graph = {node["id"]: set(node.get("depends_on", [])) for node in nodes}
    seen: set = set()
    while graph:
        ready = [key for key, deps in graph.items() if deps <= seen]
        if not ready:
            raise ProgramError("program plan has a dependency cycle")
        for key in ready:
            seen.add(key)
            del graph[key]
    return plan


def plan_node(plan: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    for node in plan["nodes"]:
        if node["id"] == node_id:
            return node
    raise ProgramError(f"node {node_id} is not in the plan")


def require_descendant(api: cp.GithubApi, old: str, new: str) -> None:
    """Refuse a plan commit that does not descend from the recorded one (STALE_PLAN)."""
    if old == new:
        return
    comparison = api._request("GET", f"/compare/{old}...{new}")
    if (comparison or {}).get("status") not in ("ahead", "identical"):
        raise ProgramError(f"STALE_PLAN: {new} does not descend from recorded plan {old}")


def default_branch(api: cp.GithubApi) -> str:
    branch = (api._request("GET", "") or {}).get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ProgramError("repository default branch is unknown")
    return branch


def require_on_default_branch(api: cp.GithubApi, commit: str) -> None:
    """The plan must be merged: the commit is the default branch head or one of its ancestors."""
    branch = default_branch(api)
    comparison = api._request("GET", f"/compare/{branch}...{commit}")
    if (comparison or {}).get("status") not in ("behind", "identical"):
        raise ProgramError(f"PLAN_NOT_MERGED: {commit} is not on the default branch {branch}")


# --------------------------------------------------------------------------- task body


def task_id_for(program: str, node: str) -> str:
    return f"{program}-{node}".upper()


def branch_for(task_id: str) -> str:
    """The only branch a task's delivery may come from, so one PR can never serve two tasks."""
    return f"astra/{task_id.lower()}"


def task_revision_for(plan_commit: str, lane: str) -> str:
    return f"p{plan_commit[:12]}-{lane}"


def quote_field_lines(text: str) -> str:
    # Spec text must never inject envelope fields: quote any line the parser would read.
    return "\n".join(("> " + line) if cp.FIELD_RE.match(line.strip()) else line for line in text.splitlines())


def key_line(program: str, node: str, request: str) -> str:
    return f"<!-- ASTRA_TASK_KEY_V1 program={program} node={node} request={request} -->"


def placeholder_body(program: str, node: str, request: str, title: str) -> str:
    return "\n".join([key_line(program, node, request), "", f"# {title}", "",
                      "Task envelope is written by the control plane when a lane is assigned. Do not dispatch."])


def render_task(plan: Dict[str, Any], node: Dict[str, Any], plan_commit: str, issue_url: str,
                request: str, lane: str) -> str:
    program, repo = plan["program"], plan["repository"]
    plan_url = f"https://github.com/{repo}/blob/{plan_commit}/{PLAN_PATH}"
    fields = [
        ("TASK_ID", task_id_for(program, node["id"])),
        ("PROJECT", plan["project"]),
        ("REPO", repo),
        ("CANONICAL_TASK_POINTER", issue_url),
        ("TASK_REVISION", task_revision_for(plan_commit, lane)),
        ("TASK_SPEC_POINTER", issue_url + "#specification"),
        ("TASK_SPEC_REVISION", plan_commit[:12]),
        ("APPROVAL_POINTER", plan["approval_pointer"]),
        ("AUTHORITATIVE_DOC_POINTERS", f"{plan['authoritative_doc_pointers']} ; {plan_url}"),
        ("EXECUTION_CLASS", "BUILDER_STANDARD"),
        ("BUILDER_ID", lane),
        ("DELIVERABLE_MODE", node["deliverable_mode"]),
        ("AUDIT_FLOOR", node["audit_floor"]),
        ("ASTRA_GATE", node["astra_gate"]),
        ("REVIEW_POLICY", "REQUIRED_NON_A0"),
        ("REVIEWER_LANE_ID", "FIRST_IDLE_EXCLUDING_OWNER"),
        ("CONTROL_RECORD_POINTER", issue_url),
        ("PROGRAM_KEY", f"{program}/{node['id']}"),
        ("PLAN_COMMIT", plan_commit),
    ]
    delivery = (f"Deliverable protocol (program mode): work on the branch `{branch_for(task_id_for(program, node['id']))}` "
                "and open the pull request from it as ready for review, not a draft, so this repository's required "
                "checks run on its head (program mode authorizes this). When it is ready, run this session's signer "
                "(the session prompt gives the command) and post one comment on this issue containing the single "
                "`ASTRA_DELIVERY_V1 ... mac=...` line it prints, then end the session. If blocked, post the signer's "
                "`ASTRA_BLOCKED_V1 ... mac=...` line (DECISION_REQUIRED, BLOCKED or STALLED) with the reason and end "
                "the session; unsigned markers are ignored. Do not merge.")
    return "\n".join([
        key_line(program, node["id"], request), "", "# TASK ENVELOPE v4", "",
        *[f"{name}: {value}" for name, value in fields], "",
        "## Specification", "", f"### {node['title']}", "", quote_field_lines(node["spec"]), "",
        "## Owner instruction", "",
        "Follow the pinned task and repository rules. Investigate, implement, test/fix/retest and deliver "
        "without routine plan approval. Stop with DECISION_REQUIRED for any consequential change outside the "
        "approved boundaries.", "", delivery,
    ])


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def author(item: Dict[str, Any]) -> str:
    return ((item.get("user") or {}).get("login") or "")


# --------------------------------------------------------------------------- materialize


def open_duplicates(api: cp.GithubApi, cfg: Dict[str, Any], program: str, node_id: str) -> List[int]:
    return sorted(i["number"] for i in task_issues(api)
                  if i.get("state") == "open" and author(i) in cfg["allowed_task_actors"]
                  and (issue_key(i) or ())[:2] == (program, node_id))


def require_single_canonical(api: cp.GithubApi, cfg: Dict[str, Any], program: str, node_id: str, issue: int) -> None:
    found = open_duplicates(api, cfg, program, node_id)
    if found not in ([issue], []):
        raise ProgramError(f"DUPLICATE_TASK: {program}/{node_id} has open issues {found}; canonical is #{issue}; "
                           "operator must close the others before dispatch")


def materialize(program: str, node_id: str, plan_commit: str) -> Dict[str, Any]:
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    api = api_for(cfg)
    plan = load_plan(api, cfg, plan_commit)
    if plan["program"] != program:
        raise ProgramError("plan program key does not match the request")
    node = plan_node(plan, node_id)
    require_on_default_branch(api, plan_commit)
    begin = host(["materialize-begin", "--program", program, "--node", node_id,
                  "--repository", cfg["repository"], "--plan-commit", plan_commit])
    request = begin["request"]
    if begin["decision"] == "CREATED":
        issue = begin["issue"]
    elif begin["decision"] == "UNRESOLVED":
        # Never create again: "not in the list" is not proof of non-creation.
        found = [i for i in task_issues(api) if issue_key(i) == (program, node_id, request)
                 and author(i) in cfg["allowed_task_actors"]]
        if not found:
            return {"status": "MATERIALIZE_UNKNOWN", "program": program, "node": node_id, "request": request}
        issue = min(i["number"] for i in found)
        host(["materialize-finish", "--program", program, "--node", node_id, "--request", request,
              "--outcome", "CREATED", "--issue", str(issue)])
    else:
        body = placeholder_body(program, node_id, request, node["title"])
        try:
            created = api._request("POST", "/issues", {"title": f"[{task_id_for(program, node_id)}] {node['title']}",
                                                       "body": body, "labels": [TASK_LABEL]})
            issue = int(created["number"])
        except (cp.ControlPlaneError, KeyError, TypeError, ValueError):
            host(["materialize-finish", "--program", program, "--node", node_id, "--request", request,
                  "--outcome", "UNKNOWN"])
            return {"status": "MATERIALIZE_UNKNOWN", "program": program, "node": node_id, "request": request}
        host(["materialize-finish", "--program", program, "--node", node_id, "--request", request,
              "--outcome", "CREATED", "--issue", str(issue)])
        if TASK_LABEL not in {label.get("name") for label in created.get("labels", [])}:
            # Recovery lists by label; never leave a canonical issue unlabeled.
            api._request("POST", f"/issues/{issue}/labels", {"labels": [TASK_LABEL]})
    require_single_canonical(api, cfg, program, node_id, issue)
    return {"status": "CREATED", "program": program, "node": node_id, "request": request, "issue": issue}


# --------------------------------------------------------------------------- host task view


def control_record(api: cp.GithubApi, cfg: Dict[str, Any], issue_number: int):
    comment = cp.find_control_comment(api.comments(issue_number), cfg["control_record_actor"])
    return comment, (cp.parse_control_record(comment.get("body") or "") if comment else None)


def task_rows(cfg: Dict[str, Any], task_id: str) -> List[Dict[str, Any]]:
    """Every host launch row of one task, oldest first: the authority for owners, pins and reviews."""
    report = host(["task-status", "--repository", cfg["repository"], "--task", task_id])
    rows = report.get("rows")
    if (report.get("repository"), report.get("task")) != (cfg["repository"], task_id) or not isinstance(rows, list) \
            or not all(isinstance(row, dict) for row in rows):
        raise ProgramError("host task status is malformed")
    return rows


def released(row: Optional[Dict[str, Any]]) -> bool:
    return bool(row) and row.get("state") == "RECONCILED" and row.get("resolution") == cp.VERIFIED_RELEASE


def pin_of(row: Optional[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    pin = (row or {}).get("pin") if released(row) else None
    return pin if isinstance(pin, dict) and pin.get("kind") == kind else None


def writer_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # A prestart failure never had a session, so it neither owns the task nor is an attempt to review.
    return [row for row in rows if row.get("role") == "WRITER" and row.get("state") != "FAILED_PRESTART"]


def writer_lanes(rows: List[Dict[str, Any]]) -> set:
    return {row.get("lane") for row in writer_rows(rows)}


def current_writer(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    writers = writer_rows(rows)
    return writers[-1] if writers else None


def review_rows(rows: List[Dict[str, Any]], review_id: str) -> List[Dict[str, Any]]:
    return [row for row in rows if row.get("role") == "REVIEWER" and row.get("review_request_id") == review_id]


def expected_revision(mstatus: Dict[str, Any], writer: Dict[str, Any]) -> str:
    """The task revision the current envelope names; host rows of any other revision are stale."""
    return task_revision_for(mstatus["plan_commit"], writer["lane"])


def current_verdicts(cfg: Dict[str, Any], task_id: str, rows: List[Dict[str, Any]], writer: Dict[str, Any],
                     head: str) -> List[tuple]:
    """(row, verdict) for every host-pinned verdict of this delivery's review ids at this head."""
    found = []
    for slot in (1, 2):
        review_id = review_request_id(cfg["repository"], task_id, writer["launch_request_id"], head, slot)
        for row in review_rows(rows, review_id):
            verdict = pin_of(row, "REVIEW")
            if verdict is not None and verdict["head"] == head:
                found.append((row, verdict))
    return found


def effective_floor(plan_floor: str, verdicts: List[tuple]) -> str:
    """DISPATCH section 13: max(configured AUDIT_FLOOR, every VERIFIED_REQUIRED_DEPTH), A0 < A1 < A2 < A3."""
    return max([plan_floor] + [verdict["required"] for _, verdict in verdicts], key=AUDIT_FLOORS.index)


def merged_delivery(api: cp.GithubApi, pin: Optional[Dict[str, Any]]) -> bool:
    """Merge existence only, never a dependency/completion predicate."""
    if pin is None:
        return False
    pr = api._request("GET", f"/pulls/{pin['pr']}")
    return pr.get("merged") is True and (pr.get("head") or {}).get("sha") == pin["head"]


def post_merge_workflow_runs(api: cp.GithubApi, branch: str, merge_sha: str) -> List[Dict[str, Any]]:
    """Actual main/push runs, not mutable evidence prose or PR checks at another SHA."""
    result, page = [], 1
    while True:
        value = api._request("GET", f"/actions/runs?event=push&branch={branch}&head_sha={merge_sha}"
                             f"&per_page=100&page={page}")
        runs = value.get("workflow_runs") if isinstance(value, dict) else None
        if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
            raise ProgramError("post-merge workflow list is malformed")
        result.extend(run for run in runs if run.get("event") == "push" and run.get("head_branch") == branch
                      and run.get("head_sha") == merge_sha)
        if len(runs) < 100:
            return result
        page += 1


def delivery_completion(api: cp.GithubApi, cfg: Dict[str, Any], pin: Optional[Dict[str, Any]],
                        task_id: str) -> Dict[str, Any]:
    """Project completion and readiness at an exact delivery/merge, DISPATCH §20.

    There is no adopted protected post-merge failure/follow-up issuer yet. A
    failed KIX verification therefore holds the original and downstream here;
    E04 can later expose the independently recorded failure+corrective task for
    original-task closure without granting downstream product readiness.
    """
    if pin is None:
        return {"status": "NOT_MERGED"}
    pr = api._request("GET", f"/pulls/{pin['pr']}")
    if pr.get("merged") is not True:
        return {"status": "NOT_MERGED"}
    merge_sha = pr.get("merge_commit_sha")
    branch = default_branch(api)
    reasons = []
    if (pr.get("head") or {}).get("sha") != pin["head"]:
        reasons.append("merged PR head differs from the host-pinned delivery")
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != cfg["repository"] \
            or (pr.get("head") or {}).get("ref") != branch_for(task_id) \
            or (pr.get("base") or {}).get("ref") != branch:
        reasons.append("merged delivery does not name this task/repository/default target")
    if not isinstance(merge_sha, str) or not SHA.fullmatch(merge_sha):
        reasons.append("merged delivery has no exact merge commit")
    else:
        try:
            require_on_default_branch(api, merge_sha)
        except ProgramError:
            reasons.append("merged delivery commit is not on the default target lineage")
    result = {"status": "MERGED_POST_VERIFY", "pr": pin["pr"], "head": pin["head"],
              "merge_commit": merge_sha, "reasons": reasons}
    if reasons:
        return result  # an already merged task must not be redispatched either
    if cfg["repository"] != "BeautifulMind-JT/kix-protocol":
        return {**result, "status": "DONE"}  # no required post-merge phase for these products
    checks = latest_check_runs(all_check_runs(api, merge_sha))
    statuses = api._request("GET", f"/commits/{merge_sha}/status") or {}
    if any(check.get("status") == "completed" and check.get("conclusion")
           not in ("success", "neutral", "skipped") for check in checks) \
            or statuses.get("statuses") and statuses.get("state") in ("failure", "error"):
        return {**result, "status": "POST_MERGE_FAILED",
                "reasons": ["KIX post-merge CI failed; durable failure and corrective task evidence required"]}
    reasons.extend(verification_reasons(api, cfg, merge_sha))
    push_runs = post_merge_workflow_runs(api, branch, merge_sha)
    newest = {}
    for run in push_runs:
        key = run.get("workflow_id", run.get("path"))
        order = (run.get("run_number", 0), run.get("run_attempt", 1), run.get("id", 0))
        if key not in newest or order > newest[key][0]:
            newest[key] = (order, run)
    push_runs = [run for _, run in newest.values()]
    if any(run.get("status") == "completed" and run.get("conclusion")
           not in ("success", "neutral", "skipped") for run in push_runs):
        return {**result, "status": "POST_MERGE_FAILED",
                "reasons": ["KIX exact main/push workflow failed; corrective task evidence required"]}
    verified_suites = {run.get("check_suite_id") for run in push_runs
                       if run.get("status") == "completed" and run.get("conclusion") == "success"
                       and type(run.get("check_suite_id")) is int}
    for name in cfg.get("program_required_checks", []):
        required = [check for check in checks if check.get("name") == name]
        if not required or any((check.get("check_suite") or {}).get("id") not in verified_suites
                               for check in required):
            reasons.append(f"KIX post-merge required check {name!r} lacks successful exact main/push origin")
    # Expected blobs are reviewed profile bytes bound by the installed runtime;
    # never parse attacker-editable AGENTS or accept caller-selected baselines.
    locked = cfg.get("program_post_merge_locked_blobs")
    locked_paths = {"runtime/crates/kix-kernel/src/lib.rs",
                    "runtime/crates/kix-kernel/tests/quarantine_capacity.rs"}
    if not isinstance(locked, dict) or set(locked) != locked_paths or not all(
            isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)
            and all(part not in (".", "..") for part in path.split("/"))
            and isinstance(blob, str) and SHA.fullmatch(blob) for path, blob in (locked or {}).items()):
        reasons.append("KIX protected post-merge locked-blob baseline is missing or invalid")
    else:
        for path, blob in locked.items():
            value = api._request("GET", f"/contents/{path}?ref={merge_sha}")
            try:
                raw = base64.b64decode("".join(value["content"].split()), validate=True)
                actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
                valid = value.get("type") == "file" and value.get("encoding") == "base64" \
                    and value.get("sha") == blob and actual == blob
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                return {**result, "status": "POST_MERGE_FAILED",
                        "reasons": [f"KIX post-merge locked blob differs at {path}"]}
    return {**result, "status": "MERGED_POST_VERIFY" if reasons else "DONE", "reasons": reasons}


def task_context(api: cp.GithubApi, cfg: Dict[str, Any], issue_number: int):
    """The canonical issue, the plan at the host-recorded plan commit, its node and the host record."""
    issue = api.issue(issue_number)
    key = issue_key(issue)
    if not key:
        raise ProgramError("task issue carries no ASTRA_TASK_KEY_V1")
    program, node_id, request = key
    status = host(["materialize-status", "--program", program, "--node", node_id])
    if (status.get("status") != "CREATED" or status.get("issue") != issue_number or status.get("request") != request
            or status.get("repository") != cfg["repository"]):
        raise ProgramError("task issue is not the host-recorded canonical issue of its plan node")
    plan = load_plan(api, cfg, status["plan_commit"])
    if plan["program"] != program:
        raise ProgramError("plan program key does not match the task key")
    return issue, plan, plan_node(plan, node_id), status


def issue_task_id(api: cp.GithubApi, cfg: Dict[str, Any], issue_number: int) -> str:
    """TASK_ID of a canonical issue, from its key and the host materialization (never the record)."""
    key = issue_key(api.issue(issue_number))
    if not key:
        raise ProgramError("task issue carries no ASTRA_TASK_KEY_V1")
    status = host(["materialize-status", "--program", key[0], "--node", key[1]])
    if (status.get("status") != "CREATED" or status.get("issue") != issue_number or status.get("request") != key[2]
            or status.get("repository") != cfg["repository"]):
        raise ProgramError("task issue is not the host-recorded canonical issue of its plan node")
    return task_id_for(key[0], key[1])


def rendered_body(issue: Dict[str, Any], plan: Dict[str, Any], node: Dict[str, Any], status: Dict[str, Any],
                  lane: str) -> str:
    return render_task(plan, node, status["plan_commit"], issue["html_url"], status["request"], lane)


def dependency_done(api: cp.GithubApi, cfg: Dict[str, Any], plan: Dict[str, Any], node_id: str) -> bool:
    """Downstream readiness, including the product's required post-merge phase."""
    status = host(["materialize-status", "--program", plan["program"], "--node", node_id])
    if status.get("status") != "CREATED" or status.get("repository") != cfg["repository"]:
        return False
    tid = task_id_for(plan["program"], node_id)
    return delivery_completion(api, cfg, pin_of(current_writer(task_rows(cfg, tid)), "DELIVERY"), tid)["status"] == "DONE"


def pending_dependencies(api: cp.GithubApi, cfg: Dict[str, Any], plan: Dict[str, Any],
                         node: Dict[str, Any]) -> List[str]:
    return [dep for dep in node.get("depends_on", []) if not dependency_done(api, cfg, plan, dep)]


# --------------------------------------------------------------------------- start (writer)


def start(issue_number: int, program: str, node_id: str, plan_commit: str, packet_path: Path,
          *, preflight: Callable[[str], bool] = preflight_ok) -> Dict[str, Any]:
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    api = api_for(cfg)
    status = host(["materialize-status", "--program", program, "--node", node_id])
    if status.get("status") != "CREATED" or status.get("issue") != issue_number:
        raise ProgramError("program node has no CREATED canonical issue matching issue_number")
    # Validate the new plan fully first; the host plan commit advances only right before the
    # envelope is rewritten, so a refused or waiting start never strands a stale body.
    plan = load_plan(api, cfg, plan_commit)
    node = plan_node(plan, node_id)
    require_on_default_branch(api, plan_commit)
    if status["plan_commit"] != plan_commit:
        require_descendant(api, status["plan_commit"], plan_commit)
    issue = api.issue(issue_number)
    if issue.get("state") != "open" or issue_key(issue) != (program, node_id, status["request"]):
        raise ProgramError("canonical issue is closed or its task key does not match the host record")
    require_single_canonical(api, cfg, program, node_id, issue_number)
    tid = task_id_for(program, node_id)
    completion = delivery_completion(api, cfg, pin_of(current_writer(task_rows(cfg, tid)), "DELIVERY"), tid)
    if completion["status"] != "NOT_MERGED":
        # Merge is terminal for the writer. Incomplete/failed post-merge
        # verification holds successors and never restarts the original writer.
        cp.write_github_output("launch_required", "false")
        return {**completion, "issue": issue_number}
    pending = pending_dependencies(api, cfg, plan, node)
    if pending:
        cp.write_github_output("launch_required", "false")
        return {"status": "WAITING_ON_DEPENDENCIES", "issue": issue_number, "pending": pending}
    _, record = control_record(api, cfg, issue_number)
    board = host_lanes()
    if active_rows_for_task(board, cfg["repository"], tid):
        cp.write_github_output("launch_required", "false")
        return {"status": "TASK_ACTIVE", "issue": issue_number}
    previous_writer = current_writer(task_rows(cfg, tid))
    terminal_writer = bool(previous_writer) and previous_writer.get("state") == "RECONCILED" \
        and previous_writer.get("resolution") in cp.TERMINAL_RELEASES
    if terminal_writer:
        # Ask before advancing the host plan/body or reserving a lane. The root
        # bridge validates a proposed approved revision; execution ambiguity is
        # fenced across revisions, while a semantic User decision is exact-scope.
        decision = fable_program("decision-status", issue_number, plan_commit=plan_commit)
        if decision.get("status") != "CLEAR":
            raise ProgramError("Fable decision is unresolved or USER_REQUIRED; an approved plan revision is required")
    attempt = None
    if record is not None:
        state = record["launch_state"]
        host_state = cp.host_request_status(record["launch_request_id"]) if state != "NOT_STARTED" else {}
        if state in {"SUBMITTING", "UNKNOWN"}:
            # Resumable only when the host has fenced that request (prestart failure or release).
            if host_state.get("state") not in ("FAILED_PRESTART", "RECONCILED"):
                raise ProgramError(f"unresolved {state} launch; operator reconciliation required")
        if state == "CONFIRMED":
            if host_state.get("state") == "CONFIRMED":
                cp.write_github_output("launch_required", "false")
                return {"status": "OWNER_LIVE", "issue": issue_number}
            if host_state.get("resolution") not in cp.TERMINAL_RELEASES:
                raise ProgramError(f"NEEDS_OPERATOR: record CONFIRMED but host is {host_state}")
        if state != "NOT_STARTED":
            attempt = cp.record_attempt_id(record) + 1
    # Ownership comes only from the host: the lane of the task's first writer session.
    writers = writer_rows(task_rows(cfg, tid))
    owner = writers[0]["lane"] if writers else None
    # A NOT_STARTED record is a pending action already bound to its lane; a
    # confirmed task keeps its owner lane. Otherwise take the first idle lane.
    pinned = owner or (record["builder_id"] if record and record["launch_state"] == "NOT_STARTED" else None)
    if pinned:
        lane = pinned if select_lane(board, cfg, preflight=None, exclude=set(LANE_ORDER) - {pinned}) else None
        if lane and not preflight(lane):
            lane = None
    else:
        lane = select_lane(board, cfg, preflight=preflight)
    if lane is None:
        cp.write_github_output("launch_required", "false")
        return {"status": "NO_IDLE_LANE", "issue": issue_number, "owner_lane": owner}
    revision = task_revision_for(plan_commit, lane)
    if record is not None and record["launch_state"] == "NOT_STARTED" and record.get("task_revision") != revision:
        # The pending action was rendered from an older plan: supersede it as a new attempt.
        attempt = cp.record_attempt_id(record) + 1
    if status["plan_commit"] != plan_commit:
        host(["materialize-plan", "--program", program, "--node", node_id,
              "--from", status["plan_commit"], "--to", plan_commit])
    body = render_task(plan, node, plan_commit, issue["html_url"], status["request"], lane)
    if (issue.get("body") or "") != body:
        api._request("PATCH", f"/issues/{issue_number}", {"body": body})  # revise: same task group, no session
    expected = {"EXPECTED_TASK_ID": tid, "EXPECTED_TASK_REVISION": revision,
                "EXPECTED_BUILDER_ID": lane, "EXPECTED_ISSUE_BODY_SHA256": sha256_text(body)}
    if attempt is not None:
        os.environ["EXPECTED_ATTEMPT_ID"] = str(attempt)
    else:
        os.environ.pop("EXPECTED_ATTEMPT_ID", None)
    cp.prepare_dispatch(issue_number, packet_path, expected)
    return {"status": "PREPARED", "issue": issue_number, "lane": lane, "attempt": attempt or 1}


# --------------------------------------------------------------------------- review


def review_request_id(repository: str, task_id: str, writer_request: str, head: str, slot: int) -> str:
    # Bound to the host writer attempt that delivered this head, not to editable record fields.
    material = f"review\0{repository}\0{task_id}\0{writer_request}\0{head}\0{slot}"
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def review_launch_id(review_id: str, attempt: int) -> str:
    return hashlib.sha256(f"review-launch\0{review_id}\0{attempt}".encode()).hexdigest()[:24]


def live_delivered_pr(api: cp.GithubApi, cfg: Dict[str, Any], delivery: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    """The host-pinned delivery, checked against the live PR (same repo, default base, same head)."""
    pr = api._request("GET", f"/pulls/{delivery['pr']}")
    head = (pr.get("head") or {})
    if (pr.get("state") != "open" or head.get("sha") != delivery["head"]
            or (head.get("repo") or {}).get("full_name") != cfg["repository"]
            or head.get("ref") != branch_for(task_id)
            or (pr.get("base") or {}).get("ref") != default_branch(api)):
        raise ProgramError("delivered PR is not open at the delivered head, from this task's branch in this "
                           "repository, against the default branch; the writer must deliver again")
    return pr


def project_review(record: Dict[str, Any], row: Dict[str, Any], slot: Optional[int] = None) -> Dict[str, Any]:
    """Mirror one host reviewer row into the record's reviews[] (a projection only)."""
    reviews = record.setdefault("reviews", [])
    entry = next((r for r in reviews if r.get("launch_request_id") == row["launch_request_id"]), None)
    if entry is None:
        entry = {"launch_request_id": row["launch_request_id"], "review_request_id": row.get("review_request_id"),
                 "slot": slot, "lane": row.get("lane"), "attempt": row.get("attempt_id"),
                 "head_sha": row.get("head_sha"), "pr": row.get("pr_number"), "session_id": None,
                 "confirmed_at": None, "verdict": None, "last_error": None}
        reviews.append(entry)
    state = {"RECONCILED": "RELEASED"}.get(row.get("state"), row.get("state"))
    entry.update(state=state, verdict=pin_of(row, "REVIEW"))
    return entry


def prepare_review(issue_number: int, slot: int, packet_path: Path,
                   *, preflight: Callable[[str], bool] = preflight_ok) -> Dict[str, Any]:
    if slot not in (1, 2):
        raise ProgramError("review slot must be 1 or 2")
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    api = api_for(cfg)
    issue, plan, node, mstatus = task_context(api, cfg, issue_number)
    tid = task_id_for(plan["program"], node["id"])
    rows = task_rows(cfg, tid)
    writer = current_writer(rows)
    delivery = pin_of(writer, "DELIVERY")
    if delivery is None:
        raise ProgramError("review waits for a host-pinned ASTRA_DELIVERY_V1 of the released current writer attempt")
    if writer.get("task_revision") != expected_revision(mstatus, writer):
        raise ProgramError("the delivered writer attempt is for an older task revision; start delivers the "
                           "current revision first")
    body = rendered_body(issue, plan, node, mstatus, writer["lane"])
    if (issue.get("body") or "") != body:
        # The envelope is fully determined by the host record and the plan: restore it, never trust it.
        # This runs before the floor check, so an A0 task (no review slot) is repaired too.
        api._request("PATCH", f"/issues/{issue_number}", {"body": body})
        issue = {**issue, "body": body}
    # A reviewer's verified required depth can raise the floor, and with it the second slot.
    floor = effective_floor(node["audit_floor"], current_verdicts(cfg, tid, rows, writer, delivery["head"]))
    if REQUIRED_REVIEWS[floor] < slot:
        raise ProgramError(f"effective audit floor {floor} does not require review slot {slot}")
    comment, record = control_record(api, cfg, issue_number)
    if record is None:
        raise ProgramError("task has no control record")
    live_delivered_pr(api, cfg, delivery, tid)
    head, pr_number = delivery["head"], delivery["pr"]
    review_id = review_request_id(cfg["repository"], tid, writer["launch_request_id"], head, slot)
    mine = review_rows(rows, review_id)
    active = [row for row in mine if row.get("state") in ("SUBMITTING", "CONFIRMED", "UNKNOWN")]
    if active or any(pin_of(row, "REVIEW") for row in mine):
        for row in mine:
            project_review(record, row, slot)
        api.update_comment(comment["id"], cp.render_control_record(record))
        if any(row.get("state") in ("SUBMITTING", "UNKNOWN") for row in active):
            raise ProgramError(f"review {review_id} is unresolved on the host; operator reconciliation required")
        cp.write_github_output("launch_required", "false")
        return {"status": "REVIEW_EXISTS", "review_request_id": review_id}
    # No session and no verdict yet (a prestart failure, or a session released on its own signed
    # blocker or by an operator): a new attempt, a bounded number of times.
    sessions = [row for row in mine if row.get("state") != "FAILED_PRESTART"]
    if len(sessions) >= MAX_REVIEW_SESSIONS:
        raise ProgramError(f"REVIEW_RETRIES_EXHAUSTED: review {review_id} ended {len(sessions)} times without a "
                           "verdict; operator decision required")
    attempt = max((row.get("attempt_id") or 0 for row in mine), default=0) + 1
    # Skip ids the host already holds under another shape (e.g. an operator's never-admitted tombstone).
    while cp.host_request_status(review_launch_id(review_id, attempt)).get("status") == "FOUND":
        attempt += 1
        if attempt > 32:
            raise ProgramError(f"review {review_id} has no free launch id; operator reconciliation required")
    others = [row for slot_other in (1, 2) if slot_other != slot
              for row in review_rows(rows, review_request_id(cfg["repository"], tid, writer["launch_request_id"],
                                                             head, slot_other))
              if row.get("state") != "FAILED_PRESTART"]
    exclude = writer_lanes(rows) | {row.get("lane") for row in others}
    lane = select_lane(host_lanes(), cfg, exclude=exclude, preflight=preflight)
    if lane is None:
        cp.write_github_output("launch_required", "false")
        return {"status": "NO_IDLE_LANE", "review_request_id": review_id}
    envelope = cp.parse_task_envelope(issue.get("body") or "")
    launch_id = review_launch_id(review_id, attempt)
    packet = {
        "schema_version": 2, "role": "REVIEWER", "owner_lane": writer["lane"],
        # Keys this session's verdict signer; only the reviewer lane and the host ledger read it.
        "review_request_id": review_id, "review_nonce": secrets.token_hex(16), "head_sha": head,
        "pr_number": pr_number, "repository": cfg["repository"], "project": cfg["project"],
        "task_issue": issue_number, "task_id": tid, "task_revision": envelope["TASK_REVISION"],
        "task_pointer": envelope["CANONICAL_TASK_POINTER"], "task_spec_pointer": envelope["TASK_SPEC_POINTER"],
        "task_spec_revision": envelope["TASK_SPEC_REVISION"], "approval_pointer": envelope["APPROVAL_POINTER"],
        "authoritative_doc_pointers": envelope["AUTHORITATIVE_DOC_POINTERS"],
        "builder_id": lane, "launch_request_id": launch_id, "attempt_id": attempt,
        "audit_floor": floor, "control_comment_id": comment["id"],
    }
    # Packet first: a failed projection write below leaves no launch behind.
    cp.write_private_json(packet_path, packet)
    record["reviews"] = [r for r in record.get("reviews", []) if r.get("launch_request_id") != launch_id] + [{
        "launch_request_id": launch_id, "review_request_id": review_id, "slot": slot, "lane": lane,
        "attempt": attempt, "head_sha": head, "pr": pr_number, "state": "SUBMITTING", "session_id": None,
        "confirmed_at": None, "verdict": None, "last_error": None}]
    api.update_comment(comment["id"], cp.render_control_record(record))
    cp.write_github_output("launch_required", "true")
    cp.write_github_output("launch_request_id", launch_id)
    return {"status": "PREPARED", "review_request_id": review_id, "lane": lane, "attempt": attempt}


def finalize_review(issue_number: int, result_path: Path, launch_request_id: str) -> str:
    cfg = cp.load_config()
    api = api_for(cfg)
    comment, record = control_record(api, cfg, issue_number)
    entry = next((r for r in (record or {}).get("reviews", []) if r.get("launch_request_id") == launch_request_id), None)
    if entry is None:
        raise ProgramError("stale review finalization rejected")
    if entry["state"] != "SUBMITTING":
        raise ProgramError(f"cannot finalize review from state {entry['state']}")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
    except Exception as exc:
        result = {"outcome": "UNKNOWN", "reason": f"malformed/missing adapter result: {exc}"}
    expected = {"repository": cfg["repository"], "task_id": record["task_id"],
                "task_revision": record["task_revision"], "builder_id": entry["lane"],
                "launch_request_id": launch_request_id, "attempt_id": entry["attempt"]}
    result = cp.bound_result(result, expected)
    outcome, session = result.get("outcome"), result.get("session_id")
    if outcome == "CONFIRMED" and isinstance(session, str) and session.strip():
        entry.update(state="CONFIRMED", session_id=session.strip(), last_error=None, confirmed_at=now_iso())
    elif outcome == "FAILED_PRESTART":
        entry.update(state="FAILED_PRESTART", last_error=str(result.get("reason") or "prestart failure"))
    else:
        entry.update(state="UNKNOWN", last_error=str(result.get("reason") or "review launch outcome ambiguous"))
    api.update_comment(comment["id"], cp.render_control_record(record))
    return entry["state"]


# --------------------------------------------------------------------------- reap


ISSUE_COMMENT_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)")
REVIEW_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)#pullrequestreview-([1-9][0-9]*)")
BLOCKER_RE = re.compile(r"(?m)^\s*(DECISION_REQUIRED|BLOCKED|STALLED)\b")


def task_comment(api: cp.GithubApi, cfg: Dict[str, Any], row: Dict[str, Any], issue_number: int,
                 evidence: str) -> str:
    """Body of a control-actor comment on this task written after the session's host reservation."""
    match = ISSUE_COMMENT_URL.fullmatch(evidence)
    if not match or match.group(1) != cfg["repository"] or int(match.group(2)) != issue_number:
        raise ProgramError("reap evidence must be a comment URL on this task issue")
    comment = api._request("GET", f"/issues/comments/{match.group(3)}")
    if author(comment) != cfg["control_record_actor"]:
        raise ProgramError("deliverable comment is not from the control actor")
    if (comment.get("created_at") or "") < row["reserved_at"]:
        raise ProgramError("deliverable comment predates this session's host reservation")
    return comment.get("body") or ""


def signed_blocker(body: str, launch_request_id: str) -> List[str]:
    return [m.group(0) for m in BLOCKED_RE.finditer(body) if m.group(2) == launch_request_id]


def writer_evidence(api: cp.GithubApi, cfg: Dict[str, Any], row: Dict[str, Any], issue_number: int,
                    evidence: str) -> tuple:
    """Exactly one signed line of this session from a fresh comment on this task: a delivery or a blocker.

    Unsigned DECISION_REQUIRED/BLOCKED/STALLED text is never evidence: any lane could post it.
    """
    body = task_comment(api, cfg, row, issue_number, evidence)
    deliveries, blockers = DELIVERY_RE.findall(body), signed_blocker(body, row["launch_request_id"])
    if len(deliveries) + len(blockers) != 1:
        raise ProgramError("deliverable comment must carry exactly one signed ASTRA_DELIVERY_V1 or "
                           "ASTRA_BLOCKED_V1 line of this session")
    if blockers:
        return blockers[0], BLOCKED_RE.fullmatch(blockers[0]).group(1)
    pr_number = int(re.search(r"pr=([1-9][0-9]*)", deliveries[0]).group(1))
    pr = api._request("GET", f"/pulls/{pr_number}")
    head = pr.get("head") or {}
    if head.get("ref") != branch_for(row["task"]) or (head.get("repo") or {}).get("full_name") != cfg["repository"]:
        raise ProgramError(f"a delivery must come from this task's branch {branch_for(row['task'])} in this repository")
    return deliveries[0], "DELIVERY"


def review_evidence(api: cp.GithubApi, cfg: Dict[str, Any], row: Dict[str, Any], issue_number: int,
                    evidence: str) -> tuple:
    """The reviewer's own PR review at the reviewed head carrying its signed verdict line, or a fresh
    task comment carrying its signed blocker line. Nothing unsigned decides whether a verdict exists."""
    review_id, pr_number = row.get("review_request_id"), row.get("pr_number")
    match = REVIEW_URL.fullmatch(evidence)
    if match is None:
        blockers = signed_blocker(task_comment(api, cfg, row, issue_number, evidence), row["launch_request_id"])
        if len(blockers) != 1:
            raise ProgramError("a reviewer released without a verdict needs exactly one signed ASTRA_BLOCKED_V1 "
                               "line of this session")
        return blockers[0], BLOCKED_RE.fullmatch(blockers[0]).group(1)
    if match.group(1) != cfg["repository"] or int(match.group(2)) != pr_number:
        raise ProgramError("review reap evidence must be a review URL on the reviewed PR")
    review = api._request("GET", f"/pulls/{pr_number}/reviews/{match.group(3)}")
    if author(review) != cfg["control_record_actor"]:
        raise ProgramError("review is not from the control actor")
    if review.get("commit_id") != row.get("head_sha"):
        raise ProgramError("review was not submitted at the reviewed head")
    if (review.get("submitted_at") or "") < row["reserved_at"]:
        raise ProgramError("review predates the reviewer session's host reservation")
    lines = [m.group(0) for m in REVIEW_RE.finditer(review.get("body") or "") if m.group(1) == review_id]
    if len(lines) != 1:
        raise ProgramError("review must carry exactly one signed ASTRA_REVIEW_V1 line for this review request")
    return lines[0], "REVIEW"


def reap(issue_number: int, launch_request_id: str, evidence: str) -> Dict[str, Any]:
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    api = api_for(cfg)
    comment, record = control_record(api, cfg, issue_number)
    if record is None:
        raise ProgramError("task has no control record")
    row = cp.host_request_status(launch_request_id)
    if row.get("status") != "FOUND" or row.get("repository") != cfg["repository"] \
            or row.get("task") != issue_task_id(api, cfg, issue_number):
        raise ProgramError("launch_request_id is not a host launch of this task")
    if released(row):
        # Write-once: project the stored pin; the host already holds the only answer.
        if row.get("evidence") != evidence:
            raise ProgramError("reap is write-once: this session was released with different evidence")
        pin, repeated, kind = row.get("pin"), True, None
    else:
        read = writer_evidence if row.get("role") == "WRITER" else review_evidence
        line, kind = read(api, cfg, row, issue_number, evidence)
        result = host(["reap", "--launch-request-id", launch_request_id, "--evidence", evidence, "--pin-stdin"],
                      {"pin": line})
        if result.get("resolution") != cp.VERIFIED_RELEASE:
            raise ProgramError("host did not verify the session terminal")
        pin, repeated = result.get("pin"), result.get("repeated")
        row = cp.host_request_status(launch_request_id)
    if row.get("role") == "WRITER":
        if record.get("launch_request_id") == launch_request_id:
            record.update(launch_state="RELEASED", released_evidence=evidence, released_at=row.get("released_at"),
                          delivery=pin if (pin or {}).get("kind") == "DELIVERY" else None)
            if record.get("owner_lane") is None:  # a lost CONFIRMED projection must not lose ownership
                record["owner_lane"] = row.get("lane")
            record["last_deliverable"] = {"kind": kind or (pin or {}).get("kind"), "evidence": evidence}
    else:
        entry = project_review(record, row)
        entry.update(released_evidence=evidence, released_at=row.get("released_at"))
    api.update_comment(comment["id"], cp.render_control_record(record))
    return {"status": "RELEASED", "launch_request_id": launch_request_id, "pin": pin, "repeated": repeated}


# --------------------------------------------------------------------------- merge readiness


def all_check_runs(api: cp.GithubApi, head: str) -> List[Dict[str, Any]]:
    runs, page = [], 1
    while True:
        chunk = (api._request("GET", f"/commits/{head}/check-runs?per_page=100&page={page}") or {}).get("check_runs", [])
        runs.extend(chunk)
        if len(chunk) < 100:
            return runs
        page += 1


def latest_check_runs(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The newest run of each check (app and name), as branch protection reads a head.

    A head keeps every run: the skipped one from while its PR was a draft, a failed attempt
    before a re-run. Only the newest run of each check says what the head is now. Check run
    ids only grow, so the larger id is the newer run.
    """
    newest: Dict[tuple, tuple] = {}
    for index, run in enumerate(runs):
        key = ((run.get("app") or {}).get("id"), run.get("name"))
        order = (run.get("id") if type(run.get("id")) is int else 0, index)
        if key not in newest or order > newest[key][0]:
            newest[key] = (order, run)
    return [run for _, run in newest.values()]


def verification_reasons(api, cfg, head):
    """One current-head CI predicate shared by merge and quota readmission."""
    reasons = []
    runs = latest_check_runs(all_check_runs(api, head)) if head else []
    if any(r.get("status") != "completed" or r.get("conclusion") not in ("success", "neutral", "skipped")
           for r in runs):
        reasons.append("verification gate: a check run is incomplete or failing on the head")
    required_checks = cfg.get("program_required_checks")
    if not isinstance(required_checks, list) or not required_checks \
            or not all(isinstance(name, str) and name.strip() for name in required_checks):
        reasons.append("verification gate: the product's required checks are not declared (program_required_checks)")
    else:
        for name in required_checks:
            named = [r for r in runs if r.get("name") == name]
            if not named or not all(r.get("status") == "completed" and r.get("conclusion") == "success"
                                    for r in named):
                reasons.append(f"verification gate: required check {name!r} has not succeeded on the head")
    combined = api._request("GET", f"/commits/{head}/status") if head else {}
    if (combined or {}).get("statuses") and (combined or {}).get("state") != "success":
        reasons.append(f"verification gate: commit statuses are {combined.get('state')}")
    return reasons


def merge_check(issue_number: int, pr_number: int) -> Dict[str, Any]:
    """DISPATCH section 18, computed from host pins, the plan and live PR state. Anything not
    computable makes the PR not ready."""
    cfg = cp.load_config()
    api = api_for(cfg)
    reasons: List[str] = []
    pr = api._request("GET", f"/pulls/{pr_number}")
    head = (pr.get("head") or {}).get("sha")
    if pr.get("state") != "open" or pr.get("draft") is not False or not SHA.fullmatch(head or ""):
        reasons.append("PR is not an open, non-draft PR at a known head")
    if pr.get("mergeable_state") != "clean":
        reasons.append(f"PR mergeable_state is {pr.get('mergeable_state')} (must be clean)")
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != cfg["repository"] \
            or (pr.get("base") or {}).get("ref") != default_branch(api):
        reasons.append("PR must come from this repository and target the default branch")
    try:
        issue, plan, node, mstatus = task_context(api, cfg, issue_number)
    except ProgramError as exc:
        return {"ready": False, "head": head, "pr": pr_number, "issue": issue_number, "reasons": reasons + [str(exc)]}
    if (pr.get("head") or {}).get("ref") != branch_for(task_id_for(plan["program"], node["id"])):
        reasons.append(f"PR must come from the task branch {branch_for(task_id_for(plan['program'], node['id']))}")
    tid = task_id_for(plan["program"], node["id"])
    rows = task_rows(cfg, tid)
    writer = current_writer(rows)
    delivery = pin_of(writer, "DELIVERY")
    if delivery is None or (delivery["pr"], delivery["head"]) != (pr_number, head):
        reasons.append("the host-pinned delivery of the current writer attempt does not name this PR head")
    elif writer.get("task_revision") != expected_revision(mstatus, writer):
        reasons.append("the pinned delivery is for an older task revision; the current revision is not delivered")
    elif (issue.get("body") or "") != rendered_body(issue, plan, node, mstatus, writer["lane"]):
        reasons.append("task issue body differs from the envelope rendered from the host-recorded plan "
                       "(operation=review restores it)")
    labels = {label.get("name") for label in issue.get("labels", [])}
    if labels & BLOCKING_LABELS:
        reasons.append(f"unresolved blocker labels: {sorted(labels & BLOCKING_LABELS)}")
    reasons.extend(verification_reasons(api, cfg, head))
    if any(row.get("role") == "REVIEWER" and row.get("state") in ("SUBMITTING", "CONFIRMED", "UNKNOWN")
           for row in rows):
        reasons.append("a review session of this task is still active or unresolved")
    verdicts = current_verdicts(cfg, tid, rows, writer, head) if delivery is not None and head else []
    floor = effective_floor(node["audit_floor"], verdicts)
    gate = node["astra_gate"] if node["astra_gate"] == "RELEASE" else \
        "ARCHITECTURE" if floor == "A3" else node["astra_gate"]  # never conceal a release reservation
    needed_depth = min(AUDIT_FLOORS.index(floor), 2)
    owners = writer_lanes(rows)
    passing_lanes, contract_change = set(), False
    for row, verdict in verdicts:
        if verdict["contract_change"] == "YES":
            contract_change = True
        if verdict["verdict"] in ("FAIL", "DECISION_REQUIRED"):
            reasons.append(f"review {verdict['review']} returned {verdict['verdict']}")
        elif AUDIT_FLOORS.index(verdict["depth"]) >= needed_depth and row.get("lane") not in owners:
            passing_lanes.add(row.get("lane"))
    if len(passing_lanes) < REQUIRED_REVIEWS[floor]:
        reasons.append(f"{len(passing_lanes)} of {REQUIRED_REVIEWS[floor]} required independent reviews at effective "
                       f"floor {floor} (host-pinned verdicts from distinct non-writer lanes) PASS at this head")
    if node.get("user_merge") is True:
        reasons.append("approved plan reserves this node's merge to the User")
    astra_status, astra_result, scope_result, astra_receipt_hold = "NOT_REQUIRED", None, None, None
    delegated_astra = node.get("astra_auto_merge") is True and node.get("user_merge") is not True \
        and node.get("astra_gate") != "RELEASE"
    if gate != "NONE" or floor == "A3":
        if not delegated_astra:
            astra_status = "USER_REQUIRED"
            reasons.append(f"Astra gate {gate} is not delegated by this approved plan; User merges")
        else:
            try:
                receipt = fable_program("check", issue_number, pr=pr_number, head=head)
            except ProgramError as exc:
                astra_status = "ERROR"
                reasons.append(str(exc))
            else:
                astra_status = receipt.get("status", "ERROR")
                astra_result, scope_result = receipt.get("result"), receipt.get("scope_result")
                expected = {"repository": cfg["repository"], "issue": issue_number,
                            "program": plan["program"], "node": node["id"], "plan_commit": mstatus["plan_commit"],
                            "task_revision": writer["task_revision"] if writer else None,
                            "writer_launch": writer["launch_request_id"] if writer else None,
                            "pr": pr_number, "head": head, "gate": gate, "depth": floor}
                if not astra_receipt_matches(receipt, expected, gate, floor):
                    astra_receipt_hold = "required current-head protected Fable scope audit is missing or not passing"
                    reasons.append(astra_receipt_hold)
    elif contract_change:
        reasons.append("a current-head review reports a contract change; Astra/User decision required")
    if contract_change:
        message = "a current-head review reports a contract change; Astra/User decision required"
        if message not in reasons:
            reasons.append(message)
    pending = pending_dependencies(api, cfg, plan, node)
    if pending:
        reasons.append(f"dependencies are not DONE: {pending}")
    if cfg.get("program_merge_policy") != "STANDARD":
        reasons.append("project merge prerequisites are not declared machine-computable (program_merge_policy)")
    return {"ready": not reasons, "head": head, "pr": pr_number, "issue": issue_number, "reasons": reasons,
            "astra_status": astra_status, "astra_result": astra_result, "scope_result": scope_result,
            "astra_audit_allowed": delegated_astra and astra_status in ("MISSING", "BUSY")
                and not any(reason != astra_receipt_hold for reason in reasons)}


def merge(issue_number: int, pr_number: int) -> Dict[str, Any]:
    """User decision M1 (2026-09-29): the merge executor is delegated, the merge condition is not.

    Merges only when merge_check is ready, and pins the merge to the head it computed, so a push
    in between makes GitHub refuse the merge instead of merging an unreviewed head.
    """
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    check = merge_check(issue_number, pr_number)
    if not check["ready"]:
        return {"status": "NOT_READY", "issue": issue_number, "pr": pr_number, "reasons": check["reasons"]}
    result = api_for(cfg)._request("PUT", f"/pulls/{pr_number}/merge", {"sha": check["head"]})
    if not isinstance(result, dict) or result.get("merged") is not True:
        raise ProgramError(f"GitHub did not merge PR {pr_number} at {check['head']}")
    return {"status": "MERGED", "issue": issue_number, "pr": pr_number, "head": check["head"],
            "merge_commit": result.get("sha")}


# --------------------------------------------------------------------------- CLI


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lanes")
    for name in ("materialize", "start", "review", "finalize-review", "reap", "merge-check", "merge", "astra-audit", "astra-consult", "quota-readiness", "quota-resume"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--args-json", default="{}")
        cmd.add_argument("--issue-number", type=int)
        cmd.add_argument("--packet", type=Path)
        cmd.add_argument("--result", type=Path)
        cmd.add_argument("--launch-request-id")
    args = parser.parse_args(argv)
    try:
        extra = json.loads(getattr(args, "args_json", "{}") or "{}")
        if not isinstance(extra, dict):
            raise ProgramError("--args-json must be a JSON object")
        if args.command == "lanes":
            result: Any = host_lanes()
        elif args.command == "materialize":
            result = materialize(extra.get("program", ""), extra.get("node", ""), extra.get("plan_commit", ""))
        elif args.command == "start":
            result = start(args.issue_number, extra.get("program", ""), extra.get("node", ""),
                           extra.get("plan_commit", ""), args.packet)
        elif args.command == "review":
            result = prepare_review(args.issue_number, int(extra.get("slot", 1)), args.packet)
        elif args.command == "finalize-review":
            result = {"state": finalize_review(args.issue_number, args.result, args.launch_request_id)}
        elif args.command == "reap":
            result = reap(args.issue_number, extra.get("launch_request_id", ""), extra.get("evidence", ""))
        elif args.command == "astra-audit":
            result = astra_audit(args.issue_number, int(extra.get("pr_number", 0)), extra.get("head", ""))
        elif args.command == "astra-consult":
            result = astra_consult(args.issue_number, int(extra.get("question_comment_id", 0)))
        elif args.command in ("quota-readiness", "quota-resume"):
            result = quota_operation(args.command, args.issue_number, extra)
        elif args.command == "merge":
            result = merge(args.issue_number, int(extra.get("pr_number", 0)))
        else:
            result = merge_check(args.issue_number, int(extra.get("pr_number", 0)))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (cp.ControlPlaneError, KeyError, ValueError, TypeError) as exc:
        print(f"PROGRAM_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
