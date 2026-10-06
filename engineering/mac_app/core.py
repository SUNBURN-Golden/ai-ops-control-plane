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
import admission
import handoff
import transport_guard
import native_transfer
from common import (AppError, DEFAULTS, TERMINAL, EVENT_RECORD_LIMIT, JOB_RECORD_LIMIT,
                    JOB_CONTROL_RESERVE, WORKER_REQUEST_LIMIT, atomic_json, digest, encoded,
                    parse_json, private_directory, read_json, repository, text,
                    validate_plan, validate_settings)
from gitops import Repositories, supervision_binding
from program_scope import bind_specs, validate_coverage

DEFAULT_GOAL = '레포의 공식 문서에 명시된 산출물을 완성하고 테스트와 독립 검토를 거쳐 최종 검수할 수 있게 해 주세요. 일반적인 구현 판단은 직접 하고, 꼭 필요한 경우에만 질문해 주세요.'
ACTIVE = ('queued', 'preparing', 'planning', 'building', 'reviewing', 'supervising', 'publishing', 'verifying', 'waiting_provider')
PROVIDER_SETUP = ('MODEL_UNAVAILABLE', 'PROVIDER_LOGIN_REQUIRED', 'CLI_SETUP_REQUIRED', 'PROVIDER_PERMISSION_OR_RESULT_ERROR',
                  'PROVIDER_PERMISSION_REQUIRED', 'PROVIDER_USAGE_LIMIT', 'MISSING_PROVIDER', 'WORKER_SPAWN_FAILED',
                  'MAC_CODEX_PROFILE_UNVERIFIED','MAC_CODEX_PROTOCOL_UNVERIFIED','MAC_CODEX_PERMISSION_REQUIRED',
                  'MAC_CODEX_TURN_TIMEOUT','MAC_CODEX_CONFIG_CHANGED','MAC_CODEX_TRUST_REQUIRED','MAC_CODEX_RUNTIME_UNQUALIFIED')
TRANSIENT_FAILURES = ('PROVIDER_TEMPORARILY_UNAVAILABLE', 'COMMAND_TIMEOUT', 'SESSION_TIMEOUT')
EXECUTION_BLOCKERS = ('HOST_ADMISSION_REQUIRED', 'ADMISSION_OBSERVATION_UNRESOLVED',
                      'TRANSPORT_EXECUTION_UNRESOLVED', 'TRANSPORT_JOURNAL_UNVERIFIED',
                      'MAC_HOST_ASTRA_AUDIT_REQUIRED', 'MAC_HOST_ASTRA_RECEIPT_UNVERIFIED',
                      'MAC_HOST_ASTRA_DECISION_UNVERIFIED', 'MAC_HOST_ASTRA_RECEIPT_CHANGED',
                      'MAC_HOST_ASTRA_RECEIPT_REPLAY', 'MAC_HOST_ASTRA_AUDIT_CONFLICT')
ROLE_STATE = {'planner': 'planning', 'builder': 'building', 'reviewer': 'reviewing', 'supervisor': 'supervising'}


def job_request(value):
    if not isinstance(value, dict) or set(value) - {'repository', 'goal', 'request_id'}:
        raise AppError('INVALID_JOB_REQUEST')
    repo = repository(value.get('repository'))
    goal = text(value.get('goal') or DEFAULT_GOAL, 'goal')
    rid = value.get('request_id')
    if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', rid):
        raise AppError('REQUEST_ID_REQUIRED')
    return repo, goal, rid


