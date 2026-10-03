#!/usr/bin/env python3
"""Candidate, manual-only Mac recovery. Requires User adoption before --apply.

An actual intervening host boot proves termination, never provider success.
No worker receipt is created, altered or interpreted as successful completion.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import math
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))

from common import AppError, JOB_RECORD_LIMIT, digest, encoded, parse_json, read_json
from core import Store
import install
from gitops import Repositories


def boot_id():
    if sys.platform != 'darwin': raise AppError('MAC_REQUIRED')
    result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                            capture_output=True, text=True, timeout=5, check=False)
    value = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}', value):
        raise AppError('BOOT_ID_UNAVAILABLE')
    return value.upper()


def host_id():
    if sys.platform != 'darwin': raise AppError('MAC_REQUIRED')
    result = subprocess.run(['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOPlatformExpertDevice'],
                            capture_output=True, timeout=5, check=False)
    try: value = plistlib.loads(result.stdout)[0]['IOPlatformUUID']
    except (ValueError, KeyError, IndexError, TypeError, plistlib.InvalidFileException):
        raise AppError('HOST_ID_UNAVAILABLE')
    if result.returncode or not isinstance(value, str) or not re.fullmatch(r'[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}', value):
        raise AppError('HOST_ID_UNAVAILABLE')
    # Retain only a hash, never the hardware identifier itself in evidence.
    return digest(value.upper())


def boot_time():
    if sys.platform != 'darwin': raise AppError('MAC_REQUIRED')
    result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'],
                            capture_output=True, text=True, timeout=5, check=False)
    match = re.match(r'^\{ sec = ([0-9]+), usec = ([0-9]+) \}', result.stdout.strip())
    if result.returncode or not match or int(match[2]) >= 1000000:
        raise AppError('BOOT_TIME_UNAVAILABLE')
    return int(match[1]) + int(match[2]) / 1000000


def jobs_readonly(state):
    path = Path(state) / 'app.sqlite3'
    install.owned_file(path, True)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink(): install.owned_file(sidecar, True)
    if Path(str(path) + '-wal').exists() and not Path(str(path) + '-shm').exists():
        raise AppError('INSTALLATION_STATE_UNVERIFIED')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.execute('PRAGMA query_only=ON')
        return [parse_json(row[0], JOB_RECORD_LIMIT) for row in db.execute('SELECT document FROM jobs')]


def validate_target(state, jobs, key):
    target = next((job for job in jobs if job['id'] == key), None)
    if not target: raise AppError('JOB_NOT_FOUND')
    if (target['state'] != 'unknown' or not target.get('attempt') or
            target.get('pause_requested') is not True):
        raise AppError('PAUSED_UNKNOWN_REQUIRED')
    # No unrelated active/paused/ready job is affected by this procedure.
    if any(job['id'] != key and job['state'] not in ('accepted', 'cancelled') for job in jobs):
        raise AppError('OTHER_JOB_NOT_TERMINAL')
    attempt = target['attempt']
    if not re.fullmatch(r'[a-f0-9]{16}', key) or not re.fullmatch(r'[a-f0-9]{32}', attempt['id']):
        raise AppError('INVALID_ATTEMPT_ID')
    folder = Path(state) / 'jobs' / key / attempt['id']
    if (folder / 'receipt.json').exists(): raise AppError('TERMINAL_RECEIPT_AVAILABLE')
    install.owned_file(folder / 'request.json', True)
    request = read_json(folder / 'request.json', 32 * 1024 * 1024)
    if request['attempt_id'] != attempt['id'] or request['binding'] != attempt['binding']:
        raise AppError('REQUEST_BINDING_MISMATCH')
    return target, digest(request)


def prepare(state, key, witness):
    state = Path(state).resolve()
    target, request_hash = validate_target(state, jobs_readonly(state), key)
    created = time.time(); old_boot_time = boot_time()
    if not old_boot_time <= target['attempt']['started'] <= created:
        raise AppError('ATTEMPT_BOOT_UNVERIFIED')
    value = {'schema_version': 1, 'state': str(state), 'uid': os.getuid(),
             'host_id': host_id(), 'boot_id': boot_id(), 'job_id': key, 'job_hash': digest(target),
             'attempt_id': target['attempt']['id'], 'binding': target['attempt']['binding'],
             'request_hash': request_hash, 'created': created, 'boot_time': old_boot_time}
    # Single use pre-boot witness; its hash must be pinned in the User decision.
    fd = os.open(witness, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(encoded(value) + '\n'); stream.flush(); os.fsync(stream.fileno())
    return value


def validate_witness(state, value, current_boot, jobs, approved_hash):
    if not isinstance(approved_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', approved_hash) or digest(value) != approved_hash:
        raise AppError('APPROVED_WITNESS_MISMATCH')
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or
            value.get('state') != str(Path(state).resolve()) or value.get('uid') != os.getuid()):
        raise AppError('RECOVERY_WITNESS_MISMATCH')
    if value.get('host_id') != host_id(): raise AppError('RECOVERY_HOST_MISMATCH')
    if not isinstance(value.get('boot_id'), str) or not re.fullmatch(r'[A-F0-9]{8}(?:-[A-F0-9]{4}){3}-[A-F0-9]{12}', value['boot_id']):
        raise AppError('INVALID_BOOT_WITNESS')
    if value['boot_id'] == current_boot: raise AppError('HOST_RESTART_REQUIRED')
    target, request_hash = validate_target(state, jobs, value['job_id'])
    if (digest(target) != value.get('job_hash') or request_hash != value.get('request_hash') or
            target['attempt']['id'] != value.get('attempt_id') or target['attempt']['binding'] != value.get('binding')):
        raise AppError('RECOVERY_WITNESS_MISMATCH')
    dates = (value.get('boot_time'), target['attempt']['started'], value.get('created'), boot_time())
    if (any(type(item) not in (int, float) or not math.isfinite(item) for item in dates) or
            not dates[0] <= dates[1] <= dates[2] < dates[3]):
        raise AppError('HOST_RESTART_REQUIRED')
    return target


def reconcile(store, value, current_boot, decision_url, approved_hash):
    # Caller holds the exclusive service lock. This transaction retains all
    # original records/workspaces and atomically records the distinct abortion.
    store.db.execute('BEGIN IMMEDIATE')
    try:
        target = validate_witness(store.directory, value, current_boot, store.jobs(), approved_hash)
        repos = Repositories(store.directory / 'workspaces')
        repos.assert_binding(target); repos.assert_scope(target)
        if target['attempt']['role'] != 'builder' and (repos.head(target) != target['attempt']['head'] or not repos.clean(target)):
            raise AppError('READ_ONLY_ROLE_MODIFIED_CHECKOUT')
        evidence = {'attempt': value['attempt_id'], 'binding': value['binding'],
                    'status': 'aborted', 'error': 'HOST_RESTARTED_WITHOUT_RECEIPT',
                    'old_boot': value['boot_id'], 'new_boot': current_boot,
                    'witness_hash': digest(value), 'decision_url': decision_url, 'at': time.time()}
        result = store.update(target['id'], state='paused', attempt=None,
                              pause_requested=True, not_before=0, last_terminal=evidence,
                              provider_error='HOST_RESTARTED_WITHOUT_RECEIPT',
                              blocker={'code': 'HOST_RESTARTED_WITHOUT_RECEIPT', 'at': evidence['at']})
        store.event(target['id'], 'host_restart_recovery', encoded(evidence))
        store.db.commit()
        return result
    except BaseException:
        store.db.rollback(); raise


def apply(state, witness, decision_url, approved_hash):
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+#issuecomment-[0-9]+', decision_url):
        raise AppError('USER_DECISION_POINTER_REQUIRED')
    state = Path(state).resolve()
    install.owned_file(witness, True)
    value = read_json(witness); current_boot = boot_id()
    # Do not stop any service or open the DB for write before real boot proof.
    validate_witness(state, value, current_boot, jobs_readonly(state), approved_hash)
    home = Path.home()
    state = install.safe_path(state, home)
    app = install.safe_path(home / 'Applications/AIOPS.app', home)
    plist = install.safe_path(home / 'Library/LaunchAgents/local.aiops.mac.plist', home)
    config = install.installed_binding(app, state, plist)
    domain = 'gui/' + str(os.getuid())
    loaded = install.loaded_service(domain, config, state)
    fd = None; store = None
    try:
        if loaded: install.launchctl('bootout', domain + '/' + install.LABEL)
        fd = install.fence(state, wait=True)
        # Recheck evidence after fencing the service; late receipts/changed jobs
        # must abort recovery rather than clearing a different reservation.
        store = Store(state)
        return reconcile(store, value, current_boot, decision_url, approved_hash)
    finally:
        if store is not None: store.close()
        if fd is not None: os.close(fd)
        if loaded: install.launchctl('bootstrap', domain, str(plist))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'apply'))
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--witness', type=Path, required=True)
    parser.add_argument('--job-id')
    parser.add_argument('--decision-url')
    parser.add_argument('--witness-sha256')
    args = parser.parse_args()
    try:
        if args.operation == 'prepare':
            if not args.job_id: raise AppError('JOB_ID_REQUIRED')
            value = prepare(args.data_dir, args.job_id, args.witness)
            print('Pre-restart witness saved. No job or reservation changed. Pin SHA256: ' + digest(value))
        else:
            result = apply(args.data_dir, args.witness, args.decision_url or '', args.witness_sha256 or '')
            print('Interrupted attempt recorded as aborted; job remains paused: ' + result['id'])
    except (AppError, OSError, ValueError) as exc:
        print(exc.code if isinstance(exc, AppError) else type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
