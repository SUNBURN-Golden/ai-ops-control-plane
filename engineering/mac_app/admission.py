"""Conservative legacy routing, not a protected-host admission implementation.

A label is not an owner. Canonical task/owner projections route work back to
host admission even when closed or terminal; they never grant local execution.
"""
from __future__ import annotations

import re
import time

from common import AppError, parse_json

MARKERS = re.compile(r'ASTRA_(?:TASK_KEY|CONTROL_RECORD)_V1|^\s*(?:TASK_ID|BUILDER_ID|LAUNCH_STATE)\s*:', re.MULTILINE)


def host_required(reason, **evidence):
    return {'mode': 'host_required', 'reason': reason, 'host_authority': 'unobserved', **evidence}


def pages(execute, path, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise AppError('ADMISSION_OBSERVATION_UNRESOLVED')
    value = parse_json(execute(['gh', 'api', '--method', 'GET', '--paginate', '--slurp', path], timeout=remaining))
    if (not isinstance(value, list) or not value or any(not isinstance(page, list) for page in value)
            or sum(len(page) for page in value) > 4096):
        raise AppError('ADMISSION_OBSERVATION_UNRESOLVED', '작업 등록 관측이 불완전합니다. 실행 전에 GitHub 읽기 결과를 확인해야 합니다.')
    return [row for page in value for row in page]


def observe(job, managed, execute):
    prior = job.get('admission') or {}
    if prior.get('mode') == 'host_required': return dict(prior)
    scope = job.get('program_scope')
    if scope:
        return host_required('canonical_program', program=scope['program'], blob=scope['blob'])
    if managed: return host_required('registered_project')
    repo = job['repository']
    registrations, historical, seen, threads = 0, 0, set(), 0
    deadline = time.monotonic() + 60
    for issue in pages(execute, 'repos/' + repo + '/issues?state=all&labels=aiops-task&per_page=100', deadline):
        if not isinstance(issue, dict): raise AppError('ADMISSION_OBSERVATION_UNRESOLVED')
        if 'pull_request' in issue: continue
        number, body, count = issue.get('number'), issue.get('body'), issue.get('comments')
        if (type(number) is not int or number < 1 or number in seen or issue.get('state') not in ('open', 'closed')
                or (body is not None and not isinstance(body, str)) or type(count) is not int or not 0 <= count <= 4096):
            raise AppError('ADMISSION_OBSERVATION_UNRESOLVED')
        seen.add(number)
        texts = [body or '']
        if count:
            threads += 1
            if threads > 32: raise AppError('ADMISSION_OBSERVATION_UNRESOLVED')
            comments = pages(execute, 'repos/' + repo + '/issues/' + str(number) + '/comments?per_page=100', deadline)
            if len(comments) < count: raise AppError('ADMISSION_OBSERVATION_UNRESOLVED')
            comment_ids = set()
            for comment in comments:
                if (not isinstance(comment, dict) or type(comment.get('id')) is not int or comment['id'] < 1
                        or comment['id'] in comment_ids or not isinstance(comment.get('body'), str)):
                    raise AppError('ADMISSION_OBSERVATION_UNRESOLVED')
                comment_ids.add(comment['id']); texts.append(comment['body'])
        declarations = '\n'.join(texts)
        if MARKERS.search(declarations):
            classification = ('owner_projection' if re.search(r'^\s*BUILDER_ID\s*:', declarations, re.MULTILINE)
                              else 'launch_projection' if re.search(r'^\s*LAUNCH_STATE\s*:', declarations, re.MULTILINE)
                              else 'canonical_task_projection')
            return host_required('canonical_issue', issue=number, issue_state=issue['state'], classification=classification)
        registrations += 1
        historical += issue['state'] == 'closed'
    # This is ordinary native work, not evidence that a protected host is idle.
    if time.monotonic() > deadline: raise AppError('ADMISSION_OBSERVATION_UNRESOLVED')
    return {'mode': 'native', 'registration_count': registrations, 'historical_registration_count': historical}


def require_native(observation):
    if observation['mode'] != 'host_required': return
    reason = observation['reason']
    context = ('원래 프로그램 ' + observation['program'] if reason == 'canonical_program'
               else '기존 canonical 작업 #' + str(observation['issue']) if reason == 'canonical_issue'
               else '기존 보호 호스트 관리 범위')
    raise AppError('HOST_ADMISSION_REQUIRED', context + '의 실행 승인을 확인할 수 없습니다. '
                   '격리된 준비와 기록 보존은 완료할 수 있지만, 모델 실행에는 보호 호스트의 canonical task·원래 계획·의존성·기존 owner lane 확인이 필요합니다. '
                   '이 앱에는 해당 승인/이관 경로가 아직 없습니다. 이슈 종료나 과거 기록은 소유권 해제 증거가 아닙니다.')
