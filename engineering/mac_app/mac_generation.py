"""Explicit owner-approved isolated work; never declares legacy work terminal.

One source node is pinned to a new task/revision. Only qualified local ACCEPTED
generations can satisfy its exact original dependencies. The immutable
boundary acknowledges the exact pre-existing opaque receipts and projections
for this new draft-only scope, without changing their origin, owner or state.
"""
from __future__ import annotations

import copy
import hashlib
import re
import time

from common import TERMINAL, digest, encoded, parse_json, repository
from mac_authority import require, original_task_key
from program_scope import load_scope
import handoff
from repository_identity import url_matches, same_repository, canonical_url
import transport_guard
import mac_bundle


@handoff.bounded_api_reads
def dependency_evidence(source,scope,node,plan_commit,*,current=True):
    """Consume only the same original revision's normal Mac completions."""
    if not node.get('depends_on',[]): return []
    import gitops
    from mac_pipeline import Pipeline
    code='MAC_GENERATION_DEPENDENCY_GATE_REQUIRED'; result=[]
    pipeline=Pipeline(source.store,source,gitops.Repositories(source.store.directory/'workspaces'))
    for dep in node.get('depends_on',[]):
        originals=[n for n in scope['nodes'] if n['id']==dep]; require(len(originals)==1,code)
        original=originals[0]; expected={'task_id':(scope['program']+'-'+dep).upper(),
            'task_revision':digest({'plan_blob':scope['blob'],'node':original}),'provenance_only':True}
        with source.store.lock:
            candidates=[]
            for row in source.store.db.execute("SELECT * FROM mac_host_tasks WHERE repository=? AND state='ACCEPTED'",
                                               (scope['repository'].lower(),)):
                bound=parse_json(row['binding'])
                if bound.get('node')==dep and 'generation_id' in bound: candidates.append((row,bound))
            require(len(candidates)==1,code); row,bound=candidates[0]
            work=parse_json(row['work'],1024*1024); generation=record(source,bound)
            require(bound['plan_blob']==scope['blob'] and bound['program']==scope['program'] and
                    work['task']==original and work['original_task']==generation['original_task']==expected and
                    digest(work)==bound['work_sha256'] and work['generation_policy']==POLICY,code)
        result.append(pipeline.accepted_dependency(bound,plan_commit,current=current))
    return result


@handoff.bounded_api_reads
def frozen_dependencies(source,bound,*,live=False):
    """Retain the pinned graph and recheck its protected completion lineage."""
    from mac_pipeline import Pipeline
    import gitops
    generation=record(source,bound); task=source._task(source.store.db,bound)
    work=parse_json(task['work'],1024*1024); node=work['task']; expected=node.get('depends_on',[])
    require(bound['dependencies']==expected and digest(work)==bound['work_sha256'],
            'MAC_GENERATION_BINDING_INVALID')
    frozen=work.get('dependency_evidence',[])
    require(isinstance(frozen,list) and [e.get('node') for e in frozen]==expected,
            'MAC_GENERATION_DEPENDENCY_GATE_REQUIRED')
    if not expected: return []
    pipeline=Pipeline(source.store,source,gitops.Repositories(source.store.directory/'workspaces'))
    for evidence in frozen: pipeline.validate_dependency_local(evidence)
    if live and expected:
        observed=dependency_evidence(source,generation['program']['scope'],node,bound['plan_commit'])
        require(observed==frozen,'MAC_HOST_DEPENDENCY_COMPLETION_CHANGED')
    return copy.deepcopy(frozen)

POLICY = {'auto_merge': False, 'draft_pr': True, 'user_only_merge': True,
          'legacy_terminal_verified': False, 'isolated_checkout': True}
FIELDS = {'repository', 'node', 'generation_id', 'decision', 'plan_commit', 'plan_blob'}

# The User's durable authority-boundary decision; prose supplied by a model
# or an arbitrary comment is never an approval. Edits/deletion fail closed.
HOST_DECISION = {
    'url': 'https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/77#issuecomment-6008874154',
    'comment_id': 6008874154, 'actor_id': 263336091, 'actor_login': 'BeautifulMind-JT',
    'body_sha256': '9cc695f0a5ce11789dc3cce57a09b03a19165c84ed0d91c201c5047cb19f8f9b',
}
DECISION_API = 'repos/BeautifulMind-JT/ai-ops-control-plane/issues/comments/6008874154'


