"""Continue one admitted Mac task through the existing inspection/draft loop.

Only bound private native terminal receipts create a delivery job. Model text,
caller-supplied review/CI JSON and a second request cannot create another writer.
"""
from __future__ import annotations

import copy
import re
import time

import agents
from common import AppError, TERMINAL, digest, encoded, parse_json
from mac_authority import require


class Pipeline:
    def __init__(self,store,source,repos,*,failure=None):
        self.store,self.source,self.repos=store,source,repos
        self.failure=failure

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
            require(job['settings']['roles']['builder']==work['profile'] and job['base_sha']==bound['plan_commit'] and
                    job['program_scope']['blob']==bound['plan_blob'] and job['settings']['publish_pr'] is True and
                    job['branch']=='aiops/native-'+lineage['request_id'][:16] and
                    len(job['plan']['tasks'])==1 and job['plan']['tasks'][0]['id']==work['task']['id'] and
                    job['plan']['tasks'][0]['instructions']==work['task']['spec'],'MAC_HOST_DELIVERY_BINDING_MISMATCH')
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
            job.update(native_lineage=lineage,settings=copy.deepcopy(work['settings']),base_sha=bound['plan_commit'],
                base_branch=program['branch'],branch='aiops/native-'+rid[:16],program_scope=copy.deepcopy(program['scope']),
                plan={'summary':node['title'],'sources':['.aiops/program.json'],'tasks':[item]},
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
            if passing:
                job.update(state='reviewing',phase='reviewing',summary=report['summary'],task_index=1,built_tasks=[node['id']])
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

    def validate_inspection(self,job,*,refresh=True):
        self.assert_job(job,refresh=refresh)
        require(job['pr_url'] and job['ci'] and job['ci']['state']=='passed' and job['ci']['checks'] and
                job['ci'].get('head')==job['head'] and job['ci'].get('source')=='GITHUB_ACTIONS_API' and
                self.repos.head(job)==job['head'] and self.repos.clean(job),'MAC_HOST_LIVE_CI_REQUIRED')
        identities=set(s for s in job.get('builder_sessions',[]) if s)
        for role in ('review','supervision'):
            evidence=job.get(role)
            require(evidence and evidence['head']==job['head'],'MAC_HOST_CURRENT_HEAD_REVIEW_REQUIRED')
            folder=self.store.directory / 'jobs' / job['id'] / evidence['attempt']
            request=self.source._private_json(folder / 'request.json')
            expected=digest({'job':job['id'],'attempt':evidence['attempt'],'head':job['head'],
                             'role':'reviewer' if role=='review' else 'supervisor','profile':evidence['profile'],
                             'plan':job['plan'],'task':None})
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
                report['status']=='complete' and report['reviewed_head']==job['head'] and report['covered_tasks']==[job['plan']['tasks'][0]['id']] and
                report['checks'] and all(c.strip() for c in report['checks']) and not report['findings'] and not report['question'].strip(),'MAC_HOST_INDEPENDENT_REVIEW_REQUIRED')
            identities.add(sid)
        require(job.get('builder_sessions') and all(s for s in job['builder_sessions']),'MAC_HOST_BUILDER_IDENTITY_REQUIRED')

    def record_inspection(self,job):
        require(job['state']=='accepted','MAC_HOST_USER_INSPECTION_REQUIRED')
        lineage=job['native_lineage']; row,document=self._document(lineage['request_id'])
        require(row and row['job']==job['id'],'MAC_HOST_DELIVERY_BINDING_MISMATCH')
        inspection={'head':job['head'],'pr_url':job['pr_url'],'task_revision':lineage['binding']['task_revision'],
                    'review_sha256':digest(job['review']),'supervision_sha256':digest(job['supervision']),
                    'ci_sha256':digest(job['ci']),'user_inspected_at':job['accepted_at']}
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
