#!/usr/bin/python3 -I
"""Whole-host install and recovery pack for the Grok computer (program mode).

Brings a reset host to the state the control plane's runtime expects: accounts,
protected directories, the host admission helper, the four builder lanes
(wrapper, adapter, supervisor), the boundary hook, the runner launch wrapper and
the ordinary program sudoers; and preserves the host ledger, host policy and
(opt-in, encrypted) lane logins so the next reset does not lose them.

It adds no daemon, timer or boot hook. Every command is an operator command.
It never runs `init`, never overwrites a file whose bytes differ (that is
drift), never rebuilds an empty ledger and never starts a model or a builder.
Shared primitives (protected-path checks, atomic writes, the private-repository
compare-and-swap) come from control_plane_recover, which the audit-host recovery
already installs and pins.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

CONFIG = Path('/etc/aiops/hostpack.json')
MANIFEST_INSTALLED = Path('/etc/aiops/hostpack-manifest.json')
RECOVER_MODULE = Path('/opt/aiops/lib/control_plane_recover.py')
RECOVERY_MANIFEST = Path('/etc/aiops/recovery-manifest.json')
LOCK = Path('/etc/aiops/hostpack.lock')
BINDING = Path('/etc/aiops/hostpack-binding.json')
RECEIPT = Path('/etc/aiops/hostpack-install.json')
STATE_FORMAT = 'AIOPS_HOSTPACK_STATE_V1'
MANIFEST_SCHEMA = 'AIOPS_HOSTPACK_MANIFEST_V1'
CONFIG_SCHEMA = 'AIOPS_HOSTPACK_CONFIG_V1'
SHA256 = re.compile(r'^[a-f0-9]{64}$')
COMMIT = re.compile(r'^[a-f0-9]{40}$')
SAFE_NAME = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')
MAX_LOGIN_BYTES = 32 << 20
MAX_LEDGER_BYTES = 256 << 20
LANES = ('DEVIN', 'GROK_BUILD', 'GLM', 'CURSOR')
REPOSITORIES = ('BeautifulMind-JT/kix-protocol', 'BeautifulMind-JT/ZARI',
                'BeautifulMind-JT/film-unit-mv-studio', 'BeautifulMind-JT/maeum-gyeol',
                'BeautifulMind-JT/kix-commerce-apps')

# lane -> (unix account, wrapper, adapter, supervisor, repo adapter dir, login paths under HOME).
# Account names are the adapters' own LANE_USER constants; login paths are the CLI state
# directories each provider keeps under HOME (to be confirmed per provider on the live host;
# a missing path is simply not captured).
LANE_SPECS = {
    'DEVIN': {'account': 'astra-builder-devin', 'wrapper': 'astra-builder-devin',
              'adapter': 'astra-devin-adapter', 'supervisor': 'astra-devin-supervisor',
              'source': 'adapters/imported', 'logins': ('.config/devin', '.local/share/devin', '.devin')},
    'GROK_BUILD': {'account': 'astra-builder-grokbuild', 'wrapper': 'astra-builder-grok-build',
                   'adapter': 'astra-grok-adapter', 'supervisor': 'astra-grok-supervisor',
                   'source': 'adapters/imported', 'logins': ('.grok',)},
    'GLM': {'account': 'astra-builder-glm', 'wrapper': 'astra-builder-glm',
            'adapter': 'astra-glm-adapter', 'supervisor': 'astra-glm-supervisor',
            'source': 'adapters/imported', 'logins': ('.config/opencode', '.local/share/opencode')},
    'CURSOR': {'account': 'astra-builder-cursor', 'wrapper': 'astra-builder-cursor',
               'adapter': 'astra-cursor-adapter', 'supervisor': 'astra-cursor-supervisor',
               'source': 'adapters/cursor', 'logins': ('.cursor', '.config/cursor')},
}
CONTROL_ACCOUNT = 'astra-control'
DEFAULT_RUNNER = 'astra-runner'

SUDOERS_SOURCE = '.github/control-plane/sudoers-aiops-program.example'
SUDOERS_DESTINATION = '/etc/sudoers.d/aiops-program'
LEDGER_DIR = '/var/lib/astra/control'
LEDGER_DB = 'admission.sqlite3'
HOST_POLICY = '/etc/astra/control-plane-host.json'
LANE_CONFIG_DIR = '/etc/astra'


class PackError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail  # Bounded, secret-free: names and counts only.
        super().__init__(code)


def fail(code, detail=None):
    raise PackError(code, detail)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(data, code='MANIFEST_REJECTED'):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                fail(code)
            out[key] = value
        return out
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: fail(code))
    except (ValueError, UnicodeError):
        fail(code)


def result(status, **fields):
    return dict({'status': status}, **fields)


# --------------------------------------------------------------------------- manifest

def file_specs():
    """The complete, fixed list of host-installed artifacts: (repo path, destination, mode, group, category)."""
    specs = [('scripts/control_plane_host.py', '/opt/astra/bin/astra-host-control', 0o755, 'root', 'host'),
             ('scripts/control_plane_boundary.py', '/opt/astra/boundary/control_plane_boundary.py', 0o644, CONTROL_ACCOUNT, 'boundary'),
             ('scripts/control_plane_boundary_hook.sh', '/opt/astra/boundary/control_plane_boundary_hook.sh', 0o755, CONTROL_ACCOUNT, 'boundary'),
             ('hostpack/astra-runner-launch', '/opt/astra/bin/astra-runner-launch', 0o755, 'root', 'runner')]
    for lane in LANES:
        spec = LANE_SPECS[lane]
        specs.append((f"{spec['source']}/{spec['wrapper']}", f"/opt/astra/bin/{spec['wrapper']}", 0o755, 'root', 'lane:' + lane))
        specs.append((f"{spec['source']}/{spec['adapter']}", f"/opt/astra/libexec/{spec['adapter']}", 0o755, 'root', 'lane:' + lane))
        specs.append((f"{spec['source']}/{spec['supervisor']}", f"/opt/astra/libexec/{spec['supervisor']}", 0o755, 'root', 'lane:' + lane))
    return specs


def account_specs(runner_user=DEFAULT_RUNNER):
    accounts = [{'name': CONTROL_ACCOUNT, 'role': 'control', 'home': '/var/lib/astra-control-home'},
                {'name': runner_user, 'role': 'runner', 'home': '/var/lib/astra-runner'}]
    for lane in LANES:
        accounts.append({'name': LANE_SPECS[lane]['account'], 'role': 'lane:' + lane,
                         'home': '/var/lib/astra-lanes/' + LANE_SPECS[lane]['account']})
    return accounts


def build_manifest(root):
    """root is the repository's engineering/ directory. Digests are of the exact bytes."""
    files = []
    for source, destination, mode, group, category in file_specs():
        data = (Path(root) / source).read_bytes()
        files.append({'cache_path': 'engineering/' + source, 'destination': destination, 'mode': mode,
                      'group': group, 'sha256': sha(data), 'category': category})
    sudoers = (Path(root) / SUDOERS_SOURCE).read_bytes()
    return {'schema': MANIFEST_SCHEMA, 'files': files,
            'sudoers': {'cache_path': 'engineering/' + SUDOERS_SOURCE, 'destination': SUDOERS_DESTINATION,
                        'mode': 0o440, 'source_sha256': sha(sudoers), 'placeholder': 'RUNNER_USER'},
            'accounts': account_specs(), 'lanes': {lane: {k: v for k, v in LANE_SPECS[lane].items() if k != 'source'}
                                                   for lane in LANES}}


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get('schema') != MANIFEST_SCHEMA:
        fail('MANIFEST_REJECTED')
    seen = set()
    expected = {destination for _, destination, _, _, _ in file_specs()}
    for item in manifest.get('files', []):
        path = item.get('destination', '')
        if (not path.startswith(('/opt/astra/', '/etc/astra/')) or '..' in Path(path).parts or path in seen
                or not SHA256.fullmatch(item.get('sha256', '')) or item.get('mode') not in (0o644, 0o755)
                or item.get('group') not in ('root', CONTROL_ACCOUNT)):
            fail('MANIFEST_REJECTED')
        seen.add(path)
    if seen != expected:
        fail('MANIFEST_REJECTED')
    sudoers = manifest.get('sudoers') or {}
    if (sudoers.get('destination') != SUDOERS_DESTINATION or sudoers.get('mode') != 0o440
            or not SHA256.fullmatch(sudoers.get('source_sha256', '')) or sudoers.get('placeholder') != 'RUNNER_USER'):
        fail('MANIFEST_REJECTED')
    return manifest


