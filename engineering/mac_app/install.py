#!/usr/bin/env python3
"""Transactional per-user app installation; preserves credentials and job ownership."""
from __future__ import annotations

import argparse
from contextlib import closing
import fcntl
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

if __name__ == '__main__':
    sys.dont_write_bytecode = True

from common import AppError, JOB_RECORD_LIMIT, VERSION, digest, encoded, parse_json, private_directory
from core import service_lock

LABEL = 'local.aiops.mac'
MANIFEST = 'install-manifest.json'


def safe_path(path, home):
    """Reject external destinations and symlink ancestors before any mkdir/write."""
    path = Path(path).expanduser().absolute()
    home = Path(home).absolute()
    if '..' in path.parts: raise AppError('UNSAFE_INSTALL_PATH')
    if path == home or home not in path.parents:
        raise AppError('PER_USER_PATH_REQUIRED')
    components = [home]
    current = home
    for part in path.relative_to(home).parts:
        current = current / part; components.append(current)
    for current in components:
        if current.is_symlink(): raise AppError('UNSAFE_INSTALL_PATH')
        if current.exists():
            info = current.lstat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise AppError('UNSAFE_INSTALL_PATH')
    return path


def owned_info(info, private=False):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
            info.st_mode & (0o077 if private else 0o022)):
        raise AppError('UNSAFE_INSTALLED_FILE')


def owned_file(path, private=False):
    owned_info(Path(path).lstat(), private)


def owned_descriptor(fd, path, private=False):
    info = os.fstat(fd)
    owned_info(info, private)
    current = Path(path).lstat()
    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
        raise AppError('UNSAFE_INSTALLED_FILE')


def service_logs(state, *, create=False):
    # launchd opens these leaves itself. Refuse links and external writes before
    # registration, and precreate absent files without following a raced link.
    for name in ('service.log', 'service-errors.log'):
        path = state / name
        if path.exists() or path.is_symlink(): owned_file(path)
        if create:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try: owned_descriptor(fd, path)
            finally: os.close(fd)


def launcher_text(arguments, *, legacy=False):
    return '#!/bin/sh\n' + ('' if legacy else 'export PYTHONDONTWRITEBYTECODE=1\n') + 'exec ' + ' '.join(shlex.quote(v) for v in [*arguments[:-1], 'open']) + '\n'