def decision_evidence(pointer):
    require(pointer == HOST_DECISION['url'], 'MAC_GENERATION_DURABLE_DECISION_REQUIRED')
    comment = handoff.api(DECISION_API)
    require(isinstance(comment, dict) and comment.get('id') == HOST_DECISION['comment_id'] and
            url_matches(pointer, comment.get('html_url')) and
            url_matches('https://api.github.com/repos/BeautifulMind-JT/ai-ops-control-plane/issues/77', comment.get('issue_url')) and
            isinstance(comment.get('user'), dict) and
            comment['user'].get('id') == HOST_DECISION['actor_id'] and
            comment['user'].get('login') == HOST_DECISION['actor_login'] and
            comment['user'].get('type') == 'User' and isinstance(comment.get('body'), str) and
            hashlib.sha256(comment['body'].encode()).hexdigest() == HOST_DECISION['body_sha256'],
            'MAC_GENERATION_DURABLE_DECISION_UNVERIFIED')
    return copy.deepcopy(HOST_DECISION)


def require_unowned_original(snapshot, program, node):
    require(snapshot.get('task_scope') == 'all' and isinstance(snapshot.get('tasks'), list),
            'MAC_HOST_HISTORY_INCOMPLETE')
    proofs = []
    for task in snapshot['tasks']:
        require(isinstance(task, dict) and task.get('state') in ('open', 'closed'),
                'MAC_HOST_HISTORY_INCOMPLETE')
        # Open projections still hold unless an exact User-confirmed prestart
        # failure proves this original task acquired no owner/session.
        if task['state'] == 'open':
            require(task.get('program') and task.get('node'),
                    'MAC_GENERATION_LINUX_OWNER_UNRESOLVED')
            if original_task_key(task['program'],task['node']) == original_task_key(program,node):
                import mac_prestart
                proofs.append(mac_prestart.verify(snapshot.get('repository', ''), task, program, node))
    require(len(proofs) <= 1, 'MAC_GENERATION_LINUX_OWNER_UNRESOLVED')
    return proofs


def preflight(source, bound, snapshot):
    value = record(source, bound)
    require(decision_evidence(value['request']['decision']) == value['decision_evidence'],
            'MAC_GENERATION_DURABLE_DECISION_UNVERIFIED')
    nodes = value['request'].get('bundle', {}).get('nodes', [bound['node']])
    proofs = []
    for node in nodes: proofs.extend(require_unowned_original(snapshot, bound['program'], node))
    require(proofs == value.get('failed_prestart_evidence', []), 'MAC_GENERATION_LINUX_OWNER_UNRESOLVED')
    if 'bundle' in value['request']:
        require(mac_bundle.decision()==value['bundle_decision'], 'MAC_BUNDLE_DECISION_UNVERIFIED')
        mac_bundle.check_receipts(source, value['program']['scope'], nodes)


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
    require(value.get('decision_evidence') == HOST_DECISION and
            value['request']['decision'] == HOST_DECISION['url'],
            'MAC_GENERATION_DURABLE_DECISION_REQUIRED')
    if 'bundle' in value['request']:
        task, packet = mac_bundle.aggregate(value['program']['scope'], value['request']['bundle'])
        row = source.store.db.execute('SELECT work FROM mac_host_tasks WHERE repository=? AND task=?',
            (bound['repository'].lower(), bound['task_id'])).fetchone()
        require(row is not None, 'MAC_BUNDLE_BINDING_INVALID')
        work = parse_json(row[0], 1024*1024)
        require(value.get('bundle_decision')==mac_bundle.DECISION, 'MAC_BUNDLE_DECISION_UNVERIFIED')
        require(work.get('task') == task and work.get('bundle') == packet and digest(work) == bound['work_sha256'],
                'MAC_BUNDLE_BINDING_INVALID')
    import mac_prestart
    require(value.get('failed_prestart_evidence', []) in ([], [mac_prestart.evidence()]),
            'MAC_GENERATION_BINDING_INVALID')
    return value


