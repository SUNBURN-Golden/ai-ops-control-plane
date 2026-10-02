"""Offline Mac app tests. No provider, GitHub write, merge or deployment is called."""
import copy
import http.client
import importlib.util
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
import agents
import aiops
import common
import core
import gitops
import worker


def plan():
    return {'summary': 'Deliver the repository requirements.', 'sources': ['README.md'],
            'tasks': [{'id': 'feature', 'title': 'Feature', 'instructions': 'Implement the approved feature.',
                       'acceptance': ['Test passes and specified output exists.'], 'depends_on': []}]}


def report(job, role, status='complete', **changes):
    value = {'status': status, 'summary': 'Completed ' + role, 'question': '',
             'plan': plan() if role == 'planner' else None, 'findings': [], 'checks': ['Verified actual test output.'],
             'reviewed_head': job['head'] or '', 'covered_tasks': ['feature'] if role in ('reviewer', 'supervisor') else []}
    value.update(changes); return value


class FakeRepos:
    def __init__(self, directory):
        self.directory = Path(directory); self.sha = 'a' * 40; self.dirty = False
        self.sync = {'changed': False}; self.checked = {'state': 'passed', 'checks': []}; self.publishes = 0
    def path(self, job): return self.directory
    def prepare(self, job): return {'base_sha': self.sha, 'head': self.sha, 'base_branch': 'main'}
    def head(self, job): return self.sha
    def clean(self, job): return not self.dirty
    def assert_binding(self, job): pass
    def source_pins(self, job, value): return {'README.md': '1' * 40}
    def checkpoint(self, job): self.sha = 'b' * 40; self.dirty = False; return self.sha
    def synchronize_base(self, job): return self.sync
    def publish(self, job): self.publishes += 1; return 'https://github.com/example/product/pull/1'
    def checks(self, job): return self.checked


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.directory = Path(self.temp.name) / 'private'
        self.store = core.Store(self.directory); self.repos = FakeRepos(Path(self.temp.name))
        self.engine = core.Engine(self.store, self.repos)
    def tearDown(self): self.store.close(); self.temp.cleanup()
    def new(self, repo='example/product', request_id='request-001'):
        return self.store.create({'repository': repo, 'goal': 'Finish repository deliverables', 'request_id': request_id})
    def job(self): return self.store.jobs()[0]
    def launch(self):
        with mock.patch.object(agents, 'command', return_value=['fake']), mock.patch.object(core.subprocess, 'Popen') as popen:
            self.engine.step(self.job())
            self.assertEqual(popen.call_count, 1)
            return self.job()
    def finish(self, value=None, error=None):
        job = self.job(); attempt = job['attempt']
        receipt = {'attempt_id': attempt['id'], 'binding': attempt['binding'], 'exit_code': 0,
                   'report': value or report(job, attempt['role']), 'error': error, 'process_group_quiescent': True}
        path = self.directory / 'jobs' / job['id'] / attempt['id'] / 'receipt.json'
        common.atomic_json(path, receipt); self.engine.step(job); return self.job()
    def prepared(self):
        self.new(); self.engine.step(self.job()); self.launch(); self.finish()
    def built(self):
        self.prepared(); self.launch(); self.finish()
    def reviewed(self):
        self.built(); self.launch(); self.finish(); self.launch(); self.finish()

    def test_idempotent_start_and_request_conflict(self):
        first = self.new(); second = self.new(); self.assertEqual(first['id'], second['id'])
        with self.assertRaises(common.AppError):
            self.store.create({'repository':'example/product','goal':'different','request_id':'request-001'})
        self.assertEqual(len(self.store.jobs()),1)

    def test_one_repository_owner_including_paused_and_ready(self):
        job = self.new(); self.store.update(job['id'], state='paused')
        with self.assertRaises(common.AppError): self.new('Example/Product', 'request-002')
        self.store.update(job['id'], state='ready')
        with self.assertRaises(common.AppError): self.new('example/product', 'request-002')
        self.store.action(job['id'], 'accept'); self.new('example/product', 'request-002')
        self.assertEqual(len(self.store.jobs()),2)

    def test_concurrent_start_has_one_owner(self):
        result=[]
        def create(rid):
            try: result.append(self.new(request_id=rid)['id'])
            except common.AppError as exc: result.append(exc.code)
        threads=[threading.Thread(target=create,args=(f'request-{n:03}',)) for n in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(self.store.jobs()),1); self.assertIn('REPOSITORY_BUSY',result)

    def test_settings_snapshot_cannot_change_running_models(self):
        job=self.new(); value=self.store.settings();value['roles']['builder']['model']='custom-model'
        self.store.set_settings(value)
        self.assertEqual(self.store.get(job['id'])['settings']['roles']['builder']['model'],'')
        self.assertEqual(self.store.settings()['roles']['builder']['model'],'custom-model')

    def test_full_loop_requires_two_independent_exact_head_reviews_and_user(self):
        self.reviewed(); job=self.job()
        self.assertEqual(job['state'],'publishing');self.assertNotEqual(job['review']['attempt'],job['supervision']['attempt'])
        self.assertEqual(job['review']['head'],job['head']);self.assertEqual(job['supervision']['head'],job['head'])
        self.engine.step(job);self.assertEqual(self.job()['state'],'verifying')
        self.engine.step(self.job());self.assertEqual(self.job()['state'],'ready')
        self.assertEqual(self.repos.publishes,1)
        self.store.action(job['id'],'accept');self.assertEqual(self.job()['state'],'accepted')

    def test_review_failure_returns_findings_to_same_writer(self):
        self.built(); job=self.launch(); branch=job['branch'];profile=job['settings']['roles']['builder']
        self.finish(report(job,'reviewer','fail',findings=['Fix the incorrect retry behavior.']))
        job=self.job();self.assertEqual(job['state'],'building');self.assertTrue(job['correcting'])
        self.assertEqual(job['branch'],branch);self.assertEqual(job['settings']['roles']['builder'],profile)
        self.assertIsNone(job['review']);self.assertIn('retry',job['feedback'][0])
        self.launch();self.finish();self.assertEqual(self.job()['state'],'reviewing')

    def test_final_inspector_failure_clears_all_old_passes(self):
        self.built();self.launch();self.finish();job=self.launch()
        self.finish(report(job,'supervisor','fail',findings=['Missing an original deliverable.']))
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review']);self.assertIsNone(self.job()['supervision'])

    def test_stale_review_cannot_pass(self):
        self.built();job=self.launch();self.finish(report(job,'reviewer',reviewed_head='0'*40))
        self.assertEqual(self.job()['state'],'reviewing');self.assertIsNone(self.job()['review'])

    def test_omitted_tasks_or_missing_evidence_cannot_pass(self):
        for changes in ({'covered_tasks':[]},{'checks':[]},{'findings':['unresolved']}):
            with self.subTest(changes=changes):
                self.store.db.execute('DELETE FROM jobs');self.store.db.execute('DELETE FROM events')
                self.built();job=self.launch();self.finish(report(job,'reviewer',**changes))
                self.assertEqual(self.job()['state'],'reviewing');self.assertIsNone(self.job()['review'])

    def test_read_only_role_modification_fences_attempt(self):
        self.built();self.launch();self.repos.dirty=True
        with self.assertRaisesRegex(common.AppError,'READ_ONLY_ROLE_MODIFIED'):self.finish()
        self.assertIsNotNone(self.job()['attempt'])

    def test_user_question_keeps_scope_and_resumes_only_with_answer(self):
        self.prepared();job=self.launch();self.finish(report(job,'builder','needs_user',question='Choose the required external account.'))
        self.assertEqual(self.job()['state'],'needs_user')
        with self.assertRaises(common.AppError):self.store.action(job['id'],'resume',{})
        self.store.action(job['id'],'resume',{'answer':'Use the previously linked account.'})
        self.assertEqual(self.job()['state'],'building');self.assertEqual(len(self.job()['user_answers']),1)

    def test_pause_waits_for_running_worker_and_does_not_cancel_it(self):
        self.prepared();job=self.launch();self.store.action(job['id'],'pause')
        self.assertIsNotNone(self.job()['attempt']);self.finish();self.engine.step(self.job())
        self.assertEqual(self.job()['state'],'paused')
        self.store.action(job['id'],'resume');self.assertEqual(self.job()['state'],'reviewing')

    def test_pause_during_preparation_cannot_release_or_reconfigure_owner(self):
        job=self.new();started=threading.Event();release=threading.Event()
        def prepare(_):
            started.set();release.wait(timeout=4)
            return {'base_sha':'a'*40,'head':'a'*40,'base_branch':'main'}
        with mock.patch.object(self.repos,'prepare',side_effect=prepare):
            thread=threading.Thread(target=self.engine.tick);thread.start();self.assertTrue(started.wait(timeout=3))
            self.store.action(job['id'],'pause')
            with self.assertRaisesRegex(common.AppError,'현재 단계'):self.store.action(job['id'],'reconfigure')
            with self.assertRaisesRegex(common.AppError,'현재 단계'):self.store.action(job['id'],'cancel')
            release.set();thread.join(timeout=5)
        self.engine.tick();self.assertEqual(self.job()['state'],'paused');self.assertIsNone(self.job()['attempt'])

    def test_provider_failure_backoff_preserves_model_and_task(self):
        self.prepared();job=self.launch();self.finish(error='PROVIDER_EXIT_1')
        current=self.job();self.assertEqual(current['state'],'waiting_provider')
        self.assertGreater(current['not_before'],time.time()+200)
        self.assertEqual(current['settings'],job['settings']);self.assertIsNone(current['attempt'])

    def test_invalid_model_requires_one_explicit_configuration_change(self):
        self.prepared();self.launch();self.finish(error='MODEL_UNAVAILABLE')
        self.assertEqual(self.job()['state'],'needs_user')
        settings=self.store.settings();settings['roles']['builder']['model']='valid-model';self.store.set_settings(settings)
        self.store.action(self.job()['id'],'reconfigure')
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['question'])
        self.assertEqual(self.job()['settings']['roles']['builder']['model'],'valid-model')

    def test_model_change_cannot_clear_contract_question_or_live_attempt(self):
        self.prepared();job=self.launch()
        with self.assertRaises(common.AppError):self.store.action(job['id'],'reconfigure')
        self.finish(report(job,'builder','needs_user',question='Contract decision required.'))
        self.store.action(job['id'],'reconfigure')
        self.assertEqual(self.job()['state'],'needs_user');self.assertEqual(self.job()['question'],'Contract decision required.')

    def test_uncertain_execution_is_never_relaunched(self):
        self.prepared();self.launch();attempt=self.job()['attempt'];attempt['started']=0
        self.store.update(self.job()['id'],attempt=attempt)
        with mock.patch.object(core.subprocess,'Popen') as popen:self.engine.tick();self.engine.tick()
        self.assertEqual(self.job()['state'],'unknown');popen.assert_not_called()
        with self.assertRaises(common.AppError):self.store.action(self.job()['id'],'resume')

    def test_restart_consumes_existing_receipt_once(self):
        self.prepared();job=self.launch();attempt=job['attempt']
        path=self.directory/'jobs'/job['id']/attempt['id']/'receipt.json'
        common.atomic_json(path,{'attempt_id':attempt['id'],'binding':attempt['binding'],'exit_code':0,'error':None,'report':report(job,'builder'),'process_group_quiescent':True})
        self.store.close();self.store=core.Store(self.directory);self.engine=core.Engine(self.store,self.repos)
        with mock.patch.object(core.subprocess,'Popen') as popen:self.engine.step(self.job())
        popen.assert_not_called();self.assertEqual(self.job()['state'],'reviewing');self.assertEqual(self.job()['calls'],2)

    def test_unknown_writer_blocks_other_repositories(self):
        self.prepared();self.launch();first=self.job();self.store.update(first['id'],state='unknown')
        second=self.new('example/second','request-002')
        with mock.patch.object(self.engine,'step') as step:self.engine.tick()
        step.assert_not_called();self.assertEqual(self.store.get(second['id'])['state'],'queued')

    def test_mismatched_receipt_never_advances(self):
        self.prepared();job=self.launch();attempt=job['attempt']
        path=self.directory/'jobs'/job['id']/attempt['id']/'receipt.json'
        common.atomic_json(path,{'attempt_id':attempt['id'],'binding':'wrong','report':report(job,'builder')})
        with self.assertRaisesRegex(common.AppError,'RECEIPT_BINDING_MISMATCH'):self.engine.step(job)

    def test_ci_failure_returns_to_builder_not_ready(self):
        self.reviewed();self.engine.step(self.job());self.repos.checked={'state':'failed','checks':[{'name':'tests','status':'FAILURE'}]}
        self.engine.step(self.job());self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])

    def test_new_base_requires_fresh_reviews(self):
        self.reviewed();self.repos.sync={'changed':True,'base':'c'*40,'conflicts':False}
        self.engine.step(self.job());self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])
        self.assertEqual(self.repos.publishes,0)

    def test_local_mutation_after_inspection_requires_fresh_reviews(self):
        self.reviewed();self.repos.dirty=True;self.engine.step(self.job())
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])
        self.assertEqual(self.repos.publishes,0)

    def test_final_user_rework_reopens_entire_verification(self):
        self.reviewed();self.engine.step(self.job());self.engine.step(self.job());job=self.job()
        self.store.action(job['id'],'revise',{'feedback':'Improve the empty state.'})
        self.assertEqual(self.job()['state'],'building');self.assertIsNone(self.job()['review'])

    def test_cap_stops_before_another_model_call(self):
        self.prepared();job=self.job();settings=job['settings'];settings['max_agent_calls']=job['calls']
        self.store.update(job['id'],settings=settings)
        with mock.patch.object(core.subprocess,'Popen') as popen:self.engine.step(self.job())
        popen.assert_not_called();self.assertEqual(self.job()['state'],'paused')

    def test_increased_budget_resumes_without_changing_running_model(self):
        self.prepared();job=self.job();settings=job['settings'];settings['max_agent_calls']=job['calls']
        self.store.update(job['id'],settings=settings,state='paused')
        new=self.store.settings();new['max_agent_calls']=10;new['roles']['builder']['model']='new-next-job-model'
        self.store.set_settings(new);self.store.action(job['id'],'resume')
        self.assertEqual(self.job()['settings']['max_agent_calls'],10)
        self.assertEqual(self.job()['settings']['roles']['builder']['model'],'')

    def test_nonquiescent_terminal_receipt_cannot_free_writer(self):
        self.prepared();job=self.launch();attempt=job['attempt']
        path=self.directory/'jobs'/job['id']/attempt['id']/'receipt.json'
        common.atomic_json(path,{'attempt_id':attempt['id'],'binding':attempt['binding'],'exit_code':0,'error':None,
                                 'report':report(job,'builder'),'process_group_quiescent':False})
        with self.assertRaisesRegex(common.AppError,'종료 상태'):self.engine.step(job)
        self.assertIsNotNone(self.job()['attempt'])


