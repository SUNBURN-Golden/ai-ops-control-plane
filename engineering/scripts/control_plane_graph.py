#!/usr/bin/env python3
"""Approved DAG around autonomous ticket loops; no LLM, polling or merge.

Ports must collect current authenticated GitHub/provider facts. CLI ports enforce
that boundary; raw webhook bodies and model output are never snapshots.
"""
from __future__ import annotations
import copy
import importlib.util
from pathlib import Path, PurePosixPath
import re

_spec = importlib.util.spec_from_file_location('graph_flow', Path(__file__).with_name('control_plane_flow.py'))
flow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flow)
require, digest = flow.require, flow.digest
REPOSITORIES = {'BeautifulMind-JT/'+r for r in
                ('ai-ops-control-plane','kix-protocol','ZARI','film-unit-mv-studio','maeum-gyeol')}


def validate_plan(plan):
    require(plan.get('schema_version') == 1 and plan.get('max_active_sessions') == 1,
            'approved single-session graph required')
    require(all(isinstance(plan.get(k),str) and plan[k] for k in ('graph_id','revision')),
            'graph identity missing')
    require(flow.github_pointer(plan.get('approval_pointer')), 'graph approval missing')
    nodes = plan.get('nodes')
    require(isinstance(nodes,list) and 0 < len(nodes) <= 100,'bounded graph required')
    ids, tasks = set(), set()
    for n in nodes:
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,64}',n.get('id','')) and n['id'] not in ids,
                'duplicate/invalid node')
        ids.add(n['id'])
        key = (n.get('repository'),n.get('task_id'))
        require(key[0] in REPOSITORIES and isinstance(key[1],str) and key[1] and key not in tasks,
                'duplicate/unregistered task')
        tasks.add(key)
        require(isinstance(n.get('revision'),str) and n['revision'], 'task revision missing')
        require(type(n.get('issue')) is int and n['issue'] > 0, 'issue required')
        require(n.get('task_pointer') == f"https://github.com/{n['repository']}/issues/{n['issue']}",
                'canonical task mismatch')
        require(flow.github_pointer(n.get('acceptance_pointer')), 'acceptance contract missing')
        require(re.fullmatch(r'[0-9a-f]{40}',n.get('base','')), 'base SHA missing')
        require(n.get('audit_floor') in ('A1','A2','A3') and
                n.get('astra_gate') in ('NONE','ARCHITECTURE','MILESTONE','RELEASE'), 'audit policy missing')
        if n['astra_gate'] in ('MILESTONE','RELEASE'):
            require(re.fullmatch(r'[0-9a-f]{64}',n.get('audit_scope_digest','')),
                    'milestone/release requires approved evidence packet digest')
        paths = n.get('allowed_paths')
        require(isinstance(paths,list) and paths and all(safe_path(p) for p in paths), 'scope missing')
        require(isinstance(n.get('locked_paths'),list) and all(safe_path(p) for p in n['locked_paths']),
                'explicit locked paths required')
        require(isinstance(n.get('required_checks'),list) and n['required_checks'], 'required checks absent')
        require(isinstance(n.get('dependencies'),list),'dependencies must be explicit')
        require(isinstance(n.get('writer'),dict) and all(n['writer'].get(k) for k in ('identity','lane')),
                'writer missing')
        reviewers = n.get('reviewers')
        require(isinstance(reviewers,list) and reviewers, 'independent reviewers missing')
        names = []
        for lane in reviewers:
            require(all(isinstance(lane.get(k),str) and lane[k] for k in ('identity','lane')) and
                    flow.github_pointer(lane.get('approval_pointer')), 'review designation missing')
            require(lane['identity'] != n['writer']['identity'], 'reviewer is writer')
            names.append(lane['identity'])
        require(len(set(names)) == len(names), 'reviewers must be independent identities')
    visiting, visited = set(),set()
    by_id = {n['id']:n for n in nodes}
    def visit(nid):
        require(nid in ids and nid not in visiting,'unknown dependency or cycle')
        if nid in visited: return
        visiting.add(nid)
        inputs=set()
        for dep in by_id[nid]['dependencies']:
            require(set(dep)=={'node','input'} and isinstance(dep['input'],str) and dep['input'] and
                    dep['input'] not in inputs,'dependency must name its consumed input')
            inputs.add(dep['input']); visit(dep['node'])
        visiting.remove(nid); visited.add(nid)
    for nid in ids: visit(nid)
    return by_id


