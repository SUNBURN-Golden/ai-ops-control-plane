#!/usr/bin/env python3
"""One explicit event -> current GitHub facts -> one qualified adapter action.

Plan and observation bindings are protected deployment configuration. Model text
cannot create those bindings. This CLI has no merge, polling or activation command.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess

def sibling(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

graph=sibling('control_plane_graph');flow=graph.flow
gateway=sibling('control_plane_flow_gateway')
graph_slack=sibling('control_plane_graph_slack')


def protected(path):
    path=Path(path).absolute();info=path.lstat()
    flow.require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and
                 info.st_uid==os.geteuid() and not info.st_mode & 0o077 and info.st_nlink==1,
                 'protected owner-only configuration required')
    return path


class GithubGraphPorts:
    def __init__(self,api,policy):self.api,self.policy=api,policy

    def record(self,binding,marker,*,pin=False):
        repo=binding['repository']
        flow.require(repo in graph.REPOSITORIES,'unregistered record repository')
        data=self.api.call('GET',f"repos/{repo}/issues/comments/{int(binding['comment_id'])}")
        flow.require(data.get('user',{}).get('login')==binding['actor'] and
                     data.get('issue_url')==f"https://api.github.com/repos/{repo}/issues/{binding['issue']}",
                     'record actor/task mismatch')
        body=data.get('body','')
        if pin:flow.require(hashlib.sha256(body.encode()).hexdigest()==binding['sha256'],'approval changed')
        return gateway.unwrap(body,marker),data['html_url']

    def plan(self):
        binding=self.policy['plan']
        flow.require(binding['actor'] in self.policy['user_actors'],'plan approval identity not authorized')
        plan,pointer=self.record(binding,'<!-- ASTRA_GRAPH_PLAN_V1 -->',pin=True)
        flow.require(plan.get('approval_pointer')==pointer,'approval pointer mismatch')
        graph.validate_plan(plan);return plan

    def observation(self,node):
        binding=self.policy['observations'][node['id']]
        flow.require(binding['repository']==node['repository'] and binding['issue']==node['issue'] and
                     binding['actor'] in self.policy['collector_actors'], 'collector registration mismatch')
        value,_=self.record(binding,'<!-- ASTRA_GRAPH_OBSERVATION_V1 -->')
        flow.require(value.get('repository')==node['repository'] and value.get('task_id')==node['task_id'] and
                     value.get('revision')==node['revision'],'observation task mismatch')
        return value

    def load(self,node,plan):
        # Collector records provider identity/lifecycle and deterministic local gates.
        # PR, changed paths, CI, and semantic review authority are read independently.
        s=self.observation(node);repo=node['repository'];prefix=f'repos/{repo}/'
        task=self.api.call('GET',prefix+f"issues/{node['issue']}")
        flow.require(hashlib.sha256((task.get('body') or '').encode()).hexdigest()==node['issue_body_sha256'],
                     'task body changed; refresh approved plan')
        s.update(task_open=task.get('state')=='open',task_pointer=task['html_url'],base=node['base'],
                 task_digest=node['issue_body_sha256'],policy_revision=flow.digest(plan),
                 required_checks=node['required_checks'],audit_floor=node['audit_floor'],astra_gate=node['astra_gate'])
        flow.require(s.get('plan_digest')==flow.digest(plan),'collector has not accepted this graph revision')
        if not s.get('pr_number'):
            s.update(pr_state='not_created',head=node['base'],draft=True,checks=[]);return s
        number=s['pr_number'];flow.require(type(number) is int and number>0,'invalid PR')
        pr=self.api.call('GET',prefix+f'pulls/{number}')
        flow.require(pr['base']['repo']['full_name']==repo and pr['base']['sha']==node['base'],
                     'PR base/repository changed')
        head=pr['head']['sha']
        flow.require(s.get('head')==head,'collector observation is stale for current PR HEAD')
        s.update(pr_state='merged' if pr.get('merged') else pr['state'],head=head,draft=pr['draft'],
                 pr_pointer=pr['html_url'],merge_sha=pr.get('merge_commit_sha'))
        if pr.get('merged'):
            s['merge_actor_verified']=pr.get('merged_by',{}).get('login') in self.policy['user_actors']
            return s
        files=self.api.pages(prefix+f'pulls/{number}/files')
        s['paths_complete']=len(files)==pr.get('changed_files')
        s['changed_paths']=list({f['filename'] for f in files}|
                                {f['previous_filename'] for f in files if f.get('previous_filename')})
        s['author_identities']=sorted(set(s['author_identities']+[pr['user']['login']]))
        s['blockers']=list(s.get('blockers',[]))
        if pr.get('mergeable') is not True:s['blockers'].append('mergeability_not_verified')
        runs=self.api.pages(prefix+'actions/runs?head_sha='+head);s['checks']=[]
        for expected in node['required_checks']:
            candidates=[r for r in runs if r.get('workflow_id')==expected['workflow_id'] and
                        r.get('head_sha')==head and r.get('event') in expected['events'] and
                        (r.get('event')!='pull_request' or any(p.get('number')==number for p in r.get('pull_requests',[])))]
            if not candidates:continue
            run=max(candidates,key=lambda r:(r['id'],r.get('run_attempt',1)))
            suite=self.api.call('GET',prefix+f"check-suites/{run['check_suite_id']}")
            path=expected['path']
            flow.require(re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml',path),'unsafe workflow path')
            blob=self.api.call('GET',prefix+f'contents/{path}?ref={head}')
            s['checks'].append(dict(name=expected['name'],workflow_id=run['workflow_id'],
                app_id=suite.get('app',{}).get('id'),head=head,status=run['status'],conclusion=run['conclusion'],
                source_verified=blob.get('sha')==expected['workflow_blob'] and run.get('path','').split('@')[0]==path,
                evidence_pointer=run['html_url']))
        # Authenticated GitHub review IDs and confirmed provider sessions remain separate.
        native=self.api.pages(prefix+f'pulls/{number}/reviews');s['reviews']={};s['audit']=None
        lanes=[('review',r) for r in node['reviewers']]
        if node.get('auditor'):lanes.append(('audit',node['auditor']))
        for phase,lane in lanes:
            matches=[]
            decisive=[r for r in native if r.get('user',{}).get('login')==lane['identity'] and
                      r.get('commit_id')==head and r.get('state') in ('APPROVED','CHANGES_REQUESTED','DISMISSED')]
            latest=max(decisive,key=lambda r:r['id']) if decisive else None
            if latest and (latest['state']=='DISMISSED' or not latest.get('body','').startswith('<!-- ASTRA_GRAPH_RESULT_V1 -->\n')):
                s['blockers'].append('native_review_withdrawn:'+lane['identity'])
                continue
            for review in native:
                if latest and review['id']<latest['id']:continue
                if (review.get('user',{}).get('login')!=lane['identity'] or
                    review.get('commit_id')!=head or review.get('state')=='DISMISSED' or
                    not review.get('body','').startswith('<!-- ASTRA_GRAPH_RESULT_V1 -->\n')):continue
                parsed=gateway.unwrap(review['body'],'<!-- ASTRA_GRAPH_RESULT_V1 -->')
                if parsed.get('phase')!=phase or parsed.get('subject')!=flow.subject(s):continue
                if parsed.get('result') in ('PASS','PASS_WITH_NOTES'):
                    flow.require(review['state']=='APPROVED' and latest is not None and
                                 latest['id']==review['id'],'PASS requires latest native approval')
                parsed['authenticated_actor']=review['user']['login']
                parsed['evidence_pointer']=review['html_url']
                # Only the protected collector may attest provider session/read-only boundary.
                delivered=s.get('review_receipts',{}).get(parsed.get('request_id'),{})
                parsed['confirmed_session_id']=delivered.get('session_id')
                parsed['read_only_verified']=delivered.get('read_only_verified') is True
                matches.append(parsed)
            flow.require(len(matches)<=1,'ambiguous native review results')
            if matches:
                if phase=='audit':s['audit']=matches[0]
                else:s['reviews'][lane['identity']]=matches[0]
        return s

    def terminals(self):
        plan=self.plan();items=[]
        for node in plan['nodes']:
            s=self.observation(node)
            flow.require(s.get('plan_digest')==flow.digest(plan),'terminal collector revision changed')
            for item in s.get('terminal_receipts',[]):
                flow.require(item.get('terminal_verified') is True and flow.github_pointer(item.get('evidence_pointer')),
                             'terminal process evidence missing')
                items.append(item)
        return items

    def project(self,a):
        plan=self.plan();node=next(n for n in plan['nodes'] if n['id']==a['node_id'])
        body='<!-- ASTRA_GRAPH_ACTION_V1 -->\n'+flow.canonical({k:v for k,v in a.items()})
        result=self.api.call('POST',f"repos/{node['repository']}/issues/{node['issue']}/comments",{'body':body})
        flow.require(flow.github_pointer(result.get('html_url')),'projection missing')
        return dict(request_id=a['request_id'],accepted=True,pointer=result['html_url'])

    def route(self,a):
        flow.require(self.policy.get('authority_mode') != 'INDEPENDENT_HOST',
                     'independent-host graph bridge not qualified; use the local-host controller admission')
        # A qualified host adapter enforces host-global admission and per-task isolation.
        # This is not a CLI-help/probe adapter from PR22 and never installs one implicitly.
        config=self.policy['adapters'][a['lane']]
        flow.require(config.get('qualified') is True and flow.github_pointer(config.get('evidence_pointer')),
                     'live execution adapter unqualified')
        flow.require(a['kind'] in config['operations'],'adapter operation not qualified')
        binary=protected(config['executable'])
        flow.require(hashlib.sha256(binary.read_bytes()).hexdigest()==config['sha256'],'adapter changed')
        run=subprocess.run([str(binary),a['kind'].lower()],input=flow.canonical(a),text=True,
             stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30,check=False,cwd='/',
             env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'C.UTF-8'})
        flow.require(run.returncode==0 and len(run.stdout)<=65536,'adapter outcome unknown')
        result=flow.decode(run.stdout)
        flow.require(result.get('request_id')==a['request_id'] and result.get('accepted') is True,
                     'adapter receipt mismatch')
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init','assess','advance','display'))
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--event-id')
    parser.add_argument('--node-id')
    parser.add_argument('--dry-run',action='store_true',help='display preview only; never execute work')
    args=parser.parse_args();os.umask(0o077)
    try:
        flow.require(not args.dry_run or args.command=='display','dry-run is display-only')
        policy=flow.decode(protected(args.policy).read_bytes())
        flow.require(policy.get('schema_version')==1,'policy version')
        store=graph.GraphStore(policy['ledger_path']) # existing authoritative host flow DB
        if args.command=='init':
            store.initialize_graph();graph_slack.initialize(store)
            result={'state':'GRAPH_SCHEMA_READY','activated':False}
        else:
            api=gateway.Api(os.environ.get('ASTRA_GRAPH_GITHUB_TOKEN'),os.environ.get('ASTRA_GRAPH_SLACK_TOKEN'))
            ports=GithubGraphPorts(api,policy);runner=graph.GraphRunner(store,ports)
            if args.command=='assess':
                _,states,_=runner.assess();result={'nodes':states,'side_effects':False}
            elif args.command=='display':
                flow.require(bool(args.node_id),'display node required')
                result=graph_slack.Display(runner,api,policy.get('slack_display',{})).refresh(args.node_id,dry_run=args.dry_run)
            else:
                flow.require(policy.get('enabled') is True and policy.get('source_audit')=='PASS' and
                             flow.github_pointer(policy.get('activation_pointer')),'graph execution disabled')
                flow.require(isinstance(args.event_id,str) and re.fullmatch(r'[A-Za-z0-9:_-]{8,160}',args.event_id),
                             'explicit event ID required')
                result=runner.advance(args.event_id)
                display_node=result.get('node_id') or args.node_id
                if policy.get('slack_display',{}).get('enabled') is True and display_node:
                    try:
                        result['slack_display']=graph_slack.Display(runner,api,policy['slack_display']).refresh(display_node)
                    except Exception:
                        # Display outage cannot alter/retry the already recorded work action.
                        result['slack_display']={'state':'BLOCKED','merge_authorized':False}
        print(flow.canonical(result));return 0
    except (flow.FlowError,gateway.flow.FlowError,graph_slack.flow.FlowError,OSError,KeyError,ValueError,TypeError,subprocess.SubprocessError):
        print(flow.canonical({'state':'BLOCKED','merge_authorized':False}));return 2


if __name__=='__main__':raise SystemExit(main())