class ContractTests(unittest.TestCase):
    def test_validates_repository_and_injection(self):
        self.assertEqual(common.repository('https://github.com/owner/repo.git'),'owner/repo')
        for value in ('file:///tmp/repo','x/y;touch /tmp/x','x/../../y','-x/y',[],None):
            with self.subTest(value=value),self.assertRaises(common.AppError):common.repository(value)

    def test_invalid_settings_do_not_enable_shell_or_other_provider(self):
        for changes in ({'provider':'shell','model':'x'},{'provider':'codex','model':'x\n--dangerous'},{'provider':'claude','model':'--flag'}):
            value=copy.deepcopy(common.DEFAULTS);value['roles']['builder']=changes
            with self.assertRaises(common.AppError):common.validate_settings(value)

    def test_plan_rejects_cycles_missing_acceptance_and_external_sources(self):
        for change in ('cycle','empty','source'):
            value=plan()
            if change=='cycle':value['tasks'][0]['depends_on']=['feature']
            elif change=='empty':value['tasks'][0]['acceptance']=[]
            else:value['sources']=['../../secret']
            with self.assertRaises(common.AppError):common.validate_plan(value)

    def test_duplicate_keys_and_nonfinite_json_refused(self):
        for value in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'):
            with self.assertRaises(common.AppError):common.parse_json(value)

    def test_provider_roles_have_fixed_commands_and_separate_read_only_sessions(self):
        with mock.patch.object(agents.shutil,'which',side_effect=lambda name:'/bin/'+name):
            for role in common.ROLES:
                argv=agents.command({'provider':'codex','model':'custom-model'},role,Path('/tmp/attempt'))
                self.assertIn('--ephemeral',argv);self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',argv)
                self.assertIn('workspace-write' if role=='builder' else 'read-only',argv)
                claude=agents.command({'provider':'claude','model':'my-model'},role,Path('/tmp/attempt'))
                self.assertIn('--bare',claude);self.assertNotIn('bypassPermissions',claude)
                if role!='builder':self.assertEqual(claude[claude.index('--tools')+1],'Read,Glob,Grep')
                self.assertFalse(json.loads(claude[claude.index('--settings')+1])['sandbox']['allowUnsandboxedCommands'])

    def test_tokens_and_api_fallback_are_not_in_worker_environment(self):
        with mock.patch.dict(os.environ,{'GH_TOKEN':'secret','OPENAI_API_KEY':'secret','ANTHROPIC_API_KEY':'secret','AWS_SECRET_ACCESS_KEY':'secret','PYTHONPATH':'bad'}):
            value=agents.environment()
        for key in ('GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','PYTHONPATH'):self.assertNotIn(key,value)

    def test_double_worker_claim_cannot_start_provider(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);common.atomic_json(folder/'request.json',{})
            (folder/'claimed').write_text('old')
            with mock.patch.object(worker.subprocess,'Popen') as popen,self.assertRaises(FileExistsError):worker.run(folder)
            popen.assert_not_called()

    def test_real_worker_subprocess_writes_terminal_bound_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);attempt=folder/'attempt';attempt.mkdir();executable=folder/'codex'
            output=report({'head':'a'*40},'reviewer')
            script='#!'+sys.executable+'\nimport json,sys,os\nfrom pathlib import Path\n'
            script+="assert 'GH_TOKEN' not in os.environ\n"
            script+="sys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1]).write_text("+repr(json.dumps(output))+')\n'
            executable.write_text(script);executable.chmod(0o755)
            common.atomic_json(attempt/'schema.json',agents.SCHEMA)
            common.atomic_json(attempt/'request.json',{'attempt_id':'one','binding':'binding','profile':{'provider':'codex','model':'custom'},
                'role':'reviewer','checkout':str(folder),'prompt':'review','timeout_seconds':10})
            with mock.patch.dict(os.environ,{'PATH':str(folder)+os.pathsep+os.environ['PATH'],'GH_TOKEN':'not-a-real-token'}):
                worker.run(attempt)
            receipt=common.read_json(attempt/'receipt.json')
            self.assertIsNone(receipt['error']);self.assertEqual(receipt['report'],output)
            self.assertEqual(receipt['binding'],'binding');self.assertTrue(receipt['process_group_quiescent'])

    def test_terminal_cli_error_classification_never_exposes_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            for message,code in [('model x does not exist TOKEN','MODEL_UNAVAILABLE'),('Please log in TOKEN','PROVIDER_LOGIN_REQUIRED'),('Unexpected argument --schema TOKEN','CLI_SETUP_REQUIRED'),('429 quota exceeded TOKEN','PROVIDER_EXIT_1')]:
                (Path(d)/'stderr.log').write_text(message)
                self.assertEqual(worker.failure_code(d,1),code)

    def test_service_has_single_process_lock(self):
        with tempfile.TemporaryDirectory() as d:
            fd=core.service_lock(d)
            try:
                with self.assertRaises(common.AppError):core.service_lock(d)
            finally:os.close(fd)

    def test_symlink_state_and_world_readable_token_refused(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);target=folder/'target';target.write_text('a'*40);target.chmod(0o600)
            link=folder/'link';link.symlink_to(target)
            with self.assertRaises(common.AppError):aiops.token(link)
            target.chmod(0o644)
            with self.assertRaises(common.AppError):aiops.token(target)