def installed_binding(app, state, plist):
    if not app.is_dir() or not plist.exists(): raise AppError('INSTALLATION_INCOMPLETE')
    owned_file(plist, True)
    with plist.open('rb') as stream: config = plistlib.load(stream)
    args = config.get('ProgramArguments')
    entry = str(app / 'Contents/Resources/aiops.py')
    if (config.get('Label') != LABEL or not isinstance(args, list) or len(args) != 5 or
            any(not isinstance(x, str) for x in args) or args[1:] != [entry, '--data-dir', str(state), 'serve'] or
            not Path(args[0]).is_absolute() or config.get('StandardOutPath') != str(state / 'service.log') or
            config.get('StandardErrorPath') != str(state / 'service-errors.log')):
        raise AppError('INSTALLATION_BINDING_MISMATCH')
    for item in (app, *app.rglob('*')):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise AppError('UNSAFE_INSTALLED_FILE')
        if not stat.S_ISDIR(info.st_mode): owned_file(item)
    with (app / 'Contents/Info.plist').open('rb') as stream: info = plistlib.load(stream)
    if info.get('CFBundleIdentifier') != LABEL or info.get('CFBundleExecutable') != 'AIOPS':
        raise AppError('INSTALLATION_BINDING_MISMATCH')
    if (app / 'Contents/MacOS/AIOPS').read_text() not in (launcher_text(args), launcher_text(args, legacy=True)):
        raise AppError('INSTALLATION_BINDING_MISMATCH')
    resources = app / 'Contents/Resources'
    manifest = resources / MANIFEST
    source = Path(__file__).resolve().parent
    allowed = {p.name for p in source.glob('*.py')} | {'ui', 'projects.json', MANIFEST}
    cache = resources / '__pycache__'
    if not manifest.exists() and cache.exists():
        allowed.add('__pycache__')
        if not cache.is_dir(): raise AppError('UNEXPECTED_INSTALLED_CONTENT')
        for item in cache.iterdir():
            match = re.fullmatch(r'([A-Za-z0-9_]+)\.cpython-\d{2,3}(?:\.opt-[12])?\.pyc', item.name)
            if not match or not (resources / (match.group(1) + '.py')).is_file():
                raise AppError('UNEXPECTED_INSTALLED_CONTENT')
            owned_file(item)
    if any(item.name not in allowed for item in resources.iterdir()):
        raise AppError('UNEXPECTED_INSTALLED_CONTENT')
    ui_files = {str(p.relative_to(source / 'ui')) for p in (source / 'ui').rglob('*') if p.is_file()}
    if {str(p.relative_to(resources / 'ui')) for p in (resources / 'ui').rglob('*') if p.is_file()} != ui_files:
        raise AppError('UNEXPECTED_INSTALLED_CONTENT')
    if (set(p.name for p in app.iterdir()) != {'Contents'} or
            set(p.name for p in (app / 'Contents').iterdir()) != {'Resources', 'MacOS', 'Info.plist'} or
            set(p.name for p in (app / 'Contents/MacOS').iterdir()) != {'AIOPS'}):
        raise AppError('UNEXPECTED_INSTALLED_CONTENT')
    if manifest.exists():
        value = parse_json(manifest.read_text())
        if (not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('application') != str(app) or
                value.get('data_directory') != str(state) or value.get('python') != args[0] or
                not isinstance(value.get('files'), dict)):
            raise AppError('INSTALLATION_MANIFEST_MISMATCH')
        actual = file_hashes(app, exclude={'Contents/Resources/' + MANIFEST})
        if value['files'] != actual: raise AppError('INSTALLATION_MANIFEST_MISMATCH')
    return config


def file_hashes(app, exclude=()):
    return {str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(app.rglob('*')) if p.is_file() and str(p.relative_to(app)) not in exclude}