class Store:
    def new_document(self,key,rid,repo,goal):
        now=time.time()
        return {'id': key, 'request_id': rid, 'repository': repo, 'goal': goal,
                'state': 'queued', 'phase': 'preparing', 'created': now, 'updated': now,
                'settings': self.settings(), 'branch': 'aiops/mac-' + key,
                'base_sha': None, 'head': None, 'plan': None, 'source_pins': {},
                'task_index': 0, 'built_tasks': [], 'feedback': [], 'user_answers': [],
                'attempt': None, 'calls': 0, 'pause_requested': False, 'correcting': False,
                'not_before': 0, 'failures': 0, 'pr_url': None, 'summary': '', 'question': None,
                'review': None, 'supervision': None, 'ci': None, 'provider_error': None,
                'program_scope': None, 'blocker': None, 'failure_fingerprint': None,
                'last_terminal': None, 'admission': None}

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
            CREATE TABLE IF NOT EXISTS handoffs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                request TEXT NOT NULL, document TEXT NOT NULL, created REAL NOT NULL);
        ''')
        self.db.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (encoded(DEFAULTS),))

    def close(self):
        audit=getattr(self,'_mac_astra_db',None)
        if audit is not None:
            audit.close();self._mac_astra_db=None
        self.db.close()

    def settings(self):
        with self.lock:
            return parse_json(self.db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])

    def set_settings(self, value):
        value = validate_settings(value)
        with self.lock: self.db.execute('UPDATE settings SET value=? WHERE id=1', (encoded(value),))
        return value

    def jobs(self):
        with self.lock:
            return [parse_json(row[0], JOB_RECORD_LIMIT) for row in self.db.execute('SELECT document FROM jobs ORDER BY created DESC')]

    def get(self, key):
        with self.lock: row = self.db.execute('SELECT document FROM jobs WHERE id=?', (key,)).fetchone()
        if not row: raise AppError('JOB_NOT_FOUND')
        return parse_json(row[0], JOB_RECORD_LIMIT)

    @staticmethod
    def document(job):
        # Reserve room for a terminal receipt, pause or execution fence. A large
        # valid plan must not prevent the app from recording that it has stopped.
        control = {'attempt', 'blocker', 'question', 'provider_error', 'last_terminal',
                   'state', 'phase', 'updated', 'pause_requested', 'not_before', 'calls',
                   'failures', 'failure_fingerprint'}
        value = encoded(job)
        payload = encoded({key: item for key, item in job.items() if key not in control})
        if (len(value.encode()) > JOB_RECORD_LIMIT or
                len(payload.encode()) > JOB_RECORD_LIMIT - JOB_CONTROL_RESERVE):
            raise AppError('JOB_RECORD_TOO_LARGE',
                           '작업 기록이 저장 한도를 넘었습니다. 원래 계획을 줄이지 않고 실행 기록의 크기를 확인해야 합니다.')
        return value

    def events(self, key, after=0):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 500', (key, after))]

    def event(self, key, kind, message):
        if not isinstance(kind, str) or not 1 <= len(kind) <= 80 or not isinstance(message, str):
            raise AppError('INVALID_EVENT_RECORD')
        message = message[:6000]
        if len(encoded({'job_id': key, 'kind': kind, 'message': message}).encode()) > EVENT_RECORD_LIMIT:
            raise AppError('EVENT_RECORD_TOO_LARGE')
        with self.lock:
            self.db.execute('INSERT INTO events(job_id,created,kind,message) VALUES (?,?,?,?)', (key, time.time(), kind, message))

    def update(self, key, **fields):
        with self.lock:
            job = self.get(key); job.update(fields); job['updated'] = time.time()
            document = self.document(job)
            self.db.execute('UPDATE jobs SET state=?, document=? WHERE id=?', (job['state'], document, key))
            return job

    def create(self, value):
        repo, goal, rid = job_request(value)
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                prior = self.db.execute('SELECT document FROM jobs WHERE request_id=?', (rid,)).fetchone()
                if prior:
                    job = parse_json(prior[0], JOB_RECORD_LIMIT)
                    if (job['repository'].lower(), job['goal']) != (repo.lower(), goal): raise AppError('REQUEST_ID_CONFLICT')
                    self.db.execute('COMMIT'); return job
                transport_guard.require_clear(self.directory)
                if native_transfer.local_fenced(self.db): raise AppError('NATIVE_LOCAL_TRANSFER_UNRESOLVED')
                predecessors = [parse_json(row[0], JOB_RECORD_LIMIT) for row in self.db.execute(
                    'SELECT document FROM jobs WHERE lower(repository)=lower(?)', (repo,))]
                for previous in predecessors:
                    if previous.get('attempt') is not None or previous['state'] == 'unknown':
                        raise AppError('LOCAL_EXECUTION_UNRESOLVED', '이 레포의 로컬 실행 결과가 미확정입니다: ' + previous['id'])
                    if previous['state'] not in TERMINAL or previous['id'] in self.execution_busy:
                        raise AppError('REPOSITORY_BUSY', '이 레포의 기존 작업을 먼저 검수하거나 이어서 진행해 주세요: ' + previous['id'])
                inherited = self.known_admission(repo)
                key = uuid.uuid4().hex[:16]; now = time.time()
                job = self.new_document(key,rid,repo,goal)
                job.update(created=now,updated=now,admission=inherited)
                document = self.document(job)
                self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', (key, rid, repo, job['state'], document, now))
                self.event(key, 'created', '작업을 맡았습니다. 레포를 읽고 계획부터 세웁니다.')
                self.db.execute('COMMIT'); return job
            except Exception:
                self.db.execute('ROLLBACK'); raise

    def known_admission(self, repo):
        """Live local canonical evidence only; terminal state never clears it."""
        with self.lock:
            for row in self.db.execute('SELECT document FROM jobs WHERE lower(repository)=lower(?)', (repo,)):
                previous = parse_json(row[0], JOB_RECORD_LIMIT)
                prior = previous.get('admission') or {}
                if prior.get('mode') == 'host_required': return dict(prior)
                if previous.get('program_scope'):
                    scope = previous['program_scope']
                    return admission.host_required('canonical_program', program=scope['program'], blob=scope['blob'])
            for row in self.db.execute('SELECT document FROM handoffs'):
                record = parse_json(row[0], handoff.LIMIT)
                if record['repository'].lower() == repo.lower():
                    return admission.host_required('prepared_handoff', handoff_id=record['id'])
        return None

    def existing_request(self, value):
        repo, goal, rid = job_request(value)
        with self.lock:
            row = self.db.execute('SELECT document FROM jobs WHERE request_id=?', (rid,)).fetchone()
        if not row: return None
        job = parse_json(row[0], JOB_RECORD_LIMIT)
        if (job['repository'].lower(), job['goal']) != (repo.lower(), goal): raise AppError('REQUEST_ID_CONFLICT')
        return job

    def existing_handoff(self, value):
        value = handoff.request(value)
        with self.lock:
            row = self.db.execute('SELECT request,document FROM handoffs WHERE request_id=?',
                                  (value['request_id'],)).fetchone()
        if not row: return None
        original = parse_json(row['request'])
        if (original['repository'].lower(), original['source_job_id']) != (value['repository'].lower(), value['source_job_id']):
            raise AppError('REQUEST_ID_CONFLICT')
        return parse_json(row['document'], handoff.LIMIT)

    def create_handoff(self, value, snapshot):
        value = handoff.request(value)
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                prior = self.existing_handoff(value)
                if prior:
                    self.db.execute('COMMIT'); return prior
                if snapshot['repository'].lower() != value['repository'].lower() or snapshot['execution_allowed'] is not False:
                    raise AppError('INVALID_HANDOFF_SNAPSHOT')
                refs, blockers = handoff.local_references(self, value['repository'], value['source_job_id'])
                key = uuid.uuid4().hex[:16]; now = time.time()
                record = {**value, 'repository': snapshot['repository'], 'id': key, 'kind': 'legacy_plan_handoff', 'state': 'prepared',
                          'created': now, 'execution_allowed': False, 'snapshot': copy.deepcopy(snapshot),
                          'snapshot_sha256': digest(snapshot), 'source_jobs': refs,
                          'blockers': [*snapshot['blockers'], *blockers]}
                document = encoded(record)
                if len(document.encode()) > handoff.LIMIT: raise AppError('HANDOFF_RECORD_TOO_LARGE')
                self.db.execute('INSERT INTO handoffs VALUES (?,?,?,?,?)',
                                (key, value['request_id'], encoded(value), document, now))
                self.db.execute('COMMIT'); return record
            except Exception:
                self.db.execute('ROLLBACK'); raise

    def handoffs(self):
        with self.lock:
            return [parse_json(row[0], handoff.LIMIT) for row in
                    self.db.execute('SELECT document FROM handoffs ORDER BY created DESC')]

    def get_handoff(self, key):
        key = handoff.handoff_id(key)
        with self.lock:
            row = self.db.execute('SELECT document FROM handoffs WHERE id=?', (key,)).fetchone()
        if not row: raise AppError('HANDOFF_NOT_FOUND')
        return parse_json(row[0], handoff.LIMIT)

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
                if job['state'] == 'needs_user' and not job.get('provider_error'):
                    answer = text((value or {}).get('answer'), 'answer')
                    answers = [*answers, {'question': job['question'], 'answer': answer, 'at': time.time()}]
                fields = {'state': job['phase'], 'pause_requested': False, 'question': None,
                          'not_before': 0, 'user_answers': answers, 'provider_error': None,
                          'blocker': None, 'failures': 0, 'failure_fingerprint': None}
                terminal = job.get('last_terminal') or {}
                if (job.get('native_lineage') and job['phase'] == 'building' and
                    (job.get('blocker') or {}).get('code') == 'AUTHORITY_EDIT_NEEDS_USER' and
                    terminal.get('role') == 'builder' and terminal.get('status') == 'complete' and
                    terminal.get('exit_code') == 0):
                    fields['checkpoint_retry'] = {'attempt': terminal['attempt'], 'head': terminal['head']}
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
                if job.get('native_lineage') and settings['roles']['builder']!=job['settings']['roles']['builder']:
                    raise AppError('MAC_HOST_OWNER_PROFILE_IMMUTABLE')
                phase = 'reviewing' if job['phase'] in ('reviewing', 'supervising', 'publishing', 'verifying') else job['phase']
                fields = {'settings': settings, 'review': None, 'supervision': None, 'ci': None, 'phase': phase}
                if job.get('provider_error') in PROVIDER_SETUP:
                    fields.update(state=phase, question=None, provider_error=None, not_before=0, pause_requested=False,
                                  blocker=None, failures=0, failure_fingerprint=None)
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
    def __init__(self, store, repos=None, canonical=None):
        self.store = store; self.repos = repos or Repositories(store.directory / 'workspaces')
        self.stopping = threading.Event(); self.wake = threading.Event(); self.children = {}
        self.awake = None
        self.canonical = canonical if canonical is not None else native_transfer.Controller(store)
        from mac_pipeline import Pipeline
        self.pipeline=Pipeline(self.store,self.canonical.source,self.repos,failure=self.operational_failure) if getattr(self.canonical.source,'mode',None)=='MAC' else None

    def tick(self):
        # Reap only Popen objects created by this host, including a native
        # wrapper whose final receipt reached us just before it exited.
        for process in list(getattr(getattr(self.canonical,'worker',None),'children',{}).values()): process.poll()
        if self.canonical.pending():
            self.keep_awake(self.store.settings()['keep_awake'])
            return self.canonical.tick()
        if self.pipeline and self.pipeline.deliver(): return True
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
            if job['state'] == 'unknown':
                # A late receipt may prove termination. Observation cannot reserve
                # or launch anything and must pass the same binding/exit fences.
                if not job['attempt'] or not self.receipt_path(job).exists(): return False
            self.store.execution_busy.add(job['id'])
        self.keep_awake(job['settings']['keep_awake'])
        try: self.step(job)
        except AppError as exc:
            current = self.store.get(job['id'])
            if current['attempt']:
                self.fence(current, exc.code)
            elif exc.code in ('COMMAND_FAILED', 'COMMAND_TIMEOUT', *PROVIDER_SETUP):
                self.operational_failure(current, exc.code)
            else:
                self.store.event(job['id'], 'blocker', str(exc))
                self.store.update(job['id'], state='needs_user', question=str(exc),
                                  blocker={'code': exc.code, 'phase': current['phase'], 'at': time.time()})
                self.notify(job)
        except (OSError, sqlite3.Error, ValueError) as exc:
            current = self.store.get(job['id'])
            if current['attempt']: self.fence(current, type(exc).__name__)
            else: self.operational_failure(current, type(exc).__name__)
        finally:
            with self.store.lock: self.store.execution_busy.discard(job['id'])
        return True

    def receipt_path(self, job):
        return self.store.directory / 'jobs' / job['id'] / job['attempt']['id'] / 'receipt.json'

    def describe(self, job, compact=False):
        """One observation of local evidence, without reading provider transcripts.

        Log bytes mean output was observed, never that useful work was completed.
        The worker's bound terminal receipt is the only way to free its reservation.
        """
        result = copy.deepcopy(job); now = time.time(); attempt = job.get('attempt')
        health = {'observed_at': now, 'status': job['state'], 'retry_at': None,
                  'implementation_count': len(job['built_tasks']),
                  'planned_count': len((job.get('plan') or {}).get('tasks', [])),
                  'program_count': (job.get('program_scope') or {}).get('count'),
                  'completion_kind': 'draft_delivery', 'last_terminal': job.get('last_terminal')}
        if attempt:
            deadline = attempt['started'] + attempt['timeout_seconds']
            folder = self.receipt_path(job).parent
            total = 0; last = None
            for name in ('stdout.log', 'stderr.log'):
                try: info = (folder / name).lstat()
                except OSError: continue
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid(): continue
                total += info.st_size
                if info.st_size: last = max(last or attempt['started'], min(now, info.st_mtime))
            health.update(status='outcome_unknown' if job['state'] == 'unknown' else 'finishing' if now > deadline
                          else 'quiet' if last is None or now-last > 300 else 'running',
                          role=attempt['role'], profile=job['settings']['roles'][attempt['role']],
                          elapsed_seconds=max(0, int(now-attempt['started'])), deadline_at=deadline,
                          output_bytes=total, last_output_at=last, attempt_id=attempt['id'])
        elif job['state'] in ACTIVE:
            blocking = next((j for j in self.store.jobs() if j['attempt'] and j['state'] == 'unknown'), None)
            if blocking: health.update(status='waiting_for_owner', blocking_job_id=blocking['id'], blocking_repository=blocking['repository'])
            elif job['state'] == 'waiting_provider': health.update(retry_at=job['not_before'], failure_count=job['failures'])
            elif job['not_before'] > now: health.update(status='feedback_wait', retry_at=job['not_before'])
        result['health'] = health
        if compact:
            # UI/relay projections retain scope IDs, acceptance and exact heads.
            # The authoritative full job stays intact in SQLite; list views do
            # not repeat every original spec and complete review report.
            result['projection'] = 'summary'
            if job.get('plan'):
                result['plan'] = {key: job['plan'][key] for key in ('summary', 'sources')}
                result['plan']['tasks'] = [{key: task[key] for key in ('id', 'title', 'acceptance', 'depends_on')}
                                          for task in job['plan']['tasks']]
            if job.get('program_scope'):
                result['program_scope'] = {key: value for key, value in job['program_scope'].items() if key != 'nodes'}
                result['program_scope']['nodes'] = [{key: value for key, value in node.items() if key != 'spec'}
                                                    for node in job['program_scope']['nodes']]
            for role in ('review', 'supervision'):
                evidence = job.get(role)
                if evidence:
                    result[role] = {key: value for key, value in evidence.items() if key != 'report'}
                    report = evidence.get('report') or {}
                    result[role].update(result=report.get('status'), summary=report.get('summary', ''),
                                        check_count=len(report.get('checks') or []))
            result.pop('user_answers', None)
            result['answer_count'] = len(job.get('user_answers') or [])
            if attempt and attempt.get('task'):
                result['attempt']['task'] = {key: attempt['task'][key] for key in ('id', 'title')}
        return result

    def fence(self, job, code):
        previous = job.get('blocker') or {}
        if job['state'] == 'unknown' and previous.get('code') == code: return
        self.store.update(job['id'], state='unknown', question='종료 근거가 확인될 때까지 중복 실행을 막습니다. 확인 코드: ' + code,
                          blocker={'code': code, 'phase': job['phase'], 'attempt': job['attempt']['id'], 'at': time.time()})
        self.store.event(job['id'], 'execution_unknown', '실행 종료 확인이 필요합니다: ' + code)
        if job['state'] != 'unknown': self.notify(job)

    def operational_failure(self, job, code):
        fingerprint = digest({'code': code, 'phase': job['phase'], 'task': job['task_index'], 'roles': job['settings']['roles']})
        failures = job['failures'] + 1 if job.get('failure_fingerprint') == fingerprint else 1
        messages = {
            'MODEL_UNAVAILABLE': '선택한 모델을 사용할 수 없습니다. 모델 설정을 확인해 주세요.',
            'PROVIDER_LOGIN_REQUIRED': '선택한 실행 도구에 Mac에서 로그인한 뒤 계속 진행해 주세요.',
            'CLI_SETUP_REQUIRED': '실행 도구의 버전과 sandbox 설정을 확인해 주세요.',
            'PROVIDER_PERMISSION_OR_RESULT_ERROR': '실행 도구의 필수 권한과 구조화된 결과 설정을 확인해 주세요.',
            'PROVIDER_PERMISSION_REQUIRED': '비대화형 실행 권한이 부족합니다. 권한을 확인한 뒤 계속 진행해 주세요.',
            'PROVIDER_USAGE_LIMIT': '선택한 계정의 사용 한도에 도달했습니다. 한도가 갱신된 뒤 계속 진행해 주세요.',
            'MISSING_PROVIDER': '선택한 실행 도구가 설치되어 있지 않습니다. 연결 화면을 확인해 주세요.',
            'WORKER_SPAWN_FAILED': '로컬 작업 프로세스를 시작하지 못했습니다. 실행 환경을 확인해 주세요.',
            'MAC_CODEX_PROFILE_UNVERIFIED': 'Codex의 실제 권한 profile과 기존 sandbox 설정 또는 활성 도구가 AIOPS의 배정 범위와 일치하지 않습니다. 설정을 보존하고 해당 경계를 확인해야 합니다.',
            'MAC_CODEX_PROTOCOL_UNVERIFIED': 'Codex의 공식 실행 응답을 검증하지 못했습니다. 같은 작업을 재호출하지 않고 런타임을 확인해야 합니다.',
            'MAC_CODEX_CONFIG_CHANGED': 'Codex 설정의 비대상 변경이 감지됐습니다. 변경을 덮어쓰지 않고 같은 작업에서 기다립니다.',
            'MAC_CODEX_TRUST_REQUIRED': '이 checkout 한 곳의 Codex 신뢰 등록에 대한 사용자 승인이 필요합니다. 다른 설정은 보존됩니다.',
            'MAC_CODEX_RUNTIME_UNQUALIFIED': '설치된 Codex 실행 파일이 검증된 버전과 다릅니다. 같은 작업을 재호출하지 않고 호환성을 확인해야 합니다.',
            'MAC_CODEX_PERMISSION_REQUIRED': 'Codex가 추가 권한 또는 배정 밖 도구 사용을 요청했습니다. 요청을 거절하고 같은 작업에서 기다립니다.',
            'MAC_CODEX_TURN_TIMEOUT': 'Codex 작업이 세션 제한에 도달해 정상 중지했습니다. 같은 작업을 자동으로 재호출하지 않고 기다립니다.',
            'HOST_ADMISSION_REQUIRED': '기존 canonical 작업의 Mac 실행 승인·이관 경로가 아직 없습니다. 기존 소유권과 계획을 보존하고 실행 승인을 확인해야 합니다.',
            'ADMISSION_OBSERVATION_UNRESOLVED': '기존 작업의 실행 승인 관측이 불완전합니다. 실제 소유권 근거를 확인해야 합니다.',
            'TRANSPORT_EXECUTION_UNRESOLVED': '기존 전달의 실제 실행·종료가 미확인입니다. 영수증을 보존하고 인증된 대사 근거를 확인해야 합니다.',
            'TRANSPORT_JOURNAL_UNVERIFIED': '기존 전달 원장을 확인할 수 없습니다. 원본을 보존하고 읽기 상태를 확인해야 합니다.',
            'MAC_HOST_ASTRA_AUDIT_REQUIRED': '현재 작업 revision과 HEAD의 Fable 감사가 필요합니다. 고정 Linux 도구가 감사 요청 이후 게시한 PR 댓글을 확인한 뒤 같은 작업을 계속하세요.',
            'MAC_HOST_ASTRA_RECEIPT_UNVERIFIED': '인증된 GitHub 감사 댓글을 검증할 수 없어 보류합니다. 작성자·PR·HEAD·schema·깊이와 실제 조회 상태를 확인하세요.',
            'MAC_HOST_ASTRA_DECISION_UNVERIFIED': 'Mac A3 전달 방식의 고정 사용자 결정 댓글을 검증할 수 없어 보류합니다.',
            'MAC_HOST_ASTRA_RECEIPT_CHANGED': '감사 댓글 또는 작업 요청이 변경·삭제되어 보류합니다. 기존 영수증을 덮어쓰지 않고 정확한 작업과 HEAD를 다시 확인하세요.',
            'MAC_HOST_ASTRA_RECEIPT_REPLAY': '다른 작업 revision·시도에 묶인 감사 댓글을 재사용할 수 없어 보류합니다.',
            'MAC_HOST_ASTRA_AUDIT_CONFLICT': '같은 HEAD의 Fable 감사 결과가 상충해 보류합니다. 이전 PASS를 선택하여 통과할 수 없습니다.',
        }
        # This holds infrastructure failures, never ordinary engineering FAILs.
        # Explicit transient outages remain automatic; unknown repeated faults
        # need a concrete environment check instead of spending calls forever.
        blocked = code in PROVIDER_SETUP or code in EXECUTION_BLOCKERS or (code not in TRANSIENT_FAILURES and failures >= 3)
        delay = min(3600, 300 * 2 ** min(failures - 1, 4))
        now = time.time()
        message = messages.get(code, '같은 실행 오류가 반복되었습니다. 실행 도구를 확인한 뒤 같은 작업을 계속 진행해 주세요.')
        blocker = {'code': code, 'phase': job['phase'], 'at': now, 'count': failures,
                   'retry_at': None if blocked else now + delay}
        self.store.update(job['id'], attempt=None, failures=failures, failure_fingerprint=fingerprint,
                          state='needs_user' if blocked else 'waiting_provider', provider_error=code,
                          question=message if blocked else None, blocker=blocker, not_before=0 if blocked else now + delay,
                          last_terminal={'attempt': job['attempt']['id'], 'role': job['attempt']['role'],
                          'head': job['attempt']['head'], 'at': now, 'error': code, 'status': 'error'}
                          if job['attempt'] else job.get('last_terminal'))
        self.store.event(job['id'], 'setup_required' if blocked else 'provider_wait',
                         message if blocked else '같은 도구와 작업으로 연결을 다시 시도합니다. 확인 코드: ' + code)
        if blocked: self.notify(job)

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
        if job.get('native_lineage'): self.pipeline.assert_job(job,refresh=True)
        phase = job['phase']
        if job.get('checkpoint_retry'):
            return self.retry_checkpoint(job)
        # A resumed old supervisor question retains its receipts and reviewer.
        # Prepare the candidate instead of repeating a model before live CI.
        if job.get('native_lineage') and phase=='supervising' and not job.get('candidate_published_head'):
            self.pipeline.validate_candidate(job,refresh=False)
            self.store.update(job['id'],phase='publishing',state='publishing')
            self.store.event(job['id'],'candidate_preparation','같은 검토 HEAD의 Draft 후보와 CI를 최종 감리 전에 준비합니다.')
            return
        if phase == 'preparing':
            self.store.update(job['id'], state='preparing')
            pins = self.repos.prepare(job)
            prepared = dict(job, **pins)
            scope = self.repos.program_scope(prepared)
            self.store.update(job['id'], **pins, program_scope=scope, state='planning', phase='planning', blocker=None)
            self.store.event(job['id'], 'prepared', '레포를 별도 작업 공간에 준비했습니다.')
        elif phase in ('planning', 'building', 'reviewing', 'supervising'):
            role = next(key for key, value in ROLE_STATE.items() if value == phase)
            self.launch(job, role)
        elif phase == 'publishing':
            if job.get('native_lineage'):
                if job['head']==job['base_sha']: raise AppError('MAC_HOST_DELIVERABLE_REQUIRED')
                self.pipeline.validate_candidate(job,refresh=False)
            if self.repos.head(job) != job['head'] or not self.repos.clean(job):
                return self.rework(job, ['검토 후 코드가 변경되었습니다. 변경을 확인하고 전체 검증을 다시 실행하세요.'])
            sync = self.repos.synchronize_base(job)
            if sync['changed']:
                self.store.update(job['id'], verified_base=sync['base'], head=self.repos.head(job))
                return self.rework(job, ['기준 브랜치가 변경되었습니다. 최신 변경을 통합하고 충돌을 해결한 뒤 전체 테스트를 다시 실행하세요.'])
            if not job['settings']['publish_pr'] or job['head'] == job['base_sha']:
                if job.get('native_lineage'): raise AppError('MAC_HOST_DELIVERABLE_REQUIRED')
                self.store.update(job['id'], state='ready', ci={'state': 'not_published', 'checks': []})
                self.store.event(job['id'], 'ready', '로컬 결과가 최종 검수를 기다립니다. GitHub CI는 확인하지 않았습니다.')
                self.notify(job, ready=True)
                return
            # Push is idempotent at one branch/head; PR creation is reconciled by
            # that unique branch. An uncertain response is never blindly retried.
            self.store.update(job['id'], state='publishing', publishing_started=True)
            url = self.repos.publish(job)
            fields={'pr_url':url,'state':'verifying','phase':'verifying','not_before':time.time()+60}
            if job.get('native_lineage'): fields['candidate_published_head']=job['head']
            self.store.update(job['id'],**fields)
            self.store.event(job['id'], 'published', '검수용 PR을 만들었습니다. GitHub 검증 결과를 확인합니다.')
        elif phase == 'verifying':
            native=bool(job.get('native_lineage'))
            if native: self.pipeline.validate_candidate(job,refresh=False)
            if not native and not self.inspected_head(job):
                return self.rework(job, ['CI 확인 중 코드나 검토 근거가 달라졌습니다. 현재 코드 전체를 다시 검증하세요.'])
            data = self.repos.checks(job)
            job=self.store.update(job['id'], ci=data)
            if native and job.get('candidate_ci_requested')!=job['head']:
                if data['state']=='passed':
                    self.store.update(job['id'],candidate_ci_requested=job['head'])
                    job=self.store.get(job['id'])
                else:
                    self.request_candidate_ci(self.store.get(job['id']))
                    return
            if data['state'] == 'failed':
                self.rework(job, ['GitHub CI 실패. 실패 원인을 수정하세요: ' + encoded(data['checks'])])
            elif data['state'] == 'pending':
                self.store.update(job['id'], not_before=time.time() + 60)
            else:
                sync = self.repos.synchronize_base(job)
                if sync['changed']:
                    self.store.update(job['id'], verified_base=sync['base'], head=self.repos.head(job))
                    return self.rework(job, ['CI 확인 중 기준 브랜치가 변경되었습니다. 최신 기준에서 전체 검증을 다시 실행하세요.'])
                if native:
                    requirement=self.pipeline.validate_audit(job)
                    self.store.update(job['id'],audit_requirement=requirement)
                    if not job.get('supervision'):
                        self.store.update(job['id'],state='supervising',phase='supervising',not_before=0)
                        self.store.event(job['id'],'candidate_ci','Draft 후보의 실제 CI를 수집했습니다. 필수 감사 적용 범위와 최종 감리를 확인합니다.')
                        return
                    self.pipeline.validate_inspection(self.store.get(job['id']),refresh=False)
                self.store.update(job['id'], state='ready', not_before=0)
                self.store.event(job['id'], 'ready', '개발·감사·감리를 마쳤습니다. 최종 결과를 검수해 주세요.')
                self.notify(job, ready=True)
        else: raise AppError('UNKNOWN_PHASE')

    def request_candidate_ci(self,job):
        """One durable intent per head/workflow; uncertain sends never retry.

        Application-owned product CI, not a VM development/ownership workflow.
        """
        self.pipeline.validate_candidate(job,refresh=False)
        workflows=self.repos.candidate_workflows(job)
        receipts=list(job.get('candidate_ci_dispatches') or [])
        for workflow in workflows:
            prior=[r for r in receipts if r['head']==job['head'] and r['workflow']==workflow]
            if prior:
                if len(prior)!=1 or prior[0]['state']!='SUBMITTED':
                    raise AppError('MAC_HOST_CI_DISPATCH_UNKNOWN','CI 요청 결과가 미확인입니다. 기존 요청을 확인하기 전 재전송하지 않습니다.')
                continue
            item={'head':job['head'],'workflow':workflow,'state':'SUBMITTING','at':time.time()}
            receipts.append(item)
            self.store.update(job['id'],candidate_ci_dispatches=receipts)
            try: self.repos.dispatch_candidate_workflow(job,workflow)
            except Exception:
                item['state']='UNKNOWN'; self.store.update(job['id'],candidate_ci_dispatches=receipts)
                raise AppError('MAC_HOST_CI_DISPATCH_UNKNOWN','CI 요청 결과가 미확인입니다. 기존 요청을 확인하기 전 재전송하지 않습니다.')
            item['state']='SUBMITTED'; self.store.update(job['id'],candidate_ci_dispatches=receipts)
        self.store.update(job['id'],candidate_ci_requested=job['head'],not_before=time.time()+60)
        self.store.event(job['id'],'candidate_ci_requested','Draft 상태를 유지한 채 현재 HEAD의 제품 검증 workflow를 한 번 요청했습니다.')

    def launch(self, job, role):
        with self.store.lock:
            job = self.store.get(job['id'])
            if job['attempt'] or job['state'] not in ACTIVE or job['phase'] != ROLE_STATE[role]: return
            if job['pause_requested']:
                self.store.update(job['id'], state='paused'); return
            context = dict(job, admission=self.store.known_admission(job['repository']) or job.get('admission'))
        # Network reads must not prevent status/pause/cancel. Recheck everything
        # under the reservation lock afterwards, including a newly prepared handoff.
        try:
            if job.get('native_lineage'):
                self.pipeline.assert_job(context,refresh=True)
                preparation=self.repos.prepare_native(context)
                observation={'mode':'mac_local','binding':job['native_lineage']['binding']}
                verification=self.repos.supervision_evidence(context) if role=='supervisor' else None
                if role=='supervisor':
                    audit=self.pipeline.validate_audit(context)
                    verification={**verification,'astra_audit':audit}
            else:
                transport_guard.require_clear(self.store.directory)
                observation = self.repos.execution_admission(context)
        except (AppError, OSError, ValueError):
            with self.store.lock:
                current = self.store.get(job['id'])
                if current['attempt'] or current['state'] not in ACTIVE or current['phase'] != ROLE_STATE[role]: return
                if current['pause_requested']:
                    self.store.update(job['id'], state='paused'); return
            raise
        with self.store.lock:
            job = self.store.get(job['id'])
            observation = observation if job.get('native_lineage') else self.store.known_admission(job['repository']) or observation
            # Cancellation stops execution, not the retention of newly observed
            # canonical scope. A later request must not forget this observation.
            if observation.get('mode') == 'host_required':
                self.store.update(job['id'], admission=observation)
            if job['attempt'] or job['state'] not in ACTIVE or job['phase'] != ROLE_STATE[role]: return
            if job['pause_requested']:
                self.store.update(job['id'], state='paused'); return
            self.store.update(job['id'], admission=observation)
            if job.get('native_lineage'):
                self.pipeline.assert_job(job,refresh=False)
                job=self.store.update(job['id'],host_preparation=preparation)
            else:
                admission.require_native(observation)
                transport_guard.require_clear(self.store.directory)
            if native_transfer.local_fenced(self.store.db): raise AppError('NATIVE_LOCAL_TRANSFER_UNRESOLVED')
            if job.get('native_lineage') and role=='supervisor':
                if verification['job_binding']!=supervision_binding(job) or verification['head']!=job['head']:
                    raise AppError('MAC_HOST_LIVE_CI_REQUIRED')
                if verification['astra_audit'].get('request_sha256')!=self.pipeline.audit_requirement(job).get('request_sha256'):
                    raise AppError('MAC_HOST_ASTRA_RECEIPT_CHANGED')
                job=dict(job,supervisor_verification=verification)
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
        self.repos.assert_scope(job)
        if role != 'builder' and not self.repos.clean(job): raise AppError('READ_ONLY_INPUT_IS_DIRTY')
        head = self.repos.head(job)
        task = None
        if role == 'builder': task = self.builder_task(job)
        context = dict(job, current_task=task)
        material={'job': job['id'], 'attempt': attempt_id, 'head': head, 'role': role,
                  'profile': profile, 'plan': job['plan'], 'task': task}
        verification=job.get('supervisor_verification')
        if verification:
            if role!='supervisor' or verification['head']!=head: raise AppError('MAC_HOST_LIVE_CI_REQUIRED')
            material['host_verification_sha256']=digest(verification)
        binding = digest(material)
        request = {'attempt_id': attempt_id, 'binding': binding, 'role': role,
                   'profile': profile, 'checkout': str(self.repos.path(job).resolve()),
                   'prompt': agents.prompt(context, role, head), 'timeout_seconds': job['settings']['session_minutes'] * 60}
        if job.get('native_lineage'): request['host_directory']=str(self.store.directory.resolve())
        if verification: request['host_verification_sha256']=material['host_verification_sha256']
        if len((encoded(request) + '\n').encode()) > WORKER_REQUEST_LIMIT:
            raise AppError('WORKER_REQUEST_TOO_LARGE',
                           '실행 입력이 한도를 넘었습니다. 원래 계획을 줄이지 않고 전달할 실행 기록의 크기를 확인해야 합니다.')
        atomic_json(attempt_dir / 'request.json', request); atomic_json(attempt_dir / 'schema.json', agents.SCHEMA)
        attempt = {'id': attempt_id, 'binding': binding, 'head': head, 'role': role,
                   'started': time.time(), 'timeout_seconds': request['timeout_seconds'], 'task': task}
        if verification: attempt['host_verification_sha256']=material['host_verification_sha256']
        self.store.update(job['id'], state=ROLE_STATE[role], attempt=attempt, calls=job['calls'] + 1)
        self.store.event(job['id'], role, {'planner': '레포를 읽고 실행 계획을 작성합니다.',
            'builder': '개발과 테스트를 진행합니다.', 'reviewer': '별도 세션에서 코드와 근거를 감사합니다.',
            'supervisor': '산출물 전체를 독립적으로 감리합니다.'}[role])
        # Once admission is persisted, a crash leaves an UNKNOWN fence unless a
        # bound terminal worker receipt is available after the service restarts.
        child = None
        try:
            # Preserve wrapper failures privately; provider stderr already has
            # its own log. Neither log is returned by the status API.
            fd = os.open(attempt_dir / 'worker-errors.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as errors:
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('worker.py')), '--attempt', str(attempt_dir)],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                         start_new_session=True, env=agents.environment())
            self.children[attempt_id] = child
        except OSError:
            if child is not None:
                self.children[attempt_id] = child
                raise AppError('WORKER_OUTCOME_UNKNOWN')
            self.store.update(job['id'], attempt=None)
            raise AppError('WORKER_SPAWN_FAILED')

    def observe(self, job):
        attempt = job['attempt']; folder = self.store.directory / 'jobs' / job['id'] / attempt['id']
        receipt_path = self.receipt_path(job)
        if not receipt_path.exists():
            child = self.children.get(attempt['id'])
            if child is not None and type(child.poll()) is int:
                raise AppError('WORKER_OUTCOME_UNKNOWN', '실행 감시 프로그램이 종료 기록 없이 끝났습니다. 중복 실행을 막기 위해 멈췄습니다.')
            if time.time() > attempt['started'] + attempt['timeout_seconds'] + 120:
                raise AppError('WORKER_OUTCOME_UNKNOWN', '실행 결과가 확인되지 않았습니다. 중복 실행을 막기 위해 멈췄습니다.')
            return
        receipt = read_json(receipt_path)
        if (not isinstance(receipt, dict) or
                not {'attempt_id', 'binding', 'exit_code', 'report', 'error', 'process_group_quiescent'} <= set(receipt)):
            raise AppError('INVALID_TERMINAL_RECEIPT')
        if receipt.get('attempt_id') != attempt['id'] or receipt.get('binding') != attempt['binding']:
            raise AppError('RECEIPT_BINDING_MISMATCH')
        if receipt.get('process_group_quiescent') is not True:
            raise AppError('WORKER_OUTCOME_UNKNOWN', '실행 프로세스 종료 상태가 확인되지 않았습니다.')
        # A failed provider can still have edited the checkout. Check before
        # clearing any reservation, including login/setup and malformed output.
        if attempt['role'] != 'builder' and (self.repos.head(job) != attempt['head'] or not self.repos.clean(job)):
            raise AppError('READ_ONLY_ROLE_MODIFIED_CHECKOUT')
        if receipt.get('error') is not None:
            error = receipt['error']
            if (not isinstance(error, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,127}', error) or
                    receipt.get('report') is not None):
                raise AppError('CONTRADICTORY_TERMINAL_RECEIPT')
            if error == 'CHILD_PROCESS_GROUP_NOT_QUIESCENT':
                raise AppError('WORKER_OUTCOME_UNKNOWN', '작업 프로세스 종료 상태를 확인해야 합니다: ' + error)
            self.operational_failure(job, error)
            return
        if type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0:
            raise AppError('CONTRADICTORY_TERMINAL_RECEIPT')
        report = receipt['report']; role = attempt['role']
        if not isinstance(report, dict) or report.get('status') not in ('complete', 'fail', 'needs_user'):
            raise AppError('INVALID_RECEIPT_REPORT')
        provider_evidence = receipt.get('provider_evidence')
        profile = job['settings']['roles'][role]
        if (not isinstance(provider_evidence, dict) or provider_evidence.get('provider') != profile['provider'] or
                provider_evidence.get('harness') != agents.CATALOG[profile['provider']]['harness'] or
                provider_evidence.get('model_requested') != profile['model']):
            raise AppError('PROVIDER_PROFILE_MISMATCH')
        agents.validate_report(report)
        if role=='builder' and job.get('native_lineage'):
            self.store.update(job['id'],builder_sessions=[*job.get('builder_sessions',[]),provider_evidence.get('session_id')])
        if report['status'] == 'complete' and (report['findings'] or report['question'].strip() or
                                                not report['checks'] or any(not check.strip() for check in report['checks'])):
            report = dict(report, status='fail', findings=[
                '완료 보고 형식이 모순됩니다: complete는 findings=[], question="", 실제 수행한 검사 근거가 있는 checks가 필요합니다. '
                '일반 관찰은 summary/checks에 쓰고, 실제 미해결 결함은 findings에 유지하여 해결 전까지 fail로 보고하세요.',
                *report['findings']])
        self.store.update(job['id'], attempt=None, failures=0, not_before=0, summary=report['summary'],
                          last_provider_evidence=provider_evidence, provider_error=None, blocker=None, failure_fingerprint=None,
                          state=ROLE_STATE[role], last_terminal={'attempt': attempt['id'], 'role': role, 'head': attempt['head'],
                          'at': time.time(), 'exit_code': 0, 'status': report['status']})
        self.store.event(job['id'], 'result', report['summary'])
        if report['status'] == 'needs_user':
            self.store.update(job['id'], state='needs_user', question=report['question']); self.notify(job); return
        if report['status'] == 'fail':
            feedback = report['findings'] or [report['summary']]
            fingerprint = digest({'role': role, 'task': attempt['task'], 'head': self.repos.head(job), 'feedback': feedback})
            repeats = job.get('feedback_repeats', 0) + 1 if job.get('feedback_fingerprint') == fingerprint else 0
            retry_at = time.time() + min(300, 10 * 2 ** min(repeats, 5))
            self.store.update(job['id'], feedback_fingerprint=fingerprint, feedback_repeats=repeats)
            if role == 'planner':
                self.store.update(job['id'], state='planning', feedback=feedback, not_before=retry_at)
            elif role == 'builder':
                self.store.update(job['id'], state='building', feedback=feedback, not_before=retry_at)
            else: self.rework(job, feedback, retry_at)
            return
        if role == 'planner':
            try:
                plan = validate_plan(report['plan']); plan = bind_specs(plan, job.get('program_scope'))
                validate_plan(plan); validate_coverage(plan, job.get('program_scope'))
                pins = self.repos.source_pins(job, plan)
            except AppError as exc:
                self.store.update(job['id'], state='planning', feedback=['계획을 수정하세요: ' + str(exc)], not_before=time.time() + 10); return
            self.store.update(job['id'], plan=plan, source_pins=pins, state='building', phase='building', feedback=[])
            self.store.event(job['id'], 'plan', str(len(plan['tasks'])) + '개 단계로 계획을 세웠습니다. 바로 개발을 진행합니다.')
        elif role == 'builder':
            self.complete_builder(job, attempt['task'])
        else:
            expected = {task['id'] for task in job['plan']['tasks']}
            if report.get('reviewed_head') != attempt['head'] or set(report.get('covered_tasks') or []) != expected or not report.get('checks') or report.get('findings'):
                self.store.update(job['id'], state=ROLE_STATE[role], feedback=['정확한 HEAD와 모든 task id, 실제 검증 근거가 필요합니다. 미해결 사항은 fail로 반환하세요.'], not_before=time.time() + 10)
                return
            evidence = {'head': attempt['head'], 'attempt': attempt['id'], 'profile': job['settings']['roles'][role],
                        'report': report, 'provider_evidence': provider_evidence}
            if role == 'reviewer':
                phase='publishing' if job.get('native_lineage') else 'supervising'
                self.store.update(job['id'], review=evidence, phase=phase, state=phase, feedback=[],
                                  candidate_published_head=None,candidate_ci_requested=None)
            else:
                if not job['review'] or job['review']['head'] != attempt['head'] or job['review']['attempt'] == attempt['id']:
                    raise AppError('INDEPENDENT_REVIEW_REQUIRED')
                phase='verifying' if job.get('native_lineage') else 'publishing'
                self.store.update(job['id'], supervision=evidence, phase=phase, state=phase, feedback=[])

    def builder_task(self, job):
        task = {'id': 'integration-rework', 'title': '검토 의견과 통합 문제 수정',
                'instructions': '모든 피드백을 해결하고 전체 산출물을 유지하세요.',
                'acceptance': ['모든 피드백 해결', '전체 회귀 검증 통과'], 'depends_on': []} if job['correcting'] else job['plan']['tasks'][job['task_index']]
        if job.get('native_lineage') and job['correcting']:
            task = copy.deepcopy(job['plan']['tasks'][0])
            task['instructions'] += '\n\nResolve all supplied findings and re-run the original node verification. Preserve this node identity and complete original spec.'
        return task

    def complete_builder(self, job, task):
        current = self.store.get(job['id']); current['current_task'] = task
        head = self.repos.checkpoint(current)
        built = list(job['built_tasks'])
        if not job['correcting'] or (job.get('native_lineage') and task['id'] not in built):
            built.append(task['id'])
        index = 1 if job.get('native_lineage') else job['task_index'] if job['correcting'] else job['task_index'] + 1
        phase = 'reviewing' if job['correcting'] or index >= len(job['plan']['tasks']) else 'building'
        self.store.update(job['id'], head=head, built_tasks=built, task_index=index, phase=phase, state=phase,
                          feedback=[], correcting=False, review=None, supervision=None, ci=None, checkpoint_retry=None)

    def retry_checkpoint(self, job):
        """Reconsume a proven completed builder after an Owner resume, never rerun it."""
        retry = job['checkpoint_retry']; terminal = job.get('last_terminal') or {}
        if (not job.get('native_lineage') or job['phase'] != 'building' or job['attempt'] or
            retry != {'attempt': terminal.get('attempt'), 'head': terminal.get('head')} or
            terminal.get('role') != 'builder' or terminal.get('status') != 'complete' or
            terminal.get('exit_code') != 0 or job['head'] != retry['head'] or
            self.repos.head(job) != retry['head'] or
            not isinstance(retry['attempt'], str) or not re.fullmatch(r'[0-9a-f]{32}', retry['attempt'])):
            raise AppError('MAC_HOST_CHECKPOINT_INPUT_CHANGED')
        task = self.builder_task(job); profile = job['settings']['roles']['builder']
        binding = digest({'job': job['id'], 'attempt': retry['attempt'], 'head': retry['head'],
                          'role': 'builder', 'profile': profile, 'plan': job['plan'], 'task': task})
        folder = self.store.directory / 'jobs' / job['id'] / retry['attempt']
        request = self.pipeline.source._private_json(folder / 'request.json')
        receipt = self.pipeline.source.private_receipt(folder, {'id': retry['attempt'], 'binding': binding})
        actor = receipt.get('provider_evidence') or {}; report = receipt.get('report') or {}
        agents.validate_report(report)
        sid = actor.get('session_id'); writers = job.get('builder_sessions') or []
        if (request.get('attempt_id') != retry['attempt'] or request.get('binding') != binding or
            request.get('role') != 'builder' or request.get('profile') != profile or
            request.get('host_directory') != str(self.store.directory.resolve()) or
            request.get('checkout') != str(self.repos.path(job).resolve()) or
            receipt.get('exit_code') != 0 or receipt.get('error') is not None or
            report['status'] != 'complete' or report['findings'] or report['question'].strip() or
            not report['checks'] or any(not check.strip() for check in report['checks']) or
            actor != job.get('last_provider_evidence') or actor.get('provider') != profile['provider'] or
            actor.get('harness') != agents.CATALOG[profile['provider']]['harness'] or
            actor.get('model_requested') != profile['model'] or not isinstance(sid, str) or not sid or
            not writers or writers[-1] != sid or writers.count(sid) != 1):
            raise AppError('MAC_HOST_CHECKPOINT_RECEIPT_REQUIRED')
        self.complete_builder(job, task)
        self.store.event(job['id'], 'checkpoint_recovered', '기존 builder의 종료 영수증과 산출물을 다시 검증해 같은 작업의 독립 검토로 전달했습니다.')

    def rework(self, job, feedback, retry_at=0):
        self.store.update(job['id'], state='building', phase='building', correcting=True,
                          feedback=feedback, review=None, supervision=None, ci=None, not_before=retry_at,
                          candidate_published_head=None,candidate_ci_requested=None)
        self.store.event(job['id'], 'rework', '개발자가 검토 의견을 반영하고 테스트를 다시 진행합니다.')

    def inspected_head(self, job):
        return (self.repos.head(job) == job['head'] and self.repos.clean(job) and
                all(job.get(role) and job[role]['head'] == job['head'] for role in ('review', 'supervision')) and
                job['review']['attempt'] != job['supervision']['attempt'])

    def user_merge(self,key,value):
        """Owner-only, head-bound human approval; no automatic merge loop."""
        if not isinstance(value,dict) or set(value)!={'head','approval'}:
            raise AppError('USER_MERGE_APPROVAL_REQUIRED')
        approval={'head':text(value['head'],'head',40),'approval':text(value['approval'],'approval',2000)}
        with self.store.lock:
            job=self.store.get(key)
            if not job.get('native_lineage') or job['state']!='accepted' or job.get('attempt') or key in self.store.execution_busy:
                raise AppError('MAC_HOST_USER_INSPECTION_REQUIRED')
            if approval['head']!=job['head']: raise AppError('USER_MERGE_HEAD_CHANGED')
            self.pipeline.validate_inspection(job,refresh=False)
            prior=job.get('user_merge')
            if prior and prior['approval']!=approval: raise AppError('USER_MERGE_APPROVAL_IMMUTABLE')
            if not prior:
                prior={'approval':approval,'state':'APPROVED','at':time.time()}
                job=self.store.update(key,user_merge=prior)
                self.store.event(key,'user_merge_approved','사용자가 검수한 정확한 HEAD의 Ready 전환과 일반 merge commit 병합을 승인했습니다.')
            self.store.execution_busy.add(key)
        try:
            pull=self.repos.merge_candidate(job)
            if pull['merged']:
                self.store.update(key,user_merge={**prior,'state':'MERGED','merge_head':pull['merge_head']})
                return self.store.get(key)
            if pull['draft']:
                if prior['state']!='APPROVED': raise AppError('USER_READY_OUTCOME_UNKNOWN')
                prior={**prior,'state':'READY_SUBMITTING'};self.store.update(key,user_merge=prior)
                self.repos.user_ready(job)
                pull=self.repos.merge_candidate(job)
                if pull['draft']: raise AppError('USER_READY_OUTCOME_UNKNOWN')
            if prior['state']=='MERGE_SUBMITTING': raise AppError('USER_MERGE_OUTCOME_UNKNOWN')
            prior={**prior,'state':'READY'};job=self.store.update(key,user_merge=prior)
            ci=self.repos.checks(job);self.store.update(key,user_merge_ci=ci)
            if ci['state']!='passed': return self.store.get(key)
            self.pipeline.validate_inspection(self.store.get(key),refresh=False)
            # The remote mutation has its own expected-head guard and obeys
            # branch protection. An uncertain call is only reconciled by GET.
            prior={**prior,'state':'MERGE_SUBMITTING'};self.store.update(key,user_merge=prior)
            result=self.repos.user_merge(job)
            self.store.update(key,user_merge={**prior,'state':'MERGED','merge_head':result['merge_head']})
            self.store.event(key,'user_merge','사용자 승인 HEAD를 일반 merge commit으로 병합했습니다. 병합 후 검증은 별도로 확인합니다.')
            return self.store.get(key)
        finally:
            with self.store.lock:self.store.execution_busy.discard(key)

    def validate_acceptance(self, job, value=None):
        if job['state'] != 'ready': raise AppError('NOT_READY_FOR_ACCEPTANCE')
        already_merged=job.get('native_lineage') and self.repos.merge_candidate(job)['merged']
        if not self.inspected_head(job):
            if already_merged: raise AppError('INSPECTION_CHANGED')
            self.rework(job, ['사용자 검수 전에 코드가 변경되었습니다. 전체 검증을 다시 실행하세요.'])
            raise AppError('INSPECTION_CHANGED', '검토 대상이 변경되어 다시 검증합니다.')
        if already_merged:
            # An Owner may explicitly approve the original reviewed head after
            # an external ordinary merge. Never merge main into that checkout
            # or infer User approval merely from a remote merged flag.
            if not isinstance(value,dict) or set(value)!={'head','approval'}:
                raise AppError('USER_INSPECTION_APPROVAL_REQUIRED')
            approval={'head':text(value['head'],'head',40),'approval':text(value['approval'],'approval',2000)}
            if approval['head']!=job['head']: raise AppError('USER_INSPECTION_HEAD_CHANGED')
            if job.get('post_merge_owner_approval') and job['post_merge_owner_approval']!=approval:
                raise AppError('USER_INSPECTION_APPROVAL_IMMUTABLE')
            self.pipeline.source.preflight(job['native_lineage']['binding'],allow_base_advance=True)
            ci=self.repos.hosted_checks(job,job['head'])
            if ci['state']!='passed': raise AppError('MAC_HOST_LIVE_CI_REQUIRED')
            candidate={**job,'ci':ci}
            self.pipeline.validate_inspection(candidate,refresh=False)
            proof=self.repos.merged(candidate)
            self.store.update(job['id'],ci=ci,post_merge_owner_approval=approval,
                              inspection_merge_proof=proof)
            return
        sync = self.repos.synchronize_base(job)
        if sync['changed']:
            self.store.update(job['id'], verified_base=sync['base'], head=self.repos.head(job))
            self.rework(job, ['사용자 검수 전에 기준 브랜치가 변경되었습니다. 통합과 전체 검증을 다시 진행하세요.'])
            raise AppError('INSPECTION_CHANGED', '기준 브랜치가 변경되어 다시 검증합니다.')
        if job.get('pr_url'):
            ci = self.repos.checks(job); self.store.update(job['id'], ci=ci)
            if ci['state'] == 'failed': self.rework(job, ['사용자 검수 전 CI 실패를 해결하세요: ' + encoded(ci['checks'])])
            elif ci['state'] == 'pending': self.store.update(job['id'], state='verifying', phase='verifying', not_before=time.time()+60)
            if ci['state'] in ('failed', 'pending'):
                raise AppError('INSPECTION_CHANGED', '최신 CI 결과를 다시 확인합니다.')
        if job.get('native_lineage'): self.pipeline.validate_inspection(self.store.get(job['id']))


def service_lock(directory):
    path = Path(directory) / 'service.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd); raise AppError('SERVICE_ALREADY_RUNNING')
    return fd
