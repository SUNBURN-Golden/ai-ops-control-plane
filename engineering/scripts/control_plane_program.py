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
  reap         release a verified-terminal session and project RELEASED
  merge-check  compute DISPATCH section 18 READY_FOR_MERGE for one PR head
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import control_plane as cp

LANE_ORDER = ("DEVIN", "GROK_BUILD", "GLM", "CURSOR")
TASK_LABEL = "aiops-task"
PLAN_PATH = ".aiops/program.json"
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[0-9a-f]{40}")
TASK_KEY_RE = re.compile(r"<!-- ASTRA_TASK_KEY_V1 program=(\S+) node=(\S+) request=([0-9a-f]{24}) -->")
DELIVERY_RE = re.compile(r"ASTRA_DELIVERY_V1 pr=([1-9][0-9]*) head=([0-9a-f]{40})")
REVIEW_RE = re.compile(r"ASTRA_REVIEW_V1 review=([0-9a-f]{24}) head=([0-9a-f]{40}) "
                       r"verdict=(PASS|PASS_WITH_NOTES|FAIL|DECISION_REQUIRED) depth=(A[0-3]) "
                       r"contract_change=(NO|YES) nonce=([0-9a-f]{32})")
AUDIT_FLOORS = ("A0", "A1", "A2", "A3")
ASTRA_GATES = ("NONE", "MILESTONE", "ARCHITECTURE", "RELEASE")
DELIVERABLE_MODES = ("PR", "NON_CODE_EVIDENCE", "NO_CHANGE_ALLOWED")
REQUIRED_REVIEWS = {"A0": 0, "A1": 1, "A2": 2, "A3": 2}
BLOCKING_LABELS = {"needs-user", "blocked", "decision-required"}


class ProgramError(cp.ControlPlaneError):
    pass


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