def safe_path(path):
    return (isinstance(path,str) and bool(path) and not path.startswith('/') and
            '..' not in PurePosixPath(path).parts and '\\' not in path and
            not any(c in path for c in '*?['))


def within(path, rules):
    return safe_path(path) and any(path == rule or (rule.endswith('/') and path.startswith(rule))
                                   for rule in rules)


def bind(plan,node,snapshot,inputs):
    s=copy.deepcopy(snapshot)
    for key in ('repository','task_id','revision','base','task_pointer'):
        require(s.get(key)==node[key], 'snapshot/task mismatch: '+key)
    require(s.get('plan_digest')==digest(plan), 'snapshot plan changed')
    require(s.get('task_open') is True or s.get('pr_state')=='merged', 'task closed')
    require(s.get('inputs')==inputs, 'dependency input changed; same owner must rebase/revalidate')
    flow.subject(s)
    require(s.get('required_checks')==node['required_checks'], 'check policy drift')
    require(s.get('audit_floor')==node['audit_floor'] and s.get('astra_gate')==node['astra_gate'],
            'audit policy drift')
    s['policy_revision']=digest(plan)
    return s


def action(plan,node,s,kind,identity,designation,**extra):
    a={'kind':kind,'subject':flow.subject(s),'graph_id':plan['graph_id'],
       'graph_revision':plan['revision'],'node_id':node['id'],'inputs':s['inputs'],
       'identity':identity,'designation':designation,'task_pointer':node['task_pointer'],
       'pr_pointer':s.get('pr_pointer'),'attempt_id':1,
       'read_only':kind in ('REVIEW','AUDIT'),'allowed_paths':node['allowed_paths'],
       'acceptance_pointer':node['acceptance_pointer'],**extra}
    a['request_id']=digest(a)
    return a