def quiescent_generation_delivery(db,state,job,tables):
    """Prove one blocked new-generation lineage, without releasing its ownership."""
    import mac_generation
    import native_transfer
    from mac_authority import LocalSource, require
    inspected=job['state']=='accepted'
    require(job['state'] in ('needs_user','paused','accepted') and job.get('attempt') is None,'UPDATE_BUSY')
    require({'mac_host_generations','mac_host_tasks','mac_host_attempts','native_local','mac_host_deliveries'} <= tables,
            'UPDATE_BUSY')
    lineage=job.get('native_lineage'); require(isinstance(lineage,dict),'UPDATE_BUSY')
    bound=native_transfer.binding(lineage['binding'])
    require('generation_id' in bound and job.get('generation_policy')==mac_generation.POLICY,'UPDATE_BUSY')
    rid,aid=lineage['request_id'],lineage['attempt_id']
    require(all(isinstance(v,str) and re.fullmatch(r'[0-9a-f]{32}',v) for v in (rid,aid)),'UPDATE_BUSY')
    generation=db.execute('SELECT document FROM mac_host_generations WHERE id=?',(bound['generation_id'],)).fetchone()
    task=db.execute('SELECT binding,work,state FROM mac_host_tasks WHERE repository=? AND task=?',
                    (bound['repository'].lower(),bound['task_id'])).fetchone()
    native=db.execute('SELECT document,terminal,state FROM mac_host_attempts WHERE request=?',(rid,)).fetchone()
    local=db.execute('SELECT document,state FROM native_local WHERE request=?',(rid,)).fetchone()
    delivery=db.execute('SELECT job,document,state FROM mac_host_deliveries WHERE request=?',(rid,)).fetchone()
    require(all((generation,task,native,local,delivery)),'UPDATE_BUSY')
    document=parse_json(generation[0],4*1024*1024); work=parse_json(task[1],1024*1024)
    request=parse_json(native[0],1024*1024)['request']; terminal=parse_json(native[1])
    local_record=parse_json(local[0],1024*1024); delivery_document=parse_json(delivery[1])
    require(document['binding']==parse_json(task[0])==bound and document['policy']==mac_generation.POLICY and
            digest(work)==bound['work_sha256'] and work['generation_policy']==mac_generation.POLICY and
            work['generation_decision']==job.get('generation_decision') and
            job['base_sha']==bound['plan_commit'] and job['program_scope']['blob']==bound['plan_blob'] and
            job['branch']=='aiops/native-'+rid[:16] and job['settings']['roles']['builder']==work['profile'] and
            job['settings']['publish_pr'] is True and len(job['plan']['tasks'])==1 and
            job['plan']['tasks'][0]['id']==work['task']['id'] and
            job['plan']['tasks'][0]['instructions']==work['task']['spec'] and
            native[2]==local[1]=='TERMINAL' and task[2]==delivery[2]==('INSPECTED' if inspected else 'DELIVERING') and
            request['binding']==local_record['binding']==bound and request['attempt']['id']==aid and
            local_record['attempt']==request['attempt'] and local_record['terminal']==terminal and
            delivery[0]==job['id'] and delivery_document['lineage']==lineage and
            delivery_document['terminal_sha256']==digest(terminal),'UPDATE_BUSY')
    # Construct no Store/LocalSource: this precheck must never migrate SQLite.
    reader=object.__new__(LocalSource)
    root=Path(state).resolve(); checkout=root/'native'/rid/aid/'checkout'
    folders=[checkout.parent]
    job_folder=root/'jobs'/job['id']
    if job_folder.is_symlink(): raise AppError('UPDATE_BUSY')
    if job_folder.exists():
        for item in job_folder.iterdir():
            if item.is_symlink(): raise AppError('UPDATE_BUSY')
            if item.is_dir():
                require(re.fullmatch(r'[0-9a-f]{32}',item.name) is not None,'UPDATE_BUSY')
                folders.append(item)
    require(type(job['calls']) is int and len(folders)==job['calls'],'UPDATE_BUSY')
    receipts={}
    for folder in folders:
        private=reader._private_json(folder/'request.json')
        require(private['host_directory']==str(root) and private['checkout']==str(checkout) and
                private['attempt_id']==folder.name,'UPDATE_BUSY')
        if folder==checkout.parent:
            require(private['binding']==request['attempt']['binding'],'UPDATE_BUSY')
        receipt=reader.private_receipt(folder,{'id':private['attempt_id'],'binding':private['binding']},
                                       terminal['result_sha256'] if folder==checkout.parent else None)
        receipts[private['attempt_id']]=receipt
        running=folder/'running.json'
        if running.exists():
            pid=reader._private_json(running).get('pid')
            require(type(pid) is int and pid>1,'UPDATE_BUSY')
            try: os.killpg(pid,0)
            except ProcessLookupError: pass
            except OSError: raise AppError('UPDATE_BUSY') from None
            else: raise AppError('UPDATE_BUSY')
    if inspected:
        proof=delivery_document.get('inspection') or {}
        require(job.get('accepted_head')==job['head']==proof.get('head') and
                job.get('accepted_at')==proof.get('user_inspected_at') and
                proof.get('task_revision')==bound['task_revision'] and
                proof.get('review_sha256')==digest(job.get('review')) and
                proof.get('supervision_sha256')==digest(job.get('supervision')) and
                proof.get('ci_sha256')==digest(job.get('ci')) and
                job['ci']['state']=='passed' and job['ci']['head']==job['head'] and
                job['ci']['source']=='GITHUB_ACTIONS_API','UPDATE_BUSY')
        actors=set(job.get('builder_sessions',[]))
        for role in ('review','supervision'):
            evidence=job[role]; receipt=receipts.get(evidence['attempt']) or {}
            actor=(receipt.get('provider_evidence') or {}).get('session_id')
            require(evidence['head']==job['head'] and receipt.get('exit_code')==0 and
                    receipt.get('error') is None and receipt.get('report')==evidence['report'] and
                    receipt.get('provider_evidence')==evidence['provider_evidence'] and
                    receipt['report']['status']=='complete' and actor and actor not in actors,'UPDATE_BUSY')
            actors.add(actor)
    return (bound['repository'].lower(),bound['task_id'])


