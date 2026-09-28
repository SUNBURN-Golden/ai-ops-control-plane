#!/usr/bin/env python3
"""Event-driven, display-only Slack projection. Never dispatch or approve work."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import re
from urllib.parse import urlparse

_spec=importlib.util.spec_from_file_location('display_flow',Path(__file__).with_name('control_plane_flow.py'))
flow=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(flow)
require=flow.require
EVENT_TYPE='astra_graph_projection_v1'
STATES={
 'IMPLEMENTATION_REQUIRED':'구현 요청 대기','IMPLEMENTING':'구현 진행',
 'CORRECTION_REQUIRED':'같은 작성자에게 수정 요청','VERIFICATION_PENDING':'검증 대기',
 'REVIEW_REQUIRED':'독립 검토 대기','AUDIT_REQUIRED':'Astra 감사 대기',
 'DECISION_REQUIRED':'계약 결정 필요','MERGE_CANDIDATE':'User 머지 검토 대기',
 'DONE':'완료','BLOCKED':'차단','WAITING_DEPENDENCY':'선행 작업 대기'}
REASONS={
 'task closed':'작업 이슈가 닫혀 있음',
 'host/task ownership not verified':'호스트·작업 소유권 미확인',
 'repository prerequisites missing':'제품 선행조건 미충족',
 'unresolved blocker':'해결되지 않은 차단 사유 있음',
 'writer declared blocked/stalled or mechanical budget reached':'작성자 중단·정체 또는 비용 한도',
 'owner session mismatch/unconfirmed':'작성자 세션 근거 미확인',
 'writer designation changed; reconcile explicit handoff':'작성자 변경 인계 미확인',
 'diff exceeds allowed/locked scope':'허용 변경 범위 초과',
 'stale/untrusted CI':'CI SHA 또는 출처 불일치',
 'review lane unqualified':'검토 레인 미검증',
 'auditor unqualified':'감사 레인 미검증',
 'review delivery/termination unconfirmed':'검토 세션 전달·종료 미확인'}


def short(value,limit=120):
    # No transcript, model prose, mention parsing or multiline field injection.
    return ' '.join(str(value).split())[:limit]


def github_url(value,repo):
    if not isinstance(value,str):return None
    u=urlparse(value)
    if (u.scheme=='https' and u.netloc=='github.com' and u.path.startswith('/'+repo+'/') and
        not any(c in value for c in '<>|\n\r\t ')):
        return value
    return None


def timestamp(value):return isinstance(value,str) and re.fullmatch(r'[0-9]{10,}\.[0-9]{6}',value)


def initialize(store):
    # Explicit schema extension of the existing protected flow DB only.
    with store.transaction() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS graph_slack_display (
            task TEXT PRIMARY KEY, binding TEXT NOT NULL, state TEXT NOT NULL,
            message_ts TEXT, confirmed_digest TEXT, pending_digest TEXT)''')


def model(runner,node_id):
    plan,states,snapshots=runner.assess()
    node=next((n for n in plan['nodes'] if n['id']==node_id),None)
    require(node is not None,'unknown display node')
    state=states[node_id];snapshot=snapshots.get(node_id,{})
    task=flow.canonical([node['repository'],node['task_id']])
    owner=runner.store.owners().get(task,{})
    delivery=None
    if state.get('action'):
        with runner.store.transaction() as db:
            row=db.execute('SELECT state FROM outbox WHERE id=?',(state['action']['request_id'],)).fetchone()
            delivery=row['state'] if row else 'NOT_STARTED'
    # Read current facts only. Never store.accept/begin_event, terminal, advance or route.
    view={'graph':plan['graph_id'],'graph_revision':plan['revision'],'plan_digest':flow.digest(plan),
          'node':node_id,'repository':node['repository'],'task_id':node['task_id'],
          'task_revision':node['revision'],'task_pointer':node['task_pointer'],
          'plan_pointer':plan['approval_pointer'],'head':snapshot.get('head'),
          'pr_pointer':github_url(snapshot.get('pr_pointer'),node['repository']),
          'state':state['state'],'reason':REASONS.get(state.get('reason'),'현재 근거 확인 필요')
                 if state['state']=='BLOCKED' else None,
          'owner':owner.get('writer',node['writer']['identity']),'planned_owner':node['writer']['identity'],
          'session':owner.get('session_id'),
          'delivery':delivery,'dependencies':[
              {'node':d['node'],'input':d['input'],'state':states[d['node']]['state']}
              for d in node['dependencies']],
          'merge_authorized':False}
    return node,view


def render(view):
    state=view['state'];require(state in STATES,'unknown display state')
    head=view['head'] if re.fullmatch(r'[0-9a-f]{40}',view.get('head') or '') else 'UNVERIFIED'
    lines=['[TASK GRAPH — 표시 전용]',
           'Graph: '+short(view['graph'])+' / revision '+short(view['graph_revision']),
           'Project: '+short(view['repository']),
           'Task: '+short(view['task_id'])+' / revision '+short(view['task_revision']),
           'State: '+state+' — '+STATES[state],
           'HEAD: '+head,
           'Owner: '+short(view['owner'])+' / Session: '+short(view.get('session') or 'UNCONFIRMED')]
    if view.get('planned_owner')!=view['owner']:lines.append('Planned owner: '+short(view['planned_owner'])+' (인계 미완료)')
    if view.get('delivery'):lines.append('Delivery: '+view['delivery']+' (완료·승인과 다름)')
    if view.get('reason'):lines.append('Blocker: '+view['reason'])
    for dep in view['dependencies'][:5]:
        lines.append('Needs: '+short(dep['node'])+' / '+short(dep['input'])+' / '+dep['state'])
    if len(view['dependencies'])>5:lines.append('추가 의존관계는 GitHub 그래프 참조')
    human={'MERGE_CANDIDATE':'현재 GitHub 근거 확인 후 User가 머지 결정',
           'DECISION_REQUIRED':'Astra 분석 후 User 결정·GitHub 기록',
           'BLOCKED':'GitHub 차단 근거 확인','DONE':'후속 작업은 GitHub 참조'}
    lines.append('Human action: '+human.get(state,'없음 — 다음 작업 이벤트 대기'))
    lines.append('GitHub 정본 / 이 메시지는 승인·실행 명령이 아닙니다.')
    text='\n'.join(lines)
    links=[('Task',view['task_pointer']),('Graph',view['plan_pointer'])]
    if view.get('pr_pointer'):links.append(('PR',view['pr_pointer']))
    for _,url in links:require(flow.github_pointer(url) and not any(c in url for c in '<>|\n\r\t '),'unsafe link')
    # Escape fallback even with mrkdwn disabled. Blocks never interpret field text.
    fallback=(text+'\n'+'\n'.join(label+': '+url for label,url in links))
    fallback=fallback.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    require(len(text)<=3000 and len(fallback)<=4000,'Slack card exceeds display limits')
    return {'text':fallback,'blocks':[
        {'type':'section','block_id':EVENT_TYPE,'text':{'type':'plain_text','text':text,'emoji':False}},
        {'type':'context','elements':[{'type':'mrkdwn','text':' | '.join('<'+url+'|'+label+'>' for label,url in links)}]}],
        'parse':'none','link_names':False,'mrkdwn':False}


class Display:
    def __init__(self,runner,api,config):self.runner,self.store,self.api,self.config=runner,runner.store,api,config

    def refresh(self,node_id,*,dry_run=False):
        config=self.config
        if not dry_run:require(config.get('enabled') is True and config.get('source_audit')=='PASS' and
                               flow.github_pointer(config.get('approval_pointer')),'Slack display disabled')
        node,view=model(self.runner,node_id);payload=render(view)
        if dry_run:return {'state':'PREVIEW','payload':payload,'side_effects':False}
        target=config['targets'][node_id]
        require(target['repository']==node['repository'] and target['task_id']==node['task_id'],
                'display target task mismatch')
        require(node.get('slack_thread')=={k:target[k] for k in ('team_id','channel_id','thread_ts')},
                'Slack thread must be pinned in approved GitHub plan')
        require(re.fullmatch(r'T[A-Z0-9]+',target['team_id']) and
                re.fullmatch(r'[CG][A-Z0-9]+',target['channel_id']) and timestamp(target['thread_ts']),
                'invalid configured Slack target')
        require(isinstance(config.get('request_channel_ids'),list) and config['request_channel_ids'] and
                all(isinstance(c,str) and re.fullmatch(r'[CG][A-Z0-9]+',c) for c in config['request_channel_ids']),
                'explicit agent request channel exclusion list required')
        require(target['channel_id'] not in config['request_channel_ids'],
                'status must not enter an agent request channel')
        binding={**{k:target[k] for k in ('team_id','channel_id','thread_ts')},
                 'publisher_user_id':config['publisher_user_id'],'task_pointer':node['task_pointer']}
        task=flow.canonical([node['repository'],node['task_id']]);bound=flow.canonical(binding)
        content_digest=flow.digest(view)
        # Single in-flight update per canonical task, including across graph revisions.
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM graph_slack_display WHERE task=?',(task,)).fetchone()
            if row:
                require(row['binding']==bound,'display binding changed; explicit reconciliation required')
                if row['state'] in ('SUBMITTING','UNKNOWN'):return {'state':row['state'],'sent':False}
                if row['confirmed_digest']==content_digest:return {'state':'UNCHANGED','sent':False}
            else:
                db.execute("INSERT INTO graph_slack_display VALUES (?,?,'IDLE',NULL,NULL,NULL)",(task,bound))
            message_ts=row['message_ts'] if row else None
            db.execute("UPDATE graph_slack_display SET state='SUBMITTING',pending_digest=? WHERE task=?",
                       (content_digest,task))
        attempted=False
        try:
            identity=self.api.call('POST','auth.test',{},slack=True)
            require(identity.get('team_id')==target['team_id'] and
                    identity.get('user_id')==config['publisher_user_id'] and identity.get('bot_id'),
                    'wrong Slack publisher/workspace')
            fresh_node,fresh=model(self.runner,node_id)
            require(flow.digest(fresh)==content_digest and fresh_node.get('slack_thread')==node.get('slack_thread'),
                    'display snapshot changed before send')
            outgoing=dict(payload,channel=target['channel_id'])
            if message_ts:
                outgoing['ts']=message_ts;outgoing.pop('mrkdwn',None);method='chat.update'
            else:
                outgoing.update(thread_ts=target['thread_ts'],reply_broadcast=False,
                    unfurl_links=False,unfurl_media=False,
                    metadata={'event_type':EVENT_TYPE,'event_payload':{'display_only':True}})
                method='chat.postMessage'
            attempted=True
            receipt=self.api.call('POST',method,outgoing,slack=True)
            require(receipt.get('ok') is True and receipt.get('channel')==target['channel_id'] and
                    timestamp(receipt.get('ts')) and (not message_ts or receipt['ts']==message_ts),
                    'Slack display receipt ambiguous')
            with self.store.transaction() as db:
                db.execute("UPDATE graph_slack_display SET state='CONFIRMED',message_ts=?,confirmed_digest=?,pending_digest=NULL WHERE task=?",
                           (receipt['ts'],content_digest,task))
            return {'state':'UPDATED' if message_ts else 'POSTED','channel':target['channel_id'],
                    'message_ts':receipt['ts'],'sent':True,'merge_authorized':False}
        except Exception:
            # No retry even for updates: a late old request must not overwrite a newer card.
            with self.store.transaction() as db:
                db.execute('UPDATE graph_slack_display SET state=? WHERE task=?',
                           ('UNKNOWN' if attempted else 'IDLE',task))
            return {'state':'UNKNOWN' if attempted else 'NOT_SENT','sent':attempted,'merge_authorized':False}
