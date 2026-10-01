"""Program inspector facts: T0 probe, T1 collection, hashing and recheck time (contract §3.4).

The read-only program inspector (User decision M7, docs/INSPECTOR.md) is advisory only and
never a gate. This module reads GitHub through ``GitHubReader`` (GET only) and the host ledger
through ``HostReader`` (three read verbs) and writes nothing but the inspector's own state.

- ``probe`` (T0, every tick): conditional GETs with the committed ETags plus the host lane
  board. New ETags are staged (``etags.pending.json``) and committed only after a successful
  T1 (``commit_t1``) or when nothing changed (``commit_unchanged``).
- ``should_run_t1``: the level-triggered early-exit rule.
- ``collect`` (T1): the AIOPS_INSPECT_FACTS_V1 document. Completion always comes from the central
  ``control_plane_program.node_completion``; every GitHub text field passes ``core.redact``;
  a group that cannot be read is listed in ``unknown`` (three-valued logic) instead of failing
  the tick; budgets, rate limits and network loss abort the whole T1 (previous facts stay).
- ``facts_core`` / ``hashes_of`` / ``next_recheck_at``: material hashes with ages replaced by
  threshold buckets, and the next time a bucket can cross.
- ``commit_t1`` / ``fail_t1``: the atomic state group after a T1.

Source tags: HOST (ledger via helper), GH_SYSTEM (fields GitHub sets), SHA_CONTENT (file content
at a pinned SHA), GH_TEXT (forgeable text under the single token: labels, state_reason, comments).
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import control_plane_inspect_core as core
from control_plane_inspect_core import InspectError
import control_plane_program as cp_program

FACTS_SCHEMA = "AIOPS_INSPECT_FACTS_V1"
SOURCES = ("HOST", "GH_SYSTEM", "SHA_CONTENT", "GH_TEXT")
TASK_LABEL = cp_program.TASK_LABEL
PLAN_PATH = cp_program.PLAN_PATH
# CONTRACT NOTE: the control repository keeps its control-plane files under engineering/
# (CI reads engineering/.github/control-plane/projects.json); the runtime workflow is at the root.
CTRL_PROJECTS_PATH = "engineering/.github/control-plane/projects.json"
CTRL_ACTIVATION_PATH = "engineering/.github/control-plane/activation.json"
RUNTIME_WORKFLOW = "control-plane-runtime.yml"
BLOCKING_LABELS = ("needs-user", "needs-operator", "needs-lane-cleanup", "blocked", "decision-required")
EXCEPTION_RE = re.compile(r"ASTRA_CONSULT_V1 result=APPROVED_SMALL_EXCEPTION")
AIOPS_DIR = ".aiops/"

T0_ISSUES_PER_PAGE = 50
T0_PULLS_PER_PAGE = 50
T0_COMMENTS_PER_PAGE = 100
MAX_PENDING_PLAN_PRS = 20
MAX_PR_FILES = 3000
FILES_PER_PAGE = 100
MAX_EVENT_PAGES = 3
MAX_PULL_PAGES = 5
MAX_EXCEPTION_PAGES = 5
MAX_CHECK_PAGES = 10
DIRECT_PUSH_PER_PAGE = 100
MAX_COMMIT_PAGES = 5
RUNTIME_RUNS = 5
MAX_LEDGER_IDS = 50
MAX_TEXT = 300
# Characters kept past a text's limit before redaction, so a token cut at the limit is still recognised
# while the regex work stays bounded (TOKEN_SHAPES is quadratic on inputs such as "eyJ" repeated).
REDACT_MARGIN = 4096
REPO_META_TTL = timedelta(hours=24)
WINDOW_7D = timedelta(days=7)
WINDOW_14D = timedelta(days=14)
RECHECK_MAX = timedelta(hours=24)
# A recheck time this close to the last T1 is due at the very next tick.
NEXT_TICK = timedelta(minutes=1)
# After a merge, settled post-merge checks are re-read every tick for this long (CI re-runs).
RERUN_WATCH = timedelta(hours=24)

# Product collection groups (the vocabulary of control_plane_inspect_signals.SIGNAL_DEPS).
PRODUCT_GROUPS = ("host", "plan", "pulls", "commits", "issues", "delivery", "comments", "files", "checks",
                  "required_checks", "events")
# Errors that abort the whole T1 (previous facts stay, t1_dirty is set) instead of one group.
ABORT_REASONS = frozenset({"GITHUB_BUDGET", "HOST_BUDGET", "T1_TIMEOUT", "GITHUB_RATE_LIMIT", "NET_DOWN",
                           "NET_UNKNOWN", "PATH_NOT_ALLOWED", "SECRET_GH_READ"})
# Read failures (GitHub or host) that leave a group UNKNOWN: the T1 is still committed, but it stays
# t1_dirty (collected again on the next tick) and counts toward DEGRADED(GITHUB_READ|HOST). Data facts
# such as FILES_TRUNCATED, DELIVERY_PR_MISSING, CHECKS_FORBIDDEN or EVENTS_MISSING are steady and are not
# listed here.
READ_FAILURE_REASONS = frozenset({"GITHUB_5XX", "GITHUB_READ", "GITHUB_JSON", "GITHUB_NOT_FOUND", "MAX_RESPONSE",
                                  "HOST_TIMEOUT", "HOST_UNAVAILABLE", "HOST_REFUSED", "HOST_ARGV", "HOST_OUTPUT"})
# Host failures after which no further host call is attempted in this T1.
HOST_DOWN_REASONS = frozenset({"HOST_TIMEOUT", "HOST_UNAVAILABLE"})
# State names (control_plane_inspect_core.StateStore, without extension).
ST_ETAGS, ST_T0, ST_REPOS, ST_CACHE = "etags", "t0", "repos", "cache"
ST_FACTS, ST_HASHES, ST_RECHECK, ST_HISTORY, ST_BASELINE = "facts", "hashes", "recheck", "history", "baseline"
PROBE_STAGED = (ST_ETAGS, ST_T0, ST_REPOS)
T1_STAGED = PROBE_STAGED + (ST_CACHE,)

SHA_RE = re.compile(r"[0-9a-f]{40}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
ISSUE_URL_NUMBER_RE = re.compile(r"/issues/([1-9][0-9]{0,11})$")

PRODUCT_SOURCES = {
    "head_sha": "GH_SYSTEM", "default_branch": "GH_SYSTEM", "required_checks": "SHA_CONTENT",
    "plan": "SHA_CONTENT", "plan.pending_plan_prs": "GH_SYSTEM", "plan.plan_commits_7d": "GH_SYSTEM",
    "nodes.materialization": "HOST", "nodes.rows": "HOST", "nodes.completion": "HOST",
    "nodes.delivery_pr": "GH_SYSTEM", "nodes.merge_runs": "GH_SYSTEM", "nodes.delivery_files": "GH_SYSTEM",
    "nodes.issue": "GH_TEXT", "nodes.blocked_since": "GH_TEXT", "nodes.exceptions_14d": "GH_TEXT",
    "merged_7d": "GH_SYSTEM", "direct_pushes_7d": "GH_SYSTEM",
}
CONTROL_SOURCES = {"main_sha": "GH_SYSTEM", "runtime_enabled": "SHA_CONTENT", "activated_runtime_sha": "SHA_CONTENT",
                   "runtime_runs": "GH_SYSTEM"}


@dataclass
class Budgets:
    """T1 limits: wall time, host calls and GitHub requests (exceeding any aborts the T1)."""

    seconds: float = 480.0
    host_calls: int = 200
    github_calls: int = 600
    clock: Callable[[], float] = field(default=time.monotonic)


# ---------------------------------------------------------------------------- small helpers

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _pos_int(value: Any) -> Optional[int]:
    return value if _is_int(value) and 0 < value < 10 ** 12 else None


def _sha(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and SHA_RE.fullmatch(value) else None


def _when(value: Any) -> Optional[str]:
    """A GitHub timestamp normalised to ``YYYY-MM-DDTHH:MM:SSZ``, else None."""
    if not isinstance(value, str):
        return None
    try:
        return core.iso(core.parse_iso(value))
    except InspectError:
        return None


def _dt(value: Any) -> Optional[datetime]:
    """An aware datetime from an ISO string or epoch seconds (host rows), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not 0 <= value < 1e11 or value != value:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return core.parse_iso(value)
        except InspectError:
            return None
    return None


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


