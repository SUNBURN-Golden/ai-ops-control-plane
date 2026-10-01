"""Program inspector core: paths, config, secrets, state, kill switch, manifest, time, hygiene.

The read-only program inspector (User decision M7, docs/INSPECTOR.md) is advisory only and
never a gate. This module holds everything the other inspector modules share and does no
network I/O. Every path derives from one root prefix so tests run in a temporary directory.
Secrets are loaded only from root-owned 0600 files, are never echoed, and every text the
inspector posts or prints passes through the hygiene functions at the end of this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------- errors

REASON_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class InspectError(Exception):
    """A handled failure. ``reason`` is an UPPER_SNAKE code; ``str(err)`` is the reason only."""

    def __init__(self, reason: str, detail: str = ""):
        # CONTRACT NOTE: a malformed reason code is coerced to INTERNAL instead of raising here.
        self.reason = reason if isinstance(reason, str) and REASON_RE.fullmatch(reason) else "INTERNAL"
        self.detail = detail if isinstance(detail, str) else ""
        super().__init__(self.reason)

    def __str__(self) -> str:
        return self.reason


# ---------------------------------------------------------------------------- paths

INSPECTOR_MODULES = (
    "control_plane_inspect.py", "control_plane_inspect_core.py", "control_plane_inspect_github.py",
    "control_plane_inspect_host.py", "control_plane_inspect_facts.py", "control_plane_inspect_signals.py",
    "control_plane_inspect_charts.py", "control_plane_inspect_publish.py",
)
CENTRAL_MODULES = ("control_plane.py", "control_plane_program.py")


@dataclass(frozen=True)
class Paths:
    """Every inspector path, derived from one root prefix ("/" when installed)."""

    root: Path

    @classmethod
    def from_root(cls, root: Union[str, Path] = "/") -> "Paths":
        path = Path(root)
        if not path.is_absolute():
            path = Path(os.path.abspath(str(path)))
        return cls(path)

    def _at(self, relative: str) -> Path:
        return self.root / relative

    @property
    def etc(self) -> Path:
        return self._at("etc/aiops")

    @property
    def config(self) -> Path:
        return self._at("etc/aiops/inspect.json")

    @property
    def gh_read(self) -> Path:
        return self._at("etc/aiops/inspect-gh-read")

    @property
    def gh_ledger(self) -> Path:
        return self._at("etc/aiops/inspect-gh-ledger")

    @property
    def slack(self) -> Path:
        return self._at("etc/aiops/inspect-slack-token")

    @property
    def kill(self) -> Path:
        return self._at("etc/aiops/inspect-disabled")

    @property
    def manifest(self) -> Path:
        return self._at("etc/aiops/inspect-expected.sha256")

    @property
    def state(self) -> Path:
        return self._at("var/lib/aiops-inspect/state")

    @property
    def runs(self) -> Path:
        return self._at("var/lib/aiops-inspect/runs")

    @property
    def lib(self) -> Path:
        return self._at("opt/aiops/inspect/lib")

    @property
    def render(self) -> Path:
        return self._at("var/lib/aiops-plot")

    @property
    def tool_bin(self) -> Path:
        return self._at("opt/aiops/bin/aiops-inspect")

    def secret(self, kind: str) -> Path:
        """The secret file for ``kind`` (gh-read, gh-ledger, slack)."""
        if kind not in SECRET_KINDS:
            raise InspectError("SECRET_KIND")
        return {"gh-read": self.gh_read, "gh-ledger": self.gh_ledger, "slack": self.slack}[kind]


# ---------------------------------------------------------------------------- canonical JSON

def canon(obj: Any) -> bytes:
    """Canonical JSON bytes (sorted keys, no spaces, UTF-8, no NaN)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _unique_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def loads_strict(data: Union[bytes, str]) -> Any:
    """Parse JSON refusing duplicate keys and NaN/Infinity; raises ValueError."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)


# ---------------------------------------------------------------------------- time

KST = timezone(timedelta(hours=9))
ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})")


def _aware(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise InspectError("TIME", "a timezone-aware datetime is required")
    return dt


def utc(dt: datetime) -> datetime:
    """``dt`` converted to UTC (must be tz-aware)."""
    return _aware(dt).astimezone(timezone.utc)


def fmt_kst(dt: datetime) -> str:
    """``10/01 14:17 KST``. Posted text always uses absolute times."""
    return _aware(dt).astimezone(KST).strftime("%m/%d %H:%M KST")


def kst_day(dt: datetime) -> str:
    return _aware(dt).astimezone(KST).strftime("%Y-%m-%d")


def iso(dt: datetime) -> str:
    """``2026-10-01T05:17:00Z`` (UTC, whole seconds)."""
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp with an explicit zone into an aware UTC datetime."""
    if not isinstance(value, str):
        raise InspectError("TIME", "timestamp is not a string")
    match = ISO_RE.fullmatch(value.strip())
    if not match:
        raise InspectError("TIME", "timestamp is not ISO-8601 with a zone")
    year, month, day, hour, minute, second, frac, zone = match.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    if zone == "Z":
        tz = timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        offset = timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
        if offset >= timedelta(hours=24):
            raise InspectError("TIME", "bad zone offset")
        tz = timezone(sign * offset)
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=tz)
    except ValueError:
        raise InspectError("TIME", "timestamp out of range") from None
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------- run ids

