"""Cursor adapter failure ordering, durable command and identity tests; no provider calls."""
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import time
import unittest
import control_plane_cursor as c


class CursorAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.policy=dict(enabled=True, independent_audit='PASS', control_uid=1001, builder_uid=1002,
            builder_user='astra-cursor', model='qualified-grok-model', billing='INCLUDED_ONLY',
            headless_write_authorized=True, checks={k:'PASS' for k in c.REQUIRED_PROOFS},
            evidence_pointer='https://github.com/owner/control/issues/1', home=self.tmp.name,
            workspace_root=self.tmp.name, custodian_root=self.tmp.name, cli='/opt/cursor/agent', repositories=['owner/repo'],
            auth_match={'authenticated':True, 'account':'dedicated-test'})
        self.packet=dict(schema_version=1, repository='owner/repo', task_id='T1', task_revision='2',
            builder_id='CURSOR', launch_request_id='launch-1', attempt_id=1,
            task_pointer='https://github.com/owner/repo/issues/1',
            task_spec_pointer='https://github.com/owner/repo/blob/main/TASK.md')
        self.calls=[]

    def invoke(self, argv, env, **kwargs):
        self.calls.append((argv,env,kwargs))
        out=''
        if argv[1:]==['status','--format','json']: out=json.dumps(self.policy['auth_match'])
        elif argv[1:]==['models']: out='available: qualified-grok-model\n'
        elif argv[0]=='/usr/bin/loginctl': out='yes\n'
        elif argv[0]=='/usr/bin/git' and argv[1]=='clone': Path(argv[-1]).mkdir()
        elif argv[1:]==['rev-parse','HEAD']: out='a'*40+'\n'
        elif argv[1:]==['create-chat']: out='chat-1234\n'
        elif argv[:3]==['/usr/bin/systemctl','--user','show']:
            out='LoadState=loaded\nActiveState=active\nResult=success\n'
        return subprocess.CompletedProcess(argv,0,out,'')

    def test_qualified_writer_uses_exact_model_separate_durable_supervisor(self):
        c.validate_policy(self.policy)
        result=c.worker_launch(self.packet,self.policy,self.invoke)
        self.assertEqual(result['session_id'],'cursor-cli:chat-1234')
        self.assertEqual(result['outcome'],'CONFIRMED')
        for k in c.IDENTITY: self.assertEqual(result[k],self.packet[k])
        argv,env,_=next(x for x in self.calls if x[0][0]=='/usr/bin/systemd-run')
        self.assertIn('--service-type=exec',argv)
        self.assertIn('--property=Restart=no',argv)
        self.assertIn('--property=UnsetEnvironment=RUNNER_TRACKING_ID',argv)
        self.assertIn('--force',argv)
        self.assertEqual(argv[argv.index('--model')+1],self.policy['model'])
        self.assertNotIn('GITHUB_TOKEN',env)
        self.assertEqual(len(list(Path(self.tmp.name).glob('*/session.json'))),1)

    def test_same_packet_does_not_create_second_provider_session(self):
        c.worker_launch(self.packet,self.policy,self.invoke)
        with self.assertRaises(FileExistsError): c.worker_launch(self.packet,self.policy,self.invoke)
        self.assertEqual(sum(a[1:]==['create-chat'] for a,_,_ in self.calls),1)

    def test_provider_response_loss_leaves_marker_and_prevents_relaunch(self):
        def failing(argv,env,**kwargs):
            result=self.invoke(argv,env,**kwargs)
            if argv[1:]==['create-chat']: raise subprocess.TimeoutExpired(argv,90)
            return result
        with self.assertRaises(subprocess.TimeoutExpired): c.worker_launch(self.packet,self.policy,failing)
        with self.assertRaises(FileExistsError): c.worker_launch(self.packet,self.policy,self.invoke)
        self.assertFalse(any(a[0]=='/usr/bin/systemd-run' for a,_,_ in self.calls))

    def test_systemd_response_loss_does_not_become_prestart_failure(self):
        def failing(argv,env,**kwargs):
            if argv[0]=='/usr/bin/systemd-run': raise subprocess.TimeoutExpired(argv,90)
            return self.invoke(argv,env,**kwargs)
        with self.assertRaises(subprocess.TimeoutExpired): c.worker_launch(self.packet,self.policy,failing)
        self.assertEqual(len(list(Path(self.tmp.name).glob('*/session.json'))),1)
        with self.assertRaises(FileExistsError): c.worker_launch(self.packet,self.policy,self.invoke)

    def test_auth_model_linger_mismatch_stop_before_session_creation(self):
        for target,value in [('status','{}'),('models','different-model'),('linger','no')]:
            self.calls.clear()
            def fail(argv,env,**kwargs):
                result=self.invoke(argv,env,**kwargs)
                if ((target=='status' and argv[1:]==['status','--format','json']) or
                    (target=='models' and argv[1:]==['models']) or
                    (target=='linger' and argv[0]=='/usr/bin/loginctl')): result.stdout=value
                return result
            with self.subTest(target=target),self.assertRaises(c.CursorError):
                c.worker_launch(self.packet,self.policy,fail)
            self.assertFalse(any(a[1:]==['create-chat'] for a,_,_ in self.calls))

    def test_policy_rejects_auto_unqualified_and_additional_billing(self):
        for key,value in [('enabled',False),('model','auto'),('model','model;rm'),('billing','PAYG'),
                          ('builder_uid',1001),('independent_audit','PENDING'),
                          ('headless_write_authorized',False),('checks',{})]:
            with self.subTest(key=key),self.assertRaises(c.CursorError):
                c.validate_policy(dict(self.policy,**{key:value}))

    def test_packet_identity_or_pointer_cannot_select_another_lane_or_repository(self):
        for key,value in [('repository','owner/other'),('builder_id','GLM'),('attempt_id',True),
                          ('task_pointer','https://github.com/owner/other/issues/1')]:
            with self.subTest(key=key),self.assertRaises(c.CursorError):
                c.validate_packet(dict(self.packet,**{key:value}),self.policy)

    def test_new_tasks_have_separate_persistent_workspaces(self):
        c.worker_launch(self.packet,self.policy,self.invoke)
        c.worker_launch(dict(self.packet,task_id='T2',launch_request_id='launch-2'),self.policy,self.invoke)
        workspaces=[x[2]['cwd'] for x in self.calls if x[0][1:]==['create-chat']]
        self.assertEqual(len(set(workspaces)),2)

    def test_worker_auth_uses_kernel_pipe_owner_not_spoofed_environment(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd); self.addCleanup(os.close, write_fd)
        c.authenticated_input(os.getuid(), read_fd)
        with self.assertRaises(c.CursorError): c.authenticated_input(os.getuid() + 1, read_fd)
        with self.assertRaises(c.CursorError): c.authenticated_input(os.getuid(), write_fd)
        with tempfile.TemporaryFile() as f, self.assertRaises(c.CursorError):
            c.authenticated_input(os.getuid(), f.fileno())

    def exercise_sender(self, unknown=False):
        root = Path(self.tmp.name)
        os.mkfifo(root/'ready'); os.mkfifo(root/'release')
        ready_fd = os.open(root/'ready', os.O_RDONLY | os.O_NONBLOCK)
        release_fd = os.open(root/'release', os.O_RDWR)
        self.addCleanup(os.close, ready_fd); self.addCleanup(os.close, release_fd)
        helper = root/'sender.py'
        helper.write_text('import sys,json\n'
            'with open(sys.argv[1]+"/ready","w") as f:f.write("ready")\n'
            'with open(sys.argv[1]+"/release") as f:f.read(1)\n'
            'p=json.load(sys.stdin);p.update(outcome="CONFIRMED",session_id="cursor-cli:test")\n'
            + ('raise SystemExit(2)\n' if unknown else 'print(json.dumps(p))\n'))
        fence = (root/'fence').open('w'); fd = fence.fileno()
        fcntl.flock(fd, fcntl.LOCK_SH)
        code = ('import sys,json,control_plane_cursor as c\n'
                'c.supervised_send(json.loads(sys.argv[1]),sys.argv[2],json.loads(sys.argv[3]),int(sys.argv[4]))\n')
        child = subprocess.Popen([sys.executable,'-c',code,
                json.dumps([sys.executable,str(helper),str(root)]),json.dumps(self.packet),
                json.dumps({'control_uid':os.getuid(),'custodian_root':str(root)}),str(fd)],
                pass_fds=(fd,),cwd=str(Path(c.__file__).parent),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertTrue(select.select([ready_fd],[],[],5)[0])
        self.assertEqual(os.read(ready_fd,5),b'ready')
        record = next(root.glob('*.json'))
        sender = json.loads(record.read_text())['pid']
        def cleanup_sender():
            try: os.kill(sender, signal.SIGTERM)
            except ProcessLookupError: pass
        self.addCleanup(cleanup_sender)
        child.kill(); child.wait(timeout=5); fence.close()
        with (root/'fence').open('r') as probe:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(release_fd,b'x')
        if unknown:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if json.loads(record.read_text())['state'] == 'UNKNOWN': break
                except json.JSONDecodeError: pass
                time.sleep(.01)
            self.assertEqual(json.loads(record.read_text())['state'],'UNKNOWN')
            with (root/'fence').open('r') as probe, self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            subprocess.run([sys.executable,'-c',
                'import sys,fcntl;f=open(sys.argv[1]);fcntl.flock(f,fcntl.LOCK_EX)',str(root/'fence')],
                check=True,timeout=5)
            self.assertEqual(json.loads(record.read_text())['state'],'CONFIRMED')

    def test_real_wrapper_death_keeps_fence_until_sender_finishes(self):
        self.exercise_sender()

    def test_real_failed_sender_keeps_fence_until_operator_teardown(self):
        self.exercise_sender(unknown=True)


if __name__=='__main__': unittest.main()