class _Redactor:
    """Redacts every text taken from GitHub (or the host) before it enters the facts."""

    def __init__(self, live_values: Iterable[str] = ()):
        self.live = tuple(v for v in live_values if isinstance(v, str))
        self.hits = 0

    def text(self, value: Any, limit: int = MAX_TEXT) -> Optional[str]:
        if not isinstance(value, str):
            return None
        # CONTRACT NOTE: only the first limit + REDACT_MARGIN characters are redacted (and kept up to
        # limit); a token that starts before the limit still shows enough of itself to match its shape,
        # and a live value is matched by any 20-character window.
        clean, hits = core.redact(value[:limit + REDACT_MARGIN], self.live)
        self.hits += hits["live"] + hits["shape"]
        return clean[:limit]

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value, 4096)
        if isinstance(value, dict):
            return {self.text(str(k), 200): self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value


def _labels(issue: Dict[str, Any], red: _Redactor) -> List[str]:
    names = set()
    for label in _list(issue.get("labels")):
        name = label.get("name") if isinstance(label, dict) else label
        clean = red.text(name, 100)
        if clean:
            names.add(clean)
    return sorted(names)


def _reduce_issue(item: Any, red: _Redactor) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict) or "pull_request" in item or _pos_int(item.get("number")) is None:
        return None
    return {"number": item["number"], "state": red.text(item.get("state"), 20),
            "state_reason": red.text(item.get("state_reason"), 40), "labels": _labels(item, red),
            "closed_at": _when(item.get("closed_at")), "updated_at": _when(item.get("updated_at"))}


def _reduce_pull(item: Any, red: _Redactor) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict) or _pos_int(item.get("number")) is None:
        return None
    return {"number": item["number"], "state": red.text(item.get("state"), 20),
            "merged_at": _when(item.get("merged_at")), "merge_commit_sha": _sha(item.get("merge_commit_sha")),
            "head_sha": _sha(_dict(item.get("head")).get("sha")),
            "base_ref": red.text(_dict(item.get("base")).get("ref"), 200),
            "updated_at": _when(item.get("updated_at"))}


def _reduce_file(item: Any, red: _Redactor) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
        return None
    return {"filename": red.text(item["filename"], 1000), "status": red.text(item.get("status"), 20),
            "previous_filename": red.text(item.get("previous_filename"), 1000)}


def _reduce_run(item: Any, red: _Redactor) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        return None
    app = _dict(item.get("app"))
    # CONTRACT NOTE: the check-run id and app id are kept because the central latest_check_runs
    # picks the newest run per (app id, name) by id; facts_core drops the id and started_at.
    return {"id": item.get("id") if _is_int(item.get("id")) else None, "name": red.text(item["name"], 200),
            "status": red.text(item.get("status"), 20), "conclusion": red.text(item.get("conclusion"), 30),
            "started_at": _when(item.get("started_at")), "completed_at": _when(item.get("completed_at")),
            "app": {"id": app.get("id") if _is_int(app.get("id")) else None, "slug": red.text(app.get("slug"), 100)}}


# ---------------------------------------------------------------------------- state access

def _committed_etags(state: core.StateStore) -> Dict[str, Any]:
    data = state.read(ST_ETAGS, None)
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        return {"tokens": {}, "since": None}
    since = data.get("since") if _when(data.get("since")) else None
    return {"tokens": {k: v for k, v in data["tokens"].items() if isinstance(k, str) and isinstance(v, str)},
            "since": since}


def _recheck(state: core.StateStore) -> Dict[str, Any]:
    data = state.read(ST_RECHECK, None)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------- T0 probe

def lanes_hash(board: Optional[Dict[str, Any]], error: Optional[str] = None) -> str:
    """Canonical hash of the host lane board (or of its failure, so a steady failure is 'unchanged')."""
    if board is None:
        return core.sha256_hex(core.canon({"unknown": error or "HOST"}))
    return core.sha256_hex(core.canon(board))


def _lanes_fact(board: Dict[str, Any], red: _Redactor) -> Dict[str, Any]:
    lanes = []
    for entry in _list(board.get("lanes")):
        if not isinstance(entry, dict):
            continue
        active = []
        for row in _list(entry.get("active")):
            if isinstance(row, dict):
                active.append(red.obj({k: row.get(k) for k in ("request", "repository", "task", "role", "state",
                                                                "created") if k in row}))
        lanes.append({"lane": red.text(entry.get("lane"), 40), "enabled": entry.get("enabled") is True,
                      "active": active})
    return {"source": "HOST", "max_active_sessions": board.get("max_active_sessions"),
            "active_total": board.get("active_total"), "lanes": lanes}


def _abort_if_global(exc: InspectError) -> None:
    """Rate limits, network loss and path-policy errors fail the whole probe."""
    if exc.reason in ABORT_REASONS:
        raise exc


def probe(reader: Any, host: Any, config: Dict[str, Any], state: core.StateStore, now: datetime,
          live_values: Iterable[str] = ()) -> Dict[str, Any]:
    """T0: conditional GETs with committed ETags plus ``lanes()``; stages new ETags and bodies.

    Returns ``{"changed", "changed_keys", "lanes", "lanes_error", "lanes_hash", "control": {...},
    "targets": {repo: {...}}, "etags": staged tokens, "since", "github_calls", "host_calls"}``.
    Rate limits and network loss raise (the tick fails); one target's read error is recorded on
    that target and compared like a token, so a steady error does not count as a change.
    """
    red = _Redactor(live_values)
    committed = _committed_etags(state)
    old_tokens = committed["tokens"]
    old_bodies = _dict(state.read(ST_T0, None))
    repos_meta = _dict(state.read(ST_REPOS, None))
    since = committed["since"]
    tokens: Dict[str, str] = {}
    bodies: Dict[str, Any] = {}
    changed_keys: List[str] = []
    new_meta: Dict[str, Any] = {}
    gh_start = reader.calls
    host_start = getattr(host, "calls", 0)

    def cond(key: str, path: str, params: Optional[Dict[str, Any]], reduce: Optional[Callable[[Any], Any]]) -> Any:
        old_token = old_tokens.get(key)
        need_body = reduce is not None
        etag = old_token if old_token and not old_token.startswith("ERR:") and (
            not need_body or key in old_bodies) else None
        resp = reader.get(path, params, etag)
        if resp.status == 304:
            tokens[key] = resp.etag or old_token or ""
            body = old_bodies.get(key) if need_body else None
        elif resp.status == 404:
            raise InspectError("GITHUB_NOT_FOUND", f"{path} -> 404")
        else:
            changed_keys.append(key)
            tokens[key] = resp.etag or ("NOETAG:" + core.sha256_hex(core.canon(resp.json))[:16])
            body = reduce(resp.json) if need_body else None
        if need_body:
            bodies[key] = body
        return body

    def target_error(key: str, exc: InspectError) -> str:
        token = "ERR:" + exc.reason
        tokens[key] = token
        if old_tokens.get(key) != token:
            changed_keys.append(key)
        return exc.reason

    # -- control repository main
    ctrl = config["control_repository"]
    control: Dict[str, Any] = {"main_sha": None, "error": None}
    try:
        control["main_sha"] = cond(f"branch:{ctrl}", f"/repos/{ctrl}/branches/main", None,
                                   lambda j: _sha(_dict(_dict(j).get("commit")).get("sha")))
    except InspectError as exc:
        _abort_if_global(exc)
        control["error"] = target_error(f"err:{ctrl}", exc)

    # -- targets
    targets: Dict[str, Any] = {}
    for target in config["targets"]:
        repo = target["repository"]
        info: Dict[str, Any] = {"default_branch": None, "head_sha": None, "issues": None, "pulls": None,
                                "error": None}
        try:
            meta = _dict(repos_meta.get(repo))
            checked = _dt(meta.get("checked_at"))
            branch = meta.get("default_branch") if isinstance(meta.get("default_branch"), str) else None
            if branch is None or checked is None or now - checked >= REPO_META_TTL or checked > now:
                # CONTRACT NOTE: GET /repos/{r} is a daily unconditional refresh of the default branch;
                # it counts as a change only when the default branch itself changed.
                resp = reader.get(f"/repos/{repo}")
                if resp.status != 200:
                    raise InspectError("GITHUB_NOT_FOUND", f"/repos/{repo} -> {resp.status}")
                fresh = _dict(resp.json).get("default_branch")
                if not isinstance(fresh, str) or not BRANCH_RE.fullmatch(fresh) or ".." in fresh:
                    raise InspectError("GITHUB_JSON", "default_branch is missing or unusable")
                if fresh != branch:
                    changed_keys.append(f"repo:{repo}")
                branch, checked = fresh, now
            new_meta[repo] = {"default_branch": branch, "checked_at": core.iso(checked)}
            info["default_branch"] = branch
            info["head_sha"] = cond(f"branch:{repo}", f"/repos/{repo}/branches/{branch}", None,
                                    lambda j: _sha(_dict(_dict(j).get("commit")).get("sha")))
            info["issues"] = cond(f"issues:{repo}", f"/repos/{repo}/issues",
                                  {"labels": TASK_LABEL, "state": "all", "sort": "updated", "direction": "desc",
                                   "per_page": T0_ISSUES_PER_PAGE},
                                  lambda j: [i for i in (_reduce_issue(x, red) for x in _list(j)) if i])
            info["pulls"] = cond(f"pulls:{repo}", f"/repos/{repo}/pulls",
                                 {"state": "all", "sort": "updated", "direction": "desc",
                                  "per_page": T0_PULLS_PER_PAGE},
                                 lambda j: {"items": [p for p in (_reduce_pull(x, red) for x in _list(j)) if p],
                                            "full": len(_list(j)) >= T0_PULLS_PER_PAGE})
            params: Dict[str, Any] = {"per_page": T0_COMMENTS_PER_PAGE}
            if since:
                params = {"since": since, "per_page": T0_COMMENTS_PER_PAGE}
            cond(f"comments:{repo}", f"/repos/{repo}/issues/comments", params, None)
        except InspectError as exc:
            _abort_if_global(exc)
            info["error"] = target_error(f"err:{repo}", exc)
            # Keep committed meta and bodies untouched for a failed target.
            if repo in repos_meta:
                new_meta.setdefault(repo, repos_meta[repo])
        targets[repo] = info

    # -- host lane board (always)
    board: Optional[Dict[str, Any]] = None
    lanes_error: Optional[str] = None
    try:
        board = _lanes_fact(host.lanes(), red)
    except InspectError as exc:
        if exc.reason == "HOST_BUDGET":
            raise
        lanes_error = exc.reason
    lhash = lanes_hash(board, lanes_error)
    tokens["host:lanes"] = lhash
    if old_tokens.get("host:lanes") != lhash:
        changed_keys.append("host:lanes")
    bodies["host:lanes"] = {"board": board, "error": lanes_error}

    # -- stage (committed by commit_t1 or commit_unchanged)
    state.stage(ST_ETAGS, {"tokens": tokens, "since": since})
    state.stage(ST_T0, bodies)
    state.stage(ST_REPOS, new_meta)
    return {"changed": bool(changed_keys), "changed_keys": sorted(set(changed_keys)), "lanes": board,
            "lanes_error": lanes_error, "lanes_hash": lhash, "control": control, "targets": targets,
            "etags": dict(tokens), "since": since, "github_calls": reader.calls - gh_start,
            "host_calls": getattr(host, "calls", 0) - host_start}


