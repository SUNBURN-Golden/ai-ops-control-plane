"""Protected Fable bridge for program audits, consultations and merge receipts.

Imported only by the root-owned aiops-fable program command. GitHub comments are
context, never receipts. The authoritative binding comes from the host ledger,
the host-recorded immutable plan and live PR state, recalculated on every read.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import control_plane as cp
import control_plane_program as prog
import control_plane_program_receipts as receipt_journal
import control_plane_program_quota as quota_runtime


BridgeError = receipt_journal.ReceiptError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def protected(path, directory=False, *, info=None):
    info = os.lstat(path) if info is None else info
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise BridgeError("program Astra state must be root-owned and not group/other writable")


POLICY_PATHS = ("AGENTS.md", "RUNBOOKS/DISPATCH.md", "docs/PROGRAM_MODE.md",
                "docs/CONTROL_PLANE_RUNTIME.md", "docs/COORDINATOR_PLAYBOOK.md",
                "docs/PROGRAM_ASTRA_AUTOMATION.md", "docs/PROGRAM_ASTRA_ADOPTION_PROPOSAL_KO.md", "docs/PROGRAM_FABLE_RECOVERY.md")


def installed_fingerprint(ctx):
    """Bind protected installed bytes, not an untrusted working tree or stdin."""
    paths = [Path(module.__file__) for module in (cp, prog, receipt_journal, quota_runtime)] + [Path(__file__), cp.CONFIG_PATH, cp.ACTIVATION_PATH,
             cp.CONFIG_PATH.with_name("projects.json")] + [cp.ROOT / name for name in POLICY_PATHS]
    hashes = []
    for path in paths:
        protected(path)
        for directory in path.parents:
            protected(directory, directory=True)
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256(canonical({"fable_sha256": ctx.tool_sha256, "installed_sha256": hashes}).encode()).hexdigest()


def require_service_authorization(fingerprint):
    """Root operator's independent adoption/qualification record is mandatory.

    This record is not the repository example, activation.json, a model verdict,
    or caller-supplied pointer. Missing/PENDING or changed installed bytes hold
    direct program entry, even if the outer workflow was bypassed.
    """
    # Installed protected activation must independently remain enabled. We do
    # not run source-checkout git commands in /opt/aiops/lib.
    try:
        activation = cp.load_activation()
    except cp.ControlPlaneError:
        raise BridgeError("installed runtime activation is invalid") from None
    if activation.get("runtime_enabled") is not True:
        raise BridgeError("installed runtime activation is disabled")
    path = cp.CONFIG_PATH.with_name("program-astra-authorization.json")
    try:
        protected(path)
        for directory in path.parents:
            protected(directory, directory=True)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise BridgeError("duplicate service authorization field")
                result[key] = value
            return result
        record = json.loads(path.read_text(), object_pairs_hook=unique)
    except (OSError, ValueError):
        raise BridgeError("protected program Astra adoption/qualification record is missing or invalid") from None
    fields = {"schema_version", "state", "runtime_enabled", "approved_commit", "installed_fingerprint",
              "decision_pointer", "audit_pointer", "qualification_pointer", "qualification_sha256"}
    if not isinstance(record, dict) or set(record) != fields or type(record.get("schema_version")) is not int \
            or record["schema_version"] != 1 or record.get("state") != "ACTIVE" \
            or record.get("runtime_enabled") is not True or record.get("installed_fingerprint") != fingerprint \
            or record.get("approved_commit") != activation.get("activated_runtime_sha") \
            or not prog.SHA.fullmatch(str(record.get("approved_commit", ""))) \
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("qualification_sha256", ""))):
        raise BridgeError("protected program Astra adoption/qualification is pending, disabled or stale")
    for key in ("decision_pointer", "audit_pointer", "qualification_pointer"):
        value = record[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 2000 or "PENDING" in value.upper() \
                or not re.fullmatch(r"https://github\.com/BeautifulMind-JT/ai-ops-control-plane/"
                                    r"(?:pull/[1-9][0-9]*(?:#issuecomment-[1-9][0-9]*)?|"
                                    r"issues/[1-9][0-9]*(?:#issuecomment-[1-9][0-9]*)?|"
                                    r"actions/runs/[1-9][0-9]*(?:/job/[1-9][0-9]*)?)", value):
            raise BridgeError("protected program Astra adoption evidence is invalid")
    return record


def request_context(api, cfg, issue_number, pr_number=None, head=None, *, decision_only=False):
    issue, plan, node, status = prog.task_context(api, cfg, issue_number)
    prog.require_on_default_branch(api, status["plan_commit"])
    if not cfg.get("deployment_enabled") or "PENDING" in plan["approval_pointer"].upper():
        raise BridgeError("program scope/start approval or project eligibility is pending")
    tid = prog.task_id_for(plan["program"], node["id"])
    rows = prog.task_rows(cfg, tid)
    writer = prog.current_writer(rows)
    terminal = prog.released(writer) or (decision_only and bool(writer) and writer.get("state") == "RECONCILED"
                                        and writer.get("resolution") in cp.TERMINAL_RELEASES)
    if not writer or not terminal:
        raise BridgeError("current writer is not terminal at the host-recorded plan revision")
    context_commit = status["plan_commit"]
    if decision_only:
        # start can advance the canonical plan before prepare/admission fails.
        # Query the released writer's immutable revision, not that newer host
        # projection, so a failed prestart cannot strand semantic reconciliation.
        revision = re.fullmatch(r"p([0-9a-f]{12})-" + re.escape(writer["lane"]),
                                str(writer.get("task_revision", "")))
        if not revision:
            raise BridgeError("terminal writer has no canonical program revision")
        if not context_commit.startswith(revision.group(1)):
            resolved = api._request("GET", f"/commits/{revision.group(1)}")
            context_commit = (resolved or {}).get("sha")
            if not prog.SHA.fullmatch(str(context_commit)) or not context_commit.startswith(revision.group(1)):
                raise BridgeError("terminal writer plan revision is unknown or ambiguous")
            prog.require_on_default_branch(api, context_commit)
            prog.require_descendant(api, context_commit, status["plan_commit"])
            plan = prog.load_plan(api, cfg, context_commit)
            node = prog.plan_node(plan, node["id"])
            if plan["program"] != status["program"]:
                raise BridgeError("terminal writer plan changes the canonical program")
    elif writer.get("task_revision") != prog.expected_revision(status, writer):
        raise BridgeError("current writer is not terminal at the host-recorded plan revision")
    if any(row.get("state") in ("SUBMITTING", "CONFIRMED", "UNKNOWN") for row in rows):
        raise BridgeError("task has an active or unresolved session")
    if not decision_only and issue.get("body", "") != prog.rendered_body(issue, plan, node, status, writer["lane"]):
        raise BridgeError("canonical task body differs from the host-recorded plan")
    binding = {"repository": cfg["repository"], "issue": issue_number, "program": plan["program"],
               "node": node["id"], "plan_commit": context_commit,
               "task_revision": writer["task_revision"], "writer_launch": writer["launch_request_id"]}
    if node.get("astra_gate") == "RELEASE" and node.get("astra_auto_merge") is True:
        raise BridgeError("RELEASE authority cannot be delegated")
    if pr_number is not None:
        delivery = prog.pin_of(writer, "DELIVERY")
        if not delivery or (delivery.get("pr"), delivery.get("head")) != (pr_number, head):
            raise BridgeError("audit does not name the host-pinned current delivery")
        pr = prog.live_delivered_pr(api, cfg, delivery, tid)
        if pr.get("draft") is not False or (pr.get("base") or {}).get("ref") != prog.default_branch(api):
            raise BridgeError("audit needs a ready PR against the default branch")
        verdicts = prog.current_verdicts(cfg, tid, rows, writer, head)
        floor = prog.effective_floor(node["audit_floor"], verdicts)
        gate = node["astra_gate"] if node["astra_gate"] == "RELEASE" else \
            "ARCHITECTURE" if floor == "A3" else node["astra_gate"]
        if gate == "NONE":
            raise BridgeError("no Astra gate is required for this delivery")
        owners, passing = prog.writer_lanes(rows), set()
        for row, verdict in verdicts:
            if verdict["verdict"] not in ("PASS", "PASS_WITH_NOTES"):
                raise BridgeError("a current-head independent review has not passed")
            if prog.AUDIT_FLOORS.index(verdict["depth"]) >= min(prog.AUDIT_FLOORS.index(floor), 2) \
                    and row.get("lane") not in owners:
                passing.add(row["lane"])
        if len(passing) < prog.REQUIRED_REVIEWS[floor]:
            raise BridgeError("required current-head independent reviews are missing")
        binding.update(pr=pr_number, head=head, gate=gate, depth=floor)
    return binding, issue, plan, node, rows, writer


class Receipts(receipt_journal.Receipts):
    def __init__(self, root, tool_sha, secret_values=()):
        super().__init__(root, tool_sha, secret_values, trust=protected)


def operator_reconcile(ctx, fable, payload):
    """Separate root operator entry. Never reachable through program stdin."""
    fields = {"repository", "admission", "expected_version", "github_token"}
    if os.geteuid() != 0 or not isinstance(payload, dict) or set(payload) not in (fields, fields | {"quota_attempt"}) \
            or not isinstance(payload.get("repository"), str) \
            or not fable.REPOSITORY_RE.fullmatch(payload["repository"]) \
            or not isinstance(payload.get("github_token"), str) or not payload["github_token"] \
            or not receipt_journal.HEX.fullmatch(str(payload.get("admission", ""))) \
            or not receipt_journal.HEX.fullmatch(str(payload.get("expected_version", ""))):
        raise BridgeError("invalid root operator reconciliation input")
    os.environ["ASTRA_TARGET_REPOSITORY"] = payload["repository"]
    cfg = cp.load_config()
    fingerprint = installed_fingerprint(ctx)
    require_service_authorization(fingerprint)
    store = Receipts(ctx.runs_dir / "program", fingerprint, getattr(ctx, "secret_values", ()))
    parent_store = store
    if "quota_attempt" in payload:
        incident = payload["quota_attempt"]
        if not isinstance(incident, str) or not receipt_journal.HEX.fullmatch(incident):
            raise BridgeError("invalid operator quota incident selector")
        quota = quota_runtime.Quota(store, lambda run: fable.verify_failure_evidence(ctx, run),
                                   lambda: (_ for _ in ()).throw(BridgeError("operator entry never runs a model")),
                                   lambda *_: None)
        store = quota.operator_store(incident, payload["admission"])
    admission = store.admission(payload["admission"])
    if admission["binding"].get("repository") != cfg["repository"]:
        raise BridgeError("reconciliation admission targets another repository")
    if "quota_attempt" in payload:
        scope = receipt_journal.digest(list(parent_store.scope(admission["action"], admission["binding"])))
        with parent_store.locked(scope + ".scope.lock") as acquired:
            if not acquired:
                return {"status": "RUNNING"}
            with parent_store.locked("model.lock") as acquired:
                if not acquired:
                    return {"status": "BUSY"}
                return store.reconcile(payload["admission"], payload["expected_version"],
                                       lambda run: fable.verify_failure_evidence(ctx, run))
    return store.reconcile(payload["admission"], payload["expected_version"],
                           lambda run: fable.verify_failure_evidence(ctx, run))


def superseding_user_decision(api, cfg, issue, binding, old_node, proposed, proposed_plan, proposed_node, writer):
    """Validate an explicit node/digest decision in a separate merged plan PR.

    Program delivery cannot merge policy edits. Under the accepted shared-token
    boundary a separate plan PR is User-reserved; this does not cryptographically
    distinguish a malicious actor who already holds that privileged token.
    Arbitrary comment text, URL shape and whitespace edits are never decisions.
    """
    old_digest, new_digest = prog.node_definition_sha256(old_node), prog.node_definition_sha256(proposed_node)
    if old_digest == new_digest:
        return False
    records = proposed_plan.get("superseding_decisions", [])
    if not isinstance(records, list) or len(records) > 10000:
        raise BridgeError("superseding decision records must be a bounded list")
    pointer = proposed_plan["approval_pointer"]
    expected = {"schema_version": 1, "repository": cfg["repository"], "program": binding["program"],
                "node": binding["node"], "issue": binding["issue"], "writer_launch": binding["writer_launch"],
                "previous_plan_commit": binding["plan_commit"], "previous_definition_sha256": old_digest,
                "definition_sha256": new_digest, "decision_pointer": pointer}
    matched = [record for record in records if record == expected]
    if len(matched) != 1:
        return False
    match = re.fullmatch(r"https://github\.com/" + re.escape(cfg["repository"]) + r"/pull/([1-9][0-9]*)", pointer)
    if not match or pointer == issue.get("html_url"):
        return False
    number = int(match.group(1))
    delivery = prog.pin_of(writer, "DELIVERY")
    if delivery and number == delivery.get("pr"):
        return False
    pr = api._request("GET", f"/pulls/{number}")
    head = (pr.get("head") or {}).get("sha")
    if pr.get("merged") is not True or pr.get("state") != "closed" \
            or (pr.get("base") or {}).get("ref") != prog.default_branch(api) \
            or ((pr.get("head") or {}).get("repo") or {}).get("full_name") != cfg["repository"] \
            or (pr.get("head") or {}).get("ref") == prog.branch_for(prog.task_id_for(binding["program"], binding["node"])) \
            or not prog.SHA.fullmatch(str(head)) or not prog.SHA.fullmatch(str(pr.get("merge_commit_sha", ""))):
        return False
    prog.require_on_default_branch(api, head)
    merge_commit = pr["merge_commit_sha"]
    prog.require_on_default_branch(api, merge_commit)
    prog.require_descendant(api, binding["plan_commit"], head)
    prog.require_descendant(api, head, merge_commit)
    if prog.PLAN_PATH not in prog.changed_paths(api, pr, number):
        return False
    # The live merged revision must contain this exact record and definition;
    # a reused old plan PR cannot authorize a later arbitrary change.
    # A normal merge's latest path commit can be its branch head rather than
    # the merge SHA. Check the actual merge bytes as well, then require linear
    # ancestry with the requested latest path revision (never a divergent PR).
    if prog.load_plan(api, cfg, head) != proposed_plan or prog.load_plan(api, cfg, merge_commit) != proposed_plan:
        return False
    if proposed != merge_commit:
        comparison = api._request("GET", f"/compare/{merge_commit}...{proposed}")
        if (comparison or {}).get("status") not in ("ahead", "behind", "identical"):
            return False
    return True


def resolve_request(api, cfg, issue_number, operation, payload):
    repository = cfg["repository"]
    if operation in ("audit", "check"):
        pr, head = payload.get("pr"), payload.get("head")
        if type(pr) is not int or pr <= 0 or not isinstance(head, str) or not prog.SHA.fullmatch(head):
            raise BridgeError("audit/check require the exact PR and head")
        binding, issue, plan, node, rows, writer = request_context(api, cfg, issue_number, pr, head)
    elif operation == "decision-status":
        binding, issue, plan, node, rows, writer = request_context(api, cfg, issue_number, decision_only=True)
        proposed = payload.get("plan_commit")
        if proposed is not None:
            if not isinstance(proposed, str) or not prog.SHA.fullmatch(proposed):
                raise BridgeError("decision-status plan_commit must be an exact commit SHA")
            proposed_plan = prog.require_current_plan(api, cfg, proposed, binding["plan_commit"])
            if proposed_plan["program"] != plan["program"]:
                raise BridgeError("decision-status proposed plan changes the canonical program")
            proposed_node = prog.plan_node(proposed_plan, node["id"])
            prog.require_on_default_branch(api, proposed)
            prog.require_descendant(api, binding["plan_commit"], proposed)
            if proposed_plan["approval_pointer"] != plan["approval_pointer"] and \
                    superseding_user_decision(api, cfg, issue, binding, node, proposed, proposed_plan, proposed_node, writer):
                binding = {**binding, "plan_commit": proposed,
                           "task_revision": prog.task_revision_for(proposed, writer["lane"])}
            plan, node = proposed_plan, proposed_node
    else:
        binding, issue, plan, node, rows, writer = request_context(api, cfg, issue_number)
        question = payload.get("question")
        if type(question) is not int or question <= 0:
            raise BridgeError("consult requires the question comment id")
        url = f"https://github.com/{repository}/issues/{issue_number}#issuecomment-{question}"
        comment = api._request("GET", f"/issues/comments/{question}")
        if comment.get("issue_url") != f"https://api.github.com/repos/{repository}/issues/{issue_number}":
            raise BridgeError("question comment belongs to another task")
        candidates = [row for row in rows if prog.released(row) and row.get("evidence") == url]
        candidate = candidates[-1] if candidates else None
        pin = (candidate or {}).get("pin") or {}
        delivery = prog.pin_of(writer, "DELIVERY")
        valid_writer = candidate == writer and pin.get("kind") == "BLOCKER" \
            and pin.get("blocker") == "DECISION_REQUIRED"
        current_reviews = prog.current_verdicts(cfg, prog.task_id_for(plan["program"], node["id"]),
                                               rows, writer, delivery["head"]) if delivery else []
        link = re.search(r"(?m)^ASTRA_REVIEW_QUESTION_V1 review=([0-9a-f]{24}) head=([0-9a-f]{40})$",
                         comment.get("body", ""))
        linked = [(row, verdict) for row, verdict in current_reviews
                  if link and verdict.get("review") == link.group(1) and verdict.get("head") == link.group(2)
                  and (verdict.get("verdict") == "DECISION_REQUIRED" or verdict.get("contract_change") == "YES")]
        valid_review = bool(linked)
        if valid_review:
            candidate = linked[-1][0]
        if not valid_writer and not valid_review:
            raise BridgeError("question is not a current host-pinned decision request")
        source = delivery["head"] if valid_review else binding["plan_commit"]
        if not prog.SHA.fullmatch(source or ""):
            raise BridgeError("consult source has no exact SHA")
        binding.update(question=question, question_sha256=hashlib.sha256(comment.get("body", "").encode()).hexdigest(),
                       source=source, question_launch=candidate["launch_request_id"])
    return binding, issue, plan, node, rows, writer


def require_quota_context(action, api, cfg, context):
    binding, issue, plan, node, rows, writer = context
    if node.get("user_merge") is True or node.get("astra_gate") == "RELEASE":
        raise BridgeError("automatic quota readmission cannot cross User-only or RELEASE authority")
    if action == "audit":
        tid = prog.task_id_for(plan["program"], node["id"])
        verdicts = prog.current_verdicts(cfg, tid, rows, writer, binding["head"])
        if any(verdict.get("contract_change") == "YES" for _, verdict in verdicts):
            raise BridgeError("automatic quota readmission cannot settle a contract-change decision")
        if issue.get("labels") and {label.get("name") for label in issue["labels"]} & prog.BLOCKING_LABELS:
            raise BridgeError("quota audit has an unresolved product blocker")
        if prog.verification_reasons(api, cfg, binding["head"]):
            raise BridgeError("quota audit requires the same current-head verification gate as merge")
        if prog.pending_dependencies(api, cfg, plan, node):
            raise BridgeError("quota audit dependencies are not DONE")


def run(ctx, fable, payload):
    allowed = {"operation", "repository", "issue", "pr", "head", "question", "github_token", "plan_commit"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise BridgeError("invalid program Astra input")
    repository, operation, issue_number = payload.get("repository"), payload.get("operation"), payload.get("issue")
    if not isinstance(repository, str) or not fable.REPOSITORY_RE.fullmatch(repository) \
            or type(issue_number) is not int or issue_number <= 0 \
            or operation not in ("audit", "check", "consult", "consult-check", "decision-status", "quota-readiness", "quota-resume"):
        raise BridgeError("invalid program Astra target or operation")
    if not isinstance(payload.get("github_token"), str) or not payload["github_token"]:
        raise BridgeError("GitHub token is required on stdin")
    if "plan_commit" in payload and operation != "decision-status":
        raise BridgeError("only decision-status accepts a proposed plan_commit")
    # Only the allowlisted installed profiles are used. No caller-controlled root
    # path, PYTHONPATH, credential path, Git ref or model is accepted.
    for path in (cp.CONFIG_PATH, cp.CONFIG_PATH.with_name("projects.json")):
        protected(path)
        for directory in path.parents:
            protected(directory, directory=True)
    os.environ["ASTRA_TARGET_REPOSITORY"] = repository
    cfg = cp.load_config()
    fingerprint = installed_fingerprint(ctx)
    require_service_authorization(fingerprint)
    api = cp.GithubApi(repository, payload["github_token"])
    context_operation = operation
    if operation in ("quota-readiness", "quota-resume"):
        if payload.get("question") is not None:
            if payload.get("pr") is not None or payload.get("head") is not None:
                raise BridgeError("quota target cannot mix audit and consultation selectors")
            context_operation = "consult"
        else:
            context_operation = "audit"
    context = resolve_request(api, cfg, issue_number, context_operation, payload)
    if operation in ("quota-readiness", "quota-resume"):
        require_quota_context(context_operation, api, cfg, context)
    binding, issue, plan, node, rows, writer = context
    root = ctx.runs_dir / "program"
    if not root.exists():
        root.mkdir(mode=0o700)
    receipts = Receipts(root, fingerprint, (*getattr(ctx, "secret_values", ()), payload["github_token"]))
    def refresh(action, expected):
        old_target = os.environ.get("ASTRA_TARGET_REPOSITORY")
        try:
            target = expected["repository"]
            os.environ["ASTRA_TARGET_REPOSITORY"] = target
            current_cfg = cp.load_config()
            current_fingerprint = installed_fingerprint(ctx)
            require_service_authorization(current_fingerprint)
            current_api = cp.GithubApi(target, payload["github_token"])
            selector = {"pr": expected["pr"], "head": expected["head"]} if action == "audit" \
                else {"question": expected["question"]}
            current_context = resolve_request(current_api, current_cfg, expected["issue"], action, selector)
            require_quota_context(action, current_api, current_cfg, current_context)
            current_binding = current_context[0]
            return {"binding": current_binding, "tool_sha256": current_fingerprint}
        finally:
            if old_target is None:
                os.environ.pop("ASTRA_TARGET_REPOSITORY", None)
            else:
                os.environ["ASTRA_TARGET_REPOSITORY"] = old_target
    quota = quota_runtime.Quota(receipts, lambda run: fable.verify_failure_evidence(ctx, run),
                               lambda: fable.preflight(ctx, getattr(ctx, "claude_version", "")), refresh)
    action = "audit" if context_operation in ("audit", "check") else "consult"
    if operation == "decision-status":
        original = receipts.decision_status(binding)
        return original if original.get("status") != "CLEAR" else quota.decision_status(binding)
    if operation == "quota-readiness":
        return quota.readiness(action, binding)
    if operation in ("check", "consult-check"):
        unresolved = quota.unresolved(action, binding)
        if unresolved:
            return {"status": "UNKNOWN", "quota_attempt": unresolved,
                    "reason": "a quota retry execution is unresolved"}
        effective = quota.effective(action, binding)
        if effective.get("status") == "POSTED" and receipts.unresolved(action, binding):
            return {"status": "UNKNOWN", "reason": "an ancestor execution is unresolved"}
        return effective
    if action == "audit":
        def invoke():
            result = fable.audit(ctx, repository, binding["pr"], binding["head"], binding["gate"], binding["depth"],
                                 again=True,
                                 program_context={"binding": binding, "node": node,
                                                  "approval_pointer": plan["approval_pointer"]})
            return result
    else:
        def invoke():
            return fable.consult(ctx, repository, issue_number, binding["question"], ref=binding["source"], again=True,
                                 program_context={"binding": binding, "node": node,
                                                  "approval_pointer": plan["approval_pointer"]})
    if operation == "quota-resume":
        return quota.resume(action, binding, invoke)
    unresolved = quota.unresolved(action, binding)
    if unresolved:
        return {"status": "UNKNOWN", "quota_attempt": unresolved}
    existing = quota.effective(action, binding)
    if existing.get("status") != "MISSING":
        return existing
    queued = quota.fair_blocked(action, binding)
    if queued:
        return {"status": "QUEUED", "quota_attempt": queued,
                "reason": "a due quota retry precedes fresh model admission"}
    def guard():
        # Recheck at the actual model-admission linearization point. Time and
        # another task's known quota evidence can change after the outer query.
        incident = quota._ancestor(binding)
        if incident:
            return {"status": "UNKNOWN", "ancestor_admission": incident}
        due = quota.fair_blocked(action, binding)
        return {"status": "QUEUED", "quota_attempt": due} if due else None
    return receipts.execute(action, binding, invoke, admission_guard=guard)
