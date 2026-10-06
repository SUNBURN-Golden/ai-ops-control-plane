"""Synthetic admission fixtures; no Linux reads, ownership release or product run."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import common
import core
import handoff
import mac_authority
import mac_generation
import mac_prestart as prestart
import native_transfer
import test_control_plane_mac_authority as fixtures
from test_control_plane_mac_generation import decision_fixture


def sha(body):
    return hashlib.sha256(body.encode()).hexdigest()


class PrestartAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = core.Store(Path(self.temp.name) / 'app'); self.addCleanup(self.store.close)
        self.source = mac_authority.LocalSource(self.store)
        self.worker = fixtures.FixtureWorker(self.store)
        self.controller = native_transfer.Controller(self.store, self.source, self.worker)
        self.control = {**prestart.SCOPE, 'schema_version': 1, 'project': 'KIX',
                        'launch_state': 'FAILED_PRESTART', 'owner_lane': None, 'owner_session_id': None}
        self.body = ('<!-- ASTRA_TASK_KEY_V1 program=kix node=agents-scope-sync request=' +
                     prestart.MATERIALIZATION + ' -->\n' + '\n'.join(name + ': ' + value for name, value in {
                     'TASK_ID': prestart.SCOPE['task_id'], 'TASK_REVISION': prestart.SCOPE['task_revision'],
                     'REPO': prestart.REPOSITORY, 'BUILDER_ID': 'CURSOR',
                     'CANONICAL_TASK_POINTER': prestart.ISSUE_URL}.items()))
        self.issue = {'number': 92, 'state': 'open', 'html_url': prestart.ISSUE_URL, 'body': self.body}
        task = {'number': 92, 'state': 'open', 'url': prestart.ISSUE_URL, 'program': 'kix',
                'node': 'agents-scope-sync', 'materialization_request_id': prestart.MATERIALIZATION,
                'declared_owners': ['CURSOR'], 'body_sha256': sha(self.body)}
        self.latest = fixtures.snapshot(repo=prestart.REPOSITORY, tasks=[task], nodes=[{
            'id': 'agents-scope-sync', 'title': 'Synthetic unchanged scope', 'spec': 'Fixture scope',
            'audit_floor': 'A2', 'astra_gate': 'NONE'}])
        self.control_pin = copy.deepcopy(prestart.CONTROL)
        self.confirmation_pin = copy.deepcopy(prestart.CONFIRMATION)
        self.control_pin['body_sha256'] = sha(self.control_body())
        self.confirmation_body = 'Synthetic User-confirmation fixture; not actual host qualification.'
        self.confirmation_pin['body_sha256'] = sha(self.confirmation_body)
        self.comments = [self.comment(self.control_pin, self.control_body()),
                         self.comment(self.confirmation_pin, self.confirmation_body)]
        for target, value in [('mac_prestart.CONTROL', self.control_pin),
                              ('mac_prestart.CONFIRMATION', self.confirmation_pin)]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        p = patch('handoff.api', side_effect=self.api); p.start(); self.addCleanup(p.stop)
        p = patch('handoff.inspect_repository', side_effect=lambda repo: copy.deepcopy(self.latest))
        p.start(); self.addCleanup(p.stop)
        self.source.initialize({'mode': 'MAC', 'decision': 'Explicit isolated fixture only'})

    def comment(self, pin, body):
        return {'id': pin['comment_id'], 'html_url': pin['url'], 'body': body,
                'issue_url': 'https://api.github.com/repos/' + prestart.REPOSITORY + '/issues/92',
                'user': copy.deepcopy(prestart.ACTOR)}

    def control_body(self):
        return prestart.MARKER + '\n```json\n' + common.encoded(self.control) + '\n```'

    def api(self, path, paginate=False):
        if path == mac_generation.DECISION_API: return decision_fixture()
        root = 'repos/' + prestart.REPOSITORY + '/issues/92'
        if path == root: return copy.deepcopy(self.issue)
        self.assertEqual(path, root + '/comments?per_page=100'); self.assertTrue(paginate)
        return [copy.deepcopy(self.comments)]

    def value(self):
        return {'repository': prestart.REPOSITORY, 'node': 'agents-scope-sync', 'generation_id': '1'*32,
                'decision': mac_generation.HOST_DECISION['url'], 'plan_commit': self.latest['source']['head'],
                'plan_blob': self.latest['source']['blob']}

    def adopt(self):
        return self.source.generation(self.value())

    def assert_blocked(self):
        before = list(self.store.db.iterdump())
        with self.assertRaises(common.AppError): self.adopt()
        self.assertEqual(list(self.store.db.iterdump()), before); self.assertEqual(self.worker.launched, 0)

    def test_exact_failed_prestart_and_user_confirmation_allow_normal_generation(self):
        bound = self.adopt()
        document = common.parse_json(self.store.db.execute('SELECT document FROM mac_host_generations').fetchone()[0])
        self.assertEqual(document['failed_prestart_evidence'], [prestart.evidence()])
        proof = document['failed_prestart_evidence'][0]
        self.assertFalse(proof['signed_host_receipt']); self.assertFalse(proof['mac_verified_host_read'])
        self.assertEqual(proof['user_confirmation']['source_message_id'], prestart.CONFIRMATION['source_message_id'])
        self.assertFalse(self.source.meta()['legacy_terminal_verified'])
        request = {'binding': bound, 'request_id': '2'*32}
        self.assertEqual(self.controller.start(request)['state'], 'RUNNING')
        self.assertEqual(self.controller.start(request)['state'], 'RUNNING'); self.assertEqual(self.worker.launched, 1)

    def test_original_rejected_intent_can_retry_after_the_exact_confirmation_arrives(self):
        confirmation = self.comments.pop(); self.assert_blocked()
        self.comments.append(confirmation)
        self.assertEqual(self.adopt(), self.adopt()); self.assertEqual(self.worker.launched, 0)

    def test_missing_edited_foreign_or_other_issue_confirmation_is_rejected(self):
        original = copy.deepcopy(self.comments)
        for change in ({'body': self.confirmation_body + ' Ambiguous restored interval'},
                       {'user': None}, {'user': {**prestart.ACTOR, 'id': 1}},
                       {'html_url': prestart.ISSUE_URL.replace('/92', '/93') + '#issuecomment-6016190250'},
                       {'issue_url': 'https://api.github.com/repos/' + prestart.REPOSITORY + '/issues/93'}):
            with self.subTest(change=change):
                self.comments = copy.deepcopy(original); self.comments[1].update(change); self.assert_blocked()
        self.comments = original[:1]; self.assert_blocked()

    def test_unresolved_session_and_wrong_exact_identity_remain_blocked_even_with_matching_hash(self):
        original = copy.deepcopy(self.control)
        changes = [{'launch_state': state} for state in ('UNKNOWN', 'SUBMITTING', 'CONFIRMED', 'RECONCILED')]
        changes += [{'owner_lane': 'CURSOR'}, {'owner_session_id': 'live-session'},
                    {'task_id': 'KIX-OTHER'}, {'task_revision': 'another-revision'},
                    {'launch_request_id': 'a'*24}, {'attempt_id': 2}, {'attempt_id': True},
                    {'repository': 'owner/another'}, {'builder_id': 'GLM'}]
        for change in changes:
            with self.subTest(change=change):
                self.control = {**original, **change}; body = self.control_body()
                self.control_pin['body_sha256'] = sha(body); self.comments[0]['body'] = body
                self.assert_blocked()
        for missing in ('owner_lane', 'owner_session_id'):
            self.control = copy.deepcopy(original); self.control.pop(missing); body = self.control_body()
            self.control_pin['body_sha256'] = sha(body); self.comments[0]['body'] = body; self.assert_blocked()

    def test_conflicting_duplicate_or_unauthorized_control_record_is_rejected(self):
        self.comments.append({**self.comments[0], 'id': 123, 'user': {'login': 'foreign'}})
        self.assert_blocked(); self.comments[-1]['id'] = self.comments[0]['id']; self.assert_blocked()

    def test_other_issue_materialization_revision_and_unkeyed_projection_cannot_reuse_confirmation(self):
        original = copy.deepcopy(self.latest)
        for change in ({'number': 93}, {'materialization_request_id': 'a'*24},
                       {'declared_owners': ['GLM']}, {'node': None}, {'body_sha256': 'd'*64}):
            with self.subTest(change=change):
                self.latest = copy.deepcopy(original); self.latest['tasks'][0].update(change); self.assert_blocked()
        self.latest = original; self.issue['body'] = self.body.replace(prestart.SCOPE['task_revision'], 'other')
        self.latest['tasks'][0]['body_sha256'] = sha(self.issue['body']); self.assert_blocked()

    def test_duplicate_same_original_issue_is_not_cleared_by_one_proof(self):
        self.latest['tasks'].append(copy.deepcopy(self.latest['tasks'][0])); self.assert_blocked()

    def test_live_record_change_after_adoption_blocks_preflight_and_stored_external_check(self):
        bound = self.adopt(); self.comments[0]['body'] += '\nChanged state'
        before = list(self.store.db.iterdump())
        with self.assertRaises(common.AppError): self.source.preflight(bound)
        with self.assertRaises(common.AppError): self.source.check_external(bound)
        self.assertEqual(list(self.store.db.iterdump()), before); self.assertEqual(self.worker.launched, 0)

    def test_owner_appearing_after_prepare_blocks_before_reservation_and_worker(self):
        bound = self.adopt(); prepare = self.worker.prepare
        def changed(*args):
            result = prepare(*args); self.comments[0]['body'] += '\nOwner changed'; return result
        with patch.object(self.worker, 'prepare', side_effect=changed):
            with self.assertRaises(common.AppError): self.controller.start({'binding': bound, 'request_id': '2'*32})
        self.assertEqual(self.worker.launched, 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mac_host_attempts').fetchone()[0], 0)

    def test_incomplete_history_and_fixture_injection_do_not_grant_authority(self):
        self.latest['task_scope'] = 'open'; self.assert_blocked()
        with self.assertRaises(common.AppError): self.source.generation({**self.value(), 'host_evidence': {}})


if __name__ == '__main__': unittest.main()
