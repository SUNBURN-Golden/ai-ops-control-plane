"""Synthetic bounded authority approval fixtures; no product/state/remote writes."""
import copy
import hashlib
import json
import os
import subprocess
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
            if args[:1] == ('--no-replace-objects',): args = args[1:]
            if args == ('rev-parse', self.pin['plan_commit'] + ':.aiops/program.json'): return self.pin['plan_blob']
            if args[:1] == ('status',): return ' M AGENTS.md\0'
            if args[:1] in (('diff',), ('ls-files',)): return self.names
            if args == ('rev-parse', self.pin['plan_commit'] + ':AGENTS.md'): return self.old_blob
            if args[:1] == ('hash-object',): return blob(self.target.read_bytes())
            if args[:1] == ('ls-tree',): return '100644 blob ' + self.pin['after_blob'] + '\tAGENTS.md\0'
            return ''
        self.decision_body = 'Synthetic bounded User approval fixture; no live product authority.'
        self.approval = {'comment_id': 123456, 'body_sha256': hashlib.sha256(self.decision_body.encode()).hexdigest(),
                         'body_utf8_bytes': len(self.decision_body.encode('utf-8')),
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

    def test_scope_mismatch_in_approval_body_cannot_reuse_the_pinned_hash(self):
        for body in (self.decision_body.replace('bounded', 'unrestricted'),
                     self.decision_body.replace('User approval', 'another scope')):
            self.comment['body'] = body; self.assertFalse(self.allowed())
        self.comment['body'] = self.decision_body
        self.approval['body_utf8_bytes'] += 1; self.assertFalse(self.allowed())

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
        with patch('handoff.api', side_effect=common.AppError('REPOSITORY_ACCESS_REQUIRED')), \
                patch('gitops.gh', return_value=json.dumps({'nameWithOwner':self.pin['repository'], 'isPrivate':True})) as gh:
            with self.assertRaises(common.AppError) as error: self.repos.publish(self.job)
        self.assertEqual(error.exception.code, 'AUTHORITY_EDIT_NEEDS_USER')
        self.assertEqual(gh.call_count, 1); self.assertEqual(gh.call_args.args[1:3], ('repo', 'view'))
        self.assertFalse(any(c[0] == 'push' for c in self.calls))

    def test_live_api_budget_is_used_and_reset_without_staging_after_timeout(self):
        clock = [0.0]
        def slow(path): clock[0] += 31; handoff.remaining_api_seconds(); return copy.deepcopy(self.comment)
        with patch('handoff.time.monotonic', side_effect=lambda:clock[0]), patch('handoff.api', side_effect=slow):
            self.assertFalse(self.allowed())
        self.assertIsNone(handoff.remaining_api_seconds())
        self.assertFalse(any(c[0] in ('add','commit') for c in self.calls))


class CommittedPublicationTests(unittest.TestCase):
    """Real temporary Git objects; network reads and pushes remain synthetic."""
    def setUp(self):
        AuthorityCheckpointTests.setUp(self)
        self.git_env = {**os.environ, 'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_CONFIG_NOSYSTEM':'1'}
        self.push_hook = None
        def real_git(checkout, *args, **kwargs):
            self.calls.append(args)
            if args[:1] == ('push',):
                if self.push_hook: self.push_hook()
                return ''
            run = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                                  *args], cwd=checkout, env=self.git_env, capture_output=True, text=True, check=True)
            return run.stdout.rstrip('\n')
        self.real_git = lambda *args: real_git(self.checkout, *args)
        p = patch('gitops.git', side_effect=real_git); p.start(); self.addCleanup(p.stop)
        self.target.write_bytes(self.before)
        self.real_git('init', '-q'); self.real_git('config', 'user.name', 'Source fixture')
        self.real_git('config', 'user.email', 'fixture@example.invalid')
        (self.checkout / 'README.md').write_text('Unchanged fixture scope\n')
        (self.checkout / '.aiops').mkdir()
        (self.checkout / '.aiops/program.json').write_text(json.dumps({'nodes':[NODE]}))
        self.real_git('add', 'AGENTS.md', 'README.md', '.aiops/program.json')
        self.real_git('commit', '-q', '-m', 'Fixture base')
        self.pin['plan_commit'] = self.real_git('rev-parse', 'HEAD')
        self.pin['plan_blob'] = self.real_git('rev-parse', 'HEAD:.aiops/program.json')
        self.scope['blob'] = self.pin['plan_blob']; self.job['program_scope'] = copy.deepcopy(self.scope)
        self.job['native_lineage']['binding']['plan_blob'] = self.pin['plan_blob']
        self.job['base_sha'] = self.pin['plan_commit']
        self.job['native_lineage']['binding']['plan_commit'] = self.pin['plan_commit']
        self.repos.head = lambda job: self.real_git('rev-parse', 'HEAD')
        self.repos.clean = lambda job: not self.real_git('status', '--porcelain=v1', '-z')
        self.target.write_bytes(self.after); self.calls.clear()

    def checkpoint(self):
        self.job['head'] = self.repos.checkpoint(self.job); self.job['state'] = 'publishing'
        self.calls.clear(); return self.job['head']

    def remote(self, repo, *args, **kwargs):
        if args[:2] == ('repo', 'view'):
            return json.dumps({'nameWithOwner':self.pin['repository'], 'isPrivate':True})
        if args[:2] == ('pr', 'list'):
            return json.dumps([{'state':'OPEN', 'headRefOid':self.job['head'],
                                'url':'https://github.com/' + repo + '/pull/123'}])
        raise AssertionError(args)

    def assert_refused_before_push(self):
        with patch('gitops.gh', side_effect=self.remote):
            with self.assertRaises(common.AppError): self.repos.publish(self.job)
        self.assertFalse(any(c[0] == 'push' for c in self.calls))

    def test_exact_real_commit_tree_and_fixed_sha_publication_succeed(self):
        head = self.checkpoint()
        self.assertEqual(self.real_git('ls-tree', '-z', head, '--', 'AGENTS.md'),
                         '100644 blob ' + self.pin['after_blob'] + '\tAGENTS.md\0')
        with patch('gitops.gh', side_effect=self.remote): self.repos.publish(self.job)
        pushes = [c for c in self.calls if c[0] == 'push']
        self.assertEqual(pushes, [('push', '--porcelain', 'origin', head + ':refs/heads/' + self.pin['branch'])])

    def test_head_move_during_approval_or_repository_read_refuses_before_push(self):
        head = self.checkpoint()
        def move_head(): self.real_git('commit', '-q', '--allow-empty', '-m', 'Unreviewed same-tree commit')
        for read in ('approval', 'repository'):
            with self.subTest(read=read):
                self.real_git('reset', '--hard', head); self.calls.clear()
                def api(path):
                    if read == 'approval': move_head()
                    return copy.deepcopy(self.comment)
                def remote(repo, *args, **kwargs):
                    if read == 'repository' and args[:2] == ('repo', 'view'): move_head()
                    return self.remote(repo, *args, **kwargs)
                with patch('handoff.api', side_effect=api), patch('gitops.gh', side_effect=remote):
                    with self.assertRaises(common.AppError): self.repos.publish(self.job)
                self.assertFalse(any(c[0] == 'push' for c in self.calls))

    def test_ref_move_after_final_validation_cannot_substitute_push_source(self):
        head = self.checkpoint()
        self.push_hook = lambda: self.real_git('commit', '-q', '--allow-empty', '-m', 'Late ref move')
        with patch('gitops.gh', side_effect=self.remote): self.repos.publish(self.job)
        self.assertNotEqual(self.repos.head(self.job), head)
        self.assertEqual([c[3] for c in self.calls if c[0] == 'push'], [head + ':refs/heads/' + self.pin['branch']])

    def test_clean_working_mode_cannot_hide_executable_committed_tree(self):
        self.checkpoint(); self.real_git('config', 'core.filemode', 'false')
        self.real_git('update-index', '--chmod=+x', 'AGENTS.md')
        self.real_git('commit', '-q', '-m', 'Wrong committed mode')
        self.target.chmod(0o644); self.job['head'] = self.repos.head(self.job)
        self.assertTrue(self.repos.clean(self.job)); self.assertEqual(blob(self.target.read_bytes()), self.pin['after_blob'])
        self.assert_refused_before_push()

    def test_assume_unchanged_cannot_hide_wrong_committed_blob(self):
        self.checkpoint(); self.target.write_bytes(self.after + b'Unapproved committed authority\n')
        self.real_git('add', 'AGENTS.md'); self.real_git('commit', '-q', '-m', 'Wrong committed bytes')
        self.real_git('update-index', '--assume-unchanged', 'AGENTS.md')
        self.target.write_bytes(self.after); self.job['head'] = self.repos.head(self.job)
        self.assertTrue(self.repos.clean(self.job)); self.assertEqual(blob(self.target.read_bytes()), self.pin['after_blob'])
        self.assert_refused_before_push()

    def test_replacement_ref_cannot_hide_the_actual_commit_sent_by_push(self):
        approved_head = self.checkpoint()
        self.target.write_bytes(self.after + b'Unapproved original object\n')
        self.real_git('add', 'AGENTS.md'); self.real_git('commit', '-q', '-m', 'Wrong actual object')
        self.job['head'] = self.real_git('rev-parse', 'HEAD')
        self.real_git('replace', self.job['head'], approved_head)
        self.real_git('read-tree', approved_head); self.target.write_bytes(self.after)
        self.assertTrue(self.repos.clean(self.job))
        self.assertEqual(self.real_git('ls-tree', '-z', self.job['head'], '--', 'AGENTS.md'),
                         '100644 blob ' + self.pin['after_blob'] + '\tAGENTS.md\0')
        self.assertNotEqual(self.real_git('--no-replace-objects', 'rev-parse', self.job['head'] + ':AGENTS.md'),
                            self.pin['after_blob'])
        self.assert_refused_before_push()

    def test_clean_filter_cannot_make_wrong_commit_a_successful_checkpoint(self):
        (self.checkout / '.git/info/attributes').write_text('AGENTS.md filter=fixture\n')
        self.real_git('config', 'filter.fixture.clean', 'sed s/Approved/Unapproved/')
        with self.assertRaises(common.AppError) as error: self.repos.checkpoint(self.job)
        self.assertEqual(error.exception.code, 'AUTHORITY_EDIT_NEEDS_USER')
        self.assertEqual(blob(self.target.read_bytes()), self.pin['after_blob'])
        self.assertNotEqual(self.real_git('rev-parse', 'HEAD:AGENTS.md'), self.pin['after_blob'])
        self.assertFalse(any(c[0] == 'push' for c in self.calls))


