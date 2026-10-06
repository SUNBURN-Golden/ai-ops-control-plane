"""Audit request binding is source context, never a fabricated Fable receipt."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
from common import AppError, digest
import mac_astra
from program_scope import load_scope


class AuditRequestTests(unittest.TestCase):
    def setUp(self):
        self.bound = {'repository': 'owner/kix', 'task_id': 'MAC-123-NODE', 'task_revision': 'a'*64,
                      'program': 'kix', 'node': 'agents-scope-sync', 'plan_commit': 'b'*40,
                      'plan_blob': 'c'*40, 'authority_kind': 'MAC_LOCAL', 'source_host': 'mac-host',
                      'generation_id': 'd'*32, 'work_sha256': 'e'*64}
        self.scope = {'repository': 'owner/kix', 'program': 'kix', 'blob': 'c'*40,
                      'path': '.aiops/program.json', 'approval_pointer': 'durable-user-decision',
                      'nodes': [{'id': 'agents-scope-sync', 'audit_floor': 'A3', 'astra_gate': 'NONE'}]}
        self.job = {'id': 'job', 'repository': 'owner/kix', 'head': 'f'*40, 'base_sha': 'b'*40,
                    'pr_url': 'https://github.com/owner/kix/pull/1',
                    'native_lineage': {'binding': self.bound, 'request_id': 'request', 'attempt_id': 'attempt'},
                    'audit_requirement': {'required': False, 'audit_receipt': 'model claimed PASS'}}

    def test_a3_promotes_gate_without_promoting_a_request_to_a_receipt(self):
        value = mac_astra.audit_requirement(self.job, self.scope)
        self.assertTrue(value['required'])
        self.assertEqual(value['astra_gate'], 'ARCHITECTURE')
        self.assertEqual(value['request_binding']['requested_depth'], 'A3')
        self.assertEqual(value['request_binding']['task_revision'], self.bound['task_revision'])
        self.assertEqual(value['request_binding']['canonical_binding'], self.bound)
        self.assertEqual(value['request_binding']['head'], self.job['head'])
        self.assertIsNone(value['audit_receipt'])
        self.assertEqual(value['receipt_support'], 'PROTECTED_MAC_CONNECTOR_REQUIRED')
        self.assertEqual(value['request_binding']['requested_auditor'], 'ASTRA_FABLE')
        self.assertNotIn('auditor_identity', value['request_binding'])
        self.assertNotIn('user_only_merge', value['request_binding'])

    def test_release_stays_reserved_for_user(self):
        self.scope['nodes'][0]['astra_gate'] = 'RELEASE'
        value = mac_astra.audit_requirement(self.job, self.scope)
        self.assertEqual(value['astra_gate'], 'RELEASE')
        self.assertTrue(value['request_binding']['declared_user_only_merge'])

    def test_url_form_program_repository_keeps_normal_delivery_and_a3_gates(self):
        for repo in ('https://github.com/owner/kix', 'https://github.com/Owner/Kix.git/'):
            for floor in ('A1', 'A2', 'A3'):
                program = {'schema_version': 1, 'program': 'kix', 'repository': repo,
                           'approval_pointer': 'durable-user-decision',
                           'authoritative_doc_pointers': 'engineering/AGENTS.md',
                           'nodes': [{'id': 'agents-scope-sync', 'title': 'Sync scope',
                                      'spec': 'Keep the original task scope.',
                                      'audit_floor': floor, 'astra_gate': 'NONE'}]}
                scope = load_scope(program, self.bound['repository'], self.bound['plan_blob'])
                with self.subTest(repository=repo, floor=floor):
                    self.assertEqual(scope['repository'], repo)
                    value = mac_astra.audit_requirement(self.job, scope)
                    self.assertEqual(value['required'], floor == 'A3')
                    self.assertEqual(value['astra_gate'], 'ARCHITECTURE' if floor == 'A3' else 'NONE')
                    self.assertEqual(value['request_binding']['repository'], 'owner/kix')
                    self.assertIsNone(value['audit_receipt'])

    def test_binding_and_job_repository_use_the_same_normalizer(self):
        self.bound['repository'] = 'https://github.com/owner/kix.git/'
        self.job['repository'] = 'https://github.com/OWNER/KIX/'
        value = mac_astra.audit_requirement(self.job, self.scope)
        self.assertEqual(value['request_binding']['repository'], 'owner/kix')
        self.assertEqual(value['request_binding']['canonical_binding'], self.bound)

    def test_missing_or_malformed_request_fields_raise_coded_scope_error(self):
        fields = (('job', 'id'), ('lineage', 'request_id'), ('lineage', 'attempt_id'),
                  ('binding', 'program'), ('binding', 'node'), ('scope', 'path'))
        for target, field in fields:
            for malformed in ('missing', None, '', ' ', 1, [], {}):
                job, scope = copy.deepcopy(self.job), copy.deepcopy(self.scope)
                document = {'job': job, 'lineage': job['native_lineage'],
                            'binding': job['native_lineage']['binding'], 'scope': scope}[target]
                if malformed == 'missing': document.pop(field)
                else: document[field] = malformed
                with self.subTest(target=target, field=field, value=malformed):
                    with self.assertRaises(AppError) as caught:
                        mac_astra.audit_requirement(job, scope)
                    self.assertEqual(caught.exception.code, 'MAC_HOST_AUDIT_SCOPE_UNVERIFIED')

    def test_invalid_repository_shapes_raise_coded_scope_error(self):
        for target in ('job', 'binding', 'scope'):
            for malformed in (None, '', 1, [], 'https://example.com/owner/kix', 'owner/other'):
                job, scope = copy.deepcopy(self.job), copy.deepcopy(self.scope)
                document = {'job': job, 'binding': job['native_lineage']['binding'], 'scope': scope}[target]
                document['repository'] = malformed
                with self.subTest(target=target, value=malformed):
                    with self.assertRaises(AppError) as caught:
                        mac_astra.audit_requirement(job, scope)
                    self.assertEqual(caught.exception.code, 'MAC_HOST_AUDIT_SCOPE_UNVERIFIED')

    def test_different_revision_head_attempt_or_pr_cannot_reuse_request_digest(self):
        original = mac_astra.audit_requirement(self.job, self.scope)['request_sha256']
        for field in ('task_revision', 'head', 'attempt_id', 'pr_url'):
            job = copy.deepcopy(self.job)
            if field == 'task_revision': job['native_lineage']['binding'][field] = '1'*64
            elif field == 'attempt_id': job['native_lineage'][field] = 'different-attempt'
            elif field == 'head': job[field] = '1'*40
            else: job[field] = 'https://github.com/owner/kix/pull/2'
            with self.subTest(field=field):
                self.assertNotEqual(mac_astra.audit_requirement(job, self.scope)['request_sha256'], original)

    def test_changed_repository_plan_blob_base_or_node_is_refused(self):
        for field, value in (('repository', 'owner/other'), ('blob', '1'*40), ('program', 'other')):
            scope = copy.deepcopy(self.scope); scope[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AppError, 'AUDIT_SCOPE_UNVERIFIED'):
                mac_astra.audit_requirement(self.job, scope)
        for field, value in (('repository', 'owner/other'), ('base_sha', '1'*40), ('head', None)):
            job = copy.deepcopy(self.job); job[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AppError, 'AUDIT_SCOPE_UNVERIFIED'):
                mac_astra.audit_requirement(job, self.scope)
        self.scope['nodes'] = []
        with self.assertRaisesRegex(AppError, 'AUDIT_SCOPE_UNVERIFIED'):
            mac_astra.audit_requirement(self.job, self.scope)

    def test_request_is_detached_from_mutable_job_context(self):
        value = mac_astra.audit_requirement(self.job, self.scope)
        frozen = copy.deepcopy(value['request_binding'])
        self.bound['task_revision'] = '1'*64
        self.assertEqual(value['request_binding'], frozen)
        self.assertEqual(value['request_sha256'], digest(frozen))

    def test_invalid_gates_are_refused_and_none_is_not_raised_to_a3(self):
        for field, value in (('audit_floor', 'A4'), ('astra_gate', 'MODEL_PASS')):
            scope = copy.deepcopy(self.scope); scope['nodes'][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AppError, 'AUDIT_SCOPE_UNVERIFIED'):
                mac_astra.audit_requirement(self.job, scope)
        self.scope['nodes'][0] = {'id': 'agents-scope-sync'}
        value = mac_astra.audit_requirement(self.job, self.scope)
        self.assertFalse(value['required'])
        self.assertEqual(value['receipt_support'], 'NOT_REQUIRED')


if __name__ == '__main__': unittest.main()
