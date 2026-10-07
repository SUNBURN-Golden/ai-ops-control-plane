"""A new KIX deliverable with immutable original members, never legacy completion."""
import copy
import re

from common import AppError, digest, encoded
from mac_authority import require, original_task_key
from product_builder import REPOSITORY
from repository_identity import current_name


def aggregate(scope, value):
    require(scope['repository'].lower() == REPOSITORY and scope['program'].lower() == 'kix',
            'MAC_BUNDLE_KIX_ONLY')
    require(isinstance(value, dict) and set(value) == {'id', 'title', 'nodes'} and
            isinstance(value['id'], str) and re.fullmatch(r'bundle-[a-z0-9][a-z0-9-]{0,47}', value['id']) and
            value['id'] not in scope['node_ids'] and isinstance(value['title'], str) and
            1 <= len(value['title'].strip()) <= 160 and isinstance(value['nodes'], list) and
            2 <= len(value['nodes']) <= 32 and all(isinstance(n, str) for n in value['nodes']) and
            len(set(value['nodes'])) == len(value['nodes']), 'MAC_BUNDLE_INVALID')
    ids = value['nodes']; originals = {n['id']: n for n in scope['nodes']}
    require(set(ids) <= set(originals), 'MAC_BUNDLE_NODE_NOT_FOUND')
    members = [copy.deepcopy(originals[n]) for n in ids]
    seen = set(); external = []
    for node in members:
        for dep in node.get('depends_on', []):
            if dep in ids:
                require(dep in seen, 'MAC_BUNDLE_ORDER_REQUIRED')
            elif dep not in external:
                external.append(dep)
        seen.add(node['id'])
    # Mixed gates retain their original declarations in members. The aggregate
    # always requires A3; RELEASE can never be weakened to ARCHITECTURE.
    gate = 'RELEASE' if any(n.get('astra_gate') == 'RELEASE' for n in members) else 'ARCHITECTURE'
    packet = {'id': value['id'], 'title': value['title'], 'nodes': members,
              'external_dependencies': external, 'plan_blob': scope['blob']}
    spec = ('Implement this one new productization deliverable in dependency order. '
            'Every original spec and internal dependency below remains mandatory. '
            'A prior node is not complete merely because a later node ran. '
            'Unresolved contract decisions stay blocked. Do not modify original jobs, receipts or owners. '
            'Provide covered_tasks in exactly the member order and an executed evidence check '
            'prefixed node:<id>: for EVERY member. Independent review, exact-HEAD CI, A3, '
            'User merge and post-merge verification remain required.\n' + encoded(packet))
    require(len(spec) <= 65536, 'MAC_BUNDLE_TOO_LARGE')
    task = {'id': value['id'], 'title': value['title'], 'spec': spec,
            'depends_on': external, 'audit_floor': 'A3', 'astra_gate': gate,
            'deliverable_mode': 'PR', 'user_merge': True, 'astra_auto_merge': False}
    return task, packet


def members(work):
    return work.get('bundle', {}).get('nodes', [work['task']])


def keys(program, work):
    return {original_task_key(program, n['id']) for n in members(work)}


def plan_tasks(work):
    if 'bundle' not in work:
        nodes = [work['task']]
    else:
        nodes = work['bundle']['nodes']
    selected = {n['id'] for n in nodes}
    return [{'id': n['id'], 'title': n['title'], 'instructions': n['spec'],
             'acceptance': ['Satisfy the complete original task spec and repository verification requirements.'],
             'depends_on': [d for d in n.get('depends_on', []) if d in selected]} for n in nodes]


def validate_report(report, packet):
    if not packet or report.get('status') != 'complete':
        return
    ids = [n['id'] for n in packet['nodes']]
    require(report.get('covered_tasks') == ids and all(
        any(c.startswith('node:' + key + ':') and c[len('node:' + key + ':'):].strip()
            for c in report.get('checks', [])) for key in ids), 'MAC_BUNDLE_MEMBER_EVIDENCE_REQUIRED')


def check_receipts(source, scope, selected):
    """Unlike legacy isolated generations, a bundle cannot acknowledge UNKNOWN."""
    import transport_guard
    task_keys = {original_task_key(scope['program'], n) for n in selected}
    from common import parse_json
    for fence in transport_guard.unresolved(source.store.directory, include_digest=True):
        row = source.store.db.execute('SELECT document FROM mac_host_receipt_scopes WHERE request=?',
                                      (fence['request_id'],)).fetchone()
        require(row is not None, 'MAC_BUNDLE_RECEIPT_SCOPE_UNRESOLVED')
        record = parse_json(row[0])
        require(record['payload_sha256'] == fence['payload_sha256'], 'MAC_HOST_RECEIPT_ORIGIN_UNVERIFIED')
        require(not (current_name(record['repository']).lower() == current_name(scope['repository']).lower() and
                     record['task_id'].upper() in task_keys | {'*'}), 'MAC_BUNDLE_OWNER_UNRESOLVED')


def prepare(source, value):
    """Read-only candidate; no task reservation, writer or execution authority."""
    import handoff
    from program_scope import load_scope
    from common import repository
    import mac_generation
    require(isinstance(value, dict) and set(value) == {'repository', 'bundle', 'plan_commit', 'plan_blob'},
            'MAC_BUNDLE_REQUEST_INVALID')
    snapshot = handoff.inspect_repository(repository(value['repository']))
    original = snapshot['source']
    require((original['head'], original['blob']) == (value['plan_commit'], value['plan_blob']),
            'MAC_HOST_PROGRAM_REVISION_CHANGED')
    scope = load_scope(original['raw_program'], snapshot['repository'], original['blob'])
    task, packet = aggregate(scope, value['bundle'])
    blockers = []
    for node in packet['nodes']:
        try: mac_generation.require_unowned_original(snapshot, scope['program'], node['id'])
        except AppError as exc: blockers.append({'node': node['id'], 'code': exc.code})
    try: check_receipts(source, scope, value['bundle']['nodes'])
    except AppError as exc: blockers.append({'code': exc.code})
    with source.store.lock:
        from common import parse_json
        selected = keys(scope['program'], {'task': task, 'bundle': packet})
        for row in owned_rows(source,scope['repository']):
            bound, work = parse_json(row['binding']), parse_json(row['work'], 1024*1024)
            if selected & keys(bound['program'], work):
                blockers.append({'code': 'MAC_GENERATION_ORIGINAL_TASK_ALREADY_OWNED', 'task': bound['task_id']})
    try: mac_generation.dependency_evidence(source, scope, task, original['head'])
    except AppError as exc: blockers.append({'code': exc.code})
    return {'status': 'CANDIDATE_ONLY', 'execution_authorized': False, 'request': copy.deepcopy(value),
            'candidate_sha256': digest(value), 'bundle': packet, 'task': task,
            'blockers': blockers, 'completion': 'NOT_EXECUTED',
            'audit_note': 'Required original gates and aggregate A3 remain; PR121 MAC_GLM53 exception does not apply.'}


def owned_rows(source, repository):
    # Namespace aliases can add a hold, never grant admission or completion.
    return [row for row in source.store.db.execute('SELECT repository,binding,work FROM mac_host_tasks')
            if current_name(row['repository']).lower() == current_name(repository).lower()]
