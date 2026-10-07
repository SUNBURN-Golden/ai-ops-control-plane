"""Read-only legacy observations and immutable local handoff preparation.

GitHub task projections do not prove protected-host ownership or quiescence.
These records never enter the worker queue and never grant execution authority.
"""
from __future__ import annotations

import base64
import binascii
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import re
import time
from urllib.parse import quote

from common import AppError, digest, parse_json, repository
from gitops import execute
from program_scope import PATH, load_scope

LIMIT = 8 * 1024 * 1024
IDENTIFIER = r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}'
TASK_KEY = re.compile(r'^<!-- ASTRA_TASK_KEY_V1 program=(' + IDENTIFIER +
                      r') node=(' + IDENTIFIER + r') request=([0-9a-f]{24}) -->$', re.MULTILINE)
UNRESOLVED = {'NOT_STARTED', 'SUBMITTING', 'CONFIRMED', 'UNKNOWN'}
API_READ_SECONDS = 30
_API_DEADLINE = ContextVar('mac_api_read_deadline', default=None)


@contextmanager
def api_read_budget():
    """Nested Mac reads share one monotonic budget, including lock wait time."""
    deadline = time.monotonic() + API_READ_SECONDS
    previous = _API_DEADLINE.get()
    token = _API_DEADLINE.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _API_DEADLINE.reset(token)


