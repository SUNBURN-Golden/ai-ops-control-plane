"""Qualified native exec adapter. One native sandbox, no user/project config.

The installed official CLI retains its normal account. The adapter never opens
an authentication file. A target-only trust approval is required before exec,
and non-target config bytes are guarded without exporting their contents.
"""
from __future__ import annotations

import argparse
import hashlib
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
import codex_app_server as sdk
import codex_config as config
from common import (AppError, VERSION, REPORT_LIMIT, WORKER_REQUEST_LIMIT, atomic_json,
                    digest, parse_json, read_json)

CLI=Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex')
BINARY=CLI.parent.parent/'CodexCLI.app/Contents/MacOS/codex'
QUALIFIED_BINARY='1180e2d56ea06ec583092acd933345685da3441cb1769a436d76dbf320613e75'
QUALIFIED_WRAPPER='50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8'
ERRORS=(*sdk.ERRORS,'MAC_CODEX_TURN_TIMEOUT',config.ERROR,'MAC_CODEX_TRUST_REQUIRED',
        'MAC_CODEX_RUNTIME_UNQUALIFIED')
FEATURES=sdk.FEATURES
LAYER_TYPES={'packagedDefaults','mdm','system','enterpriseManaged','user','project',
             'sessionFlags','legacyManagedConfigTomlFromFile','legacyManagedConfigTomlFromMdm'}


def qualified_cli():
    sdk.require(sys.platform=='darwin','MAC_CODEX_RUNTIME_UNQUALIFIED')
    for path,expected in ((CLI,QUALIFIED_WRAPPER),(BINARY,QUALIFIED_BINARY)):
        try:actual=hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:raise AppError('MAC_CODEX_RUNTIME_UNQUALIFIED') from None
        sdk.require(actual==expected,'MAC_CODEX_RUNTIME_UNQUALIFIED')
    return str(CLI)


def overrides(request,name,profile,runtime):
    from product_builder import options
    return {**options(request['profile']), 'default_permissions':name,'permissions.'+name:profile,'analytics.enabled':False,
        'model_provider':'openai','web_search':'disabled','mcp_servers':{},'notify':[],
        'shell_environment_policy':{'inherit':'core','set':{
            'PATH':agents.environment().get('PATH','/usr/bin:/bin'),
            'TMPDIR':str(runtime.parent/'provider-tmp'),'GIT_CONFIG_GLOBAL':os.devnull}},
        'sqlite_home':str(runtime/'state'),'log_dir':str(runtime/'log'),
        'projects.'+json.dumps(str(Path(request['checkout']).resolve()))+'.trust_level':'untrusted',
        **{'features.'+key:False for key in FEATURES}}


def flags(values):
    return sum((['-c',key+'='+sdk.toml_inline(value)] for key,value in values.items()),[])


def bound_profile(read,listed,name,profile,expected_path=None,expected_environment=None):
    layers=read.get('layers') or []
    sdk.require(layers and all(v.get('name',{}).get('type') in LAYER_TYPES for v in layers))
    # Official exec excludes user config; project trust is pinned untrusted.
    # System/managed/session layers are still applied and must remain safe.
    applied=[v for v in layers if v['name']['type'] not in ('user','project') and not v.get('disabledReason')]
    sessions=[v['config'] for v in applied if v['name']['type']=='sessionFlags']
    effective=read['config']
    safe=lambda v:not(sdk.legacy_settings(v) or v.get('mcp_servers') or v.get('notify') or
        any(value for key,value in (v.get('hooks') or {}).items() if key!='enabled'))
    checks={'remaining_layers_safe':all(safe(v.get('config',{})) for v in applied),
        'raw_profile_matches':any(v.get('permissions',{}).get(name)==profile for v in sessions),
        'effective_profile_matches':sdk.without_nulls(effective.get('permissions',{}).get(name))==profile,
        'default_profile_matches':effective.get('default_permissions')==name,
        'provider_matches':effective.get('model_provider')=='openai',
        'web_disabled':effective.get('web_search')=='disabled',
        'project_layer_disabled':all(v.get('disabledReason') for v in layers if v['name']['type']=='project'),
        'selection_allowed':any(v.get('id')==name and v.get('allowed') is True and
            v.get('description')==profile['description'] for v in listed.get('data',[]))}
    if expected_path is not None:
        checks['shell_PATH_matches']=(effective.get('shell_environment_policy') or {}).get('set',{}).get('PATH')==expected_path
    if expected_environment is not None:
        checks['shell_environment_matches']=(effective.get('shell_environment_policy') or {}).get('set',{})==expected_environment
    sdk.require(all(checks.values()))
    return checks


