"""Synthetic bounded authority approval fixtures; no product/state/remote writes."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import common
import gitops
import handoff
import mac_authority_checkpoint as authority

NODE = json.loads((Path(__file__).parent / 'fixtures/mac_agents_scope_sync.json').read_text())


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class AuthorityCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.checkout = Path(self.temp.name); self.target = self.checkout / 'AGENTS.md'
        self.before = b'Original authority\n## 5. Scope\nOriginal pointers\n## 6. Unchanged locks\n'
        self.after = self.before.replace(b'Original pointers', b'Original pointers\nApproved bounded pointer')
        self.target.write_bytes(self.after)
        self.pin = {**authority.PINS, 'before_blob': blob(self.before), 'after_blob': blob(self.after)}
        self.scope = {'blob': self.pin['plan_blob'], 'nodes': [copy.deepcopy(NODE)]}
        bound = {'authority_kind': 'MAC_LOCAL', 'repository': self.pin['repository'], 'program': 'kix',
                 'node': self.pin['node'], 'plan_commit': self.pin['plan_commit'], 'plan_blob': self.pin['plan_blob']}
        task = {'id': NODE['id'], 'title': NODE['title'], 'instructions': NODE['spec']}
        self.job = {'id': self.pin['job'], 'repository': self.pin['repository'], 'branch': self.pin['branch'],
                    'base_sha': self.pin['plan_commit'], 'head': 'c'*40, 'state': 'building', 'attempt': None,
                    'native_lineage': {'binding': bound}, 'program_scope': copy.deepcopy(self.scope),
                    'plan': {'tasks': [task]}, 'current_task': task}
        self.repos = gitops.Repositories(self.checkout / 'workspaces')
        self.repos.path = lambda job: self.checkout
        self.repos.program_scope = lambda job: copy.deepcopy(self.scope)
        self.repos.assert_binding = lambda job: None; self.repos.assert_scope = lambda job: None
        self.repos.head = lambda job: 'c'*40; self.repos.clean = lambda job: True
        self.names = 'AGENTS.md\0'; self.old_blob = self.pin['before_blob']; self.calls = []
        def git(checkout, *args, **kwargs):
            self.calls.append(args)
            if args[:1] == ('status',): return ' M AGENTS.md\0'
            if args[:1] in (('diff',), ('ls-files',)): return self.names
            if args == ('rev-parse', self.pin['plan_commit'] + ':AGENTS.md'): return self.old_blob
            if args[:1] == ('hash-object',): return blob(self.target.read_bytes())
            return ''
        self.decision_body = 'Synthetic bounded User approval fixture; no live product authority.'
        self.approval = {'comment_id': 123456, 'body_sha256': hashlib.sha256(self.decision_body.encode()).hexdigest(),
                         'created_at': '2026-10-06T00:00:00Z', 'updated_at': '2026-10-06T00:00:00Z'}
        self.comment = {'id': self.approval['comment_id'], 'body': self.decision_body,
            'html_url': 'https://github.com/' + self.pin['repository'] + '/issues/92#issuecomment-123456',
            'issue_url': 'https://api.github.com/repos/' + self.pin['repository'] + '/issues/92',
            'user': copy.deepcopy(authority.ACTOR), 'created_at': self.approval['created_at'],
            'updated_at': self.approval['updated_at']}
        for p in (patch.object(authority, 'PINS', self.pin), patch.object(authority, 'APPROVAL', self.approval),
                  patch('gitops.git', side_effect=git), patch('handoff.api', side_effect=lambda path: copy.deepcopy(self.comment))):
            p.start(); self.addCleanup(p.stop)

    def allowed(self): return authority.approved(self.repos, self.job, 'AGENTS.md')

    def test_exact_scope_user_decision_and_bytes_allow_checkpoint_with_a3_unchanged(self):
        self.assertEqual(hashlib.sha256(NODE['spec'].encode()).hexdigest(), self.pin['spec_sha256'])
        self.assertTrue(self.allowed()); self.assertFalse(any(c[0] in ('add','commit','push') for c in self.calls))
        self.assertEqual(self.repos.checkpoint(self.job), 'c'*40)
        self.assertTrue(any(c[0] == 'commit' for c in self.calls)); self.assertEqual(self.scope['nodes'][0]['audit_floor'], 'A3')

    def test_missing_edited_deleted_foreign_or_other_issue_decision_refuses_before_staging(self):
        original = copy.deepcopy(self.comment)
        changes = [{'body': self.decision_body + ' edit'}, {'id': 1}, {'user': None},
                   {'user': {**authority.ACTOR, 'id': 1}}, {'user': {**authority.ACTOR, 'type': 'Bot'}},
                   {'updated_at': '2026-10-06T00:01:00Z'}, {'created_at': '2026-10-05T00:00:00Z'},
                   {'issue_url': original['issue_url'].replace('/92', '/93')},
                   {'html_url': original['html_url'].replace('/92#', '/93#')}]
        for change in changes:
            with self.subTest(change=change):
                self.comment = {**original, **change}; self.calls.clear()
                with self.assertRaisesRegex(common.AppError, '기준 계약'): self.repos.checkpoint(self.job)
                self.assertFalse(any(c[0] in ('add','commit','push') for c in self.calls))
        with patch('handoff.api', side_effect=common.AppError('REPOSITORY_ACCESS_REQUIRED')):
            self.assertFalse(self.allowed())
        with patch.object(authority, 'APPROVAL', None): self.assertFalse(self.allowed())

    def test_other_job_repo_node_plan_spec_gate_and_alias_do_not_inherit(self):
        original = copy.deepcopy(self.job)
        for key, value in [('id','other-job'), ('repository','owner/other'), ('branch','aiops/other'),
                           ('base_sha','d'*40), ('state','unknown'), ('attempt',{'id':'active'})]:
            self.job = {**copy.deepcopy(original), key: value}; self.assertFalse(self.allowed())
        for key, value in [('repository','owner/other'), ('node','scope-sync'), ('program','kix-agents'),
                           ('plan_commit','d'*40), ('plan_blob','e'*40), ('authority_kind','VM')]:
            self.job = copy.deepcopy(original); self.job['native_lineage']['binding'][key] = value
            self.assertFalse(self.allowed())
        self.job = copy.deepcopy(original); self.job['plan']['tasks'][0]['instructions'] += 'New scope'
        self.assertFalse(self.allowed())
        for key, value in [('audit_floor','A1'), ('user_merge',False), ('astra_auto_merge',True), ('astra_gate','RELEASE')]:
            self.job = copy.deepcopy(original); self.scope['nodes'][0] = {**NODE, key:value}
            self.job['program_scope'] = copy.deepcopy(self.scope); self.assertFalse(self.allowed())

    def test_other_file_mixed_changes_and_committed_authority_edits_remain_blocked(self):
        for name in ('nested/AGENTS.md', '.aiops/program.json', 'RUNBOOKS/new.md', 'docs/decisions/new.md'):
            self.assertFalse(authority.approved(self.repos, self.job, name))
            self.names = name+'\0'; self.calls.clear()
            with self.assertRaisesRegex(common.AppError, '기준 계약'): self.repos.checkpoint(self.job)
            self.assertFalse(any(c[0] in ('add','commit') for c in self.calls))
        self.names = 'AGENTS.md\0README.md\0'; self.assertFalse(self.allowed())

    def test_other_bytes_line_endings_links_mode_and_before_blob_refuse(self):
        for changed in (self.after + b'\n', self.after.replace(b'locks',b'unlocked'), self.after.replace(b'\n',b'\r\n')):
            self.target.write_bytes(changed); self.assertFalse(self.allowed())
        self.target.write_bytes(self.after); self.target.chmod(0o755); self.assertFalse(self.allowed())
        self.target.chmod(0o644); self.old_blob = 'f'*40; self.assertFalse(self.allowed())
        self.old_blob = self.pin['before_blob']; other = self.checkout/'other'; other.write_bytes(self.after)
        self.target.unlink(); self.target.symlink_to(other); self.assertFalse(self.allowed())
        self.target.unlink(); self.target.hardlink_to(other); self.assertFalse(self.allowed())

    def test_local_change_during_github_read_is_rechecked_before_staging(self):
        def changed(path):
            self.target.write_bytes(self.after + b'New authority'); return copy.deepcopy(self.comment)
        with patch('handoff.api', side_effect=changed):
            with self.assertRaises(common.AppError): self.repos.checkpoint(self.job)
        self.assertFalse(any(c[0] in ('add','commit') for c in self.calls))
        self.target.write_bytes(self.after)
        def mixed(path): self.names = 'AGENTS.md\0docs/decisions/new.md\0'; return copy.deepcopy(self.comment)
        with patch('handoff.api', side_effect=mixed): self.assertFalse(self.allowed())

    def test_approval_deletion_after_checkpoint_blocks_publication_before_any_push(self):
        self.repos.checkpoint(self.job); self.calls.clear(); self.job['state'] = 'publishing'
        with patch('handoff.api', side_effect=common.AppError('REPOSITORY_ACCESS_REQUIRED')), patch('gitops.gh') as gh:
            with self.assertRaises(common.AppError) as error: self.repos.publish(self.job)
        self.assertEqual(error.exception.code, 'AUTHORITY_EDIT_NEEDS_USER'); gh.assert_not_called()
        self.assertFalse(any(c[0] == 'push' for c in self.calls))

    def test_live_api_budget_is_used_and_reset_without_staging_after_timeout(self):
        clock = [0.0]
        def slow(path): clock[0] += 31; handoff.remaining_api_seconds(); return copy.deepcopy(self.comment)
        with patch('handoff.time.monotonic', side_effect=lambda:clock[0]), patch('handoff.api', side_effect=slow):
            self.assertFalse(self.allowed())
        self.assertIsNone(handoff.remaining_api_seconds())
        self.assertFalse(any(c[0] in ('add','commit') for c in self.calls))


if __name__ == '__main__': unittest.main()