def host(arguments: List[str]) -> Dict[str, Any]:
    report = cp.host_call(arguments)
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
        if (lane in excluded or lane not in cfg["enabled_builders"] or not entry
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
    for node in nodes:
        if not isinstance(node.get("title"), str) or not node["title"].strip():
            raise ProgramError(f"node {node['id']} needs a title")
        if not isinstance(node.get("spec"), str) or not node["spec"].strip():
            raise ProgramError(f"node {node['id']} needs a spec")
        deps = node.get("depends_on", [])
        if not isinstance(deps, list) or any(dep not in ids or dep == node["id"] for dep in deps):
            raise ProgramError(f"node {node['id']} has invalid depends_on")
        node.setdefault("audit_floor", "A1")
        node.setdefault("astra_gate", "NONE")
        node.setdefault("deliverable_mode", "PR")
        if (node["audit_floor"] not in AUDIT_FLOORS or node["astra_gate"] not in ASTRA_GATES
                or node["deliverable_mode"] not in DELIVERABLE_MODES):
            raise ProgramError(f"node {node['id']} has an invalid gate field")
        if node["audit_floor"] == "A3":
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
    delivery = ("Deliverable protocol (program mode): when the pull request is ready, post one comment on this "
                "issue with the exact line `ASTRA_DELIVERY_V1 pr=<number> head=<40-hex head sha>`, then end the "
                "session. If blocked, post `DECISION_REQUIRED`, `BLOCKED` or `STALLED` with the reason and end the "
                "session. Do not merge.")
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


# --------------------------------------------------------------------------- start (writer)


def control_record(api: cp.GithubApi, cfg: Dict[str, Any], issue_number: int):
    comment = cp.find_control_comment(api.comments(issue_number), cfg["control_record_actor"])
    return comment, (cp.parse_control_record(comment.get("body") or "") if comment else None)


def start(issue_number: int, program: str, node_id: str, plan_commit: str, packet_path: Path,
          *, preflight: Callable[[str], bool] = preflight_ok) -> Dict[str, Any]:
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    api = api_for(cfg)
    status = host(["materialize-status", "--program", program, "--node", node_id])
    if status.get("status") != "CREATED" or status.get("issue") != issue_number:
        raise ProgramError("program node has no CREATED canonical issue matching issue_number")
    # Validate the new plan fully before recording it; a broken plan must not wedge the node.
    plan = load_plan(api, cfg, plan_commit)
    node = plan_node(plan, node_id)
    require_on_default_branch(api, plan_commit)
    if status["plan_commit"] != plan_commit:
        require_descendant(api, status["plan_commit"], plan_commit)
        host(["materialize-plan", "--program", program, "--node", node_id,
              "--from", status["plan_commit"], "--to", plan_commit])
    issue = api.issue(issue_number)
    if issue.get("state") != "open" or issue_key(issue) != (program, node_id, status["request"]):
        raise ProgramError("canonical issue is closed or its task key does not match the host record")
    require_single_canonical(api, cfg, program, node_id, issue_number)
    _, record = control_record(api, cfg, issue_number)
    board = host_lanes()
    tid = task_id_for(program, node_id)
    if active_rows_for_task(board, cfg["repository"], tid):
        cp.write_github_output("launch_required", "false")
        return {"status": "TASK_ACTIVE", "issue": issue_number}
    attempt = None
    if record is not None:
        state = record["launch_state"]
        if state in {"SUBMITTING", "UNKNOWN"}:
            raise ProgramError(f"unresolved {state} launch; operator reconciliation required")
        if state == "CONFIRMED":
            host_state = cp.host_request_status(record["launch_request_id"])
            if host_state.get("state") == "CONFIRMED":
                cp.write_github_output("launch_required", "false")
                return {"status": "OWNER_LIVE", "issue": issue_number}
            if host_state.get("resolution") not in cp.TERMINAL_RELEASES:
                raise ProgramError(f"NEEDS_OPERATOR: record CONFIRMED but host is {host_state}")
        if state in {"FAILED_PRESTART", "RELEASED", "CONFIRMED"}:
            attempt = cp.record_attempt_id(record) + 1
    owner = (record or {}).get("owner_lane")
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


def review_request_id(repository: str, task_id: str, revision: str, writer_attempt: int, head: str,
                      slot: int) -> str:
    material = f"review\0{repository}\0{task_id}\0{revision}\0{writer_attempt}\0{head}\0{slot}"
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def review_launch_id(review_id: str, attempt: int) -> str:
    return hashlib.sha256(f"review-launch\0{review_id}\0{attempt}".encode()).hexdigest()[:24]


def delivered_pr(api: cp.GithubApi, cfg: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    """The delivery pinned at the writer's reap, checked against the live PR (same repo, default base)."""
    delivery = record.get("delivery")
    if not isinstance(delivery, dict):
        raise ProgramError("no pinned ASTRA_DELIVERY_V1 for the current writer attempt")
    pr = api._request("GET", f"/pulls/{delivery['pr']}")
    head = (pr.get("head") or {})
    if (pr.get("state") != "open" or head.get("sha") != delivery["head"]
            or (head.get("repo") or {}).get("full_name") != cfg["repository"]
            or (pr.get("base") or {}).get("ref") != default_branch(api)):
        raise ProgramError("delivered PR is not open at the delivered head in this repository against the "
                           "default branch; the writer must deliver again")
    return pr


def prepare_review(issue_number: int, slot: int, packet_path: Path,
                   *, preflight: Callable[[str], bool] = preflight_ok) -> Dict[str, Any]:
    if slot not in (1, 2):
        raise ProgramError("review slot must be 1 or 2")
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    api = api_for(cfg)
    issue = api.issue(issue_number)
    envelope = cp.parse_task_envelope(issue.get("body") or "")
    comment, record = control_record(api, cfg, issue_number)
    if record is None or not record.get("owner_lane"):
        raise ProgramError("review needs a task with an established owner lane")
    if record["launch_state"] != "RELEASED":
        raise ProgramError("review waits until the writer session is verified terminal (RELEASED)")
    if REQUIRED_REVIEWS[envelope.get("AUDIT_FLOOR", "A1")] < slot:
        raise ProgramError(f"audit floor {envelope.get('AUDIT_FLOOR')} does not require review slot {slot}")
    delivered_pr(api, cfg, record)
    pr_number, head = record["delivery"]["pr"], record["delivery"]["head"]
    reviews = record.setdefault("reviews", [])
    review_id = review_request_id(cfg["repository"], record["task_id"], record["task_revision"],
                                  cp.record_attempt_id(record), head, slot)
    entry = next((r for r in reviews if r.get("review_request_id") == review_id), None)
    if entry is not None and entry["state"] in {"SUBMITTING", "UNKNOWN"}:
        host_state = cp.host_request_status(entry["launch_request_id"])
        if host_state.get("state") == "CONFIRMED":
            entry.update(state="CONFIRMED", confirmed_at=entry.get("confirmed_at") or now_iso(),
                         last_error="projection repaired from host CONFIRMED")
            api.update_comment(comment["id"], cp.render_control_record(record))
            cp.write_github_output("launch_required", "false")
            return {"status": "REVIEW_EXISTS", "review_request_id": review_id, "repaired": True}
        if host_state.get("state") not in ("FAILED_PRESTART", "RECONCILED"):
            raise ProgramError(f"review {review_id} is {entry['state']} (host {host_state.get('state')}); "
                               "operator reconciliation required")
    elif entry is not None and entry["state"] in {"CONFIRMED", "RELEASED"}:
        cp.write_github_output("launch_required", "false")
        return {"status": "REVIEW_EXISTS", "review_request_id": review_id}
    exclude = {record["owner_lane"]} | {r["lane"] for r in reviews if r.get("head_sha") == head and r is not entry}
    lane = select_lane(host_lanes(), cfg, exclude=exclude, preflight=preflight)
    if lane is None:
        cp.write_github_output("launch_required", "false")
        return {"status": "NO_IDLE_LANE", "review_request_id": review_id}
    attempt = 1 if entry is None else entry["attempt"] + 1
    nonce = secrets.token_hex(16)  # only the reviewer's packet carries it; the record keeps its hash
    new_entry = {"review_request_id": review_id, "slot": slot, "head_sha": head, "pr": pr_number,
                 "lane": lane, "attempt": attempt, "launch_request_id": review_launch_id(review_id, attempt),
                 "nonce_sha256": sha256_text(nonce), "state": "SUBMITTING", "session_id": None,
                 "confirmed_at": None, "verdict": None, "last_error": None}
    record["reviews"] = [r for r in reviews if r is not entry] + [new_entry]
    api.update_comment(comment["id"], cp.render_control_record(record))
    packet = {
        "schema_version": 2, "role": "REVIEWER", "owner_lane": record["owner_lane"],
        "review_request_id": review_id, "review_nonce": nonce, "head_sha": head, "pr_number": pr_number,
        "repository": cfg["repository"], "project": cfg["project"], "task_issue": issue_number,
        "task_id": record["task_id"], "task_revision": record["task_revision"],
        "task_pointer": envelope["CANONICAL_TASK_POINTER"], "task_spec_pointer": envelope["TASK_SPEC_POINTER"],
        "task_spec_revision": envelope["TASK_SPEC_REVISION"], "approval_pointer": envelope["APPROVAL_POINTER"],
        "authoritative_doc_pointers": envelope["AUTHORITATIVE_DOC_POINTERS"],
        "builder_id": lane, "launch_request_id": new_entry["launch_request_id"], "attempt_id": attempt,
        "audit_floor": envelope.get("AUDIT_FLOOR", "A1"), "control_comment_id": comment["id"],
    }
    fd = os.open(packet_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)  # the mode argument applies only when the file is new
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(packet, indent=2, sort_keys=True) + "\n")
    cp.write_github_output("launch_required", "true")
    cp.write_github_output("launch_request_id", new_entry["launch_request_id"])
    return {"status": "PREPARED", "review_request_id": review_id, "lane": lane}


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


def session_floor(launch_request_id: str) -> str:
    """Host reservation time of a launch: anything its session wrote is at or after it."""
    floor = cp.host_request_status(launch_request_id).get("reserved_at")
    if not isinstance(floor, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", floor):
        raise ProgramError("host has no reservation time for this launch")
    return floor


def pin_writer_deliverable(api: cp.GithubApi, cfg: Dict[str, Any], record: Dict[str, Any], issue_number: int,
                           evidence: str) -> Dict[str, Any]:
    """The evidence must be a deliverable comment on this task, written after this attempt was reserved."""
    match = ISSUE_COMMENT_URL.fullmatch(evidence)
    if not match or match.group(1) != cfg["repository"] or int(match.group(2)) != issue_number:
        raise ProgramError("writer reap evidence must be a comment URL on this task issue")
    comment = api._request("GET", f"/issues/comments/{match.group(3)}")
    body = comment.get("body") or ""
    if author(comment) != cfg["control_record_actor"]:
        raise ProgramError("deliverable comment is not from the control actor")
    if (comment.get("created_at") or "") < session_floor(record["launch_request_id"]):
        raise ProgramError("deliverable comment predates this attempt's host reservation")
    delivery = DELIVERY_RE.search(body)
    blocker = BLOCKER_RE.search(body)
    if delivery:
        return {"kind": "DELIVERY", "pr": int(delivery.group(1)), "head": delivery.group(2),
                "comment_id": int(match.group(3)), "attempt": cp.record_attempt_id(record)}
    if blocker:
        return {"kind": blocker.group(1), "comment_id": int(match.group(3)), "attempt": cp.record_attempt_id(record)}
    raise ProgramError("evidence comment carries no ASTRA_DELIVERY_V1 or blocker marker")


def pin_review_verdict(api: cp.GithubApi, cfg: Dict[str, Any], entry: Dict[str, Any], evidence: str) -> Dict[str, Any]:
    """The evidence must be the reviewer's PR review at the reviewed head, carrying its secret nonce."""
    match = REVIEW_URL.fullmatch(evidence)
    if not match or match.group(1) != cfg["repository"] or int(match.group(2)) != entry["pr"]:
        raise ProgramError("review reap evidence must be a review URL on the reviewed PR")
    review = api._request("GET", f"/pulls/{entry['pr']}/reviews/{match.group(3)}")
    verdict = REVIEW_RE.search(review.get("body") or "")
    if author(review) != cfg["control_record_actor"]:
        raise ProgramError("review is not from the control actor")
    if review.get("commit_id") != entry["head_sha"]:
        raise ProgramError("review was not submitted at the reviewed head")
    if (review.get("submitted_at") or "") < session_floor(entry["launch_request_id"]):
        raise ProgramError("review predates the reviewer session's host reservation")
    if (not verdict or verdict.group(1) != entry["review_request_id"] or verdict.group(2) != entry["head_sha"]
            or sha256_text(verdict.group(6)) != entry.get("nonce_sha256")):
        raise ProgramError("review verdict line does not bind this review request, head and nonce")
    return {"result": verdict.group(3), "depth": verdict.group(4), "contract_change": verdict.group(5),
            "review_id": int(match.group(3))}


def reap(issue_number: int, launch_request_id: str, evidence: str) -> Dict[str, Any]:
    cfg = cp.load_config()
    cp.require_runtime_enabled()
    api = api_for(cfg)
    comment, record = control_record(api, cfg, issue_number)
    if record is None:
        raise ProgramError("task has no control record")
    if record.get("launch_request_id") == launch_request_id:
        target = record
        pinned = pin_writer_deliverable(api, cfg, record, issue_number, evidence)
    else:
        target = next((r for r in record.get("reviews", []) if r.get("launch_request_id") == launch_request_id), None)
        if target is None:
            raise ProgramError("launch_request_id is not part of this task's control record")
        pinned = pin_review_verdict(api, cfg, target, evidence)
    result = host(["reap", "--launch-request-id", launch_request_id, "--evidence", evidence])
    if result.get("resolution") != cp.VERIFIED_RELEASE:
        raise ProgramError("host did not verify the session terminal")
    key = "launch_state" if target is record else "state"
    changed = False
    if target.get(key) != "RELEASED":
        target[key], target["released_evidence"], target["released_at"] = "RELEASED", evidence, now_iso()
        changed = True
    if target is record:
        if record.get("owner_lane") is None:  # a lost CONFIRMED projection must not lose ownership
            record["owner_lane"] = record["builder_id"]
            changed = True
        if record.get("delivery") != (pinned if pinned["kind"] == "DELIVERY" else None):
            record["delivery"] = pinned if pinned["kind"] == "DELIVERY" else None
            changed = True
        record["last_deliverable"] = pinned
    elif target.get("verdict") != pinned:
        target["verdict"] = pinned
        changed = True
    if changed or target is record:
        api.update_comment(comment["id"], cp.render_control_record(record))
    return {"status": "RELEASED", "launch_request_id": launch_request_id, "repeated": result.get("repeated")}


# --------------------------------------------------------------------------- merge readiness


def all_check_runs(api: cp.GithubApi, head: str) -> List[Dict[str, Any]]:
    runs, page = [], 1
    while True:
        chunk = (api._request("GET", f"/commits/{head}/check-runs?per_page=100&page={page}") or {}).get("check_runs", [])
        runs.extend(chunk)
        if len(chunk) < 100:
            return runs
        page += 1


def merge_check(issue_number: int, pr_number: int) -> Dict[str, Any]:
    """DISPATCH section 18, computed. Anything not computable makes the PR not ready."""
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
    issue = api.issue(issue_number)
    envelope = cp.parse_task_envelope(issue.get("body") or "")
    _, record = control_record(api, cfg, issue_number)
    record = record or {}
    if record.get("task_revision") != envelope["TASK_REVISION"]:
        reasons.append("task revision is not current in the control record")
    labels = {label.get("name") for label in issue.get("labels", [])}
    if labels & BLOCKING_LABELS:
        reasons.append(f"unresolved blocker labels: {sorted(labels & BLOCKING_LABELS)}")
    delivery = record.get("delivery") or {}
    if (delivery.get("pr"), delivery.get("head")) != (pr_number, head) or record.get("launch_state") != "RELEASED":
        reasons.append("the pinned delivery of the released writer attempt does not name this PR head")
    runs = all_check_runs(api, head) if head else []
    if not runs or any(r.get("status") != "completed" or
                       r.get("conclusion") not in ("success", "neutral", "skipped") for r in runs):
        reasons.append("verification gate: check runs are not all green on the head")
    combined = api._request("GET", f"/commits/{head}/status") if head else {}
    if (combined or {}).get("statuses") and (combined or {}).get("state") != "success":
        reasons.append(f"verification gate: commit statuses are {combined.get('state')}")
    floor = envelope.get("AUDIT_FLOOR", "A1")
    needed_depth = min(AUDIT_FLOORS.index(floor), 2)
    passing_lanes, contract_change = set(), False
    for review in record.get("reviews", []):
        verdict = review.get("verdict")
        if review.get("head_sha") != head or review.get("state") != "RELEASED" or not verdict:
            continue
        if verdict["contract_change"] == "YES":
            contract_change = True
        if verdict["result"] in ("FAIL", "DECISION_REQUIRED"):
            reasons.append(f"review {review['review_request_id']} returned {verdict['result']}")
        elif AUDIT_FLOORS.index(verdict["depth"]) >= needed_depth and review["lane"] != record.get("owner_lane"):
            passing_lanes.add(review["lane"])
    if len(passing_lanes) < REQUIRED_REVIEWS[floor]:
        reasons.append(f"{len(passing_lanes)} of {REQUIRED_REVIEWS[floor]} required independent reviews "
                       "(distinct non-owner lanes, released, pinned) PASS at this head")
    if contract_change:
        reasons.append("a current-head review reports a contract change; Astra/User decision required")
    if envelope.get("ASTRA_GATE", "NONE") != "NONE" or floor == "A3":
        reasons.append(f"Astra gate {envelope.get('ASTRA_GATE')} is not machine-verifiable here; User merges")
    if cfg.get("program_merge_policy") != "STANDARD":
        reasons.append("project merge prerequisites are not declared machine-computable (program_merge_policy)")
    return {"ready": not reasons, "head": head, "pr": pr_number, "issue": issue_number, "reasons": reasons}


# --------------------------------------------------------------------------- CLI


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lanes")
    for name in ("materialize", "start", "review", "finalize-review", "reap", "merge-check"):
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
        else:
            result = merge_check(args.issue_number, int(extra.get("pr_number", 0)))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (cp.ControlPlaneError, ValueError, TypeError) as exc:
        print(f"PROGRAM_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