def should_run_t1(probe_result: Dict[str, Any], state: core.StateStore, now: datetime) -> Tuple[bool, str]:
    """Early-exit rule: skip T1 only if the probe is unchanged, ``t1_dirty`` is false and the recheck
    time has not come. Returns ``(run, reason)``."""
    if probe_result.get("changed") is not False:
        return True, "CHANGED"
    recheck = _recheck(state)
    if recheck.get("t1_dirty") is not False:
        return True, "DIRTY"
    due = _dt(recheck.get("next_recheck_at"))
    if due is None or now >= due:
        return True, "RECHECK_DUE"
    return False, "UNCHANGED"


def commit_unchanged(state: core.StateStore) -> List[str]:
    """Commit the probe's staged ETags when T1 was skipped (nothing changed)."""
    state.discard_staged([ST_CACHE])
    return state.commit_staged(list(PROBE_STAGED))


# ---------------------------------------------------------------------------- T1 collection

class _Run:
    """One T1: budgets, caches and the guarded GitHub/host calls."""

    def __init__(self, reader: Any, host: Any, budgets: Budgets, cache: Dict[str, Any], red: _Redactor):
        self.reader = reader
        self.host = host
        self.budgets = budgets
        self.red = red
        self.started = budgets.clock()
        self.gh_start = reader.calls
        self.host_calls = 0
        self.host_down: Optional[str] = None
        self.read_errors: Set[str] = set()
        self.now: Optional[datetime] = None  # set by collect(); gates reuse of cached check runs
        self.old_cache = {k: _dict(cache.get(k)) for k in ("plan", "files", "checks", "commit_pulls", "control")}
        self.cache: Dict[str, Dict[str, Any]] = {k: {} for k in self.old_cache}

    # -- budgets
    def _time(self) -> None:
        if self.budgets.clock() - self.started > self.budgets.seconds:
            raise InspectError("T1_TIMEOUT", f"T1 exceeded {self.budgets.seconds:.0f}s")

    def github_calls(self) -> int:
        return self.reader.calls - self.gh_start

    def _gh_budget(self) -> None:
        self._time()
        if self.github_calls() >= self.budgets.github_calls:
            raise InspectError("GITHUB_BUDGET", f"T1 exceeded {self.budgets.github_calls} GitHub requests")

    def note(self, reason: Optional[str]) -> None:
        """Remember a read failure of this T1 (see READ_FAILURE_REASONS)."""
        if reason in READ_FAILURE_REASONS:
            self.read_errors.add(reason)

    def _read(self, path: str, params: Optional[Dict[str, Any]], forbidden: Optional[str] = None) -> Any:
        """``forbidden``: the steady data reason a plain 403 (not a rate limit) on this path is raised as,
        instead of a read failure (a read token without that permission gets it on every T1)."""
        try:
            return self.reader.get(path, params)
        except InspectError as exc:
            if forbidden and exc.reason == "GITHUB_READ" and exc.detail.endswith(" -> 403"):
                raise InspectError(forbidden, exc.detail) from None
            self.note(exc.reason)
            raise

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._gh_budget()
        return self._read(path, params)

    def pages(self, path: str, params: Dict[str, Any], max_pages: int, key: Optional[str] = None,
              forbidden: Optional[str] = None) -> Tuple[List[Any], bool]:
        """Paginate page by page so the request budget is checked before every page."""
        items: List[Any] = []
        for page in range(1, max_pages + 1):
            self._gh_budget()
            resp = self._read(path, dict(params, page=page), forbidden)
            if resp.status != 200:
                self.note("GITHUB_READ")
                raise InspectError("GITHUB_READ", f"GET {path} -> {resp.status} while paginating")
            data = resp.json.get(key) if key and isinstance(resp.json, dict) else resp.json
            if not isinstance(data, list):
                self.note("GITHUB_JSON")
                raise InspectError("GITHUB_JSON", f"GET {path}: page is not a list")
            items.extend(data)
            per_page = params.get("per_page", 30)
            if len(data) < per_page:
                return items, False
        return items, True

    def host_call(self, verb: Callable[..., Dict[str, Any]], *args: str) -> Dict[str, Any]:
        self._time()
        if self.host_down:
            raise InspectError(self.host_down, "host unavailable earlier in this T1")
        if self.host_calls >= self.budgets.host_calls:
            raise InspectError("HOST_BUDGET", f"T1 exceeded {self.budgets.host_calls} host calls")
        self.host_calls += 1
        try:
            return verb(*args)
        except InspectError as exc:
            if exc.reason in HOST_DOWN_REASONS:
                self.host_down = exc.reason
            self.note(exc.reason)
            raise

    # -- caches by immutable SHA
    def cached(self, section: str, key: str) -> Any:
        if key in self.cache[section]:
            return self.cache[section][key]
        if key in self.old_cache[section]:
            self.cache[section][key] = self.old_cache[section][key]
            return self.cache[section][key]
        return None

    def keep(self, section: str, key: str, value: Any) -> None:
        self.cache[section][key] = value


def _guard(unknown: Set[str], group: str, fn: Callable[[], Any], default: Any = None) -> Any:
    """Run one collection step; a non-global failure marks ``group`` UNKNOWN and returns ``default``."""
    try:
        return fn()
    except InspectError as exc:
        if exc.reason in ABORT_REASONS:
            raise
        unknown.add(group)
        return default
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        unknown.add(group)
        return default


def _content_json(run: _Run, repo: str, path: str, ref: str) -> Optional[Any]:
    """JSON content of ``path`` at ``ref`` (contents API); None on 404. Raises ValueError on bad content."""
    resp = run.get(f"/repos/{repo}/contents/{path}", {"ref": ref})
    if resp.status == 404:
        return None
    data = _dict(resp.json)
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str) or not data["content"]:
        raise ValueError("content is not inline base64")
    return core.loads_strict(base64.b64decode(data["content"]).decode("utf-8"))


