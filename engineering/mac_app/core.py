"""Persistent local delivery loop. One writer; fresh reviewers; final human acceptance.

This is the Mac app's draft-delivery runtime, not a replacement source of legacy
protected host pins. It never merges, deploys or fabricates a legacy A3 receipt.
"""
from __future__ import annotations

import copy
import fcntl
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

import agents
from common import (AppError, DEFAULTS, TERMINAL, atomic_json, digest, encoded,
                    parse_json, private_directory, read_json, repository, text,
                    validate_plan, validate_settings)
from gitops import Repositories

DEFAULT_GOAL = '레포의 공식 문서에 명시된 산출물을 완성하고 테스트와 독립 검토를 거쳐 최종 검수할 수 있게 해 주세요. 일반적인 구현 판단은 직접 하고, 꼭 필요한 경우에만 질문해 주세요.'
ACTIVE = ('queued', 'preparing', 'planning', 'building', 'reviewing', 'supervising', 'publishing', 'verifying', 'waiting_provider')
PROVIDER_SETUP = ('MODEL_UNAVAILABLE', 'PROVIDER_LOGIN_REQUIRED', 'CLI_SETUP_REQUIRED', 'PROVIDER_PERMISSION_OR_RESULT_ERROR')
ROLE_STATE = {'planner': 'planning', 'builder': 'building', 'reviewer': 'reviewing', 'supervisor': 'supervising'}


