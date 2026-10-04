"""Serialized native attempts with a user-initialized Mac authority adapter.

An uninitialized adapter fails closed. The optional protected-source seam remains
unsupported by the installed factory. No VM/workflow dispatch is provided.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import agents
import transport_guard
from common import AppError, TERMINAL, WORKER_REQUEST_LIMIT, atomic_json, digest, encoded, parse_json, private_directory, read_json, repository
from gitops import git

PROVIDERS = {'DEVIN': 'devin', 'GROK_BUILD': 'grok_build', 'GLM': 'glm', 'CURSOR': 'cursor',
             'MAC_CLAUDE': 'claude', 'MAC_CODEX': 'codex'}
FIELDS = {'repository', 'task_id', 'task_revision', 'issue', 'program', 'node',
          'materialization_request_id', 'plan_commit', 'plan_blob', 'dependencies',
          'owner_lane', 'source_host', 'target_host', 'work_sha256'}


def require(test, code='NATIVE_RECEIPT_INVALID'):
    if not test: raise AppError(code)


def binding(value):
    require(isinstance(value, dict) and set(value) in (FIELDS, FIELDS | {'authority_kind', 'canonical_task_pointer'},
            FIELDS | {'authority_kind', 'canonical_task_pointer', 'generation_id'}), 'NATIVE_BINDING_INVALID')
    if 'generation_id' in value:
        require(isinstance(value['generation_id'],str) and re.fullmatch(r'[0-9a-f]{32}',value['generation_id']), 'NATIVE_BINDING_INVALID')
    if 'authority_kind' in value:
        require(value['authority_kind'] == 'MAC_LOCAL' and isinstance(value['canonical_task_pointer'], str) and
                value['canonical_task_pointer'].startswith('mac-host:'), 'NATIVE_BINDING_INVALID')
    repository(value['repository'])
    for key in ('task_id', 'task_revision', 'program', 'node', 'source_host', 'target_host'):
        require(isinstance(value[key], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value[key]))
    require(value['source_host'] != value['target_host'] and value['owner_lane'] in PROVIDERS)
    require(type(value['issue']) is int and value['issue'] > 0)
    for key, size in (('plan_commit', 40), ('plan_blob', 40), ('materialization_request_id', 24), ('work_sha256', 64)):
        require(isinstance(value[key], str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', value[key]))
    require(isinstance(value['dependencies'], list) and len(value['dependencies']) <= 256)
    seen = set()
    for dep in value['dependencies']:
        if value.get('authority_kind') == 'MAC_LOCAL':
            require(isinstance(dep,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',dep) and dep not in seen)
            seen.add(dep); continue
        require(isinstance(dep, dict) and set(dep) == {'task_id', 'task_revision', 'launch_request_id', 'head_sha', 'gate_evidence'})
        require(isinstance(dep['task_id'], str) and dep['task_id'] not in seen and dep['task_id'] != value['task_id'])
        seen.add(dep['task_id'])
    require(len(encoded(value).encode()) <= 65536)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class SourceReply:
    # These identifiers come from a qualified adapter's authenticated channel,
    # not from the response JSON. Raw HTTP/fixture bodies are never accepted.
    source_host: str
    target_host: str
    document: dict


class UnsupportedSource:
    production_qualified = False
    source_host = target_host = None

    def call(self, operation, payload):
        raise AppError('NATIVE_SOURCE_UNSUPPORTED', '인증된 canonical 이관 source adapter가 설치·검증되지 않았습니다. 제품 실행은 차단합니다.')


def configured_source(store=None):
    # Implementing/provisioning a pinned, mutually authenticated fixed-operation
    # channel requires a separate protected service authorization. No fallback.
    if store is not None:
        from mac_authority import LocalSource
        return LocalSource(store)
    return UnsupportedSource()


class NativeWorker:
    def __init__(self, directory, settings):
        self.directory, self.settings = directory, settings
        self.children = {}

    def prepare(self, value, work, request_id, attempt_id):
        require(work.get('task_id') == value['task_id'] and work.get('task_revision') == value['task_revision'] and
                work.get('plan_commit') == value['plan_commit'], 'NATIVE_SCOPE_MISMATCH')
        profile = self.settings()['roles']['builder']
        require(profile == work.get('profile') and profile['provider'] == PROVIDERS[value['owner_lane']], 'NATIVE_OWNER_PROFILE_MISMATCH')
        native=private_directory(self.directory / 'native')
        request_folder=private_directory(native / request_id)
        folder = private_directory(request_folder / attempt_id)
        checkout = folder / 'checkout'
        require(not checkout.exists(), 'NATIVE_CHECKOUT_EXISTS')
        git(None, 'clone', '--no-local', '--no-checkout', 'https://github.com/' + value['repository'] + '.git', str(checkout), timeout=600)
        git(checkout, 'checkout', '-b', 'aiops/native-' + request_id[:16], value['plan_commit'])
        git(checkout, 'config', 'user.name', 'AIOPS Mac')
        git(checkout, 'config', 'user.email', 'aiops-mac@users.noreply.github.com')
        head = git(checkout, 'rev-parse', 'HEAD').strip()
        require(head == value['plan_commit'], 'NATIVE_CHECKOUT_BINDING_MISMATCH')
        preparation=None
        if value.get('authority_kind')=='MAC_LOCAL':
            from gitops import prepare_host_checkout
            branch=git(checkout,'symbolic-ref','--short','refs/remotes/origin/HEAD').removeprefix('origin/')
            preparation=prepare_host_checkout(checkout,branch,value['plan_commit'],value['plan_blob'])
        attempt = {'id': attempt_id, 'head': head, 'profile': profile}
        attempt['binding'] = digest({'request_id': request_id, 'canonical': value, **attempt})
        agents.command(profile, 'builder', folder, checkout=checkout)
        prompt = (agents.RULES + '\nYou are the one original canonical writer. Preserve the exact task/revision/plan/dependencies and lane. '
                  'Implement only the frozen authorized task scope below. Do not replan, create another writer, run a VM, '
                  'merge or deploy. Return the AIOPS structured completion schema. Source gates and ownership are not '
                  'granted by your report.\nCanonical binding:\n' + encoded(value) + '\nFrozen scope:\n' + encoded(work) + '\n' + encoded(agents.SCHEMA))
        if preparation:
            prompt += ('\nTrusted host preparation:\n'+encoded(preparation)+
                       '\nThe AIOPS host has performed the required git fetch and verified the pinned base and plan blob. '
                       'Read all authoritative documents at that pin before editing. Git metadata is host-owned and read-only to you; '
                       'do not repeat fetch, commit, push, checkout, or other Git metadata writes. This changes no repository technical gate.')
        if 'generation_id' in value:
            prompt += ('\nExplicit User-approved new Mac generation: the original task ID/revision and legacy issues are provenance only; '
                       'you are not their owner and must not change or resume them. The User approved a new isolated Mac task/branch '
                       'with automatic merge forbidden. This checkout uses the host-created aiops/native branch rather than any legacy '
                       'astra branch. Preserve the exact original technical spec and gates. Do not modify legacy scope or any other checkout, '
                       'create legacy task markers/issues, enable auto-merge, mark a draft ready, merge, deploy, or claim legacy termination.')
        request = {'attempt_id': attempt_id, 'binding': attempt['binding'], 'profile': profile, 'role': 'builder',
                   'checkout': str(checkout), 'prompt': prompt, 'timeout_seconds': self.settings()['session_minutes'] * 60}
        if value.get('authority_kind') == 'MAC_LOCAL':
            request['host_directory'] = str(self.directory.resolve())
        require(len(encoded(request).encode()) <= WORKER_REQUEST_LIMIT, 'WORKER_REQUEST_TOO_LARGE')
        atomic_json(folder / 'request.json', request); atomic_json(folder / 'schema.json', agents.SCHEMA)
        return attempt

    def folder(self, record):
        return self.directory / 'native' / record['request_id'] / record['attempt']['id']

    def launch(self, record):
        folder = self.folder(record)
        fd = os.open(folder / 'worker-errors.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as errors:
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('worker.py')), '--attempt', str(folder)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                     start_new_session=True, env=agents.environment())
        self.children[record['request_id']] = child

    def receipt(self, record):
        path = self.folder(record) / 'receipt.json'
        if path.exists(): return read_json(path)
        child = self.children.get(record['request_id'])
        if child is not None and child.poll() is not None: raise AppError('WORKER_OUTCOME_UNKNOWN')
        if time.time() > record['updated'] + self.settings()['session_minutes'] * 60 + 120:
            raise AppError('WORKER_OUTCOME_UNKNOWN')
        return None

    def stop(self,record):
        folder=self.folder(record)
        require(folder.is_dir() and not folder.is_symlink(),'NATIVE_STOP_UNAVAILABLE')
        atomic_json(folder / 'stop-request.json',{'attempt_id':record['attempt']['id'],
                                                 'binding':record['attempt']['binding']})


class Controller:
    def __init__(self, store, source=None, worker=None):
        self.store = store
        self.source = source if source is not None else configured_source(store)
        self.worker = worker if worker is not None else NativeWorker(store.directory, store.settings)
        self.in_progress = set()
        with store.lock:
            store.db.executescript('''CREATE TABLE IF NOT EXISTS native_local (
                request TEXT PRIMARY KEY, state TEXT NOT NULL, document TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS native_local_one_active ON native_local((1)) WHERE state != 'TERMINAL';''')

    def capability(self):
        return {'supported': self.authorized_source(),
                'code': 'NATIVE_SOURCE_UNSUPPORTED' if not self.authorized_source() else 'MAC_HOST_CONFIGURED' if getattr(self.source,'mode',None)=='MAC' else 'QUALIFIED_SOURCE',
                'execution_allowed': False, 'per_task_admission_required':True,
                'mode': getattr(self.source, 'mode', 'PROTECTED_TRANSFER_CANDIDATE')}

    def authorized_source(self):
        # Mac mode initialization is an explicit owner decision; it is not a
        # claim of production qualification or of a protected VM adapter audit.
        if getattr(self.source,'mode',None)=='MAC': return self.source.authority_initialized is True
        return self.source.production_qualified is True

    @staticmethod
    def public(record):
        require(record is not None, 'NATIVE_REQUEST_NOT_FOUND')
        result = {key: record[key] for key in ('request_id', 'state', 'updated', 'error')} | {
            'canonical': {key:record['binding'][key] for key in ('repository','task_id','task_revision','plan_commit','owner_lane','source_host','target_host')},
            'worker_started': record.get('worker_started'), 'stop_requested':record.get('stop_requested',False),
            'provider_started':record.get('provider_started')}
        if 'generation_id' in record['binding']: result['canonical']['generation_id']=record['binding']['generation_id']
        return result

    def get(self, request):
        require(isinstance(request, str) and re.fullmatch(r'[0-9a-f]{32}', request), 'NATIVE_REQUEST_INVALID')
        with self.store.lock:
            row = self.store.db.execute('SELECT document FROM native_local WHERE request=?', (request,)).fetchone()
        return parse_json(row[0], 1024*1024) if row else None

    def save(self, record, state, **changes):
        record = copy.deepcopy(record); record.update(changes, state=state, updated=time.time())
        document = encoded(record)
        require(len(document.encode()) <= 1024*1024, 'NATIVE_RECORD_TOO_LARGE')
        with self.store.lock:
            self.store.db.execute('UPDATE native_local SET state=?,document=? WHERE request=?', (state, document, record['request_id']))
        return record

    def _call(self, operation, payload, value):
        require(self.authorized_source(), 'NATIVE_SOURCE_UNSUPPORTED')
        if operation=='claim' and hasattr(self.source,'preflight'): self.source.preflight(value)
        require((self.source.source_host, self.source.target_host) == (value['source_host'], value['target_host']), 'NATIVE_CHANNEL_BINDING_MISMATCH')
        response = self.source.call(operation, copy.deepcopy(payload))
        require(isinstance(response, SourceReply) and (response.source_host, response.target_host) ==
                (value['source_host'], value['target_host']), 'NATIVE_AUTHENTICATED_REPLY_REQUIRED')
        require(isinstance(response.document, dict) and len(encoded(response.document).encode()) <= 1024*1024)
        return response.document

    def _view(self, view, record, state):
        expected = {'request_id': record['request_id'], 'binding': record['binding'], 'attempt': record['attempt']}
        require(set(view) == {'state', 'schema_version', 'request', 'source_snapshot_sha256', 'source_authorization', 'work', 'reserved_at', 'terminal'})
        require(type(view['schema_version']) is int and view['schema_version'] == 1 and view['state'] == state)
        require(view['request'] == expected and digest(view['work']) == record['binding']['work_sha256'], 'NATIVE_RECEIPT_BINDING_MISMATCH')
        require(isinstance(view['source_snapshot_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', view['source_snapshot_sha256']))
        prefix = 'mac-host:'+self.source.source_host+'#' if record['binding'].get('authority_kind')=='MAC_LOCAL' else 'https://'
        require(isinstance(view['source_authorization'], str) and view['source_authorization'].startswith(prefix))
        require(type(view['reserved_at']) in (int, float) and math.isfinite(view['reserved_at']) and view['reserved_at'] >= 0)
        require(view['source_snapshot_sha256'] == record['source_snapshot_sha256'], 'NATIVE_SOURCE_SNAPSHOT_CHANGED')
        if state != 'TERMINAL': require(view['terminal'] is None)
        return view

    def start(self, value):
        require(isinstance(value, dict) and set(value) == {'request_id', 'binding'}, 'NATIVE_START_INVALID')
        canonical = binding(value['binding']); rid = value['request_id']
        previous = self.get(rid)
        if previous:
            require(previous['binding'] == canonical, 'NATIVE_REQUEST_CONFLICT')
            return previous  # Replay is a read, including interrupted/terminal requests.
        require(self.authorized_source(), 'NATIVE_SOURCE_UNSUPPORTED')
        if hasattr(self.source,'preflight'): self.source.preflight(canonical)
        with self.store.lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                previous = self.get(rid)
                if previous:
                    require(previous['binding'] == canonical, 'NATIVE_REQUEST_CONFLICT')
                    self.store.db.execute('COMMIT'); return previous
                require(not any(j['attempt'] or j['state'] not in TERMINAL for j in self.store.jobs()) and not self.store.execution_busy, 'NATIVE_LOCAL_WORK_BUSY')
                require(not self.store.db.execute("SELECT 1 FROM native_local WHERE state!='TERMINAL'").fetchone(), 'NATIVE_LOCAL_TRANSFER_UNRESOLVED')
                if hasattr(self.source, 'check_external'):
                    self.source.check_external(canonical)
                else: transport_guard.require_clear(self.store.directory)
                record = {'request_id': rid, 'binding': canonical, 'state': 'PREPARING', 'attempt': None,
                          'updated': time.time(), 'receipt': None, 'terminal': None, 'error': None, 'worker_started': False}
                self.store.db.execute('INSERT INTO native_local VALUES (?,?,?)', (rid, 'PREPARING', encoded(record)))
                self.store.db.execute('COMMIT')
                self.in_progress.add(rid)
            except Exception:
                self.store.db.execute('ROLLBACK'); raise
        try:
            observed = self._call('read', canonical, canonical)
            require(observed.get('status') == 'OBSERVED' and observed.get('binding') == canonical and
                    isinstance(observed.get('work'), dict) and digest(observed['work']) == canonical['work_sha256'], 'NATIVE_SOURCE_SCOPE_MISMATCH')
            # Only an authenticated source may assert its terminal and dependency
            # evidence. The source fixed operation validates it against its ledger.
            require(set(observed) == {'status', 'binding', 'work', 'gate_evidence', 'history', 'dependencies', 'observed_at'})
            require(isinstance(observed['history'], list) and len(observed['history']) <= 4096 and isinstance(observed['dependencies'], list))
            require(all(isinstance(r, dict) and r.get('state') in ('RECONCILED', 'FAILED_PRESTART') for r in observed['history']))
            require(len(observed['dependencies']) == len(canonical['dependencies']))
            source_hash = digest({k:v for k,v in observed.items() if k not in ('status', 'observed_at')})
            attempt = self.worker.prepare(canonical, observed['work'], rid, uuid.uuid4().hex)
            if hasattr(self.source,'preflight'): self.source.preflight(canonical)
            record = self.save(record, 'RESERVING', attempt=attempt, source_snapshot_sha256=source_hash)
            payload = {'request_id': rid, 'binding': canonical, 'attempt': attempt}
            reserved = self._view(self._call('reserve', payload, canonical), record, 'RESERVED')
            record = self.save(record, 'CLAIMING', receipt=reserved)
            claim = {'request_id': rid, 'attempt': attempt}
            admitted = self._view(self._call('claim', claim, canonical), record, 'CLAIMED')
            require({k:v for k,v in admitted.items() if k != 'state'} == {k:v for k,v in reserved.items() if k != 'state'}, 'NATIVE_RECEIPT_CHANGED')
            if hasattr(self.source, 'check_external'):
                self.source.check_external(canonical)
            else: transport_guard.require_clear(self.store.directory)
            record = self.save(record, 'STARTING', receipt=admitted, worker_started=None)
            self.worker.launch(record)  # Only after both durable source/local fences.
            return self.save(record, 'RUNNING', worker_started=True)
        except Exception as exc:
            # PREPARING performs only a fixed read and local checkout preparation.
            # A source mutation is always preceded by the durable RESERVING state.
            # This prestart failure releases no source owner and retains its ID.
            self.save(record, 'TERMINAL' if record['state'] == 'PREPARING' else 'UNKNOWN',
                      error=exc.code if isinstance(exc, AppError) else type(exc).__name__)
            raise
        finally:
            with self.store.lock: self.in_progress.discard(rid)

    def pending(self):
        with self.store.lock:
            row = self.store.db.execute("SELECT document FROM native_local WHERE state!='TERMINAL' LIMIT 1").fetchone()
        return parse_json(row[0], 1024*1024) if row else None

    def tick(self):
        record = self.pending()
        if not record: return False
        with self.store.lock:
            if record['request_id'] in self.in_progress: return True
        if record['state'] == 'UNKNOWN':
            if record.get('terminal') is not None or (record.get('receipt') or {}).get('state') != 'CLAIMED': return True
        elif record['state'] != 'RUNNING':
            self.save(record, 'UNKNOWN', error='NATIVE_INTERRUPTED_ADMISSION'); return True
        try:
            receipt = self.worker.receipt(record)
            if receipt is None: return True
            attempt = record['attempt']
            require(receipt.get('attempt_id') == attempt['id'] and receipt.get('binding') == attempt['binding'], 'NATIVE_TERMINAL_BINDING_MISMATCH')
            require(receipt.get('process_group_quiescent') is True, 'NATIVE_WORKER_NOT_TERMINAL')
            require(type(receipt.get('exit_code')) is int or receipt.get('provider_started') is False, 'NATIVE_WORKER_NOT_TERMINAL')
            report, error = receipt.get('report'), receipt.get('error')
            require((isinstance(report, dict) and error is None) or (isinstance(error, str) and report is None), 'NATIVE_TERMINAL_RESULT_INVALID')
            evidence = receipt.get('provider_evidence') or {}
            if report is not None:
                agents.validate_report(report)
                require(receipt['exit_code'] == 0 and evidence.get('provider') == attempt['profile']['provider'] and
                        evidence.get('model_requested') == attempt['profile']['model'] and
                        evidence.get('harness') == agents.CATALOG[attempt['profile']['provider']]['harness'], 'NATIVE_PROVIDER_BINDING_MISMATCH')
            terminal = {'attempt_id': attempt['id'], 'binding': attempt['binding'], 'head': attempt['head'],
                        'exit_code': receipt['exit_code'] if type(receipt['exit_code']) is int else -1,
                        'process_group_quiescent': True, 'result_sha256': digest(receipt), 'session_id': evidence.get('session_id')}
            record = self.save(record, 'FINISHING', terminal=terminal)
            payload = {'request_id': record['request_id'], 'attempt': attempt, 'terminal': terminal}
            view = self._view(self._call('finish', payload, record['binding']), record, 'TERMINAL')
            require(view['terminal'] == terminal, 'NATIVE_TERMINAL_BINDING_MISMATCH')
            self.save(record, 'TERMINAL', receipt=view, error=error, provider_started=receipt.get('provider_started'),
                      worker_started=receipt.get('provider_started') is True or record.get('worker_started'))
        except Exception as exc:
            self.save(record, 'UNKNOWN', error=exc.code if isinstance(exc, AppError) else type(exc).__name__)
        return True

    def reconcile(self, request):
        record = self.get(request)
        require(record is not None and record['state'] == 'UNKNOWN' and record.get('terminal') is not None, 'NATIVE_RECONCILIATION_UNPROVEN')
        # A lost terminal reply is recovered with a read. This cannot release a
        # running/reserved/unknown worker or resend the launch/reservation.
        view = self._view(self._call('status', {'request_id': request, 'attempt': record['attempt']}, record['binding']), record, 'TERMINAL')
        require(view['terminal'] == record['terminal'], 'NATIVE_TERMINAL_BINDING_MISMATCH')
        return self.save(record, 'TERMINAL', receipt=view)

    def stop(self,request):
        record=self.get(request)
        require(record is not None and record.get('attempt') and record['binding'].get('authority_kind')=='MAC_LOCAL'
                and record['state'] in ('RUNNING','UNKNOWN'),'NATIVE_STOP_UNAVAILABLE')
        self.worker.stop(record)
        # A request is not a terminal result. Unknown execution remains fenced
        # until the bound worker's exit and quiescence receipts are validated.
        return self.save(record,record['state'],stop_requested=True)


def local_fenced(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_local'").fetchone(): return False
    return bool(db.execute("SELECT 1 FROM native_local WHERE state!='TERMINAL' LIMIT 1").fetchone())
