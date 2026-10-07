"""Transferred URLs retain the original authority, body and receipt identity."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import handoff
import repository_identity as identity
from common import AppError

OLD = 'BeautifulMind-JT/kix-protocol'
NEW = 'SUNBURN-Golden/kix-protocol'
CENTRAL = 'SUNBURN-Golden/ai-ops-control-plane'


def metadata(repo):
    return dict(id=identity.PINS[repo], full_name=repo, private=True, fork=False, archived=False,
                owner=dict(id=338877516, login='SUNBURN-Golden', type='Organization'))


def decision():
    pin = identity.COMPAT_DECISION
    return dict(id=pin['comment_id'], user=dict(id=263336091, login='BeautifulMind-JT', type='User'),
                created_at=pin['created_at'], updated_at=pin['created_at'],
                html_url=f'https://github.com/{CENTRAL}/pull/83#issuecomment-{pin["comment_id"]}',
                issue_url=f'https://api.github.com/repos/{CENTRAL}/issues/83',
                body=(Path(__file__).resolve().parents[1] / 'docs/MAC_SAME_ID_URL_COMPAT_DECISION_20261007.md').read_text())


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.calls = []; self.pin = decision(); self.repo = metadata(NEW)
        def read(path, paginate=False):
            self.calls.append(path)
            if path == 'repos/' + NEW: return copy.deepcopy(self.repo)
            if path == 'repos/' + CENTRAL: return metadata(CENTRAL)
            if path == f'repos/{CENTRAL}/issues/comments/6030780072': return copy.deepcopy(self.pin)
            if path == 'repos/' + NEW + '/issues/92': return dict(number=92, html_url=f'https://github.com/{NEW}/issues/92')
            raise AssertionError(path)
        self.patch = patch.object(handoff, '_raw_api', side_effect=read); self.patch.start(); self.addCleanup(self.patch.stop)

    def test_original_url_is_only_linked_to_same_verified_id_number_and_kind(self):
        old = f'https://github.com/{OLD}/issues/92#issuecomment-6018278031'
        new = old.replace(OLD, NEW)
        with handoff.api_read_budget():
            self.assertTrue(identity.url_matches(old, new))
            self.assertEqual(identity.canonical_url(OLD, new), old)
            for bad in (new.replace('/92#', '/93#'), new.replace('/issues/', '/pull/'),
                        new.replace('6018278031', '6018278032'), new+'?x=1', new.replace('https:', 'http:'),
                        new.replace(NEW, 'SUNBURN-Golden/ZARI'), new.replace(NEW, 'attacker/kix-protocol')):
                with self.subTest(bad=bad): self.assertFalse(identity.url_matches(old, bad))
        self.assertIn('repos/' + NEW, self.calls)
        self.assertFalse(any('repos/' + OLD in call for call in self.calls))

    def test_legacy_api_uses_current_endpoint_but_returns_unmodified_actual_response(self):
        with handoff.api_read_budget():
            actual = handoff.api('repos/' + OLD + '/issues/92')
        self.assertEqual(actual, dict(number=92, html_url=f'https://github.com/{NEW}/issues/92'))
        self.assertFalse(any('repos/' + OLD in call for call in self.calls))

    def test_foreign_identity_or_changed_scoped_decision_never_links(self):
        originals = copy.deepcopy((self.repo, self.pin))
        for change in ('repo_id', 'owner', 'fork', 'actor', 'body', 'edited', 'comment_id'):
            self.repo, self.pin = copy.deepcopy(originals)
            if change == 'repo_id': self.repo['id'] = 99
            if change == 'owner': self.repo['owner']['id'] = 263336091
            if change == 'fork': self.repo['fork'] = True
            if change == 'actor': self.pin['user']['id'] = 99
            if change == 'body': self.pin['body'] += '\nchanged'
            if change == 'edited': self.pin['updated_at'] = '2030-01-01T00:00:00Z'
            if change == 'comment_id': self.pin['id'] += 1
            with self.subTest(change=change), handoff.api_read_budget(), self.assertRaises(AppError):
                identity.url_matches(f'https://github.com/{OLD}/issues/92', f'https://github.com/{NEW}/issues/92')

    def test_fresh_old_name_inspection_still_rejected_without_reading(self):
        with self.assertRaisesRegex(AppError, 'SUBJECT_REQUIRED'): handoff.inspect_repository(OLD)
        self.assertEqual(self.calls, [])

    def test_receipt_bytes_and_original_pointer_survive_transport_change(self):
        import mac_astra_receipt as receipts
        from test_control_plane_mac_astra_receipt import audit_comment
        requirement = {'request_binding': {'repository': OLD, 'pr_url': f'https://github.com/{OLD}/pull/2',
                       'head': 'a'*40, 'gate': 'ARCHITECTURE', 'requested_depth': 'A3'}}
        original = audit_comment(requirement)
        before = receipts.parse_comment(original, requirement)
        actual = copy.deepcopy(original)
        actual['html_url'] = actual['html_url'].replace(OLD, NEW)
        actual['issue_url'] = actual['issue_url'].replace(OLD, NEW)
        with handoff.api_read_budget():
            self.assertEqual(receipts.parse_comment(actual, requirement), before)
        self.assertEqual(actual['body'], original['body'])
        self.assertEqual(before['body_sha256'], receipts.body_hash(original['body']))
        self.assertEqual(before['auditor_identity'], 'ASTRA_FABLE')
        for change in ('producer', 'head', 'number'):
            bad = copy.deepcopy(actual)
            if change == 'producer': bad['body'] = bad['body'].replace('auditor=ASTRA_FABLE', 'auditor=MAC_GLM53')
            if change == 'head': bad['body'] = bad['body'].replace('head='+'a'*40, 'head='+'b'*40)
            if change == 'number': bad['html_url'] = bad['html_url'].replace('/pull/2#', '/pull/3#')
            with self.subTest(change=change), handoff.api_read_budget(), self.assertRaises(AppError):
                receipts.parse_comment(bad, requirement)

    def test_nested_repository_id_mismatch_is_not_a_same_name_match(self):
        with handoff.api_read_budget():
            self.assertTrue(identity.repository_object_matches(OLD, metadata(NEW)))
            for bad in (dict(metadata(NEW), id=99), dict(metadata(NEW), id=True), {'full_name': NEW}):
                self.assertFalse(identity.repository_object_matches(OLD, bad))

    def test_identity_and_approval_cache_does_not_cross_operation_boundary(self):
        with handoff.api_read_budget(): identity.transport(OLD)
        self.pin['body'] += '\nrevoked'
        with handoff.api_read_budget(), self.assertRaises(AppError): identity.transport(OLD)

    def test_existing_receipt_journal_is_byte_identical_and_negative_hold_survives_transfer(self):
        import core
        import common
        import mac_astra_receipt as receipts
        from test_control_plane_mac_astra_receipt import audit_comment
        request = dict(repository=OLD, pr_url=f'https://github.com/{OLD}/pull/2', head='a'*40,
                       branch='aiops/fixture', gate='ARCHITECTURE', requested_depth='A3',
                       task_id='EXISTING', task_revision='d'*64,
                       canonical_binding=dict(task_id='EXISTING', task_revision='d'*64))
        requirement = dict(request_binding=request, request_sha256=common.digest(request))
        original = audit_comment(requirement)
        comments = [copy.deepcopy(original)]
        pull = dict(number=2, html_url=request['pr_url'],
                    head=dict(sha=request['head'], ref=request['branch'], repo=dict(full_name=OLD, id=1365416872)),
                    base=dict(repo=dict(full_name=OLD, id=1365416872)))
        def api(path, paginate=False):
            if path.endswith('/pulls/2'): return copy.deepcopy(pull)
            if path.endswith('/issues/comments/123'): return copy.deepcopy(comments[0])
            if '/comments?' in path: return [copy.deepcopy(comments)]
            raise AssertionError(path)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(handoff, 'api', side_effect=api), \
                patch.object(receipts, 'decision_evidence', return_value=copy.deepcopy(receipts.DECISION)):
            store = core.Store(Path(folder)/'app')
            try:
                journal = receipts.Journal(store)
                pinned = journal.consume(requirement)
                before = '\n'.join(journal.db.iterdump())
                for item in comments:
                    for key in ('html_url', 'issue_url'): item[key] = item[key].replace(OLD, NEW)
                pull['html_url'] = pull['html_url'].replace(OLD, NEW)
                for key in ('head', 'base'): pull[key]['repo'] = dict(full_name=NEW, id=1365416872)
                self.assertEqual(journal.consume(requirement), pinned)
                self.assertEqual('\n'.join(journal.db.iterdump()), before)
                self.assertEqual(receipts.read_receipt(requirement, 123, expected=pinned['comment'], journal=journal), pinned['comment'])
                negative = audit_comment(requirement, cid=124, result='FAIL')
                for key in ('html_url', 'issue_url'): negative[key] = negative[key].replace(OLD, NEW)
                comments.append(negative)
                with self.assertRaisesRegex(AppError, 'AUDIT_CONFLICT'): journal.consume(requirement)
                held = '\n'.join(journal.db.iterdump())
                comments.pop()
                with self.assertRaisesRegex(AppError, 'AUDIT_CONFLICT'): receipts.Journal(store).consume(requirement)
                self.assertEqual('\n'.join(journal.db.iterdump()), held)
                self.assertEqual(request['repository'], OLD)
                self.assertEqual(common.digest(request), requirement['request_sha256'])
            finally:
                store.close()


if __name__ == '__main__': unittest.main()