def evaluate_node(plan,node,s,owner):
    """Return pointers, not paraphrased findings. No confidence score or retry count."""
    def emit(state,kind,lane,**extra):
        return {'state':state,'action':action(plan,node,s,kind,lane['identity'],
                lane.get('approval_pointer',plan['approval_pointer']),lane=lane['lane'],**extra)}
    def decision(reason,pointers):
        require(pointers and all(flow.github_pointer(p) for p in pointers),'decision evidence missing')
        return emit('DECISION_REQUIRED','DECISION',node['astra'],reason=reason,evidence=pointers)
    def fix(reason,pointers):
        require(owner and owner.get('session_id'), 'confirmed writer session missing')
        require(pointers and all(flow.github_pointer(p) for p in pointers),'failure pointer missing')
        return emit('CORRECTION_REQUIRED','FEEDBACK',node['writer'],
                    owner_session_id=owner['session_id'],reason=reason,evidence=pointers)
    require(s.get('ownership_verified') is True,'host/task ownership not verified')
    require(s.get('prerequisites_verified') is True,'repository prerequisites missing')
    require(s.get('blockers')==[], 'unresolved blocker')
    require(s.get('writer_state') not in ('BLOCKED','STALLED','BUDGET_LIMIT_REACHED'),
            'writer declared blocked/stalled or mechanical budget reached')
    if s.get('contract_change_required') is True:
        return decision('approved contract must change',s.get('decision_pointers',[]))
    require(s.get('contract_change_required') is False,'contract-change state unobserved')
    if owner:
        require(s.get('owner_session_id')==owner.get('session_id') and owner.get('session_id'),
                'owner session mismatch/unconfirmed')
    elif s.get('owner_session_id'):
        raise flow.FlowError('existing session requires explicit reconciled adoption; never relaunch')
    if s['pr_state']=='not_created':
        if owner: return {'state':'IMPLEMENTING'}
        require(s.get('writer_state')=='NOT_STARTED','writer state uncertain')
        return emit('IMPLEMENTATION_REQUIRED','IMPLEMENT',node['writer'])
    require(owner is not None,'PR has no graph writer ownership')
    if s['pr_state']=='merged':
        require(s.get('merge_actor_verified') is True and s.get('post_merge_verified') is True and
                re.fullmatch(r'[a-f0-9]{40}',s.get('merge_sha','')),'merge/post-merge evidence missing')
        receipt=s.get('post_merge_receipt',{})
        require(receipt.get('subject')==flow.subject(s) and receipt.get('merge_sha')==s['merge_sha'] and
                receipt.get('pr_pointer')==s.get('pr_pointer') and receipt.get('inputs')==s['inputs'] and
                receipt.get('verified') is True and flow.github_pointer(receipt.get('evidence_pointer')),
                'post-merge receipt does not bind actual merge/current task')
        return {'state':'DONE','merge_sha':s['merge_sha']}
    require(s['pr_state']=='open','PR is closed')
    paths=s.get('changed_paths')
    require(s.get('paths_complete') is True and isinstance(paths,list), 'complete diff path list missing')
    require(all(within(p,node['allowed_paths']) and not within(p,node['locked_paths']) for p in paths),
            'diff exceeds allowed/locked scope')
    failures=[]; pending=False
    for expected in node['required_checks']:
        matches=[c for c in s.get('checks',[]) if all(c.get(k)==expected[k]
                   for k in ('name','app_id','workflow_id'))]
        require(len(matches)<=1,'ambiguous CI')
        if not matches: pending=True; continue
        check=matches[0]
        require(check.get('head')==s['head'] and check.get('source_verified') is True,'stale/untrusted CI')
        if check.get('status')!='completed': pending=True; continue
        if check.get('conclusion')!='success': failures.append(check.get('evidence_pointer'))
    if failures: return fix('CI_FAIL',failures)
    if pending: return {'state':'VERIFICATION_PENDING'}
    require(s.get('writer_state')=='IDLE','writer must yield before independent review')
    authors=s['author_identities']; sessions=s['author_sessions']
    require(node['writer']['identity'] in authors and owner['session_id'] in sessions,'authorship missing')
    effective=flow.DEPTH[node['audit_floor']]
    review_sessions=set()
    for lane in node['reviewers']:
        require(lane.get('enabled') is True and lane.get('read_only_verified') is True,'review lane unqualified')
        require(lane['identity'] not in authors and flow.github_pointer(lane.get('approval_pointer')),
                'reviewer author conflict or missing designation')
        a=action(plan,node,s,'REVIEW',lane['identity'],lane['approval_pointer'],lane=lane['lane'])
        result=s.get('reviews',{}).get(lane['identity'])
        if result is None: return {'state':'REVIEW_REQUIRED','action':a}
        flow.verify_result(result,a,authors,sessions)
        if result['contract_change'] or result['result']=='DECISION_REQUIRED':
            return decision('review contract decision',[result['evidence_pointer']])
        if result['result']=='FAIL' or result['blockers']:
            return fix('REVIEW_FAIL',[result['evidence_pointer']])
        effective=max(effective,flow.DEPTH[result['required_depth']])
        require(flow.DEPTH[result['verified_depth']]>=min(effective,2),'review depth insufficient')
        require(result['session_id'] not in review_sessions,'reviewers reused one session')
        review_sessions.add(result['session_id'])
    gate=node['astra_gate']
    if effective==3 and 'ARCHITECTURE' not in gate:
        gate='ARCHITECTURE' if gate=='NONE' else gate+'+ARCHITECTURE'
    if gate!='NONE':
        lane=node['auditor']
        require(lane.get('enabled') is True and lane.get('read_only_verified') is True,'auditor unqualified')
        require(lane['identity'] not in authors and flow.github_pointer(lane.get('approval_pointer')),
                'auditor author conflict or missing designation')
        a=action(plan,node,s,'AUDIT',lane['identity'],lane['approval_pointer'],lane=lane['lane'],
                 gate=gate,scope_digest=digest([flow.subject(s),s['inputs'],gate,node.get('audit_scope_digest')]))
        result=s.get('audit')
        if result is None:return {'state':'AUDIT_REQUIRED','action':a}
        flow.verify_result(result,a,authors,sessions)
        require(result.get('gate')==a['gate'] and result.get('scope_digest')==a['scope_digest'],'audit scope stale')
        if result['contract_change'] or result['result']=='DECISION_REQUIRED':
            return decision('audit contract decision',[result['evidence_pointer']])
        if result['result']=='FAIL' or result['blockers']:
            return fix('AUDIT_FAIL',[result['evidence_pointer']])
        require(flow.DEPTH[result['verified_depth']]>=effective,'audit depth insufficient')
    return {'state':'MERGE_CANDIDATE','head':s['head'],'draft':s.get('draft'),
            'effective_depth':effective,'merge_authorized':False}


