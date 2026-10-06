"""Authenticated Mac-only audit channel adopted by the User on 2026-10-06.

The Linux fixed tool produces audits. This module reads GitHub directly; caller
JSON, model text and request digests cannot supply an audit result.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import os
import re
import sqlite3
import stat

from common import AppError, digest, encoded, parse_json, repository
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


def read_receipt(requirement, comment_id, *, expected=None, journal=None):
    require(type(comment_id) is int and comment_id > 0)
    decision_evidence(); live_pr(requirement)
    repo, number, request = context(requirement)
    comment = api('repos/' + repo + '/issues/comments/' + str(comment_id))
    try:evidence = parse_comment(comment, requirement)
    except AppError:
        if journal is not None:journal.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_CHANGED')
        raise
    require(evidence['comment_id'] == comment_id and evidence['result'] in ('PASS', 'PASS_WITH_NOTES') and
            DEPTHS.index(evidence['depth']) >= DEPTHS.index(request['requested_depth']) and
            evidence['contract_change_required'] == 'NO','MAC_HOST_ASTRA_RECEIPT_CHANGED')
    if expected is not None:
        require(evidence == expected, 'MAC_HOST_ASTRA_RECEIPT_CHANGED')
    # A later same-HEAD FAIL/decision cannot be hidden by selecting an older PASS.
    if journal is not None:
        observed=journal.observe(requirement)
        if not any(item==evidence for item in observed):
            journal.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_CHANGED',evidence)
        live_pr(requirement)
        return evidence
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
            require(other['result'] == evidence['result'] and other['contract_change_required'] == 'NO' and
                    DEPTHS.index(other['depth']) >= DEPTHS.index(request['requested_depth']),
                    'MAC_HOST_ASTRA_AUDIT_CONFLICT')
    require(found, 'MAC_HOST_ASTRA_RECEIPT_CHANGED')
    live_pr(requirement)
    return evidence


def admission(node):
    """The User's live channel adoption opens work, never its completion gate."""
    if node.get('audit_floor', 'A1') == 'A3' or node.get('astra_gate', 'NONE') != 'NONE':
        return decision_evidence()
    return None


