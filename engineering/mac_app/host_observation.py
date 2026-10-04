"""Protected-host read contract and offline handoff assessment.

Uses the existing v2 host's public views, never packets or signing nonces.
No transport or authority is installed here. Fixtures cannot authorize execution.
"""
from __future__ import annotations

import math
import re
import time

from common import AppError, digest, repository

LANES = {'DEVIN', 'GROK_BUILD', 'GLM', 'CURSOR'}
ACTIVE = {'SUBMITTING', 'CONFIRMED', 'UNKNOWN'}
STATES = ACTIVE | {'FAILED_PRESTART', 'RECONCILED'}
TERMINAL = {'SESSION_TERMINAL', 'SESSION_TERMINAL_VERIFIED'}
LIMIT = 8 * 1024 * 1024


def require(condition, code='HOST_OBSERVATION_INVALID'):
    if not condition: raise AppError(code)


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def one_of(value, options):
    return isinstance(value, str) and value in options


def identity(record):
    require(isinstance(record, dict) and record.get('kind') == 'legacy_plan_handoff')
    require(matches(record.get('id'), r'[0-9a-f]{16}'))
    require(matches(record.get('snapshot_sha256'), r'[0-9a-f]{64}'))
    snap = record.get('snapshot', {}); require(isinstance(snap, dict))
    source = snap.get('source', {}); require(isinstance(source, dict))
    require(isinstance(record.get('blockers'), list) and all(isinstance(b, dict) for b in record['blockers']))
    require(isinstance(snap.get('tasks'), list) and all(isinstance(t, dict) and
            (t.get('node') is None or isinstance(t['node'], str)) and
            (t.get('program') is None or isinstance(t['program'], str)) for t in snap['tasks']))
    repo = repository(record.get('repository'))
    require(snap.get('repository') == repo and matches(source.get('head'), r'[0-9a-f]{40}'))
    require(matches(source.get('program'), r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'), 'HOST_IDENTIFIER_UNSUPPORTED')
    nodes = source.get('node_ids')
    require(isinstance(nodes, list) and 1 <= len(nodes) <= 256)
    require(all(matches(n, r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}') for n in nodes), 'HOST_IDENTIFIER_UNSUPPORTED')
    require(len({n.lower() for n in nodes}) == len(nodes))
    return repo, source['program'], nodes, source['head']


def task_id(program, node):
    return (program + '-' + node).upper()


def query_plan(record):
    repo, program, nodes, head = identity(record)
    queries = [['materialize-list', '--repository', repo, '--program', program]]
    for node in nodes:
        queries += [['materialize-status', '--program', program, '--node', node],
                    ['task-status', '--repository', repo, '--task', task_id(program, node)]]
    queries.append(['status', '--lanes'])
    return {'schema_version': 1, 'handoff_id': record['id'], 'snapshot_sha256': record['snapshot_sha256'],
            'plan_commit': head, 'queries': queries, 'execution_allowed': False,
            'transport': 'NOT_CONFIGURED', 'dynamic_reads': 'status for every launch_request_id in task-status'}


def _rows(value, repo, task):
    require(isinstance(value, dict) and value.get('status') == 'OK' and
            value.get('repository') == repo and value.get('task') == task)
    rows = value.get('rows'); require(isinstance(rows, list) and len(rows) <= 4096)
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and row.get('repository') == repo and row.get('task') == task)
        rid = row.get('launch_request_id')
        require(matches(rid, r'[0-9a-f]{24}') and rid not in seen)
        seen.add(rid)
        require(one_of(row.get('state'), STATES) and one_of(row.get('role'), ('WRITER', 'REVIEWER')) and one_of(row.get('lane'), LANES))
        require(type(row.get('attempt_id')) is int and row['attempt_id'] > 0)
        require(isinstance(row.get('task_revision'), str) and row['task_revision'])
        require(type(row.get('reserved_ts')) in (int, float) and math.isfinite(row['reserved_ts']))
        require('packet' not in row and 'nonce' not in row)
    require(rows == sorted(rows, key=lambda x: (x['reserved_ts'], x['launch_request_id'])))
    return rows


def capture_fixture(record, read, *, clock=time.time):
    """Dependency-injected read adapter for tests/operator integration development.

    Never opens SSH, calls a workflow or reads the live protected ledger itself.
    The resulting envelope is explicitly a fixture, even if supplied real views.
    """
    if read is None: raise AppError('HOST_TRANSPORT_NOT_CONFIGURED')
    plan = query_plan(record); repo, program, nodes, _ = identity(record)
    started = clock(); responses = []; requests = set()
    for args in plan['queries']:
        result = read(list(args)); responses.append({'args': args, 'result': result})
        if args[0] == 'task-status':
            for row in _rows(result, repo, args[-1]): requests.add(row['launch_request_id'])
            require(len(requests) <= 4096, 'HOST_OBSERVATION_TOO_LARGE')
    for rid in sorted(requests):
        args = ['status', '--launch-request-id', rid]
        responses.append({'args': args, 'result': read(args)})
    return {'schema_version': 1, 'source': 'fixture', 'handoff_id': record['id'],
            'snapshot_sha256': record['snapshot_sha256'], 'started_at': started,
            'observed_at': clock(), 'responses': responses}


def assess(record, evidence, *, now=None):
    repo, program, nodes, head = identity(record); plan = query_plan(record)
    require(record['snapshot'].get('task_scope') == 'all' and
            all(one_of(t.get('state'), ('open', 'closed')) for t in record['snapshot']['tasks']),
            'HANDOFF_TASK_HISTORY_INCOMPLETE')
    require(isinstance(evidence, dict) and set(evidence) == {
        'schema_version', 'source', 'handoff_id', 'snapshot_sha256', 'started_at', 'observed_at', 'responses'})
    require(type(evidence['schema_version']) is int and evidence['schema_version'] == 1 and evidence['source'] == 'fixture')
    require((evidence['handoff_id'], evidence['snapshot_sha256']) == (record['id'], record['snapshot_sha256']), 'HOST_OBSERVATION_BINDING_MISMATCH')
    now = time.time() if now is None else now
    start, end = evidence['started_at'], evidence['observed_at']
    require(all(type(t) in (int, float) and math.isfinite(t) for t in (start, end, now)))
    require(0 <= end - start <= 60 and -5 <= now - end <= 60, 'HOST_OBSERVATION_STALE')
    responses = evidence['responses']; require(isinstance(responses, list) and len(responses) <= 10000)
    observed = {}
    for item in responses:
        require(isinstance(item, dict) and set(item) == {'args', 'result'})
        args = item['args']; require(isinstance(args, list) and all(isinstance(a, str) for a in args))
        key = tuple(args); require(key not in observed, 'HOST_OBSERVATION_DUPLICATE')
        observed[key] = item['result']
    def get(args):
        require(tuple(args) in observed, 'HOST_OBSERVATION_INCOMPLETE')
        return observed[tuple(args)]
    blockers = [dict(x) for x in record.get('blockers', []) if x.get('code') not in
                ('HOST_AUTHORITY_UNOBSERVED', 'HANDOFF_ADMISSION_NOT_AVAILABLE', 'LEGACY_OWNER_DECLARED')]
    def hold(code, **fields): blockers.append({'code': code, **fields})
    listing = get(plan['queries'][0])
    require(isinstance(listing, dict) and listing.get('repository') == repo and listing.get('program') == program)
    entries = listing.get('rows'); require(isinstance(entries, list) and len(entries) <= 10000)
    canonical = {}
    for row in entries:
        require(isinstance(row, dict) and row.get('program') == program and isinstance(row.get('node'), str))
        require(row['node'] not in canonical)
        canonical[row['node']] = row
        if row['node'] not in nodes: hold('HOST_UNPROJECTED_NODE', node=row['node'])
    projections = [x for x in record['snapshot']['tasks'] if x.get('program') == program]
    statuses, owners, history, dynamic, active = {}, {}, {}, [], {}
    bindings, recorded_plans, plan_advances = {}, {}, {}
    for node in nodes:
        node_projections = [x for x in projections if x.get('node') == node]
        projection = None
        status = get(['materialize-status', '--program', program, '--node', node])
        require(isinstance(status, dict) and status.get('program') == program and status.get('node') == node)
        state = status.get('status'); require(state in ('NOT_FOUND', 'CREATED', 'SUBMITTING', 'UNKNOWN', 'ABANDONED'))
        if state == 'NOT_FOUND':
            if node in canonical or any(x['state'] == 'open' for x in node_projections):
                hold('HOST_MATERIALIZATION_MISSING', node=node)
        else:
            require(status.get('repository') == repo and matches(status.get('request'), r'[0-9a-f]{24}') and
                    matches(status.get('plan_commit'), r'[0-9a-f]{40}'))
            listing_row = canonical.get(node)
            if listing_row is None or any(listing_row.get(f) != status.get(f) for f in ('program', 'node', 'status', 'request', 'issue', 'plan_commit')):
                hold('HOST_MATERIALIZATION_INCONSISTENT', node=node)
            if state != 'CREATED': hold('HOST_MATERIALIZATION_UNRESOLVED', node=node)
            else:
                require(type(status.get('issue')) is int and status['issue'] > 0)
                matching = [x for x in node_projections if
                            (x.get('number'), x.get('materialization_request_id')) == (status['issue'], status['request'])]
                if len(matching) != 1:
                    hold('HOST_CANONICAL_BINDING_MISMATCH', node=node)
                else:
                    projection = matching[0]
                    bindings[node] = {'issue': projection['number'], 'state': projection['state'],
                                      'materialization_request_id': status['request']}
            recorded_plans[node] = status['plan_commit']
            if status['plan_commit'] != head:
                plan_advances[node] = {'recorded': status['plan_commit'], 'requested': head,
                                       'validation': 'REQUIRED_BY_EXISTING_PROGRAM_START'}
        statuses[node] = state
        rows = _rows(get(['task-status', '--repository', repo, '--task', task_id(program, node)]), repo, task_id(program, node))
        history[node] = rows
        writer_attempts = [r for r in rows if r['role'] == 'WRITER']
        writers = [r for r in writer_attempts if r['state'] != 'FAILED_PRESTART']
        owners[node] = writers[0]['lane'] if writers else None
        if len({r['lane'] for r in writers}) > 1: hold('HOST_OWNER_HISTORY_CONFLICT', node=node)
        declared = (projection or {}).get('declared_owners', [])
        # A confirmed pre-start failure never acquired ownership. Its last lane
        # declaration can remain on the issue, but cannot become a new owner.
        failed_declaration = (not writers and writer_attempts and
                              declared == [writer_attempts[-1]['lane']])
        if declared and declared != [owners[node]] and not failed_declaration:
            hold('HOST_OWNER_PROJECTION_MISMATCH', node=node)
        for row in rows:
            rid = row['launch_request_id']; args = ['status', '--launch-request-id', rid]; dynamic.append(args)
            status_row = get(args)
            if not isinstance(status_row, dict) or status_row != {'status': 'FOUND', **row}:
                hold('HOST_LAUNCH_OBSERVATION_CHANGED', node=node, launch_request_id=rid)
            if row['state'] in ACTIVE:
                hold('HOST_EXECUTION_UNRESOLVED', node=node, launch_request_id=rid)
                require(rid not in active); active[rid] = row
            elif row['state'] == 'RECONCILED' and not one_of(row.get('resolution'), TERMINAL):
                hold('HOST_TERMINAL_EVIDENCE_UNVERIFIED', node=node, launch_request_id=rid)
    require(set(observed) == {tuple(a) for a in plan['queries'] + dynamic}, 'HOST_OBSERVATION_UNEXPECTED_QUERY')
    board = get(['status', '--lanes'])
    require(isinstance(board, dict) and board.get('status') == 'OK' and type(board.get('active_total')) is int and
            type(board.get('max_active_sessions')) is int and board['max_active_sessions'] > 0 and isinstance(board.get('lanes'), list))
    lane_names, board_requests, own_active, count = set(), set(), {}, 0
    for lane in board['lanes']:
        require(isinstance(lane, dict) and one_of(lane.get('lane'), LANES) and lane['lane'] not in lane_names and
                type(lane.get('enabled')) is bool and isinstance(lane.get('active'), list))
        lane_names.add(lane['lane'])
        for row in lane['active']:
            require(isinstance(row, dict) and matches(row.get('request'), r'[0-9a-f]{24}') and
                    row['request'] not in board_requests and row.get('lane') == lane['lane'] and one_of(row.get('state'), ACTIVE))
            count += 1; board_requests.add(row['request'])
            if row.get('repository') == repo: own_active[row['request']] = row
    require(lane_names and count == board['active_total'])
    if set(own_active) != set(active): hold('HOST_LANE_OBSERVATION_CHANGED')
    for rid in set(own_active) & set(active):
        if any(own_active[rid].get(f) != active[rid].get(f) for f in ('repository', 'task', 'role', 'lane', 'state')):
            hold('HOST_LANE_OBSERVATION_CHANGED', launch_request_id=rid)
    return {'schema_version': 1, 'handoff_id': record['id'], 'snapshot_sha256': record['snapshot_sha256'],
            'evidence_sha256': digest(evidence), 'source': 'fixture', 'execution_allowed': False,
            'observations_consistent': not blockers, 'blockers': blockers, 'owners': owners,
            'materializations': statuses, 'canonical_bindings': bindings,
            'recorded_plan_commits': recorded_plans, 'required_plan_advances': plan_advances,
            'launch_count': sum(len(r) for r in history.values()),
            'native_mac_admission': 'UNSUPPORTED', 'required_operations':
            ['QUALIFY_AUTHENTICATED_HOST_READ_TRANSPORT', 'USE_EXISTING_PROGRAM_START_AND_HOST_ADMISSION']}


def preview_start(record, evidence, node, request_id, *, now=None):
    assessment = assess(record, evidence, now=now)
    repo, program, nodes, head = identity(record)
    require(isinstance(node, str) and node in nodes and assessment['materializations'][node] == 'CREATED', 'HOST_CANONICAL_NODE_REQUIRED')
    require(assessment['observations_consistent'], 'HOST_OBSERVATION_BLOCKED')
    require(matches(request_id, r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}'), 'REQUEST_ID_REQUIRED')
    binding = assessment['canonical_bindings'].get(node)
    require(binding is not None and binding['state'] == 'open', 'HOST_CANONICAL_ISSUE_NOT_OPEN')
    issue = binding['issue']
    return {'scope': 'HANDOFF_PREVIEW_ONLY', 'execution_allowed': False, 'source': 'fixture',
            'assessment': assessment, 'owner_lane': assessment['owners'][node],
            'request': {'schema_version': 1, 'request_id': request_id, 'execution_host': 'current',
                        'repository': repo, 'operation': 'start', 'issue_number': issue,
                        'args': {'program': program, 'node': node, 'plan_commit': head}}}
