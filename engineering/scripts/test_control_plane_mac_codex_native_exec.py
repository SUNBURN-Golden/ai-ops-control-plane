"""No-model checks for native exec/config guards and real fixture subprocesses."""
import copy
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock

APP=Path(__file__).resolve().parents[1]/'mac_app';sys.path.insert(0,str(APP))
import agents
import codex_config as config
import codex_native_exec as native
import common
import worker


def report(status='complete'):
    return {'status':status,'summary':'Synthetic native exec only; no product or actual model.',
        'question':'Fixture missing dependency.' if status=='needs_user' else '',
        'plan':None,'findings':[],'checks':['Executed synthetic fixture command.'],
        'reviewed_head':'a'*40,'covered_tasks':['fixture']}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.state=common.private_directory(self.base/'state')
        self.folder=common.private_directory(self.state/'native'/('a'*32)/('b'*32))
        self.checkout=common.private_directory(self.folder/'checkout')
        self.request={'host_directory':str(self.state),'attempt_id':'b'*32,'binding':'fixture-bound',
            'checkout':str(self.checkout),'role':'builder','profile':{'provider':'codex','model':''},
            'timeout_seconds':5,'prompt':'synthetic only'}
        self.config=self.base/'config.toml';self.header='[projects.'+json.dumps(str(self.checkout))+']\n'
        self.config.write_text('model="synthetic-secret-marker"\n'+self.header+'trust_level="trusted"\n');self.config.chmod(0o600)
        common.atomic_json(self.state/'codex-trust-approval.json',{'schema_version':1,
            'checkout':str(self.checkout),'field':'trust_level','value':'trusted','user_approved':True,
            'config_file':str(self.config)})
        common.atomic_json(self.folder/'request.json',self.request);common.atomic_json(self.folder/'schema.json',agents.SCHEMA)
        self.driver=native.Driver(self.request,self.folder)


class ConfigScopeTests(Fixture):
    def test_existing_trust_only_preserves_exact_other_bytes_and_target_journal(self):
        self.config.write_text('model="synthetic-secret-marker"\n'+self.header+'trust_level = "untrusted" # target\nother=true\n')
        before=config.Snapshot(self.config,self.checkout)
        self.config.write_bytes(before.raw.replace(b'"untrusted"',b'"trusted"'))
        self.assertTrue(before.verify(config.Snapshot(self.config,self.checkout)))
        self.assertNotIn('synthetic-secret-marker',json.dumps(before.journal()))
        self.assertEqual(before.journal()['previous_trust'],'untrusted')
    def test_absent_table_can_append_only_target_header_and_trust(self):
        self.config.write_text('model="synthetic"\n');before=config.Snapshot(self.config,self.checkout)
        self.config.write_bytes(before.raw+('\n'+self.header+'trust_level="trusted"\n').encode())
        self.assertTrue(before.verify(config.Snapshot(self.config,self.checkout)))
        self.config.write_bytes(self.config.read_bytes()+b'extra=true\n')
        with self.assertRaisesRegex(common.AppError,'CONFIG_CHANGED'):before.verify(config.Snapshot(self.config,self.checkout))
    def test_concurrent_non_target_bytes_and_file_mode_are_rejected(self):
        for change in ('comment','field','mode'):
            before=config.Snapshot(self.config,self.checkout)
            if change=='comment':self.config.write_bytes(before.raw+b'# concurrent edit\n')
            elif change=='field':self.config.write_bytes(before.raw.replace(b'synthetic-secret-marker',b'other-value'))
            else:self.config.chmod(0o640)
            self.assertFalse(before.unchanged())
            with self.assertRaisesRegex(common.AppError,'CONFIG_CHANGED'):before.verify(config.Snapshot(self.config,self.checkout))
    def test_approval_is_one_exact_canonical_checkout(self):
        self.assertTrue(config.approved(self.state,self.checkout)['user_approved'])
        for target in (self.base,self.checkout/'other'):
            with self.assertRaises(common.AppError):config.approved(self.state,target)
    def test_multiline_and_symlink_config_are_not_broadened(self):
        self.config.write_text('text="""\n'+self.header+'"""\n')
        with self.assertRaises(common.AppError):config.Snapshot(self.config,self.checkout)
        self.config.unlink();self.config.symlink_to(self.folder/'schema.json')
        with self.assertRaises(common.AppError):config.Snapshot(self.config,self.checkout)