def validate_config(cfg, manifest_sha256=None):
    if not isinstance(cfg, dict) or cfg.get('schema') != CONFIG_SCHEMA:
        fail('CONFIG_REJECTED')
    if not COMMIT.fullmatch(str(cfg.get('source_commit', ''))):
        fail('CONFIG_REJECTED', 'source_commit')
    if manifest_sha256 is not None and cfg.get('manifest_sha256') != manifest_sha256:
        fail('MANIFEST_REJECTED', 'manifest digest differs from config')
    runner = cfg.get('runner_user', DEFAULT_RUNNER)
    if not SAFE_NAME.fullmatch(runner) or runner in (CONTROL_ACCOUNT, 'root') or runner in {s['account'] for s in LANE_SPECS.values()}:
        fail('CONFIG_REJECTED', 'runner_user')
    builders = cfg.get('enabled_builders', [])
    if not isinstance(builders, list) or not builders or any(b not in LANES for b in builders) or len(set(builders)) != len(builders):
        fail('CONFIG_REJECTED', 'enabled_builders')
    repos = cfg.get('allowed_repositories', [])
    if not isinstance(repos, list) or not repos or any(r not in REPOSITORIES for r in repos):
        fail('CONFIG_REJECTED', 'allowed_repositories')
    if not isinstance(cfg.get('max_active_sessions'), int) or not 1 <= cfg['max_active_sessions'] <= len(builders):
        fail('CONFIG_REJECTED', 'max_active_sessions')
    limit = cfg.get('max_launches_per_24h')
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        fail('CONFIG_REJECTED', 'max_launches_per_24h')
    logins = cfg.get('preserve_logins', [])
    if not isinstance(logins, list) or any(b not in LANES for b in logins):
        fail('CONFIG_REJECTED', 'preserve_logins')
    if cfg.get('state_repository') != 'BeautifulMind-JT/aiops-state' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,60}', cfg.get('state_branch', '')):
        fail('CONFIG_REJECTED', 'state')
    return cfg


# --------------------------------------------------------------------------- host policy

