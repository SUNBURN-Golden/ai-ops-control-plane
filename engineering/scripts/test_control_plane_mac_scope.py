"""Pinned program intake and scope-loss regressions; no host/provider calls."""
import copy
import json
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1] / 'mac_app'
sys.path.insert(0, str(APP))
from common import AppError
import program_scope

BLOB = 'a' * 40
REPO = 'example/product'
POINTER = 'https://github.com/example/control-plane/issues/58'


def manifest(count=2):
    nodes = []
    for index in range(count):
        key = f'{index + 1:03}'
        nodes.append({'id': key, 'title': 'Deliver ' + key,
                      'spec': 'Original complete acceptance for ' + key + '\nRetain this failure fixture.',
                      'depends_on': [] if index == 0 else [f'{index:03}']})
    nodes[0].update(audit_floor='A3', astra_gate='RELEASE', user_merge=True,
                    astra_auto_merge=False, deliverable_mode='PR')
    return {'schema_version': 1, 'program': 'example-program', 'repository': REPO,
            'approval_pointer': POINTER, 'authoritative_doc_pointers': 'AGENTS.md ; docs/spec.md',
            'nodes': nodes}


def plan_for(value):
    return {'summary': 'Implement every original local node.', 'sources': [program_scope.PATH],
            'tasks': [{'id': node['id'], 'title': node['title'],
                       'instructions': 'Read the pinned scope.\n' + node['spec'],
                       'acceptance': ['Verify every original requirement and failure fixture.'],
                       'depends_on': copy.deepcopy(node.get('depends_on', []))}
                      for node in value['nodes']]}


