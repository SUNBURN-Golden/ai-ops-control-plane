import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import control_plane_graph as g
import control_plane_graph_slack as display
from test_control_plane_graph import fixture,Ports,POINTER


class Slack:
    def __init__(self):self.calls=[];self.hook=None
    def call(self,method,path,body,*,slack=False):
        assert slack and method=='POST'
        self.calls.append((path,copy.deepcopy(body)))
        if self.hook:
            value=self.hook(path,body)
            if value is not None:return value
        if path=='auth.test':return dict(ok=True,team_id='T123',user_id='U123',bot_id='B123')
        return dict(ok=True,channel='C123',ts=body.get('ts','1790550001.123456'))
    def writes(self):return [c for c in self.calls if c[0]!='auth.test']


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=g.GraphStore(Path(self.tmp.name)/'flow.db',initialize=True)
        self.store.initialize_graph();display.initialize(self.store)
        self.plan,self.s=fixture()
        self.thread=dict(team_id='T123',channel_id='C123',thread_ts='1790550000.123456')
        self.plan['nodes'][0]['slack_thread']=self.thread.copy()
        self.s.update(plan_digest=g.digest(self.plan),policy_revision=g.digest(self.plan))
        self.ports=Ports(self.plan,self.s);self.runner=g.GraphRunner(self.store,self.ports)
        self.api=Slack();self.config=dict(enabled=True,source_audit='PASS',approval_pointer=POINTER,
            publisher_user_id='U123',request_channel_ids=['C999'],targets={'unit':dict(self.thread,
                repository=self.s['repository'],task_id=self.s['task_id'])})
        self.display=display.Display(self.runner,self.api,self.config)
    def refresh(self):return self.display.refresh('unit')
    def change(self):self.s['blockers']=['do not expose transcript/secret']

    def test_create_once_then_update_same_message_only_on_change(self):
        self.assertEqual(self.refresh()['state'],'POSTED')
        first=self.api.writes()[0];self.assertEqual(first[0],'chat.postMessage')
        self.assertEqual(first[1]['thread_ts'],self.thread['thread_ts'])
        self.assertFalse(first[1]['reply_broadcast']);self.assertFalse(first[1]['unfurl_links'])
        self.assertEqual(self.refresh()['state'],'UNCHANGED');self.assertEqual(len(self.api.calls),2)
        self.change();self.assertEqual(self.refresh()['state'],'UPDATED')
        update=self.api.writes()[-1];self.assertEqual(update[0],'chat.update')
        self.assertEqual(update[1]['ts'],'1790550001.123456');self.assertNotIn('thread_ts',update[1])
        self.assertNotIn('secret',str(update));self.assertNotIn('transcript',str(update))

    def test_display_never_dispatches_or_mutates_work_control(self):
        self.refresh();self.change();self.refresh()
        self.assertEqual(self.ports.sent,[]);self.assertEqual(self.ports.projected,[])
        self.assertEqual(self.store.owners(),{})
        with self.store.transaction() as db:
            for table in ('inbox','outbox','graph_slots'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_preview_requires_no_slack_access_and_writes_nothing(self):
        self.config['enabled']=False
        r=self.display.refresh('unit',dry_run=True)
        self.assertEqual(r['state'],'PREVIEW');self.assertEqual(self.api.calls,[])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM graph_slack_display').fetchone()[0],0)

    def test_disabled_missing_audit_or_approval_never_sends(self):
        for key,value in [('enabled',False),('source_audit','PENDING'),('approval_pointer',None)]:
            c=copy.deepcopy(self.config);c[key]=value
            with self.assertRaises(display.flow.FlowError):display.Display(self.runner,self.api,c).refresh('unit')
        self.assertEqual(self.api.calls,[])

    def test_post_loss_freezes_across_restart_and_head_change(self):
        def loss(path,body):
            if path=='chat.postMessage':raise TimeoutError('do not expose transport details')
        self.api.hook=loss
        self.assertEqual(self.refresh()['state'],'UNKNOWN');self.change()
        restarted=display.Display(self.runner,self.api,self.config)
        self.assertEqual(restarted.refresh('unit')['state'],'UNKNOWN')
        self.assertEqual(len(self.api.writes()),1)

    def test_update_loss_blocks_newer_write(self):
        self.refresh();self.change()
        def loss(path,body):
            if path=='chat.update':raise TimeoutError()
        self.api.hook=loss;self.assertEqual(self.refresh()['state'],'UNKNOWN')
        self.s['blockers']=[]
        self.assertEqual(self.refresh()['state'],'UNKNOWN');self.assertEqual(len(self.api.writes()),2)

    def test_concurrent_refresh_cannot_send_second_message(self):
        overlapping=[]
        def overlap(path,body):
            if path=='chat.postMessage':overlapping.append(self.refresh())
        self.api.hook=overlap
        self.assertEqual(self.refresh()['state'],'POSTED')
        self.assertEqual(overlapping,[dict(state='SUBMITTING',sent=False)])
        self.assertEqual(len(self.api.writes()),1)

    def test_stale_snapshot_is_not_posted(self):
        def change_on_auth(path,body):
            if path=='auth.test':self.change()
        self.api.hook=change_on_auth
        self.assertEqual(self.refresh()['state'],'NOT_SENT');self.assertEqual(self.api.writes(),[])
        self.api.hook=None;self.assertEqual(self.refresh()['state'],'POSTED')

    def test_wrong_publisher_workspace_rejected_before_write(self):
        self.api.hook=lambda path,body:dict(ok=True,team_id='TOTHER',user_id='U123',bot_id='B123')
        self.assertEqual(self.refresh()['state'],'NOT_SENT');self.assertEqual(self.api.writes(),[])

    def test_plan_thread_and_request_channel_must_match(self):
        for key,value in [('channel_id','C777'),('task_id','other'),('thread_ts','bad')]:
            c=copy.deepcopy(self.config);c['targets']['unit'][key]=value
            with self.assertRaises(display.flow.FlowError):display.Display(self.runner,self.api,c).refresh('unit')
        self.config['request_channel_ids']=['C123']
        with self.assertRaises(display.flow.FlowError):self.refresh()
        self.assertEqual(self.api.calls,[])

    def test_binding_cannot_move_after_first_post(self):
        self.refresh();self.config['publisher_user_id']='UOTHER'
        with self.assertRaises(display.flow.FlowError):self.refresh()
        self.assertEqual(len(self.api.writes()),1)

    def test_wrong_receipt_never_confirms_or_reposts(self):
        def bad(path,body):
            if path=='chat.postMessage':return dict(ok=True,channel='COTHER',ts='1790550001.123456')
        self.api.hook=bad
        self.assertEqual(self.refresh()['state'],'UNKNOWN');self.assertEqual(self.refresh()['state'],'UNKNOWN')
        self.assertEqual(len(self.api.writes()),1)

    def test_render_does_not_create_mentions_controls_or_auto_approval(self):
        _,view=display.model(self.runner,'unit');view['task_id']='<!channel> <@U123>'
        view['state']='MERGE_CANDIDATE';view['head']='b'*40
        payload=display.render(view)
        self.assertNotIn('<!channel>',payload['text']);self.assertFalse(payload['mrkdwn'])
        self.assertFalse(payload['link_names']);self.assertEqual(payload['blocks'][0]['text']['type'],'plain_text')
        self.assertNotIn('actions',[b['type'] for b in payload['blocks']])
        self.assertIn('User',payload['text']);self.assertIn('b'*40,payload['text'])

    def test_unknown_head_never_reuses_old_candidate(self):
        self.refresh();self.s['plan_digest']='bad'
        self.assertEqual(self.refresh()['state'],'UPDATED')
        text=self.api.writes()[-1][1]['text']
        self.assertIn('BLOCKED',text);self.assertIn('UNVERIFIED',text)

    def test_pending_handoff_does_not_relabel_existing_session(self):
        self.runner.advance('writer-start')
        owner=next(iter(self.store.owners().values()))
        self.s['owner_session_id']=owner['session_id']
        self.plan['nodes'][0]['writer']['identity']='other-writer'
        self.s.update(plan_digest=g.digest(self.plan),policy_revision=g.digest(self.plan))
        _,view=display.model(self.runner,'unit')
        self.assertEqual(view['state'],'BLOCKED');self.assertEqual(view['owner'],'devin')
        self.assertEqual(view['session'],owner['session_id']);self.assertEqual(view['planned_owner'],'other-writer')
        self.assertIn('인계 미완료',display.render(view)['text'])

    def test_no_global_digest_suppression_when_state_returns(self):
        self.refresh();self.change();self.refresh();self.s['blockers']=[]
        self.assertEqual(self.refresh()['state'],'UPDATED');self.assertEqual(len(self.api.writes()),3)


class CLIWiringTests(unittest.TestCase):
    def test_advance_dry_run_cannot_launch_anything(self):
        import control_plane_graph_cli as cli
        with patch('sys.argv',['graph','advance','--dry-run','--policy','/nonexistent']),patch.object(cli.graph,'GraphStore') as store,patch('builtins.print'):
            self.assertEqual(cli.main(),2);store.assert_not_called()

    def test_display_failure_does_not_retry_successful_advance(self):
        import json
        import control_plane_graph_cli as cli
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'policy.json'
            path.write_text(json.dumps(dict(schema_version=1,ledger_path='existing',enabled=True,
                source_audit='PASS',activation_pointer=POINTER,slack_display=dict(enabled=True))))
            path.chmod(0o600)
            with patch('sys.argv',['graph','advance','--policy',str(path),'--event-id','event-1234']),patch.object(cli.graph,'GraphStore'),patch.object(cli.graph,'GraphRunner') as runner,patch.object(cli.graph_slack,'Display') as panel,patch('builtins.print') as output:
                runner.return_value.advance.return_value=dict(state='IMPLEMENTATION_REQUIRED',delivery='CONFIRMED',node_id='unit')
                panel.return_value.refresh.side_effect=RuntimeError('Slack unavailable')
                self.assertEqual(cli.main(),0)
                runner.return_value.advance.assert_called_once_with('event-1234')
                result=json.loads(output.call_args.args[0])
                self.assertEqual(result['delivery'],'CONFIRMED');self.assertEqual(result['slack_display']['state'],'BLOCKED')


if __name__=='__main__':unittest.main()
