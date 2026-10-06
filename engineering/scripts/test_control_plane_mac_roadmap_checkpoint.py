"""Pinned status-header and private completed-builder recovery regressions.

Fixtures only: no real AIOPS state, product model, GitHub or permission writes.
"""
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
import core
import gitops
import test_control_plane_mac_pipeline as pipeline_tests

NODE = json.loads((Path(__file__).parent / 'fixtures/mac_roadmap_status_header.json').read_text())


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class RoadmapHeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.checkout = Path(self.temp.name)
        self.target = self.checkout / gitops.ROADMAP_HEADER['path']
        self.target.parent.mkdir(parents=True)
        self.before = b'Title\n\nOld status\nOld effect\nUnchanged historical body\n'
        self.after = b'Title\n\nExisting approval status\nConditional follow-up pointer\nUnchanged historical body\n'
        self.target.write_bytes(self.after)
        self.pin = dict(gitops.ROADMAP_HEADER, before_blob=blob(self.before), after_blob=blob(self.after))
        pinning = patch.object(gitops, 'ROADMAP_HEADER', self.pin); pinning.start(); self.addCleanup(pinning.stop)
        self.scope = {'blob': self.pin['plan_blob'], 'nodes': [copy.deepcopy(NODE)]}
        task = {'id': NODE['id'], 'title': NODE['title'], 'instructions': NODE['spec']}
        bound = {'authority_kind': 'MAC_LOCAL', 'repository': self.pin['repository'], 'node': NODE['id'],
                 'plan_commit': self.pin['base'], 'plan_blob': self.pin['plan_blob']}
        self.job = {'repository': self.pin['repository'], 'base_sha': self.pin['base'],
                    'native_lineage': {'binding': bound}, 'program_scope': copy.deepcopy(self.scope),
                    'plan': {'tasks': [task]}, 'current_task': task}
        self.repos = gitops.Repositories(self.checkout / 'workspaces')
        self.repos.path = lambda job: self.checkout
        self.repos.program_scope = lambda job: copy.deepcopy(self.scope)
        self.old_blob = self.pin['before_blob']; self.calls = []
        def git(checkout, *args, **kwargs):
            self.calls.append(args)
            if args == ('rev-parse', self.pin['base'] + ':' + self.pin['path']): return self.old_blob
            if args[:1] == ('hash-object',): return blob(self.target.read_bytes())
            return ''
        reader = patch('gitops.git', side_effect=git); reader.start(); self.addCleanup(reader.stop)

    def allowed(self):
        return self.repos.approved_roadmap_header(self.job, self.pin['path'])

    def test_only_exact_header_bytes_with_the_original_spec_are_allowed(self):
        self.assertEqual(hashlib.sha256(NODE['spec'].encode()).hexdigest(), self.pin['spec_sha256'])
        self.assertTrue(self.allowed())
        self.assertFalse(any(call[0] in ('add', 'commit', 'push') for call in self.calls))

    def test_body_header_line_endings_and_executable_mode_changes_refuse(self):
        for changed in (self.after.replace(b'historical', b'rewritten'), self.after + b'\n',
                        self.after.replace(b'Existing approval', b'New authority'), self.after.replace(b'\n', b'\r\n')):
            with self.subTest(changed=changed):
                self.target.write_bytes(changed); self.assertFalse(self.allowed())
        self.target.write_bytes(self.after); self.target.chmod(0o755)
        self.assertFalse(self.allowed())

    def test_symlinks_and_hardlinks_refuse(self):
        other = self.checkout / 'other.md'; other.write_bytes(self.after)
        self.target.unlink(); self.target.symlink_to(other)
        self.assertFalse(self.allowed())
        self.target.unlink(); self.target.hardlink_to(other)
        self.assertFalse(self.allowed())

    def test_other_repositories_nodes_plans_specs_and_gates_do_not_inherit(self):
        original = copy.deepcopy(self.job)
        for field, value in [('repository', 'owner/other'), ('node', 'agents-scope-sync'),
                             ('authority_kind', 'VM'), ('plan_commit', 'd' * 40), ('plan_blob', 'e' * 40)]:
            self.job = copy.deepcopy(original); self.job['native_lineage']['binding'][field] = value
            with self.subTest(field=field): self.assertFalse(self.allowed())
        self.job = copy.deepcopy(original); self.job['repository'] = 'owner/other'; self.assertFalse(self.allowed())
        self.job = copy.deepcopy(original); self.job['plan']['tasks'][0]['instructions'] += '\nNew approval'
        self.assertFalse(self.allowed())
        self.job = copy.deepcopy(original); self.scope['nodes'][0]['audit_floor'] = 'A3'
        self.assertFalse(self.allowed()); self.scope['nodes'][0]['audit_floor'] = 'A1'
        self.scope['nodes'][0]['astra_gate'] = 'ARCHITECTURE'; self.assertFalse(self.allowed())

    def test_changed_base_or_missing_native_binding_refuses(self):
        self.old_blob = 'f' * 40; self.assertFalse(self.allowed())
        self.old_blob = self.pin['before_blob']; self.job.pop('native_lineage'); self.assertFalse(self.allowed())

    def test_general_authority_guard_still_refuses_before_any_staging(self):
        self.repos.assert_binding = lambda job: None; self.repos.assert_scope = lambda job: None
        for name in ('AGENTS.md', 'nested/AGENTS.md', '.aiops/program.json', 'RUNBOOKS/new.md',
                     'docs/decisions/other.md'):
            calls = []
            def git(checkout, *args, **kwargs):
                calls.append(args)
                return name + '\0' if args[:1] in (('status',), ('diff',)) else ''
            with self.subTest(name=name), patch('gitops.git', side_effect=git):
                with self.assertRaisesRegex(common.AppError, '기준 계약'): self.repos.checkpoint(self.job)
                self.assertFalse(any(call[0] in ('add', 'commit') for call in calls))


