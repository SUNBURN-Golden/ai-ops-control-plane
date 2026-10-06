"""User-initialized authoritative Mac host, inside the existing private app ledger.

The Mac owns new reservations. Legacy claims/opaque receipts remain immutable
conflict barriers; no VM access, terminal assertion or ownership release occurs.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import stat
import time
import uuid

from common import AppError, TERMINAL, digest, encoded, parse_json, repository
from program_scope import load_scope
import handoff
import transport_guard


def require(test, code):
    if not test: raise AppError(code)


def original_task_key(program, node):
    require(isinstance(program,str) and program and isinstance(node,str) and node,
            'MAC_HOST_CANONICAL_BINDING_MISMATCH')
    return (program+'-'+node).upper()


class LocalSource:
    mode = 'MAC'

    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS mac_host_meta(id INTEGER PRIMARY KEY CHECK(id=1),document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mac_host_programs(repository TEXT PRIMARY KEY,document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mac_host_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,repository TEXT NOT NULL,
                    task TEXT NOT NULL,binding TEXT NOT NULL,work TEXT NOT NULL,state TEXT NOT NULL,
                    UNIQUE(repository,task));
                CREATE TABLE IF NOT EXISTS mac_host_external(id TEXT PRIMARY KEY,repository TEXT NOT NULL,
                    task TEXT NOT NULL,document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mac_host_receipt_scopes(request TEXT PRIMARY KEY,document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mac_host_attempts(request TEXT PRIMARY KEY,repository TEXT NOT NULL,
                    task TEXT NOT NULL,state TEXT NOT NULL,document TEXT NOT NULL,terminal TEXT);
                CREATE TABLE IF NOT EXISTS mac_host_deliveries(request TEXT PRIMARY KEY,job TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL,document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mac_host_generations(id TEXT PRIMARY KEY,repository TEXT NOT NULL,document TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS mac_host_one_active ON mac_host_attempts((1)) WHERE state!='TERMINAL';
            ''')

    def meta(self):
        with self.store.lock:
            row=self.store.db.execute('SELECT document FROM mac_host_meta WHERE id=1').fetchone()
        return parse_json(row[0]) if row else None

    @property
    def authority_initialized(self):
        return (self.meta() or {}).get('mode') == 'MAC'

    @property
    def source_host(self): return (self.meta() or {}).get('host_id')

    @property
    def target_host(self):
        key=self.source_host
        return key+'-worker' if key else None

    def initialize(self, value):
        require(isinstance(value,dict) and set(value)=={'mode','decision'} and value['mode']=='MAC' and
                isinstance(value['decision'],str) and 1<=len(value['decision'])<=2000, 'MAC_HOST_EXPLICIT_MODE_REQUIRED')
        with self.store.lock:
            previous=self.meta()
            if previous: return previous
            require(not any(j['attempt'] or j['state'] not in TERMINAL for j in self.store.jobs()) and
                    not self.store.execution_busy, 'MAC_HOST_LOCAL_WORK_BUSY')
            native_table=self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_local'").fetchone()
            require(not native_table or not self.store.db.execute("SELECT 1 FROM native_local WHERE state!='TERMINAL'").fetchone(),
                    'MAC_HOST_LOCAL_WORK_BUSY')
            record={'mode':'MAC','host_id':'mac-'+uuid.uuid4().hex[:24], 'decision':value['decision'],
                    'initialized_at':time.time(), 'authority':'LOCAL_SQLITE_SINGLE_WRITER',
                    'legacy_terminal_verified':False}
            self.store.db.execute('INSERT INTO mac_host_meta VALUES (1,?)',(encoded(record),))
            return record

    def annotate_receipt(self,value):
        require(self.authority_initialized,'MAC_HOST_NOT_INITIALIZED')
        require(isinstance(value,dict) and set(value)=={'request_id','payload_sha256','repository','task_id','decision'},'MAC_HOST_RECEIPT_SCOPE_INVALID')
        repo=repository(value['repository']).lower()
        require(isinstance(value['task_id'],str) and (value['task_id']=='*' or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',value['task_id'])) and
                isinstance(value['decision'],str) and 1<=len(value['decision'])<=2000,'MAC_HOST_RECEIPT_SCOPE_INVALID')
        fences=transport_guard.unresolved(self.store.directory, include_digest=True)
        require(any(r['request_id']==value['request_id'] and r['payload_sha256']==value['payload_sha256'] for r in fences),'MAC_HOST_RECEIPT_ORIGIN_UNVERIFIED')
        document={**value,'repository':repo,'task_id':value['task_id'].upper(),'state':'UNRESOLVED','scope_only':True}
        with self.store.lock:
            old=self.store.db.execute('SELECT document FROM mac_host_receipt_scopes WHERE request=?',(value['request_id'],)).fetchone()
            if old:
                require(parse_json(old[0])==document,'MAC_HOST_RECEIPT_SCOPE_IMMUTABLE'); return document
            self.store.db.execute('INSERT INTO mac_host_receipt_scopes VALUES (?,?)',(value['request_id'],encoded(document)))
        return document

    def register(self,snapshot):
        require(self.authority_initialized,'MAC_HOST_NOT_INITIALIZED')
        require(isinstance(snapshot,dict) and snapshot.get('task_scope')=='all' and isinstance(snapshot.get('tasks'),list),'MAC_HOST_HISTORY_INCOMPLETE')
        repo=repository(snapshot['repository']).lower(); source=snapshot['source']
        raw=source['raw_program']
        require(isinstance(raw,str),'MAC_HOST_PLAN_PIN_REQUIRED')
        import hashlib
        content=raw.encode()
        require(hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()==source['blob'],
                'MAC_HOST_PLAN_BLOB_MISMATCH')
        scope=load_scope(raw,snapshot['repository'],source['blob'])
        require(isinstance(source.get('head'),str) and re.fullmatch(r'[0-9a-f]{40}',source['head']),'MAC_HOST_PLAN_PIN_REQUIRED')
        profile=self.store.settings()['roles']['builder']
        lanes={'devin':'DEVIN','cursor':'CURSOR','glm':'GLM','grok_build':'GROK_BUILD','claude':'MAC_CLAUDE','codex':'MAC_CODEX'}
        require(profile['provider'] in lanes,'MAC_HOST_PROFILE_UNSUPPORTED')
        # Observation timestamps can change on a read-only refresh. Authority,
        # original plan, profile and the full legacy projection cannot.
        require(isinstance(source.get('branch'),str) and source['branch'] and '\n' not in source['branch'],
                'MAC_HOST_BASE_BRANCH_REQUIRED')
        settings=self.store.settings()
        record={'scope':scope,'head':source['head'],'branch':source['branch'],'profile':profile,
                'settings':settings,'tasks':copy.deepcopy(snapshot['tasks'])}
        with self.store.lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                # register and generation are two entrypoints to the same
                # original node. Serialize both directions of admission.
                for owned in self.store.db.execute('SELECT binding FROM mac_host_tasks WHERE repository=?',(repo,)):
                    owner=parse_json(owned['binding'])
                    require(not('generation_id' in owner and original_task_key(owner.get('program'),owner.get('node')) in
                                {original_task_key(scope['program'],n) for n in scope['node_ids']}),
                            'MAC_HOST_ORIGINAL_TASK_ALREADY_OWNED')
                previous=self.store.db.execute('SELECT document FROM mac_host_programs WHERE repository=?',(repo,)).fetchone()
                if previous:
                    require(parse_json(previous[0])==record,'MAC_HOST_PROGRAM_REVISION_CHANGED')
                    self.store.db.execute('COMMIT'); return self.tasks(repo)
                self.store.db.execute('INSERT INTO mac_host_programs VALUES (?,?)',(repo,encoded(record)))
                for node in scope['nodes']:
                    task=(scope['program']+'-'+node['id']).upper()
                    require(len(task)<=64 and len(node['id'])<=64 and len(scope['program'])<=64,
                            'MAC_HOST_TASK_IDENTIFIER_UNSUPPORTED')
                    revision=digest({'plan_blob':scope['blob'],'node':node})
                    work={'task_id':task,'task_revision':revision,'plan_commit':source['head'],'profile':profile,
                          'task':copy.deepcopy(node),'original_plan':copy.deepcopy(source['raw_program']),
                          'settings':settings}
                    require(len(encoded(work).encode())<=300000,'MAC_HOST_SCOPE_TOO_LARGE')
                    cursor=self.store.db.execute('INSERT INTO mac_host_tasks(repository,task,binding,work,state) VALUES (?,?,?, ?,?)',
                        (repo,task,'{}',encoded(work),'READY'))
                    bound={'repository':snapshot['repository'],'task_id':task,'task_revision':revision,'issue':cursor.lastrowid,
                           'program':scope['program'],'node':node['id'],'materialization_request_id':digest({'host':self.source_host,'repo':repo,'task':task})[:24],
                           'plan_commit':source['head'],'plan_blob':scope['blob'],'dependencies':copy.deepcopy(node.get('depends_on',[])),
                           'owner_lane':lanes[profile['provider']],'source_host':self.source_host,'target_host':self.target_host,
                           'work_sha256':digest(work),'authority_kind':'MAC_LOCAL',
                           'canonical_task_pointer':'mac-host:'+self.source_host+':'+repo+':'+task}
                    self.store.db.execute('UPDATE mac_host_tasks SET binding=? WHERE id=?',(encoded(bound),cursor.lastrowid))
                # Closed/FAILED text is not a terminal proof. Canonical external
                # projections are held at the corresponding node, not globally.
                for task in snapshot['tasks']:
                    require(isinstance(task,dict),'MAC_HOST_HISTORY_INCOMPLETE')
                    # An unresolved/malformed canonical issue also fences its
                    # repository. Absence of an owner declaration is not proof.
                    if task:
                        key=(task.get('program','')+'-'+task.get('node','')).upper() if task.get('node') else '*'
                        claim={'origin':'GITHUB_PROJECTION','state':'UNRESOLVED','task':copy.deepcopy(task),'repository':repo,'task_key':key}
                        self.store.db.execute('INSERT OR IGNORE INTO mac_host_external VALUES (?,?,?,?)',(digest(claim),repo,key,encoded(claim)))
                self.store.db.execute('COMMIT')
            except Exception:
                self.store.db.execute('ROLLBACK'); raise
        return self.tasks(repo)

    def preflight(self,bound,*,allow_base_advance=False):
        # Fresh read-only projections add barriers only. They never release an
        # external owner, infer terminal execution or advance the pinned plan.
        snapshot=handoff.inspect_repository(bound['repository'])
        import mac_astra_receipt
        with self.store.lock:
            task=self._task(self.store.db,bound)
            node=parse_json(task['work'],1024*1024)['task']
        mac_astra_receipt.admission(node)
        if 'generation_id' in bound:
            import mac_generation
            mac_generation.preflight(self,bound,snapshot)
        with self.store.lock:
            self._task(self.store.db,bound)
            record=self.program(bound)
            require(snapshot.get('task_scope')=='all' and isinstance(snapshot.get('tasks'),list) and
                    (allow_base_advance or snapshot['source']['head']==record['head']) and snapshot['source']['blob']==record['scope']['blob'],
                    'MAC_HOST_PROGRAM_REVISION_CHANGED')
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                for task in snapshot['tasks']:
                    require(isinstance(task,dict),'MAC_HOST_HISTORY_INCOMPLETE')
                    key=(task.get('program','')+'-'+task.get('node','')).upper() if task.get('node') else '*'
                    claim={'origin':'GITHUB_PROJECTION','state':'UNRESOLVED','task':copy.deepcopy(task),
                           'repository':bound['repository'].lower(),'task_key':key}
                    self.store.db.execute('INSERT OR IGNORE INTO mac_host_external VALUES (?,?,?,?)',
                        (digest(claim),bound['repository'].lower(),key,encoded(claim)))
                self.store.db.execute('COMMIT')
            except Exception:
                self.store.db.execute('ROLLBACK'); raise
        self.check_external(bound)

    def tasks(self,repo):
        with self.store.lock:
            rows=self.store.db.execute('SELECT task,binding,state FROM mac_host_tasks WHERE repository=? ORDER BY id',(repository(repo).lower(),)).fetchall()
        return [{'task_id':r['task'],'binding':parse_json(r['binding']),'state':r['state']} for r in rows]

    def _task(self,db,bound):
        require(self.authority_initialized and bound.get('authority_kind')=='MAC_LOCAL' and
                (bound.get('source_host'),bound.get('target_host'))==(self.source_host,self.target_host),'MAC_HOST_AUTHORITY_BINDING_MISMATCH')
        row=db.execute('SELECT * FROM mac_host_tasks WHERE repository=? AND task=?',(bound['repository'].lower(),bound['task_id'])).fetchone()
        require(row is not None and parse_json(row['binding'])==bound,'MAC_HOST_CANONICAL_BINDING_MISMATCH')
        if 'generation_id' in bound:
            import mac_generation
            mac_generation.record(self,bound)
        return row

    def generation(self,value):
        import mac_generation
        return mac_generation.adopt(self,value)

    def program(self,bound):
        if 'generation_id' in bound:
            import mac_generation
            return mac_generation.record(self,bound)['program']
        row=self.store.db.execute('SELECT document FROM mac_host_programs WHERE repository=?',
                                  (bound['repository'].lower(),)).fetchone()
        require(row is not None,'MAC_HOST_PROGRAM_NOT_FOUND')
        return parse_json(row[0])

    def check_external(self,bound):
        with self.store.lock:
            self._task(self.store.db,bound)
            if 'generation_id' in bound:
                import mac_generation
                return mac_generation.check_external(self,bound)
            conflict=self.store.db.execute("SELECT 1 FROM mac_host_external WHERE repository=? AND task IN (?, '*')",(bound['repository'].lower(),bound['task_id'])).fetchone()
            require(not conflict,'MAC_HOST_EXTERNAL_EXECUTION_UNRESOLVED')
            for fence in transport_guard.unresolved(self.store.directory,include_digest=True):
                row=self.store.db.execute('SELECT document FROM mac_host_receipt_scopes WHERE request=?',(fence['request_id'],)).fetchone()
                require(row is not None,'MAC_HOST_EXTERNAL_SCOPE_UNRESOLVED')
                scoped=parse_json(row[0])
                require(scoped['payload_sha256']==fence['payload_sha256'],'MAC_HOST_RECEIPT_ORIGIN_UNVERIFIED')
                require(not(scoped['repository']==bound['repository'].lower() and scoped['task_id'] in ('*',bound['task_id'])),'MAC_HOST_EXTERNAL_EXECUTION_UNRESOLVED')

    def _read(self,db,bound):
        row=self._task(db,bound); self.check_external(bound)
        require(row['state']=='READY','MAC_HOST_TASK_COMPLETION_GATE_REQUIRED')
        work=parse_json(row['work'],1024*1024); deps=[]
        require(work['settings']['publish_pr'] is True,'MAC_HOST_PR_PUBLICATION_REQUIRED')
        if 'generation_id' in bound and bound['dependencies']:
            import mac_generation
            deps=mac_generation.frozen_dependencies(self,bound,live=True)
        for node in (() if 'generation_id' in bound else bound['dependencies']):
            key=(bound['program']+'-'+node).upper()
            dependency=db.execute('SELECT task,state,binding FROM mac_host_tasks WHERE repository=? AND task=?',(bound['repository'].lower(),key)).fetchone()
            require(dependency is not None and dependency['state']=='ACCEPTED','MAC_HOST_DEPENDENCY_GATE_REQUIRED')
            deps.append({'task_id':key,'state':'ACCEPTED','binding_sha256':digest(parse_json(dependency['binding']))})
        import mac_astra_receipt
        mac_astra_receipt.admission(work['task'])
        return {'binding':bound,'work':work,'gate_evidence':'mac-host:'+self.source_host+'#'+
                ('generation-'+bound['generation_id'] if 'generation_id' in bound else 'explicit-owner-mode'), 'history':[],'dependencies':deps}

    @staticmethod
    def _private_json(path):
        path=Path(path)
        try:
            fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd,'r') as stream:
                info=os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not(info.st_mode & 0o077)
                        and info.st_nlink==1 and info.st_size<=4*1024*1024,'MAC_HOST_PRIVATE_RECEIPT_REQUIRED')
                return parse_json(stream.read(4*1024*1024+1))
        except (OSError,UnicodeError):
            raise AppError('MAC_HOST_PRIVATE_RECEIPT_REQUIRED') from None

    def _terminal(self,request,terminal):
        attempt=request['attempt']
        folder=self.store.directory / 'native' / request['request_id'] / attempt['id']
        return self.private_receipt(folder,attempt,terminal['result_sha256'])

    def private_receipt(self,folder,attempt,result_sha256=None):
        folder=Path(folder)
        for path in (folder.parent,folder):
            try: info=path.lstat()
            except OSError: raise AppError('MAC_HOST_PRIVATE_RECEIPT_REQUIRED') from None
            require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and not(info.st_mode & 0o077),
                    'MAC_HOST_PRIVATE_RECEIPT_REQUIRED')
        receipt=self._private_json(folder / 'receipt.json')
        require(receipt.get('attempt_id')==attempt['id'] and receipt.get('binding')==attempt['binding'] and
                receipt.get('process_group_quiescent') is True and
                (result_sha256 is None or digest(receipt)==result_sha256),
                'MAC_HOST_TERMINAL_RECEIPT_UNPROVEN')
        if receipt.get('provider_started') is True:
            process=self._private_json(folder / 'provider-process.json')
            exit_record=self._private_json(folder / 'provider-exit.json')
            require(process.get('attempt_id')==attempt['id'] and process.get('binding')==attempt['binding'] and
                    type(process.get('pid')) is int and process['pid']>1 and process.get('pgid')==process['pid'] and
                    exit_record.get('attempt_id')==attempt['id'] and exit_record.get('binding')==attempt['binding'] and
                    exit_record.get('pid')==process['pid'] and exit_record.get('exit_code')==receipt.get('exit_code'),
                    'MAC_HOST_TERMINAL_RECEIPT_UNPROVEN')
            try: os.killpg(process['pgid'],0)
            except ProcessLookupError: pass
            except OSError: raise AppError('MAC_HOST_PROCESS_GROUP_UNVERIFIED')
            else: raise AppError('MAC_HOST_PROCESS_GROUP_STILL_ACTIVE')
        else:
            require(receipt.get('provider_started') is False and isinstance(receipt.get('error'),str) and
                    receipt.get('report') is None,'MAC_HOST_TERMINAL_RECEIPT_UNPROVEN')
        return receipt

    def status(self):
        with self.store.lock:
            repositories=[r[0] for r in self.store.db.execute('SELECT repository FROM mac_host_programs ORDER BY repository')]
            active=self.store.db.execute("SELECT request,repository,task,state FROM mac_host_attempts WHERE state!='TERMINAL'").fetchall()
            generations=[{'generation_id':r['id'],'repository':r['repository']} for r in self.store.db.execute('SELECT id,repository FROM mac_host_generations ORDER BY rowid')]
        return {'mode':self.meta(),'repositories':repositories,'generations':generations,'active':[dict(r) for r in active],
                'automatic_execution_enabled':False,'legacy_termination':'UNVERIFIED'}

    def select(self,repo,task):
        matches=[r['binding'] for r in self.tasks(repo) if r['task_id']==task.upper()]
        require(len(matches)==1,'MAC_HOST_TASK_NOT_FOUND')
        return matches[0]

    def advance_base(self,value):
        require(isinstance(value,dict) and set(value)=={'repository','decision'} and
                isinstance(value['decision'],str) and 1<=len(value['decision'])<=2000,'MAC_HOST_REQUEST_INVALID')
        repo=repository(value['repository']).lower()
        snapshot=handoff.inspect_repository(repo)
        with self.store.lock:
            previous=self.store.db.execute('SELECT document FROM mac_host_programs WHERE repository=?',(repo,)).fetchone()
            require(previous is not None,'MAC_HOST_PROGRAM_NOT_FOUND')
            program=parse_json(previous[0])
            require(snapshot['source']['blob']==program['scope']['blob'] and snapshot['source']['branch']==program['branch'],
                    'MAC_HOST_PROGRAM_REVISION_CHANGED')
            require(not any(j['attempt'] or j['state'] not in TERMINAL for j in self.store.jobs()) and not self.store.execution_busy,
                    'MAC_HOST_LOCAL_WORK_BUSY')
            require(not self.store.db.execute("SELECT 1 FROM native_local WHERE state!='TERMINAL'").fetchone() and
                    not self.store.db.execute("SELECT 1 FROM mac_host_attempts WHERE state!='TERMINAL'").fetchone() and
                    not self.store.db.execute("SELECT 1 FROM mac_host_tasks WHERE repository=? AND state NOT IN ('READY','ACCEPTED')",(repo,)).fetchone(),
                    'MAC_HOST_BASE_ADVANCE_BUSY')
            head=snapshot['source']['head']
            if head==program['head']: return self.tasks(repo)
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                program['base_advances']=[*program.get('base_advances',[]),{'from':program['head'],'to':head,
                     'decision':value['decision'],'at':time.time()}]
                require(len(program['base_advances'])<=256,'MAC_HOST_BASE_ADVANCE_HISTORY_LIMIT')
                program['head']=head
                for row in self.store.db.execute("SELECT * FROM mac_host_tasks WHERE repository=? AND state='READY'",(repo,)).fetchall():
                    bound=parse_json(row['binding']); work=parse_json(row['work'],1024*1024)
                    # Only never-active/terminal-prestart work can have its base
                    # advanced. Task, original spec, revision and owner stay pinned.
                    bound['plan_commit']=head; work['plan_commit']=head; bound['work_sha256']=digest(work)
                    self.store.db.execute('UPDATE mac_host_tasks SET binding=?,work=? WHERE id=?',
                                         (encoded(bound),encoded(work),row['id']))
                self.store.db.execute('UPDATE mac_host_programs SET document=? WHERE repository=?',(encoded(program),repo))
                for task in snapshot['tasks']:
                    key=(task.get('program','')+'-'+task.get('node','')).upper() if task.get('node') else '*'
                    claim={'origin':'GITHUB_PROJECTION','state':'UNRESOLVED','task':copy.deepcopy(task),'repository':repo,'task_key':key}
                    self.store.db.execute('INSERT OR IGNORE INTO mac_host_external VALUES (?,?,?,?)',(digest(claim),repo,key,encoded(claim)))
                self.store.db.execute('COMMIT')
            except Exception:
                self.store.db.execute('ROLLBACK'); raise
        return self.tasks(repo)

    @staticmethod
    def _view(row):
        return {**parse_json(row['document'],1024*1024),'state':row['state'],'terminal':parse_json(row['terminal']) if row['terminal'] else None}

    def call(self,operation,payload):
        from native_transfer import SourceReply
        with self.store.lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                db=self.store.db
                if operation=='read':
                    view={'status':'OBSERVED',**self._read(db,payload),'observed_at':time.time()}
                elif operation=='reserve':
                    require(set(payload)=={'request_id','binding','attempt'},'MAC_HOST_RESERVATION_INVALID')
                    bound=payload['binding']; snapshot=self._read(db,bound); attempt=payload['attempt']
                    require(isinstance(attempt,dict) and set(attempt)=={'id','head','profile','binding'} and
                            isinstance(attempt['id'],str) and re.fullmatch(r'[0-9a-f]{32}',attempt['id']) and
                            isinstance(attempt['binding'],str) and re.fullmatch(r'[0-9a-f]{64}',attempt['binding']),
                            'MAC_HOST_ATTEMPT_BINDING_MISMATCH')
                    require(attempt['profile']==snapshot['work']['profile'] and attempt['head']==bound['plan_commit'] and
                        attempt['binding']==digest({'request_id':payload['request_id'],'canonical':bound,**{k:v for k,v in attempt.items() if k!='binding'}}),'MAC_HOST_ATTEMPT_BINDING_MISMATCH')
                    previous=db.execute('SELECT * FROM mac_host_attempts WHERE request=?',(payload['request_id'],)).fetchone()
                    if previous:
                        require(parse_json(previous['document'])['request']==payload,'MAC_HOST_REQUEST_CONFLICT'); view=self._view(previous)
                    else:
                        require(not db.execute("SELECT 1 FROM mac_host_attempts WHERE state!='TERMINAL'").fetchone(),'MAC_HOST_RESERVATION_BUSY')
                        document={'schema_version':1,'request':copy.deepcopy(payload),'source_snapshot_sha256':digest(snapshot),
                                  'source_authorization':'mac-host:'+self.source_host+'#explicit-owner-mode','work':snapshot['work'],'reserved_at':time.time()}
                        db.execute('INSERT INTO mac_host_attempts VALUES (?,?,?,?,?,NULL)',(payload['request_id'],bound['repository'].lower(),bound['task_id'],'RESERVED',encoded(document)))
                        view=self._view(db.execute('SELECT * FROM mac_host_attempts WHERE request=?',(payload['request_id'],)).fetchone())
                else:
                    require(operation in ('claim','status','finish'),'MAC_HOST_OPERATION_UNSUPPORTED')
                    row=db.execute('SELECT * FROM mac_host_attempts WHERE request=?',(payload['request_id'],)).fetchone()
                    require(row is not None,'MAC_HOST_ATTEMPT_NOT_FOUND')
                    document=parse_json(row['document'],1024*1024); request=document['request']; self._task(db,request['binding'])
                    require(payload['attempt']==request['attempt'],'MAC_HOST_ATTEMPT_BINDING_MISMATCH')
                    if operation=='claim':
                        self.check_external(request['binding']); require(row['state']!='TERMINAL','MAC_HOST_TERMINAL_REPLAY')
                        db.execute("UPDATE mac_host_attempts SET state='CLAIMED' WHERE request=? AND state='RESERVED'",(row['request'],))
                    elif operation=='finish':
                        terminal=payload['terminal']; attempt=request['attempt']
                        require(row['state'] in ('CLAIMED','TERMINAL') and terminal.get('attempt_id')==attempt['id'] and
                                terminal.get('binding')==attempt['binding'] and terminal.get('head')==attempt['head'] and
                                terminal.get('process_group_quiescent') is True,'MAC_HOST_TERMINAL_BINDING_MISMATCH')
                        if row['state']=='TERMINAL':
                            require(parse_json(row['terminal'])==terminal,'MAC_HOST_TERMINAL_IMMUTABLE')
                            self._terminal(request,terminal)
                        else:
                            receipt=self._terminal(request,terminal)
                            db.execute("UPDATE mac_host_attempts SET state='TERMINAL',terminal=? WHERE request=?",(encoded(terminal),row['request']))
                            state='READY' if receipt['provider_started'] is False else 'REVIEW_REQUIRED' if (receipt.get('report') or {}).get('status')=='complete' else 'REWORK_REQUIRED'
                            db.execute("UPDATE mac_host_tasks SET state=? WHERE repository=? AND task=?",(state,row['repository'],row['task']))
                    view=self._view(db.execute('SELECT * FROM mac_host_attempts WHERE request=?',(row['request'],)).fetchone())
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK'); raise
        return SourceReply(self.source_host,self.target_host,view)
