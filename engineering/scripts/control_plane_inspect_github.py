"""Program inspector GitHub access: a read-only reader and the single ledger writer (§3.2).

The read-only program inspector (User decision M7, docs/INSPECTOR.md) is advisory only and never
a gate. ``GitHubReader`` issues GETs only, and only below ``/repos/<allowed repository>``.
``GitHubLedgerWriter`` is the ONLY GitHub write surface: it can create a comment on, and replace
the body of, the one configured ledger issue; no method takes a path or another issue number.

Every request goes through an injectable transport
``transport(method, url, headers, body_bytes_or_None, timeout) -> (status, headers_lowercase, body)``.
The default transport uses ``urllib.request``, never follows redirects, and tells a connection that
could not be opened (``NET_DOWN``: nothing was sent) from a failure after the connection was open
(``NET_UNKNOWN``: the request may have reached GitHub). Writes follow the UNKNOWN discipline: an
outcome that may have happened is reported as ``GITHUB_WRITE_UNKNOWN`` and must never be resent.
Tokens live only in request headers; they never appear in errors, reprs or return values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import http.client
import json
import re
import ssl
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

from control_plane_inspect_core import InspectError, REPO_RE, loads_strict, parse_iso, iso

API_BASE = "https://api.github.com"
API_HOST = "api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "aiops-inspect"
ACCEPT = "application/vnd.github+json"
DEFAULT_TIMEOUT = 30.0
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_WRITE_RESPONSE = 1024 * 1024
MAX_BODY_CHARS = 60000
LEDGER_PAGES = 10
# CONTRACT NOTE: ledger comments can reach 60,000 chars (~180 KB of Korean UTF-8); 30 per page keeps a
# page under the 8 MiB response cap, and 10 pages still cover 300 comments.
LEDGER_PER_PAGE = 30
MAX_ISSUE = 10 ** 9

Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, Dict[str, str], bytes]]

# Allowed path characters. "%" is refused so an encoded ".." can never leave the repository prefix.
# CONTRACT NOTE: refs or file names that would need percent-encoding are refused (PATH_NOT_ALLOWED).
PATH_CHARS_RE = re.compile(r"/[A-Za-z0-9_.~/@:,+=-]*")
PARAM_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\[\]]{0,63}")
TOKEN_RE = re.compile(r"[\x21-\x7e]{1,512}")
ETAG_RE = re.compile(r"[\x20-\x7e]{1,512}")
LINK_RE = re.compile(r'<([^>]*)>\s*((?:;\s*[A-Za-z]+\s*=\s*(?:"[^"]*"|[^;,\s]+)\s*)*)')
REL_RE = re.compile(r';\s*rel\s*=\s*(?:"([^"]*)"|([^;,\s]+))')
REPOSITORIES_ID_RE = re.compile(r"/repositories/[0-9]{1,20}((?:/.*)?)")
EXPIRY_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})\s*(UTC|Z|[+-]\d{2}:?\d{2})")


@dataclass(frozen=True)
class Resp:
    """One GET result. ``json`` is None for 304 and 404."""

    status: int
    json: Any
    etag: Optional[str]
    headers: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------- helpers

def parse_token_expiry(value: Any) -> Optional[datetime]:
    """Parse ``github-authentication-token-expiration`` (``2026-11-30 12:00:00 UTC`` or ``... +0900``)."""
    if not isinstance(value, str):
        return None
    match = EXPIRY_RE.fullmatch(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    if zone in ("UTC", "Z"):
        tz = timezone.utc
    else:
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        if offset >= timedelta(hours=24):
            return None
        tz = timezone(offset if zone[0] == "+" else -offset)
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=tz)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc)


def parse_link_next(value: Any) -> Optional[str]:
    """The ``rel="next"`` URL of a Link header, or None."""
    if not isinstance(value, str):
        return None
    for match in LINK_RE.finditer(value):
        url, params = match.group(1), match.group(2) or ""
        for rel in REL_RE.finditer(params):
            rels = (rel.group(1) if rel.group(1) is not None else rel.group(2) or "").split()
            if "next" in rels:
                return url.strip()
    return None


def _lower_headers(items: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in items:
        name = str(key).lower()
        out[name] = f"{out[name]}, {value}" if name in out else str(value)
    return out


def _check_token(token: Any, reason: str) -> str:
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise InspectError(reason, "token missing or malformed")
    return token


def _check_repository(repository: Any) -> str:
    if not isinstance(repository, str) or not REPO_RE.fullmatch(repository):
        raise InspectError("CONFIG", "repository must be owner/name")
    return repository


def _encode_params(params: Optional[Any]) -> List[Tuple[str, str]]:
    if params is None:
        return []
    items = list(params.items()) if isinstance(params, dict) else list(params)
    out: List[Tuple[str, str]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise InspectError("GITHUB_ARGS", "params must be key/value pairs")
        key, value = item
        if not isinstance(key, str) or not PARAM_KEY_RE.fullmatch(key):
            raise InspectError("GITHUB_ARGS", "bad parameter name")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise InspectError("GITHUB_ARGS", f"parameter {key} must be str or int")
        text = str(value)
        if len(text) > 512 or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
            raise InspectError("GITHUB_ARGS", f"parameter {key} is not plain text")
        out.append((key, text))
    return out


def _with_query(path: str, query: List[Tuple[str, str]]) -> str:
    return API_BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")


def _base_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION, "User-Agent": USER_AGENT}


# ---------------------------------------------------------------------------- default transport

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the Authorization header to an unchecked URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class _TrackedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that records when the TLS connection (through any proxy tunnel) is open."""

    def __init__(self, state: Dict[str, bool]):
        super().__init__(context=ssl.create_default_context())
        self._state = state

    def https_open(self, req):  # type: ignore[override]
        state = self._state

        class _Connection(http.client.HTTPSConnection):
            def connect(self) -> None:
                super().connect()
                state["connected"] = True

        return self.do_open(_Connection, req, context=self._context)


