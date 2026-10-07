"""Exact transferred repository pins; no task/receipt rewrite or admission grant.

Callers use the app's authenticated bounded GET seam. Supplied JSON and redirects
alone are not capabilities. Original canonical subjects keep their own guards.
"""
from __future__ import annotations

from common import AppError

PINS = {
    'SUNBURN-Golden/ai-ops-control-plane': 1373567344,
    'SUNBURN-Golden/kix-protocol': 1365416872,
    'SUNBURN-Golden/kix-commerce-apps': 1388268331,
    'SUNBURN-Golden/ZARI': 1373217962,
    'SUNBURN-Golden/film-unit-mv-studio': 1365377662,
}
OWNER_ID = 338877516


def current_name(repository):
    """Lookup shared constraints; this does not grant old-name admission."""
    for name in PINS:
        old_name = 'BeautifulMind-JT/' + name.split('/', 1)[1]
        if repository.lower() in (name.lower(), old_name.lower()):
            return name
    return repository


def reject_old_admission(repository):
    old_names = {'beautifulmind-jt/' + name.split('/', 1)[1].lower() for name in PINS}
    if repository.lower() in old_names:
        raise AppError('REPOSITORY_IDENTITY_MIGRATION_SUBJECT_REQUIRED')


def current_pin(repository):
    for name, pin in PINS.items():
        if repository.lower() == name.lower():
            return pin
    if repository.lower().startswith('sunburn-golden/'):
        raise AppError('REPOSITORY_IDENTITY_SCOPE_REQUIRED')
    return None


def verify(repository, metadata):
    expected = current_pin(repository)
    if expected is None:
        return metadata
    owner = metadata.get('owner') if isinstance(metadata, dict) else None
    if (not isinstance(metadata, dict) or type(metadata.get('id')) is not int
            or metadata['id'] != expected or metadata.get('full_name') != current_name(repository)
            or metadata.get('fork') is not False or metadata.get('private') is not True
            or metadata.get('archived') is not False or not isinstance(owner, dict)
            or type(owner.get('id')) is not int or owner['id'] != OWNER_ID
            or owner.get('login') != 'SUNBURN-Golden' or owner.get('type') != 'Organization'):
        raise AppError('REPOSITORY_IDENTITY_MISMATCH')
    return metadata


def observe(repository, authenticated_read):
    if current_pin(repository) is None:
        return None
    return verify(repository, authenticated_read('repos/' + current_name(repository)))


COMPAT_DECISION = {
    'comment_id': 6030780072, 'actor_id': 263336091,
    'created_at': '2026-10-07T04:14:01Z',
    'body_sha256': '2ad33e4a17fd2e7000c8c720f43d1f54570a5c0602225fea617a997682fb7e60',
}


def historical(repository):
    return repository.lower().startswith('beautifulmind-jt/') and current_name(repository) in PINS


def authorize_compat(read):
    """Re-read the immutable, specifically scoped User decision; no fixture grant."""
    import hashlib
    import handoff
    if handoff._COMPAT_VERIFIED.get():
        return
    central = 'SUNBURN-Golden/ai-ops-control-plane'
    observe(central, read)
    pin = COMPAT_DECISION
    value = read('repos/' + central + '/issues/comments/' + str(pin['comment_id']))
    actor = value.get('user', {}) if isinstance(value, dict) else {}
    if (not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] != pin['comment_id']
            or type(actor.get('id')) is not int or actor['id'] != pin['actor_id']
            or actor.get('login') != 'BeautifulMind-JT' or actor.get('type') != 'User'
            or value.get('created_at') != value.get('updated_at') or value.get('created_at') != pin['created_at']
            or value.get('html_url') != f'https://github.com/{central}/pull/83#issuecomment-{pin["comment_id"]}'
            or value.get('issue_url') != f'https://api.github.com/repos/{central}/issues/83'
            or not isinstance(value.get('body'), str)
            or hashlib.sha256(value['body'].encode()).hexdigest() != pin['body_sha256']):
        raise AppError('REPOSITORY_URL_COMPAT_DECISION_UNVERIFIED')
    if handoff._API_DEADLINE.get() is not None:
        handoff._COMPAT_VERIFIED.set(True)


def transport(repository, *, metadata=False):
    """Authenticated current endpoint; a lookup never grants fresh admission."""
    import handoff
    name = current_name(repository)
    if name not in PINS:
        current_pin(repository)  # reject an unapproved name in the new owner
        return repository
    if historical(repository):
        authorize_compat(handoff._raw_api)
    observation = observe(name, handoff._raw_api)
    if handoff._API_DEADLINE.get() is not None:
        handoff._IDENTITY_VERIFIED.set(handoff._IDENTITY_VERIFIED.get() | {name})
    return (name, observation) if metadata else name


def same_repository(canonical, observed):
    if not isinstance(observed, str):
        return False
    if canonical.lower() == observed.lower():
        return True
    name = current_name(canonical)
    if name not in PINS or current_name(observed) != name:
        return False
    import handoff
    if name not in handoff._IDENTITY_VERIFIED.get():
        transport(canonical)
    if historical(canonical) or historical(observed):
        authorize_compat(handoff._raw_api)
    return True


def url_matches(canonical, observed):
    """Only namespace changes: host, kind, number, query and fragment stay exact."""
    import re
    if not isinstance(canonical, str) or not isinstance(observed, str):
        return False
    if canonical == observed:
        return True
    pattern = r'(https://(?:github\.com/|api\.github\.com/repos/))([^/#?]+/[^/#?]+)(/[^\s]*)'
    left, right = re.fullmatch(pattern, canonical), re.fullmatch(pattern, observed)
    return bool(left and right and left[1] == right[1] and left[3] == right[3]
                and same_repository(left[2], right[2]))


def canonical_url(repository, url):
    """Derive a canonical pointer from verified transport, without editing API JSON."""
    import re
    match = re.fullmatch(r'(https://(?:github\.com/|api\.github\.com/repos/))([^/#?]+/[^/#?]+)(/[^\s]*)', url or '')
    if not match or not same_repository(repository, match[2]):
        raise AppError('REPOSITORY_IDENTITY_MISMATCH')
    name = current_name(repository)
    canonical = ('BeautifulMind-JT/' + name.split('/', 1)[1]) if historical(repository) else repository
    return match[1] + canonical + match[3]


def repository_object_matches(repository, value):
    if not isinstance(value, dict) or not same_repository(repository, value.get('full_name')):
        return False
    pin = PINS.get(current_name(repository))
    return pin is None or type(value.get('id')) is int and value['id'] == pin