RUN_KIND_RE = re.compile(r"[a-z][a-z0-9-]{0,23}")
RUN_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}-[a-z][a-z0-9-]{0,23}")


def new_run_id(now: datetime, kind: str) -> str:
    """``<YYYYMMDDTHHMMSSZ>-<8hex>-<kind>``."""
    if not isinstance(kind, str) or not RUN_KIND_RE.fullmatch(kind):
        raise InspectError("RUN_KIND")
    return f"{utc(now).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}-{kind}"


# ---------------------------------------------------------------------------- config

CONFIG_SCHEMA = "AIOPS_INSPECT_CONFIG_V1"
STAGES = ("DRY", "LIVE")
RESERVED_PREFIXES = ("CTRL",)
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PREFIX_RE = re.compile(r"[A-Z]{4}")
TEAM_RE = re.compile(r"T[A-Z0-9]{8,}")
USER_ID_RE = re.compile(r"[UW][A-Z0-9]{8,}")
CHANNEL_RE = re.compile(r"[CG][A-Z0-9]{8,}")
# CONTRACT NOTE: a Slack id made of its letter and zeros (optionally ending in 1) is a placeholder.
PLACEHOLDER_ID_RE = re.compile(r"[TUWCG]0{7,}1?")
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
ACCOUNT_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
HELPER_RE = re.compile(r"(?:/[A-Za-z0-9_.-]+)+")
PAIR_PATH_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
MAX_ISSUE = 10 ** 9
MAX_CONFIG_BYTES = 1 << 20
DEFAULT_TEST_PATH_REGEX = r"(^|/)(tests?|__tests__|spec)/|(_test|[.]test|[.]spec)[.]"
# CONTRACT NOTE: the §3.5 THRESHOLDS defaults live here too so config validation knows the keys;
# control_plane_inspect_signals.THRESHOLDS must hold the same keys and values.
THRESHOLD_DEFAULTS: Dict[str, int] = {
    "orphan_watch": 1, "orphan_at_risk": 2, "resumes_watch": 2, "resumes_at_risk": 3,
    "review_fail_watch": 2, "review_fail_at_risk": 3, "prestart_watch": 2, "exceptions_node_watch": 3,
    "exceptions_product_watch": 6, "blocked_watch_h": 24, "done_gap_watch_d": 3, "done_gap_at_risk_d": 7,
    "confirmed_watch_h": 6, "confirmed_at_risk_h": 12, "cleanup_watch_h": 24, "idle_waiting_snapshots": 2,
    "idle_waiting_min_span_h": 1, "pending_checks_watch_h": 24,
}
CONFIG_REQUIRED = ("schema", "stage", "control_repository", "targets", "ledger_issue", "test_ledger_issue",
                   "slack", "user_login", "host_helper", "ledger_account", "render_account", "tick_minute",
                   "daily_hour_kst", "deadman_hours")
# CONTRACT NOTE: contract_pairs, test_path_regex and thresholds may be omitted; defaults are filled in.
CONFIG_OPTIONAL = ("contract_pairs", "test_path_regex", "thresholds")
SLACK_KEYS = ("team_id", "bot_user_id", "channel_id", "test_channel_id")
MAX_TARGETS = 32
MAX_PAIRS = 64


def _cfg(condition: bool, detail: str) -> None:
    if not condition:
        raise InspectError("CONFIG", detail)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in(obj: Dict[str, Any], key: str, low: int, high: int) -> int:
    value = obj.get(key)
    _cfg(_is_int(value) and low <= value <= high, f"{key} must be an integer in {low}..{high}")
    return value


def _exact_keys(obj: Any, required: Sequence[str], optional: Sequence[str], where: str) -> None:
    _cfg(isinstance(obj, dict), f"{where} must be an object")
    keys = set(obj)
    unknown = sorted(keys - set(required) - set(optional))
    missing = sorted(set(required) - keys)
    _cfg(not unknown, f"{where} has unknown keys: {', '.join(unknown)[:200]}")
    _cfg(not missing, f"{where} is missing keys: {', '.join(missing)}")


def _str_match(obj: Dict[str, Any], key: str, regex: "re.Pattern[str]", where: str = "") -> str:
    value = obj.get(key)
    _cfg(isinstance(value, str) and len(value) <= 200 and bool(regex.fullmatch(value)),
         f"{where}{key} has an invalid value")
    return value


