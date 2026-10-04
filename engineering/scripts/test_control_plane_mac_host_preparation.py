"""Host fetch pinning contracts; synthetic responses, no product/GitHub calls."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac_app'))
import common
import gitops


class HostPreparationTests(unittest.TestCase):
    def prepare(self,*,remote='a'*40,blob='b'*40,after='c'*40):
        self.calls=[];self.head_reads=0
        def git(checkout,*args,**kwargs):
            self.calls.append(args)
            if args==('rev-parse','HEAD'):
                self.head_reads+=1;return 'c'*40 if self.head_reads==1 else after
            if args==('rev-parse','FETCH_HEAD'):return remote
            if args[:1]==('rev-parse',):return blob
            return ''
        with patch('gitops.git',side_effect=git):
            return gitops.prepare_host_checkout('/fixture/assigned','main','a'*40,'b'*40)
    def test_host_fetch_precedes_provider_without_repin_rebase_merge_or_checkout(self):
        evidence=self.prepare()
        self.assertEqual(evidence['fetched_head'],evidence['base_sha'])
        self.assertEqual(evidence['plan_blob'],'b'*40);self.assertEqual(evidence['checkout_head'],'c'*40)
        self.assertIn(('fetch','--no-tags','origin','main'),self.calls)
        self.assertFalse(any(c[0] in ('merge','rebase','checkout','reset') for c in self.calls))
    def test_changed_remote_head_blocks_execution_without_moving_the_pin(self):
        with self.assertRaisesRegex(common.AppError,'REVISION_CHANGED'):self.prepare(remote='d'*40)
        self.assertFalse(any(c[0] in ('merge','rebase','checkout','reset') for c in self.calls))
    def test_changed_local_head_during_fetch_blocks_execution(self):
        with self.assertRaisesRegex(common.AppError,'REVISION_CHANGED'):self.prepare(after='d'*40)
    def test_original_plan_blob_mismatch_blocks_before_fetch(self):
        with self.assertRaisesRegex(common.AppError,'REVISION_CHANGED'):self.prepare(blob='d'*40)
        self.assertFalse(any(c[0]=='fetch' for c in self.calls))
    def test_ref_injection_rejected_before_any_git_command(self):
        for branch in ('--upload-pack=command','main:other','',None):
            with patch('gitops.git') as git:
                with self.assertRaisesRegex(common.AppError,'BRANCH_INVALID'):
                    gitops.prepare_host_checkout('/fixture/assigned',branch,'a'*40,'b'*40)
                git.assert_not_called()


if __name__=='__main__':unittest.main()