class GitTests(unittest.TestCase):
    def test_missing_repo_and_expired_login_are_actionable_without_leaking_stderr(self):
        for stderr,code in [('repository not found token-secret','REPOSITORY_ACCESS_REQUIRED'),('HTTP 401 token-secret','GITHUB_LOGIN_REQUIRED')]:
            result=subprocess.CompletedProcess(['gh'],1,'',stderr)
            with mock.patch.object(gitops.subprocess,'run',return_value=result):
                with self.assertRaises(common.AppError) as caught:gitops.execute(['gh','repo','view'])
            self.assertEqual(caught.exception.code,code);self.assertNotIn('token-secret',str(caught.exception))

    def test_required_checks_missing_or_skipped_cannot_pass(self):
        with tempfile.TemporaryDirectory() as d:
            repos=gitops.Repositories(d);job={'repository':'BeautifulMind-JT/ZARI','branch':'aiops/x','head':'a'*40}
            data={'headRefOid':job['head'],'state':'OPEN','statusCheckRollup':[]}
            with mock.patch.object(gitops,'gh',return_value=json.dumps(data)):
                self.assertEqual(repos.checks(job)['state'],'pending')
            data['statusCheckRollup']=[{'name':'bridge','status':'COMPLETED','conclusion':'SKIPPED'}]
            with mock.patch.object(gitops,'gh',return_value=json.dumps(data)):
                self.assertEqual(repos.checks(job)['state'],'failed')

    def test_real_git_checkpoint_keeps_branch_and_blocks_credential_files(self):
        with tempfile.TemporaryDirectory() as d:
            repos=gitops.Repositories(d);job={'id':'test','repository':'example/product','branch':'aiops/test','current_task':{'title':'Feature'}}
            checkout=repos.path(job);checkout.mkdir()
            gitops.git(checkout,'init','-b',job['branch']);gitops.git(checkout,'config','user.name','Test');gitops.git(checkout,'config','user.email','test@example.invalid')
            gitops.git(checkout,'remote','add','origin','https://github.com/example/product.git')
            (checkout/'README.md').write_text('spec');gitops.git(checkout,'add','.');gitops.git(checkout,'commit','-m','baseline')
            before=repos.head(job);(checkout/'app.py').write_text('print("done")\n')
            after=repos.checkpoint(job);self.assertNotEqual(before,after);self.assertTrue(repos.clean(job))
            (checkout/'.env').write_text('FAKE=not-a-secret')
            with self.assertRaisesRegex(common.AppError,'자격증명'):repos.checkpoint(job)
            self.assertEqual(repos.head(job),after)