def validate_config(obj: Any, allow_placeholders: bool = False) -> Dict[str, Any]:
    """Strictly validate AIOPS_INSPECT_CONFIG_V1 and return a copy with optional keys filled.

    ``allow_placeholders`` accepts issue number 0 and all-zero Slack ids (the example file);
    a config holding any placeholder is valid only in stage DRY.
    """
    _exact_keys(obj, CONFIG_REQUIRED, CONFIG_OPTIONAL, "config")
    _cfg(obj["schema"] == CONFIG_SCHEMA, "schema must be " + CONFIG_SCHEMA)
    _cfg(obj["stage"] in STAGES, "stage must be DRY or LIVE")
    control = _str_match(obj, "control_repository", REPO_RE)
    placeholders: List[str] = []

    targets = obj["targets"]
    _cfg(isinstance(targets, list) and 1 <= len(targets) <= MAX_TARGETS, f"targets must list 1..{MAX_TARGETS}")
    repos: List[str] = []
    prefixes: List[str] = []
    for index, target in enumerate(targets):
        where = f"targets[{index}]."
        _exact_keys(target, ("repository", "prefix"), (), f"targets[{index}]")
        repo = _str_match(target, "repository", REPO_RE, where)
        prefix = _str_match(target, "prefix", PREFIX_RE, where)
        _cfg(prefix not in RESERVED_PREFIXES, f"{where}prefix {prefix} is reserved")
        # CONTRACT NOTE: repositories compare case-insensitively (GitHub names do).
        _cfg(repo.lower() not in [r.lower() for r in repos], f"{where}repository is listed twice")
        _cfg(prefix not in prefixes, f"{where}prefix is not unique")
        # CONTRACT NOTE: the control repository is never a target (it has its own CTRL row).
        _cfg(repo.lower() != control.lower(), f"{where}repository is the control repository")
        repos.append(repo)
        prefixes.append(prefix)

    for key in ("ledger_issue", "test_ledger_issue"):
        value = _int_in(obj, key, 0, MAX_ISSUE)
        if value == 0:
            _cfg(allow_placeholders, f"{key} must be > 0")
            placeholders.append(key)
    if obj["ledger_issue"] and obj["test_ledger_issue"]:
        _cfg(obj["ledger_issue"] != obj["test_ledger_issue"], "ledger_issue and test_ledger_issue must differ")

    slack = obj["slack"]
    _exact_keys(slack, SLACK_KEYS, (), "slack")
    _str_match(slack, "team_id", TEAM_RE, "slack.")
    _str_match(slack, "bot_user_id", USER_ID_RE, "slack.")
    _str_match(slack, "channel_id", CHANNEL_RE, "slack.")
    _str_match(slack, "test_channel_id", CHANNEL_RE, "slack.")
    _cfg(slack["channel_id"] != slack["test_channel_id"], "slack channel_id and test_channel_id must differ")
    for key in SLACK_KEYS:
        if PLACEHOLDER_ID_RE.fullmatch(slack[key]):
            _cfg(allow_placeholders, f"slack.{key} is a placeholder")
            placeholders.append("slack." + key)

    _str_match(obj, "user_login", LOGIN_RE)
    helper = obj["host_helper"]
    _cfg(isinstance(helper, str) and len(helper) <= 200 and bool(HELPER_RE.fullmatch(helper))
         and "/../" not in helper + "/" and "/./" not in helper + "/", "host_helper must be an absolute path")
    ledger_account = _str_match(obj, "ledger_account", ACCOUNT_RE)
    render_account = _str_match(obj, "render_account", ACCOUNT_RE)
    _cfg(ledger_account != render_account, "ledger_account and render_account must differ")
    _cfg("root" not in (ledger_account, render_account), "accounts must not be root")
    _int_in(obj, "tick_minute", 0, 59)
    _int_in(obj, "daily_hour_kst", 0, 23)
    # CONTRACT NOTE: ticks are hourly, so the dead-man window must cover at least two ticks.
    _int_in(obj, "deadman_hours", 2, 72)

    pairs = obj.get("contract_pairs", [])
    _cfg(isinstance(pairs, list) and len(pairs) <= MAX_PAIRS, f"contract_pairs must list at most {MAX_PAIRS}")
    lowered = [r.lower() for r in repos]
    for index, pair in enumerate(pairs):
        _exact_keys(pair, ("a", "b"), (), f"contract_pairs[{index}]")
        for side in ("a", "b"):
            where = f"contract_pairs[{index}].{side}."
            _exact_keys(pair[side], ("repository", "path"), (), where[:-1])
            repo = _str_match(pair[side], "repository", REPO_RE, where)
            # CONTRACT NOTE: a contract pair may only name configured target repositories.
            _cfg(repo.lower() in lowered, f"{where}repository is not a target")
            path = pair[side].get("path")
            _cfg(isinstance(path, str) and len(path) <= 400 and bool(PAIR_PATH_RE.fullmatch(path))
                 and ".." not in path.split("/") and "." not in path.split("/"), f"{where}path is invalid")
        _cfg(pair["a"] != pair["b"], f"contract_pairs[{index}] names the same file twice")

    regex = obj.get("test_path_regex", DEFAULT_TEST_PATH_REGEX)
    _cfg(isinstance(regex, str) and 0 < len(regex) <= 500, "test_path_regex must be a short string")
    try:
        re.compile(regex)
    except re.error:
        raise InspectError("CONFIG", "test_path_regex does not compile") from None

    thresholds = obj.get("thresholds", {})
    _cfg(isinstance(thresholds, dict), "thresholds must be an object")
    for key, value in thresholds.items():
        _cfg(key in THRESHOLD_DEFAULTS, f"thresholds has unknown key {str(key)[:64]}")
        _cfg(_is_int(value) and 1 <= value <= 10000, f"thresholds.{key} must be an integer in 1..10000")

    if placeholders:
        _cfg(obj["stage"] == "DRY", "a config with placeholders must use stage DRY")

    out = json.loads(json.dumps(obj))
    out.setdefault("contract_pairs", [])
    out.setdefault("test_path_regex", DEFAULT_TEST_PATH_REGEX)
    out.setdefault("thresholds", {})
    return out


