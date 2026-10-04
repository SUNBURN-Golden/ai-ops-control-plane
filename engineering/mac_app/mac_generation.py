"""Explicit owner-approved isolated work; never declares legacy work terminal.

One dependency-free source node is pinned to a new task/revision. The immutable
boundary acknowledges the exact pre-existing opaque receipts and projections
for this new draft-only scope, without changing their origin, owner or state.
"""
from __future__ import annotations

import copy
import hashlib
import re
import time

from common import TERMINAL, digest, encoded, parse_json, repository
from mac_authority import require
from program_scope import load_scope
import handoff
import transport_guard

POLICY = {'auto_merge': False, 'draft_pr': True, 'user_only_merge': True,
          'legacy_terminal_verified': False, 'isolated_checkout': True}
FIELDS = {'repository', 'node', 'generation_id', 'decision', 'plan_commit', 'plan_blob'}


def claim(repo, task):
    key = (task.get('program', '') + '-' + task.get('node', '')).upper() if task.get('node') else '*'
    return {'origin': 'GITHUB_PROJECTION', 'state': 'UNRESOLVED', 'task': copy.deepcopy(task),
            'repository': repo, 'task_key': key}


def record(source, bound):
    key = bound.get('generation_id')
    require(isinstance(key, str) and re.fullmatch(r'[0-9a-f]{32}', key), 'MAC_GENERATION_BINDING_INVALID')
    row = source.store.db.execute('SELECT document FROM mac_host_generations WHERE id=?', (key,)).fetchone()
    require(row is not None, 'MAC_GENERATION_NOT_FOUND')
    value = parse_json(row[0], 4 * 1024 * 1024)
    require(value['binding'] == bound and value['source_host'] == source.source_host and
            value['policy'] == POLICY, 'MAC_GENERATION_BINDING_INVALID')
    return value


