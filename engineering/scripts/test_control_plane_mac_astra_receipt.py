"""Synthetic authenticated-API fixtures; no live audit or runtime qualification."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
from common import AppError
import mac_astra_receipt as receipts


def audit_comment(requirement, *, cid=123, result='PASS', depth='A3'):
    request = requirement['request_binding']; repo = request['repository']; head = request['head']
    number = int(request['pr_url'].rsplit('/', 1)[1]); session = 'fixture-auditor-session'
    body = (receipts.MARK + '\n' +
            f'ASTRA_AUDIT_V1 pr={number} head={head} result={result} depth={depth} auditor=ASTRA_FABLE session={session}\n\n'
            'Synthetic fixture, not an actual protected audit.\n\n```text\n'
            f'AUDIT_REQUEST_ID: 20300101T000000Z-12345678\nAUDIT_ATTEMPT_ID: 1\nAUDIT_RESULT: {result}\n'
            f'AUDITOR_IDENTITY_OR_SESSION: ASTRA_FABLE claude-fable-5-1 session={session}\n'
            f'AUDITED_TASK_REVISION_OR_MILESTONE: {repo}#{number} gate={request["gate"]} requested_depth={request["requested_depth"]}\n'
            f'AUDITED_HEAD_OR_EVIDENCE_SHA: {head}\nAUDITED_MERGE_BASE_SHA: ' + 'b'*40 + '\n'
            f'VERIFIED_AUDIT_DEPTH: {depth}\nVERIFIED_TOUCHED_AREAS: fixture\n'
            'VERIFIED_CONTRACT_CHANGE_REQUIRED: NO\nFINDING_POINTERS: none\n```\n')
    return {'id': cid, 'html_url': request['pr_url'] + '#issuecomment-' + str(cid),
            'issue_url': 'https://api.github.com/repos/' + repo + '/issues/' + str(number),
            'user': {'login': 'BeautifulMind-JT', 'id': 263336091, 'type': 'User'},
            'created_at': '2030-01-01T00:00:00Z', 'updated_at': '2030-01-01T00:00:00Z', 'body': body}


class CommentReceiptTests(unittest.TestCase):
    def setUp(self):
        self.requirement = {'required': True, 'request_binding': {
            'repository': 'owner/kix', 'pr_url': 'https://github.com/owner/kix/pull/2',
            'head': 'a'*40, 'branch': 'aiops/native-fixture', 'gate': 'ARCHITECTURE', 'requested_depth': 'A3'}}
        self.comment = audit_comment(self.requirement)
        self.pages = [[copy.deepcopy(self.comment)]]
        self.pull = {'number': 2, 'html_url': self.requirement['request_binding']['pr_url'],
                     'head': {'sha': 'a'*40, 'ref': 'aiops/native-fixture', 'repo': {'full_name': 'owner/kix'}},
                     'base': {'repo': {'full_name': 'owner/kix'}}}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch('mac_astra_receipt.decision_evidence', return_value=copy.deepcopy(receipts.DECISION)).start()
        patch('handoff.api', side_effect=self.api).start()

    def api(self, path, paginate=False):
        self.calls.append((path, paginate))
        if '/issues/comments/' in path: return copy.deepcopy(self.comment)
        if '/issues/2/comments?' in path: return copy.deepcopy(self.pages)
        if path.endswith('/pulls/2'): return copy.deepcopy(self.pull)
        raise AssertionError(path)

    def read(self, **kw): return receipts.read_receipt(self.requirement, 123, **kw)

    def test_pass_and_notes_are_read_directly_and_return_narrow_bound_evidence(self):
        for result in ('PASS', 'PASS_WITH_NOTES'):
            self.comment = audit_comment(self.requirement, result=result); self.pages = [[self.comment]]
            value = self.read()
            self.assertEqual(value['result'], result); self.assertEqual(value['head'], 'a'*40)
            self.assertEqual(value['body_sha256'], receipts.body_hash(self.comment['body']))
            self.assertEqual(value['auditor_session'], 'fixture-auditor-session')
            self.assertNotIn('body', value)
        self.assertIn(('repos/owner/kix/issues/comments/123', False), self.calls)
        self.assertIn(('repos/owner/kix/issues/2/comments?per_page=100', True), self.calls)

    def test_wrong_actor_number_repo_head_url_or_edits_fail_closed(self):
        original = copy.deepcopy(self.comment)
        changes = [('user', {'login':'BeautifulMind-JT','id':1,'type':'User'}),
                   ('user', {'login':'other','id':263336091,'type':'User'}),
                   ('id', 124), ('html_url', 'https://github.com/owner/other/pull/2#issuecomment-123'),
                   ('issue_url', 'https://api.github.com/repos/owner/kix/issues/3'),
                   ('updated_at', '2030-01-01T00:01:00Z'), ('body', original['body'].replace('head='+'a'*40, 'head='+'c'*40))]
        for field, value in changes:
            self.comment = {**original, field:value}; self.pages=[[self.comment]]
            with self.subTest(field=field), self.assertRaises(AppError): self.read()

    def test_failure_shallow_audit_contract_yes_and_inconsistent_schema_are_rejected(self):
        for result, depth in (('FAIL','A3'),('DECISION_REQUIRED','A3'),('PASS','A2')):
            self.comment=audit_comment(self.requirement,result=result,depth=depth);self.pages=[[self.comment]]
            with self.subTest(result=result,depth=depth), self.assertRaises(AppError): self.read()
        for old,new in (('VERIFIED_CONTRACT_CHANGE_REQUIRED: NO','VERIFIED_CONTRACT_CHANGE_REQUIRED: YES'),
                        ('AUDIT_RESULT: PASS','AUDIT_RESULT: FAIL'),
                        ('AUDIT_ATTEMPT_ID: 1','AUDIT_ATTEMPT_ID: 2'),
                        ('gate=ARCHITECTURE','gate=MILESTONE'),
                        ('AUDIT_RESULT: PASS','AUDIT_RESULT: PASS\nAUDIT_RESULT: PASS')):
            self.comment=audit_comment(self.requirement);self.comment['body']=self.comment['body'].replace(old,new)
            self.pages=[[self.comment]]
            with self.subTest(old=old), self.assertRaises(AppError): self.read()

    def test_body_hash_change_deletion_conflict_and_api_failure_are_rejected(self):
        expected=self.read()
        self.comment['body']+='\nEdited after pinning.';self.pages=[[self.comment]]
        with self.assertRaisesRegex(AppError,'RECEIPT_CHANGED'):self.read(expected=expected)
        self.comment=audit_comment(self.requirement);self.pages=[[]]
        with self.assertRaises(AppError):self.read(expected=expected)
        self.pages=[[self.comment,audit_comment(self.requirement,cid=124,result='FAIL')]]
        with self.assertRaisesRegex(AppError,'AUDIT_CONFLICT'):self.read()
        with patch('handoff.api',side_effect=AppError('COMMAND_FAILED')):
            with self.assertRaisesRegex(AppError,'RECEIPT_UNVERIFIED'):self.read()

    def test_live_remote_head_branch_and_repository_are_checked(self):
        for field,value in (('sha','c'*40),('ref','other'),('repo',{'full_name':'owner/other'})):
            old=copy.deepcopy(self.pull);self.pull['head'][field]=value
            with self.subTest(field=field),self.assertRaises(AppError):self.read()
            self.pull=old


class DecisionReceiptTests(unittest.TestCase):
    def test_live_decision_actor_body_url_and_edit_checks(self):
        body='Synthetic owner decision fixture.'
        value={'id':receipts.DECISION['comment_id'],'html_url':receipts.DECISION['url'],
               'issue_url':'https://api.github.com/repos/BeautifulMind-JT/ai-ops-control-plane/issues/78',
               'user':{'login':'BeautifulMind-JT','id':263336091,'type':'User'},
               'created_at':receipts.DECISION['created_at'],'updated_at':receipts.DECISION['created_at'],'body':body}
        with patch.dict(receipts.DECISION,body_sha256=receipts.body_hash(body)),patch('handoff.api',return_value=value):
            self.assertEqual(receipts.decision_evidence(),receipts.DECISION)
            for field,changed in (('body',body+' edit'),('updated_at','2030-01-01T00:00:00Z'),
                                  ('html_url','https://github.com/owner/other/pull/78#issuecomment-6011271646'),
                                  ('user',{'login':'other','id':263336091,'type':'User'})):
                bad={**value,field:changed}
                with self.subTest(field=field),patch('handoff.api',return_value=bad),self.assertRaises(AppError):
                    receipts.decision_evidence()


if __name__ == '__main__': unittest.main()