def thresholds_of(config: Dict[str, Any]) -> Dict[str, int]:
    """THRESHOLD_DEFAULTS with the config's overrides applied."""
    merged = dict(THRESHOLD_DEFAULTS)
    merged.update(config.get("thresholds") or {})
    return merged


def active_ledger_issue(config: Dict[str, Any]) -> int:
    """The ledger issue for the configured stage (test issue in DRY)."""
    return config["test_ledger_issue"] if config["stage"] == "DRY" else config["ledger_issue"]


def active_channel(config: Dict[str, Any]) -> str:
    """The Slack channel for the configured stage (test channel in DRY)."""
    return config["slack"]["test_channel_id"] if config["stage"] == "DRY" else config["slack"]["channel_id"]


def _read_owned(path: Path, *, secret_mode: bool, require_root_owner: bool, limit: int, reason: str) -> bytes:
    """Read a regular, non-symlink file with owner and mode checks (fstat after open, no TOCTOU)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        raise InspectError(reason, "missing") from None
    except OSError:
        raise InspectError(reason, "unreadable or a symlink") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InspectError(reason, "not a regular file")
        if require_root_owner and info.st_uid != 0:
            raise InspectError(reason, "not owned by root")
        mode = stat.S_IMODE(info.st_mode)
        if secret_mode and mode != 0o600:
            raise InspectError(reason, "mode must be 0600")
        if not secret_mode and mode & 0o077:
            raise InspectError(reason, "mode must not grant group or other access")
        if info.st_size > limit:
            raise InspectError(reason, "too large")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise InspectError(reason, "too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_config(path: Union[str, Path], allow_placeholders: bool = False,
                require_root_owner: bool = True) -> Dict[str, Any]:
    """Read and validate the config file (root-owned, no group/other access)."""
    # CONTRACT NOTE: the config must be root-owned with no group/other bits (0600 or 0400).
    data = _read_owned(Path(path), secret_mode=False, require_root_owner=require_root_owner,
                       limit=MAX_CONFIG_BYTES, reason="CONFIG")
    try:
        obj = loads_strict(data)
    except (ValueError, UnicodeDecodeError):
        raise InspectError("CONFIG", "not valid JSON") from None
    return validate_config(obj, allow_placeholders=allow_placeholders)


# ---------------------------------------------------------------------------- secrets

SECRET_KINDS = ("gh-read", "gh-ledger", "slack")
SECRET_SHAPES = {
    "gh-read": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "gh-ledger": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "slack": re.compile(r"xoxb-[A-Za-z0-9-]{20,}"),
}
MAX_SECRET_BYTES = 4096


def secret_reason(kind: str) -> str:
    return "SECRET_" + kind.upper().replace("-", "_")


def check_secret_shape(kind: str, value: str) -> str:
    """Return the stripped value when it has the kind's shape; the value is never echoed."""
    if kind not in SECRET_KINDS:
        raise InspectError("SECRET_KIND")
    value = value.strip() if isinstance(value, str) else ""
    if not SECRET_SHAPES[kind].fullmatch(value):
        raise InspectError(secret_reason(kind), "value does not have the expected shape")
    return value


def load_secret(path: Union[str, Path], kind: str, require_root_owner: bool = True) -> str:
    """Load one secret: regular file, not a symlink, owner uid 0, mode exactly 0600, fine-grained shape."""
    if kind not in SECRET_KINDS:
        raise InspectError("SECRET_KIND")
    data = _read_owned(Path(path), secret_mode=True, require_root_owner=require_root_owner,
                       limit=MAX_SECRET_BYTES, reason=secret_reason(kind))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InspectError(secret_reason(kind), "not UTF-8") from None
    return check_secret_shape(kind, text)


def load_secrets(paths: Paths, require_root_owner: bool = True) -> Dict[str, str]:
    """All three secrets keyed by kind."""
    return {kind: load_secret(paths.secret(kind), kind, require_root_owner) for kind in SECRET_KINDS}


def write_secret(path: Union[str, Path], kind: str, value: str) -> str:
    """Validate and store a secret atomically with mode 0600; returns 8 hex of its sha256."""
    value = check_secret_shape(kind, value)
    _atomic_write(Path(path), (value + "\n").encode("utf-8"), 0o600)
    return sha256_hex(value.encode("utf-8"))[:8]


