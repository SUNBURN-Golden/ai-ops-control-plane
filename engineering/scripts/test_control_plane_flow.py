"""Adversarial event-flow tests; no Slack/GitHub/provider calls or credentials."""
import copy
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import urlencode


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


f = load('control_plane_flow')
g = load('control_plane_flow_gateway')


def snapshot():
    return dict(repository='owner/repo', task_id='T1', revision='1', head='a'*40, base='b'*40,
                policy_revision='c'*64, task_digest='d'*64,
                task_pointer='https://github.com/owner/repo/issues/1',
                approval_pointer='https://github.com/owner/repo/issues/1#issuecomment-1',
                pr_pointer='https://github.com/owner/repo/pull/2', pr_number=2,
                pr_state='open', draft=False, prerequisites_verified=True,
                dependencies_verified=True, blockers=[], audit_floor='A1', astra_gate='NONE',
                required_checks=[dict(name='CI', app_id=1, workflow_id=2)],
                checks=[dict(name='CI', app_id=1, workflow_id=2, head='a'*40,
                             source_verified=True, status='completed', conclusion='success')],
                author_identities=['author'], author_sessions=['writer-session'],
                builder_id='DEVIN', builder_identity='author',
                reviewer=dict(identity='reviewer', enabled=True, read_only_verified=True,
                              approval_pointer='https://github.com/owner/repo/issues/1#issuecomment-3'),
                auditor=dict(identity='auditor', active=True, read_only_verified=True,
                             designation_pointer='https://github.com/owner/repo/issues/1#issuecomment-4'),
                review=None, audit=None)


def passing(action, depth='A1'):
    return dict(action, authenticated_actor=action['identity'], session_id='independent',
                confirmed_session_id='independent', read_only_verified=True,
                evidence_pointer='https://github.com/owner/repo/pull/2#pullrequestreview-1',
                result='PASS', required_depth=depth, verified_depth='A3',
                contract_change=False, blockers=[])


