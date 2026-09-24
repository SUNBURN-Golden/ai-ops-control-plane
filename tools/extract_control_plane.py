#!/usr/bin/env python3
"""One-shot, PR-only migration. Requires Python 3.10+ and an authenticated gh CLI.
Default is read-only planning. --apply creates/updates this migration's branches.
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
MARKER = "# Repository-specific engineering rules (preserved)"
STAMP = "engineering/migration-state.json"
PRODUCT_STAMP = ".github/control-plane-client.json"
PRODUCTS = [SOURCE, f"{OWNER}/ZARI", f"{OWNER}/film-unit-mv-studio", f"{OWNER}/maeum-gyeol"]
LOCKED = {
    "runtime/crates/kix-kernel/src/lib.rs": "69564b166f0c27f9af5d8422f0a466b18d74c20f",
    "runtime/crates/kix-kernel/tests/quarantine_capacity.rs": "b607996c83a119c349f1cc90469ac1ba82764e20",
}
SHARED_DOCS = {"RUNBOOKS/BOUNDARY.md", "RUNBOOKS/DISPATCH.md", "TASKS/TEMPLATE.md",
               "docs/CONTROL_PLANE_FLOW.md", "docs/CONTROL_PLANE_RUNTIME.md"}

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

def split_agents(data):
    text = data.decode("utf-8")
    if text.count(MARKER) != 1:
        raise MigrationError("AGENTS boundary missing/ambiguous; preserve file and stop")
    common, local = text.split(MARKER, 1)
    return common.rstrip().encode() + b"\n", (MARKER + local).encode()

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

    def publish(self, repo, changes, title, body, expected_main=None):
        head, tree, existing, exists = self.working(repo)
        if expected_main is not None and self.main(repo) != expected_main:
            raise MigrationError("main advanced; refusing stale migration proposal")
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
    changes, manifest = {}, []
    for path, e in sorted(source.items()):
        if not selected(path) and path != "AGENTS.md":
            continue
        if e["mode"] not in {"100644", "100755"}:
            raise MigrationError("unexpected source mode")
        original = gh.read(SOURCE, e)
        data = original
        if path == "AGENTS.md":
            data, _ = split_agents(original)
        elif path.endswith("/activation.json"):
            data = reset_activation(original)
        elif path == ".github/control-plane/config.json":
            v = json.loads(original)
            v.update(repository=DEST, project="CONTROL_PLANE")
            data = encoded(v)
        elif path == ".github/control-plane/flow-policy.example.json":
            v = json.loads(original)
            v.update(enabled=False, deployment_audit="PENDING", deployment_approval_pointer=None,
                     source_sha256={}, repositories=[], registrations={}, review_routes={}, review_lanes={})
            for lane in v.get("lanes", {}).values():
                lane["enabled"] = False
            v["slack"]["projects"] = {}
            data = encoded(v)
        target = "engineering/" + path
        changes[target] = (e["mode"], data)
        if data != original:
            changes["engineering/provenance/kix/" + path] = ("100644", original)
        manifest.append({"source_path": path, "source_blob": e["sha"], "destination_path": target,
                         "destination_blob": blob_sha(data), "mode": e["mode"], "transformed": data != original})
    if not any(m["source_path"] == "scripts/control_plane.py" for m in manifest) or len(manifest) < 20:
        raise MigrationError("incomplete source selection")
    changes["engineering/source-manifest.json"] = ("100644", encoded({
        "schema_version": 1, "source_repository": SOURCE, "source_commit": SOURCE_SHA,
        "source_tree": SOURCE_TREE, "entries": manifest,
        "historical_tasks_and_validation": f"https://github.com/{SOURCE}/tree/{SOURCE_SHA}",
        "note": "Historical task/validation files remain at the original immutable commit; no history rewritten."}))
    changes[STAMP] = ("100644", encoded({"migration_task": TASK, "source_commit": SOURCE_SHA,
                                       "stage": "SOURCE_IMPORTED_NOT_DEPLOYED", "runtime_enabled": False}))
    return changes

def product_changes(gh, repo, entries, imported_sha):
    if "AGENTS.md" not in entries:
        raise MigrationError(f"{repo}: missing AGENTS; refuse to replace unknown governance")
    _, local = split_agents(gh.read(repo, entries["AGENTS.md"]))
    local = clean_legacy_pointer(local)
    note = pointer(repo, imported_sha)
    changes = {p: None for p in entries if selected(p)}
    # Templates are project-local task inputs; preserve their exact fields and eligibility rules.
    if "TASKS/TEMPLATE.md" in entries:
        changes.pop("TASKS/TEMPLATE.md", None)
    header = (f"# Product agent governance\n\nShared engineering source is maintained in `{DEST}`.\n"
              f"See `docs/CONTROL_PLANE_POINTER.md` and `{PRODUCT_STAMP}` for the pinned candidate.\n"
              "The source import is not runtime activation. User-only merge, non-author exact-HEAD\n"
              "review, single writer, UNKNOWN fencing, no polling and no automatic retry remain required.\n"
              "If shared policy and project contracts conflict, stop with DECISION_REQUIRED.\n\n").encode()
    changes["AGENTS.md"] = ("100644", header + local)
    for path in ("docs/CONTROL_PLANE_POINTER.md", "RUNBOOKS/DISPATCH.md",
                 "docs/CONTROL_PLANE_RUNTIME.md", "docs/CONTROL_PLANE_FLOW.md", "RUNBOOKS/BOUNDARY.md"):
        if path == "docs/CONTROL_PLANE_POINTER.md" or path in entries:
            changes[path] = ("100644", note)
    if "docs/KIX_CONTROL_PLANE_POINTER.md" in entries:
        changes["docs/KIX_CONTROL_PLANE_POINTER.md"] = ("100644", note)
    changes[PRODUCT_STAMP] = ("100644", encoded({"schema_version": 1, "migration_task": TASK,
        "repository": repo, "control_repository": DEST, "control_source_commit": imported_sha,
        "control_source_subdirectory": "engineering", "status": "SOURCE_ONLY_PENDING_REVIEW_AND_CUTOVER",
        "runtime_enabled": False}))
    if repo.endswith("/ZARI") and "docs/IMPLEMENTATION_STATUS.md" in entries:
        old = gh.read(repo, entries["docs/IMPLEMENTATION_STATUS.md"])
        token = b"<!-- CP-EXTRACT-001 -->"
        if token not in old:
            changes["docs/IMPLEMENTATION_STATUS.md"] = ("100644", old + b"\n\n" + token + b"\n"
                + b"Shared engineering source extraction candidate; application code unchanged.\n"
                + f"Tracking: {TASK}\n".encode()
                + b"Source/PR preparation only. No application test PASS, independent audit, deployment or activation claimed.\n")
    for path, expected in LOCKED.items():
        if repo == SOURCE and (entries.get(path, {}).get("sha") != expected or path in changes):
            raise MigrationError("locked KIX blob mismatch")
    return changes

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write only migration branches and draft PRs")
    args = parser.parse_args()
    gh = GitHub(args.apply)
    if gh.main(SOURCE) != SOURCE_SHA:
        raise MigrationError("KIX main advanced beyond pinned source; re-plan instead of silently rebasing")
    tree_sha, source = gh.tree(SOURCE, SOURCE_SHA)
    if tree_sha != SOURCE_TREE:
        raise MigrationError("source tree mismatch")
    snapshots = {}
    for repo in PRODUCTS:
        main_sha = gh.main(repo)
        _, entries = gh.tree(repo, main_sha)
        split_agents(gh.read(repo, entries["AGENTS.md"]))
        snapshots[repo] = (main_sha, entries)
    changes = build_import(gh, source)
    # Preflight every product before the first write, not halfway through migration.
    for repo, (_, entries) in snapshots.items():
        product_changes(gh, repo, entries, "PENDING_IMPORT_COMMIT")
    print(f"Pinned source: {SOURCE}@{SOURCE_SHA}; selected destination files: {len(changes)}")
    print("No runtime/host/Slack/credentials/ledger/merge actions are performed.")
    if not args.apply:
        print("READ-ONLY PLAN complete. Re-run once with --apply to publish draft PRs.")
        return
    body = (f"Refs {TASK}\n\nExact source imported from `{SOURCE}@{SOURCE_SHA}` under `engineering/`.\n"
            "See source-manifest.json for source/destination Git blobs and explicit transforms.\n"
            "Activation is reset to NOT_APPROVED/PENDING/false. Nested workflows are inert source.\n"
            "Legacy KIX tests/fixtures and source path assumptions are preserved, NOT a claim of\n"
            "cross-repository runtime readiness. No product code, secret, runner, ledger or Slack change.\n"
            "Required: exact-HEAD non-author audit/CI, User merge; separate runtime identity and cutover task.\n"
            "The author cannot self-approve. No application or live-provider tests claimed.\n")
    imported_sha = gh.publish(DEST, changes, "refactor: import shared engineering control plane from KIX (disabled)", body)
    # Verify every published blob before preparing any product-side deletion proposal.
    _, imported = gh.tree(DEST, imported_sha)
    for path, (mode, data) in changes.items():
        if imported.get(path, {}).get("sha") != blob_sha(data) or imported[path]["mode"] != mode:
            raise MigrationError("remote import verification failed; no product deletion PR prepared")
    for repo, (base, entries) in snapshots.items():
        if gh.main(repo) != base:
            raise MigrationError(f"{repo}: main moved; stop without rebasing")
        change = product_changes(gh, repo, entries, imported_sha)
        text = (f"Refs {TASK}\n\nBase `{base}`. Imported source candidate `{DEST}@{imported_sha}`.\n"
                "Remove shared scripts/policies/workflows from this product; retain project governance,\n"
                "immutable tasks, product source/CI, historical evidence and local task-template fields.\n"
                "This PR MUST remain draft until destination import is reviewed/merged and any legacy\n"
                "dispatcher is drained/fenced. Do not retire an active legacy runner by merging blindly.\n"
                "No runtime enablement or renewed FILM UNIT/마음결 rollout. No audit/CI PASS claimed.\n"
                "User-only merge. Runtime identity separation, ledger continuity, host/Slack cutover\n"
                "and a separately authorized canary remain independent gates.\n")
        gh.publish(repo, change, "refactor: remove shared control-plane ownership from product", text, base)
    print("Draft source/consumer PR preparation complete. Nothing merged or deployed.")

if __name__ == "__main__":
    try:
        main()
    except (MigrationError, KeyError, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        sys.exit(1)
