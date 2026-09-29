#!/usr/bin/python3 -I
"""Installed, protected host admission boundary.

No scheduler. The only automatic release is `reap`, which retires a CONFIRMED
session after the host itself proves its builder lane UID has no live process.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

POLICY_PATH = Path("/etc/astra/control-plane-host.json")
INSTALLED_PATH = Path("/opt/astra/bin/astra-host-control")
WRAPPERS = {"DEVIN": "/opt/astra/bin/astra-builder-devin",
            "GROK_BUILD": "/opt/astra/bin/astra-builder-grok-build",
            "GLM": "/opt/astra/bin/astra-builder-glm",
            "CURSOR": "/opt/astra/bin/astra-builder-cursor"}
IDENTITY = ("repository", "task_id", "task_revision", "builder_id", "launch_request_id", "attempt_id")
ACTIVE = ("SUBMITTING", "CONFIRMED", "UNKNOWN")
ACTIVE_SQL = "state IN ('SUBMITTING','CONFIRMED','UNKNOWN')"
SCHEMA_VERSION = 2
ROLES = ("WRITER", "REVIEWER")
SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
REQUEST_RE = re.compile(r"[0-9a-f]{24}")
NONCE_RE = re.compile(r"[0-9a-f]{32}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
# Session-signed marker lines (docs/PROGRAM_MODE.md section 4.2). The MAC key is the
# packet's secret nonce, readable only by the session's own lane UID and this ledger.
# depth = VERIFIED_REVIEW_DEPTH, required = VERIFIED_REQUIRED_DEPTH (DISPATCH section 13).
REVIEW_PIN_RE = re.compile(r"ASTRA_REVIEW_V1 review=([0-9a-f]{24}) head=([0-9a-f]{40}) "
                           r"verdict=(PASS|PASS_WITH_NOTES|FAIL|DECISION_REQUIRED) depth=(A[0-3]) "
                           r"required=(A[1-3]) contract_change=(NO|YES) mac=([0-9a-f]{64})")
DELIVERY_PIN_RE = re.compile(r"ASTRA_DELIVERY_V1 pr=([1-9][0-9]{0,9}) head=([0-9a-f]{40}) mac=([0-9a-f]{64})")
BLOCKER_PIN_RE = re.compile(r"ASTRA_BLOCKED_V1 kind=(DECISION_REQUIRED|BLOCKED|STALLED) launch=([0-9a-f]{24}) "
                            r"mac=([0-9a-f]{64})")
OPERATOR_ONLY = {"init", "reconcile", "migrate", "materialize-resolve"}
MATERIALIZE_STATES = ("SUBMITTING", "CREATED", "UNKNOWN", "ABANDONED")
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


class HostError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HostError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(HostError("nonfinite JSON")))
        if not isinstance(value, dict):
            raise HostError("expected one JSON object")
        return value
    except (ValueError, TypeError) as exc:
        raise HostError("invalid JSON") from exc


def evidence_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return (parsed.scheme in {"http", "https"} and bool(host) and not parsed.username
                and host not in {"localhost", "example.com", "example.org", "example.net"}
                and not host.endswith((".invalid", ".example")) and not any(c.isspace() for c in value))
    except ValueError:
        return False


def protected_leaf(path, uid, *, directory=False, mode=None):
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != uid or info.st_mode & 0o022:
        raise HostError(f"unsafe ownership/type/permissions: {path}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise HostError(f"required mode {mode:o}: {path}")
    if not directory and info.st_nlink != 1:
        raise HostError(f"hard-linked protected file: {path}")


def protected_root_path(path, *, directory=False):
    if not path.is_absolute() or ".." in path.parts:
        raise HostError("protected paths must be absolute and normalized")
    for parent in reversed(path.parents):
        protected_leaf(parent, 0, directory=True)
    protected_leaf(path, 0, directory=directory)


def validate_policy(policy):
    ids = [policy.get("control_uid"), policy.get("runner_uid")]
    builders = policy.get("builder_uids")
    if (not isinstance(builders, dict) or
            set(builders) not in ({"DEVIN", "GROK_BUILD", "GLM"}, set(WRAPPERS))):
        raise HostError("builder_uids must register the legacy builders and optionally CURSOR")
    ids.extend(builders.values())
    if any(type(uid) is not int or uid <= 0 for uid in ids) or len(set(ids)) != len(ids):
        raise HostError("control, runner and builder Unix UIDs must be distinct and non-root")
    if type(policy.get("max_active_sessions")) is not int or policy["max_active_sessions"] < 1:
        raise HostError("max_active_sessions must be a positive integer")
    launch_limit = policy.get("max_launches_per_24h")
    if launch_limit is not None and (type(launch_limit) is not int or launch_limit < 1):
        raise HostError("max_launches_per_24h must be null (unlimited) or a positive integer")
    repos, enabled = policy.get("allowed_repositories"), policy.get("enabled_builders")
    if (not isinstance(repos, list) or not repos or
            any(not isinstance(repo, str) or len(repo.split("/")) != 2 or not all(repo.split("/")) for repo in repos)):
        raise HostError("allowed_repositories must contain owner/repository names")
    if (not isinstance(enabled, list) or not enabled or
            any(not isinstance(builder, str) or builder not in builders for builder in enabled)
            or len(set(enabled)) != len(enabled)):
        raise HostError("enabled_builders must be a nonempty subset of registered builders")
    if policy.get("wrapper_paths") != {builder: WRAPPERS[builder] for builder in builders}:
        raise HostError("wrapper_paths must match fixed installed adapter paths")
    if not evidence_url(policy.get("boundary_evidence_pointer")):
        raise HostError("provisioned boundary evidence URL is required")
    path = policy.get("ledger_path")
    if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts:
        raise HostError("ledger_path must be absolute and normalized")


def authorize_identity(policy, command):
    if os.getuid() != policy["control_uid"] or os.geteuid() != policy["control_uid"]:
        raise HostError("helper must run as the configured non-root control identity")
    caller = os.environ.get("SUDO_UID", "")
    if not caller.isdecimal():
        raise HostError("helper requires a sudo-authenticated caller")
    caller = int(caller)
    if caller in policy["builder_uids"].values() or caller == policy["control_uid"]:
        raise HostError("builder/control identities cannot invoke admission commands")
    if command in OPERATOR_ONLY and caller == policy["runner_uid"]:
        raise HostError("operator-only command; runner is forbidden")


def load_host_policy(command):
    protected_root_path(POLICY_PATH)
    policy = parse_json(POLICY_PATH.read_text(encoding="utf-8"))
    validate_policy(policy)
    authorize_identity(policy, command)
    if Path(__file__).absolute() != INSTALLED_PATH:
        raise HostError("helper must execute from its protected installed path")
    protected_root_path(INSTALLED_PATH)
    for builder in policy["enabled_builders"]:
        wrapper = WRAPPERS[builder]
        protected_root_path(Path(wrapper))
        if not os.access(wrapper, os.X_OK):
            raise HostError(f"adapter is not executable: {wrapper}")
    ledger = Path(policy["ledger_path"])
    protected_root_path(ledger.parent.parent, directory=True)
    protected_leaf(ledger.parent, policy["control_uid"], directory=True, mode=0o700)
    for file in [ledger, *(Path(str(ledger) + suffix) for suffix in ("-journal", "-wal", "-shm", ".inflight.lock"))]:
        if command == "init" and file == ledger and not file.exists() and not file.is_symlink():
            continue
        if file == ledger or file.exists() or file.is_symlink():
            protected_leaf(file, policy["control_uid"], mode=0o600)
    return policy


def packet_role(packet):
    return packet.get("role", "WRITER") if packet.get("schema_version") == 2 else "WRITER"


def validate_packet(packet, policy):
    if type(packet.get("schema_version")) is not int or packet["schema_version"] not in (1, 2):
        raise HostError("unsupported launch packet schema")
    for field in IDENTITY[:-1]:
        value = packet.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\0" in value:
            raise HostError(f"invalid packet field: {field}")
    if type(packet.get("attempt_id")) is not int or packet["attempt_id"] < 1:
        raise HostError("attempt_id must be a positive integer")
    if packet["repository"] not in policy["allowed_repositories"]:
        raise HostError("repository is not admitted by host policy")
    if packet["builder_id"] not in policy["enabled_builders"]:
        raise HostError("builder is not enabled by host policy")
    if packet["schema_version"] == 2:
        role, owner = packet.get("role"), packet.get("owner_lane")
        if role not in ROLES:
            raise HostError("packet role must be WRITER or REVIEWER")
        if owner not in WRAPPERS:
            raise HostError("packet owner_lane must name a registered lane")
        if role == "WRITER" and owner != packet["builder_id"]:
            raise HostError("a writer runs on its own owner lane")
        if role == "REVIEWER":
            if owner == packet["builder_id"]:
                raise HostError("a reviewer lane must differ from the task owner lane")
            key = packet.get("review_request_id")
            if not isinstance(key, str) or not REQUEST_RE.fullmatch(key):
                raise HostError("reviewer packet needs a 24-hex review_request_id")
            if not isinstance(packet.get("head_sha"), str) or not SHA_RE.fullmatch(packet["head_sha"]):
                raise HostError("reviewer packet needs the exact 40-hex head_sha")
    canonical(packet)


def pin_mac(nonce, fields):
    return hmac.new(bytes.fromhex(nonce), "|".join(fields).encode(), hashlib.sha256).hexdigest()


def verify_pin(row, line):
    """Verify a session-signed marker line against the stored packet.

    A session whose packet carries a signing key is released only with one of its own
    signed lines; None ("nothing pinned") is left for keyless legacy packets.
    """
    packet = parse_json(row["packet"])
    key = packet.get("review_nonce" if row["role"] == "REVIEWER" else "delivery_nonce")
    if line is None:
        if key is not None:
            raise HostError("this session signs its markers; a signed line is required to release it")
        return {"kind": "NONE"}
    if not isinstance(line, str) or len(line) > 512:
        raise HostError("invalid pin line")
    blocker = BLOCKER_PIN_RE.fullmatch(line)
    if blocker:
        kind, launch, mac = blocker.groups()
        if launch != packet["launch_request_id"]:
            raise HostError("blocker pin does not name this session's launch")
        if row["role"] == "REVIEWER" and kind == "DECISION_REQUIRED":
            raise HostError("a reviewer escalates with the DECISION_REQUIRED verdict, not a blocker")
        fields = ("ASTRA_BLOCKED_V1", launch, kind)
        pin = {"kind": "BLOCKER", "blocker": kind}
        nonce = key
    elif row["role"] == "REVIEWER":
        match, nonce = REVIEW_PIN_RE.fullmatch(line), packet.get("review_nonce")
        if not match:
            raise HostError("a reviewer pin must be exactly one ASTRA_REVIEW_V1 or ASTRA_BLOCKED_V1 line")
        review, head, verdict, depth, required, change, mac = match.groups()
        if review != packet.get("review_request_id") or head != packet.get("head_sha"):
            raise HostError("review pin does not name this session's review request and head")
        fields = ("ASTRA_REVIEW_V1", review, head, verdict, depth, required, change)
        pin = {"kind": "REVIEW", "review": review, "head": head, "verdict": verdict, "depth": depth,
               "required": required, "contract_change": change}
    else:
        match, nonce = DELIVERY_PIN_RE.fullmatch(line), packet.get("delivery_nonce")
        if not match:
            raise HostError("a writer pin must be exactly one ASTRA_DELIVERY_V1 or ASTRA_BLOCKED_V1 line")
        pr, head, mac = match.groups()
        fields = ("ASTRA_DELIVERY_V1", packet["launch_request_id"], pr, head)
        pin = {"kind": "DELIVERY", "pr": int(pr), "head": head}
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise HostError("launch packet has no signing nonce; the pin cannot be verified")
    if not hmac.compare_digest(pin_mac(nonce, fields), mac):
        raise HostError("pin MAC does not verify with this session's key")
    return pin


def iso(ts):
    # Floored to the second, the resolution GitHub timestamps have.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def row_view(row):
    """Public view of one launch row. Never includes the packet or its nonces."""
    packet = parse_json(row["packet"]) if row["packet"] else {}
    view = {"launch_request_id": row["request"], "state": row["state"], "role": row["role"], "lane": row["lane"],
            "repository": row["repository"], "task": row["task"], "attempt_id": packet.get("attempt_id"),
            "task_revision": packet.get("task_revision"),
            "owner_lane": packet.get("owner_lane"), "reserved_at": iso(row["created"]), "reserved_ts": row["created"]}
    if row["role"] == "REVIEWER":
        view.update(review_request_id=row["review_key"], head_sha=packet.get("head_sha"),
                    pr_number=packet.get("pr_number"))
    if row["evidence"]:
        evidence = parse_json(row["evidence"])
        view.update(resolution=evidence.get("resolution"), pin=evidence.get("pin"),
                    evidence=evidence.get("terminal_evidence"))
        if isinstance(evidence.get("at"), (int, float)):
            view.update(released_at=iso(evidence["at"]), released_ts=evidence["at"])
    return view


def result_for(packet, outcome, reason=None, session_id=None):
    result = {key: packet[key] for key in IDENTITY}
    result.update(outcome=outcome, session_id=session_id)
    if reason:
        result["reason"] = reason
    return result


V2_SCHEMA = """
    CREATE UNIQUE INDEX one_active_writer ON launches(repository, task)
        WHERE role='WRITER' AND state IN ('SUBMITTING','CONFIRMED','UNKNOWN');
    CREATE UNIQUE INDEX one_active_review ON launches(repository, task, review_key)
        WHERE role='REVIEWER' AND state IN ('SUBMITTING','CONFIRMED','UNKNOWN');
    CREATE UNIQUE INDEX one_active_per_lane ON launches(lane)
        WHERE state IN ('SUBMITTING','CONFIRMED','UNKNOWN');
    CREATE TABLE materializations (
        program TEXT NOT NULL, node TEXT NOT NULL, repository TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt >= 1), request TEXT NOT NULL,
        plan_commit TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('SUBMITTING','CREATED','UNKNOWN','ABANDONED')),
        issue INTEGER, sealed TEXT NOT NULL, updated REAL NOT NULL, evidence TEXT,
        PRIMARY KEY(program, node));
