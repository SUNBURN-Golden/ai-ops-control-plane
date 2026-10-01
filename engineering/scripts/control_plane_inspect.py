#!/usr/bin/python3 -I
"""aiops-inspect: the read-only program inspector (User decision M7, docs/INSPECTOR.md).

Advisory only, never a gate. One root-owned host loop (``daemon``) runs ``tick`` once an hour at
``tick_minute`` KST: Gate 0 (config, kill switch, manifest, secrets) -> reconcile UNKNOWN posts ->
T0 probe -> T1 facts (only on change) -> signals and findings -> render (as the render account,
resource-limited) -> publish through the journal -> heartbeat, daily line, dead-man.

Every subcommand prints exactly ONE JSON line on stdout and exits 0 (OK or handled), 1
(``{"status":"ERROR","reason":<CODE>}``) or 2 (argument error). Secrets are never printed: every
output line is redacted against the live secret values. The tool writes only to its own state,
the ONE configured GitHub ledger issue and the configured Slack channel.

Installed as ``/opt/aiops/bin/aiops-inspect`` (a copy of this file, run with ``python3 -I``); the
other modules are loaded from ``/opt/aiops/inspect/lib``. In the repository they are loaded from
this file's directory. ``--root`` is for tests only.
"""
from __future__ import annotations

import os
import sys

INSTALLED_NAME = "aiops-inspect"
INSTALLED_LIB = "/opt/aiops/inspect/lib"


def _lib_dir(script: str) -> str:
    """Where the inspector modules live: the fixed lib dir when installed, else next to this file."""
    path = os.path.abspath(script)
    if os.path.basename(path) == INSTALLED_NAME:
        return INSTALLED_LIB
    return os.path.dirname(path)


# ``python3 -I`` puts neither the script directory nor user site-packages on sys.path.
_LIB = _lib_dir(__file__)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import argparse  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
import fcntl  # noqa: E402
import getpass  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
import pwd  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple  # noqa: E402

import control_plane_inspect_core as core  # noqa: E402
from control_plane_inspect_core import InspectError  # noqa: E402
import control_plane_inspect_charts as charts  # noqa: E402
import control_plane_inspect_facts as facts  # noqa: E402
import control_plane_inspect_github as gh  # noqa: E402
import control_plane_inspect_host as hostmod  # noqa: E402
import control_plane_inspect_publish as pub  # noqa: E402
import control_plane_inspect_signals as sig  # noqa: E402

TOOL_PATH = os.path.abspath(__file__)
CHARTS_PATH = os.path.abspath(charts.__file__)

TICK_CAP_SECONDS = 45 * 60
T1_SECONDS = 480.0
RENDER_WALL_SECONDS = 120
RENDER_LIMITS = (("RLIMIT_CPU", 60), ("RLIMIT_AS", 1536 << 20), ("RLIMIT_FSIZE", 20 << 20),
                 ("RLIMIT_NOFILE", 64), ("RLIMIT_NPROC", 32))
RENDER_OUTPUT_CAP = 64 * 1024
STOP_WAIT_SECONDS = 30
# lock_held() probes (start, status, stop) take daemon.lock for an instant; the daemon retries briefly.
DAEMON_LOCK_TRIES = 10
DAEMON_LOCK_RETRY_SECONDS = 0.1
START_WAIT_SECONDS = 10
WAIT_CHUNK_SECONDS = 2.0
ERROR_WINDOW = timedelta(hours=24)
ERROR_LIMIT = 3
ERROR_KILL_REASON = "ERROR_REPEATED"
# Reasons that engage the kill switch the moment they happen (contract §3.9).
KILL_REASONS = ("TOOL_TAMPERED", "SECRET_LIVE", "SLACK_SCOPE")
DEGRADED_AFTER = 3
RUNS_KEEP = timedelta(days=30)
NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")
DEFAULT_TICK_MINUTE = 17
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
SECRET_ARGS = core.SECRET_KINDS


# ---------------------------------------------------------------------------- injection points

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    """Everything with side effects outside the state dir. Tests replace these with fakes."""

    now: Callable[[], datetime] = _utcnow
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    github_transport: Optional[Any] = None
    slack_transport: Optional[Any] = None
    host_runner: Optional[Any] = None
    # render(ctx, data_path, out_dir, account, font) -> charts result dict ({"status": ...}).
    render: Optional[Callable[..., Dict[str, Any]]] = None
    getpass: Callable[[str], str] = getpass.getpass
    getpwnam: Callable[[str], Any] = pwd.getpwnam
    kill: Callable[[int, int], None] = os.kill
    killpg: Callable[[int, int], None] = os.killpg
    proc_root: str = "/proc"
    spawn_daemon: Optional[Callable[[List[str]], int]] = None
    spawn_tick: Optional[Callable[[List[str]], Any]] = None
    euid: Callable[[], int] = os.geteuid
    install_signals: bool = True


class Ctx:
    """One command: paths, injected dependencies and the live secret values for redaction."""

    def __init__(self, root: Any = "/", deps: Optional[Deps] = None, installed: Optional[bool] = None,
                 check_manifest: Optional[bool] = None):
        self.paths = core.Paths.from_root(root)
        self.deps = deps or Deps()
        # Installed mode (root "/"): root-owned files and the expected-digest manifest are enforced.
        self.installed = (self.paths.root == Path("/")) if installed is None else installed
        self.check_manifest = self.installed if check_manifest is None else check_manifest
        self.live: List[str] = []
        self.run_id: Optional[str] = None
        self._store: Optional[core.StateStore] = None

    # -- basics

    def now(self) -> datetime:
        return core.utc(self.deps.now())

    @property
    def store(self) -> core.StateStore:
        if self._store is None:
            _ensure_dir(self.paths.state.parent, 0o700)
            self._store = core.StateStore(self.paths.state)
        return self._store

    def config(self) -> Dict[str, Any]:
        return core.load_config(self.paths.config, require_root_owner=self.installed)

    def secret(self, kind: str) -> str:
        value = core.load_secret(self.paths.secret(kind), kind, require_root_owner=self.installed)
        if value not in self.live:
            self.live.append(value)
        return value

    def secrets(self) -> Dict[str, str]:
        return {kind: self.secret(kind) for kind in core.SECRET_KINDS}

    def tool_argv(self, *args: str) -> List[str]:
        """argv that runs this tool again (``python3 -I <tool> [--root R] <args>``)."""
        argv = [sys.executable, "-I", TOOL_PATH]
        if self.paths.root != Path("/"):
            argv += ["--root", str(self.paths.root)]
        return argv + list(args)

    def lock_path(self, name: str) -> Path:
        self.store  # noqa: B018 - creates the state dir
        return self.paths.state / name

    # -- clients

    def reader(self, config: Dict[str, Any], token: str) -> gh.GitHubReader:
        repos = [config["control_repository"]] + [t["repository"] for t in config["targets"]]
        return gh.GitHubReader(token, repos, transport=self.deps.github_transport)

    def writer(self, config: Dict[str, Any], token: str) -> gh.GitHubLedgerWriter:
        return gh.GitHubLedgerWriter(token, config["control_repository"], core.active_ledger_issue(config),
                                     transport=self.deps.github_transport)

    def slack(self, config: Dict[str, Any], token: str) -> pub.SlackClient:
        return pub.SlackClient(token, config["slack"]["team_id"], config["slack"]["bot_user_id"],
                               transport=self.deps.slack_transport, live_values=self.live)

    def host(self, config: Dict[str, Any]) -> hostmod.HostReader:
        return hostmod.HostReader(config["host_helper"], config["ledger_account"], runner=self.deps.host_runner)


