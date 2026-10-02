#!/usr/bin/env python3
"""Install a per-user .app launcher and launchd service; never requires sudo.

The app opens the real loopback UI in the user's default browser. Python, GitHub
CLI and selected agent CLIs use their existing installations and logins.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

from common import AppError, VERSION, private_directory


def install(destination=None, data=None):
    if sys.platform != 'darwin': raise AppError('MAC_REQUIRED', '설치는 Mac에서 실행해 주세요.')
    if sys.version_info < (3, 10): raise AppError('PYTHON_310_REQUIRED')
    app = Path(destination or Path.home() / 'Applications/AIOPS.app').expanduser().absolute()
    state = private_directory(data or Path.home() / 'Library/Application Support/AIOPS')
    if app.exists(): raise AppError('ALREADY_INSTALLED', '이미 설치되어 있습니다. 실행 중인 작업을 마친 뒤 앱 코드 업데이트를 진행해 주세요.')
    agents = Path.home() / 'Library/LaunchAgents'
    plist = agents / 'local.aiops.mac.plist'
    if plist.exists(): raise AppError('EXISTING_LAUNCH_AGENT')
    if not shutil.which('gh'): raise AppError('GITHUB_CLI_REQUIRED', '먼저 GitHub CLI(gh)를 설치해 주세요.')
    if not shutil.which('git'): raise AppError('GIT_REQUIRED')
    resources = app / 'Contents/Resources'; executable = app / 'Contents/MacOS'
    resources.mkdir(parents=True, mode=0o700); executable.mkdir(mode=0o700)
    source = Path(__file__).resolve().parent
    for item in source.iterdir():
        if item.suffix == '.py': shutil.copy2(item, resources / item.name)
    shutil.copytree(source / 'ui', resources / 'ui')
    registry = source.parent / '.github/control-plane/projects.json'
    if registry.exists(): shutil.copy2(registry, resources / 'projects.json')
    info = {'CFBundleName': 'AIOPS', 'CFBundleDisplayName': 'AIOPS',
            'CFBundleIdentifier': 'local.aiops.mac', 'CFBundleVersion': VERSION,
            'CFBundleShortVersionString': VERSION, 'CFBundleExecutable': 'AIOPS',
            'CFBundlePackageType': 'APPL', 'LSUIElement': True}
    with open(app / 'Contents/Info.plist', 'wb') as stream: plistlib.dump(info, stream)
    python = str(Path(sys.executable).resolve()); entry = str(resources / 'aiops.py')
    launcher = '#!/bin/sh\nexec ' + ' '.join(shlex.quote(v) for v in [python, entry, '--data-dir', str(state), 'open']) + '\n'
    (executable / 'AIOPS').write_text(launcher); (executable / 'AIOPS').chmod(0o755)
    agents.mkdir(parents=True, exist_ok=True)
    config = {'Label': 'local.aiops.mac', 'ProgramArguments': [python, entry, '--data-dir', str(state), 'serve'],
              'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
              'ProcessType': 'Background', 'Umask': 0o077,
              'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')},
              'StandardOutPath': str(state / 'service.log'), 'StandardErrorPath': str(state / 'service-errors.log')}
    with open(plist, 'xb') as stream: plistlib.dump(config, stream)
    plist.chmod(0o600)
    subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(plist)], check=True)
    print('설치했습니다: ' + str(app))
    print('GitHub와 선택한 CLI의 로그인을 마친 뒤 AIOPS.app을 여세요. 모델 연결은 앱에서 확인할 수 있습니다.')
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--destination'); parser.add_argument('--data-dir')
    args = parser.parse_args()
    try: install(args.destination, args.data_dir)
    except (AppError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(2)