class Store:
    def __init__(self, directory):
        self.directory = private_directory(directory)
        self.lock = threading.RLock()
        self.execution_busy = set()
        path = self.directory / 'app.sqlite3'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd); os.close(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise AppError('PRIVATE_DATABASE_REQUIRED')
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                repository TEXT NOT NULL, state TEXT NOT NULL, document TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
        ''')
        self.db.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (encoded(DEFAULTS),))

    def close(self): self.db.close()

    def settings(self):
        with self.lock:
            return parse_json(self.db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])

    def set_settings(self, value):
        value = validate_settings(value)
        with self.lock: self.db.execute('UPDATE settings SET value=? WHERE id=1', (encoded(value),))
        return value

    def jobs(self):
        with self.lock:
            return [parse_json(row[0]) for row in self.db.execute('SELECT document FROM jobs ORDER BY created DESC')]

    def get(self, key):
        with self.lock: row = self.db.execute('SELECT document FROM jobs WHERE id=?', (key,)).fetchone()
        if not row: raise AppError('JOB_NOT_FOUND')
        return parse_json(row[0])

    def events(self, key, after=0):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 500', (key, after))]

    def event(self, key, kind, message):
        with self.lock:
            self.db.execute('INSERT INTO events(job_id,created,kind,message) VALUES (?,?,?,?)', (key, time.time(), kind, message[:6000]))

    def update(self, key, **fields):
        with self.lock:
            job = self.get(key); job.update(fields); job['updated'] = time.time()
            self.db.execute('UPDATE jobs SET state=?, document=? WHERE id=?', (job['state'], encoded(job), key))
            return job

    def create(self, value):
        if not isinstance(value, dict) or set(value) - {'repository', 'goal', 'request_id'}:
            raise AppError('INVALID_JOB_REQUEST')
        repo = repository(value.get('repository'))
        goal = text(value.get('goal') or DEFAULT_GOAL, 'goal')
        rid = value.get('request_id')
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', rid):
            raise AppError('REQUEST_ID_REQUIRED')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                prior = self.db.execute('SELECT document FROM jobs WHERE request_id=?', (rid,)).fetchone()
                if prior:
                    job = parse_json(prior[0])
                    if (job['repository'].lower(), job['goal']) != (repo.lower(), goal): raise AppError('REQUEST_ID_CONFLICT')
                    self.db.execute('COMMIT'); return job
                busy = self.db.execute("SELECT id FROM jobs WHERE lower(repository)=lower(?) AND state NOT IN ('accepted','cancelled')", (repo,)).fetchone()
                if busy: raise AppError('REPOSITORY_BUSY', '이 레포의 기존 작업을 먼저 검수하거나 이어서 진행해 주세요.')
                key = uuid.uuid4().hex[:16]; now = time.time()
                job = {'id': key, 'request_id': rid, 'repository': repo, 'goal': goal,
                       'state': 'queued', 'phase': 'preparing', 'created': now, 'updated': now,
                       'settings': self.settings(), 'branch': 'aiops/mac-' + key,
                       'base_sha': None, 'head': None, 'plan': None, 'source_pins': {},
                       'task_index': 0, 'built_tasks': [], 'feedback': [], 'user_answers': [],
                       'attempt': None, 'calls': 0, 'pause_requested': False, 'correcting': False,
                       'not_before': 0, 'failures': 0, 'pr_url': None, 'summary': '', 'question': None,
                       'review': None, 'supervision': None, 'ci': None, 'provider_error': None}
                self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', (key, rid, repo, job['state'], encoded(job), now))
                self.event(key, 'created', '작업을 맡았습니다. 레포를 읽고 계획부터 세웁니다.')
                self.db.execute('COMMIT'); return job
            except Exception:
                self.db.execute('ROLLBACK'); raise

    def action(self, key, action, value=None):
        with self.lock:
            job = self.get(key)
            if action != 'pause' and key in self.execution_busy:
                raise AppError('STEP_IN_PROGRESS', '현재 단계를 정리 중입니다. 잠시 뒤 다시 시도해 주세요.')
            if action == 'pause':
                if job['state'] not in ACTIVE: raise AppError('NOT_RUNNING')
                fields = {'pause_requested': True}
                if not job['attempt'] and key not in self.execution_busy: fields['state'] = 'paused'
                message = '현재 단계를 마친 뒤 잠시 멈춥니다.'
            elif action == 'resume':
                if job['state'] not in ('paused', 'needs_user', 'waiting_provider') or job['attempt']:
                    raise AppError('CANNOT_RESUME', '실행 결과가 불명확한 작업은 재실행할 수 없습니다.')
                answers = job['user_answers']
                if job['state'] == 'needs_user':
                    answer = text((value or {}).get('answer'), 'answer')
                    answers = [*answers, {'question': job['question'], 'answer': answer, 'at': time.time()}]
                fields = {'state': job['phase'], 'pause_requested': False, 'question': None,
                          'not_before': 0, 'user_answers': answers, 'provider_error': None}
                if job['settings']['max_agent_calls'] and job['calls'] >= job['settings']['max_agent_calls']:
                    new_limit = self.settings()['max_agent_calls']
                    if new_limit and new_limit <= job['calls']:
                        raise AppError('CALL_LIMIT_REACHED', '모델 설정에서 실행 한도를 늘린 뒤 이어서 진행해 주세요.')
                    settings = copy.deepcopy(job['settings']); settings['max_agent_calls'] = new_limit
                    fields['settings'] = settings
                message = '같은 작업과 모델 구성으로 이어서 진행합니다.'
            elif action == 'reconfigure':
                if job['state'] not in ('paused', 'needs_user', 'waiting_provider') or job['attempt']:
                    raise AppError('PAUSE_BEFORE_MODEL_CHANGE')
                settings = self.settings()
                phase = 'reviewing' if job['phase'] in ('reviewing', 'supervising', 'publishing', 'verifying') else job['phase']
                fields = {'settings': settings, 'review': None, 'supervision': None, 'ci': None, 'phase': phase}
                if job.get('provider_error') in PROVIDER_SETUP:
                    fields.update(state=phase, question=None, provider_error=None, not_before=0, pause_requested=False)
                # A model change cannot answer a consequential decision for the user.
                self.event(key, 'model_profile_replaced', encoded({'old': job['settings']['roles'], 'new': settings['roles']}))
                message = '사용자가 이 작업에 새 모델 설정을 적용했습니다. 필요한 검토는 다시 수행합니다.'
            elif action == 'accept':
                if job['state'] != 'ready': raise AppError('NOT_READY_FOR_ACCEPTANCE')
                fields = {'state': 'accepted', 'accepted_head': job['head'], 'accepted_at': time.time()}
                message = '사용자가 결과를 검수했습니다. 병합·배포는 수행하지 않았습니다.'
            elif action == 'revise':
                if job['state'] != 'ready': raise AppError('NOT_READY_FOR_REVISION')
                feedback = text((value or {}).get('feedback'), 'feedback')
                fields = {'state': 'building', 'phase': 'building', 'correcting': True,
                          'feedback': [feedback], 'review': None, 'supervision': None, 'ci': None}
                message = '검수 의견을 반영하고 다시 검증합니다.'
            elif action == 'cancel':
                if job['attempt'] or job['state'] in ('unknown', 'accepted', 'cancelled'):
                    raise AppError('CANNOT_CANCEL_ACTIVE_ATTEMPT')
                fields = {'state': 'cancelled'}; message = '작업을 취소했습니다. 코드와 기록은 보관합니다.'
            else: raise AppError('UNKNOWN_ACTION')
            job = self.update(key, **fields); self.event(key, action, message); return job


class Engine:
    def __init__(self, store, repos=None):
        self.store = store; self.repos = repos or Repositories(store.directory / 'workspaces')
        self.stopping = threading.Event(); self.wake = threading.Event(); self.children = {}
        self.awake = None

    def tick(self):
        for process in list(self.children.values()): process.poll()
        jobs = sorted(self.store.jobs(), key=lambda x: x['created'])
        inflight = [j for j in jobs if j['attempt']]
        # An uncertain outstanding writer blocks the whole local lane, including
        # another repository. Never treat a missing receipt as permission to retry.
        candidates = inflight or [j for j in jobs if j['state'] in ACTIVE and j['not_before'] <= time.time()]
        if not candidates: self.keep_awake(False); return False
        with self.store.lock:
            job = self.store.get(candidates[0]['id'])
            if job['id'] in self.store.execution_busy or (not job['attempt'] and job['state'] not in ACTIVE):
                return False
            if job['state'] == 'unknown': return False
            self.store.execution_busy.add(job['id'])
        self.keep_awake(job['settings']['keep_awake'])
        try: self.step(job)
        except AppError as exc:
            self.store.event(job['id'], 'blocker', str(exc))
            if self.store.get(job['id'])['attempt']:
                self.store.update(job['id'], state='unknown', question='작업 실행 기록 확인이 필요합니다. ' + str(exc))
            elif exc.code in ('COMMAND_FAILED', 'COMMAND_TIMEOUT'):
                self.store.update(job['id'], state='waiting_provider', not_before=time.time() + 300)
            else:
                self.store.update(job['id'], state='needs_user', question=str(exc))
                self.notify(job)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.store.event(job['id'], 'blocker', type(exc).__name__)
            self.store.update(job['id'], state='unknown' if self.store.get(job['id'])['attempt'] else 'needs_user',
                              question='실행 환경 확인이 필요합니다: ' + type(exc).__name__)
        finally:
            with self.store.lock: self.store.execution_busy.discard(job['id'])
        return True

    def keep_awake(self, enabled):
        if sys.platform != 'darwin': return
        if enabled and (not self.awake or self.awake.poll() is not None):
            try:
                self.awake = subprocess.Popen(['/usr/bin/caffeinate', '-is', '-w', str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError: self.awake = None
        elif not enabled and self.awake:
            try:
                self.awake.terminate(); self.awake.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired): pass
            self.awake = None

    def notify(self, job, ready=False):
        if sys.platform != 'darwin': return
        message = '결과가 준비되었습니다. AIOPS에서 최종 검수해 주세요.' if ready else '꼭 필요한 확인이 있습니다. AIOPS에서 질문을 확인해 주세요.'
        script = 'on run argv\ndisplay notification (item 1 of argv) with title "AIOPS" subtitle (item 2 of argv)\nend run'
        try:
            subprocess.run(['/usr/bin/osascript', '-e', script, message, job['repository']],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired): pass

    def run(self):
        while not self.stopping.is_set():
            self.tick(); self.wake.wait(2); self.wake.clear()
        self.keep_awake(False)

    def step(self, job):
        if job['attempt']: return self.observe(job)
        if job['pause_requested']:
            self.store.update(job['id'], state='paused'); return
        phase = job['phase']
        if phase == 'preparing':
            self.store.update(job['id'], state='preparing')
            pins = self.repos.prepare(job)
            self.store.update(job['id'], **pins, state='planning', phase='planning')
            self.store.event(job['id'], 'prepared', '레포를 별도 작업 공간에 준비했습니다.')
        elif phase in ('planning', 'building', 'reviewing', 'supervising'):
            role = next(key for key, value in ROLE_STATE.items() if value == phase)
            self.launch(job, role)
        elif phase == 'publishing':
            if self.repos.head(job) != job['head'] or not self.repos.clean(job):
                return self.rework(job, ['검토 후 코드가 변경되었습니다. 변경을 확인하고 전체 검증을 다시 실행하세요.'])
            sync = self.repos.synchronize_base(job)
            if sync['changed']:
                self.store.update(job['id'], verified_base=sync['base'], head=self.repos.head(job))
                return self.rework(job, ['기준 브랜치가 변경되었습니다. 최신 변경을 통합하고 충돌을 해결한 뒤 전체 테스트를 다시 실행하세요.'])
            if not job['settings']['publish_pr'] or job['head'] == job['base_sha']:
                self.store.update(job['id'], state='ready', ci={'state': 'not_published', 'checks': []})
                self.store.event(job['id'], 'ready', '로컬 결과가 최종 검수를 기다립니다. GitHub CI는 확인하지 않았습니다.')
                self.notify(job, ready=True)
                return
            # Push is idempotent at one branch/head; PR creation is reconciled by
            # that unique branch. An uncertain response is never blindly retried.
            self.store.update(job['id'], state='publishing', publishing_started=True)
            url = self.repos.publish(job)
            self.store.update(job['id'], pr_url=url, state='verifying', phase='verifying', not_before=time.time() + 60)
            self.store.event(job['id'], 'published', '검수용 PR을 만들었습니다. GitHub 검증 결과를 확인합니다.')
        elif phase == 'verifying':
            data = self.repos.checks(job)
            self.store.update(job['id'], ci=data)
            if data['state'] == 'failed':
                self.rework(job, ['GitHub CI 실패. 실패 원인을 수정하세요: ' + encoded(data['checks'])])
            elif data['state'] == 'pending':
                self.store.update(job['id'], not_before=time.time() + 60)
            else:
                self.store.update(job['id'], state='ready', not_before=0)
                self.store.event(job['id'], 'ready', '개발·감사·감리를 마쳤습니다. 최종 결과를 검수해 주세요.')
                self.notify(job, ready=True)
        else: raise AppError('UNKNOWN_PHASE')

    def launch(self, job, role):
        with self.store.lock:
            job = self.store.get(job['id'])
            if job['attempt'] or job['state'] not in ACTIVE or job['phase'] != ROLE_STATE[role]: return
            if job['pause_requested']:
                self.store.update(job['id'], state='paused'); return
            self._launch(job, role)

    def _launch(self, job, role):
        maximum = job['settings']['max_agent_calls']
        if maximum and job['calls'] >= maximum:
            self.store.update(job['id'], state='paused', question='설정한 모델 실행 한도에 도달했습니다.')
            self.store.event(job['id'], 'limit', '설정한 실행 한도에 도달해 멈췄습니다.'); return
        # Probe command construction before reserving: no model or shell is run.
        folder = private_directory(self.store.directory / 'jobs' / job['id'])
        attempt_id = uuid.uuid4().hex
        attempt_dir = private_directory(folder / attempt_id)
        profile = job['settings']['roles'][role]
        agents.command(profile, role, attempt_dir, checkout=self.repos.path(job))
        self.repos.assert_binding(job)
        if role != 'builder' and not self.repos.clean(job): raise AppError('READ_ONLY_INPUT_IS_DIRTY')
        head = self.repos.head(job)
        task = None
        if role == 'builder':
            task = {'id': 'integration-rework', 'title': '검토 의견과 통합 문제 수정',
                    'instructions': '모든 피드백을 해결하고 전체 산출물을 유지하세요.',
                    'acceptance': ['모든 피드백 해결', '전체 회귀 검증 통과'], 'depends_on': []} if job['correcting'] else job['plan']['tasks'][job['task_index']]
        context = dict(job, current_task=task)
        binding = digest({'job': job['id'], 'attempt': attempt_id, 'head': head, 'role': role,
                          'profile': profile, 'plan': job['plan'], 'task': task})
        request = {'attempt_id': attempt_id, 'binding': binding, 'role': role,
                   'profile': profile, 'checkout': str(self.repos.path(job).resolve()),
                   'prompt': agents.prompt(context, role, head), 'timeout_seconds': job['settings']['session_minutes'] * 60}
        atomic_json(attempt_dir / 'request.json', request); atomic_json(attempt_dir / 'schema.json', agents.SCHEMA)
        attempt = {'id': attempt_id, 'binding': binding, 'head': head, 'role': role,
                   'started': time.time(), 'timeout_seconds': request['timeout_seconds'], 'task': task}
        self.store.update(job['id'], state=ROLE_STATE[role], attempt=attempt, calls=job['calls'] + 1)
        self.store.event(job['id'], role, {'planner': '레포를 읽고 실행 계획을 작성합니다.',
            'builder': '개발과 테스트를 진행합니다.', 'reviewer': '별도 세션에서 코드와 근거를 감사합니다.',
            'supervisor': '산출물 전체를 독립적으로 감리합니다.'}[role])
        # Once admission is persisted, a crash leaves an UNKNOWN fence unless a
        # bound terminal worker receipt is available after the service restarts.
        try:
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('worker.py')), '--attempt', str(attempt_dir)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, env=agents.environment())
            self.children[attempt_id] = child
        except OSError:
            self.store.update(job['id'], attempt=None)
            raise AppError('WORKER_SPAWN_FAILED')

    def observe(self, job):
        attempt = job['attempt']; folder = self.store.directory / 'jobs' / job['id'] / attempt['id']
        receipt_path = folder / 'receipt.json'
        if not receipt_path.exists():
            if time.time() > attempt['started'] + attempt['timeout_seconds'] + 120:
                raise AppError('WORKER_OUTCOME_UNKNOWN', '실행 결과가 확인되지 않았습니다. 중복 실행을 막기 위해 멈췄습니다.')
            return
        receipt = read_json(receipt_path)
        if receipt.get('attempt_id') != attempt['id'] or receipt.get('binding') != attempt['binding']:
            raise AppError('RECEIPT_BINDING_MISMATCH')
        if receipt.get('process_group_quiescent') is not True:
            raise AppError('WORKER_OUTCOME_UNKNOWN', '실행 프로세스 종료 상태가 확인되지 않았습니다.')
        # A failed provider can still have edited the checkout. Check before
        # clearing any reservation, including login/setup and malformed output.
        if attempt['role'] != 'builder' and (self.repos.head(job) != attempt['head'] or not self.repos.clean(job)):
            raise AppError('READ_ONLY_ROLE_MODIFIED_CHECKOUT')
        if receipt.get('error'):
            error = receipt['error']
            if error == 'CHILD_PROCESS_GROUP_NOT_QUIESCENT':
                raise AppError('WORKER_OUTCOME_UNKNOWN', '작업 프로세스 종료 상태를 확인해야 합니다: ' + error)
            if error in PROVIDER_SETUP:
                message = {'MODEL_UNAVAILABLE': '선택한 모델을 이 계정에서 사용할 수 없습니다. 모델 설정을 고친 뒤 이 작업에 새 설정을 적용해 주세요.',
                           'PROVIDER_LOGIN_REQUIRED': '선택한 실행 도구에 로그인이 필요합니다. Mac에서 로그인한 뒤 이어서 진행해 주세요.',
                           'CLI_SETUP_REQUIRED': '선택한 실행 도구의 버전 또는 sandbox 설정 확인이 필요합니다.',
                           'PROVIDER_PERMISSION_OR_RESULT_ERROR': '실행 도구의 필수 권한 또는 구조화된 결과 설정을 확인해야 합니다.'}[error]
                self.store.update(job['id'], attempt=None, state='needs_user', provider_error=error, question=message)
                self.store.event(job['id'], 'setup_required', message); self.notify(job); return
            delay = min(3600, 300 * 2 ** min(job['failures'], 4))
            self.store.update(job['id'], attempt=None, state='waiting_provider', not_before=time.time() + delay,
                              failures=job['failures'] + 1)
            self.store.event(job['id'], 'provider_wait', '실행 도구가 결과를 반환하지 못했습니다. 같은 모델로 나중에 다시 시도합니다. ' + error)
            return
        report = receipt['report']; role = attempt['role']
        if not isinstance(report, dict) or report.get('status') not in ('complete', 'fail', 'needs_user'):
            raise AppError('INVALID_RECEIPT_REPORT')
        provider_evidence = receipt.get('provider_evidence')
        if provider_evidence is not None:
            profile = job['settings']['roles'][role]
            if (not isinstance(provider_evidence, dict) or provider_evidence.get('provider') != profile['provider'] or
                    provider_evidence.get('harness') != agents.CATALOG[profile['provider']]['harness'] or
                    provider_evidence.get('model_requested') != profile['model']):
                raise AppError('PROVIDER_PROFILE_MISMATCH')
        self.store.update(job['id'], attempt=None, failures=0, not_before=0, summary=report['summary'],
                          last_provider_evidence=provider_evidence)
        self.store.event(job['id'], 'result', report['summary'])
        if report['status'] == 'needs_user':
            self.store.update(job['id'], state='needs_user', question=report['question']); self.notify(job); return
        if report['status'] == 'fail':
            feedback = report['findings'] or [report['summary']]
            if role == 'planner':
                self.store.update(job['id'], state='planning', feedback=feedback, not_before=time.time() + 10)
            elif role == 'builder':
                self.store.update(job['id'], state='building', feedback=feedback)
            else: self.rework(job, feedback)
            return
        if role == 'planner':
            try:
                plan = validate_plan(report['plan']); pins = self.repos.source_pins(job, plan)
            except AppError as exc:
                self.store.update(job['id'], state='planning', feedback=['계획을 수정하세요: ' + str(exc)], not_before=time.time() + 10); return
            self.store.update(job['id'], plan=plan, source_pins=pins, state='building', phase='building', feedback=[])
            self.store.event(job['id'], 'plan', str(len(plan['tasks'])) + '개 단계로 계획을 세웠습니다. 바로 개발을 진행합니다.')
        elif role == 'builder':
            current = self.store.get(job['id']); current['current_task'] = attempt['task']
            head = self.repos.checkpoint(current)
            built = list(job['built_tasks'])
            if not job['correcting']: built.append(attempt['task']['id'])
            index = job['task_index'] if job['correcting'] else job['task_index'] + 1
            phase = 'reviewing' if job['correcting'] or index >= len(job['plan']['tasks']) else 'building'
            self.store.update(job['id'], head=head, built_tasks=built, task_index=index, phase=phase, state=phase,
                              feedback=[], correcting=False, review=None, supervision=None, ci=None)
        else:
            expected = {task['id'] for task in job['plan']['tasks']}
            if report.get('reviewed_head') != attempt['head'] or set(report.get('covered_tasks') or []) != expected or not report.get('checks') or report.get('findings'):
                self.store.update(job['id'], state=ROLE_STATE[role], feedback=['정확한 HEAD와 모든 task id, 실제 검증 근거가 필요합니다. 미해결 사항은 fail로 반환하세요.'], not_before=time.time() + 10)
                return
            evidence = {'head': attempt['head'], 'attempt': attempt['id'], 'profile': job['settings']['roles'][role],
                        'report': report, 'provider_evidence': provider_evidence}
            if role == 'reviewer':
                self.store.update(job['id'], review=evidence, phase='supervising', state='supervising', feedback=[])
            else:
                if not job['review'] or job['review']['head'] != attempt['head'] or job['review']['attempt'] == attempt['id']:
                    raise AppError('INDEPENDENT_REVIEW_REQUIRED')
                self.store.update(job['id'], supervision=evidence, phase='publishing', state='publishing', feedback=[])

    def rework(self, job, feedback):
        self.store.update(job['id'], state='building', phase='building', correcting=True,
                          feedback=feedback, review=None, supervision=None, ci=None, not_before=0)
        self.store.event(job['id'], 'rework', '개발자가 검토 의견을 반영하고 테스트를 다시 진행합니다.')


def service_lock(directory):
    path = Path(directory) / 'service.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd); raise AppError('SERVICE_ALREADY_RUNNING')
    return fd
