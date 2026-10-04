"""Outer Mac provider fence. The trusted worker writes receipts outside it.

Native CLI sandboxes still apply. This fence additionally denies the provider
process and its descendants access to the host ledger, owner/relay tokens and
control code. Only its isolated checkout and named adapter outputs are excepted.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from common import AppError


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
             '(allow file-read* '+('file-write* ' if writing else '')+'(subpath '+quote(workspace)+'))']
    rules.append('(deny file-write* (subpath '+quote(workspace / '.git')+'))')
    for name in ('prompt.txt', 'schema.json', 'devin-config.json'):
        rules.append('(allow file-read* (literal '+quote(attempt / name)+'))')
    for name in ('last-message.json', 'trajectory.json'):
        rules.append('(allow file-read* file-write* (literal '+quote(attempt / name)+'))')
    return ['/usr/bin/sandbox-exec', '-p', '\n'.join(rules), *argv]