# ---------------------------------------------------------------------------- small helpers

def _ensure_dir(path: Path, mode: int) -> None:
    """Create ``path`` (and parents) and give the final directory ``mode``; refuse a symlink."""
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    info = os.lstat(str(path))
    if not stat.S_ISDIR(info.st_mode):
        raise InspectError("STATE_DIR", "not a directory")
    if stat.S_IMODE(info.st_mode) != mode:
        os.chmod(str(path), mode)


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            return core.parse_iso(value)
        except InspectError:
            return None
    return None


def _read_small(path: Path, limit: int) -> Optional[bytes]:
    """Read a small regular file without following a symlink; None when absent."""
    # O_NONBLOCK: a FIFO planted by a less trusted writer must not hang the tick.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise InspectError("STATE_CORRUPT", path.name)
        return os.read(fd, limit + 1)[:limit]
    finally:
        os.close(fd)


def _safe_read(store: core.StateStore, name: str, default: Any) -> Any:
    """A state file, or ``default`` when it is missing or corrupt (display-only reads)."""
    try:
        return store.read(name, default)
    except InspectError:
        return default


def _write_text(path: Path, text: str, mode: int = 0o600) -> None:
    core._atomic_write(path, text.encode("utf-8"), mode)  # shared atomic writer (tmp, fsync, replace)


# ---------------------------------------------------------------------------- locks

class Lock:
    """A non-blocking exclusive flock on one state file (released when the fd closes)."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(self.path), flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def lock_held(path: Path) -> bool:
    """True when another open file description holds the lock."""
    probe = Lock(path)
    if probe.acquire():
        probe.release()
        return False
    return True


def with_tick_lock(ctx: Ctx, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    lock = Lock(ctx.lock_path("tick.lock"))
    if not lock.acquire():
        return {"status": "BUSY"}
    try:
        return fn()
    finally:
        lock.release()


# ---------------------------------------------------------------------------- errors and kill switch

def record_error(ctx: Ctx, reason: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Append to errors.jsonl; 3 errors within 24 h (since the last resume) engage the kill switch."""
    store = ctx.store
    store.append_jsonl("errors", {"at": core.iso(now), "reason": reason, "run": ctx.run_id})
    count = 0
    for row in store.read_jsonl("errors"):
        if not isinstance(row, dict):
            continue
        if "resume" in row:
            count = 0  # a resume starts a new count
            continue
        at = _time(row.get("at"))
        if at is not None and now - at <= ERROR_WINDOW:
            count += 1
    if count >= ERROR_LIMIT:
        return core.write_kill(ctx.paths, ERROR_KILL_REASON, ctx.run_id, now)
    return None


def engage_kill(ctx: Ctx, exc: InspectError, now: datetime) -> Optional[Dict[str, Any]]:
    if exc.reason in KILL_REASONS:
        return core.write_kill(ctx.paths, exc.reason, ctx.run_id, now)
    return None


def prune_errors(ctx: Ctx, now: datetime) -> None:
    rows = ctx.store.read_jsonl("errors")
    keep = [r for r in rows if isinstance(r, dict) and (_time(r.get("at")) or now) > now - RUNS_KEEP]
    if len(keep) != len(rows):
        ctx.store.write_jsonl("errors", keep)


# ---------------------------------------------------------------------------- manifest and tool digest

def tool_digest(ctx: Ctx) -> str:
    """sha256 identifying the running tool: the verified manifest when installed, else the module files."""
    if ctx.check_manifest:
        return core.verify_manifest(ctx.paths, core.own_files(ctx.paths),
                                    require_root_owner=ctx.installed)["tool_sha256"]
    here = Path(_LIB)
    files = {}
    for name in core.INSPECTOR_MODULES + core.CENTRAL_MODULES:
        path = here / name
        try:
            files[name] = core.file_sha256(path)
        except OSError:
            files[name] = None
    return core.sha256_hex(core.canon(files))


# ---------------------------------------------------------------------------- children (daemon bookkeeping)

def read_children(ctx: Ctx) -> Dict[str, Any]:
    try:
        data = ctx.store.read("children", {})
    except InspectError:
        data = {}
    data = _dict(data)
    return {"tick": data.get("tick") if isinstance(data.get("tick"), dict) else None,
            "render": [r for r in _list(data.get("render")) if isinstance(r, dict) and _int(r.get("pgid"))]}


def _proc_stat(ctx: Ctx, pid: int) -> Optional[List[str]]:
    """``/proc/<pid>/stat`` fields after the command name (index 2 = pgrp, 19 = start time)."""
    try:
        raw = (Path(ctx.deps.proc_root) / str(pid) / "stat").read_bytes().decode("utf-8", "replace")
        return raw[raw.rindex(")") + 2:].split()
    except (OSError, ValueError):
        return None


def _proc_start(ctx: Ctx, pid: int) -> Optional[str]:
    fields = _proc_stat(ctx, pid)
    return fields[19] if fields is not None and len(fields) > 19 and fields[19].isdigit() else None


def note_render_child(ctx: Ctx, pgid: Optional[int], uid: Optional[int] = None,
                      start: Optional[str] = None) -> None:
    """The tick records (or clears) its render child's process group, uid and start time so the daemon
    can stop exactly that process instance.

    Best effort: the tick itself always waits for (and on timeout kills) its own render child.
    """
    try:
        data = read_children(ctx)
        data["render"] = [] if pgid is None else [{"pgid": pgid, "uid": uid, "start": start, "run": ctx.run_id,
                                                   "at": core.iso(ctx.now())}]
        ctx.store.write("children", data)
    except (InspectError, OSError):
        pass


