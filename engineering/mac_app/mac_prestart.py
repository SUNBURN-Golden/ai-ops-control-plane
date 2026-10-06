"""One User-confirmed legacy prestart failure, never a signed host receipt.

FAILED_PRESTART has no writer ownership in control_plane_program.writer_rows
and host_observation.assess. GitHub text alone still cannot prove that state:
this narrowly pinned exception also requires the User's terminal confirmation.
The confirmation records a relayed User observation, not a Mac host read.
"""
from __future__ import annotations

import copy
import hashlib
import re

from common import parse_json
from mac_authority import require
import handoff

REPOSITORY = 'BeautifulMind-JT/kix-protocol'
ISSUE = 92
ISSUE_URL = 'https://github.com/' + REPOSITORY + '/issues/92'
ACTOR = {'id': 263336091, 'login': 'BeautifulMind-JT', 'type': 'User'}
SCOPE = {'repository': REPOSITORY, 'task_id': 'KIX-AGENTS-SCOPE-SYNC',
         'task_revision': 'pc85e614245eb-CURSOR', 'launch_request_id': '077849e0e68f521245e7175f',
         'attempt_id': 1, 'builder_id': 'CURSOR'}
MATERIALIZATION = 'd8821ba080eb6c52c818f554'
CONTROL = {'comment_id': 5956874897, 'url': ISSUE_URL + '#issuecomment-5956874897',
           'body_sha256': '867801b42e1644962560c4b6dd230e67f51b8d604e5970effacd00af03315960'}
CONFIRMATION = {'comment_id': 6016190250, 'url': ISSUE_URL + '#issuecomment-6016190250',
                'body_sha256': '4369ebe024b764413ff4a9ca22bff6536a80f0369ddd6232a718af1d28bd1cc1',
                'source_thread_id': '01a0f57c-0df9-72ce-bb48-d1aaf753d004',
                'source_message_id': 'Sentinel_a77166906c0c819184c3400304e90d93'}
MARKER = '<!-- ASTRA_CONTROL_RECORD_V1 -->'
CODE = 'MAC_GENERATION_LINUX_OWNER_UNRESOLVED'


def evidence():
    return {'basis': 'USER_CONFIRMED_FAILED_PRESTART', 'scope': copy.deepcopy(SCOPE),
            'control_projection': copy.deepcopy(CONTROL), 'user_confirmation': copy.deepcopy(CONFIRMATION),
            'actor': copy.deepcopy(ACTOR), 'host_observation_source': 'USER_DIRECT_CHECK_RELAYED',
            'mac_verified_host_read': False, 'signed_host_receipt': False}


def pinned_comment(comment, pin):
    require(isinstance(comment, dict) and comment.get('id') == pin['comment_id'] and
            comment.get('html_url') == pin['url'] and
            comment.get('issue_url') == 'https://api.github.com/repos/' + REPOSITORY + '/issues/92' and
            isinstance(comment.get('user'), dict) and
            all(comment['user'].get(k) == v for k, v in ACTOR.items()) and
            isinstance(comment.get('body'), str) and
            hashlib.sha256(comment['body'].encode()).hexdigest() == pin['body_sha256'], CODE)


@handoff.bounded_api_reads
def verify(repo, task, program, node):
    """Allow only #92's exact projection plus unchanged User confirmation.

    No caller-supplied history/fixture/decision prose enters this predicate.
    Every use re-reads the issue and its complete comment set. Missing,
    conflicting or changed restored history retains the original hold.
    """
    require(all(isinstance(v, str) for v in (program, node, task.get('program'), task.get('node'))) and
            (program.lower(), node.lower()) == (task['program'].lower(), task['node'].lower()) ==
            ('kix', 'agents-scope-sync'), CODE)
    require(isinstance(repo, str) and repo.lower() == REPOSITORY.lower() and task.get('number') == ISSUE and
            task.get('url') == ISSUE_URL and task.get('state') == 'open' and
            task.get('materialization_request_id') == MATERIALIZATION and
            task.get('declared_owners') == ['CURSOR'], CODE)
    root = 'repos/' + REPOSITORY + '/issues/92'
    issue = handoff.api(root)
    require(isinstance(issue, dict) and issue.get('number') == ISSUE and issue.get('state') == 'open' and
            issue.get('html_url') == ISSUE_URL and isinstance(issue.get('body'), str) and
            hashlib.sha256(issue['body'].encode()).hexdigest() == task.get('body_sha256'), CODE)
    body = issue['body']
    require(handoff.TASK_KEY.findall(body) == [('kix', 'agents-scope-sync', MATERIALIZATION)], CODE)
    for name, expected in {'TASK_ID': SCOPE['task_id'], 'TASK_REVISION': SCOPE['task_revision'],
                           'REPO': REPOSITORY, 'BUILDER_ID': 'CURSOR',
                           'CANONICAL_TASK_POINTER': ISSUE_URL}.items():
        require(re.findall(r'^' + name + r':[ \t]*(\S+)[ \t]*$', body, re.MULTILINE) == [expected], CODE)
    # An unresolved state declared on the issue is an additional negative fence.
    require(not re.search(r'^LAUNCH_STATE:', body, re.MULTILINE), CODE)
    pages = handoff.api(root + '/comments?per_page=100', paginate=True)
    require(isinstance(pages, list) and pages and all(isinstance(p, list) for p in pages), CODE)
    comments = [c for p in pages for c in p]
    require(len(comments) <= 4096 and all(isinstance(c, dict) and type(c.get('id')) is int and
            isinstance(c.get('body'), str) for c in comments) and
            len({c['id'] for c in comments}) == len(comments), CODE)
    controls = [c for c in comments if MARKER in c['body']]
    confirmations = [c for c in comments if c['id'] == CONFIRMATION['comment_id']]
    require(len(controls) == len(confirmations) == 1, CODE)
    pinned_comment(controls[0], CONTROL); pinned_comment(confirmations[0], CONFIRMATION)
    match = re.fullmatch(re.escape(MARKER) + r'\s*```json\s*(\{.*\})\s*```\s*',
                         controls[0]['body'], re.DOTALL)
    require(match is not None, CODE)
    record = parse_json(match.group(1))
    require(isinstance(record, dict) and all(record.get(k) == v for k, v in SCOPE.items()) and
            type(record.get('attempt_id')) is int and record.get('schema_version') == 1 and
            record.get('launch_state') == 'FAILED_PRESTART' and
            'owner_lane' in record and record['owner_lane'] is None and
            'owner_session_id' in record and record['owner_session_id'] is None, CODE)
    return evidence()
