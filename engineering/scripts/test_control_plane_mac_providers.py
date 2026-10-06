"""Provider contracts use official-format fixtures and real local fake CLIs.

No provider account, network model call, paid request or target repo is used.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_app'))
import agents
import common
import core
import worker
from provider_catalog import CATALOG, public_catalog


MODELS = {'codex': 'test-codex', 'claude': 'test-claude', 'cursor': 'test-cursor',
          'glm': 'zai-coding-plan/glm-test', 'grok_build': 'test-grok', 'devin': 'test-devin'}


def profile(provider): return {'provider': provider, 'model': MODELS[provider]}


def report():
    return {'status': 'complete', 'summary': 'Checked the exact source.', 'question': '',
            'plan': None, 'findings': [], 'checks': ['Verified test evidence.'],
            'reviewed_head': 'a' * 40, 'covered_tasks': ['feature']}


def opencode_events(value=None, mid='msg-final', sid='ses-test', reason='stop'):
    def event(kind, part):
        return {'type': kind, 'sessionID': sid, 'part': dict(sessionID=sid, messageID=mid, **part)}
    return [event('step_start', {'id': 'start', 'type': 'step-start'}),
            event('text', {'id': 'answer', 'type': 'text', 'text': json.dumps(value or report()), 'time': {'end': 1}}),
            event('step_finish', {'id': 'finish', 'type': 'step-finish', 'reason': reason})]


def ndjson(events): return '\n'.join(json.dumps(e) for e in events) + '\n'


def cursor_events():
    def assistant(text): return {'type': 'assistant', 'session_id': 'cursor-test',
                                'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}
    return [assistant('I will read the source first.'), {'type': 'tool_call', 'subtype': 'completed'},
            assistant(json.dumps(report())), {'type': 'result', 'subtype': 'success', 'is_error': False,
                                            'result': 'All narration and final text combined.', 'session_id': 'cursor-test'}]


def trajectory(value=None):
    return {'schema_version': 'ATIF-v1.7', 'session_id': 'devin-test',
            'agent': {'name': 'devin', 'version': 'test'},
            'steps': [{'step_id': 1, 'source': 'user', 'message': 'Review the source.'},
                      {'step_id': 2, 'source': 'agent', 'message': json.dumps(value or report())}]}


class ProviderContracts(unittest.TestCase):
    def test_six_distinct_harnesses_and_executables(self):
        self.assertEqual(set(common.PROVIDERS), set(MODELS))
        self.assertEqual(len({p['harness'] for p in public_catalog()}), 6)
        self.assertEqual(CATALOG['cursor']['executables'], ['agent', 'cursor-agent'])
        self.assertEqual(CATALOG['glm']['executables'], ['opencode'])
        self.assertEqual(CATALOG['grok_build']['executables'], ['grok'])
        self.assertEqual(CATALOG['devin']['harness'], 'DEVIN_LOCAL_CLI')

    def test_every_role_can_choose_each_provider_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            store = core.Store(Path(d) / 'state')
            try:
                for provider in MODELS:
                    value = copy.deepcopy(common.DEFAULTS)
                    value['roles'] = {role: profile(provider) for role in common.ROLES}
                    self.assertEqual(store.set_settings(value), value)
                    self.assertEqual(store.settings(), value)
            finally: store.close()

    def test_glm_requires_coding_plan_model_and_cursor_requires_exact_model(self):
        for provider, model in [('glm', ''), ('glm', 'zai/glm-test'), ('glm', 'anthropic/glm-test'),
                                ('glm', 'zai-coding-plan/other'), ('cursor', ''), ('cursor', 'auto'),
                                ('cursor', 'default'), ('devin', '--cloud')]:
            with self.subTest(provider=provider, model=model), self.assertRaises(common.AppError):
                common.validate_profile({'provider': provider, 'model': model})
        for provider in ('codex', 'claude', 'grok_build', 'devin'):
            common.validate_profile({'provider': provider, 'model': ''})

    def test_commands_are_local_fresh_and_use_exact_selected_model(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(agents.shutil, 'which', side_effect=lambda n: '/cli/' + n):
            for provider in MODELS:
                for role in common.ROLES:
                    with self.subTest(provider=provider, role=role):
                        argv = agents.command(profile(provider), role, d)
                        self.assertEqual(argv[0], '/cli/' + CATALOG[provider]['executables'][0])
                        self.assertEqual(argv[argv.index('--model') + 1], MODELS[provider])
                        for flag in ('--cloud', '--resume', '--continue', '--attach', '--api-key', '--approve-mcps'):
                            self.assertNotIn(flag, argv)
                        if provider == 'cursor':
                            self.assertIn('--sandbox', argv)
                            self.assertEqual('--force' in argv, role == 'builder')
                            if role != 'builder': self.assertEqual(argv[argv.index('--mode') + 1], 'ask')
                        if provider == 'grok_build':
                            self.assertEqual(json.loads(argv[argv.index('--json-schema') + 1]), agents.SCHEMA)
                            self.assertEqual(argv[argv.index('--sandbox') + 1], 'workspace' if role == 'builder' else 'read-only')
                            self.assertIn('--no-subagents', argv)
                            self.assertEqual(argv[argv.index('--session-id') + 1], agents.session_uuid(d))
                            if role != 'builder': self.assertIn('Edit', argv); self.assertIn('Bash', argv)
                        if provider == 'devin':
                            self.assertIn('--sandbox', argv); self.assertIn('--export', argv)
                            self.assertEqual(argv[argv.index('--permission-mode') + 1], 'auto')
                            self.assertNotIn('dangerous', argv)

    def test_cursor_alias_and_missing_provider_no_implicit_fallback(self):
        with mock.patch.object(agents.shutil, 'which', side_effect=lambda n: '/cli/cursor-agent' if n == 'cursor-agent' else None):
            self.assertEqual(agents.command(profile('cursor'), 'builder', '/tmp/x')[0], '/cli/cursor-agent')
            with self.assertRaisesRegex(common.AppError, 'Grok Build'): agents.command(profile('grok_build'), 'builder', '/tmp/x')

    def test_glm_role_permissions_route_and_secret_environment(self):
        secrets = {key: 'DO_NOT_INHERIT' for key in ('GH_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
                    'XAI_API_KEY', 'ZAI_API_KEY', 'ZHIPUAI_API_KEY', 'CURSOR_API_KEY', 'DEVIN_API_KEY',
                    'OPENCODE_CONFIG_CONTENT', 'OPENCODE_PERMISSION', 'DEVIN_PERMISSION_MODE')}
        with mock.patch.dict(os.environ, secrets):
            for role in common.ROLES:
                env = agents.environment(profile('glm'), role)
                self.assertNotIn('DO_NOT_INHERIT', json.dumps(env))
                config = json.loads(env['OPENCODE_CONFIG_CONTENT']); permissions = json.loads(env['OPENCODE_PERMISSION'])
                self.assertEqual(config['enabled_providers'], ['zai-coding-plan'])
                self.assertEqual(config['agent']['aiops']['model'], MODELS['glm'])
                self.assertEqual(permissions['*'], 'deny'); self.assertEqual(permissions['task'], 'deny')
                self.assertEqual(permissions['external_directory'], 'deny')
                self.assertEqual(permissions.get('edit', 'deny'), 'allow' if role == 'builder' else 'deny')
                self.assertEqual(permissions.get('bash', 'deny'), 'allow' if role == 'builder' else 'deny')
                self.assertEqual(config['share'], 'disabled')

    def test_private_prompt_and_devin_config_do_not_modify_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); checkout = root / 'checkout'; checkout.mkdir()
            for role in common.ROLES:
                folder = root / role; folder.mkdir()
                agents.prepare(profile('devin'), role, folder, checkout, 'private assignment')
                config = common.read_json(folder / 'devin-config.json')
                self.assertFalse(config['subagents_enabled']); self.assertFalse(config['auto_update'])
                self.assertIn('mcp__*', config['permissions']['deny'])
                if role != 'builder':
                    for rule in ('edit', 'exec', 'Write(/**)'): self.assertIn(rule, config['permissions']['deny'])
                self.assertEqual((folder / 'prompt.txt').stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(checkout.iterdir()), [])

    def test_prompt_includes_schema_for_tools_without_structured_output_flags(self):
        text = agents.prompt({'id': 'one'}, 'supervisor', 'a' * 40)
        self.assertIn('OUTPUT CONTRACT', text); self.assertIn(common.encoded(agents.SCHEMA), text)

    def test_cursor_final_result_and_error_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'stdout.log'
            payload = cursor_events()
            path.write_text(ndjson(payload)); result = agents.completion(profile('cursor'), d)
            self.assertEqual(result['report'], report()); self.assertEqual(result['session_id'], 'cursor-cli:cursor-test')
            for changes in ({'is_error': True}, {'subtype': 'error'}, {'type': 'assistant'}, {'session_id': ''}):
                path.write_text(ndjson([*payload[:-1], dict(payload[-1], **changes)]))
                with self.subTest(changes=changes), self.assertRaises(common.AppError): agents.result(profile('cursor'), d)

    def test_cursor_requires_completed_stream_same_session_and_no_trailing_tool(self):
        events = cursor_events()
        for value in (events[:-1], events + [events[-2]], [*events[:-1], {'type': 'tool_call'}, events[-1]],
                      [*events[:-1], dict(events[-1], session_id='foreign')]):
            with self.subTest(value=value), self.assertRaises(common.AppError): agents.cursor_completion(ndjson(value))

    def test_grok_result_requires_exact_native_session_and_normal_completion(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'stdout.log'; payload = {'text': 'First I inspected the repository. This is narration.',
                'structuredOutput': report(), 'stopReason': 'end_turn', 'sessionId': agents.session_uuid(d)}
            path.write_text(json.dumps(payload)); self.assertEqual(agents.result(profile('grok_build'), d), report())
            for changes in ({'stopReason': 'max_tokens'}, {'stopReason': 'cancelled'}, {'sessionId': 'another-session'},
                            {'type': 'error'}, {'structuredOutputError': 'schema failed'}, {'structuredOutput': None, 'text': json.dumps(report())}):
                path.write_text(json.dumps(dict(payload, **changes)))
                with self.subTest(changes=changes), self.assertRaises(common.AppError): agents.result(profile('grok_build'), d)

    def test_glm_uses_only_terminal_assistant_step(self):
        preceding = opencode_events({'injected': 'old report'}, 'msg-tool', reason='tool-calls')
        value, sid = agents.opencode_completion(ndjson(preceding + opencode_events()))
        self.assertEqual(value, report()); self.assertEqual(sid, 'opencode-cli:ses-test')

    def test_glm_rejects_incomplete_error_mixed_session_and_trailing_tool(self):
        error = {'type': 'error', 'sessionID': 'ses-test', 'error': {'message': 'denied'}}
        tool = {'type': 'tool_use', 'sessionID': 'ses-test', 'part': {'messageID': 'msg-final', 'type': 'tool'}}
        cases = [opencode_events(reason='tool-calls'), opencode_events()[:-1], opencode_events() + [error],
                 opencode_events() + opencode_events(sid='foreign'), opencode_events() + [tool]]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(common.AppError): agents.opencode_completion(ndjson(events))

    def test_devin_export_final_agent_message_not_tool_output_or_copied_context(self):
        original = trajectory(); value, sid = agents.devin_completion(json.dumps(original))
        self.assertEqual(value, report()); self.assertEqual(sid, 'devin-cli:devin-test')
        for changes in ({'source': 'user'}, {'tool_calls': [{'tool': 'exec'}]}, {'is_copied_context': True}, {'observation': {'text': json.dumps(report())}}):
            payload = copy.deepcopy(original); payload['steps'][-1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(common.AppError): agents.devin_completion(json.dumps(payload))
        original['continued_trajectory_ref'] = 'unfinished.json'
        with self.assertRaises(common.AppError): agents.devin_completion(json.dumps(original))

    def test_json_answer_never_searches_transcripts_for_a_passing_report(self):
        self.assertEqual(agents.json_answer('```json\n' + json.dumps(report()) + '\n```'), report())
        for text in ('tool output:\n' + json.dumps(report()), json.dumps(report()) + '\nfailed',
                     '{"status":"complete","status":"fail"}'):
            with self.subTest(text=text), self.assertRaises(common.AppError): agents.json_answer(text)

    def test_result_symlinks_and_oversize_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); target = root / 'target'; target.write_text('{}')
            (root / 'output').symlink_to(target)
            with self.assertRaises(common.AppError): agents.read_output(root / 'output')
            with self.assertRaises(common.AppError): agents.read_output(target, 1)

    def test_probe_has_all_six_providers_and_never_starts_a_model(self):
        with mock.patch.object(agents.shutil, 'which', side_effect=lambda n: '/cli/' + n), \
             mock.patch.object(agents.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'version-test\n', '')) as run:
            data = agents.availability()
        self.assertTrue(all(data[key]['installed'] for key in MODELS))
        for call in run.call_args_list:
            self.assertTrue(call.args[0][1:] in (['--version'], ['auth', 'status', '--active', '--hostname', 'github.com']))
        self.assertTrue(all(data[key]['authentication'] == 'not_checked' for key in MODELS))

    def test_github_probe_uses_active_account_with_other_expired_accounts_saved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); binary = root / 'gh'
            source = '#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\n'
            source += "assert sys.argv[1:3] == ['auth', 'status']\n"
            source += "assert sys.argv[sys.argv.index('--hostname') + 1] == 'github.com'\n"
            source += "active_valid = (Path(__file__).parent / 'active-valid').exists()\n"
            source += "print('saved inactive account expired; secret-marker', file=sys.stderr)\n"
            source += "sys.exit(0 if '--active' in sys.argv and active_valid else 1)\n"
            binary.write_text(source); binary.chmod(0o755)
            for active_valid in (True, False):
                marker = root / 'active-valid'
                if active_valid: marker.touch()
                else: marker.unlink()
                with self.subTest(active_valid=active_valid), mock.patch.dict(os.environ, {'PATH': str(root)}):
                    data = agents.availability(providers=set(), versions=False)
                self.assertEqual(data['github_authenticated'], active_valid)
                self.assertEqual(data['github_authentication'], 'authenticated' if active_valid else 'required')
                self.assertNotIn('secret-marker', json.dumps(data))

    def test_real_worker_runs_all_four_new_harnesses_once_and_records_identity(self):
        for provider in ('cursor', 'glm', 'grok_build', 'devin'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as d:
                root = Path(d); folder = root / 'attempt'; folder.mkdir(); binary = root / CATALOG[provider]['executables'][0]
                payload = {'grok_build': {'text': 'Narration', 'structuredOutput': report(), 'stopReason': 'end_turn', 'sessionId': agents.session_uuid(folder)}}
                script = '#!' + sys.executable + '\nimport os,sys,json\nfrom pathlib import Path\n'
                script += "assert 'GH_TOKEN' not in os.environ and 'XAI_API_KEY' not in os.environ\n"
                if provider in ('cursor', 'glm'): script += "assert 'private assignment' in sys.stdin.read()\n"
                else: script += "assert Path(sys.argv[sys.argv.index('--prompt-file')+1]).read_text() == 'private assignment'\n"
                if provider == 'devin': script += "Path(sys.argv[sys.argv.index('--export')+1]).write_text(" + repr(json.dumps(trajectory())) + ')\n'
                elif provider == 'glm': script += 'print(' + repr(ndjson(opencode_events())) + ')\n'
                elif provider == 'cursor': script += 'print(' + repr(ndjson(cursor_events())) + ')\n'
                else: script += 'print(' + repr(json.dumps(payload[provider])) + ')\n'
                binary.write_text(script); binary.chmod(0o755)
                common.atomic_json(folder / 'request.json', {'attempt_id': 'one', 'binding': 'bound-input',
                    'profile': profile(provider), 'role': 'reviewer', 'checkout': str(root),
                    'prompt': 'private assignment', 'timeout_seconds': 10})
                with mock.patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'GH_TOKEN': 'secret', 'XAI_API_KEY': 'secret'}):
                    worker.run(folder)
                    with self.assertRaises(FileExistsError): worker.run(folder)
                receipt = common.read_json(folder / 'receipt.json')
                self.assertIsNone(receipt['error']); self.assertEqual(receipt['report'], report())
                self.assertTrue(receipt['process_group_quiescent']); self.assertEqual(receipt['binding'], 'bound-input')
                evidence = receipt['provider_evidence']; self.assertEqual(evidence['provider'], provider)
                self.assertEqual(evidence['harness'], CATALOG[provider]['harness'])
                self.assertEqual(evidence['model_requested'], MODELS[provider]); self.assertTrue(evidence['session_id'])

    def test_stdout_provider_error_classification_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'stdout.log').write_text('{"type":"error","error":{"name":"ProviderAuthError","secret":"FAKE"}}')
            self.assertEqual(worker.failure_code(d, 1), 'PROVIDER_LOGIN_REQUIRED')


if __name__ == '__main__': unittest.main()
