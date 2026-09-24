import base64
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
            content = ('# Common\n\n' + m.AGENTS_MARKERS[m.SOURCE] + '\n\n# Local contract\nKeep rules.\n').encode()
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

class FakeGH(Fake):
    """In-memory remote for plan/binding tests. No branches exist unless added."""
    def __init__(self):
        super().__init__({})
        self.mains = {}      # repo -> sha string
        self.entries = {}    # repo -> {path: entry}
        self.branches = {}   # repo -> head sha (existing migration branch)
    def add_repo(self, repo, entries, data, main='m' * 40):
        self.entries[repo] = entries
        self.data.update(data)
        self.mains[repo] = main
    def api(self, path, method='GET', value=None):
        raise AssertionError(f'unexpected api call {path}')
    def main(self, repo):
        return self.mains[repo]
    def commit_tree(self, repo, sha):
        return m.SOURCE_TREE if repo == m.SOURCE else 'tree'
    def tree(self, repo, ref):
        if repo == m.SOURCE and ref == m.SOURCE_SHA:
            return m.SOURCE_TREE, self.entries[repo]
        return 'tree-' + ref[:8], self.entries[repo]
    def working(self, repo):
        head = self.branches.get(repo)
        if head is None:
            return self.mains[repo], 'tree', self.entries[repo], False
        return head, 'tree', self.entries[repo], True

