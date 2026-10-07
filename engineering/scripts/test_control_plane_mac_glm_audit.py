"""Synthetic execution/API fixtures, never real audit qualification or a PASS."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac_app'))
import core
import common
import handoff
import mac_astra_receipt as fable
import mac_glm_audit as audit
import mac_glm_runner as runner


def requirement():
    request={'repository':'BeautifulMind-JT/kix-protocol','pr_url':'https://github.com/BeautifulMind-JT/kix-protocol/pull/121',
        'job':audit.JOB,'head':'a'*40,'branch':'aiops/fixture','gate':'ARCHITECTURE','requested_depth':'A3',
        'requested_auditor':'ASTRA_FABLE','task_id':'EXISTING','task_revision':'d'*64,'writer_sessions':['codex-cli:writer'],
        'canonical_binding':{'authority_kind':'MAC_LOCAL','program':'kix','node':'agents-scope-sync',
            'plan_commit':'7481b0e16ce9b903abbffa62249bb91cd9e63cfe','plan_blob':'ff0f39a8129ca8b8d30818cce35c3d4e588872fc',
            'task_id':'EXISTING','task_revision':'d'*64}}
    return {'required':True,'audit_receipt':None,'request_binding':request,'request_sha256':common.digest(request)}


def events(run):
    verdict={'request_sha256':run['request_sha256'],'packet_sha256':run['packet_sha256'],
        'head':run['request']['head'],'gate':'ARCHITECTURE','depth':'A3','result':'PASS',
        'contract_change_required':'NO','summary':'Synthetic fixture, not an actual audit.',
        'findings':[],'decision_question':''}
    return [{'type':'system','subtype':'init','model':audit.MODEL,'session_id':run['session'],'tools':['StructuredOutput']},
        {'type':'assistant','message':{'model':audit.MODEL,'content':[]}},
        {'type':'result','subtype':'success','is_error':False,'session_id':run['session'],
         'modelUsage':{audit.MODEL:{}},'permission_denials':[],'structured_output':verdict}]


class GlmReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=core.Store(Path(self.temp.name)/'app');self.addCleanup(self.store.close)
        self.req=requirement();self.journal=audit.Journal(self.store)
        p=patch.object(fable,'decision_evidence',return_value=copy.deepcopy(fable.DECISION));p.start();self.addCleanup(p.stop)
        original=fable.Journal(self.store).request(self.req)
        common.private_directory(self.store.directory/'mac-glm-audits')
        folder=common.private_directory(self.store.directory/'mac-glm-audits'/self.req['request_sha256'])
        self.run={'producer':audit.PRODUCER,'model':audit.MODEL,'request':copy.deepcopy(self.req['request_binding']),
            'request_sha256':self.req['request_sha256'],'approval':copy.deepcopy(audit.DECISION),
            'session':'12345678-abcd-4321-8000-123456789abc','excluded_sessions':['codex-cli:writer','claude-cli:reviewer'],
            'started':time.time(),'finished':time.time(),'requested_at':original['created_at'],
            'exit_code':0,'process_group_quiescent':True}
        self.run.update(cli_path='/fixture/synthetic-cli',cli_sha256='c'*64,producer_code_sha256='e'*64,pid=99999)
        common.atomic_json(folder/'packet.json',{'request':self.run['request'],'excluded_sessions':self.run['excluded_sessions']})
        self.run['packet_sha256']=hashlib.sha256((folder/'packet.json').read_bytes()).hexdigest()
        self.stream=events(self.run)
        (folder/'stdout.jsonl').write_text('\n'.join(common.encoded(x) for x in self.stream)+'\n')
        (folder/'stderr.txt').write_text('')
        for p in folder.iterdir():p.chmod(0o600)
        self.run['files']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        self.run['verdict']=self.stream[-1]['structured_output']
        body=audit.comment_body(self.run,self.run['verdict']);now=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.comment={'id':123,'body':body,'created_at':now,'updated_at':now,
            'html_url':'https://github.com/SUNBURN-Golden/kix-protocol/pull/121#issuecomment-123',
            'issue_url':'https://api.github.com/repos/SUNBURN-Golden/kix-protocol/issues/121',
            'user':{'id':263336091,'login':'BeautifulMind-JT','type':'User'}}
        self.run['comment']={'id':123,'created_at':now,'body_sha256':fable.body_hash(body)}
        self.pages=[[copy.deepcopy(self.comment)]]
        self.journal.db.execute('INSERT INTO mac_glm_runs VALUES (?,?,?,?)',
            (self.req['request_sha256'],audit.scope(self.req),'PUBLISHED',common.encoded(self.run)))
        from test_control_plane_mac_url_compat import metadata,decision,CENTRAL,NEW
        def raw(path,paginate=False):
            if path=='repos/'+CENTRAL:return metadata(CENTRAL)
            if path=='repos/'+NEW:return metadata(NEW)
            if path=='repos/'+CENTRAL+'/issues/comments/6030780072':return decision()
            raise AssertionError(path)
        def api(path,paginate=False):
            if path.endswith('/issues/comments/123'):return copy.deepcopy(self.comment)
            if '/comments?' in path:return copy.deepcopy(self.pages)
            raise AssertionError(path)
        for p in (patch.object(audit,'approval',return_value=copy.deepcopy(audit.DECISION)),
                  patch.object(fable,'live_pr'),patch.object(handoff,'api',side_effect=api),patch.object(handoff,'_raw_api',side_effect=raw)):
            p.start();self.addCleanup(p.stop)

    def test_actual_local_proof_and_live_comment_bind_original_request_without_rewriting(self):
        before='\n'.join(self.journal.db.iterdump());original=copy.deepcopy(self.req)
        result=self.journal.consume(self.req)
        self.assertEqual(result['comment']['auditor_identity'],'MAC_GLM53')
        self.assertEqual(result['comment']['auditor_model'],'glm-5.3')
        self.assertNotEqual(result['source'],'AUTHENTICATED_GITHUB_AUDIT_COMMENT')
        self.assertEqual(self.req,original);self.assertEqual(result['request']['requested_auditor'],'ASTRA_FABLE')
        self.assertEqual('\n'.join(self.journal.db.iterdump()),before)
        self.assertEqual(audit.existing_receipt(self.store,self.req),result)

    def test_formatted_github_pass_without_private_execution_is_never_a_receipt(self):
        self.journal.db.execute('DELETE FROM mac_glm_runs')
        with self.assertRaisesRegex(common.AppError,'ASTRA_AUDIT_REQUIRED'):self.journal.consume(self.req)
        self.assertIsNone(audit.existing_receipt(self.store,self.req))

    def test_loss_of_previously_consumed_private_record_is_not_absence_of_audit(self):
        expected=self.journal.consume(self.req)
        self.journal.db.execute('DELETE FROM mac_glm_runs')
        with self.assertRaises(common.AppError):audit.existing_receipt(self.store,self.req,expected=expected)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM mac_glm_holds').fetchone()[0],1)

    def test_modified_or_deleted_local_execution_creates_durable_hold(self):
        (self.journal.folder(self.req)/'stdout.jsonl').write_text('forged PASS')
        with self.assertRaises(common.AppError):self.journal.consume(self.req)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM mac_glm_holds').fetchone()[0],1)
        with self.assertRaises(common.AppError):audit.Journal(self.store).consume(self.req)

    def test_deleted_published_comment_cannot_be_hidden_by_restoring_old_pass(self):
        self.pages=[[]]
        with self.assertRaises(common.AppError):self.journal.consume(self.req)
        self.pages=[[self.comment]]
        with self.assertRaises(common.AppError):self.journal.consume(self.req)

    def test_comment_edit_actor_time_number_and_body_changes_are_rejected(self):
        for key,value in [('body',self.comment['body']+' '),('updated_at','2030-01-01T00:00:00Z'),
                          ('html_url',self.comment['html_url'].replace('/121#','/122#')),
                          ('user',{'id':99,'login':'BeautifulMind-JT','type':'User'})]:
            altered={**self.comment,key:value}
            with self.subTest(key=key),self.assertRaises(common.AppError):self.journal.verify_comment(self.req,self.run,altered)

    def test_init_assistant_usage_model_and_session_must_all_be_actual_expected_identity(self):
        for target in ('init','assistant','usage','session','tools','denial','exit'):
            stream=copy.deepcopy(self.stream)
            if target=='init':stream[0]['model']='claude-other'
            if target=='assistant':stream[1]['message']['model']='claude-other'
            if target=='usage':stream[-1]['modelUsage']={'other':{}}
            if target=='session':stream[-1]['session_id']='other'
            if target=='tools':stream[0]['tools']=['Read']
            if target=='denial':stream[-1]['permission_denials']=[{'tool':'Read'}]
            if target=='exit':stream[-1]['is_error']=True
            with self.subTest(target=target),self.assertRaises(common.AppError):
                audit.validate_output('\n'.join(map(common.encoded,stream)).encode(),self.run)

    def test_head_request_packet_gate_depth_and_inconsistent_pass_do_not_validate(self):
        for key,value in [('head','b'*40),('request_sha256','c'*64),('packet_sha256','c'*64),('gate','RELEASE'),
                          ('depth','A2'),('findings',[{'severity':'BLOCKING','pointer':'AGENTS.md','detail':'failure'}]),
                          ('decision_question','Need a decision')]:
            stream=copy.deepcopy(self.stream);stream[-1]['structured_output'][key]=value
            with self.subTest(key=key),self.assertRaises(common.AppError):
                audit.validate_output('\n'.join(map(common.encoded,stream)).encode(),self.run)

    def test_writer_or_reviewer_session_cannot_be_reused_under_different_prefix(self):
        for prefix in ('codex-cli:','claude-cli:','mac-glm53:',''):
            run=copy.deepcopy(self.run);run['excluded_sessions'].append(prefix+run['session'])
            with self.subTest(prefix=prefix),self.assertRaises(common.AppError):self.journal.proof(self.req,run)

    def test_original_request_record_and_request_timestamp_are_required(self):
        run=copy.deepcopy(self.run);run['started']=0
        with self.assertRaises(common.AppError):self.journal.proof(self.req,run)
        self.journal.db.execute('DELETE FROM mac_host_astra_requests')
        with self.assertRaises(common.AppError):self.journal.proof(self.req,self.run)

    def test_failed_negative_or_contract_yes_cannot_be_consumed(self):
        for result in ('FAIL','DECISION_REQUIRED'):
            run=copy.deepcopy(self.run);run['verdict']['result']=result
            with patch.object(self.journal,'proof',return_value=run['verdict']),self.assertRaises(common.AppError):
                self.journal.consume(self.req)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM mac_glm_holds').fetchone()[0],1)

    def test_unknown_run_fences_new_review_request_for_same_head(self):
        self.journal.save(self.req,'RUNNING',self.run)
        revised=copy.deepcopy(self.req);revised['request_binding']['review_sha256']='e'*64
        revised['request_sha256']=common.digest(revised['request_binding'])
        with self.assertRaisesRegex(common.AppError,'GLM_AUDIT_PENDING'):audit.existing_receipt(self.store,revised)
        with self.assertRaisesRegex(common.AppError,'GLM_AUDIT_PENDING'):audit.require_idle(self.store.directory)

    def test_same_head_hold_blocks_another_request_and_producer_selection(self):
        with self.assertRaises(common.AppError):self.journal.hold(self.req,'MAC_HOST_ASTRA_AUDIT_CONFLICT')
        revised=copy.deepcopy(self.req);revised['request_binding']['review_sha256']='e'*64
        revised['request_sha256']=common.digest(revised['request_binding'])
        with self.assertRaisesRegex(common.AppError,'AUDIT_CONFLICT'):audit.existing_receipt(self.store,revised)

    def test_unrelated_jobs_and_release_gate_are_not_authorized(self):
        for field,value in [('job','other'),('pr_url',self.req['request_binding']['pr_url'].replace('/121','/122')),
                            ('gate','RELEASE'),('requested_auditor','MAC_GLM53')]:
            req=copy.deepcopy(self.req);req['request_binding'][field]=value;req['request_sha256']=common.digest(req['request_binding'])
            with self.subTest(field=field),self.assertRaises(common.AppError):audit.scope(req)

    def test_published_audit_is_reconsumed_without_new_model_or_comment(self):
        pipeline=type('Pipeline',(),{'store':self.store,'audit_requirement':lambda _,job:self.req})()
        with patch.object(runner,'execute') as execute,patch.object(runner,'publish') as publish:
            self.assertEqual(runner.produce(pipeline,{'id':audit.JOB})['comment']['result'],'PASS')
            execute.assert_not_called();publish.assert_not_called()

    def pipeline(self):
        from mac_pipeline import Pipeline
        job={'id':audit.JOB,'head':'a'*40,'candidate_published_head':'a'*40,
             'ci':{'state':'passed','head':'a'*40,'source':'GITHUB_ACTIONS_API'},
             'review':{'provider_evidence':{'session_id':'claude-cli:reviewer'}}}
        pipeline=Pipeline(self.store,None,SimpleNamespace(head=lambda _:job['head'],clean=lambda _:True))
        pipeline.validate_candidate=lambda *args,**kwargs:None
        pipeline.audit_requirement=lambda _:copy.deepcopy(self.req)
        pipeline.writer_sessions=lambda _:['codex-cli:writer']
        for p in (patch.object(self.store,'get',side_effect=lambda _:copy.deepcopy(job)),
                  patch.object(self.store,'update',side_effect=lambda _,**kw:job.update(kw))):
            p.start();self.addCleanup(p.stop)
        return pipeline,job

    def test_product_pipeline_consumes_distinct_mac_proof_without_changing_original_request(self):
        pipeline,job=self.pipeline();original=copy.deepcopy(self.req['request_binding'])
        admitted=pipeline.validate_audit(job)
        self.assertEqual(admitted['audit_receipt']['comment']['auditor_identity'],'MAC_GLM53')
        self.assertEqual(admitted['request_binding'],original)

    def test_fable_negative_still_blocks_a_valid_mac_pass(self):
        from test_control_plane_mac_astra_receipt import audit_comment
        negative=audit_comment(self.req,cid=124,result='FAIL')
        self.pages[0].append(negative)
        pipeline,job=self.pipeline()
        with self.assertRaisesRegex(common.AppError,'AUDIT_CONFLICT'):pipeline.validate_audit(job)

    def test_later_fable_pass_does_not_replace_already_consumed_mac_receipt(self):
        pipeline,job=self.pipeline();first=pipeline.validate_audit(job)
        other={'source':'AUTHENTICATED_GITHUB_AUDIT_COMMENT','comment':{'auditor_session':'fresh-fable-session'}}
        with patch.object(pipeline.astra,'consume',return_value=other) as fable_read:
            self.assertEqual(pipeline.validate_audit(job),first)
            fable_read.assert_called_once()

    def test_mac_hold_still_blocks_a_fable_pass(self):
        from test_control_plane_mac_astra_receipt import audit_comment
        self.pages[0].append(audit_comment(self.req,cid=124))
        with self.assertRaises(common.AppError):self.journal.hold(self.req,'MAC_HOST_ASTRA_AUDIT_CONFLICT')
        pipeline,job=self.pipeline()
        with self.assertRaisesRegex(common.AppError,'AUDIT_CONFLICT'):pipeline.validate_audit(job)

    def test_uncertain_comment_post_is_recovered_by_reading_without_reposting(self):
        self.journal.save(self.req,'PUBLISHING',self.run)
        pipeline=SimpleNamespace(store=self.store)
        with patch.object(runner,'preflight'),patch.object(self.store,'get',return_value={'id':audit.JOB}),\
                patch('gitops.execute') as write,patch.object(runner,'execute') as model:
            result=runner.publish(self.journal,pipeline,{'id':audit.JOB},self.req,self.run,recovering=True)
        self.assertEqual(result['comment']['comment_id'],123);write.assert_not_called();model.assert_not_called()

    def test_unknown_publication_with_no_or_duplicate_matching_comments_stays_fenced(self):
        for pages in ([[]],[[self.comment,self.comment]]):
            self.pages=pages
            with patch.object(runner,'preflight'),patch.object(self.store,'get',return_value={'id':audit.JOB}),\
                    patch('gitops.execute') as write,self.assertRaisesRegex(common.AppError,'GLM_AUDIT_PENDING'):
                runner.publish(self.journal,SimpleNamespace(store=self.store),{'id':audit.JOB},self.req,self.run,recovering=True)
            write.assert_not_called()


class GlmProcessTests(unittest.TestCase):
    def test_fixed_wrapper_runs_a_toolless_child_and_captures_actual_exit_and_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);home=root/'home';(home/'.claude').mkdir(parents=True)
            (home/'.claude/settings.json').write_text(json.dumps({'env':{'ANTHROPIC_BASE_URL':'https://api.z.ai/api/anthropic'}}))
            common.private_directory(root/'runs');folder=common.private_directory(root/'runs/fixture')
            req=requirement();run={'request':req['request_binding'],'request_sha256':req['request_sha256'],
                'session':'12345678-abcd-4321-8000-123456789abc','started':time.time()}
            common.atomic_json(folder/'packet.json',{'request':run['request'],'request_sha256':run['request_sha256']})
            run['packet_sha256']=hashlib.sha256((folder/'packet.json').read_bytes()).hexdigest()
            fixture=root/'synthetic-cli'
            fixture.write_text('#!'+sys.executable+'\n'+'''import sys,json
args=sys.argv[1:];data=json.load(sys.stdin)
assert args[args.index('--tools')+1]==''
assert args[args.index('--permission-mode')+1]=='dontAsk'
assert '--no-session-persistence' in args and '--strict-mcp-config' in args
session=args[args.index('--session-id')+1];model=args[args.index('--model')+1]
packet=args[1].split('packet_sha256=')[1]
verdict=dict(request_sha256=data['request_sha256'],packet_sha256=packet,head=data['request']['head'],gate='ARCHITECTURE',depth='A3',result='PASS',contract_change_required='NO',summary='Synthetic child fixture, never real audit evidence.',findings=[],decision_question='')
for event in [dict(type='system',subtype='init',model=model,session_id=session,tools=['StructuredOutput']),dict(type='assistant',message=dict(model=model,content=[])),dict(type='result',subtype='success',is_error=False,session_id=session,modelUsage={model:{}},permission_denials=[],structured_output=verdict)]:print(json.dumps(event),flush=True)
''')
            fixture.chmod(0o700)
            with patch.object(Path,'home',return_value=home),patch.object(runner.shutil,'which',return_value=str(fixture)):
                runner.execute(run,folder)
            self.assertEqual(run['exit_code'],0);self.assertTrue(run['process_group_quiescent'])
            self.assertEqual(run['verdict']['head'],req['request_binding']['head'])
            self.assertEqual(set(run['files']),{'packet.json','stdout.jsonl','stderr.txt'})


class GlmPacketTests(unittest.TestCase):
    def test_export_preserves_exact_blobs_and_never_includes_unrelated_tracked_data(self):
        from mac_authority_checkpoint import ACTOR,APPROVAL
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            def git(*args):
                return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
                    '-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],cwd=root,text=True).strip()
            git('init','-b','main');(root/'.aiops').mkdir()
            (root/'.aiops/program.json').write_bytes(b'{"fixture":true}\n\n')
            (root/'AGENTS.md').write_bytes(b'Original authority\n\n')
            (root/'private-transactions.json').write_text('PRIVATE_UNRELATED_DATA_MUST_NOT_BE_SENT')
            git('add','.');git('commit','-m','fixture base');base=git('rev-parse','HEAD')
            after=b'\tApproved fixture change\n\n';(root/'AGENTS.md').write_bytes(after)
            git('add','AGENTS.md');git('commit','-m','fixture change');head=git('rev-parse','HEAD')
            req=requirement();req['request_binding']['head']=head;req['request_sha256']=common.digest(req['request_binding'])
            job={'base_sha':base,'head':head,'plan':{'sources':['.aiops/program.json']},
                 'review':{'head':head,'report':{'summary':'synthetic'},'provider_evidence':{'session_id':'reviewer'}}}
            pipeline=SimpleNamespace(repos=SimpleNamespace(path=lambda _:root))
            body=(Path(__file__).resolve().parents[1]/'docs/MAC_AGENTS_SCOPE_APPROVAL_6018278031.md').read_text()
            approved={'body':body,'user':ACTOR,'created_at':APPROVAL['created_at'],'updated_at':APPROVAL['created_at']}
            with patch('mac_authority_checkpoint.decision_verified',return_value=True),\
                    patch.object(handoff,'api',return_value=approved),patch.object(audit,'approval',return_value=audit.DECISION):
                value=runner.packet(pipeline,job,req,{'fixture':'CI'},['writer','reviewer'])
                self.assertEqual(value['files']['AGENTS.md']['text'].encode(),after)
                self.assertTrue(value['base_files']['.aiops/program.json']['same_blob_as_head'])
                self.assertEqual(set(value['files']),{'AGENTS.md','.aiops/program.json'})
                self.assertNotIn('PRIVATE_UNRELATED_DATA_MUST_NOT_BE_SENT',common.encoded(value))
                (root/'private-transactions.json').write_text('changed unrelated private data')
                git('add','.');git('commit','-m','out of scope fixture');job['head']=git('rev-parse','HEAD')
                with self.assertRaises(common.AppError):runner.packet(pipeline,job,req,{},[])


class GlmApprovalTests(unittest.TestCase):
    def test_exact_live_approval_actor_body_timestamp_and_pointer_are_required(self):
        repo='SUNBURN-Golden/ai-ops-control-plane';body=(Path(__file__).resolve().parents[1]/'docs/MAC_GLM_PRODUCT_AUDIT_DECISION_20261007.md').read_text()
        value={'id':6031603785,'body':body,'user':{'id':263336091,'login':'BeautifulMind-JT','type':'User'},
            'created_at':audit.DECISION['created_at'],'updated_at':audit.DECISION['created_at'],
            'html_url':f'https://github.com/{repo}/pull/84#issuecomment-6031603785',
            'issue_url':f'https://api.github.com/repos/{repo}/issues/84'}
        with patch.object(handoff,'api',return_value=value):self.assertEqual(audit.approval(),audit.DECISION)
        for field,changed in [('body',body+' edit'),('id',1),('updated_at','2030-01-01T00:00:00Z'),
                               ('html_url',value['html_url'].replace('/84#','/85#')),
                               ('user',{'id':1,'login':'BeautifulMind-JT','type':'User'})]:
            with self.subTest(field=field),patch.object(handoff,'api',return_value={**value,field:changed}),self.assertRaises(common.AppError):audit.approval()


if __name__=='__main__':unittest.main()