def render_host_policy(cfg, uids):
    """Policy for control-plane-host.json from facts only: real numeric UIDs and the operator's config."""
    lanes = {lane: uids[LANE_SPECS[lane]['account']] for lane in LANES}
    return {'control_uid': uids[CONTROL_ACCOUNT], 'runner_uid': uids[cfg.get('runner_user', DEFAULT_RUNNER)],
            'builder_uids': lanes,
            'allowed_repositories': list(cfg['allowed_repositories']),
            'enabled_builders': list(cfg['enabled_builders']),
            'max_active_sessions': cfg['max_active_sessions'],
            'max_launches_per_24h': cfg.get('max_launches_per_24h'),
            'ledger_path': LEDGER_DIR + '/' + LEDGER_DB,
            'wrapper_paths': {lane: '/opt/astra/bin/' + LANE_SPECS[lane]['wrapper'] for lane in LANES},
            'boundary_evidence_pointer': cfg.get('boundary_evidence_pointer', 'PENDING'),
            'control_repository': 'BeautifulMind-JT/ai-ops-control-plane',
            'control_source_sha': cfg['source_commit'],
            'control_runtime_enabled': False}


def render_sudoers(source_bytes, runner_user):
    text = source_bytes.decode()
    if 'RUNNER_USER' not in text or not SAFE_NAME.fullmatch(runner_user):
        fail('MANIFEST_REJECTED', 'sudoers placeholder')
    rendered = re.sub(r'(?m)^RUNNER_USER ', runner_user + ' ', text)
    if re.search(r'(?m)^RUNNER_USER\b', rendered):
        fail('MANIFEST_REJECTED', 'sudoers placeholder left')
    return rendered.encode()


# --------------------------------------------------------------------------- crypto and state

def derive(password, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    if not password:
        fail('STATE_PASSWORD_REQUIRED')
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def encrypt(payload, key, salt, version, previous, source_commit, chunk=4 << 20):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aad = {'format': STATE_FORMAT, 'source': source_commit, 'version': version, 'previous_commit': previous,
           'salt': base64.b64encode(salt).decode()}
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key).encrypt(nonce, canonical(payload), canonical(aad))
    envelope = dict(aad, nonce=base64.b64encode(nonce).decode(),
                    chunks=[sha(cipher[i:i + chunk]) for i in range(0, len(cipher), chunk)])
    return envelope, cipher