# ---------------------------------------------------------------------------- atomic files

def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """tmp file in the same directory + fsync + os.replace + directory fsync."""
    directory = path.parent
    tmp = directory / f".{path.name}.{secrets.token_hex(6)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(tmp), flags, mode)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    _fsync_dir(directory)


# ---------------------------------------------------------------------------- state store

MAX_STATE_BYTES = 32 << 20
STATE_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class StateStore:
    """JSON state files under one directory (created 0700).

    CONTRACT NOTE: names are given without extension (``facts`` -> ``facts.json``,
    ``history`` -> ``history.jsonl``); a trailing ``.json``/``.jsonl`` is accepted and stripped.
    Names may hold up to three ``/``-separated segments (``publish/<run>``).
    """

    def __init__(self, directory: Union[str, Path]):
        self.dir = Path(directory)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(self.dir), 0o700)

    def _name(self, name: str) -> str:
        if not isinstance(name, str):
            raise InspectError("STATE_NAME")
        for ext in (".jsonl", ".json"):
            if name.endswith(ext):
                name = name[: -len(ext)]
                break
        segments = name.split("/")
        if (not 1 <= len(segments) <= 3 or not all(STATE_SEGMENT_RE.fullmatch(s) for s in segments)
                or name.endswith(".pending") or any(s in (".", "..") for s in segments)):
            raise InspectError("STATE_NAME")
        return name

    def path(self, name: str, suffix: str = ".json") -> Path:
        """The file behind ``name`` (``suffix`` .json, .pending.json or .jsonl)."""
        path = self.dir / (self._name(name) + suffix)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def _load(self, path: Path, default: Any) -> Any:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(str(path), flags)
        except FileNotFoundError:
            return default
        except OSError:
            raise InspectError("STATE_CORRUPT", path.name) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
                raise InspectError("STATE_CORRUPT", path.name)
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read(MAX_STATE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_STATE_BYTES:
            raise InspectError("STATE_CORRUPT", path.name)
        try:
            return loads_strict(data)
        except (ValueError, UnicodeDecodeError):
            raise InspectError("STATE_CORRUPT", path.name) from None

    @staticmethod
    def _dump(obj: Any) -> bytes:
        try:
            data = json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise InspectError("STATE_TYPE") from None
        if len(data) > MAX_STATE_BYTES:
            raise InspectError("STATE_SIZE")
        return data

    def read(self, name: str, default: Any = None) -> Any:
        return self._load(self.path(name), default)

    def write(self, name: str, obj: Any) -> None:
        _atomic_write(self.path(name), self._dump(obj), 0o600)

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def remove(self, name: str) -> None:
        try:
            os.unlink(str(self.path(name)))
        except FileNotFoundError:
            pass

    def append_jsonl(self, name: str, obj: Any) -> None:
        """Append one JSON line (fsynced); the file stays within the size cap."""
        line = json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8") + b"\n"
        path = self.path(name, ".jsonl")
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(str(path), flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise InspectError("STATE_CORRUPT", path.name)
            if info.st_size + len(line) > MAX_STATE_BYTES:
                raise InspectError("STATE_SIZE")
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def read_jsonl(self, name: str) -> List[Any]:
        """Every parseable line; a torn or corrupt line (crash mid-append) is skipped."""
        path = self.path(name, ".jsonl")
        try:
            info = os.lstat(str(path))
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            raise InspectError("STATE_CORRUPT", path.name)
        rows = []
        with open(str(path), "rb") as handle:
            for raw in handle:
                try:
                    rows.append(loads_strict(raw))
                except (ValueError, UnicodeDecodeError):
                    continue
        return rows

    def write_jsonl(self, name: str, rows: Iterable[Any]) -> None:
        """Replace a JSONL file atomically (used for pruning)."""
        data = b"".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                   separators=(",", ":")).encode("utf-8") + b"\n" for r in rows)
        if len(data) > MAX_STATE_BYTES:
            raise InspectError("STATE_SIZE")
        _atomic_write(self.path(name, ".jsonl"), data, 0o600)

    def stage(self, name: str, obj: Any) -> None:
        """Write ``<name>.pending.json``; it becomes ``<name>.json`` only through commit_staged."""
        _atomic_write(self.path(name, ".pending.json"), self._dump(obj), 0o600)

    def read_staged(self, name: str, default: Any = None) -> Any:
        return self._load(self.path(name, ".pending.json"), default)

    def commit_staged(self, names: Iterable[str]) -> List[str]:
        """Rename every existing pending file over its target (atomic per file). Returns committed names.

        Every pending file is parsed first; if any is corrupt nothing is renamed.
        """
        names = [self._name(n) for n in names]
        present = []
        for name in names:
            pending = self.path(name, ".pending.json")
            if os.path.lexists(str(pending)):
                self._load(pending, None)
                present.append(name)
        for name in present:
            os.replace(str(self.path(name, ".pending.json")), str(self.path(name)))
        for directory in {self.path(n).parent for n in present}:
            _fsync_dir(directory)
        return present

    def discard_staged(self, names: Iterable[str]) -> None:
        for name in names:
            try:
                os.unlink(str(self.path(name, ".pending.json")))
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------- kill switch