class Journal:
    """Private Mac ledger: a comment can be bound to exactly one audit request.

    Requests are durable before the audit is consumed. An earlier audit cannot
    be retroactively assigned a new task revision, writer/review or attempt.
    Neither this journal nor its caller accepts a supplied receipt document.
    """
    def __init__(self, store):
        self.store = store
        with store.lock:
            # Independent durable authority: a rejected child admission rolls
            # back app.sqlite3, but must not erase an observed negative audit.
            if getattr(store,'_mac_astra_db',None) is None:
                path=store.directory/'mac-astra.sqlite3'
                try:
                    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
                    try:info=os.fstat(fd)
                    finally:os.close(fd)
                    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and
                            not info.st_mode & 0o077 and info.st_nlink==1)
                    db=sqlite3.connect(path,isolation_level=None,check_same_thread=False,timeout=10)
                    db.row_factory=sqlite3.Row
                    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
                    store._mac_astra_db=db
                except (OSError,sqlite3.Error):
                    raise AppError('MAC_HOST_ASTRA_RECEIPT_UNVERIFIED') from None
            self.db=store._mac_astra_db
            self.db.execute('''CREATE TABLE IF NOT EXISTS mac_host_astra_requests(
                request_sha256 TEXT PRIMARY KEY, document TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS mac_host_astra_receipts(
                request_sha256 TEXT PRIMARY KEY, repository TEXT NOT NULL,
                comment_id INTEGER NOT NULL, audit_request_id TEXT NOT NULL, document TEXT NOT NULL,
                UNIQUE(repository, comment_id), UNIQUE(repository, audit_request_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS mac_host_astra_observations(
                repository TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL,
                comment_id INTEGER NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(repository, pr, head, comment_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS mac_host_astra_holds(
                repository TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL,
                code TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(repository, pr, head))''')

    def hold(self, requirement, code, evidence=None):
        repo, number, request=context(requirement)
        document={'request_sha256':requirement['request_sha256'],'head':request['head'],
                  'code':code,'evidence':evidence}
        with self.store.lock:
            self.db.execute('INSERT OR IGNORE INTO mac_host_astra_holds VALUES (?,?,?,?,?)',
                                  (repo.lower(),number,request['head'],code,encoded(document)))
        raise AppError(code)

    def check_hold(self,requirement):
        repo,number,request=context(requirement)
        key=(repo.lower(),number,request['head'])
        with self.store.lock:
            held=self.db.execute('SELECT code FROM mac_host_astra_holds WHERE repository=? AND pr=? AND head=?',key).fetchone()
        if held: raise AppError(held[0])

    def observe(self, requirement):
        repo,number,request=context(requirement)
        key=(repo.lower(),number,request['head'])
        self.check_hold(requirement)
        live_pr(requirement)
        pages=api('repos/'+repo+'/issues/'+str(number)+'/comments?per_page=100',paginate=True)
        require(isinstance(pages,list) and pages and all(isinstance(page,list) for page in pages) and
                sum(len(page) for page in pages)<=4096)
        observed={};seen=set()
        for page in pages:
            for item in page:
                require(isinstance(item,dict) and type(item.get('id')) is int and item['id'] not in seen)
                seen.add(item['id'])
                if not trusted_actor(item) or not isinstance(item.get('body'),str) or not item['body'].startswith(MARK):continue
                lines=item['body'].splitlines()
                header=HEADER.fullmatch(lines[1]) if len(lines)>=2 else None
                if header is None:self.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_UNVERIFIED')
                if header[2]!=request['head']:continue
                try:value=parse_comment(item,requirement)
                except AppError:self.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_CHANGED')
                observed[value['comment_id']]=value
        with self.store.lock:
            self.check_hold(requirement)
            old={row['comment_id']:parse_json(row['document']) for row in self.db.execute(
                'SELECT comment_id,document FROM mac_host_astra_observations WHERE repository=? AND pr=? AND head=?',key)}
            for cid,value in old.items():
                if observed.get(cid)!=value:self.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_CHANGED',value)
            for cid,value in observed.items():
                self.db.execute('INSERT OR IGNORE INTO mac_host_astra_observations VALUES (?,?,?,?,?)',
                                      (*key,cid,encoded(value)))
        values=list(observed.values())
        if len({value['result'] for value in values})>1:
            self.hold(requirement,'MAC_HOST_ASTRA_AUDIT_CONFLICT',values)
        for value in values:
            if value['result'] not in ('PASS','PASS_WITH_NOTES') or value['contract_change_required']!='NO' or \
                    DEPTHS.index(value['depth'])<DEPTHS.index(request['requested_depth']):
                self.hold(requirement,'MAC_HOST_ASTRA_AUDIT_CONFLICT',value)
        return values

    def request(self, requirement):
        _, _, request = context(requirement)
        sha = requirement.get('request_sha256')
        require(sha == digest(request) and isinstance(request.get('task_id'), str) and request['task_id'] and
                isinstance(request.get('task_revision'), str) and re.fullmatch(r'[0-9a-f]{64}', request['task_revision']) and
                isinstance(request.get('canonical_binding'), dict) and
                request['canonical_binding'].get('task_id') == request['task_id'] and
                request['canonical_binding'].get('task_revision') == request['task_revision'])
        decision_evidence()
        with self.store.lock:
            row = self.db.execute('SELECT document FROM mac_host_astra_requests WHERE request_sha256=?', (sha,)).fetchone()
            if row:
                document = parse_json(row[0])
                require(document['request'] == request and document['decision'] == DECISION)
            else:
                document = {'schema_version': 1, 'request_sha256': sha, 'request': copy.deepcopy(request),
                            'decision': copy.deepcopy(DECISION),
                            'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
                self.db.execute('INSERT INTO mac_host_astra_requests VALUES (?,?)', (sha, encoded(document)))
        return document

    def consume(self, requirement):
        document = self.request(requirement); sha = document['request_sha256']
        repo, number, request = context(requirement)
        observed=self.observe(requirement)
        with self.store.lock:
            row = self.db.execute('SELECT document FROM mac_host_astra_receipts WHERE request_sha256=?', (sha,)).fetchone()
            pinned = parse_json(row[0]) if row else None
        if pinned:
            require(pinned.get('request_sha256') == sha and pinned.get('request') == document['request'] and
                    pinned.get('decision') == DECISION)
            try:evidence = read_receipt(requirement, pinned['comment']['comment_id'], expected=pinned['comment'],journal=self)
            except AppError as exc:
                if exc.code in ('MAC_HOST_ASTRA_RECEIPT_CHANGED','MAC_HOST_ASTRA_AUDIT_CONFLICT'):
                    self.hold(requirement,exc.code,pinned['comment'])
                raise
        else:
            candidates=[value['comment_id'] for value in observed if value['created_at']>=document['created_at']]
            require(candidates, 'MAC_HOST_ASTRA_AUDIT_REQUIRED')
            try:evidence = read_receipt(requirement, min(candidates),journal=self)
            except AppError as exc:
                if exc.code in ('MAC_HOST_ASTRA_RECEIPT_CHANGED','MAC_HOST_ASTRA_AUDIT_CONFLICT'):
                    self.hold(requirement,exc.code)
                raise
        require(evidence['created_at'] >= document['created_at'], 'MAC_HOST_ASTRA_RECEIPT_REPLAY')
        try:
            invoked_at=datetime.strptime(evidence['audit_request_id'].split('-')[0],'%Y%m%dT%H%M%SZ').strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            raise AppError('MAC_HOST_ASTRA_RECEIPT_UNVERIFIED') from None
        # Equal seconds do not prove ordering; the audit could have started
        # before a later request in that same second. Refuse that ambiguity.
        require(document['created_at'] < invoked_at <= evidence['created_at'],'MAC_HOST_ASTRA_RECEIPT_REPLAY')
        result = {'schema_version': 1, 'source': 'AUTHENTICATED_GITHUB_AUDIT_COMMENT',
                  'request_sha256': sha, 'request': copy.deepcopy(document['request']),
                  'decision': copy.deepcopy(DECISION), 'comment': evidence}
        with self.store.lock:
            self.check_hold(requirement)
            old = self.db.execute('SELECT document FROM mac_host_astra_receipts WHERE request_sha256=?', (sha,)).fetchone()
            if old:
                require(parse_json(old[0]) == result, 'MAC_HOST_ASTRA_RECEIPT_CHANGED')
            else:
                try:
                    self.db.execute('INSERT INTO mac_host_astra_receipts VALUES (?,?,?,?,?)',
                                          (sha, repo.lower(), evidence['comment_id'], evidence['audit_request_id'], encoded(result)))
                except sqlite3.IntegrityError:
                    raise AppError('MAC_HOST_ASTRA_RECEIPT_REPLAY') from None
        return result
