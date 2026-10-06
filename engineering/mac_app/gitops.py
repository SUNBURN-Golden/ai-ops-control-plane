"""GitHub I/O owned by the app. Agents never receive a GitHub token in their env."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import subprocess
import time

import agents
import admission
from common import AppError, digest, encoded, parse_json, read_json
from program_scope import load_scope

# The original roadmap-sync explicitly names this status header. These pins
# permit exactly its reviewed two-line alignment, retaining every body byte.
# They do not authorize another decision, scope revision, repository or path.
ROADMAP_HEADER = {
    'repository': 'BeautifulMind-JT/kix-protocol', 'node': 'roadmap-sync',
    'base': '5155ed307c71917ba3442fc5e1fc4cb950efefdc',
    'plan_blob': 'ff0f39a8129ca8b8d30818cce35c3d4e588872fc',
    'spec_sha256': '6a732abbfd4833c02d3e474088035417665ca27f7ae17aae9e8383537564fe41',
    'path': 'docs/decisions/TOKEN_LAYER_AND_RIGHTS_SCALE_SCOPE_20260929.md',
    'before_blob': 'ae63ff63d25c08e21bc61769d5a2c235b865afef',
    'after_blob': '2ceb88df12d1344b7d8e8eeb7738526a3a150c6e',
}


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


def prepare_host_checkout(checkout, branch, base_sha, plan_blob):
    """Fetch as the trusted host without moving the immutable admitted base."""
    if not isinstance(branch,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*',branch):
        raise AppError('MAC_HOST_BASE_BRANCH_INVALID')
    git(checkout,'check-ref-format','refs/heads/'+branch)
    before=git(checkout,'rev-parse','HEAD')
    if git(checkout,'rev-parse',base_sha+':.aiops/program.json')!=plan_blob:
        raise AppError('MAC_HOST_PROGRAM_REVISION_CHANGED')
    git(checkout,'fetch','--no-tags','origin',branch,timeout=120)
    fetched=git(checkout,'rev-parse','FETCH_HEAD')
    if fetched!=base_sha or git(checkout,'rev-parse','HEAD')!=before:
        raise AppError('MAC_HOST_PROGRAM_REVISION_CHANGED')
    return {'operation':'git fetch origin '+branch,'branch':branch,'base_sha':base_sha,
            'plan_blob':plan_blob,'checkout_head':before,'fetched_head':fetched,'at':time.time()}


def supervision_binding(job):
    return digest({key:job.get(key) for key in
                   ('id','head','pr_url','ci','review','plan','native_lineage')})


class Repositories:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, job):
        if job.get('native_lineage'):
            lineage=job['native_lineage']
            if any(not isinstance(lineage.get(k),str) or not re.fullmatch(r'[0-9a-f]{32}',lineage[k]) for k in ('request_id','attempt_id')):
                raise AppError('MAC_HOST_DELIVERY_BINDING_MISMATCH')
            path=self.directory.parent / 'native' / lineage['request_id'] / lineage['attempt_id'] / 'checkout'
            if path.is_symlink() or not path.resolve().is_relative_to(self.directory.parent.resolve()):
                raise AppError('MAC_HOST_DELIVERY_BINDING_MISMATCH')
            return path
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
        # Cloning and pinning a dedicated branch does not admit a provider.
        # Execution admission is checked on every launch, including resumed jobs.
        git(None, 'clone', '--no-local', '--single-branch', '--branch', branch,
            'https://github.com/' + job['repository'] + '.git', str(checkout), timeout=600)
        git(checkout, 'config', 'user.name', 'AIOPS Mac')
        git(checkout, 'config', 'user.email', 'aiops-mac@users.noreply.github.com')
        git(checkout, 'checkout', '-b', job['branch'])
        return {'base_sha': self.head(job), 'base_branch': branch, 'head': self.head(job)}

    def execution_admission(self, job):
        registry = Path(__file__).with_name('projects.json')
        if not registry.exists(): registry = Path(__file__).parent.parent / '.github/control-plane/projects.json'
        if not registry.is_file(): raise AppError('ADMISSION_REGISTRY_UNAVAILABLE')
        projects = read_json(registry)
        if not isinstance(projects, dict): raise AppError('ADMISSION_REGISTRY_UNAVAILABLE')
        managed = any(name.lower() == job['repository'].lower() for name in projects)
        # Older prepared jobs may predate persisted program_scope. Re-read the
        # pinned manifest rather than inferring authority from a missing field.
        if not job.get('program_scope') and job.get('base_sha'):
            job = dict(job, program_scope=self.program_scope(job))
        return admission.observe(job, managed, execute)

    def head(self, job):
        return git(self.path(job), 'rev-parse', 'HEAD')

    def prepare_native(self,job):
        self.assert_binding(job);self.assert_scope(job)
        bound=job['native_lineage']['binding']
        return prepare_host_checkout(self.path(job),job['base_branch'],bound['plan_commit'],bound['plan_blob'])

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

    def approved_roadmap_header(self, job, name):
        """Match one frozen original spec and exact before/after document bytes."""
        pin = ROADMAP_HEADER
        if name != pin['path'] or job.get('repository') != pin['repository']: return False
        try:
            bound = job['native_lineage']['binding']
            if (bound['authority_kind'] != 'MAC_LOCAL' or bound['repository'] != pin['repository'] or
                bound['node'] != pin['node'] or bound['plan_commit'] != pin['base'] or
                bound['plan_blob'] != pin['plan_blob'] or job['base_sha'] != pin['base'] or
                job['program_scope']['blob'] != pin['plan_blob']): return False
            scope = self.program_scope(job)
            if not scope or scope['blob'] != pin['plan_blob']: return False
            nodes = [node for node in scope['nodes'] if node['id'] == pin['node']]
            tasks = job['plan']['tasks']
            if len(nodes) != 1 or len(tasks) != 1: return False
            node, task = nodes[0], tasks[0]
            if (node.get('audit_floor') != 'A1' or node.get('astra_gate', 'NONE') != 'NONE' or
                task['id'] != node['id'] or task['title'] != node['title'] or
                task['instructions'] != node['spec'] or
                hashlib.sha256(node['spec'].encode()).hexdigest() != pin['spec_sha256']): return False
            checkout = self.path(job); target = checkout / name
            if (target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1 or
                target.stat().st_mode & 0o111 or
                not target.resolve().is_relative_to(checkout.resolve())): return False
            return (git(checkout, 'rev-parse', pin['base'] + ':' + name) == pin['before_blob'] and
                    git(checkout, 'hash-object', '--no-filters', '--', name) == pin['after_blob'])
        except (KeyError, TypeError, AttributeError, OSError, AppError):
            return False

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
                if not self.approved_roadmap_header(job, name):
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
        generation = 'generation_id' in (job.get('native_lineage') or {}).get('binding',{})
        if generation:
            from mac_generation import POLICY
            if job.get('generation_policy') != POLICY: raise AppError('MAC_GENERATION_PUBLICATION_POLICY_REQUIRED')
        self.assert_binding(job)
        if self.head(job) != job['head'] or not self.clean(job): raise AppError('STALE_PUBLISH_HEAD')
        if job.get('native_lineage'):
            metadata=parse_json(gh(job['repository'],'repo','view','--json','nameWithOwner,isPrivate'))
            if not isinstance(metadata,dict) or metadata.get('nameWithOwner','').lower()!=job['repository'].lower() or metadata.get('isPrivate') is not True:
                raise AppError('MAC_HOST_CANDIDATE_PUBLICATION_SCOPE_REQUIRED')
        checkout = self.path(job)
        git(checkout, 'push', '--porcelain', 'origin', 'HEAD:refs/heads/' + job['branch'], timeout=180)
        fields='url,state,headRefOid'+(',isDraft,autoMergeRequest' if generation else '')
        prior = parse_json(gh(job['repository'], 'pr', 'list', '--head', job['branch'], '--state', 'all',
                              '--json', fields, '--limit', '10'))
        if prior:
            if len(prior) != 1 or prior[0]['state'] != 'OPEN' or prior[0]['headRefOid'] != job['head']:
                raise AppError('PR_BINDING_MISMATCH')
            if generation and (prior[0].get('isDraft') is not True or prior[0].get('autoMergeRequest') is not None):
                raise AppError('MAC_GENERATION_PUBLICATION_POLICY_REQUIRED')
            return prior[0]['url']
        body = '\n'.join([
            '## 요청과 결과', job['goal'], '', job.get('summary', ''), '',
            '## 검증', f"- 작업: `{job['id']}`", f"- 기준: `{job['base_sha']}`", f"- 검토한 HEAD: `{job['head']}`",
            '- 작성 세션과 분리된 검토 근거를 앱에 보관합니다. 기존 보호 서비스의 A3 증명이나 CI 결과를 대체하지 않습니다.',
            '- Draft 후보입니다. 실제 CI·필수 감사·최종 감리 완료 전에는 검수 준비 상태가 아닙니다. 자동 병합·배포는 수행하지 않습니다.', '',
            '## 계획', *[f"- {task['title']}: {'; '.join(task['acceptance'])}" for task in job['plan']['tasks']], '',
            '## 모델 구성', *[f"- {role}: {config['provider']} / {config['model'] or 'CLI configured default'}" for role, config in job['settings']['roles'].items()],
        ])
        if generation:
            bound=job['native_lineage']['binding']
            body += ('\n\n## Mac 새 실행 세대\n' +
                     f"- generation: `{bound['generation_id']}`\n- canonical task: `{bound['task_id']}` / `{bound['task_revision']}`\n" +
                     f"- original scope: `{bound['program']}` / `{bound['node']}` at `{bound['plan_commit']}`\n" +
                     '- 기존 작업의 소유권·종료·UNKNOWN 상태는 변경하지 않았습니다. 새 Mac 작업의 원문 기술 명세를 고정했습니다.\n' +
                     '- 자동 병합은 금지되어 있습니다. Draft 상태·사용자 병합 승인 규칙을 유지합니다.')
        path = self.directory.parent / 'jobs' / job['id'] / 'pr-body.md'
        path.write_text(body, encoding='utf-8')
        url = gh(job['repository'], 'pr', 'create', '--draft', '--base', job['base_branch'],
                 '--head', job['branch'], '--title', 'AIOPS: ' + job['goal'].split('\n')[0][:120], '--body-file', str(path))
        if not re.fullmatch(r'https://github\.com/' + re.escape(job['repository']) + r'/pull/[0-9]+', url):
            raise AppError('PUBLISH_RECEIPT_UNKNOWN')
        return url

    def checks(self, job):
        fields='headRefOid,statusCheckRollup,url,state'
        if job.get('native_lineage'): fields+=',isDraft,autoMergeRequest'
        data = parse_json(gh(job['repository'], 'pr', 'view', job['branch'], '--json', fields))
        if data['headRefOid'] != job['head'] or data['state'] != 'OPEN':
            raise AppError('STALE_REMOTE_HEAD')
        if job.get('native_lineage'):
            approval=(job.get('user_merge') or {}).get('approval') or {}
            human_ready=job['state']=='accepted' and approval.get('head')==job['head'] and bool(approval.get('approval'))
            if (data.get('isDraft') is not True and not human_ready) or data.get('autoMergeRequest') is not None:
                raise AppError('MAC_GENERATION_PUBLICATION_POLICY_REQUIRED')
            return self.hosted_checks(job,job['head'])
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

    def candidate_workflows(self,job):
        """Bound product verification only; no control-plane/VM dispatch."""
        if job['repository'].lower()!='beautifulmind-jt/kix-protocol':
            raise AppError('MAC_HOST_DRAFT_CI_ROUTE_REQUIRED')
        workflows=['ktx-kernel.yml','protocol.yml']
        for name in workflows:
            raw=git(self.path(job),'show',job['head']+':.github/workflows/'+name)
            if not re.search(r'(?m)^  workflow_dispatch:\s*$',raw):
                raise AppError('MAC_HOST_DRAFT_CI_ROUTE_REQUIRED')
        return workflows

    def dispatch_candidate_workflow(self,job,workflow):
        from handoff import api
        if workflow not in ('ktx-kernel.yml','protocol.yml'): raise AppError('MAC_HOST_DRAFT_CI_ROUTE_REQUIRED')
        if workflow not in self.candidate_workflows(job): raise AppError('MAC_HOST_DRAFT_CI_ROUTE_REQUIRED')
        self.assert_binding(job)
        if self.head(job)!=job['head'] or not self.clean(job): raise AppError('STALE_PUBLISH_HEAD')
        repo=job['repository']; match=re.fullmatch(r'https://github\.com/'+re.escape(repo)+r'/pull/([0-9]+)',job['pr_url'] or '')
        if not match: raise AppError('MAC_HOST_CANDIDATE_BINDING_UNVERIFIED')
        pull=api('repos/'+repo+'/pulls/'+match.group(1))
        ref=api('repos/'+repo+'/git/ref/heads/'+job['branch'])
        target=api('repos/'+repo+'/actions/workflows/'+workflow)
        if not (pull.get('state')=='open' and pull.get('draft') is True and pull.get('auto_merge') is None and
                pull.get('head',{}).get('sha')==job['head'] and pull.get('head',{}).get('ref')==job['branch'] and
                pull.get('base',{}).get('ref')==job['base_branch'] and ref.get('object',{}).get('sha')==job['head'] and
                target.get('path')=='.github/workflows/'+workflow and target.get('state')=='active'):
            raise AppError('MAC_HOST_CANDIDATE_BINDING_UNVERIFIED')
        execute(['gh','api','--method','POST','repos/'+repo+'/actions/workflows/'+workflow+'/dispatches',
                 '-f','ref='+job['branch']])

    def hosted_checks(self,job,head,*,post_merge=False):
        """Mac node CI comes from real GitHub Actions checks/runs, not status text."""
        from handoff import api
        repo=job['repository']; data=api('repos/'+repo+'/commits/'+head+'/check-runs?per_page=100')
        if (not isinstance(data,dict) or type(data.get('total_count')) is not int or not 0<=data['total_count']<=100 or
                not isinstance(data.get('check_runs'),list) or len(data['check_runs'])!=data['total_count']):
            raise AppError('MAC_HOST_CI_OBSERVATION_UNVERIFIED')
        checks=[]; runs={}
        for check in data['check_runs']:
            if check.get('head_sha')!=head: raise AppError('MAC_HOST_CI_OBSERVATION_UNVERIFIED')
            if (check.get('app') or {}).get('slug')!='github-actions': continue
            match=re.fullmatch(r'https://github\.com/'+re.escape(repo)+r'/actions/runs/([0-9]+)(?:/job/[0-9]+)?',check.get('details_url') or '')
            if not match: raise AppError('MAC_HOST_CI_OBSERVATION_UNVERIFIED')
            run_id=match.group(1)
            if run_id not in runs: runs[run_id]=api('repos/'+repo+'/actions/runs/'+run_id)
            run=runs[run_id]
            if run.get('head_sha')!=head or (run.get('repository') or {}).get('full_name','').lower()!=repo.lower():
                raise AppError('MAC_HOST_CI_OBSERVATION_UNVERIFIED')
            status=check.get('conclusion') if check.get('status')=='completed' else check.get('status')
            # A Draft's skipped PR checks are not passes or code failures. The
            # exact-head manual product verification supplies the missing jobs.
            if run.get('event')=='pull_request' and status in ('skipped','neutral'): continue
            if status=='success' and (run.get('status')!='completed' or run.get('conclusion')!='success'):
                status='in_progress' if run.get('status')!='completed' else 'failure'
            checks.append({'name':check.get('name'),'status':str(status).upper(),'url':check['details_url'],
                           'head':head,'run_id':int(run_id),'event':run.get('event'),
                           'workflow_name':run.get('name'),'run_status':run.get('status'),
                           'run_conclusion':run.get('conclusion')})
        registry=Path(__file__).with_name('projects.json')
        if not registry.exists(): registry=Path(__file__).parent.parent / '.github/control-plane/projects.json'
        config=read_json(registry).get(repo,{}) if registry.exists() else {}
        key='program_post_merge_required_checks' if post_merge else 'program_required_checks'
        for required in config.get(key,[]):
            matches=[c for c in checks if c['name']==required]
            if not matches: checks.append({'name':required,'status':'EXPECTED','url':None,'head':head})
            elif any(c['status'] in ('SKIPPED','NEUTRAL') for c in matches):
                checks.append({'name':required,'status':'FAILURE','url':None,'head':head})
        failed=any(c['status'] in ('FAILURE','ERROR','TIMED_OUT','CANCELLED','ACTION_REQUIRED','STARTUP_FAILURE') for c in checks)
        pending=not any(c['status']=='SUCCESS' for c in checks) or any(c['status'] not in ('SUCCESS','SKIPPED','NEUTRAL') for c in checks)
        return {'state':'failed' if failed else 'pending' if pending else 'passed','checks':checks,'head':head,
                'source':'GITHUB_ACTIONS_API'}

    def supervision_evidence(self,job):
        """Sanitized live reads for the isolated inspector; never a model verdict."""
        from handoff import api
        def require(value):
            if not value: raise AppError('MAC_HOST_LIVE_CI_REQUIRED')
        repo,head=job['repository'],job['head']
        match=re.fullmatch(r'https://github\.com/'+re.escape(repo)+r'/pull/([0-9]+)',job.get('pr_url') or '')
        require(job.get('native_lineage') and match and job.get('candidate_published_head')==head)
        paths={'.github/workflows/'+name for name in self.candidate_workflows(job)}
        def pull():
            value=api('repos/'+repo+'/pulls/'+match.group(1))
            require(value.get('html_url')==job['pr_url'] and value.get('state')=='open' and
                    value.get('draft') is True and value.get('auto_merge') is None and
                    value.get('head',{}).get('sha')==head and value['head'].get('ref')==job['branch'] and
                    value.get('base',{}).get('ref')==job['base_branch'])
            return {'url':value['html_url'],'number':int(match.group(1)),'state':value['state'],
                    'draft':value['draft'],'auto_merge':None,'head':head,'head_branch':value['head']['ref'],
                    'base_branch':value['base']['ref'],'base_head':value['base']['sha']}
        before=pull();ci=self.checks(job)
        require(ci.get('state')=='passed' and ci.get('head')==head and ci.get('source')=='GITHUB_ACTIONS_API')
        runs=[];seen=set()
        for check in ci['checks']:
            if check.get('status')!='SUCCESS' or check.get('run_id') in seen: continue
            run_id=check.get('run_id'); require(type(run_id) is int and run_id>0)
            seen.add(run_id);run=api('repos/'+repo+'/actions/runs/'+str(run_id))
            require(run.get('head_sha')==head and run.get('head_branch')==job['branch'] and
                    run.get('repository',{}).get('full_name','').lower()==repo.lower() and run.get('path') in paths and
                    run.get('status')=='completed' and run.get('conclusion')=='success' and
                    run.get('html_url')=='https://github.com/'+repo+'/actions/runs/'+str(run_id))
            response=api('repos/'+repo+'/actions/runs/'+str(run_id)+'/jobs?per_page=100')
            jobs=response.get('jobs');require(isinstance(jobs,list) and type(response.get('total_count')) is int and
                0<len(jobs)==response['total_count']<=100)
            selected=[row for row in jobs if row.get('name')==check['name']]
            require(len(selected)==1);row=selected[0]
            require(row.get('head_sha')==head and row.get('status')=='completed' and row.get('conclusion')=='success' and
                    type(row.get('id')) is int and row['id']>0 and
                    row.get('html_url')==run['html_url']+'/job/'+str(row['id']))
            steps=row.get('steps');require(isinstance(steps,list) and 0<len(steps)<=256)
            for step in steps:
                require(isinstance(step,dict) and isinstance(step.get('name'),str) and 0<len(step['name'])<=512 and
                        type(step.get('number')) is int and step['number']>0 and step.get('status')=='completed' and
                        step.get('conclusion') in ('success','skipped','neutral','failure','cancelled','timed_out'))
            runs.append({'id':run_id,'url':run['html_url'],'workflow':run['path'],'event':run['event'],
                         'head':head,'head_branch':run['head_branch'],'status':run['status'],'conclusion':run['conclusion'],
                         'job':{'id':row['id'],'name':row['name'],'url':row['html_url'],'head':head,
                                'status':row['status'],'conclusion':row['conclusion'],
                                'steps':[{k:s.get(k) for k in ('name','number','status','conclusion','started_at','completed_at')}
                                         for s in steps]}})
        require({run['workflow'] for run in runs}==paths)
        require(pull()==before)
        return {'schema_version':1,'source':'AUTHENTICATED_GITHUB_API','observed_at':time.time(),
                'job_binding':supervision_binding(job),'repository':repo,'head':head,'pr':before,'runs':runs}

    def merge_candidate(self,job):
        from handoff import api
        match=re.fullmatch(r'https://github\.com/'+re.escape(job['repository'])+r'/pull/([0-9]+)',job['pr_url'] or '')
        if not match: raise AppError('USER_MERGE_BINDING_UNVERIFIED')
        pull=api('repos/'+job['repository']+'/pulls/'+match.group(1))
        if not (pull.get('head',{}).get('sha')==job['head'] and pull.get('head',{}).get('ref')==job['branch'] and
                pull.get('base',{}).get('ref')==job['base_branch'] and pull.get('auto_merge') is None):
            raise AppError('USER_MERGE_HEAD_CHANGED')
        if pull.get('merged') is not True:
            if pull.get('state')!='open' or pull.get('base',{}).get('sha')!=job.get('verified_base',job['base_sha']):
                raise AppError('USER_MERGE_BASE_CHANGED')
        return {'merged':pull.get('merged') is True,'draft':pull.get('draft') is True,
                'merge_head':pull.get('merge_commit_sha'),'number':int(match.group(1))}

    def user_ready(self,job):
        pull=self.merge_candidate(job)
        if pull['merged'] or not pull['draft']: return
        gh(job['repository'],'pr','ready',str(pull['number']))

    def user_merge(self,job):
        pull=self.merge_candidate(job)
        if pull['merged']: return pull
        if pull['draft']: raise AppError('USER_READY_REQUIRED')
        result=parse_json(execute(['gh','api','--method','PUT',
            'repos/'+job['repository']+'/pulls/'+str(pull['number'])+'/merge',
            '-f','sha='+job['head'],'-f','merge_method=merge']))
        if result.get('merged') is not True: raise AppError('USER_MERGE_OUTCOME_UNKNOWN')
        observed=self.merge_candidate(job)
        if not observed['merged'] or observed['merge_head']!=result.get('sha'):
            raise AppError('USER_MERGE_OUTCOME_UNKNOWN')
        return observed

    def merged(self,job):
        from handoff import api
        repo=job['repository']; match=re.fullmatch(r'https://github\.com/'+re.escape(repo)+r'/pull/([0-9]+)',job['pr_url'] or '')
        if not match: raise AppError('MAC_HOST_MERGE_BINDING_UNVERIFIED')
        pull=api('repos/'+repo+'/pulls/'+match.group(1)); merge=pull.get('merge_commit_sha')
        if not (pull.get('merged') is True and pull.get('state')=='closed' and pull.get('head',{}).get('sha')==job['head'] and
                pull.get('base',{}).get('ref')==job['base_branch'] and isinstance(merge,str) and re.fullmatch(r'[0-9a-f]{40}',merge)):
            raise AppError('MAC_HOST_MERGE_BINDING_UNVERIFIED')
        git(self.path(job),'fetch','origin',job['base_branch'])
        latest=git(self.path(job),'rev-parse','FETCH_HEAD')
        if git(self.path(job),'merge-base',merge,latest)!=merge: raise AppError('MAC_HOST_MERGE_BINDING_UNVERIFIED')
        if git(self.path(job),'rev-parse',merge+'^2')!=job['head']:
            raise AppError('MAC_HOST_MERGE_BINDING_UNVERIFIED')
        registry=Path(__file__).with_name('projects.json')
        if not registry.exists(): registry=Path(__file__).parent.parent/'.github/control-plane/projects.json'
        config=read_json(registry).get(repo,{}) if registry.exists() else {}
        for path,blob in config.get('program_post_merge_locked_blobs',{}).items():
            if git(self.path(job),'rev-parse',merge+':'+path)!=blob: raise AppError('MAC_HOST_POST_MERGE_LOCK_CHANGED')
        ci=self.hosted_checks(job,merge,post_merge=True)
        if ci['state']!='passed': raise AppError('MAC_HOST_POST_MERGE_CI_REQUIRED')
        return {'pr_url':job['pr_url'],'reviewed_head':job['head'],'merge_head':merge,'default_head':latest,
                'post_merge_ci':ci,'source':'AUTHENTICATED_GITHUB_READ'}

    def dependency_completion(self,job,plan_commit,*,current=True):
        """Reobserve one recorded completion; never merge or edit its ledger.

        The normal merge verifier supplies protected-lock/postmerge checks.
        A dependent admission additionally binds the reviewed tree, original
        base, target ancestry and the exact recorded premerge Actions jobs.
        """
        from handoff import api
        proof=self.merged(job); merge=proof['merge_head']; checkout=self.path(job)
        if (current and proof['default_head']!=plan_commit or
                git(checkout,'merge-base',merge,plan_commit)!=merge or
                git(checkout,'rev-parse',merge+'^1')!=job['base_sha'] or
                git(checkout,'rev-parse',merge+'^{tree}')!=git(checkout,'rev-parse',job['head']+'^{tree}')):
            raise AppError('MAC_HOST_DEPENDENCY_MERGE_UNVERIFIED')
        recorded=job['ci']; runs=[]
        registry=Path(__file__).with_name('projects.json')
        if not registry.exists(): registry=Path(__file__).parent.parent/'.github/control-plane/projects.json'
        config=read_json(registry).get(job['repository'],{}) if registry.exists() else {}
        passed={c.get('name') for c in recorded['checks'] if c.get('status')=='SUCCESS'}
        if not (recorded.get('state')=='passed' and recorded.get('head')==job['head'] and
                recorded.get('source')=='GITHUB_ACTIONS_API' and
                set(config.get('program_required_checks',[]))<=passed):
            raise AppError('MAC_HOST_DEPENDENCY_CI_UNVERIFIED')
        for check in recorded['checks']:
            if check.get('status')!='SUCCESS': continue
            url=check.get('url') or ''
            match=re.fullmatch(r'https://github\.com/'+re.escape(job['repository'])+
                               r'/actions/runs/([0-9]+)(?:/job/([0-9]+))?',url)
            if not match or check.get('run_id')!=int(match.group(1)):
                raise AppError('MAC_HOST_DEPENDENCY_CI_UNVERIFIED')
            prefix='repos/'+job['repository']+'/actions/'
            run=api(prefix+'runs/'+match.group(1))
            if match.group(2):
                selected=api(prefix+'jobs/'+match.group(2))
            else:
                items=api(prefix+'runs/'+match.group(1)+'/jobs?per_page=100')
                if items.get('total_count')!=len(items.get('jobs',[])) or items['total_count']>100:
                    raise AppError('MAC_HOST_DEPENDENCY_CI_UNVERIFIED')
                matches=[item for item in items['jobs'] if item.get('name')==check.get('name')]
                if len(matches)!=1: raise AppError('MAC_HOST_DEPENDENCY_CI_UNVERIFIED')
                selected=matches[0]
            steps=selected.get('steps')
            if not (run.get('id')==check['run_id'] and run.get('head_sha')==job['head'] and
                    (run.get('repository') or {}).get('full_name','').lower()==job['repository'].lower() and
                    run.get('status')=='completed' and run.get('conclusion')=='success' and
                    run.get('event')==check.get('event') and run.get('name')==check.get('workflow_name') and
                    selected.get('run_id')==run['id'] and
                    (not match.group(2) or selected.get('id')==int(match.group(2))) and
                    selected.get('head_sha')==job['head'] and selected.get('name')==check.get('name') and
                    selected.get('status')=='completed' and selected.get('conclusion')=='success' and
                    isinstance(steps,list) and steps and any(s.get('conclusion')=='success' for s in steps) and
                    all(s.get('status')=='completed' and s.get('conclusion') in ('success','skipped') for s in steps)):
                raise AppError('MAC_HOST_DEPENDENCY_CI_UNVERIFIED')
            runs.append({'run_id':run['id'],'job_id':selected['id'],'head':job['head'],
                         'run_attempt':run.get('run_attempt'),'name':selected['name'],
                         'steps_sha256':digest(steps)})
        if not runs: raise AppError('MAC_HOST_DEPENDENCY_CI_UNVERIFIED')
        return {**proof,'tree':git(checkout,'rev-parse',merge+'^{tree}'),
                'target_plan_commit':plan_commit,'recorded_ci_sha256':digest(recorded),'runs':runs}
