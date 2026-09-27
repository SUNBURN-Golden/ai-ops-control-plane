#!/usr/bin/python3 -I
"""Cursor CLI builder adapter. Install only after separate host qualification.

The public wrapper runs as the control UID; its fixed worker runs as CURSOR's
dedicated UID via sudo. It uses a lingering systemd user manager, never nohup or
an Actions child as the durable owner. Default/example policy is disabled.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import signal
import subprocess
import sys

INSTALLED = Path('/opt/astra/bin/astra-builder-cursor')
POLICY = Path('/etc/astra/cursor-adapter.json')
IDENTITY = ('repository', 'task_id', 'task_revision', 'builder_id', 'launch_request_id', 'attempt_id')
REQUIRED_PROOFS = ('authentication', 'durable_session', 'credential_isolation',
                   'duplicate_unknown', 'trusted_workflow_boundary', 'quota_policy')


class CursorError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise CursorError(message)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def decode(raw):
    def pairs(values):
        out = {}
        for key, value in values:
            require(key not in out, 'duplicate JSON key')
            out[key] = value
        return out
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(CursorError('invalid JSON number')))
    require(isinstance(value, dict), 'expected one JSON object')
    return value


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'unsafe protected path')
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        require(info.st_uid == 0 and not info.st_mode & 0o022 and
                (stat.S_ISDIR(info.st_mode) if current != path else stat.S_ISREG(info.st_mode)),
                'unprotected adapter configuration or executable')
    require(path.stat().st_nlink == 1, 'hard-linked protected file')


def validate_policy(p):
    require(p.get('enabled') is True and p.get('independent_audit') == 'PASS', 'adapter not approved')
    require(type(p.get('control_uid')) is int and type(p.get('builder_uid')) is int and
            0 < p['control_uid'] != p['builder_uid'] > 0, 'distinct nonroot UIDs required')
    require(re.fullmatch(r'[a-z_][a-z0-9_-]*', p.get('builder_user', '')), 'invalid builder user')
    model = p.get('model', '')
    require(isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', model)
            and model.upper() not in {'AUTO', 'DEFAULT', 'PENDING', 'CONFIG_REQUIRED'}, 'exact model required')
    require(p.get('billing') == 'INCLUDED_ONLY', 'additional billing not authorized')
    require(p.get('headless_write_authorized') is True, 'headless writer permission not qualified')
    require(all(p.get('checks', {}).get(k) == 'PASS' for k in REQUIRED_PROOFS), 'host qualification missing')
    require(re.fullmatch(r'https://github\.com/[^/\s]+/[^/\s]+/(issues|pull)/[1-9][0-9]*(#[^\s]+)?',
                         p.get('evidence_pointer', '')), 'qualification evidence missing')
    for key in ('home', 'workspace_root', 'custodian_root', 'cli'):
        v = p.get(key, '')
        require(isinstance(v, str) and re.fullmatch(r'/[A-Za-z0-9_./-]+', v) and '..' not in Path(v).parts,
                'invalid configured path')
    repos = p.get('repositories')
    require(isinstance(repos, list) and repos and all(
        isinstance(r, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', r) for r in repos),
        'repository allowlist required')
    require(isinstance(p.get('auth_match'), dict) and bool(p['auth_match']) and
            all(isinstance(k, str) and isinstance(v, (str, bool)) for k, v in p['auth_match'].items()),
            'exact nonsecret installed status fields required')


def run(argv, env, *, cwd='/', input=None):
    return subprocess.run(argv, input=input if input is not None else '', env=env, cwd=cwd, text=True,
                          capture_output=True, check=True, timeout=90)


def authenticated_input(control_uid, fd=0):
    """Kernel-owned anonymous read pipe, not SUDO_UID or other caller text."""
    info = os.fstat(fd)
    require(stat.S_ISFIFO(info.st_mode) and info.st_uid == control_uid and
            os.readlink('/proc/self/fd/' + str(fd)).startswith('pipe:[') and
            fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY,
            'worker requires a control-owned anonymous input pipe')


def supervised_send(argv, raw, p, fence_fd):
    """Detached control-UID custodian: preserve fence through wrapper death.

    An unsuccessful sender keeps its fence indefinitely. An operator must fence
    descendants and reconcile before terminating that custodian; never auto-expire.
    The builder gets only a read pipe, never the admission descriptor.
    """
    info = os.fstat(fence_fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == p['control_uid'], 'invalid admission fence')
    root = Path(p['custodian_root']); ri = root.lstat()
    require(root.resolve() == root and stat.S_ISDIR(ri.st_mode) and
            ri.st_uid == p['control_uid'] and not ri.st_mode & 0o077, 'unprotected custodian directory')
    digest = hashlib.sha256(raw.encode()).hexdigest()
    record = root / (digest + '.json')
    # This control-owned marker prevents a second sender even if provider result
    # delivery was lost. A new approved attempt uses a different packet.
    with record.open('x') as out:
        out.write(encode({'state': 'SUBMITTING', 'packet_digest': digest})); out.flush(); os.fsync(out.fileno())
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid:
        os.close(write_fd)
        with os.fdopen(read_fd) as response:
            result = response.read(1048577)
        require(len(result) <= 1048576, 'sender response too large')
        value = decode(result)
        require(value.get('outcome') == 'CONFIRMED', 'sender unresolved; custodian retains fence')
        return value
    os.close(read_fd)
    try:
        os.setsid()
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        # Only the custodian holds this FD. sudo/subprocess close_fds stays true.
        os.set_inheritable(fence_fd, False)
        with open(os.devnull, 'r+b', buffering=0) as null:
            for fd in (0, 1, 2): os.dup2(null.fileno(), fd)
        state = {'state': 'SUBMITTING', 'pid': os.getpid(), 'packet_digest': digest,
                 'fence_device': info.st_dev, 'fence_inode': info.st_ino}
        record.write_text(encode(state))
        try:
            result = decode(run(argv, {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, input=raw).stdout)
            packet = decode(raw)
            require(result.get('outcome') == 'CONFIRMED' and all(result.get(k) == packet[k] for k in IDENTITY)
                    and isinstance(result.get('session_id'), str) and result['session_id'], 'invalid sender result')
            state.update(state='CONFIRMED', session_id=result['session_id'])
            record.write_text(encode(state))
        except BaseException:
            state['state'] = 'UNKNOWN'
            record.write_text(encode(state))
            try: os.write(write_fd, encode({'outcome': 'UNKNOWN'}).encode())
            except OSError: pass
            os.close(write_fd)
            while True: signal.pause()
        try: os.write(write_fd, encode(result).encode())
        except OSError: pass  # Sender ended; parent loss is reconciled from record.
        os._exit(0)
    except BaseException:
        # Even failed local bookkeeping must not release an uncertain send fence.
        try: os.close(write_fd)
        except OSError: pass
        while True: signal.pause()


def environment(p):
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'HOME': p['home'], 'XDG_RUNTIME_DIR': '/run/user/' + str(p['builder_uid']),
            'GIT_TERMINAL_PROMPT': '0'}


def live_preflight(p, invoke=run):
    env = environment(p)
    # Installed-status schema is pinned from observed official CLI output at qualification.
    # No guessed login success from an exit code alone; account fields must match.
    report = decode(invoke([p['cli'], 'status', '--format', 'json'], env).stdout)
    require(all(report.get(k) == v for k, v in p['auth_match'].items()), 'Cursor authentication identity changed')
    models = invoke([p['cli'], 'models'], env).stdout
    require(re.search(r'(?<![A-Za-z0-9._:/-])' + re.escape(p['model']) +
                      r'(?![A-Za-z0-9._:/-])', models), 'configured model unavailable')
    linger = invoke(['/usr/bin/loginctl', 'show-user', str(p['builder_uid']), '-p', 'Linger', '--value'], env)
    require(linger.stdout.strip() == 'yes', 'durable user manager not provisioned')
    invoke(['/usr/bin/systemctl', '--user', 'show-environment'], env)
    return {'status': 'PASS', 'builder_id': 'CURSOR', 'harness': 'CURSOR_CLI', 'model': p['model'],
            'execution_mode': 'PERSISTENT_SUPERVISOR', 'parallel_safe': True,
            'launch_contract_version': 2, 'worktree_root': p['workspace_root'],
            'evidence_pointer': p['evidence_pointer']}


def validate_packet(packet, p):
    require(packet.get('schema_version') == 1 and packet.get('builder_id') == 'CURSOR', 'wrong builder packet')
    require(packet.get('repository') in p['repositories'], 'repository not approved')
    for key in IDENTITY[:-1]:
        require(isinstance(packet.get(key), str) and 0 < len(packet[key]) <= 4096 and
                '\0' not in packet[key], 'invalid packet identity')
    require(type(packet.get('attempt_id')) is int and packet['attempt_id'] > 0, 'invalid attempt')
    for key in ('task_pointer', 'task_spec_pointer'):
        require(isinstance(packet.get(key), str) and packet[key].startswith(
                'https://github.com/' + packet['repository'] + '/') and not any(
                c.isspace() for c in packet[key]), 'task pointer outside repository')


def worker_launch(packet, p, invoke=run):
    validate_packet(packet, p)
    env = environment(p)
    live_preflight(p, invoke)
    digest = hashlib.sha256(encode(packet).encode()).hexdigest()
    root = Path(p['workspace_root'])
    # Atomic persistent directory is also the adapter's no-relaunch marker. Host
    # admission owns the global writer claim. A partial directory requires reconcile.
    directory = root / digest
    directory.mkdir(mode=0o700)
    workspace = directory / 'repo'
    unit = 'astra-cursor-' + digest[:32]
    (directory / 'packet.json').write_text(encode(packet))
    invoke(['/usr/bin/git', 'clone', '--no-hardlinks', '--no-checkout', '--',
            'https://github.com/' + packet['repository'] + '.git', str(workspace)], env)
    invoke(['/usr/bin/git', 'checkout', '-b', 'astra/cursor/' + digest[:24], 'origin/HEAD'], env, cwd=str(workspace))
    base = invoke(['/usr/bin/git', 'rev-parse', 'HEAD'], env, cwd=str(workspace)).stdout.strip()
    require(re.fullmatch(r'[0-9a-f]{40}', base), 'checkout SHA unavailable')
    # create-chat may itself create a provider session. Every subsequent failure
    # is UNKNOWN, never FAILED_PRESTART, and the persistent directory blocks retry.
    chat = invoke([p['cli'], 'create-chat'], env, cwd=str(workspace)).stdout.strip()
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}', chat), 'unrecognized provider chat ID')
    manifest = dict(packet_digest=digest, session_id='cursor-cli:' + chat, unit=unit,
                    checkout_sha=base, workspace=str(workspace), model=p['model'])
    (directory / 'session.json').write_text(encode(manifest))
    prompt = ('Own the canonical task through investigation, implementation, test/debug/retest and PR evidence. '
              'Read repository rules and the exact task revision first. Preserve approved contracts; '
              'escalate required contract changes. Never merge. Task data: ' + encode(packet))
    argv = ['/usr/bin/systemd-run', '--user', '--unit=' + unit, '--service-type=exec',
            '--property=Restart=no', '--property=RemainAfterExit=yes', '--property=KillMode=control-group',
            '--property=UMask=0077', '--property=WorkingDirectory=' + str(workspace),
            '--property=StandardOutput=append:' + str(directory / 'output.log'),
            '--property=StandardError=append:' + str(directory / 'error.log'),
            '--setenv=HOME=' + p['home'], '--setenv=PATH=' + env['PATH'], '--setenv=LANG=C.UTF-8',
            '--property=UnsetEnvironment=RUNNER_TRACKING_ID', '--', p['cli'], '--print', '--force', '--trust', '--output-format', 'json',
            '--model', p['model'], '--resume', chat, '--workspace', str(workspace), prompt]
    invoke(argv, env)
    state = invoke(['/usr/bin/systemctl', '--user', 'show', unit, '--property=LoadState,ActiveState,Result'], env)
    values = dict(line.split('=', 1) for line in state.stdout.splitlines() if '=' in line)
    require(values.get('LoadState') == 'loaded' and values.get('ActiveState') == 'active' and
            values.get('Result') == 'success', 'supervisor state unresolved')
    return {**{k: packet[k] for k in IDENTITY}, 'outcome': 'CONFIRMED', 'session_id': manifest['session_id']}


def main(argv=None):
    os.umask(0o077)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        require(Path(__file__).absolute() == INSTALLED, 'must use the protected installed adapter')
        protected(POLICY); protected(INSTALLED)
        p = decode(POLICY.read_text()); validate_policy(p)
        protected(p['cli'])
        require(sha(INSTALLED) == p.get('wrapper_sha256') and sha(Path(p['cli'])) == p.get('binary_sha256'),
                'installed adapter/binary changed')
        worker = args in (['--worker-preflight'], ['--worker-launch'])
        if worker:
            require(os.getuid() == os.geteuid() == p['builder_uid'], 'wrong worker UID')
            authenticated_input(p['control_uid'])
            for key in ('home', 'workspace_root'):
                path = Path(p[key]); info = path.lstat()
                require(path.resolve() == path and stat.S_ISDIR(info.st_mode) and
                        info.st_uid == p['builder_uid'] and not info.st_mode & 0o077,
                        'builder home/workspace must be isolated owner-only directories')
            if args == ['--worker-preflight']:
                result = live_preflight(p)
            else:
                raw = sys.stdin.read(1048577)
                require(len(raw) <= 1048576, 'packet too large')
                result = worker_launch(decode(raw), p)
            print(encode(result)); return 0
        require(os.getuid() == os.geteuid() == p['control_uid'], 'control UID required')
        require(len(args) == 1, 'expected --preflight or packet path')
        raw = None
        if args != ['--preflight']:
            path = Path(args[0]); info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == p['control_uid'] and
                    not info.st_mode & 0o077 and info.st_nlink == 1 and info.st_size <= 1048576,
                    'unsafe admission packet')
            raw = path.read_text(); validate_packet(decode(raw), p)
        mode = '--worker-preflight' if raw is None else '--worker-launch'
        command = ['/usr/bin/sudo', '-n', '-u', p['builder_user'], str(INSTALLED), mode]
        if raw is None:
            result = decode(run(command, {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, input='').stdout)
        else:
            result = supervised_send(command, raw, p, int(os.environ['ASTRA_HOST_INFLIGHT_FD']))
        print(encode(result)); return 0
    except (CursorError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        # Never expose provider output, credentials or the packet in logs.
        print(encode({'outcome': 'UNKNOWN', 'reason': 'CURSOR_ADAPTER_BLOCKED_OR_UNRESOLVED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