class PreflightClient:
    def __init__(self,driver,path):
        self.driver=driver;self.path=path;self.calls=[];self.active=None;self.account='chatgpt';self.stopping=False
        raw={'permissions':{driver.name:driver.profile},'default_permissions':driver.name,
            'model_provider':'openai','web_search':'disabled','mcp_servers':{},'notify':[],
            'features':{key:False for key in native.FEATURES}}
        user={'sandbox_mode':'danger-full-access','mcp_servers':{'fixture':{'command':'must-not-run'}},
            'notify':['must-not-run'],'projects':{driver.request['checkout']:{'trust_level':'trusted'}}}
        self.read={'config':dict(raw,sandbox_mode='danger-full-access',mcp_servers=user['mcp_servers']),
            'layers':[{'name':{'type':'sessionFlags'},'config':raw},
                {'name':{'type':'user','file':str(path),'profile':None},'config':user},
                {'name':{'type':'project'},'config':{},'disabledReason':'untrusted'},
                {'name':{'type':'system'},'config':{}}]}
    def send(self,value):pass
    def rpc(self,method,params,timeout=20):
        self.calls.append(method)
        if method=='initialize':return {}
        if method=='config/read':return copy.deepcopy(self.read)
        if method=='permissionProfile/list':return {'data':[{'id':self.driver.name,'description':self.driver.profile['description'],'allowed':True}]}
        if method=='experimentalFeature/list':return {'data':[{'name':key,'enabled':key==self.active} for key in native.FEATURES],'nextCursor':None}
        if method=='account/read':return {'account':{'type':self.account}}
        raise AssertionError('Unexpected RPC '+method)


class NativePreflightTests(Fixture):
    def preflight(self,client):
        child=mock.Mock();child.stdin=io.BytesIO();child.returncode=0
        with mock.patch.object(native.subprocess,'Popen',return_value=child),mock.patch.object(native.sdk,'Protocol',return_value=client):
            self.driver.preflight('fixture-cli',{})
    def test_official_exclusion_keeps_user_legacy_mcp_and_notify_unchanged(self):
        client=PreflightClient(self.driver,self.config);before=self.config.read_bytes();self.preflight(client)
        self.assertTrue(self.driver.proof['loaded_policy_verified'])
        self.assertEqual(before,self.config.read_bytes())
        self.assertFalse(any(method in client.calls for method in ('mcpServerStatus/list','thread/start','turn/start')))
    def test_system_mcp_legacy_and_notify_cannot_be_ignored(self):
        for unsafe in ({'sandbox_mode':'workspace-write'},{'mcp_servers':{'x':{}}},{'notify':['handler']},{'hooks':{'onStart':[{}]}}):
            client=PreflightClient(self.driver,self.config);client.read['layers'][-1]['config']=unsafe
            with self.assertRaisesRegex(common.AppError,'PROFILE_UNVERIFIED'):self.preflight(client)
            self.assertNotIn('account/read',client.calls)
    def test_wrong_profile_or_enabled_external_surface_blocks_before_account(self):
        client=PreflightClient(self.driver,self.config);client.active='apps'
        with self.assertRaises(common.AppError):self.preflight(client)
        self.assertNotIn('account/read',client.calls)
        client=PreflightClient(self.driver,self.config);client.read['config']['default_permissions']=':workspace'
        with self.assertRaises(common.AppError):self.preflight(client)
    def test_only_normal_chatgpt_account_is_accepted(self):
        client=PreflightClient(self.driver,self.config);client.account='apiKey'
        with self.assertRaisesRegex(common.AppError,'LOGIN_REQUIRED'):self.preflight(client)
        self.assertNotIn('thread/start',client.calls)
    def test_missing_scoped_approval_stops_before_server(self):
        (self.state/'codex-trust-approval.json').unlink()
        with mock.patch.object(native.subprocess,'Popen') as spawn,self.assertRaises(common.AppError):self.driver.preflight('fixture-cli',{})
        spawn.assert_not_called()
    def test_runtime_change_blocks_before_exec_without_opening_auth(self):
        with mock.patch.object(native,'QUALIFIED_BINARY','0'*64),self.assertRaisesRegex(common.AppError,'RUNTIME_UNQUALIFIED'):native.qualified_cli()


