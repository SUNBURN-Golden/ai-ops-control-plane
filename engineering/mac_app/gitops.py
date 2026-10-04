"""GitHub I/O owned by the app. Agents never receive a GitHub token in their env."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import agents
from common import AppError, encoded, parse_json
from program_scope import load_scope


def execute(argv, cwd=None, timeout=120, allowed=(0,)):
    try:
        run = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                             timeout=timeout, env=agents.environment())
    except subprocess.TimeoutExpired as exc:
        raise AppError('COMMAND_TIMEOUT') from exc
    if run.returncode not in allowed:
        # Output can contain credentials or repository data; keep it out of API errors.
        diagnostic = run.stderr.lower()
        if any(marker in diagnostic for marker in ('repository not found', 'could not resolve to a repository', 'could not resolve to a pullrequest')):
            raise AppError('REPOSITORY_ACCESS_REQUIRED', 'GitHub 레포 주소와 해당 계정의 접근 권한을 확인해 주세요.')
        if any(marker in diagnostic for marker in ('authentication failed', 'not logged into', 'gh auth login', 'http 401', 'bad credentials')):
            raise AppError('GITHUB_LOGIN_REQUIRED', 'GitHub 로그인을 갱신한 뒤 이어서 진행해 주세요.')
        raise AppError('COMMAND_FAILED', f'{Path(argv[0]).name} 명령 실패 ({run.returncode}). 연결 또는 작업 기록을 확인해 주세요.')
    if len(run.stdout) > 2 * 1024 * 1024: raise AppError('COMMAND_OUTPUT_TOO_LARGE')
    return run.stdout.rstrip('\n')


def git(checkout, *args, **kwargs):
    return execute(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                    '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
                    *args], cwd=checkout, **kwargs)


def gh(repo, *args, **kwargs):
    if args[:2] == ('repo', 'view'):
        return execute(['gh', 'repo', 'view', repo, *args[2:]], **kwargs)
    return execute(['gh', *args, '--repo', repo], **kwargs)


class Repositories:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, job):
        return self.directory / job['id']

    def prepare(self, job):
        checkout = self.path(job)
        if checkout.exists():
            # A crash during read-only preparation may be resumed only after the
            # dedicated checkout's exact repository/branch and clean tree verify.
            self.assert_binding(job)
            if not self.clean(job): raise AppError('PREPARATION_OUTCOME_UNKNOWN')
            branch = git(checkout, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD').removeprefix('origin/')
            return {'base_sha': self.head(job), 'base_branch': branch, 'head': self.head(job)}
        metadata = parse_json(gh(job['repository'], 'repo', 'view', '--json', 'defaultBranchRef,isArchived'))
        if metadata.get('isArchived'): raise AppError('ARCHIVED_REPOSITORY')
        branch = (metadata.get('defaultBranchRef') or {}).get('name')
        if not branch: raise AppError('EMPTY_REPOSITORY')
        # Existing legacy task owners require host migration, not a second admission ledger.
        active = parse_json(gh(job['repository'], 'issue', 'list', '--state', 'open', '--label', 'aiops-task',
                               '--limit', '1', '--json', 'number,url'))
        if active:
            raise AppError('EXISTING_AIOPS_OWNER', '이 레포에 기존 AIOPS 작업 등록이 있습니다. handoff inspect/prepare로 원래 계획과 기록을 가져올 수 있습니다. 실행은 보호 호스트의 소유권 확인 전까지 보류합니다: ' + active[0]['url'])
        git(None, 'clone', '--no-local', '--single-branch', '--branch', branch,
            'https://github.com/' + job['repository'] + '.git', str(checkout), timeout=600)
        git(checkout, 'config', 'user.name', 'AIOPS Mac')
        git(checkout, 'config', 'user.email', 'aiops-mac@users.noreply.github.com')
        git(checkout, 'checkout', '-b', job['branch'])
        return {'base_sha': self.head(job), 'base_branch': branch, 'head': self.head(job)}

    def head(self, job):
        return git(self.path(job), 'rev-parse', 'HEAD')

    def clean(self, job):
        return not git(self.path(job), 'status', '--porcelain')

    def assert_binding(self, job):
        checkout = self.path(job)
        if git(checkout, 'branch', '--show-current') != job['branch']:
            raise AppError('BRANCH_CHANGED')
        if git(checkout, 'remote', 'get-url', 'origin') != 'https://github.com/' + job['repository'] + '.git':
            raise AppError('REMOTE_CHANGED')
        if git(checkout, 'rev-parse', '--is-shallow-repository') != 'false':
            raise AppError('SHALLOW_CHECKOUT')
        if job.get('base_sha') and git(checkout, 'merge-base', job['base_sha'], 'HEAD') != job['base_sha']:
            raise AppError('HISTORY_REWRITTEN')

    def source_pins(self, job, plan):
        result = {}
        for path in plan['sources']:
            target = self.path(job) / path
            if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(self.path(job).resolve()):
                raise AppError('MISSING_AUTHORITATIVE_SOURCE', '계획이 존재하지 않는 기준 문서를 참조했습니다: ' + path)
            sha = git(self.path(job), 'rev-parse', job['base_sha'] + ':' + path)
            result[path] = sha
        return result

    def program_scope(self, job):
        checkout = self.path(job)
        tracked = git(checkout, 'ls-tree', '--name-only', job['base_sha'], '--', '.aiops/program.json')
        if not tracked: return None
        blob = git(checkout, 'rev-parse', job['base_sha'] + ':.aiops/program.json')
        raw = git(checkout, 'show', job['base_sha'] + ':.aiops/program.json')
        return load_scope(raw, job['repository'], blob)

    def assert_scope(self, job):
        scope = job.get('program_scope')
        if not scope: return
        changed = git(self.path(job), 'diff', '--name-only', job['base_sha'], 'HEAD', '--', scope['path'])
        if changed or git(self.path(job), 'diff', 'HEAD', '--', scope['path']):
            raise AppError('PROGRAM_SCOPE_CHANGED', '원래 프로그램 계획이 변경되었습니다. 기존 범위의 완료 조건을 바꿀 수 없습니다.')

    def checkpoint(self, job):
        self.assert_binding(job)
        self.assert_scope(job)
        checkout = self.path(job)
        changed = git(checkout, 'status', '--porcelain=v1', '-z')
        # App data is outside the checkout. Common credential files cannot be added
        # as a side effect of a broad git add. Existing tracked examples are allowed.
        names = git(checkout, 'diff', 'HEAD', '--name-only', '-z') + git(checkout, 'ls-files', '--others', '--exclude-standard', '-z')
        if job.get('base_sha'): names += git(checkout, 'diff', job.get('verified_base', job['base_sha']), 'HEAD', '--name-only', '-z')
        for name in names.split('\x00'):
            if name == 'AGENTS.md' or name.endswith('/AGENTS.md') or name.startswith(('.aiops/', 'RUNBOOKS/', 'docs/decisions/')):
                raise AppError('AUTHORITY_EDIT_NEEDS_USER', '기준 계약 변경은 별도 결정을 남겨야 합니다: ' + name)
            if name and re.search(r'(^|/)(\.env(\.[^/]+)?|id_rsa|id_ed25519|credentials\.json|[^/]+\.(pem|p12|key))$', name) and not name.endswith(('.example', '.sample', '.template')):
                raise AppError('SENSITIVE_FILE_CHANGE', '자격증명 파일 변경을 제외해야 합니다: ' + name)
        if changed:
            git(checkout, 'add', '-A', '--', '.')
            git(checkout, 'commit', '-m', f"AIOPS: {job['current_task']['title'][:120]}", timeout=120)
        head = self.head(job)
        if not self.clean(job): raise AppError('DIRTY_CHECKPOINT')
        return head

    def synchronize_base(self, job):
        self.assert_binding(job)
        git(self.path(job), 'fetch', 'origin', job['base_branch'])
        remote = git(self.path(job), 'rev-parse', 'FETCH_HEAD')
        if remote == job.get('verified_base', job['base_sha']): return {'changed': False}
        try:
            git(self.path(job), 'merge', '--no-edit', remote)
        except AppError:
            # Conflicts stay in the assigned checkout and go back to its builder.
            return {'changed': True, 'conflicts': True, 'base': remote}
        return {'changed': True, 'conflicts': False, 'base': remote}

    def publish(self, job):
        self.assert_binding(job)
        if self.head(job) != job['head'] or not self.clean(job): raise AppError('STALE_PUBLISH_HEAD')
        checkout = self.path(job)
        git(checkout, 'push', '--porcelain', 'origin', 'HEAD:refs/heads/' + job['branch'], timeout=180)
        prior = parse_json(gh(job['repository'], 'pr', 'list', '--head', job['branch'], '--state', 'all',
                              '--json', 'url,state,headRefOid', '--limit', '10'))
        if prior:
            if len(prior) != 1 or prior[0]['state'] != 'OPEN' or prior[0]['headRefOid'] != job['head']:
                raise AppError('PR_BINDING_MISMATCH')
            return prior[0]['url']
        body = '\n'.join([
            '## 요청과 결과', job['goal'], '', job.get('summary', ''), '',
            '## 검증', f"- 작업: `{job['id']}`", f"- 기준: `{job['base_sha']}`", f"- 검토한 HEAD: `{job['head']}`",
            '- 작성 세션과 분리된 감사·감리 세션 결과를 앱에 보관했습니다. 기존 보호 서비스의 A3 증명이나 CI 결과를 대체하지 않습니다.',
            '- 최종 사용자 검수 대기 중입니다. 이 앱은 자동으로 병합·배포하지 않습니다.', '',
            '## 계획', *[f"- {task['title']}: {'; '.join(task['acceptance'])}" for task in job['plan']['tasks']], '',
            '## 모델 구성', *[f"- {role}: {config['provider']} / {config['model'] or 'CLI configured default'}" for role, config in job['settings']['roles'].items()],
        ])
        path = self.directory.parent / 'jobs' / job['id'] / 'pr-body.md'
        path.write_text(body, encoding='utf-8')
        url = gh(job['repository'], 'pr', 'create', '--draft', '--base', job['base_branch'],
                 '--head', job['branch'], '--title', 'AIOPS: ' + job['goal'].split('\n')[0][:120], '--body-file', str(path))
        if not re.fullmatch(r'https://github\.com/' + re.escape(job['repository']) + r'/pull/[0-9]+', url):
            raise AppError('PUBLISH_RECEIPT_UNKNOWN')
        return url

    def checks(self, job):
        data = parse_json(gh(job['repository'], 'pr', 'view', job['branch'], '--json', 'headRefOid,statusCheckRollup,url,state'))
        if data['headRefOid'] != job['head'] or data['state'] != 'OPEN':
            raise AppError('STALE_REMOTE_HEAD')
        checks = []
        for row in data.get('statusCheckRollup') or []:
            status = row.get('conclusion') if row.get('status') == 'COMPLETED' else row.get('status') or row.get('state')
            checks.append({'name': row.get('name') or row.get('context'), 'status': status,
                           'url': row.get('detailsUrl') or row.get('targetUrl')})
        registry = Path(__file__).with_name('projects.json')
        if not registry.exists(): registry = Path(__file__).parent.parent / '.github/control-plane/projects.json'
        from common import read_json
        config = read_json(registry).get(job['repository'], {}) if registry.exists() else {}
        for required in config.get('program_required_checks', []):
            matches = [c for c in checks if c['name'] == required]
            if not matches: checks.append({'name': required, 'status': 'EXPECTED', 'url': None})
            elif any(c['status'] in ('SKIPPED', 'NEUTRAL') for c in matches):
                checks.append({'name': required, 'status': 'FAILURE', 'url': None})
        failed = [c for c in checks if c['status'] in ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE')]
        pending = [c for c in checks if c['status'] not in ('SUCCESS', 'NEUTRAL', 'SKIPPED') and c not in failed]
        # An empty API observation is not proof that configured CI does not exist.
        if not checks:
            workflows = self.path(job) / '.github/workflows'
            if workflows.exists() and any(p.suffix in ('.yml', '.yaml') for p in workflows.iterdir()):
                return {'state': 'pending', 'checks': [{'name': 'repository workflows', 'status': 'EXPECTED', 'url': None}]}
        return {'state': 'failed' if failed else 'pending' if pending else 'passed' if checks else 'not_configured',
                'checks': checks}