class ProcessTree:
    """Observe only descendants of our live CLI. Never signal an observed PID."""
    def __init__(self,pid):self.pid=pid;self.observed={};self.verified=True
    def rows(self):
        try:
            output=subprocess.check_output(['/bin/ps','-axo','uid=,pid=,ppid=,pgid=,lstart='],
                text=True,timeout=2)
            rows={}
            for line in output.splitlines():
                values=line.split(None,4)
                if len(values)==5:
                    uid,pid,parent,group=map(int,values[:4]);rows[pid]=(uid,parent,group,values[4])
            return rows
        except (OSError,ValueError,subprocess.SubprocessError):
            self.verified=False;return {}
    def capture(self):
        rows=self.rows();parents={self.pid};found=True
        while found:
            found=False
            for pid,value in rows.items():
                if value[1] in parents and pid not in parents:
                    if value[0]!=os.getuid():self.verified=False
                    parents.add(pid);self.observed[pid]=(value[0],value[2],value[3]);found=True
    def gone(self):
        rows=self.rows()
        return self.verified and not any(pid in rows and (rows[pid][0],rows[pid][2],rows[pid][3])==identity
            for pid,identity in self.observed.items())


class Driver:
    def __init__(self,request,folder):
        self.request=request;self.folder=Path(folder).resolve()
        self.name,self.profile,self.tmp,self.runtime=sdk.policy(request,self.folder)
        self.values=overrides(request,self.name,self.profile,self.runtime)
        self.stopping=False;self.child=None;self.tree=None;self.snapshot=None;self.protocol=None;self.preflight_shutdown=True
        self.thread=None;self.turn_started=False;self.turn_completed=False;self.final=None
        self.pending=set();self.command_outputs=[]
        self.proof={'attempt_id':request['attempt_id'],'binding':request['binding'],
            'role':request['role'],'checkout':str(Path(request['checkout']).resolve()),
            'profile_id':self.name,'profile_sha256':digest(self.profile),'transport':'native-exec',
            'profile_application':'qualified-native-exec','qualified_binary_sha256':QUALIFIED_BINARY,
            'native_profile':True,'outer_sandbox':False,'model_turn_requested':False,'commands':[],
            'user_config_excluded':True,'project_trust_pinned_untrusted':True,'shutdown_verified':False}
    def save(self):atomic_json(self.folder/'codex-policy-evidence.json',self.proof)
    def stop(self,*_):
        self.stopping=True
        if self.protocol:self.protocol.stopping=True
    def preflight(self,cli,env):
        approval=config.approved(self.request['host_directory'],self.request['checkout'])
        self.snapshot=config.Snapshot(approval['config_file'],self.request['checkout'])
        sdk.require(self.snapshot.value=='trusted','MAC_CODEX_TRUST_REQUIRED')
        self.proof['target_trust_approval_verified']=True;self.save()
        argv=[cli,*flags(self.values),'app-server','--stdio','--strict-config']
        child=subprocess.Popen(argv,cwd=self.request['checkout'],env=env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=sys.stderr.buffer)
        protocol=sdk.Protocol(child,time.monotonic()+30)
        self.protocol=protocol;self.preflight_shutdown=False
        shutdown=False
        try:
            protocol.rpc('initialize',{'clientInfo':{'name':'aiops_mac_exec_preflight','version':VERSION},
                'capabilities':{'experimentalApi':True}});protocol.send({'method':'initialized'})
            read=protocol.rpc('config/read',{'cwd':self.request['checkout'],'includeLayers':True})
            layer=config.user_layer(read)
            sdk.require(Path(layer['name']['file']).resolve()==self.snapshot.path.resolve(),config.ERROR)
            sdk.require((layer['config'].get('projects') or {}).get(self.proof['checkout'],{}).get('trust_level')=='trusted',config.ERROR)
            listed=protocol.rpc('permissionProfile/list',{'cwd':self.request['checkout']})
            self.proof['profile_checks']=bound_profile(read,listed,self.name,self.profile,
                self.values['shell_environment_policy']['set']['PATH'],
                self.values['shell_environment_policy']['set'])
            from product_builder import options
            tuning=options(self.request['profile'])
            sdk.require(all(read['config'].get(k)==v for k,v in tuning.items()))
            if tuning:
                models=protocol.rpc('model/list',{'includeHidden':True})
                matches=[m for m in models.get('data',[]) if m.get('model')==self.request['profile']['model']]
                sdk.require(len(matches)==1 and any(e.get('reasoningEffort')=='high' for e in matches[0].get('supportedReasoningEfforts',[])) and
                    any(t.get('id')==tuning['service_tier'] for t in matches[0].get('serviceTiers',[])))
                self.proof['builder_options_verified']=tuning
            features=protocol.rpc('experimentalFeature/list',{'limit':1000})
            self.proof['feature_checks']={key:any(v.get('name')==key and v.get('enabled') is False
                for v in features['data']) for key in FEATURES};self.save()
            sdk.require(features.get('nextCursor') is None and all(self.proof['feature_checks'].values()))
            account=protocol.rpc('account/read',{'refreshToken':False})
            sdk.require((account.get('account') or {}).get('type')=='chatgpt','PROVIDER_LOGIN_REQUIRED')
            sdk.require(self.snapshot.unchanged(),config.ERROR)
            self.proof.update(loaded_policy_verified=True,active_profile_verified=True,
                surfaces_disabled=True,account_type='chatgpt',preflight_model_calls=0,
                preflight_at=time.time());self.save()
        finally:
            child.stdin.close()
            try:child.wait(timeout=5);shutdown=True
            except subprocess.TimeoutExpired:
                child.terminate()
                try:child.wait(timeout=2);shutdown=True
                except subprocess.TimeoutExpired:pass
            self.protocol=None;self.preflight_shutdown=shutdown
            if shutdown:child.stdout.close()
            sdk.require(shutdown,'MAC_CODEX_STOP_UNVERIFIED')
        sdk.require(child.returncode==0,'MAC_CODEX_PROTOCOL_UNVERIFIED')
        sdk.require(not self.stopping,'USER_STOPPED')
    def scoped_change(self,path):
        sdk.require(isinstance(path,str))
        root=Path(self.request['checkout']).resolve();value=Path(path)
        if not value.is_absolute():value=root/value
        value=value.resolve();sdk.require(root in value.parents and self.request['role']=='builder')
        sdk.require(value.relative_to(root).parts[0] not in ('.git','.agents','.codex','.aws'))
    def event(self,value):
        kind=value.get('type');sdk.require(not self.turn_completed,'MAC_CODEX_PROTOCOL_UNVERIFIED')
        if kind=='thread.started':
            current=value.get('thread_id')
            sdk.require(self.thread is None and isinstance(current,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',current),'MAC_CODEX_PROTOCOL_UNVERIFIED')
            self.thread=current;self.proof['thread_id']=current
        elif kind=='turn.started':
            sdk.require(self.thread and not self.turn_started,'MAC_CODEX_PROTOCOL_UNVERIFIED');self.turn_started=True
        elif kind=='turn.completed':
            sdk.require(self.turn_started and self.final is not None,'MAC_CODEX_PROTOCOL_UNVERIFIED');self.turn_completed=True
        elif kind in ('item.started','item.updated','item.completed'):
            item=value.get('item') or {};item_kind=item.get('type')
            sdk.require(self.thread,'MAC_CODEX_PROTOCOL_UNVERIFIED')
            if item_kind=='command_execution':
                ident=item.get('id');sdk.require(isinstance(ident,str) and self.turn_started,'MAC_CODEX_PROTOCOL_UNVERIFIED')
                command=item.get('command');sdk.require(isinstance(command,str),'MAC_CODEX_PROTOCOL_UNVERIFIED')
                record={'id':ident,'event':kind,'command_sha256':digest(command),'exit_code':item.get('exit_code'),
                    'status':item.get('status'),'profile_sha256':self.proof['profile_sha256']}
                self.proof['commands'].append(record)
                if type(item.get('exit_code')) is int and item.get('status') in ('completed','failed'):
                    self.pending.discard(ident)
                    output=item.get('aggregated_output') or '';sdk.require(isinstance(output,str),'MAC_CODEX_PROTOCOL_UNVERIFIED')
                    self.command_outputs.append(dict(record,command=command,output=output[:262144],
                        output_sha256=digest(output),output_truncated=len(output)>262144))
                    sdk.require(len(json.dumps(self.command_outputs).encode())<=8388608,'MAC_CODEX_PROTOCOL_UNVERIFIED')
                    atomic_json(self.folder/'codex-command-evidence.json',self.command_outputs)
                else:self.pending.add(ident)
                sdk.require(len(self.proof['commands'])<=1000,'MAC_CODEX_PROTOCOL_UNVERIFIED')
            elif item_kind=='file_change':
                for change in item.get('changes') or []:self.scoped_change(change.get('path'))
            elif item_kind=='agent_message':
                if kind=='item.completed':self.final=item.get('text')
            elif item_kind=='error':
                # This qualified CLI reports a metadata warning as an item.
                # It is not authorization to select a different model/provider.
                message=item.get('message') or ''
                sdk.require(message.startswith('Model metadata for `') and 'Defaulting to fallback metadata' in message,
                    'MAC_CODEX_PROTOCOL_UNVERIFIED')
            else:sdk.require(item_kind in ('reasoning','todo_list'),'MAC_CODEX_PERMISSION_REQUIRED')
        elif kind in ('error','turn.failed'):
            raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED')
        else:raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED')
        self.save()
    def execute(self,cli,env):
        model=['--model',self.request['profile']['model']] if self.request['profile']['model'] else []
        argv=[cli,'-a','never',*flags(self.values),'exec','--ignore-user-config','--ephemeral',
            '--strict-config','--json','--color','never','--output-schema',str(self.folder/'schema.json'),
            '--output-last-message',str(self.folder/'native-final.json'),*model,'-']
        sdk.require(self.snapshot.unchanged(),config.ERROR)
        self.proof.update(model_turn_requested=True,exec_argv_sha256=digest(argv));self.save()
        self.child=subprocess.Popen(argv,cwd=self.request['checkout'],env=env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=sys.stderr.buffer)
        self.tree=ProcessTree(self.child.pid)
        # A private file supplies the assignment, never an adapter stdin pipe.
        runtime_instruction=('AIOPS native runtime: use exec_command with login=false and direct commands. '
            'Login shells reset the application PATH; do not wrap commands in zsh/bash -l. '
            'Host-provided tool paths do not grant additional filesystem access.\n')
        assignment=(runtime_instruction+self.request['prompt']).encode();offset=0;buffer=b''
        os.set_blocking(self.child.stdin.fileno(),False)
        deadline=time.monotonic()+self.request['timeout_seconds']
        eof=False
        while not eof:
            self.tree.capture()
            sdk.require(not self.stopping,'USER_STOPPED')
            sdk.require(time.monotonic()<deadline,'MAC_CODEX_TURN_TIMEOUT')
            inputs=[self.child.stdout];writes=[self.child.stdin] if offset<len(assignment) else []
            ready,writable,_=select.select(inputs,writes,[],.2)
            if writable:
                try:offset+=os.write(self.child.stdin.fileno(),assignment[offset:offset+65536])
                except BrokenPipeError:raise AppError('MAC_CODEX_PROTOCOL_UNVERIFIED') from None
            if offset==len(assignment) and not self.child.stdin.closed:self.child.stdin.close()
            if ready:
                chunk=os.read(self.child.stdout.fileno(),65536)
                if not chunk:eof=True
                else:buffer+=chunk
                sdk.require(len(buffer)<=16777216,'MAC_CODEX_PROTOCOL_UNVERIFIED')
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1)
                    if not line.strip():continue
                    value=parse_json(line.decode(),16777216);sdk.require(isinstance(value,dict),'MAC_CODEX_PROTOCOL_UNVERIFIED')
                    # The bound official stream remains private to the worker.
                    print(json.dumps(value),flush=True);self.event(value)
        sdk.require(not buffer.strip(),'MAC_CODEX_PROTOCOL_UNVERIFIED')
        self.child.wait(timeout=3)
        sdk.require(self.child.returncode==0 and self.turn_completed and not self.pending,'MAC_CODEX_PROTOCOL_UNVERIFIED')
        final=agents.json_answer(self.final);report=agents.validate_report(final)
        sdk.require(agents.json_answer(agents.read_output(self.folder/'native-final.json',REPORT_LIMIT))==report,
            'MAC_CODEX_PROTOCOL_UNVERIFIED')
        return report
    def shutdown(self):
        if not self.preflight_shutdown:return False
        if self.child is None:return True
        interrupted=self.child.poll() is None
        if interrupted:self.child.send_signal(signal.SIGINT)
        try:self.child.wait(timeout=7)
        except subprocess.TimeoutExpired:return False
        if not self.child.stdin.closed:self.child.stdin.close()
        self.child.stdout.close()
        if self.tree:
            self.tree.capture()
            until=time.monotonic()+1
            while not self.tree.gone() and time.monotonic()<until:time.sleep(.05)
            if not self.tree.gone():return False
            self.proof.update(observed_descendants=len(self.tree.observed),descendants_quiescent=True,
                cli_sigint_requested=interrupted)
        if self.pending and not(interrupted and self.tree and self.tree.observed):return False
        return True


def run(folder):
    request=read_json(Path(folder)/'request.json',WORKER_REQUEST_LIMIT)
    sdk.require(request.get('profile',{}).get('provider')=='codex')
    driver=Driver(request,folder);driver.save();report=None;error=None;shutdown=False
    signal.signal(signal.SIGTERM,driver.stop);signal.signal(signal.SIGINT,driver.stop)
    try:
        cli=qualified_cli()
        env=agents.environment(request['profile'],request['role']);env.update(TMPDIR=str(driver.tmp),PYTHONDONTWRITEBYTECODE='1')
        driver.preflight(cli,env);report=driver.execute(cli,env)
    except Exception as exc:
        error=exc.code if isinstance(exc,AppError) else 'MAC_CODEX_PROTOCOL_UNVERIFIED'
    finally:
        try:
            shutdown=driver.shutdown()
            if driver.snapshot:
                driver.snapshot.verify(config.Snapshot(driver.snapshot.path,request['checkout']))
                driver.proof['non_target_config_bytes_preserved']=True
        except AppError as exc:error=exc.code;report=None
        except Exception:error='MAC_CODEX_STOP_UNVERIFIED';shutdown=False;report=None
        if not shutdown:error='MAC_CODEX_STOP_UNVERIFIED';report=None
        driver.proof.update(shutdown_verified=shutdown,error=error);driver.save()
        if error:
            atomic_json(Path(folder)/'codex-adapter-error.json',{'attempt_id':request['attempt_id'],
                'binding':request['binding'],'code':error if error in ERRORS else 'MAC_CODEX_PROTOCOL_UNVERIFIED',
                'shutdown_verified':shutdown})
        elif report is not None:atomic_json(Path(folder)/'last-message.json',report)
    return 0 if error is None and shutdown and report is not None else 1


if __name__=='__main__':
    sys.dont_write_bytecode=True
    parser=argparse.ArgumentParser();parser.add_argument('--attempt',required=True)
    sys.exit(run(parser.parse_args().attempt))