class GateTests(unittest.TestCase):
    def test_a1_no_astra_and_never_merge_authorization(self):
        s = snapshot()
        r = f.assess(s)
        self.assertEqual(r['state'], 'REVIEW_REQUIRED')
        s['review'] = passing(r['action'])
        r = f.assess(s)
        self.assertEqual(r['state'], 'READY_FOR_MERGE')
        self.assertFalse(r['merge_authorized'])

    def test_reviewer_a3_forces_architecture_even_without_contract_change(self):
        s = snapshot()
        s['review'] = passing(f.assess(s)['action'], 'A3')
        r = f.assess(s)
        self.assertEqual((r['state'], r['action']['gate']), ('AUDIT_REQUIRED', 'ARCHITECTURE'))
        s['audit'] = passing(r['action'], 'A3')
        self.assertEqual(f.assess(s)['state'], 'READY_FOR_MERGE')

    def test_a3_does_not_erase_release_scope(self):
        s = snapshot(); s['astra_gate'] = 'RELEASE'
        s['review'] = passing(f.assess(s)['action'], 'A3')
        self.assertEqual(f.assess(s)['state'], 'BLOCKED')
        s['audit_scope_digest'] = 'e'*64
        r = f.assess(s)
        self.assertEqual(r['action']['gate'], 'RELEASE+ARCHITECTURE')

    def test_no_vacuous_or_untrusted_ci(self):
        for mutate in (lambda s: s.update(required_checks=[]),
                       lambda s: s['checks'].clear(),
                       lambda s: s['checks'][0].update(head='f'*40),
                       lambda s: s['checks'][0].update(app_id=999),
                       lambda s: s['checks'][0].update(source_verified=False),
                       lambda s: s['checks'][0].update(conclusion='skipped'),
                       lambda s: s['checks'].append(dict(s['checks'][0]))):
            with self.subTest(mutate=mutate):
                s=snapshot(); mutate(s)
                self.assertEqual(f.assess(s)['state'], 'BLOCKED')

    def test_author_and_unqualified_reviewer_rejected(self):
        for values in ({'identity':'author'}, {'read_only_verified':False}, {'enabled':False}):
            s=snapshot(); s['reviewer'].update(values)
            self.assertEqual(f.assess(s)['state'], 'BLOCKED')

    def test_exact_result_identity_fields(self):
        for key, value in (('subject', {}), ('request_id', 'wrong'), ('attempt_id',2),
                           ('identity','author'), ('designation','changed'),
                           ('authenticated_actor','forged'), ('session_id','unbound'),
                           ('read_only_verified',False)):
            with self.subTest(key=key):
                s=snapshot(); s['review']=passing(f.assess(s)['action']); s['review'][key]=value
                self.assertEqual(f.assess(s)['state'], 'BLOCKED')

    def test_revoked_auditor_old_pass_not_reusable(self):
        s=snapshot(); s['review']=passing(f.assess(s)['action'],'A3')
        s['audit']=passing(f.assess(s)['action'],'A3')
        s['auditor']['active']=False
        self.assertEqual(f.assess(s)['state'],'BLOCKED')
        s['auditor']['active']=True; s['auditor']['designation_pointer']+='-replacement'
        self.assertEqual(f.assess(s)['state'],'BLOCKED')

    def test_contract_change_sends_decision_not_ready(self):
        s=snapshot(); s['review']=passing(f.assess(s)['action'])
        s['review']['contract_change']=True
        self.assertEqual(f.assess(s)['action']['kind'],'DECISION')

    def test_draft_can_receive_review_but_not_be_ready(self):
        s=snapshot(); s['draft']=True
        s['review']=passing(f.assess(s)['action'])
        self.assertEqual(f.assess(s)['state'],'BLOCKED')

    def test_head_base_policy_change_invalidates_results(self):
        for key in ('head','base','policy_revision','task_digest'):
            s=snapshot(); s['review']=passing(f.assess(s)['action']); s[key]='f'*40
            self.assertEqual(f.assess(s)['state'],'BLOCKED')

    def test_route_is_fixed_not_cheapest_available_fallback(self):
        lane=snapshot()['reviewer']
        self.assertEqual(f.assign_reviewer('DEVIN', {'DEVIN':'reviewer'}, {'reviewer':lane}, ['author']),lane)
        with self.assertRaises(f.FlowError):
            f.assign_reviewer('GLM', {'DEVIN':'reviewer'}, {'reviewer':lane}, ['author'])

    def test_incomplete_grok_glm_evidence_never_qualifies(self):
        for builder in ('GROK_BUILD','GLM'):
            report=dict(builder_id=builder, runtime_sha='a'*40, wrapper_sha256='b'*64,
                        binary_sha256='c'*64, harness='candidate', model='pinned', cli_version='1',
                        host_id='staging', execution_mode='PERSISTENT_SUPERVISOR',
                        evidence_pointer='https://github.com/owner/repo/issues/1',
                        checks={x:'PASS' for x in ('cli','authentication','credential_isolation',
                               'durable_session','duplicate_unknown','trusted_workflow_boundary','quota_policy')})
            approval=dict(active=True, pointer=report['evidence_pointer'], report_digest=f.digest(report))
            self.assertFalse(f.qualify_lane(report,approval,'a'*40)['production_enabled'])
            report['checks']['trusted_workflow_boundary']='NOT_TESTED'
            approval['report_digest']=f.digest(report)
            with self.assertRaises(f.FlowError): f.qualify_lane(report,approval,'a'*40)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.secret=b'signing-secret-123456789'
        self.policy=dict(team_ids=['T1'], app_ids=['A1'], user_ids=['U1'], channel_ids=['C1'],
                         projects={'KIX':'owner/repo'})
        self.fields=dict(team_id='T1',api_app_id='A1',user_id='U1',channel_id='C1',command='/astra',
                         text='refresh KIX 1',trigger_id='trigger')

    def sign(self, fields=None):
        raw=urlencode(fields or self.fields).encode(); ts='1000'
        signature='v0='+hmac.new(self.secret,b'v0:'+ts.encode()+b':'+raw,hashlib.sha256).hexdigest()
        return raw, {'X-Slack-Request-Timestamp':ts,'X-Slack-Signature':signature}

    def test_verified_minimal_command_and_retry_identity(self):
        raw,headers=self.sign()
        first=f.verify_slack(raw,headers,self.secret,self.policy,1000)
        headers['X-Slack-Retry-Num']='1'
        self.assertEqual(first,f.verify_slack(raw,headers,self.secret,self.policy,1000))
        self.assertNotIn('response_url',first[1])

    def test_stale_tampered_scope_and_dangerous_commands(self):
        raw,headers=self.sign()
        for value,now in ((raw+b'x',1000),(raw,1400)):
            with self.assertRaises(f.FlowError): f.verify_slack(value,headers,self.secret,self.policy,now)
        for key,value in (('user_id','U2'),('team_id','T2'),('api_app_id','A2'),('channel_id','C2'),
                          ('text','merge KIX 1'),('text','dispatch KIX 1;cat /etc/passwd'),('text','PASS KIX 1')):
            fields=dict(self.fields); fields[key]=value; raw,headers=self.sign(fields)
            with self.assertRaises(f.FlowError): f.verify_slack(raw,headers,self.secret,self.policy,1000)

    def test_github_signed_payload_only_refreshes(self):
        raw=json.dumps({'repository':{'full_name':'owner/repo'},'state':'success'}).encode()
        headers={'x-github-delivery':'delivery-12345','x-github-event':'status',
                 'x-hub-signature-256':'sha256='+hmac.new(self.secret,raw,hashlib.sha256).hexdigest()}
        _,command=f.verify_github(raw,headers,self.secret,['owner/repo'])
        self.assertEqual(command['operation'],'refresh_repository')
        self.assertNotIn('state',command)

    def test_short_or_missing_slack_secret_rejected(self):
        raw,headers=self.sign()
        for secret in (b'',b'too-short','not-bytes',None):
            with self.subTest(secret=secret):
                with self.assertRaises(f.FlowError):
                    f.verify_slack(raw,headers,secret,self.policy,1000)

    def test_signed_but_empty_or_overfull_body_rejected(self):
        # A validly signed empty command still fails field validation.
        raw=b''; ts='1000'
        sig='v0='+hmac.new(self.secret,b'v0:'+ts.encode()+b':'+raw,hashlib.sha256).hexdigest()
        with self.assertRaises(f.FlowError):
            f.verify_slack(raw,{'X-Slack-Request-Timestamp':ts,'X-Slack-Signature':sig},
                           self.secret,self.policy,1000)
        fields=dict(self.fields,**{f'f{i}':'1' for i in range(40)})
        raw,headers=self.sign(fields)
        with self.assertRaises(f.FlowError):
            f.verify_slack(raw,headers,self.secret,self.policy,1000)
        fields=dict(self.fields); del fields['trigger_id']
        raw,headers=self.sign(fields)
        with self.assertRaises(f.FlowError):
            f.verify_slack(raw,headers,self.secret,self.policy,1000)

    def test_github_intake_faults_fail_closed(self):
        body=json.dumps({'repository':{'full_name':'owner/repo'}}).encode()
        def signed(raw=body,event='status',delivery='delivery-12345'):
            return raw,{'x-github-delivery':delivery,'x-github-event':event,
                'x-hub-signature-256':'sha256='+hmac.new(self.secret,raw,hashlib.sha256).hexdigest()}
        cases=[signed(event='push'),signed(delivery='bad id!'),signed(delivery='short'),
               signed(raw=json.dumps({'repository':{'full_name':'other/repo'}}).encode()),
               signed(raw=b'['),signed(raw=json.dumps(['not-dict']).encode())]
        for raw,headers in cases:
            with self.assertRaises(f.FlowError):
                f.verify_github(raw,headers,self.secret,['owner/repo'])
        raw,headers=signed(); headers['x-hub-signature-256']='sha256='+'0'*64
        with self.assertRaises(f.FlowError):
            f.verify_github(raw,headers,self.secret,['owner/repo'])
        raw,headers=signed(); headers.pop('x-hub-signature-256')
        with self.assertRaises(f.FlowError):
            f.verify_github(raw,headers,self.secret,['owner/repo'])
        raw,headers=signed(); headers.pop('x-github-delivery')
        with self.assertRaises(f.FlowError):
            f.verify_github(raw,headers,self.secret,['owner/repo'])
        with self.assertRaises(f.FlowError):
            f.verify_github(raw,headers,b'short',['owner/repo'])
        with self.assertRaises(f.FlowError):
            f.verify_github(b' '*1048577,{},self.secret,['owner/repo'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=f.Store(Path(self.temp.name)/'flow.sqlite',initialize=True)
        self.action=f.request(snapshot(),'AUDIT','auditor','https://github.com/owner/repo/issues/1')

    def send(self,a): return dict(accepted=True,request_id=a['request_id'])

    def state(self):
        with self.store.transaction() as db:
            return dict(db.execute('SELECT * FROM outbox').fetchone())

    def test_not_started_reuses_pending_action_once(self):
        self.store.reserve(self.action)
        self.store.reserve(self.action)
        send=Mock(side_effect=self.send)
        self.assertEqual(self.store.send_once(self.action,send,lambda _:True)['state'],'CONFIRMED')
        self.store.send_once(self.action,send,lambda _:True)
        self.assertEqual(send.call_count,1)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_concurrent_event_delivery_only_one_external_effect(self):
        barrier=threading.Barrier(6); calls=[]
        def run(_):
            barrier.wait()
            return self.store.send_once(self.action,lambda a:(calls.append(a) or self.send(a)),lambda _:True)
        with ThreadPoolExecutor(max_workers=6) as pool: list(pool.map(run,range(6)))
        self.assertEqual(len(calls),1)

    def test_response_loss_unknown_no_retry_or_new_scope_delivery(self):
        send=Mock(side_effect=TimeoutError)
        self.store.send_once(self.action,send,lambda _:True)
        self.store.send_once(self.action,send,lambda _:True)
        self.assertEqual((self.state()['state'],send.call_count),('UNKNOWN',1))
        changed=dict(self.action,request_id='new',designation='changed')
        with self.assertRaises(f.FlowError): self.store.reserve(changed)

    def test_crash_after_claim_stays_submitting(self):
        def crash(_): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.store.send_once(self.action,crash,lambda _:True)
        self.assertEqual(self.state()['state'],'SUBMITTING')
        send=Mock(); self.store.send_once(self.action,send,lambda _:True); send.assert_not_called()

    def test_invalidation_fences_pending_action(self):
        self.store.reserve(self.action); self.store.invalidate(self.action['request_id'])
        send=Mock()
        self.assertEqual(self.store.send_once(self.action,send,lambda _:True)['state'],'STALE')
        send.assert_not_called()

    def test_stale_authority_invalidates_and_never_revives(self):
        send=Mock(side_effect=self.send)
        self.assertEqual(self.store.send_once(self.action,send,lambda a:False),
                         {'state':'STALE','sent':False})
        send.assert_not_called()
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT valid FROM outbox').fetchone()[0],0)
        self.assertEqual(self.store.send_once(self.action,send,lambda a:True)['state'],'STALE')
        send.assert_not_called()

    def test_request_id_cannot_be_reused_with_different_body(self):
        self.store.reserve(self.action)
        with self.assertRaises(f.FlowError):
            self.store.reserve(dict(self.action,designation='other-pointer'))

    def test_terminal_and_missing_events_never_reprocess(self):
        self.assertTrue(self.store.accept('e',{'operation':'refresh'}))
        self.assertIsNotNone(self.store.begin_event('e'))
        self.store.end_event('e',True)
        self.assertIsNone(self.store.begin_event('e'))
        self.store.accept('b',{'operation':'refresh'})
        self.store.begin_event('b')
        self.store.end_event('b',False)
        self.assertIsNone(self.store.begin_event('b'))
        with self.assertRaises(f.FlowError): self.store.begin_event('absent')

    def test_send_outcome_table_no_retry(self):
        outcomes=[('confirm',lambda a:dict(accepted=True,request_id=a['request_id']),'CONFIRMED'),
                  ('timeout',TimeoutError,'UNKNOWN'),
                  ('wrong-request-id',lambda a:dict(accepted=True,request_id='other'),'UNKNOWN'),
                  ('not-accepted',lambda a:dict(accepted=False,request_id=a['request_id']),'UNKNOWN'),
                  ('non-dict-receipt',lambda a:'ok','UNKNOWN'),
                  ('missing-request-id',lambda a:dict(accepted=True),'UNKNOWN')]
        for name,impl,expected in outcomes:
            with self.subTest(name=name):
                store=f.Store(Path(self.temp.name)/('flow-'+name+'.sqlite'),initialize=True)
                action=dict(self.action,request_id='req-'+name)
                send=Mock(side_effect=impl)
                self.assertEqual(store.send_once(action,send,lambda _:True)['state'],expected)
                self.assertEqual(send.call_count,1)
                again=Mock()
                self.assertEqual(store.send_once(action,again,lambda _:True)['state'],expected)
                again.assert_not_called()

    def test_duplicate_event_cannot_change_payload(self):
        self.assertTrue(self.store.accept('event',{'operation':'refresh'}))
        self.assertFalse(self.store.accept('event',{'operation':'refresh'}))
        with self.assertRaises(f.FlowError): self.store.accept('event',{'operation':'dispatch'})

    def test_github_projection_precedes_routing(self):
        s=snapshot(); calls=[]
        ports=Mock()
        ports.load.return_value=s; ports.is_current.return_value=True
        ports.project.side_effect=lambda a:(calls.append('github') or self.send(a))
        ports.route.side_effect=lambda a:(calls.append('route') or self.send(a))
        self.store.accept('e',dict(operation='refresh'))
        f.Coordinator(self.store,ports).handle('e')
        self.assertEqual(calls,['github','route'])

    def test_unknown_projection_cannot_dispatch(self):
        ports=Mock(); ports.load.return_value=snapshot(); ports.is_current.return_value=True
        ports.project.side_effect=TimeoutError
        self.store.accept('e',dict(operation='refresh'))
        f.Coordinator(self.store,ports).handle('e')
        ports.route.assert_not_called()

    def test_status_ids_change_with_observed_evidence_not_event_noise(self):
        s=snapshot(); a=f.request(s,'STATUS','MECHANICAL','N/A')
        s['irrelevant_delivery']='other'; self.assertEqual(a,f.request(s,'STATUS','MECHANICAL','N/A'))
        s['checks'][0]['conclusion']='failure'
        self.assertNotEqual(a['request_id'],f.request(s,'STATUS','MECHANICAL','N/A')['request_id'])


class GatewayTests(unittest.TestCase):
    def test_no_redirect_credentials(self):
        with self.assertRaises(g.flow.FlowError): g.NoRedirect().redirect_request(None)

    def test_pagination_limit_fails_not_partial_success(self):
        api=g.Api('secret','secret'); api.call=Mock(return_value=[{}]*100)
        with self.assertRaises(g.flow.FlowError): api.pages('repos/owner/repo/pulls/1/reviews')
        self.assertEqual(api.call.call_count,20)

    def test_approval_body_edit_revokes_before_same_head_result(self):
        body='<!-- TEST -->\n'+json.dumps({'active':True})
        api=Mock(); api.call.return_value={'user':{'login':'owner'},'body':body+' '}
        ports=g.GithubPorts(api,{'user_actors':['owner']})
        binding=dict(actor='owner',comment_id=1,sha256=hashlib.sha256(body.encode()).hexdigest())
        with self.assertRaises(g.flow.FlowError): ports.bound_comment('owner/repo',binding,'<!-- TEST -->')

    def test_two_issue_aliases_for_same_task_block(self):
        ports=g.GithubPorts(Mock(),{'registrations':{'owner/repo#1':{'task_id':'T1'},'owner/repo#2':{'task_id':'T1'}}})
        with self.assertRaises(g.flow.FlowError): ports.command_for(snapshot())

    def test_disabled_ingress_does_not_read_body_or_call_network(self):
        ports=Mock(); store=Mock()
        ingress=g.Ingress(store,ports,{'enabled':False},b'',b'',executor=Mock())
        response=[]
        result=ingress({'REQUEST_METHOD':'POST'},lambda status,headers:response.append(status))
        self.assertEqual(response,['403 Forbidden']); store.accept.assert_not_called(); ports.load.assert_not_called()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.s = snapshot()
        self.s.update(dependencies=[], approval_pointer='https://github.com/owner/repo/issues/1#issuecomment-10')
        self.s['required_checks'][0].update(path='.github/workflows/ci.yml', workflow_blob='9'*40,
                                            events=['pull_request'])
        self.issue_body='TASK_ID: T1\nTASK_REVISION: 1\nBUILDER_ID: DEVIN\nREPO: owner/repo'
        def body(marker, value): return marker+'\n'+json.dumps(value)
        def binding(comment_id, value):
            return dict(comment_id=comment_id, actor='owner', sha256=hashlib.sha256(value.encode()).hexdigest())
        task_body=body('<!-- ASTRA_FLOW_TASK_V1 -->', self.s)
        lane_body=body('<!-- ASTRA_FLOW_LANE_V1 -->',dict(active=True,identity='reviewer',read_only_verified=True))
        lane=dict(self.s['reviewer'], binding=binding(3,lane_body))
        registration=dict(binding(10,task_body), task_id='T1', issue_body_sha256=hashlib.sha256(self.issue_body.encode()).hexdigest())
        scoped=dict(f.subject(self.s),task_digest=registration['sha256']); scoped.pop('policy_revision')
        prerequisite=body('<!-- ASTRA_FLOW_PREREQUISITES_V1 -->',dict(active=True,subject=scoped,result='PASS'))
        registration['prerequisites']=binding(5,prerequisite)
        self.policy=dict(enabled=True,user_actors=['owner'],repositories=['owner/repo'],
                         registrations={'owner/repo#1':registration}, review_routes={'DEVIN':'reviewer'},
                         review_lanes={'reviewer':lane})
        self.data={
          'repos/owner/repo/issues/1':dict(state='open',body=self.issue_body,html_url=self.s['task_pointer']),
          'repos/owner/repo/pulls/2':dict(state='open',draft=False,mergeable=True,html_url=self.s['pr_pointer'],
                base=dict(sha='b'*40,repo=dict(full_name='owner/repo')),head=dict(sha='a'*40),user=dict(login='author')),
          'repos/owner/repo/check-suites/99':dict(app=dict(id=1)),
          'repos/owner/repo/contents/.github/workflows/ci.yml?ref='+'a'*40:dict(sha='9'*40)}
        for number,text in ((10,task_body),(3,lane_body),(5,prerequisite)):
            self.data['repos/owner/repo/issues/comments/'+str(number)]=dict(user=dict(login='owner'),body=text,
                  issue_url='https://api.github.com/repos/owner/repo/issues/1',
                  html_url='https://github.com/owner/repo/issues/1#issuecomment-'+str(number))
        self.runs=[dict(id=10,workflow_id=2,head_sha='a'*40,event='pull_request',run_attempt=1,
                        check_suite_id=99,path='.github/workflows/ci.yml',pull_requests=[dict(number=2)],
                        status='completed',conclusion='success')]
        self.reviews=[]
        self.api=Mock()
        self.api.call.side_effect=lambda method,path,*args,**kwargs:copy.deepcopy(self.data[path])
        self.api.pages.side_effect=lambda path:copy.deepcopy(self.reviews if '/reviews' in path else self.runs)
        self.ports=g.GithubPorts(self.api,self.policy)
        self.command=dict(repository='owner/repo',issue=1,operation='refresh')

    def test_full_collector_to_independent_review_to_ready(self):
        first=self.ports.load(self.command)
        self.assertTrue(first['prerequisites_verified'])
        request=f.assess(first)['action']; report=passing(request); report['phase']='review'
        self.reviews.append(dict(id=12,user=dict(login='reviewer'),commit_id='a'*40,state='APPROVED',
                                 html_url=report['evidence_pointer'],body='<!-- ASTRA_FLOW_RESULT_V1 -->\n'+json.dumps(report)))
        current=self.ports.load(self.command)
        self.assertEqual(f.assess(current)['state'],'READY_FOR_MERGE')
        self.reviews[0]['state']='DISMISSED'
        self.assertEqual(f.assess(self.ports.load(self.command))['state'],'REVIEW_REQUIRED')

    def test_pinned_envelope_must_match_flow_identity(self):
        for field, replacement in (("TASK_ID: T1", "TASK_ID: OTHER"),
                                   ("TASK_REVISION: 1", "TASK_REVISION: 2"),
                                   ("BUILDER_ID: DEVIN", "BUILDER_ID: GLM")):
            body = self.issue_body.replace(field, replacement)
            self.data['repos/owner/repo/issues/1']['body'] = body
            self.policy['registrations']['owner/repo#1']['issue_body_sha256'] = hashlib.sha256(body.encode()).hexdigest()
            with self.assertRaises(g.flow.FlowError): self.ports.load(self.command)

    def test_dispatch_carries_pinned_identity_and_rejects_stale_action(self):
        current = self.ports.load(self.command)
        action = f.request(current, 'DISPATCH', 'author', 'N/A')
        with patch.object(self.ports, 'dispatch_authorized', return_value=True), \
             patch.object(self.api, 'call') as call, patch.object(self.ports, 'load', return_value=current):
            self.ports.route(action)
            inputs = call.call_args.args[2]['inputs']
            self.assertEqual(inputs['expected_task_id'], 'T1')
            self.assertEqual(inputs['expected_task_revision'], '1')
            self.assertEqual(inputs['expected_builder_id'], 'DEVIN')
            self.assertEqual(inputs['expected_issue_body_sha256'], hashlib.sha256(self.issue_body.encode()).hexdigest())
            call.reset_mock()
            current['revision'] = '2'
            with self.assertRaises(g.flow.FlowError): self.ports.route(action)
            call.assert_not_called()

    def test_new_failed_ci_attempt_overrides_old_success(self):
        self.runs.append(dict(self.runs[0],run_attempt=2,status='completed',conclusion='failure'))
        self.assertEqual(f.assess(self.ports.load(self.command))['state'],'BLOCKED')

    def test_issue_body_edit_and_foreign_pr_run_cannot_pass(self):
        self.data['repos/owner/repo/issues/1']['body']='changed'
        with self.assertRaises(g.flow.FlowError): self.ports.load(self.command)
        self.data['repos/owner/repo/issues/1']['body']=self.issue_body
        self.runs[0]['pull_requests']=[dict(number=100)]
        self.assertEqual(f.assess(self.ports.load(self.command))['state'],'BLOCKED')


class SlackFixtureTests(unittest.TestCase):
    """Deterministic /slack/commands fixture for the #36 staging status/refresh flow.

    No network: the WSGI environ is built in memory, the executor runs inline
    and ports are mocked. Covers fail-closed request validation, replay/stale
    rejection, and that status/refresh grammar can never reach the dispatch path.
    """

    class Future:
        def add_done_callback(self, callback):
            callback(self)

    class Executor:
        def __init__(self):
            self.submissions = []
        def submit(self, fn, *args):
            self.submissions.append(args)
            fn(*args)
            return SlackFixtureTests.Future()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        # The gateway loads its own private flow module; use its classes so the
        # exceptions raised inside Ingress are the same FlowError it catches.
        self.store = g.flow.Store(Path(self.temp.name)/'flow.sqlite', initialize=True)
        self.secret = b'staging-signing-secret-0123456789'
        self.policy = dict(enabled=True, repositories=['owner/repo'],
                           slack=dict(team_ids=['T1'], app_ids=['A1'], user_ids=['U1'],
                                      channel_ids=['C1'], projects={'KIX': 'owner/repo'}))
        self.fields = dict(team_id='T1', api_app_id='A1', user_id='U1', channel_id='C1',
                           command='/astra', text='status KIX 36', trigger_id='trigger-36')
        self.calls = []
        self.ports = Mock()
        self.ports.load.return_value = snapshot()
        self.ports.is_current.return_value = True
        self.ports.project.side_effect = lambda a: (self.calls.append('project') or
            dict(accepted=True, request_id=a['request_id'],
                 pointer='https://github.com/owner/repo/issues/36#issuecomment-1'))
        self.ports.route.side_effect = lambda a: (self.calls.append(('route', a['kind'])) or
                                                  dict(accepted=True, request_id=a['request_id']))
        self.executor = self.Executor()
        self.ingress = g.Ingress(self.store, self.ports, self.policy, self.secret,
                                 b'github-webhook-secret-unused', executor=self.executor)

    def sign(self, fields=None, timestamp=None):
        raw = urlencode(fields or self.fields).encode()
        ts = str(timestamp if timestamp is not None else int(time.time()))
        signature = 'v0=' + hmac.new(self.secret, b'v0:' + ts.encode() + b':' + raw,
                                     hashlib.sha256).hexdigest()
        return raw, {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': signature}

    def post(self, raw, headers, path='/slack/commands', method='POST', ingress=None):
        environ = {'REQUEST_METHOD': method, 'PATH_INFO': path,
                   'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': io.BytesIO(raw)}
        for key, value in headers.items():
            environ['HTTP_' + key.upper().replace('-', '_')] = value
        status = []
        body = (ingress or self.ingress)(environ, lambda s, h: status.append(s))
        return status[0], body

    def inbox(self):
        with self.store.transaction() as db:
            return db.execute('SELECT id,body,state FROM inbox').fetchall()

    def test_status_and_refresh_roundtrip_ephemeral_ack(self):
        for verb, kind in (('status', 'STATUS'), ('refresh', 'REVIEW')):
            with self.subTest(verb=verb):
                fields = dict(self.fields, text=verb + ' KIX 36', trigger_id='trigger-' + verb)
                status, body = self.post(*self.sign(fields))
                self.assertEqual(status, '200 OK')
                self.assertEqual(json.loads(body[0])['response_type'], 'ephemeral')
                row = self.inbox()[-1]
                self.assertEqual(row['state'], 'DONE')
                command = json.loads(row['body'])
                self.assertEqual(command, dict(operation=verb, repository='owner/repo',
                                               issue=36, actor='U1', channel='C1'))
                self.assertIn(('route', kind), self.calls)
        self.ports.dispatch_authorized.assert_not_called()
        order = [c if isinstance(c, str) else c[0] for c in self.calls]
        self.assertEqual(order, ['project', 'route', 'project', 'route'])

    def test_ready_snapshot_refresh_reports_ready_not_dispatch(self):
        s = snapshot(); s['review'] = passing(f.assess(s)['action'])
        self.ports.load.return_value = s
        fields = dict(self.fields, text='refresh KIX 36', trigger_id='trigger-ready')
        status, _ = self.post(*self.sign(fields))
        self.assertEqual(status, '200 OK')
        self.assertIn(('route', 'READY'), self.calls)
        self.ports.dispatch_authorized.assert_not_called()

    def test_status_refresh_grammar_cannot_reach_dispatch(self):
        for verb in ('status', 'refresh'):
            fields = dict(self.fields, text=verb + ' KIX 36')
            _, command = f.verify_slack(*self.sign(fields), self.secret,
                                        self.policy['slack'], int(time.time()))
            self.assertEqual(command['operation'], verb)
            self.assertNotIn('dispatch', command.values())
        # response_url/token in the raw form are never persisted into the ledger.
        fields = dict(self.fields, token='xoxb-secret', response_url='https://hooks.slack.com/x')
        _, command = f.verify_slack(*self.sign(fields), self.secret,
                                    self.policy['slack'], int(time.time()))
        self.assertNotIn('token', command); self.assertNotIn('response_url', command)
        # Explicit dispatch text parses but stays behind dispatch_authorized, which is off here.
        self.ports.dispatch_authorized.return_value = False
        fields = dict(self.fields, text='dispatch KIX 36', trigger_id='trigger-dispatch')
        status, _ = self.post(*self.sign(fields))
        self.assertEqual(status, '403 Forbidden')
        self.assertEqual(self.inbox()[-1]['state'], 'BLOCKED')
        self.ports.route.assert_not_called()
        self.assertNotIn(('route', 'DISPATCH'), self.calls)

    def test_request_validation_fail_closed(self):
        raw, headers = self.sign()
        cases = [('tampered-body', raw + b'&x=1', headers),
                 ('bad-signature', raw, dict(headers, **{'X-Slack-Signature': 'v0=' + '0'*64})),
                 ('non-numeric-timestamp', raw,
                  dict(headers, **{'X-Slack-Request-Timestamp': 'abc'}))]
        cases.append(('stale-timestamp', *self.sign(timestamp=int(time.time()) - 301)))
        for key, value in (('user_id', 'U2'), ('team_id', 'T2'), ('api_app_id', 'A2'),
                           ('channel_id', 'C2'), ('command', '/other'),
                           ('text', 'merge KIX 36'), ('text', 'PASS KIX 36'),
                           ('text', 'status KIX'), ('text', 'status KIX 36 extra'),
                           ('text', 'status OTHER 36'), ('text', 'status KIX 0'),
                           ('text', 'status KIX abc'), ('trigger_id', '')):
            fields = dict(self.fields); fields[key] = value
            cases.append((key + '=' + value, *self.sign(fields)))
        for name, raw, headers in cases:
            with self.subTest(name=name):
                self.assertEqual(self.post(raw, headers)[0], '403 Forbidden')
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.executor.submissions, [])
        self.ports.load.assert_not_called()

    def test_replay_and_retry_dedupe_same_event(self):
        raw, headers = self.sign()
        self.assertEqual(self.post(raw, headers)[0], '200 OK')
        self.assertEqual(len(self.inbox()), 1)
        # Slack retry reuses the trigger-derived event id; no second event or route.
        retry = dict(headers, **{'X-Slack-Retry-Num': '1', 'X-Slack-Retry-Reason': 'http_timeout'})
        self.assertEqual(self.post(raw, retry)[0], '200 OK')
        rows = self.inbox()
        self.assertEqual((len(rows), rows[0]['state']), (1, 'DONE'))
        self.assertEqual(self.ports.route.call_count, 1)
        # The same trigger id with a different command is rejected, never a second writer.
        fields = dict(self.fields, text='refresh KIX 36')
        self.assertEqual(self.post(*self.sign(fields))[0], '403 Forbidden')
        self.assertEqual(len(self.inbox()), 1)

    def test_malformed_bodies_and_headers_fail_closed(self):
        raw, headers = self.sign()
        cases = [('empty-body', b'', headers),
                 ('truncated-signature', raw,
                  dict(headers, **{'X-Slack-Signature': 'v0=abc123'})),
                 ('prefixless-signature', raw,
                  dict(headers, **{'X-Slack-Signature': headers['X-Slack-Signature'][3:]}))]
        nosig = dict(headers); nosig.pop('X-Slack-Signature')
        cases.append(('missing-signature', raw, nosig))
        nots = dict(headers); nots.pop('X-Slack-Request-Timestamp')
        cases.append(('missing-timestamp', raw, nots))
        cases.append(('future-timestamp', *self.sign(timestamp=int(time.time()) + 3600)))
        for key, value in (('channel_id', 'C9'),):
            fields = dict(self.fields, **{key: value}, text='dispatch KIX 36')
            cases.append((key + '=' + value + '+dispatch', *self.sign(fields)))
        for text in ('STATUS KIX 36', 'status KIX 036', 'status KIX 1234567890'):
            cases.append(('text=' + text, *self.sign(dict(self.fields, text=text))))
        cases.append(('oversized-trigger',
                      *self.sign(dict(self.fields, trigger_id='t' * 513))))
        # Duplicate form field: hand-built body, still validly signed.
        dup = urlencode(self.fields).encode() + b'&team_id=T1'
        ts = str(int(time.time()))
        dup_headers = {'X-Slack-Request-Timestamp': ts,
                       'X-Slack-Signature': 'v0=' + hmac.new(
                           self.secret, b'v0:' + ts.encode() + b':' + dup,
                           hashlib.sha256).hexdigest()}
        cases.append(('duplicate-field', dup, dup_headers))
        big = urlencode(dict(self.fields, padding='x' * 70000)).encode()
        ts = str(int(time.time()))
        big_headers = {'X-Slack-Request-Timestamp': ts,
                       'X-Slack-Signature': 'v0=' + hmac.new(
                           self.secret, b'v0:' + ts.encode() + b':' + big,
                           hashlib.sha256).hexdigest()}
        cases.append(('oversized-body', big, big_headers))
        for name, raw, headers in cases:
            with self.subTest(name=name):
                self.assertEqual(self.post(raw, headers)[0], '403 Forbidden')
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.executor.submissions, [])
        self.ports.load.assert_not_called()

    def test_malformed_content_length_and_truncated_body(self):
        raw, headers = self.sign()
        base = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/slack/commands',
                'wsgi.input': io.BytesIO(raw)}
        for key, value in headers.items():
            base['HTTP_' + key.upper().replace('-', '_')] = value
        variants = [dict(base), dict(base, CONTENT_LENGTH='abc'),
                    dict(base, CONTENT_LENGTH='-1'), dict(base, CONTENT_LENGTH=''),
                    dict(base, CONTENT_LENGTH=str(len(raw) + 10))]
        for environ in variants:
            with self.subTest(length=environ.get('CONTENT_LENGTH')):
                status = []
                self.ingress(environ, lambda s, h: status.append(s))
                self.assertEqual(status, ['403 Forbidden'])
        self.assertEqual(self.inbox(), [])

    def test_retry_header_on_first_delivery_is_normal_delivery(self):
        # Slack can mark a delivery retried even when the original never
        # arrived; the trigger-derived event id still dedupes it.
        raw, headers = self.sign()
        retry = dict(headers, **{'X-Slack-Retry-Num': '1',
                               'X-Slack-Retry-Reason': 'http_timeout'})
        self.assertEqual(self.post(raw, retry)[0], '200 OK')
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.post(raw, retry)[0], '200 OK')
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.ports.route.call_count, 1)

    def test_content_type_is_not_an_authentication_boundary(self):
        # Content-Type is never consulted: a wrong type cannot bypass the
        # signature check, and a browser-like type gains no authority.
        raw, headers = self.sign()
        environ = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/slack/commands',
                   'CONTENT_LENGTH': str(len(raw)), 'CONTENT_TYPE': 'text/plain',
                   'wsgi.input': io.BytesIO(raw)}
        bad = dict(headers, **{'X-Slack-Signature': 'v0=' + '0' * 64})
        for key, value in bad.items():
            environ['HTTP_' + key.upper().replace('-', '_')] = value
        status = []
        self.ingress(environ, lambda s, h: status.append(s))
        self.assertEqual(status, ['403 Forbidden'])
        self.assertEqual(self.inbox(), [])

    def test_wrong_method_path_and_disabled_fail_closed(self):
        raw, headers = self.sign()
        self.assertEqual(self.post(raw, headers, method='GET')[0], '403 Forbidden')
        self.assertEqual(self.post(raw, headers, path='/github/events')[0], '403 Forbidden')
        self.assertEqual(self.post(raw, headers, path='/')[0], '403 Forbidden')
        disabled = g.Ingress(self.store, self.ports, dict(self.policy, enabled=False),
                             self.secret, b'', executor=self.executor)
        environ = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/slack/commands',
                   'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': Mock()}
        status = []
        disabled(environ, lambda s, h: status.append(s))
        self.assertEqual(status, ['403 Forbidden'])
        environ['wsgi.input'].read.assert_not_called()
        self.assertEqual(self.inbox(), [])


if __name__=='__main__': unittest.main()