def decrypt(envelope, cipher, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    try:
        aad = {k: envelope[k] for k in ('format', 'source', 'version', 'previous_commit', 'salt')}
        if aad['format'] != STATE_FORMAT or type(aad['version']) is not int or aad['version'] < 1:
            fail('STATE_CORRUPT')
        raw = AESGCM(key).decrypt(base64.b64decode(envelope['nonce'], validate=True), cipher, canonical(aad))
        return strict_json(raw, 'STATE_CORRUPT')
    except InvalidTag:
        fail('STATE_PASSWORD_OR_INTEGRITY_ERROR')
    except (KeyError, ValueError, TypeError):
        fail('STATE_CORRUPT')


def safe_member(name):
    parts = Path(name).parts
    if (not isinstance(name, str) or not parts or Path(name).is_absolute() or str(Path(*parts)) != name
            or any(p in ('', '.', '..') for p in parts)):
        fail('STATE_CORRUPT')
    return Path(*parts)


def read_tree_files(base, relative_roots, *, limit, label):
    """Regular files only, no links or special files. Returns [(relative path, mode, bytes)]."""
    out, total = [], 0
    for rel in relative_roots:
        top = Path(base) / rel
        if not os.path.lexists(top):
            continue
        for current, dirs, names in os.walk(top, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                path = Path(current) / name
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode):
                    fail('STATE_UNSUPPORTED_FILE', f'{label}: {path.relative_to(base)}')
                total += info.st_size
                if total > limit:
                    fail('STATE_TOO_LARGE', label)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    out.append((str(path.relative_to(base)), stat.S_IMODE(info.st_mode), stream.read()))
            for name in dirs:
                if os.path.islink(Path(current) / name):
                    fail('STATE_UNSUPPORTED_FILE', f'{label}: {(Path(current) / name).relative_to(base)}')
    return out


def sqlite_snapshot(db_path):
    """One consistent copy of the ledger through SQLite's own backup API (never a raw copy of a live WAL)."""
    source = sqlite3.connect(Path(db_path).as_uri() + '?mode=ro', uri=True, timeout=30)
    try:
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            fail('STATE_CORRUPT', 'ledger quick_check')
        with tempfile.TemporaryDirectory(prefix='hostpack-') as tmp:
            target_path = Path(tmp) / 'snapshot.sqlite3'
            target = sqlite3.connect(str(target_path))
            try:
                source.backup(target)
            finally:
                target.close()
            data = target_path.read_bytes()
    finally:
        source.close()
    if len(data) > MAX_LEDGER_BYTES:
        fail('STATE_TOO_LARGE', 'ledger')
    return data


def collect_payload(prefix, cfg, manifest_sha256):
    """The preserved host state. Credentials of lanes are included only for lanes the operator opted in."""
    prefix = Path(prefix)
    entries = []

    def add(kind, path, mode, data, owner):
        entries.append({'kind': kind, 'path': path, 'mode': mode, 'owner': owner,
                        'bytes': base64.b64encode(data).decode(), 'sha256': sha(data), 'size': len(data)})

    ledger = prefix / LEDGER_DIR.lstrip('/')
    db = ledger / LEDGER_DB
    if os.path.lexists(db):
        add('ledger-db', LEDGER_DB, 0o600, sqlite_snapshot(db), CONTROL_ACCOUNT)
        for name in sorted(os.listdir(ledger)):
            # Everything else the helper keeps there (locks, census files, side files) except the live SQLite trio.
            if name == LEDGER_DB or name.startswith(LEDGER_DB + '-'):
                continue
            path = ledger / name
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                fail('STATE_UNSUPPORTED_FILE', f'ledger: {name}')
            if name.endswith('.lock'):
                continue  # kernel locks do not survive a reset; recreated empty by their owner
            add('ledger-file', name, stat.S_IMODE(info.st_mode), path.read_bytes(), CONTROL_ACCOUNT)
    policy = prefix / HOST_POLICY.lstrip('/')
    if os.path.lexists(policy):
        add('host-policy', 'control-plane-host.json', 0o644, policy.read_bytes(), 'root')
    for lane in LANES:
        lane_config = prefix / LANE_CONFIG_DIR.lstrip('/') / f'{LANE_SPECS[lane]["wrapper"].replace("astra-builder-", "")}-lane.json'
        if os.path.lexists(lane_config):
            add('lane-config', lane_config.name, 0o644, lane_config.read_bytes(), 'root')
    for lane in cfg.get('preserve_logins', []):
        account = LANE_SPECS[lane]['account']
        home = prefix / 'var/lib/astra-lanes' / account
        for rel, mode, data in read_tree_files(home, LANE_SPECS[lane]['logins'], limit=MAX_LOGIN_BYTES, label='login ' + lane):
            add('lane-login', f'{lane}/{rel}', mode & 0o700 | 0o600, data, account)
    return {'format': STATE_FORMAT, 'created': int(time.time()), 'manifest_sha256': manifest_sha256,
            'source_commit': cfg['source_commit'], 'entries': entries}


def validate_payload(payload, manifest_sha256, source_commit):
    if (not isinstance(payload, dict) or payload.get('format') != STATE_FORMAT
            or payload.get('source_commit') != source_commit or not isinstance(payload.get('entries'), list)):
        fail('STATE_CORRUPT')
    seen = set()
    for entry in payload['entries']:
        try:
            data = base64.b64decode(entry['bytes'], validate=True)
            kind, path, mode = entry['kind'], entry['path'], entry['mode']
        except (KeyError, ValueError, TypeError):
            fail('STATE_CORRUPT')
        if (kind not in ('ledger-db', 'ledger-file', 'host-policy', 'lane-config', 'lane-login')
                or sha(data) != entry.get('sha256') or len(data) != entry.get('size') or (kind, path) in seen
                or not isinstance(mode, int) or mode & ~0o777):
            fail('STATE_CORRUPT')
        seen.add((kind, path))
        safe_member(path)
        if kind == 'lane-login' and path.split('/', 1)[0] not in LANES:
            fail('STATE_CORRUPT')
    return payload


def destination_for(entry, prefix):
    prefix = Path(prefix)
    kind, path = entry['kind'], safe_member(entry['path'])
    if kind in ('ledger-db', 'ledger-file'):
        return prefix / LEDGER_DIR.lstrip('/') / path
    if kind == 'host-policy':
        return prefix / HOST_POLICY.lstrip('/')
    if kind == 'lane-config':
        return prefix / LANE_CONFIG_DIR.lstrip('/') / path
    lane, rest = path.parts[0], Path(*path.parts[1:])
    return prefix / 'var/lib/astra-lanes' / LANE_SPECS[lane]['account'] / rest


class Hostpack:
    """Everything under one injectable root so the offline tests never touch the real host."""

    def __init__(self, manifest, cfg, prefix=Path('/'), owner=0, ops=None):
        self.manifest = validate_manifest(manifest)
        self.manifest_sha256 = sha(canonical(manifest))
        self.cfg = validate_config(cfg, self.manifest_sha256)
        self.prefix, self.owner = Path(prefix), owner
        self.runner = cfg.get('runner_user', DEFAULT_RUNNER)
        self.ops = ops or SystemOps()

    def path(self, absolute):
        return self.prefix / absolute.lstrip('/')

    # ---- read-only inspection -------------------------------------------------

    def check_file(self, item):
        path = self.path(item['destination'])
        if not os.path.lexists(path):
            return 'MISSING'
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != item['mode']:
            return 'DRIFT'
        if self.ops.enforce_ownership and (info.st_uid != self.owner or info.st_mode & 0o022):
            return 'DRIFT'
        if sha(path.read_bytes()) != item['sha256']:
            return 'DRIFT'
        return 'OK'

    def accounts(self):
        out = {}
        for item in account_specs(self.runner):
            out[item['name']] = self.ops.lookup_user(item['name'])
        return out

    def verify(self):
        """Read-only. Returns per-component status and the exact next operator steps."""
        components, steps = {}, []
        files = {item['destination']: self.check_file(item) for item in self.manifest['files']}
        components['files'] = {'status': 'OK' if all(v == 'OK' for v in files.values()) else 'HOLD',
                               'missing': sorted(k for k, v in files.items() if v == 'MISSING'),
                               'drift': sorted(k for k, v in files.items() if v == 'DRIFT')}
        if components['files']['drift']:
            steps.append('Differing installed files are drift: stop and report them; nothing is overwritten.')
        elif components['files']['missing']:
            steps.append('aiops-hostpack install')
        users = self.accounts()
        components['accounts'] = {'status': 'OK' if all(users.values()) else 'HOLD',
                                  'missing': sorted(k for k, v in users.items() if not v)}
        policy_path = self.path(HOST_POLICY)
        if not os.path.lexists(policy_path):
            components['host_policy'] = {'status': 'HOLD', 'reason': 'MISSING'}
        else:
            components['host_policy'] = {'status': 'OK' if self.policy_matches(users) else 'HOLD',
                                         'reason': None if self.policy_matches(users) else 'DRIFT'}
        sudoers = self.path(SUDOERS_DESTINATION)
        components['sudoers'] = {'status': 'OK' if os.path.lexists(sudoers) and self.sudoers_bytes() == sudoers.read_bytes() else 'HOLD'}
        ledger = self.path(LEDGER_DIR) / LEDGER_DB
        if os.path.lexists(ledger):
            components['ledger'] = {'status': 'OK' if self.ledger_ok(ledger) else 'HOLD'}
        else:
            components['ledger'] = {'status': 'HOLD', 'reason': 'LEDGER_ABSENT'}
            steps.append('Ledger absent: if a checkpoint exists run aiops-hostpack restore; otherwise the administrator '
                         'initialises it once (never an automatic empty reset).')
        components['lanes'] = {lane: self.lane_state(lane, users) for lane in LANES}
        pending = [lane for lane, v in components['lanes'].items() if v['status'] != 'OK' and lane in self.cfg['enabled_builders']]
        if pending:
            steps.append('Provider login / CLI qualification pending for: ' + ', '.join(pending))
        overall = 'READY_FOR_ACTIVATION_CHECK' if all(
            c['status'] == 'OK' for key, c in components.items() if key != 'lanes') and not pending else 'HOLD'
        return result(overall, components=components, next_steps=steps)

    def lane_state(self, lane, users):
        spec = LANE_SPECS[lane]
        reasons = []
        if not users.get(spec['account']):
            reasons.append('ACCOUNT_MISSING')
        for key in ('wrapper', 'adapter', 'supervisor'):
            where = '/opt/astra/bin/' if key == 'wrapper' else '/opt/astra/libexec/'
            if self.check_file(next(i for i in self.manifest['files'] if i['destination'] == where + spec[key])) != 'OK':
                reasons.append(key.upper() + '_NOT_INSTALLED')
        if lane == 'CURSOR' and not os.path.lexists(self.path(LANE_CONFIG_DIR) / 'cursor-lane.json'):
            reasons.append('CURSOR_LANE_CONFIG_MISSING')
        if lane in self.cfg['enabled_builders'] and not self.ops.lane_logged_in(self, lane):
            reasons.append('LOGIN_REQUIRED')
        return {'status': 'OK' if not reasons else 'HOLD', 'reasons': reasons}

    def policy_matches(self, users):
        try:
            value = strict_json(self.path(HOST_POLICY).read_bytes(), 'CONFIG_REJECTED')
        except (OSError, PackError):
            return False
        if not all(users.values()):
            return False
        expected = render_host_policy(self.cfg, {k: v['uid'] for k, v in users.items()})
        return value == expected

    def sudoers_bytes(self):
        return render_sudoers(self.ops.read_cached(self, self.manifest['sudoers']['cache_path'],
                                                   self.manifest['sudoers']['source_sha256']), self.runner)

    def ledger_ok(self, db):
        try:
            con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10)
            try:
                return con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            finally:
                con.close()
        except sqlite3.Error:
            return False

    # ---- install -----------------------------------------------------------------

    def install(self):
        """Missing components only. Differing bytes stop the whole install before anything is written."""
        for item in self.manifest['files']:
            if self.check_file(item) == 'DRIFT':
                fail('INSTALLATION_DRIFT', item['destination'])
        data = {}
        for item in self.manifest['files']:
            if self.check_file(item) == 'MISSING':
                blob = self.ops.read_cached(self, item['cache_path'], item['sha256'])
                if sha(blob) != item['sha256']:
                    fail('ARTIFACT_HASH_MISMATCH', item['destination'])
                data[item['destination']] = blob
        sudoers_rendered = self.sudoers_bytes()
        sudoers_path = self.path(SUDOERS_DESTINATION)
        if os.path.lexists(sudoers_path) and sudoers_path.read_bytes() != sudoers_rendered:
            fail('INSTALLATION_DRIFT', SUDOERS_DESTINATION)
        atomic = self.ops.atomic
        atomic(self.path('/etc/aiops/hostpack-install.json'), canonical({'complete': False, 'manifest_sha256': self.manifest_sha256}), 0o600, 0, 0)
        users = {}
        for item in account_specs(self.runner):
            users[item['name']] = self.ops.ensure_account(self, item)
        self.make_directories(users)
        for item in self.manifest['files']:
            if item['destination'] in data:
                gid = users[CONTROL_ACCOUNT]['gid'] if item['group'] == CONTROL_ACCOUNT else self.owner
                atomic(self.path(item['destination']), data[item['destination']], item['mode'], self.owner, gid, replace=False)
        policy_path = self.path(HOST_POLICY)
        expected = render_host_policy(self.cfg, {k: v['uid'] for k, v in users.items()})
        if os.path.lexists(policy_path):
            if strict_json(policy_path.read_bytes(), 'CONFIG_REJECTED') != expected:
                fail('INSTALLATION_DRIFT', HOST_POLICY)
        else:
            atomic(policy_path, json.dumps(expected, indent=2, sort_keys=True).encode() + b'\n', 0o644, self.owner, self.owner, replace=False)
        if not os.path.lexists(sudoers_path):
            self.ops.validate_sudoers(self, sudoers_rendered)
            atomic(sudoers_path, sudoers_rendered, 0o440, self.owner, self.owner, replace=False)
        for item in self.manifest['files']:
            if self.check_file(item) != 'OK':
                fail('INSTALLATION_DRIFT', item['destination'])
        atomic(self.path('/etc/aiops/hostpack-install.json'), canonical({'complete': True, 'manifest_sha256': self.manifest_sha256}), 0o600, 0, 0)
        return result('INSTALLED', manifest_sha256=self.manifest_sha256, users=sorted(users))

    def make_directories(self, users):
        control, runner = users[CONTROL_ACCOUNT], users[self.runner]
        plan = [('/opt/astra', 'root', 0o755), ('/opt/astra/bin', 'root', 0o755), ('/opt/astra/libexec', 'root', 0o755),
                ('/opt/astra/boundary', 'root', 0o755), ('/etc/astra', 'root', 0o755), ('/var/lib/astra', 'root', 0o755),
                (LEDGER_DIR, control, 0o700), ('/var/lib/astra/boundary-evidence', runner, 0o700),
                ('/var/lib/astra-lanes', 'root', 0o755), ('/var/cache/aiops-hostpack', 'root', 0o700),
                ('/etc/sudoers.d', 'root', 0o755)]
        for item in account_specs(self.runner):
            plan.append((item['home'], users[item['name']], 0o700))
        for path, owner, mode in plan:
            uid, gid = (self.owner, self.owner) if owner == 'root' else (owner['uid'], owner['gid'])
            target = self.path(path)
            if not os.path.lexists(target):
                target.mkdir(mode=mode, parents=True)
            info = os.lstat(target)
            if not stat.S_ISDIR(info.st_mode):
                fail('INSTALLATION_DRIFT', path)
            if self.ops.enforce_ownership:
                if (info.st_uid, info.st_gid) != (uid, gid) or stat.S_IMODE(info.st_mode) != mode:
                    if os.listdir(target) and path in (LEDGER_DIR,):
                        fail('INSTALLATION_DRIFT', path)  # never re-own an existing ledger
                    os.chown(target, uid, gid)
                    os.chmod(target, mode)
            else:
                os.chmod(target, mode)


