"""Mac audit request context, never an audit result or execution authority.

The fixed protected producer does not yet bind MAC_LOCAL tasks. Keep its
absence explicit; a request digest, model text or GitHub comment is no receipt.
"""
from __future__ import annotations

import copy
import re

from common import AppError, digest


def require(value):
    if not value:
        raise AppError('MAC_HOST_AUDIT_SCOPE_UNVERIFIED')


def audit_requirement(job, scope):
    lineage = job.get('native_lineage')
    require(isinstance(lineage, dict) and isinstance(lineage.get('binding'), dict))
    bound = lineage['binding']
    require(isinstance(scope, dict) and scope.get('blob') == bound.get('plan_blob') and
            scope.get('program') == bound.get('program') and
            str(scope.get('repository', '')).lower() == str(bound.get('repository', '')).lower() and
            str(job.get('repository', '')).lower() == str(bound.get('repository', '')).lower() and
            job.get('base_sha') == bound.get('plan_commit'))
    require(all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value)
                for value in (job.get('head'), bound.get('plan_commit'), bound.get('plan_blob'))))
    require(isinstance(bound.get('task_id'), str) and bound['task_id'] and
            isinstance(bound.get('task_revision'), str) and
            re.fullmatch(r'[0-9a-f]{64}', bound['task_revision']))
    nodes = scope.get('nodes')
    require(isinstance(nodes, list))
    matches = [node for node in nodes if isinstance(node, dict) and node.get('id') == bound.get('node')]
    require(len(matches) == 1)
    node = matches[0]
    floor, gate = node.get('audit_floor', 'A1'), node.get('astra_gate', 'NONE')
    require(floor in ('A0', 'A1', 'A2', 'A3') and gate in ('NONE', 'MILESTONE', 'ARCHITECTURE', 'RELEASE'))
    if floor == 'A3' and gate != 'RELEASE':
        gate = 'ARCHITECTURE'
    required = floor == 'A3' or gate != 'NONE'
    request = {
        'repository': bound['repository'], 'task_id': bound['task_id'],
        'task_revision': bound['task_revision'], 'canonical_binding': copy.deepcopy(bound),
        'program': bound['program'], 'node': bound['node'],
        'plan_commit': bound['plan_commit'], 'plan_blob': bound['plan_blob'],
        'job': job['id'], 'native_request_id': lineage['request_id'],
        'native_attempt_id': lineage['attempt_id'], 'head': job['head'],
        'pr_url': job.get('pr_url'), 'gate': gate, 'requested_depth': floor,
        'approval_pointer': scope.get('approval_pointer'),
        'auditor_identity': 'ASTRA_FABLE', 'user_only_merge': True,
    }
    return {
        'node': bound['node'], 'head': job['head'], 'audit_floor': floor, 'astra_gate': gate,
        'required': required, 'source': scope['path'], 'plan_commit': bound['plan_commit'],
        'plan_blob': bound['plan_blob'],
        'authority': 'engineering/AGENTS.md sections 8 and 14; pinned program node',
        'audit_receipt': None, 'request_binding': request, 'request_sha256': digest(request),
        'receipt_support': 'PROTECTED_MAC_CONNECTOR_REQUIRED' if required else 'NOT_REQUIRED',
    }