class GraphStore(flow.Store):
    def initialize_graph(self):
        """Explicit ledger upgrade, never triggered by a provider event."""
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS graph_owners (task TEXT PRIMARY KEY, request TEXT, session TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS graph_slots (request TEXT PRIMARY KEY, session TEXT, state TEXT)')

    def owners(self):
        with self.transaction() as db:
            return {r['task']:{'request_id':r['request'],'session_id':r['session'],
                    'writer':flow.decode(r['body'])['identity'],'lane':flow.decode(r['body'])['lane']}
                    for r in db.execute('SELECT g.*,o.body FROM graph_owners g JOIN outbox o ON o.id=g.request')}

    def reserve_in_transaction(self,db,a):
        task=flow.canonical([a['subject']['repository'],a['subject']['task_id']])
        old=db.execute('SELECT * FROM graph_slots WHERE request=?',(a['request_id'],)).fetchone()
        require(old is None,'prior execution reservation requires reconciliation')
        busy=list(db.execute("SELECT g.*,o.state AS delivery FROM graph_slots g JOIN outbox o ON o.id=g.request WHERE g.state!='TERMINAL'"))
        if a['kind']=='FEEDBACK':
            owner=db.execute('SELECT * FROM graph_owners WHERE task=?',(task,)).fetchone()
            require(owner is not None and owner['session']==a['owner_session_id'],'feedback owner mismatch')
            # Same confirmed owner may receive feedback while working; uncertainty
            # or any other writer/reviewer must retain the capacity fence.
            if any(r['state']!='ACTIVE' or r['delivery']!='CONFIRMED' or
                   r['session']!=owner['session'] for r in busy):return False
        elif busy:return False
        if a['kind']=='IMPLEMENT':
            require(db.execute('SELECT 1 FROM graph_owners WHERE task=?',(task,)).fetchone() is None,
                    'task already has a writer')
            db.execute('INSERT INTO graph_owners VALUES (?,?,NULL)',(task,a['request_id']))
        db.execute("INSERT INTO graph_slots VALUES (?,NULL,'RESERVED')",(a['request_id'],))
        return True

    def send_execution(self,a,send,is_current):
        self.reserve(a)
        if not is_current(a):return {'state':'STALE','sent':False}
        with self.transaction() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(a['request_id'],)).fetchone()
            if row['state']!='NOT_STARTED' or not row['valid']:
                return {'state':row['state'] if row['valid'] else 'STALE','sent':False}
            if not self.reserve_in_transaction(db,a):
                return {'state':'WAITING_CAPACITY','sent':False}
            # Capacity reservation and potentially-sent boundary are one commit.
            db.execute("UPDATE outbox SET state='SUBMITTING' WHERE id=?",(a['request_id'],))
        try:
            receipt=send(a)
            require(isinstance(receipt,dict) and receipt.get('request_id')==a['request_id'] and
                    receipt.get('accepted') is True,'ambiguous delivery receipt')
            self.confirm_execution(a,receipt)
            state='CONFIRMED'
        except Exception:
            state,receipt='UNKNOWN',{'reason':'reconcile existing request; never resend'}
        with self.transaction() as db:
            db.execute("UPDATE outbox SET state=?,receipt=? WHERE id=? AND state='SUBMITTING'",
                       (state,flow.canonical(receipt),a['request_id']))
        return {'state':state,'sent':True,'receipt':receipt}

    def verify_observed_results(self,s):
        results=list(s.get('reviews',{}).values())
        if s.get('audit'):results.append(s['audit'])
        with self.transaction() as db:
            for r in results:
                row=db.execute('SELECT * FROM outbox WHERE id=?',(r.get('request_id'),)).fetchone()
                slot=db.execute('SELECT * FROM graph_slots WHERE request=?',(r.get('request_id'),)).fetchone()
                require(row is not None and row['valid']==1 and row['state']=='CONFIRMED' and
                        slot is not None and slot['state']=='TERMINAL','review delivery/termination unconfirmed')
                a=flow.decode(row['body']);receipt=flow.decode(row['receipt'])
                require(a['kind'] in ('REVIEW','AUDIT') and a['identity']==r.get('identity') and
                        a['subject']==r.get('subject') and a['read_only'] is True and
                        slot['session']==receipt.get('session_id')==r.get('session_id')==r.get('confirmed_session_id'),
                        'review does not match actual delivered session')

    def confirm_execution(self,a,receipt):
        session=receipt.get('session_id')
        require(isinstance(session,str) and session,'confirmed provider session missing')
        if a['kind']=='FEEDBACK': require(session==a['owner_session_id'],'feedback opened another session')
        with self.transaction() as db:
            db.execute("UPDATE graph_slots SET session=?,state='ACTIVE' WHERE request=?",(session,a['request_id']))
            if a['kind']=='IMPLEMENT':
                db.execute('UPDATE graph_owners SET session=? WHERE request=?',(session,a['request_id']))

    def terminal(self,request_id,session):
        """Caller must verify terminal evidence from the protected observation collector."""
        with self.transaction() as db:
            row=db.execute('SELECT g.*,o.state AS delivery FROM graph_slots g JOIN outbox o ON o.id=g.request WHERE request=?',(request_id,)).fetchone()
            require(row is not None and row['delivery']=='CONFIRMED' and row['session']==session and row['state'] in ('ACTIVE','TERMINAL'),
                    'terminal does not reconcile an ambiguous launch')
            db.execute("UPDATE graph_slots SET state='TERMINAL' WHERE request=?",(request_id,))