def kill_state(paths: Paths) -> Optional[Dict[str, Any]]:
    """The kill switch record, or None when absent. A present but unreadable file still halts."""
    if not os.path.lexists(str(paths.kill)):
        return None
    try:
        info = os.lstat(str(paths.kill))
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("not a small regular file")
        obj = loads_strict(paths.kill.read_bytes())
        if not isinstance(obj, dict):
            raise ValueError("not an object")
    except (OSError, ValueError, UnicodeDecodeError):
        # CONTRACT NOTE: fail closed; presence alone halts the tool.
        return {"reason": "KILL_UNREADABLE", "run": None, "at": None}
    reason = obj.get("reason")
    return {"reason": reason if isinstance(reason, str) and REASON_RE.fullmatch(reason) else "KILL_UNREADABLE",
            "run": obj.get("run") if isinstance(obj.get("run"), str) else None,
            "at": obj.get("at") if isinstance(obj.get("at"), str) else None}


def write_kill(paths: Paths, reason: str, run_id: Optional[str], now: datetime) -> Dict[str, Any]:
    """Engage the kill switch (JSON reason/run/at, mode 0644). An existing switch is kept as is."""
    existing = kill_state(paths)
    if existing is not None:
        # CONTRACT NOTE: the first recorded reason wins; later halts never overwrite it.
        return existing
    record = {"reason": reason if isinstance(reason, str) and REASON_RE.fullmatch(reason) else "INTERNAL",
              "run": run_id if isinstance(run_id, str) else None, "at": iso(now)}
    paths.kill.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _atomic_write(paths.kill, canon(record) + b"\n", 0o644)
    return record


def clear_kill(paths: Paths) -> bool:
    """Remove the kill switch. Only the ``resume`` subcommand calls this."""
    try:
        os.unlink(str(paths.kill))
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------- manifest

MANIFEST_LINE_RE = re.compile(r"([0-9a-f]{64})  (/[^\n]*)")
MAX_MANIFEST_BYTES = 1 << 20
MAX_TOOL_FILE = 64 << 20


def own_files(paths: Paths) -> List[Path]:
    """Installed tool files that the manifest must list."""
    return [paths.lib / name for name in INSPECTOR_MODULES + CENTRAL_MODULES] + [paths.tool_bin]


