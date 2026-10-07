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
