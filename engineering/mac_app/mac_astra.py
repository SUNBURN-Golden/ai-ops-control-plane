"""Mac audit request context; verified results come from the separate journal."""
from __future__ import annotations

import copy
import re

from common import AppError, digest, repository


def require(value):
    if not value:
        raise AppError('MAC_HOST_AUDIT_SCOPE_UNVERIFIED')


def audit_requirement(job, scope):
    require(isinstance(job, dict) and isinstance(scope, dict))
    lineage = job.get('native_lineage')
    require(isinstance(lineage, dict) and isinstance(lineage.get('binding'), dict))
    bound = lineage['binding']
    try:
        scope_repo = repository(scope.get('repository'))
        bound_repo = repository(bound.get('repository'))
        job_repo = repository(job.get('repository'))
    except AppError as exc:
        raise AppError('MAC_HOST_AUDIT_SCOPE_UNVERIFIED') from exc
    require(scope.get('blob') == bound.get('plan_blob') and
            scope.get('program') == bound.get('program') and
            scope_repo.lower() == bound_repo.lower() == job_repo.lower() and
            job.get('base_sha') == bound.get('plan_commit'))
    require(all(isinstance(value, str) and value.strip()
                for value in (job.get('id'), lineage.get('request_id'), lineage.get('attempt_id'),
                              bound.get('program'), bound.get('node'), scope.get('path'))))
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
        'repository': bound_repo, 'task_id': bound['task_id'],
        'task_revision': bound['task_revision'], 'canonical_binding': copy.deepcopy(bound),
        'program': bound['program'], 'node': bound['node'],
        'plan_commit': bound['plan_commit'], 'plan_blob': bound['plan_blob'],
        'job': job['id'], 'native_request_id': lineage['request_id'],
        'native_attempt_id': lineage['attempt_id'], 'head': job['head'],
        'branch': job.get('branch'), 'writer_sessions': copy.deepcopy(job.get('builder_sessions') or []),
        'review_sha256': digest(job.get('review')),
        'pr_url': job.get('pr_url'), 'gate': gate, 'requested_depth': floor,
        'approval_pointer': scope.get('approval_pointer'),
        'requested_auditor': 'ASTRA_FABLE', 'declared_user_only_merge': True,
    }
    return {
        'node': bound['node'], 'head': job['head'], 'audit_floor': floor, 'astra_gate': gate,
        'required': required, 'source': scope['path'], 'plan_commit': bound['plan_commit'],
        'plan_blob': bound['plan_blob'],
        'authority': 'engineering/AGENTS.md sections 8 and 14; pinned program node',
        'audit_receipt': None, 'request_binding': request, 'request_sha256': digest(request),
        'receipt_support': 'AUTHENTICATED_GITHUB_AUDIT_COMMENT' if required else 'NOT_REQUIRED',
    }