# --------------------------------------------------------------------------- operations on the real host

class SystemOps:
    """The only place that touches accounts, the network, sudo and root ownership."""
    enforce_ownership = True

    def lookup_user(self, name):
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            return None
        return {'uid': entry.pw_uid, 'gid': entry.pw_gid, 'home': entry.pw_dir, 'shell': entry.pw_shell}

    def ensure_account(self, pack, item):
        import grp
        existing = self.lookup_user(item['name'])
        if existing is None:
            subprocess.run(['/usr/sbin/useradd', '--system', '--user-group', '--home-dir', item['home'],
                            '--shell', '/usr/sbin/nologin', '--no-create-home', item['name']],
                           check=True, capture_output=True)
            existing = self.lookup_user(item['name'])
        if (existing['uid'] == 0 or existing['gid'] == 0 or existing['home'] != item['home']
                or existing['shell'] != '/usr/sbin/nologin' or grp.getgrgid(existing['gid']).gr_name != item['name']
                or set(os.getgrouplist(item['name'], existing['gid'])) != {existing['gid']}):
            fail('INSTALLATION_DRIFT', 'account ' + item['name'])
        for ranges in ('/etc/subuid', '/etc/subgid'):
            try:
                if re.search(r'(?m)^' + re.escape(item['name']) + ':', Path(ranges).read_text()):
                    fail('INSTALLATION_DRIFT', f'{item["name"]} has {ranges} entries')
            except FileNotFoundError:
                pass
        return existing

    def atomic(self, path, data, mode, uid, gid, replace=True):
        rec = load_recover()
        rec.atomic(path, data, mode, uid, gid, replace=replace)

    def read_cached(self, pack, cache_path, expected_sha):
        """Artifacts come from the pinned source commit; bytes are copied from one descriptor and hashed."""
        cache = pack.path('/var/cache/aiops-hostpack') / cache_path
        rec = load_recover()
        if os.path.lexists(cache):
            data = rec.read_regular(cache, boundary=pack.prefix, mode=0o600)
        else:
            token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
            if not token:
                fail('GITHUB_AUTH_REQUIRED')
            data = fetch_github_file(token, 'BeautifulMind-JT/ai-ops-control-plane', pack.cfg['source_commit'], cache_path)
        if sha(data) != expected_sha:
            fail('ARTIFACT_HASH_MISMATCH', cache_path)
        return data

    def validate_sudoers(self, pack, rendered):
        with tempfile.NamedTemporaryFile(prefix='hostpack-sudoers-', mode='wb', dir='/dev/shm', delete=False) as handle:
            handle.write(rendered)
            name = handle.name
        try:
            os.chmod(name, 0o440)
            done = subprocess.run(['/usr/sbin/visudo', '-cf', name], capture_output=True, timeout=30)
            if done.returncode != 0:
                fail('SUDOERS_REJECTED')
        finally:
            os.unlink(name)

    def lane_logged_in(self, pack, lane):
        """A login is proven only by the lane's own preflight through the host helper; never inferred from files."""
        return False


