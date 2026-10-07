"""Transferred identity regression tests; no provider or GitHub network."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import control_plane as cp
sys.path.insert(0, str(cp.ROOT / 'mac_app'))
import repository_identity as mac_identity
from common import AppError
import handoff


def metadata(repo='SUNBURN-Golden/kix-protocol'):
    return {'id':cp.MIGRATED_REPOSITORY_IDS[repo], 'full_name':repo, 'fork':False,
            'private':True, 'archived':False,
            'owner':{'id':338877516, 'login':'SUNBURN-Golden', 'type':'Organization'}}


class RepositoryIdentityTests(unittest.TestCase):
    def test_all_five_exact_transferred_identities(self):
        for repo in cp.MIGRATED_REPOSITORY_IDS:
            cp.verify_current_repository(repo, metadata(repo))
            mac_identity.verify(repo, metadata(repo))

    def test_foreign_fork_owner_recreated_namespace_and_missing_proof_fail(self):
        changes=[{'id':1}, {'id':True}, {'fork':True}, {'private':False},
                 {'archived':True}, {'full_name':'BeautifulMind-JT/kix-protocol'},
                 {'owner':{'id':263336091,'login':'BeautifulMind-JT','type':'User'}},
                 {'owner':{'id':338877516,'login':'other','type':'Organization'}},
                 {'owner':{'id':338877516,'login':'SUNBURN-Golden','type':'User'}}]
        for change in changes:
            value={**metadata(),**change}
            with self.subTest(change=change), self.assertRaises(cp.ControlPlaneError):
                cp.verify_current_repository('SUNBURN-Golden/kix-protocol',value)
            with self.subTest(change=change), self.assertRaises(AppError):
                mac_identity.verify('SUNBURN-Golden/kix-protocol',value)

    def test_mac_old_alias_does_not_create_new_admission_or_read_foreign_namespace(self):
        with patch.object(handoff,'api') as read:
            for repository in ('BeautifulMind-JT/kix-protocol', 'beautifulmind-jt/KIX-PROTOCOL'):
                with self.subTest(repository=repository), self.assertRaises(AppError):
                    handoff.inspect_repository(repository)
            read.assert_not_called()

    def test_api_verifies_identity_before_any_mutation(self):
        api=cp.GithubApi('SUNBURN-Golden/kix-protocol','fixture-token')
        with patch.object(api,'_raw_request',return_value={**metadata(),'id':99}) as raw:
            with self.assertRaises(cp.ControlPlaneError):
                api.create_comment(92,'fixture')
            self.assertEqual([c.args[0] for c in raw.call_args_list],['GET'])

    def test_same_number_preserves_type_namespace_and_fragment_boundaries(self):
        current='https://github.com/SUNBURN-Golden/kix-protocol/issues/92'
        old='https://github.com/BeautifulMind-JT/kix-protocol/issues/92'
        self.assertTrue(cp.historical_pointer_matches(old,current,'SUNBURN-Golden/kix-protocol',92))
        for value in [old.replace('/92','/93'),old.replace('/issues/','/pull/'),
                      old.replace('kix-protocol','ZARI'),old+'#issuecomment-1',old.replace('https:','http:')]:
            self.assertFalse(cp.historical_pointer_matches(value,current,'SUNBURN-Golden/kix-protocol',92))

    def test_append_only_projection_is_bound_to_original_subject_bytes_and_source(self):
        envelope={'REPO':'BeautifulMind-JT/kix-protocol','TASK_ID':'KIX-T1','TASK_REVISION':'r1','BUILDER_ID':'CURSOR'}
        record={'repository':envelope['REPO'],'task_id':'KIX-T1','task_revision':'r1','builder_id':'CURSOR',
                'launch_request_id':'a'*24,'attempt_id':1,'claim_id':'b'*24,'owner_lane':None,'owner_session_id':None}
        originals=copy.deepcopy((envelope,record)); api=Mock(); actor={'id':263336091,'login':'BeautifulMind-JT','type':'User'}
        def create(number, body):
            return {'id':1,'user':actor,'created_at':'t','updated_at':'t','body':body}
        api.create_comment.side_effect=create
        with patch.object(cp,'migration_decision_verified'), patch.object(cp.subprocess,'check_output',return_value='c'*40+'\n'):
            cp.connect_historical_task(api,[],record,envelope,92,'fixture-token','SUNBURN-Golden/kix-protocol','original\n')
            body=api.create_comment.call_args.args[1]
            data=json.loads(body.split('```json\n')[1].split('\n```')[0])
            self.assertFalse(data['execution_authorized'])
            self.assertEqual(data['envelope_sha256'],hashlib.sha256(b'original\n').hexdigest())
            api.create_comment.reset_mock()
            existing=create(92,body)
            cp.connect_historical_task(api,[existing],record,envelope,92,'fixture-token','SUNBURN-Golden/kix-protocol','original\n')
            api.create_comment.assert_not_called()
            with self.assertRaises(cp.ControlPlaneError):
                cp.connect_historical_task(api,[],{**record,'task_id':'OTHER'},envelope,92,'fixture-token','SUNBURN-Golden/kix-protocol','original\n')
            with self.assertRaises(cp.ControlPlaneError):
                cp.connect_historical_task(api,[{**existing,'updated_at':'changed'}],record,envelope,92,'fixture-token','SUNBURN-Golden/kix-protocol','original\n')
        self.assertEqual((envelope,record),originals)


if __name__=='__main__': unittest.main()