FAKE_EXEC=r'''
import json,os,signal,sys,time
from pathlib import Path
assignment=sys.stdin.buffer.read()
def emit(value):print(json.dumps(value),flush=True)
emit({'type':'thread.started','thread_id':'fixture-native-session'})
emit({'type':'turn.started'})
if MODE=='wrong-session':emit({'type':'thread.started','thread_id':'other-session'});sys.exit(0)
if MODE=='unapproved-tool':emit({'type':'item.completed','item':{'id':'x','type':'mcp_tool_call'}});sys.exit(0)
if MODE=='exit-only':sys.exit(0)
command={'id':'command','type':'command_execution','command':'fixture command','status':'in_progress','exit_code':None}
emit({'type':'item.started','item':command})
if MODE=='hang':time.sleep(20);sys.exit(0)
command.update(status='completed',exit_code=None if MODE=='pending' else 0,aggregated_output='synthetic command result')
emit({'type':'item.completed','item':command})
target=Path(sys.argv[sys.argv.index('--output-last-message')+1]);target.write_text(json.dumps(REPORT))
emit({'type':'item.completed','item':{'id':'answer','type':'agent_message','text':json.dumps(REPORT)}})
emit({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}})
'''


class NativeExecSubprocessTests(Fixture):
    def fake_exec(self,mode='complete'):
        path=self.base/'codex-fixture';path.write_text('#!'+sys.executable+'\nMODE='+repr(mode)+'\nREPORT='+repr(report('needs_user' if mode=='needs-user' else 'complete'))+'\n'+FAKE_EXEC)
        path.chmod(0o755);self.driver.snapshot=config.Snapshot(self.config,self.checkout);return str(path)
    def execute(self,mode='complete'):
        with mock.patch('sys.stdout',new=io.StringIO()):return self.driver.execute(self.fake_exec(mode),agents.environment())
    def test_bound_session_actual_command_and_complete_report(self):
        self.assertEqual(self.execute(),report());self.assertTrue(self.driver.shutdown())
        self.assertEqual(self.driver.proof['thread_id'],'fixture-native-session')
        evidence=common.read_json(self.folder/'codex-command-evidence.json')
        self.assertEqual(evidence[0]['exit_code'],0);self.assertEqual(evidence[0]['output'],'synthetic command result')
    def test_terminal_needs_user_is_preserved(self):
        self.assertEqual(self.execute('needs-user'),report('needs_user'));self.assertTrue(self.driver.shutdown())
    def test_exit_zero_wrong_session_and_external_tool_are_not_completion(self):
        for mode in ('exit-only','wrong-session','unapproved-tool'):
            self.driver=native.Driver(self.request,self.folder)
            with self.assertRaises(common.AppError):self.execute(mode)
            self.assertTrue(self.driver.shutdown())
    def test_unobserved_pending_command_cannot_prove_shutdown(self):
        with self.assertRaises(common.AppError):self.execute('pending')
        self.assertFalse(self.driver.shutdown())
    def test_large_assignment_never_blocks_adapter_stdin(self):
        self.request['prompt']='synthetic '*450000
        self.assertEqual(self.execute(),report());self.assertTrue(self.driver.shutdown())
    def test_timeout_uses_normal_cli_interrupt_and_no_completion(self):
        self.request['timeout_seconds']=.3
        with self.assertRaisesRegex(common.AppError,'TURN_TIMEOUT'):self.execute('hang')
        self.driver.pending.clear() # fixture has no actual command subprocess
        self.assertTrue(self.driver.shutdown());self.assertNotEqual(self.driver.child.returncode,0)
    def test_worker_routes_only_to_trusted_native_driver(self):
        argv=worker.native_command(self.folder)
        self.assertEqual(argv,[sys.executable,str(APP/'codex_native_exec.py'),'--attempt',str(self.folder)])


if __name__=='__main__':unittest.main()