class InstallerTests(unittest.TestCase):
    def test_per_user_app_and_launch_agent_have_literal_space_safe_paths(self):
        spec=importlib.util.spec_from_file_location('mac_installer',APP/'install.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as d:
            test_home=Path(d)/'Test User';test_home.mkdir();destination=test_home/'Applications/AIOPS.app';data=test_home/'Library/Application Support/AIOPS'
            with mock.patch.object(installer.sys,'platform','darwin'),mock.patch.object(installer.Path,'home',return_value=test_home),\
                 mock.patch.object(installer.shutil,'which',return_value='/usr/bin/tool'),mock.patch.object(installer.subprocess,'run') as run,\
                 mock.patch('sys.stdout',new_callable=io.StringIO):
                installer.install(destination,data)
            config=plistlib.loads((test_home/'Library/LaunchAgents/local.aiops.mac.plist').read_bytes())
            self.assertEqual(config['ProgramArguments'][1],str(destination/'Contents/Resources/aiops.py'))
            self.assertIn(str(data),config['ProgramArguments']);self.assertTrue(config['RunAtLoad'])
            self.assertNotIn('sudo',run.call_args.args[0]);self.assertEqual(run.call_count,1)
            launcher=destination/'Contents/MacOS/AIOPS'
            subprocess.run(['bash','-n',str(launcher)],check=True)
            self.assertTrue((destination/'Contents/Resources/ui/app.js').is_file())


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        available={k:{'installed':True,'version':'test'} for k in ('git','gh','codex','claude')};available['github_authenticated']=True
        with mock.patch.object(agents,'availability',return_value=available):self.app=aiops.Application(Path(self.temp.name)/'state')
        self.server=aiops.ThreadingHTTPServer(('127.0.0.1',0),aiops.handler(self.app,'http://127.0.0.1:0'))
        self.port=self.server.server_port;self.origin='http://127.0.0.1:'+str(self.port)
        self.server.RequestHandlerClass=aiops.handler(self.app,self.origin)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.app.store.close();self.temp.cleanup()
    def request(self,path,method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        raw=json.dumps(body) if body is not None else None
        h={'Content-Type':'application/json'};h.update(headers or {})
        conn.request(method,path,raw,h);response=conn.getresponse();data=response.read();result=(response.status,dict(response.headers),data)
        conn.close();return result
    def test_static_page_is_real_but_private_api_requires_auth(self):
        status,headers,data=self.request('/');self.assertEqual(status,200);self.assertIn('AIOPS'.encode(),data)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(self.request('/api/state')[0],401)
    def test_host_origin_and_cross_origin_preflight_are_rejected(self):
        auth={'Authorization':'Bearer '+self.app.owner_token}
        self.assertEqual(self.request('/api/state',headers={**auth,'Host':'evil.invalid'})[0],400)
        self.assertEqual(self.request('/api/state',headers={**auth,'Origin':'https://evil.invalid'})[0],400)
        self.assertEqual(self.request('/api/jobs','OPTIONS')[0],403)
    def test_owner_session_cookie_is_httponly_and_mutations_need_same_origin(self):
        auth={'Authorization':'Bearer '+self.app.owner_token}
        status,headers,_=self.request('/api/session','POST',{},auth);self.assertEqual(status,200)
        cookie=headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        header={'Cookie':cookie.split(';')[0]}
        self.assertEqual(self.request('/api/state',headers=header)[0],200)
        self.assertEqual(self.request('/api/settings','POST',self.app.store.settings(),header)[0],401)
        header.update(Origin=self.origin,**{'X-AIOPS-Client':'desktop'})
        self.assertEqual(self.request('/api/settings','POST',self.app.store.settings(),header)[0],200)
    def test_bot_start_is_idempotent_but_cannot_accept_or_change_models(self):
        auth={'Authorization':'Bearer '+self.app.relay_token};value={'repository':'example/product','request_id':'request-001'}
        first=self.request('/api/jobs','POST',value,auth);second=self.request('/api/jobs','POST',value,auth)
        self.assertEqual(first[0],201);self.assertEqual(json.loads(first[2])['id'],json.loads(second[2])['id'])
        self.assertEqual(self.request('/api/settings','POST',self.app.store.settings(),auth)[0],409)
        key=json.loads(first[2])['id'];self.app.store.update(key,state='ready')
        self.assertEqual(self.request('/api/jobs/'+key+'/accept','POST',{},auth)[0],409)
        self.assertEqual(self.app.store.get(key)['state'],'ready')
    def test_traversal_route_never_serves_secrets(self):
        auth={'Authorization':'Bearer '+self.app.owner_token}
        self.assertEqual(self.request('/../desktop-token',headers=auth)[0],404)
    def test_mcp_handshake_and_tool_contract(self):
        source=io.StringIO('\n'.join(json.dumps(x) for x in [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'}])+'\n')
        out=io.StringIO();aiops.mcp(self.app.store.directory,source,out);rows=[json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(rows),2);self.assertEqual(rows[0]['result']['protocolVersion'],'2025-06-18')
        names={x['name'] for x in rows[1]['result']['tools']};self.assertEqual(names,{'aiops_start','aiops_status','aiops_list','aiops_pause'})

    def test_mcp_malformed_objects_return_errors_without_crashing(self):
        data='[]\n{"id":1,"method":"tools/call","params":{"arguments":"bad"}}\n'
        output=io.StringIO();aiops.mcp(self.app.store.directory,io.StringIO(data),output)
        rows=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows),2);self.assertTrue(all('error' in row for row in rows))


if __name__=='__main__':unittest.main()