class ScopeTests(unittest.TestCase):
    def test_original_specs_are_attached_locally_without_model_echo_or_input_mutation(self):
        value = manifest(); scope = self.load(value); plan = plan_for(value)
        for task in plan['tasks']: task['instructions'] = 'Implement and test this node.'
        original = copy.deepcopy(plan)
        bound = program_scope.bind_specs(plan, scope)
        self.assertEqual(plan, original)
        self.assertEqual(program_scope.validate_coverage(bound, scope), bound)
        for node, task in zip(value['nodes'], bound['tasks']):
            self.assertTrue(task['instructions'].startswith(node['spec']))
            self.assertIn('Implement and test this node.', task['instructions'])

    def load(self, value=None, repository=REPO, blob=BLOB):
        return program_scope.load_scope(json.dumps(value or manifest()), repository, blob)

    def assert_unsupported(self, value, code='PROGRAM_SCOPE_UNSUPPORTED'):
        with self.assertRaises(AppError) as caught:
            self.load(value)
        self.assertEqual(caught.exception.code, code)

    def test_pins_exact_original_nodes_without_promoting_gates_or_mutating_input(self):
        value = manifest()
        del value['nodes'][1]['depends_on']
        before = copy.deepcopy(value)
        scope = program_scope.load_scope(value, REPO, BLOB)
        self.assertEqual(scope['nodes'], before['nodes'])
        self.assertEqual(scope['count'], 2)
        self.assertEqual(scope['node_ids'], ['001', '002'])
        self.assertEqual(scope['blob'], BLOB)
        self.assertEqual(scope['approval_pointer'], POINTER)
        self.assertEqual(scope['authoritative_doc_pointers'], before['authoritative_doc_pointers'])
        self.assertEqual(scope['nodes'][0]['astra_gate'], 'RELEASE')
        self.assertNotIn('audit_floor', scope['nodes'][1])
        self.assertNotIn('depends_on', scope['nodes'][1])
        self.assertEqual(value, before)

    def test_73_numeric_nodes_keep_complete_scope_and_dependencies(self):
        value = manifest(73)
        scope = self.load(value)
        plan = plan_for(value)
        self.assertIs(program_scope.validate_coverage(plan, scope), plan)
        self.assertEqual(scope['count'], 73)
        self.assertEqual(scope['node_ids'][-1], '073')

    def test_opaque_decision_pointer_is_provenance_and_never_a_synthetic_receipt(self):
        value = manifest()
        value['approval_pointer'] = 'durable-user-decision-pointer'
        scope = self.load(value)
        self.assertEqual(scope['approval_pointer'], 'durable-user-decision-pointer')
        self.assertNotIn('approved', scope)
        self.assertNotIn('gate_pass', scope)

    def test_pending_or_explicitly_blocked_start_cannot_be_imported(self):
        for marker in ('PENDING_EXPANDED_SCOPE_REVIEW_DO_NOT_DISPATCH', 'pending',
                       'DO_NOT_DISPATCH', 'NOT_APPROVED', 'REJECTED'):
            value = manifest()
            value['approval_pointer'] = marker
            with self.subTest(marker=marker):
                self.assert_unsupported(value, 'PROGRAM_APPROVAL_PENDING')

    def test_unknown_top_schema_fields_cannot_implicitly_adopt_expanded_registration(self):
        for changes in ({'schema_version': 2}, {'schema_version': True},
                        {'registration_scope': {'approved': True}}, {'external_reader': True}):
            value = manifest()
            value.update(changes)
            with self.subTest(changes=changes):
                self.assert_unsupported(value)

    def test_missing_provenance_wrong_repository_or_unpinned_blob_refused(self):
        for field in ('repository', 'approval_pointer', 'authoritative_doc_pointers', 'program'):
            value = manifest()
            del value[field]
            with self.subTest(field=field):
                self.assert_unsupported(value)
        value = manifest()
        value['repository'] = 'other/product'
        self.assert_unsupported(value)
        for blob in ('main', 'a' * 39, None):
            with self.subTest(blob=blob), self.assertRaises(AppError):
                self.load(blob=blob)

    def test_optional_project_and_case_insensitive_github_target(self):
        value = manifest()
        value['project'] = 'EXAMPLE'
        scope = self.load(value, repository='Example/Product')
        self.assertEqual(scope['repository'], REPO)

    def test_external_dependencies_refused_even_when_field_is_empty(self):
        for deps in ([], [{'repository': 'other/product', 'node': 'one'}]):
            value = manifest()
            value['nodes'][0]['depends_on_external'] = deps
            with self.subTest(deps=deps):
                self.assert_unsupported(value)

    def test_duplicate_dangling_self_cyclic_and_malformed_dependencies_refused(self):
        for kind in ('duplicate', 'case_duplicate', 'dangling', 'self', 'cycle', 'duplicate_dep', 'bad_dep'):
            value = manifest()
            first, second = value['nodes']
            if kind == 'duplicate': second['id'] = first['id']
            elif kind == 'case_duplicate':
                first['id'] = 'Node'; second['id'] = 'node'; second['depends_on'] = []
            elif kind == 'dangling': second['depends_on'] = ['missing']
            elif kind == 'self': first['depends_on'] = ['001']
            elif kind == 'cycle': first['depends_on'] = ['002']
            elif kind == 'duplicate_dep': second['depends_on'] = ['001', '001']
            else: second['depends_on'] = [{}]
            with self.subTest(kind=kind):
                self.assert_unsupported(value)

    def test_unknown_node_fields_invalid_gates_and_conflicting_merge_flags_refused(self):
        for fields in ({'hidden_scope': True}, {'audit_floor': 'PASS'}, {'astra_gate': 'AUTO'},
                       {'deliverable_mode': 'DIRECT_MAIN'}, {'user_merge': 'true'},
                       {'astra_auto_merge': True}):
            value = manifest()
            value['nodes'][0].update(fields)
            with self.subTest(fields=fields):
                self.assert_unsupported(value)

    def test_identifiers_are_bounded_but_numeric_ids_are_valid(self):
        for key in ('', '-node', 'node/escape', 'x' * 81):
            value = manifest(1)
            value['nodes'][0]['id'] = key
            with self.subTest(key=key):
                self.assert_unsupported(value)
        value = manifest(1)
        value['nodes'][0]['id'] = '1' * 80
        self.assertEqual(self.load(value)['node_ids'], ['1' * 80])

    def test_json_duplicates_and_nonfinite_values_refused(self):
        for raw in ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}', '[]'):
            with self.subTest(raw=raw), self.assertRaises(AppError):
                program_scope.load_scope(raw, REPO, BLOB)

    def test_coverage_requires_canonical_source_and_every_original_node_exactly_once(self):
        value = manifest()
        scope = self.load(value)
        for kind in ('source', 'omission', 'extra', 'duplicate'):
            plan = plan_for(value)
            if kind == 'source': plan['sources'] = ['docs/spec.md']
            elif kind == 'omission': plan['tasks'].pop()
            elif kind == 'extra':
                extra = copy.deepcopy(plan['tasks'][0]); extra['id'] = 'new'; plan['tasks'].append(extra)
            else: plan['tasks'].append(copy.deepcopy(plan['tasks'][0]))
            with self.subTest(kind=kind), self.assertRaises(AppError) as caught:
                program_scope.validate_coverage(plan, scope)
            self.assertEqual(caught.exception.code, 'PROGRAM_COVERAGE_REQUIRED')

    def test_coverage_rejects_spec_dilution_and_dependency_weakening(self):
        value = manifest()
        scope = self.load(value)
        for kind in ('spec', 'drop_dep', 'add_dep', 'duplicate_dep'):
            plan = plan_for(value)
            if kind == 'spec': plan['tasks'][0]['instructions'] = 'Summarized implementation request.'
            elif kind == 'drop_dep': plan['tasks'][1]['depends_on'] = []
            elif kind == 'add_dep': plan['tasks'][0]['depends_on'] = ['002']
            else: plan['tasks'][1]['depends_on'] = ['001', '001']
            with self.subTest(kind=kind), self.assertRaises(AppError):
                program_scope.validate_coverage(plan, scope)

    def test_repository_without_program_keeps_existing_plan_contract(self):
        plan = plan_for(manifest())
        self.assertIs(program_scope.validate_coverage(plan, None), plan)


class DownloadedManifestTests(unittest.TestCase):
    """Use actual downloaded product snapshots when present in the workspace.

    The snapshots contain the pre-start PENDING pointer. Tests first prove it is
    refused, then replace only that pointer to exercise intake of its exact
    unchanged source nodes. This does not assert that a fixture is operational.
    """
    def test_actual_kix_73_and_zari_numeric_nodes_remain_exact(self):
        root = APP.parents[2] / 'program-expansion'
        paths = [root / repo / '.aiops/program.json' for repo in ('kix-protocol', 'ZARI')]
        if not all(path.is_file() for path in paths):
            self.skipTest('Downloaded product snapshots are absent; self-contained scope regressions still run.')
        for path, count in zip(paths, (73, 36)):
            value = json.loads(path.read_text())
            with self.subTest(repository=value['repository']):
                with self.assertRaises(AppError) as caught:
                    program_scope.load_scope(value, value['repository'], BLOB)
                self.assertEqual(caught.exception.code, 'PROGRAM_APPROVAL_PENDING')
                original_nodes = copy.deepcopy(value['nodes'])
                value['approval_pointer'] = POINTER
                scope = program_scope.load_scope(value, value['repository'], BLOB)
                self.assertEqual(scope['nodes'], original_nodes)
                self.assertEqual(scope['count'], count)
                program_scope.validate_coverage(plan_for(value), scope)
                if count == 36:
                    self.assertEqual(scope['node_ids'][0], '001')


if __name__ == '__main__':
    unittest.main()