def idle_database(state):
    """Read existing state only. Both the reject-only precheck and fenced check use this."""
    database = state / 'app.sqlite3'
    if database.is_symlink(): raise AppError('UNSAFE_INSTALLED_FILE')
    if not database.exists():
        if any((state / name).exists() for name in ('jobs', 'workspaces', 'endpoint.json', 'desktop-token', 'relay-token')):
            raise AppError('INSTALLATION_STATE_UNVERIFIED')
        return
    owned_file(database, True)
    for suffix in ('-wal', '-shm', '-journal'):
        path = Path(str(database) + suffix)
        if path.exists() or path.is_symlink(): owned_file(path, True)
    # A read-only WAL connection must not create a missing shared-memory file.
    if Path(str(database) + '-wal').exists() and not Path(str(database) + '-shm').exists():
        raise AppError('INSTALLATION_STATE_UNVERIFIED')
    uri = database.as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise AppError('INSTALLATION_STATE_UNVERIFIED')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if not {'settings', 'jobs', 'events'} <= tables or columns != {'id', 'request_id', 'repository', 'state', 'document', 'created'}:
                raise AppError('INSTALLATION_STATE_UNVERIFIED')
            blocked_tasks=set()
            for key, state_name, document in db.execute('SELECT id,state,document FROM jobs'):
                job = parse_json(document, JOB_RECORD_LIMIT)
                if not isinstance(job, dict) or job.get('id') != key or job.get('state') != state_name or 'attempt' not in job:
                    raise AppError('INSTALLATION_STATE_UNVERIFIED')
                if state_name not in ('accepted', 'cancelled') or job.get('attempt') is not None:
                    try: blocked_tasks.add(quiescent_generation_delivery(db,state,job,tables))
                    except (AppError,KeyError,TypeError,ValueError,OSError):
                        raise AppError('UPDATE_BUSY', '작업을 완료·검수하거나 안전하게 취소한 뒤 업데이트해 주세요. 대기·일시정지·실행 불명 작업은 유지합니다.') from None
                elif state_name=='accepted' and job.get('native_lineage'):
                    lineage=job['native_lineage']['binding']
                    row=db.execute('SELECT state FROM mac_host_tasks WHERE repository=? AND task=?',
                                   (lineage['repository'].lower(),lineage['task_id'])).fetchone()
                    if row and row[0]=='INSPECTED':
                        try: blocked_tasks.add(quiescent_generation_delivery(db,state,job,tables))
                        except (AppError,KeyError,TypeError,ValueError,OSError): raise AppError('UPDATE_BUSY') from None
            for table,allowed in (('native_local',('TERMINAL',)),('mac_host_attempts',('TERMINAL',)),
                                  ):
                if table in tables:
                    marks=','.join('?' for _ in allowed)
                    if db.execute('SELECT 1 FROM '+table+' WHERE state NOT IN ('+marks+') LIMIT 1',allowed).fetchone():
                        raise AppError('UPDATE_BUSY','Mac canonical 작업의 실행·검토·병합 근거가 미완료입니다. 원장과 산출물을 보존합니다.')
            if 'mac_host_tasks' in tables:
                for repo,task,state_name in db.execute('SELECT repository,task,state FROM mac_host_tasks'):
                    if state_name not in ('READY','ACCEPTED') and not(state_name in ('DELIVERING','INSPECTED') and (repo,task) in blocked_tasks):
                        raise AppError('UPDATE_BUSY','Mac canonical 작업의 실행·검토·병합 근거가 미완료입니다. 원장과 산출물을 보존합니다.')
    except sqlite3.Error as exc:
        raise AppError('INSTALLATION_STATE_UNVERIFIED') from exc


def launchctl(*args, check=True):
    return subprocess.run(['/bin/launchctl', *args], check=check, capture_output=True, text=True, timeout=15)


