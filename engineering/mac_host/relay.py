#!/usr/bin/env python3
"""Agent-neutral, one-shot relay to the existing AIOPS workflow on a MacBook VM.

prepare is offline. submit --execute sends one authenticated workflow request.
The backend retains all actor, plan, host-admission, review and cost gates.
No shell, model calls, scheduler, retry loop, VM lifecycle or recovery interface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import urllib.error
import urllib.request

CONTROL_REPO = "BeautifulMind-JT/ai-ops-control-plane"
WORKFLOW = "control-plane-runtime.yml"
API = "https://api.github.com/repos/" + CONTROL_REPO
WEB = "https://github.com/" + CONTROL_REPO
TARGETS = frozenset("BeautifulMind-JT/" + name for name in
                    ("kix-protocol", "kix-commerce-apps", "ZARI", "film-unit-mv-studio", "maeum-gyeol"))
LANES = frozenset(("ALL", "DEVIN", "GROK_BUILD", "GLM", "CURSOR"))
ARGUMENTS = {
    "lanes": ({}, {}),
    "preflight": ({}, {}),
    "materialize": ({"program": "id", "node": "id", "plan_commit": "sha"}, {}),
    "start": ({"program": "id", "node": "id", "plan_commit": "sha"}, {}),
    "review": ({}, {"slot": "slot"}),
    "merge-check": ({"pr_number": "positive"}, {}),
    "astra-audit": ({"pr_number": "positive", "head": "sha"}, {}),
    "astra-consult": ({"question_comment_id": "positive"}, {}),
}


class RelayError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RelayError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def constant(value):
        raise RelayError("NON_JSON_NUMBER")
    if len(raw.encode("utf-8")) > 65536:
        raise RelayError("REQUEST_TOO_LARGE")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_value(value, kind):
    return {"id": lambda: matches(value, r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"),
            "sha": lambda: matches(value, r"[0-9a-f]{40}"),
            "positive": lambda: type(value) is int and 0 < value < 10 ** 10,
            "slot": lambda: type(value) is int and value in (1, 2)}[kind]()


def prepare(request):
    if not isinstance(request, dict) or type(request.get("schema_version")) is not int or request["schema_version"] != 1:
        raise RelayError("SCHEMA_VERSION_REQUIRED")
    if set(request) - {"schema_version", "request_id", "runner_name", "execution_host", "repository", "operation", "issue_number", "args", "builder_id"}:
        raise RelayError("UNKNOWN_REQUEST_FIELD")
    if not matches(request.get("request_id"), r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"):
        raise RelayError("REQUEST_ID_REQUIRED")
    execution_host = request.get("execution_host", "macbook")
    if execution_host == "macbook":
        if not matches(request.get("runner_name"), r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"):
            raise RelayError("EXACT_RUNNER_NAME_REQUIRED")
    elif execution_host == "current":
        if "runner_name" in request: raise RelayError("UNEXPECTED_RUNNER_OVERRIDE")
    else:
        raise RelayError("UNSUPPORTED_EXECUTION_HOST")
    if not isinstance(request.get("repository"), str) or request["repository"] not in TARGETS:
        raise RelayError("UNREGISTERED_TARGET")
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in ARGUMENTS:
        raise RelayError("UNSUPPORTED_OPERATION")
    args = request.get("args", {})
    required, optional = ARGUMENTS[operation]
    if not isinstance(args, dict) or set(args) - (required.keys() | optional.keys()) or required.keys() - args.keys():
        raise RelayError("INVALID_OPERATION_ARGUMENTS")
    for key, value in args.items():
        if not validate_value(value, (required | optional)[key]):
            raise RelayError("INVALID_ARGUMENT_" + key.upper())
    issue = request.get("issue_number")
    if operation in ("lanes", "preflight", "materialize"):
        if "issue_number" in request:
            raise RelayError("UNEXPECTED_ISSUE_NUMBER")
    elif not validate_value(issue, "positive"):
        raise RelayError("ISSUE_NUMBER_REQUIRED")
    if "builder_id" in request and (operation != "preflight" or not isinstance(request["builder_id"], str) or request["builder_id"] not in LANES):
        raise RelayError("INVALID_PREFLIGHT_BUILDER")
    inputs = {"execution_host": execution_host,
              "target_repository": request["repository"], "operation": operation,
              "program_args": canonical(args)}
    if execution_host == "macbook": inputs["expected_runner_name"] = request["runner_name"]
    if issue is not None:
        inputs["issue_number"] = str(issue)
    if operation == "preflight":
        inputs["builder_id"] = request.get("builder_id", "ALL")
    body = {"ref": "main", "inputs": inputs, "return_run_details": True}
    return {"request_id": request["request_id"], "payload_sha256": hashlib.sha256(canonical(body).encode()).hexdigest(),
            "workflow": WORKFLOW, "control_repository": CONTROL_REPO, "body": body,
            "scope": "WORKFLOW_REQUEST_ONLY", "task_completion": "NOT_CHECKED"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RelayError("REDIRECT_REFUSED")


class Github:
    def __init__(self, token):
        if not isinstance(token, str) or not token.strip():
            raise RelayError("GH_TOKEN_REQUIRED")
        self.token = token.strip()
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, method, suffix, body=None):
        if method == "POST":
            if suffix != "/actions/workflows/" + WORKFLOW + "/dispatches":
                raise RelayError("UNSUPPORTED_API_WRITE")
        elif method != "GET" or not re.fullmatch(r"/actions/runs/[1-9][0-9]*", suffix):
            raise RelayError("UNSUPPORTED_API_READ")
        req = urllib.request.Request(API + suffix, method=method,
                data=canonical(body).encode() if body is not None else None,
                headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json", "X-GitHub-Api-Version": "2026-03-10",
                         "User-Agent": "aiops-mac-relay/1"})
        with self.opener.open(req, timeout=30) as response:
            raw = response.read(65537)
            return response.status, strict_json(raw.decode()) if raw else None


class Journal:
    """Local transport receipts, never an admission ledger or a reconciliation authority."""
    def __init__(self, directory):
        directory = Path(directory).expanduser()
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RelayError("RECEIPT_DIRECTORY_MUST_BE_PRIVATE")
        self.path = directory / "requests.sqlite3"
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise RelayError("UNSAFE_RECEIPT_FILE")
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL, created INTEGER NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, request_id):
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise RelayError("REQUEST_NOT_FOUND")
        return json.loads(row["receipt"])

    def reserve(self, prepared):
        request_id, digest = prepared["request_id"], prepared["payload_sha256"]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise RelayError("REQUEST_ID_REUSED_WITH_DIFFERENT_PAYLOAD")
                self.db.commit()
                return False, json.loads(row["receipt"])
            # Commit uncertainty BEFORE the network write. A killed process never becomes retryable.
            receipt = {"request_id": request_id, "payload_sha256": digest, "state": "UNKNOWN",
                       "task_completion": "NOT_CHECKED", "reason": "SUBMISSION_NOT_YET_CONFIRMED"}
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (request_id, digest, "UNKNOWN", canonical(receipt), int(time.time())))
            self.db.commit()
            return True, receipt
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, receipt):
        with self.db:
            self.db.execute("UPDATE requests SET state=?,receipt=? WHERE id=?", (receipt["state"], canonical(receipt), receipt["request_id"]))


def submit(request, journal, api):
    prepared = prepare(request)
    fresh, receipt = journal.reserve(prepared)
    if not fresh:
        return {**receipt, "reused": True}  # Never resubmit SUBMITTED or UNKNOWN.
    try:
        code, result = api.call("POST", "/actions/workflows/" + WORKFLOW + "/dispatches", prepared["body"])
        if code == 204:
            receipt.update(state="SUBMITTED", reason="GITHUB_ACCEPTED_WITHOUT_RUN_ID")
        elif code == 200 and isinstance(result, dict) and type(result.get("workflow_run_id")) is int and result["workflow_run_id"] > 0:
            run_id = result["workflow_run_id"]
            expected = WEB + "/actions/runs/" + str(run_id)
            if result.get("html_url") != expected or result.get("run_url") != API + "/actions/runs/" + str(run_id):
                raise RelayError("RUN_RECEIPT_TARGET_MISMATCH")
            receipt.update(state="SUBMITTED", reason="GITHUB_ACCEPTED", run_id=run_id, run_url=expected)
        else:
            raise RelayError("UNEXPECTED_DISPATCH_RESPONSE")
    except Exception as exc:
        # Never store API error bodies, raw exceptions, credentials, or mutable GitHub prose.
        reason = "HTTP_" + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        receipt.update(state="UNKNOWN", reason=reason)
    journal.finish(receipt)
    return receipt


def refresh(receipt, api):
    run_id = receipt.get("run_id")
    if receipt.get("state") != "SUBMITTED" or type(run_id) is not int:
        return {**receipt, "observation": "NO_CONFIRMED_RUN_ID"}
    code, run = api.call("GET", "/actions/runs/" + str(run_id))
    if (code != 200 or not isinstance(run, dict) or run.get("id") != run_id
            or run.get("html_url") != WEB + "/actions/runs/" + str(run_id)
            or run.get("path") != ".github/workflows/" + WORKFLOW
            or run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"):
        raise RelayError("WORKFLOW_OBSERVATION_MISMATCH")
    return {**receipt, "workflow_status": run.get("status"), "workflow_conclusion": run.get("conclusion"),
            "task_completion": "NOT_CHECKED"}


def default_directory():
    import platform
    return Path.home() / ("Library/Application Support/AIOPS/relay" if platform.system() == "Darwin" else ".local/state/aiops-relay")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "submit"):
        p = subs.add_parser(name)
        p.add_argument("--request", type=Path, required=True)
        if name == "submit":
            p.add_argument("--execute", action="store_true")
            p.add_argument("--state-dir", type=Path, default=default_directory())
    status = subs.add_parser("status")
    status.add_argument("--request-id", required=True)
    status.add_argument("--state-dir", type=Path, default=default_directory())
    status.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    journal = None
    try:
        if args.command in ("prepare", "submit"):
            with args.request.open("rb") as source:
                raw = source.read(65537)
            request = strict_json(raw.decode("utf-8"))
            result = prepare(request)
            if args.command == "submit":
                if not args.execute:
                    result["state"] = "PREVIEW_ONLY"
                else:
                    api = Github(os.environ.get("GH_TOKEN", ""))
                    journal = Journal(args.state_dir)
                    result = submit(request, journal, api)
        else:
            journal = Journal(args.state_dir)
            result = journal.get(args.request_id)
            if args.refresh:
                result = refresh(result, Github(os.environ.get("GH_TOKEN", "")))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 3 if result.get("state") == "UNKNOWN" else 0
    except (RelayError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "HOLD", "reason": str(exc) if isinstance(exc, RelayError) else type(exc).__name__}))
        return 2
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
