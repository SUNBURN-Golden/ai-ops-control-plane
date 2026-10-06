"""One bounded User-approved AGENTS §5 checkpoint, never a general override."""
from __future__ import annotations

import hashlib
import re
import stat

from common import AppError
import handoff

PINS = {
    'repository': 'BeautifulMind-JT/kix-protocol', 'node': 'agents-scope-sync',
    'job': '20531604498943e1', 'branch': 'aiops/native-2972272cbeb64071', 'path': 'AGENTS.md',
    'plan_commit': '7481b0e16ce9b903abbffa62249bb91cd9e63cfe',
    'plan_blob': 'ff0f39a8129ca8b8d30818cce35c3d4e588872fc',
    'spec_sha256': 'df27b538c6a1decf216505fe516cd906d31471289d45ca13d316b344b83fc579',
    'before_blob': '5ef2f06dcfe35f3a53ea8e7d021aba8bc38f121e',
    'after_blob': '190bcae4f0c7d60666b942dd7df9e87749a09297',
}
# Verified actual User API body. No caller-supplied answer or fixture grants access.
APPROVAL = {
    'comment_id': 6018278031,
    'body_sha256': '000fa33005152da022795f01ef3ab91b3d4d085a81454b41907f8fd44488e951',
    'body_utf8_bytes': 348,
    'created_at': '2026-10-06T14:18:45Z',
    'updated_at': '2026-10-06T14:18:45Z',
}
ACTOR = {'login': 'BeautifulMind-JT', 'id': 263336091, 'type': 'User'}


def decision_verified():
    pin = APPROVAL
    if not isinstance(pin, dict) or type(pin.get('comment_id')) is not int or not re.fullmatch(
            r'[0-9a-f]{64}', pin.get('body_sha256', '')): return False
    root = 'repos/' + PINS['repository'] + '/issues/comments/' + str(pin['comment_id'])
    comment = handoff.api(root)
    return (isinstance(comment, dict) and comment.get('id') == pin['comment_id'] and
            comment.get('html_url') == 'https://github.com/' + PINS['repository'] +
                '/issues/92#issuecomment-' + str(pin['comment_id']) and
            comment.get('issue_url') == 'https://api.github.com/repos/' + PINS['repository'] + '/issues/92' and
            isinstance(comment.get('user'), dict) and all(comment['user'].get(k) == v for k, v in ACTOR.items()) and
            comment.get('created_at') == pin['created_at'] and comment.get('updated_at') == pin['updated_at'] and
            isinstance(comment.get('body'), str) and
            len(comment['body'].encode('utf-8')) == pin['body_utf8_bytes'] and
            hashlib.sha256(comment['body'].encode('utf-8')).hexdigest() == pin['body_sha256'])


def object_git(checkout, *args):
    """Read actual objects/parents without replacement, graft or graph overrides."""
    from gitops import git
    return git(checkout, '--no-replace-objects', *args, literal_git_objects=True)


def committed_matches(repos, job, head):
    """Verify the actual commit tree, independently of working-tree Git settings."""
    try:
        if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head): return False
        checkout = repos.path(job)
        object_git(checkout, 'merge-base', '--is-ancestor', PINS['plan_commit'], head)
        entry = object_git(checkout, 'ls-tree', '-z', head, '--', PINS['path'])
        expected = '100644 blob ' + PINS['after_blob'] + '\t' + PINS['path'] + '\0'
        names = object_git(checkout, 'diff', PINS['plan_commit'], head, '--name-only', '-z')
        return (entry == expected and {p for p in names.split('\0') if p} == {PINS['path']} and
                repos.head(job) == head and repos.clean(job))
    except (KeyError, TypeError, AttributeError, OSError, AppError):
        return False


@handoff.bounded_api_reads
def approved(repos, job, name, *, publication=False):
    """Re-read the User decision, then recheck all local pins before staging.

    A failed read/changed/deleted decision retains AUTHORITY_EDIT_NEEDS_USER so
    normal checkpoint_retry can reconsume the same private completed builder.
    """
    pin = PINS
    if (name != pin['path'] or job.get('id') != pin['job'] or
            job.get('repository') != pin['repository'] or job.get('branch') != pin['branch'] or
            job.get('state') == 'unknown' or job.get('attempt')): return False
    try:
        bound = job['native_lineage']['binding']
        if (bound.get('authority_kind') != 'MAC_LOCAL' or bound.get('repository') != pin['repository'] or
                (bound.get('program'), bound.get('node')) != ('kix', pin['node']) or
                bound.get('plan_commit') != pin['plan_commit'] or bound.get('plan_blob') != pin['plan_blob'] or
                job.get('base_sha') != pin['plan_commit'] or job['program_scope']['blob'] != pin['plan_blob']): return False
        if not decision_verified(): return False
        if publication and not committed_matches(repos, job, job.get('head')): return False
        # Network time must not create a window for a different local scope/file.
        repos.assert_binding(job); repos.assert_scope(job)
        scope = repos.program_scope(job)
        if not scope or scope != job['program_scope'] or scope['blob'] != pin['plan_blob']: return False
        nodes = [node for node in scope['nodes'] if node['id'] == pin['node']]
        tasks = job['plan']['tasks']
        if len(nodes) != 1 or len(tasks) != 1: return False
        node, task = nodes[0], tasks[0]
        if (node.get('audit_floor') != 'A3' or node.get('astra_gate', 'NONE') != 'NONE' or
                node.get('user_merge') is not True or node.get('astra_auto_merge') is not False or
                task['id'] != node['id'] or task['title'] != node['title'] or task['instructions'] != node['spec'] or
                hashlib.sha256(node['spec'].encode()).hexdigest() != pin['spec_sha256']): return False
        checkout = repos.path(job)
        if object_git(checkout, 'rev-parse', pin['plan_commit'] + ':.aiops/program.json') != pin['plan_blob']: return False
        object_git(checkout, 'merge-base', '--is-ancestor', pin['plan_commit'], 'HEAD')
        target = checkout / name; info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o111 or
                not target.resolve().is_relative_to(checkout.resolve())): return False
        names = (object_git(checkout, 'diff', 'HEAD', '--name-only', '-z') +
                 object_git(checkout, 'ls-files', '--others', '--exclude-standard', '-z') +
                 object_git(checkout, 'diff', pin['plan_commit'], 'HEAD', '--name-only', '-z'))
        if {p for p in names.split('\0') if p} != {pin['path']}: return False
        return (object_git(checkout, 'rev-parse', pin['plan_commit'] + ':' + name) == pin['before_blob'] and
                object_git(checkout, 'hash-object', '--no-filters', '--', name) == pin['after_blob'])
    except (KeyError, TypeError, AttributeError, OSError, AppError):
        return False
