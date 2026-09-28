#!/usr/bin/env python3
"""GitHub task ownership and one-shot grants shared by independent compute hosts.

State lives in commit messages on a reserved control branch, never source files or
main. Ref creation and non-forced fast-forward updates serialize honest controllers.
No lease, deletion, force update, implicit takeover, or model-visible write token.
"""
import copy
import hashlib
import json
import re
import uuid
from urllib.error import HTTPError

REPOSITORY = "BeautifulMind-JT/ai-ops-control-plane"
PREFIX = "heads/aiops-ownership/"
MARKER = "ASTRA_HOST_OWNERSHIP_V1\n"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
TERMINAL = {"FINISHED", "FAILED_PRESTART"}


class OwnershipError(RuntimeError):
    pass


class Uncertain(OwnershipError):
    """A remote write may have happened. No external action may follow."""


def require(value, message):
    if not value:
        raise OwnershipError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def task_key(repository, issue):
    require(isinstance(repository, str) and re.fullmatch(r"BeautifulMind-JT/[A-Za-z0-9_.-]+", repository),
            "invalid canonical repository")
    require(type(issue) is int and issue > 0, "canonical issue required")
    # Revision/host/task label intentionally excluded: aliases cannot create a new owner.
    return digest([repository.lower(), issue])


def host_identity(host, instance):
    require(isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", host), "invalid host")
    require(isinstance(instance, str) and re.fullmatch(r"[a-f0-9-]{36}", instance), "installed host instance required")
    return dict(host_id=host, instance_id=instance)


class Registry:
    def __init__(self, api):
        self.api = api
        self.prefix = "repos/" + REPOSITORY + "/git/"

    def read(self, repository, issue):
        ref = PREFIX + task_key(repository, issue)
        try:
            value = self.api.call("GET", self.prefix + "ref/" + ref)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        require(value.get("ref") == "refs/" + ref and value.get("object", {}).get("type") == "commit",
                "ownership reference mismatch")
        head = value["object"]["sha"]
        commit = self.api.call("GET", self.prefix + "commits/" + head)
        message = commit.get("message", "")
        require(commit.get("sha") == head and commit.get("tree", {}).get("sha") == EMPTY_TREE and
                message.startswith(MARKER), "invalid ownership record")
        state = json.loads(message[len(MARKER):])
        require(state.get("schema") == 1 and state.get("repository", "").lower() == repository.lower() and
                state.get("issue") == issue and isinstance(state.get("actions"), dict), "wrong task ownership")
        return head, state

    def write(self, repository, issue, old_head, state):
        # Each proposed commit has exactly the observed parent. Competing proposals
        # are siblings, so force=false rejects the loser rather than overwriting it.
        ref = PREFIX + task_key(repository, issue)
        try:
            state = dict(state, mutation_id=uuid.uuid4().hex)
            tree = self.api.call("POST", self.prefix + "trees", {"tree": []})
            require(tree.get("sha") == EMPTY_TREE, "unexpected control tree")
            commit = self.api.call("POST", self.prefix + "commits", dict(
                message=MARKER + canonical(state), tree=EMPTY_TREE,
                parents=[] if old_head is None else [old_head]))
            head = commit["sha"]
            if old_head is None:
                result = self.api.call("POST", self.prefix + "refs", dict(ref="refs/" + ref, sha=head))
            else:
                result = self.api.call("PATCH", self.prefix + "refs/" + ref, dict(sha=head, force=False))
            require(result.get("ref") == "refs/" + ref and result.get("object", {}).get("sha") == head,
                    "ownership write receipt mismatch")
            return head
        except Exception as exc:
            # Includes conflict and response loss; do not retry a CAS or infer no grant.
            raise Uncertain("ownership write unconfirmed; read/reconcile, never launch or resend") from exc

    def acquire(self, assignment):
        repository, issue = assignment["repository"], assignment["issue"]
        owner = host_identity(assignment["host_id"], assignment["instance_id"])
        require(assignment.get("admission") == "FRESH_TASK" and assignment.get("active") is True,
                "first ownership requires explicitly assigned fresh work; legacy work needs fenced import")
        current = self.read(repository, issue)
        if current:
            head, state = current
            require(state["owner"] == owner and state["assignment_digest"] == digest(assignment),
                    "task already assigned; no automatic takeover or assignment rewrite")
            return head, state
        state = dict(schema=1, repository=repository, issue=issue, owner=owner, epoch=1,
                     assignment_digest=digest(assignment), assignment=assignment, actions={})
        return self.write(repository, issue, None, state), state

    def owned(self, assignment):
        current = self.read(assignment["repository"], assignment["issue"])
        require(current is not None, "task has no owner")
        head, state = current
        require(state["owner"] == host_identity(assignment["host_id"], assignment["instance_id"]) and
                state["assignment_digest"] == digest(assignment), "wrong host/assignment")
        return head, state

    def continuity(self, assignment, checkpoint):
        head, current = self.owned(assignment)
        cursor, newer = head, current
        for _ in range(256):
            if cursor == checkpoint:
                return head, current
            commit = self.api.call("GET", self.prefix + "commits/" + cursor)
            parents = commit.get("parents", [])
            require(len(parents) == 1, "ownership history replaced or truncated")
            parent = parents[0]["sha"] if isinstance(parents[0], dict) else parents[0]
            old_commit = self.api.call("GET", self.prefix + "commits/" + parent)
            require(old_commit.get("tree", {}).get("sha") == EMPTY_TREE and
                    old_commit.get("message", "").startswith(MARKER), "invalid ownership ancestor")
            older = json.loads(old_commit["message"][len(MARKER):])
            for field in ("owner", "assignment_digest", "repository", "issue", "epoch", "schema"):
                require(older[field] == newer[field], "ownership identity changed")
            for key, action in older["actions"].items():
                next_action = newer["actions"].get(key, {})
                require(all(next_action.get(k) == action[k] for k in ("operation", "payload_digest", "epoch")),
                        "ownership action history removed/changed")
                require(next_action.get("state") in TERMINAL | {"SUBMITTING"} and
                        (action["state"] not in TERMINAL or action == next_action), "terminal history changed")
            cursor, newer = parent, older
        raise OwnershipError("ownership history exceeds bounded reconciliation window")

    def failed_prestart(self, assignment, action_id, operation, payload, evidence):
        # Explicit operator proof, not a retry or a new execution grant.
        head, current = self.owned(assignment)
        if action_id not in current["actions"]:
            proposed = copy.deepcopy(current)
            proposed["actions"][action_id] = dict(operation=operation, payload_digest=digest(payload),
                                                  epoch=current["epoch"], state="SUBMITTING")
            self.write(assignment["repository"], assignment["issue"], head, proposed)
        return self.finish(assignment, action_id, evidence, state="FAILED_PRESTART")

    def begin(self, assignment, action_id, operation, payload):
        require(re.fullmatch(r"[a-f0-9]{64}", action_id or ""), "invalid action ID")
        require(operation in {"WORK_DIAGNOSTIC", "BUILDER", "REVIEW"}, "unsupported host action")
        head, state = self.owned(assignment)
        old = state["actions"].get(action_id)
        binding = dict(operation=operation, payload_digest=digest(payload), epoch=state["epoch"])
        if old:
            require(all(old[k] == v for k, v in binding.items()), "action identity reused with different input")
            return dict(start_allowed=False, state=old["state"], action_id=action_id)
        require(all(a["state"] in TERMINAL for a in state["actions"].values()),
                "unresolved previous action; no revision/host/session replacement")
        proposed = copy.deepcopy(state)
        proposed["actions"][action_id] = dict(binding, state="SUBMITTING")
        receipt = self.write(assignment["repository"], assignment["issue"], head, proposed)
        return dict(start_allowed=True, state="SUBMITTING", action_id=action_id, ownership_sha=receipt)

    def finish(self, assignment, action_id, evidence, *, state="FINISHED"):
        require(state in TERMINAL and isinstance(evidence, dict) and
                evidence.get("sender_fenced") is True and evidence.get("session_ended") is True and
                evidence.get("publication_resolved") is True and
                re.fullmatch(r"https://github\.com/BeautifulMind-JT/[A-Za-z0-9_.-]+/(issues|pull)/[1-9][0-9]*#issuecomment-[1-9][0-9]*",
                             evidence.get("pointer", "")), "terminal/fence/publication evidence required")
        head, current = self.owned(assignment)
        require(action_id in current["actions"], "unknown action")
        old = current["actions"][action_id]
        if old["state"] in TERMINAL:
            require(old["state"] == state and old.get("evidence") == evidence, "terminal evidence changed")
            return head
        proposed = copy.deepcopy(current)
        proposed["actions"][action_id].update(state=state, evidence=evidence)
        return self.write(assignment["repository"], assignment["issue"], head, proposed)