class CompletedCheckpointTests(unittest.TestCase):
    setUp = pipeline_tests.MacPipelineTests.setUp
    end_builder = pipeline_tests.MacPipelineTests.end_builder
    seed = pipeline_tests.MacPipelineTests.seed

    def blocked(self):
        job = self.seed(status='fail')
        with patch('core.agents.command', return_value=['fixture-not-executed']), patch('core.subprocess.Popen'):
            self.engine.launch(job, 'builder')
        job = self.store.get(job['id']); attempt = job['attempt']
        self.folder = self.store.directory / 'jobs' / job['id'] / attempt['id']
        pipeline_tests.private_outcome(self.folder, attempt, pipeline_tests.report(job['head']),
            profile=job['settings']['roles']['builder'], sid='fixture-completed-builder')
        with patch.object(self.repos, 'checkpoint', side_effect=common.AppError('AUTHORITY_EDIT_NEEDS_USER')):
            with self.assertRaises(common.AppError): self.engine.observe(job)
        job = self.store.get(job['id']); self.engine.operational_failure(job, 'AUTHORITY_EDIT_NEEDS_USER')
        return self.store.get(job['id'])

    def resumed(self):
        job = self.blocked()
        return self.store.action(job['id'], 'resume', {'answer': 'Fixture-only scoped checkpoint repair is installed.'})

    def test_normal_resume_reuses_proven_output_without_model_or_duplicate_writer(self):
        job = self.resumed(); before = copy.deepcopy(job)
        private_before = {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}
        with patch.object(self.engine, 'launch') as launch:
            self.engine.step(job)
        launch.assert_not_called(); current = self.store.get(job['id'])
        self.assertEqual(current['state'], 'reviewing'); self.assertEqual(current['phase'], 'reviewing')
        self.assertEqual(current['calls'], before['calls'])
        self.assertEqual(current['builder_sessions'], before['builder_sessions'])
        self.assertEqual(current['last_terminal'], before['last_terminal'])
        self.assertEqual(current['plan'], before['plan']); self.assertIsNone(current['checkpoint_retry'])
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}, private_before)
        self.assertEqual(self.store.events(job['id'])[-1]['kind'], 'checkpoint_recovered')
        with self.assertRaisesRegex(common.AppError, '재실행'): self.store.action(job['id'], 'resume', {})

    def test_changed_head_and_plan_refuse_without_checkpoint_or_model(self):
        job = self.resumed(); before = copy.deepcopy(job)
        for changed in ('head', 'plan'):
            current = copy.deepcopy(before)
            if changed == 'head': current['head'] = 'd' * 40
            else: current['plan']['tasks'][0]['instructions'] += 'Changed'
            with self.subTest(changed=changed), patch.object(self.repos, 'checkpoint') as checkpoint, patch.object(self.engine, 'launch') as launch:
                with self.assertRaises(common.AppError): self.engine.retry_checkpoint(current)
                checkpoint.assert_not_called(); launch.assert_not_called()
        self.assertEqual(self.store.get(job['id']), before)

    def test_wrong_request_profile_and_session_refuse_before_checkpoint(self):
        job = self.resumed(); saved = common.read_json(self.folder / 'request.json')
        for field, value in [('role', 'reviewer'), ('checkout', '/fixture/unrelated'), ('host_directory', '/fixture/other'),
                             ('profile', {'provider': 'codex', 'model': 'other'})]:
            current = dict(saved, **{field: value}); common.atomic_json(self.folder / 'request.json', current)
            with self.subTest(field=field), patch.object(self.repos, 'checkpoint') as checkpoint:
                with self.assertRaises(common.AppError): self.engine.retry_checkpoint(job)
                checkpoint.assert_not_called()
        common.atomic_json(self.folder / 'request.json', saved)
        changed = copy.deepcopy(job); changed['last_provider_evidence']['session_id'] = 'unrelated-session'
        with patch.object(self.repos, 'checkpoint') as checkpoint:
            with self.assertRaises(common.AppError): self.engine.retry_checkpoint(changed)
            checkpoint.assert_not_called()

    def test_missing_or_nonquiescent_receipt_refuses_without_relaunch(self):
        job = self.resumed(); receipt = common.read_json(self.folder / 'receipt.json')
        common.atomic_json(self.folder / 'receipt.json', dict(receipt, process_group_quiescent=False))
        with patch.object(self.repos, 'checkpoint') as checkpoint, patch.object(self.engine, 'launch') as launch:
            with self.assertRaises(common.AppError): self.engine.retry_checkpoint(job)
            checkpoint.assert_not_called(); launch.assert_not_called()
        (self.folder / 'receipt.json').unlink()
        with self.assertRaises(common.AppError): self.engine.retry_checkpoint(job)

    def test_unknown_or_failed_provider_cannot_be_recovered_as_completed_output(self):
        job = self.blocked()
        self.store.update(job['id'], state='unknown')
        with self.assertRaises(common.AppError): self.store.action(job['id'], 'resume', {'answer': 'fixture'})
        terminal = dict(job['last_terminal'], status='fail')
        self.store.update(job['id'], state='needs_user', last_terminal=terminal)
        current = self.store.action(job['id'], 'resume', {'answer': 'fixture'})
        self.assertFalse(current.get('checkpoint_retry'))


if __name__ == '__main__': unittest.main()
