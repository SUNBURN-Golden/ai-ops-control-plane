from __future__ import annotations

from datetime import datetime, timezone
import http.client
import inspect
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_plane_inspect_github as gh  # noqa: E402
from control_plane_inspect_core import InspectError  # noqa: E402

TOKEN = "github_pat_" + "Z9y8X7w6V5" * 6
REPO = "BeautifulMind-JT/ZARI"
OTHER = "BeautifulMind-JT/kix-protocol"
CTRL = "BeautifulMind-JT/ai-ops-control-plane"
API = "https://api.github.com"


class FakeTransport:
    """Records every call; answers from a queue of (status, headers, body) or exceptions."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def push(self, *responses):
        self.responses.extend(responses)

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body,
                           "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected request " + method + " " + url)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, headers_out, payload = item
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode("utf-8")
        return status, headers_out, payload


def ok(data, **headers):
    return (200, {k.replace("_", "-"): v for k, v in headers.items()}, data)


def path_and_query(url):
    parts = urllib.parse.urlsplit(url)
    return parts.path, urllib.parse.parse_qsl(parts.query, keep_blank_values=True)


class ReaderTests(unittest.TestCase):
    def reader(self, *responses, **kwargs):
        t = FakeTransport(*responses)
        return gh.GitHubReader(TOKEN, [REPO, CTRL], transport=t, **kwargs), t

    def assertReason(self, reason, fn, *args, **kwargs):
        with self.assertRaises(InspectError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.reason, reason)
        self.assertNotIn(TOKEN, str(ctx.exception) + ctx.exception.detail)
        return ctx.exception

    def test_get_sends_fixed_headers_and_query(self):
        r, t = self.reader(ok({"default_branch": "main"}, etag='W/"abc"'))
        resp = r.get(f"/repos/{REPO}/issues", {"labels": "aiops-task", "state": "all", "per_page": 50})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json, {"default_branch": "main"})
        self.assertEqual(resp.etag, 'W/"abc"')
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertIsNone(call["body"])
        self.assertEqual(call["url"], f"{API}/repos/{REPO}/issues?labels=aiops-task&state=all&per_page=50")
        self.assertEqual(call["headers"], {
            "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiops-inspect"})
        self.assertEqual(r.calls, 1)

    def test_repo_root_path_is_allowed(self):
        r, t = self.reader(ok({"default_branch": "main"}))
        self.assertEqual(r.get(f"/repos/{REPO}").json["default_branch"], "main")
        self.assertEqual(t.calls[0]["url"], f"{API}/repos/{REPO}")

    def test_path_allowlist(self):
        r, t = self.reader()
        bad = [
            f"/repos/{OTHER}/issues", f"/repos/{REPO}-evil/issues", f"/repos/{REPO}x", "/user", "/",
            f"/repos/{REPO}/../kix-protocol/issues", f"/repos/{REPO}/%2e%2e/x", f"/repos/{REPO}/issues?x=1",
            f"/repos/{REPO}/issues#frag", f"/repos/{REPO}//issues", f"https://api.github.com/repos/{REPO}/issues",
            f"repos/{REPO}/issues", f"/repos/{REPO}/issues\n", f"/repos/{REPO}/a b", f"/repos/{REPO}/./x",
            "/repos/beautifulmind-jt/zari/issues", "/repositories/123/issues", None, 5,
        ]
        for path in bad:
            with self.subTest(path=path):
                self.assertReason("PATH_NOT_ALLOWED", r.get, path)
        self.assertEqual(t.calls, [])
        self.assertEqual(r.calls, 0)

    def test_conditional_get_and_304(self):
        r, t = self.reader((304, {"etag": '"e2"'}, b""), (304, {}, b""))
        resp = r.get(f"/repos/{REPO}/branches/main", etag='"e1"')
        self.assertEqual(t.calls[0]["headers"]["If-None-Match"], '"e1"')
        self.assertEqual((resp.status, resp.json, resp.etag), (304, None, '"e2"'))
        resp = r.get(f"/repos/{REPO}/branches/main", etag='"e1"')
        self.assertEqual((resp.status, resp.json, resp.etag), (304, None, '"e1"'))
        self.assertReason("GITHUB_ARGS", r.get, f"/repos/{REPO}/branches/main", etag='"x"\r\nX-Evil: 1')

    def test_no_etag_header_without_etag(self):
        r, t = self.reader(ok([]))
        r.get(f"/repos/{REPO}/pulls")
        self.assertNotIn("If-None-Match", t.calls[0]["headers"])

    def test_404(self):
        r, _ = self.reader((404, {"etag": '"x"'}, {"message": "Not Found"}))
        resp = r.get(f"/repos/{REPO}/contents/.aiops/program.json", {"ref": "abc"})
        self.assertEqual((resp.status, resp.json, resp.etag), (404, None, None))

    def test_rate_limit(self):
        cases = [(403, {"x-ratelimit-remaining": "0"}), (429, {"retry-after": "60"}),
                 (403, {"retry-after": "5"}), (429, {"x-ratelimit-remaining": "0"})]
        for status, headers in cases:
            with self.subTest(status=status, headers=headers):
                r, _ = self.reader((status, headers, {"message": "rate"}))
                self.assertReason("GITHUB_RATE_LIMIT", r.get, f"/repos/{REPO}")

    def test_other_errors(self):
        cases = [(403, {"x-ratelimit-remaining": "12"}, "GITHUB_READ"), (429, {}, "GITHUB_READ"),
                 (400, {}, "GITHUB_READ"), (401, {}, "GITHUB_READ"), (422, {}, "GITHUB_READ"),
                 (301, {"location": "https://evil.example/"}, "GITHUB_READ"), (100, {}, "GITHUB_READ"),
                 (500, {}, "GITHUB_5XX"), (502, {}, "GITHUB_5XX"), (503, {"retry-after": "1"}, "GITHUB_5XX"),
                 (599, {}, "GITHUB_5XX")]
        for status, headers, reason in cases:
            with self.subTest(status=status):
                r, _ = self.reader((status, headers, b"{}"))
                self.assertReason(reason, r.get, f"/repos/{REPO}")

    def test_response_size_cap(self):
        r, _ = self.reader(ok(b'"' + b"a" * 98 + b'"'), ok(b'"' + b"a" * 99 + b'"'), max_bytes=100)
        self.assertEqual(len(r.get(f"/repos/{REPO}").json), 98)
        self.assertReason("MAX_RESPONSE", r.get, f"/repos/{REPO}")
        r, _ = self.reader((404, {}, b"x" * 101), max_bytes=100)
        self.assertReason("MAX_RESPONSE", r.get, f"/repos/{REPO}")

    def test_bad_json(self):
        for payload in (b"{not json", b'{"a": 1, "a": 2}', b"\xff\xfe", b"", b"NaN"):
            with self.subTest(payload=payload):
                r, _ = self.reader(ok(payload))
                self.assertReason("GITHUB_JSON", r.get, f"/repos/{REPO}")

    def test_token_expiry(self):
        r, _ = self.reader(ok({}), ok({}, github_authentication_token_expiration="2026-11-30 12:00:00 UTC"),
                           ok({}), ok({}, github_authentication_token_expiration="2026-11-30 21:00:00 +0900"),
                           ok({}, github_authentication_token_expiration="soon"))
        r.get(f"/repos/{REPO}")
        self.assertIsNone(r.token_expiry)
        r.get(f"/repos/{REPO}")
        expected = datetime(2026, 11, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(r.token_expiry, expected)
        r.get(f"/repos/{REPO}")
        self.assertEqual(r.token_expiry, expected)
        r.get(f"/repos/{REPO}")
        self.assertEqual(r.token_expiry, expected)
        r.get(f"/repos/{REPO}")
        self.assertIsNone(r.token_expiry)

    def test_parse_token_expiry(self):
        self.assertEqual(gh.parse_token_expiry("2026-01-02 03:04:05 -0130"),
                         datetime(2026, 1, 2, 4, 34, 5, tzinfo=timezone.utc))
        for bad in (None, "", "2026-13-01 00:00:00 UTC", "2026-01-01 00:00:00", "2026-01-01 00:00:00 +9900"):
            self.assertIsNone(gh.parse_token_expiry(bad))

    def test_calls_counted_even_on_failure_and_budget(self):
        r, _ = self.reader((500, {}, b""), InspectError("NET_DOWN"), ok({}), max_calls=3)
        self.assertReason("GITHUB_5XX", r.get, f"/repos/{REPO}")
        self.assertReason("NET_DOWN", r.get, f"/repos/{REPO}")
        r.get(f"/repos/{REPO}")
        self.assertEqual(r.calls, 3)
        self.assertReason("GITHUB_BUDGET", r.get, f"/repos/{REPO}")
        self.assertEqual(r.calls, 3)

    def test_bad_params(self):
        r, t = self.reader()
        for params in ({"x": True}, {"x": None}, {"x": [1]}, {"x": "a\nb"}, {"bad key": 1}, {1: "a"}, ["x"]):
            with self.subTest(params=params):
                self.assertReason("GITHUB_ARGS", r.get, f"/repos/{REPO}/issues", params)
        self.assertEqual(t.calls, [])

    def test_constructor_validation_and_repr(self):
        for token in ("", "has space", None, "a\nb"):
            with self.subTest(token=token):
                with self.assertRaises(InspectError) as ctx:
                    gh.GitHubReader(token, [REPO], transport=FakeTransport())
                self.assertEqual(ctx.exception.reason, "SECRET_GH_READ")
        for repos in ([], ["not-a-repo"], ["a/b/c"]):
            with self.assertRaises(InspectError) as ctx:
                gh.GitHubReader(TOKEN, repos, transport=FakeTransport())
            self.assertEqual(ctx.exception.reason, "CONFIG")
        r = gh.GitHubReader(TOKEN, REPO, transport=FakeTransport())
        self.assertEqual(r.repositories, (REPO,))
        self.assertNotIn(TOKEN, repr(r))
        self.assertNotIn(TOKEN, repr(vars(r).get("repositories")))

    def test_errors_never_carry_the_token(self):
        r, _ = self.reader((500, {}, TOKEN.encode()), ok(TOKEN.encode()), (418, {}, TOKEN.encode()))
        for reason in ("GITHUB_5XX", "GITHUB_JSON", "GITHUB_READ"):
            self.assertReason(reason, r.get, f"/repos/{REPO}")


class PaginateTests(unittest.TestCase):
    def reader(self, *responses):
        t = FakeTransport(*responses)
        return gh.GitHubReader(TOKEN, [REPO], transport=t), t

    def test_follows_next_on_same_path(self):
        path = f"/repos/{REPO}/issues/comments"
        r, t = self.reader(
            ok([1, 2], link=f'<{API}{path}?since=x&per_page=2&page=2>; rel="next", <{API}{path}?page=3>; rel="last"'),
            ok([3], link=f'<{API}{path}?page=1>; rel="first", <{API}{path}?page=2>; rel="prev"'))
        self.assertEqual(r.paginate(path, {"since": "x", "per_page": 2}, 5), [1, 2, 3])
        self.assertFalse(r.last_truncated)
        self.assertEqual(path_and_query(t.calls[1]["url"]), (path, [("since", "x"), ("per_page", "2"), ("page", "2")]))
        self.assertEqual(r.calls, 2)

    def test_repositories_id_form_maps_back_to_checked_path(self):
        path = f"/repos/{REPO}/issues/comments"
        r, t = self.reader(ok([1], link=f'<{API}/repositories/123456/issues/comments?page=2>; rel="next"'),
                           ok([2]))
        self.assertEqual(r.paginate(path, None, 5), [1, 2])
        self.assertEqual(t.calls[1]["url"], f"{API}{path}?page=2")

    def test_foreign_links_refused(self):
        path = f"/repos/{REPO}/pulls"
        links = [
            f"https://evil.example{path}?page=2",
            f"http://api.github.com{path}?page=2",
            f"https://api.github.com:8443{path}?page=2",
            f"https://user@api.github.com{path}?page=2",
            f"{API}/repos/{OTHER}/pulls?page=2",
            f"{API}/repos/{REPO}/issues?page=2",
            f"{API}/repositories/1/issues?page=2",
            f"{API}/repositories/1/pulls/../../x?page=2",
            f"{API}{path}?page=2#frag",
            f"//api.github.com{path}?page=2",
        ]
        for link in links:
            with self.subTest(link=link):
                r, t = self.reader(ok([1], link=f'<{link}>; rel="next"'))
                with self.assertRaises(InspectError) as ctx:
                    r.paginate(path, None, 5)
                self.assertEqual(ctx.exception.reason, "PATH_NOT_ALLOWED")
                self.assertEqual(len(t.calls), 1)

    def test_max_pages_truncates(self):
        path = f"/repos/{REPO}/pulls"
        r, t = self.reader(*[ok([i], link=f'<{API}{path}?page={i + 2}>; rel="next"') for i in range(3)])
        self.assertEqual(r.paginate(path, None, 3), [0, 1, 2])
        self.assertTrue(r.last_truncated)
        self.assertEqual(len(t.calls), 3)

    def test_loop_detected(self):
        path = f"/repos/{REPO}/pulls"
        r, _ = self.reader(ok([1], link=f'<{API}{path}?page=2>; rel="next"'),
                           ok([2], link=f'<{API}{path}?page=2>; rel="next"'))
        with self.assertRaises(InspectError) as ctx:
            r.paginate(path, None, 5)
        self.assertEqual(ctx.exception.reason, "GITHUB_READ")

    def test_key_and_shape(self):
        path = f"/repos/{REPO}/commits/abc/check-runs"
        r, _ = self.reader(ok({"total_count": 1, "check_runs": [{"name": "bridge"}]}), ok({"x": 1}), ok({"a": 1}))
        self.assertEqual(r.paginate(path, {"per_page": 100}, 1, key="check_runs"), [{"name": "bridge"}])
        with self.assertRaises(InspectError) as ctx:
            r.paginate(path, None, 1, key="check_runs")
        self.assertEqual(ctx.exception.reason, "GITHUB_JSON")
        with self.assertRaises(InspectError) as ctx:
            r.paginate(path, None, 1)
        self.assertEqual(ctx.exception.reason, "GITHUB_JSON")

    def test_404_is_not_an_empty_list(self):
        r, _ = self.reader((404, {}, b"{}"))
        with self.assertRaises(InspectError) as ctx:
            r.paginate(f"/repos/{REPO}/pulls", None, 2)
        self.assertEqual(ctx.exception.reason, "GITHUB_READ")

    def test_bad_args(self):
        r, t = self.reader()
        for pages in (0, -1, True, "2"):
            with self.assertRaises(InspectError):
                r.paginate(f"/repos/{REPO}/pulls", None, pages)
        with self.assertRaises(InspectError) as ctx:
            r.paginate(f"/repos/{OTHER}/pulls", None, 2)
        self.assertEqual(ctx.exception.reason, "PATH_NOT_ALLOWED")
        self.assertEqual(t.calls, [])

    def test_parse_link_next(self):
        self.assertEqual(gh.parse_link_next('<https://a/x?page=2>; rel="next"'), "https://a/x?page=2")
        self.assertEqual(gh.parse_link_next('<https://a/1>; rel="prev", <https://a/3>; rel="next last"'),
                         "https://a/3")
        self.assertEqual(gh.parse_link_next("<https://a/3>; rel=next"), "https://a/3")
        self.assertIsNone(gh.parse_link_next('<https://a/1>; rel="prev"'))
        self.assertIsNone(gh.parse_link_next(None))
        self.assertIsNone(gh.parse_link_next(""))


class WriterTests(unittest.TestCase):
    ISSUE = 41

    def writer(self, *responses):
        t = FakeTransport(*responses)
        return gh.GitHubLedgerWriter(TOKEN, CTRL, self.ISSUE, transport=t), t

    def comment(self, cid=9001):
        return (201, {}, {"id": cid, "html_url": f"https://github.com/{CTRL}/issues/{self.ISSUE}#issuecomment-{cid}",
                          "created_at": "2026-10-01T05:17:03Z", "body": "x", "user": {"login": "bot"}})

    def assertReason(self, reason, fn, *args):
        with self.assertRaises(InspectError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.reason, reason)
        self.assertNotIn(TOKEN, str(ctx.exception) + ctx.exception.detail)
        return ctx.exception

    def test_create_comment(self):
        w, t = self.writer(self.comment())
        out = w.create_comment("<!-- aiops-inspect -->\n감리 본문 @user")
        self.assertEqual(out, {"id": 9001, "created_at": "2026-10-01T05:17:03Z",
                               "html_url": f"https://github.com/{CTRL}/issues/41#issuecomment-9001"})
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{API}/repos/{CTRL}/issues/41/comments")
        self.assertEqual(json.loads(call["body"].decode("utf-8")), {"body": "<!-- aiops-inspect -->\n감리 본문 @user"})
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(call["headers"]["User-Agent"], "aiops-inspect")
        self.assertEqual(w.calls, 1)

    def test_update_body_sends_exactly_body(self):
        w, t = self.writer((200, {}, {"number": 41, "updated_at": "2026-10-01T05:18:00Z"}), (200, {}, b"garbage"))
        self.assertEqual(w.update_body("표"), {"updated_at": "2026-10-01T05:18:00Z"})
        call = t.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["url"], f"{API}/repos/{CTRL}/issues/41")
        self.assertEqual(json.loads(call["body"].decode("utf-8")), {"body": "표"})
        # A 2xx PATCH happened even if its response is unreadable.
        self.assertEqual(w.update_body("표2"), {"updated_at": None})

    def test_outcome_mapping(self):
        cases = [
            ((400, {}, b"{}"), "GITHUB_WRITE_REFUSED"), ((401, {}, b"{}"), "GITHUB_WRITE_REFUSED"),
            ((403, {"x-ratelimit-remaining": "0"}, b"{}"), "GITHUB_WRITE_REFUSED"),
            ((404, {}, b"{}"), "GITHUB_WRITE_REFUSED"), ((410, {}, b""), "GITHUB_WRITE_REFUSED"),
            ((422, {}, b"{}"), "GITHUB_WRITE_REFUSED"), ((429, {"retry-after": "3"}, b""), "GITHUB_WRITE_REFUSED"),
            ((500, {}, b""), "GITHUB_WRITE_UNKNOWN"), ((502, {}, b""), "GITHUB_WRITE_UNKNOWN"),
            ((504, {}, b""), "GITHUB_WRITE_UNKNOWN"), ((302, {}, b""), "GITHUB_WRITE_UNKNOWN"),
            ((100, {}, b""), "GITHUB_WRITE_UNKNOWN"),
            (InspectError("NET_UNKNOWN"), "GITHUB_WRITE_UNKNOWN"),
            (InspectError("NET_DOWN"), "GITHUB_WRITE_REFUSED"),
            (InspectError("SOMETHING_ELSE"), "GITHUB_WRITE_UNKNOWN"),
            (TimeoutError("timed out"), "GITHUB_WRITE_UNKNOWN"),
            (ConnectionResetError("reset"), "GITHUB_WRITE_UNKNOWN"),
            (RuntimeError(TOKEN), "GITHUB_WRITE_UNKNOWN"),
        ]
        for response, reason in cases:
            for method in ("create_comment", "update_body"):
                with self.subTest(response=response, method=method):
                    w, t = self.writer(response)
                    self.assertReason(reason, getattr(w, method), "본문")
                    self.assertEqual(len(t.calls), 1)

    def test_not_connected_detail_suffix_is_pinned(self):
        # publish.NOT_CONNECTED matches on this suffix: a NET_DOWN refusal sent nothing and is no attempt.
        import control_plane_inspect_publish as pub
        self.assertEqual(pub.NOT_CONNECTED, "connection not opened")
        for method in ("create_comment", "update_body"):
            w, _ = self.writer(InspectError("NET_DOWN"))
            err = self.assertReason("GITHUB_WRITE_REFUSED", getattr(w, method), "본문")
            self.assertTrue(err.detail.endswith(pub.NOT_CONNECTED), err.detail)
            for response in ((401, {}, b"{}"), (422, {}, b"{}")):
                w, _ = self.writer(response)
                err = self.assertReason("GITHUB_WRITE_REFUSED", getattr(w, method), "본문")
                self.assertFalse(err.detail.endswith(pub.NOT_CONNECTED), err.detail)

    def test_create_comment_unreadable_2xx_is_unknown(self):
        for payload in (b"not json", b"[]", b'{"id": "9", "html_url": "u", "created_at": "t"}',
                        b'{"id": 0, "html_url": "u", "created_at": "t"}',
                        b'{"id": true, "html_url": "u", "created_at": "t"}',
                        b'{"id": 5, "created_at": "t"}', b""):
            with self.subTest(payload=payload):
                w, _ = self.writer((201, {}, payload))
                self.assertReason("GITHUB_WRITE_UNKNOWN", w.create_comment, "본문")

    def test_body_cap_and_type(self):
        w, t = self.writer(self.comment(), (200, {}, b"{}"))
        w.create_comment("가" * 60000)
        w.update_body("x" * 60000)
        self.assertEqual(len(t.calls), 2)
        for body in ("가" * 60001, "", "   ", None, b"bytes", 5):
            for method in ("create_comment", "update_body"):
                with self.subTest(body=type(body), method=method):
                    self.assertReason("GITHUB_WRITE_REFUSED", getattr(w, method), body)
        self.assertEqual(len(t.calls), 2)

    def test_constructor_validation(self):
        for issue in (0, -1, True, "41", 41.0, None, 10 ** 10):
            with self.subTest(issue=issue):
                with self.assertRaises(InspectError) as ctx:
                    gh.GitHubLedgerWriter(TOKEN, CTRL, issue, transport=FakeTransport())
                self.assertEqual(ctx.exception.reason, "CONFIG")
        for repo in ("x", "a/b/c", "", None, "a/b?c"):
            with self.assertRaises(InspectError):
                gh.GitHubLedgerWriter(TOKEN, repo, 41, transport=FakeTransport())
        with self.assertRaises(InspectError) as ctx:
            gh.GitHubLedgerWriter("bad token", CTRL, 41, transport=FakeTransport())
        self.assertEqual(ctx.exception.reason, "SECRET_GH_LEDGER")
        w = gh.GitHubLedgerWriter(TOKEN, CTRL, 41, transport=FakeTransport())
        self.assertNotIn(TOKEN, repr(w))

    def test_comments_since(self):
        path = f"/repos/{CTRL}/issues/41/comments"
        pages = [ok([{"id": i}, "junk"], link=f'<{API}/repositories/77/issues/41/comments?page={i + 2}>; rel="next"')
                 for i in range(12)]
        w, t = self.writer(*pages)
        out = w.comments_since("2026-10-01T14:17:00+09:00")
        self.assertEqual(out, [{"id": i} for i in range(10)])
        self.assertTrue(w.last_truncated)
        self.assertEqual(len(t.calls), 10)
        self.assertEqual(w.calls, 10)
        first_path, first_query = path_and_query(t.calls[0]["url"])
        self.assertEqual(first_path, path)
        self.assertEqual(first_query, [("since", "2026-10-01T05:17:00Z"), ("per_page", str(gh.LEDGER_PER_PAGE))])
        for call in t.calls:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(path_and_query(call["url"])[0], path)
            self.assertIsNone(call["body"])

    def test_comments_since_bad_input_and_errors(self):
        w, t = self.writer((500, {}, b""), (404, {}, b""))
        for bad in ("yesterday", "", None, "2026-10-01T05:17:00"):
            with self.assertRaises(InspectError) as ctx:
                w.comments_since(bad)
            self.assertEqual(ctx.exception.reason, "TIME")
        self.assertEqual(t.calls, [])
        self.assertReason("GITHUB_5XX", w.comments_since, "2026-10-01T05:17:00Z")
        self.assertReason("GITHUB_READ", w.comments_since, "2026-10-01T05:17:00Z")

    def test_comments_since_rejects_links_to_other_issues(self):
        w, t = self.writer(ok([{"id": 1}], link=f'<{API}/repos/{CTRL}/issues/42/comments?page=2>; rel="next"'))
        self.assertReason("PATH_NOT_ALLOWED", w.comments_since, "2026-10-01T05:17:00Z")
        self.assertEqual(len(t.calls), 1)

    def test_write_surface_is_exactly_three_methods_without_targets(self):
        public = {name for name, _ in inspect.getmembers(gh.GitHubLedgerWriter) if not name.startswith("_")}
        self.assertEqual(public, {"create_comment", "update_body", "comments_since", "calls"})
        self.assertEqual(list(inspect.signature(gh.GitHubLedgerWriter.create_comment).parameters), ["self", "body"])
        self.assertEqual(list(inspect.signature(gh.GitHubLedgerWriter.update_body).parameters), ["self", "body"])
        self.assertEqual(list(inspect.signature(gh.GitHubLedgerWriter.comments_since).parameters),
                         ["self", "since_iso"])

    def test_no_method_writes_anywhere_but_the_configured_issue(self):
        hostile = [f"/repos/{OTHER}/issues/1/comments", "../../42", "https://evil.example/", "\r\nHost: x",
                   "issues/42", "@everyone"]
        responses = []
        for _ in hostile:
            responses.extend([self.comment(), (200, {}, b"{}"),
                              ok([], link=f'<{API}/repos/{CTRL}/issues/41/comments?page=2>; rel="next"'), ok([])])
        w, t = self.writer(*responses)
        for body in hostile:
            w.create_comment(body)
            w.update_body(body)
            w.comments_since("2026-10-01T00:00:00Z")
        allowed = {("POST", f"{API}/repos/{CTRL}/issues/41/comments"), ("PATCH", f"{API}/repos/{CTRL}/issues/41")}
        for call in t.calls:
            if call["method"] == "GET":
                self.assertEqual(path_and_query(call["url"])[0], f"/repos/{CTRL}/issues/41/comments")
                self.assertTrue(call["url"].startswith(f"{API}/repos/{CTRL}/issues/41/comments?"))
            else:
                self.assertIn((call["method"], call["url"]), allowed)
                self.assertEqual(list(json.loads(call["body"].decode("utf-8"))), ["body"])

    def test_reader_never_writes(self):
        t = FakeTransport(*[ok([]) for _ in range(3)])
        r = gh.GitHubReader(TOKEN, [REPO], transport=t)
        r.get(f"/repos/{REPO}/pulls")
        r.paginate(f"/repos/{REPO}/pulls", None, 2)
        self.assertEqual({c["method"] for c in t.calls}, {"GET"})
        self.assertTrue(all(c["body"] is None for c in t.calls))

    def test_module_has_no_other_write_verbs(self):
        source = Path(gh.__file__).read_text(encoding="utf-8")
        for verb in ('"PUT"', '"DELETE"', "'PUT'", "'DELETE'", '"POST"', '"PATCH"'):
            count = source.count(verb)
            if verb in ('"POST"', '"PATCH"'):
                self.assertEqual(count, 1, verb)
            else:
                self.assertEqual(count, 0, verb)


class FakeResponse(io.BytesIO):
    def __init__(self, status, headers, body):
        super().__init__(body)
        self.status = status
        self.headers = http.client.HTTPMessage()
        for key, value in headers:
            self.headers[key] = value
        self.read_limits = []

    def read(self, n=-1):
        self.read_limits.append(n)
        return super().read(n)

    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self, state, action):
        self.state = state
        self.action = action
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return self.action(self.state, request)


class UrllibTransportTests(unittest.TestCase):
    URL = f"{API}/repos/{REPO}"

    def transport(self, action, max_bytes=100):
        openers = []

        def factory(state):
            opener = FakeOpener(state, action)
            openers.append(opener)
            return opener
        return gh.UrllibTransport(max_bytes=max_bytes, opener_factory=factory), openers

    def test_refuses_non_api_urls(self):
        t, openers = self.transport(lambda s, r: None)
        for url in ("http://api.github.com/repos/a/b", "https://evil.example/x", "https://api.github.com.evil/x",
                    "https://api.github.com"):
            with self.assertRaises(InspectError) as ctx:
                t("GET", url, {}, None, 5)
            self.assertEqual(ctx.exception.reason, "PATH_NOT_ALLOWED")
        self.assertEqual(openers, [])

    def test_success_reads_capped_body_and_lowercases_headers(self):
        response = FakeResponse(200, [("ETag", '"e"'), ("Link", "<a>; rel=next")], b"x" * 500)

        def action(state, request):
            state["connected"] = True
            return response
        t, openers = self.transport(action)
        status, headers, body = t("GET", self.URL, {"Authorization": "Bearer x", "User-Agent": "aiops-inspect"},
                                  None, 7)
        self.assertEqual(status, 200)
        self.assertEqual(headers, {"etag": '"e"', "link": "<a>; rel=next"})
        self.assertEqual(len(body), 101)
        self.assertEqual(response.read_limits, [101])
        request, timeout = openers[0].requests[0]
        self.assertEqual(timeout, 7)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, self.URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer x")

    def test_http_error_is_returned(self):
        def action(state, request):
            state["connected"] = True
            hdrs = http.client.HTTPMessage()
            hdrs["X-RateLimit-Remaining"] = "0"
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs, io.BytesIO(b'{"m": 1}'))
        t, _ = self.transport(action)
        self.assertEqual(t("POST", self.URL, {}, b"{}", 5), (403, {"x-ratelimit-remaining": "0"}, b'{"m": 1}'))

    def test_network_failures_before_and_after_connect(self):
        def before(state, request):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        def after_url(state, request):
            state["connected"] = True
            raise urllib.error.URLError(ConnectionResetError("reset"))

        def after_timeout(state, request):
            state["connected"] = True
            raise TimeoutError("read timed out")

        def after_disconnect(state, request):
            state["connected"] = True
            raise http.client.RemoteDisconnected("closed")

        def ssl_fail(state, request):
            import ssl
            raise urllib.error.URLError(ssl.SSLError("bad cert"))
        for action, reason in ((before, "NET_DOWN"), (ssl_fail, "NET_DOWN"), (after_url, "NET_UNKNOWN"),
                               (after_timeout, "NET_UNKNOWN"), (after_disconnect, "NET_UNKNOWN")):
            with self.subTest(action=action.__name__):
                t, _ = self.transport(action)
                with self.assertRaises(InspectError) as ctx:
                    t("POST", self.URL, {}, b"{}", 5)
                self.assertEqual(ctx.exception.reason, reason)

    def test_real_opener_tracks_connection(self):
        """Real urllib opener; the socket layer is patched so nothing touches the network."""
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("https_proxy", "http_proxy", "all_proxy")}
        transport = gh.UrllibTransport(max_bytes=100)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(http.client.HTTPSConnection, "connect",
                                   side_effect=ConnectionRefusedError("refused")):
                with self.assertRaises(InspectError) as ctx:
                    transport("GET", self.URL, {}, None, 1)
                self.assertEqual(ctx.exception.reason, "NET_DOWN")
            def send_then_reset(conn, data):
                if conn.sock is None:
                    conn.connect()
                raise ConnectionResetError("reset")
            with mock.patch.object(http.client.HTTPSConnection, "connect", return_value=None), \
                    mock.patch.object(http.client.HTTPConnection, "send", send_then_reset):
                with self.assertRaises(InspectError) as ctx:
                    transport("POST", self.URL, {}, b"{}", 1)
                self.assertEqual(ctx.exception.reason, "NET_UNKNOWN")

    def test_redirects_are_not_followed(self):
        handler = gh._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))
        opener = gh._build_opener({"connected": False})
        self.assertTrue(any(isinstance(h, gh._NoRedirect) for h in opener.handlers))
        self.assertFalse(any(type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers))

    def test_default_transports(self):
        r = gh.GitHubReader(TOKEN, [REPO])
        self.assertIsInstance(r._transport, gh.UrllibTransport)
        w = gh.GitHubLedgerWriter(TOKEN, CTRL, 3)
        self.assertIsInstance(w._transport, gh.UrllibTransport)
        self.assertNotIn(TOKEN, repr(r._transport))


if __name__ == "__main__":
    unittest.main()
