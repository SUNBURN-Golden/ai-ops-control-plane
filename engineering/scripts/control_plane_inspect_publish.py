"""Program inspector publishing: ledger comment, issue body, Slack card, heartbeat, daily line, dead-man (§3.8).

The read-only program inspector (User decision M7, docs/INSPECTOR.md) is advisory only and never a
gate. This module renders what the inspector shows and posts it to exactly two places: the ONE
configured GitHub ledger issue (through ``GitHubLedgerWriter``) and the configured Slack channel
through ``SlackClient``, which can call only these Slack methods: auth.test, chat.postMessage,
chat.update, chat.scheduleMessage, chat.deleteScheduledMessage, files.getUploadURLExternal and
files.completeUploadExternal (plus a POST of chart bytes to the upload URL Slack returns).

Every write goes through the publish journal ``state/publish/<run>.json`` and follows the UNKNOWN
discipline: a write whose outcome is unknown (timeout or reset after sending, 5xx, a crash while
sending) is recorded UNKNOWN and is NEVER resent with the same content. Only a later positive
observation (a ledger comment whose body sha256 equals the journaled one) turns it into POSTED.
A write that was refused (nothing was sent) may be retried on later ticks, at most 3 attempts.

All dynamic text goes through ``gh_text``/``gh_ref`` (GitHub) or ``slack_text``/``slack_link``
(Slack), every output passes ``redact_output`` (a live secret aborts with SECRET_LIVE), and every
time shown is an absolute KST time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import urllib.parse

import control_plane_inspect_core as core
from control_plane_inspect_core import InspectError
import control_plane_inspect_github as gh
import control_plane_inspect_signals as sig

# ---------------------------------------------------------------------------- constants

SLACK_API = "https://slack.com/api/"
SLACK_UPLOAD_HOST = "files.slack.com"
# The ONLY Slack Web API methods this tool may call (a test scans the module source for others).
SLACK_METHODS = ("auth.test", "chat.postMessage", "chat.update", "chat.scheduleMessage",
                 "chat.deleteScheduledMessage", "files.getUploadURLExternal", "files.completeUploadExternal")
REQUIRED_SCOPES = frozenset({"chat:write", "files:write"})
SLACK_TIMEOUT = 20.0
UPLOAD_TIMEOUT = 60.0
MAX_SLACK_RESPONSE = 1024 * 1024
MAX_CARD_CHARS = 3900
MAX_REPLY_CHARS = 3000
MAX_REPLIES = 10
MAX_UPLOAD_FILES = 10
MAX_PNG_BYTES = 2 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

COMMENT_LIMIT = gh.MAX_BODY_CHARS  # 60,000 chars
COMMENT_MARK = "<!-- aiops-inspect -->"
JOURNAL_SCHEMA = "AIOPS_INSPECT_PUBLISH_V1"
MAX_ATTEMPTS = 3          # CONTRACT NOTE: "retried at most 3 times" read as at most 3 attempts in total.
# Detail suffix of a REFUSED write whose connection was never opened (NET_DOWN). Both the Slack client
# below and GitHubLedgerWriter._write end the detail with it. Such a refusal is not a write attempt.
NOT_CONNECTED = "connection not opened"
UNKNOWN_WINDOW = timedelta(hours=24)
RECONCILE_MARGIN = timedelta(minutes=5)
JOURNAL_KEEP = timedelta(days=30)
TOKEN_WARN_DAYS = 14

# Step states inside the journal.
PENDING, SENDING, POSTED, UNKNOWN, REFUSED = "PENDING", "SENDING", "POSTED", "UNKNOWN", "REFUSED"
FAILED, SKIPPED, ABANDONED, SUPERSEDED, UNCHANGED = "FAILED", "SKIPPED", "ABANDONED", "SUPERSEDED", "UNCHANGED"
RETRYABLE = (PENDING, REFUSED)
SETTLED_FOR_SLACK = (POSTED, UNKNOWN, ABANDONED)

HEARTBEAT_STATES = ("OK", "DEGRADED", "HALTED", "PAUSED")
CHANGE_WORDS = {"NEW": "새 발견", "WORSENED": "악화", "REOPENED": "재발", "RESOLVED": "해소", "OPEN": "열림"}
ALERT_CHANGES = ("NEW", "WORSENED", "REOPENED")

LEVEL_WORDS = sig.LEVEL_WORDS
LEVEL_RANK = sig.LEVEL_RANK
SIGNAL_NAMES = sig.SIGNAL_NAMES
LANE_ORDER = tuple(sig.LANE_ORDER)
CTRL = sig.CTRL
STAGE_ORDER = ("planned", "materializing", "not_started", "in_progress", "delivered", "done")
STAGE_KEY_WORDS = {key: sig.STAGE_WORDS[stage] for stage, key in sig.STAGE_KEYS.items()}
NOT_RECORDED_WORD = "기록 없음"

CHANNEL_RE = core.CHANNEL_RE
TS_RE = re.compile(r"[0-9]{1,12}\.[0-9]{1,9}")
SLACK_ID_RE = re.compile(r"[A-Za-z0-9]{4,64}")
SLACK_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
PNG_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\.png")
HEX_RE = re.compile(r"[0-9a-f]{12,64}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
TASK_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,99}")
ROLE_RE = re.compile(r"[A-Z][A-Z_]{0,31}")
FINDING_ID_RE = re.compile(r"INS-[A-Z]{4}-[0-9]{4,}")
COMMENT_REF_RE = re.compile(r"issuecomment-([1-9][0-9]{0,19})")
URL_SAFE_RE = re.compile(r"[A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------- small helpers

def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sha(text: str) -> str:
    return core.sha256_hex(text.encode("utf-8"))


def _time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return core.utc(value)
    if isinstance(value, str):
        try:
            return core.parse_iso(value)
        except InspectError:
            return None
    return None


def _word(level: Any) -> str:
    return LEVEL_WORDS.get(level, LEVEL_WORDS[sig.UNKNOWN]) if isinstance(level, str) else LEVEL_WORDS[sig.UNKNOWN]


def _code(level: Any) -> str:
    return level if isinstance(level, str) and CODE_RE.fullmatch(level) else sig.UNKNOWN


def arrow(current: Any, previous: Any) -> str:
    """``↑`` worse, ``↓`` better, ``=`` same; ``~`` when there is no previous or a level is not ranked."""
    # CONTRACT NOTE: only ↑/↓/= are specified; "~" marks a change that has no direction
    # (first run, or PAUSED/UNKNOWN/NOT_CONFIGURED on either side).
    if not isinstance(previous, str) or previous not in LEVEL_WORDS:
        return "~"
    if current == previous:
        return "="
    if current in LEVEL_RANK and previous in LEVEL_RANK:
        return "↑" if LEVEL_RANK[current] > LEVEL_RANK[previous] else "↓"
    return "~"


def next_tick_at(now: datetime, tick_minute: int) -> datetime:
    """The next KST time whose minute is ``tick_minute``, strictly after ``now`` (UTC result)."""
    local = core.utc(now).astimezone(core.KST)
    candidate = local.replace(minute=int(tick_minute) % 60, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(hours=1)
    return candidate.astimezone(timezone.utc)


def token_warning(expiry: Optional[Dict[str, Optional[datetime]]], now: datetime) -> Optional[str]:
    """``D-<days> (<kind> <KST>)`` for the soonest token expiry within 14 days, else None."""
    soonest: Optional[Tuple[datetime, str]] = None
    for kind, when in sorted(_dict(expiry).items()):
        when = _time(when)
        if when is None or kind not in core.SECRET_KINDS:
            continue
        if soonest is None or when < soonest[0]:
            soonest = (when, kind)
    if soonest is None:
        return None
    days = math.floor((soonest[0] - core.utc(now)).total_seconds() / 86400)
    if days > TOKEN_WARN_DAYS:
        return None
    if days < 0:
        return f"만료됨 ({soonest[1]} {core.fmt_kst(soonest[0])})"
    return f"D-{days} ({soonest[1]} {core.fmt_kst(soonest[0])})"


def github_url(evidence: Dict[str, Any], ledger_issue: Optional[int] = None,
               control_repository: Optional[str] = None) -> Optional[str]:
    """A github.com URL for one evidence reference (Slack only; GitHub output uses code spans)."""
    repo = evidence.get("repository")
    if not isinstance(repo, str) or not core.REPO_RE.fullmatch(repo):
        return None
    kind = evidence.get("kind")
    base = f"https://github.com/{repo}"
    number = _int(evidence.get("number"))
    sha = evidence.get("sha")
    ref = evidence.get("ref")
    if number is not None and number > 0:
        if kind == "pr":
            return f"{base}/pull/{number}"
        if kind == "issue":
            return f"{base}/issues/{number}"
        if kind == "run":
            return f"{base}/actions/runs/{number}"
        return None
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{7,64}", sha) and kind == "commit":
        return f"{base}/commit/{sha}"
    if isinstance(ref, str):
        match = COMMENT_REF_RE.fullmatch(ref)
        if match and ledger_issue and repo == control_repository:
            return f"{base}/issues/{int(ledger_issue)}#issuecomment-{match.group(1)}"
    return None


def evidence_label(evidence: Dict[str, Any]) -> str:
    """Plain label for one evidence item (``owner/repo#12``, ``owner/repo@abc1234``, ...)."""
    repo = evidence.get("repository") if isinstance(evidence.get("repository"), str) else "?"
    if evidence.get("kind") == "host":
        return "호스트 기록"
    number = _int(evidence.get("number"))
    if number is not None:
        return f"{repo} run {number}" if evidence.get("kind") == "run" else f"{repo}#{number}"
    if isinstance(evidence.get("sha"), str):
        return f"{repo}@{evidence['sha'][:12]}"
    if isinstance(evidence.get("ref"), str):
        return f"{repo} {evidence['ref']}"
    return repo


def evidence_ref_gh(evidence: Dict[str, Any]) -> str:
    """Evidence as an inert GitHub code span (no cross-reference events)."""
    if not isinstance(evidence, dict):
        return ""
    if evidence.get("kind") == "host":
        return "호스트 기록"
    repo = evidence.get("repository")
    kind = evidence.get("kind")
    try:
        if _int(evidence.get("number")) is not None:
            return core.gh_ref(repo, kind if kind in ("issue", "pr", "run") else "issue", evidence["number"])
        if isinstance(evidence.get("sha"), str):
            return core.gh_ref(repo, "commit", evidence["sha"])
        ref = evidence.get("ref")
        if isinstance(ref, str):
            match = COMMENT_REF_RE.fullmatch(ref)
            if match:
                return core.gh_ref(repo, "comment", int(match.group(1)))
            return core.gh_ref(repo, "ref", ref)
    except InspectError:
        pass
    return _g(evidence_label(evidence), 120)


# ---------------------------------------------------------------------------- Slack transport and client

class SlackError(InspectError):
    """A Slack write that did not succeed. ``outcome`` is REFUSED (nothing posted) or UNKNOWN."""

    def __init__(self, reason: str, outcome: str, slack_error: Optional[str] = None, detail: str = ""):
        super().__init__(reason, detail)
        self.outcome = outcome if outcome in (REFUSED, UNKNOWN) else UNKNOWN
        self.slack_error = slack_error if isinstance(slack_error, str) and SLACK_ERROR_RE.fullmatch(slack_error) \
            else None


def _slack_url_ok(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    if url.startswith(SLACK_API):
        return url[len(SLACK_API):] in SLACK_METHODS
    return upload_url_ok(url)


def upload_url_ok(url: Any) -> bool:
    """True for an https URL on files.slack.com with no credentials, port or fragment."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in url):
        return False
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme == "https" and parts.netloc == SLACK_UPLOAD_HOST and not parts.fragment
            and parts.path.startswith("/upload/"))


class SlackUrllibTransport:
    """Default Slack transport (``urllib.request``); POST only, Slack API and upload host only.

    ``NET_DOWN``: the connection was never opened (nothing sent). ``NET_UNKNOWN``: it failed after
    the connection was open (the message may have been posted). HTTP error statuses are returned.
    """

    def __init__(self, max_bytes: int = MAX_SLACK_RESPONSE,
                 opener_factory: Optional[Callable[[Dict[str, bool]], Any]] = None):
        self.max_bytes = int(max_bytes)
        # CONTRACT NOTE: reuses the GitHub module's no-redirect opener that records "connected".
        self._opener_factory = opener_factory or gh._build_opener

    def __repr__(self) -> str:
        return f"SlackUrllibTransport(max_bytes={self.max_bytes})"

    def __call__(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
                 timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        import http.client
        import urllib.error
        import urllib.request
        if method != "POST" or not _slack_url_ok(url):
            raise InspectError("PATH_NOT_ALLOWED", "Slack transport posts only to allowed Slack URLs")
        state = {"connected": False}
        request = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
        limit = self.max_bytes + 1
        try:
            opener = self._opener_factory(state)
            try:
                with opener.open(request, timeout=timeout) as response:
                    content = response.read(limit)
                    status = int(getattr(response, "status", None) or response.getcode())
                    return status, gh._lower_headers(response.headers.items()), content
            except urllib.error.HTTPError as exc:
                try:
                    content = exc.read(limit) or b""
                except (OSError, http.client.HTTPException, ValueError, AttributeError):
                    content = b""
                items = exc.headers.items() if exc.headers is not None else []
                status = int(exc.code)
                exc.close()
                return status, gh._lower_headers(items), content
        except (OSError, http.client.HTTPException) as exc:
            name = type(getattr(exc, "reason", exc)).__name__
            if state["connected"]:
                raise InspectError("NET_UNKNOWN", f"Slack POST failed after connect: {name}") from None
            raise InspectError("NET_DOWN", f"Slack POST could not connect: {name}") from None


class SlackClient:
    """Bot-token Slack client limited to SLACK_METHODS. ``verify()`` must pass before any write.

    Write outcomes: ``ok: true`` -> success; ``ok: false`` or HTTP 4xx or NET_DOWN -> SlackError
    REFUSED; HTTP 5xx, NET_UNKNOWN or an unreadable 2xx answer -> SlackError UNKNOWN.
    """

    def __init__(self, token: str, team_id: str, bot_user_id: str, transport: Optional[Any] = None,
                 timeout: float = SLACK_TIMEOUT, live_values: Iterable[str] = ()):
        try:
            self._token = core.check_secret_shape("slack", token)
        except InspectError:
            raise InspectError("SECRET_SLACK", "Slack token missing or malformed") from None
        if not isinstance(team_id, str) or not core.TEAM_RE.fullmatch(team_id):
            raise InspectError("CONFIG", "bad Slack team id")
        if not isinstance(bot_user_id, str) or not core.USER_ID_RE.fullmatch(bot_user_id):
            raise InspectError("CONFIG", "bad Slack bot user id")
        self.team_id = team_id
        self.bot_user_id = bot_user_id
        self.timeout = float(timeout)
        self._transport = transport if transport is not None else SlackUrllibTransport()
        self._live = tuple(v for v in live_values if isinstance(v, str)) + (self._token,)
        self.verified = False
        self.requests = 0
        self.shape_hits = 0

    def __repr__(self) -> str:
        return f"SlackClient(team_id={self.team_id!r}, verified={self.verified})"

    # -- plumbing

    def _clean(self, value: Any) -> str:
        text, hits = core.redact_output(str(value), self._live)
        self.shape_hits += hits
        return text

    def _send(self, url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        self.requests += 1
        try:
            status, rheaders, content = self._transport("POST", url, headers, body, timeout)
        except InspectError as exc:
            if exc.reason == "NET_DOWN":
                raise SlackError("SLACK_REFUSED", REFUSED, None, NOT_CONNECTED) from None
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, exc.reason) from None
        except Exception as exc:  # noqa: BLE001 - any other transport failure may have sent the request
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, type(exc).__name__) from None
        rheaders = gh._lower_headers(rheaders.items()) if isinstance(rheaders, dict) else {}
        content = bytes(content) if isinstance(content, (bytes, bytearray)) else b""
        if isinstance(status, bool) or not isinstance(status, int):
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, "no HTTP status")
        return status, rheaders, content

    def _call(self, method: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        if method not in SLACK_METHODS:
            raise InspectError("SLACK_METHOD", "method is not allowed")
        if method != "auth.test" and not self.verified:
            raise InspectError("SLACK_NOT_VERIFIED", "auth.test must pass before any write")
        form = [(key, self._clean(value)) for key, value in params.items() if value is not None]
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": gh.USER_AGENT,
                   "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        status, rheaders, content = self._send(SLACK_API + method, headers, body, self.timeout)
        if 400 <= status <= 499:
            raise SlackError("SLACK_REFUSED", REFUSED, None, f"{method} -> {status}")
        if not 200 <= status <= 299:
            # CONTRACT NOTE: 5xx (and any other non-2xx/4xx) is UNKNOWN per the repo-wide discipline.
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, f"{method} -> {status}")
        try:
            if len(content) > MAX_SLACK_RESPONSE:
                raise ValueError("oversize")
            data = core.loads_strict(content)
        except (ValueError, UnicodeDecodeError):
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, f"{method}: answer is not JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, f"{method}: answer has no ok field")
        if data["ok"] is not True:
            error = data.get("error") if isinstance(data.get("error"), str) else None
            raise SlackError("SLACK_REFUSED", REFUSED, error, f"{method}: ok=false")
        return data, rheaders

    # -- identity

    def verify(self) -> Dict[str, Any]:
        """auth.test: ok, configured team and bot user, and scopes exactly {chat:write, files:write}."""
        self.verified = False
        try:
            data, headers = self._call("auth.test", {})
        except SlackError as exc:
            raise InspectError("SLACK_AUTH", f"auth.test failed ({exc.slack_error or exc.outcome})") from None
        # CONTRACT NOTE: a token for another workspace or bot is treated like a scope breach (SLACK_SCOPE,
        # which engages the kill switch); an unreachable or rejected auth.test is SLACK_AUTH.
        if data.get("team_id") != self.team_id or data.get("user_id") != self.bot_user_id:
            raise InspectError("SLACK_SCOPE", "auth.test identity differs from the configured team/bot")
        raw = headers.get("x-oauth-scopes")
        scopes = {s.strip() for s in raw.split(",") if s.strip()} if isinstance(raw, str) else set()
        if scopes != set(REQUIRED_SCOPES):
            raise InspectError("SLACK_SCOPE", "bot scopes must be exactly chat:write and files:write")
        self.verified = True
        return {"team_id": self.team_id, "user_id": self.bot_user_id, "scopes": sorted(scopes)}

    # -- writes

    @staticmethod
    def _channel(channel: Any) -> str:
        if not isinstance(channel, str) or not CHANNEL_RE.fullmatch(channel):
            raise InspectError("SLACK_ARGS", "bad channel id")
        return channel

    @staticmethod
    def _ts(ts: Any) -> str:
        if not isinstance(ts, str) or not TS_RE.fullmatch(ts):
            raise InspectError("SLACK_ARGS", "bad message ts")
        return ts

    def post_message(self, channel: str, text: str, thread_ts: Optional[str] = None) -> Dict[str, Any]:
        """chat.postMessage (no unfurls, no link parsing) -> {"ts", "channel"}."""
        params = {"channel": self._channel(channel), "text": text, "parse": "none",
                  "unfurl_links": "false", "unfurl_media": "false",
                  "thread_ts": self._ts(thread_ts) if thread_ts is not None else None}
        data, _ = self._call("chat.postMessage", params)
        ts = data.get("ts")
        if not isinstance(ts, str) or not TS_RE.fullmatch(ts):
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, "chat.postMessage: no ts")
        return {"ts": ts, "channel": channel}

    def update_message(self, channel: str, ts: str, text: str) -> Dict[str, Any]:
        """chat.update of one message the bot posted -> {"ts"}."""
        self._call("chat.update", {"channel": self._channel(channel), "ts": self._ts(ts), "text": text,
                                   "parse": "none"})
        return {"ts": ts}

    def schedule_message(self, channel: str, text: str, post_at: datetime) -> Dict[str, Any]:
        """chat.scheduleMessage -> {"id", "post_at"} (post_at as ISO UTC)."""
        when = int(core.utc(post_at).timestamp())
        data, _ = self._call("chat.scheduleMessage", {"channel": self._channel(channel), "text": text,
                                                      "post_at": str(when), "parse": "none",
                                                      "unfurl_links": "false", "unfurl_media": "false"})
        sid = data.get("scheduled_message_id")
        if not isinstance(sid, str) or not SLACK_ID_RE.fullmatch(sid):
            raise SlackError("SLACK_UNKNOWN", UNKNOWN, None, "chat.scheduleMessage: no id")
        return {"id": sid, "post_at": core.iso(datetime.fromtimestamp(when, timezone.utc))}

    def delete_scheduled(self, channel: str, scheduled_id: str) -> None:
        """chat.deleteScheduledMessage."""
        if not isinstance(scheduled_id, str) or not SLACK_ID_RE.fullmatch(scheduled_id):
            raise InspectError("SLACK_ARGS", "bad scheduled message id")
        self._call("chat.deleteScheduledMessage", {"channel": self._channel(channel),
                                                   "scheduled_message_id": scheduled_id})

    def upload_files(self, channel: str, thread_ts: str, files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """External upload into a thread: getUploadURLExternal -> POST bytes -> completeUploadExternal.

        ``files``: [{"name": "c1_ladder.png", "data": bytes, "title": str}]. Any failure before
        completeUploadExternal is REFUSED (nothing is visible in the channel until completion).
        """
        channel = self._channel(channel)
        thread_ts = self._ts(thread_ts)
        if not files or len(files) > MAX_UPLOAD_FILES:
            raise InspectError("SLACK_ARGS", "1..10 files")
        uploaded = []
        for item in files:
            name, data = item.get("name"), item.get("data")
            if not isinstance(name, str) or not PNG_NAME_RE.fullmatch(name) or not isinstance(data, bytes) \
                    or not data or len(data) > MAX_PNG_BYTES:
                raise InspectError("SLACK_ARGS", "bad upload file")
            try:
                info, _ = self._call("files.getUploadURLExternal", {"filename": name, "length": str(len(data))})
            except SlackError as exc:
                # Nothing is visible before completeUploadExternal, so any failure here is REFUSED.
                # A connection that was never opened keeps its NOT_CONNECTED suffix (not an attempt).
                detail = f"getUploadURLExternal: {NOT_CONNECTED}" if exc.detail.endswith(NOT_CONNECTED) \
                    else "getUploadURLExternal failed"
                raise SlackError("SLACK_REFUSED", REFUSED, exc.slack_error, detail) from None
            url, file_id = info.get("upload_url"), info.get("file_id")
            if not upload_url_ok(url) or not isinstance(file_id, str) or not SLACK_ID_RE.fullmatch(file_id):
                raise SlackError("SLACK_REFUSED", REFUSED, None, "upload URL not on files.slack.com")
            # CONTRACT NOTE: the bot token is NOT sent to the upload URL (Slack does not require it there).
            try:
                status, _, _ = self._send(url, {"User-Agent": gh.USER_AGENT,
                                                "Content-Type": "application/octet-stream"}, data, UPLOAD_TIMEOUT)
            except SlackError as exc:
                raise SlackError("SLACK_REFUSED", REFUSED, None, f"upload POST failed: {exc.detail}") from None
            if not 200 <= status <= 299:
                raise SlackError("SLACK_REFUSED", REFUSED, None, f"upload POST -> {status}")
            title = item.get("title") if isinstance(item.get("title"), str) else name
            uploaded.append({"id": file_id, "title": core.slack_text(title, 120)})
        self._call("files.completeUploadExternal", {"files": json.dumps(uploaded, ensure_ascii=False),
                                                    "channel_id": channel, "thread_ts": thread_ts})
        return {"file_ids": [u["id"] for u in uploaded]}


# ---------------------------------------------------------------------------- report model

def build_report(*, run: str, now: datetime, stage: str, snapshot: str, tool_sha256: str,
                 facts: Dict[str, Any], facts_sha256: str, result: Dict[str, Any],
                 control_repository: str, ledger_issue: int, pngs: Iterable[Dict[str, Any]] = (),
                 render_dir: Optional[Any] = None, render_status: str = "OK") -> Dict[str, Any]:
    """Collect what publishing shows from one committed snapshot.

    ``result`` is ``control_plane_inspect_signals.run(...)`` output (evaluation, state, changes,
    previous_verdicts); ``pngs`` the charts' render.json entries ({"name", "sha256", ...}).
    """
    if not isinstance(run, str) or not core.RUN_ID_RE.fullmatch(run):
        raise InspectError("PUBLISH_ARGS", "bad run id")
    if stage not in core.STAGES:
        raise InspectError("PUBLISH_ARGS", "bad stage")
    for value, name, regex in ((snapshot, "snapshot", HEX64_RE), (tool_sha256, "tool", HEX_RE),
                               (facts_sha256, "facts", HEX64_RE)):
        if not isinstance(value, str) or not regex.fullmatch(value):
            raise InspectError("PUBLISH_ARGS", f"bad {name} hash")
    evaluation = _dict(result.get("evaluation"))
    state = _dict(result.get("state"))
    previous = _dict(result.get("previous_verdicts"))
    findings = sig.ordered_findings(state) if state else []
    changed_by_product: Dict[str, List[str]] = {}
    for finding in findings:
        if finding.get("tick_change") in ALERT_CHANGES and FINDING_ID_RE.fullmatch(str(finding.get("id"))):
            changed_by_product.setdefault(str(finding.get("product")), []).append(finding["id"])
    products = []
    for repo in _list(evaluation.get("order")):
        entry = _dict(_dict(evaluation.get("products")).get(repo))
        if not entry:
            continue
        products.append({"repository": repo, "name": entry.get("name") or repo.split("/", 1)[-1],
                         "prefix": entry.get("prefix") or "XXXX", "verdict": entry.get("verdict"),
                         "previous": previous.get(repo), "paused": bool(entry.get("paused")),
                         "plan_state": entry.get("plan_state"), "ladder": _dict(entry.get("ladder")),
                         "unknown_groups": [g for g in _list(entry.get("unknown_groups")) if isinstance(g, str)],
                         "new_ids": changed_by_product.get(repo, [])})
    ctrl = _dict(evaluation.get("ctrl"))
    chart_files = []
    for png in pngs:
        png = _dict(png)
        name, sha = png.get("name"), png.get("sha256")
        if isinstance(name, str) and PNG_NAME_RE.fullmatch(name) and isinstance(sha, str) and HEX64_RE.fullmatch(sha):
            path = str(Path(render_dir) / name) if render_dir is not None else png.get("path")
            chart_files.append({"name": name, "sha256": sha, "path": path if isinstance(path, str) else None})
    return {
        "run": run, "at": core.iso(now), "stage": stage, "snapshot": snapshot, "tool_sha256": tool_sha256,
        "facts_sha256": facts_sha256, "control_repository": control_repository, "ledger_issue": ledger_issue,
        "products": products,
        "ctrl": {"verdict": ctrl.get("verdict"), "previous": previous.get(CTRL),
                 "new_ids": changed_by_product.get(CTRL, [])},
        "findings": findings, "changes": _list(result.get("changes")),
        "open": sig.open_counts(state) if state else {"open": 0, "at_risk": 0, "unknown": 0},
        "info": [i for i in _list(evaluation.get("info")) if isinstance(i, dict)],
        "lanes": facts.get("lanes") if isinstance(facts.get("lanes"), dict) else None,
        "lanes_unknown": "lanes" in _list(facts.get("unknown")),
        "stats": _dict(facts.get("stats")), "pngs": chart_files, "render_status": render_status,
        "findings_state": state,
    }


def publish_needed(result: Dict[str, Any], first_run: bool) -> bool:
    """A comment and card are posted when verdicts or finding states changed, or on the first run."""
    if first_run:
        return True
    if _list(result.get("changes")):
        return True
    current = _dict(result.get("verdicts"))
    previous = _dict(result.get("previous_verdicts"))
    return current != previous


def lane_parts(lanes: Optional[Dict[str, Any]], unknown: bool = False) -> List[str]:
    """Lane summary pieces built only from lane names, counts, task ids and roles."""
    if unknown or not isinstance(lanes, dict) or not isinstance(lanes.get("lanes"), list):
        return ["레인 확인 불가"]
    by_lane = {lane.get("lane"): lane for lane in lanes["lanes"] if isinstance(lane, dict)}
    parts = []
    for name in LANE_ORDER:
        lane = by_lane.get(name)
        if lane is None:
            parts.append(f"{name} 확인 불가")
            continue
        if lane.get("enabled") is not True:
            parts.append(f"{name} 꺼짐")
            continue
        active = [row for row in _list(lane.get("active")) if isinstance(row, dict)]
        tasks = []
        for row in active[:4]:
            task, role = row.get("task"), row.get("role")
            if isinstance(task, str) and TASK_RE.fullmatch(task):
                tasks.append(task + (f" {role}" if isinstance(role, str) and ROLE_RE.fullmatch(role) else ""))
        more = f" 외 {len(active) - 4}" if len(active) > 4 else ""
        parts.append(f"{name} {len(active)}" + (f" ({', '.join(tasks)}{more})" if tasks else ""))
    total, cap = _int(lanes.get("active_total")), _int(lanes.get("max_active_sessions"))
    if total is not None and cap is not None:
        parts.append(f"동시 세션 {total}/{cap}")
    return parts


def ladder_parts(ladder: Dict[str, Any]) -> List[str]:
    """``계획 1 · 생성 중 0 · ... · 완료 3 (검사 통과 2·실패 0·대기 1)``-style pieces."""
    parts = [f"{STAGE_KEY_WORDS[key]} {_int(ladder.get(key)) or 0}" for key in STAGE_ORDER]
    checks = _dict(ladder.get("merge_checks"))
    parts[-1] += (f" (검사 통과 {_int(checks.get('PASS')) or 0}·실패 {_int(checks.get('FAIL')) or 0}"
                  f"·대기 {_int(checks.get('PENDING')) or 0})")
    return parts


# ---------------------------------------------------------------------------- GitHub rendering

def header_lines(report: Dict[str, Any]) -> List[str]:
    """The exact two header lines of a ledger comment."""
    verdicts = [f"{p['prefix']}:{_code(p['verdict'])}" for p in report["products"]
                if isinstance(p.get("prefix"), str) and re.fullmatch(r"[A-Z]{4}", p["prefix"])]
    verdicts.append(f"{CTRL}:{_code(report['ctrl'].get('verdict'))}")
    return [COMMENT_MARK,
            f"AIOPS_INSPECT_V1 run={report['run']} snapshot={report['snapshot']} stage={report['stage']} "
            f"advisory=true gate=none model=none tool={report['tool_sha256'][:12]} verdicts={','.join(verdicts)}"]


# gh_text escapes "_", so the whole-body redact_output pass can no longer see token shapes such as
# ghp_... or github_pat_... once a value is escaped: _g redacts shapes in the raw value first and
# remembers the distinct shapes it redacted (Publisher._out_gh counts them as output shape hits).
# Every live secret also has a known shape, so _g checks the publisher's live values (_GH_LIVE, set by
# Publisher._out_gh) BEFORE the shape pass: a live hit raises SECRET_LIVE instead of being counted as a shape.
_GH_SHAPES_SEEN: set = set()
_GH_LIVE: Tuple[str, ...] = ()


def _g(text: Any, limit: int = 200) -> str:
    """``gh_text`` of ``text`` with live secrets refused and token shapes redacted before escaping."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    clean, hits = core.redact(text, _GH_LIVE)
    if hits["live"]:
        raise InspectError("SECRET_LIVE", "output contained a live secret; nothing was posted")
    _GH_SHAPES_SEEN.update(core.sha256_hex(m.group(0).encode("utf-8")) for m in core.TOKEN_SHAPES.finditer(text))
    return core.gh_text(clean, limit)


def _product_table(report: Dict[str, Any]) -> List[str]:
    lines = ["| 제품 | 판정 | 변화 | " + " | ".join(STAGE_KEY_WORDS[k] for k in STAGE_ORDER)
             + " | 병합 후 검사 통과/실패/대기/미설정 | 배포 | 실사용 검증 | 계획 밖 변경 | 대기 계획 PR |",
             "|" + "---|" * 14]
    for product in report["products"]:
        ladder = product["ladder"]
        verdict, previous = product["verdict"], product["previous"]
        change = arrow(verdict, previous)
        change_text = change if change == "=" else f"{change} {_word(previous) if previous else '없음'}→{_word(verdict)}"
        name = f"{_g(product['name'], 80)} ({_g(product['prefix'], 8)})"
        verdict_text = f"{_word(verdict)} {_code(verdict)}" + (" (계획 없음)" if product["paused"] else "")
        if product["paused"]:
            cells = ["-"] * 6 + ["-", NOT_RECORDED_WORD, NOT_RECORDED_WORD, "-", "-"]
        else:
            checks = _dict(ladder.get("merge_checks"))
            cells = [str(_int(ladder.get(k)) or 0) for k in STAGE_ORDER]
            cells.append("/".join(str(_int(checks.get(k)) or 0) for k in ("PASS", "FAIL", "PENDING", "NOT_CONFIGURED")))
            cells += [NOT_RECORDED_WORD, NOT_RECORDED_WORD, str(_int(ladder.get("orphans")) or 0),
                      str(_int(ladder.get("pending_plan_prs")) or 0)]
        lines.append(f"| {name} | {verdict_text} | {_g(change_text, 40)} | " + " | ".join(cells) + " |")
    ctrl = report["ctrl"]
    change = arrow(ctrl.get("verdict"), ctrl.get("previous"))
    change_text = change if change == "=" else \
        f"{change} {_word(ctrl.get('previous')) if ctrl.get('previous') else '없음'}→{_word(ctrl.get('verdict'))}"
    lines.append(f"| {CTRL} (기록·레인) | {_word(ctrl.get('verdict'))} {_code(ctrl.get('verdict'))} | "
                 f"{_g(change_text, 40)} | " + " | ".join(["-"] * 11) + " |")
    return lines


def _finding_line(finding: Dict[str, Any], detail: bool) -> str:
    fid = str(finding.get("id"))
    fid = fid if FINDING_ID_RE.fullmatch(fid) else "INS-????"
    change = finding.get("tick_change") or finding.get("state") or "OPEN"
    severity = finding.get("severity")
    signal = finding.get("signal") if finding.get("signal") in SIGNAL_NAMES else "S?"
    head = (f"- **{fid}** {_word(severity)} {_code(severity)} · {CHANGE_WORDS.get(change, '열림')} · "
            f"{signal} {SIGNAL_NAMES.get(signal, '')} · 대상 {_g(finding.get('subject_key'), 120)}")
    if finding.get("unknown"):
        head += " · 확인 불가(입력 없음, 상태 유지)"
    head += f" · {_g(finding.get('title_ko'), 80)}"
    if not detail:
        return head
    evidence = [evidence_ref_gh(e) for e in _list(finding.get("evidence"))[:6] if isinstance(e, dict)]
    body = f"  {_g(finding.get('detail_ko'), 400)}"
    if evidence:
        body += " 근거: " + ", ".join(e for e in evidence if e)
    return head + "\n" + body


def _findings_section(report: Dict[str, Any], mode: int, keep_changed: Optional[int] = None) -> List[str]:
    """mode 0 full; 1 OPEN without detail; 2 OPEN as an id list; 3 changed without detail too;
    4 no OPEN list; ``keep_changed`` caps how many changed findings are listed."""
    findings = report["findings"]
    changed = [f for f in findings if f.get("tick_change") in sig.CHANGE_STATES]
    still = [f for f in findings if f.get("tick_change") not in sig.CHANGE_STATES]
    counts = report["open"]
    lines = ["### 발견 사항",
             f"이번 점검 변화 {len(changed)}건 · 열린 발견 {counts.get('open', 0)}건 (위험 {counts.get('at_risk', 0)}) · "
             f"확인 불가 {counts.get('unknown', 0)}건"]
    for info in report["info"][:5]:
        lines.append(f"- 정보: {_g(info.get('text_ko'), 200)}")
    shown = changed if keep_changed is None else changed[:keep_changed]
    for finding in shown:
        lines.append(_finding_line(finding, detail=mode < 3))
    if len(shown) < len(changed):
        lines.append(f"- 길이 제한으로 변화 {len(changed) - len(shown)}건 생략")
    if mode <= 1:
        for finding in still:
            lines.append(_finding_line(finding, detail=mode == 0))
    elif mode in (2, 3) and still:
        ids = [str(f.get("id")) for f in still if FINDING_ID_RE.fullmatch(str(f.get("id")))]
        lines.append(f"- 열린 발견(길이 제한으로 요약) {len(still)}건: " + ", ".join(ids))
    elif still:
        lines.append(f"- 열린 발견 {len(still)}건은 길이 제한으로 생략")
    if not findings:
        lines.append("- 없음")
    return lines


def _details(report: Dict[str, Any]) -> List[str]:
    stats = report["stats"]
    lines = ["<details><summary>기록 정보</summary>", "",
             f"- tool sha256: {_g(report['tool_sha256'], 80)}",
             f"- facts sha256: {_g(report['facts_sha256'], 80)}",
             f"- snapshot: {_g(report['snapshot'], 80)}"]
    if report["pngs"]:
        for png in report["pngs"]:
            lines.append(f"- 그림 {_g(png['name'], 70)}: {png['sha256']}")
    else:
        lines.append(f"- 그림 없음 ({_g(report.get('render_status') or 'NONE', 40)})")
    seconds = stats.get("seconds")
    seconds_text = f"{float(seconds):.1f}초" if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) \
        else "?"
    lines.append(f"- 수집: GitHub 요청 {_int(stats.get('github_calls')) or 0} · 호스트 호출 "
                 f"{_int(stats.get('host_calls')) or 0} · {seconds_text}")
    unknown = [f"{p['prefix']}: {', '.join(p['unknown_groups'])}" for p in report["products"] if p["unknown_groups"]]
    if report.get("lanes_unknown"):
        unknown.append("CTRL: lanes")
    lines.append("- 확인 불가 그룹: " + (_g("; ".join(unknown), 600) if unknown else "없음"))
    lines += ["", "</details>"]
    return lines


def render_comment(report: Dict[str, Any], limit: int = COMMENT_LIMIT) -> str:
    """The ledger comment, fitted to ``limit`` chars (OPEN details are dropped first)."""
    head = header_lines(report) + ["", f"{core.HOST_LINE} · 점검 {core.fmt_kst(_time(report['at']))} · "
                                       f"{report['stage']}", ""]
    table = ["### 제품별 판정"] + _product_table(report) + [""]
    lanes = ["### 레인", _g(" · ".join(lane_parts(report["lanes"], report.get("lanes_unknown"))), 900),
             "DEVIN에 먼저 몰리는 것은 정상(고정 순서)", ""]
    tail = _details(report)

    def build(mode: int, keep: Optional[int] = None) -> str:
        return "\n".join(head + table + _findings_section(report, mode, keep) + [""] + lanes + tail) + "\n"

    for mode in range(5):
        text = build(mode)
        if len(text) <= limit:
            return text
    changed = sum(1 for f in report["findings"] if f.get("tick_change") in sig.CHANGE_STATES)
    low, high = 0, changed
    while low < high:  # largest number of changed findings that still fits
        mid = (low + high + 1) // 2
        if len(build(4, mid)) <= limit:
            low = mid
        else:
            high = mid - 1
    text = build(4, low)
    if len(text) > limit:
        # CONTRACT NOTE: last resort; the two header lines and the host line always survive.
        text = text[: max(0, limit - 40)].rstrip() + "\n\n(길이 제한으로 잘림)\n"
    return text


def open_table(findings_state: Dict[str, Any], max_rows: int = 300) -> List[str]:
    """Rows of the open-findings table for the issue body (time-free, so a change means content changed)."""
    active = [f for f in _dict(findings_state.get("findings")).values()
              if isinstance(f, dict) and f.get("state") != "RESOLVED"]
    active.sort(key=lambda f: (-LEVEL_RANK.get(f.get("severity"), 0), str(f.get("id"))))
    rows = ["| ID | 수준 | 신호 | 제품 | 대상 | 제목 | 처음 관측 |", "|---|---|---|---|---|---|---|"]
    for finding in active[:max_rows]:
        fid = str(finding.get("id")) if FINDING_ID_RE.fullmatch(str(finding.get("id"))) else "INS-????"
        signal = finding.get("signal") if finding.get("signal") in SIGNAL_NAMES else "S?"
        first = _time(finding.get("first_seen"))
        level = f"{_word(finding.get('severity'))} {_code(finding.get('severity'))}"
        if finding.get("unknown"):
            level += " (확인 불가)"
        rows.append(f"| {fid} | {level} | {signal} {SIGNAL_NAMES.get(signal, '')} | "
                    f"{_g(finding.get('product'), 80)} | {_g(finding.get('subject_key'), 100)} | "
                    f"{_g(finding.get('title_ko'), 80)} | {core.fmt_kst(first) if first else '-'} |")
    if len(active) > max_rows:
        rows.append(f"| … | 외 {len(active) - max_rows}건 | | | | | |")
    if not active:
        rows.append("| - | 없음 | | | | | |")
    return rows


def render_issue_body(stage: str, now: datetime, run: str, findings_state: Dict[str, Any]) -> Tuple[str, str]:
    """(issue body, sha256 of its open-findings table). The body is replaced only when the table changes."""
    rows = open_table(findings_state)
    while True:
        table_sha = _sha("\n".join(rows))
        body = "\n".join([COMMENT_MARK,
                          f"AIOPS_INSPECT_V1 ledger stage={stage} advisory=true gate=none model=none", "",
                          f"{core.HOST_LINE}", "",
                          "### 열린 발견 사항",
                          f"마지막 갱신 점검 {core.fmt_kst(now)} · run {_g(run, 64)} · 단계 {stage}", ""] + rows) + "\n"
        if len(body) <= COMMENT_LIMIT or len(rows) <= 3:
            return body, table_sha
        rows = open_table(findings_state, max_rows=max(1, (len(rows) - 2) // 2))


# ---------------------------------------------------------------------------- Slack rendering

def _s(text: Any, limit: int = 300) -> str:
    return core.slack_text(text, limit)


def render_card(report: Dict[str, Any]) -> str:
    """The card text without the GitHub link (added at send time only when the comment is POSTED)."""
    lines = [f"AIOPS_INSPECT_V1 card · {core.fmt_kst(_time(report['at']))} · {report['stage']} · run {_s(report['run'], 64)}"]
    for product in report["products"] + [dict(report["ctrl"], name=CTRL, paused=False)]:
        verdict, previous = product.get("verdict"), product.get("previous")
        new_ids = [i for i in product.get("new_ids", []) if FINDING_ID_RE.fullmatch(str(i))]
        line = (f"{_s(product['name'], 80)}: {_word(verdict)} {_code(verdict)} {arrow(verdict, previous)} "
                f"(전: {_word(previous) if previous else '없음'})")
        if product.get("paused"):
            line += " · 계획 없음"
        line += " · 새 발견 " + (", ".join(new_ids[:8]) + (f" 외 {len(new_ids) - 8}" if len(new_ids) > 8 else "")
                               if new_ids else "없음")
        lines.append(line)
    for product in report["products"]:
        if product["paused"]:
            continue
        ladder = product["ladder"]
        lines.append(f"진행 {_s(product['name'], 80)}: " + " · ".join(ladder_parts(ladder))
                     + f" · 배포·실사용 검증 {NOT_RECORDED_WORD} · 계획 밖 변경 {_int(ladder.get('orphans')) or 0}"
                     + f" · 대기 계획 PR {_int(ladder.get('pending_plan_prs')) or 0}")
    lines.append("레인: " + _s(" · ".join(lane_parts(report["lanes"], report.get("lanes_unknown"))), 600))
    counts = report["open"]
    lines.append(f"열린 발견 {counts.get('open', 0)} (위험 {counts.get('at_risk', 0)})")
    lines.append(core.HOST_LINE)
    text = "\n".join(lines)
    if len(text) > MAX_CARD_CHARS:
        text = text[: MAX_CARD_CHARS - len(core.HOST_LINE) - 3].rstrip() + "…\n" + core.HOST_LINE
    return text


def card_with_link(card: str, comment_url: Optional[str]) -> str:
    if comment_url:
        return card + "\n" + core.slack_link(comment_url, "GitHub 기록")
    return card


def render_replies(report: Dict[str, Any]) -> List[Dict[str, str]]:
    """At most 10 thread replies, one per NEW/WORSENED/REOPENED finding, with clickable evidence."""
    out = []
    for finding in report["findings"]:
        if finding.get("tick_change") not in ALERT_CHANGES:
            continue
        fid = str(finding.get("id"))
        if not FINDING_ID_RE.fullmatch(fid):
            continue
        signal = finding.get("signal") if finding.get("signal") in SIGNAL_NAMES else "S?"
        severity = finding.get("severity")
        links = []
        for evidence in _list(finding.get("evidence"))[:6]:
            if not isinstance(evidence, dict):
                continue
            url = github_url(evidence, report.get("ledger_issue"), report.get("control_repository"))
            label = evidence_label(evidence)
            links.append(core.slack_link(url, label) if url else _s(label, 120))
        text = "\n".join([
            f"{fid} · {CHANGE_WORDS[finding['tick_change']]} · {_word(severity)} {_code(severity)} · "
            f"{signal} {SIGNAL_NAMES.get(signal, '')} · 근거 {_s(finding.get('basis'), 20)}",
            f"{_s(finding.get('title_ko'), 100)} — {_s(finding.get('detail_ko'), 600)}",
            f"대상: {_s(finding.get('subject_key'), 120)}",
            "근거 링크: " + (" · ".join(links) if links else "없음"),
        ])
        out.append({"finding": fid, "text": text[:MAX_REPLY_CHARS]})
        if len(out) >= MAX_REPLIES:
            break
    return out


def heartbeat_text(*, last_tick: datetime, status: str, reason: Optional[str], next_tick: datetime,
                   open_n: int, at_risk: int, unknown_m: int, token: Optional[str] = None,
                   halted_at: Optional[datetime] = None) -> str:
    """``AIOPS_INSPECT_V1 heartbeat · 마지막 점검 <KST> · 상태 ... · 다음 점검 <KST> · ...``."""
    if status not in HEARTBEAT_STATES:
        raise InspectError("PUBLISH_ARGS", "bad heartbeat status")
    word = status
    if status in ("DEGRADED", "HALTED"):
        word = f"{status}({reason if isinstance(reason, str) and CODE_RE.fullmatch(reason) else 'UNKNOWN'})"
    parts = ["AIOPS_INSPECT_V1 heartbeat", f"마지막 점검 {core.fmt_kst(last_tick)}", f"상태 {word}"]
    if status == "HALTED" and halted_at is not None:
        # CONTRACT NOTE: "HALTED since <KST>" is shown as its own segment after the state.
        parts.append(f"정지 시작 {core.fmt_kst(halted_at)}")
    parts += [f"다음 점검 {core.fmt_kst(next_tick)}", f"열린 발견 {int(open_n)} (위험 {int(at_risk)})",
              f"게시 미확인 {int(unknown_m)}"]
    if token:
        parts.append(f"토큰 만료 {token}")
    return " · ".join(parts)


def daily_text(now: datetime, ticks_ok: int, ticks: int, cards: int, open_n: int, unknown_m: int) -> str:
    return (f"AIOPS_INSPECT_V1 daily · {core.kst_day(now)} · 점검 {int(ticks_ok)}/{int(ticks)} · 카드 {int(cards)} · "
            f"열린 발견 {int(open_n)} · 게시 미확인 {int(unknown_m)}")


def deadman_text(hours: int, last_tick: datetime) -> str:
    return (f"AIOPS_INSPECT_V1 status=STALE · 감리가 {int(hours)}시간 넘게 점검하지 못했습니다 · "
            f"마지막 점검 {core.fmt_kst(last_tick)} · 그록봇에게 \"aiops-inspect status\" 실행을 지시하세요")


def recovered_text(gap_hours: int) -> str:
    return f"AIOPS_INSPECT_V1 status=RECOVERED · 공백 {int(gap_hours)}시간"


# ---------------------------------------------------------------------------- publisher (journal + Slack state)

def _step(**fields: Any) -> Dict[str, Any]:
    base = {"state": PENDING, "attempts": 0, "error": None, "unknown_at": None, "at": None}
    base.update(fields)
    return base


def _outcome(exc: InspectError) -> Optional[str]:
    if isinstance(exc, SlackError):
        return exc.outcome
    if exc.reason == "GITHUB_WRITE_UNKNOWN":
        return UNKNOWN
    if exc.reason == "GITHUB_WRITE_REFUSED":
        return REFUSED
    return None


class Publisher:
    """Posts through the journal and keeps the Slack heartbeat, daily line and dead-man state.

    State files (in the inspector state store): ``publish/<run>.json`` (journal), ``ledger_body.json``,
    ``heartbeat.json``, ``deadman.json``, ``daily.json``, ``slack_unknown.json``.
    """

    def __init__(self, store: core.StateStore, config: Dict[str, Any], *,
                 writer: Optional[gh.GitHubLedgerWriter] = None, slack: Optional[SlackClient] = None,
                 live_values: Iterable[str] = ()):
        self.store = store
        self.config = config
        self.writer = writer
        self.slack = slack
        self.live = tuple(v for v in live_values if isinstance(v, str))
        self.channel = core.active_channel(config)
        self.shape_hits = 0
        self._slack_state: Optional[str] = None  # None = not checked yet, "OK", or an error code

    # -- output hygiene and Slack readiness

    def _out(self, text: str) -> str:
        clean, hits = core.redact_output(text, self.live)
        self.shape_hits += hits
        return clean

    def _out_gh(self, render: Callable[[], Any], count: bool = True) -> Any:
        """Run a GitHub rendering with this publisher's live values (``_g`` raises SECRET_LIVE on one)
        and count the distinct token shapes ``_g`` redacted in it (``count=False`` for a re-render)."""
        global _GH_LIVE
        _GH_SHAPES_SEEN.clear()
        _GH_LIVE = self.live
        try:
            return render()
        finally:
            _GH_LIVE = ()
            if count:
                self.shape_hits += len(_GH_SHAPES_SEEN)
            _GH_SHAPES_SEEN.clear()

    def slack_ready(self) -> bool:
        """auth.test once per Publisher. SLACK_SCOPE propagates (kill switch); other failures -> False."""
        if self.slack is None:
            return False
        if self._slack_state is None:
            try:
                self.slack.verify()
                self._slack_state = "OK"
            except InspectError as exc:
                if exc.reason == "SLACK_SCOPE":
                    self._slack_state = exc.reason
                    raise
                self._slack_state = exc.reason
        return self._slack_state == "OK"

    @property
    def slack_status(self) -> Optional[str]:
        return self._slack_state

    # -- journal storage

    @staticmethod
    def _name(run: str) -> str:
        return f"publish/{run}"

    def journal_runs(self) -> List[str]:
        directory = self.store.dir / "publish"
        if not directory.is_dir():
            return []
        runs = []
        for path in directory.iterdir():
            name = path.name
            if name.endswith(".json") and not name.endswith(".pending.json") and \
                    core.RUN_ID_RE.fullmatch(name[:-5]) and stat.S_ISREG(os.lstat(str(path)).st_mode):
                runs.append(name[:-5])
        return sorted(runs)

    def load(self, run: str) -> Optional[Dict[str, Any]]:
        try:
            entry = self.store.read(self._name(run), None)
        except InspectError:
            return None
        if not isinstance(entry, dict) or entry.get("schema") != JOURNAL_SCHEMA or entry.get("run") != run:
            return None
        return entry

    def _save(self, entry: Dict[str, Any]) -> None:
        entry["state"] = overall_state(entry)
        self.store.write(self._name(entry["run"]), entry)

    def _log(self, entry: Dict[str, Any], step: str, now: datetime) -> None:
        entry.setdefault("log", []).append({"step": step, "at": core.iso(now)})

    def first_run(self) -> bool:
        return not self.journal_runs()

    # -- prepare

    def prepare(self, report: Dict[str, Any], now: datetime,
                findings_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Render every body, decide the issue-body update, and write the PREPARED journal."""
        run = report["run"]
        if findings_state is None:
            findings_state = _dict(report.get("findings_state"))
        if self.load(run) is not None or self.store.path(self._name(run)).exists():
            raise InspectError("JOURNAL_EXISTS", "a journal for this run already exists")
        # CONTRACT NOTE: journals are ordered by this sequence number, not by the time-based run id, so a
        # backward clock step can never make advance() supersede the report prepared last.
        seq = 1 + max([_seq(self.load(name)) for name in self.journal_runs()] or [0])
        comment = self._out(self._out_gh(lambda: render_comment(report)))
        if len(comment) > COMMENT_LIMIT:
            comment = self._out(self._out_gh(
                lambda: render_comment(report, COMMENT_LIMIT - (len(comment) - COMMENT_LIMIT) - 200), count=False))
        body, table_sha = self._out_gh(lambda: render_issue_body(report["stage"], now, run, findings_state))
        body = self._out(body)
        last_body = _dict(self.store.read("ledger_body", {}))
        body_state = UNCHANGED if last_body.get("table_sha256") == table_sha and \
            last_body.get("stage") == report["stage"] else PENDING
        card = self._out(render_card(report))
        replies = [_step(finding=r["finding"], text=self._out(r["text"]), sha256=None, ts=None)
                   for r in render_replies(report)]
        entry = {
            "schema": JOURNAL_SCHEMA, "run": run, "seq": seq, "started_at": core.iso(now), "stage": report["stage"],
            "channel": self.channel, "ledger_issue": report.get("ledger_issue"),
            "comment": _step(body=comment, sha256=_sha(comment), id=None, url=None),
            "issue_body": _step(body=body if body_state == PENDING else None, sha256=_sha(body),
                                table_sha256=table_sha, state=body_state),
            "card": _step(text=card, sha256=None, ts=None),
            "charts": _step(files=list(report["pngs"]), file_ids=[],
                            state=PENDING if report["pngs"] else SKIPPED),
            "replies": replies, "log": [],
        }
        self._log(entry, "PREPARED", now)
        self._save(entry)
        return entry

    def publish(self, report: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        """prepare() then advance(): the ledger comment, issue body, card, charts and replies of one run."""
        self.prepare(report, now)
        return self.advance(now)

    # -- one write attempt with SENDING persisted first

    def _attempt(self, entry: Dict[str, Any], step: Dict[str, Any], send: Callable[[], Dict[str, Any]],
                 now: datetime) -> Optional[str]:
        previous = (step["state"], step["attempts"])
        step["state"], step["attempts"], step["at"] = SENDING, step["attempts"] + 1, core.iso(now)
        self._save(entry)  # a crash from here on reads as UNKNOWN, never as "not sent"
        try:
            result = send()
        except InspectError as exc:
            outcome = _outcome(exc)
            if outcome is None:  # not a write outcome (e.g. SECRET_LIVE before sending): restore and raise
                step["state"], step["attempts"] = previous
                self._save(entry)
                raise
            step["error"] = exc.reason if not getattr(exc, "slack_error", None) else f"{exc.reason}:{exc.slack_error}"
            if outcome == UNKNOWN:
                step["state"], step["unknown_at"] = UNKNOWN, core.iso(now)
            elif exc.detail.endswith(NOT_CONNECTED):
                # CONTRACT NOTE: a connection that was never opened sent nothing and reached no server, so
                # it does not use up one of the 3 attempts; otherwise a ~2 h network outage would turn the
                # comment FAILED and drop the card and replies of that change for good. Newer journals
                # still supersede it and prune() removes it after 30 days.
                step["state"], step["attempts"] = REFUSED, previous[1]
                step["not_connected"] = (_int(step.get("not_connected")) or 0) + 1
            else:
                step["state"] = FAILED if step["attempts"] >= MAX_ATTEMPTS else REFUSED
            self._save(entry)
            return step["state"]
        step.update(result)
        step["state"], step["error"] = POSTED, None
        self._save(entry)
        return POSTED

    # -- advance

    def advance(self, now: datetime) -> Dict[str, Any]:
        """Run every pending or retryable step of the newest journal; older retryable steps are superseded."""
        runs = self.journal_runs()
        if not runs:
            return {"run": None}
        entries = {run: self.load(run) for run in runs}
        readable = sorted((r for r in runs if entries[r] is not None), key=lambda r: (_seq(entries[r]), r))
        # The newest is the highest sequence number; an unreadable journal whose run id sorts last still
        # blocks advancing, as before (it cannot be ordered).
        newest = readable[-1] if readable and entries[runs[-1]] is not None else None
        for run in readable:
            if run == newest:
                continue
            entry = entries[run]
            changed = False
            for step in _all_steps(entry):
                if step["state"] in RETRYABLE:
                    step["state"], changed = SUPERSEDED, True
            if changed:
                self._save(entry)
        if newest is None:
            return {"run": runs[-1], "state": "JOURNAL_UNREADABLE"}
        entry = entries[newest]
        self._recover_sending(entry, now)
        self._advance_github(entry, now)
        self._advance_slack(entry, now)
        return {"run": entry["run"], "state": entry["state"], "comment": entry["comment"]["state"],
                "issue_body": entry["issue_body"]["state"], "card": entry["card"]["state"],
                "charts": entry["charts"]["state"], "replies": [r["state"] for r in entry["replies"]],
                "comment_url": entry["comment"].get("url"), "slack": self._slack_state}

    def _recover_sending(self, entry: Dict[str, Any], now: datetime) -> bool:
        changed = False
        for step in _all_steps(entry):
            if step["state"] == SENDING:
                step["state"], step["unknown_at"] = UNKNOWN, step.get("at") or core.iso(now)
                step["error"] = "CRASH_WHILE_SENDING"
                changed = True
        if changed:
            self._save(entry)
        return changed

    def _advance_github(self, entry: Dict[str, Any], now: datetime) -> None:
        comment, body = entry["comment"], entry["issue_body"]
        if comment["state"] in RETRYABLE:
            if self.writer is None:
                raise InspectError("CONFIG", "no ledger writer")
            text = comment["body"]

            def send_comment() -> Dict[str, Any]:
                res = self.writer.create_comment(text)
                return {"id": res["id"], "url": res["html_url"], "created_at": res["created_at"]}

            state = self._attempt(entry, comment, send_comment, now)
            self._log(entry, {POSTED: "GH_POSTED", UNKNOWN: "GH_UNKNOWN"}.get(state, f"GH_{state}"), now)
            self._save(entry)
        if comment["state"] == FAILED:
            for step in [body, entry["card"], entry["charts"]] + entry["replies"]:
                if step["state"] in RETRYABLE:
                    step["state"], step["error"] = SKIPPED, "GITHUB_FAILED"
            self._save(entry)
            return
        if comment["state"] not in SETTLED_FOR_SLACK:
            return
        if body["state"] in RETRYABLE and body.get("body"):
            text = body["body"]
            state = self._attempt(entry, body, lambda: dict(self.writer.update_body(text)), now)
            self._log(entry, {POSTED: "GH_BODY_UPDATED", UNKNOWN: "GH_BODY_UNKNOWN"}.get(state, f"GH_BODY_{state}"),
                      now)
            self._save(entry)
            if state in (POSTED, UNKNOWN):
                # The same table is never PATCHed again, even when the outcome was unknown.
                self.store.write("ledger_body", {"table_sha256": body["table_sha256"], "stage": entry["stage"],
                                                 "run": entry["run"], "state": state, "at": core.iso(now)})

    def _advance_slack(self, entry: Dict[str, Any], now: datetime) -> None:
        comment, body, card = entry["comment"], entry["issue_body"], entry["card"]
        if comment["state"] not in SETTLED_FOR_SLACK or body["state"] in (PENDING, SENDING):
            return
        pending = [s for s in [card, entry["charts"]] + entry["replies"] if s["state"] in RETRYABLE]
        if not pending:
            return
        if not self.slack_ready():
            for step in pending:
                step["error"] = self._slack_state or "SLACK_UNAVAILABLE"
            self._save(entry)
            return
        channel = entry["channel"]
        if card["state"] in RETRYABLE:
            text = card_with_link(card["text"], comment.get("url") if comment["state"] == POSTED else None)
            card["sha256"] = _sha(text)
            state = self._attempt(entry, card, lambda: {"ts": self.slack.post_message(channel, text)["ts"]}, now)
            self._log(entry, {POSTED: "SLACK_POSTED", UNKNOWN: "SLACK_UNKNOWN"}.get(state, f"SLACK_{state}"), now)
            self._save(entry)
            if state == POSTED:
                self._count("cards")
            elif state == UNKNOWN:
                self._note_unknown("card", now)
        if card["state"] in (UNKNOWN, FAILED, ABANDONED):
            for step in [entry["charts"]] + entry["replies"]:
                if step["state"] in RETRYABLE:
                    step["state"], step["error"] = SKIPPED, "CARD_NOT_POSTED"
            self._save(entry)
            return
        if card["state"] != POSTED:
            return
        thread = card["ts"]
        charts = entry["charts"]
        if charts["state"] in RETRYABLE:
            files, problem = _load_pngs(charts["files"])
            if problem:
                charts["state"], charts["error"] = SKIPPED, problem
                self._save(entry)
            else:
                self._attempt(entry, charts, lambda: self.slack.upload_files(channel, thread, files), now)
        for reply in entry["replies"]:
            if reply["state"] in RETRYABLE:
                text = reply["text"]
                reply["sha256"] = _sha(text)
                self._attempt(entry, reply, lambda t=text: {"ts": self.slack.post_message(channel, t, thread)["ts"]},
                              now)

    # -- UNKNOWN reconciliation

    def reconcile_unknown(self, now: datetime) -> Dict[str, Any]:
        """Adopt UNKNOWN ledger comments whose body sha256 is now seen; abandon (display) after 24 h."""
        out = {"adopted": [], "abandoned": [], "pending": 0, "listed": None}
        entries = []
        for run in self.journal_runs():
            entry = self.load(run)
            if entry is None:
                continue
            self._recover_sending(entry, now)
            if entry["comment"]["state"] == UNKNOWN:
                entries.append(entry)
        if not entries:
            return out
        comments: Optional[List[Dict[str, Any]]] = None
        if self.writer is not None:
            since = min(_time(e["started_at"]) or core.utc(now) for e in entries) - RECONCILE_MARGIN
            try:
                comments = self.writer.comments_since(core.iso(since))
            except InspectError as exc:
                out["listed"] = exc.reason
        by_sha: Dict[str, Dict[str, Any]] = {}
        for item in comments or []:
            if isinstance(item.get("body"), str) and _int(item.get("id")):
                by_sha.setdefault(_sha(item["body"]), item)
        if comments is not None:
            out["listed"] = len(comments)
        for entry in entries:
            step = entry["comment"]
            seen = by_sha.get(step["sha256"])
            if seen is not None:
                url = seen.get("html_url") if isinstance(seen.get("html_url"), str) else None
                step.update(state=POSTED, id=seen["id"], url=url, adopted_at=core.iso(now),
                            created_at=seen.get("created_at") if isinstance(seen.get("created_at"), str) else None)
                self._log(entry, "GH_POSTED", now)
                out["adopted"].append(entry["run"])
            elif now - (_time(step.get("unknown_at")) or core.utc(now)) >= UNKNOWN_WINDOW:
                step["state"] = ABANDONED
                self._log(entry, "GH_ABANDONED_UNKNOWN", now)
                out["abandoned"].append(entry["run"])
            else:
                out["pending"] += 1
                continue
            self._save(entry)
        return out

    def unknown_count(self, now: datetime) -> int:
        """Writes whose outcome is still unknown and younger than 24 h (shown as 게시 미확인)."""
        count = 0
        for run in self.journal_runs():
            entry = self.load(run)
            if entry is None:
                continue
            for step in _all_steps(entry):
                if step["state"] in (UNKNOWN, SENDING):
                    at = _time(step.get("unknown_at") or step.get("at"))
                    if at is None or now - at < UNKNOWN_WINDOW:
                        count += 1
        for item in _list(self.store.read("slack_unknown", [])):
            at = _time(_dict(item).get("at"))
            if at is not None and now - at < UNKNOWN_WINDOW and _dict(item).get("kind") != "card":
                count += 1
        return count

    def _note_unknown(self, kind: str, now: datetime) -> None:
        if kind == "card":
            return  # cards are counted from the journal
        items = [i for i in _list(self.store.read("slack_unknown", [])) if isinstance(i, dict)
                 and (_time(i.get("at")) or now) > now - UNKNOWN_WINDOW]
        items.append({"kind": kind, "at": core.iso(now)})
        self.store.write("slack_unknown", items[-200:])

    def prune(self, now: datetime) -> int:
        """Remove journals older than 30 days."""
        removed = 0
        for run in self.journal_runs():
            entry = self.load(run)
            started = _time(entry.get("started_at")) if entry else None
            if started is not None and now - started > JOURNAL_KEEP:
                self.store.remove(self._name(run))
                removed += 1
        return removed

    # -- heartbeat

    def heartbeat(self, now: datetime, *, status: str = "OK", reason: Optional[str] = None,
                  last_tick: Optional[datetime] = None, next_tick: Optional[datetime] = None,
                  open_counts: Optional[Dict[str, int]] = None,
                  token_expiry: Optional[Dict[str, Optional[datetime]]] = None,
                  halted_at: Optional[datetime] = None) -> Dict[str, Any]:
        """Edit the one heartbeat message of the channel (create it once; message_not_found -> new)."""
        self._recover_heartbeat_create(now)
        if open_counts is None:
            open_counts = self._open_counts()
        text = self._out(heartbeat_text(
            last_tick=last_tick or now, status=status, reason=reason,
            next_tick=next_tick or next_tick_at(now, self.config.get("tick_minute", 17)),
            open_n=open_counts.get("open", 0), at_risk=open_counts.get("at_risk", 0),
            unknown_m=self.unknown_count(now), token=token_warning(token_expiry, now), halted_at=halted_at))
        if not self.slack_ready():
            return {"heartbeat": "SLACK_UNAVAILABLE", "reason": self._slack_state, "text": text}
        state = _dict(self.store.read("heartbeat", {}))
        entry = _dict(state.get(self.channel))
        ts = entry.get("ts") if isinstance(entry.get("ts"), str) and TS_RE.fullmatch(entry["ts"]) else None
        if ts is not None:
            try:
                self.slack.update_message(self.channel, ts, text)
                entry.update(updated_at=core.iso(now), last=POSTED, status=status)
                state[self.channel] = entry
                self.store.write("heartbeat", state)
                return {"heartbeat": "UPDATED", "ts": ts, "text": text}
            except SlackError as exc:
                if exc.slack_error != "message_not_found":
                    entry.update(last=exc.outcome, error=exc.slack_error or exc.reason, tried_at=core.iso(now))
                    state[self.channel] = entry
                    self.store.write("heartbeat", state)
                    return {"heartbeat": exc.outcome, "error": exc.slack_error or exc.reason, "text": text}
        # A kill after Slack took the message but before its ts is stored reads as an UNKNOWN create next tick.
        state[self.channel] = dict(entry, creating_at=core.iso(now))
        self.store.write("heartbeat", state)
        try:
            posted = self.slack.post_message(self.channel, text)
        except SlackError as exc:
            # CONTRACT NOTE: an UNKNOWN create is not retried with this text; the next tick's text differs
            # (new times), so creating a heartbeat then is not a resend.
            state[self.channel] = {"ts": None, "last": exc.outcome, "error": exc.slack_error or exc.reason,
                                   "tried_at": core.iso(now)}
            self.store.write("heartbeat", state)
            if exc.outcome == UNKNOWN:
                self._note_unknown("heartbeat", now)
            return {"heartbeat": exc.outcome, "error": exc.slack_error or exc.reason, "text": text}
        state[self.channel] = {"ts": posted["ts"], "created_at": core.iso(now), "updated_at": core.iso(now),
                               "last": POSTED, "status": status, "replaced": ts}
        self.store.write("heartbeat", state)
        return {"heartbeat": "CREATED", "ts": posted["ts"], "text": text}

    def _recover_heartbeat_create(self, now: datetime) -> None:
        """A create that was cut off (``creating_at`` left behind) is an UNKNOWN create (게시 미확인)."""
        state = _dict(self.store.read("heartbeat", {}))
        entry = _dict(state.get(self.channel))
        if not entry.get("creating_at"):
            return
        at = _time(entry.get("creating_at")) or core.utc(now)
        state[self.channel] = {"ts": None, "last": UNKNOWN, "error": "CRASH_WHILE_SENDING",
                               "tried_at": core.iso(at), "replaced": entry.get("ts") or entry.get("replaced")}
        self.store.write("heartbeat", state)
        self._note_unknown("heartbeat", at)

    def _open_counts(self) -> Dict[str, int]:
        try:
            findings = self.store.read("findings", None)
        except InspectError:
            return {"open": 0, "at_risk": 0, "unknown": 0}
        return sig.open_counts(findings) if isinstance(findings, dict) else {"open": 0, "at_risk": 0, "unknown": 0}

    # -- daily line

    def count_tick(self, ok: bool) -> None:
        """Count one tick for the next daily line."""
        self._count("ticks")
        if ok:
            self._count("ticks_ok")

    def _count(self, key: str) -> None:
        state = _dict(self.store.read("daily", {}))
        counters = _dict(state.get("counters"))
        counters[key] = (_int(counters.get(key)) or 0) + 1
        state["counters"] = counters
        self.store.write("daily", state)

    def daily_due(self, now: datetime) -> bool:
        state = _dict(self.store.read("daily", {}))
        local = core.utc(now).astimezone(core.KST)
        return local.hour >= int(self.config.get("daily_hour_kst", 9)) and state.get("last_day") != core.kst_day(now)

    def daily(self, now: datetime, open_counts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """Post the daily line at the first tick at or after daily_hour_kst each KST day."""
        self._recover_daily(now)
        if not self.daily_due(now):
            return {"daily": "NOT_DUE"}
        if not self.slack_ready():
            return {"daily": "SLACK_UNAVAILABLE", "reason": self._slack_state}
        state = _dict(self.store.read("daily", {}))
        counters = _dict(state.get("counters"))
        day = core.kst_day(now)
        attempts = (_int(state.get("attempts")) or 0) if state.get("attempt_day") == day else 0
        open_counts = open_counts if open_counts is not None else self._open_counts()
        text = self._out(daily_text(now, _int(counters.get("ticks_ok")) or 0, _int(counters.get("ticks")) or 0,
                                    _int(counters.get("cards")) or 0, open_counts.get("open", 0),
                                    self.unknown_count(now)))
        # Persisted first: a kill after Slack took the line reads as UNKNOWN next tick, never as "not sent".
        state["sending"] = {"day": day, "at": core.iso(now), "attempts": attempts + 1, "counters": dict(counters)}
        self.store.write("daily", state)
        not_connected = False
        try:
            self.slack.post_message(self.channel, text)
            outcome = POSTED
        except SlackError as exc:
            outcome = exc.outcome
            not_connected = outcome == REFUSED and exc.detail.endswith(NOT_CONNECTED)
        state.pop("sending", None)
        if not not_connected:  # a connection that was never opened is not an attempt (§9.2)
            attempts += 1
        if outcome == REFUSED and attempts < MAX_ATTEMPTS:
            state.update(attempt_day=day, attempts=attempts)
            self.store.write("daily", state)
            return {"daily": REFUSED, "text": text}
        if outcome == UNKNOWN:
            self._note_unknown("daily", now)
        # POSTED, UNKNOWN (never resent) or REFUSED for the last time: the day is closed.
        state = {"last_day": day, "last_outcome": outcome if outcome != REFUSED else FAILED,
                 "counters": {}, "attempt_day": day, "attempts": attempts}
        self.store.write("daily", state)
        return {"daily": outcome, "text": text}

    def _recover_daily(self, now: datetime) -> None:
        """Close the day of a daily line that was cut off while sending (UNKNOWN, never resent)."""
        state = _dict(self.store.read("daily", {}))
        sending = state.get("sending")
        if sending is None:
            return
        sending = _dict(sending)
        day = sending.get("day") if isinstance(sending.get("day"), str) else core.kst_day(now)
        # Ticks counted after the cut-off line stay for the next line.
        sent = _dict(sending.get("counters"))
        counters = {}
        for key, value in _dict(state.get("counters")).items():
            left = (_int(value) or 0) - (_int(sent.get(key)) or 0)
            if left > 0:
                counters[key] = left
        self.store.write("daily", {"last_day": day, "last_outcome": UNKNOWN, "counters": counters,
                                   "attempt_day": day, "attempts": _int(sending.get("attempts")) or 1})
        self._note_unknown("daily", _time(sending.get("at")) or core.utc(now))

    # -- dead-man

    def deadman(self, now: datetime, last_tick: Optional[datetime] = None) -> Dict[str, Any]:
        """Detect a fired STALE message (-> one RECOVERED line), schedule the next one, then delete the old.

        Order: schedule new -> persist its id -> delete previous; failed deletes stay in pending_delete.
        """
        now = core.utc(now)
        hours = int(self.config.get("deadman_hours", 3))
        st = self._deadman_state()
        out: Dict[str, Any] = {"fired": False, "recovered": None, "scheduled": None, "deleted": 0}
        if not self.slack_ready():
            out["scheduled"] = "SLACK_UNAVAILABLE"
            return out
        # 0. a crash while scheduling leaves an id we never learned: keep it for display/fired detection.
        if st.get("sending"):
            st["unknown"].append(dict(st["sending"]))
            st["sending"] = None
        # 1. fired detection: a scheduled message whose post_at passed without being deleted was sent.
        fired = []
        live = [st["current"]] if st["current"] else []
        for item in live + st["pending_delete"] + st["unknown"]:
            post_at = _time(item.get("post_at"))
            if post_at is not None and post_at <= now:
                fired.append(item)
        if fired:
            out["fired"] = True
            if st["current"] in fired:
                st["current"] = None
            st["pending_delete"] = [i for i in st["pending_delete"] if i not in fired]
            st["unknown"] = [i for i in st["unknown"] if i not in fired]
            # CONTRACT NOTE: the gap runs from the last recorded tick (not the fired message's own tick),
            # and a possibly-scheduled UNKNOWN message that may have fired also gets a RECOVERED line.
            if st.get("recover") is None:
                st["recover"] = {"since": st.get("last_tick") or fired[0].get("last_tick"), "attempts": 0}
        self.store.write("deadman", st)
        # 2. the RECOVERED line (REFUSED retried on later ticks, at most 3 attempts; UNKNOWN never resent).
        if st.get("recover") and st["recover"].get("sending"):
            # A RECOVERED line cut off while sending is UNKNOWN and never resent.
            self._note_unknown("recovered", _time(st["recover"]["sending"]) or now)
            st["recover"] = None
            out["recovered"] = UNKNOWN
            self.store.write("deadman", st)
        if st.get("recover"):
            since = _time(st["recover"].get("since"))
            gap = int(max(0.0, (now - since).total_seconds()) // 3600) if since else hours
            text = self._out(recovered_text(gap))
            st["recover"]["sending"] = core.iso(now)
            self.store.write("deadman", st)  # a kill after Slack took the line reads as UNKNOWN next tick
            not_connected = False
            try:
                self.slack.post_message(self.channel, text)
                outcome = POSTED
            except SlackError as exc:
                outcome = exc.outcome
                not_connected = outcome == REFUSED and exc.detail.endswith(NOT_CONNECTED)
            st["recover"]["sending"] = None
            if not not_connected:  # a connection that was never opened is not an attempt (§9.2)
                st["recover"]["attempts"] = (_int(st["recover"].get("attempts")) or 0) + 1
            if outcome == UNKNOWN:
                self._note_unknown("recovered", now)
            if outcome != REFUSED or st["recover"]["attempts"] >= MAX_ATTEMPTS:
                st["recover"] = None
            out["recovered"] = outcome
            self.store.write("deadman", st)
        # 3. schedule the next STALE message first and persist it.
        post_at = now + timedelta(hours=hours)
        text = self._out(deadman_text(hours, last_tick or now))
        st["sending"] = {"post_at": core.iso(post_at), "at": core.iso(now), "channel": self.channel,
                         "last_tick": core.iso(last_tick or now)}
        self.store.write("deadman", st)
        try:
            res = self.slack.schedule_message(self.channel, text, post_at)
        except SlackError as exc:
            pending = st.pop("sending")
            st["sending"] = None
            if exc.outcome == UNKNOWN:
                st["unknown"].append(pending)
            out["scheduled"] = exc.outcome
        else:
            old = st["current"]
            st["current"] = {"id": res["id"], "post_at": res["post_at"], "channel": self.channel,
                             "last_tick": core.iso(last_tick or now), "scheduled_at": core.iso(now)}
            st["sending"] = None
            if old:
                st["pending_delete"].append(dict(old, attempts=0))
            out["scheduled"] = POSTED
        st["last_tick"] = core.iso(last_tick or now)
        self.store.write("deadman", st)  # the new id is durable BEFORE any delete
        # 4. delete the previous message(s); failures stay pending and are retried every tick.
        out["deleted"] = self._delete_pending(st)
        out["pending_delete"] = len(st["pending_delete"])
        return out

    def cancel_deadman(self, now: datetime) -> Dict[str, Any]:
        """Delete every scheduled STALE message (``stop``)."""
        st = self._deadman_state()
        if not self.slack_ready():
            return {"cancelled": 0, "pending_delete": len(st["pending_delete"]) + (1 if st["current"] else 0),
                    "reason": self._slack_state}
        if st["current"]:
            st["pending_delete"].append(dict(st["current"], attempts=0))
            st["current"] = None
            self.store.write("deadman", st)
        deleted = self._delete_pending(st)
        return {"cancelled": deleted, "pending_delete": len(st["pending_delete"])}

    def _deadman_state(self) -> Dict[str, Any]:
        st = _dict(self.store.read("deadman", {}))
        st.setdefault("current", None)
        st["pending_delete"] = [i for i in _list(st.get("pending_delete")) if isinstance(i, dict)]
        st["unknown"] = [i for i in _list(st.get("unknown")) if isinstance(i, dict)]
        st.setdefault("sending", None)
        st.setdefault("recover", None)
        return st

    def _delete_pending(self, st: Dict[str, Any]) -> int:
        deleted = 0
        keep = []
        for item in st["pending_delete"]:
            try:
                self.slack.delete_scheduled(item.get("channel") or self.channel, item.get("id"))
                deleted += 1
            except InspectError as exc:
                # CONTRACT NOTE: Slack answers invalid_scheduled_message_id when the message is already gone.
                if getattr(exc, "slack_error", None) == "invalid_scheduled_message_id" or exc.reason == "SLACK_ARGS":
                    continue
                item["attempts"] = (_int(item.get("attempts")) or 0) + 1
                item["error"] = getattr(exc, "slack_error", None) or exc.reason
                keep.append(item)
        st["pending_delete"] = keep
        self.store.write("deadman", st)
        return deleted


def overall_state(entry: Dict[str, Any]) -> str:
    """The journal's contract step name: PREPARED -> GH_POSTED|GH_UNKNOWN -> GH_BODY_* -> SLACK_*."""
    comment, body, card = entry["comment"], entry["issue_body"], entry["card"]
    if card["state"] == POSTED:
        return "SLACK_POSTED"
    if card["state"] == UNKNOWN:
        return "SLACK_UNKNOWN"
    if body["state"] == POSTED:
        return "GH_BODY_UPDATED"
    if body["state"] == UNKNOWN:
        return "GH_BODY_UNKNOWN"
    return {POSTED: "GH_POSTED", UNKNOWN: "GH_UNKNOWN", ABANDONED: "GH_ABANDONED_UNKNOWN",
            FAILED: "GH_FAILED", REFUSED: "GH_REFUSED", SENDING: "GH_SENDING"}.get(comment["state"], "PREPARED")


def _seq(entry: Optional[Dict[str, Any]]) -> int:
    """The journal's sequence number (0 when missing or unreadable)."""
    return (_int(_dict(entry).get("seq")) or 0) if isinstance(entry, dict) else 0


def _all_steps(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [entry["comment"], entry["issue_body"], entry["card"], entry["charts"]] + list(entry.get("replies", []))


def _load_pngs(files: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Read the rendered PNGs and check name, signature, size and the journaled sha256."""
    out = []
    for item in files[:MAX_UPLOAD_FILES]:
        path = item.get("path")
        if not isinstance(path, str):
            return [], "CHART_MISSING"
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PNG_BYTES:
                return [], "CHART_INVALID"
            with open(path, "rb") as handle:
                data = handle.read(MAX_PNG_BYTES + 1)
        except OSError:
            return [], "CHART_MISSING"
        if not data.startswith(PNG_SIGNATURE) or core.sha256_hex(data) != item.get("sha256"):
            return [], "CHART_INVALID"
        out.append({"name": item["name"], "data": data, "title": item["name"][:-4]})
    return (out, None) if out else ([], "CHART_MISSING")