def _build_opener(state: Dict[str, bool]) -> Any:
    return urllib.request.build_opener(_TrackedHTTPSHandler(state), _NoRedirect())


class UrllibTransport:
    """Default transport (``urllib.request``); reads at most ``max_bytes + 1`` body bytes.

    Raises ``InspectError("NET_DOWN")`` when the connection could not be opened (nothing was sent)
    and ``InspectError("NET_UNKNOWN")`` for any failure after it was open (the request may have
    been received). HTTP error statuses are returned, not raised; redirects are not followed.
    """

    def __init__(self, max_bytes: int = MAX_READ_BYTES,
                 opener_factory: Optional[Callable[[Dict[str, bool]], Any]] = None):
        self.max_bytes = int(max_bytes)
        self._opener_factory = opener_factory or _build_opener

    def __repr__(self) -> str:
        return f"UrllibTransport(max_bytes={self.max_bytes})"

    def __call__(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
                 timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        if not isinstance(url, str) or not url.startswith(API_BASE + "/"):
            raise InspectError("PATH_NOT_ALLOWED", "transport only talks to the GitHub API over https")
        state = {"connected": False}
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        limit = self.max_bytes + 1
        try:
            opener = self._opener_factory(state)
            try:
                with opener.open(request, timeout=timeout) as response:
                    content = response.read(limit)
                    status = int(getattr(response, "status", None) or response.getcode())
                    return status, _lower_headers(response.headers.items()), content
            except urllib.error.HTTPError as exc:
                try:
                    content = exc.read(limit) or b""
                except (OSError, http.client.HTTPException, ValueError, AttributeError):
                    content = b""
                header_items = exc.headers.items() if exc.headers is not None else []
                status = int(exc.code)
                exc.close()
                return status, _lower_headers(header_items), content
        except (OSError, http.client.HTTPException) as exc:
            # URLError is an OSError; ssl, socket timeout and reset errors are OSErrors too.
            name = type(getattr(exc, "reason", exc)).__name__
            if state["connected"]:
                raise InspectError("NET_UNKNOWN", f"{method} failed after connect: {name}") from None
            raise InspectError("NET_DOWN", f"{method} could not connect: {name}") from None


# ---------------------------------------------------------------------------- reader

class GitHubReader:
    """Read-only GitHub client limited to ``/repos/<one of repositories>``.

    ``calls`` counts every request handed to the transport; ``token_expiry`` is the parsed
    ``github-authentication-token-expiration`` header (None until a response carries it);
    ``last_truncated`` is True when the last ``paginate`` stopped at ``max_pages``.
    """

    def __init__(self, token: str, repositories: Iterable[str], transport: Optional[Transport] = None,
                 max_bytes: int = MAX_READ_BYTES, timeout: float = DEFAULT_TIMEOUT,
                 max_calls: Optional[int] = None):
        self._token = _check_token(token, "SECRET_GH_READ")
        if isinstance(repositories, str):
            repositories = [repositories]
        repos = tuple(_check_repository(r) for r in repositories)
        if not repos:
            raise InspectError("CONFIG", "no repositories")
        self.repositories = repos
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise InspectError("GITHUB_ARGS", "max_bytes must be a positive int")
        self.max_bytes = max_bytes
        self.timeout = float(timeout)
        # CONTRACT NOTE: optional request budget (None = unlimited); exceeding it is GITHUB_BUDGET.
        self.max_calls = max_calls
        self._transport: Transport = transport if transport is not None else UrllibTransport(max_bytes)
        self.calls = 0
        self.token_expiry: Optional[datetime] = None
        self.last_truncated = False

    def __repr__(self) -> str:
        return f"GitHubReader(repositories={list(self.repositories)!r}, calls={self.calls})"

    # -- path policy

    def _repo_of(self, path: str) -> Optional[str]:
        for repo in self.repositories:
            prefix = f"/repos/{repo}"
            if path == prefix or path.startswith(prefix + "/"):
                return repo
        return None

    def check_path(self, path: Any) -> str:
        """Return ``path`` when it is a plain API path below an allowed repository."""
        # CONTRACT NOTE: "/repos/<repo>" itself is allowed too (T0 needs GET /repos/{r}).
        if not isinstance(path, str) or len(path) > 2048 or not PATH_CHARS_RE.fullmatch(path):
            raise InspectError("PATH_NOT_ALLOWED", "path is not a plain API path")
        segments = path.split("/")[1:]
        if any(seg in ("", ".", "..") for seg in segments):
            raise InspectError("PATH_NOT_ALLOWED", "path has an empty or dot segment")
        if self._repo_of(path) is None:
            raise InspectError("PATH_NOT_ALLOWED", "path is outside the allowed repositories")
        return path

    # -- requests

    def _send(self, path: str, query: List[Tuple[str, str]], etag: Optional[str]) -> Resp:
        headers = _base_headers(self._token)
        if etag is not None:
            if not isinstance(etag, str) or not ETAG_RE.fullmatch(etag):
                raise InspectError("GITHUB_ARGS", "bad etag")
            headers["If-None-Match"] = etag
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise InspectError("GITHUB_BUDGET", "GitHub request budget exhausted")
        self.calls += 1
        status, rheaders, body = self._transport("GET", _with_query(path, query), headers, None, self.timeout)
        rheaders = _lower_headers(rheaders.items()) if isinstance(rheaders, dict) else {}
        if "github-authentication-token-expiration" in rheaders:
            # CONTRACT NOTE: updated only from responses that carry the header; unparseable -> None.
            self.token_expiry = parse_token_expiry(rheaders["github-authentication-token-expiration"])
        body = body if isinstance(body, (bytes, bytearray)) else b""
        where = f"GET {path} -> {status}"
        if len(body) > self.max_bytes:
            raise InspectError("MAX_RESPONSE", f"GET {path}: response larger than {self.max_bytes} bytes")
        if status == 304:
            return Resp(304, None, rheaders.get("etag") or etag, rheaders)
        if status == 404:
            return Resp(404, None, None, rheaders)
        if status in (403, 429) and (rheaders.get("x-ratelimit-remaining", "").strip() == "0"
                                     or "retry-after" in rheaders):
            raise InspectError("GITHUB_RATE_LIMIT", where)
        if 500 <= status <= 599:
            raise InspectError("GITHUB_5XX", where)
        if not 200 <= status <= 299:
            # CONTRACT NOTE: 1xx/3xx (redirects are never followed) are treated like other errors.
            raise InspectError("GITHUB_READ", where)
        if status == 204 and not body:
            return Resp(status, None, rheaders.get("etag"), rheaders)
        try:
            data = loads_strict(bytes(body))
        except (ValueError, UnicodeDecodeError):
            raise InspectError("GITHUB_JSON", f"GET {path}: body is not JSON") from None
        return Resp(status, data, rheaders.get("etag"), rheaders)

    def get(self, path: str, params: Optional[Any] = None, etag: Optional[str] = None) -> Resp:
        """GET one allowed path; 304 and 404 are returned, every other failure raises InspectError."""
        return self._send(self.check_path(path), _encode_params(params), etag)

    def _next_query(self, path: str, link: str) -> List[Tuple[str, str]]:
        """Query of a ``rel="next"`` link that stays on the API host and on ``path``."""
        parts = urllib.parse.urlsplit(link)
        if parts.scheme != "https" or parts.netloc.lower() != API_HOST or parts.fragment:
            raise InspectError("PATH_NOT_ALLOWED", "pagination link leaves the GitHub API host")
        next_path = parts.path
        same = next_path.lower() == path.lower()
        if not same:
            # GitHub often writes next links as /repositories/<id>/<rest>; accept only the same <rest>.
            match = REPOSITORIES_ID_RE.fullmatch(next_path)
            repo = self._repo_of(path) or ""
            same = bool(match) and match.group(1) == path[len(f"/repos/{repo}"):]
        if not same:
            raise InspectError("PATH_NOT_ALLOWED", "pagination link leaves the requested path")
        # CONTRACT NOTE: only the link's query is used; the request goes to the original checked path.
        return _encode_params(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))

    def paginate(self, path: str, params: Optional[Any] = None, max_pages: int = 10,
                 key: Optional[str] = None) -> List[Any]:
        """Follow ``Link: rel="next"`` for up to ``max_pages`` pages and concatenate the items.

        Each page must be a JSON list, or a dict whose ``key`` is a list (check-runs, workflow runs).
        """
        # CONTRACT NOTE: stopping at max_pages is not an error; ``last_truncated`` says it happened.
        # A 304 or 404 on any page raises GITHUB_READ: a missing list must never read as an empty one.
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise InspectError("GITHUB_ARGS", "max_pages must be a positive int")
        path = self.check_path(path)
        query = _encode_params(params)
        items: List[Any] = []
        seen = set()
        self.last_truncated = False
        for page in range(max_pages):
            signature = tuple(query)
            if signature in seen:
                raise InspectError("GITHUB_READ", f"GET {path}: pagination loop")
            seen.add(signature)
            resp = self._send(path, query, None)
            if resp.status != 200 and resp.status != 204:
                raise InspectError("GITHUB_READ", f"GET {path} -> {resp.status} while paginating")
            data = resp.json
            if key is not None:
                data = data.get(key) if isinstance(data, dict) else None
            if not isinstance(data, list):
                raise InspectError("GITHUB_JSON", f"GET {path}: page is not a list")
            items.extend(data)
            link = parse_link_next(resp.headers.get("link"))
            if link is None:
                return items
            query = self._next_query(path, link)
        self.last_truncated = True
        return items


