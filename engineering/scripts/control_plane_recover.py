#!/usr/bin/python3 -I
"""HR-D3 audit-host recovery. No runner/lane resume and no implicit model probe."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, redirect_stdout
import fcntl
import getpass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCE = "a964c0d72285a752cb567c39bfc4a7fb83ff3eae"
CONFIG = Path('/etc/aiops/recovery.json')
STATE = Path('/var/lib/aiops-fable')
BINDING = Path('/etc/aiops/recovery-binding.json')
TOKEN = Path('/etc/aiops/fable-claude-token')
GH_SAVED = Path('/etc/aiops/recovery-github-token')
MAX_STATE = 512 << 20
CHUNK = 4 << 20
OAUTH = re.compile(r'^sk-ant-oat01-[A-Za-z0-9_-]{80,}$')
SHA = re.compile(r'^[a-f0-9]{64}$')
REPO = re.compile(r'^BeautifulMind-JT/[A-Za-z0-9_.-]{1,100}$')


class RecoveryError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail  # Local relative paths and sizes only.
        super().__init__(code)  # Never incorporate provider output, URL or secrets.


def fail(code, detail=None):
    raise RecoveryError(code, detail)


def hold(error):
    code = error.code if isinstance(error, RecoveryError) else 'RECOVERY_ERROR'
    value = {'status': 'HOLD', 'reason': code,
             'command': 'aiops-recover --reissue-token' if code == 'TOKEN_REISSUE_REQUIRED' else None}
    if isinstance(error, RecoveryError) and error.detail:
        value['detail'] = error.detail
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(data):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                fail('STATE_CORRUPT')
            out[k] = v
        return out
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: fail('STATE_CORRUPT'))
    except (ValueError, UnicodeError):
        fail('STATE_CORRUPT')


def safe_relative(value):
    if not isinstance(value, str):
        fail('STATE_CORRUPT')
    p = PurePosixPath(value)
    if (not isinstance(value, str) or p.is_absolute() or str(p) != value
            or not p.parts or any(x in ('', '.', '..') for x in p.parts)):
        fail('STATE_CORRUPT')
    return p


def protected_parent(path, boundary=Path('/'), owner=0):
    path = Path(path).absolute()
    try:
        relative = path.relative_to(boundary.absolute())
    except ValueError:
        fail('INSTALLATION_DRIFT')
    parent = boundary.absolute()
    for name in ('', *relative.parts[:-1]):
        if name:
            parent /= name
        s = os.lstat(parent)
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != owner or s.st_mode & 0o022:
            fail('INSTALLATION_DRIFT')


def read_regular(path, *, limit=MAX_STATE, protected=True, boundary=Path('/'), mode=None, hardlinks=False):
    if protected:
        protected_parent(path, boundary)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        fail('STATE_CORRUPT' if protected else 'INSTALLATION_DRIFT')
    with os.fdopen(fd, 'rb') as stream:
        s = os.fstat(stream.fileno())
        if (not stat.S_ISREG(s.st_mode) or (s.st_nlink != 1 and not hardlinks) or s.st_size > limit
                or (protected and (s.st_uid != 0 or s.st_mode & 0o022))
                or (mode is not None and stat.S_IMODE(s.st_mode) != mode)):
            fail('INSTALLATION_DRIFT')
        data = stream.read(limit + 1)
        if len(data) > limit:
            fail('STATE_TOO_LARGE')
        return data


def atomic(path, data, mode=0o600, uid=0, gid=0, *, replace=True):
    """Caller holds the recovery barrier and has validated protected ancestors."""
    path = Path(path)
    tmp = path.parent / ('.recover-' + secrets.token_hex(12))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchown(f.fileno(), uid, gid)
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path, follow_symlinks=False)
            tmp.unlink()
        d = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        if os.path.lexists(tmp):
            tmp.unlink()


@contextmanager
def barrier(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o077:
            fail('INSTALLATION_DRIFT')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('RECOVERY_BUSY')
        yield
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail('STATE_AUTHORITY_UNAVAILABLE')


class GithubState:
    """Encrypted blobs only. A non-fast-forward child loses the compare-and-swap."""
    def __init__(self, repository, branch, token, request=None):
        if not REPO.fullmatch(repository) or repository.split('/')[1] != 'aiops-state':
            fail('STATE_REPOSITORY_REJECTED')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}', branch) or '..' in branch:
            fail('STATE_REPOSITORY_REJECTED')
        self.repository, self.branch, self.token = repository, branch, token
        self.request_override = request
        repo = self.request('GET', '')
        self.default_branch = repo.get('default_branch', 'main')
        if repo.get('private') is not True:
            fail('STATE_REPOSITORY_NOT_PRIVATE')

    def request(self, method, suffix, data=None):
        if self.request_override:
            return self.request_override(method, suffix, data)
        req = urllib.request.Request('https://api.github.com/repos/' + self.repository + suffix,
                                     data=canonical(data) if data is not None else None,
                                     headers={'Authorization': 'Bearer ' + self.token,
                                              'Accept': 'application/vnd.github+json',
                                              'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
                if response.geturl() != req.full_url:
                    fail('STATE_AUTHORITY_UNAVAILABLE')
                return strict_json(response.read(12 << 20))
        except urllib.error.HTTPError as e:
            if method == 'GET' and '/git/ref/heads/' in suffix and e.code == 404:
                fail('STATE_BRANCH_MISSING')
            if method in ('PATCH', 'POST') and suffix.startswith('/git/ref') and e.code in (409, 422):
                fail('STATE_CAS_CONFLICT')
            fail('STATE_AUTHORITY_UNAVAILABLE')
        except (OSError, ValueError):
            fail('STATE_AUTHORITY_UNAVAILABLE')

    def head(self, *, missing=False):
        try:
            value = self.request('GET', '/git/ref/heads/' + self.branch)
        except RecoveryError as error:
            if error.code != 'STATE_BRANCH_MISSING':
                raise
            if missing:
                return None
            fail('STATE_CORRUPT')
        h = (value.get('object') or {}).get('sha', '')
        if not re.fullmatch(r'[a-f0-9]{40}', h):
            fail('STATE_CORRUPT')
        return h

    def load(self, head):
        commit = self.request('GET', '/git/commits/' + head)
        tree = self.request('GET', '/git/trees/' + commit['tree']['sha'])
        if tree.get('truncated'):
            fail('STATE_CORRUPT')
        blobs = {x['path']: x['sha'] for x in tree['tree'] if x['type'] == 'blob' and x['mode'] == '100644'}
        if 'checkpoint.json' not in blobs:
            fail('STATE_CORRUPT')
        def get_blob(digest):
            v = self.request('GET', '/git/blobs/' + digest)
            if v.get('encoding') != 'base64':
                fail('STATE_CORRUPT')
            try:
                return base64.b64decode(v['content'], validate=False)
            except (ValueError, KeyError):
                fail('STATE_CORRUPT')
        envelope = strict_json(get_blob(blobs['checkpoint.json']))
        chunks = []
        for i, expected in enumerate(envelope.get('chunks', [])):
            key = f'cipher-{i:06d}'
            if key not in blobs:
                fail('STATE_CORRUPT')
            chunk = get_blob(blobs[key])
            if len(chunk) > CHUNK or sha(chunk) != expected:
                fail('STATE_CORRUPT')
            chunks.append(chunk)
        if set(blobs) != {'checkpoint.json', *(f'cipher-{i:06d}' for i in range(len(chunks)))}:
            fail('STATE_CORRUPT')
        cipher = b''.join(chunks)
        if not cipher or len(cipher) > MAX_STATE:
            fail('STATE_CORRUPT')
        return envelope, cipher

    def publish(self, expected, envelope, cipher):
        if self.head(missing=True) != expected:
            fail('STATE_CAS_CONFLICT')
        parent = expected
        if expected is None:
            parent = self.request('GET', '/git/ref/heads/' + self.default_branch).get('object', {}).get('sha')
            if not re.fullmatch('[a-f0-9]{40}', parent or ''):
                fail('STATE_CORRUPT')
        entries = []
        for name, content in [('checkpoint.json', canonical(envelope))] + [
                (f'cipher-{i:06d}', cipher[i * CHUNK:(i + 1) * CHUNK])
                for i in range((len(cipher) + CHUNK - 1) // CHUNK)]:
            b = self.request('POST', '/git/blobs', {'encoding': 'base64', 'content': base64.b64encode(content).decode()})
            entries.append({'path': name, 'mode': '100644', 'type': 'blob', 'sha': b['sha']})
        tree = self.request('POST', '/git/trees', {'tree': entries})
        commit = self.request('POST', '/git/commits', {'message': 'Encrypted audit-host checkpoint',
                              'tree': tree['sha'], 'parents': [parent]})
        # GitHub rejects a competing child because it is not a fast-forward.
        if expected is None:
            self.request('POST', '/git/refs', {'ref': 'refs/heads/' + self.branch, 'sha': commit['sha']})
        else:
            self.request('PATCH', '/git/refs/heads/' + self.branch, {'sha': commit['sha'], 'force': False})
        if self.head() != commit['sha']:
            fail('STATE_CAS_CONFLICT')
        return commit['sha']


def derive(password, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    if not password:
        fail('STATE_PASSWORD_REQUIRED')
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def encrypt(payload, key, salt, version, previous):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aad = {'format': 'AIOPS_AUDIT_CHECKPOINT_V1', 'source': SOURCE,
           'version': version, 'previous_commit': previous,
           'salt': base64.b64encode(salt).decode()}
    nonce = secrets.token_bytes(12)
    raw = canonical(payload)
    cipher = AESGCM(key).encrypt(nonce, raw, canonical(aad))
    if len(cipher) > MAX_STATE:
        fail('STATE_TOO_LARGE')
    envelope = dict(aad, nonce=base64.b64encode(nonce).decode(),
                    chunks=[sha(cipher[i:i + CHUNK]) for i in range(0, len(cipher), CHUNK)])
    return envelope, cipher


def decrypt(envelope, cipher, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    try:
        aad = {k: envelope[k] for k in ('format', 'source', 'version', 'previous_commit', 'salt')}
        if aad['format'] != 'AIOPS_AUDIT_CHECKPOINT_V1' or aad['source'] != SOURCE or type(aad['version']) is not int or aad['version'] < 1:
            fail('STATE_CORRUPT')
        raw = AESGCM(key).decrypt(base64.b64decode(envelope['nonce'], validate=True), cipher, canonical(aad))
        return strict_json(raw)
    except InvalidTag:
        fail('STATE_PASSWORD_OR_INTEGRITY_ERROR')
    except (KeyError, ValueError, TypeError):
        fail('STATE_CORRUPT')


# Bulky per-run artifacts that nothing in the audit-host scope reads back. Only the
# program bridge's verify_failure_evidence() rereads them, and the audit-host wrapper
# refuses every program command. Duplicate and re-run protection lives in the small
# run evidence (request-intent, model-attempt, failure-evidence, publish-intent,
# publish-response, run.json, comment.md), the wrapper journal and the PR comment.
EXCLUDED_RUN_FILES = ('claude-output.jsonl', 'claude-stderr.txt')
EXCLUDED_RUN_DIRS = ('work',)


def excluded(name):
    parts = PurePosixPath(name).parts
    return (len(parts) >= 3 and parts[0] == 'runs'
            and (parts[2] in EXCLUDED_RUN_DIRS or (len(parts) == 3 and parts[2] in EXCLUDED_RUN_FILES)))


def walk(root):
    """Every ledger path except the excluded artifacts, never descending into them."""
    found = []
    def unreadable(error):
        fail('STATE_CORRUPT')  # Never silently omit part of the ledger.
    for directory, dirnames, filenames in os.walk(root, onerror=unreadable, followlinks=False):
        here = Path(directory)
        kept = []
        for name in dirnames:
            if not excluded(str((here / name).relative_to(root))):
                kept.append(name)
        dirnames[:] = kept
        for name in kept + filenames:
            path = here / name
            if not excluded(str(path.relative_to(root))):
                found.append(path)
    return sorted(found)


def inventory(root, auditor_uid, auditor_gid):
    """Descriptor-read inventory of claims, quotas, journals and run evidence."""
    records, total = [], 0
    root_info = root.lstat() if os.path.lexists(root) else None
    if (root_info is None or not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid not in (0, auditor_uid)
            or root_info.st_gid not in (0, auditor_gid) or root_info.st_mode & 0o022):
        fail('STATE_CORRUPT')
    largest = ('', 0)
    for path in walk(root):
        info = path.lstat()
        name = str(path.relative_to(root))
        safe_relative(name)
        if (info.st_uid not in (0, auditor_uid) or info.st_gid not in (0, auditor_gid)
                or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            fail('STATE_CORRUPT')
        record = {'path': name, 'kind': 'dir' if path.is_dir() else 'file',
                  'mode': stat.S_IMODE(info.st_mode), 'uid': 'AUDITOR' if info.st_uid == auditor_uid and auditor_uid != 0 else 0,
                  'gid': 'AUDITOR' if info.st_gid == auditor_gid and auditor_gid != 0 else 0}
        if record['kind'] == 'file':
            data = read_regular(path, protected=False, hardlinks=True)
            total += len(data)
            if len(data) >= largest[1]:
                largest = (name, len(data))
            if total > MAX_STATE * 2 // 3:
                fail('STATE_TOO_LARGE', f'total>{MAX_STATE * 2 // 3} largest={largest[0]} bytes={largest[1]}')
            record.update(size=len(data), sha256=sha(data), bytes=base64.b64encode(data).decode())
        records.append(record)
    if not records:
        fail('STATE_CORRUPT')  # Only explicit enrollment may establish the initial marker.
    return records


def validate_payload(payload, uid, gid):
    if not isinstance(payload, dict) or payload.get('format') != 'AUDIT_HOST_ONLY_V1':
        fail('STATE_CORRUPT')
    files = payload.get('files')
    if not isinstance(files, list) or not files:
        fail('STATE_CORRUPT')
    names = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            fail('STATE_CORRUPT')
        try:
            name = str(safe_relative(item['path']))
            if (name in names or excluded(name) or item['uid'] not in (0, 'AUDITOR')
                    or item['gid'] not in (0, 'AUDITOR')):
                fail('STATE_CORRUPT')
            names.add(name)
            mode = item['mode']
            if type(mode) is not int or mode < 0 or mode > 0o777 or mode & 0o022:
                fail('STATE_CORRUPT')
            if item['kind'] == 'file':
                data = base64.b64decode(item['bytes'], validate=True)
                total += len(data)
                if len(data) != item['size'] or sha(data) != item['sha256'] or total > MAX_STATE:
                    fail('STATE_CORRUPT')
            elif item['kind'] != 'dir':
                fail('STATE_CORRUPT')
        except (KeyError, TypeError, ValueError):
            fail('STATE_CORRUPT')
    kinds = {x['path']: x['kind'] for x in files}
    for name in names:
        for parent in PurePosixPath(name).parents:
            if str(parent) != '.' and kinds.get(str(parent)) != 'dir':
                fail('STATE_CORRUPT')
    token = payload.get('claude_token', {})
    if not isinstance(token, dict):
        fail('STATE_CORRUPT')
    if (not isinstance(token.get('value'), str) or not OAUTH.fullmatch(token.get('value', '')) or len(token['value']) > 4096
            or token.get('verified') is not True
            or type(token.get('issued_at')) is not int or type(token.get('expires_at')) is not int
            or not 0 < token['issued_at'] < token['expires_at']):
        fail('STATE_CORRUPT')
    if 'github_token' in payload and (not isinstance(payload['github_token'], str) or not payload['github_token']):
        fail('STATE_CORRUPT')
    return payload


class Checkpoints:
    def __init__(self, backend, root, binding, token_path, uid, gid, boundary=Path('/')):
        self.backend, self.root, self.binding = backend, Path(root), Path(binding)
        self.token_path, self.uid, self.gid = Path(token_path), uid, gid
        self.boundary = boundary
        self.receipt = self.binding.parent / 'recovery-restore.json'
        # Root-only marker written before this tool mutates the ledger and removed after
        # the checkpoint is bound. It lets an interrupted publish be finished, never a
        # foreign or older change be accepted.
        self.pending = self.binding.parent / 'recovery-pending.json'

    def bound_commit(self):
        if not os.path.lexists(self.binding):
            return None
        return strict_json(read_regular(self.binding, boundary=self.boundary, mode=0o600)).get('commit')

    def read_pending(self):
        if not os.path.lexists(self.pending):
            return None
        value = strict_json(read_regular(self.pending, boundary=self.boundary, mode=0o600))
        if not isinstance(value, dict) or value.get('format') != 'AUDIT_HOST_PENDING_V1' or 'base' not in value:
            fail('STATE_CORRUPT')
        if value['base'] != self.bound_commit():
            # Bindings change only after a successful bind, so this marker is spent.
            self.pending.unlink()
            return None
        return value

    def mark_pending(self):
        if self.read_pending() is None:
            atomic(self.pending, canonical({'format': 'AUDIT_HOST_PENDING_V1', 'base': self.bound_commit()}))

    def clear_pending(self):
        if os.path.lexists(self.pending):
            self.pending.unlink()

    def fetch(self, password=None, key=None):
        h = self.backend.head()
        envelope, cipher = self.backend.load(h)
        try:
            salt = base64.b64decode(envelope['salt'], validate=True)
        except (ValueError, KeyError):
            fail('STATE_CORRUPT')
        if len(salt) != 16:
            fail('STATE_CORRUPT')
        key = key or derive(password, salt)
        payload = validate_payload(decrypt(envelope, cipher, key), self.uid, self.gid)
        if self.backend.head() != h:
            fail('STATE_CAS_CONFLICT')
        return h, envelope, payload, key

    def current(self, *, check_inventory=True, key=None):
        local = strict_json(read_regular(self.binding, boundary=self.boundary, mode=0o600))
        pending = self.read_pending()
        remote = self.backend.head()
        if remote != local.get('commit'):
            if pending is not None and key is not None:
                local = self.acknowledge(remote, local, key)
            else:
                fail('STATE_ROLLBACK_DETECTED')
        if not check_inventory:
            return local
        if sha(canonical(inventory(self.root, self.uid, self.gid))) == local.get('inventory_sha256'):
            if pending is not None:
                self.clear_pending()  # Marked, but nothing changed before the interruption.
            return local
        if pending is not None and key is not None:
            # This tool's own journal write reached disk but its publish did not.
            _, _, payload, _ = self.fetch(key=key)
            self.save(key, payload['claude_token'], github_token=payload.get('github_token'))
            return strict_json(read_regular(self.binding, boundary=self.boundary, mode=0o600))
        if pending is not None:
            fail('STATE_PUBLISH_PENDING')  # Finish with aiops-recover (state password).
        journal = self.root / 'recovery-audits.json'
        if journal.exists():
            entries = strict_json(read_regular(journal, boundary=self.boundary, mode=0o600))
            if any(x.get('state') in ('STARTED', 'UNKNOWN') for x in entries.values()):
                fail('UNKNOWN_REPRESENTATIVE_DECISION_REQUIRED')
        fail('STATE_CORRUPT')

    def acknowledge(self, remote, local, key):
        """The ref moved to our own next checkpoint but the local bind was lost."""
        if remote is None:
            fail('STATE_ROLLBACK_DETECTED')
        envelope, cipher = self.backend.load(remote)
        payload = validate_payload(decrypt(envelope, cipher, key), self.uid, self.gid)
        if (envelope.get('previous_commit') != local.get('commit') or envelope.get('salt') != local.get('salt')
                or envelope.get('version') != local.get('version', 0) + 1
                or payload['files'] != inventory(self.root, self.uid, self.gid)
                or self.backend.head() != remote):
            fail('STATE_ROLLBACK_DETECTED')
        self.bind(remote, envelope, payload)
        self.clear_pending()
        return strict_json(read_regular(self.binding, boundary=self.boundary, mode=0o600))

    def bind(self, head, envelope, payload):
        value = {'commit': head, 'version': envelope['version'], 'salt': envelope['salt'],
                 'inventory_sha256': sha(canonical(payload['files'])), 'claude_token': {
                     k: v for k, v in payload['claude_token'].items() if k != 'value'}}
        atomic(self.binding, canonical(value))

    def restore(self, password, *, key=None):
        h, envelope, payload, key = self.fetch(password, key)
        if os.path.lexists(self.binding):
            self.current(key=key)
            # An intact current instance is not overwritten; an interrupted publish is finished.
            _, _, payload, key = self.fetch(key=key)
            return payload, key
        expected_inventory = sha(canonical(payload['files']))
        transaction = {'commit': h, 'inventory_sha256': expected_inventory}
        pending = self.read_pending()
        if pending is not None and os.path.lexists(self.root):
            # This host's own enrollment created the branch, then lost its local bind.
            if (envelope.get('version') != 1 or envelope.get('previous_commit') is not None
                    or inventory(self.root, self.uid, self.gid) != payload['files']):
                fail('STATE_CORRUPT')
            self.restore_token(payload)
            if self.backend.head() != h:
                fail('STATE_CAS_CONFLICT')
            self.bind(h, envelope, payload)
            self.clear_pending()
            return payload, key
        if os.path.lexists(self.root):
            # Resume only our exact, durable transaction, never an unbound ledger.
            if not self.receipt.exists() or strict_json(read_regular(self.receipt, boundary=self.boundary, mode=0o600)) != transaction:
                fail('STATE_CORRUPT')
            if inventory(self.root, self.uid, self.gid) != payload['files']:
                fail('STATE_CORRUPT')
            self.restore_token(payload)
            if self.backend.head() != h:
                fail('STATE_CAS_CONFLICT')
            self.bind(h, envelope, payload)
            self.receipt.unlink()
            return payload, key
        protected_parent(self.root, self.boundary)
        protected_parent(self.token_path, self.boundary)
        stage = self.root.parent / ('.audit-restore-' + secrets.token_hex(8))
        stage.mkdir(mode=0o750)
        os.chmod(stage, 0o750)
        os.chown(stage, 0, self.gid)
        try:
            for item in sorted(payload['files'], key=lambda x: (len(PurePosixPath(x['path']).parts), x['path'])):
                path = stage / item['path']
                if item['kind'] == 'dir':
                    path.mkdir(mode=item['mode'])
                    os.chown(path, self.uid if item['uid'] == 'AUDITOR' else 0, self.gid if item['gid'] == 'AUDITOR' else 0)
                    os.chmod(path, item['mode'])
                else:
                    atomic(path, base64.b64decode(item['bytes']), item['mode'], self.uid if item['uid'] == 'AUDITOR' else 0, self.gid if item['gid'] == 'AUDITOR' else 0, replace=False)
            if inventory(stage, self.uid, self.gid) != payload['files']:
                fail('STATE_CORRUPT')
            if self.backend.head() != h:
                fail('STATE_CAS_CONFLICT')
            atomic(self.receipt, canonical(transaction))
            os.rename(stage, self.root)
            self.restore_token(payload)
            self.bind(h, envelope, payload)
            self.receipt.unlink()
            return payload, key
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def restore_token(self, payload):
        value = payload['claude_token']['value'].encode()
        if os.path.lexists(self.token_path):
            if read_regular(self.token_path, boundary=self.boundary, mode=0o600) != value:
                fail('INSTALLATION_DRIFT')
        else:
            atomic(self.token_path, value, replace=False)

    def save(self, key, token, *, github_token=None, enrollment=False, expected=None, salt=None):
        if enrollment:
            if self.binding.exists() or expected is not None or self.backend.head(missing=True) is not None:
                fail('STATE_CAS_CONFLICT')
            if salt is None or len(salt) != 16:
                fail('STATE_CORRUPT')
            version = 1
        else:
            local = self.current(check_inventory=False, key=key)
            expected = local['commit']
            salt, version = base64.b64decode(local['salt']), local['version'] + 1
        self.mark_pending()
        payload = {'format': 'AUDIT_HOST_ONLY_V1', 'files': inventory(self.root, self.uid, self.gid), 'claude_token': token}
        if github_token:
            payload['github_token'] = github_token
        validate_payload(payload, self.uid, self.gid)
        envelope, cipher = encrypt(payload, key, salt, version, expected)
        h = self.backend.publish(expected, envelope, cipher)
        self.bind(h, envelope, payload)
        self.clear_pending()
        return h


def token_warning(token, now=None):
    now = int(time.time()) if now is None else now
    remaining = token['expires_at'] - now
    if remaining <= 0:
        fail('TOKEN_REISSUE_REQUIRED')
    return {'expires_within_30_days': remaining <= 30 * 86400, 'days_remaining': max(0, remaining // 86400)}


def confirm_model(input_fn=input):
    return input_fn('모델 호출이 발생합니다. 실행할까요? [y/N] ').strip().lower() == 'y'


def check_platform(manifest):
    if manifest.get('python_version') and list(sys.version_info[:2]) != manifest['python_version']:
        fail('PYTHON_VERSION_DRIFT')
    if manifest.get('platform'):
        import platform
        release = platform.freedesktop_os_release()
        if (manifest['platform'] != 'debian-13-x86_64-glibc-2.41' or release.get('ID') != 'debian'
                or release.get('VERSION_ID') != '13' or platform.machine() != 'x86_64'
                or platform.libc_ver() != ('glibc', '2.41')):
            fail('HOST_PLATFORM_DRIFT')


class Installer:
    """Fixed destinations; source hashes are verified on already-open descriptors."""
    def __init__(self, manifest, prefix=Path('/'), owner=0):
        self.manifest, self.prefix, self.owner = manifest, Path(prefix), owner
        if manifest.get('source_commit') != SOURCE or manifest.get('scope') != 'AUDIT_HOST_ONLY':
            fail('MANIFEST_REJECTED')
        destinations = set()
        for item in manifest.get('files', []):
            path = item.get('destination', '')
            if (not path.startswith(('/opt/aiops/', '/usr/local/bin/', '/etc/aiops/'))
                    or '..' in Path(path).parts or path in destinations or not SHA.fullmatch(item.get('sha256', ''))
                    or item.get('mode') not in (0o644, 0o755, 0o600)):
                fail('MANIFEST_REJECTED')
            destinations.add(path)
        if not destinations:
            fail('MANIFEST_REJECTED')

    def path(self, absolute):
        return self.prefix / absolute.lstrip('/')

    def check_file(self, item):
        path = self.path(item['destination'])
        protected_parent(path, self.prefix, self.owner)
        data = read_regular(path, boundary=self.prefix, mode=item['mode'])
        if sha(data) != item['sha256']:
            fail('INSTALLATION_DRIFT')

    def verify(self, *, receipt=True):
        check_platform(self.manifest)
        if receipt:
            value = strict_json(read_regular(self.path('/etc/aiops/recovery-install.json'), boundary=self.prefix, mode=0o600))
            if value != {'complete': True, 'manifest_sha256': sha(canonical(self.manifest))}:
                fail('INSTALLATION_INCOMPLETE')
        for item in self.manifest['files']:
            self.check_file(item)

    def install(self, fetch):
        check_platform(self.manifest)
        for directory in ('/opt/aiops', '/opt/aiops/bin', '/opt/aiops/lib', '/opt/aiops/lib/fable', '/etc/aiops', '/usr/local/bin'):
            path = self.path(directory)
            if not path.exists():
                protected_parent(path, self.prefix, self.owner)
                path.mkdir(mode=0o755)
                os.chmod(path, 0o755)
            protected_parent(path / 'sentinel', self.prefix, self.owner)
        for item in self.manifest['files']:
            parent = self.path(item['destination']).parent
            missing_parents = []
            while not parent.exists():
                missing_parents.append(parent); parent = parent.parent
            for parent in reversed(missing_parents):
                protected_parent(parent, self.prefix, self.owner)
                parent.mkdir(mode=0o755)
                os.chmod(parent, 0o755)
            protected_parent(self.path(item['destination']), self.prefix, self.owner)
        # Validate ALL existing files and ALL incoming bytes before writing anything.
        missing = []
        for item in self.manifest['files']:
            path = self.path(item['destination'])
            if os.path.lexists(path):
                self.check_file(item)
            else:
                data = fetch(item)  # Fetch returns bytes copied from the SAME verified fd.
                if sha(data) != item['sha256']:
                    fail('ARTIFACT_HASH_MISMATCH')
                missing.append((item, data))
        receipt = self.path('/etc/aiops/recovery-install.json')
        atomic(receipt, canonical({'complete': False, 'manifest_sha256': sha(canonical(self.manifest))}))
        for item, data in missing:
            atomic(self.path(item['destination']), data, item['mode'], replace=False)
        self.verify(receipt=False)
        atomic(receipt, canonical({'complete': True, 'manifest_sha256': sha(canonical(self.manifest))}))


KEY_DIR = Path('/dev/shm/aiops-recover-key')


def keyring(action, value=None):
    """Root-only kernel key; on unsupported kernels, root-private tmpfs key."""
    tool = '/usr/bin/keyctl'
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    try:
        ring = subprocess.run([tool, 'get_persistent', '0'], capture_output=True, check=True, env=env).stdout.strip().decode()
        if action == 'set':
            ident = subprocess.run([tool, 'padd', 'user', 'aiops-audit-checkpoint', ring], input=value,
                                   capture_output=True, check=True, env=env).stdout.strip().decode()
            if not ident.isdigit():
                fail('STATE_KEY_UNAVAILABLE')
            subprocess.run([tool, 'setperm', ident, '0x003f0000'], capture_output=True, check=True, env=env)
            return
        ident = subprocess.run([tool, 'search', ring, 'user', 'aiops-audit-checkpoint'], capture_output=True, check=True, env=env).stdout.strip().decode()
        key = subprocess.run([tool, 'pipe', ident], capture_output=True, check=True, env=env).stdout
        if len(key) != 32:
            fail('STATE_KEY_UNAVAILABLE')
        return key
    except (OSError, subprocess.CalledProcessError, RecoveryError):
        return tmpfs_key(action, value)


def tmpfs_key(action, value=None):
    # /dev/shm is sticky, so all secrets are in a separate root-private directory.
    mounts = Path('/proc/self/mountinfo').read_text().splitlines()
    if not any(x.split()[4] == str(KEY_DIR.parent) and x.split(' - ', 1)[1].split()[0] == 'tmpfs'
               for x in mounts if ' - ' in x):
        fail('STATE_KEY_UNAVAILABLE')
    if not KEY_DIR.exists():
        if action != 'set':
            fail('STATE_KEY_UNAVAILABLE')
        KEY_DIR.mkdir(mode=0o700)
    info = KEY_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        fail('STATE_KEY_UNAVAILABLE')
    path = KEY_DIR / 'derived-key'
    if action == 'set':
        if not isinstance(value, bytes) or len(value) != 32:
            fail('STATE_KEY_UNAVAILABLE')
        atomic(path, value)
        return
    try:
        key = read_regular(path, boundary=KEY_DIR, mode=0o600, limit=32)
    except RecoveryError:
        fail('STATE_KEY_UNAVAILABLE')
    if len(key) != 32:
        fail('STATE_KEY_UNAVAILABLE')
    return key


def config():
    if os.geteuid() != 0:
        fail('PRIVILEGED_EXECUTOR_UNAVAILABLE')
    value = strict_json(read_regular(CONFIG, mode=0o600))
    if value.get('scope') != 'AUDIT_HOST_ONLY' or value.get('source_commit') != SOURCE:
        fail('MANIFEST_REJECTED')
    return value


def pinned_module(path, digest):
    data = read_regular(path)
    if sha(data) != digest:
        fail('INSTALLATION_DRIFT')
    # Execute exactly the verified bytes; do not reopen a mutable script.
    import types
    module = types.ModuleType('aiops_pinned_' + Path(path).stem)
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


def components(cfg, *, create_account=False):
    manifest_bytes = read_regular('/etc/aiops/recovery-manifest.json')
    if sha(manifest_bytes) != cfg['manifest_sha256']:
        fail('MANIFEST_REJECTED')
    manifest = strict_json(manifest_bytes)
    inst = Installer(manifest)
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token and cfg.get('store_github_token') is True and GH_SAVED.exists():
        token = read_regular(GH_SAVED, mode=0o600).decode()
    if not token:
        fail('GITHUB_AUTH_REQUIRED')
    backend = GithubState(cfg['state_repository'], cfg['state_branch'], token)
    if cfg.get('account') != manifest.get('account') or cfg.get('claude_version') != manifest.get('claude_version'):
        fail('MANIFEST_REJECTED')
    account = ensure_account(cfg, create=create_account)
    cfg['_auditor'] = {'uid': account.pw_uid, 'gid': account.pw_gid}
    cp = Checkpoints(backend, STATE, BINDING, TOKEN, account.pw_uid, account.pw_gid)
    item = next(x for x in manifest['files'] if x['destination'] == '/opt/aiops/lib/fable/control_plane_fable.py')
    return inst, cp, item, token


def ensure_account(cfg, *, create=True):
    import grp
    item = cfg['account']
    if item.get('name') != 'aiops-auditor' or item.get('home') != '/var/lib/aiops-auditor':
        fail('MANIFEST_REJECTED')
    try:
        existing = pwd.getpwnam(item['name'])
    except KeyError:
        if not create:
            fail('INSTALLATION_DRIFT')
        subprocess.run(['/usr/sbin/useradd', '--system', '--user-group', '--home-dir', item['home'],
                        '--shell', '/usr/sbin/nologin', '--no-create-home', item['name']],
                       check=True, capture_output=True)
        existing = pwd.getpwnam(item['name'])
    if (existing.pw_uid == 0 or existing.pw_gid == 0 or existing.pw_dir != item['home']
            or existing.pw_shell != '/usr/sbin/nologin'
            or grp.getgrgid(existing.pw_gid).gr_name != item['name']
            or set(os.getgrouplist(item['name'], existing.pw_gid)) != {existing.pw_gid}):
        fail('INSTALLATION_DRIFT')
    # Query the effective sudo policy, not just membership in a group named sudo.
    # Listing another user exits 0 (sudo 1.9.15/1.9.16) or 1 with this exact line
    # when that user has no rule; any listed rule refuses the account.
    if Path('/usr/bin/sudo').exists():
        policy = subprocess.run(['/usr/bin/sudo', '-n', '-l', '-U', item['name']], capture_output=True,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, timeout=30)
        listing = policy.stdout + policy.stderr
        if (policy.returncode not in (0, 1)
                or b'User ' + item['name'].encode() + b' is not allowed to run sudo' not in listing
                or b'may run the following commands' in listing):
            fail('AUDITOR_SUDO_FORBIDDEN')
    home = Path(item['home'])
    if not os.path.lexists(home):
        if not create:
            fail('INSTALLATION_DRIFT')
        home.mkdir(mode=0o700)
        os.chown(home, existing.pw_uid, existing.pw_gid)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != existing.pw_uid or info.st_gid != existing.pw_gid or stat.S_IMODE(info.st_mode) != 0o700:
        fail('INSTALLATION_DRIFT')
    return existing


def fetch_artifact(item, cfg):
    # Cache bytes are copied from a single descriptor; wheels are never executed.
    cached = cfg.setdefault('_artifact_bytes', {})
    identity = item.get('wheel_sha256') or item['sha256']
    if identity in cached:
        data = cached[identity]
    else:
        data = None
    source = Path(cfg['artifact_cache']) / str(safe_relative(item['cache_path']))
    if data is not None:
        pass
    elif source.exists():
        data = read_regular(source, protected=False)
    else:
        url = item.get('url', '')
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('downloads.claude.ai', 'files.pythonhosted.org') or parsed.username or parsed.password:
            fail('ARTIFACT_UNAVAILABLE')
        try:
            with urllib.request.build_opener(NoRedirect()).open(url, timeout=120) as response:
                data = response.read(MAX_STATE + 1)
            if len(data) > MAX_STATE:
                fail('STATE_TOO_LARGE')
        except (OSError, ValueError):
            fail('ARTIFACT_UNAVAILABLE')
    cached[identity] = data
    if 'wheel_member' in item:
        import zipfile
        if sha(data) != item.get('wheel_sha256'):
            fail('ARTIFACT_HASH_MISMATCH')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = str(safe_relative(item['wheel_member']))
            try:
                info = archive.getinfo(member)
                if info.file_size > 32 << 20 or stat.S_ISLNK(info.external_attr >> 16):
                    fail('MANIFEST_REJECTED')
                return archive.read(info)
            except KeyError:
                fail('ARTIFACT_HASH_MISMATCH')
    return data




def authentication_error(event):
    """Only typed error fields. IDs, hashes, result text and line numbers are data."""
    if not isinstance(event, dict):
        return False
    if event.get('type') == 'result' and event.get('is_error') is not True:
        return False
    if event.get('type') == 'result' and event.get('is_error') is True:
        scopes = [event, event.get('error'), *(event.get('errors') or [])]
    elif event.get('status') == 'ERROR' or event.get('type') == 'error':
        scopes = [event, event.get('error')]
    else:
        return False
    for error in scopes:
        if not isinstance(error, dict):
            continue
        if any(error.get(k) in (401, '401') for k in ('status_code', 'http_status', 'api_error_status', 'status')):
            return True
        if error.get('type') == 'authentication_error' or error.get('error_code') in ('TOKEN_REISSUE_REQUIRED', 'AUTHENTICATION_ERROR'):
            return True
    return False


def invoke_fable(fable, args):
    """Observe original tool execution without changing its pinned file bytes."""
    observed = {'model_attempted': False, 'failure': None}
    original = fable.production_context
    contexts = []
    def context(*a, **kw):
        ctx, version = original(*a, **kw)
        contexts.append(ctx)
        return ctx, version
    fable.production_context = context
    output = io.StringIO()
    try:
        try:
            with redirect_stdout(output):
                rc = fable.main(args)
        except SystemExit as error:
            rc = error.code if isinstance(error.code, int) else 1
    finally:
        fable.production_context = original
        for ctx in contexts:
            execution = getattr(ctx.runner, 'last_execution', {})
            observed['model_attempted'] |= execution.get('model_attempted') is True
            if isinstance(getattr(ctx, 'last_failure', None), dict):
                observed['failure'] = ctx.last_failure
    rendered = output.getvalue().strip()
    try:
        event = json.loads(rendered)
        if not isinstance(event, dict):
            event = {}
    except ValueError:
        event = {}
    return rc, rendered, event, observed


# Audit verdicts, consult answers (aiops-fable consult) and a completed preflight.
RESULTS = ('PASS', 'PASS_WITH_NOTES', 'FAIL', 'DECISION_REQUIRED', 'ANSWERED', 'USER_REQUIRED')


def execution_state(event, observed):
    if event.get('result') in RESULTS or (event.get('status') == 'PASS' and isinstance(event.get('run'), str)):
        return 'RESULT'
    failure = observed.get('failure') or {}
    if event.get('status') == 'BUSY' or observed.get('model_attempted') is False or failure.get('error_code') == 'PRE_MODEL_FAILED':
        return 'NOT_STARTED'
    return 'UNKNOWN'


class AuditGuard:
    def __init__(self, checkpoints, key, token, fable, github_token=None):
        self.cp, self.key, self.token, self.fable = checkpoints, key, token, fable
        self.github_token = github_token
        self.journal = self.cp.root / 'recovery-audits.json'

    def entries(self):
        if not self.journal.exists():
            return {}
        value = strict_json(read_regular(self.journal, boundary=self.cp.boundary, mode=0o600))
        if not isinstance(value, dict):
            fail('STATE_CORRUPT')
        return value

    def inspect(self, repository, number, head, comments, *, again=False):
        self.cp.current(check_inventory=not again, key=self.key)
        key = f'{repository}#{number}@{head}'
        old = self.entries().get(key)
        prior = None
        for c in reversed(comments):
            body = c.get('body') or ''
            match = re.search(r'ASTRA_AUDIT_V1\s+pr=(\d+)\s+head=([a-f0-9]{40})\s+result=(PASS_WITH_NOTES|PASS|FAIL|DECISION_REQUIRED)\b', body)
            if match and int(match[1]) == number and match[2] == head:
                prior = {'status': 'AUDIT_EXISTS', 'result': match[3], 'head': head}
                break
        if again:
            # Show bounded previous state only, no provider output or credentials.
            print(json.dumps({'status': 'PREVIOUS_EXECUTION', 'state': (old or {}).get('state', 'NO_LOCAL_RECORD'),
                              'result': (prior or old or {}).get('result'), 'head': head}))
            if input('다시 실행할까요? [y/N] ').strip().lower() != 'y':
                return {'status': 'CANCELLED', 'model_called': False}
            return None
        if prior:
            return prior
        if old is not None:
            if old.get('state') == 'RESULT':
                return {'status': 'AUDIT_EXISTS', 'result': old.get('result'), 'head': head}
            if old.get('state') != 'NOT_STARTED':
                fail('UNKNOWN_REPRESENTATIVE_DECISION_REQUIRED')
        return None

    def before(self, repository, number, head, comments, *, again=False, admitted=False):
        if not admitted:
            existing = self.inspect(repository, number, head, comments, again=again)
            if existing:
                return existing
        key = f'{repository}#{number}@{head}'
        entries = self.entries()
        old = entries.get(key)
        history = list((old or {}).get('history', []))
        if old:
            history.append({k: v for k, v in old.items() if k != 'history'})
        entries[key] = {'state': 'STARTED', 'head': head, 'history': history}
        self.cp.mark_pending()
        atomic(self.journal, canonical(entries))
        self.cp.save(self.key, self.token, github_token=self.github_token)
        return None

    def after(self, repository, number, head, event, observed):
        key = f'{repository}#{number}@{head}'
        entries = self.entries()
        if entries.get(key, {}).get('state') != 'STARTED':
            fail('STATE_CORRUPT')
        entries[key]['state'] = execution_state(event, observed)
        if event.get('result') in RESULTS:
            entries[key]['result'] = event['result']
        self.cp.mark_pending()
        atomic(self.journal, canonical(entries))
        self.cp.save(self.key, self.token, github_token=self.github_token)


def prepare_enrollment(cp, input_fn=input):
    if cp.binding.exists() or cp.backend.head(missing=True) is not None:
        fail('STATE_CAS_CONFLICT')
    if cp.root.exists() and any(cp.root.iterdir()):
        inventory(cp.root, cp.uid, cp.gid)
        return
    if input_fn('기존 기록 없이 시작합니다. 중복 감사는 PR 댓글로만 막습니다. 계속할까요? [y/N] ').strip().lower() != 'y':
        fail('ENROLLMENT_CANCELLED')
    protected_parent(cp.root, cp.boundary)
    if not cp.root.exists():
        cp.root.mkdir(mode=0o750)
        os.chmod(cp.root, 0o750)
        os.chown(cp.root, 0, cp.gid)
    # An explicit initial-registration marker; restore never manufactures this.
    runs = cp.root / 'runs'
    runs.mkdir(mode=0o755, exist_ok=True)
    os.chmod(runs, 0o755)
    atomic(cp.root / 'initial-registration.json', canonical({'format': 'HR_D3_INITIAL_REGISTRATION_V1',
                                                            'without_existing_records_confirmed': True}))
    inventory(cp.root, cp.uid, cp.gid)


def new_state_password(prompt=getpass.getpass):
    """The only key to every later restore: confirm it before anything is created."""
    password = prompt('상태 암호: ')
    if prompt('상태 암호 확인: ') != password:
        fail('STATE_PASSWORD_MISMATCH')
    return password


def verify_enrollment(cp, key):
    """Read the created checkpoint back with the same key before reporting success."""
    head, _, check, _ = cp.fetch(key=key)
    if head != cp.bound_commit() or check['files'] != inventory(cp.root, cp.uid, cp.gid):
        fail('STATE_CORRUPT')


def recover_main(argv=None):
    parser = argparse.ArgumentParser(prog='aiops-recover')
    parser.add_argument('--reissue-token', action='store_true')
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--enroll', action='store_true', help='create-only registration; explicitly confirm if no prior ledger exists')
    args = parser.parse_args(argv)
    try:
        cfg = config()
        with barrier('/etc/aiops/recovery.lock'):
            inst, cp, fable_item, gh = components(cfg, create_account=True)
            inst.install(lambda item: fetch_artifact(item, cfg))
            if args.enroll:
                prepare_enrollment(cp)
                password = new_state_password()
                salt = secrets.token_bytes(16)
                key = derive(password, salt)
                del password
                token_item = next(x for x in inst.manifest['files'] if x['destination'] == '/opt/aiops/lib/control_plane_recover_token.py')
                token_module = pinned_module(Path(token_item['destination']), token_item['sha256'])
                token = token_module.enrollment_token(cfg, inst, TOKEN)
                cp.save(key, token, github_token=gh if cfg.get('store_github_token') is True else None, enrollment=True, salt=salt)
                atomic(TOKEN, token['value'].encode())
                verify_enrollment(cp, key)
                keyring('set', key)
                token_module.cleanup(cfg)
                result = {'status': 'AUDIT_HOST_ENROLLED'}
            elif args.reissue_token:
                inst.verify()
                token_item = next(x for x in inst.manifest['files'] if x['destination'] == '/opt/aiops/lib/control_plane_recover_token.py')
                token_module = pinned_module(Path(token_item['destination']), token_item['sha256'])
                reissue = token_module.reissue
                try:
                    key = keyring('get')
                    payload, key = cp.restore(None, key=key)
                except RecoveryError as e:
                    if e.code != 'STATE_KEY_UNAVAILABLE':
                        raise
                    payload, key = cp.restore(getpass.getpass('상태 암호: '))
                token = reissue(cfg, inst)
                cp.save(key, token, github_token=gh if cfg.get('store_github_token') is True else None)
                atomic(TOKEN, token['value'].encode())
                keyring('set', key)
                token_module.cleanup(cfg)
                result = {'status': 'TOKEN_REISSUED'}
            else:
                payload, key = cp.restore(getpass.getpass('상태 암호: '))
                sync_token(payload)
                keyring('set', key)
                if cfg.get('store_github_token') is True and payload.get('github_token'):
                    atomic(GH_SAVED, payload['github_token'].encode())
                result = {'status': 'AUDIT_HOST_RESTORED', **token_warning(payload['claude_token'])}
            # Recovery NEVER runs a preflight unless separately confirmed by the operator.
        if args.preflight:
            return fable_main(['preflight'])
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps(hold(sys.exc_info()[1])))
        return 1


def sync_token(payload):
    """The bound, authenticated checkpoint is the only source of the model credential."""
    value = payload['claude_token']['value'].encode()
    protected_parent(TOKEN)
    if not os.path.lexists(TOKEN) or read_regular(TOKEN, mode=0o600) != value:
        atomic(TOKEN, value)


def fable_main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ('audit', 'consult', 'preflight') or ('--again' in args and args[0] == 'preflight'):
        print(json.dumps({'status': 'HOLD', 'reason': 'AUDIT_HOST_ONLY'}))
        return 1
    if args[0] == 'preflight' and not confirm_model():
        print(json.dumps({'status': 'CANCELLED', 'model_called': False}))
        return 0
    try:
        cfg = config()
        with barrier('/etc/aiops/recovery.lock'):
            inst, cp, fable_item, gh = components(cfg)
            inst.verify()
            again = args[0] in ('audit', 'consult') and '--again' in args
            key = keyring('get')
            # With the key, an interrupted publish of this tool's own journal is finished here.
            cp.current(check_inventory=not again, key=key)
            _, _, payload, _ = cp.fetch(key=key)
            token = payload['claude_token']
            sync_token(payload)
            fable = pinned_module(Path(fable_item['destination']), fable_item['sha256'])
            # CLI path is a fixed root-owned launcher that disables both update paths.
            cli = subprocess.run(['/usr/local/bin/claude', '--version'], capture_output=True, timeout=30, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'DISABLE_AUTOUPDATER': '1', 'DISABLE_UPDATES': '1'})
            if cli.returncode or cli.stdout.strip().decode() != cfg['claude_version']:
                fail('CLI_VERSION_DRIFT')
            guard = AuditGuard(cp, key, token, fable, gh if cfg.get('store_github_token') is True else None)
            if args[0] == 'audit':
                parsed = argparse.ArgumentParser(add_help=False)
                for name in ('repository', 'pr', 'head'):
                    parsed.add_argument('--' + name, required=True)
                target, _ = parsed.parse_known_args(args[1:])
                if not REPO.fullmatch(target.repository) or not re.fullmatch('[a-f0-9]{40}', target.head):
                    fail('REQUEST_REJECTED')
                number = int(target.pr)
                if number < 1:
                    fail('REQUEST_REJECTED')
                comments = fable.GitHub(gh).pages(f'/repos/{target.repository}/issues/{number}/comments')
                existing = guard.inspect(target.repository, number, target.head, comments, again=again)
                if existing:
                    print(json.dumps(existing))
                    return 0
            else:
                # Consult/preflight also persist a one-shot start under exact arguments.
                same = [x for x in args if x != '--again']  # --again names the same consult.
                target = argparse.Namespace(repository='BeautifulMind-JT/ai-ops-control-plane',
                                            head=secrets.token_hex(20) if args[0] == 'preflight' else hashlib.sha1(canonical([same, sha(token['value'].encode())])).hexdigest())
                number = 0
                comments = []
                existing = guard.inspect(target.repository, number, target.head, comments, again=again)
                if existing:
                    print(json.dumps(existing))
                    return 0
            warning = token_warning(token)
            guard.before(target.repository, number, target.head, comments, again=again, admitted=True)
            rc, rendered, event, observed = invoke_fable(fable, args)
            try:
                guard.after(target.repository, number, target.head, event, observed)
            except RecoveryError as error:
                # The result is still shown; the marked publish finishes on the next command.
                print(rendered)
                print(json.dumps(hold(error)))
                return 1
            # Preserve the actual audit output, including normal IDs containing 401.
            print(rendered)
            failure = observed.get('failure') or {}
            if authentication_error(event) or failure.get('api_error_status') == 401:
                print(json.dumps({'status': 'HOLD', 'reason': 'TOKEN_REISSUE_REQUIRED',
                                  'prefix_valid': True, 'length': len(token['value']),
                                  'command': 'aiops-recover --reissue-token'}))
                return 1
            if warning['expires_within_30_days']:
                print(json.dumps({'warning': 'TOKEN_EXPIRES_WITHIN_30_DAYS', **warning}))
            return rc
    except Exception:
        print(json.dumps(hold(sys.exc_info()[1])))
        return 1


if __name__ == '__main__':
    sys.exit(recover_main())