def interpreter_process_path(interpreter):
    # Framework Python execs its Python.app binary. Observe that representation
    # from the exact configured interpreter in isolation, never from PATH or a
    # guessed alias. The full service argv must still match below.
    script = ('import os, subprocess; '
              'print(subprocess.check_output(["/bin/ps", "-p", str(os.getpid()), '
              '"-o", "comm="], text=True).strip())')
    result = subprocess.run([interpreter, '-I', '-S', '-c', script],
                            env={'PATH': '/usr/bin:/bin'}, check=False,
                            capture_output=True, text=True, timeout=5)
    path = result.stdout.strip()
    if result.returncode != 0 or not Path(path).is_absolute() or '\n' in path or '\r' in path:
        raise AppError('INSTALLATION_BINDING_MISMATCH')
    return path


def loaded_service(domain, config, state):
    result = launchctl('print', domain + '/' + LABEL, check=False)
    if result.returncode != 0:
        if re.search(r'could not find (?:specified )?service|service not found|no such process', result.stderr.lower()):
            return False
        raise AppError('INSTALLER_SERVICE_STATE_UNKNOWN')
    # A first install has no trusted registration. Existence alone refuses it;
    # never bootstrap over or bootout another service with the fixed label.
    if config is None: return True
    output = result.stdout
    printed = re.search(r'^\s*arguments\s*=\s*\{\s*\n(.*?)^\s*\}', output, re.MULTILINE | re.DOTALL)
    arguments = [re.sub(r'^\d+\s*=\s*', '', line.strip()) for line in printed.group(1).splitlines() if line.strip()] if printed else None
    if arguments != config['ProgramArguments']:
        raise AppError('INSTALLATION_BINDING_MISMATCH')
    match = re.search(r'^\s*pid\s*=\s*(\d+)\s*$', output, re.MULTILINE)
    if match:
        endpoint = state / 'endpoint.json'
        owned_file(endpoint, True)
        value = parse_json(endpoint.read_text())
        if not isinstance(value, dict) or value.get('pid') != int(match.group(1)):
            raise AppError('INSTALLATION_BINDING_MISMATCH')
        result = subprocess.run(['/bin/ps', '-p', match.group(1), '-o', 'command='],
                                check=False, capture_output=True, text=True, timeout=5)
        # ps renders argv with spaces but does not shell-quote paths containing
        # spaces. Compare its full expected rendering, never split that output.
        if result.returncode != 0:
            raise AppError('INSTALLATION_BINDING_MISMATCH')
        arguments = config['ProgramArguments']
        if result.stdout.strip() != ' '.join(arguments):
            observed = interpreter_process_path(arguments[0])
            if result.stdout.strip() != ' '.join([observed, *arguments[1:]]):
                raise AppError('INSTALLATION_BINDING_MISMATCH')
    return True


def fence(state, wait=False):
    deadline = time.monotonic() + (5 if wait else 0)
    while True:
        lock = state / 'service.lock'
        if lock.exists(): owned_file(lock, True)
        try:
            fd = service_lock(state)
            try: owned_descriptor(fd, lock, True)
            except BaseException:
                os.close(fd); raise
            return fd
        except AppError as exc:
            if exc.code != 'SERVICE_ALREADY_RUNNING' or time.monotonic() >= deadline: raise
            time.sleep(0.1)


