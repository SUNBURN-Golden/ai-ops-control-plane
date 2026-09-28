import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import control_plane_graph as g
import control_plane_graph_cli as cli

REPO='BeautifulMind-JT/ai-ops-control-plane'
POINTER=f'https://github.com/{REPO}/issues/25'


def fixture():
    lane=lambda who:dict(identity=who,lane=who,approval_pointer=POINTER,enabled=True,read_only_verified=True)
    node=dict(id='unit',repository=REPO,issue=25,task_id='GRAPH-TEST',revision='1',
        task_pointer=POINTER,acceptance_pointer=POINTER,base='a'*40,issue_body_sha256='d'*64,
        allowed_paths=['src/','tests/'],locked_paths=['src/locked.py'],dependencies=[],
        writer=dict(identity='devin',lane='DEVIN'),reviewers=[lane('cursor-grok'),lane('zcode-glm')],
        astra=lane('astra'),auditor=lane('auditor'),audit_floor='A1',astra_gate='NONE',
        required_checks=[dict(name='ci',workflow_id=1,app_id=2,events=['pull_request'],
            path='.github/workflows/ci.yml',workflow_blob='e'*40)])
    plan=dict(schema_version=1,graph_id='graph',revision='1',approval_pointer=POINTER,
              max_active_sessions=1,nodes=[node])
    s=dict(repository=REPO,task_id=node['task_id'],revision='1',head='a'*40,base='a'*40,
        task_pointer=POINTER,task_digest='d'*64,policy_revision=g.digest(plan),plan_digest=g.digest(plan),
        task_open=True,inputs={},ownership_verified=True,prerequisites_verified=True,blockers=[],
        contract_change_required=False,writer_state='NOT_STARTED',owner_session_id=None,
        pr_state='not_created',draft=True,checks=[],required_checks=node['required_checks'],
        audit_floor='A1',astra_gate='NONE',reviews={},audit=None,author_identities=['devin'],
        author_sessions=[],changed_paths=['src/code.py'],paths_complete=True)
    return plan,s


class Ports:
    def __init__(self,plan,s):
        self.p=plan;self.snapshots={'unit':s};self.sent=[];self.projected=[];self.finished=[]
    def plan(self):return copy.deepcopy(self.p)
    def load(self,node,plan):return copy.deepcopy(self.snapshots[node['id']])
    def terminals(self):return copy.deepcopy(self.finished)
    def project(self,a):
        self.projected.append(copy.deepcopy(a))
        return dict(request_id=a['request_id'],accepted=True,pointer=POINTER)
    def route(self,a):
        self.sent.append(copy.deepcopy(a))
        session=a.get('owner_session_id','provider-'+a['request_id'][:12])
        return dict(request_id=a['request_id'],accepted=True,session_id=session)
    def terminal(self,a):
        self.finished.append(dict(request_id=a['request_id'],
            session_id=a.get('owner_session_id','provider-'+a['request_id'][:12]),
            terminal_verified=True,evidence_pointer=POINTER))


