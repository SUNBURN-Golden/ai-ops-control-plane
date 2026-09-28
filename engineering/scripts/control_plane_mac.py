#!/usr/bin/env python3
"""Native Mac diagnostic partition, NOT a production/provider dispatch boundary."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

# -I excludes cwd/PYTHONPATH. Only the adjacent installed, digest-checked code is used.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from control_plane_host import (ACTIVE, HostError, Ledger, canonical, evidence_url,
                                parse_json, protected_leaf, result_for)

REPOSITORY = "BeautifulMind-JT/ai-ops-control-plane"
FILES = ("control_plane_mac.py", "control_plane_mac_probe.py", "control_plane_host.py")
LANES = ("DEVIN", "GROK_BUILD", "CURSOR", "GLM", "ISOLATION", "LIFETIME")
ROOT = Path.home() / ".astra-mac"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def create_file(path, content):
    """No replacement, symlink following, or silent repair of runtime state."""
    data = content.encode() if isinstance(content, str) else content
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_private(path):
    protected_leaf(path, os.getuid(), mode=0o600)
    return parse_json(path.read_text())


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise HostError("expected a canonical UUID")
    return value


def root_check(root):
    if (sys.platform != "darwin" or os.getuid() == 0 or os.getuid() != os.geteuid()
            or root != Path.home() / ".astra-mac" or root != root.resolve()):
        raise HostError("use the non-root Mac account and its fixed ~/.astra-mac directory")
    for parent in root.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o022):
            raise HostError(f"unsafe runtime ancestor: {parent}")


def install(root, evidence):
    root_check(root)
    if not evidence_url(evidence) or not evidence.startswith(f"https://github.com/{REPOSITORY}/issues/"):
        raise HostError("canonical GitHub intake/decision URL required")
    root.mkdir(mode=0o700)  # existing, partial or damaged installs are never overwritten
    for name in ("bin", "control", "workspaces"):
        (root / name).mkdir(mode=0o700)
    source = Path(__file__).resolve().parent
    for name in FILES:
        create_file(root / "bin" / name, (source / name).read_bytes())
    python = str(Path(sys.executable).resolve())
    home = Path.home()
    candidates = {
        "DEVIN": ["/opt/homebrew/bin/devin", "--version"],
        "GROK_BUILD": [str(home / ".local/bin/grok"), "--no-auto-update", "--version"],
        "CURSOR": [str(home / ".local/bin/cursor-agent"), "--version"],
        "GLM": ["/usr/local/bin/node", "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs", "doctor", "--json"],
    }
    adapters = {}
    for lane, argv in candidates.items():
        paths = [Path(argv[0]).resolve(strict=True)]
        argv[0] = str(paths[0])
        if lane == "GLM":
            paths.append(Path(argv[1]).resolve(strict=True))
            argv[1] = str(paths[1])
        if lane == "CURSOR":
            paths.extend([paths[0].parent / "node", paths[0].parent / "index.js"])
        adapters[lane] = {"argv": argv, "pins": {str(path): digest(path) for path in paths},
                          "read_roots": [str(paths[0].parent if lane == "CURSOR" else paths[0])]
                          + ([str(paths[1].parent)] if lane == "GLM" else [])}
    policy = {"schema_version": 1, "scope": "OFFLINE_DIAGNOSTICS_ONLY", "uid": os.getuid(),
              "host_id": str(uuid.uuid4()), "repository": REPOSITORY, "intake": evidence,
              "production_enabled": False, "handoff_enabled": False, "network_enabled": False,
              "python": python, "python_prefix": sys.base_prefix, "python_sha256": digest(python),
              "adapters": adapters, "source_pins": {name: digest(root / "bin" / name) for name in FILES},
              "allowed_repositories": [REPOSITORY], "enabled_builders": list(LANES),
              "max_active_sessions": 1, "max_launches_per_24h": None}
    Ledger(root / "control" / "admission.sqlite").initialize()
    create_file(root / "control" / "control-canary", "PUBLIC CONTROL CANARY\n")
    create_file(root / "workspaces" / "other-task-canary", "PUBLIC OTHER TASK CANARY\n")
    create_file(root / "policy.json", canonical(policy))  # written last, not an activation flag
    return {"status": "INSTALLED_DIAGNOSTICS_ONLY", "root": str(root), "host_id": policy["host_id"]}


def verify_policy(root, policy):
    if (policy.get("scope") != "OFFLINE_DIAGNOSTICS_ONLY" or policy.get("schema_version") != 1
            or policy.get("uid") != os.getuid() or policy.get("repository") != REPOSITORY
            or any(policy.get(key) is not False for key in
                   ("production_enabled", "handoff_enabled", "network_enabled"))
            or policy.get("allowed_repositories") != [REPOSITORY]
            or policy.get("enabled_builders") != list(LANES) or policy.get("max_active_sessions") != 1):
        raise HostError("diagnostic-only policy mismatch; no activation or takeover mode exists")
    identifier(policy["host_id"])
    for name in FILES:
        path = root / "bin" / name
        protected_leaf(path, os.getuid(), mode=0o600)
        if digest(path) != policy["source_pins"].get(name):
            raise HostError("installed source digest mismatch")
    if digest(policy["python"]) != policy["python_sha256"]:
        raise HostError("Python identity changed; do not silently repin")


def validate_request(packet, policy):
    request = identifier(packet.get("launch_request_id"))
    expected = {"schema_version": 1, "repository": REPOSITORY,
                "task_id": f"macdiag:{policy['host_id']}:{request}", "task_revision": "r1",
                "attempt_id": 1, "launch_request_id": request, "builder_id": packet.get("builder_id"),
                "intake": policy["intake"]}
    if packet != expected or packet["builder_id"] not in LANES:
        raise HostError("only newly minted local diagnostic identities are accepted; no imports or prompts")


def command(policy, lane):
    if lane in {"ISOLATION", "LIFETIME"}:
        return None
    entry = policy["adapters"][lane]
    for path, expected in entry["pins"].items():
        if digest(path) != expected:
            raise HostError(f"{lane} identity changed; no automatic fallback/upgrade")
    argv = entry["argv"]
    # No arbitrary command, inference, auto-update, resume, or credential operations.
    suffix = {"DEVIN": ["--version"], "GROK_BUILD": ["--no-auto-update", "--version"],
              "CURSOR": ["--version"], "GLM": ["doctor", "--json"]}[lane]
    if argv[-len(suffix):] != suffix or len(argv) != len(suffix) + (2 if lane == "GLM" else 1):
        raise HostError("adapter command is not a fixed offline diagnostic")
    return argv


def profile(workspace, read_roots):
    def quoted(value):
        # JSON quoting is also valid for these Seatbelt string literals.
        return canonical(str(value))
    readonly = ["/System", "/usr/bin", "/usr/lib", "/usr/libexec", "/usr/share", "/bin", "/sbin",
                "/private/var/db/dyld", *read_roots]
    rules = " ".join(f"(subpath {quoted(path)})" for path in readonly)
    return ("(version 1)\n(deny default)\n(allow process-exec process-fork sysctl-read)\n"
            "(allow signal (target self))\n(allow file-read-metadata)\n"
            f'(allow file-read* (literal "/") {rules} (subpath {quoted(workspace)}))\n'
            f"(allow file-write* (subpath {quoted(workspace)}))\n"
            '(allow file-read* file-write* (literal "/dev/null"))\n')


def wait_group_gone(pgid, timeout=5, *, clock=time.monotonic, sleep=time.sleep):
    """Bounded cleanup observation, not a retry/relaunch or admission timeout."""
    deadline = clock() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False  # unresolved group keeps the admission slot occupied
        sleep(min(0.05, remaining))


class Launchd:
    def __init__(self, uid):
        self.domain = f"gui/{uid}"

    def call(self, *args):
        return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True,
                              env=ENV, cwd="/", timeout=30)

    def status(self, label):
        result = self.call("print", f"{self.domain}/{label}")
        if result.returncode:
            # Other errors (including permissions/domain failures) are NOT absence.
            expected = f'Could not find service "{label}" in domain for user gui:'
            return "ABSENT" if expected in result.stderr else "UNKNOWN"
        found = re.search(r"^\s*state = (.+)$", result.stdout, re.MULTILINE)
        if found and found[1] == "not running":
            return "EXITED"
        if found and found[1] in {"running", "spawn scheduled", "waiting", "spawn pending"}:
            return "RUNNING"
        return "UNKNOWN"


class Runtime:
    def __init__(self, root=ROOT, launchd=None):
        self.root = root
        root_check(root)
        for path in (root, root / "bin", root / "control", root / "workspaces"):
            protected_leaf(path, os.getuid(), directory=True, mode=0o700)
        self.policy = read_private(root / "policy.json")
        verify_policy(root, self.policy)
        self.ledger = Ledger(root / "control" / "admission.sqlite")
        for suffix in ("", "-journal", "-wal", "-shm", ".inflight.lock"):
            path = Path(str(self.ledger.path) + suffix)
            if not suffix or path.exists() or path.is_symlink():
                protected_leaf(path, os.getuid(), mode=0o600)
        self.ledger.connect().close()  # never initialize a missing/corrupt ledger on recovery
        self.launchd = launchd or Launchd(os.getuid())

    def directory(self, request):
        path = self.root / "control" / identifier(request)
        protected_leaf(path, os.getuid(), directory=True, mode=0o700)
        return path

    def packet(self, request):
        packet = read_private(self.directory(request) / "packet.json")
        validate_request(packet, self.policy)
        if packet["launch_request_id"] != request:
            raise HostError("request filename/identity mismatch")
        return packet

    def label(self, request):
        return "org.ai-ops.macdiag." + identifier(request)

    def new(self, lane):
        if lane not in LANES:
            raise HostError("unknown diagnostic")
        command(self.policy, lane)
        request = str(uuid.uuid4())
        packet = {"schema_version": 1, "repository": REPOSITORY,
                  "task_id": f"macdiag:{self.policy['host_id']}:{request}", "task_revision": "r1",
                  "builder_id": lane, "launch_request_id": request, "attempt_id": 1,
                  "intake": self.policy["intake"]}
        directory = self.root / "control" / request
        workspace = self.root / "workspaces" / request
        directory.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        for name in ("home", "tmp"):
            (workspace / name).mkdir(mode=0o700)
        create_file(directory / "packet.json", canonical(packet))
        create_file(directory / "job.plist", plistlib.dumps(self.job(request)))
        return packet

    def job(self, request):
        directory = self.directory(request)
        return {
            "Label": self.label(request), "ProgramArguments": [self.policy["python"], "-I",
                str(self.root / "bin" / FILES[0]), "worker", request],
            "RunAtLoad": True, "KeepAlive": False, "ProcessType": "Background", "Umask": 0o077,
            "WorkingDirectory": str(directory), "EnvironmentVariables": ENV,
            "StandardOutPath": str(directory / "worker.stdout"),
            "StandardErrorPath": str(directory / "worker.stderr")}

    def start(self, request):
        packet = self.packet(request)
        command(self.policy, packet["builder_id"])
        with self.ledger.inflight_lock():
            admitted, result = self.ledger.reserve(packet, self.policy)
            if not admitted:
                return result
            try:
                job = self.directory(request) / "job.plist"
                protected_leaf(job, os.getuid(), mode=0o600)
                if plistlib.loads(job.read_bytes()) != self.job(request):
                    raise HostError("launchd job differs from fixed diagnostic contract")
                response = self.launchd.call("bootstrap", self.launchd.domain, str(job))
                if response.returncode:
                    raise HostError("launchd bootstrap did not acknowledge; do not resubmit")
                result = result_for(packet, "CONFIRMED", "local offline diagnostic unit only; NOT a provider session",
                                    self.label(request))
            except Exception as exc:
                result = result_for(packet, "UNKNOWN", f"bootstrap unresolved: {type(exc).__name__}")
            self.ledger.finalize(packet, result)
            return result

    def row(self, request):
        db = self.ledger.connect()
        try:
            row = db.execute("SELECT * FROM launches WHERE request=?", (identifier(request),)).fetchone()
            if row is None:
                raise HostError("request has not been admitted")
            return dict(row)
        finally:
            db.close()

    def status(self, request):
        packet, row = self.packet(request), self.row(request)
        if canonical(packet) != row["packet"]:
            raise HostError("packet differs from durable reservation")
        receipt = self.directory(request) / "receipt.json"
        return {"ledger_state": row["state"], "result": parse_json(row["result"]),
                "unit_state": self.launchd.status(self.label(request)),
                "receipt": read_private(receipt) if receipt.exists() else None}

    def stop(self, request):
        row = self.row(request)
        if row["state"] not in ACTIVE:
            raise HostError("no active diagnostic owner")
        result = self.launchd.call("kill", "SIGTERM", f"{self.launchd.domain}/{self.label(request)}")
        return {"status": "STOP_REQUESTED" if result.returncode == 0 else "UNKNOWN",
                "slot_released": False, "launch_request_id": request}

    def release(self, request, evidence):
        if not evidence_url(evidence) or not evidence.startswith(f"https://github.com/{REPOSITORY}/issues/"):
            raise HostError("publish terminal diagnostic evidence before releasing its slot")
        with self.ledger.inflight_lock(exclusive=True):
            state = self.status(request)
            receipt = state["receipt"]
            if (not receipt or receipt.get("packet") != self.packet(request)
                    or receipt.get("terminal") is not True or receipt.get("child_group_gone") is not True
                    or state["unit_state"] not in {"EXITED", "ABSENT"}):
                raise HostError("terminal receipt and explicit exited/absent unit required; no timeout-based release")
            report = self.directory(request) / "probe.stdout"
            if receipt.get("stdout_sha256") != digest(report):
                raise HostError("terminal report digest mismatch")
            if state["unit_state"] == "EXITED":
                result = self.launchd.call("bootout", f"{self.launchd.domain}/{self.label(request)}")
                if result.returncode:
                    raise HostError("bootout unresolved; reservation retained")
            if self.launchd.status(self.label(request)) != "ABSENT":
                raise HostError("unit removal not confirmed; reservation retained")
            # Exact receipt + removed local unit fences this fixed diagnostic sender.
            # This is NOT existing-host fencing or provider-session reconciliation.
            return self.ledger._reconcile(request, self.label(request), evidence)

    def worker(self, request):
        packet, row = self.packet(request), self.row(request)
        if row["state"] not in ACTIVE or row["packet"] != canonical(packet):
            raise HostError("worker has no matching durable reservation")
        directory = self.directory(request)
        create_file(directory / "execution.started", canonical({"request": request, "pid": os.getpid()}))
        # O_EXCL marker is permanent: launchd kickstart or a second worker cannot replay.
        workspace = self.root / "workspaces" / request
        protected_leaf(workspace, os.getuid(), directory=True, mode=0o700)
        lane = packet["builder_id"]
        argv = command(self.policy, lane)
        read_roots = [self.policy["python_prefix"], str(self.root / "bin" / FILES[1])]
        if lane in {"ISOLATION", "LIFETIME"}:
            argv = [self.policy["python"], "-I", str(self.root / "bin" / FILES[1]), lane,
                    str(workspace), str(self.root / "control" / "control-canary")]
        else:
            read_roots.extend(self.policy["adapters"][lane]["read_roots"])
        create_file(directory / "profile.sb", profile(workspace, read_roots))
        env = {**ENV, "HOME": str(workspace / "home"), "TMPDIR": str(workspace / "tmp"),
               "AGENT_CLI_CREDENTIAL_STORE": "file", "NODE_COMPILE_CACHE": str(workspace / "tmp" / "node-cache")}
        cancelled = False

        def cancel(signum, frame):
            nonlocal cancelled
            cancelled = True

        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGINT, cancel)
        with (directory / "probe.stdout").open("xb") as stdout, (directory / "probe.stderr").open("xb") as stderr:
            child = subprocess.Popen(["/usr/bin/sandbox-exec", "-f", str(directory / "profile.sb"), *argv],
                                     cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=stdout,
                                     stderr=stderr, start_new_session=True)
            deadline = time.monotonic() + 45
            while child.poll() is None and not cancelled and time.monotonic() < deadline:
                time.sleep(0.1)
            timed_out = child.poll() is None and not cancelled
            # Always kill remaining group members, even if the CLI parent already exited.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=10)
            group_gone = wait_group_gone(child.pid)
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
        receipt = {"packet": packet, "terminal": group_gone, "child_group_gone": group_gone,
                   "outcome": "CANCELLED" if cancelled else "TIMEOUT" if timed_out else
                   "PASS" if child.returncode == 0 else "FAIL", "returncode": child.returncode,
                   "stdout_sha256": digest(directory / "probe.stdout"), "finished_at": time.time(),
                   "scope": "OFFLINE_DIAGNOSTIC_ONLY", "provider_inference_verified": False}
        create_file(directory / "receipt.json", canonical(receipt))
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install")
    setup.add_argument("--evidence", required=True)
    setup.add_argument("--confirm-local-diagnostic-install", action="store_true", required=True)
    commands.add_parser("new").add_argument("lane", choices=LANES)
    for name in ("start", "status", "recover", "stop", "release", "worker"):
        child = commands.add_parser(name)
        child.add_argument("request")
        if name == "release":
            child.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "install":
            result = install(ROOT, args.evidence)
        else:
            runtime = Runtime()
            if Path(__file__).resolve() != ROOT / "bin" / FILES[0]:
                raise HostError("run the installed diagnostic runtime, not a working checkout")
            if args.command == "new":
                result = runtime.new(args.lane)
            elif args.command == "release":
                result = runtime.release(args.request, args.evidence)
            else:
                result = getattr(runtime, "status" if args.command == "recover" else args.command)(args.request)
        print(canonical(result))
        return 0
    except (HostError, OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(canonical({"status": "ERROR", "reason": str(exc), "automatic_retry": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
