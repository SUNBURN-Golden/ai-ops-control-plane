#!/usr/bin/env python3
"""One-shot, PR-only migration. Requires Python 3.10+ and an authenticated gh CLI.
Default is read-only planning: emits a machine-readable plan (--plan FILE) binding
each repo's main SHA, migration branch HEAD SHA and preimage blob SHAs for every
path written or deleted. --apply re-validates the identical plan binding against
live remote state before any write; drift aborts and requires a re-plan.
Never merges, changes settings, enables runtime, invokes builders or retries sends.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import PurePosixPath
from urllib.parse import quote

OWNER = "BeautifulMind-JT"
SOURCE = f"{OWNER}/kix-protocol"
DEST = f"{OWNER}/ai-ops-control-plane"
SOURCE_SHA = "ec3f6db0d613385bfdf2392a4295f0099be1eec6"
SOURCE_TREE = "b328572528dc0c2d52b6df9ace39b376a25bd93e"
BRANCH = "ops/cp-extract-001"
TASK = f"https://github.com/{DEST}/issues/1"
STAMP = "engineering/migration-state.json"
PRODUCT_STAMP = ".github/control-plane-client.json"
PRODUCTS = [SOURCE, f"{OWNER}/ZARI", f"{OWNER}/film-unit-mv-studio", f"{OWNER}/maeum-gyeol"]
# P1-1: explicit per-repository AGENTS boundary markers. The marker must appear as
# an exact standalone line; the suffix after it (marker line included) is preserved
# byte-for-byte. No fuzzy or alternate headings may enlarge the split.
AGENTS_MARKERS = {
    SOURCE: "# Repository-specific engineering rules (preserved)",
    f"{OWNER}/ZARI": "# Repository-specific engineering rules (preserved)",
    f"{OWNER}/film-unit-mv-studio": "## Repository-specific engineering constraints",
    f"{OWNER}/maeum-gyeol": "## Repository-specific engineering constraints",
}
LOCKED = {
    "runtime/crates/kix-kernel/src/lib.rs": "69564b166f0c27f9af5d8422f0a466b18d74c20f",
    "runtime/crates/kix-kernel/tests/quarantine_capacity.rs": "b607996c83a119c349f1cc90469ac1ba82764e20",
}
SHARED_DOCS = {"RUNBOOKS/BOUNDARY.md", "RUNBOOKS/DISPATCH.md", "TASKS/TEMPLATE.md",
               "docs/CONTROL_PLANE_FLOW.md", "docs/CONTROL_PLANE_RUNTIME.md"}
# after_blob values with these reasons embed the destination import commit SHA,
# which only exists after the import commit is published; binding still enforced
# on repo/action/path/before_blob/main_sha/head_sha.
IMPORT_DEPENDENT_REASONS = {"pointer_note_installed", "product_stamp"}

class MigrationError(RuntimeError):
    pass

def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()

def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()

def selected(path):
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts:
        raise MigrationError("unsafe source path")
    return (path.startswith(".github/control-plane/")
            or (path.startswith(".github/workflows/control-plane-") and path.endswith(".yml"))
            or (p.parent.as_posix() == "scripts" and
                (p.name.startswith("control_plane") or p.name.startswith("test_control_plane"))
                and p.suffix in {".py", ".sh"})
            or path in SHARED_DOCS)

def marker_for(repo):
    try:
        return AGENTS_MARKERS[repo]
    except KeyError:
        raise MigrationError(f"{repo}: no pinned AGENTS boundary marker; refusing governance edit")

def split_agents(data, marker):
    """Split at the single exact standalone marker line. The local suffix
    (marker line included) is preserved byte-for-byte."""
    lines = data.decode("utf-8").split("\n")
    hits = [i for i, line in enumerate(lines) if line == marker]
    if len(hits) != 1:
        raise MigrationError("AGENTS boundary missing/ambiguous; preserve file and stop")
    i = hits[0]
    common = "\n".join(lines[:i]).rstrip() + "\n"
    local = "\n".join(lines[i:])
    return common.encode(), local.encode()

def clean_legacy_pointer(data):
    """Remove only the exact known E2 authority appendix, never product rules."""
    title = "## KIX control-plane SoT pointer (E2)"
    text = data.decode("utf-8")
    if title not in text:
        return data
    expected = (title + "\n\n"
        "Control-plane SoT, dispatch policy, and production activation are managed in "
        "**BeautifulMind-JT/kix-protocol**, not in this sibling tree.\n"
        "See [`docs/KIX_CONTROL_PLANE_POINTER.md`](docs/KIX_CONTROL_PLANE_POINTER.md).\n"
        "Do not enable sibling runtime, copy full policy, or change activation/workflows from this pointer PR.")
    if text.count(title) != 1 or expected not in text:
        raise MigrationError("unrecognized E2 appendix; preserve governance and stop")
    return text.replace(expected, "").encode()


def reset_activation(data):
    value = json.loads(data)
    value.update(user_activation_approval="NOT_APPROVED", user_activation_approval_pointer=None,
                 implementation_audit="PENDING", implementation_audit_pointer=None,
                 runner_preflight="PENDING", runner_preflight_pointer=None,
                 runtime_enabled=False, activated_runtime_sha="PENDING")
    return encoded(value)

def pointer(repo, imported_sha):
    return (f"# Shared engineering control plane\n\n"
            f"Shared source owner: `{DEST}`. This product is not the shared control-plane host.\n\n"
            f"Migration decision: {TASK}\n\n"
            f"Imported source candidate: https://github.com/{DEST}/tree/{imported_sha}/engineering\n"
            f"Target product: `{repo}`. Product contracts, tasks, locked files and product CI stay here.\n\n"
            "This is source/reference extraction, NOT production cutover. The destination import must\n"
            "be independently reviewed and merged first. Do not enable a runner, copy credentials,\n"
            "start a builder or assume KIX activation/audit evidence transfers. Preserve task, owner,\n"
            "request and ledger identities. Fence/drain legacy dispatch before retiring it; never run\n"
            "two dispatchers. Runtime identity separation and host/Slack cutover are separate gates.\n\n"
            "User-only merge; no automatic fallback, retries, polling or standing routines.\n").encode()

class GitHub:
    def __init__(self, apply=False):
        self.apply = apply
        self.cache = {}

    def api(self, path, method="GET", value=None):
        if method != "GET" and not self.apply:
            raise MigrationError("write attempted during read-only plan")
        args = ["gh", "api", "--hostname", "github.com", "-X", method, path]
        if value is not None:
            args += ["--input", "-"]
        try:
            p = subprocess.run(args, input=json.dumps(value) if value is not None else None,
                               text=True, capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise MigrationError("gh CLI missing; install/authenticate it without putting tokens in chat") from exc
        if p.returncode:
            raise MigrationError(f"GitHub {method} failed at {path}; stopped, no retry. Inspect gh auth/permissions locally.")
        return json.loads(p.stdout) if p.stdout.strip() else None

    def commit_tree(self, repo, sha):
        return self.api(f"repos/{repo}/commits/{sha}")["commit"]["tree"]["sha"]

    def main(self, repo):
        b = self.api(f"repos/{repo}/branches/main")
        return b["commit"]["sha"]

    def tree(self, repo, ref):
        t = self.api(f"repos/{repo}/git/trees/{ref}?recursive=1")
        if t.get("truncated"):
            raise MigrationError("truncated tree; refusing incomplete migration")
        return t["sha"], {x["path"]: x for x in t["tree"] if x["type"] == "blob"}

    def read(self, repo, entry):
        key = (repo, entry["sha"])
        if key not in self.cache:
            raw = self.api(f"repos/{repo}/git/blobs/{entry['sha']}")
            if raw.get("encoding") != "base64":
                raise MigrationError("unexpected blob encoding")
            data = base64.b64decode(raw["content"])
            if blob_sha(data) != entry["sha"]:
                raise MigrationError("source blob hash mismatch")
            self.cache[key] = data
        return self.cache[key]

    def working(self, repo):
        q = quote(f"{OWNER}:{BRANCH}", safe="")
        lineage = self.api(f"repos/{repo}/pulls?state=all&head={q}&base=main")
        if any(pr["state"] != "open" or not pr.get("draft") for pr in lineage):
            raise MigrationError("migration lineage is closed/merged/review-ready; do not reuse it")
        refs = self.api(f"repos/{repo}/git/matching-refs/heads/{BRANCH}")
        exact = [r for r in refs if r["ref"] == f"refs/heads/{BRANCH}"]
        if not exact:
            head = self.main(repo)
            tree, entries = self.tree(repo, head)
            return head, tree, entries, False
        head = exact[0]["object"]["sha"]
        tree, entries = self.tree(repo, head)
        stamp = STAMP if repo == DEST else PRODUCT_STAMP
        if stamp not in entries or json.loads(self.read(repo, entries[stamp])).get("migration_task") != TASK:
            raise MigrationError("existing branch not owned by this migration")
        return head, tree, entries, True

    def publish(self, repo, changes, title, body, expected_main=None, expected_head=None):
        head, tree, existing, exists = self.working(repo)
        if expected_main is not None and self.main(repo) != expected_main:
            raise MigrationError("main advanced; refusing stale migration proposal")
        if head != expected_head:
            raise MigrationError("working HEAD changed before publish; stop")
        elements = []
        for path, value in sorted(changes.items()):
            if value is None:
                if path in existing:
                    elements.append({"path": path, "mode": existing[path]["mode"], "type": "blob", "sha": None})
                continue
            mode, data = value
            sha = blob_sha(data)
            if path in existing and existing[path]["sha"] == sha and existing[path]["mode"] == mode:
                continue
            out = self.api(f"repos/{repo}/git/blobs", "POST",
                           {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            if out["sha"] != sha:
                raise MigrationError("destination blob hash mismatch")
            elements.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
        if elements:
            new_tree = self.api(f"repos/{repo}/git/trees", "POST", {"base_tree": tree, "tree": elements})["sha"]
            commit = self.api(f"repos/{repo}/git/commits", "POST",
                              {"message": title, "tree": new_tree, "parents": [head]})["sha"]
            observed, _, _, still_exists = self.working(repo)
            if observed != head or still_exists != exists or self.main(repo) != expected_main:
                raise MigrationError("remote moved during object preparation; no ref update")
            if exists:
                self.api(f"repos/{repo}/git/refs/heads/{BRANCH}", "PATCH", {"sha": commit, "force": False})
            else:
                self.api(f"repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{BRANCH}", "sha": commit})
            head = commit
        q = quote(f"{OWNER}:{BRANCH}", safe="")
        prs = self.api(f"repos/{repo}/pulls?state=open&head={q}&base=main")
        if len(prs) > 1:
            raise MigrationError("ambiguous PR lineage")
        if prs:
            if not prs[0].get("draft"):
                raise MigrationError("migration PR no longer draft; do not change reviewed lineage")
            pr = self.api(f"repos/{repo}/pulls/{prs[0]['number']}", "PATCH", {"title": title, "body": body})
        else:
            pr = self.api(f"repos/{repo}/pulls", "POST",
                          {"title": title, "body": body, "head": BRANCH, "base": "main", "draft": True})
        print(f"{repo}: {head} {pr['html_url']}")
        return head

def build_import(gh, source):
    changes, reasons, manifest = {}, {}, []
    for path, e in sorted(source.items()):
        if not selected(path) and path != "AGENTS.md":
            continue
        if e["mode"] not in {"100644", "100755"}:
            raise MigrationError("unexpected source mode")
        original = gh.read(SOURCE, e)
        data, reason = original, None
        if path == "AGENTS.md":
            data, _ = split_agents(original, marker_for(SOURCE))
            reason = "agents_common_extracted"
        elif path.endswith("/activation.json"):
            data = reset_activation(original)
            reason = "activation_reset"
        elif path == ".github/control-plane/config.json":
            v = json.loads(original)
            v.update(repository=DEST, project="CONTROL_PLANE")
            data = encoded(v)
            reason = "config_repinned_to_destination"
        elif path == ".github/control-plane/flow-policy.example.json":
            v = json.loads(original)
            v.update(enabled=False, deployment_audit="PENDING", deployment_approval_pointer=None,
                     source_sha256={}, repositories=[], registrations={}, review_routes={}, review_lanes={})
            for lane in v.get("lanes", {}).values():
                lane["enabled"] = False
            v["slack"]["projects"] = {}
            data = encoded(v)
            reason = "flow_policy_disabled"
        target = "engineering/" + path
        changes[target] = (e["mode"], data)
        if reason:
            reasons[target] = reason
        if data != original:
            prov = "engineering/provenance/kix/" + path
            changes[prov] = ("100644", original)
            reasons[prov] = "original_blob_preserved"
        entry = {"source_path": path, "source_blob": e["sha"], "destination_path": target,
                 "destination_blob": blob_sha(data), "mode": e["mode"], "transformed": data != original}
        if reason:
            entry["reason"] = reason
        if data != original:
            entry["original_preserved_path"] = prov
            entry["original_preserved_blob"] = e["sha"]
        manifest.append(entry)
    if not any(m["source_path"] == "scripts/control_plane.py" for m in manifest) or len(manifest) < 20:
        raise MigrationError("incomplete source selection")
    changes["engineering/source-manifest.json"] = ("100644", encoded({
        "schema_version": 1, "source_repository": SOURCE, "source_commit": SOURCE_SHA,
        "source_tree": SOURCE_TREE, "entries": manifest,
        "historical_tasks_and_validation": f"https://github.com/{SOURCE}/tree/{SOURCE_SHA}",
        "note": "Historical task/validation files remain at the original immutable commit; no history rewritten."}))
    reasons["engineering/source-manifest.json"] = "manifest_generated"
    changes[STAMP] = ("100644", encoded({"migration_task": TASK, "source_commit": SOURCE_SHA,
                                       "stage": "SOURCE_IMPORTED_NOT_DEPLOYED", "runtime_enabled": False}))
    reasons[STAMP] = "migration_stamp"
    return changes, reasons

def product_changes(gh, repo, entries, imported_sha):
    if "AGENTS.md" not in entries:
        raise MigrationError(f"{repo}: missing AGENTS; refuse to replace unknown governance")
    _, local = split_agents(gh.read(repo, entries["AGENTS.md"]), marker_for(repo))
    local = clean_legacy_pointer(local)
    note = pointer(repo, imported_sha)
    changes, reasons = {}, {}
    for p in entries:
        if selected(p):
            changes[p] = None
            reasons[p] = "shared_control_plane_removed"
    # Templates are project-local task inputs; preserve their exact fields and eligibility rules.
    changes.pop("TASKS/TEMPLATE.md", None)
    reasons.pop("TASKS/TEMPLATE.md", None)
    header = (f"# Product agent governance\n\nShared engineering source is maintained in `{DEST}`.\n"
              f"See `docs/CONTROL_PLANE_POINTER.md` and `{PRODUCT_STAMP}` for the pinned candidate.\n"
              "The source import is not runtime activation. User-only merge, non-author exact-HEAD\n"
              "review, single writer, UNKNOWN fencing, no polling and no automatic retry remain required.\n"
              "If shared policy and project contracts conflict, stop with DECISION_REQUIRED.\n\n").encode()
    changes["AGENTS.md"] = ("100644", header + local)
    reasons["AGENTS.md"] = "agents_header_rewrite_local_preserved"
    for path in ("docs/CONTROL_PLANE_POINTER.md", "RUNBOOKS/DISPATCH.md",
                 "docs/CONTROL_PLANE_RUNTIME.md", "docs/CONTROL_PLANE_FLOW.md", "RUNBOOKS/BOUNDARY.md"):
        if path == "docs/CONTROL_PLANE_POINTER.md" or path in entries:
            changes[path] = ("100644", note)
            reasons[path] = "pointer_note_installed"
    if "docs/KIX_CONTROL_PLANE_POINTER.md" in entries:
        changes["docs/KIX_CONTROL_PLANE_POINTER.md"] = ("100644", note)
        reasons["docs/KIX_CONTROL_PLANE_POINTER.md"] = "pointer_note_installed"
    changes[PRODUCT_STAMP] = ("100644", encoded({"schema_version": 1, "migration_task": TASK,
        "repository": repo, "control_repository": DEST, "control_source_commit": imported_sha,
        "control_source_subdirectory": "engineering", "status": "SOURCE_ONLY_PENDING_REVIEW_AND_CUTOVER",
        "runtime_enabled": False}))
    reasons[PRODUCT_STAMP] = "product_stamp"
    if repo.endswith("/ZARI") and "docs/IMPLEMENTATION_STATUS.md" in entries:
        old = gh.read(repo, entries["docs/IMPLEMENTATION_STATUS.md"])
        token = b"<!-- CP-EXTRACT-001 -->"
        if token not in old:
            changes["docs/IMPLEMENTATION_STATUS.md"] = ("100644", old + b"\n\n" + token + b"\n"
                + b"Shared engineering source extraction candidate; application code unchanged.\n"
                + f"Tracking: {TASK}\n".encode()
                + b"Source/PR preparation only. No application test PASS, independent audit, deployment or activation claimed.\n")
            reasons["docs/IMPLEMENTATION_STATUS.md"] = "status_note_appended"
    for path, expected in LOCKED.items():
        if repo == SOURCE and (entries.get(path, {}).get("sha") != expected or path in changes):
            raise MigrationError("locked KIX blob mismatch")
    return changes, reasons

# --- P2: machine-readable plan, binding, and post-publish verification ---

def actions_for(repo, changes, reasons, entries, main_sha, head_sha):
    out = []
    for path, value in sorted(changes.items()):
        before = entries.get(path, {}).get("sha")
        base = {"repo": repo, "path": path, "before_blob": before,
                "main_sha": main_sha, "head_sha": head_sha}
        if value is None:
            if before is None:
                continue
            out.append({**base, "action": "delete", "after_blob": None,
                        "reason": reasons.get(path, "shared_control_plane_removed")})
            continue
        mode, data = value
        after = blob_sha(data)
        action = "add" if before is None else ("preserve" if before == after else "update")
        a = {**base, "action": action, "after_blob": after}
        if path in reasons:
            a["reason"] = reasons[path]
        out.append(a)
    if repo == SOURCE:
        for path, sha in sorted(LOCKED.items()):
            out.append({"repo": repo, "path": path, "action": "preserve",
                        "before_blob": sha, "after_blob": sha,
                        "main_sha": main_sha, "head_sha": head_sha,
                        "reason": "locked_kix_blob"})
    return out

def plan_id(bindings, actions):
    payload = json.dumps({"bindings": bindings, "actions": actions}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

def reject_overwrite(changes, baseline, working):
    """A task stamp is not permission to overwrite branch-local source edits."""
    for path, value in changes.items():
        before = baseline.get(path, {}).get("sha")
        current = working.get(path, {}).get("sha")
        desired = None if value is None else blob_sha(value[1])
        if current != before and current != desired:
            raise MigrationError(f"branch-local change would be overwritten: {path}")

def build_plan(gh):
    """Read-only. Returns a plan dict binding every write/delete to remote state.
    plan['_changes'] maps repo -> (changes, reasons, preimage entries) for apply."""
    if gh.main(SOURCE) != SOURCE_SHA:
        raise MigrationError("KIX main advanced beyond pinned source; re-plan instead of silently rebasing")
    if gh.commit_tree(SOURCE, SOURCE_SHA) != SOURCE_TREE:
        raise MigrationError("source tree mismatch")
    _, source = gh.tree(SOURCE, SOURCE_TREE)
    bindings, actions, prepared = {}, [], {}
    dmain = gh.main(DEST)
    dhead, _, dentries, dexists = gh.working(DEST)
    changes, reasons = build_import(gh, source)
    # Import only once. Adaptation must never be overwritten by a fresh import.
    if "engineering/source-manifest.json" in dentries:
        raise MigrationError("source already imported; reconcile/adapt, do not reapply")
    for path, value in changes.items():
        if path != STAMP and path in dentries and dentries[path]["sha"] != blob_sha(value[1]):
            raise MigrationError(f"existing destination path conflicts: {path}")
    actions += actions_for(DEST, changes, reasons, dentries, dmain, dhead)
    bindings[DEST] = {"main_sha": dmain, "head_sha": dhead}
    prepared[DEST] = (changes, dentries)
    # Preflight every product before the first write, not halfway through migration.
    for repo in PRODUCTS:
        main_sha = gh.main(repo)
        _, entries = gh.tree(repo, main_sha)
        phead, _, pentries, pexists = gh.working(repo)
        changes, reasons = product_changes(gh, repo, entries, "PENDING_IMPORT_COMMIT")
        head_sha = phead if pexists else None
        basis = pentries if pexists else entries
        reject_overwrite(changes, entries, basis)
        actions += actions_for(repo, changes, reasons, basis, main_sha, head_sha)
        bindings[repo] = {"main_sha": main_sha, "head_sha": head_sha}
        prepared[repo] = entries
    plan = {"schema_version": 1, "task": TASK, "branch": BRANCH,
            "source": {"repository": SOURCE, "commit": SOURCE_SHA, "tree": SOURCE_TREE},
            "note": "after_blob for pointer_note_installed/product_stamp embeds the destination "
                    "import commit and is finalized at apply; binding fields are authoritative.",
            "bindings": bindings, "actions": actions}
    plan["plan_id"] = plan_id(bindings, actions)
    plan["_changes"] = prepared
    return plan

def check_plan(gh, plan):
    """Re-validate a reviewed plan against live remote state. Any drift in main
    SHA, branch HEAD SHA or preimage blob for a targeted path aborts."""
    if plan.get("task") != TASK or plan.get("schema_version") != 1:
        raise MigrationError("plan does not belong to this migration")
    if plan_id(plan.get("bindings"), plan.get("actions")) != plan.get("plan_id"):
        raise MigrationError("plan integrity check failed; re-plan")
    fresh = build_plan(gh)
    if fresh["plan_id"] != plan["plan_id"]:
        raise MigrationError("plan binding drifted (main/head/preimage changed); re-plan required")
    return fresh

def check_actions(planned, actual):
    """Per-repo re-validation at apply: same targets, same binding fields.
    after_blob may differ only for import-dependent reasons."""
    pa = {(a["repo"], a["path"]): a for a in planned}
    aa = {(a["repo"], a["path"]): a for a in actual}
    if set(pa) != set(aa):
        raise MigrationError("plan binding drifted (targets changed); re-plan required")
    for key, p in pa.items():
        a = aa[key]
        for field in ("action", "before_blob", "main_sha", "head_sha"):
            if p[field] != a[field]:
                raise MigrationError("plan binding drifted; re-plan required")
        if p["after_blob"] != a["after_blob"] and p.get("reason") not in IMPORT_DEPENDENT_REASONS:
            raise MigrationError("plan binding drifted; re-plan required")

def verify_remote(gh, repo, head, changes):
    """After publish: remote HEAD, tree blobs, deletions and locked KIX blobs."""
    refs = gh.api(f"repos/{repo}/git/matching-refs/heads/{BRANCH}")
    exact = [r for r in refs if r["ref"] == f"refs/heads/{BRANCH}"]
    if not exact or exact[0]["object"]["sha"] != head:
        raise MigrationError(f"{repo}: remote HEAD mismatch after publish")
    _, entries = gh.tree(repo, head)
    for path, value in changes.items():
        if value is None:
            if path in entries:
                raise MigrationError(f"{repo}: deletion not reflected on remote: {path}")
        else:
            mode, data = value
            if entries.get(path, {}).get("sha") != blob_sha(data) or entries[path]["mode"] != mode:
                raise MigrationError(f"{repo}: published blob mismatch: {path}")
    if repo == SOURCE:
        for path, sha in LOCKED.items():
            if entries.get(path, {}).get("sha") != sha:
                raise MigrationError("locked KIX blob changed on remote")

def public_plan(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write only migration branches and draft PRs")
    parser.add_argument("--plan", default="cp-extract-001-plan.json",
                        help="plan file: written in read-only mode, required input for --apply")
    args = parser.parse_args()
    gh = GitHub(args.apply)
    if args.apply:
        try:
            with open(args.plan) as f:
                plan = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(f"cannot load reviewed plan {args.plan}: {exc}")
        plan = check_plan(gh, plan)
    else:
        plan = build_plan(gh)
        with open(args.plan, "w") as f:
            json.dump(public_plan(plan), f, indent=2)
            f.write("\n")
    dest_changes = plan["_changes"][DEST][0]
    print(f"Pinned source: {SOURCE}@{SOURCE_SHA}; selected destination files: {len(dest_changes)}")
    print(f"Plan {plan['plan_id']}: {len(plan['actions'])} actions across {len(plan['bindings'])} repos.")
    print("No runtime/host/Slack/credentials/ledger/merge actions are performed.")
    if not args.apply:
        print(f"READ-ONLY PLAN written to {args.plan}. Re-run once with --apply --plan {args.plan} to publish draft PRs.")
        return
    body = (f"Refs {TASK}\n\nExact source imported from `{SOURCE}@{SOURCE_SHA}` under `engineering/`.\n"
            "See source-manifest.json for source/destination Git blobs and explicit transforms.\n"
            "Activation is reset to NOT_APPROVED/PENDING/false. Nested workflows are inert source.\n"
            "Legacy KIX tests/fixtures and source path assumptions are preserved, NOT a claim of\n"
            "cross-repository runtime readiness. No product code, secret, runner, ledger or Slack change.\n"
            "Required: exact-HEAD non-author audit/CI, User merge; separate runtime identity and cutover task.\n"
            "The author cannot self-approve. No application or live-provider tests claimed.\n")
    imported_sha = gh.publish(DEST, dest_changes,
                              "refactor: import shared engineering control plane from KIX (disabled)",
                              body, plan["bindings"][DEST]["main_sha"], plan["bindings"][DEST]["head_sha"])
    verify_remote(gh, DEST, imported_sha, dest_changes)
    for repo in PRODUCTS:
        main_sha = gh.main(repo)
        _, entries = gh.tree(repo, main_sha)
        phead, _, pentries, pexists = gh.working(repo)
        change, reasons = product_changes(gh, repo, entries, imported_sha)
        basis = pentries if pexists else entries
        actual = actions_for(repo, change, reasons, basis, main_sha, phead if pexists else None)
        check_actions([a for a in plan["actions"] if a["repo"] == repo], actual)
        text = (f"Refs {TASK}\n\nBase `{main_sha}`. Imported source candidate `{DEST}@{imported_sha}`.\n"
                "Remove shared scripts/policies/workflows from this product; retain project governance,\n"
                "immutable tasks, product source/CI, historical evidence and local task-template fields.\n"
                "This PR MUST remain draft until destination import is reviewed/merged and any legacy\n"
                "dispatcher is drained/fenced. Do not retire an active legacy runner by merging blindly.\n"
                "No runtime enablement or renewed FILM UNIT/마음결 rollout. No audit/CI PASS claimed.\n"
                "User-only merge. Runtime identity separation, ledger continuity, host/Slack cutover\n"
                "and a separately authorized canary remain independent gates.\n")
        head = gh.publish(repo, change, "refactor: remove shared control-plane ownership from product",
                          text, main_sha, phead)
        verify_remote(gh, repo, head, change)
    print("Draft source/consumer PR preparation complete. Nothing merged or deployed.")

if __name__ == "__main__":
    try:
        main()
    except (MigrationError, KeyError, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        sys.exit(1)

