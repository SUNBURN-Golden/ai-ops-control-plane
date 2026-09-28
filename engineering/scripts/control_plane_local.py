#!/usr/bin/python3 -I
"""Host-local dispatch route without GitHub Actions.

Runs the same validate -> prepare -> launch -> finalize sequence as
control-plane-runtime.yml, on the control host, as the runner identity. GitHub
is reached only through its REST API with the User's token. Every existing gate
still applies (activation.json, audited runtime SHA, host admission ledger);
a protected host policy additionally enables this route, and a per-task lock
replaces the workflow concurrency group.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

# Explicit sibling import supports python -I from a protected checkout.
_spec = importlib.util.spec_from_file_location("control_plane", Path(__file__).with_name("control_plane.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

POLICY_PATH = Path("/etc/astra/local-dispatch.json")
POLICY_OWNER_UID = 0
CONTROL_REPOSITORY = "BeautifulMind-JT/ai-ops-control-plane"
DISPATCH_ENV = ("ASTRA_TARGET_REPOSITORY", "GITHUB_REPOSITORY", "GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR",
                "GITHUB_TOKEN", "GITHUB_OUTPUT", "EXPECTED_TASK_ID", "EXPECTED_TASK_REVISION",
                "EXPECTED_BUILDER_ID", "EXPECTED_ISSUE_BODY_SHA256", "EXPECTED_ATTEMPT_ID")


class LocalDispatchError(RuntimeError):
    pass


def require_protected_file(path: Path, owner_uid: int, *, mode: int | None = None) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022
            or info.st_nlink != 1):
        raise LocalDispatchError(f"unprotected file: {path}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise LocalDispatchError(f"required mode {mode:o}: {path}")


def load_policy(path: Path = POLICY_PATH) -> dict:
    try:
        require_protected_file(path, POLICY_OWNER_UID)
        policy = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LocalDispatchError(f"local dispatch policy missing: {path}") from exc
    except (OSError, ValueError) as exc:
        raise LocalDispatchError("local dispatch policy unreadable or invalid") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise LocalDispatchError("unsupported local dispatch policy")
    if policy.get("enabled") is not True:
        raise LocalDispatchError("local dispatch route is disabled by host policy")
    for key in ("token_path", "lock_dir"):
        value = policy.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise LocalDispatchError(f"{key} must be an absolute normalized path")
    return policy


def read_token(path: Path) -> str:
    # Readable only by the dispatch (runner) identity; builders run as other UIDs.
    require_protected_file(path, os.geteuid(), mode=0o600)
    token = path.read_text(encoding="utf-8").strip()
    if not token or any(c.isspace() for c in token):
        raise LocalDispatchError("GitHub token file is empty or malformed")
    return token


def token_login(token: str) -> str:
    request = urllib.request.Request("https://api.github.com/user", headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "astra-control-plane-local"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            login = json.loads(response.read().decode()).get("login")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LocalDispatchError("cannot resolve the GitHub token's login") from exc
    if not isinstance(login, str) or not login:
        raise LocalDispatchError("GitHub token has no user login")
    return login


def require_main_checkout() -> None:
    # Mirrors the workflow's default-branch guard; RUNTIME_PATHS/activation
    # checks in require_runtime_enabled still bind the audited SHA.
    branch = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cp.ROOT,
                            capture_output=True, text=True, check=False)
    if branch.returncode or branch.stdout.strip() != "main":
        raise LocalDispatchError("local dispatch runs only from a checkout of main")


@contextmanager
def task_lock(lock_dir: Path, target: str, issue: int):
    info = lock_dir.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise LocalDispatchError(f"lock directory must be owner-only: {lock_dir}")
    name = hashlib.sha256(f"{target}#{issue}".encode()).hexdigest()[:32] + ".lock"
    fd = os.open(lock_dir / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LocalDispatchError("another dispatch for this task is still running") from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def dispatch_environment(values: dict):
    saved = {key: os.environ.get(key) for key in DISPATCH_ENV}
    try:
        for key in DISPATCH_ENV:
            os.environ.pop(key, None)
        os.environ.update({key: value for key, value in values.items() if value is not None})
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def read_outputs(path: Path) -> dict:
    outputs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return outputs


def dispatch(args, policy: dict) -> int:
    require_main_checkout()
    with task_lock(Path(policy["lock_dir"]), args.target, args.issue_number):
        token = read_token(Path(policy["token_path"]))
        login = token_login(token)
        with tempfile.TemporaryDirectory(prefix="astra-local-") as work:
            work = Path(work)
            packet, result, outputs = work / "packet.json", work / "result.json", work / "outputs"
            outputs.touch(mode=0o600)
            env = {
                "ASTRA_TARGET_REPOSITORY": args.target, "GITHUB_REPOSITORY": CONTROL_REPOSITORY,
                "GITHUB_ACTOR": login, "GITHUB_TRIGGERING_ACTOR": login, "GITHUB_TOKEN": token,
                "GITHUB_OUTPUT": str(outputs), "EXPECTED_TASK_ID": args.expected_task_id,
                "EXPECTED_TASK_REVISION": args.expected_task_revision,
                "EXPECTED_BUILDER_ID": args.expected_builder_id,
                "EXPECTED_ISSUE_BODY_SHA256": args.expected_issue_body_sha256,
                "EXPECTED_ATTEMPT_ID": str(args.expected_attempt_id) if args.expected_attempt_id else None,
            }
            with dispatch_environment(env):
                cp.validate_repo()
                cp.prepare_dispatch(args.issue_number, packet)
                prepared = read_outputs(outputs)
                if prepared.get("launch_required") != "true":
                    print("no launch required")
                    return 0
                try:
                    cp.launch_dispatch(packet, result)
                finally:
                    # Like the workflow's always() step: record the exact outcome
                    # (UNKNOWN when launch did not return) before exiting.
                    state = cp.finalize_dispatch(args.issue_number, result, prepared["launch_request_id"])
                print(f"final launch state: {state}")
                return 0 if state == "CONFIRMED" else 3


def preflight(args) -> int:
    with dispatch_environment({"ASTRA_TARGET_REPOSITORY": args.target, "GITHUB_REPOSITORY": CONTROL_REPOSITORY}):
        cp.validate_repo()
        cp.host_preflight(args.builder_id)
    return 0


def positive_int(raw: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,8}", raw):
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("preflight")
    pre.add_argument("--target", required=True)
    pre.add_argument("--builder-id", choices=cp.ALLOWED_BUILDERS)
    run = commands.add_parser("dispatch")
    run.add_argument("--target", required=True)
    run.add_argument("--issue-number", type=positive_int, required=True)
    for name in ("expected-task-id", "expected-task-revision", "expected-builder-id",
                 "expected-issue-body-sha256"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--expected-attempt-id", type=positive_int)
    args = parser.parse_args(argv)
    try:
        policy = load_policy()
        if args.command == "preflight":
            return preflight(args)
        return dispatch(args, policy)
    except (LocalDispatchError, cp.ControlPlaneError) as exc:
        print(f"LOCAL_DISPATCH_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