def plan_fixture():
    gh = FakeGH()
    source_entries, source_data = {}, {}
    paths = [f'scripts/control_plane_fixture_{i}.py' for i in range(20)]
    paths += ['scripts/control_plane.py', 'AGENTS.md', '.github/control-plane/activation.json',
              '.github/control-plane/config.json', '.github/control-plane/flow-policy.example.json',
              '.github/workflows/control-plane-runtime.yml']
    for p in paths:
        content = b'example\n'
        if p == 'AGENTS.md':
            content = ('# Common\n\n' + m.AGENTS_MARKERS[m.SOURCE] + '\n\n# KIX local\n').encode()
        elif p.endswith('activation.json'):
            content = m.encoded(dict(runtime_enabled=True))
        elif p.endswith('config.json'):
            content = m.encoded(dict(repository=m.SOURCE))
        elif p.endswith('flow-policy.example.json'):
            content = m.encoded(dict(enabled=True, slack={'projects': {}}, lanes={}))
        sha = m.blob_sha(content)
        source_entries[p] = dict(path=p, sha=sha, mode='100644', type='blob')
        source_data[sha] = content
    for p, sha in m.LOCKED.items():
        source_entries[p] = dict(path=p, sha=sha, mode='100644', type='blob')
        source_data[sha] = b'locked\n'
    gh.add_repo(m.SOURCE, source_entries, source_data, main=m.SOURCE_SHA)
    gh.entries[m.DEST] = {}
    gh.mains[m.DEST] = 'd' * 40
    for repo in m.PRODUCTS[1:]:
        marker = m.AGENTS_MARKERS[repo]
        content = ('# Common\n\n' + marker + '\n\nlocal rules ' + repo + '\n').encode()
        entries = {'AGENTS.md': dict(path='AGENTS.md', sha=m.blob_sha(content), mode='100644', type='blob'),
                   'scripts/control_plane.py': dict(path='scripts/control_plane.py', sha=m.blob_sha(b'x\n'), mode='100644', type='blob')}
        gh.add_repo(repo, entries, {m.blob_sha(content): content, m.blob_sha(b'x\n'): b'x\n'})
    return gh

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

    # --- P1-1: explicit per-repository AGENTS boundary markers ---
    def test_per_repo_markers_explicit(self):
        self.assertEqual(m.AGENTS_MARKERS[m.SOURCE], '# Repository-specific engineering rules (preserved)')
        self.assertEqual(m.AGENTS_MARKERS['BeautifulMind-JT/ZARI'], '# Repository-specific engineering rules (preserved)')
        self.assertEqual(m.AGENTS_MARKERS['BeautifulMind-JT/film-unit-mv-studio'], '## Repository-specific engineering constraints')
        self.assertEqual(m.AGENTS_MARKERS['BeautifulMind-JT/maeum-gyeol'], '## Repository-specific engineering constraints')
        self.assertEqual(sorted(m.AGENTS_MARKERS), sorted(m.PRODUCTS))
    def test_split_preserves_local_bytes(self):
        marker = m.AGENTS_MARKERS[m.SOURCE]
        local = (marker + '\n\n원문 규칙.\n').encode()
        self.assertEqual(m.split_agents(b'common\n\n' + local, marker)[1], local)
    def test_split_suffix_bytes_preserved_all_products(self):
        for repo in m.PRODUCTS:
            marker = m.AGENTS_MARKERS[repo]
            local = (marker + '\n\nlocal \xc3\xa9 bytes\nlast-no-newline').encode()
            got = m.split_agents(b'common\n\n' + local, marker)
            self.assertEqual(got[1], local)
    def test_split_requires_exact_marker_line(self):
        marker = m.AGENTS_MARKERS[m.SOURCE]
        # marker text embedded in a longer heading must NOT be treated as the boundary
        data = ('# Common\n\n' + marker + ' extra words\n\nlocal\n').encode()
        with self.assertRaises(m.MigrationError):
            m.split_agents(data, marker)
    def test_split_does_not_enlarge_via_other_headings(self):
        marker = m.AGENTS_MARKERS[m.SOURCE]
        earlier = '## Repository-specific technical contracts (preserved)'
        data = ('# Common\n\n' + earlier + '\nkeep me\n\n' + marker + '\n\nlocal\n').encode()
        common, local = m.split_agents(data, marker)
        self.assertIn(earlier.encode(), common)
        self.assertIn(b'keep me', common)
        self.assertEqual(local, (marker + '\n\nlocal\n').encode())
    def test_split_rejects_ambiguous(self):
        marker = m.AGENTS_MARKERS[m.SOURCE]
        for v in [b'no marker', (marker + '\nx\n' + marker + '\n').encode()]:
            with self.assertRaises(m.MigrationError): m.split_agents(v, marker)
    def test_marker_for_unknown_repo_rejected(self):
        with self.assertRaises(m.MigrationError): m.marker_for('BeautifulMind-JT/beautiful-mind')

    # --- FILM legacy pointer: exact match only ---
    def test_clean_noop(self):
        self.assertEqual(m.clean_legacy_pointer(b'KIX product contract\n'), b'KIX product contract\n')
    def test_clean_unknown_appendix_stops(self):
        with self.assertRaises(m.MigrationError):
            m.clean_legacy_pointer(b'## KIX control-plane SoT pointer (E2)\nUNKNOWN')
    def test_clean_legacy_pointer_exact_removal(self):
        title = '## KIX control-plane SoT pointer (E2)'
        expected = (title + '\n\n'
            'Control-plane SoT, dispatch policy, and production activation are managed in '
            '**BeautifulMind-JT/kix-protocol**, not in this sibling tree.\n'
            'See [`docs/KIX_CONTROL_PLANE_POINTER.md`](docs/KIX_CONTROL_PLANE_POINTER.md).\n'
            'Do not enable sibling runtime, copy full policy, or change activation/workflows from this pointer PR.')
        out = m.clean_legacy_pointer(('# Governance\n\n' + expected + '\n\nrest\n').encode())
        self.assertEqual(out, '# Governance\n\n\n\nrest\n'.encode())

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
        changes, reasons = m.build_import(gh, entries)
        self.assertNotIn('.github/workflows/control-plane-runtime.yml', changes)
        self.assertIn('engineering/.github/workflows/control-plane-runtime.yml', changes)
        self.assertNotIn('engineering/runtime/product.rs', changes)
        manifest = json.loads(changes['engineering/source-manifest.json'][1])
        for e in manifest['entries']:
            mode, data = changes[e['destination_path']]
            self.assertEqual(m.blob_sha(data), e['destination_blob'])
            self.assertEqual(mode, e['mode'])
            if e['transformed']:
                self.assertTrue(e.get('reason'), 'transformed entries need a reason')
                self.assertTrue(e.get('source_path') and e.get('source_blob'))
        self.assertIn('engineering/provenance/kix/AGENTS.md', changes)
        reasons = {e['source_path']: e.get('reason') for e in manifest['entries']}
        self.assertEqual(reasons['AGENTS.md'], 'agents_common_extracted')
        self.assertEqual(reasons['.github/control-plane/activation.json'], 'activation_reset')
    def test_import_config_and_flow_disabled(self):
        gh, entries = fixture()
        changes, _ = m.build_import(gh, entries)
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
        changes, _ = m.product_changes(gh, m.PRODUCTS[1], entries, 'a' * 40)
        self.assertNotIn('TASKS/TEMPLATE.md', changes)
        self.assertNotIn('runtime/product.rs', changes)
        self.assertIsNone(changes['scripts/control_plane.py'])
        self.assertIn(b'# Local contract\nKeep rules.\n', changes['AGENTS.md'][1])
        self.assertEqual(json.loads(changes[m.PRODUCT_STAMP][1])['control_source_commit'], 'a' * 40)
        self.assertIn('docs/IMPLEMENTATION_STATUS.md', changes)
    def test_product_changes_per_repo_marker(self):
        # ZARI and FILM markers differ; each must split on its own marker only.
        gh, entries = fixture()
        film = 'BeautifulMind-JT/film-unit-mv-studio'
        marker = m.AGENTS_MARKERS[film]
        content = ('# Common\n\n' + marker + '\n\nFILM local\n').encode()
        entries['AGENTS.md'] = dict(path='AGENTS.md', sha=m.blob_sha(content), mode='100644', type='blob')
        gh.data[m.blob_sha(content)] = content
        changes, _ = m.product_changes(gh, film, entries, 'a' * 40)
        self.assertIn(b'FILM local\n', changes['AGENTS.md'][1])
        self.assertTrue(changes['AGENTS.md'][1].startswith(b'# Product agent governance'))
        # wrong marker must refuse, never silently preserve
        with self.assertRaises(m.MigrationError):
            m.product_changes(gh, m.PRODUCTS[1], entries, 'a' * 40)
    def test_locked_blobs_required(self):
        gh, entries = fixture()
        with self.assertRaises(m.MigrationError): m.product_changes(gh, m.SOURCE, entries, 'a' * 40)
        for p, sha in m.LOCKED.items(): entries[p] = dict(sha=sha, mode='100644')
        changes, _ = m.product_changes(gh, m.SOURCE, entries, 'a' * 40)
        self.assertTrue(set(m.LOCKED).isdisjoint(changes))

    # --- P1-2 / P2: plan binding and machine-readable evidence ---
    def test_plan_actions_schema(self):
        gh = plan_fixture()
        plan = m.build_plan(gh)
        self.assertEqual(plan['task'], m.TASK)
        self.assertTrue(plan['plan_id'])
        for repo in [m.DEST] + m.PRODUCTS:
            self.assertIn(repo, plan['bindings'])
            self.assertEqual(plan['bindings'][repo]['main_sha'], gh.mains[repo])
        for a in plan['actions']:
            for key in ['repo', 'action', 'path', 'before_blob', 'after_blob', 'main_sha', 'head_sha']:
                self.assertIn(key, a)
            self.assertIn(a['action'], {'add', 'update', 'delete', 'preserve'})
            self.assertEqual(a['main_sha'], gh.mains[a['repo']])
            if a['action'] == 'delete':
                                self.assertIsNone(a['after_blob'])
        dest_deletes = [a for a in plan['actions'] if a['repo'] == m.DEST and a['action'] == 'delete']
        self.assertEqual(dest_deletes, [])
        adds = [a for a in plan['actions'] if a['repo'] == m.DEST and a['action'] == 'add']
        self.assertTrue(any(a['path'] == 'engineering/AGENTS.md' for a in adds))
        deletes = [a for a in plan['actions'] if a['repo'] != m.DEST and a['action'] == 'delete']
        self.assertTrue(any(a['path'] == 'scripts/control_plane.py' for a in deletes))
        preserved = [a for a in plan['actions'] if a['action'] == 'preserve' and a['path'] in m.LOCKED]
        self.assertEqual({a['path'] for a in preserved}, set(m.LOCKED))
        for a in preserved:
            self.assertEqual(a['before_blob'], a['after_blob'])
            self.assertEqual(a['after_blob'], m.LOCKED[a['path']])
    def test_plan_binding_stable(self):
        gh = plan_fixture()
        self.assertEqual(m.build_plan(gh)['plan_id'], m.build_plan(gh)['plan_id'])
    def test_plan_binding_rejects_main_drift(self):
        gh = plan_fixture()
        plan = m.build_plan(gh)
        gh.mains['BeautifulMind-JT/ZARI'] = '0' * 40
        with self.assertRaisesRegex(m.MigrationError, 'plan'):
            m.check_plan(gh, plan)
    def test_plan_binding_rejects_head_drift(self):
        gh = plan_fixture()
        plan = m.build_plan(gh)
        gh.branches[m.DEST] = 'f' * 40  # foreign head appeared after plan
        gh.entries[m.DEST] = {}
        with self.assertRaisesRegex(m.MigrationError, 'plan'):
            m.check_plan(gh, plan)
    def test_plan_binding_rejects_preimage_drift(self):
        gh = plan_fixture()
        plan = m.build_plan(gh)
        e = gh.entries['BeautifulMind-JT/ZARI']['scripts/control_plane.py']
        e['sha'] = m.blob_sha(b'changed\n')
        gh.data[e['sha']] = b'changed\n'
        with self.assertRaisesRegex(m.MigrationError, 'plan'):
            m.check_plan(gh, plan)
    def test_plan_accepts_matching_binding(self):
        gh = plan_fixture()
        plan = m.build_plan(gh)
        self.assertEqual(m.check_plan(gh, plan)['plan_id'], plan['plan_id'])
    def test_plan_integrity_detects_tamper(self):
        gh = plan_fixture()
        plan = m.build_plan(gh)
        plan['actions'][0]['after_blob'] = '0' * 40
        with self.assertRaises(m.MigrationError):
            m.check_plan(gh, plan)

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
