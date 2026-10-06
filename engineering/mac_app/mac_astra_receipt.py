"""Authenticated Mac-only audit channel adopted by the User on 2026-10-06.

The Linux fixed tool produces audits. This module reads GitHub directly; caller
JSON, model text and request digests cannot supply an audit result.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import sqlite3
import stat

from common import AppError, digest, encoded, parse_json, repository
import handoff

MARK = '<!-- aiops-fable-audit -->'
DEPTHS = ('A0', 'A1', 'A2', 'A3')
CLOCK_SKEW_SECONDS = 30
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


@handoff.bounded_api_reads
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


def field(body, name, *, optional=False):
    values = re.findall(r'^' + re.escape(name) + r': ([^\r\n]*)$', body, re.MULTILINE)
    if optional and not values:return None
    require(len(values) == 1)
    return values[0]


def scope(body):
    value=field(body,'AUDITED_TASK_REVISION_OR_MILESTONE').split()
    require(value and re.fullmatch(r'[^ #]+/[^ #]+#[1-9][0-9]*',value[0]))
    fields={}
    for token in value[1:]:
        pair=token.split('=',1)
        require(len(pair)==2 and pair[0] not in fields)
        fields[pair[0]]=pair[1]
    require(fields.get('gate') in ('MILESTONE','ARCHITECTURE','RELEASE'))
    repo,number=value[0].rsplit('#',1)
    return repo,int(number),fields['gate']


def relevant(comment,requirement):
    """Malformed required fields block this read; known unrelated audits do not."""
    repo,number,request=context(requirement)
    lines=comment['body'].splitlines()
    require(len(lines)>=2 and lines[0]==MARK)
    header=HEADER.fullmatch(lines[1]);require(header is not None)
    require(int(header[1])==number)
    if header[2]!=request['head'] or DEPTHS.index(header[4])<DEPTHS.index(request['requested_depth']):return False
    scoped=scope(comment['body'])
    require(scoped[0].lower()==repo.lower() and scoped[1]==number)
    return scoped[2]==request['gate']


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
    require(int(pr)==number and head==request['head'] and relevant(comment,requirement))
    for name,expected in (('AUDIT_RESULT',result),('VERIFIED_AUDIT_DEPTH',depth),
                          ('AUDITED_HEAD_OR_EVIDENCE_SHA',head)):
        value=field(body,name,optional=True)
        require(value is None or value==expected)
    identity=field(body,'AUDITOR_IDENTITY_OR_SESSION',optional=True)
    require(identity is None or re.fullmatch(r'ASTRA_FABLE(?: [^\r\n]+)? session='+re.escape(session),identity))
    run=field(body,'AUDIT_REQUEST_ID',optional=result in ('FAIL','DECISION_REQUIRED'))
    require(run is None or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}',run))
    if result in ('PASS','PASS_WITH_NOTES'):
        started=re.match(r'[0-9]{8}T[0-9]{6}Z',run)
        require(started is not None)
        try:datetime.strptime(started[0],'%Y%m%dT%H%M%SZ')
        except ValueError:raise AppError('MAC_HOST_ASTRA_RECEIPT_UNVERIFIED') from None
    contract = field(body, 'VERIFIED_CONTRACT_CHANGE_REQUIRED',optional=result in ('FAIL','DECISION_REQUIRED')) or 'NO'
    require(contract in ('NO', 'YES'))
    return {'schema': 'ASTRA_AUDIT_V1', 'comment_id': comment['id'], 'comment_url': comment['html_url'],
            'body_sha256': body_hash(body), 'created_at': comment['created_at'], 'updated_at': comment['updated_at'],
            'repository': repo, 'pr': number, 'head': head, 'result': result, 'depth': depth,
            'auditor_identity': 'ASTRA_FABLE', 'auditor_session': session,
            'actor_login': DECISION['actor_login'], 'actor_id': DECISION['actor_id'],
            'audit_request_id':run or 'comment-'+str(comment['id']), 'contract_change_required': contract}


@handoff.bounded_api_reads
def read_receipt(requirement, comment_id, *, expected=None, journal=None):
    require(type(comment_id) is int and comment_id > 0)
    decision_evidence(); live_pr(requirement)
    repo, number, request = context(requirement)
    comment = api('repos/' + repo + '/issues/comments/' + str(comment_id))
    if expected is not None and (not isinstance(comment.get('body'),str) or
            body_hash(comment['body'])!=expected['body_sha256'] or
            comment.get('updated_at')!=expected['updated_at']):
        if journal is not None:journal.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_CHANGED',expected)
        raise AppError('MAC_HOST_ASTRA_RECEIPT_CHANGED')
    evidence = parse_comment(comment, requirement)
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
            if not relevant(item,requirement):continue
            other = parse_comment(item, requirement)
            require(other['result'] in ('PASS','PASS_WITH_NOTES') and other['contract_change_required'] == 'NO' and
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
            self.db.execute('''CREATE TABLE IF NOT EXISTS mac_host_astra_scoped_observations(
                repository TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL,
                gate TEXT NOT NULL, comment_id INTEGER NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(repository,pr,head,gate,comment_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS mac_host_astra_scoped_holds(
                repository TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL,
                gate TEXT NOT NULL, depth TEXT NOT NULL, code TEXT NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(repository,pr,head,gate,depth))''')
            # Preserve proven legacy failures/edits in their original scope.
            # Legacy malformed or shallow/unrelated holds are historical records,
            # rather than authority under the User's new compatibility policy.
            for row in self.db.execute('SELECT * FROM mac_host_astra_holds').fetchall():
                held=parse_json(row['document'])
                request_row=self.db.execute('SELECT document FROM mac_host_astra_requests WHERE request_sha256=?',
                                            (held['request_sha256'],)).fetchone()
                if not request_row:continue
                original=parse_json(request_row[0])['request'];gate=original['gate'];floor=original['requested_depth']
                proofs=held.get('evidence');proofs=proofs if isinstance(proofs,list) else [proofs]
                for proof in proofs:
                    if not isinstance(proof,dict) or proof.get('depth') not in DEPTHS:continue
                    if DEPTHS.index(proof['depth'])<DEPTHS.index(floor):continue
                    negative=proof.get('result') in ('FAIL','DECISION_REQUIRED') or proof.get('contract_change_required')=='YES'
                    changed=row['code']=='MAC_HOST_ASTRA_RECEIPT_CHANGED' and proof.get('body_sha256')
                    if negative or changed:
                        self.db.execute('INSERT OR IGNORE INTO mac_host_astra_scoped_holds VALUES (?,?,?,?,?,?,?)',
                                        (row['repository'],row['pr'],row['head'],gate,proof['depth'],row['code'],row['document']))

            requests=[parse_json(row[0])['request'] for row in self.db.execute('SELECT document FROM mac_host_astra_requests')]
            pins=[parse_json(row[0]) for row in self.db.execute('SELECT document FROM mac_host_astra_receipts')]
            for row in self.db.execute('SELECT * FROM mac_host_astra_observations').fetchall():
                proof=parse_json(row['document'])
                matching=[req for req in requests if req['repository'].lower()==row['repository'] and
                          req['pr_url'].rsplit('/',1)[-1]==str(row['pr']) and req['head']==row['head']]
                pinned={pin['request']['gate'] for pin in pins if pin['comment']==proof}
                gates=pinned or {req['gate'] for req in matching}
                if len(gates)==1:
                    self._preserve_observation(row['repository'],row['pr'],row['head'],next(iter(gates)),proof)

    def _preserve_observation(self,repo,number,head,gate,proof):
        self.db.execute('INSERT OR IGNORE INTO mac_host_astra_scoped_observations VALUES (?,?,?,?,?,?)',
                        (repo,number,head,gate,proof['comment_id'],encoded(proof)))
        if proof['result'] not in ('PASS','PASS_WITH_NOTES') or proof['contract_change_required']!='NO':
            code='MAC_HOST_ASTRA_AUDIT_CONFLICT'
            self.db.execute('INSERT OR IGNORE INTO mac_host_astra_scoped_holds VALUES (?,?,?,?,?,?,?)',
                            (repo,number,head,gate,proof['depth'],code,encoded({'code':code,'evidence':proof})))

    def hold(self, requirement, code, evidence=None):
        repo, number, request=context(requirement)
        depth=evidence.get('depth',request['requested_depth']) if isinstance(evidence,dict) else request['requested_depth']
        document={'request_sha256':requirement['request_sha256'],'head':request['head'],
                  'gate':request['gate'],'depth':depth,'code':code,'evidence':evidence}
        with self.store.lock:
            self.db.execute('INSERT OR IGNORE INTO mac_host_astra_scoped_holds VALUES (?,?,?,?,?,?,?)',
                                  (repo.lower(),number,request['head'],request['gate'],depth,code,encoded(document)))
        raise AppError(code)

    def check_hold(self,requirement):
        repo,number,request=context(requirement)
        key=(repo.lower(),number,request['head'],request['gate'])
        with self.store.lock:
            held=self.db.execute('SELECT code,depth FROM mac_host_astra_scoped_holds WHERE repository=? AND pr=? AND head=? AND gate=?',key).fetchall()
        for row in held:
            if DEPTHS.index(row['depth'])>=DEPTHS.index(request['requested_depth']):raise AppError(row['code'])

    @handoff.bounded_api_reads
    def observe(self, requirement):
        repo,number,request=context(requirement)
        key=(repo.lower(),number,request['head'],request['gate'])
        self.check_hold(requirement)
        live_pr(requirement)
        pages=api('repos/'+repo+'/issues/'+str(number)+'/comments?per_page=100',paginate=True)
        require(isinstance(pages,list) and pages and all(isinstance(page,list) for page in pages) and
                sum(len(page) for page in pages)<=4096)
        items={}
        for page in pages:
            for item in page:
                require(isinstance(item,dict) and type(item.get('id')) is int and item['id'] not in items)
                items[item['id']]=item
        with self.store.lock:
            # Ambiguous legacy scope requires the unchanged authenticated body.
            # Its absence cannot invent a permanent hold for an unrelated gate.
            for row in self.db.execute('SELECT * FROM mac_host_astra_observations WHERE repository=? AND pr=? AND head=?',key[:3]).fetchall():
                proof=parse_json(row['document'])
                if DEPTHS.index(proof['depth'])<DEPTHS.index(request['requested_depth']):continue
                if self.db.execute('SELECT 1 FROM mac_host_astra_scoped_observations WHERE repository=? AND pr=? AND head=? AND comment_id=?',
                                   (*key[:3],row['comment_id'])).fetchone():continue
                item=items.get(row['comment_id'])
                require(item is not None and trusted_actor(item) and isinstance(item.get('body'),str) and
                        body_hash(item['body'])==proof['body_sha256'] and item.get('updated_at')==proof['updated_at'])
                gate=scope(item['body'])[2]
                scoped={**requirement,'request_binding':{**request,'gate':gate}}
                require(parse_comment(item,scoped)==proof)
                self._preserve_observation(*key[:3],gate,proof)
            self.check_hold(requirement)
            old={row['comment_id']:parse_json(row['document']) for row in self.db.execute(
                'SELECT comment_id,document FROM mac_host_astra_scoped_observations WHERE repository=? AND pr=? AND head=? AND gate=?',key)
                 if DEPTHS.index(parse_json(row['document'])['depth'])>=DEPTHS.index(request['requested_depth'])}
            # A malformed edit of a known receipt is not a retryable new format.
            for cid,value in old.items():
                item=items.get(cid)
                if (item is None or not trusted_actor(item) or not isinstance(item.get('body'),str) or
                        body_hash(item['body'])!=value['body_sha256'] or item.get('updated_at')!=value['updated_at']):
                    self.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_CHANGED',value)
        observed={}
        for item in items.values():
            if not trusted_actor(item) or not isinstance(item.get('body'),str) or not item['body'].startswith(MARK):continue
            if not relevant(item,requirement):continue
            value=parse_comment(item,requirement)
            observed[value['comment_id']]=value
        with self.store.lock:
            self.check_hold(requirement)
            old={row['comment_id']:parse_json(row['document']) for row in self.db.execute(
                'SELECT comment_id,document FROM mac_host_astra_scoped_observations WHERE repository=? AND pr=? AND head=? AND gate=?',key)
                 if DEPTHS.index(parse_json(row['document'])['depth'])>=DEPTHS.index(request['requested_depth'])}
            for cid,value in old.items():
                if observed.get(cid)!=value:self.hold(requirement,'MAC_HOST_ASTRA_RECEIPT_CHANGED',value)
            for cid,value in observed.items():
                self.db.execute('INSERT OR IGNORE INTO mac_host_astra_scoped_observations VALUES (?,?,?,?,?,?)',
                                      (*key,cid,encoded(value)))
        values=list(observed.values())
        for value in values:
            if value['result'] not in ('PASS','PASS_WITH_NOTES') or value['contract_change_required']!='NO' or \
                    DEPTHS.index(value['depth'])<DEPTHS.index(request['requested_depth']):
                self.hold(requirement,'MAC_HOST_ASTRA_AUDIT_CONFLICT',value)
        return values

    @handoff.bounded_api_reads
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

    @handoff.bounded_api_reads
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
            oldest=datetime.strptime(document['created_at'],'%Y-%m-%dT%H:%M:%SZ')-timedelta(seconds=CLOCK_SKEW_SECONDS)
            candidates=[value['comment_id'] for value in observed if datetime.strptime(value['created_at'],'%Y-%m-%dT%H:%M:%SZ')>=oldest]
            require(candidates, 'MAC_HOST_ASTRA_AUDIT_REQUIRED')
            try:evidence = read_receipt(requirement, min(candidates),journal=self)
            except AppError as exc:
                if exc.code in ('MAC_HOST_ASTRA_RECEIPT_CHANGED','MAC_HOST_ASTRA_AUDIT_CONFLICT'):
                    self.hold(requirement,exc.code)
                raise
        try:
            started=re.match(r'[0-9]{8}T[0-9]{6}Z',evidence['audit_request_id'])
            require(started is not None)
            invoked_at=datetime.strptime(started[0],'%Y%m%dT%H%M%SZ')
            created_at=datetime.strptime(evidence['created_at'],'%Y-%m-%dT%H:%M:%SZ')
            requested_at=datetime.strptime(document['created_at'],'%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            raise AppError('MAC_HOST_ASTRA_RECEIPT_UNVERIFIED') from None
        skew=timedelta(seconds=CLOCK_SKEW_SECONDS)
        require(requested_at-skew<=invoked_at<=created_at+skew and created_at>=requested_at-skew,
                'MAC_HOST_ASTRA_RECEIPT_REPLAY')
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