class ApprovalArtifactTests(unittest.TestCase):
    def test_actual_pinned_comment_copy_metadata_and_scope_match(self):
        docs = Path(__file__).resolve().parents[1] / 'docs'
        record = json.loads((docs / 'MAC_AGENTS_SCOPE_APPROVAL_6018278031_RECORD.json').read_text())
        body = (docs / record['exact_body_copy']).read_bytes()
        self.assertEqual(len(body), authority.APPROVAL['body_utf8_bytes'])
        self.assertEqual(hashlib.sha256(body).hexdigest(), authority.APPROVAL['body_sha256'])
        self.assertEqual(record['body_sha256'], authority.APPROVAL['body_sha256'])
        self.assertEqual(record['body_utf8_bytes'], authority.APPROVAL['body_utf8_bytes'])
        for key in ('comment_id', 'created_at', 'updated_at'):
            self.assertEqual(record[key], authority.APPROVAL[key])
        self.assertEqual(record['actor'], authority.ACTOR)
        comment = {'id': record['comment_id'], 'body': body.decode('utf-8'), 'html_url': record['url'],
                   'issue_url': record['issue_url'], 'user': record['actor'],
                   'created_at': record['created_at'], 'updated_at': record['updated_at']}
        with patch('handoff.api', return_value=comment): self.assertTrue(authority.decision_verified())
        for key in ('job', 'branch', 'before_blob', 'after_blob', 'plan_commit'):
            self.assertIn(authority.PINS[key], comment['body'])
        self.assertIn('AGENTS.md §5', comment['body']); self.assertIn('A3 감사·병합은 그대로', comment['body'])
        self.assertNotIn(authority.PINS['plan_blob'], comment['body'])
        self.assertNotIn(authority.PINS['spec_sha256'], comment['body'])


if __name__ == '__main__': unittest.main()
