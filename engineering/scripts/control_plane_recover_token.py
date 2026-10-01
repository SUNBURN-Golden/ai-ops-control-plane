#!/usr/bin/python3 -I
"""Foreground P3 transcript replay; terminal/provider output is never forwarded."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import webbrowser

from control_plane_recover import (RecoveryError, OAUTH, fail, confirm_model,
                                  read_regular, protected_parent, sha, token_warning, pinned_module, billing, atomic, canonical, strict_json)

TRANSCRIPT = Path('/dev/shm/aiops-token.typescript')
EXPECTED_CLI = '2.1.286 (Claude Code)'
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
       'DISABLE_AUTOUPDATER': '1', 'DISABLE_UPDATES': '1'}


def replay(data, columns=500, lines=60):
    """Reconstruct cells, not ANSI stripping. History preserves scrolled rows."""
    try:
        import pyte
    except ImportError:
        fail('PYTE_UNAVAILABLE')
    screen = pyte.HistoryScreen(columns, lines, history=10000)
    stream = pyte.Stream(screen)
    stream.feed(data.decode('utf-8', errors='replace'))
    rows = list(screen.history.top) + [screen.buffer[i] for i in range(lines)]
    display = [''.join(row[i].data for i in range(columns)).rstrip() for row in rows]
    candidates = set()
    for index, row in enumerate(display):
        start = row.find('sk-ant-oat01-')
        if start < 0:
            continue
        candidate = ''
        for line in display[index:]:
            piece = line[start:] if not candidate else line
            start = 0
            match = re.match(r'[A-Za-z0-9_\-\s]+', piece)
            if not match:
                break
            candidate += re.sub(r'\s+', '', match[0])
            if OAUTH.fullmatch(candidate) and len(candidate) <= 4096:
                # A complete short row ends the token; full-width rows wrap.
                if len(line) < columns or len(match[0]) != len(piece):
                    candidates.add(candidate)
                    break
            if len(match[0]) != len(piece) or not piece:
                break
    if len(candidates) != 1:
        fail('TOKEN_EXTRACTION_FAILED_REUSE_TRANSCRIPT')
    return candidates.pop()


def selftest():
    fake = 'sk-ant-oat01-' + 'A' * 96
    cases = [fake, fake[:38] + '\r\n' + fake[38:],
             fake[:30] + '\x1b[7C' + fake[30:],
             fake + '\r\n' + ('!\r\n' * 90)]
    for raw in cases:
        if replay(raw.encode(), columns=40, lines=8) != fake:
            fail('TOKEN_EXTRACTOR_SELFTEST_FAILED')


def prerequisites(cfg, run=subprocess.run):
    try:
        import pyte
        from importlib.metadata import version
        if version('pyte') != '0.8.2':
            fail('PYTE_VERSION_DRIFT')
    except ImportError:
        fail('PYTE_UNAVAILABLE')
    selftest()
    if not Path('/usr/bin/script').is_file():
        fail('SCRIPT_UNAVAILABLE')
    # Width is tested INSIDE a real PTY, before setup-token can display a URL.
    check = run(['/usr/bin/script', '-q', '-e', '-c',
                 '/usr/bin/stty cols 500 && /usr/bin/stty size', '/dev/null'],
                capture_output=True, timeout=30, env=ENV)
    if check.returncode or not re.search(rb'\b\d+ 500\b', check.stdout):
        fail('PTY_WIDTH_UNVERIFIED')
    result = run(['/usr/local/bin/claude', '--version'], capture_output=True, timeout=30, env=ENV)
    if result.returncode or result.stdout.strip().decode() != EXPECTED_CLI or cfg.get('claude_version') != EXPECTED_CLI:
        fail('CLI_VERSION_DRIFT')


def transcript_path(cfg):
    # No caller-selected log location or durable raw transcript.
    return TRANSCRIPT


def check_tmpfs(path, mounts=Path('/proc/self/mountinfo')):
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        fail('TOKEN_TRANSCRIPT_UNSAFE')
    # /dev/shm itself is normally sticky/world-writable; leaf O_EXCL/NOFOLLOW,
    # root ownership and 0600 are essential. Exact mount identity is required.
    if not any(x.split()[4] == str(path.parent) and x.split(' - ', 1)[1].split()[0] == 'tmpfs'
               for x in mounts.read_text().splitlines() if ' - ' in x):
        fail('TOKEN_TRANSCRIPT_NOT_TMPFS')


def capture(cfg, path, popen=subprocess.Popen, open_url=webbrowser.open):
    check_tmpfs(path)
    # A private parent prevents another principal replacing the transcript.
    parent = path.parent / 'aiops-recover-token'
    if not parent.exists():
        parent.mkdir(mode=0o700)
    s = parent.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or stat.S_IMODE(s.st_mode) != 0o700:
        fail('TOKEN_TRANSCRIPT_UNSAFE')
    path = parent / 'typescript'
    if path.exists():
        return path  # A failed extraction never starts another authorization.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    # Synchronous foreground subprocess. Never tmux, a service or a detached job.
    command = '/usr/bin/stty cols 500 && exec /usr/local/bin/claude setup-token'
    with popen(['/usr/bin/script', '-q', '-e', '-c', command, str(path)],
               stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV) as proc:
        pending = b''
        opened = False
        while True:
            block = proc.stdout.read(1)
            if not block:
                break
            pending = (pending + block)[-16384:]
            # Open the authorization URL without printing it or the transcript.
            if not opened:
                match = re.search(rb'https://(?:claude\.ai|console\.anthropic\.com)/[^\s\x1b]+[\r\n]', pending)
                if match:
                    if not open_url(match[0].strip().decode(), new=2):
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill(); proc.wait()
                        fail('LOGIN_BROWSER_UNAVAILABLE_TRANSCRIPT_RETAINED')
                    opened = True
        rc = proc.wait()
        if rc:
            fail('TOKEN_ISSUANCE_FAILED_TRANSCRIPT_RETAINED')
    return path


def validate(token, cfg, run=subprocess.run, confirm=confirm_model, now=None, policy=None):
    if not OAUTH.fullmatch(token) or len(token) > 4096:
        fail('TOKEN_FORMAT_INVALID')
    if not confirm():
        fail('TOKEN_VALIDATION_CANCELLED_TRANSCRIPT_RETAINED')
    # Required single minimal model call, explicit confirmation above. Credential
    # only in the CLI's required environment; never argv, a dump, or a log.
    env = dict(ENV, CLAUDE_CODE_OAUTH_TOKEN=token, HOME='/var/lib/aiops-auditor')
    result = run(['/usr/local/bin/claude', '-p', 'ok', '--model', 'claude-fable-5-1',
                  '--effort', 'low', '--tools', '', '--max-turns', '1',
                  '--output-format', 'stream-json', '--verbose', '--restricted', '--safe-mode',
                  '--disable-slash-commands', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                  '--permission-mode', 'dontAsk', '--no-session-persistence'],
                 capture_output=True, timeout=120, env=env, user=cfg.get('account', {}).get('uid'),
                 group=cfg.get('account', {}).get('gid'), extra_groups=[], umask=0o077, cwd='/')
    output = result.stdout + result.stderr
    if b'401' in output or b'authentication_error' in output:
        # Only safe length/prefix diagnostics may leave this boundary.
        raise RecoveryError('TOKEN_REISSUE_REQUIRED')
    if result.returncode:
        fail('TOKEN_VALIDATION_FAILED_TRANSCRIPT_RETAINED')
    events = []
    try:
        events = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
    except (ValueError, UnicodeError):
        fail('TOKEN_VALIDATION_FAILED_TRANSCRIPT_RETAINED')
    if policy is not None:
        signals = [x.get('rate_limit_info') for x in events if x.get('type') == 'rate_limit_event']
        if not signals:
            fail('OVERAGE_UNVERIFIED')
        for signal in signals:
            code, _ = policy(signal)
            if code:
                fail(code)
    results = [x for x in events if x.get('type') == 'result']
    if len(results) != 1 or results[0].get('is_error') is not False or not results[0].get('result'):
        fail('TOKEN_VALIDATION_FAILED_TRANSCRIPT_RETAINED')
    issued = int(time.time()) if now is None else now
    return {'value': token, 'issued_at': issued, 'expires_at': issued + 365 * 86400,
            'verified': True}


def reissue(cfg, installer):
    installer.verify()
    prerequisites(cfg)
    path = TRANSCRIPT.parent / 'aiops-recover-token' / 'typescript'
    rejected = path.parent / 'reissue-needed.json'
    if rejected.exists():
        marker = strict_json(read_regular(rejected, boundary=path.parent, mode=0o600))
        if not path.exists() or sha(read_regular(path, boundary=path.parent, mode=0o600, limit=16 << 20)) != marker.get('record_sha256'):
            fail('TOKEN_TRANSCRIPT_UNSAFE')
        # A NEW explicit --reissue-token invocation authorizes fresh issuance
        # after 401. Preserve the rejected record; never retry in this run.
        index = 0
        while (path.parent / f'rejected-{index}.typescript').exists():
            index += 1
        os.rename(path, path.parent / f'rejected-{index}.typescript')
        rejected.unlink()
    if not path.exists():
        path = capture(cfg, TRANSCRIPT)
    # Capture still retained after failure; run this command again to replay it.
    raw = read_regular(path, boundary=path.parent, mode=0o600, limit=16 << 20)
    token = replay(raw)
    try:
        fable_item = next(x for x in installer.manifest['files'] if x['destination'] == '/opt/aiops/lib/fable/control_plane_fable.py')
        fable = pinned_module(Path(fable_item['destination']), fable_item['sha256'])
        billing(cfg, fable, token)
        return validate(token, cfg, policy=fable.overage_policy, now=int(path.stat().st_mtime))
    except RecoveryError as error:
        if error.code == 'TOKEN_REISSUE_REQUIRED':
            atomic(rejected, canonical({'record_sha256':sha(raw)}))
            print(json.dumps({'status': 'HOLD', 'reason': error.code,
                              'prefix_valid': True, 'length': len(token),
                              'command': 'aiops-recover --reissue-token'}))
        raise


def cleanup(cfg):
    # Only called after successful validation AND durable encrypted checkpoint.
    path = TRANSCRIPT.parent / 'aiops-recover-token' / 'typescript'
    if path.exists():
        read_regular(path, boundary=path.parent, mode=0o600, limit=16 << 20)
        path.unlink()
        for old in path.parent.glob('rejected-*.typescript'):
            read_regular(old, boundary=path.parent, mode=0o600, limit=16 << 20)
            old.unlink()
