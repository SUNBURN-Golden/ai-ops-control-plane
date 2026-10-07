#!/usr/bin/env python3
"""One User-approved empty pre-admission directory; no ledger or gate override."""
from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

if __name__ == '__main__': sys.dont_write_bytecode = True

from common import AppError, atomic_json, digest, encoded, private_directory
import handoff
import install
import mac_astra_receipt
import mac_glm_audit
from mac_authority import LocalSource

JOB = '20531604498943e1'
ATTEMPT = 'd8d194e55af64b0b8a44a487bd41fd94'
HEAD = 'dea4bd94a3adc8b4aca23f51b1fd12f323972d91'
BIRTH = 1791349134.219293
EVENT_TIME = 1791349134.227145
DECISION = {'comment_id':6032340624, 'created_at':'2026-10-07T06:28:48Z',
            'body_sha256':'60fda2c6fc6c8e99c118b6d05e6a09adc3e30388f2d4c6580d74b8256c255699'}
ERROR = 'EMPTY_ATTEMPT_RECOVERY_UNVERIFIED'
BUILDER_RECEIPT_SHA = '37ec496a3d73967fe92108b8fa09fc37b3cb1bd57e5607ebc7c55d1a865f17cb'


def require(value):
    if not value: raise AppError(ERROR)


@handoff.bounded_api_reads
def approval():
    root = 'SUNBURN-Golden/ai-ops-control-plane'
    value = handoff.api(f'repos/{root}/issues/comments/{DECISION["comment_id"]}')
    require(mac_astra_receipt.trusted_actor(value) and value.get('id') == DECISION['comment_id'] and
            value.get('created_at') == value.get('updated_at') == DECISION['created_at'] and
            value.get('issue_url') == f'https://api.github.com/repos/{root}/issues/85' and
            value.get('html_url') == f'https://github.com/{root}/pull/85#issuecomment-{DECISION["comment_id"]}' and
            isinstance(value.get('body'), str) and
            hashlib.sha256(value['body'].encode()).hexdigest() == DECISION['body_sha256'])


def directory(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700)
    return info


def empty_metadata(path):
    info = directory(path)
    require(not list(path.iterdir()))
    require(abs(info.st_birthtime - BIRTH) < 0.00001 and abs(info.st_mtime - BIRTH) < 0.00001)
    return {'device':info.st_dev, 'inode':info.st_ino, 'uid':info.st_uid, 'gid':info.st_gid,
            'mode':stat.S_IMODE(info.st_mode), 'birth':info.st_birthtime, 'mtime_ns':info.st_mtime_ns,
            'links':info.st_nlink}


def database_snapshot(state):
    result = {}
    for name in ('app.sqlite3', 'mac-astra.sqlite3'):
        path = state/name
        install.owned_file(path, True)
        for suffix in ('-wal', '-shm', '-journal'):
            side = Path(str(path)+suffix)
            if side.exists() or side.is_symlink(): install.owned_file(side, True)
        require(not Path(str(path)+'-wal').exists() or Path(str(path)+'-shm').exists())
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)])
            lines = list(db.iterdump())
            require(not any(ATTEMPT in line.lower() or ATTEMPT.encode().hex() in line.lower() for line in lines))
            result[name] = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
    return result


