"""Fable #47 F1/F4/F5/F6 regressions, with no model/network/host activation."""
import copy
import io
import itertools
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import control_plane_fable as fable
import control_plane_program_receipts as journal
from test_control_plane_fable import Base, FakeGitHub, FakeRunner, HEAD, REPO, SESSION, verdict
from test_control_plane_fable_failure import EXECUTION, NOW_MS, failed_events

ROOT = Path(__file__).resolve().parents[1]


class PolicyAndLinesTests(unittest.TestCase):
    def test_runtime_billing_table_matches_overage_policy_and_exception_limits(self):
        text = (ROOT / 'docs/CONTROL_PLANE_RUNTIME.md').read_text()
        table = text.split('<!-- FABLE_OVERAGE_POLICY_V1 -->')[1].split('<!-- /FABLE_OVERAGE_POLICY_V1 -->')[0]
        rows = [[cell.strip() for cell in row.strip().strip('|').split('|')]
                for row in table.split('\n') if row.strip().startswith('|')][2:]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][:4], ['`rejected`', '무관', '정확히 bool `false`', 'CONTINUE'])
        self.assertEqual(rows[1][:4], ['키 없음', '`allowed` 또는 `allowed_warning`', '정확히 bool `false`', 'CONTINUE'])
        self.assertEqual(rows[2][3], 'STOP')
        self.assertIn('과금 차단 확인 아님', rows[1][4])
        self.assertIn('quota 재시도 없음', rows[1][4])
        missing = object()
        for overage, status, using in itertools.product(
                [missing, 'rejected', 'allowed', 'allowed_warning', None, '', 'unknown'],
                ['allowed', 'allowed_warning', 'rejected', None, 'unknown'],
                [False, True, None, 0, 1, 'false']):
            info = {'status': status, 'isUsingOverage': using}
            if overage is not missing:
                info['overageStatus'] = overage
            # Interpret the two CONTINUE rows; the catch-all row is STOP.
            accepted = using is False and (overage == 'rejected' or
                       overage is missing and status in ('allowed', 'allowed_warning'))
            with self.subTest(info=info):
                self.assertEqual(fable.overage_policy(info)[0] is None, accepted)
        self.assertIsNotNone(fable.overage_policy(None)[0])
        self.assertIsNotNone(fable.overage_policy({'status': 'allowed'})[0])

    def test_jsonl_keeps_raw_unicode_separators_inside_successful_json_strings(self):
        for separator in ('\u2028', '\u2029', '\u0085'):
            value = verdict(); value['summary'] = 'before' + separator + 'after'
            events = [{'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'isUsingOverage': False}},
                      {'type': 'result', 'subtype': 'success', 'is_error': False,
                       'modelUsage': {fable.MODEL: {}}, 'structured_output': value, 'session_id': SESSION}]
            raw = b'\n'.join(json.dumps(event, ensure_ascii=False).encode() for event in events) + b'\n'
            with self.subTest(separator=separator):
                streamed, stopped = fable.read_stream(io.BytesIO(raw))
                self.assertFalse(stopped)
                self.assertEqual(fable.events_of(streamed), events)
                self.assertEqual(fable.model_output(streamed)['structured_output']['summary'], value['summary'])

    def test_failure_hashes_use_same_lf_framing_for_unicode_crlf_and_blank_lines(self):
        events = failed_events(); events[-1]['result'] = 'limit\u2028details\u2029more\u0085end'
        lines = [json.dumps(event, ensure_ascii=False).encode() for event in events]
        raw = b'\r\n\r\n'.join(lines) + b'\r\n'
        self.assertEqual(fable.events_of(raw), events)
        failure = fable.failure_of(raw, version=fable.SUPPORTED_CLAUDE_VERSION,
                                  execution=EXECUTION, observed_at_epoch_ms=NOW_MS)
        self.assertEqual(failure['terminal_evidence'], 'VERIFIED')
        self.assertEqual(failure['raw_result_sha256'], fable.sha256_bytes(lines[-1]))

    def test_ascii_record_separators_do_not_turn_one_line_into_extra_json_events(self):
        for separator in (b'\x1c', b'\x1d', b'\x1e', b'\x85'):
            raw = b'{"type":"system"}' + separator + b'{"type":"result"}\n'
            with self.subTest(separator=separator):
                with self.assertRaises(fable.FableError): fable.events_of(raw)
                self.assertEqual(fable.read_stream(io.BytesIO(raw))[1]['error_code'], 'STREAM_INVALID')
        # JSON forbids raw ASCII control characters inside strings. Escaped
        # versions, which json.dumps emits, are legitimate and remain one event.
        for separator in ('\x1c', '\x1d', '\x1e'):
            event = {'text': 'a' + separator + 'b'}
            self.assertEqual(fable.events_of(json.dumps(event).encode() + b'\n'), [event])

    def test_dispatch_program_procedure_uses_completion_predicate_and_both_holds(self):
        text = (ROOT / 'RUNBOOKS/DISPATCH.md').read_text()
        writer = text.split('Writer (`operation=start`):')[1].split('Resume:')[0]
        self.assertIn('delivery_completion(...).status == DONE', writer)
        self.assertIn('`MERGED_POST_VERIFY`', writer)
        self.assertIn('`POST_MERGE_FAILED`', writer)
        self.assertIn('Merge alone is not DONE', writer)
        self.assertNotIn('returns DONE, and never redispatches, once', writer)


class PreModelTests(Base):
    def setUp(self):
        super().setUp()
        self.chain = patch.object(fable, 'protected_run_chain', lambda path: None)
        self.chain.start(); self.addCleanup(self.chain.stop)
        self.binding = {'repository': REPO, 'program': 'p', 'node': 'n', 'issue': 5,
                        'head': HEAD, 'writer_launch': 'd' * 24, 'plan_commit': 'c' * 40}
        self.journal_root = self.runs / 'program'; self.journal_root.mkdir()
        self.store = journal.Receipts(self.journal_root, 'f' * 64, trust=lambda *a, **kw: None)

    def fail_request(self, ctx, invoke, action='audit'):
        with self.assertRaises(fable.FableError) as caught:
            self.store.execute(action, self.binding,
                lambda: ctx.program_invoke(action, self.binding, 5, invoke))
        return caught.exception, self.store.read(action, self.binding)

    def test_head_moved_transport_and_archive_errors_have_sealed_never_attempted_proof(self):
        for mode in ('HEAD_MOVED', 'transport', 'archive'):
            github = FakeGitHub(head='e' * 40 if mode == 'HEAD_MOVED' else HEAD)
            runner = FakeRunner(structured=verdict()); ctx = self.context(github, runner)
            if mode == 'transport': github.get = lambda *_: (_ for _ in ()).throw(OSError('transport'))
            if mode == 'archive': github.archive = lambda *_: (_ for _ in ()).throw(OSError('archive'))
            binding = {**self.binding, 'node': mode}
            with self.subTest(mode=mode), self.assertRaises(fable.FableError) as caught:
                self.store.execute('audit', binding, lambda: ctx.program_invoke('audit', binding, 5,
                    lambda: fable.audit(ctx, REPO, 5, HEAD, 'ARCHITECTURE', 'A3')))
            failure = caught.exception.failure
            self.assertIs(failure['model_attempted'], False)
            self.assertEqual(fable.verify_failure_evidence(ctx, failure['run']), failure)
            self.assertEqual(runner.calls, []); self.assertEqual(github.posts, [])
            first = self.store.read('audit', binding)
            key = first['admission']; original = self.store._path(key, 'outcome').read_bytes()
            result = self.store.reconcile(key, first['state_version'], lambda run: fable.verify_failure_evidence(ctx, run))
            self.assertEqual(result['status'], 'RECONCILED_FAILED')
            self.assertEqual(self.store._path(key, 'outcome').read_bytes(), original)
            self.assertEqual(self.store.read('audit', binding)['status'], 'ERROR')
            self.assertEqual(self.store.execute('audit', binding, lambda: self.fail('retry'))['status'], 'ERROR')
            self.assertEqual(self.store.read('audit', {**binding, 'head': 'e' * 40})['status'], 'MISSING')

    def test_consult_pre_model_failure_is_bound_to_consult_and_operator_only(self):
        runner = FakeRunner(); ctx = self.context(None, runner)
        exc, first = self.fail_request(ctx, lambda: (_ for _ in ()).throw(OSError('fetch')), action='consult')
        self.assertEqual(exc.failure['kind'], 'consult')
        self.assertEqual(self.store.decision_status(self.binding)['status'], 'BLOCKED')
        self.store.reconcile(first['admission'], first['state_version'], lambda run: fable.verify_failure_evidence(ctx, run))
        self.assertEqual(self.store.decision_status(self.binding)['status'], 'CLEAR')
        self.assertEqual(runner.calls, [])

    def test_marker_is_durable_before_runner_and_prevents_false_never_attempted_claim(self):
        runner = FakeRunner()
        def entered(work):
            request = ctx._program_request
            self.assertTrue((request / 'model-invoke-intent.json').exists())
            raise OSError('runner may have started')
        runner.inspect = entered; ctx = self.context(None, runner)
        def invoke():
            _, run = ctx.new_run('audit', REPO, 5)
            return ctx.run_model(run, '', {}, '', lambda value: value)
        exc, first = self.fail_request(ctx, invoke)
        self.assertNotEqual(exc.failure['error_code'], 'PRE_MODEL_FAILED')
        with self.assertRaises((journal.ReceiptError, fable.FableError)):
            self.store.reconcile(first['admission'], first['state_version'], lambda run: fable.verify_failure_evidence(ctx, run))
        self.assertEqual(len(runner.calls), 1)

    def test_sealing_error_stops_before_runner_and_does_not_fabricate_proof(self):
        runner = FakeRunner(); ctx = self.context(None, runner)
        with patch.object(fable, 'sealed_file', side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):
                self.store.execute('audit', self.binding, lambda: ctx.program_invoke('audit', self.binding, 5,
                    lambda: self.fail('must not invoke')))
        first = self.store.read('audit', self.binding)
        self.assertNotIn('failure', first)
        with self.assertRaisesRegex(journal.ReceiptError, 'unproven'):
            self.store.reconcile(first['admission'], first['state_version'], lambda _: self.fail())
        self.assertEqual(runner.calls, [])

    def test_changed_sealed_intent_or_added_attempt_marker_cannot_settle(self):
        for mode in ('binding', 'false_type', 'attempt', 'publication', 'symlink'):
            binding = {**self.binding, 'node': mode}
            ctx = self.context(None, FakeRunner())
            with self.assertRaises(fable.FableError) as caught:
                ctx.program_invoke('audit', binding, 5, lambda: (_ for _ in ()).throw(OSError('fetch')))
            failure = caught.exception.failure
            run = next(self.runs.glob(failure['run'] + '-*'))
            intent_path = run / 'request-intent.json'
            if mode in ('binding', 'false_type'):
                intent = json.loads(intent_path.read_bytes())
                if mode == 'binding': intent['program_binding']['node'] = 'another'
                else: intent['model_attempted'] = 0
                intent_path.write_text(json.dumps(intent))
            elif mode == 'attempt': (run / 'model-invoke-intent.json').write_text('{}')
            elif mode == 'publication': (run / 'publish-intent.json').write_text('{}')
            else:
                content = intent_path.read_bytes(); intent_path.unlink()
                target = run / 'target'; target.write_bytes(content); intent_path.symlink_to(target)
            with self.subTest(mode=mode), self.assertRaises(fable.FableError):
                fable.verify_failure_evidence(ctx, failure['run'])


if __name__ == '__main__':
    unittest.main()