def stage_bundle(stage, app, state):
    resources = stage / 'Contents/Resources'; executable = stage / 'Contents/MacOS'
    for directory in (stage / 'Contents', resources, executable): directory.mkdir(mode=0o700)
    source = Path(__file__).resolve().parent
    for item in source.glob('*.py'):
        if item.is_symlink(): raise AppError('UNSAFE_SOURCE_FILE')
        shutil.copy2(item, resources / item.name)
    shutil.copytree(source / 'ui', resources / 'ui', symlinks=True)
    registry = source.parent / '.github/control-plane/projects.json'
    if registry.exists(): shutil.copy2(registry, resources / 'projects.json')
    info = {'CFBundleName': 'AIOPS', 'CFBundleDisplayName': 'AIOPS', 'CFBundleIdentifier': LABEL,
            'CFBundleVersion': VERSION, 'CFBundleShortVersionString': VERSION,
            'CFBundleExecutable': 'AIOPS', 'CFBundlePackageType': 'APPL', 'LSUIElement': True}
    (stage / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    arguments = [str(Path(sys.executable).resolve()), str(app / 'Contents/Resources/aiops.py'), '--data-dir', str(state), 'serve']
    (executable / 'AIOPS').write_text(launcher_text(arguments)); (executable / 'AIOPS').chmod(0o755)
    for item in stage.rglob('*'):
        if item.is_symlink() or not (item.is_file() or item.is_dir()): raise AppError('UNSAFE_SOURCE_FILE')
        if item.is_file(): item.chmod(0o755 if item == executable / 'AIOPS' else 0o600)
        else: item.chmod(0o700)
    value = {'schema_version': 1, 'version': VERSION, 'application': str(app), 'data_directory': str(state),
             'python': arguments[0], 'files': file_hashes(stage)}
    (resources / MANIFEST).write_text(encoded(value) + '\n'); (resources / MANIFEST).chmod(0o600)
    return {'Label': LABEL, 'ProgramArguments': arguments, 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 10, 'ProcessType': 'Background', 'Umask': 0o077,
            'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'), 'PYTHONDONTWRITEBYTECODE': '1'},
            'StandardOutPath': str(state / 'service.log'), 'StandardErrorPath': str(state / 'service-errors.log')}


def install(destination=None, data=None, *, update=False):
    if sys.platform != 'darwin': raise AppError('MAC_REQUIRED', '설치는 Mac에서 실행해 주세요.')
    if sys.version_info < (3, 10): raise AppError('PYTHON_310_REQUIRED')
    home = Path.home().absolute()
    app = safe_path(destination or home / 'Applications/AIOPS.app', home)
    state = safe_path(data or home / 'Library/Application Support/AIOPS', home)
    agents = safe_path(home / 'Library/LaunchAgents', home)
    plist = safe_path(agents / (LABEL + '.plist'), home)
    installer_state = safe_path(home / 'Library/Application Support/AIOPS Installer', home)
    private_directory(installer_state)
    lock_path = installer_state / 'installer.lock'
    if lock_path.exists() or lock_path.is_symlink(): owned_file(lock_path, True)
    global_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        owned_descriptor(global_fd, lock_path, True)
        try: fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc: raise AppError('INSTALLER_ALREADY_RUNNING') from exc
        # The fixed app/plist/label are shared even when --data-dir differs.
        # Re-read them only after this per-user transaction lock is admitted.
        return install_locked(app, state, agents, plist, update=update)
    finally: os.close(global_fd)