def group_is_ours(ctx: Ctx, pgid: Any, marker: str, uid: Any, start: Any) -> bool:
    """True only for the recorded process instance: a live process-group leader started at ``start``
    (``/proc/<pid>/stat`` field 22), running as ``uid`` (real and effective, never root), whose command
    line holds ``marker``. A recycled pid fails the start-time or uid check and is never signalled."""
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpid():
        return False
    if _int(uid) is None or uid <= 0 or not isinstance(start, str) or not start.isdigit():
        return False
    base = Path(ctx.deps.proc_root) / str(pgid)
    fields = _proc_stat(ctx, pgid)
    try:
        status = (base / "status").read_text("utf-8", "replace")
        cmdline = (base / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    if fields is None or len(fields) < 20 or fields[2] != str(pgid) or fields[19] != start:
        return False
    uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    uids = uid_line.split()[1:]
    if len(uids) < 2 or uids[0] != str(uid) or uids[1] != str(uid):
        return False
    return any(marker.encode() in part for part in cmdline)


# ---------------------------------------------------------------------------- render (as the render account)

def _render_limits() -> None:  # pragma: no cover - runs in the render child
    import resource
    for name, value in RENDER_LIMITS:
        limit = getattr(resource, name, None)
        if limit is not None:
            resource.setrlimit(limit, (value, value))


def launch_render(ctx: Ctx, data_path: Path, out_dir: Path, account: str,
                  font: Optional[str] = None) -> Dict[str, Any]:
    """Run the charts command mode as the render account: own session, rlimits, 120 s wall then killpg."""
    try:
        entry = ctx.deps.getpwnam(account)
    except KeyError:
        return {"status": "ERROR", "reason": "RENDER_ACCOUNT"}
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        return {"status": "ERROR", "reason": "RENDER_ACCOUNT"}
    argv = [sys.executable, "-I", CHARTS_PATH, "render", "--data", str(data_path), "--out", str(out_dir)]
    if font:
        argv += ["--font", font]
    out_file = out_dir.parent / f".{out_dir.name}.stdout"
    flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(out_file), flags, 0o600)
    try:
        proc = subprocess.Popen(argv, cwd=str(out_dir), env=dict(CLEAN_ENV), stdin=subprocess.DEVNULL,
                                stdout=fd, stderr=subprocess.DEVNULL, user=entry.pw_uid, group=entry.pw_gid,
                                extra_groups=[], umask=0o077, start_new_session=True, close_fds=True,
                                preexec_fn=_render_limits)
        # Popen returns after the exec: the child already runs as the render account.
        note_render_child(ctx, proc.pid, entry.pw_uid, _proc_start(ctx, proc.pid))
        try:
            try:
                proc.wait(timeout=RENDER_WALL_SECONDS)
            except subprocess.TimeoutExpired:
                # Our own unreaped child leads its own group: the pgid cannot belong to anyone else.
                try:
                    ctx.deps.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return {"status": "ERROR", "reason": "RENDER_TIMEOUT"}
        finally:
            note_render_child(ctx, None)
        size = os.fstat(fd).st_size
        os.lseek(fd, max(0, size - RENDER_OUTPUT_CAP), os.SEEK_SET)
        tail = os.read(fd, RENDER_OUTPUT_CAP)
    finally:
        os.close(fd)
        try:
            os.unlink(str(out_file))
        except OSError:
            pass
    lines = [line for line in tail.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        result = core.loads_strict(lines[-1]) if lines else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        return {"status": "ERROR", "reason": "RENDER_OUTPUT"}
    return result


def empty_chart_doc(now: datetime) -> Dict[str, Any]:
    """A valid AIOPS_INSPECT_CHARTS_V1 document with no products (preflight render check)."""
    return {"schema": charts.SCHEMA, "generated_at": core.iso(now), "snapshot": "0" * 12, "stage": "DRY",
            "scorecard": {"products": [], "signals": list(charts.SIGNALS), "cells": {}, "verdicts": {}},
            "ladder": {}, "dag": {}, "burnup": {},
            "lanes": {"window_start": core.iso(now - timedelta(days=7)), "window_end": core.iso(now),
                      "order": list(charts.LANE_ORDER), "enabled": {}, "intervals": []}}


def _render_dirs(ctx: Ctx, config: Dict[str, Any], run_id: str) -> Path:
    """``<render root>/<run>``: owned by the render account (installed), mode 0700.

    The render root is root:<render group> 0750 (docs/INSPECTOR.md §2.2): the render account may
    enter it but cannot create, rename or remove entries there.
    """
    _ensure_dir(ctx.paths.render, 0o750 if ctx.installed else 0o700)
    entry = ctx.deps.getpwnam(config["render_account"]) if ctx.installed else None
    if entry is not None and os.lstat(str(ctx.paths.render)).st_gid != entry.pw_gid:
        os.chown(str(ctx.paths.render), 0, entry.pw_gid)
    out_dir = ctx.paths.render / run_id
    out_dir.mkdir(mode=0o700)
    if entry is not None:
        os.chown(str(out_dir), entry.pw_uid, entry.pw_gid)
    return out_dir


def _run_render(ctx: Ctx, data: Path, out: Path, account: str, font: Optional[str] = None) -> Dict[str, Any]:
    render = ctx.deps.render or launch_render
    return render(ctx, data, out, account, font)


def render_charts(ctx: Ctx, config: Dict[str, Any], run_id: str,
                  doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Path], str]:
    """Render the chart document; returns (pngs, dir holding root-owned copies, status). Never raises
    for render problems: charts are optional and the card goes text-only."""
    try:
        out_dir = _render_dirs(ctx, config, run_id)
        data_path = out_dir / "charts.json"
        _write_text(data_path, core.canon(doc).decode("utf-8"), 0o644)
        result = _run_render(ctx, data_path, out_dir, config["render_account"])
    except Exception as exc:  # noqa: BLE001 - charts are optional; any launch failure means text-only
        reason = exc.reason if isinstance(exc, InspectError) else "RENDER_LAUNCH"
        if reason == "SECRET_LIVE":
            raise
        return [], None, reason
    if not isinstance(result, dict):
        return [], None, "RENDER_OUTPUT"
    status = result.get("status") if isinstance(result.get("status"), str) else "RENDER"
    if status != "OK":
        code = status if status != "ERROR" else result.get("reason")
        return [], None, code if isinstance(code, str) and core.REASON_RE.fullmatch(code) else "RENDER"
    # The render account is less trusted: re-read every PNG without following links, validate it,
    # and keep a root-owned copy in the run dir; only those copies are published.
    keep_dir = ctx.paths.runs / run_id / "charts"
    _ensure_dir(ctx.paths.runs, 0o700)
    _ensure_dir(keep_dir, 0o700)
    pngs = []
    for item in _list(result.get("pngs"))[:pub.MAX_UPLOAD_FILES]:
        item = _dict(item)
        name = item.get("name")
        if not isinstance(name, str) or not pub.PNG_NAME_RE.fullmatch(name):
            return [], None, "PNG_INVALID"
        try:
            data = _read_small(out_dir / name, charts.MAX_PNG_BYTES)
            if data is None:
                return [], None, "PNG_MISSING"
            width, height = charts.validate_png(data)
        except (InspectError, OSError, ValueError):
            return [], None, "PNG_INVALID"
        digest = core.sha256_hex(data)
        if item.get("sha256") != digest:
            return [], None, "PNG_INVALID"
        core._atomic_write(keep_dir / name, data, 0o600)
        pngs.append({"name": name, "sha256": digest, "w": width, "h": height})
    return pngs, keep_dir, "OK"


# ---------------------------------------------------------------------------- tick

def _journal_views(publisher: pub.Publisher) -> Tuple[List[int], Dict[str, str]]:
    """Ledger comment ids the inspector posted (for S0 reads) and {comment id: body sha256}."""
    ids: List[int] = []
    journal: Dict[str, str] = {}
    for run in publisher.journal_runs():
        entry = publisher.load(run)
        comment = _dict(_dict(entry).get("comment"))
        cid = _int(comment.get("id"))
        if comment.get("state") == pub.POSTED and cid and isinstance(comment.get("sha256"), str):
            ids.append(cid)
            journal[str(cid)] = comment["sha256"]
    return ids, journal


def _comment_state(publisher: pub.Publisher, run: Any) -> Optional[str]:
    """The ledger comment state of one journal, or None when it is missing or unreadable."""
    if not isinstance(run, str) or not core.RUN_ID_RE.fullmatch(run):
        return None
    entry = publisher.load(run)
    return _dict(entry.get("comment")).get("state") if entry is not None else None


def _tokens(ctx: Ctx, reader: Optional[gh.GitHubReader], writer: Optional[gh.GitHubLedgerWriter]) -> Dict[str, Any]:
    """Last known token expiry per kind (persisted so status can show it without network)."""
    known = _dict(_safe_read(ctx.store, "tokens", {}))
    for kind, client in (("gh-read", reader), ("gh-ledger", writer)):
        expiry = getattr(client, "token_expiry", None) if client is not None else None
        if isinstance(expiry, datetime):
            known[kind] = core.iso(expiry)
    try:
        ctx.store.write("tokens", known)
    except (InspectError, OSError):
        pass  # display only
    return known


def _last_tick(ctx: Ctx) -> Optional[datetime]:
    return _time(_dict(_safe_read(ctx.store, "tick", {})).get("at"))