def _collect_control(run: _Run, config: Dict[str, Any], main_sha: Optional[str],
                     unknown: Set[str]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Control facts and the parsed projects.json (None when it could not be read)."""
    ctrl = config["control_repository"]
    control: Dict[str, Any] = {"main_sha": main_sha, "runtime_enabled": None, "activated_runtime_sha": None,
                               "runtime_runs": None, "sources": dict(CONTROL_SOURCES)}
    if main_sha is None:
        unknown.add("control")
        return control, None

    def files() -> Tuple[Dict[str, Any], Dict[str, Any]]:
        cached = run.cached("control", main_sha)
        if isinstance(cached, dict):
            return cached["projects"], cached["activation"]
        projects = _content_json(run, ctrl, CTRL_PROJECTS_PATH, main_sha)
        activation = _content_json(run, ctrl, CTRL_ACTIVATION_PATH, main_sha)
        if not isinstance(projects, dict) or not isinstance(activation, dict):
            raise ValueError("projects.json or activation.json missing")
        reduced = {repo: {"project": _dict(p).get("project"),
                          "program_required_checks": _dict(p).get("program_required_checks")}
                   for repo, p in projects.items() if isinstance(repo, str)}
        reduced = run.red.obj(reduced)
        act = {"runtime_enabled": activation.get("runtime_enabled") if isinstance(
            activation.get("runtime_enabled"), bool) else None,
            "activated_runtime_sha": _sha(activation.get("activated_runtime_sha"))}
        run.keep("control", main_sha, {"projects": reduced, "activation": act})
        return reduced, act

    pair = _guard(unknown, "control", files)
    projects = None
    if pair is not None:
        projects, act = pair
        control.update(act)

    def runs() -> List[Dict[str, Any]]:
        resp = run.get(f"/repos/{ctrl}/actions/workflows/{RUNTIME_WORKFLOW}/runs", {"per_page": RUNTIME_RUNS})
        if resp.status != 200:
            run.note("GITHUB_READ")
            raise InspectError("GITHUB_READ", "runtime workflow runs unavailable")
        out = []
        for item in _list(_dict(resp.json).get("workflow_runs"))[:RUNTIME_RUNS]:
            if isinstance(item, dict) and _pos_int(item.get("id")):
                out.append({"id": item["id"], "head_sha": _sha(item.get("head_sha")),
                            "conclusion": run.red.text(item.get("conclusion"), 30),
                            "created_at": _when(item.get("created_at"))})
        return sorted(out, key=lambda r: r["id"], reverse=True)

    control["runtime_runs"] = _guard(unknown, "control", runs)
    return control, projects


def _plan_fact(run: _Run, repo: str, branch: str, project: Optional[str], now: datetime,
               unknown: Set[str]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """The committed plan on the default branch: PRESENT / NONE / INVALID (never an exception)."""
    plan: Dict[str, Any] = {"state": "UNKNOWN", "program": None, "plan_commit": None, "committed_at": None,
                            "nodes": [], "plan_commits_7d": None, "pending_plan_prs": [], "source": "SHA_CONTENT"}

    def latest() -> Optional[Dict[str, Any]]:
        resp = run.get(f"/repos/{repo}/commits", {"path": PLAN_PATH, "sha": branch, "per_page": 1})
        if resp.status != 200:
            run.note("GITHUB_READ")
            raise InspectError("GITHUB_READ", "plan commits unavailable")
        items = _list(resp.json)
        if not items:
            return None
        commit = _dict(items[0])
        sha = _sha(commit.get("sha"))
        if sha is None:
            raise ValueError("plan commit sha missing")
        committer = _dict(_dict(commit.get("commit")).get("committer"))
        return {"sha": sha, "date": _when(committer.get("date"))}

    marker = object()
    head = _guard(unknown, "plan", latest, marker)
    if head is marker:
        return plan, None
    if head is None:
        plan["state"] = "NONE"
        return plan, None
    plan["plan_commit"] = head["sha"]
    plan["committed_at"] = head["date"]

    def count_7d() -> int:
        items, _ = run.pages(f"/repos/{repo}/commits",
                             {"path": PLAN_PATH, "sha": branch, "since": core.iso(now - WINDOW_7D), "per_page": 100},
                             1)
        return len(items)

    plan["plan_commits_7d"] = _guard(unknown, "plan", count_7d)

    cache_key = f"{repo}@{head['sha']}@{project}"
    parsed = run.cached("plan", cache_key)
    if not isinstance(parsed, dict):
        def content() -> Dict[str, Any]:
            try:
                raw = _content_json(run, repo, PLAN_PATH, head["sha"])
            except (ValueError, UnicodeDecodeError):
                return {"state": "INVALID", "reason": "plan file is not readable JSON"}
            if raw is None:
                return {"state": "NONE"}
            # CONTRACT NOTE: when projects.json could not be read the plan's own project is used, so the
            # repository check still applies; an unregistered target is INVALID (the runtime refuses it).
            cfg = {"repository": repo, "project": project if project is not None else _dict(raw).get("project")}
            try:
                valid = cp_program.validate_plan(copy.deepcopy(raw), cfg)
            except (cp_program.ProgramError, KeyError, TypeError, ValueError, AttributeError) as exc:
                return {"state": "INVALID", "reason": run.red.text(str(exc), 200) or "invalid plan"}
            nodes = [{"id": n["id"], "title": run.red.text(n.get("title"), 200),
                      "depends_on": [d for d in n.get("depends_on", []) if isinstance(d, str)],
                      "audit_floor": n["audit_floor"], "astra_gate": n["astra_gate"]} for n in valid["nodes"]]
            return {"state": "PRESENT", "program": valid["program"], "nodes": nodes}

        parsed = _guard(unknown, "plan", content)
        if parsed is None:
            return plan, None
        run.keep("plan", cache_key, parsed)
    plan["state"] = parsed["state"]
    if parsed["state"] == "INVALID":
        plan["invalid_reason"] = parsed.get("reason")
    if parsed["state"] == "PRESENT":
        plan["program"] = parsed["program"]
        plan["nodes"] = copy.deepcopy(parsed["nodes"])
        return plan, parsed
    return plan, None


def _pr_files(run: _Run, repo: str, number: int, head: Optional[str]) -> List[Dict[str, Any]]:
    """Files of a PR (filename, status, previous_filename), cached by PR + head SHA."""
    key = f"{repo}#{number}@{head}"
    if head is not None:
        cached = run.cached("files", key)
        if isinstance(cached, list):
            return cached
    items, truncated = run.pages(f"/repos/{repo}/pulls/{number}/files", {"per_page": FILES_PER_PAGE},
                                 MAX_PR_FILES // FILES_PER_PAGE)
    files = sorted((f for f in (_reduce_file(x, run.red) for x in items) if f), key=lambda f: f["filename"] or "")
    if truncated:
        # CONTRACT NOTE: GitHub lists at most 3000 files; a list cut there is not a complete fact.
        raise InspectError("FILES_TRUNCATED", f"PR {number} lists more than {MAX_PR_FILES} files")
    if head is not None:
        run.keep("files", key, files)
    return files


def _checks_final(required: Sequence[str], runs: List[Dict[str, Any]]) -> bool:
    """Whether a run list can no longer change what it says: the required checks PASS, or, with no
    required checks configured, every run is completed."""
    state = cp_program.required_checks_state(required, runs)
    if state == "NOT_CONFIGURED":
        return bool(runs) and all(r.get("status") == "completed" for r in runs)
    return state == "PASS"


def _check_runs(run: _Run, repo: str, sha: str, required: Sequence[str] = (),
                merged_at: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Check runs at one commit, cached by SHA once final (see ``_checks_final``)."""
    # CONTRACT NOTE: "cache by SHA once all completed" is narrowed to a final answer: a required check
    # not created yet reads PENDING and a FAIL can be re-run, so only a PASS of the current required list
    # is cached. A PASS can also be re-run into a FAIL at the same SHA, so a cached list is reused only for
    # a merge older than the 7-day window (and only while it still PASSes the current required list);
    # inside the window every T1 reads the check runs again.
    key = f"{repo}@{sha}"
    cached = run.cached("checks", key)
    settled = merged_at is not None and run.now is not None and run.now - merged_at > WINDOW_7D
    if settled and isinstance(cached, list) and _checks_final(required, cached):
        return cached
    run.cache["checks"].pop(key, None)
    # A read token without the Checks permission (INSPECTOR.md §12.7) gets a plain 403 here on every
    # T1: CHECKS_FORBIDDEN is a steady data fact ("checks" UNKNOWN), not a read failure.
    items, truncated = run.pages(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100}, MAX_CHECK_PAGES,
                                 key="check_runs", forbidden="CHECKS_FORBIDDEN")
    if truncated:
        raise InspectError("CHECKS_TRUNCATED", "too many check runs")
    runs = sorted((r for r in (_reduce_run(x, run.red) for x in items) if r),
                  key=lambda r: (r["name"] or "", r["id"] or 0))
    if _checks_final(required, runs):
        run.keep("checks", key, runs)
    return runs


def _delivery_pr(run: _Run, repo: str, number: int) -> Optional[Dict[str, Any]]:
    resp = run.get(f"/repos/{repo}/pulls/{number}")
    if resp.status == 404:
        raise InspectError("DELIVERY_PR_MISSING", f"pinned PR {number} not found")
    data = _dict(resp.json)
    if _pos_int(data.get("number")) is None:
        raise ValueError("pull without number")
    return {"number": data["number"], "state": run.red.text(data.get("state"), 20),
            "merged": data.get("merged") is True, "merged_at": _when(data.get("merged_at")),
            "merge_commit_sha": _sha(data.get("merge_commit_sha")),
            "head": {"sha": _sha(_dict(data.get("head")).get("sha"))},
            "base": {"ref": run.red.text(_dict(data.get("base")).get("ref"), 200)},
            "updated_at": _when(data.get("updated_at"))}