def result(a,verdict='PASS',depth='A1',change=False):
    return dict(subject=a['subject'],request_id=a['request_id'],attempt_id=a['attempt_id'],
        identity=a['identity'],designation=a['designation'],authenticated_actor=a['identity'],
        session_id='provider-'+a['request_id'][:12],confirmed_session_id='provider-'+a['request_id'][:12],
        read_only_verified=True,evidence_pointer=POINTER,result=verdict,required_depth=depth,
        verified_depth=depth,contract_change=change,blockers=[],gate=a.get('gate'),scope_digest=a.get('scope_digest'))


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=g.GraphStore(Path(self.tmp.name)/'flow.db',initialize=True);self.store.initialize_graph()
        self.plan,self.s=fixture();self.ports=Ports(self.plan,self.s);self.runner=g.GraphRunner(self.store,self.ports)
        self.i=0
    def advance(self):
        self.i+=1;return self.runner.advance('event-'+str(self.i))
    def start(self):
        self.assertEqual(self.advance()['delivery'],'CONFIRMED');a=self.ports.sent[-1]
        self.s['owner_session_id']='provider-'+a['request_id'][:12]
        self.s['author_sessions']=[self.s['owner_session_id']];self.s['writer_state']='ACTIVE'
        return a
    def pr(self,head='b'*40):
        self.s.update(head=head,pr_state='open',writer_state='IDLE',pr_pointer=f'https://github.com/{REPO}/pull/26')
        self.s['checks']=[dict(name='ci',workflow_id=1,app_id=2,head=head,status='completed',
                              conclusion='success',source_verified=True,evidence_pointer=POINTER)]
    def review_pass(self,depth='A1'):
        a=self.ports.sent[-1];self.s['reviews'][a['identity']]=result(a,depth=depth);self.ports.terminal(a)

    def test_complete_loop_same_owner_fix_new_sha_two_blind_reviews(self):
        writer=self.start();self.ports.terminal(writer);self.pr()
        self.s['checks'][0]['conclusion']='failure'
        r=self.advance();self.assertEqual(r['state'],'CORRECTION_REQUIRED')
        feedback=self.ports.sent[-1]
        self.assertEqual(feedback['owner_session_id'],self.s['owner_session_id'])
        self.assertEqual(feedback['evidence'],[POINTER]);self.assertEqual(feedback['allowed_paths'],['src/','tests/'])
        self.ports.terminal(feedback);self.pr('c'*40)
        self.assertEqual(self.advance()['state'],'REVIEW_REQUIRED');self.review_pass()
        self.assertEqual(self.advance()['state'],'REVIEW_REQUIRED')
        self.assertEqual(self.ports.sent[-1]['identity'],'zcode-glm')
        self.assertNotIn('reviews',self.ports.sent[-1]);self.review_pass()
        r=self.advance();self.assertEqual(r['nodes']['unit']['state'],'MERGE_CANDIDATE')
        self.assertFalse(r['merge_authorized']);self.assertTrue(self.s['draft'])
        self.assertEqual(sum(a['kind']=='IMPLEMENT' for a in self.ports.sent),1)

    def test_duplicate_event_and_new_event_do_not_relaunch_writer(self):
        self.start();self.assertEqual(self.runner.advance('event-1')['state'],'EVENT_REUSED')
        self.advance();self.assertEqual(len(self.ports.sent),1)

    def test_concurrent_events_only_one_writer(self):
        def once(i):
            try:return self.runner.advance('concurrent-'+str(i))
            except g.flow.FlowError:return {'state':'BLOCKED'}
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(once,range(2)))
        self.assertEqual(sum(a['kind']=='IMPLEMENT' for a in self.ports.sent),1)

    def test_response_loss_and_restart_never_launch_again(self):
        def lost(a):self.ports.sent.append(a);raise TimeoutError()
        self.ports.route=lost
        self.assertEqual(self.advance()['delivery'],'UNKNOWN')
        self.runner=g.GraphRunner(g.GraphStore(self.store.path),self.ports)
        self.advance();self.assertEqual(len(self.ports.sent),1)

    def test_projection_loss_prevents_provider_call_and_resend(self):
        def lost(a):self.ports.projected.append(a);raise TimeoutError()
        self.ports.project=lost
        self.assertEqual(self.advance()['state'],'PROJECTION_UNKNOWN')
        self.advance();self.assertEqual(len(self.ports.projected),1);self.assertEqual(self.ports.sent,[])

    def test_ci_matrix_pending_and_stale_sha_never_review(self):
        w=self.start();self.ports.terminal(w);self.pr();self.s['checks'][0]['status']='in_progress'
        self.assertEqual(self.advance()['nodes']['unit']['state'],'VERIFICATION_PENDING')
        self.s['checks'][0].update(status='completed',head='d'*40)
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')
        self.assertEqual(len(self.ports.sent),1)

    def test_review_fail_returns_only_same_writer_and_dedupes(self):
        w=self.start();self.ports.terminal(w);self.pr();self.advance();a=self.ports.sent[-1]
        self.s['reviews'][a['identity']]=result(a,'FAIL');self.ports.terminal(a)
        self.assertEqual(self.advance()['state'],'CORRECTION_REQUIRED')
        f=self.ports.sent[-1];self.assertEqual(f['reason'],'REVIEW_FAIL')
        self.assertEqual(f['owner_session_id'],self.s['owner_session_id'])
        self.advance();self.assertEqual(len(self.ports.sent),3)

    def test_a3_escalates_and_audit_fail_returns_owner(self):
        w=self.start();self.ports.terminal(w);self.pr();self.advance();self.review_pass('A3')
        self.advance();self.review_pass('A3')
        self.assertEqual(self.advance()['state'],'AUDIT_REQUIRED');a=self.ports.sent[-1]
        self.assertEqual(a['gate'],'ARCHITECTURE')
        self.s['audit']=result(a,'FAIL',depth='A3');self.ports.terminal(a)
        self.assertEqual(self.advance()['state'],'CORRECTION_REQUIRED')
        self.assertEqual(self.ports.sent[-1]['reason'],'AUDIT_FAIL')

    def test_old_review_on_new_head_cannot_authorize_candidate(self):
        w=self.start();self.ports.terminal(w);self.pr();self.advance();self.review_pass()
        self.pr('c'*40)
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')

    def test_actual_author_session_cannot_review(self):
        w=self.start();self.ports.terminal(w);self.pr();self.advance();a=self.ports.sent[-1]
        r=result(a);r['session_id']=r['confirmed_session_id']=self.s['owner_session_id']
        self.s['reviews'][a['identity']]=r
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')

    def test_contract_change_is_decision_not_implementation(self):
        self.s.update(contract_change_required=True,decision_pointers=[POINTER])
        self.assertEqual(self.advance()['state'],'DECISION_REQUIRED')
        self.assertEqual(self.ports.sent[-1]['kind'],'DECISION')
        self.assertEqual(self.store.owners(),{})

    def test_scope_and_explicit_stall_block(self):
        w=self.start();self.ports.terminal(w);self.pr();self.s['changed_paths']=['src/locked.py']
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')
        self.s['changed_paths']=['src/code.py'];self.s['writer_state']='STALLED'
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')

    def test_no_arbitrary_three_failure_cutoff(self):
        w=self.start();self.ports.terminal(w)
        for c in 'bcdef':
            self.pr(c*40);self.s['checks'][0]['conclusion']='failure'
            self.assertEqual(self.advance()['state'],'CORRECTION_REQUIRED')
            self.ports.terminal(self.ports.sent[-1])
        self.assertEqual(sum(a['kind']=='FEEDBACK' for a in self.ports.sent),5)

    def test_no_existing_task_adoption_or_uncertain_terminal(self):
        self.s['owner_session_id']='old-host-session'
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')
        self.assertEqual(self.ports.sent,[])
        with self.assertRaises(g.flow.FlowError):self.store.terminal('unknown','invented')

    def test_graph_rejects_cycle_and_unnamed_edges(self):
        for deps in ([dict(node='unit',input='x')],[dict(node='missing',input='x')],[dict(node='unit')]):
            p=copy.deepcopy(self.plan);p['nodes'][0]['dependencies']=deps
            with self.assertRaises(g.flow.FlowError):g.validate_plan(p)

    def test_dependency_waits_for_actual_merge_and_postmerge(self):
        w=self.start();self.ports.terminal(w);self.pr()
        n=copy.deepcopy(self.plan['nodes'][0]);n.update(id='consumer',task_id='CONSUMER',issue=26,
            task_pointer=f'https://github.com/{REPO}/issues/26',dependencies=[dict(node='unit',input='library')])
        self.plan['nodes'].append(n)
        self.s['plan_digest']=g.digest(self.plan)
        t=copy.deepcopy(self.s);t.update(task_id=n['task_id'],task_pointer=n['task_pointer'],owner_session_id=None,
             pr_state='not_created',head='a'*40,writer_state='NOT_STARTED',author_sessions=[],inputs={})
        self.ports.snapshots['consumer']=t
        self.assertEqual(self.runner.assess()[1]['consumer']['state'],'WAITING_DEPENDENCY')
        self.s.update(pr_state='merged',merge_sha='f'*40,merge_actor_verified=True,post_merge_verified=False)
        self.assertEqual(self.runner.assess()[1]['consumer']['state'],'WAITING_DEPENDENCY')
        self.s['post_merge_verified']=True;self.s['task_open']=False;t['inputs']={'library':'f'*40}
        self.s['policy_revision']=g.digest(self.plan)
        self.s['post_merge_receipt']=dict(subject=g.flow.subject(self.s),merge_sha='f'*40,
            pr_pointer=self.s['pr_pointer'],inputs={},verified=True,evidence_pointer=POINTER)
        self.assertEqual(self.runner.assess()[1]['consumer']['state'],'IMPLEMENTATION_REQUIRED')
        self.assertEqual(self.runner.assess()[1]['unit']['state'],'DONE')

    def test_active_session_blocks_second_node(self):
        self.start()
        a=copy.deepcopy(self.ports.sent[-1]);a['subject']['task_id']='OTHER';a['request_id']='f'*64
        self.assertEqual(self.store.send_execution(a,self.ports.route,lambda _:True)['state'],'WAITING_CAPACITY')

    def test_capacity_wait_keeps_unsent_action_and_resumes_after_terminal(self):
        w=self.start()
        a=copy.deepcopy(w);a['subject']['task_id']='OTHER';a['request_id']='f'*64
        sent=len(self.ports.sent)
        self.assertEqual(self.store.send_execution(a,self.ports.route,lambda _:True)['state'],'WAITING_CAPACITY')
        self.assertEqual(self.store.reserve(a)['state'],'NOT_STARTED');self.assertEqual(len(self.ports.sent),sent)
        self.store.terminal(w['request_id'],self.s['owner_session_id'])
        self.assertEqual(self.store.send_execution(a,self.ports.route,lambda _:True)['state'],'CONFIRMED')

    def test_feedback_cannot_wake_writer_during_review_or_unknown(self):
        w=self.start();self.ports.terminal(w);self.pr();self.advance();review=self.ports.sent[-1]
        self.s['checks'][0]['conclusion']='failure'
        r=self.advance();self.assertEqual(r['delivery'],'WAITING_CAPACITY')
        self.assertEqual(self.ports.sent[-1],review)
        self.ports.terminal(review)
        self.assertEqual(self.advance()['delivery'],'CONFIRMED')
        feedback=self.ports.sent[-1];self.assertEqual(feedback['kind'],'FEEDBACK')
        self.assertEqual(feedback['owner_session_id'],self.s['owner_session_id'])
        # A new feedback pointer cannot bypass an unresolved earlier delivery.
        with self.store.transaction() as db:
            db.execute("UPDATE graph_slots SET state='RESERVED',session=NULL WHERE request=?",(feedback['request_id'],))
        a=copy.deepcopy(feedback);a['request_id']='0'*64
        self.assertEqual(self.store.send_execution(a,self.ports.route,lambda _:True)['state'],'WAITING_CAPACITY')

    def test_coauthor_review_and_audit_are_rejected_before_dispatch(self):
        w=self.start();self.ports.terminal(w);self.pr();self.s['author_identities'].append('cursor-grok')
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED');self.assertEqual(len(self.ports.sent),1)
        self.s['author_identities'].remove('cursor-grok');self.advance();self.review_pass('A3')
        self.advance();self.review_pass('A3');self.s['author_identities'].append('auditor')
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')
        self.assertFalse(any(a['kind']=='AUDIT' for a in self.ports.sent))

    def test_unlaunched_review_and_invented_receipt_cannot_pass(self):
        w=self.start();self.ports.terminal(w);self.pr()
        a=g.action(self.plan,self.plan['nodes'][0],self.s,'REVIEW','cursor-grok',POINTER,lane='cursor-grok')
        self.s['reviews']['cursor-grok']=result(a)
        self.assertEqual(self.advance()['nodes']['unit']['state'],'BLOCKED')
        self.assertEqual(len(self.ports.sent),1)

    def test_candidate_status_is_durable_once_and_not_merge_authorization(self):
        w=self.start();self.ports.terminal(w);self.pr();self.advance();self.review_pass()
        self.advance();self.review_pass();self.advance();self.advance()
        statuses=[a for a in self.ports.projected if a['kind']=='GRAPH_STATUS']
        self.assertEqual(len(statuses),1);self.assertEqual(statuses[0]['status']['head'],'b'*40)
        self.assertFalse(statuses[0]['merge_authorized'])


    def test_decision_waits_for_writer_capacity(self):
        w=self.start();self.s.update(contract_change_required=True,decision_pointers=[POINTER])
        self.assertEqual(self.advance()['delivery'],'WAITING_CAPACITY')
        self.assertEqual(len(self.ports.sent),1)
        self.ports.terminal(w)
        self.assertEqual(self.advance()['delivery'],'CONFIRMED')
        self.assertEqual(self.ports.sent[-1]['kind'],'DECISION')

    def test_stale_postmerge_receipt_does_not_release_dependency(self):
        w=self.start();self.ports.terminal(w);self.pr()
        self.s.update(pr_state='merged',merge_sha='f'*40,merge_actor_verified=True,post_merge_verified=True)
        self.s['post_merge_receipt']=dict(subject=g.flow.subject(self.s),merge_sha='c'*40,
            pr_pointer=self.s['pr_pointer'],inputs={},verified=True,evidence_pointer=POINTER)
        self.assertEqual(self.runner.assess()[1]['unit']['state'],'BLOCKED')
        self.s['post_merge_receipt']['merge_sha']='f'*40
        self.assertEqual(self.runner.assess()[1]['unit']['state'],'DONE')

    def test_release_requires_explicit_approved_scope_digest(self):
        self.plan['nodes'][0]['astra_gate']='RELEASE'
        with self.assertRaises(g.flow.FlowError):g.validate_plan(self.plan)
        self.plan['nodes'][0]['audit_scope_digest']='e'*64
        g.validate_plan(self.plan)