def adopt(source, value):
    require(source.authority_initialized, 'MAC_HOST_NOT_INITIALIZED')
    require(isinstance(value, dict) and set(value) == FIELDS, 'MAC_GENERATION_REQUEST_INVALID')
    repo = repository(value['repository']).lower()
    require(isinstance(value['generation_id'], str) and re.fullmatch(r'[0-9a-f]{32}', value['generation_id']) and
            isinstance(value['node'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', value['node']) and
            isinstance(value['decision'], str) and 1 <= len(value['decision']) <= 2000 and
            all(isinstance(value[k], str) and re.fullmatch(r'[0-9a-f]{40}', value[k])
                for k in ('plan_commit', 'plan_blob')), 'MAC_GENERATION_REQUEST_INVALID')
    request = {**value, 'repository': repo}
    with source.store.lock:
        previous = source.store.db.execute('SELECT document FROM mac_host_generations WHERE id=?',
                                          (value['generation_id'],)).fetchone()
        if previous:
            old = parse_json(previous[0], 4 * 1024 * 1024)
            require(old['request'] == request, 'MAC_GENERATION_IMMUTABLE')
            record(source, old['binding'])
            return copy.deepcopy(old['binding'])
    snapshot = handoff.inspect_repository(repo)
    require(snapshot.get('task_scope') == 'all' and isinstance(snapshot.get('tasks'), list),
            'MAC_HOST_HISTORY_INCOMPLETE')
    original = snapshot['source']; raw = original['raw_program']; data = raw.encode()
    require(original['head'] == value['plan_commit'] and original['blob'] == value['plan_blob'] and
            hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == original['blob'],
            'MAC_HOST_PROGRAM_REVISION_CHANGED')
    scope = load_scope(raw, snapshot['repository'], original['blob'])
    matches = [n for n in scope['nodes'] if n['id'] == value['node']]
    require(len(matches) == 1, 'MAC_GENERATION_NODE_NOT_FOUND')
    node = matches[0]
    require(node.get('audit_floor', 'A1') != 'A3' and node.get('astra_gate', 'NONE') == 'NONE',
            'MAC_HOST_ASTRA_GATE_REQUIRED')
    # No legacy dependency completion is inherited. Later dependent adoption
    # needs a separately implemented, proven new-generation dependency reader.
    require(not node.get('depends_on', []), 'MAC_GENERATION_DEPENDENCY_GATE_REQUIRED')
    settings = source.store.settings(); profile = settings['roles']['builder']
    lanes = {'devin': 'DEVIN', 'cursor': 'CURSOR', 'glm': 'GLM', 'grok_build': 'GROK_BUILD',
             'claude': 'MAC_CLAUDE', 'codex': 'MAC_CODEX'}
    require(profile['provider'] in lanes and settings['publish_pr'] is True, 'MAC_HOST_PROFILE_UNSUPPORTED')
    origin_task = (scope['program'] + '-' + node['id']).upper()
    origin_revision = digest({'plan_blob': scope['blob'], 'node': node})
    task = 'MAC-' + value['generation_id'].upper() + '-' + digest({'original_task':origin_task})[:12].upper()
    require(len(task) <= 64 and len(scope['program']) <= 64 and len(node['id']) <= 64,
            'MAC_HOST_TASK_IDENTIFIER_UNSUPPORTED')
    revision = digest({'generation_id': value['generation_id'], 'original_revision': origin_revision, 'policy': POLICY})
    work = {'task_id': task, 'task_revision': revision, 'plan_commit': original['head'],
            'profile': profile, 'settings': settings, 'task': copy.deepcopy(node), 'original_plan': raw,
            'generation_policy': copy.deepcopy(POLICY), 'generation_decision': value['decision'],
            'original_task': {'task_id': origin_task, 'task_revision': origin_revision, 'provenance_only': True}}
    require(len(encoded(work).encode()) <= 300000, 'MAC_HOST_SCOPE_TOO_LARGE')
    program = {'scope': scope, 'head': original['head'], 'branch': original['branch'],
               'profile': profile, 'settings': settings, 'tasks': copy.deepcopy(snapshot['tasks'])}
    legacy = [claim(repo, t) for t in snapshot['tasks']]
    receipts = transport_guard.unresolved(source.store.directory, include_digest=True)
    with source.store.lock:
        db = source.store.db; db.execute('BEGIN IMMEDIATE')
        try:
            previous = db.execute('SELECT document FROM mac_host_generations WHERE id=?',
                                  (value['generation_id'],)).fetchone()
            if previous:
                old = parse_json(previous[0], 4 * 1024 * 1024)
                require(old['request'] == request, 'MAC_GENERATION_IMMUTABLE')
                db.execute('COMMIT'); return copy.deepcopy(old['binding'])
            require(not any(j['attempt'] or j['state'] not in TERMINAL for j in source.store.jobs()) and
                    not source.store.execution_busy and
                    not db.execute("SELECT 1 FROM mac_host_attempts WHERE state!='TERMINAL'").fetchone(),
                    'MAC_HOST_LOCAL_WORK_BUSY')
            require(not db.execute("SELECT 1 FROM native_local WHERE state!='TERMINAL'").fetchone(),
                    'MAC_HOST_LOCAL_WORK_BUSY')
            cursor = db.execute('INSERT INTO mac_host_tasks(repository,task,binding,work,state) VALUES (?,?,?,?,?)',
                                (repo, task, '{}', encoded(work), 'READY'))
            bound = {'repository': snapshot['repository'], 'task_id': task, 'task_revision': revision,
                     'issue': cursor.lastrowid, 'program': scope['program'], 'node': node['id'],
                     'materialization_request_id': digest({'host': source.source_host, 'generation': value['generation_id']})[:24],
                     'plan_commit': original['head'], 'plan_blob': scope['blob'], 'dependencies': [],
                     'owner_lane': lanes[profile['provider']], 'source_host': source.source_host,
                     'target_host': source.target_host, 'work_sha256': digest(work), 'authority_kind': 'MAC_LOCAL',
                     'canonical_task_pointer': 'mac-host:' + source.source_host + ':' + repo + ':' + task,
                     'generation_id': value['generation_id']}
            document = {'request': request, 'binding': bound, 'program': program, 'source_host': source.source_host,
                        'policy': copy.deepcopy(POLICY), 'original_task': work['original_task'],
                        'legacy_claims': sorted({digest(c) for c in legacy} | {r[0] for r in db.execute(
                            'SELECT id FROM mac_host_external WHERE repository=?',(repo,))}),
                        'legacy_receipts': copy.deepcopy(receipts),
                        'adopted_at': time.time()}
            db.execute('UPDATE mac_host_tasks SET binding=? WHERE id=?', (encoded(bound), cursor.lastrowid))
            db.execute('INSERT INTO mac_host_generations VALUES (?,?,?)', (value['generation_id'], repo, encoded(document)))
            for item in legacy:
                db.execute('INSERT OR IGNORE INTO mac_host_external VALUES (?,?,?,?)',
                           (digest(item), repo, item['task_key'], encoded(item)))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK'); raise
    return copy.deepcopy(bound)


def check_external(source, bound):
    value = record(source, bound)
    original = value['original_task']['task_id']
    for row in source.store.db.execute('SELECT id FROM mac_host_external WHERE repository=? AND task IN (?,?,?)',
                                      (bound['repository'].lower(), original, bound['task_id'], '*')):
        require(row['id'] in value['legacy_claims'], 'MAC_GENERATION_EXTERNAL_CHANGE_UNRESOLVED')
    acknowledged = {digest(r) for r in value['legacy_receipts']}
    for fence in transport_guard.unresolved(source.store.directory, include_digest=True):
        require(digest(fence) in acknowledged, 'MAC_GENERATION_EXTERNAL_RECEIPT_UNRESOLVED')