def halted_tick(ctx: Ctx, config: Dict[str, Any], kill: Dict[str, Any], now: datetime,
                out: Dict[str, Any]) -> Dict[str, Any]:
    """Kill switch present: only the heartbeat (HALTED) and the daily line."""
    out.update(status="HALTED", reason=kill.get("reason"))
    try:
        slack = ctx.slack(config, ctx.secret("slack"))
        publisher = pub.Publisher(ctx.store, config, slack=slack, live_values=ctx.live)
        last = _last_tick(ctx)
        out["heartbeat"] = publisher.heartbeat(now, status="HALTED", reason=kill.get("reason"),
                                               last_tick=last or now, halted_at=_time(kill.get("at")),
                                               token_expiry=_dict(_safe_read(ctx.store, "tokens", {})))["heartbeat"]
        publisher.count_tick(False)
        out["daily"] = publisher.daily(now)["daily"]
    except InspectError as exc:
        out["slack"] = exc.reason
    return out


class Tick:
    """One tick (contract §3.9 steps 1-7). Gate 0 failures raise; later failures are recorded,
    the heartbeat, daily line and dead-man still run, and the error is raised at the end."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.store = ctx.store
        self.now = ctx.now()
        self.run_id = core.new_run_id(self.now, "tick")
        ctx.run_id = self.run_id
        self.out: Dict[str, Any] = {"status": "OK", "run": self.run_id, "t1": "SKIPPED", "published": False}
        self.reader: Optional[gh.GitHubReader] = None
        self.writer: Optional[gh.GitHubLedgerWriter] = None
        self.publisher: Optional[pub.Publisher] = None
        # True between commit_t1 (recheck clean) and the findings.json write of the same snapshot.
        self.unsaved_t1 = False

    def redirty_t1(self) -> None:
        """T1 was committed clean but its findings were never saved: collect again next tick, so that a
        newer journal supersedes any journal prepared here and the changes are not held back until the
        recheck time (or posted twice once stale findings are recomputed). Best effort."""
        try:
            recheck = _dict(self.store.read(facts.ST_RECHECK, {}))
            self.store.write(facts.ST_RECHECK, dict(recheck, t1_dirty=True))
        except (InspectError, OSError):
            pass

    def run(self) -> Dict[str, Any]:
        ctx, now = self.ctx, self.now
        # 1. Gate 0 (no network)
        config = ctx.config()
        kill = core.kill_state(ctx.paths)
        if kill is not None:
            return halted_tick(ctx, config, kill, now, self.out)
        try:
            tool_sha = tool_digest(ctx)
        except InspectError as exc:
            engage_kill(ctx, exc, now)
            raise
        secrets = ctx.secrets()
        self.reader = ctx.reader(config, secrets["gh-read"])
        self.writer = ctx.writer(config, secrets["gh-ledger"])
        host = ctx.host(config)
        self.publisher = pub.Publisher(self.store, config, writer=self.writer,
                                       slack=ctx.slack(config, secrets["slack"]), live_values=ctx.live)
        problem: Optional[InspectError] = None
        try:
            self.steps(config, host, tool_sha)
        except InspectError as exc:
            problem = exc
            engage_kill(ctx, exc, now)
        except Exception as exc:  # noqa: BLE001 - the heartbeat and dead-man still run
            problem = InspectError("INTERNAL", type(exc).__name__)
        if problem is not None and self.unsaved_t1:
            self.redirty_t1()
        self.finish(config, problem)
        if problem is not None:
            raise problem
        return self.out

    # -- steps 2-6

    def steps(self, config: Dict[str, Any], host: hostmod.HostReader, tool_sha: str) -> None:
        ctx, now, store, out = self.ctx, self.now, self.store, self.out
        publisher = self.publisher
        assert publisher is not None and self.reader is not None
        # CONTRACT NOTE: auth.test (identity + exact scopes) runs before ANY write of the tick, so a
        # SLACK_SCOPE breach halts before the ledger is touched; an unreachable Slack only defers Slack.
        publisher.slack_ready()
        # 2. adopt or abandon earlier UNKNOWN ledger posts
        rec = publisher.reconcile_unknown(now)
        out["reconciled"] = {"adopted": len(rec["adopted"]), "abandoned": len(rec["abandoned"]),
                             "pending": rec["pending"]}
        # 3. T0 probe and the early-exit rule
        try:
            probe = facts.probe(self.reader, host, config, store, now, ctx.live)
        except InspectError as exc:
            if exc.reason == "SECRET_LIVE":
                raise
            facts.fail_t1(store, now, exc.reason)
            out.update(t1="FAILED", t1_reason=exc.reason)
            publisher.advance(now)
            return
        run_t1, why = facts.should_run_t1(probe, store, now)
        out["t1_reason"] = why
        if not run_t1:
            facts.commit_unchanged(store)
            publisher.advance(now)  # retries REFUSED steps of the newest journal, if any
            return
        # 4. T1 (time, host and GitHub budgets abort it; previous facts stay)
        ledger_ids, journal = _journal_views(publisher)
        budgets = facts.Budgets(seconds=T1_SECONDS, clock=ctx.deps.monotonic)
        try:
            doc = facts.collect(self.reader, host, config, store, now, budgets, probe_result=probe,
                                ledger_ids=ledger_ids, live_values=ctx.live)
            self.unsaved_t1 = True  # commit_t1 marks recheck dirty first, so a failure inside it is covered too
            committed = facts.commit_t1(store, doc, now, config)
        except InspectError as exc:
            if exc.reason == "SECRET_LIVE":
                raise
            facts.fail_t1(store, now, exc.reason)
            out.update(t1="FAILED", t1_reason=exc.reason)
            publisher.advance(now)
            return
        out["t1"] = "DONE"
        snapshot = committed["hashes"]["snapshot"]
        out["snapshot"] = snapshot
        # 5. signals, findings lifecycle, chart data
        result = sig.run(doc, config, now, state=store.read("findings", None), snapshot=snapshot,
                         journal=journal, history=store.read_jsonl("history"))
        # CONTRACT NOTE: findings.json moves on when a journal is prepared, so the changes of the journal it
        # records (`unannounced`) are carried into this one when that journal's ledger comment was never sent:
        # FAILED or SUPERSEDED (a FAILED comment alone makes this T1 publish), or PENDING/REFUSED when this
        # T1 publishes anyway (the new journal supersedes it). POSTED, UNKNOWN and ABANDONED are not carried.
        carry = result["state"].pop("unannounced", None)
        if isinstance(carry, dict):
            sent = _comment_state(publisher, carry.get("run"))
            if sent in (pub.FAILED, pub.SUPERSEDED) or (
                    sent in pub.RETRYABLE and pub.publish_needed(result, publisher.first_run())):
                out["carried"] = sig.carry_unannounced(result, carry)
        out["changes"] = len(result["changes"])
        # 6. render and publish through the journal
        if not pub.publish_needed(result, publisher.first_run()):
            if carry is not None:
                result["state"]["unannounced"] = carry   # the newest journal may still post, or fail
            store.write("findings", result["state"])
            self.unsaved_t1 = False
            publisher.advance(now)
            return
        pngs, render_dir, render_status = render_charts(ctx, config, self.run_id, result["charts"])
        out["render"] = render_status
        report = pub.build_report(run=self.run_id, now=now, stage=config["stage"], snapshot=snapshot,
                                  tool_sha256=tool_sha, facts=doc, facts_sha256=core.sha256_hex(core.canon(doc)),
                                  result=result, control_repository=config["control_repository"],
                                  ledger_issue=core.active_ledger_issue(config), pngs=pngs,
                                  render_dir=render_dir, render_status=render_status)
        # CONTRACT NOTE: the journal is prepared BEFORE findings.json is written. A failure in between (or
        # anywhere after commit_t1) re-dirties recheck (redirty_t1), so the next tick runs T1 again, recomputes
        # the same changes from the unchanged findings.json and its newer journal supersedes this one (no
        # double post). A hard crash (SIGKILL) there is not covered: the next T1 comes with the next change
        # or the recheck time.
        publisher.prepare(report, now)
        result["state"]["unannounced"] = sig.unannounced_record(self.run_id, result)
        store.write("findings", result["state"])
        self.unsaved_t1 = False
        # CONTRACT NOTE: the 3-minute publish budget is bounded by the per-request timeouts (GitHub 30 s,
        # Slack 20 s, upload 60 s) and the daemon's 45-minute cap; it is not a separate timer.
        adv = publisher.advance(now)
        out.update(published=True, journal=adv.get("state"))

    # -- step 7

    def finish(self, config: Dict[str, Any], problem: Optional[InspectError]) -> None:
        ctx, now, store, out = self.ctx, self.now, self.store, self.out
        publisher = self.publisher
        assert publisher is not None
        status, reason, halted_at = "OK", None, None
        recheck = _dict(_safe_read(store, "recheck", {}))
        failures = _int(recheck.get("t1_failures")) or 0
        if failures >= DEGRADED_AFTER:
            last = str(recheck.get("last_error") or "")
            status, reason = "DEGRADED", "HOST" if last.startswith("HOST_") else "GITHUB_READ"
        overrun = _safe_read(store, "overrun", None)
        if overrun:
            status, reason = "DEGRADED", "OVERRUN"
        if problem is not None:
            kill = core.kill_state(ctx.paths)
            if kill is not None:
                status, reason, halted_at = "HALTED", kill.get("reason"), _time(kill.get("at"))
            else:
                status, reason = "DEGRADED", problem.reason
        tokens = _tokens(ctx, self.reader, self.writer)
        slack: Dict[str, Any] = {}

        def guarded(name: str, call: Callable[[], Dict[str, Any]], key: str) -> None:
            try:
                slack[name] = _dict(call()).get(key)
            except InspectError as exc:
                slack[name] = exc.reason
                if exc.reason in KILL_REASONS:
                    engage_kill(ctx, exc, now)

        guarded("heartbeat", lambda: publisher.heartbeat(
            now, status=status, reason=reason, last_tick=now,
            next_tick=pub.next_tick_at(now, config["tick_minute"]), token_expiry=tokens,
            halted_at=halted_at), "heartbeat")
        try:
            publisher.count_tick(problem is None and out["t1"] != "FAILED")
        except InspectError as exc:
            slack["count"] = exc.reason
        guarded("daily", lambda: publisher.daily(now), "daily")
        guarded("deadman", lambda: publisher.deadman(now, last_tick=now), "scheduled")
        out["slack"] = slack
        out["heartbeat"] = status if reason is None else f"{status}({reason})"
        if overrun and slack.get("heartbeat") in ("UPDATED", "CREATED"):
            store.remove("overrun")
        store.write("tick", {"run": self.run_id, "at": core.iso(now), "t1": out["t1"],
                             "published": out["published"], "heartbeat": out["heartbeat"],
                             "error": problem.reason if problem is not None else None})
        try:
            publisher.prune(now)
            prune_run_dirs(ctx.paths.runs, now)
            prune_run_dirs(ctx.paths.render, now)
            prune_errors(ctx, now)
        except (InspectError, OSError):
            pass


def prune_run_dirs(directory: Path, now: datetime) -> int:
    """Remove ``<run id>`` directories older than 30 days (never follows a link)."""
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.iterdir():
        if not core.RUN_ID_RE.fullmatch(path.name):
            continue
        try:
            started = datetime.strptime(path.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        info = os.lstat(str(path))
        if now - started > RUNS_KEEP and stat.S_ISDIR(info.st_mode):
            shutil.rmtree(str(path), ignore_errors=True)
            removed += 1
    return removed


def cmd_tick(ctx: Ctx) -> Dict[str, Any]:
    def body() -> Dict[str, Any]:
        tick = Tick(ctx)
        try:
            return tick.run()
        except BaseException as exc:
            reason = exc.reason if isinstance(exc, InspectError) else "INTERNAL"
            try:
                record_error(ctx, reason, tick.now)
            except (InspectError, OSError):
                pass
            raise
    return with_tick_lock(ctx, body)


# ---------------------------------------------------------------------------- daemon, start, stop

class Daemon:
    """The root loop: one ``tick`` child per hour at ``tick_minute`` KST, 45-minute cap, no sleeping in
    tests (clock, sleep and spawn are injected)."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.stopping = False
        self.proc: Any = None
        self.ticks = 0

    def tick_minute(self) -> int:
        try:
            return int(self.ctx.config()["tick_minute"])
        except InspectError:
            return DEFAULT_TICK_MINUTE

    def next_tick(self) -> datetime:
        return pub.next_tick_at(self.ctx.now(), self.tick_minute())

    def wait_until(self, target: datetime) -> bool:
        while not self.stopping:
            left = (target - self.ctx.now()).total_seconds()
            if left <= 0:
                return True
            # Short chunks: a SIGTERM (flag) is noticed within seconds, well inside stop's 30 s wait.
            self.ctx.deps.sleep(min(WAIT_CHUNK_SECONDS, left))
        return False

    def spawn(self) -> Any:
        argv = self.ctx.tool_argv("tick")
        if self.ctx.deps.spawn_tick is not None:
            return self.ctx.deps.spawn_tick(argv)
        return subprocess.Popen(argv, cwd="/", env=dict(CLEAN_ENV), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True)

    def kill_groups(self) -> List[int]:
        """killpg the tick child's group (our unreaped child) and the render group the tick recorded."""
        killed = []
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                self.ctx.deps.killpg(proc.pid, signal.SIGKILL)
                killed.append(proc.pid)
            except ProcessLookupError:
                pass
        for entry in read_children(self.ctx)["render"]:
            pgid = entry.get("pgid")
            # A render group outlives a killed tick; signal it only while it is still the recorded charts
            # process (same uid and start time), never a recycled pid.
            if group_is_ours(self.ctx, pgid, "control_plane_inspect_charts", entry.get("uid"), entry.get("start")):
                try:
                    self.ctx.deps.killpg(pgid, signal.SIGKILL)
                    killed.append(pgid)
                except ProcessLookupError:
                    pass
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        return killed

    def run_tick(self) -> Dict[str, Any]:
        """Spawn one tick child, supervise it and handle overrun. A tick failure never ends the loop."""
        ctx = self.ctx
        started = ctx.now()
        self.proc = self.spawn()
        try:
            # Display and bookkeeping only: the child is supervised (45-minute cap) even when this fails.
            ctx.store.write("children", {"tick": {"pgid": self.proc.pid, "at": core.iso(started)}, "render": []})
        except (InspectError, OSError):
            pass
        begin = ctx.deps.monotonic()
        outcome = "DONE"
        while True:
            code = self.proc.poll()
            if code is not None:
                break
            if self.stopping:
                self.kill_groups()
                outcome = "STOPPED"
                break
            left = TICK_CAP_SECONDS - (ctx.deps.monotonic() - begin)
            if left <= 0:
                self.kill_groups()
                outcome = "OVERRUN"
                break
            ctx.deps.sleep(min(WAIT_CHUNK_SECONDS, left))
        code = self.proc.poll()
        now = ctx.now()
        try:
            if outcome == "OVERRUN":
                ctx.store.write("overrun", {"at": core.iso(now), "started": core.iso(started)})
                record_error(ctx, "OVERRUN", now)
            elif outcome == "DONE" and code not in (0, 1):
                record_error(ctx, "TICK_CRASH", now)
            ctx.store.write("children", {"tick": None, "render": []})
        except (InspectError, OSError):
            pass
        self.proc = None
        self.ticks += 1
        return {"outcome": outcome, "code": code}

    def loop(self, max_ticks: Optional[int] = None) -> None:
        while not self.stopping and (max_ticks is None or self.ticks < max_ticks):
            if not self.wait_until(self.next_tick()):
                break
            try:
                self.run_tick()
            except Exception:  # noqa: BLE001 - e.g. fork EAGAIN, disk full: never ends the loop
                self.recover()

    def recover(self) -> None:
        """After an unexpected error in one iteration: stop a tick child that would otherwise run on
        unsupervised, record the error (best effort) and wait for the next tick minute."""
        try:
            if self.proc is not None:
                self.kill_groups()
        except Exception:  # noqa: BLE001 - best effort
            pass
        self.proc = None
        try:
            record_error(self.ctx, "DAEMON_LOOP", self.ctx.now())
        except Exception:  # noqa: BLE001 - best effort
            pass