def bounded_api_reads(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        with api_read_budget():
            return function(*args, **kwargs)
    return bounded


def remaining_api_seconds():
    deadline = _API_DEADLINE.get()
    if deadline is None: return None
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise AppError('COMMAND_TIMEOUT')
    return remaining


def request(value, inspect=False):
    allowed = {'repository'} if inspect else {'repository', 'request_id', 'source_job_id'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise AppError('INVALID_HANDOFF_REQUEST')
    result = {'repository': repository(value.get('repository'))}
    if not inspect:
        rid = value.get('request_id')
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', rid):
            raise AppError('REQUEST_ID_REQUIRED')
        key = value.get('source_job_id')
        if key is not None:
            handoff_id(key, code='INVALID_JOB_ID')
        result.update(request_id=rid, source_job_id=key)
    return result


def handoff_id(value, code='INVALID_HANDOFF_ID'):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{16}', value):
        raise AppError(code)
    return value


def api(path, paginate=False):
    parts = path.split('/')
    if len(parts) >= 3 and parts[0] == 'repos':
        from repository_identity import current_pin, observe
        target = '/'.join(parts[1:3])
        if current_pin(target) is not None:
            metadata = observe(target, _raw_api)
            if len(parts) == 3 and not paginate:
                return metadata
    return _raw_api(path, paginate)


def _raw_api(path, paginate=False):
    args = ['gh', 'api', '--method', 'GET']
    if paginate: args += ['--paginate', '--slurp']
    try:
        remaining = remaining_api_seconds()
        raw = execute([*args, path], github_access='READ', **({'timeout': remaining} if remaining is not None else {}))
        remaining_api_seconds()
    except OSError as exc:
        raise AppError('CLI_SETUP_REQUIRED', 'GitHub CLI 실행 경로와 권한을 확인해 주세요.') from exc
    return parse_json(raw, LIMIT)


@bounded_api_reads
def inspect_repository(value):
    repo = repository(value)
    from repository_identity import reject_old_admission
    reject_old_admission(repo)
    root = 'repos/' + repo
    metadata = api(root)
    if (not isinstance(metadata, dict) or not isinstance(metadata.get('full_name'), str) or
            metadata['full_name'].lower() != repo.lower()):
        raise AppError('HANDOFF_REPOSITORY_MISMATCH')
    repo = repository(metadata['full_name'])
    root = 'repos/' + repo
    if metadata.get('archived') is not False: raise AppError('ARCHIVED_REPOSITORY')
    branch = metadata.get('default_branch')
    if not isinstance(branch, str) or not branch: raise AppError('EMPTY_REPOSITORY')
    commit = api(root + '/commits/' + quote(branch, safe=''))
    head = commit.get('sha') if isinstance(commit, dict) else None
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head):
        raise AppError('HANDOFF_HEAD_UNRESOLVED')
    content = api(root + '/contents/' + PATH + '?ref=' + head)
    if (not isinstance(content, dict) or content.get('type') != 'file' or
            content.get('encoding') != 'base64' or not isinstance(content.get('content'), str)):
        raise AppError('HANDOFF_PROGRAM_UNRESOLVED')
    try:
        raw = base64.b64decode(content['content'].replace('\n', ''), validate=True)
        program = raw.decode('utf-8')
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise AppError('HANDOFF_PROGRAM_UNRESOLVED') from exc
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if content.get('sha') != blob: raise AppError('HANDOFF_PROGRAM_BLOB_MISMATCH')
    scope = load_scope(program, repo, blob)
    pages = api(root + '/issues?state=all&labels=aiops-task&per_page=100', paginate=True)
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise AppError('HANDOFF_TASKS_UNRESOLVED')
    if sum(len(page) for page in pages) > 4096: raise AppError('HANDOFF_TOO_MANY_TASKS')
    tasks, blockers, historical_notes, numbers, keys = [], [], [], set(), set()
    for page in pages:
        for issue in page:
            if not isinstance(issue, dict): raise AppError('HANDOFF_TASKS_UNRESOLVED')
            if 'pull_request' in issue: continue
            number, body = issue.get('number'), issue.get('body')
            if (type(number) is not int or number < 1 or number in numbers or
                    issue.get('state') not in ('open', 'closed') or not isinstance(body, str)):
                raise AppError('HANDOFF_TASKS_UNRESOLVED')
            numbers.add(number)
            task = {'number': number, 'state': issue['state'], 'url': 'https://github.com/' + repo + '/issues/' + str(number),
                    'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'classification': 'unresolved_projection'}
            holds = blockers if issue['state'] == 'open' else historical_notes
            matches = TASK_KEY.findall(body)
            if len(matches) != 1:
                holds.append({'code': 'LEGACY_TASK_KEY_UNRESOLVED', 'issue': number})
            else:
                program_id, node, rid = matches[0]
                task.update(program=program_id, node=node, materialization_request_id=rid)
                key = (program_id, node)
                if issue['state'] == 'open' and key in keys:
                    blockers.append({'code': 'LEGACY_DUPLICATE_TASK', 'issue': number})
                if issue['state'] == 'open': keys.add(key)
                if program_id != scope['program'] or node not in scope['node_ids']:
                    holds.append({'code': 'LEGACY_PLAN_BINDING_MISMATCH', 'issue': number})
                else:
                    task['classification'] = 'registered_plan_projection'
            # Text declarations add conservative holds, never positive authority.
            owners = re.findall(r'^BUILDER_ID:[ \t]*([A-Z_]+)[ \t]*$', body, re.MULTILINE)
            if owners:
                task.update(classification='declared_owner_projection', declared_owners=sorted(set(owners)))
                holds.append({'code': 'LEGACY_OWNER_DECLARED', 'issue': number})
            states = re.findall(r'^LAUNCH_STATE:[ \t]*([A-Z_]+)[ \t]*$', body, re.MULTILINE)
            if set(states) & UNRESOLVED:
                holds.append({'code': 'LEGACY_LAUNCH_UNRESOLVED', 'issue': number})
            tasks.append(task)
    # This adapter has no protected-host reconciliation/admission capability.
    # Even an empty issue list or RELEASED declaration cannot remove these holds.
    blockers += [{'code': 'HOST_AUTHORITY_UNOBSERVED'}, {'code': 'HANDOFF_ADMISSION_NOT_AVAILABLE'}]
    return {'schema_version': 1, 'repository': repo, 'observed_at': time.time(),
            'source': {'head': head, 'branch': branch, 'path': PATH, 'blob': blob,
                       'program': scope['program'], 'node_ids': scope['node_ids'],
                       'node_count': scope['count'], 'raw_program': program},
            'tasks': tasks, 'task_scope': 'all', 'task_count': len(tasks), 'blockers': blockers,
            'historical_notes': historical_notes,
            'host_authority': 'unobserved', 'execution_allowed': False,
            'next_step': 'COMPARE_PROTECTED_HOST_RECORDS'}


def local_references(store, repo, source_job_id=None):
    """Caller holds Store.lock; preserve every existing document/event unchanged."""
    if source_job_id is not None:
        source = store.get(source_job_id)
        if source['repository'].lower() != repo.lower(): raise AppError('HANDOFF_SOURCE_JOB_MISMATCH')
    refs, blockers = [], []
    for job in store.jobs():
        if job['repository'].lower() != repo.lower(): continue
        # events() is a paged UI view; hash the complete history here.
        events = [dict(row) for row in store.db.execute(
            'SELECT * FROM events WHERE job_id=? ORDER BY sequence', (job['id'],))]
        refs.append({'job_id': job['id'], 'state': job['state'], 'phase': job['phase'],
                     'document_sha256': digest(job), 'events_sha256': digest(events),
                     'event_count': len(events), 'blocker_code': (job.get('blocker') or {}).get('code')})
        if job.get('attempt') or job['state'] == 'unknown':
            blockers.append({'code': 'LOCAL_EXECUTION_UNRESOLVED', 'job_id': job['id']})
        if job['state'] not in ('accepted', 'cancelled') or job['id'] in store.execution_busy:
            blockers.append({'code': 'LOCAL_JOB_NOT_TERMINAL', 'job_id': job['id']})
    return refs, blockers


def summary(value):
    result = copy.deepcopy(value)
    source = result.get('snapshot', result).get('source', {})
    source.pop('raw_program', None)
    result['projection'] = 'summary'
    return result
