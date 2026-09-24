import importlib.util
import json
import pathlib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('migration', pathlib.Path(__file__).resolve().parents[1] / 'tools/extract_control_plane.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Fake:
    def __init__(self, data):
        self.data = data
    def read(self, repo, entry):
        return self.data[entry['sha']]

def fixture():
    data, entries = {}, {}
    paths = [f'scripts/control_plane_fixture_{i}.py' for i in range(20)]
    paths += ['scripts/control_plane.py', 'AGENTS.md', '.github/control-plane/activation.json',
              '.github/control-plane/config.json', '.github/control-plane/flow-policy.example.json',
              '.github/workflows/control-plane-runtime.yml', 'TASKS/TEMPLATE.md',
              'RUNBOOKS/DISPATCH.md', 'runtime/product.rs', 'docs/IMPLEMENTATION_STATUS.md']
    for p in paths:
        content = b'example\n'
        if p == 'AGENTS.md':
            content = ('# Common\n\n' + m.MARKER + '\n\n# Local contract\nKeep rules.\n').encode()
        elif p.endswith('activation.json'):
            content = m.encoded(dict(schema_version=1, runtime_enabled=True, implementation_audit='PASS',
                                     runner_preflight='PASS', activated_runtime_sha='a' * 40))
        elif p.endswith('config.json'):
            content = m.encoded(dict(repository=m.SOURCE, project='KIX', enabled_builders=['DEVIN']))
        elif p.endswith('flow-policy.example.json'):
            content = m.encoded(dict(enabled=True, slack={'projects': {'KIX': m.SOURCE}},
                                     lanes={'DEVIN': {'enabled': True}}))
        sha = m.blob_sha(content)
        entries[p] = dict(path=p, sha=sha, mode='100644', type='blob')
        data[sha] = content
    return Fake(data), entries

class ExtractionTests(unittest.TestCase):
    def test_blob_sha_known(self):
        self.assertEqual(m.blob_sha(b''), 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')
    def test_selector_shared(self):
        for p in ['scripts/control_plane.py', 'scripts/control_plane_boundary_hook.sh',
                  'scripts/test_control_plane.py', '.github/control-plane/config.json']:
            self.assertTrue(m.selected(p))
    def test_selector_product_excluded(self):
        for p in ['runtime/crates/kix-kernel/src/lib.rs', '.github/workflows/protocol.yml',
                  'scripts/bootstrap.sh', 'docs/tasks/CP-BOUNDARY-003.md', 'validation/history.txt']:
            self.assertFalse(m.selected(p))
    def test_selector_unsafe_rejected(self):
        for p in ['/tmp/control_plane.py', '../scripts/control_plane.py']:
            with self.assertRaises(m.MigrationError): m.selected(p)
    def test_split_preserves_local_bytes(self):
        local = (m.MARKER + '\n\n원문 규칙.\n').encode()
        self.assertEqual(m.split_agents(b'common\n\n' + local)[1], local)
    def test_split_rejects_ambiguous(self):
        for v in [b'no marker', (m.MARKER * 2).encode()]:
            with self.assertRaises(m.MigrationError): m.split_agents(v)
    def test_clean_noop(self):
        self.assertEqual(m.clean_legacy_pointer(b'KIX product contract\n'), b'KIX product contract\n')
    def test_clean_unknown_appendix_stops(self):
        with self.assertRaises(m.MigrationError):
            m.clean_legacy_pointer(b'## KIX control-plane SoT pointer (E2)\nUNKNOWN')
    def test_reset_activation(self):
        v = json.loads(m.reset_activation(b'{"runtime_enabled":true}'))
        self.assertFalse(v['runtime_enabled'])
        self.assertEqual(v['user_activation_approval'], 'NOT_APPROVED')
        self.assertEqual(v['implementation_audit'], 'PENDING')
        self.assertEqual(v['runner_preflight'], 'PENDING')
        self.assertEqual(v['activated_runtime_sha'], 'PENDING')
        self.assertIsNone(v['user_activation_approval_pointer'])
    def test_import_inert_and_manifest(self):
        gh, entries = fixture()
        changes = m.build_import(gh, entries)
        self.assertNotIn('.github/workflows/control-plane-runtime.yml', changes)
        self.assertIn('engineering/.github/workflows/control-plane-runtime.yml', changes)
        self.assertNotIn('engineering/runtime/product.rs', changes)
        manifest = json.loads(changes['engineering/source-manifest.json'][1])
        for e in manifest['entries']:
            mode, data = changes[e['destination_path']]
            self.assertEqual(m.blob_sha(data), e['destination_blob'])
            self.assertEqual(mode, e['mode'])
        self.assertIn('engineering/provenance/kix/AGENTS.md', changes)
    def test_import_config_and_flow_disabled(self):
        gh, entries = fixture()
        changes = m.build_import(gh, entries)
        cfg = json.loads(changes['engineering/.github/control-plane/config.json'][1])
        flow = json.loads(changes['engineering/.github/control-plane/flow-policy.example.json'][1])
        self.assertEqual(cfg['repository'], m.DEST)
        self.assertFalse(flow['enabled'])
        self.assertFalse(flow['lanes']['DEVIN']['enabled'])
        self.assertEqual(flow['slack']['projects'], {})
        self.assertEqual(flow['repositories'], [])
    def test_incomplete_import_rejected(self):
        with self.assertRaises(m.MigrationError): m.build_import(Fake({}), {})
    def test_product_rules_template_preserved(self):
        gh, entries = fixture()
        changes = m.product_changes(gh, m.PRODUCTS[1], entries, 'a' * 40)
        self.assertNotIn('TASKS/TEMPLATE.md', changes)
        self.assertNotIn('runtime/product.rs', changes)
        self.assertIsNone(changes['scripts/control_plane.py'])
        self.assertIn(b'# Local contract\nKeep rules.\n', changes['AGENTS.md'][1])
        self.assertEqual(json.loads(changes[m.PRODUCT_STAMP][1])['control_source_commit'], 'a' * 40)
        self.assertIn('docs/IMPLEMENTATION_STATUS.md', changes)
    def test_locked_blobs_required(self):
        gh, entries = fixture()
        with self.assertRaises(m.MigrationError): m.product_changes(gh, m.SOURCE, entries, 'a' * 40)
        for p, sha in m.LOCKED.items(): entries[p] = dict(sha=sha, mode='100644')
        changes = m.product_changes(gh, m.SOURCE, entries, 'a' * 40)
        self.assertTrue(set(m.LOCKED).isdisjoint(changes))
    def test_plan_refuses_write(self):
        with self.assertRaises(m.MigrationError): m.GitHub(False).api('anything', 'POST', {})
    def test_no_gh_actionable_failure(self):
        with patch.object(m.subprocess, 'run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(m.MigrationError, 'gh CLI missing'): m.GitHub().api('repos/example')
    def test_api_failure_not_retried(self):
        with patch.object(m.subprocess, 'run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(m.MigrationError): m.GitHub().api('repos/example')
            self.assertEqual(run.call_count, 1)
    def test_soULBOUND_excluded(self):
        self.assertNotIn('BeautifulMind-JT/beautiful-mind', m.PRODUCTS)

if __name__ == '__main__': unittest.main()