def read_pid(ctx: Ctx) -> Optional[int]:
    try:
        raw = _read_small(ctx.paths.state / "daemon.pid", 64)
    except (InspectError, OSError):
        return None
    if raw is None:
        return None
    text = raw.decode("ascii", "replace").strip()
    return int(text) if re.fullmatch(r"[1-9][0-9]{0,9}", text) else None


def cmd_daemon(ctx: Ctx) -> Dict[str, Any]:
    ctx.config()  # refuse to start on an invalid config
    lock = Lock(ctx.lock_path("daemon.lock"))
    for attempt in range(DAEMON_LOCK_TRIES):
        if lock.acquire():
            break
        if attempt + 1 == DAEMON_LOCK_TRIES:
            return {"status": "ALREADY_RUNNING", "pid": read_pid(ctx)}
        ctx.deps.sleep(DAEMON_LOCK_RETRY_SECONDS)
    daemon = Daemon(ctx)
    try:
        _write_text(ctx.paths.state / "daemon.pid", f"{os.getpid()}\n")
        if ctx.deps.install_signals:
            def on_term(signum: int, frame: Any) -> None:
                daemon.stopping = True
            signal.signal(signal.SIGTERM, on_term)
            signal.signal(signal.SIGINT, on_term)
        daemon.loop()
        if daemon.proc is not None:
            daemon.kill_groups()
    finally:
        try:
            os.unlink(str(ctx.paths.state / "daemon.pid"))
        except OSError:
            pass
        lock.release()
    return {"status": "STOPPED", "ticks": daemon.ticks}