def _blocked_since(run: _Run, repo: str, number: int, labels: Sequence[str]) -> Tuple[str, Dict[str, str]]:
    """Earliest of the times each current blocking label was last added (issue events, 3 pages), and
    those times per label (``label_since``; S7 reads the age of needs-lane-cleanup itself)."""
    current = [label for label in labels if label in BLOCKING_LABELS]
    items, truncated = run.pages(f"/repos/{repo}/issues/{number}/events", {"per_page": 100}, MAX_EVENT_PAGES)
    if truncated:
        # CONTRACT NOTE: events come oldest first; a list cut at 3 pages may miss the latest re-add of a
        # label, so its age would be overstated: "events" is UNKNOWN instead.
        raise InspectError("EVENTS_TRUNCATED", f"issue {number} has more than {MAX_EVENT_PAGES} pages of events")
    last_added: Dict[str, datetime] = {}
    for event in items:
        if not isinstance(event, dict) or event.get("event") != "labeled":
            continue
        name = _dict(event.get("label")).get("name")
        when = _dt(event.get("created_at"))
        if name in current and when is not None and (name not in last_added or when > last_added[name]):
            last_added[name] = when
    if not last_added:
        # CONTRACT NOTE: a blocking label with no labeled event in 3 pages has an unknown age.
        raise InspectError("EVENTS_MISSING", "no labeled event for a current blocking label")
    return core.iso(min(last_added.values())), {name: core.iso(when) for name, when in sorted(last_added.items())}


