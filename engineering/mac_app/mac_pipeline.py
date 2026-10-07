"""Continue one admitted Mac task through the existing inspection/draft loop.

Only bound private native terminal receipts create a delivery job. Model text,
caller-supplied review/CI JSON and a second request cannot create another writer.
"""
from __future__ import annotations

import copy
import re
import time

import agents
import mac_bundle
import handoff
from common import AppError, TERMINAL, digest, encoded, parse_json
from mac_authority import require


class Pipeline:
    def __init__(self,store,source,repos,*,failure=None):
        self.store,self.source,self.repos=store,source,repos
        self.failure=failure
        self._astra=None

    @property
    def astra(self):
        # Read-only legacy inspections/install checks must not migrate a ledger.
        if self._astra is None:
            from mac_astra_receipt import Journal
            self._astra=Journal(self.store)
        return self._astra

    def _document(self,request):
        row=self.store.db.execute('SELECT * FROM mac_host_deliveries WHERE request=?',(request,)).fetchone()
        return (row,parse_json(row['document'])) if row else (None,None)

    def assert_job(self,job,*,refresh):
        lineage=job.get('native_lineage'); require(isinstance(lineage,dict),'MAC_HOST_DELIVERY_BINDING_MISMATCH')
        bound=lineage['binding']
        if refresh: self.source.preflight(bound)
        with self.store.lock:
            task=self.source._task(self.store.db,bound)
            row,document=self._document(lineage['request_id'])
            require(row and row['job']==job['id'] and document['lineage']==lineage and
                    task['state'] in ('DELIVERING','INSPECTED','ACCEPTED'),'MAC_HOST_DELIVERY_BINDING_MISMATCH')
            work=parse_json(task['work'],1024*1024)
            if 'generation_id' in bound:
                import mac_generation
                require(job.get('generation_policy')==work['generation_policy']==mac_generation.POLICY and
                        job.get('generation_decision')==work['generation_decision'], 'MAC_GENERATION_PUBLICATION_POLICY_REQUIRED')
                if bound['dependencies']: mac_generation.frozen_dependencies(self.source,bound)
            require(job['settings']['roles']['builder']==work['profile'] and job['base_sha']==bound['plan_commit'] and
                    job['program_scope']['blob']==bound['plan_blob'] and job['settings']['publish_pr'] is True and
                    job['branch']=='aiops/native-'+lineage['request_id'][:16] and
                    job['plan']['tasks']==mac_bundle.plan_tasks(work) and job.get('bundle')==work.get('bundle'),'MAC_HOST_DELIVERY_BINDING_MISMATCH')
            self.source.check_external(bound)

    def deliver(self):
        with self.store.lock:
            if not self.source.authority_initialized: return False
            # A pending legacy job cannot be bypassed by adopting its checkout.
            if any(j['attempt'] or j['state'] not in TERMINAL for j in self.store.jobs()): return False
            rows=self.store.db.execute("SELECT a.* FROM mac_host_attempts a JOIN mac_host_tasks t "
                "ON a.repository=t.repository AND a.task=t.task WHERE a.state='TERMINAL' "
                "AND t.state IN ('REVIEW_REQUIRED','REWORK_REQUIRED') ORDER BY a.rowid DESC").fetchall()
            if not rows: return False
            attempt_row=rows[0]; request=parse_json(attempt_row['document'],1024*1024)['request']
            bound=request['binding']; rid=request['request_id']
            # A source terminal write whose app-side reply was lost still holds
            # native_local. Only Controller.reconcile can finish that recovery.
            local=self.store.db.execute('SELECT state FROM native_local WHERE request=?',(rid,)).fetchone()
            if not local or local['state']!='TERMINAL': return False
            terminal=parse_json(attempt_row['terminal'])
            key=digest({'native_delivery':rid})[:16]
            lineage={'request_id':rid,'attempt_id':request['attempt']['id'],'binding':copy.deepcopy(bound)}
            row,document=self._document(rid)
            if row:
                require(row['job']==key and document['lineage']==lineage,'MAC_HOST_DELIVERY_BINDING_MISMATCH')
            else:
                document={'lineage':lineage,'terminal_sha256':digest(terminal),'created_at':time.time()}
                self.store.db.execute('INSERT INTO mac_host_deliveries VALUES (?,?,?,?)',(rid,key,'PREPARING',encoded(document)))
        try:
            # Lost or changed private proof holds this task without terminating
            # the service or silently substituting a second execution.
            self.source.preflight(bound)
            with self.store.lock:
                receipt=self.source._terminal(request,terminal)
                require(receipt['provider_started'] is True,'MAC_HOST_DELIVERY_RECEIPT_REQUIRED')
                task=self.source._task(self.store.db,bound)
                work=parse_json(task['work'],1024*1024)
                program=self.source.program(bound)
            job=self.store.new_document(key,'native-delivery-'+rid,bound['repository'],work['task']['spec'])
            node=work['task']
            item={'id':node['id'],'title':node['title'],'instructions':node['spec'],
                  'acceptance':['Satisfy the complete original task spec and repository verification requirements.'],
                  # Local delivery contains one already-admitted node. Its exact
                  # program dependencies remain in the immutable canonical binding.
                  'depends_on':[]}
            items=mac_bundle.plan_tasks(work)
            if 'bundle' in work: job['bundle']=copy.deepcopy(work['bundle'])
            job.update(native_lineage=lineage,settings=copy.deepcopy(work['settings']),base_sha=bound['plan_commit'],
                base_branch=program['branch'],branch='aiops/native-'+rid[:16],program_scope=copy.deepcopy(program['scope']),
                plan={'summary':node['title'],'sources':['.aiops/program.json'],'tasks':items},
                source_pins={'.aiops/program.json':bound['plan_blob']},calls=1,task_index=0,built_tasks=[],
                current_task=item,builder_sessions=[] if receipt.get('error')=='PROVIDER_LOGIN_REQUIRED' else
                    [(receipt.get('provider_evidence') or {}).get('session_id')],
                feedback=[],correcting=False,admission={'mode':'mac_local','binding':bound})
            if 'generation_id' in bound:
                job['generation_policy']=copy.deepcopy(work['generation_policy'])
                job['generation_decision']=work['generation_decision']
            # The existing host checkpoint validates remote/branch/history and
            # authority-file changes before staging or creating a local commit.
            head=self.repos.checkpoint(job); job['head']=head
            report=receipt.get('report')
            if report is not None: agents.validate_report(report)
            passing=report and report['status']=='complete' and not report['findings'] and not report['question'].strip() and report['checks'] and all(c.strip() for c in report['checks'])
            if passing and job.get('bundle'):
                try: mac_bundle.validate_report(report, job['bundle'])
                except AppError: passing=False
            if passing:
                job.update(state='reviewing',phase='reviewing',summary=report['summary'],task_index=len(items),built_tasks=[n['id'] for n in items])
            elif report and report['status']=='needs_user':
                # A successful provider exit is not a successful implementation.
                # Preserve the exact question without spending another model call.
                job.update(state='needs_user',phase='building',correcting=True,
                    summary=report['summary'],question=report['question'],feedback=report['findings'])
            else:
                job.update(state='building',phase='building',correcting=True,
                    feedback=(report or {}).get('findings') or [receipt.get('error') or (report or {}).get('summary') or 'Native task needs rework.'])
                if receipt.get('error')=='USER_STOPPED': job.update(state='paused',pause_requested=True)
            job['last_terminal']={'attempt':lineage['attempt_id'],'role':'builder','head':head,
                'at':time.time(),'exit_code':receipt['exit_code'],'status':(report or {}).get('status'),
                'error':receipt.get('error')}
            with self.store.lock:
                self.store.db.execute('BEGIN IMMEDIATE')
                try:
                    previous=self.store.db.execute('SELECT document FROM jobs WHERE id=? OR request_id=?',(key,job['request_id'])).fetchone()
                    if previous:
                        existing=parse_json(previous[0])
                        require(existing.get('native_lineage')==lineage,'MAC_HOST_DELIVERY_BINDING_MISMATCH')
                    else:
                        self.store.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                            (key,job['request_id'],job['repository'],job['state'],self.store.document(job),job['created']))
                        self.store.event(key,'canonical_delivery','같은 Mac canonical 작업의 산출물을 독립 감사·감리 경로로 전달했습니다.')
                    self.store.db.execute("UPDATE mac_host_deliveries SET state='DELIVERING' WHERE request=?",(rid,))
                    self.store.db.execute("UPDATE mac_host_tasks SET state='DELIVERING' WHERE repository=? AND task=?",
                        (bound['repository'].lower(),bound['task_id']))
                    self.store.db.execute('COMMIT')
                except Exception:
                    self.store.db.execute('ROLLBACK'); raise
            if receipt.get('error') and receipt['error']!='USER_STOPPED' and self.failure:
                self.failure(self.store.get(key),receipt['error'])
        except Exception as exc:
            with self.store.lock:
                row,document=self._document(rid)
                document['error']=exc.code if isinstance(exc,AppError) else type(exc).__name__
                self.store.db.execute("UPDATE mac_host_deliveries SET state='BLOCKED',document=? WHERE request=?",(encoded(document),rid))
                self.store.db.execute("UPDATE mac_host_tasks SET state='DELIVERY_BLOCKED' WHERE repository=? AND task=?",
                    (bound['repository'].lower(),bound['task_id']))
        return True

    def audit_requirement(self,job):
        """Observe the admitted schema-v1 node's gate; never manufacture a receipt.

        Defaults match the adopted program reader. A3 promotes to ARCHITECTURE
        while a declared RELEASE remains reserved. Other nodes stay unchanged.
        """
        import mac_astra
        bound=job['native_lineage']['binding']
        return mac_astra.audit_requirement(job,self.source.program(bound)['scope'])

    @handoff.bounded_api_reads
    def validate_audit(self,job):
        requirement=self.audit_requirement(job)
        if not requirement['required']: return requirement
        require(isinstance(requirement.get('request_binding'),dict),'MAC_HOST_ASTRA_AUDIT_REQUIRED')
        self.validate_candidate(job,refresh=False)
        require(job.get('candidate_published_head')==job['head'] and job.get('ci') and
                job['ci'].get('state')=='passed' and job['ci'].get('head')==job['head'] and
                job['ci'].get('source')=='GITHUB_ACTIONS_API','MAC_HOST_LIVE_CI_REQUIRED')
        request=self.astra.request(requirement)
        requirement={**requirement,'request_recorded_at':request['created_at']}
        with self.store.lock:
            recorded=self.store.get(job['id']).get('audit_requirement')
            # Live verification must not rewrite an unchanged accepted job:
            # dependency proofs bind its complete private document digest.
            if not isinstance(recorded,dict) or {**recorded,'audit_receipt':None}!=requirement:
                self.store.update(job['id'],audit_requirement=requirement)
        # A scoped Mac run is separate from the immutable original Fable request.
        # Check both producers: a Mac negative/deletion cannot be bypassed by a
        # Fable PASS, and a Fable negative cannot be bypassed by a Mac PASS.
        import mac_glm_audit
        mac_receipt=mac_glm_audit.existing_receipt(self.store,requirement,
                                                expected=recorded.get('audit_receipt') if isinstance(recorded,dict) else None)
        try: receipt=self.astra.consume(requirement)
        except AppError as exc:
            if exc.code!='MAC_HOST_ASTRA_AUDIT_REQUIRED' or mac_receipt is None: raise
            receipt=mac_receipt
        if (mac_receipt is not None and isinstance(recorded,dict) and
                (recorded.get('audit_receipt') or {}).get('source')=='AUTHENTICATED_MAC_GLM_AUDIT_COMMENT'):
            receipt=mac_receipt  # keep an already consumed receipt stable; both channels were checked
        require(receipt['comment']['auditor_session'] not in
                self.writer_sessions(job)+[job['review']['provider_evidence']['session_id']],
                'MAC_HOST_INDEPENDENT_REVIEW_REQUIRED')
        with self.store.lock:
            self.astra.check_hold(requirement)
            current=self.store.get(job['id'])
            require(self.audit_requirement(current)['request_sha256']==requirement['request_sha256'] and
                    self.repos.head(current)==job['head'] and self.repos.clean(current),
                    'MAC_HOST_ASTRA_RECEIPT_CHANGED')
            requirement={**requirement,'audit_receipt':receipt}
            if current.get('audit_requirement')!=requirement:
                self.store.update(job['id'],audit_requirement=requirement)
        return requirement

    def validate_candidate(self,job,*,refresh=True):
        self.assert_job(job,refresh=refresh)
        require(self.repos.head(job)==job['head'] and self.repos.clean(job),'MAC_HOST_INSPECTION_CHANGED')
        self.validate_reviews(job,('review',))

    @handoff.bounded_api_reads
    def validate_inspection(self,job,*,refresh=True):
        self.assert_job(job,refresh=refresh)
        require(job['pr_url'] and job['ci'] and job['ci']['state']=='passed' and job['ci']['checks'] and
                job['ci'].get('head')==job['head'] and job['ci'].get('source')=='GITHUB_ACTIONS_API' and
                self.repos.head(job)==job['head'] and self.repos.clean(job),'MAC_HOST_LIVE_CI_REQUIRED')
        audit=self.validate_audit(job)
        self.validate_reviews(dict(job,audit_requirement=audit),('review','supervision'))

    def writer_sessions(self,job):
        """Exclude only a proven initial native trust preflight, keeping its record."""
        writers=list(job.get('builder_sessions') or [])
        if writers and writers[0] is None:
            from codex_native_exec import QUALIFIED_BINARY
            lineage=job['native_lineage']
            row=self.store.db.execute('SELECT document,terminal,state FROM mac_host_attempts WHERE request=?',
                                      (lineage['request_id'],)).fetchone()
            require(row and row['state']=='TERMINAL','MAC_HOST_BUILDER_IDENTITY_REQUIRED')
            request=parse_json(row['document'],1024*1024)['request']; attempt=request['attempt']
            require(request['binding']==lineage['binding'] and attempt['id']==lineage['attempt_id'],
                    'MAC_HOST_BUILDER_IDENTITY_REQUIRED')
            receipt=self.source._terminal(request,parse_json(row['terminal']))
            folder=self.store.directory/'native'/lineage['request_id']/lineage['attempt_id']
            private=self.source._private_json(folder/'request.json')
            proof=self.source._private_json(folder/'codex-policy-evidence.json')
            error=self.source._private_json(folder/'codex-adapter-error.json')
            require(private['attempt_id']==proof['attempt_id']==error['attempt_id']==attempt['id'] and
                    private['binding']==proof['binding']==error['binding']==attempt['binding'] and
                    private['profile']==attempt['profile']==job['settings']['roles']['builder'] and
                    private['profile']['provider']=='codex' and private['role']==proof['role']=='builder' and
                    private['checkout']==proof['checkout']==str(self.repos.path(job).resolve()) and
                    private['host_directory']==str(self.store.directory.resolve()) and
                    receipt.get('error')==proof.get('error')==error.get('code')=='MAC_CODEX_TRUST_REQUIRED' and
                    receipt.get('report') is None and not receipt.get('provider_evidence') and
                    proof.get('qualified_binary_sha256')==QUALIFIED_BINARY and
                    proof.get('profile_application')=='qualified-native-exec' and
                    proof.get('model_turn_requested') is False and proof.get('commands')==[] and
                    not proof.get('thread_id') and proof.get('shutdown_verified') is True and
                    error.get('shutdown_verified') is True,'MAC_HOST_BUILDER_IDENTITY_REQUIRED')
            writers=writers[1:]
        require(all(isinstance(s,str) and s.strip() for s in writers),'MAC_HOST_BUILDER_IDENTITY_REQUIRED')
        return writers

    def validate_reviews(self,job,roles):
        writers=self.writer_sessions(job);identities=set(writers)
        for role in roles:
            evidence=job.get(role)
            require(evidence and evidence['head']==job['head'],'MAC_HOST_CURRENT_HEAD_REVIEW_REQUIRED')
            folder=self.store.directory / 'jobs' / job['id'] / evidence['attempt']
            request=self.source._private_json(folder / 'request.json')
            material={'job':job['id'],'attempt':evidence['attempt'],'head':job['head'],
                      'role':'reviewer' if role=='review' else 'supervisor','profile':evidence['profile'],
                      'plan':job['plan'],'task':None}
            if role=='supervision' and self.audit_requirement(job)['required']:
                require('host_verification_sha256' in request,'MAC_HOST_ASTRA_RECEIPT_CHANGED')
            if 'host_verification_sha256' in request:
                proof=request['host_verification_sha256']
                require(role=='supervision' and isinstance(proof,str) and
                        re.fullmatch(r'[0-9a-f]{64}',proof),'MAC_HOST_PRIVATE_REVIEW_REQUIRED')
                try:
                    context=parse_json(request['prompt'].split('\nTRUSTED JOB CONTEXT\n')[1].split('\nOUTPUT CONTRACT\n')[0])
                    snapshot=context['host_verification']
                except (KeyError,IndexError,TypeError,AttributeError,AppError):
                    raise AppError('MAC_HOST_PRIVATE_REVIEW_REQUIRED') from None
                require(isinstance(snapshot,dict) and digest(snapshot)==proof and
                        snapshot.get('source')=='AUTHENTICATED_GITHUB_API' and snapshot.get('head')==job['head'] and
                        snapshot.get('repository')==job['repository'] and context.get('id')==job['id'] and
                        context.get('exact_head')==job['head'] and context.get('plan')==job['plan'] and
                        context.get('canonical_binding')==job['native_lineage']['binding'],'MAC_HOST_PRIVATE_REVIEW_REQUIRED')
                audit=job.get('audit_requirement') or {}
                if self.audit_requirement(job)['required']:
                    require(snapshot.get('astra_audit')==audit and audit.get('audit_receipt') is not None,
                            'MAC_HOST_ASTRA_RECEIPT_CHANGED')
                material['host_verification_sha256']=proof
            expected=digest(material)
            receipt=self.source.private_receipt(folder,{'id':request['attempt_id'],'binding':request['binding']})
            require(request['attempt_id']==evidence['attempt'] and request['binding']==expected and
                request['host_directory']==str(self.store.directory.resolve()) and request['checkout']==str(self.repos.path(job).resolve()) and
                request['profile']==evidence['profile'] and request['role']==('reviewer' if role=='review' else 'supervisor') and
                receipt['exit_code']==0 and receipt['error'] is None and receipt['report']==evidence['report'] and
                receipt.get('provider_evidence')==evidence['provider_evidence'],'MAC_HOST_PRIVATE_REVIEW_REQUIRED')
            report=receipt['report']; agents.validate_report(report)
            actor=receipt['provider_evidence']; sid=actor.get('session_id')
            require(isinstance(sid,str) and sid and sid not in identities and actor['provider']==request['profile']['provider'] and
                actor['harness']==agents.CATALOG[actor['provider']]['harness'] and actor['model_requested']==request['profile']['model'] and
                report['status']=='complete' and report['reviewed_head']==job['head'] and report['covered_tasks']==[n['id'] for n in job['plan']['tasks']] and
                report['checks'] and all(c.strip() for c in report['checks']) and not report['findings'] and not report['question'].strip(),'MAC_HOST_INDEPENDENT_REVIEW_REQUIRED')
            mac_bundle.validate_report(report, job.get('bundle'))
            identities.add(sid)
        require(bool(writers),'MAC_HOST_BUILDER_IDENTITY_REQUIRED')

    def record_inspection(self,job):
        require(job['state']=='accepted','MAC_HOST_USER_INSPECTION_REQUIRED')
        lineage=job['native_lineage']; row,document=self._document(lineage['request_id'])
        require(row and row['job']==job['id'],'MAC_HOST_DELIVERY_BINDING_MISMATCH')
        inspection={'head':job['head'],'pr_url':job['pr_url'],'task_revision':lineage['binding']['task_revision'],
                    'review_sha256':digest(job['review']),'supervision_sha256':digest(job['supervision']),
                    'ci_sha256':digest(job['ci']),'user_inspected_at':job['accepted_at']}
        if self.audit_requirement(job)['required']:
            require((job.get('audit_requirement') or {}).get('audit_receipt') is not None,
                    'MAC_HOST_ASTRA_AUDIT_REQUIRED')
            inspection['astra_receipt_sha256']=digest(job['audit_requirement']['audit_receipt'])
        require('inspection' not in document or document['inspection']==inspection,'MAC_HOST_INSPECTION_IMMUTABLE')
        document['inspection']=inspection
        self.store.db.execute("UPDATE mac_host_deliveries SET state='INSPECTED',document=? WHERE request=?",(encoded(document),lineage['request_id']))
        self.store.db.execute("UPDATE mac_host_tasks SET state='INSPECTED' WHERE repository=? AND task=?",
            (job['repository'].lower(),lineage['binding']['task_id']))

    def retry_delivery(self,repo,task):
        bound=self.source.select(repo,task)
        self.source.preflight(bound)
        with self.store.lock:
            row=self.source._task(self.store.db,bound)
            require(row['state']=='DELIVERY_BLOCKED','MAC_HOST_DELIVERY_NOT_BLOCKED')
            require(not any(j['attempt'] or j['state'] not in TERMINAL for j in self.store.jobs()),'MAC_HOST_LOCAL_WORK_BUSY')
            attempt=self.store.db.execute("SELECT * FROM mac_host_attempts WHERE repository=? AND task=? ORDER BY rowid DESC LIMIT 1",
                                         (repo.lower(),bound['task_id'])).fetchone()
            require(attempt and attempt['state']=='TERMINAL','MAC_HOST_TERMINAL_RECEIPT_UNPROVEN')
            request=parse_json(attempt['document'],1024*1024)['request']
            self.source._terminal(request,parse_json(attempt['terminal']))
            self.store.db.execute("UPDATE mac_host_tasks SET state='REWORK_REQUIRED' WHERE repository=? AND task=?",(repo.lower(),bound['task_id']))
        return {'task_id':bound['task_id'],'state':'REWORK_REQUIRED','same_lineage':True}

    @handoff.bounded_api_reads
    def accepted_dependency(self,bound,plan_commit,*,current=True):
        """Read a normal ACCEPTED delivery and its actual private/live proof.

        No label, model PASS, legacy projection or caller-supplied evidence can
        substitute for the original owner, inspection and merge lineage.
        """
        with self.store.lock:
            task=self.source._task(self.store.db,bound)
            require(task['state']=='ACCEPTED','MAC_HOST_ACCEPTED_DEPENDENCY_REQUIRED')
            matches=[]
            for row in self.store.db.execute("SELECT * FROM mac_host_deliveries WHERE state='ACCEPTED'"):
                document=parse_json(row['document'])
                if document['lineage']['binding']==bound: matches.append((row,document))
            require(len(matches)==1,'MAC_HOST_DEPENDENCY_LINEAGE_UNVERIFIED')
            row,document=matches[0]; job=self.store.get(row['job']); lineage=job['native_lineage']
            inspection=document.get('inspection'); completion=document.get('completion')
            require(isinstance(inspection,dict) and isinstance(completion,dict) and
                    job['state']=='accepted' and job.get('accepted_head')==inspection.get('head')==job['head'] and
                    inspection.get('task_revision')==bound['task_revision'] and inspection.get('pr_url')==job['pr_url'] and
                    inspection.get('review_sha256')==digest(job['review']) and
                    inspection.get('supervision_sha256')==digest(job['supervision']) and
                    inspection.get('ci_sha256')==digest(job['ci']) and
                    completion.get('source')=='AUTHENTICATED_GITHUB_READ' and
                    completion.get('reviewed_head')==job['head'] and completion.get('pr_url')==job['pr_url'] and
                    completion.get('post_merge_ci',{}).get('state')=='passed' and
                    completion['post_merge_ci'].get('head')==completion.get('merge_head'),
                    'MAC_HOST_DEPENDENCY_INSPECTION_UNVERIFIED')
            native=self.store.db.execute('SELECT * FROM mac_host_attempts WHERE request=?',(row['request'],)).fetchone()
            require(native is not None and native['state']=='TERMINAL' and row['request']==lineage['request_id'],
                    'MAC_HOST_DEPENDENCY_LINEAGE_UNVERIFIED')
            request=parse_json(native['document'],1024*1024)['request']; terminal=parse_json(native['terminal'])
            require(request['binding']==bound and request['request_id']==row['request'] and
                    request['attempt']['id']==lineage['attempt_id'] and
                    document['terminal_sha256']==digest(terminal),'MAC_HOST_DEPENDENCY_LINEAGE_UNVERIFIED')
            self.source._terminal(request,terminal)
            local={'task_sha256':digest(dict(task)),'delivery_sha256':digest(dict(row)),
                   'native_sha256':digest(dict(native)),'job_sha256':digest(job)}
        self.validate_inspection(job,refresh=False)
        live=self.repos.dependency_completion(job,plan_commit,current=current)
        require(live['source']=='AUTHENTICATED_GITHUB_READ' and live['reviewed_head']==completion['reviewed_head'] and
                live['merge_head']==completion['merge_head'] and live['pr_url']==completion['pr_url'],
                'MAC_HOST_DEPENDENCY_COMPLETION_CHANGED')
        result={'node':bound['node'],'task_id':bound['task_id'],'task_revision':bound['task_revision'],
                'generation_id':bound['generation_id'],'request_id':row['request'],'job_id':job['id'],
                'state':'ACCEPTED','binding_sha256':digest(bound),'inspection_sha256':digest(inspection),
                'completion_sha256':digest(completion),'reviewed_head':job['head'],'merge_head':live['merge_head'],
                'recorded_ci_sha256':live['recorded_ci_sha256'],'tree':live['tree'],
                'gate_evidence':'mac-host:'+self.source.source_host+'#accepted-'+row['request'],'local':local}
        with self.store.lock: self.validate_dependency_local(result)
        return result

    def validate_dependency_local(self,evidence):
        """Detect a changed or partial local completion before any reservation."""
        db=self.store.db
        task=db.execute('SELECT * FROM mac_host_tasks WHERE task=?',(evidence['task_id'],)).fetchone()
        delivery=db.execute('SELECT * FROM mac_host_deliveries WHERE request=?',(evidence['request_id'],)).fetchone()
        native=db.execute('SELECT * FROM mac_host_attempts WHERE request=?',(evidence['request_id'],)).fetchone()
        require(task is not None and delivery is not None and native is not None and
                task['state']==delivery['state']=='ACCEPTED' and native['state']=='TERMINAL' and
                {'task_sha256':digest(dict(task)),'delivery_sha256':digest(dict(delivery)),
                 'native_sha256':digest(dict(native)),'job_sha256':digest(self.store.get(evidence['job_id']))}==evidence['local'],
                'MAC_HOST_DEPENDENCY_COMPLETION_CHANGED')

    @handoff.bounded_api_reads
    def reconcile_accepted(self,repo,task):
        bound=self.source.select(repo,task)
        self.source.preflight(bound,allow_base_advance=True)
        with self.store.lock:
            current=self.source._task(self.store.db,bound)
            require(current['state'] in ('INSPECTED','ACCEPTED'),'MAC_HOST_USER_INSPECTION_REQUIRED')
            matches=[]
            for row in self.store.db.execute("SELECT * FROM mac_host_deliveries WHERE state IN ('INSPECTED','ACCEPTED')"):
                document=parse_json(row['document'])
                if document['lineage']['binding']==bound: matches.append((row,document))
            require(len(matches)==1,'MAC_HOST_DELIVERY_BINDING_MISMATCH')
            row,document=matches[0]; job=self.store.get(row['job'])
            require(job['state']=='accepted' and job['accepted_head']==document['inspection']['head']==job['head'],
                    'MAC_HOST_USER_INSPECTION_REQUIRED')
            require(document['inspection']['review_sha256']==digest(job['review']) and
                    document['inspection']['supervision_sha256']==digest(job['supervision']) and
                    document['inspection']['ci_sha256']==digest(job['ci']),'MAC_HOST_INSPECTION_CHANGED')
        self.validate_inspection(job,refresh=False)
        # This read cannot merge. A model's PASS or a caller's claimed PR status
        # is never accepted; the native GitHub helper verifies merge/ancestry/CI.
        proof=self.repos.merged(job)
        with self.store.lock:
            self.assert_job(job,refresh=False)
            require(self.repos.head(job)==job['head'] and self.repos.clean(job),'MAC_HOST_INSPECTION_CHANGED')
            require('completion' not in document or document['completion']['merge_head']==proof['merge_head'],
                    'MAC_HOST_COMPLETION_IMMUTABLE')
            document['completion']=proof
            self.store.db.execute("UPDATE mac_host_deliveries SET state='ACCEPTED',document=? WHERE request=?",
                (encoded(document),row['request']))
            self.store.db.execute("UPDATE mac_host_tasks SET state='ACCEPTED' WHERE repository=? AND task=?",
                (repo.lower(),bound['task_id']))
        return {'task_id':bound['task_id'],'state':'ACCEPTED','head':proof['reviewed_head'],'merge_head':proof['merge_head']}
