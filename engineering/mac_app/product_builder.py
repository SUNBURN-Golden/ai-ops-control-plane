"""Explicit KIX-only future builder selection; never edits a running job."""
import copy

from common import AppError

REPOSITORY = 'sunburn-golden/kix-protocol'
PROFILE = {'provider': 'codex', 'model': 'gpt-6-astra',
           'reasoning_effort': 'high', 'service_tier': 'default'}
# Preserve previously frozen profiles and their requested-value evidence.
FAST_PROFILE = {**PROFILE, 'service_tier': 'fast'}


def validate(value):
    if not isinstance(value, dict) or set(value) != {REPOSITORY} or value[REPOSITORY] not in (PROFILE, FAST_PROFILE):
        raise AppError('KIX_BUILDER_PROFILE_REQUIRED')
    return copy.deepcopy(value)


def resolve(settings, repository):
    result = copy.deepcopy(settings)
    overrides = result.get('product_builders', {})
    if overrides:
        validate(overrides)
        if repository.lower() == REPOSITORY:
            result['roles']['builder'] = copy.deepcopy(overrides[REPOSITORY])
    return result


def options(profile):
    if 'reasoning_effort' not in profile and 'service_tier' not in profile:
        return {}
    if profile not in (PROFILE, FAST_PROFILE):
        raise AppError('KIX_BUILDER_PROFILE_REQUIRED')
    return {'model_reasoning_effort': profile['reasoning_effort'],
            'service_tier': 'priority' if profile['service_tier'] == 'fast' else 'default'}


def evidence(profile):
    return ({'reasoning_effort_requested': profile['reasoning_effort'],
             'service_tier_requested': profile['service_tier']}
            if options(profile) else {})


def matches(profile, receipt):
    return all(receipt.get(k) == v for k, v in evidence(profile).items())