def fetch_github_file(token, repository, commit, cache_path):
    import urllib.request
    import urllib.error
    if not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', cache_path) or '..' in cache_path:
        fail('MANIFEST_REJECTED')
    request = urllib.request.Request(
        f'https://api.github.com/repos/{repository}/contents/{cache_path}?ref={commit}',
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github.raw'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read(8 << 20)
    except (OSError, urllib.error.URLError):
        fail('ARTIFACT_UNAVAILABLE', cache_path)


def load_recover():
    """control_plane_recover from its protected installed path, only if it is the file the recovery manifest pins."""
    module = sys.modules.get('control_plane_recover')
    if module is not None:
        return module
    for parent in RECOVER_MODULE.parents:
        info = os.lstat(parent)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail('INSTALLATION_DRIFT', 'recover module path')
    raw = RECOVER_MODULE.read_bytes()
    manifest = strict_json(RECOVERY_MANIFEST.read_bytes())
    pinned = {x['destination']: x['sha256'] for x in manifest.get('files', [])}
    if pinned.get(str(RECOVER_MODULE)) != sha(raw):
        fail('INSTALLATION_DRIFT', 'recover module digest')
    spec = importlib.util.spec_from_file_location('control_plane_recover', str(RECOVER_MODULE))
    module = importlib.util.module_from_spec(spec)
    sys.modules['control_plane_recover'] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- checkpoints

class Checkpoints:
    """Encrypted host state on the private aiops-state repository. A stale or damaged head always stops."""

    def __init__(self, backend, pack, binding_path=BINDING):
        self.backend, self.pack, self.binding_path = backend, pack, Path(binding_path)

    def bound(self):
        if not os.path.lexists(self.binding_path):
            return None
        value = strict_json(self.binding_path.read_bytes(), 'STATE_CORRUPT')
        if not COMMIT.fullmatch(str(value.get('commit', ''))):
            fail('STATE_CORRUPT')
        return value['commit']

    def remote(self, key=None, password=None):
        head = self.backend.head(missing=True)
        if head is None:
            return None, None, None
        envelope, cipher = self.backend.load(head)
        if key is None:
            key = derive(password, base64.b64decode(envelope['salt']))
        payload = validate_payload(decrypt(envelope, cipher, key), self.pack.manifest_sha256, self.pack.cfg['source_commit'])
        return head, envelope, payload

    def save(self, password):
        head = self.backend.head(missing=True)
        bound = self.bound()
        if head is not None and bound != head:
            fail('STATE_ROLLBACK_DETECTED' if bound is not None else 'STATE_FRESHNESS_UNVERIFIED')
        if head is None and bound is not None:
            fail('STATE_ROLLBACK_DETECTED')
        version, salt = 1, secrets.token_bytes(16)
        if head is not None:
            envelope, _ = self.backend.load(head)
            version, salt = envelope['version'] + 1, base64.b64decode(envelope['salt'])
        key = derive(password, salt)
        if head is not None:
            self.remote(key=key)  # the password must open the head we are about to extend
        payload = collect_payload(self.pack.prefix, self.pack.cfg, self.pack.manifest_sha256)
        envelope, cipher = encrypt(payload, key, salt, version, head, self.pack.cfg['source_commit'])
        new_head = self.backend.publish(head, envelope, cipher)
        self.pack.ops.atomic(self.binding_path, canonical({'commit': new_head}), 0o600, 0, 0)
        return result('SAVED', commit=new_head, version=version, entries=len(payload['entries']))

    def restore(self, password):
        head, _, payload = self.remote(password=password)
        if head is None:
            fail('STATE_NOT_FOUND')
        bound = self.bound()
        if bound is not None and bound != head:
            # A different head than the one this host last wrote: only newer-by-ancestry is acceptable,
            # which this client cannot prove, so it stops instead of guessing.
            fail('STATE_FRESHNESS_UNVERIFIED')
        written = []
        ledger_entries = [e for e in payload['entries'] if e['kind'] in ('ledger-db', 'ledger-file')]
        ledger_dir = self.pack.path(LEDGER_DIR)
        if ledger_entries and os.path.lexists(ledger_dir / LEDGER_DB):
            fail('LEDGER_EXISTS', 'restore never replaces a live ledger')
        users = self.pack.accounts()
        for entry in payload['entries']:
            target = destination_for(entry, self.pack.prefix)
            if os.path.lexists(target):
                if sha(target.read_bytes()) == entry['sha256']:
                    continue
                fail('RESTORE_CONFLICT', str(target.relative_to(self.pack.prefix)))
            owner = self.owner_ids(entry, users)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.pack.ops.enforce_ownership and entry['kind'] == 'lane-login':
                self.chown_parents(target.parent, self.pack.path('/var/lib/astra-lanes'), owner)
            self.pack.ops.atomic(target, base64.b64decode(entry['bytes']), entry['mode'], owner[0], owner[1], replace=False)
            written.append(entry['path'])
        db = ledger_dir / LEDGER_DB
        if os.path.lexists(db):
            con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10)
            try:
                if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    fail('STATE_CORRUPT', 'restored ledger quick_check')
            finally:
                con.close()
        self.pack.ops.atomic(self.binding_path, canonical({'commit': head}), 0o600, 0, 0)
        return result('RESTORED', commit=head, written=len(written))

    def owner_ids(self, entry, users):
        if entry['owner'] == 'root' or not self.pack.ops.enforce_ownership:
            return (self.pack.owner, self.pack.owner)
        user = users.get(entry['owner'])
        if not user:
            fail('INSTALLATION_DRIFT', 'account ' + entry['owner'])
        return (user['uid'], user['gid'])

    def chown_parents(self, directory, stop, owner):
        directory, stop = Path(directory), Path(stop)
        while directory != stop and directory != directory.parent:
            os.chown(directory, owner[0], owner[1])
            os.chmod(directory, 0o700)
            directory = directory.parent


