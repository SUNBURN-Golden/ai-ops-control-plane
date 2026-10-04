"""Trusted stdio adapter; model commands use one pinned native permissions profile.

No credential is extracted, copied or changed by this adapter. The official
server uses its ordinary installed account. Only the trusted worker writes the
completion, policy proof and failure files outside the model's filesystem scope.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time

import agents
from common import (AppError, REPORT_LIMIT, WORKER_REQUEST_LIMIT, atomic_json,
                    digest, parse_json, private_directory, read_json)

FEATURES = ('apps','plugins','remote_plugin','recommended_plugins','plugin_sharing',
            'browser_use','browser_use_external','browser_use_full_cdp_access',
            'in_app_browser','computer_use','enable_mcp_apps','skill_mcp_dependency_install',
            'tool_call_mcp_elicitation','shell_snapshot','hooks','multi_agent','multi_agent_v2',
            'memories','external_agent_memory_import','image_generation','realtime_conversation')
ERRORS = ('MAC_CODEX_PROFILE_UNVERIFIED','MAC_CODEX_PROTOCOL_UNVERIFIED',
          'MAC_CODEX_PERMISSION_REQUIRED','MAC_CODEX_STOP_UNVERIFIED',
          'PROVIDER_LOGIN_REQUIRED','MODEL_UNAVAILABLE','PROVIDER_USAGE_LIMIT','USER_STOPPED')
POLICY_ERROR = 'MAC_CODEX_PROFILE_UNVERIFIED'


def require(value, code=POLICY_ERROR):
    if not value: raise AppError(code)


def without_nulls(value):
    if isinstance(value,dict): return {k:without_nulls(v) for k,v in value.items() if v is not None}
    if isinstance(value,list): return [without_nulls(v) for v in value]
    return value


def legacy_settings(value):
    if isinstance(value,dict):
        return bool({'sandbox_mode','sandbox_workspace_write'} & value.keys()) or any(legacy_settings(v) for v in value.values())
    if isinstance(value,list): return any(legacy_settings(v) for v in value)
    return False


def toml_inline(value):
    if isinstance(value,dict): return '{'+','.join(json.dumps(k)+'='+toml_inline(v) for k,v in value.items())+'}'
    if isinstance(value,list): return '['+','.join(toml_inline(v) for v in value)+']'
    return json.dumps(value)


def policy(request,folder):
    root,checkout=Path(request['host_directory']).resolve(),Path(request['checkout']).resolve()
    folder=Path(folder).resolve()
    require(root in folder.parents and root in checkout.parents and root!=checkout)
    require(request['role'] in ('planner','builder','reviewer','supervisor'))
    require(re.fullmatch(r'[0-9a-f]{32}',request['attempt_id']) and folder.name==request['attempt_id'])
    tmp=private_directory(folder/'provider-tmp')
    runtime=private_directory(folder/'provider-runtime')
    private_directory(runtime/'state');private_directory(runtime/'log')
    writing=request['role']=='builder'
    filesystem={':root':'deny',':minimal':'read',':tmpdir':'deny',':slash_tmp':'deny',
        str(root):'deny',str(Path(__file__).resolve().parent):'deny',
        str(Path.home()/'Library/Application Support/AIOPS Development'):'deny',
        str(Path.home()/'Library/LaunchAgents/local.aiops.mac.plist'):'deny',
        '/Library/Frameworks/Python.framework':'read',str(checkout):'write' if writing else 'read',
        **{str(checkout/name):'read' for name in ('.git','.agents','.codex','.aws')}}
    if writing:filesystem[str(tmp)]='write'
    value={'extends':':workspace' if writing else ':read-only','workspace_roots':{str(checkout):True},
        'filesystem':filesystem,'network':{'enabled':writing}}
    value['description']='AIOPS '+request['role']+' '+digest({'binding':request['binding'],'policy':value})
    name='aiops-'+request['role']+'-'+request['attempt_id']
    return name,value,tmp,runtime


def bound_profile(listed,read,name,expected,diagnostics=None):
    config=read['config'];layers=read.get('layers') or []
    # Hooks are separately disabled as an actual runtime feature. User-supplied
    # external handlers are rejected even when that feature is off.
    hooks=config.get('hooks') or {}
    checks={'legacy_clear':not any(legacy_settings(layer.get('config',{})) for layer in layers),
        'effective_profile_matches':without_nulls(config.get('permissions',{}).get(name))==expected,
        'raw_profile_matches':any(layer.get('config',{}).get('permissions',{}).get(name)==expected for layer in layers),
        'profile_selection_allowed':any(v['id']==name and v.get('allowed') is True and v.get('description')==expected['description'] for v in listed['data']),
        'default_profile_matches':config.get('default_permissions')==name,
        # Config/read exposes an unset default as null. Thread/start explicitly
        # selects openai and its response must confirm that exact provider.
        'model_provider_matches':config.get('model_provider') in (None,'openai'),
        'web_search_disabled':config.get('web_search')=='disabled','mcp_empty':not config.get('mcp_servers'),
        'notify_empty':not config.get('notify'),'external_hooks_empty':not any(v for k,v in hooks.items() if k not in ('enabled',))}
    if diagnostics is not None:diagnostics.update(checks)
    require(all(checks.values()))
    return digest(expected)


class Protocol:
    def __init__(self,child,deadline):
        self.child=child;self.deadline=deadline;self.buffer=b'';self.serial=0
        self.events=deque();self.responses={};self.stopping=False

    def send(self,value):
        try:self.child.stdin.write((json.dumps(value)+'\n').encode());self.child.stdin.flush()
        except (BrokenPipeError,OSError):raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED') from None

    def receive(self,deadline=None):
        until=min(self.deadline,deadline or self.deadline)
        while time.monotonic()<until:
            if b'\n' in self.buffer:
                line,self.buffer=self.buffer.split(b'\n',1)
                try:value=parse_json(line.decode(),16777216)
                except (ValueError,UnicodeError,AppError):raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED') from None
                require(isinstance(value,dict),'MAC_CODEX_PROTOCOL_UNVERIFIED')
                if 'method' in value and 'id' in value:
                    # Never grant permissions, run a dynamic tool, or fulfill an
                    # external elicitation. Stop this turn normally afterwards.
                    method=value['method']
                    if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                        answer={'decision':'decline'}
                    elif method=='item/permissions/requestApproval':answer={'permissions':{}}
                    elif method=='mcpServer/elicitation/request':answer={'action':'decline','content':None}
                    else:answer=None
                    self.send({'id':value['id'],**({'result':answer} if answer is not None else
                        {'error':{'code':-32601,'message':'AIOPS disallows this capability'}})})
                    raise AppError('MAC_CODEX_PERMISSION_REQUIRED')
                return value
            if len(self.buffer)>16777216:raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED')
            if self.stopping:raise AppError('USER_STOPPED')
            ready,_,_=select.select([self.child.stdout],[],[],min(.25,max(0,until-time.monotonic())))
            if not ready:continue
            chunk=os.read(self.child.stdout.fileno(),65536)
            if not chunk:raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED')
            self.buffer+=chunk
        raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED')

    def rpc(self,method,params,timeout=20):
        self.serial+=1;ident=self.serial;self.send({'id':ident,'method':method,'params':params})
        until=min(self.deadline,time.monotonic()+timeout)
        while True:
            value=self.responses.pop(ident,None)
            if value is None:value=self.receive(until)
            if value.get('id')==ident:
                if 'error' in value:raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED')
                require(isinstance(value.get('result'),dict),'MAC_CODEX_PROTOCOL_UNVERIFIED')
                return value['result']
            if 'id' in value:self.responses[value['id']]=value
            else:self.events.append(value)

    def event(self):
        if self.events:return self.events.popleft()
        value=self.receive()
        if 'id' in value:self.responses[value['id']]=value;return self.event()
        return value


class Adapter:
    def __init__(self,request,folder,client):
        self.request=request;self.folder=Path(folder);self.client=client
        self.name,self.expected,self.tmp,self.runtime=policy(request,folder)
        self.thread=None;self.turn=None;self.command_items=[];self.final_messages={}
        self.command_outputs=[]
        self.evidence={'attempt_id':request['attempt_id'],'binding':request['binding'],
            'profile_id':self.name,'profile_sha256':digest(self.expected),'role':request['role'],
            'checkout':request['checkout'],'model_turn_requested':False,'commands':[],
            'native_profile':True,'outer_sandbox':False,'shutdown_verified':False}

    def save(self):atomic_json(self.folder/'codex-policy-evidence.json',self.evidence)

    def preflight(self):
        read=self.client.rpc('config/read',{'includeLayers':True,'cwd':self.request['checkout']})
        listed=self.client.rpc('permissionProfile/list',{'cwd':self.request['checkout']})
        checks={}
        try:require(bound_profile(listed,read,self.name,self.expected,checks)==self.evidence['profile_sha256'])
        finally:self.evidence['profile_checks']=checks;self.save()
        features=self.client.rpc('experimentalFeature/list',{'limit':1000})
        require(features.get('nextCursor') is None)
        require(all(any(x['name']==key and x.get('enabled') is False for x in features['data']) for key in FEATURES))
        # Query MCP status only after the loaded config proves no server exists.
        require(self.client.rpc('mcpServerStatus/list',{}).get('data')==[])
        self.evidence.update(loaded_policy_verified=True,surfaces_disabled=True,
            loaded_layers_sha256=digest([layer.get('config',{}).get('permissions',{}).get(self.name) for layer in read.get('layers') or []]),
            preflight_at=time.time());self.save()

    def account(self):
        result=self.client.rpc('account/read',{'refreshToken':False})
        require((result.get('account') or {}).get('type')=='chatgpt','PROVIDER_LOGIN_REQUIRED')
        self.evidence['account_type']='chatgpt';self.save()

    def start(self):
        self.preflight();self.account()
        params={'cwd':self.request['checkout'],'permissions':self.name,'runtimeWorkspaceRoots':[self.request['checkout']],
            'approvalPolicy':'never','ephemeral':True,'modelProvider':'openai','allowProviderModelFallback':False,
            'dynamicTools':[],'environments':[],'serviceName':'aiops-mac'}
        if self.request['profile']['model']:params['model']=self.request['profile']['model']
        result=self.client.rpc('thread/start',params)
        self.thread=result.get('thread',{}).get('id')
        require(isinstance(self.thread,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',self.thread))
        require(result.get('thread',{}).get('ephemeral') is True)
        require(result.get('activePermissionProfile')=={'id':self.name,'extends':self.expected['extends']})
        require(result.get('cwd')==self.request['checkout'] and result.get('approvalPolicy')=='never' and
                result.get('modelProvider')=='openai' and result.get('runtimeWorkspaceRoots')==[self.request['checkout']])
        if self.request['profile']['model']:require(result.get('model')==self.request['profile']['model'])
        self.evidence.update(thread_id=self.thread,thread_request_sha256=digest(params),
            active_profile_verified=True,model_selected=result.get('model'));self.save()
        print(json.dumps({'type':'thread.started','thread_id':self.thread}),flush=True)
        self.preflight()
        params={'threadId':self.thread,'cwd':self.request['checkout'],'permissions':self.name,
            'runtimeWorkspaceRoots':[self.request['checkout']],'approvalPolicy':'never','environments':[],
            'input':[{'type':'text','text':self.request['prompt']}],'outputSchema':agents.SCHEMA}
        if self.request['profile']['model']:params['model']=self.request['profile']['model']
        self.evidence.update(model_turn_requested=True,turn_request_sha256=digest(params));self.save()
        result=self.client.rpc('turn/start',params)
        self.turn=result.get('turn',{}).get('id');require(isinstance(self.turn,str))
        self.evidence['turn_id']=self.turn;self.save()

    def scoped_path(self,value,writing=False):
        require(isinstance(value,str))
        path=Path(value)
        if not path.is_absolute():path=Path(self.request['checkout'])/path
        root=Path(self.request['checkout']).resolve();resolved=path.resolve()
        require(root==resolved or root in resolved.parents)
        if writing:
            parts=resolved.relative_to(root).parts
            require(self.request['role']=='builder' and parts and parts[0] not in ('.git','.agents','.codex','.aws'))

    def item(self,event):
        params=event.get('params') or {}
        require(params.get('threadId')==self.thread and params.get('turnId')==self.turn)
        value=params.get('item') or {};kind=value.get('type')
        if kind=='commandExecution':
            self.scoped_path(value.get('cwd'))
            require(not value.get('pluginId'))
            self.preflight()
            record={'id':value.get('id'),'cwd':value.get('cwd'),'command_sha256':digest(value.get('command')),
                'profile_sha256':self.evidence['profile_sha256'],'event':event['method'],'status':value.get('status'),
                'exit_code':value.get('exitCode')}
            self.evidence['commands'].append(record);self.save()
            if event['method']=='item/completed':
                output=value.get('aggregatedOutput') or ''
                require(isinstance(output,str),'MAC_CODEX_PROTOCOL_UNVERIFIED')
                self.command_outputs.append(dict(record,command=value.get('command'),
                    duration_ms=value.get('durationMs'),output=output[:262144],
                    output_sha256=digest(output),output_truncated=len(output)>262144))
                require(len(json.dumps(self.command_outputs).encode())<=8388608,'MAC_CODEX_PROTOCOL_UNVERIFIED')
                atomic_json(self.folder/'codex-command-evidence.json',self.command_outputs)
        elif kind=='fileChange':
            require(self.request['role']=='builder')
            for change in value.get('changes',[]):self.scoped_path(change.get('path'),writing=True)
        elif kind=='imageView':self.scoped_path(value.get('path'))
        elif kind=='agentMessage':
            if event['method']=='item/completed' and value.get('phase') in ('final_answer',None):
                require(isinstance(value.get('text'),str),'MAC_CODEX_PROTOCOL_UNVERIFIED')
                self.final_messages[value['id']]=value['text']
        else:require(kind in ('userMessage','reasoning','plan','functionCallOutput','contextCompaction'))

    def completed_report(self,turn):
        require(turn.get('id')==self.turn,'MAC_CODEX_PROTOCOL_UNVERIFIED')
        status=turn.get('status')
        if status!='completed':
            error=(turn.get('error') or {}).get('codexErrorInfo')
            code={'unauthorized':'PROVIDER_LOGIN_REQUIRED','usageLimitExceeded':'PROVIDER_USAGE_LIMIT',
                  'sandboxError':POLICY_ERROR}.get(error,'MAC_CODEX_PROTOCOL_UNVERIFIED') if isinstance(error,str) else 'MAC_CODEX_PROTOCOL_UNVERIFIED'
            raise AppError(code)
        finals=[item['text'] for item in turn.get('items',[]) if item.get('type')=='agentMessage' and item.get('phase')=='final_answer']
        if not finals:finals=list(self.final_messages.values())[-1:]
        require(len(finals)==1,'MAC_CODEX_PROTOCOL_UNVERIFIED')
        report=agents.validate_report(parse_json(finals[0],REPORT_LIMIT))
        self.preflight()
        return report

    def run(self):
        self.start()
        while True:
            event=self.client.event();method=event.get('method')
            if method in ('item/started','item/completed'):self.item(event)
            elif method=='turn/completed':
                require(event.get('params',{}).get('threadId')==self.thread)
                return self.completed_report(event['params']['turn'])
            elif method=='turn/started':
                require(event.get('params',{}).get('threadId')==self.thread and event.get('params',{}).get('turn',{}).get('id')==self.turn)
            elif method=='thread/started':require(event.get('params',{}).get('thread',{}).get('id')==self.thread)
            elif method and (method.startswith(('item/','thread/','turn/','error','hook'))):
                # Deltas/status carry no additional authority; require their
                # session identity whenever the official event supplies it.
                params=event.get('params') or {}
                if 'threadId' in params:require(params['threadId']==self.thread)
                if 'turnId' in params:require(params['turnId']==self.turn)
                require(not any(s in method.lower() for s in ('mcp','browser','permission','approval','hook','collab','dynamic')))

    def shutdown(self):
        self.client.stopping=False;self.client.deadline=time.monotonic()+7
        if self.thread:
            if self.turn:
                try:self.client.rpc('turn/interrupt',{'threadId':self.thread,'turnId':self.turn},timeout=2)
                except AppError:pass # An already-completed turn has nothing to interrupt.
            self.client.rpc('thread/backgroundTerminals/clean',{'threadId':self.thread},timeout=2)
            terminals=self.client.rpc('thread/backgroundTerminals/list',{'threadId':self.thread},timeout=2)
            require(terminals.get('data')==[] and terminals.get('nextCursor') is None,'MAC_CODEX_STOP_UNVERIFIED')


def run(folder):
    folder=Path(folder).resolve();request=read_json(folder/'request.json',WORKER_REQUEST_LIMIT)
    require(request.get('profile',{}).get('provider')=='codex')
    name,expected,tmp,runtime=policy(request,folder)
    cli=agents.executable('codex');require(cli,'MISSING_PROVIDER')
    overrides={'default_permissions':name,'permissions.'+name:expected,'analytics.enabled':False,
        'web_search':'disabled','mcp_servers':{},'sqlite_home':str(runtime/'state'),'log_dir':str(runtime/'log'),
        **{'features.'+key:False for key in FEATURES}}
    argv=[cli,*sum((['-c',key+'='+toml_inline(value)] for key,value in overrides.items()),[]),'app-server','--stdio','--strict-config']
    env=agents.environment(request['profile'],request['role']);env.update(TMPDIR=str(tmp),PYTHONDONTWRITEBYTECODE='1')
    child=None;adapter=None;error=None;report=None;shutdown=False
    try:
        child=subprocess.Popen(argv,cwd=request['checkout'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=sys.stderr.buffer)
        client=Protocol(child,time.monotonic()+request['timeout_seconds'])
        adapter=Adapter(request,folder,client)
        signal.signal(signal.SIGTERM,lambda *_:setattr(client,'stopping',True))
        signal.signal(signal.SIGINT,lambda *_:setattr(client,'stopping',True))
        client.rpc('initialize',{'clientInfo':{'name':'aiops_mac','version':'0.3.7'},'capabilities':{'experimentalApi':True}})
        client.send({'method':'initialized'})
        report=adapter.run()
    except Exception as exc:
        error=exc.code if isinstance(exc,AppError) else 'MAC_CODEX_PROTOCOL_UNVERIFIED'
    finally:
        if child is not None:
            try:
                if adapter:adapter.shutdown()
                child.stdin.close();child.wait(timeout=5)
                require(child.returncode==0 or (adapter and adapter.thread is None and
                        adapter.evidence['model_turn_requested'] is False),'MAC_CODEX_STOP_UNVERIFIED')
                shutdown=True
            except (AppError,OSError,subprocess.TimeoutExpired):
                error='MAC_CODEX_STOP_UNVERIFIED';report=None
                # Only the admitted server is signalled, never a receipt PID.
                if child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=2)
                    except subprocess.TimeoutExpired:pass
            if adapter:
                adapter.evidence.update(shutdown_verified=shutdown,error=error);adapter.save()
        if error:
            atomic_json(folder/'codex-adapter-error.json',{'attempt_id':request['attempt_id'],'binding':request['binding'],
                'code':error if error in ERRORS else 'MAC_CODEX_PROTOCOL_UNVERIFIED','shutdown_verified':shutdown})
        elif report is not None and shutdown:atomic_json(folder/'last-message.json',report)
    return 0 if error is None and shutdown and report is not None else 1


if __name__=='__main__':
    sys.dont_write_bytecode=True
    parser=argparse.ArgumentParser();parser.add_argument('--attempt',required=True)
    sys.exit(run(parser.parse_args().attempt))