def install_locked(app, state, agents, plist, *, update):
    if app in state.parents or state in app.parents or app == state: raise AppError('UNSAFE_INSTALL_PATH')
    if app.exists() and not update: raise AppError('ALREADY_INSTALLED', '이미 설치되어 있습니다. 작업을 마친 뒤 --update로 업데이트해 주세요.')
    if update and not app.exists(): raise AppError('NOT_INSTALLED')
    if not update and plist.exists(): raise AppError('EXISTING_LAUNCH_AGENT')
    if not shutil.which('gh'): raise AppError('GITHUB_CLI_REQUIRED', '먼저 GitHub CLI(gh)를 설치해 주세요.')
    if not shutil.which('git'): raise AppError('GIT_REQUIRED')
    old_config = installed_binding(app, state, plist) if update else None
    state = private_directory(state)
    service_logs(state)
    # Reject known busy jobs before stopping anything. This is not an admission
    # decision: repeat the read only after the service lock has fenced writers.
    idle_database(state)
    domain = 'gui/' + str(os.getuid())
    was_loaded = loaded_service(domain, old_config, state)
    if was_loaded and not update: raise AppError('EXISTING_LAUNCH_AGENT')
    stopped = False; fd = None; stage = None; prior = None; swapped = False; published = False
    old_plist = plist.read_bytes() if update else None
    try:
        if was_loaded:
            launchctl('bootout', domain + '/' + LABEL); stopped = True
        fd = fence(state, wait=stopped)
        idle_database(state)
        service_logs(state, create=True)
        app.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        agents.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.aiops-stage-', dir=app.parent))
        config = stage_bundle(stage, app, state)
        staged_plist = stage.parent / (stage.name + '.plist')
        staged_plist.write_bytes(plistlib.dumps(config)); staged_plist.chmod(0o600)
        # A foreign caller may register the same label despite our installer
        # lock. Recheck immediately before publishing any files.
        if loaded_service(domain, None, state): raise AppError('INSTALLER_SERVICE_STATE_CHANGED')
        if update:
            installed_binding(app, state, plist)
            if plist.read_bytes() != old_plist: raise AppError('INSTALLATION_BINDING_MISMATCH')
        elif app.exists() or plist.exists(): raise AppError('INSTALLATION_BINDING_MISMATCH')
        if update:
            prior = stage.parent / (stage.name + '.previous')
            os.rename(app, prior)
        try:
            os.rename(stage, app); stage = None; swapped = True
        except BaseException:
            if prior is not None: os.rename(prior, app); prior = None
            raise
        os.replace(staged_plist, plist); published = True
        # launchd's service needs the same exclusive lock. File replacement and
        # the authoritative no-owner check have completed before releasing it.
        os.close(fd); fd = None
        service_logs(state)
        # Updating files must preserve a user's stopped service. In particular,
        # bootstrap would undo bootout or fail against a disabled launchd label.
        # A fresh installation still starts normally; a running update resumes
        # only the service that this transaction stopped.
        if not update or was_loaded:
            launchctl('bootstrap', domain, str(plist))
    except BaseException:
        if swapped:
            # A bootstrap may have registered the narrow label before failing.
            # Bootout that label only; no broad PID matching or provider kills.
            try:
                if loaded_service(domain, config, state): launchctl('bootout', domain + '/' + LABEL)
            except (AppError, OSError, ValueError, subprocess.SubprocessError) as exc:
                raise AppError('INSTALLER_ROLLBACK_BLOCKED', '서비스의 설치 binding을 확인할 수 없어 앱 파일을 보존했습니다.') from exc
            if fd is None:
                # Never remove an installed tree while a possibly spawned
                # service still holds its fence. Preserve it for inspection.
                fd = fence(state, wait=True)
            # A partially successful bootstrap could have admitted a new job
            # before reporting an error. Preserve its code and ownership.
            try: idle_database(state)
            except AppError as exc:
                raise AppError('INSTALLER_ROLLBACK_BLOCKED', '새 서비스의 작업 상태가 확실하지 않아 앱 파일을 보존했습니다. 작업 소유권을 먼저 확인해 주세요.') from exc
            shutil.rmtree(app)
            if prior is not None: os.rename(prior, app); prior = None
        if published:
            if old_plist is None: plist.unlink()
            else:
                plist.write_bytes(old_plist); plist.chmod(0o600)
        if fd is not None: os.close(fd); fd = None
        if stopped:
            try: launchctl('bootstrap', domain, str(plist))
            except (OSError, subprocess.SubprocessError) as exc:
                raise AppError('INSTALLER_RESTART_REQUIRED', '기존 앱 파일은 보존했습니다. 기존 AIOPS LaunchAgent를 다시 시작해 주세요.') from exc
        raise
    finally:
        if fd is not None: os.close(fd)
        if stage is not None: shutil.rmtree(stage)
        if 'staged_plist' in locals() and staged_plist.exists(): staged_plist.unlink()
        if prior is not None and not app.exists(): os.rename(prior, app)
    if prior is not None: shutil.rmtree(prior)
    print(('업데이트했습니다: ' if update else '설치했습니다: ') + str(app))
    if update and not was_loaded:
        print('기존 AIOPS 서비스는 중지 상태로 유지했습니다. 자동 시작 설정은 변경하지 않았습니다.')
    print('GitHub와 선택한 CLI의 로그인을 마친 뒤 AIOPS.app을 여세요. 모델 연결은 앱에서 확인할 수 있습니다.')
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--destination'); parser.add_argument('--data-dir')
    parser.add_argument('--update', action='store_true', help='완료·취소된 작업만 있는 기존 설치를 안전하게 업데이트합니다.')
    args = parser.parse_args()
    try: install(args.destination, args.data_dir, update=args.update)
    except (AppError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(2)