# --------------------------------------------------------------------------- boundary policy

def render_boundary_policy(example, *, repository, repository_id, owner_id, actor_id, workflow_sha, main_ref='refs/heads/main'):
    """Boundary policy for exactly one main commit (GITHUB_WORKFLOW_SHA moves with every main commit)."""
    if not COMMIT.fullmatch(workflow_sha) or repository != 'BeautifulMind-JT/ai-ops-control-plane':
        fail('BOUNDARY_REJECTED')
    for value in (repository_id, owner_id, actor_id):
        if type(value) is not int or value < 1:
            fail('BOUNDARY_REJECTED')
    rule = {
        'name': 'control-plane-runtime-main',
        'claims': {
            'env:GITHUB_REPOSITORY': repository, 'env:GITHUB_REPOSITORY_ID': str(repository_id),
            'env:GITHUB_REPOSITORY_OWNER_ID': str(owner_id), 'env:GITHUB_REF': main_ref,
            'env:GITHUB_WORKFLOW_REF': f'{repository}/.github/workflows/control-plane-runtime.yml@{main_ref}',
            'env:GITHUB_WORKFLOW_SHA': workflow_sha, 'env:GITHUB_EVENT_NAME': 'workflow_dispatch',
            'env:GITHUB_JOB': 'control', 'env:GITHUB_ACTOR_ID': str(actor_id),
            'event:repository.id': repository_id, 'event:repository.owner.id': owner_id, 'event:sender.id': actor_id}}
    policy = dict(example)
    policy['policy_id'] = 'prod-boundary-' + workflow_sha[:12]
    policy['description'] = 'Rendered by aiops-hostpack for one main commit; re-render after every main commit.'
    policy['allow'] = [rule]
    return policy


