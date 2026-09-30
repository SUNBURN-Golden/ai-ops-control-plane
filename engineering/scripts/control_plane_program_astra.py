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


class BridgeError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def protected(path, directory=False):
    info = os.lstat(path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise BridgeError("program Astra state must be root-owned and not group/other writable")


def request_context(api, cfg, issue_number, pr_number=None, head=None):
    issue, plan, node, status = prog.task_context(api, cfg, issue_number)
    prog.require_on_default_branch(api, status["plan_commit"])
    if not cfg.get("deployment_enabled") or "PENDING" in plan["approval_pointer"].upper():
        raise BridgeError("program scope/start approval or project eligibility is pending")
    tid = prog.task_id_for(plan["program"], node["id"])
    rows = prog.task_rows(cfg, tid)
    writer = prog.current_writer(rows)
    if not writer or not prog.released(writer) or writer.get("task_revision") != prog.expected_revision(status, writer):
        raise BridgeError("current writer is not terminal at the host-recorded plan revision")
    if any(row.get("state") in ("SUBMITTING", "CONFIRMED", "UNKNOWN") for row in rows):
        raise BridgeError("task has an active or unresolved session")
    if issue.get("body", "") != prog.rendered_body(issue, plan, node, status, writer["lane"]):
        raise BridgeError("canonical task body differs from the host-recorded plan")
    binding = {"repository": cfg["repository"], "issue": issue_number, "program": plan["program"],
               "node": node["id"], "plan_commit": status["plan_commit"],
               "task_revision": writer["task_revision"], "writer_launch": writer["launch_request_id"]}
    if pr_number is not None:
        delivery = prog.pin_of(writer, "DELIVERY")
        if not delivery or (delivery.get("pr"), delivery.get("head")) != (pr_number, head):
            raise BridgeError("audit does not name the host-pinned current delivery")
        pr = prog.live_delivered_pr(api, cfg, delivery, tid)
        if pr.get("draft") is not False or (pr.get("base") or {}).get("ref") != prog.default_branch(api):
            raise BridgeError("audit needs a ready PR against the default branch")
        verdicts = prog.current_verdicts(cfg, tid, rows, writer, head)
        floor = prog.effective_floor(node["audit_floor"], verdicts)
        gate = "ARCHITECTURE" if floor == "A3" else node["astra_gate"]
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


class Receipts:
    """Root-owned, fail-closed admission and results; one request per exact binding.

    A crash or lost result leaves RUNNING/UNKNOWN. It is never auto-resubmitted.
    A changed head/revision/question gets a different key; old authority is not
    transferred. No user-supplied path, run id or arbitrary GitHub verdict is read.
    """
    def __init__(self, root, tool_sha):
        self.root, self.tool_sha = Path(root), tool_sha
        protected(self.root, directory=True)

    def key(self, action, binding):
        return hashlib.sha256(canonical({"action": action, "binding": binding,
                                        "tool_sha256": self.tool_sha}).encode()).hexdigest()

    def read(self, action, binding):
        path = self.root / (self.key(action, binding) + ".json")
        if not path.exists():
            return {"status": "MISSING"}
        protected(path)
        try:
            value = json.loads(path.read_text())
        except (ValueError, OSError):
            raise BridgeError("program Astra receipt is unreadable") from None
        if not isinstance(value, dict) or value.get("action") != action or value.get("binding") != binding \
                or value.get("tool_sha256") != self.tool_sha:
            raise BridgeError("program Astra receipt binding is invalid")
        return value

    def write(self, action, binding, result):
        path = self.root / (self.key(action, binding) + ".json")
        value = {"action": action, "binding": binding, "tool_sha256": self.tool_sha, **result}
        fd, temporary = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return value

    def execute(self, action, binding, invoke):
        lock_path = self.root / (self.key(action, binding) + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            protected(lock_path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "RUNNING"}
            previous = self.read(action, binding)
            if previous["status"] != "MISSING":
                return previous if previous["status"] != "RUNNING" else {**previous, "status": "UNKNOWN"}
            global_fd = os.open(self.root / "model.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                protected(self.root / "model.lock")
                try:
                    fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"status": "BUSY"}  # no request has been admitted or model invoked
                return self._invoke(action, binding, invoke)
            finally:
                os.close(global_fd)
        finally:
            os.close(fd)

    def _invoke(self, action, binding, invoke):
        self.write(action, binding, {"status": "RUNNING"})
        try:
            result = invoke()
            if result.get("status") != "POSTED" or result.get("program_binding") != binding:
                raise BridgeError("Fable result is not a bound durable POSTED receipt")
            return self.write(action, binding, result)
        except Exception as exc:
            self.write(action, binding, {"status": "ERROR", "reason": type(exc).__name__})
            raise

    def decision_status(self, binding):
        for path in self.root.glob("*.json"):
            protected(path)
            value = json.loads(path.read_text())
            if value.get("action") not in ("consult", "audit"):
                continue
            if any(value.get("binding", {}).get(key) != val for key, val in binding.items()):
                continue
            if value.get("status") != "POSTED" or value.get("result") in ("USER_REQUIRED", "DECISION_REQUIRED") \
                    or value.get("scope_result") == "USER_REQUIRED":
                return {"status": "BLOCKED", "reason": "Fable decision is unresolved or requires the User"}
        return {"status": "CLEAR"}  # no new grant; the existing Opus/User path still applies


def run(ctx, fable, payload):
    allowed = {"operation", "repository", "issue", "pr", "head", "question", "github_token"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise BridgeError("invalid program Astra input")
    repository, operation, issue_number = payload.get("repository"), payload.get("operation"), payload.get("issue")
    if not isinstance(repository, str) or not fable.REPOSITORY_RE.fullmatch(repository) \
            or type(issue_number) is not int or issue_number <= 0 \
            or operation not in ("audit", "check", "consult", "consult-check", "decision-status"):
        raise BridgeError("invalid program Astra target or operation")
    if not isinstance(payload.get("github_token"), str) or not payload["github_token"]:
        raise BridgeError("GitHub token is required on stdin")
    # Only the allowlisted installed profiles are used. No caller-controlled root
    # path, PYTHONPATH, credential path, Git ref or model is accepted.
    for path in (cp.CONFIG_PATH, cp.CONFIG_PATH.with_name("projects.json")):
        protected(path)
        for directory in path.parents:
            protected(directory, directory=True)
    os.environ["ASTRA_TARGET_REPOSITORY"] = repository
    cfg = cp.load_config()
    api = cp.GithubApi(repository, payload["github_token"])
    if operation in ("audit", "check"):
        pr, head = payload.get("pr"), payload.get("head")
        if type(pr) is not int or pr <= 0 or not isinstance(head, str) or not prog.SHA.fullmatch(head):
            raise BridgeError("audit/check require the exact PR and head")
        binding, issue, plan, node, rows, writer = request_context(api, cfg, issue_number, pr, head)
    elif operation == "decision-status":
        binding, issue, plan, node, rows, writer = request_context(api, cfg, issue_number)
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
    root = ctx.runs_dir / "program"
    if not root.exists():
        root.mkdir(mode=0o700)
    fingerprint = hashlib.sha256((ctx.tool_sha256 + hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + "".join(
        hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for module in (cp, prog))).encode()).hexdigest()
    receipts = Receipts(root, fingerprint)
    if operation == "check":
        return receipts.read("audit", binding)
    if operation == "consult-check":
        return receipts.read("consult", binding)
    if operation == "decision-status":
        return receipts.decision_status(binding)
    if operation == "audit":
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
    return receipts.execute(operation, binding, invoke)