@handoff.bounded_api_reads
def adopt(source, value):
    require(source.authority_initialized, 'MAC_HOST_NOT_INITIALIZED')
    require(isinstance(value, dict) and set(value) in (FIELDS, FIELDS | {'bundle'}), 'MAC_GENERATION_REQUEST_INVALID')
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
    snapshot = handoff.inspect_repository(repo, **({'bundle_candidate':True} if 'bundle' in value else {}))
    require(snapshot.get('task_scope') == 'all' and isinstance(snapshot.get('tasks'), list),
            'MAC_HOST_HISTORY_INCOMPLETE')
    original = snapshot['source']; raw = original['raw_program']; data = raw.encode()
    require(original['head'] == value['plan_commit'] and original['blob'] == value['plan_blob'] and
            hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == original['blob'],
            'MAC_HOST_PROGRAM_REVISION_CHANGED')
    scope = load_scope(raw, snapshot['repository'], original['blob'],bundle_repository_alias='bundle' in value)
    bundle = None
    if 'bundle' in value:
        bundle_approval = mac_bundle.decision()
        bundle_task, bundle = mac_bundle.aggregate(scope, value['bundle'])
        require(value['node'] == bundle_task['id'], 'MAC_BUNDLE_INVALID')
        prestart = []
        for member in bundle['nodes']:
            prestart.extend(require_unowned_original(snapshot, scope['program'], member['id']))
        mac_bundle.check_receipts(source, scope, value['bundle']['nodes'])
    else:
        prestart = require_unowned_original(snapshot, scope['program'], value['node'])
    approval = decision_evidence(value['decision'])
    matches = [bundle_task] if bundle else [n for n in scope['nodes'] if n['id'] == value['node']]
    require(len(matches) == 1, 'MAC_GENERATION_NODE_NOT_FOUND')
    node = matches[0]
    import mac_astra_receipt
    astra_decision = mac_astra_receipt.admission(node)
    # Legacy DONE/closed/merged/PASS text never supplies dependency authority.
    # This reader validates existing immutable, normally ACCEPTED Mac lineage.
    dependencies=dependency_evidence(source,scope,node,original['head'])
    from product_builder import resolve
    settings = resolve(source.store.settings(), repo); profile = settings['roles']['builder']
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
            'generation_decision_evidence': approval,
            'original_task': {'task_id': origin_task, 'task_revision': origin_revision, 'provenance_only': True}}
    if bundle: work['bundle'] = bundle
    if astra_decision: work['astra_decision'] = astra_decision
    if dependencies: work['dependency_evidence']=dependencies
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
            for row in mac_bundle.owned_rows(source,repo):
                owner = parse_json(row['binding'])
                require(not (mac_bundle.keys(owner['program'], parse_json(row['work'], 1024*1024)) &
                             mac_bundle.keys(scope['program'], work)),
                        'MAC_GENERATION_ORIGINAL_TASK_ALREADY_OWNED')
            if bundle: mac_bundle.check_receipts(source, scope, value['bundle']['nodes'])
            if dependencies:
                from mac_pipeline import Pipeline
                pipeline=Pipeline(source.store,source,None)
                for evidence in dependencies: pipeline.validate_dependency_local(evidence)
            cursor = db.execute('INSERT INTO mac_host_tasks(repository,task,binding,work,state) VALUES (?,?,?,?,?)',
                                (repo, task, '{}', encoded(work), 'READY'))
            bound = {'repository': snapshot['repository'], 'task_id': task, 'task_revision': revision,
                     'issue': cursor.lastrowid, 'program': scope['program'], 'node': node['id'],
                     'materialization_request_id': digest({'host': source.source_host, 'generation': value['generation_id']})[:24],
                     'plan_commit': original['head'], 'plan_blob': scope['blob'], 'dependencies': copy.deepcopy(node.get('depends_on',[])),
                     'owner_lane': lanes[profile['provider']], 'source_host': source.source_host,
                     'target_host': source.target_host, 'work_sha256': digest(work), 'authority_kind': 'MAC_LOCAL',
                     'canonical_task_pointer': 'mac-host:' + source.source_host + ':' + repo + ':' + task,
                     'generation_id': value['generation_id']}
            document = {'request': request, 'binding': bound, 'program': program, 'source_host': source.source_host,
                        'decision_evidence': approval,
                        'failed_prestart_evidence': prestart,
                        'policy': copy.deepcopy(POLICY), 'original_task': work['original_task'],
                        'legacy_claims': sorted({digest(c) for c in legacy} | {r[0] for r in db.execute(
                            'SELECT id FROM mac_host_external WHERE repository=?',(repo,))}),
                        'legacy_receipts': copy.deepcopy(receipts),
                        'adopted_at': time.time()}
            if bundle: document['bundle_decision']=bundle_approval
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
    nodes = value['request'].get('bundle', {}).get('nodes', [bound['node']])
    originals = {original_task_key(bound['program'], n) for n in nodes}
    for row in source.store.db.execute('SELECT id,task,document FROM mac_host_external WHERE repository=?',
                                      (bound['repository'].lower(),)):
        if row['task'].upper() not in originals | {bound['task_id'], '*'}: continue
        for node in nodes:
            proofs = require_unowned_original({'repository': bound['repository'], 'task_scope': 'all',
                                              'tasks': [parse_json(row['document'])['task']]}, bound['program'], node)
            require(not proofs or proofs == value.get('failed_prestart_evidence', []),
                    'MAC_GENERATION_LINUX_OWNER_UNRESOLVED')
        require(row['id'] in value['legacy_claims'], 'MAC_GENERATION_EXTERNAL_CHANGE_UNRESOLVED')
    if 'bundle' in value['request']: mac_bundle.check_receipts(source, value['program']['scope'], nodes)
    acknowledged = {digest(r) for r in value['legacy_receipts']}
    for fence in transport_guard.unresolved(source.store.directory, include_digest=True):
        require(digest(fence) in acknowledged, 'MAC_GENERATION_EXTERNAL_RECEIPT_UNRESOLVED')
