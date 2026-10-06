"""Authenticated Mac-only audit channel adopted by the User on 2026-10-06.

The Linux fixed tool produces audits. This module reads GitHub directly; caller
JSON, model text and request digests cannot supply an audit result.
"""
from __future__ import annotations

import copy
import hashlib
import re

from common import AppError, repository
import handoff

MARK = '<!-- aiops-fable-audit -->'
DEPTHS = ('A0', 'A1', 'A2', 'A3')
HEADER = re.compile(r'ASTRA_AUDIT_V1 pr=([1-9][0-9]*) head=([0-9a-f]{40}) '
                    r'result=(PASS|PASS_WITH_NOTES|FAIL|DECISION_REQUIRED) depth=(A[0-3]) '
                    r'auditor=ASTRA_FABLE session=([A-Za-z0-9-]{8,64})')
DECISION = {
    'url': 'https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/78#issuecomment-6011271646',
    'comment_id': 6011271646, 'actor_login': 'BeautifulMind-JT', 'actor_id': 263336091,
    'body_sha256': '46dc2cf0fdd02eeabbc2ac234ffcda0d410820f169d6e87f2f904d7ad939f132',
    'created_at': '2026-10-06T07:10:27Z',
}


def require(value, code='MAC_HOST_ASTRA_RECEIPT_UNVERIFIED'):
    if not value:
        raise AppError(code)


def api(path, *, paginate=False):
    try:
        return handoff.api(path, paginate=paginate)
    except (AppError, OSError, ValueError):
        raise AppError('MAC_HOST_ASTRA_RECEIPT_UNVERIFIED') from None


def trusted_actor(comment):
    actor = comment.get('user') if isinstance(comment, dict) else None
    return (isinstance(actor, dict) and actor.get('login') == DECISION['actor_login'] and
            type(actor.get('id')) is int and actor['id'] == DECISION['actor_id'] and
            actor.get('type') == 'User')


def body_hash(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def decision_evidence():
    comment = api('repos/BeautifulMind-JT/ai-ops-control-plane/issues/comments/' + str(DECISION['comment_id']))
    require(trusted_actor(comment) and comment.get('id') == DECISION['comment_id'] and
            comment.get('html_url') == DECISION['url'] and
            comment.get('issue_url') == 'https://api.github.com/repos/BeautifulMind-JT/ai-ops-control-plane/issues/78' and
            comment.get('created_at') == comment.get('updated_at') == DECISION['created_at'] and
            isinstance(comment.get('body'), str) and body_hash(comment['body']) == DECISION['body_sha256'],
            'MAC_HOST_ASTRA_DECISION_UNVERIFIED')
    return copy.deepcopy(DECISION)


def context(requirement):
    request = requirement.get('request_binding') if isinstance(requirement, dict) else None
    require(isinstance(request, dict), 'MAC_HOST_ASTRA_AUDIT_REQUIRED')
    url = request.get('pr_url')
    require(isinstance(url, str))
    match = re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)', url)
    require(match is not None)
    try:
        repo = repository(request.get('repository'))
        require(repository(match[1]).lower() == repo.lower())
    except AppError:
        raise AppError('MAC_HOST_ASTRA_RECEIPT_UNVERIFIED') from None
    require(isinstance(request.get('head'), str) and re.fullmatch(r'[0-9a-f]{40}', request['head']) and
            request.get('gate') in ('MILESTONE', 'ARCHITECTURE', 'RELEASE') and
            request.get('requested_depth') in DEPTHS)
    return repo, int(match[2]), request


def live_pr(requirement):
    repo, number, request = context(requirement)
    pull = api('repos/' + repo + '/pulls/' + str(number))
    require(isinstance(pull, dict) and pull.get('number') == number and
            pull.get('html_url', '').lower() == request['pr_url'].lower() and
            isinstance(pull.get('head'), dict) and pull['head'].get('sha') == request['head'] and
            isinstance(pull.get('base'), dict) and isinstance(pull['base'].get('repo'), dict) and
            str(pull['base']['repo'].get('full_name', '')).lower() == repo.lower() and
            isinstance(pull['head'].get('repo'), dict) and
            str(pull['head']['repo'].get('full_name', '')).lower() == repo.lower() and
            (not request.get('branch') or pull['head'].get('ref') == request['branch']))
    return pull


def field(body, name):
    values = re.findall(r'^' + re.escape(name) + r': ([^\r\n]*)$', body, re.MULTILINE)
    require(len(values) == 1)
    return values[0]