def file_sha256(path: Path) -> str:
    """sha256 of a regular (non-symlink) file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOOL_FILE:
            raise OSError("not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def verify_manifest(paths: Paths, required: Iterable[Path], require_root_owner: bool = True) -> Dict[str, Any]:
    """Check the expected-digest manifest; any problem -> InspectError("TOOL_TAMPERED").

    Lines are ``<64hex>  <absolute path>``. Every required file must be listed; every listed file
    must exist and match. Returns ``{"files": {path: sha256}, "tool_sha256": sha256(canon(files))}``.
    """
    # CONTRACT NOTE: the return value is not specified; tool_sha256 feeds the ledger header tool=<12hex>.
    def tampered(detail: str) -> InspectError:
        return InspectError("TOOL_TAMPERED", detail)

    try:
        info = os.lstat(str(paths.manifest))
    except OSError:
        raise tampered("manifest missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
        raise tampered("manifest is not a regular file")
    if require_root_owner and (info.st_uid != 0 or info.st_mode & 0o022):
        raise tampered("manifest must be root-owned and not group/other writable")
    try:
        text = paths.manifest.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise tampered("manifest unreadable") from None
    listed: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = MANIFEST_LINE_RE.fullmatch(line)
        if not match:
            raise tampered("malformed manifest line")
        digest, name = match.groups()
        name = os.path.normpath(name)
        if name in listed and listed[name] != digest:
            raise tampered("conflicting manifest lines")
        listed[name] = digest
    if not listed:
        raise tampered("empty manifest")
    for path in required:
        name = os.path.normpath(str(path))
        if not os.path.isabs(name) or name not in listed:
            raise tampered("a required file is not listed")
    for name, digest in sorted(listed.items()):
        try:
            file_info = os.lstat(name)
            if require_root_owner and (file_info.st_uid != 0 or file_info.st_mode & 0o022):
                raise tampered("a listed file is not root-owned or is writable")
            actual = file_sha256(Path(name))
        except OSError:
            raise tampered("a listed file is missing") from None
        if actual != digest:
            raise tampered("a listed file does not match")
    return {"files": dict(sorted(listed.items())), "tool_sha256": sha256_hex(canon(listed))}


# ---------------------------------------------------------------------------- hygiene (§3.6)

# CONTRACT NOTE: the JWT shape also consumes the signature part, and the private-key shape
# consumes the key body up to its END line (or the end of the text), so no key material survives.
TOKEN_SHAPE_PATTERNS = (
    r"sk-ant-[A-Za-z0-9_-]{20,}",
    r"gh[opsru]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    r"sess-[A-Za-z0-9]{20,}",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*",
    r"xox[abposr]-[A-Za-z0-9-]{10,}",
    r"xapp-[A-Za-z0-9-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|[\s\S]*)",
)
# CONTRACT NOTE: TOKEN_SHAPES is one compiled alternation (like control_plane_fable.TOKEN_SHAPES);
# the individual patterns are in TOKEN_SHAPE_PATTERNS.
TOKEN_SHAPES = re.compile("|".join(f"(?:{p})" for p in TOKEN_SHAPE_PATTERNS))
LIVE_WINDOW = 20
MIN_LIVE = 8
HOST_LINE = "감리는 로그인·코드·명령을 요청하지 않는다 · 자문 전용 · 게이트 아님"
ZWSP = "\u200b"


def _mark(secret_text: str) -> str:
    return f"[redacted sha256={sha256_hex(secret_text.encode('utf-8'))[:8]}]"


def _live_spans(text: str, value: str) -> List[Tuple[int, int]]:
    """Spans of ``text`` covered by any >=20-char substring of ``value`` (the whole value if shorter)."""
    if len(value) < LIVE_WINDOW:
        spans = []
        start = text.find(value)
        while start != -1:
            spans.append((start, start + len(value)))
            start = text.find(value, start + len(value))
        return spans
    windows = {value[i:i + LIVE_WINDOW] for i in range(len(value) - LIVE_WINDOW + 1)}
    spans = []
    i = 0
    last = len(text) - LIVE_WINDOW
    while i <= last:
        if text[i:i + LIVE_WINDOW] in windows:
            j = i
            while j + 1 <= last and text[j + 1:j + 1 + LIVE_WINDOW] in windows:
                j += 1
            spans.append((i, j + LIVE_WINDOW))
            i = j + LIVE_WINDOW
        else:
            i += 1
    return spans


def redact(text: str, live_values: Iterable[str] = ()) -> Tuple[str, Dict[str, int]]:
    """Replace live-secret substrings and token shapes with ``[redacted sha256=<8hex>]``.

    Returns ``(text, hits)`` with ``hits = {"live": n, "shape": m}``; live values are matched first,
    so a live token that also has a known shape counts as live.
    """
    # CONTRACT NOTE: hits is a dict so callers can tell a LIVE hit (-> SECRET_LIVE) from a shape hit.
    if not isinstance(text, str):
        text = str(text)
    hits = {"live": 0, "shape": 0}
    values = sorted({v.strip() for v in live_values if isinstance(v, str) and len(v.strip()) >= MIN_LIVE},
                    key=len, reverse=True)
    for value in values:
        spans = _live_spans(text, value)
        if not spans:
            continue
        hits["live"] += len(spans)
        pieces = []
        cursor = 0
        for start, end in spans:
            pieces.append(text[cursor:start])
            pieces.append(_mark(text[start:end]))
            cursor = end
        pieces.append(text[cursor:])
        text = "".join(pieces)

    def shape(match: "re.Match[str]") -> str:
        hits["shape"] += 1
        return _mark(match.group(0))

    text = TOKEN_SHAPES.sub(shape, text)
    return text, hits


def redact_output(text: str, live_values: Iterable[str] = ()) -> Tuple[str, int]:
    """Redact text about to leave the host. A live-secret hit raises SECRET_LIVE (-> kill switch)."""
    clean, hits = redact(text, live_values)
    if hits["live"]:
        raise InspectError("SECRET_LIVE", "output contained a live secret; nothing was posted")
    return clean, hits["shape"]


def redact_obj(obj: Any, live_values: Iterable[str] = ()) -> Any:
    """Redact every string (keys included) inside a JSON-like value."""
    values = tuple(live_values)
    if isinstance(obj, str):
        return redact(obj, values)[0]
    if isinstance(obj, dict):
        return {redact(str(k), values)[0]: redact_obj(v, values) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, values) for v in obj]
    return obj


# Bidi overrides/isolates and other invisible format characters that can disguise text.
_BIDI = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c\ufeff")
_WHITESPACE = set("\t\n\r\x0b\x0c\x85\u2028\u2029")
_MARKER_RE = re.compile(r"(ASTRA_|AIOPS_|<!--)")
_GH_ESCAPE = set("\\`*_[]()#|<>!~&")


def _plain_units(text: str) -> List[str]:
    """Characters of ``text`` with whitespace runs collapsed and control/bidi characters dropped."""
    units: List[str] = []
    for ch in text:
        if ch in _WHITESPACE or ch == " ":
            if units and units[-1] != " ":
                units.append(" ")
            continue
        code = ord(ch)
        if code < 0x20 or code == 0x7f or 0x80 <= code < 0xa0 or ch in _BIDI:
            continue
        units.append(ch)
    while units and units[-1] == " ":
        units.pop()
    return units


def _cap(units: List[str], limit: int) -> str:
    """Join escaped units, never splitting one, with an ellipsis when cut."""
    if limit <= 0:
        return ""
    total = sum(len(u) for u in units)
    if total <= limit:
        return "".join(units)
    out: List[str] = []
    used = 0
    for unit in units:
        if used + len(unit) > limit - 1:
            break
        out.append(unit)
        used += len(unit)
    return "".join(out).rstrip(" ") + "…"


def strip_markers(text: str) -> str:
    """Put U+200B before ``ASTRA_``, ``AIOPS_`` and ``<!--`` so no text reads as a marker.

    CONTRACT NOTE: done at every position, not only at a line start, because several marker
    parsers search anywhere in a body.
    """
    if not isinstance(text, str):
        text = str(text)
    return _MARKER_RE.sub(ZWSP + r"\1", text.replace(ZWSP + "ASTRA_", "ASTRA_")
                          .replace(ZWSP + "AIOPS_", "AIOPS_").replace(ZWSP + "<!--", "<!--"))


def gh_text(text: Any, limit: int = 200) -> str:
    """Plain, inert text for GitHub Markdown (one line, no markup, mentions, references or links).

    Escapes ``\\ ` * _ [ ] ( ) # | < > ! ~ &``, collapses newlines and tabs, puts U+200B after
    ``@`` and ``#`` and inside ``://`` and ``www.``, neutralises markers and caps the length.
    """
    # CONTRACT NOTE: `&` is escaped too (entities such as &#64; would otherwise render as `@`), and
    # U+200B also follows `#` and breaks URLs, so no issue reference or cross-reference event can form.
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    base = strip_markers("".join(_plain_units(text)))
    base = re.sub(r"(?i)(://)", ":" + ZWSP + "//", base)
    base = re.sub(r"(?i)\b(www)\.", r"\1" + ZWSP + ".", base)
    units: List[str] = []
    for ch in base:
        if ch == "@" or ch == "#":
            units.append(("\\#" if ch == "#" else "@") + ZWSP)
        elif ch in _GH_ESCAPE:
            units.append("\\" + ch)
        else:
            units.append(ch)
    return _cap(units, limit)


