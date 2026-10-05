"""Small, dependency-free contracts shared by the Mac app and its local workers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from provider_catalog import CATALOG

VERSION = '0.3.13'
REPORT_LIMIT = 2 * 1024 * 1024
JOB_RECORD_LIMIT = 16 * 1024 * 1024
JOB_CONTROL_RESERVE = 1024 * 1024
WORKER_REQUEST_LIMIT = 32 * 1024 * 1024
EVENT_RECORD_LIMIT = 64 * 1024
ROLES = ('planner', 'builder', 'reviewer', 'supervisor')
PROVIDERS = tuple(CATALOG)
TERMINAL = ('accepted', 'cancelled')
DEFAULTS = {
    'schema_version': 1,
    'roles': {role: {'provider': 'codex', 'model': ''} for role in ROLES},
    'session_minutes': 90,
    'max_agent_calls': 0,
    'keep_awake': True,
    'publish_pr': True,
}


class AppError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def parse_json(text, limit=4194304):
    if len(text.encode()) > limit:
        raise AppError('TOO_LARGE', '요청이 너무 큽니다.')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise AppError('DUPLICATE_KEY')
            value[key] = item
        return value
    def invalid(_):
        raise AppError('INVALID_NUMBER')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AppError('INVALID_JSON', 'JSON 형식을 확인해 주세요.') from exc


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AppError('PRIVATE_DIRECTORY_REQUIRED')
    return path


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(encoded(value) + '\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def read_json(path, limit=4194304):
    with open(path, encoding='utf-8') as stream:
        return parse_json(stream.read(limit + 1), limit)


def repository(value):
    if isinstance(value, str) and value.startswith('https://github.com/'):
        value = value.removeprefix('https://github.com/').rstrip('/')
        if value.endswith('.git'): value = value[:-4]
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise AppError('INVALID_REPOSITORY', 'GitHub 주소 또는 owner/repository를 입력해 주세요.')
    return value


def validate_profile(config):
    if not isinstance(config, dict) or set(config) != {'provider', 'model'}:
        raise AppError('INVALID_MODEL_PROFILE')
    provider, model = config['provider'], config['model']
    if not isinstance(provider, str) or provider not in PROVIDERS or not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{0,120}', model) or model.startswith('-'):
        raise AppError('INVALID_MODEL', '지원하는 실행 도구와 올바른 모델 ID를 지정해 주세요.')
    if CATALOG[provider]['model_required'] and (not model or model.lower() in ('auto', 'default')):
        raise AppError('EXACT_MODEL_REQUIRED', CATALOG[provider]['name'] + '에서 사용할 정확한 모델 ID를 입력해 주세요.')
    if provider == 'glm' and not re.fullmatch(r'zai-coding-plan/glm-[A-Za-z0-9][A-Za-z0-9_.-]*', model):
        raise AppError('GLM_CODING_PLAN_REQUIRED', 'GLM은 zai-coding-plan/glm-… 모델 ID를 사용합니다. OpenCode의 Z.AI Coding Plan 연결을 확인해 주세요.')
    return config


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise AppError('INVALID_SETTINGS')
    if not isinstance(value['roles'], dict) or set(value['roles']) != set(ROLES):
        raise AppError('INVALID_ROLES')
    for role in ROLES:
        validate_profile(value['roles'][role])
    if type(value['session_minutes']) is not int or not 5 <= value['session_minutes'] <= 720:
        raise AppError('INVALID_SESSION_LIMIT')
    if type(value['max_agent_calls']) is not int or not 0 <= value['max_agent_calls'] <= 100000:
        raise AppError('INVALID_CALL_LIMIT')
    if any(type(value[k]) is not bool for k in ('keep_awake', 'publish_pr')):
        raise AppError('INVALID_BOOLEAN')
    return parse_json(encoded(value))


def text(value, name, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise AppError('INVALID_' + name.upper())
    return value.strip()


def strings(value, name, maximum=100):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise AppError('INVALID_' + name.upper())
    return [text(v, name, 4000) for v in value]


def validate_plan(value):
    if not isinstance(value, dict) or set(value) != {'summary', 'sources', 'tasks'}:
        raise AppError('INVALID_PLAN')
    if len(encoded(value).encode()) > 2 * 1024 * 1024:
        raise AppError('PLAN_TOO_LARGE')
    text(value['summary'], 'summary')
    sources = strings(value['sources'], 'sources')
    if any(Path(p).is_absolute() or '..' in Path(p).parts for p in sources):
        raise AppError('INVALID_SOURCE_PATH')
    tasks = value['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 256:
        raise AppError('INVALID_TASKS')
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {'id', 'title', 'instructions', 'acceptance', 'depends_on'}:
            raise AppError('INVALID_TASK')
        key = task['id']
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', key) or key in seen:
            raise AppError('INVALID_TASK_ID')
        text(task['title'], 'title', 160); text(task['instructions'], 'instructions', 65536)
        strings(task['acceptance'], 'acceptance')
        deps = task['depends_on']
        if not isinstance(deps, list) or any(not isinstance(dep, str) or dep not in seen for dep in deps) or len(deps) != len(set(deps)):
            raise AppError('PLAN_MUST_BE_TOPOLOGICAL')
        seen.add(key)
    return value