"""


def materialize_request(program, node, attempt):
    return hashlib.sha256(f"materialize\0{program}\0{node}\0{attempt}".encode()).hexdigest()[:24]


def safe_key(value, name):
    if not isinstance(value, str) or not SAFE_KEY.fullmatch(value):
        raise HostError(f"invalid {name}")
    return value


def own_pid_namespace_is_procs(status="/proc/self/status"):
    """True when this process's PID namespace is the one the /proc mount shows (NSpid has one level).

    Lanes are started by this helper's launch, so they live in this namespace or below it,
    and every process in those namespaces is visible in this /proc.
    """
    try:
        for line in Path(status).read_text(encoding="utf-8").splitlines():
            if line.startswith("NSpid:"):
                return len(line.split()[1:]) == 1
    except OSError:
        return False
    return False


def has_subordinate_ids(uid, files=("/etc/subuid", "/etc/subgid")):
    """True when the lane may map subordinate IDs (its user-namespace processes would use other host UIDs)."""
    try:
        names = {str(uid), pwd.getpwuid(uid).pw_name}
    except KeyError:
        names = {str(uid)}
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return True  # cannot prove absence
        if any(line.split(":", 1)[0].strip() in names for line in text.splitlines() if ":" in line):
            return True
    return False


def proc_hides_processes(mountinfo="/proc/self/mountinfo"):
    """True unless the topmost /proc mount is procfs without hidepid (other users' processes visible)."""
    try:
        lines = Path(mountinfo).read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    visible = None
    for line in lines:  # later entries are mounted over earlier ones: the last /proc wins
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if sep and len(fields) > 4 and fields[4] == "/proc":
            if not tail or tail[0] != "proc":
                visible = False
                continue
            options = dict(opt.partition("=")[::2] for opt in (tail[2] if len(tail) > 2 else "").split(","))
            visible = options.get("hidepid", "0") in ("0", "off")
    return visible is not True


def live_processes(uid, proc="/proc"):
    """PIDs whose real, effective, saved or filesystem UID is `uid` (host-side, unforgeable).

    A process that exits during the scan is skipped. Any status that exists but cannot be read
    or parsed makes the scan unverifiable (fail closed), never "absent".
    """
    found = []
    with os.scandir(proc) as entries:
        pids = [entry for entry in entries if entry.name.isdecimal()]
    for entry in pids:
        try:
            text = Path(entry.path, "status").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            raise HostError(f"cannot read /proc/{entry.name}/status; lane quiescence unverifiable") from exc
        uids = None
        for line in text.splitlines():
            if line.startswith("Uid:"):
                try:
                    uids = {int(v) for v in line.split()[1:5]}
                except ValueError:
                    uids = None
                break
        if not uids:
            raise HostError(f"unparseable /proc/{entry.name}/status; lane quiescence unverifiable")
        if uid in uids:
            found.append(int(entry.name))
    return found


QUIESCENCE_GRACE = 10.0  # seconds a lane may take to exit after reporting a prestart failure


def lane_census(lane):
    """Race-free census of the lane UID, taken from inside the lane through its wrapper.

    The lane identity sends SIGSTOP to every process of its UID with kill(-1). That cannot
    race a fork: the kernel aborts (and later restarts) any fork that sees the pending stop,
    and kill(-1) iterates the task list under the lock that attaching a child needs. So the
    stopped set can only shrink until SIGCONT; the census lists it and thaws it. A single
    /proc scan from outside has no such guarantee (a parent can fork and exit between reads).
    """
    completed = subprocess.run([WRAPPERS[lane], "--quiescence"], capture_output=True, text=True,
                               timeout=60, cwd="/", env=CLEAN_ENV)
    report = parse_json(completed.stdout)
    live = report.get("live")
    if (completed.returncode != 0 or report.get("status") != "OK" or not isinstance(live, list)
            or not all(type(pid) is int and pid > 0 for pid in live)):
        raise HostError("lane census failed; lane quiescence unverifiable")
    return live


@contextmanager
def census_lock(lane, policy):
    """One frozen census per lane at a time.

    Two concurrent censuses of one lane would SIGSTOP each other and could leave the lane
    frozen, so the host serializes them. The wait is bounded by the census's own timeout.
    """
    if lane not in WRAPPERS:
        raise HostError("unknown lane")
    try:
        fd = os.open(str(Path(policy["ledger_path"]).parent / f"census-{lane}.lock"),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise HostError("census lock unavailable; lane quiescence unverifiable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise HostError("unsafe census lock file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def lane_quiescence(lane, policy, *, census=None):
    if proc_hides_processes():
        raise HostError("/proc hides processes (hidepid); lane quiescence cannot be verified")
    if not own_pid_namespace_is_procs():
        raise HostError("/proc is not this helper's PID namespace; lane quiescence cannot be verified")
    uid = policy["builder_uids"].get(lane)
    if type(uid) is not int:
        raise HostError("lane has no registered builder UID")
    if has_subordinate_ids(uid):
        raise HostError("lane UID has subordinate uid/gid ranges; its user-namespace processes would be invisible")
    # The outside scan is kept as a second witness; the frozen census is the race-free one.
    outside = live_processes(uid)
    with census_lock(lane, policy):
        frozen = (census or lane_census)(lane)
    return sorted(set(outside) | set(frozen))


def lane_empty(lane, policy, quiescence, grace=None, clock=time.monotonic, sleep=time.sleep):
    """True once the host itself sees no process of the lane UID (within grace); never on doubt."""
    deadline = clock() + (QUIESCENCE_GRACE if grace is None else grace)
    while True:
        try:
            if not quiescence(lane, policy):
                return True
        except HostError:
            return False
        if clock() >= deadline:
            return False
        sleep(0.5)


class Ledger:
    """Filesystem policy is checked by CLI; this class also supports isolated unit tests."""
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock

    def connect(self, *, allow_version=(SCHEMA_VERSION,)):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA trusted_schema=OFF")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in allow_version or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise HostError(f"ledger schema v{version} unavailable or corrupt (need v{SCHEMA_VERSION}; run migrate)")
            columns = "packet, state, result, admitted, created, evidence"
            if version >= 2:
                columns += ", role, lane, review_key"
                db.execute("SELECT program, request, state, sealed FROM materializations LIMIT 0")
            db.execute(f"SELECT {columns} FROM launches LIMIT 0")
        except Exception:
            db.close()
            raise
        return db

    def initialize(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            db.executescript("""
                PRAGMA synchronous=FULL;
                BEGIN IMMEDIATE;
                CREATE TABLE launches (
                    request TEXT PRIMARY KEY, repository TEXT NOT NULL, task TEXT NOT NULL,
                    packet TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                    ('SUBMITTING','CONFIRMED','UNKNOWN','FAILED_PRESTART','RECONCILED')),
                    result TEXT NOT NULL, admitted INTEGER NOT NULL CHECK(admitted IN (0,1)),
                    created REAL NOT NULL, evidence TEXT,
                    role TEXT NOT NULL DEFAULT 'WRITER' CHECK(role IN ('WRITER','REVIEWER')),
                    lane TEXT NOT NULL DEFAULT '', review_key TEXT NOT NULL DEFAULT '');
            """ + V2_SCHEMA + f"PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
        finally:
            db.close()

    def migrate(self, target):
        """Operator-only, in place, one transaction; backs up the v1 file first."""
        if target != SCHEMA_VERSION:
            raise HostError(f"only migration to v{SCHEMA_VERSION} is supported")
        with self.inflight_lock(exclusive=True):
            db = self.connect(allow_version=(1, SCHEMA_VERSION))
            try:
                if db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                    return {"status": "ALREADY_V2"}
                backup = self.path.with_name(f"{self.path.name}.v1-backup-{int(self.clock())}")
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                target_db = sqlite3.connect(str(backup))
                try:
                    db.backup(target_db)
                finally:
                    target_db.close()
                db.execute("BEGIN IMMEDIATE")
                try:
                    for statement in (
                            "ALTER TABLE launches ADD COLUMN role TEXT NOT NULL DEFAULT 'WRITER' "
                            "CHECK(role IN ('WRITER','REVIEWER'))",
                            "ALTER TABLE launches ADD COLUMN lane TEXT NOT NULL DEFAULT ''",
                            "ALTER TABLE launches ADD COLUMN review_key TEXT NOT NULL DEFAULT ''",
                            "UPDATE launches SET lane=json_extract(packet, '$.builder_id') WHERE packet != ''",
                            "DROP INDEX one_active_task"):
                        db.execute(statement)
                    for statement in V2_SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                return {"status": "MIGRATED", "from": 1, "to": SCHEMA_VERSION, "backup": str(backup)}
            finally:
                db.close()

    @contextmanager
    def inflight_lock(self, *, exclusive=False):
        # Shared across launch invocations; reconciliation requires every invocation
        # to have stopped sending before an operator can release an unresolved slot.
        fd = os.open(str(self.path) + ".inflight.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise HostError("unsafe in-flight lock file")
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HostError("launch or operator reconciliation is still in flight") from exc
            yield fd
        finally:
            os.close(fd)

    def reserve(self, packet, policy):
        validate_packet(packet, policy)
        body, request = canonical(packet), packet["launch_request_id"]
        role, lane = packet_role(packet), packet["builder_id"]
        review_key = packet.get("review_request_id", "") if role == "REVIEWER" else ""
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM launches WHERE request=?", (request,)).fetchone()
            if previous:
                if previous["packet"] != body:
                    raise HostError("launch_request_id reused with a different packet")
                db.commit()
                if previous["state"] == "RECONCILED":
                    # The stored result names a session that is gone; replaying it would revive it.
                    return False, result_for(packet, "FAILED_PRESTART",
                                             "request already released on the host; a resume is a new attempt")
                return False, parse_json(previous["result"])
            task = (packet["repository"], packet["task_id"])
            reason = None
            if role == "WRITER" and db.execute(
                    f"SELECT 1 FROM launches WHERE repository=? AND task=? AND role='WRITER' AND {ACTIVE_SQL}",
                    task).fetchone():
                reason = "task already has an active or unresolved owner"
            elif role == "WRITER" and db.execute(
                    f"SELECT 1 FROM launches WHERE repository=? AND task=? AND role='REVIEWER' AND {ACTIVE_SQL}",
                    task).fetchone():
                reason = "task has an active or unresolved review; the writer waits"
            elif role == "REVIEWER" and db.execute(
                    f"SELECT 1 FROM launches WHERE repository=? AND task=? AND role='WRITER' AND {ACTIVE_SQL}",
                    task).fetchone():
                reason = "task writer session is still active or unresolved; review waits"
            elif role == "REVIEWER" and db.execute(
                    f"SELECT 1 FROM launches WHERE repository=? AND task=? AND role='REVIEWER' "
                    f"AND review_key=? AND {ACTIVE_SQL}", (*task, review_key)).fetchone():
                reason = "review request already has an active or unresolved session"
            elif db.execute(f"SELECT 1 FROM launches WHERE lane=? AND {ACTIVE_SQL}", (lane,)).fetchone():
                reason = "lane busy"
            elif db.execute(f"SELECT count(*) FROM launches WHERE {ACTIVE_SQL}").fetchone()[0] >= policy["max_active_sessions"]:
                reason = "host max_active_sessions reached"
            elif (policy.get("max_launches_per_24h") is not None and
                  db.execute("SELECT count(*) FROM launches WHERE admitted=1 AND created>=?",
                             (self.clock() - 86400,)).fetchone()[0] >= policy["max_launches_per_24h"]):
                reason = "host max_launches_per_24h reached"
            state = "FAILED_PRESTART" if reason else "SUBMITTING"
            result = result_for(packet, "FAILED_PRESTART" if reason else "UNKNOWN",
                                reason or "durable SUBMITTING reservation; outcome unresolved")
            db.execute("INSERT INTO launches (request, repository, task, packet, state, result, admitted, "
                       "created, evidence, role, lane, review_key) VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?)",
                       (request, packet["repository"], packet["task_id"], body, state,
                        canonical(result), int(reason is None), self.clock(), role, lane, review_key))
            db.commit()  # FULL synchronous commit precedes every provider invocation.
            return reason is None, result
        finally:
            db.close()

    def finalize(self, packet, result):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE launches SET state=?, result=? WHERE request=? AND packet=? AND state='SUBMITTING'",
                                 (result["outcome"], canonical(result), packet["launch_request_id"], canonical(packet))).rowcount
            if changed != 1:
                raise HostError("reservation changed before finalization; operator reconciliation required")
            db.commit()
        finally:
            db.close()

    def status(self, request):
        # Read-only. RECONCILED and FAILED_PRESTART rows are terminal (finalize and
        # reconcile only move active rows), so no in-flight lock is required.
        if not isinstance(request, str) or not request.strip() or len(request) > 4096 or "\0" in request:
            raise HostError("invalid launch_request_id")
        db = self.connect()
        try:
            row = db.execute("SELECT * FROM launches WHERE request=?", (request,)).fetchone()
        finally:
            db.close()
        if row is None:
            return {"status": "NOT_FOUND", "launch_request_id": request, "state": None}
        # reserved_at is host time: nothing the session wrote can predate its reservation.
        return {"status": "FOUND", **row_view(row)}

    def task_status(self, repository, task):
        """Read-only: every launch row of one task, the host record merge readiness is computed from."""
        if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
            raise HostError("invalid repository")
        if not isinstance(task, str) or not task.strip() or len(task) > 256 or "\0" in task:
            raise HostError("invalid task")
        db = self.connect()
        try:
            rows = db.execute("SELECT * FROM launches WHERE repository=? AND task=? ORDER BY created, request",
                              (repository, task)).fetchall()
        finally:
            db.close()
        return {"status": "OK", "repository": repository, "task": task, "rows": [row_view(row) for row in rows]}

    def lanes(self, policy):
        """Read-only lane board source: active reservations per registered lane."""
        db = self.connect()
        try:
            rows = db.execute(f"SELECT request, repository, task, role, lane, state, created FROM launches "
                              f"WHERE {ACTIVE_SQL} ORDER BY created").fetchall()
        finally:
            db.close()
        active = [dict(row) for row in rows]
        return {"status": "OK", "max_active_sessions": policy["max_active_sessions"], "active_total": len(active),
                "lanes": [{"lane": lane, "enabled": lane in policy["enabled_builders"],
                           "active": [row for row in active if row["lane"] == lane]}
                          for lane in WRAPPERS if lane in policy["builder_uids"]]}

    def reap(self, request, evidence, policy, *, pin=None, quiescence=None):
        """Retire a CONFIRMED session when its lane UID has no live process; pin its signed marker.

        Write-once: a repeated reap must present the same evidence and pin, and gets the stored result.
        """
        if not isinstance(request, str) or not REQUEST_RE.fullmatch(request):
            raise HostError("invalid launch_request_id")
        if not evidence_url(evidence):
            raise HostError("durable deliverable evidence URL required")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM launches WHERE request=?", (request,)).fetchone()
            if row is None:
                raise HostError("no reservation for request")
            if row["state"] == "RECONCILED":
                stored = parse_json(row["evidence"] or "{}")
                if stored.get("resolution") != "SESSION_TERMINAL_VERIFIED":
                    raise HostError("request was reconciled by an operator, not reaped")
                if stored.get("terminal_evidence") != evidence or stored.get("pin") != verify_pin(row, pin):
                    raise HostError("reap is write-once: this session was released with different evidence or pin")
                db.commit()
                return {"status": "RECONCILED", "resolution": "SESSION_TERMINAL_VERIFIED",
                        "launch_request_id": request, "session_id": stored.get("session_id"),
                        "evidence": stored.get("terminal_evidence"), "pin": stored.get("pin"), "repeated": True}
            if row["state"] != "CONFIRMED":
                raise HostError(f"only a CONFIRMED session can be reaped (state {row['state']}); "
                                "UNKNOWN/SUBMITTING stay operator-only")
            session = parse_json(row["result"]).get("session_id")
            if not isinstance(session, str) or not session.strip():
                raise HostError("confirmed row lacks its session id")
            pinned = verify_pin(row, pin)
            if pinned["kind"] == "DELIVERY":
                # One PR is the delivery of one task: otherwise the author of a PR could review it
                # as a "non-owner" lane of the second task.
                for other in db.execute("SELECT task, evidence FROM launches WHERE repository=? AND role='WRITER' "
                                        "AND state='RECONCILED' AND task<>? AND evidence IS NOT NULL",
                                        (row["repository"], row["task"])).fetchall():
                    if (parse_json(other["evidence"]).get("pin") or {}).get("pr") == pinned["pr"]:
                        raise HostError(f"PR {pinned['pr']} is already the delivery of task {other['task']}")
            pids = (quiescence or lane_quiescence)(row["lane"], policy)
            if pids:
                raise HostError(f"lane {row['lane']} still has {len(pids)} live process(es); session not terminal")
            at = self.clock()
            record = {"resolution": "SESSION_TERMINAL_VERIFIED", "session_id": session,
                      "verifier": "LANE_UID_QUIESCENT", "lane": row["lane"], "role": row["role"],
                      "terminal_evidence": evidence, "pin": pinned, "at": at}
            db.execute("UPDATE launches SET state='RECONCILED', evidence=? WHERE request=? AND state='CONFIRMED'",
                       (canonical(record), request))
            db.commit()
            return {"status": "RECONCILED", "resolution": "SESSION_TERMINAL_VERIFIED", "launch_request_id": request,
                    "session_id": session, "evidence": evidence, "pin": pinned, "repeated": False}
        finally:
            db.close()

    def materialize_begin(self, program, node, repository, plan_commit, policy):
        safe_key(program, "program")
        safe_key(node, "node")
        if repository not in policy["allowed_repositories"]:
            raise HostError("repository is not admitted by host policy")
        if not isinstance(plan_commit, str) or not SHA_RE.fullmatch(plan_commit):
            raise HostError("plan_commit must be a 40-hex commit SHA")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM materializations WHERE program=? AND node=?", (program, node)).fetchone()
            if row is not None and row["repository"] != repository:
                raise HostError("program node is bound to a different repository")
            if row is None or row["state"] == "ABANDONED":
                attempt = 1 if row is None else row["attempt"] + 1
                request = materialize_request(program, node, attempt)
                sealed = "[]" if row is None else row["sealed"]
                db.execute("INSERT OR REPLACE INTO materializations VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                           (program, node, repository, attempt, request, plan_commit, "SUBMITTING",
                            None, sealed, self.clock()))
                db.commit()  # durable before any GitHub create request
                return {"decision": "CREATE_ALLOWED", "program": program, "node": node, "request": request,
                        "attempt": attempt, "plan_commit": plan_commit, "sealed": json.loads(sealed)}
            db.commit()
            decision = "CREATED" if row["state"] == "CREATED" else "UNRESOLVED"
            return {"decision": decision, "program": program, "node": node, "request": row["request"],
                    "attempt": row["attempt"], "state": row["state"], "issue": row["issue"],
                    "plan_commit": row["plan_commit"], "sealed": json.loads(row["sealed"])}
        finally:
            db.close()

    def materialize_finish(self, program, node, request, outcome, issue):
        safe_key(program, "program")
        safe_key(node, "node")
        if outcome not in ("CREATED", "UNKNOWN"):
            raise HostError("outcome must be CREATED or UNKNOWN")
        if outcome == "CREATED" and (type(issue) is not int or issue < 1):
            raise HostError("CREATED needs the issue number")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM materializations WHERE program=? AND node=?", (program, node)).fetchone()
            if row is None:
                raise HostError("no materialization request for program node")
            if request != row["request"]:
                # A late response of an abandoned request can never replace the current one.
                raise HostError("request is not the current materialization request (sealed or stale)")
            if row["state"] == "CREATED":
                if outcome == "CREATED" and issue == row["issue"]:
                    db.commit()
                    return {"status": "CREATED", "issue": issue, "repeated": True}
                raise HostError("program node is already CREATED with a different issue")
            if row["state"] not in ("SUBMITTING", "UNKNOWN"):
                raise HostError(f"cannot finish a {row['state']} request")
            db.execute("UPDATE materializations SET state=?, issue=?, updated=? WHERE program=? AND node=?",
                       (outcome, issue if outcome == "CREATED" else None, self.clock(), program, node))
            db.commit()
            return {"status": outcome, "issue": issue if outcome == "CREATED" else None, "repeated": False}
        finally:
            db.close()

    def materialize_resolve(self, program, node, request, evidence):
        """Operator-only: record a verified non-creation, sealing the request id forever."""
        safe_key(program, "program")
        safe_key(node, "node")
        if not evidence_url(evidence):
            raise HostError("operator evidence URL required")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM materializations WHERE program=? AND node=?", (program, node)).fetchone()
            if row is None or row["request"] != request or row["state"] not in ("SUBMITTING", "UNKNOWN"):
                raise HostError("only the current SUBMITTING/UNKNOWN request can be resolved as not created")
            sealed = json.loads(row["sealed"]) + [request]
            db.execute("UPDATE materializations SET state='ABANDONED', sealed=?, updated=?, evidence=? "
                       "WHERE program=? AND node=?",
                       (json.dumps(sealed), self.clock(),
                        canonical({"resolution": "NOT_CREATED", "evidence": evidence}), program, node))
            db.commit()
            return {"status": "ABANDONED", "sealed": sealed}
        finally:
            db.close()

    def materialize_plan(self, program, node, old, new):
        """Compare-and-swap the node's plan commit. The runtime proves `new` descends from `old`."""
        safe_key(program, "program")
        safe_key(node, "node")
        for value in (old, new):
            if not isinstance(value, str) or not SHA_RE.fullmatch(value):
                raise HostError("plan commits must be 40-hex SHAs")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE materializations SET plan_commit=?, updated=? "
                                 "WHERE program=? AND node=? AND plan_commit=?",
                                 (new, self.clock(), program, node, old)).rowcount
            db.commit()
        finally:
            db.close()
        if changed != 1:
            raise HostError("STALE_PLAN: the recorded plan commit changed or does not match")
        return {"status": "PLAN_ADVANCED", "plan_commit": new}

    def materialize_status(self, program, node):
        safe_key(program, "program")
        safe_key(node, "node")
        db = self.connect()
        try:
            row = db.execute("SELECT * FROM materializations WHERE program=? AND node=?", (program, node)).fetchone()
        finally:
            db.close()
        if row is None:
            return {"status": "NOT_FOUND", "program": program, "node": node}
        return {"status": row["state"], "program": program, "node": node, "request": row["request"],
                "attempt": row["attempt"], "issue": row["issue"], "plan_commit": row["plan_commit"],
                "repository": row["repository"], "sealed": json.loads(row["sealed"])}

    def reconcile(self, request, session, evidence, *, no_session=False, sender_fenced=False,
                  never_admitted=False):
        with self.inflight_lock(exclusive=True):
            return self._reconcile(request, session, evidence, no_session=no_session,
                                   sender_fenced=sender_fenced, never_admitted=never_admitted)

    def _reconcile(self, request, session, evidence, *, no_session=False, sender_fenced=False,
                   never_admitted=False):
        if not evidence_url(evidence):
            raise HostError("operator-verified terminal session and durable evidence URL required")
        if no_session:
            if session is not None or sender_fenced is not True:
                raise HostError("no-session reconciliation requires explicit sender fencing and no session ID")
        elif not isinstance(session, str) or not session.strip():
            raise HostError("terminal session ID is required")
        if never_admitted and not no_session:
            raise HostError("never-admitted reconciliation requires --no-session --sender-fenced")
        if not isinstance(request, str) or not request.strip() or len(request) > 4096 or "\0" in request:
            raise HostError("invalid launch_request_id")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM launches WHERE request=?", (request,)).fetchone()
            if never_admitted:
                # The request never reached this ledger (e.g. the SUBMITTING write
                # succeeded but launch never ran). Record a terminal tombstone so a
                # late send of this request is refused (packet mismatch) and a
                # retry can prove the prior attempt is fenced.
                if row is not None:
                    raise HostError("request is in the ledger; never-admitted reconciliation does not apply")
                at = self.clock()
                db.execute("INSERT INTO launches (request, repository, task, packet, state, result, admitted, "
                           "created, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
                           (request, "", "", "", "RECONCILED",
                            canonical({"launch_request_id": request, "outcome": "UNKNOWN", "session_id": None,
                                       "reason": "never admitted; operator-recorded fence"}),
                            0, at, canonical({"resolution": "NEVER_ADMITTED", "session_id": None,
                                              "sender_fenced": True, "terminal_evidence": evidence, "at": at})))
                db.commit()
                return {"status": "RECONCILED", "resolution": "NEVER_ADMITTED", "launch_request_id": request,
                        "session_id": None, "evidence": evidence}
            if row is None or row["state"] not in ACTIVE:
                raise HostError("no active reservation to reconcile")
            known = parse_json(row["result"]).get("session_id")
            if no_session and (known is not None or row["state"] == "CONFIRMED"):
                raise HostError("known session cannot be reconciled as no-session")
            if not no_session and known is not None and known != session:
                raise HostError("reconciliation session does not match recorded owner")
            resolution = "NO_SESSION_CONFIRMED" if no_session else "SESSION_TERMINAL"
            db.execute("UPDATE launches SET state='RECONCILED', evidence=? WHERE request=?",
                       (canonical({"resolution": resolution, "session_id": session,
                                   "sender_fenced": sender_fenced, "terminal_evidence": evidence,
                                   "at": self.clock()}), request))
            db.commit()
            return {"status": "RECONCILED", "resolution": resolution, "launch_request_id": request,
                    "session_id": session, "evidence": evidence}
        finally:
            db.close()


def adapter_launch(packet, policy, lock_fd):
    # Packet stays within the control-owned directory, never the caller's workspace.
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", prefix="packet-",
                                     dir=Path(policy["ledger_path"]).parent) as packet_file:
        packet_file.write(canonical(packet))
        packet_file.flush()
        return subprocess.run([WRAPPERS[packet["builder_id"]], packet_file.name],
                              capture_output=True, text=True, timeout=180, cwd="/", pass_fds=(lock_fd,),
                              env={**CLEAN_ENV, "ASTRA_HOST_INFLIGHT_FD": str(lock_fd)})


def launch(packet, policy, ledger, invoke=None, *, quiescence=None):
    with ledger.inflight_lock() as lock_fd:
        return launch_held(packet, policy, ledger, invoke, lock_fd, quiescence)


def launch_held(packet, policy, ledger, invoke, lock_fd, quiescence=None):
    admitted, previous = ledger.reserve(packet, policy)
    if not admitted:
        return previous
    try:
        completed = adapter_launch(packet, policy, lock_fd) if invoke is None else invoke(packet, policy)
        if completed.returncode != 0:
            raise HostError("adapter exited nonzero; launch outcome ambiguous")
        result = parse_json(completed.stdout)
        if any(type(result.get(key)) is not type(packet[key]) or result.get(key) != packet[key] for key in IDENTITY):
            raise HostError("adapter result identity mismatch")
        outcome, session = result.get("outcome"), result.get("session_id")
        if outcome not in {"CONFIRMED", "FAILED_PRESTART", "UNKNOWN"}:
            raise HostError("adapter returned invalid outcome")
        if session is not None and (not isinstance(session, str) or not session.strip()):
            raise HostError("adapter returned invalid session_id")
        if result.get("reason") is not None and not isinstance(result["reason"], str):
            raise HostError("adapter returned invalid reason")
        if outcome == "CONFIRMED" and (not isinstance(session, str) or not session.strip()):
            raise HostError("adapter confirmation lacks actual session_id")
        if outcome == "FAILED_PRESTART" and (session is not None or not isinstance(result.get("reason"), str) or not result["reason"].strip()):
            raise HostError("adapter FAILED_PRESTART lacks definite prestart reason or claims a session")
        reason = result.get("reason")
        if outcome == "FAILED_PRESTART" and not lane_empty(packet["builder_id"], policy,
                                                            quiescence or lane_quiescence):
            # The builder UID can write the adapter's result files. A prestart failure frees the
            # slot only when the host itself sees the lane empty; otherwise the slot is kept.
            outcome, reason = "UNKNOWN", f"prestart failure reported but the lane is not quiescent ({reason})"
        result = result_for(packet, outcome, reason, session)
    except Exception as exc:
        result = result_for(packet, "UNKNOWN", f"adapter outcome unresolved: {type(exc).__name__}: {exc}")
    ledger.finalize(packet, result)
    return result


def preflight(builder, policy, ledger):
    if builder not in policy["enabled_builders"]:
        raise HostError("builder is not enabled by host policy")
    db = ledger.connect()
    db.close()
    completed = subprocess.run([WRAPPERS[builder], "--preflight"], capture_output=True,
                               text=True, timeout=90, cwd="/", env=CLEAN_ENV)
    report = parse_json(completed.stdout)
    if (completed.returncode != 0 or report.get("status") != "PASS" or report.get("builder_id") != builder
            or report.get("execution_mode") not in {"REMOTE_SESSION", "PERSISTENT_SUPERVISOR"}
            or report.get("parallel_safe") is not True or type(report.get("launch_contract_version")) is not int
            or report["launch_contract_version"] != 2):
        raise HostError("adapter preflight failed")
    if builder == "CURSOR" and (
            report.get("harness") != "CURSOR_CLI"
            or report.get("execution_mode") != "PERSISTENT_SUPERVISOR"
            or not isinstance(report.get("model"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", report["model"])
            or report["model"].upper() in {"AUTO", "DEFAULT", "CONFIG_REQUIRED", "PENDING", "UNKNOWN"}):
        raise HostError("CURSOR requires explicit Cursor CLI harness/model provenance")
    report.update(host_admission="ENFORCED", ledger_schema_version=SCHEMA_VERSION,
                  boundary_evidence_pointer=policy["boundary_evidence_pointer"],
                  allowed_repositories=policy["allowed_repositories"],
                  helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight").add_argument("--builder-id", choices=tuple(WRAPPERS), required=True)
    commands.add_parser("launch")
    commands.add_parser("init")
    commands.add_parser("migrate").add_argument("--to", type=int, required=True)
    task_parser = commands.add_parser("task-status")
    for flag in ("repository", "task"):
        task_parser.add_argument("--" + flag, required=True)
    status_parser = commands.add_parser("status")
    which = status_parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--launch-request-id")
    which.add_argument("--lanes", action="store_true")
    reap_parser = commands.add_parser("reap")
    for flag in ("launch-request-id", "evidence"):
        reap_parser.add_argument("--" + flag, required=True)
    reap_parser.add_argument("--pin-stdin", action="store_true",
                             help='read {"pin": "<signed marker line>"} from stdin')
    reconcile_parser = commands.add_parser("reconcile")
    for flag in ("launch-request-id", "evidence"):
        reconcile_parser.add_argument("--" + flag, required=True)
    outcome = reconcile_parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--session-id")
    outcome.add_argument("--no-session", action="store_true")
    reconcile_parser.add_argument("--sender-fenced", action="store_true")
    reconcile_parser.add_argument("--never-admitted", action="store_true",
                                  help="record a request that never reached this ledger as fenced")
    begin = commands.add_parser("materialize-begin")
    for flag in ("program", "node", "repository", "plan-commit"):
        begin.add_argument("--" + flag, required=True)
    finish = commands.add_parser("materialize-finish")
    for flag in ("program", "node", "request"):
        finish.add_argument("--" + flag, required=True)
    finish.add_argument("--outcome", choices=("CREATED", "UNKNOWN"), required=True)
    finish.add_argument("--issue", type=int)
    resolve = commands.add_parser("materialize-resolve")
    for flag in ("program", "node", "request", "evidence"):
        resolve.add_argument("--" + flag, required=True)
    resolve.add_argument("--not-created", action="store_true", required=True)
    mplan = commands.add_parser("materialize-plan")
    for flag in ("program", "node", "from", "to"):
        mplan.add_argument("--" + flag, required=True)
    mstatus = commands.add_parser("materialize-status")
    for flag in ("program", "node"):
        mstatus.add_argument("--" + flag, required=True)
    args, packet = parser.parse_args(argv), None
    os.umask(0o077)
    try:
        if args.command == "launch" or getattr(args, "pin_stdin", False):
            raw = sys.stdin.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise HostError("stdin document too large")
            packet = parse_json(raw)
        policy = load_host_policy(args.command)
        ledger = Ledger(policy["ledger_path"])
        if args.command == "init":
            ledger.initialize()
            result = {"status": "INITIALIZED", "schema_version": SCHEMA_VERSION}
        elif args.command == "migrate":
            result = ledger.migrate(args.to)
        elif args.command == "preflight":
            result = preflight(args.builder_id, policy, ledger)
        elif args.command == "launch":
            result = launch(packet, policy, ledger)
        elif args.command == "status":
            result = ledger.lanes(policy) if args.lanes else ledger.status(args.launch_request_id)
        elif args.command == "reap":
            pin = packet.get("pin") if packet is not None else None
            if args.pin_stdin and (set(packet) != {"pin"} or not isinstance(pin, str)):
                raise HostError('reap --pin-stdin expects exactly {"pin": "<line>"}')
            result = ledger.reap(args.launch_request_id, args.evidence, policy, pin=pin)
        elif args.command == "task-status":
            result = ledger.task_status(args.repository, args.task)
        elif args.command == "materialize-begin":
            result = ledger.materialize_begin(args.program, args.node, args.repository, args.plan_commit, policy)
        elif args.command == "materialize-finish":
            result = ledger.materialize_finish(args.program, args.node, args.request, args.outcome, args.issue)
        elif args.command == "materialize-resolve":
            result = ledger.materialize_resolve(args.program, args.node, args.request, args.evidence)
        elif args.command == "materialize-plan":
            result = ledger.materialize_plan(args.program, args.node, getattr(args, "from"), args.to)
        elif args.command == "materialize-status":
            result = ledger.materialize_status(args.program, args.node)
        else:
            result = ledger.reconcile(args.launch_request_id, args.session_id, args.evidence,
                                      no_session=args.no_session, sender_fenced=args.sender_fenced,
                                      never_admitted=args.never_admitted)
        print(canonical(result))
        return 0
    except (HostError, OSError, sqlite3.Error, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"HOST_CONTROL_ERROR: {exc}", file=sys.stderr)
        result = {"status": "ERROR", "reason": str(exc)}
        if args.command == "launch":
            result.update({key: packet[key] for key in IDENTITY if isinstance(packet, dict) and key in packet})
            result.update(outcome="UNKNOWN", session_id=None)
        print(canonical(result))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