class PortTests(unittest.TestCase):
    def test_web_record_actor_and_digest_are_checked(self):
        plan,s=fixture();raw='<!-- ASTRA_GRAPH_PLAN_V1 -->\n'+g.flow.canonical(plan)
        class API:
            def call(self,*args):return dict(user=dict(login='intruder'),body=raw,html_url=POINTER,
                     issue_url=f'https://api.github.com/repos/{REPO}/issues/25')
        p=cli.GithubGraphPorts(API(),dict(user_actors=['User'],plan=dict(repository=REPO,
            issue=25,comment_id=1,actor='User',sha256=cli.hashlib.sha256(raw.encode()).hexdigest())))
        with self.assertRaises(cli.flow.FlowError):p.plan()

    def test_plain_later_changes_requested_revokes_structured_pass(self):
        plan,s=fixture();node=plan['nodes'][0];body='canonical task'
        node['issue_body_sha256']=cli.hashlib.sha256(body.encode()).hexdigest()
        s.update(plan_digest=g.digest(plan),pr_number=26,head='b'*40)
        s.update(task_digest=node['issue_body_sha256'],policy_revision=g.digest(plan))
        a=g.action(plan,node,s,'REVIEW','cursor-grok',POINTER,lane='cursor-grok')
        r=result(a);r['phase']='review'
        native=[dict(id=1,user=dict(login='cursor-grok'),commit_id='b'*40,state='APPROVED',
                    html_url=POINTER,body='<!-- ASTRA_GRAPH_RESULT_V1 -->\n'+g.flow.canonical(r)),
                dict(id=2,user=dict(login='cursor-grok'),commit_id='b'*40,state='CHANGES_REQUESTED',
                     html_url=POINTER,body='Approval withdrawn: regression found')]
        class API:
            def call(self,method,path):
                if path.endswith('issues/25'):return dict(body=body,state='open',html_url=POINTER)
                if path.endswith('pulls/26'):return dict(base=dict(repo=dict(full_name=REPO),sha='a'*40),
                    head=dict(sha='b'*40),state='open',draft=True,html_url=POINTER,user=dict(login='devin'),
                    changed_files=1,mergeable=True)
                raise AssertionError(path)
            def pages(self,path):
                if path.endswith('/files'):return [dict(filename='src/code.py')]
                if path.endswith('/reviews'):return native
                if 'actions/runs?' in path:return []
                raise AssertionError(path)
        ports=cli.GithubGraphPorts(API(),dict(user_actors=['User']))
        ports.observation=lambda _:copy.deepcopy(s)
        observed=ports.load(node,plan)
        self.assertEqual(observed['reviews'],{})
        self.assertIn('native_review_withdrawn:cursor-grok',observed['blockers'])
        # A bound canonical FAIL is exact correction evidence, not a generic blocker.
        failed=dict(r,result='FAIL')
        native[-1]['body']='<!-- ASTRA_GRAPH_RESULT_V1 -->\n'+g.flow.canonical(failed)
        observed=ports.load(node,plan)
        self.assertEqual(observed['reviews']['cursor-grok']['result'],'FAIL')
        self.assertNotIn('native_review_withdrawn:cursor-grok',observed['blockers'])

    def test_adapter_unqualified_or_wrong_digest_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'adapter';path.write_text('not executed');path.chmod(0o700)
            config=dict(executable=str(path),qualified=False,evidence_pointer=POINTER,operations=['REVIEW'],sha256='0'*64)
            ports=cli.GithubGraphPorts(None,dict(adapters={'lane':config}))
            with patch.object(cli.subprocess,'run') as run:
                for qualified in (False,True):
                    config['qualified']=qualified
                    with self.assertRaises(cli.flow.FlowError):ports.route(dict(lane='lane',kind='REVIEW'))
                run.assert_not_called()

    def test_credentials_are_not_forwarded_to_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'adapter';path.write_text('test');path.chmod(0o700)
            config=dict(executable=str(path),qualified=True,evidence_pointer=POINTER,operations=['REVIEW'],
                        sha256=cli.hashlib.sha256(path.read_bytes()).hexdigest())
            ports=cli.GithubGraphPorts(None,dict(adapters={'lane':config}))
            a=dict(lane='lane',kind='REVIEW',request_id='r')
            with patch.dict(os.environ,{'ASTRA_GRAPH_GITHUB_TOKEN':'secret'}),patch.object(cli.subprocess,'run') as run:
                run.return_value=cli.subprocess.CompletedProcess([],0,json.dumps(dict(request_id='r',accepted=True,session_id='s')))
                ports.route(a)
                self.assertNotIn('ASTRA_GRAPH_GITHUB_TOKEN',run.call_args.kwargs['env'])
                self.assertEqual(run.call_args.args[0],[str(path),'review'])


if __name__=='__main__':unittest.main()
