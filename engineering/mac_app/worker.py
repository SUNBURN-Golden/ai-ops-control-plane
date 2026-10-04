"""Detached local subprocess wrapper. A receipt survives the app window closing."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import re
import subprocess
import time

import agents
import mac_sandbox
from common import AppError, WORKER_REQUEST_LIMIT, atomic_json, read_json


def communicate(child,request,folder,stdin):
    if 'host_directory' not in request:
        return child.communicate(stdin,timeout=request['timeout_seconds'])
    deadline=time.monotonic()+request['timeout_seconds']
    first=True
    while True:
        cancellation=folder / 'stop-request.json'
        if cancellation.exists():
            value=read_json(cancellation,4096)
            if value!={'attempt_id':request['attempt_id'],'binding':request['binding']}:
                raise AppError('STOP_REQUEST_BINDING_MISMATCH')
            # Only this wrapper's live child group is signalled. A PID from an
            # old receipt is never used to kill another process after a restart.
            os.killpg(child.pid,signal.SIGTERM)
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: raise AppError('STOP_NOT_TERMINAL')
            raise AppError('USER_STOPPED')
        remaining=deadline-time.monotonic()
        if remaining<=0: raise subprocess.TimeoutExpired(child.args,request['timeout_seconds'])
        try: return child.communicate(stdin if first else None,timeout=min(1,remaining))
        except subprocess.TimeoutExpired: first=False


def failure_code(folder, returncode):
    # Classify terminal CLI errors, never forward stderr (which may contain secrets).
    fragments = []
    for name in ('stderr.log', 'stdout.log'):
        path = Path(folder) / name
        if not path.exists(): continue
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 32768))
            fragments.append(stream.read().decode('utf-8', 'replace').lower())
    message = '\n'.join(fragments)
    if re.search(r'(unknown|invalid|unsupported|unavailable) model|model.{0,100}(does not exist|not found|not supported|not available)', message):
        return 'MODEL_UNAVAILABLE'
    if re.search(r'not (logged|signed) in|authentication (failed|required)|login required|please (log|sign) in|401 unauthorized|invalid api key|missing.{0,30}(api key|credentials)|providerautherror', message):
        return 'PROVIDER_LOGIN_REQUIRED'
    if re.search(r'unexpected argument|unrecognized (argument|option)|unknown option|sandbox.{0,80}(unavailable|not available|failed|not supported)', message):
        return 'CLI_SETUP_REQUIRED'
    if re.search(r'permission denied|(?:sudo.{0,80})?(?:a )?password (?:is )?required|(?:approval|permission).{0,60}(?:required|non.?interactive)|(?:cannot|can not|unable to).{0,40}(?:prompt|ask).{0,40}(?:approval|permission)', message):
        return 'PROVIDER_PERMISSION_REQUIRED'
    # Exhausted subscription capacity needs an operator action, even if the
    # provider reports HTTP 429. It is not a transient rate-limit retry.
    if re.search(r'(?:subscription|free(?:.?tier)?|weekly|usage|quota).{0,80}(?:exhausted|exceeded|depleted|reached|used up)|(?:insufficient|exhausted|exceeded|depleted).{0,30}(?:quota|credits)|(?:usage|weekly|subscription|free(?:.?tier)?).{0,40}(?:limit|cap).{0,40}(?:hit|reached|exceeded)|(?:hit|reached|exceeded).{0,40}(?:usage|weekly|subscription|free(?:.?tier)?).{0,40}(?:limit|cap)|(?:out of|no remaining).{0,20}(?:credits|quota)', message):
        return 'PROVIDER_USAGE_LIMIT'
    if re.search(r'temporar(?:y|ily).{0,60}(?:unavailable|failure|error)|(?:connection|network).{0,40}(?:reset|refused|unreachable|timed out|timeout)|econnreset|econnrefused|etimedout|rate.?limit|\b429\b|\b(?:http(?: status)?|status(?: code)?|error(?: code)?)\s*[:=]?\s*5\d\d\b|\b5\d\d\s+(?:bad gateway|service unavailable|internal server error|gateway timeout)', message):
        return 'PROVIDER_TEMPORARILY_UNAVAILABLE'
    return 'PROVIDER_EXIT_' + str(returncode)


def run(folder):
    folder = Path(folder).resolve()
    request = read_json(folder / 'request.json', WORKER_REQUEST_LIMIT)
    # O_EXCL is the second admission fence, independent of the app's SQLite lock.
    claim = os.open(folder / 'claimed', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(claim, 'w') as stream:
        stream.write(str(os.getpid())); stream.flush(); os.fsync(stream.fileno())
    receipt = {'attempt_id': request['attempt_id'], 'binding': request['binding'],
               'started': time.time(), 'exit_code': None, 'report': None, 'error': None,
               'provider_started': False, 'process_group_quiescent': False}
    atomic_json(folder / 'running.json', {'pid': os.getpid(), 'started': receipt['started']})
    child = None
    try:
        argv = agents.command(request['profile'], request['role'], folder, checkout=request['checkout'])
        agents.prepare(request['profile'], request['role'], folder, request['checkout'], request['prompt'])
        if 'host_directory' in request:
            argv = mac_sandbox.command(argv, request['host_directory'], folder, request['checkout'],writing=request['role']=='builder')
        with open(folder / 'stdout.log', 'wb') as stdout, open(folder / 'stderr.log', 'wb') as stderr:
            child = subprocess.Popen(argv, cwd=request['checkout'], stdin=subprocess.PIPE,
                                     stdout=stdout, stderr=stderr,
                                     env=agents.environment(request['profile'], request['role']), start_new_session=True)
            receipt['provider_started'] = True
            # Persist identity before waiting: an interrupted wrapper must not
            # leave operators guessing which provider process group it owned.
            atomic_json(folder / 'provider-process.json', {
                'attempt_id': request['attempt_id'], 'binding': request['binding'],
                'pid': child.pid, 'pgid': child.pid, 'started': time.time()})
            try:
                stdin = b'' if request['profile']['provider'] in ('grok_build', 'devin') else request['prompt'].encode()
                communicate(child,request,folder,stdin)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=10)
                raise AppError('SESSION_TIMEOUT')
            receipt['exit_code'] = child.returncode
            atomic_json(folder / 'provider-exit.json', {
                'attempt_id': request['attempt_id'], 'binding': request['binding'],
                'pid': child.pid, 'exit_code': child.returncode, 'observed': time.time()})
            # Fence descendants left behind by a CLI: no next writer until the group is gone.
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError: pass
            else:
                os.killpg(child.pid, signal.SIGTERM)
                raise AppError('CHILD_PROCESS_GROUP_NOT_QUIESCENT')
            if child.returncode != 0: raise AppError(failure_code(folder, child.returncode))
        completed = agents.completion(request['profile'], folder)
        receipt['report'] = completed.pop('report')
        receipt['provider_evidence'] = completed
    except Exception as exc:
        # Unexpected adapter failures also need a terminal record. Never copy
        # exception messages, credentials or provider logs into that record.
        receipt['error'] = exc.code if isinstance(exc, AppError) else type(exc).__name__
    finally:
        if child is None:
            receipt['process_group_quiescent'] = True
        else:
            if type(child.poll()) is int:
                receipt['exit_code']=child.returncode
                atomic_json(folder / 'provider-exit.json', {
                    'attempt_id': request['attempt_id'], 'binding': request['binding'],
                    'pid': child.pid, 'exit_code': child.returncode, 'observed': time.time()})
            # Even a timed-out session may be continued only after its entire
            # admitted process group is gone. A timeout alone is not that proof.
            try: os.killpg(child.pid, 0)
            except ProcessLookupError: receipt['process_group_quiescent'] = True
            except OSError:
                # Failed observation is not proof of termination. In particular
                # EPERM here must not prevent the receipt itself being written.
                receipt['error'] = 'PROCESS_GROUP_OBSERVATION_FAILED'
            if not receipt['process_group_quiescent']:
                receipt['report'] = None
                receipt.pop('provider_evidence', None)
        receipt['finished'] = time.time()
        atomic_json(folder / 'receipt.json', receipt)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--attempt', required=True)
    run(parser.parse_args().attempt)