def context(state):
    """Read the admitted executions, preserving every original private receipt."""
    snapshots = database_snapshot(state)
    with closing(sqlite3.connect((state/'app.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        jobs = [json.loads(r[0]) for r in db.execute('SELECT document FROM jobs')]
        require(all(j.get('attempt') is None for j in jobs))
        matches = [j for j in jobs if j.get('id') == JOB]; require(len(matches) == 1)
        job = matches[0]
        require(job['head'] == HEAD and job['state'] in ('needs_user','paused') and
                job['phase'] == 'verifying' and type(job['calls']) is int and job['calls'] == 3)
        for table in ('native_local','mac_host_attempts'):
            require(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] > 0)
            require(db.execute('SELECT COUNT(*) FROM '+table+' WHERE state != ?', ('TERMINAL',)).fetchone()[0] == 0)
        event = db.execute('SELECT message FROM events WHERE job_id=? AND kind=? AND created=?',
                           (JOB,'setup_required',EVENT_TIME)).fetchall()
        require(len(event) == 1 and event[0][0] == '선택한 실행 도구가 설치되어 있지 않습니다. 연결 화면을 확인해 주세요.')
    lineage = job['native_lineage']
    root = state/'jobs'/JOB
    directory(root)
    folders = [state/'native'/lineage['request_id']/lineage['attempt_id']]
    for item in root.iterdir():
        require(not item.is_symlink())
        if item.name != ATTEMPT and item.is_dir(): folders.append(item)
    require(len(folders) == job['calls'])
    reader = object.__new__(LocalSource)
    receipts = {}
    for folder in folders:
        directory(folder)
        request = reader._private_json(folder/'request.json')
        require(request['attempt_id'] == folder.name and request['host_directory'] == str(state))
        reader.private_receipt(folder, {'id':folder.name,'binding':request['binding']})
        receipts[str(folder/'receipt.json')] = hashlib.sha256((folder/'receipt.json').read_bytes()).hexdigest()
        running = folder/'running.json'
        if running.exists():
            pid = reader._private_json(running).get('pid')
            require(type(pid) is int and pid > 1)
            try: os.killpg(pid, 0)
            except ProcessLookupError: pass
            else: raise AppError(ERROR)
    require(receipts.get(str(root/'f10a3b1d95804b94b8eb8390fe98e290/receipt.json')) == BUILDER_RECEIPT_SHA)
    return {'databases':snapshots, 'receipts':receipts, 'job_sha256':digest(job),
            'event':{'time':EVENT_TIME,'kind':'setup_required','body_sha256':hashlib.sha256(event[0][0].encode()).hexdigest()},
            'attribution':'INFERRED_PRE_ADMISSION_MISSING_PROVIDER; event does not name UUID'}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def move_exclusive(source, destination):
    """Atomic same-filesystem rename that never replaces another entry."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint]
        args = (os.fsencode(source),os.fsencode(destination),4)  # RENAME_EXCL, sys/stdio.h
    elif sys.platform.startswith('linux'):  # Real offline filesystem regression in CI.
        function = libc.renameat2
        function.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        args = (-100,os.fsencode(source),-100,os.fsencode(destination),1)  # RENAME_NOREPLACE
    else: raise AppError(ERROR)
    function.restype = ctypes.c_int
    if function(*args):
        code = ctypes.get_errno()
        raise OSError(code,os.strerror(code),str(destination))


def recover_locked(state, operation):
    approval()
    mac_glm_audit.require_idle(state)
    original = state/'jobs'/JOB/ATTEMPT
    archive = state/'recovery'/('empty-attempt-'+ATTEMPT)
    target = archive/'attempt'
    evidence_path = archive/'evidence.json'
    current = context(state)
    if archive.exists() or archive.is_symlink():
        directory(archive.parent); directory(archive)
        evidence = json.loads(mac_glm_audit.private_bytes(evidence_path))
        require(set(p.name for p in archive.iterdir()) <= {'evidence.json','attempt'})
        require(evidence['schema'] == 1 and evidence['approval'] == DECISION and
                evidence['original_path'] == str(original) and evidence['quarantine_path'] == str(target) and
                evidence['context'] == current)
    else:
        require(operation != 'restore')
        evidence = {'schema':1,'approval':DECISION,'original_path':str(original),'quarantine_path':str(target),
                    'metadata':empty_metadata(original),'context':current,'state':'PREPARED',
                    'created_at':time.time(),'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if operation == 'inspect': return {'result':'ELIGIBLE','evidence':evidence}
        private_directory(archive.parent); private_directory(archive)
        atomic_json(evidence_path, evidence); sync_directory(archive); sync_directory(archive.parent)
    require(evidence['state'] in ('PREPARED','QUARANTINED','RESTORED'))
    require(not original.is_symlink() and not target.is_symlink())
    require(original.exists() != target.exists())
    present = original if original.exists() else target
    require(empty_metadata(present) == evidence['metadata'])
    if operation == 'inspect': return {'result':evidence['state'],'evidence':evidence}
    if operation == 'restore':
        require(evidence['state'] in ('PREPARED','QUARANTINED') and present == target)
        move_exclusive(target, original)
        sync_directory(original.parent); sync_directory(archive)
        evidence['state'] = 'RESTORED'; atomic_json(evidence_path, evidence); sync_directory(archive)
        return {'result':'RESTORED','original_path':str(original),'evidence_path':str(evidence_path)}
    require(operation == 'quarantine' and evidence['state'] != 'RESTORED')
    pending = evidence['state'] == 'PREPARED'
    try:
        if present == original:
            require(evidence['state'] == 'PREPARED')
            require(not target.exists() and not target.is_symlink())
            move_exclusive(original, target)
            sync_directory(original.parent); sync_directory(archive)
        require(empty_metadata(target) == evidence['metadata'])
        require(context(state) == current)
        # The original installer guard is authoritative and is never relaxed.
        install.idle_database(state)
    except BaseException:
        if pending and not original.exists() and empty_metadata(target) == evidence['metadata']:
            move_exclusive(target, original); sync_directory(original.parent); sync_directory(archive)
        raise
    evidence['state'] = 'QUARANTINED'; atomic_json(evidence_path, evidence); sync_directory(archive)
    return {'result':'QUARANTINED','original_path':str(original),'quarantine_path':str(target),
            'evidence_path':str(evidence_path),'evidence_sha256':hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            'installer_guard':'PASS','ledgers_unchanged':True,'receipts_unchanged':True}


def command(operation):
    require(sys.platform == 'darwin')
    home = Path.home().absolute()
    state = install.safe_path(home/'Library/Application Support/AIOPS',home)
    app = install.safe_path(home/'Applications/AIOPS.app',home)
    plist = install.safe_path(home/'Library/LaunchAgents/local.aiops.mac.plist',home)
    installer = install.safe_path(home/'Library/Application Support/AIOPS Installer',home)
    directory(state); directory(installer)
    path = installer/'installer.lock'
    install.owned_file(path, True)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    service_fd = None
    try:
        install.owned_descriptor(fd,path,True)
        try: fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc: raise AppError('INSTALLER_ALREADY_RUNNING') from exc
        config = install.installed_binding(app,state,plist)
        require(not install.loaded_service('gui/'+str(os.getuid()),config,state))
        service_fd = install.fence(state)
        require(not install.loaded_service('gui/'+str(os.getuid()),config,state))
        install.safe_path(state/'jobs'/JOB/ATTEMPT,home)
        install.safe_path(state/'recovery'/('empty-attempt-'+ATTEMPT),home)
        return recover_locked(state,operation)
    finally:
        if service_fd is not None: os.close(service_fd)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('inspect','quarantine','restore'))
    args = parser.parse_args()
    try: print(encoded(command(args.operation)))
    except (AppError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        print(encoded({'error':getattr(exc,'code',ERROR)})); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
