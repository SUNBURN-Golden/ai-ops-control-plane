#!/usr/bin/env python3
"""Disabled host install / pin tooling for the central control plane.

Prepares a host to run the control plane from
BeautifulMind-JT/ai-ops-control-plane at one exact SHA. The install is always
disabled: it refuses to run unless the in-repo activation record is
runtime_enabled=false / NOT_APPROVED / PENDING, and it never writes any
enabling state. KIX historical audit/activation evidence is not imported;
central approval requires fresh non-author review and a separate bounded
canary authorization.

Commands:
  plan    Print the exact install actions as JSON without changing anything.
  verify  Non-mutating post-install/preflight check of pin and policy state.
  apply   Write the runtime source pin and update host policy pin fields.
          Requires --confirm-disabled-install. Does not enable anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL_REPOSITORY = "BeautifulMind-JT/ai-ops-control-plane"
DEFAULT_POLICY_PATH = Path("/etc/astra/control-plane-host.json")
DEFAULT_PIN_PATH = Path("/opt/astra/RUNTIME_SOURCE_SHA.txt")
DEFAULT_ACTIVATION_PATH = ROOT / ".github" / "control-plane" / "activation.json"
SHA_RE = re.compile(r"[0-9a-f]{40}")

# Host policy fields owned by this installer. allowed_repositories is left
# untouched: it admits consumer product repositories for dispatch, and this
# cutover does not add FILM/maeum-gyeol rollout or SOULBOUND.
POLICY_PIN_FIELDS = ("control_repository", "control_source_sha", "control_runtime_enabled")


class InstallError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InstallError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InstallError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{path} must contain a JSON object")
    return value


def require_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise InstallError("control source pin must be a full 40-hex commit SHA")
    return value


def require_disabled_activation(path: Path) -> dict:
    activation = load_json(path)
    if activation.get("runtime_enabled") is not False:
        raise InstallError("activation record must have runtime_enabled=false")
    if activation.get("user_activation_approval") != "NOT_APPROVED":
        raise InstallError("activation record must be NOT_APPROVED")
    if activation.get("implementation_audit") != "PENDING" or activation.get("runner_preflight") != "PENDING":
        raise InstallError("activation audit/preflight must be PENDING; KIX evidence does not transfer")
    return activation


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def desired_policy(policy: dict, sha: str) -> dict:
    updated = dict(policy)
    updated["control_repository"] = CONTROL_REPOSITORY
    updated["control_source_sha"] = sha
    updated["control_runtime_enabled"] = False
    return updated


def build_plan(sha: str, policy_path: Path, pin_path: Path, activation_path: Path) -> dict:
    sha = require_sha(sha)
    require_disabled_activation(activation_path)
    policy = load_json(policy_path)
    current_pin = None
    if pin_path.exists():
        current_pin = pin_path.read_text(encoding="utf-8").strip() or None
    updated = desired_policy(policy, sha)
    policy_changes = {
        key: {"from": policy.get(key), "to": updated[key]}
        for key in POLICY_PIN_FIELDS
        if policy.get(key) != updated[key]
    }
    return {
        "status": "DISABLED_INSTALL_PLAN",
        "control_repository": CONTROL_REPOSITORY,
        "control_source_sha": sha,
        "runtime_enabled": False,
        "actions": [
            {"write_pin": {"path": str(pin_path), "from": current_pin, "to": sha}},
            {"update_policy": {"path": str(policy_path), "changes": policy_changes}},
        ],
        "unchanged": {
            "allowed_repositories": policy.get("allowed_repositories"),
            "enabled_builders": policy.get("enabled_builders"),
            "ledger_path": policy.get("ledger_path"),
        },
        "not_performed": [
            "runtime_enabled stays false; activation stays NOT_APPROVED",
            "no KIX audit/runner/activation evidence is inherited",
            "no dispatcher is started; no canary is authorized",
        ],
    }


def verify(sha: str, policy_path: Path, pin_path: Path, activation_path: Path) -> dict:
    sha = require_sha(sha)
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        require_disabled_activation(activation_path)
        check("activation_disabled", True, "runtime_enabled=false, NOT_APPROVED, audit PENDING")
    except InstallError as exc:
        check("activation_disabled", False, str(exc))

    try:
        pin = pin_path.read_text(encoding="utf-8").strip()
        check("source_pin", pin == sha, f"pin={pin or 'EMPTY'} expected={sha}")
    except FileNotFoundError:
        check("source_pin", False, f"missing pin file {pin_path}")

    try:
        policy = load_json(policy_path)
        check("policy_control_repository", policy.get("control_repository") == CONTROL_REPOSITORY,
              f"control_repository={policy.get('control_repository')!r}")
        check("policy_control_source_sha", policy.get("control_source_sha") == sha,
              f"control_source_sha={policy.get('control_source_sha')!r}")
        check("policy_runtime_disabled", policy.get("control_runtime_enabled") is False,
              f"control_runtime_enabled={policy.get('control_runtime_enabled')!r}")
        repos = policy.get("allowed_repositories")
        check("allowed_repositories_present", isinstance(repos, list) and bool(repos),
              f"allowed_repositories={repos!r}")
    except InstallError as exc:
        check("policy_readable", False, str(exc))

    return {"status": "PASS" if all(c["ok"] for c in checks) else "FAIL", "checks": checks}


def apply(sha: str, policy_path: Path, pin_path: Path, activation_path: Path) -> dict:
    plan = build_plan(sha, policy_path, pin_path, activation_path)
    policy = load_json(policy_path)
    atomic_write(pin_path, plan["control_source_sha"] + "\n")
    atomic_write(policy_path, json.dumps(desired_policy(policy, plan["control_source_sha"]), indent=2) + "\n")
    report = verify(sha, policy_path, pin_path, activation_path)
    if report["status"] != "PASS":
        raise InstallError("post-install verification failed: " + json.dumps(report))
    return {"status": "INSTALLED_DISABLED", "plan": plan, "verify": report}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "verify", "apply"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--sha", required=True)
        cmd.add_argument("--policy-path", type=Path, default=DEFAULT_POLICY_PATH)
        cmd.add_argument("--pin-path", type=Path, default=DEFAULT_PIN_PATH)
        cmd.add_argument("--activation-path", type=Path, default=DEFAULT_ACTIVATION_PATH)
    sub.choices["apply"].add_argument("--confirm-disabled-install", action="store_true",
                                      help="required acknowledgement that this installs disabled only")

    args = parser.parse_args(argv)
    try:
        if args.command == "apply" and not args.confirm_disabled_install:
            raise InstallError("apply requires --confirm-disabled-install; enabling is out of scope")
        result = {
            "plan": build_plan,
            "verify": verify,
            "apply": apply,
        }[args.command](args.sha, args.policy_path, args.pin_path, args.activation_path)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") != "FAIL" else 3
    except InstallError as exc:
        print(f"INSTALL_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