GH_REF_KINDS = ("issue", "pr", "commit", "run", "comment", "ref")
_SHA_REF_RE = re.compile(r"[0-9a-f]{7,40}")
_BRANCH_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def gh_ref(repository: str, kind: str, n: Any) -> str:
    """A reference as a code span (`` `owner/repo#12` ``) so GitHub creates no cross-reference events."""
    if not isinstance(repository, str) or not REPO_RE.fullmatch(repository) or kind not in GH_REF_KINDS:
        raise InspectError("GH_REF")
    if kind in ("issue", "pr", "run", "comment"):
        if not _is_int(n) or not 0 < n < 10 ** 12:
            raise InspectError("GH_REF")
        body = {"issue": f"{repository}#{n}", "pr": f"{repository}#{n}",
                "run": f"{repository} run {n}", "comment": f"{repository} comment {n}"}[kind]
    elif kind == "commit":
        if not isinstance(n, str) or not _SHA_REF_RE.fullmatch(n):
            raise InspectError("GH_REF")
        body = f"{repository}@{n[:12]}"
    else:
        if not isinstance(n, str) or not _BRANCH_REF_RE.fullmatch(n) or ".." in n:
            raise InspectError("GH_REF")
        body = f"{repository}:{n}"
    return f"`{body}`"


def slack_text(text: Any, limit: int = 300) -> str:
    """Plain text for Slack mrkdwn: ``& < >`` escaped, U+200B after ``@``, one line, capped."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    base = strip_markers("".join(_plain_units(text)))
    table = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "@": "@" + ZWSP}
    return _cap([table.get(ch, ch) for ch in base], limit)


_SLACK_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.\-/#?=&%]+")


def slack_link(url: str, label: str, limit: int = 120) -> str:
    """A Slack link ``<url|label>`` for a github.com URL; anything else becomes plain text."""
    shown = slack_text(label, limit)
    if not isinstance(url, str) or not _SLACK_URL_RE.fullmatch(url):
        return shown
    return f"<{url.replace('&', '&amp;')}|{shown}>"


# ---------------------------------------------------------------------------- output

def _one_line_json(obj: Any) -> str:
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)
    return line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def emit(obj: Dict[str, Any], live_values: Iterable[str] = (), stream: Any = None) -> str:
    """Print exactly one JSON line (ensure_ascii=False) after redaction; returns the line."""
    line = _one_line_json(redact_obj(obj, live_values))
    out = stream if stream is not None else sys.stdout
    out.write(line + "\n")
    out.flush()
    return line


def error_line(exc: BaseException, live_values: Iterable[str] = ()) -> Dict[str, Any]:
    """The ``{"status": "ERROR", "reason": ...}`` dict for an exception caught at the top.

    An InspectError keeps its code and a redacted, one-line detail; anything else becomes
    INTERNAL with its class name only (arbitrary messages may carry data).
    """
    if isinstance(exc, InspectError):
        out: Dict[str, Any] = {"status": "ERROR", "reason": exc.reason}
        if exc.detail:
            detail = redact(" ".join(exc.detail.split()), live_values)[0]
            out["detail"] = detail[:300]
        return out
    return {"status": "ERROR", "reason": "INTERNAL", "detail": type(exc).__name__[:80]}