class GraphRunner:
    def __init__(self,store,ports):self.store,self.ports=store,ports

    def assess(self):
        plan=self.ports.plan(); nodes=validate_plan(plan); owners=self.store.owners()
        states={}; snapshots={}
        def run(nid):
            if nid in states:return states[nid]
            n=nodes[nid]; inputs={}
            for d in n['dependencies']:
                upstream=run(d['node'])
                if upstream['state']!='DONE':
                    states[nid]={'state':'WAITING_DEPENDENCY'};return states[nid]
                inputs[d['input']]=upstream['merge_sha']
            try:
                s=bind(plan,n,self.ports.load(n,plan),inputs);snapshots[nid]=s
                self.store.verify_observed_results(s)
                key=flow.canonical([n['repository'],n['task_id']])
                owner=owners.get(key)
                if owner:
                    require(owner['writer']==n['writer']['identity'] and owner['lane']==n['writer']['lane'],
                            'writer designation changed; reconcile explicit handoff')
                states[nid]=evaluate_node(plan,n,s,owner)
            except (flow.FlowError,KeyError,TypeError,ValueError) as exc:
                states[nid]={'state':'BLOCKED','reason':str(exc)}
            return states[nid]
        for nid in nodes:run(nid)
        return plan,states,snapshots

    def current(self,a):
        plan,states,_=self.assess()
        candidate=states.get(a['node_id'],{}).get('action')
        return candidate is not None and candidate['request_id']==a['request_id']

    def advance(self,event_id):
        initial=self.ports.plan()
        self.store.accept(event_id,{'operation':'graph-advance','graph_digest':digest(initial)})
        if self.store.begin_event(event_id) is None:return {'state':'EVENT_REUSED'}
        try:
            # Observations are authenticated mechanical records, not model assertions.
            for item in self.ports.terminals():
                self.store.terminal(item['request_id'],item['session_id'])
            plan,states,_=self.assess()
            for node in plan['nodes']:
                a=states[node['id']].get('action')
                if not a:continue
                # Already delivered actions are not relaunched and do not starve other nodes.
                record=self.store.reserve(a)
                if record['state']!='NOT_STARTED':continue
                projection=dict(a,kind='PROJECTION',action_kind=a['kind'],
                                action_request_id=a['request_id'],request_id=digest(['projection',a['request_id']]))
                p=self.store.send_once(projection,self.ports.project,
                                      lambda _:self.current(a))
                if p['state']!='CONFIRMED':
                    self.store.end_event(event_id,False)
                    return {'state':'PROJECTION_'+p['state'],'nodes':states}
                result=self.store.send_execution(a,self.ports.route,self.current)
                self.store.end_event(event_id,result['state']=='CONFIRMED')
                return {'state':states[node['id']]['state'],'delivery':result['state'],'nodes':states}
            for node in plan['nodes']:
                state=states[node['id']]
                if state['state'] not in ('MERGE_CANDIDATE','DONE','BLOCKED'):continue
                status={'kind':'GRAPH_STATUS','subject':{'repository':node['repository'],'task_id':node['task_id']},
                        'node_id':node['id'],'graph_digest':digest(plan),'status':state,'merge_authorized':False}
                status['request_id']=digest(status)
                row=self.store.reserve(status)
                if row['state']!='NOT_STARTED':continue
                def fresh_status(_):
                    current,now,_=self.assess()
                    return digest(current)==digest(plan) and now.get(node['id'])==state
                delivered=self.store.send_once(status,self.ports.project,fresh_status)
                self.store.end_event(event_id,delivered['state']=='CONFIRMED')
                return {'state':'STATUS_PROJECTED','delivery':delivered['state'],'nodes':states,'merge_authorized':False}
            self.store.end_event(event_id,True)
            return {'state':'QUIET','nodes':states,'merge_authorized':False}
        except Exception:
            self.store.end_event(event_id,False);raise
