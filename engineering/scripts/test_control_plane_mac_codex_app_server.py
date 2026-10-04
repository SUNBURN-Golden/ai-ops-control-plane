"""No-model protocol and failure checks for the trusted native-profile adapter."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

APP=Path(__file__).resolve().parents[1]/'mac_app'
sys.path.insert(0,str(APP))
import agents
import codex_app_server as sdk
import common
import worker


def report(status='complete'):
    return {'status':status,'summary':'Synthetic protocol only; no real model or product implementation.',
        'question':'Fixture dependency needed.' if status=='needs_user' else '',
        'plan':None,'findings':[],'checks':['Executed a synthetic protocol fixture.'],
        'reviewed_head':'a'*40,'covered_tasks':['fixture']}


class FixtureClient:
    def __init__(self):self.calls=[];self.events=[];self.adapter=None;self.change=None
    def rpc(self,method,params,timeout=20):
        self.calls.append((method,copy.deepcopy(params)))
        a=self.adapter
        config={'permissions':{a.name:a.expected},'default_permissions':a.name,'model_provider':'openai',
            'web_search':'disabled','mcp_servers':{},'notify':[],'hooks':{}}
        if self.change:self.change(config)
        if method=='config/read':return {'config':config,'layers':[{'config':config}]}
        if method=='permissionProfile/list':return {'data':[{'id':a.name,'description':a.expected['description'],'allowed':True}]}
        if method=='experimentalFeature/list':return {'data':[{'name':key,'enabled':False} for key in sdk.FEATURES],'nextCursor':None}
        if method=='mcpServerStatus/list':return {'data':[]}
        if method=='account/read':return {'account':{'type':'chatgpt'}} # synthetic response, not an account lookup
        if method=='thread/start':return {'thread':{'id':'fixture-thread','ephemeral':True},'activePermissionProfile':{'id':a.name,'extends':a.expected['extends']},
            'cwd':a.request['checkout'],'approvalPolicy':'never','runtimeWorkspaceRoots':[a.request['checkout']],
            'modelProvider':'openai','model':'fixed-model'}
        if method=='turn/start':return {'turn':{'id':'fixture-turn'}}
        if method=='thread/backgroundTerminals/list':return {'data':[],'nextCursor':None}
        if method in ('turn/interrupt','thread/backgroundTerminals/clean'):return {}
        raise AssertionError(method)
    def event(self):return self.events.pop(0)


class AdapterPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=common.private_directory(Path(self.temp.name)/'state')
        self.folder=common.private_directory(self.root/'native/fixture'/('a'*32))
        self.checkout=common.private_directory(self.folder/'checkout')
        self.request={'host_directory':str(self.root),'attempt_id':'a'*32,'binding':'fixture-bound',
            'role':'builder','profile':{'provider':'codex','model':'fixed-model'},'checkout':str(self.checkout),
            'prompt':'synthetic instruction secret-test-marker','timeout_seconds':10}
        self.client=FixtureClient();self.adapter=sdk.Adapter(self.request,self.folder,self.client);self.client.adapter=self.adapter
    def start(self):
        with mock.patch('sys.stdout',new=io.StringIO()):self.adapter.start()
    def item(self,kind,**fields):
        return {'method':'item/completed','params':{'threadId':'fixture-thread','turnId':'fixture-turn',
            'item':{'id':'fixture-item','type':kind,**fields}}}
    def test_profile_and_schema_are_bound_to_thread_and_turn_and_not_legacy(self):
        self.start()
        for method,params in self.client.calls:
            if method in ('thread/start','turn/start'):
                self.assertEqual(params['permissions'],self.adapter.name)
                self.assertEqual(params['approvalPolicy'],'never')
                self.assertNotIn('sandbox',params);self.assertNotIn('sandboxPolicy',params)
        self.assertEqual(next(p for m,p in self.client.calls if m=='turn/start')['outputSchema'],agents.SCHEMA)
        evidence=common.read_json(self.folder/'codex-policy-evidence.json')
        self.assertTrue(evidence['active_profile_verified']);self.assertTrue(evidence['model_turn_requested'])
        self.assertNotIn('secret-test-marker',json.dumps(evidence))
    def test_legacy_setting_blocks_before_account_thread_or_turn(self):
        self.client.change=lambda config:config.update(sandbox_mode='workspace-write')
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.start()
        self.assertFalse(any(m in ('account/read','thread/start','turn/start') for m,_ in self.client.calls))
    def test_changed_policy_blocks_before_model(self):
        changed=copy.deepcopy(self.adapter.expected);changed['filesystem'][':root']='read'
        self.client.change=lambda config:config.update(permissions={self.adapter.name:changed})
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.start()
        self.assertFalse(any(m=='turn/start' for m,_ in self.client.calls))
    def test_unexpected_mcp_is_rejected_before_status_can_start_it(self):
        self.client.change=lambda config:config.update(mcp_servers={'unexpected':{'command':'unrelated'}})
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.start()
        self.assertFalse(any(m in ('mcpServerStatus/list','thread/start') for m,_ in self.client.calls))
    def test_feature_enablement_blocks_before_model(self):
        original=self.client.rpc
        def response(method,params,timeout=20):
            value=original(method,params,timeout)
            if method=='experimentalFeature/list':value['data'][0]['enabled']=True
            return value
        self.client.rpc=response
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.start()
        self.assertFalse(any(m=='turn/start' for m,_ in self.client.calls))
    def test_wrong_selected_thread_profile_never_starts_turn(self):
        original=self.client.rpc
        def response(method,params,timeout=20):
            value=original(method,params,timeout)
            if method=='thread/start':value['activePermissionProfile']['id']=':workspace'
            return value
        self.client.rpc=response
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.start()
        self.assertFalse(any(m=='turn/start' for m,_ in self.client.calls))
    def test_each_command_is_scope_checked_and_revalidates_policy(self):
        self.start();before=len(self.client.calls)
        self.adapter.item(self.item('commandExecution',cwd=str(self.checkout),command='synthetic test',status='completed',exitCode=0))
        self.assertGreater(len(self.client.calls),before)
        self.assertEqual(self.adapter.evidence['commands'][0]['exit_code'],0)
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):
            self.adapter.item(self.item('commandExecution',cwd=str(self.root),command='bad',status='completed',exitCode=0))
    def test_file_changes_cannot_escape_git_metadata_or_reviewer(self):
        self.start()
        for path in (self.root/'receipt.json',self.checkout/'.git/config'):
            with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):
                self.adapter.item(self.item('fileChange',changes=[{'path':str(path)}]))
        self.adapter.request['role']='reviewer'
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):
            self.adapter.item(self.item('fileChange',changes=[{'path':str(self.checkout/'code.py')}]))
    def test_unexpected_tool_and_wrong_session_are_rejected(self):
        self.start()
        for kind in ('mcpToolCall','dynamicToolCall','collabAgentToolCall','webSearch','hookPrompt'):
            with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.adapter.item(self.item(kind))
        value=self.item('agentMessage',text=json.dumps(report()),phase='final_answer');value['params']['turnId']='other'
        with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.adapter.item(value)
    def test_terminal_needs_user_preserves_question_not_complete_or_retry(self):
        self.start();self.adapter.final_messages={'message':json.dumps(report('needs_user'))}
        outcome=self.adapter.completed_report({'id':'fixture-turn','status':'completed','items':[]})
        self.assertEqual(outcome['status'],'needs_user');self.assertEqual(outcome['question'],'Fixture dependency needed.')
        self.assertEqual(sum(m=='turn/start' for m,_ in self.client.calls),1)
    def test_exit_or_incomplete_turn_cannot_grant_completion(self):
        self.start()
        for turn in ({'id':'fixture-turn','status':'failed','error':{'codexErrorInfo':'sandboxError'}},
                     {'id':'fixture-turn','status':'completed','items':[]}):
            with self.assertRaises(common.AppError):self.adapter.completed_report(turn)
    def test_shutdown_interrupts_and_verifies_background_tools(self):
        self.start();self.adapter.shutdown()
        self.assertEqual([m for m,_ in self.client.calls[-3:]],
            ['turn/interrupt','thread/backgroundTerminals/clean','thread/backgroundTerminals/list'])
        original=self.client.rpc
        self.client.rpc=lambda method,params,timeout=20: {'data':[{'processId':'still-alive'}]} if method=='thread/backgroundTerminals/list' else original(method,params,timeout)
        with self.assertRaisesRegex(common.AppError,'STOP_UNVERIFIED'):self.adapter.shutdown()


class ProtocolRejectionTests(unittest.TestCase):
    def test_permission_or_external_request_never_gets_a_grant(self):
        for method in ('item/permissions/requestApproval','item/commandExecution/requestApproval','mcpServer/elicitation/request','unexpected/tool'):
            child=mock.Mock();child.stdin=io.BytesIO();child.stdout=io.BytesIO()
            client=sdk.Protocol(child,time.monotonic()+1)
            client.buffer=(json.dumps({'id':9,'method':method,'params':{'permissions':{'network':{'enabled':True}}}})+'\n').encode()
            with self.assertRaisesRegex(common.AppError,'PERMISSION_REQUIRED'):client.receive()
            answer=json.loads(child.stdin.getvalue())
            self.assertNotIn('accept',json.dumps(answer));self.assertNotIn('true',json.dumps(answer))
    def test_stop_flag_interrupts_receive_without_restarting(self):
        client=sdk.Protocol(mock.Mock(),time.monotonic()+1);client.stopping=True
        with self.assertRaisesRegex(common.AppError,'USER_STOPPED'):client.receive()
    def test_malformed_protocol_is_sanitized(self):
        client=sdk.Protocol(mock.Mock(),time.monotonic()+1);client.buffer=b'not-json secret-test-marker\n'
        with self.assertRaisesRegex(common.AppError,'PROTOCOL_UNVERIFIED') as exc:client.receive()
        self.assertNotIn('secret-test-marker',str(exc.exception))


FAKE_SERVER = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,APP_PATH)
import codex_app_server as sdk
def fixture_inline(text):
 quoted=False;escaped=False;result=[]
 for char in text:
  if char=='"' and not escaped:quoted=not quoted
  result.append(':' if char=='=' and not quoted else char)
  escaped=(char=='\\' and not escaped) if quoted else False
 return json.loads(''.join(result))
values={}
for i,arg in enumerate(sys.argv):
 if arg=='-c':
  key,text=sys.argv[i+1].split('=',1);values[key]=fixture_inline(text)
name=values['default_permissions'];profile=values['permissions.'+name]
config={'permissions':{name:profile},'default_permissions':name,'model_provider':'openai',
 'web_search':'disabled','mcp_servers':{},'notify':[],'hooks':{}}
if MODE=='legacy':config['sandbox_mode']='workspace-write'
def emit(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
 value=json.loads(line);method=value.get('method');ident=value.get('id');params=value.get('params',{})
 if method is None or ident is None:continue
 result={}
 if method=='config/read':result={'config':config,'layers':[{'config':config}]}
 elif method=='permissionProfile/list':result={'data':[{'id':name,'description':profile['description'],'allowed':True}]}
 elif method=='experimentalFeature/list':result={'data':[{'name':k,'enabled':False} for k in sdk.FEATURES],'nextCursor':None}
 elif method=='mcpServerStatus/list':result={'data':[]}
 elif method=='account/read':result={'account':{'type':'apiKey' if MODE=='api-key' else 'chatgpt'}}
 elif method=='thread/start':result={'thread':{'id':'fixture-thread','ephemeral':True},'activePermissionProfile':{'id':name,'extends':profile['extends']},
  'cwd':params['cwd'],'approvalPolicy':'never','runtimeWorkspaceRoots':params['runtimeWorkspaceRoots'],'modelProvider':'openai','model':'fixed-model'}
 elif method=='turn/start':
  result={'turn':{'id':'fixture-turn'}}
  emit({'id':ident,'result':result})
  if MODE=='hang':continue
  if MODE=='permission':
   emit({'id':999,'method':'item/permissions/requestApproval','params':{'threadId':'fixture-thread','turnId':'fixture-turn','permissions':{'network':{'enabled':True}}}});continue
  emit({'method':'turn/started','params':{'threadId':'fixture-thread','turn':{'id':'fixture-turn'}}})
  message={'id':'message','type':'agentMessage','phase':'final_answer','text':json.dumps(REPORT)}
  emit({'method':'item/completed','params':{'threadId':'fixture-thread','turnId':'fixture-turn','item':message}})
  emit({'method':'turn/completed','params':{'threadId':'fixture-thread','turn':{'id':'fixture-turn','status':'completed','items':[message]}}})
  continue
 elif method=='thread/backgroundTerminals/list':result={'data':[{'processId':'unconfirmed-fixture'}] if MODE=='stop-unverified' else [],'nextCursor':None}
 emit({'id':ident,'result':result})
'''


class WorkerNativeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.state=common.private_directory(self.base/'state')
        self.folder=common.private_directory(self.state/'native'/('b'*32)/('c'*32))
        self.checkout=common.private_directory(self.folder/'checkout')
        self.bin=common.private_directory(self.base/'bin')
        self.request={'attempt_id':'c'*32,'binding':'fixture-stdio-bound',
            'profile':{'provider':'codex','model':'fixed-model'},'role':'builder','host_directory':str(self.state),
            'checkout':str(self.checkout),'prompt':'protocol fixture only','timeout_seconds':8}
        common.atomic_json(self.folder/'request.json',self.request);common.atomic_json(self.folder/'schema.json',agents.SCHEMA)
    def fake_server(self,mode='complete'):
        text='#!'+sys.executable+'\nAPP_PATH='+repr(str(APP))+'\nMODE='+repr(mode)+'\nREPORT='+repr(report('needs_user' if mode=='needs-user' else 'complete'))+'\n'+FAKE_SERVER
        path=self.bin/'codex';path.write_text(text);path.chmod(0o755)
    def run_worker(self):
        with mock.patch.dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ.get('PATH','')):
            worker.run(self.folder)
        return common.read_json(self.folder/'receipt.json')
    def test_real_stdio_subprocess_preserves_bound_native_completion_and_private_proof(self):
        self.fake_server();receipt=self.run_worker()
        self.assertIsNone(receipt['error']);self.assertEqual(receipt['report'],report())
        self.assertTrue(receipt['process_group_quiescent'])
        self.assertEqual(receipt['provider_evidence']['transport'],'app-server')
        self.assertEqual(receipt['provider_evidence']['session_id'],'codex-cli:fixture-thread')
        proof=common.read_json(self.folder/'codex-policy-evidence.json')
        self.assertTrue(proof['shutdown_verified']);self.assertTrue(proof['active_profile_verified'])
        self.assertEqual(receipt['provider_evidence']['policy_evidence_sha256'],common.digest(proof))
        with mock.patch.object(worker.subprocess,'Popen') as spawn,self.assertRaises(FileExistsError):worker.run(self.folder)
        spawn.assert_not_called()
    def test_prestart_legacy_policy_failure_has_terminal_receipt_without_model_turn(self):
        self.fake_server('legacy');receipt=self.run_worker()
        self.assertEqual(receipt['error'],'MAC_CODEX_PROFILE_UNVERIFIED');self.assertIsNone(receipt['report'])
        self.assertTrue(receipt['process_group_quiescent'])
        self.assertFalse(common.read_json(self.folder/'codex-policy-evidence.json')['model_turn_requested'])
    def test_api_key_account_is_not_an_oauth_fallback(self):
        self.fake_server('api-key');receipt=self.run_worker()
        self.assertEqual(receipt['error'],'PROVIDER_LOGIN_REQUIRED');self.assertTrue(receipt['process_group_quiescent'])
        self.assertFalse(common.read_json(self.folder/'codex-policy-evidence.json')['model_turn_requested'])
    def test_permission_request_is_declined_and_terminal_with_no_completion(self):
        self.fake_server('permission');receipt=self.run_worker()
        self.assertEqual(receipt['error'],'MAC_CODEX_PERMISSION_REQUIRED')
        self.assertTrue(receipt['process_group_quiescent']);self.assertIsNone(receipt['report'])
    def test_report_needs_user_is_preserved(self):
        self.fake_server('needs-user');receipt=self.run_worker()
        self.assertIsNone(receipt['error']);self.assertEqual(receipt['report'],report('needs_user'))
        self.assertTrue(receipt['process_group_quiescent'])
    def test_unverified_tool_shutdown_cannot_be_a_quiescent_completion(self):
        self.fake_server('stop-unverified');receipt=self.run_worker()
        self.assertFalse(receipt['process_group_quiescent']);self.assertIsNone(receipt['report'])
    def test_wrapper_normal_stop_interrupts_the_admitted_sdk_only(self):
        self.fake_server('hang')
        env=dict(agents.environment(),PATH=str(self.bin)+os.pathsep+os.environ.get('PATH',''))
        child=subprocess.Popen([sys.executable,str(APP/'worker.py'),'--attempt',str(self.folder)],env=env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        self.addCleanup(lambda:child.poll() is None and child.terminate())
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            proof=self.folder/'codex-policy-evidence.json'
            if proof.exists() and common.read_json(proof).get('model_turn_requested'):break
            time.sleep(.05)
        else:self.fail('fixture SDK turn did not start')
        common.atomic_json(self.folder/'stop-request.json',{'attempt_id':self.request['attempt_id'],'binding':self.request['binding']})
        stdout,stderr=child.communicate(timeout=12);self.assertEqual(child.returncode,0,stderr[-1000:])
        receipt=common.read_json(self.folder/'receipt.json')
        self.assertEqual(receipt['error'],'USER_STOPPED');self.assertTrue(receipt['process_group_quiescent'])
        self.assertIsNone(receipt['report']);self.assertTrue(common.read_json(proof)['shutdown_verified'])
    def test_native_timeout_has_no_completion_and_no_automatic_transient_retry_code(self):
        self.fake_server('hang');self.request['timeout_seconds']=1
        common.atomic_json(self.folder/'request.json',self.request)
        receipt=self.run_worker()
        self.assertIn(receipt['error'],('MAC_CODEX_TURN_TIMEOUT','MAC_CODEX_PROTOCOL_UNVERIFIED'))
        self.assertTrue(receipt['process_group_quiescent']);self.assertIsNone(receipt['report'])
    def test_large_assignment_is_read_from_private_request_not_an_unread_adapter_stdin(self):
        self.fake_server();self.request['prompt']='synthetic assignment '*220000
        common.atomic_json(self.folder/'request.json',self.request)
        receipt=self.run_worker()
        self.assertIsNone(receipt['error']);self.assertTrue(receipt['process_group_quiescent'])
        self.assertEqual(receipt['report'],report())


if __name__=='__main__':unittest.main()