def github_ids(token, repository, actor, request=None):
    import urllib.request
    def get(suffix):
        if request is not None:
            return request(suffix)
        req = urllib.request.Request('https://api.github.com' + suffix,
                                     headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return strict_json(response.read(1 << 20), 'BOUNDARY_REJECTED')
    repo = get('/repos/' + repository)
    user = get('/users/' + actor)
    return repo['id'], repo['owner']['id'], user['id']


# --------------------------------------------------------------------------- CLI

@contextmanager
def barrier(path):
    rec = load_recover()
    with rec.barrier(path):
        yield


def load_pack(prefix=Path('/')):
    rec = load_recover()
    manifest_bytes = rec.read_regular(MANIFEST_INSTALLED)
    cfg = strict_json(rec.read_regular(CONFIG), 'CONFIG_REJECTED')
    if sha(manifest_bytes) != cfg.get('manifest_sha256_file', sha(manifest_bytes)):
        fail('MANIFEST_REJECTED')
    return Hostpack(strict_json(manifest_bytes), cfg, prefix)


def hold(error):
    value = {'status': 'HOLD', 'reason': error.code if isinstance(error, PackError) else 'HOSTPACK_ERROR'}
    if isinstance(error, PackError) and error.detail:
        value['detail'] = error.detail
    return value


def github_backend(pack):
    rec = load_recover()
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        fail('GITHUB_AUTH_REQUIRED')
    return rec.GithubState(pack.cfg['state_repository'], pack.cfg['state_branch'], token)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='aiops-hostpack')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('verify', 'install', 'save', 'restore'):
        sub.add_parser(name)
    boundary = sub.add_parser('boundary-render')
    boundary.add_argument('--commit', required=True)
    boundary.add_argument('--actor', default='BeautifulMind-JT')
    sub.add_parser('build-manifest').add_argument('--root', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'build-manifest':
            print(json.dumps(build_manifest(args.root), indent=2, sort_keys=True))
            return 0
        if os.geteuid() != 0:
            fail('ROOT_REQUIRED')
        with barrier(LOCK):
            pack = load_pack()
            if args.command == 'verify':
                out = pack.verify()
            elif args.command == 'install':
                out = pack.install()
            elif args.command == 'boundary-render':
                token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or fail('GITHUB_AUTH_REQUIRED')
                ids = github_ids(token, 'BeautifulMind-JT/ai-ops-control-plane', args.actor)
                example = strict_json(pack.ops.read_cached(pack, 'engineering/.github/control-plane/boundary-policy.example.json',
                                                           pack.manifest.get('boundary_example_sha256', '')) if False else b'{}')
                out = result('RENDERED', policy=render_boundary_policy(
                    example, repository='BeautifulMind-JT/ai-ops-control-plane', repository_id=ids[0], owner_id=ids[1],
                    actor_id=ids[2], workflow_sha=args.commit))
            else:
                cp = Checkpoints(github_backend(pack), pack)
                password = getpass.getpass('상태 암호: ')
                out = cp.save(password) if args.command == 'save' else cp.restore(password)
        print(json.dumps(out, sort_keys=True))
        return 0 if out.get('status') in ('INSTALLED', 'SAVED', 'RESTORED', 'RENDERED', 'READY_FOR_ACTIVATION_CHECK') else 1
    except (PackError, Exception) as error:  # bounded output only: never a traceback with state in it
        code = error if isinstance(error, PackError) else PackError('HOSTPACK_ERROR')
        print(json.dumps(hold(code), sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