def spawn_daemon(argv: List[str]) -> int:  # pragma: no cover - forks; tests inject a fake
    """Double fork + setsid + stdio to /dev/null, then exec the daemon. Returns the daemon pid."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            os.setsid()
            grandchild = os.fork()
            if grandchild > 0:
                os.write(write_end, str(grandchild).encode())
                os._exit(0)
            os.close(write_end)
            os.chdir("/")
            os.umask(0o077)
            null = os.open(os.devnull, os.O_RDWR)
            for target in (0, 1, 2):
                os.dup2(null, target)
            os.execve(argv[0], argv, dict(CLEAN_ENV))
        finally:
            os._exit(127)
    os.close(write_end)
    try:
        data = os.read(read_end, 32)
    finally:
        os.close(read_end)
        os.waitpid(pid, 0)
    text = data.decode("ascii", "replace")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", text):
        raise InspectError("START_FAILED", "fork failed")
    return int(text)


def cmd_start(ctx: Ctx) -> Dict[str, Any]:
    ctx.config()  # fail fast, no network
    path = ctx.lock_path("daemon.lock")
    if lock_held(path):
        return {"status": "ALREADY_RUNNING", "pid": read_pid(ctx)}
    # CONTRACT NOTE: start checks daemon.lock and the daemon itself takes it for its lifetime; a second
    # concurrent start loses the lock in its daemon, which then exits with ALREADY_RUNNING.
    spawn = ctx.deps.spawn_daemon or spawn_daemon
    pid = spawn(ctx.tool_argv("daemon"))
    deadline = ctx.deps.monotonic() + START_WAIT_SECONDS
    while True:
        if lock_held(path) and read_pid(ctx) == pid:
            return {"status": "STARTED", "pid": pid}
        if ctx.deps.monotonic() >= deadline:
            raise InspectError("START_FAILED", "the daemon did not take daemon.lock")
        ctx.deps.sleep(0.2)


def _proc_owned_daemon(ctx: Ctx, pid: int) -> bool:
    """/proc/<pid>/status Uid 0 (real and effective) and a cmdline naming aiops-inspect and daemon."""
    base = Path(ctx.deps.proc_root) / str(pid)
    try:
        status = (base / "status").read_text("utf-8", "replace")
        cmdline = [p.decode("utf-8", "replace") for p in (base / "cmdline").read_bytes().split(b"\0") if p]
    except OSError:
        return False
    uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    uids = uid_line.split()[1:]
    if len(uids) < 2 or uids[0] != "0" or uids[1] != "0":
        return False
    # CONTRACT NOTE: only the installed name counts (a repository checkout is never stopped this way).
    named = any(os.path.basename(p) == INSTALLED_NAME for p in cmdline)
    return named and "daemon" in cmdline


def _alive(ctx: Ctx, pid: int) -> bool:
    return (Path(ctx.deps.proc_root) / str(pid)).exists()


def _pause_slack(ctx: Ctx, now: datetime) -> Dict[str, Any]:
    """Heartbeat PAUSED and cancel the dead-man message (best effort)."""
    out: Dict[str, Any] = {}
    try:
        config = ctx.config()
        publisher = pub.Publisher(ctx.store, config, slack=ctx.slack(config, ctx.secret("slack")),
                                  live_values=ctx.live)
        out["heartbeat"] = publisher.heartbeat(now, status="PAUSED", last_tick=_last_tick(ctx) or now)["heartbeat"]
        cancelled = publisher.cancel_deadman(now)
        out["deadman_cancelled"] = cancelled.get("cancelled")
        out["deadman_pending"] = cancelled.get("pending_delete")
    except InspectError as exc:
        out["slack"] = exc.reason
    return out


def cmd_stop(ctx: Ctx) -> Dict[str, Any]:
    now = ctx.now()
    pid = read_pid(ctx)
    running = lock_held(ctx.lock_path("daemon.lock"))
    result: Dict[str, Any] = {"status": "NOT_RUNNING", "pid": pid}
    if running and pid is not None:
        if pid <= 1 or pid == os.getpid() or not _proc_owned_daemon(ctx, pid):
            raise InspectError("NOT_DAEMON", "daemon.pid does not name a root aiops-inspect daemon")
        ctx.deps.kill(pid, signal.SIGTERM)  # one known pid; never a group, never -1
        deadline = ctx.deps.monotonic() + STOP_WAIT_SECONDS
        while _alive(ctx, pid) and lock_held(ctx.lock_path("daemon.lock")):
            if ctx.deps.monotonic() >= deadline:
                raise InspectError("STOP_TIMEOUT", "the daemon did not exit within 30 s")
            ctx.deps.sleep(0.5)
        result["status"] = "STOPPED"
    elif running:
        raise InspectError("NOT_DAEMON", "daemon.lock is held but daemon.pid is missing")
    result.update(_pause_slack(ctx, now))
    return result


# ---------------------------------------------------------------------------- status, probe, preflight

def _findings_by_level(state: Any) -> Dict[str, int]:
    counts = {"WATCH": 0, "AT_RISK": 0, "unknown": 0}
    for item in _dict(_dict(state).get("findings")).values():
        if not isinstance(item, dict) or item.get("state") == "RESOLVED":
            continue
        if item.get("severity") in ("WATCH", "AT_RISK"):
            counts[item["severity"]] += 1
        if item.get("unknown"):
            counts["unknown"] += 1
    return counts


def cmd_status(ctx: Ctx) -> Dict[str, Any]:
    """Everything from local files; no network."""
    now = ctx.now()
    store = ctx.store
    out: Dict[str, Any] = {"status": "OK"}
    out["daemon"] = {"running": lock_held(ctx.lock_path("daemon.lock")), "pid": read_pid(ctx)}
    out["kill"] = core.kill_state(ctx.paths)

    def safe(name: str, fn: Callable[[], Any]) -> None:
        try:
            out[name] = fn()
        except InspectError as exc:
            out[name] = {"error": exc.reason}

    safe("last_tick", lambda: store.read("tick", None))
    safe("recheck", lambda: {k: v for k, v in _dict(store.read("recheck", {})).items()
                             if k in ("next_recheck_at", "t1_dirty", "last_t1", "t1_failures", "last_error")})
    safe("findings", lambda: _findings_by_level(store.read("findings", {})))
    safe("tokens", lambda: store.read("tokens", {}))
    try:
        config = ctx.config()
        out["config"] = "OK"
        publisher = pub.Publisher(store, config)
        out["unknown_posts"] = publisher.unknown_count(now)
        entry = _dict(_dict(store.read("heartbeat", {})).get(core.active_channel(config)))
        out["heartbeat"] = {k: entry.get(k) for k in ("status", "last", "updated_at", "error")}
    except InspectError as exc:
        out["config"] = exc.reason
    if ctx.check_manifest:
        try:
            tool_digest(ctx)
            out["manifest"] = "OK"
        except InspectError as exc:
            out["manifest"] = exc.reason
    else:
        out["manifest"] = "SKIPPED"
    return out


def cmd_probe(ctx: Ctx) -> Dict[str, Any]:
    """T0 + T1 into a scratch state under runs/<id>/probe-state; prints counts and hashes only."""
    now = ctx.now()
    config = ctx.config()
    kill = core.kill_state(ctx.paths)
    if kill is not None:
        # CONTRACT NOTE: the kill switch halts every networked command except preflight and stop.
        return {"status": "HALTED", "reason": kill.get("reason")}
    run_id = core.new_run_id(now, "probe")
    ctx.run_id = run_id
    _ensure_dir(ctx.paths.runs, 0o700)
    _ensure_dir(ctx.paths.runs / run_id, 0o700)
    scratch = core.StateStore(ctx.paths.runs / run_id / "probe-state")
    reader = ctx.reader(config, ctx.secret("gh-read"))
    host = ctx.host(config)
    probe = facts.probe(reader, host, config, scratch, now, ctx.live)
    doc = facts.collect(reader, host, config, scratch, now, facts.Budgets(seconds=T1_SECONDS,
                                                                          clock=ctx.deps.monotonic),
                        probe_result=probe, live_values=ctx.live)
    hashes = facts.hashes_of(doc, config)
    products = {}
    for repo, product in sorted(_dict(doc.get("products")).items()):
        plan = _dict(_dict(product).get("plan"))
        products[repo] = {"hash": hashes["products"].get(repo), "plan": plan.get("state"),
                          "nodes": len(_list(plan.get("nodes"))), "merged_7d": len(_list(product.get("merged_7d"))),
                          "unknown": [g for g in _list(product.get("unknown")) if isinstance(g, str)]}
    return {"status": "OK", "run": run_id, "snapshot": hashes["snapshot"], "changed_keys": len(probe["changed_keys"]),
            "github_calls": reader.calls, "host_calls": host.calls, "products": products,
            "unknown": [g for g in _list(doc.get("unknown")) if isinstance(g, str)]}


def _check(checks: List[Dict[str, Any]], name: str, required: bool, fn: Callable[[], Any]) -> Any:
    try:
        detail = fn()
        checks.append({"name": name, "ok": True, "required": required, "detail": detail})
        return detail
    except InspectError as exc:
        checks.append({"name": name, "ok": False, "required": required, "detail": exc.reason})
    except Exception as exc:  # noqa: BLE001 - one failed check never ends preflight; class name only
        checks.append({"name": name, "ok": False, "required": required, "detail": type(exc).__name__})
    return None


def _owner_mode(path: Path, mode: int, gid: Optional[int] = None, directory: bool = False) -> str:
    info = os.lstat(str(path))
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok:
        raise InspectError("PATH_KIND", path.name)
    if info.st_uid != 0:
        raise InspectError("PATH_OWNER", path.name)
    if stat.S_IMODE(info.st_mode) != mode:
        raise InspectError("PATH_MODE", path.name)
    if gid is not None and info.st_gid != gid:
        raise InspectError("PATH_GROUP", path.name)
    return "OK"


def cmd_preflight(ctx: Ctx) -> Dict[str, Any]:
    """Installation checks; no GitHub or Slack writes."""
    now = ctx.now()
    checks: List[Dict[str, Any]] = []
    config: Optional[Dict[str, Any]] = None
    if _check(checks, "config", True, lambda: ctx.config() and "OK"):
        config = ctx.config()
    _check(checks, "kill_switch", True, lambda: _no_kill(ctx))
    accounts: Dict[str, Any] = {}
    if config is not None:
        for key in ("ledger_account", "render_account"):
            accounts[key] = _check(checks, key, True, lambda k=key: _account(ctx, config[k]))
    if ctx.installed:
        paths = ctx.paths
        render_gid = _dict(accounts.get("render_account")).get("gid")
        for name, path, mode, kwargs in (
                ("path_config", paths.config, 0o600, {}),
                ("path_manifest", paths.manifest, 0o644, {}),
                ("path_state", paths.state, 0o700, {"directory": True}),
                ("path_runs", paths.runs, 0o700, {"directory": True}),
                ("path_lib", paths.lib, 0o755, {"directory": True}),
                ("path_tool", paths.tool_bin, 0o755, {}),
                ("path_render", paths.render, 0o750, {"directory": True, "gid": render_gid})):
            _check(checks, name, True, lambda p=path, m=mode, k=kwargs: _owner_mode(p, m, **k))
    if ctx.check_manifest:
        _check(checks, "manifest", True, lambda: tool_digest(ctx)[:12])
    for kind in core.SECRET_KINDS:
        _check(checks, f"secret_{kind}", True, lambda k=kind: ctx.secret(k) and "OK")
    if config is not None:
        if all(c["ok"] for c in checks if c["name"] == "secret_slack"):
            _check(checks, "slack_auth", True, lambda: ctx.slack(config, ctx.secret("slack")).verify()["scopes"])
        if all(c["ok"] for c in checks if c["name"] == "secret_gh-read"):
            reader = ctx.reader(config, ctx.secret("gh-read"))
            for repo in [config["control_repository"]] + [t["repository"] for t in config["targets"]]:
                branch = _check(checks, f"github_read:{repo}", True, lambda r=repo: _github_read(reader, r))
                if branch and repo != config["control_repository"]:
                    # Not required: without "Checks: read" only S5's post-merge checks show 확인 불가.
                    _check(checks, f"github_checks:{repo}", False,
                           lambda r=repo, b=branch: _github_checks(reader, r, b))
        if all(c["ok"] for c in checks if c["name"] == "secret_gh-ledger"):
            writer = ctx.writer(config, ctx.secret("gh-ledger"))
            _check(checks, "github_ledger_read", True,
                   lambda: f"{len(writer.comments_since(core.iso(now - timedelta(minutes=5))))} comments")
        _check(checks, "host_lanes", True, lambda: f"{len(ctx.host(config).lanes()['lanes'])} lanes")
        _check(checks, "render", False, lambda: _render_probe(ctx, config, now))
    ok = all(c["ok"] for c in checks if c["required"])
    return {"status": "PASS" if ok else "FAIL", "checks": checks}


def _no_kill(ctx: Ctx) -> str:
    kill = core.kill_state(ctx.paths)
    if kill is not None:
        raise InspectError("HALTED", str(kill.get("reason")))
    return "ABSENT"


def _account(ctx: Ctx, name: str) -> Dict[str, Any]:
    entry = ctx.deps.getpwnam(name)
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        raise InspectError("ACCOUNT_ROOT", name)
    if entry.pw_shell not in NOLOGIN_SHELLS:
        raise InspectError("ACCOUNT_SHELL", name)
    return {"uid": entry.pw_uid, "gid": entry.pw_gid}


def _github_read(reader: gh.GitHubReader, repo: str) -> str:
    """GET /repos/{r}; returns the default branch."""
    resp = reader.get(f"/repos/{repo}")
    if resp.status != 200:
        raise InspectError("GITHUB_READ", f"status {resp.status}")
    branch = _dict(resp.json).get("default_branch")
    if not isinstance(branch, str) or not facts.BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise InspectError("GITHUB_JSON", "default_branch missing")
    return branch


def _github_checks(reader: gh.GitHubReader, repo: str, branch: str) -> str:
    resp = reader.get(f"/repos/{repo}/commits/{branch}/check-runs", {"per_page": 1})
    if resp.status != 200:
        raise InspectError("GITHUB_READ", f"status {resp.status}")
    return "OK"


def _render_probe(ctx: Ctx, config: Dict[str, Any], now: datetime) -> str:
    run_id = core.new_run_id(now, "preflight")
    out_dir = _render_dirs(ctx, config, run_id)
    try:
        data_path = out_dir / "charts.json"
        _write_text(data_path, core.canon(empty_chart_doc(now)).decode("utf-8"), 0o644)
        result = _run_render(ctx, data_path, out_dir, config["render_account"])
    finally:
        shutil.rmtree(str(out_dir), ignore_errors=True)
    status = result.get("status")
    if status == "OK":
        return f"OK ({len(_list(result.get('pngs')))} charts)"
    reason = result.get("reason") if status == "ERROR" else status
    raise InspectError(reason if isinstance(reason, str) and core.REASON_RE.fullmatch(reason) else "RENDER")


# ---------------------------------------------------------------------------- resume, set-secret, render

def cmd_resume(ctx: Ctx, pointer: str) -> Dict[str, Any]:
    now = ctx.now()
    config = ctx.config()
    pattern = (r"https://github\.com/" + re.escape(config["control_repository"])
               + r"/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*")
    if not isinstance(pointer, str) or not re.fullmatch(pattern, pointer):
        raise InspectError("POINTER", "pointer must be a comment URL in the control repository")
    kill = core.kill_state(ctx.paths)
    if kill is None:
        return {"status": "NOT_HALTED"}
    core.clear_kill(ctx.paths)
    record = {"resume": pointer, "at": core.iso(now)}
    ctx.store.append_jsonl("history", record)
    ctx.store.append_jsonl("errors", record)  # errors before a resume no longer count
    return {"status": "RESUMED", "previous": {"reason": kill.get("reason"), "at": kill.get("at")},
            "pointer": pointer}


def cmd_set_secret(ctx: Ctx, kind: str) -> Dict[str, Any]:
    value = ctx.deps.getpass(f"{kind}: ")
    if isinstance(value, str) and value.strip():
        ctx.live.append(value.strip())
    path = ctx.paths.secret(kind)
    if not path.parent.exists():
        _ensure_dir(path.parent, 0o755)
    digest = core.write_secret(path, kind, value)
    return {"status": "STORED", "kind": kind, "sha256_8": digest}


def cmd_render(ctx: Ctx, data: str, out: str, font: Optional[str]) -> Dict[str, Any]:
    """Internal: run the charts command mode as the render account with the render limits."""
    config = ctx.config()
    data_path, out_dir = Path(os.path.abspath(data)), Path(os.path.abspath(out))
    if not out_dir.is_dir() or out_dir.is_symlink():
        raise InspectError("OUT_DIR", "output must be an existing directory")
    result = _run_render(ctx, data_path, out_dir, config["render_account"], font)
    if not isinstance(result.get("status"), str):
        raise InspectError("RENDER_OUTPUT")
    return result


# ---------------------------------------------------------------------------- argument parsing and main

class _ArgsError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one JSON line, exit 2, no usage text
        raise _ArgsError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        raise _ArgsError(message or "")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=INSTALLED_NAME, add_help=False)
    parser.add_argument("--root", default="/")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    for name in ("daemon", "start", "stop", "status", "tick", "probe", "preflight"):
        sub.add_parser(name, add_help=False)
    resume = sub.add_parser("resume", add_help=False)
    resume.add_argument("--pointer", required=True)
    secret = sub.add_parser("set-secret", add_help=False)
    secret.add_argument("kind", choices=SECRET_ARGS)
    render = sub.add_parser("render", add_help=False)
    render.add_argument("--data", required=True)
    render.add_argument("--out", required=True)
    render.add_argument("--font")
    return parser


NO_TICK_LOCK = ("status", "stop", "set-secret", "daemon", "start")


def dispatch(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    command = args.command
    if ctx.installed and ctx.deps.euid() != 0:
        raise InspectError("NOT_ROOT", "aiops-inspect runs as root")
    simple: Dict[str, Callable[[], Dict[str, Any]]] = {
        "status": lambda: cmd_status(ctx), "stop": lambda: cmd_stop(ctx),
        "set-secret": lambda: cmd_set_secret(ctx, args.kind),
        # CONTRACT NOTE: daemon and start hold daemon.lock instead of tick.lock (the daemon's own tick
        # child takes tick.lock, so the daemon must not hold it).
        "daemon": lambda: cmd_daemon(ctx), "start": lambda: cmd_start(ctx),
    }
    if command in simple:
        return simple[command]()
    if command == "tick":
        return cmd_tick(ctx)
    locked: Dict[str, Callable[[], Dict[str, Any]]] = {
        "probe": lambda: cmd_probe(ctx), "preflight": lambda: cmd_preflight(ctx),
        "resume": lambda: cmd_resume(ctx, args.pointer),
        "render": lambda: cmd_render(ctx, args.data, args.out, args.font),
    }
    return with_tick_lock(ctx, locked[command])


def main(argv: Optional[List[str]] = None, deps: Optional[Deps] = None, stream: Any = None) -> int:
    """Parse, run one command, print exactly one redacted JSON line. Never prints a traceback."""
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.command is None:
            raise _ArgsError("command required")
    except _ArgsError:
        core.emit({"status": "ERROR", "reason": "ARGS"}, stream=stream)
        return 2
    ctx = Ctx(args.root, deps)
    try:
        result = dispatch(ctx, args)
        code = 1 if result.get("status") == "ERROR" else 0
    except BaseException as exc:  # noqa: BLE001 - every failure becomes one ERROR line
        result, code = core.error_line(exc, ctx.live), 1
    try:
        core.emit(result, ctx.live, stream=stream)
    except (TypeError, ValueError):
        core.emit({"status": "ERROR", "reason": "OUTPUT"}, stream=stream)
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
