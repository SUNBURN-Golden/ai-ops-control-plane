"""Outer Mac provider fence. The trusted worker writes receipts outside it.

Native CLI sandboxes still apply. This fence additionally denies the provider
process and its descendants access to the host ledger, owner/relay tokens and
control code. Only its isolated checkout and named adapter outputs are excepted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

from common import AppError, private_directory


def command(argv, directory, folder, checkout, *, platform=None, writing=True):
    if (platform or sys.platform) != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise AppError('MAC_HOST_SANDBOX_UNAVAILABLE')
    root, attempt, workspace = (Path(p).resolve() for p in (directory, folder, checkout))
    if root == workspace or root not in attempt.parents or root not in workspace.parents:
        raise AppError('MAC_HOST_SANDBOX_SCOPE_INVALID')
    quote = lambda p: json.dumps(str(p), ensure_ascii=True)
    denied = [root, Path(__file__).resolve().parent,
              Path.home() / 'Library/Application Support/AIOPS Development',
              Path.home() / 'Library/LaunchAgents/local.aiops.mac.plist']
    # Seatbelt evaluates the specific allow rules as exceptions to these broad
    # control-directory denials. Metadata/receipts are deliberately not excepted.
    rules = ['(version 1)', '(allow default)',
             '(deny file-read* file-write* ' + ' '.join('(subpath '+quote(p)+')' for p in denied) + ')',
             '(deny file-write* (subpath '+quote(Path.home() / 'Documents/Codex')+'))',
             '(allow file-read* '+('file-write* ' if writing else '')+'(subpath '+quote(workspace)+'))']
    rules.append('(deny file-write* (subpath '+quote(workspace / '.git')+'))')
    for name in ('prompt.txt', 'schema.json', 'devin-config.json'):
        rules.append('(allow file-read* (literal '+quote(attempt / name)+'))')
    for name in ('last-message.json', 'trajectory.json'):
        rules.append('(allow file-read* file-write* (literal '+quote(attempt / name)+'))')
    return ['/usr/bin/sandbox-exec', '-p', '\n'.join(rules), *argv]


def candidate_scratch_paths(folder):
    runtime = Path(folder).absolute() / 'provider-runtime'
    return tuple(runtime / name for name in (
        'state', 'tmp', 'empty-codex/.tmp', 'empty-codex/tmp',
        'empty-codex/skills/.system', 'empty-codex/installation_id'))


def prepare_candidate_runtime(folder):
    """Create new, empty, per-attempt qualification scratch; never reuse it.

    This helper is not called by the production worker. No account/configuration
    file is copied. Trusted receipts remain siblings outside this directory.
    """
    folder = Path(folder).absolute()
    info = folder.lstat()
    if (folder.resolve() != folder or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise AppError('MAC_CANDIDATE_RUNTIME_INVALID')
    runtime = folder / 'provider-runtime'
    try:
        runtime.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise AppError('MAC_CANDIDATE_RUNTIME_EXISTS') from exc
    for name in ('empty-home', 'empty-codex', 'state', 'tmp',
                 'empty-codex/.tmp', 'empty-codex/tmp',
                 'empty-codex/skills', 'empty-codex/skills/.system'):
        private_directory(runtime / name)
    return runtime


def validate_candidate_runtime(folder):
    """Reject a preexisting link or shared inode before granting scratch IO."""
    runtime = Path(folder).absolute() / 'provider-runtime'
    if runtime.resolve() != runtime:
        raise AppError('MAC_CANDIDATE_RUNTIME_INVALID')
    for path in (runtime, *runtime.rglob('*')):
        info = path.lstat()
        if (info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode) or
                not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or
                (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise AppError('MAC_CANDIDATE_RUNTIME_INVALID')
    for path in (runtime, runtime/'empty-home', runtime/'empty-codex',
                 *candidate_scratch_paths(folder)[:-1]):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise AppError('MAC_CANDIDATE_RUNTIME_INVALID')


def external_candidate_command(argv,directory,folder,checkout,*,platform=None,writing=True,scratch_write=False):
    """Unwired candidate for synthetic/unauthenticated qualification only.

    No account roots, keychain or provider output files are granted. The reviewer
    cannot write source or use network. Optional new scratch allows only the
    vendor's local SQLite, bundled skills, installation ID and temporary files;
    no scratch is accepted as trusted completion evidence.
    An authenticated model transport still needs separate qualification.
    """
    # Reuse the existing path validation, but not its allow-default policy.
    command(argv,directory,folder,checkout,platform=platform,writing=writing)
    root,attempt,workspace=(Path(p).resolve() for p in (directory,folder,checkout))
    quote=lambda p:json.dumps(str(p),ensure_ascii=True)
    systems=[Path(p) for p in ('/bin','/sbin','/usr','/System/Library',
        '/Library/Apple','/Library/Frameworks/Python.framework','/opt/homebrew',
        '/etc','/private/etc','/var/db/timezone','/private/var/db/timezone')]
    # The exact installed vendor executable is read-only; no user state is
    # included. These are runtime code/library paths, never writable roots.
    systems.append(Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli'))
    denied=[root,Path(__file__).resolve().parent,
            Path.home()/'Library/Application Support/AIOPS Development',
            Path.home()/'Library/LaunchAgents/local.aiops.mac.plist']
    sysctls=('hw.activecpu','hw.byteorder','hw.cacheconfig','hw.cpufamily','hw.cputype','hw.pagesize_compat',
        'hw.logicalcpu_max','hw.machine','hw.model','hw.memsize','hw.ncpu','hw.nperflevels',
        'hw.pagesize','hw.physicalcpu','hw.physicalcpu_max','hw.logicalcpu','hw.tbfrequency_compat',
        'kern.argmax','kern.hostname','kern.maxfilesperproc','kern.maxproc','kern.osproductversion',
        'kern.osrelease','kern.ostype','kern.osvariant_status','kern.osversion','kern.secure_kernel',
        'kern.sysv.semmns','kern.usrstack64','kern.version','sysctl.proc_cputype','vm.loadavg')
    rules=['(version 1)','(deny default)','(allow process-exec)','(allow process-fork)',
        '(allow signal (target same-sandbox))','(allow process-info* (target same-sandbox))',
        '(allow mach-lookup (global-name "com.apple.secinitd") (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.bsd.dirhelper"))',
        '(allow system-mac-syscall (mac-policy-name "vnguard"))',
        '(allow system-mac-syscall (require-all (mac-policy-name "Sandbox") (mac-syscall-number 67)))',
        '(allow sysctl-read '+ ' '.join('(sysctl-name '+json.dumps(s)+')' for s in sysctls)+
            ' (sysctl-name-prefix "hw.optional.arm.") (sysctl-name-prefix "hw.optional.armv8_") (sysctl-name-prefix "hw.perflevel"))',
        '(allow file-read-metadata '+ ' '.join('(path-ancestors '+quote(p)+')' for p in systems+[workspace,attempt/'provider-runtime'])+')',
        '(allow file-read* file-map-executable '+ ' '.join('(subpath '+quote(p)+')' for p in systems)+')',
        '(deny file-read* file-write* '+ ' '.join('(subpath '+quote(p)+')' for p in denied)+')',
        '(allow file-read* (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))',
        '(allow file-read* (literal "/"))',
        '(allow file-read* (literal "/private/var/select/sh"))',
        '(allow file-read-metadata (path-ancestors "/private/var/select/sh"))',
        '(allow file-read-data file-write-data (literal "/dev/fd/0") (literal "/dev/fd/1") (literal "/dev/fd/2"))',
        '(allow file-write-data (literal "/dev/null"))',
        '(allow file-read* file-map-executable (subpath '+quote(workspace)+'))',
        '(allow file-read* (subpath '+quote(attempt/'provider-runtime')+'))']
    if writing:
        rules += ['(allow file-write* (subpath '+quote(workspace)+'))',
                  '(allow file-read* file-write* (subpath '+quote(attempt/'provider-tmp')+'))',
                  '(allow network*)']
    if scratch_write:
        validate_candidate_runtime(attempt)
        for path in candidate_scratch_paths(attempt):
            kind = 'literal' if path.name == 'installation_id' else 'subpath'
            rules.append('(allow file-read* file-write* ('+kind+' '+quote(path)+'))')
    # Read-only runtime IO uses stdio. It cannot create writable token/cache or
    # output leaves that a reviewer child could alter using the same OS policy.
    for name in ('.git','.agents','.codex','.aws'):
        rules.append('(deny file-write* (subpath '+quote(workspace/name)+'))')
    rules += ['(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.securityd.xpc"))']
    return ['/usr/bin/sandbox-exec','-p','\n'.join(rules),*argv]