def _node_issue(run: _Run, repo: str, number: Optional[int], t0_issues: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if number is None:
        return None
    issue = t0_issues.get(number)
    if issue is None:
        # CONTRACT NOTE: an issue older than the T0 page (50 most recently updated) is read directly.
        resp = run.get(f"/repos/{repo}/issues/{number}")
        if resp.status == 404:
            return None
        issue = _reduce_issue(resp.json, run.red)
        if issue is None:
            raise ValueError("issue unreadable")
    return {k: issue.get(k) for k in ("number", "state", "state_reason", "labels", "closed_at")}


def _exceptions(run: _Run, repo: str, now: datetime) -> Dict[int, int]:
    """APPROVED_SMALL_EXCEPTION consult results per issue over the last 14 days (GH_TEXT, claimed)."""
    items, truncated = run.pages(f"/repos/{repo}/issues/comments",
                                 {"since": core.iso(now - WINDOW_14D), "per_page": 100}, MAX_EXCEPTION_PAGES)
    if truncated:
        # CONTRACT NOTE: comments come oldest first; a list cut at 5 pages misses the newest results, so
        # the count is incomplete: "comments" is UNKNOWN instead of an undercount.
        raise InspectError("EXCEPTIONS_TRUNCATED", f"more than {MAX_EXCEPTION_PAGES} pages of comments in 14 days")
    counts: Dict[int, int] = {}
    for comment in items:
        if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
            continue
        if not EXCEPTION_RE.search(comment["body"]):
            continue
        match = ISSUE_URL_NUMBER_RE.search(str(comment.get("issue_url") or ""))
        if match:
            number = int(match.group(1))
            counts[number] = counts.get(number, 0) + 1
    return counts


def _merged_7d_pulls(run: _Run, repo: str, branch: str, t0_pulls: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """Pulls updated within 7 days: the T0 page, extended up to 5 pages when it does not reach back."""
    items = list(_list(t0_pulls.get("items")))
    cutoff = now - WINDOW_7D
    reaches = not t0_pulls.get("full") or any((_dt(p.get("updated_at")) or now) < cutoff for p in items)
    page = 2
    while not reaches and page <= MAX_PULL_PAGES:
        resp = run.get(f"/repos/{repo}/pulls", {"state": "all", "sort": "updated", "direction": "desc",
                                                "per_page": T0_PULLS_PER_PAGE, "page": page})
        if resp.status != 200 or not isinstance(resp.json, list):
            run.note("GITHUB_READ")
            raise InspectError("GITHUB_READ", f"pulls page {page} unavailable")
        data = resp.json
        items.extend(p for p in (_reduce_pull(x, run.red) for x in data) if p)
        reaches = len(data) < T0_PULLS_PER_PAGE or any((_dt(_dict(x).get("updated_at")) or now) < cutoff
                                                       for x in data)
        page += 1
    if not reaches:
        # CONTRACT NOTE: more pulls were updated in 7 days than MAX_PULL_PAGES pages hold; an unread pull
        # could be an orphan merge, so "pulls" is UNKNOWN instead of a silent undercount.
        raise InspectError("PULLS_TRUNCATED", f"more than {MAX_PULL_PAGES} pages of pulls updated in 7 days")
    seen: Dict[int, Dict[str, Any]] = {}
    for pull in items:
        seen.setdefault(pull["number"], pull)
    return list(seen.values())


def _empty_product(prefix: str, default_branch: Optional[str], head_sha: Optional[str]) -> Dict[str, Any]:
    return {"prefix": prefix, "default_branch": default_branch, "head_sha": head_sha, "required_checks": None,
            "required_checks_shrank": False,
            "plan": {"state": "UNKNOWN", "program": None, "plan_commit": None, "committed_at": None, "nodes": [],
                     "plan_commits_7d": None, "pending_plan_prs": [], "source": "SHA_CONTENT"},
            "nodes": {}, "merged_7d": [], "direct_pushes_7d": [], "unknown": [],
            "sources": dict(PRODUCT_SOURCES)}


def _collect_product(run: _Run, repo: str, prefix: str, info: Dict[str, Any], projects: Optional[Dict[str, Any]],
                     baseline: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    branch, head = info.get("default_branch"), info.get("head_sha")
    product = _empty_product(prefix, branch, head)
    unknown: Set[str] = set()
    if info.get("error") or not branch:
        product["unknown"] = list(PRODUCT_GROUPS)
        return product

    # -- required checks (projects.json at control main)
    project: Optional[str] = None
    if projects is None:
        unknown.add("required_checks")
    else:
        profile = _dict(projects.get(repo))
        project = profile.get("project") if isinstance(profile.get("project"), str) else ""
        names = profile.get("program_required_checks")
        required = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
        product["required_checks"] = required
        first = baseline.get(repo)
        if isinstance(first, list):
            product["required_checks_shrank"] = bool(set(first) - set(required))

    # -- plan
    plan, parsed = _plan_fact(run, repo, branch, project, now, unknown)
    product["plan"] = plan

    t0_pulls = _dict(info.get("pulls"))
    pulls = [p for p in _list(t0_pulls.get("items")) if isinstance(p, dict)]
    if info.get("pulls") is None:
        unknown.add("pulls")
    t0_issue_list = info.get("issues")
    if t0_issue_list is None:
        unknown.add("issues")
    t0_issues = {i["number"]: i for i in _list(t0_issue_list) if isinstance(i, dict)}

    # -- pending plan PRs: open PRs touching .aiops/program.json (cap 20)
    pending = []
    for pull in [p for p in pulls if p.get("state") == "open"][:MAX_PENDING_PLAN_PRS]:
        files = _guard(unknown, "pulls", lambda p=pull: _pr_files(run, repo, p["number"], p.get("head_sha")))
        if files and any(PLAN_PATH in (f.get("filename"), f.get("previous_filename")) for f in files):
            pending.append({"number": pull["number"], "head_sha": pull.get("head_sha"),
                            "updated_at": pull.get("updated_at")})
    plan["pending_plan_prs"] = sorted(pending, key=lambda p: p["number"])

    # -- nodes
    required_list = product["required_checks"] or []
    delivery_prs: Set[int] = set()
    if parsed is not None:
        program = parsed["program"]
        for node in parsed["nodes"]:
            product["nodes"][node["id"]] = _collect_node(run, repo, program, node["id"], required_list,
                                                         t0_issues, unknown, delivery_prs)

    # -- small exceptions (14 days) per node
    if parsed is not None:
        counts = _guard(unknown, "comments", lambda: _exceptions(run, repo, now))
        for node in product["nodes"].values():
            number = _pos_int(_dict(node.get("materialization")).get("issue"))
            node["exceptions_14d"] = counts.get(number, 0) if counts is not None and number else 0

    # -- merged PRs in 7 days on the default branch
    merged: List[Dict[str, Any]] = []
    all_pulls = _guard(unknown, "pulls", lambda: _merged_7d_pulls(run, repo, branch, t0_pulls, now), pulls)
    known_merges: Set[str] = set()
    for pull in all_pulls:
        if pull.get("merge_commit_sha") and pull.get("merged_at"):
            known_merges.add(pull["merge_commit_sha"])
        merged_at = _dt(pull.get("merged_at"))
        if merged_at is None or now - merged_at > WINDOW_7D or pull.get("base_ref") != branch:
            continue
        files = _guard(unknown, "files", lambda p=pull: _pr_files(run, repo, p["number"], p.get("head_sha")))
        if files is None:
            # CONTRACT NOTE: without its files a merged PR cannot be classified (aiops_only), so the
            # orphan count is incomplete: "pulls" is UNKNOWN too.
            unknown.add("pulls")
        merged.append({"number": pull["number"], "merged_at": pull.get("merged_at"),
                       "merge_commit_sha": pull.get("merge_commit_sha"), "head_sha": pull.get("head_sha"),
                       "pinned": pull["number"] in delivery_prs,
                       "aiops_only": bool(files) and all(str(f.get("filename") or "").startswith(AIOPS_DIR)
                                                         for f in files),
                       "files": files})
    product["merged_7d"] = sorted(merged, key=lambda p: p["number"])

    # -- direct pushes on the default branch in 7 days
    def pushes() -> List[Dict[str, Any]]:
        commits, truncated = run.pages(f"/repos/{repo}/commits", {"sha": branch, "since": core.iso(now - WINDOW_7D),
                                                                   "per_page": DIRECT_PUSH_PER_PAGE},
                                       MAX_COMMIT_PAGES)
        if truncated:
            # CONTRACT NOTE: an unread commit could be a direct push: "commits" is UNKNOWN instead.
            raise InspectError("COMMITS_TRUNCATED", f"more than {MAX_COMMIT_PAGES} pages of commits in 7 days")
        out = []
        for item in commits:
            sha = _sha(_dict(item).get("sha"))
            if sha is None or sha in known_merges:
                continue
            key = f"{repo}@{sha}"
            numbers = run.cached("commit_pulls", key)
            if not isinstance(numbers, list):
                listed = run.get(f"/repos/{repo}/commits/{sha}/pulls")
                if listed.status != 200 or not isinstance(listed.json, list):
                    run.note("GITHUB_READ")
                    raise InspectError("GITHUB_READ", "commit pulls unavailable")
                numbers = sorted(n for n in (_pos_int(_dict(p).get("number")) for p in listed.json) if n)
                run.keep("commit_pulls", key, numbers)
            if not numbers:
                date = _when(_dict(_dict(_dict(item).get("commit")).get("committer")).get("date"))
                out.append({"sha": sha, "date": date})
        return sorted(out, key=lambda c: c["sha"])

    product["direct_pushes_7d"] = _guard(unknown, "commits", pushes, [])
    product["unknown"] = sorted(unknown)
    return product


def _collect_node(run: _Run, repo: str, program: str, node_id: str, required: List[str],
                  t0_issues: Dict[int, Dict[str, Any]], unknown: Set[str], delivery_prs: Set[int]) -> Dict[str, Any]:
    task_id = cp_program.task_id_for(program, node_id)
    node: Dict[str, Any] = {"task_id": task_id, "materialization": None, "rows": None, "delivery_pr": None,
                            "merge_runs": None, "delivery_files": None, "completion": None, "issue": None,
                            "blocked_since": None, "label_since": None, "exceptions_14d": 0}
    mstatus = _guard(unknown, "host", lambda: run.red.obj(run.host_call(run.host.materialize_status, program,
                                                                        node_id)))
    if mstatus is None:
        return node
    node["materialization"] = mstatus
    rows: Optional[List[Dict[str, Any]]] = None
    pr: Optional[Dict[str, Any]] = None
    merge_runs: Optional[List[Dict[str, Any]]] = None
    complete = True
    if mstatus.get("status") == "CREATED" and mstatus.get("repository") == repo:
        report = _guard(unknown, "host", lambda: run.host_call(run.host.task_status, repo, task_id))
        if report is None:
            return node
        rows = run.red.obj([r for r in _list(report.get("rows")) if isinstance(r, dict)])
        node["rows"] = rows
        for row in cp_program.writer_rows(rows):
            pinned = _pos_int(_dict(cp_program.pin_of(row, "DELIVERY")).get("pr"))
            if pinned:
                # CONTRACT NOTE: every released writer's DELIVERY pin counts as "pinned" for merged_7d,
                # not only the current writer's, so a resumed task never shows its earlier PR as an orphan.
                delivery_prs.add(pinned)
        pin = cp_program.pin_of(cp_program.current_writer(rows), "DELIVERY")
        number = _pos_int(_dict(pin).get("pr"))
        if pin is not None and number is None:
            unknown.add("delivery")
            complete = False
        elif number is not None:
            pr = _guard(unknown, "delivery", lambda: _delivery_pr(run, repo, number))
            if pr is None:
                complete = False
            else:
                node["delivery_pr"] = pr
                head = _dict(pr.get("head")).get("sha")
                node["delivery_files"] = _guard(unknown, "files", lambda: _pr_files(run, repo, number, head))
                if pr.get("merged") and pr.get("merge_commit_sha"):
                    merge_runs = _guard(unknown, "checks",
                                        lambda: _check_runs(run, repo, pr["merge_commit_sha"], required,
                                                            _dt(pr.get("merged_at"))))
                    node["merge_runs"] = merge_runs
    if complete:
        node["completion"] = cp_program.node_completion(mstatus, repo, rows, pr, required, merge_runs)
    # -- issue (GH_TEXT) and blocked time
    number = _pos_int(mstatus.get("issue"))
    issue = _guard(unknown, "issues", lambda: _node_issue(run, repo, number, t0_issues))
    node["issue"] = issue
    if issue and issue.get("state") == "open" and any(l in BLOCKING_LABELS for l in issue.get("labels") or []):
        # CONTRACT NOTE: events are read only for OPEN issues; a closed issue's labels block nothing.
        blocked = _guard(unknown, "events", lambda: _blocked_since(run, repo, issue["number"], issue["labels"]))
        if blocked is not None:
            node["blocked_since"], node["label_since"] = blocked
    return node


def _ledger(run: _Run, config: Dict[str, Any], ledger_ids: Iterable[Any]) -> Dict[str, Any]:
    ctrl = config["control_repository"]
    ids = sorted({i for i in ledger_ids if _pos_int(i)})[-MAX_LEDGER_IDS:]
    comments = []
    for comment_id in ids:
        resp = run.get(f"/repos/{ctrl}/issues/comments/{comment_id}")
        if resp.status == 404:
            comments.append({"id": comment_id, "body_sha256": None, "updated_at": None, "deleted": True})
            continue
        data = _dict(resp.json)
        body = data.get("body") if isinstance(data.get("body"), str) else ""
        # The body is hashed as GitHub returns it and never stored.
        comments.append({"id": comment_id, "body_sha256": core.sha256_hex(body.encode("utf-8")),
                         "updated_at": _when(data.get("updated_at")), "deleted": False})
    return {"source": "GH_SYSTEM", "comments": comments}


def _probe_view(state: core.StateStore) -> Dict[str, Any]:
    """Rebuild the T0 view from the staged (else committed) bodies when no probe result is passed."""
    bodies = state.read_staged(ST_T0, None)
    if not isinstance(bodies, dict):
        bodies = _dict(state.read(ST_T0, None))
    meta = state.read_staged(ST_REPOS, None)
    if not isinstance(meta, dict):
        meta = _dict(state.read(ST_REPOS, None))
    targets = {}
    for key, value in bodies.items():
        if key.startswith("branch:"):
            repo = key[len("branch:"):]
            targets.setdefault(repo, {})["head_sha"] = value
    out_targets = {}
    for repo, info in targets.items():
        out_targets[repo] = {"default_branch": _dict(meta.get(repo)).get("default_branch"),
                             "head_sha": info.get("head_sha"), "issues": bodies.get(f"issues:{repo}"),
                             "pulls": bodies.get(f"pulls:{repo}"), "error": None}
    lanes = _dict(bodies.get("host:lanes"))
    return {"targets": out_targets, "lanes": lanes.get("board"), "lanes_error": lanes.get("error")}


def collect(reader: Any, host: Any, config: Dict[str, Any], state: core.StateStore, now: datetime,
            budgets: Optional[Budgets] = None, *, probe_result: Optional[Dict[str, Any]] = None,
            ledger_ids: Iterable[Any] = (), live_values: Iterable[str] = ()) -> Dict[str, Any]:
    """T1: build AIOPS_INSPECT_FACTS_V1. Raises InspectError only for aborting failures (budgets,
    rate limit, network); the caller then runs ``fail_t1``. Stages the SHA caches and the primed
    comment ETags; ``commit_t1`` commits them.

    ``ledger_ids``: comment ids of the inspector's own journaled ledger comments (S0).
    """
    budgets = budgets or Budgets()
    red = _Redactor(live_values)
    cache = _dict(state.read(ST_CACHE, None))
    run = _Run(reader, host, budgets, cache, red)
    run.now = now
    view = probe_result if isinstance(probe_result, dict) else _probe_view(state)
    ctrl = config["control_repository"]
    top_unknown: Set[str] = set()
    # Read failures the probe already met (target, control branch, lane board) count for this T1 too.
    run.note(view.get("lanes_error"))
    run.note(_dict(view.get("control")).get("error"))
    for info in _dict(view.get("targets")).values():
        run.note(_dict(info).get("error"))

    ctrl_info = _dict(view.get("control"))
    main_sha = ctrl_info.get("main_sha")
    if main_sha is None and not ctrl_info:
        bodies = state.read_staged(ST_T0, None)
        if not isinstance(bodies, dict):
            bodies = _dict(state.read(ST_T0, None))
        main_sha = bodies.get(f"branch:{ctrl}")
    control, projects = _collect_control(run, config, _sha(main_sha), top_unknown)

    board = view.get("lanes")
    if isinstance(board, dict):
        lanes: Optional[Dict[str, Any]] = copy.deepcopy(board)
    else:
        lanes = None
        top_unknown.add("lanes")

    baseline = _dict(state.read(ST_BASELINE, None))
    products: Dict[str, Any] = {}
    targets = _dict(view.get("targets"))
    for target in config["targets"]:
        repo = target["repository"]
        info = _dict(targets.get(repo))
        if not info:
            info = {"error": "NOT_PROBED"}
        products[repo] = _collect_product(run, repo, target["prefix"], info, projects, baseline, now)

    ledger = _guard(top_unknown, "ledger", lambda: _ledger(run, config, ledger_ids))

    # Prime the comments cursor: the next probe asks for comments since this T1 with a fresh ETag.
    staged = state.read_staged(ST_ETAGS, None)
    etags = staged if isinstance(staged, dict) and isinstance(staged.get("tokens"), dict) else _committed_etags(state)
    tokens = dict(etags["tokens"])
    since = core.iso(now)
    for target in config["targets"]:
        repo = target["repository"]
        key = f"comments:{repo}"
        tokens.pop(key, None)
        if _dict(targets.get(repo)).get("error"):
            continue
        # A failed priming read only drops the token: the next probe then sees a change and collects again.
        resp = _guard(set(), "comments", lambda r=repo: run.get(f"/repos/{r}/issues/comments",
                                                                 {"since": since, "per_page": T0_COMMENTS_PER_PAGE}))
        if resp is not None and resp.status in (200, 304):
            tokens[key] = resp.etag or ("NOETAG:" + core.sha256_hex(core.canon(resp.json))[:16])
    state.stage(ST_ETAGS, {"tokens": tokens, "since": since})
    state.stage(ST_CACHE, run.cache)

    facts: Dict[str, Any] = {"schema": FACTS_SCHEMA, "collected_at": core.iso(now), "control": control,
                             "lanes": lanes, "products": products, "ledger": ledger,
                             "unknown": sorted(top_unknown),
                             "stats": {"github_calls": run.github_calls(), "host_calls": run.host_calls,
                                       "seconds": round(max(0.0, budgets.clock() - run.started), 3),
                                       "redactions": red.hits, "read_errors": sorted(run.read_errors)}}
    return facts


# ---------------------------------------------------------------------------- hashing

def _bucket(age_hours: Optional[float], thresholds: Sequence[float]) -> Optional[int]:
    """Number of thresholds the age has reached (0 = below the first)."""
    if age_hours is None:
        return None
    return sum(1 for t in sorted(thresholds) if age_hours >= t)


def _hours_since(now: datetime, value: Any) -> Optional[float]:
    when = _dt(value)
    return None if when is None else max(0.0, (now - when).total_seconds() / 3600.0)


def _thresholds(config_or_thresholds: Optional[Dict[str, Any]]) -> Dict[str, int]:
    merged = dict(core.THRESHOLD_DEFAULTS)
    source = _dict(config_or_thresholds)
    overrides = source.get("thresholds") if isinstance(source.get("thresholds"), dict) else source
    for key, value in _dict(overrides).items():
        if key in merged and _is_int(value) and value > 0:
            merged[key] = value
    return merged


def _bucket_sets(th: Dict[str, int]) -> Dict[str, Tuple[float, ...]]:
    return {"session": (float(th["confirmed_watch_h"]), float(th["confirmed_at_risk_h"])),
            "blocked": tuple(sorted({float(th["blocked_watch_h"]), float(th["cleanup_watch_h"]), 72.0})),
            "done_gap": (24.0 * th["done_gap_watch_d"], 24.0 * th["done_gap_at_risk_d"]),
            "pending": (float(th["pending_checks_watch_h"]),)}


def _done_gap_base(product: Dict[str, Any]) -> Optional[str]:
    """Latest merged_at of DONE nodes' delivery PRs, else the plan commit time; None when all are DONE."""
    nodes = _dict(product.get("nodes"))
    plan = _dict(product.get("plan"))
    if plan.get("state") != "PRESENT" or not nodes:
        return None
    stages = {nid: _dict(n.get("completion")).get("stage") for nid, n in nodes.items() if isinstance(n, dict)}
    if stages and all(s == "DONE" for s in stages.values()):
        return None
    done = [_dt(_dict(nodes[nid].get("delivery_pr")).get("merged_at")) for nid, s in stages.items() if s == "DONE"]
    done = [d for d in done if d is not None]
    if done:
        return core.iso(max(done))
    return plan.get("committed_at")


def _drop(obj: Any, keys: Iterable[str]) -> Any:
    keys = set(keys)
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k not in keys}
    return obj


def _product_core(product: Dict[str, Any], now: datetime, buckets: Dict[str, Tuple[float, ...]]) -> Dict[str, Any]:
    out = copy.deepcopy(product)
    plan = _dict(out.get("plan"))
    plan["pending_plan_prs"] = [_drop(p, ("updated_at",)) for p in _list(plan.get("pending_plan_prs"))]
    for node in _dict(out.get("nodes")).values():
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("delivery_pr"), dict):
            node["delivery_pr"] = _drop(node["delivery_pr"], ("updated_at",))
        if isinstance(node.get("merge_runs"), list):
            node["merge_runs"] = [_drop(r, ("id", "started_at")) for r in node["merge_runs"]]
        if isinstance(node.get("issue"), dict):
            node["issue"] = _drop(node["issue"], ("updated_at",))
        blocked = node.pop("blocked_since", None)
        node["blocked_bucket"] = _bucket(_hours_since(now, blocked), buckets["blocked"])
        label_since = node.pop("label_since", None)
        if isinstance(label_since, dict):
            node["label_buckets"] = {label: _bucket(_hours_since(now, when), buckets["blocked"])
                                     for label, when in sorted(label_since.items())}
        completion = _dict(node.get("completion"))
        merged_at = _dict(node.get("delivery_pr")).get("merged_at")
        node["pending_checks_bucket"] = (_bucket(_hours_since(now, merged_at), buckets["pending"])
                                         if completion.get("stage") == "DONE"
                                         and completion.get("merge_checks") == "PENDING" else None)
    out["done_gap_bucket"] = _bucket(_hours_since(now, _done_gap_base(product)), buckets["done_gap"])
    return out


def _lanes_core(lanes: Any, now: datetime, buckets: Dict[str, Tuple[float, ...]]) -> Any:
    if not isinstance(lanes, dict):
        return None
    out = copy.deepcopy(lanes)
    for entry in _list(out.get("lanes")):
        for row in _list(_dict(entry).get("active")):
            if isinstance(row, dict):
                created = row.pop("created", None)
                row["age_bucket"] = _bucket(_hours_since(now, created), buckets["session"])
    return out


def _control_core(control: Any) -> Any:
    if not isinstance(control, dict):
        return None
    # CONTRACT NOTE: runtime runs enter the hash by conclusion only (newest first); run ids and times
    # change with every runtime run and are not material.
    runs = control.get("runtime_runs")
    return {"main_sha": control.get("main_sha"), "runtime_enabled": control.get("runtime_enabled"),
            "activated_runtime_sha": control.get("activated_runtime_sha"),
            "runtime_conclusions": [_dict(r).get("conclusion") for r in runs] if isinstance(runs, list) else None}


def facts_core(facts: Dict[str, Any], thresholds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The material part of a facts document: volatile fields removed, ages replaced by buckets.

    Ages are measured at the document's own ``collected_at``; ``thresholds`` is a config (its
    ``thresholds`` key) or a thresholds dict, defaulting to the §3.5 defaults.
    """
    now = _dt(facts.get("collected_at"))
    if now is None:
        raise InspectError("FACTS", "collected_at missing")
    buckets = _bucket_sets(_thresholds(thresholds))
    products = {repo: _product_core(p, now, buckets) for repo, p in _dict(facts.get("products")).items()
                if isinstance(p, dict)}
    return {"schema": facts.get("schema"), "control": _control_core(facts.get("control")),
            "lanes": _lanes_core(facts.get("lanes"), now, buckets), "products": products,
            "unknown": sorted(g for g in _list(facts.get("unknown")) if isinstance(g, str))}


def hashes_of(facts: Dict[str, Any], thresholds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``{"products": {repo: material hash}, "snapshot": hash}`` of a facts document."""
    core_doc = facts_core(facts, thresholds)
    products = {repo: core.sha256_hex(core.canon(p)) for repo, p in core_doc["products"].items()}
    snapshot = core.sha256_hex(core.canon({"products": sorted(products.items()), "control": core_doc["control"],
                                           "lanes": core_doc["lanes"], "unknown": core_doc["unknown"]}))
    return {"products": products, "snapshot": snapshot}


def next_recheck_at(facts: Dict[str, Any], now: datetime, config: Optional[Dict[str, Any]] = None) -> datetime:
    """The earliest future bucket crossing, the next KST daily-line time, or now + 24 h."""
    th = _thresholds(config)
    buckets = _bucket_sets(th)
    candidates: List[datetime] = [now + RECHECK_MAX]

    def crossings(start: Optional[datetime], hours: Iterable[float]) -> None:
        if start is None:
            return
        for h in hours:
            at = start + timedelta(hours=h)
            if at > now:
                candidates.append(at)

    lanes = facts.get("lanes")
    for entry in _list(_dict(lanes).get("lanes")):
        for row in _list(_dict(entry).get("active")):
            if isinstance(row, dict) and row.get("state") == "CONFIRMED":
                crossings(_dt(row.get("created")), buckets["session"])
    for product in _dict(facts.get("products")).values():
        if not isinstance(product, dict):
            continue
        for node in _dict(product.get("nodes")).values():
            if not isinstance(node, dict):
                continue
            crossings(_dt(node.get("blocked_since")), buckets["blocked"])
            for when in _dict(node.get("label_since")).values():
                crossings(_dt(when), buckets["blocked"])
            completion = _dict(node.get("completion"))
            if completion.get("stage") == "DONE" and completion.get("merge_checks") in ("PASS", "FAIL"):
                # CONTRACT NOTE: a re-run can turn a settled result around at the same SHA (PASS -> FAIL)
                # without changing anything the T0 probe reads; CI re-runs come soon after the merge, so
                # every tick runs T1 for 24 h after it (later re-runs are seen at the next T1).
                merged_at = _dt(_dict(node.get("delivery_pr")).get("merged_at"))
                if merged_at is not None and now - merged_at <= RERUN_WATCH:
                    candidates.append(now + NEXT_TICK)
            if completion.get("stage") == "DONE" and completion.get("merge_checks") == "PENDING":
                merged_at = _dt(_dict(node.get("delivery_pr")).get("merged_at"))
                crossings(merged_at, buckets["pending"])
                # CONTRACT NOTE: a check run finishing changes nothing the T0 probe reads, so while a DONE
                # node's post-merge checks are PENDING (merged within 7 days) every tick runs T1;
                # otherwise a FAIL would surface only at the 24 h crossing or the daily line.
                if merged_at is None or now - merged_at <= WINDOW_7D:
                    candidates.append(now + NEXT_TICK)
        crossings(_dt(_done_gap_base(product)), buckets["done_gap"])
        # CONTRACT NOTE: a merge or direct push leaving the 7-day window also changes the material facts.
        for item in _list(product.get("merged_7d")):
            crossings(_dt(_dict(item).get("merged_at")), (WINDOW_7D.total_seconds() / 3600.0,))
        for item in _list(product.get("direct_pushes_7d")):
            crossings(_dt(_dict(item).get("date")), (WINDOW_7D.total_seconds() / 3600.0,))
    hour = _dict(config).get("daily_hour_kst")
    hour = hour if _is_int(hour) and 0 <= hour <= 23 else 9
    local = now.astimezone(core.KST)
    daily = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if daily <= local:
        daily += timedelta(days=1)
    candidates.append(daily.astimezone(timezone.utc))
    return min(candidates)


# ---------------------------------------------------------------------------- commit

def history_row(facts: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """``{"t", "products": {repo: {"planned", "done"}}}`` for products with a PRESENT plan."""
    products = {}
    for repo, product in sorted(_dict(facts.get("products")).items()):
        plan = _dict(_dict(product).get("plan"))
        if plan.get("state") != "PRESENT":
            continue
        ids = [n.get("id") for n in _list(plan.get("nodes")) if isinstance(n, dict)]
        nodes = _dict(product.get("nodes"))
        done = sum(1 for i in ids if _dict(_dict(nodes.get(i)).get("completion")).get("stage") == "DONE")
        products[repo] = {"planned": len(ids), "done": done}
    return {"t": core.iso(now), "products": products}


def _new_baseline(baseline: Dict[str, Any], facts: Dict[str, Any]) -> Dict[str, Any]:
    """First-seen required checks per target; it only grows (a removal stays visible as a shrink)."""
    # CONTRACT NOTE: names added later join the baseline, so a later removal of them is a shrink too.
    out = {k: list(v) for k, v in baseline.items() if isinstance(v, list)}
    for repo, product in _dict(facts.get("products")).items():
        required = _dict(product).get("required_checks")
        if not isinstance(required, list):
            continue
        merged = list(out.get(repo, []))
        for name in required:
            if isinstance(name, str) and name not in merged:
                merged.append(name)
        out[repo] = merged
    return out


def _idle_progress(previous: Any, facts: Dict[str, Any], now: datetime,
                   config: Optional[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[datetime]]:
    """The idle-while-waiting run seen by committed T1s (``{"since", "points"}``) and, while that run is
    still too short for S7 (idle_waiting_snapshots points spanning idle_waiting_min_span_h), the time
    of the next T1 it needs. Idle lanes with ready nodes change nothing the probe reads."""
    import control_plane_inspect_signals as signals  # lazy: signals never imports this module

    if not signals.idle_waiting_hit(facts, now):
        return None, None
    th = _thresholds(config)
    prev = _dict(previous)
    since = _dt(prev.get("since"))
    points = prev.get("points") if _is_int(prev.get("points")) and prev.get("points") > 0 else 0
    if since is None or since > now:
        since, points = now, 0
    points += 1
    span_due = since + timedelta(hours=th["idle_waiting_min_span_h"])
    if points < th["idle_waiting_snapshots"] - 1:
        due: Optional[datetime] = now + NEXT_TICK
    elif points < th["idle_waiting_snapshots"] or now < span_due:
        due = max(now + NEXT_TICK, span_due)
    else:
        due = None
    return {"since": core.iso(since), "points": points}, due


def commit_t1(state: core.StateStore, facts: Dict[str, Any], now: datetime,
              config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write the atomic group after a successful T1 and return ``{"hashes", "next_recheck_at"}``.

    Order: recheck is marked dirty first, then facts, hashes, baseline, ETags, bodies and caches are
    renamed into place, history is appended, and the clean recheck is renamed last. A crash anywhere
    leaves ``t1_dirty`` true, so the next tick simply collects again.
    """
    if not isinstance(facts, dict) or facts.get("schema") != FACTS_SCHEMA:
        raise InspectError("FACTS", "not an AIOPS_INSPECT_FACTS_V1 document")
    hashes = hashes_of(facts, config)
    due = next_recheck_at(facts, now, config)
    previous = _recheck(state)
    idle, idle_due = _idle_progress(previous.get("idle"), facts, now, config)
    if idle_due is not None:
        due = min(due, idle_due)
    state.write(ST_RECHECK, dict(previous, t1_dirty=True))
    state.stage(ST_FACTS, facts)
    state.stage(ST_HASHES, dict(hashes, at=core.iso(now)))
    state.stage(ST_BASELINE, _new_baseline(_dict(state.read(ST_BASELINE, None)), facts))
    state.commit_staged([ST_FACTS, ST_HASHES, ST_BASELINE] + list(T1_STAGED))
    state.append_jsonl(ST_HISTORY, history_row(facts, now))
    record: Dict[str, Any] = {"next_recheck_at": core.iso(due), "t1_dirty": False, "last_t1": core.iso(now),
                              "t1_failures": 0, "last_error": None, "idle": idle}
    errors = sorted(r for r in _list(_dict(facts.get("stats")).get("read_errors")) if r in READ_FAILURE_REASONS)
    if errors:
        # CONTRACT NOTE: a T1 with read failures is committed (its groups are UNKNOWN, three-valued
        # logic), but it stays dirty so the next tick collects again (the change that started it is
        # not lost), and it counts as a failure toward DEGRADED like an aborted T1. A GitHub reason
        # wins over a host reason for last_error.
        failures = previous.get("t1_failures") if _is_int(previous.get("t1_failures")) else 0
        record.update(t1_dirty=True, t1_failures=failures + 1, last_error=errors[0], last_failure=core.iso(now))
    state.stage(ST_RECHECK, record)
    state.commit_staged([ST_RECHECK])
    return {"hashes": hashes, "next_recheck_at": core.iso(due)}


def fail_t1(state: core.StateStore, now: datetime, reason: str) -> Dict[str, Any]:
    """After a failed T1: drop staged ETags and caches, keep previous facts, set ``t1_dirty``."""
    state.discard_staged(list(T1_STAGED) + [ST_FACTS, ST_HASHES, ST_BASELINE, ST_RECHECK])
    previous = _recheck(state)
    failures = previous.get("t1_failures") if _is_int(previous.get("t1_failures")) else 0
    record = dict(previous, t1_dirty=True, t1_failures=failures + 1,
                  last_error=reason if isinstance(reason, str) and core.REASON_RE.fullmatch(reason) else "INTERNAL",
                  last_failure=core.iso(now))
    state.write(ST_RECHECK, record)
    return record
