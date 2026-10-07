"""Scoped Mac product auditor: actual private execution AND live GitHub receipt.

This is MAC_GLM53, never ASTRA_FABLE. Original canonical requests are immutable.
The trusted Mac host account owns execution evidence; no signature is claimed.
"""
from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from common import AppError, atomic_json, digest, encoded, parse_json, private_directory
import handoff
import mac_astra_receipt as fable
from repository_identity import transport, url_matches, canonical_url

PRODUCER = 'MAC_GLM53'
MODEL = 'glm-5.3'
MARK = '<!-- aiops-mac-glm-audit-v1 -->'
DECISION = {'comment_id': 6031603785, 'created_at': '2026-10-07T05:27:40Z',
            'body_sha256': '8bb3a81d2cfd90d229f4c33d92f4ce74785df9225fd445321a460986bae1ce4f'}
JOB = '20531604498943e1'
ERROR = 'MAC_HOST_GLM_AUDIT_UNVERIFIED'


def require(value, code=ERROR):
    if not value: raise AppError(code)


def supported(requirement):
    request = requirement.get('request_binding', {})
    return request.get('job') == JOB


def scope(requirement):
    repo, number, request = fable.context(requirement)
    bound = request.get('canonical_binding', {})
    require(request.get('job') == JOB and repo.lower() == 'beautifulmind-jt/kix-protocol' and number == 121 and
            request['gate'] == 'ARCHITECTURE' and request['requested_depth'] == 'A3' and
            request.get('requested_auditor') == 'ASTRA_FABLE' and
            bound.get('authority_kind') == 'MAC_LOCAL' and bound.get('program') == 'kix' and
            bound.get('node') == 'agents-scope-sync' and
            bound.get('plan_commit') == '7481b0e16ce9b903abbffa62249bb91cd9e63cfe' and
            bound.get('plan_blob') == 'ff0f39a8129ca8b8d30818cce35c3d4e588872fc' and
            requirement.get('request_sha256') == digest(request))
    return digest({'repository_id': 1365416872, 'pr': number, 'head': request['head'], 'gate': request['gate']})


@handoff.bounded_api_reads
def approval(*, evidence=False):
    repo = 'SUNBURN-Golden/ai-ops-control-plane'
    value = handoff.api(f'repos/{repo}/issues/comments/{DECISION["comment_id"]}')
    require(fable.trusted_actor(value) and type(value.get('id')) is int and value['id'] == DECISION['comment_id'] and
            value.get('created_at') == value.get('updated_at') == DECISION['created_at'] and
            value.get('html_url') == f'https://github.com/{repo}/pull/84#issuecomment-{DECISION["comment_id"]}' and
            value.get('issue_url') == f'https://api.github.com/repos/{repo}/issues/84' and
            isinstance(value.get('body'), str) and fable.body_hash(value['body']) == DECISION['body_sha256'])
    return {**copy.deepcopy(DECISION),'body':value['body']} if evidence else copy.deepcopy(DECISION)


def private_bytes(path, limit=16*1024*1024):
    path = Path(path)
    for parent in (path.parent, path.parent.parent):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and
                not info.st_mode & 0o077 and info.st_size <= limit)
        with os.fdopen(fd, 'rb', closefd=False) as stream: return stream.read(limit+1)
    finally: os.close(fd)


def session_key(value):
    return value.rsplit(':', 1)[-1] if isinstance(value, str) else ''


