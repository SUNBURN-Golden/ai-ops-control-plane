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
from common import AppError, WORKER_REQUEST_LIMIT, atomic_json, read_json


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
        with open(folder / 'stdout.log', 'wb') as stdout, open(folder / 'stderr.log', 'wb') as stderr:
            child = subprocess.Popen(argv, cwd=request['checkout'], stdin=subprocess.PIPE,
                                     stdout=stdout, stderr=stderr,
                                     env=agents.environment(request['profile'], request['role']), start_new_session=True)
            receipt['provider_started'] = True
            try:
                stdin = b'' if request['profile']['provider'] in ('grok_build', 'devin') else request['prompt'].encode()
                child.communicate(stdin, timeout=request['timeout_seconds'])
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=10)
                raise AppError('SESSION_TIMEOUT')
            receipt['exit_code'] = child.returncode
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
    except (OSError, AppError, ValueError) as exc:
        receipt['error'] = exc.code if isinstance(exc, AppError) else type(exc).__name__
    finally:
        if child is None:
            receipt['process_group_quiescent'] = True
        else:
            # Even a timed-out session may be continued only after its entire
            # admitted process group is gone. A timeout alone is not that proof.
            try: os.killpg(child.pid, 0)
            except ProcessLookupError: receipt['process_group_quiescent'] = True
        receipt['finished'] = time.time()
        atomic_json(folder / 'receipt.json', receipt)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--attempt', required=True)
    run(parser.parse_args().attempt)