# ---------------------------------------------------------------------------- ledger writer

class GitHubLedgerWriter:
    """The only GitHub write surface: comments on, and the body of, ONE configured issue.

    Write outcomes: 2xx -> ok; 4xx or NET_DOWN (nothing sent) -> ``GITHUB_WRITE_REFUSED``;
    5xx, NET_UNKNOWN or anything else -> ``GITHUB_WRITE_UNKNOWN`` (never resend the same content).
    """

    def __init__(self, token: str, repository: str, issue_number: int, transport: Optional[Transport] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self._token = _check_token(token, "SECRET_GH_LEDGER")
        self.repository = _check_repository(repository)
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or not 0 < issue_number <= MAX_ISSUE:
            raise InspectError("CONFIG", "ledger issue number must be a positive int")
        self.issue_number = issue_number
        self.timeout = float(timeout)
        # The default transport also serves comments_since pages, so it reads up to the reader cap.
        self._transport: Transport = transport if transport is not None else UrllibTransport(MAX_READ_BYTES)
        self._issue_path = f"/repos/{repository}/issues/{issue_number}"
        self._comments_path = f"{self._issue_path}/comments"
        self._reader = GitHubReader(token, [repository], self._transport, timeout=timeout)
        self._reader.check_path(self._comments_path)
        self.writes = 0
        self.token_expiry: Optional[datetime] = None
        self.last_truncated = False

    def __repr__(self) -> str:
        return f"GitHubLedgerWriter(repository={self.repository!r}, issue={self.issue_number})"

    @property
    def calls(self) -> int:
        return self.writes + self._reader.calls

    def _write(self, method: str, path: str, body: Any) -> Tuple[int, Dict[str, str], bytes]:
        # CONTRACT NOTE: a bad or oversize body is GITHUB_WRITE_REFUSED (nothing was sent).
        if not isinstance(body, str) or not body.strip():
            raise InspectError("GITHUB_WRITE_REFUSED", "body must be a non-empty string")
        if len(body) > MAX_BODY_CHARS:
            raise InspectError("GITHUB_WRITE_REFUSED", f"body longer than {MAX_BODY_CHARS} chars")
        payload = json.dumps({"body": body}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = _base_headers(self._token)
        headers["Content-Type"] = "application/json"
        where = f"{method} ledger issue {self.issue_number}"
        self.writes += 1
        try:
            status, rheaders, content = self._transport(method, API_BASE + path, headers, payload, self.timeout)
        except InspectError as exc:
            if exc.reason == "NET_DOWN":
                raise InspectError("GITHUB_WRITE_REFUSED", f"{where}: connection not opened") from None
            raise InspectError("GITHUB_WRITE_UNKNOWN", f"{where}: {exc.reason}") from None
        except Exception as exc:  # noqa: BLE001 - any other transport failure may have sent the request
            raise InspectError("GITHUB_WRITE_UNKNOWN", f"{where}: {type(exc).__name__}") from None
        rheaders = _lower_headers(rheaders.items()) if isinstance(rheaders, dict) else {}
        if "github-authentication-token-expiration" in rheaders:
            self.token_expiry = parse_token_expiry(rheaders["github-authentication-token-expiration"])
        if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 499:
            raise InspectError("GITHUB_WRITE_REFUSED", f"{where} -> {status}")
        if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status <= 299:
            # CONTRACT NOTE: 5xx and anything that is neither 2xx nor 4xx count as UNKNOWN.
            raise InspectError("GITHUB_WRITE_UNKNOWN", f"{where} -> {status}")
        return status, rheaders, content if isinstance(content, (bytes, bytearray)) else b""

    def create_comment(self, body: str) -> Dict[str, Any]:
        """POST one comment on the ledger issue -> ``{"id", "html_url", "created_at"}``."""
        _, _, content = self._write("POST", self._comments_path, body)
        # CONTRACT NOTE: a 2xx whose response cannot be read means the comment probably exists
        # but its id is unknown -> GITHUB_WRITE_UNKNOWN (reconciled later by body sha256).
        try:
            if len(content) > MAX_WRITE_RESPONSE:
                raise ValueError("oversize")
            data = loads_strict(bytes(content))
        except (ValueError, UnicodeDecodeError):
            raise InspectError("GITHUB_WRITE_UNKNOWN", "comment response is not JSON") from None
        comment_id = data.get("id") if isinstance(data, dict) else None
        html_url = data.get("html_url") if isinstance(data, dict) else None
        created_at = data.get("created_at") if isinstance(data, dict) else None
        if (isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0
                or not isinstance(html_url, str) or not isinstance(created_at, str)):
            raise InspectError("GITHUB_WRITE_UNKNOWN", "comment response lacks id/html_url/created_at")
        return {"id": comment_id, "html_url": html_url, "created_at": created_at}

    def update_body(self, body: str) -> Dict[str, Any]:
        """PATCH the ledger issue with exactly ``{"body": body}`` -> ``{"updated_at": str|None}``."""
        _, _, content = self._write("PATCH", self._issue_path, body)
        updated_at = None
        try:
            data = loads_strict(bytes(content)) if content and len(content) <= MAX_WRITE_RESPONSE else None
            if isinstance(data, dict) and isinstance(data.get("updated_at"), str):
                updated_at = data["updated_at"]
        except (ValueError, UnicodeDecodeError):
            updated_at = None
        return {"updated_at": updated_at}

    def comments_since(self, since_iso: str) -> List[Dict[str, Any]]:
        """Ledger-issue comments updated at or after ``since_iso`` (read; at most 10 pages)."""
        since = iso(parse_iso(since_iso))
        items = self._reader.paginate(self._comments_path, {"since": since, "per_page": LEDGER_PER_PAGE},
                                      LEDGER_PAGES)
        self.last_truncated = self._reader.last_truncated
        if self._reader.token_expiry is not None:
            self.token_expiry = self._reader.token_expiry
        return [item for item in items if isinstance(item, dict)]