def validate_output(raw, run):
    """Model prose is never interpreted as a harness event or execution receipt."""
    events = [parse_json(line, 16*1024*1024) for line in raw.decode().splitlines() if line.strip()]
    init = [e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init']
    result = [e for e in events if e.get('type') == 'result']
    require(len(init) == len(result) == 1)
    start, final = init[0], result[0]
    require(start.get('model') == MODEL and start.get('session_id') == final.get('session_id') == run['session'] and
            set(start.get('tools', [])) <= {'StructuredOutput'} and
            final.get('is_error') is False and final.get('subtype') == 'success' and
            not final.get('permission_denials') and not final.get('errors') and
            set(final.get('modelUsage', {})) == {MODEL})
    models = set()
    for event in events:
        if event.get('type') == 'assistant':
            message = event.get('message', {}); models.add(message.get('model'))
            require(all(c.get('type') != 'tool_use' or c.get('name') == 'StructuredOutput'
                        for c in message.get('content', [])))
        if event.get('type') == 'rate_limit_event':
            rate = event.get('rate_limit_info', {})
            require(rate.get('isUsingOverage') is False and
                    (rate.get('overageStatus') == 'rejected' or
                     'overageStatus' not in rate and rate.get('status') in ('allowed', 'allowed_warning')))
    require(models == {MODEL})
    verdict = final.get('structured_output'); request = run['request']
    require(isinstance(verdict, dict) and set(verdict) == {
        'request_sha256','packet_sha256','head','gate','depth','result','contract_change_required','summary','findings','decision_question'})
    require(verdict['request_sha256'] == run['request_sha256'] and verdict['packet_sha256'] == run['packet_sha256'] and
            verdict['head'] == request['head'] and verdict['gate'] == request['gate'] and verdict['depth'] == 'A3' and
            verdict['result'] in ('PASS','PASS_WITH_NOTES','FAIL','DECISION_REQUIRED') and
            verdict['contract_change_required'] in ('YES','NO') and isinstance(verdict['summary'],str) and verdict['summary'].strip() and
            isinstance(verdict['decision_question'],str) and isinstance(verdict['findings'],list))
    for finding in verdict['findings']:
        require(isinstance(finding,dict) and set(finding)=={'severity','pointer','detail'} and
                finding['severity'] in ('NOTE','BLOCKING') and
                all(isinstance(finding[k],str) and finding[k].strip() for k in ('pointer','detail')))
    if verdict['result'] in ('PASS','PASS_WITH_NOTES'):
        require(not verdict['decision_question'] and all(f['severity']=='NOTE' for f in verdict['findings']))
        if verdict['result']=='PASS': require(not verdict['findings'])
    if verdict['result']=='FAIL': require(any(f['severity']=='BLOCKING' for f in verdict['findings']))
    if verdict['result']=='DECISION_REQUIRED': require(verdict['decision_question'].strip())
    return verdict


def comment_body(run, verdict):
    return MARK + '\n' + encoded({'schema':'MAC_GLM_AUDIT_V1','producer':PRODUCER,'model':MODEL,
        'session':run['session'],'request_sha256':run['request_sha256'],'packet_sha256':run['packet_sha256'],
        'execution_sha256':run['files']['stdout.jsonl'],'verdict':verdict}) + '\n'


class Journal:
    def __init__(self, store):
        self.store = store
        self.db = fable.Journal(store).db
        with store.lock:
            self.db.execute('CREATE TABLE IF NOT EXISTS mac_glm_runs(request TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,document TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS mac_glm_holds(scope TEXT PRIMARY KEY,code TEXT NOT NULL)')

    def get(self, requirement):
        scope(requirement)
        with self.store.lock:
            row = self.db.execute('SELECT * FROM mac_glm_runs WHERE request=?',(requirement['request_sha256'],)).fetchone()
        return (row['state'],parse_json(row['document'],4*1024*1024)) if row else (None,None)

    def hold(self, requirement, code=ERROR):
        with self.store.lock:
            self.db.execute('INSERT OR IGNORE INTO mac_glm_holds VALUES (?,?)',(scope(requirement),code))
        raise AppError(code)

    def check_hold(self, requirement):
        with self.store.lock:
            row = self.db.execute('SELECT code FROM mac_glm_holds WHERE scope=?',(scope(requirement),)).fetchone()
        if row: raise AppError(row[0])

    def check_related(self, requirement):
        # A revised review/request cannot hide an earlier same-HEAD receipt or
        # launch with an unknown outcome. Never transplant its PASS to this request.
        with self.store.lock:
            rows=self.db.execute('SELECT request,document FROM mac_glm_runs WHERE scope=? AND request!=?',
                                 (scope(requirement),requirement['request_sha256'])).fetchall()
        for row in rows:
            run=parse_json(row['document'])
            self.consume({'request_sha256':row['request'],'request_binding':run['request']})

    def save(self, requirement, state, run):
        with self.store.lock:
            self.db.execute('UPDATE mac_glm_runs SET state=?,document=? WHERE request=?',
                            (state,encoded(run),requirement['request_sha256']))

    def folder(self, requirement):
        return self.store.directory/'mac-glm-audits'/requirement['request_sha256']

    def proof(self, requirement, run):
        original=self.db.execute('SELECT document FROM mac_host_astra_requests WHERE request_sha256=?',
                                 (requirement['request_sha256'],)).fetchone()
        require(original is not None)
        requested=parse_json(original[0])
        requested_time=datetime.strptime(requested['created_at'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        require(run['request'] == requirement['request_binding'] and run['request_sha256'] == requirement['request_sha256'] and
                run['approval'] == DECISION and run['producer']==PRODUCER and run['model']==MODEL and
                requested['request']==run['request'] and requested['created_at']==run['requested_at'] and
                isinstance(run.get('cli_path'),str) and Path(run['cli_path']).is_absolute() and
                all(isinstance(run.get(k),str) and re.fullmatch('[0-9a-f]{64}',run[k])
                    for k in ('cli_sha256','producer_code_sha256')) and type(run.get('pid')) is int and run['pid']>1 and
                run['started']>=requested_time-30 and isinstance(run['session'],str) and
                re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',run['session']) and
                set(run['request'].get('writer_sessions',[]))<=set(run['excluded_sessions']) and
                run.get('exit_code') == 0 and run.get('process_group_quiescent') is True and
                run.get('finished',0)>=run['started'] and
                run['session'] not in {session_key(s) for s in run['excluded_sessions']})
        require(set(run['files'])=={'packet.json','stdout.jsonl','stderr.txt'})
        folder = self.folder(requirement)
        for name, expected in run['files'].items():
            require(hashlib.sha256(private_bytes(folder/name)).hexdigest()==expected)
        packet = private_bytes(folder/'packet.json')
        require(hashlib.sha256(packet).hexdigest()==run['packet_sha256'])
        context = parse_json(packet.decode(),2*1024*1024)
        require(context['request']==run['request'] and context['excluded_sessions']==run['excluded_sessions'])
        verdict=validate_output(private_bytes(folder/'stdout.jsonl'),run)
        require(verdict==run['verdict'])
        return verdict

    @handoff.bounded_api_reads
    def consume(self, requirement):
        self.check_hold(requirement)
        state,run=self.get(requirement)
        require(state is not None,'MAC_HOST_ASTRA_AUDIT_REQUIRED')
        require(state=='PUBLISHED', 'MAC_HOST_GLM_AUDIT_PENDING')
        approval(); fable.live_pr(requirement)
        try:
            verdict=self.proof(requirement,run)
            require(verdict['result'] in ('PASS','PASS_WITH_NOTES') and verdict['contract_change_required']=='NO')
            repo,number,_=fable.context(requirement)
            comment=handoff.api(f'repos/{repo}/issues/comments/{run["comment"]["id"]}')
            self.verify_comment(requirement,run,comment)
            pages=handoff.api(f'repos/{repo}/issues/{number}/comments?per_page=100',paginate=True)
            require(isinstance(pages,list) and pages and all(isinstance(p,list) for p in pages) and sum(map(len,pages))<=4096)
            matches=[c for page in pages for c in page if c.get('id')==comment['id']]
            require(len(matches)==1)
            self.verify_comment(requirement,run,matches[0])
            fable.live_pr(requirement)
        except (AppError,OSError,KeyError,TypeError,ValueError): self.hold(requirement)
        proof={'schema':'MAC_GLM_AUDIT_V1','comment_id':comment['id'],
               'comment_url':canonical_url(repo,comment['html_url']),'body_sha256':fable.body_hash(comment['body']),
               'created_at':comment['created_at'],'updated_at':comment['updated_at'],'repository':repo,'pr':number,
               'head':verdict['head'],'result':verdict['result'],'depth':verdict['depth'],
               'auditor_identity':PRODUCER,'auditor_model':MODEL,'auditor_session':'mac-glm53:'+run['session'],
               'contract_change_required':verdict['contract_change_required'],'execution_sha256':run['files']['stdout.jsonl']}
        return {'schema_version':1,'source':'AUTHENTICATED_MAC_GLM_AUDIT_COMMENT',
                'request_sha256':requirement['request_sha256'],'request':copy.deepcopy(run['request']),
                'decision':copy.deepcopy(DECISION),'comment':proof}

    def verify_comment(self,requirement,run,comment):
        repo,number,_=fable.context(requirement)
        require(isinstance(comment,dict) and fable.trusted_actor(comment) and type(comment.get('id')) is int and
                comment['id']==run['comment']['id'] and comment.get('created_at')==comment.get('updated_at')==run['comment']['created_at'] and
                url_matches(f'https://github.com/{repo}/pull/{number}#issuecomment-{comment["id"]}',comment.get('html_url')) and
                url_matches(f'https://api.github.com/repos/{repo}/issues/{number}',comment.get('issue_url')) and
                comment.get('body')==comment_body(run,run['verdict']) and
                fable.body_hash(comment['body'])==run['comment']['body_sha256'])
        posted=datetime.strptime(comment['created_at'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        require(run['finished']-30<=posted<=time.time()+30)


def existing_receipt(store, requirement):
    if not supported(requirement): return None
    journal=Journal(store);journal.check_hold(requirement);journal.check_related(requirement)
    state,_=journal.get(requirement)
    return journal.consume(requirement) if state is not None else None


def require_idle(directory):
    path=Path(directory)/'mac-astra.sqlite3'
    if not path.exists(): return
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077 and info.st_nlink==1)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='mac_glm_runs'").fetchone():
            require(not db.execute("SELECT 1 FROM mac_glm_runs WHERE state IN ('RUNNING','PUBLISHING')").fetchone(),
                    'MAC_HOST_GLM_AUDIT_PENDING')
