"""Import a pinned local schema-v1 program without inventing execution authority.

An approval pointer is provenance, not a protected approval receipt. This module
preserves declared gates and does not evaluate them or authorize merge/release.
"""
from __future__ import annotations

import re
import copy

from common import AppError, encoded, parse_json, repository as validate_repository, text

PATH = '.aiops/program.json'
LIMIT = 2 * 1024 * 1024
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')
TOP_REQUIRED = {'schema_version', 'program', 'repository', 'approval_pointer',
                'authoritative_doc_pointers', 'nodes'}
NODE_REQUIRED = {'id', 'title', 'spec'}
NODE_OPTIONAL = {'depends_on', 'audit_floor', 'astra_gate', 'deliverable_mode',
                 'user_merge', 'astra_auto_merge'}


def unsupported(message):
    return AppError('PROGRAM_SCOPE_UNSUPPORTED',
                    '기존 프로그램 범위를 확인해야 합니다. ' + message)


def load_scope(raw, repository, blob, *, bundle_repository_alias=False):
    """Validate and retain exact local nodes at a known Git blob.

    Missing optional fields remain missing. Cross-repository dependencies and
    expanded registration schemas require a different adopted reader; an empty
    external-dependency field must not imply that this reader supports it.
    """
    try:
        data = parse_json(raw, LIMIT) if isinstance(raw, str) else parse_json(encoded(raw), LIMIT)
    except (AppError, TypeError, ValueError, RecursionError) as exc:
        raise unsupported(PATH + '의 JSON 또는 크기를 확인해 주세요.') from exc
    if (not isinstance(data, dict) or set(data) - (TOP_REQUIRED | {'project'}) or
            not TOP_REQUIRED <= set(data) or type(data.get('schema_version')) is not int or
            data['schema_version'] != 1):
        raise unsupported('로컬 schema_version 1 형식만 지원합니다.')
    if not isinstance(blob, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', blob):
        raise unsupported('프로그램의 정확한 Git blob이 필요합니다.')
    try:
        target = validate_repository(data['repository'])
        expected = validate_repository(repository)
        for field in ('approval_pointer', 'authoritative_doc_pointers'):
            text(data[field], field)
        if 'project' in data:
            text(data['project'], 'project', 160)
    except AppError as exc:
        raise unsupported('대상 레포와 기준 문서·결정 포인터를 확인해 주세요.') from exc
    if target.lower() != expected.lower():
        from repository_identity import same_repository
        if not (bundle_repository_alias and expected.lower() == 'sunburn-golden/kix-protocol' and
                same_repository(expected,target)):
            raise unsupported('프로그램이 다른 레포를 가리킵니다: ' + target)
    if not isinstance(data['program'], str) or not IDENTIFIER.fullmatch(data['program']):
        raise unsupported('프로그램 ID 형식을 확인해 주세요.')
    if any(marker in data['approval_pointer'].upper() for marker in
           ('PENDING', 'DO_NOT_DISPATCH', 'NOT_APPROVED', 'REJECTED')):
        raise AppError('PROGRAM_APPROVAL_PENDING',
                       '프로그램의 범위·시작 결정이 대기 중입니다: ' + data['approval_pointer'])
    nodes = data['nodes']
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 256:
        raise unsupported('로컬 노드는 1개 이상 256개 이하여야 합니다.')
    ids = []
    for node in nodes:
        if not isinstance(node, dict):
            raise unsupported('노드 형식을 확인해 주세요.')
        if 'depends_on_external' in node:
            raise unsupported('외부 레포 의존성은 아직 지원하지 않습니다. 대기 카탈로그에 보존해 주세요.')
        if set(node) - (NODE_REQUIRED | NODE_OPTIONAL) or not NODE_REQUIRED <= set(node):
            raise unsupported('지원하지 않는 노드 필드가 있습니다.')
        key = node['id']
        if not isinstance(key, str) or not IDENTIFIER.fullmatch(key):
            raise unsupported('노드 ID 형식을 확인해 주세요.')
        try:
            text(node['title'], 'title', 160)
            text(node['spec'], 'spec')
        except AppError as exc:
            raise unsupported('노드 ' + key + '의 제목과 원래 명세가 필요합니다.') from exc
        for field, allowed in (('audit_floor', ('A0', 'A1', 'A2', 'A3')),
                               ('astra_gate', ('NONE', 'MILESTONE', 'ARCHITECTURE', 'RELEASE')),
                               ('deliverable_mode', ('PR',))):
            if field in node and node[field] not in allowed:
                raise unsupported('노드 ' + key + '의 ' + field + '를 확인해 주세요.')
        for flag in ('user_merge', 'astra_auto_merge'):
            if flag in node and type(node[flag]) is not bool:
                raise unsupported('노드 ' + key + '의 ' + flag + '는 boolean이어야 합니다.')
        if node.get('astra_auto_merge') is True and (node.get('user_merge') is True or
                                                     node.get('astra_gate') == 'RELEASE'):
            raise unsupported('노드 ' + key + '의 사용자 병합·릴리스 예약과 자동 병합 설정이 충돌합니다.')
        ids.append(key)
    if len({key.lower() for key in ids}) != len(ids):
        raise unsupported('노드 ID는 대소문자 차이만으로 구분할 수 없습니다.')
    graph = {}
    known = set(ids)
    for node in nodes:
        deps = node.get('depends_on', [])
        if (not isinstance(deps, list) or any(not isinstance(dep, str) or dep not in known or
                                            dep == node['id'] for dep in deps) or
                len(deps) != len(set(deps))):
            raise unsupported('노드 ' + node['id'] + '의 로컬 의존성을 확인해 주세요.')
        graph[node['id']] = set(deps)
    visited = set()
    while graph:
        ready = [key for key, deps in graph.items() if deps <= visited]
        if not ready:
            raise unsupported('프로그램의 로컬 의존성에 순환이 있습니다.')
        for key in ready:
            visited.add(key)
            del graph[key]
    return {'path': PATH, 'blob': blob, 'program': data['program'],
            'repository': expected if bundle_repository_alias else data['repository'],
            **({'source_repository':data['repository']} if bundle_repository_alias else {}), 'approval_pointer': data['approval_pointer'],
            'authoritative_doc_pointers': data['authoritative_doc_pointers'],
            'nodes': nodes, 'node_ids': ids, 'count': len(nodes)}


def bind_specs(plan, scope):
    """Attach source-owned specs locally instead of asking a model to copy them.

    Node omissions and dependency changes are still rejected by validate_coverage.
    Implementation notes never replace the pinned original requirement.
    """
    if scope is None: return plan
    result = copy.deepcopy(plan)
    specs = {node['id']: node['spec'] for node in scope['nodes']}
    for task in result['tasks']:
        spec = specs.get(task['id'])
        if spec is not None and spec not in task['instructions']:
            task['instructions'] = spec + '\n\nImplementation notes (subject to the original scope):\n' + task['instructions']
    return result


def validate_coverage(plan, scope):
    """A planner may order nodes but cannot omit, dilute or weaken their DAG."""
    if scope is None:
        return plan
    def fail(message):
        raise AppError('PROGRAM_COVERAGE_REQUIRED',
                       '고정된 프로그램 범위에 맞게 계획을 수정하세요. ' + message)
    if not isinstance(plan, dict) or not isinstance(plan.get('sources'), list) or PATH not in plan['sources']:
        fail('sources에 ' + PATH + '를 포함해야 합니다.')
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or any(not isinstance(task, dict) or
                                         not isinstance(task.get('id'), str) for task in tasks):
        fail('원래 노드 ID를 유지한 tasks가 필요합니다.')
    ids = [task['id'] for task in tasks]
    expected = set(scope['node_ids'])
    if len(ids) != len(set(ids)) or set(ids) != expected:
        missing = sorted(expected - set(ids))
        extra = sorted(set(ids) - expected)
        fail('원래 노드를 빠짐없이 한 번씩 유지하세요. 누락: ' + ', '.join(missing) +
             '; 추가: ' + ', '.join(extra))
    by_id = {task['id']: task for task in tasks}
    for node in scope['nodes']:
        task = by_id[node['id']]
        instructions = task.get('instructions')
        if not isinstance(instructions, str) or node['spec'] not in instructions:
            fail('노드 ' + node['id'] + '의 원래 spec 전체를 instructions에 그대로 포함하세요.')
        deps = task.get('depends_on')
        if (not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps) or
                len(deps) != len(set(deps)) or set(deps) != set(node.get('depends_on', []))):
            fail('노드 ' + node['id'] + '의 depends_on을 원래 로컬 의존성과 동일하게 유지하세요.')
    return plan
