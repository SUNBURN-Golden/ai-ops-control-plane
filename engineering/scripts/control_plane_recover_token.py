#!/usr/bin/python3 -I
"""Foreground P3 transcript replay; terminal/provider output is never forwarded."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import getpass
import codecs
import urllib.parse

from control_plane_recover import (RecoveryError, OAUTH, fail, confirm_model,
                                  read_regular, protected_parent, sha, token_warning, pinned_module, atomic, canonical, strict_json, authentication_error)

TRANSCRIPT = Path('/dev/shm/aiops-token.typescript')
EXPECTED_CLI = '2.1.286 (Claude Code)'
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
       'DISABLE_AUTOUPDATER': '1', 'DISABLE_UPDATES': '1', 'BROWSER': '/bin/false'}


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


def terminal_lines(screen):
    rows = list(screen.history.top) + [screen.buffer[i] for i in range(screen.lines)]
    return [''.join(row[i].data for i in range(screen.columns)).rstrip() for row in rows]


def login_url(screen):
    """Join only URL continuation cells, never emit the remaining CLI screen."""
    rows = terminal_lines(screen)
    for index, row in enumerate(rows):
        start = row.find('https://')
        if start < 0:
            continue
        parts = []
        for line in rows[index:]:
            piece = line[start:] if not parts else line.strip()
            start = 0
            if parts and not re.fullmatch(r'[A-Za-z0-9:/?&=_%.~+\-#]+', piece):
                break
            match = re.match(r'[A-Za-z0-9:/?&=_%.~+\-#]+', piece)
            if not match:
                break
            parts.append(match[0])
            if len(match[0]) != len(piece):
                break
        candidate = ''.join(parts)
        parsed = urllib.parse.urlparse(candidate)
        if (parsed.scheme == 'https' and parsed.hostname in ('claude.ai', 'console.anthropic.com')
                and not parsed.username and not parsed.password and len(candidate) <= 16384
                and 'sk-ant-' not in candidate):
            return candidate
    return None


def capture(cfg, path, popen=subprocess.Popen, read_code=getpass.getpass, emit=print):
    import pyte
    check_tmpfs(path)
    parent = path.parent / 'aiops-recover-token'
    if not parent.exists():
        parent.mkdir(mode=0o700)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        fail('TOKEN_TRANSCRIPT_UNSAFE')
    path = parent / 'typescript'
    if path.exists():
        return path
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    command = '/usr/bin/stty cols 500 && exec /usr/local/bin/claude setup-token'
    screen = pyte.HistoryScreen(500, 60, history=10000)
    stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    try:
        with popen(['/usr/bin/script', '-q', '-e', '-c', command, str(path)],
                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV) as proc:
            sent = False
            try:
                while True:
                    block = proc.stdout.read(1)
                    if not block:
                        break
                    stream.feed(decoder.decode(block))
                    if not sent and block in (b':', b'>', b'\n', b'\r'):
                        rows = terminal_lines(screen)
                        # Wait for the code prompt so a wrapped URL is complete.
                        prompt = any(re.search(r'(paste|enter).{0,80}code', row, re.I) for row in rows)
                        url = login_url(screen) if prompt else None
                        if url:
                            emit(url)
                            emit('브라우저에서 승인 → 받은 코드를 붙여 넣고 Enter')
                            code = read_code('승인 코드: ')
                            if not code or len(code) > 8192 or '\n' in code or '\r' in code:
                                fail('LOGIN_CODE_INVALID_RETRY')
                            proc.stdin.write((code + '\n').encode())
                            proc.stdin.flush()
                            del code
                            sent = True
                if proc.wait():
                    fail('TOKEN_ISSUANCE_FAILED_RETRY')
                if not sent:
                    fail('LOGIN_PROMPT_NOT_SHOWN_RETRY')
            except BaseException:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                raise
    except BaseException:
        # An unfinished login holds no token: keep the record aside and let the next
        # explicit --reissue-token/--enroll show a fresh login URL.
        set_aside(path, 'failed')
        raise
    return path


def set_aside(path, prefix):
    if not os.path.lexists(path):
        return
    index = 0
    while (path.parent / f'{prefix}-{index}.typescript').exists():
        index += 1
    os.rename(path, path.parent / f'{prefix}-{index}.typescript')


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
                 capture_output=True, timeout=120, env=env, user=cfg.get('_auditor', {}).get('uid'),
                 group=cfg.get('_auditor', {}).get('gid'), extra_groups=[], umask=0o077, cwd='/')
    try:
        events = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
        if any(not isinstance(x, dict) for x in events):
            fail('TOKEN_VALIDATION_FAILED_TRANSCRIPT_RETAINED')
    except (ValueError, UnicodeError):
        fail('TOKEN_VALIDATION_FAILED_TRANSCRIPT_RETAINED')
    if any(authentication_error(event) for event in events):
        fail('TOKEN_REISSUE_REQUIRED')
    if result.returncode:
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


def enrollment_token(cfg, installer, path):
    """Reuse a protected existing OAuth credential; never reissue on 401."""
    protected_parent(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return reissue(cfg, installer)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            fail('INSTALLATION_DRIFT')
        raw = stream.read(4097)
    try:
        token = raw.decode('ascii').strip()
    except UnicodeError:
        token = ''
    if len(token) > 4096 or not OAUTH.fullmatch(token):
        return reissue(cfg, installer)
    installer.verify()
    fable_item = next(x for x in installer.manifest['files'] if x['destination'] == '/opt/aiops/lib/fable/control_plane_fable.py')
    fable = pinned_module(Path(fable_item['destination']), fable_item['sha256'])
    return validate(token, cfg, policy=fable.overage_policy, now=int(info.st_mtime))


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
        set_aside(path, 'rejected')
        rejected.unlink()
    if not path.exists():
        path = capture(cfg, TRANSCRIPT)
    # A completed capture is replayed, so a validation failure needs no new approval.
    raw = read_regular(path, boundary=path.parent, mode=0o600, limit=16 << 20)
    try:
        token = replay(raw)
    except RecoveryError:
        # The pinned extractor is deterministic: replaying these bytes can never succeed.
        set_aside(path, 'failed')
        fail('TOKEN_EXTRACTION_FAILED_RETRY')
    try:
        fable_item = next(x for x in installer.manifest['files'] if x['destination'] == '/opt/aiops/lib/fable/control_plane_fable.py')
        fable = pinned_module(Path(fable_item['destination']), fable_item['sha256'])
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
    if path.parent.exists():
        for pattern in ('rejected-*.typescript', 'failed-*.typescript'):
            for old in path.parent.glob(pattern):
                read_regular(old, boundary=path.parent, mode=0o600, limit=16 << 20)
                old.unlink()