def parse_comment(comment, requirement):
    repo, number, request = context(requirement)
    require(trusted_actor(comment) and type(comment.get('id')) is int and comment['id'] > 0 and
            isinstance(comment.get('body'), str) and len(comment['body'].encode()) <= 1024 * 1024 and
            isinstance(comment.get('created_at'), str) and
            re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', comment['created_at']) and
            comment.get('updated_at') == comment['created_at'])
    require(isinstance(comment.get('html_url'), str) and isinstance(comment.get('issue_url'), str))
    url = re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)',
                       comment.get('html_url', ''))
    issue = re.fullmatch(r'https://api\.github\.com/repos/([^/]+/[^/]+)/issues/([1-9][0-9]*)',
                         comment.get('issue_url', ''))
    require(url and issue and url[1].lower() == issue[1].lower() == repo.lower() and
            int(url[2]) == int(issue[2]) == number and int(url[3]) == comment['id'])
    body = comment['body']; lines = body.splitlines()
    require(len(lines) >= 2 and lines[0] == MARK)
    header = HEADER.fullmatch(lines[1]); require(header is not None)
    pr, head, result, depth, session = header.groups()
    require(int(pr) == number and head == request['head'] and
            field(body, 'AUDIT_RESULT') == result and field(body, 'VERIFIED_AUDIT_DEPTH') == depth and
            field(body, 'AUDITED_HEAD_OR_EVIDENCE_SHA') == head and
            field(body, 'AUDITOR_IDENTITY_OR_SESSION') == 'ASTRA_FABLE claude-fable-5-1 session=' + session and
            re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', field(body, 'AUDIT_REQUEST_ID')) and
            field(body, 'AUDIT_ATTEMPT_ID') == '1' and
            re.fullmatch(r'[0-9a-f]{40}', field(body, 'AUDITED_MERGE_BASE_SHA')))
    scoped = re.fullmatch(r'([^ #]+/[^ #]+)#([1-9][0-9]*) gate=(MILESTONE|ARCHITECTURE|RELEASE) requested_depth=(A[0-3])',
                          field(body, 'AUDITED_TASK_REVISION_OR_MILESTONE'))
    require(scoped and scoped[1].lower() == repo.lower() and int(scoped[2]) == number and
            scoped[3] == request['gate'] and DEPTHS.index(scoped[4]) >= DEPTHS.index(request['requested_depth']))
    contract = field(body, 'VERIFIED_CONTRACT_CHANGE_REQUIRED')
    require(contract in ('NO', 'YES'))
    return {'schema': 'ASTRA_AUDIT_V1', 'comment_id': comment['id'], 'comment_url': comment['html_url'],
            'body_sha256': body_hash(body), 'created_at': comment['created_at'], 'updated_at': comment['updated_at'],
            'repository': repo, 'pr': number, 'head': head, 'result': result, 'depth': depth,
            'auditor_identity': 'ASTRA_FABLE', 'auditor_session': session,
            'actor_login': DECISION['actor_login'], 'actor_id': DECISION['actor_id'],
            'audit_request_id': field(body, 'AUDIT_REQUEST_ID'), 'contract_change_required': contract}


def read_receipt(requirement, comment_id, *, expected=None):
    require(type(comment_id) is int and comment_id > 0)
    decision_evidence(); live_pr(requirement)
    repo, number, request = context(requirement)
    comment = api('repos/' + repo + '/issues/comments/' + str(comment_id))
    evidence = parse_comment(comment, requirement)
    require(evidence['comment_id'] == comment_id and evidence['result'] in ('PASS', 'PASS_WITH_NOTES') and
            DEPTHS.index(evidence['depth']) >= DEPTHS.index(request['requested_depth']) and
            evidence['contract_change_required'] == 'NO')
    if expected is not None:
        require(evidence == expected, 'MAC_HOST_ASTRA_RECEIPT_CHANGED')
    # A later same-HEAD FAIL/decision cannot be hidden by selecting an older PASS.
    pages = api('repos/' + repo + '/issues/' + str(number) + '/comments?per_page=100', paginate=True)
    require(isinstance(pages, list) and pages and all(isinstance(page, list) for page in pages) and
            sum(len(page) for page in pages) <= 4096)
    seen = set(); found = False
    for page in pages:
        for item in page:
            require(isinstance(item, dict) and type(item.get('id')) is int and item['id'] not in seen)
            seen.add(item['id'])
            if item['id'] == comment_id:
                require(parse_comment(item, requirement) == evidence, 'MAC_HOST_ASTRA_RECEIPT_CHANGED'); found = True
            if not trusted_actor(item) or not isinstance(item.get('body'), str) or not item['body'].startswith(MARK):
                continue
            lines = item['body'].splitlines(); require(len(lines) >= 2)
            header = HEADER.fullmatch(lines[1]); require(header is not None)
            if header[2] != request['head']:
                continue
            other = parse_comment(item, requirement)
            require(other['result'] == evidence['result'], 'MAC_HOST_ASTRA_AUDIT_CONFLICT')
    require(found, 'MAC_HOST_ASTRA_RECEIPT_CHANGED')
    live_pr(requirement)
    return evidence
