"""Read-only, conservative fences for the Mac relay's transport receipts.

These receipts do not identify authoritative task ownership or prove terminal
execution. The legacy journal does not retain repository/task bindings, so an
unresolved row fences every new native launch. This is never host admission.
"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import stat

from common import AppError, parse_json


def unresolved(directory, *, include_digest=False):
    folder = Path(directory) / 'relay'
    database = folder / 'requests.sqlite3'
    try:
        if not folder.exists() and not folder.is_symlink(): return []
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe journal directory')
        if not database.exists() and not database.is_symlink():
            if any(folder.iterdir()): raise ValueError('missing journal database')
            return []
        info = database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('unsafe journal database')
        identity = (info.st_dev, info.st_ino)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                item = sidecar.lstat()
                if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid() or
                        item.st_mode & 0o077 or item.st_nlink != 1):
                    raise ValueError('unsafe journal sidecar')
        if Path(str(database) + '-wal').exists() and not Path(str(database) + '-shm').exists():
            raise ValueError('read would create shared memory')
        fences = []
        with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute('SELECT id,digest,state,receipt FROM requests LIMIT 4097').fetchall()
            if len(rows) > 4096: raise ValueError('unbounded journal')
            for request_id, digest, state, raw in rows:
                if not isinstance(raw, str): raise ValueError('invalid receipt encoding')
                receipt = parse_json(raw, 65536)
                if (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', request_id)
                        or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                        or state not in ('UNKNOWN', 'SUBMITTED') or not isinstance(receipt, dict)
                        or receipt.get('request_id') != request_id or receipt.get('payload_sha256') != digest
                        or receipt.get('state') != state or receipt.get('task_completion') != 'NOT_CHECKED'):
                    raise ValueError('unverified transport receipt')
                fences.append({'request_id': request_id, 'state': state, **({'payload_sha256':digest} if include_digest else {})})
        current = database.lstat()
        if identity != (current.st_dev, current.st_ino): raise ValueError('journal changed identity')
        return fences
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        raise AppError('TRANSPORT_JOURNAL_UNVERIFIED', '기존 전달 원장을 읽기 전용으로 확인할 수 없어 새 실행을 보류합니다.') from exc


def require_clear(directory):
    fences = unresolved(directory)
    if fences:
        raise AppError('TRANSPORT_EXECUTION_UNRESOLVED',
                       '기존 시작 전달의 실제 실행·종료가 미확인입니다: ' + fences[0]['request_id'] +
                       '. 새 요청 ID로 재전송하거나 Mac 모델 실행을 시작할 수 없습니다. 원본 영수증을 보존하고 인증된 대사 근거가 필요합니다.')
