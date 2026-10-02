"""Offline tests for the whole-host pack: a fake root under a temp dir, no host, account, network or model."""
import base64
import copy
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import control_plane_hostpack as h
import control_plane_recover as rec

ENGINEERING = Path(__file__).resolve().parents[1]
COMMIT = 'a' * 40
EVIDENCE = 'https://github.com/BeautifulMind-JT/ai-ops-control-plane/actions/runs/1'


class FakeBackend:
    def __init__(self):
        self.h, self.data, self.conflict = None, {}, False
        self.count = 0

    def head(self, *, missing=False):
        if self.h is None and not missing:
            rec.fail('STATE_CORRUPT')
        return self.h

    def load(self, head):
        if head not in self.data:
            rec.fail('STATE_CORRUPT')
        return self.data[head]

    def publish(self, expected, envelope, cipher):
        if expected != self.h or self.conflict:
            rec.fail('STATE_CAS_CONFLICT')
        self.count += 1
        self.h = format(self.count, '040x')
        self.data[self.h] = envelope, cipher
        return self.h


class FakeOps(h.SystemOps):
    """No useradd, no sudo, no network, no chown: accounts are a table, artifacts come from the repo checkout."""
    enforce_ownership = False

    def __init__(self, root):
        self.root, self.users, self.next_uid, self.validated = Path(root), {}, 2000, []
        self.proven = set()

    def lookup_user(self, name):
        return self.users.get(name)

    def ensure_account(self, pack, item):
        if item['name'] not in self.users:
            self.users[item['name']] = {'uid': self.next_uid, 'gid': self.next_uid, 'home': item['home'], 'shell': '/usr/sbin/nologin'}
            self.next_uid += 1
        return self.users[item['name']]

    def atomic(self, path, data, mode, uid, gid, replace=True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.tmp-' + path.name)
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        if replace:
            os.replace(tmp, path)
        else:
            if os.path.lexists(path):
                tmp.unlink()
                raise FileExistsError(path)
            os.link(tmp, path)
            tmp.unlink()

    def read_cached(self, pack, cache_path, expected_sha):
        data = (ENGINEERING.parent / cache_path).read_bytes()
        if h.sha(data) != expected_sha:
            h.fail('ARTIFACT_HASH_MISMATCH', cache_path)
        return data

    def validate_sudoers(self, pack, rendered):
        self.validated.append(rendered)

    def lane_proven(self, pack, lane):
        return lane in self.proven


def make_config(manifest, **override):
    cfg = {'schema': h.CONFIG_SCHEMA, 'source_commit': COMMIT, 'manifest_sha256': h.sha(h.canonical(manifest)),
           'runner_user': 'astra-runner', 'enabled_builders': list(h.LANES), 'allowed_repositories': list(h.REPOSITORIES),
           'max_active_sessions': 4, 'max_launches_per_24h': None, 'preserve_logins': list(h.LANES),
           'boundary_evidence_pointer': EVIDENCE, 'state_repository': 'BeautifulMind-JT/aiops-state', 'state_branch': 'host-state'}
    cfg.update(override)
    return cfg


class PackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = json.loads((ENGINEERING / 'hostpack/manifest.json').read_text())
        self.ops = FakeOps(self.root)
        self.pack = self.make_pack()

    def make_pack(self, **override):
        return h.Hostpack(self.manifest, make_config(self.manifest, **override), prefix=self.root, owner=os.getuid(), ops=self.ops)

    def expect(self, code, fn, *args, **kwargs):
        with self.assertRaises(h.PackError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(code, ctx.exception.code)
        return ctx.exception

    def fake_ledger(self):
        directory = self.root / 'var/lib/astra/control'
        directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(directory / h.LEDGER_DB)
        db.execute('CREATE TABLE launches(id TEXT PRIMARY KEY, state TEXT)')
        db.execute("INSERT INTO launches VALUES('req1','CONFIRMED')")
        db.commit()
        db.close()
        (directory / 'census-DEVIN.lock').write_bytes(b'')
        (directory / 'side.json').write_bytes(b'{"x":1}')
        return directory


class ManifestTests(PackTest):
    def test_manifest_matches_repository_bytes(self):
        self.assertEqual(self.manifest, h.build_manifest(ENGINEERING))

    def test_every_lane_account_is_the_adapters_own_lane_user(self):
        for lane, spec in h.LANE_SPECS.items():
            adapter = (ENGINEERING / spec['source'] / spec['adapter']).read_text()
            self.assertIn(f'LANE_USER = "{spec["account"]}"', adapter, lane)

    def test_wrapper_sudo_targets_use_the_same_account_and_adapter(self):
        for lane, spec in h.LANE_SPECS.items():
            wrapper = (ENGINEERING / spec['source'] / spec['wrapper']).read_text()
            self.assertIn(f'sudo -n -u {spec["account"]} -- "$INNER"', wrapper, lane)
            self.assertIn('INNER=/opt/astra/libexec/' + spec['adapter'], wrapper, lane)

    def test_manifest_covers_the_helper_wrappers_adapters_supervisors_and_boundary(self):
        destinations = {i['destination'] for i in self.manifest['files']}
        self.assertIn(h.HELPER, destinations)
        for spec in h.LANE_SPECS.values():
            self.assertIn('/opt/astra/bin/' + spec['wrapper'], destinations)
            self.assertIn('/opt/astra/libexec/' + spec['adapter'], destinations)
            self.assertIn('/opt/astra/libexec/' + spec['supervisor'], destinations)
        self.assertIn('/opt/astra/boundary/control_plane_boundary_hook.sh', destinations)

    def test_installed_helper_is_the_exact_repository_file(self):
        # host-preflight compares the installed helper's digest with the repository's control_plane_host.py
        item = self.pack.item(h.HELPER)
        self.assertEqual(item['sha256'], h.sha((ENGINEERING / 'scripts/control_plane_host.py').read_bytes()))

    def test_manifest_rejects_foreign_destinations_and_missing_entries(self):
        broken = copy.deepcopy(self.manifest)
        broken['files'][0]['destination'] = '/etc/passwd'
        self.expect('MANIFEST_REJECTED', h.validate_manifest, broken)
        broken = copy.deepcopy(self.manifest)
        broken['files'].pop()
        self.expect('MANIFEST_REJECTED', h.validate_manifest, broken)
        broken = copy.deepcopy(self.manifest)
        broken['files'][0]['mode'] = 0o777
        self.expect('MANIFEST_REJECTED', h.validate_manifest, broken)


class ConfigTests(PackTest):
    def test_example_config_is_rejected_until_every_placeholder_is_set(self):
        example = json.loads((ENGINEERING / 'hostpack/hostpack.example.json').read_text())
        self.expect('CONFIG_REJECTED', h.validate_config, example)
        example['source_commit'] = COMMIT
        example['manifest_sha256'] = h.sha(h.canonical(self.manifest))
        h.validate_config(example, h.sha(h.canonical(self.manifest)))  # pointer is checked at install, not here
        self.assertFalse(h.evidence_url(example['boundary_evidence_pointer']))

    def test_config_validation(self):
        base = make_config(self.manifest)
        for key, bad in [('runner_user', 'root'), ('runner_user', 'astra-control'), ('runner_user', 'astra-builder-glm'),
                         ('enabled_builders', ['NOPE']), ('enabled_builders', []), ('allowed_repositories', ['x/y']),
                         ('max_active_sessions', 5), ('max_active_sessions', 0), ('max_launches_per_24h', 0),
                         ('preserve_logins', ['X']), ('state_repository', 'BeautifulMind-JT/other'), ('source_commit', 'abc')]:
            cfg = dict(base, **{key: bad})
            self.expect('CONFIG_REJECTED', h.validate_config, cfg)

    def test_manifest_digest_must_match_config(self):
        cfg = make_config(self.manifest)
        cfg['manifest_sha256'] = '0' * 64
        self.expect('MANIFEST_REJECTED', h.Hostpack, self.manifest, cfg, self.root, 0, self.ops)

    def test_evidence_url_rule_matches_the_host_helpers(self):
        import control_plane_host as host
        for value in (EVIDENCE, 'PENDING', 'https://example.com/x', 'http://localhost/x', '', None, 'https://u@github.com/x'):
            self.assertEqual(host.evidence_url(value), h.evidence_url(value), value)


class RenderTests(PackTest):
    def uids(self):
        return {item['name']: 3000 + n for n, item in enumerate(h.account_specs('astra-runner'))}

    def test_rendered_host_policy_passes_the_helpers_own_validator(self):
        import control_plane_host as host
        policy = h.render_host_policy(make_config(self.manifest), self.uids())
        host.validate_policy(policy)
        self.assertFalse(policy['control_runtime_enabled'])
        self.assertEqual(policy['control_source_sha'], COMMIT)

    def test_policy_never_enables_runtime_and_keeps_uids_distinct_and_nonroot(self):
        policy = h.render_host_policy(make_config(self.manifest), self.uids())
        ids = [policy['control_uid'], policy['runner_uid'], *policy['builder_uids'].values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i > 0 for i in ids))

    def test_base_sudoers_has_only_the_fixed_commands(self):
        text = h.render_base_sudoers('astra-runner').decode()
        self.assertIn('astra-runner ALL=(astra-control) NOPASSWD: AIOPS_BASE_HOST', text)
        self.assertIn('astra-host-control launch', text)
        self.assertIn('^status --launch-request-id [0-9a-f]{24}$', text)
        for builder in ('DEVIN', 'GROK_BUILD', 'GLM'):
            self.assertIn(f'preflight --builder-id {builder}', text)
        rules = [l for l in text.splitlines() if 'NOPASSWD' in l]
        self.assertTrue(rules)
        for rule in rules:
            self.assertNotIn('(root)', rule)
            self.assertNotRegex(rule, r'NOPASSWD:\s*(ALL|/bin/|/usr/bin/)')
            self.assertNotIn(' init', rule)
            self.assertNotIn('reconcile', rule)

    def test_base_and_program_sudoers_together_cover_every_wrapper_invocation(self):
        base = h.render_base_sudoers('astra-runner').decode()
        program = h.render_program_sudoers((ENGINEERING / h.PROGRAM_SUDOERS_SOURCE).read_bytes(), 'astra-runner').decode()
        combined = base + program
        for lane, spec in h.LANE_SPECS.items():
            adapter = '/opt/astra/libexec/' + spec['adapter']
            for mode in ('--preflight', '--launch', '--quiescence'):
                self.assertIn(f'{adapter} {mode}', combined, f'{lane} {mode}')
        for builder in h.LANES:
            self.assertIn(f'preflight --builder-id {builder}', combined)

    def test_program_sudoers_substitutes_only_the_runner_user(self):
        source = (ENGINEERING / h.PROGRAM_SUDOERS_SOURCE).read_bytes()
        rendered = h.render_program_sudoers(source, 'astra-runner').decode()
        self.assertNotRegex(rendered, r'(?m)^RUNNER_USER\b')
        self.assertIn('astra-runner ALL=(astra-control) NOPASSWD: AIOPS_PROGRAM_HOST', rendered)
        self.assertEqual(source.decode().replace('RUNNER_USER ALL=', 'astra-runner ALL=', 1), rendered)
        self.expect('MANIFEST_REJECTED', h.render_program_sudoers, b'no placeholder\n', 'astra-runner')
        self.expect('CONFIG_REJECTED', h.render_base_sudoers, 'Bad User')

    def test_sudoers_syntax_is_accepted_by_visudo_when_available(self):
        visudo = Path('/usr/sbin/visudo')
        if not visudo.exists():
            self.skipTest('visudo not installed')
        import subprocess
        source = (ENGINEERING / h.PROGRAM_SUDOERS_SOURCE).read_bytes()
        for name, data in (('base', h.render_base_sudoers('astra-runner')), ('program', h.render_program_sudoers(source, 'astra-runner'))):
            path = self.root / f'{name}.sudoers'
            path.write_bytes(data)
            os.chmod(path, 0o440)
            done = subprocess.run([str(visudo), '-cf', str(path)], capture_output=True)
            self.assertEqual(done.returncode, 0, name + done.stdout.decode() + done.stderr.decode())

    def test_boundary_policy_pins_one_commit_and_real_ids(self):
        example = json.loads((ENGINEERING / h.BOUNDARY_EXAMPLE_SOURCE).read_text())
        policy = h.render_boundary_policy(example, repository_id=11, owner_id=22, actor_id=33, workflow_sha='b' * 40)
        claims = policy['allow'][0]['claims']
        self.assertEqual(claims['env:GITHUB_WORKFLOW_SHA'], 'b' * 40)
        self.assertEqual(claims['event:sender.id'], 33)
        self.assertEqual(len(policy['allow']), 1)
        self.expect('BOUNDARY_REJECTED', h.render_boundary_policy, example, repository_id=0, owner_id=22, actor_id=33, workflow_sha='b' * 40)
        self.expect('BOUNDARY_REJECTED', h.render_boundary_policy, example, repository_id=1, owner_id=2, actor_id=3, workflow_sha='short')


class InstallTests(PackTest):
    def install(self):
        return self.pack.install()

    def test_install_creates_everything_and_verifies(self):
        out = self.install()
        self.assertEqual(out['status'], 'INSTALLED')
        for item in self.manifest['files']:
            self.assertEqual(self.pack.check_file(item), 'OK', item['destination'])
        for destination in (h.BASE_SUDOERS_DESTINATION, h.PROGRAM_SUDOERS_DESTINATION):
            self.assertTrue((self.root / destination.lstrip('/')).exists())
        self.assertEqual(len(self.ops.validated), 2)
        self.assertEqual(len(self.ops.users), 6)
        policy = json.loads((self.root / h.HOST_POLICY.lstrip('/')).read_text())
        self.assertFalse(policy['control_runtime_enabled'])
        self.assertEqual(oct(os.stat(self.root / 'var/lib/astra/control').st_mode & 0o777), '0o700')

    def test_install_is_idempotent(self):
        self.install()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.install()
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_install_never_overwrites_a_differing_file(self):
        self.install()
        helper = self.root / h.HELPER.lstrip('/')
        os.chmod(helper, 0o755)
        helper.write_bytes(b'tampered')
        error = self.expect('INSTALLATION_DRIFT', self.install)
        self.assertEqual(error.detail, h.HELPER)
        self.assertEqual(helper.read_bytes(), b'tampered')

    def test_drift_found_before_anything_is_written(self):
        (self.root / 'opt/astra/bin').mkdir(parents=True)
        wrapper = self.root / 'opt/astra/bin/astra-builder-glm'
        wrapper.write_bytes(b'other')
        os.chmod(wrapper, 0o755)
        self.expect('INSTALLATION_DRIFT', self.install)
        self.assertFalse((self.root / 'etc/aiops/hostpack-install.json').exists())
        self.assertFalse((self.root / h.HOST_POLICY.lstrip('/')).exists())

    def test_differing_sudoers_or_policy_is_drift(self):
        self.install()
        path = self.root / h.BASE_SUDOERS_DESTINATION.lstrip('/')
        os.chmod(path, 0o644)
        path.write_bytes(b'something else\n')
        self.expect('INSTALLATION_DRIFT', self.install)
        path.write_bytes(h.render_base_sudoers('astra-runner'))
        policy = self.root / h.HOST_POLICY.lstrip('/')
        policy.write_text('{"control_uid": 1}')
        self.expect('INSTALLATION_DRIFT', self.install)

    def test_artifact_hash_mismatch_stops_before_any_write(self):
        class Bad(FakeOps):
            def read_cached(self, pack, cache_path, expected_sha):
                return b'not the pinned bytes'
        pack = h.Hostpack(self.manifest, make_config(self.manifest), prefix=self.root, owner=os.getuid(), ops=Bad(self.root))
        self.expect('ARTIFACT_HASH_MISMATCH', pack.install)
        self.assertFalse((self.root / 'opt/astra/bin/astra-host-control').exists())

    def test_install_requires_a_real_boundary_evidence_url(self):
        pack = self.make_pack(boundary_evidence_pointer='PENDING')
        self.expect('BOUNDARY_EVIDENCE_REQUIRED', pack.install)
        self.assertFalse((self.root / 'etc/aiops/hostpack-install.json').exists())

    def test_interrupted_install_leaves_an_incomplete_receipt_and_resumes(self):
        calls = {'n': 0}
        original = self.ops.atomic

        def flaky(path, data, mode, uid, gid, replace=True):
            calls['n'] += 1
            if calls['n'] == 6:
                raise OSError('simulated crash')
            return original(path, data, mode, uid, gid, replace=replace)
        self.ops.atomic = flaky
        with self.assertRaises(OSError):
            self.install()
        receipt = json.loads((self.root / 'etc/aiops/hostpack-install.json').read_text())
        self.assertFalse(receipt['complete'])
        self.ops.atomic = original
        for stale in self.root.rglob('.tmp-*'):
            stale.unlink()
        self.assertEqual(self.install()['status'], 'INSTALLED')

    def test_ledger_is_never_created_or_initialised(self):
        self.install()
        self.assertFalse((self.root / 'var/lib/astra/control' / h.LEDGER_DB).exists())
        report = self.pack.verify()
        self.assertEqual(report['components']['ledger']['reason'], 'LEDGER_ABSENT')
        self.assertEqual(report['status'], 'HOLD')

    def test_root_helper_digest_is_not_group_or_world_writable_after_install(self):
        self.install()
        for item in self.manifest['files']:
            mode = os.stat(self.root / item['destination'].lstrip('/')).st_mode & 0o777
            self.assertEqual(mode, item['mode'], item['destination'])
            self.assertFalse(mode & 0o022)


class VerifyTests(PackTest):
    def test_verify_before_install_lists_the_install_step(self):
        report = self.pack.verify()
        self.assertEqual(report['status'], 'HOLD')
        self.assertIn('aiops-hostpack install', report['next_steps'])
        self.assertTrue(report['components']['files']['missing'])

    def test_logins_are_never_claimed_proven_by_this_tool(self):
        self.pack.install()
        self.fake_ledger()
        report = self.pack.verify()
        for lane in h.LANES:
            self.assertIn('PREFLIGHT_NOT_PROVEN', report['components']['lanes'][lane]['reasons'])
        self.assertEqual(report['status'], 'HOLD')

    def test_ready_only_when_everything_including_lane_preflight_holds(self):
        self.pack.install()
        self.fake_ledger()
        (self.root / 'etc/astra/cursor-lane.json').write_text('{}')
        self.pack.boundary_render(COMMIT, (1, 2, 3))
        self.ops.proven = set(h.LANES)
        self.assertEqual(self.pack.verify()['status'], 'READY_FOR_ACTIVATION_CHECK')
        self.ops.proven = set(h.LANES) - {'GLM'}
        self.assertEqual(self.pack.verify()['status'], 'HOLD')

    def test_boundary_digests_come_from_the_manifest_not_the_disk(self):
        self.pack.install()
        out = self.pack.boundary_render('c' * 40, (10, 20, 30))
        expected = json.loads((self.root / h.BOUNDARY_EXPECTED.lstrip('/')).read_text())
        self.assertEqual(expected['hook_sha256'], self.pack.item('/opt/astra/boundary/control_plane_boundary_hook.sh')['sha256'])
        self.assertEqual(expected['evaluator_sha256'], self.pack.item('/opt/astra/boundary/control_plane_boundary.py')['sha256'])
        self.assertEqual(out['policy_sha256'], expected['policy_sha256'])
        self.assertEqual(self.pack.boundary_state()['status'], 'OK')
        (self.root / h.BOUNDARY_POLICY.lstrip('/')).write_text('{}')
        self.assertEqual(self.pack.boundary_state()['reason'], 'DIGEST_MISMATCH')


class CheckpointTests(PackTest):
    PASSWORD = 'offline-test-password'

    def setUp(self):
        super().setUp()
        self.pack.install()
        self.backend = FakeBackend()
        self.cp = h.Checkpoints(self.backend, self.pack, Path('/etc/aiops/hostpack-binding.json'))

    def lane_login(self, lane='GLM', rel='.config/opencode/auth.json', data=b'{"secret":"x"}'):
        account = h.LANE_SPECS[lane]['account']
        path = self.root / 'var/lib/astra-lanes' / account / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return path

    def test_crypto_roundtrip_and_wrong_password(self):
        salt = b'0123456789abcdef'
        key = h.derive('pw', salt)
        envelope, cipher = h.encrypt({'format': h.STATE_FORMAT, 'entries': []}, key, salt, 1, None, COMMIT)
        self.assertEqual(h.decrypt(envelope, cipher, key)['entries'], [])
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR', h.decrypt, envelope, cipher, h.derive('other', salt))
        tampered = dict(envelope, version=2)
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR', h.decrypt, tampered, cipher, key)

    def test_save_captures_ledger_lane_configs_and_logins_encrypted(self):
        directory = self.fake_ledger()
        (self.root / 'etc/astra/cursor-lane.json').write_text('{"cli":"x"}')
        self.lane_login()
        out = self.cp.save(self.PASSWORD)
        self.assertEqual(out['status'], 'SAVED')
        envelope, cipher = self.backend.data[out['commit']]
        self.assertNotIn(b'secret', cipher)
        self.assertNotIn(b'launches', cipher)
        payload = h.decrypt(envelope, cipher, h.derive(self.PASSWORD, base64.b64decode(envelope['salt'])))
        kinds = sorted((e['kind'], e['path']) for e in payload['entries'])
        self.assertIn(('ledger-db', h.LEDGER_DB), kinds)
        self.assertIn(('ledger-file', 'side.json'), kinds)
        self.assertNotIn(('ledger-file', 'census-DEVIN.lock'), kinds)  # a kernel lock is recreated, not restored
        self.assertIn(('lane-config', 'cursor-lane.json'), kinds)
        self.assertIn(('lane-login', 'GLM/.config/opencode/auth.json'), kinds)
        self.assertTrue(all(e['kind'] != 'host-policy' for e in payload['entries']))
        self.assertTrue((directory / h.LEDGER_DB).exists())

    def test_logins_are_captured_only_for_opted_in_lanes(self):
        self.lane_login()
        self.lane_login('GROK_BUILD', '.grok/auth.json')
        pack = self.make_pack(preserve_logins=['GLM'])
        cp = h.Checkpoints(self.backend, pack, Path('/etc/aiops/hostpack-binding.json'))
        self.fake_ledger()
        out = cp.save(self.PASSWORD)
        envelope, cipher = self.backend.data[out['commit']]
        payload = h.decrypt(envelope, cipher, h.derive(self.PASSWORD, base64.b64decode(envelope['salt'])))
        logins = [e['path'] for e in payload['entries'] if e['kind'] == 'lane-login']
        self.assertEqual(logins, ['GLM/.config/opencode/auth.json'])

    def test_symlinks_and_special_files_in_a_login_directory_stop_the_save(self):
        self.lane_login()
        link = self.root / 'var/lib/astra-lanes' / h.LANE_SPECS['GLM']['account'] / '.config/opencode/link'
        os.symlink('/etc/passwd', link)
        self.fake_ledger()
        self.expect('STATE_UNSUPPORTED_FILE', self.cp.save, self.PASSWORD)
        self.assertIsNone(self.backend.h)

    def test_oversized_login_directory_stops_the_save(self):
        self.lane_login(data=b'x' * 1024)
        with patch.object(h, 'MAX_LOGIN_BYTES', 100):
            self.expect('STATE_TOO_LARGE', self.cp.save, self.PASSWORD)

    def test_restore_on_a_fresh_host_brings_back_ledger_configs_and_logins(self):
        self.fake_ledger()
        (self.root / 'etc/astra/cursor-lane.json').write_text('{"cli":"x"}')
        self.lane_login()
        self.cp.save(self.PASSWORD)
        # a reset: new empty host with the same pack installed, no ledger, no logins
        for name in ('var/lib/astra/control', 'var/lib/astra-lanes', 'etc/astra/cursor-lane.json', 'etc/aiops/hostpack-binding.json'):
            target = self.root / name
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        self.pack.make_directories({i['name']: self.ops.ensure_account(self.pack, i) for i in h.account_specs('astra-runner')})
        out = self.cp.restore(self.PASSWORD)
        self.assertEqual(out['status'], 'RESTORED')
        db = sqlite3.connect(self.root / 'var/lib/astra/control' / h.LEDGER_DB)
        self.assertEqual(db.execute("SELECT state FROM launches WHERE id='req1'").fetchone()[0], 'CONFIRMED')
        db.close()
        self.assertEqual((self.root / 'etc/astra/cursor-lane.json').read_text(), '{"cli":"x"}')
        login = self.root / 'var/lib/astra-lanes' / h.LANE_SPECS['GLM']['account'] / '.config/opencode/auth.json'
        self.assertEqual(login.read_bytes(), b'{"secret":"x"}')
        self.assertEqual(os.stat(login).st_mode & 0o777, 0o600)

    def test_restore_never_replaces_a_live_ledger(self):
        self.fake_ledger()
        self.cp.save(self.PASSWORD)
        self.expect('LEDGER_EXISTS', self.cp.restore, self.PASSWORD)

    def test_restore_conflict_with_a_different_existing_file_stops_without_writing(self):
        self.fake_ledger()
        (self.root / 'etc/astra/cursor-lane.json').write_text('{"cli":"x"}')
        self.cp.save(self.PASSWORD)
        shutil.rmtree(self.root / 'var/lib/astra/control')
        (self.root / 'var/lib/astra/control').mkdir(mode=0o700)
        (self.root / 'etc/astra/cursor-lane.json').write_text('{"cli":"different"}')
        self.expect('RESTORE_CONFLICT', self.cp.restore, self.PASSWORD)
        self.assertFalse((self.root / 'var/lib/astra/control' / h.LEDGER_DB).exists())

    def test_wrong_password_restore_and_save_stop(self):
        self.fake_ledger()
        self.cp.save(self.PASSWORD)
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR', self.cp.restore, 'wrong')
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR', self.cp.save, 'wrong')

    def test_rollback_and_freshness_failures_stop(self):
        self.fake_ledger()
        self.cp.save(self.PASSWORD)
        # another host advanced the head: this host's binding is now behind
        other = h.Checkpoints(self.backend, self.pack, Path('/tmp/never-used-binding'))
        other.binding_path = self.root / 'other-binding'
        self.backend.h = self.backend.publish(self.backend.h, *self.backend.data[self.backend.h])
        self.expect('STATE_ROLLBACK_DETECTED', self.cp.save, self.PASSWORD)
        self.expect('STATE_FRESHNESS_UNVERIFIED', self.cp.restore, self.PASSWORD)
        # a head that vanished after a save is a rollback of the remote
        self.backend.h = None
        self.expect('STATE_ROLLBACK_DETECTED', self.cp.save, self.PASSWORD)

    def test_first_save_without_binding_but_existing_remote_head_is_unverified(self):
        self.fake_ledger()
        self.cp.save(self.PASSWORD)
        (self.root / 'etc/aiops/hostpack-binding.json').unlink()
        self.expect('STATE_FRESHNESS_UNVERIFIED', self.cp.save, self.PASSWORD)

    def test_cas_conflict_leaves_the_binding_unchanged(self):
        self.fake_ledger()
        first = self.cp.save(self.PASSWORD)['commit']
        self.backend.conflict = True
        with self.assertRaises(rec.RecoveryError) as ctx:
            self.cp.save(self.PASSWORD)
        self.assertEqual(h.hold(ctx.exception), {'status': 'HOLD', 'reason': 'STATE_CAS_CONFLICT'})  # code survives the CLI
        self.assertEqual(self.cp.bound(), first)

    def test_corrupt_payload_entries_are_rejected(self):
        salt = b'0123456789abcdef'
        key = h.derive(self.PASSWORD, salt)
        good = {'kind': 'lane-login', 'path': 'GLM/x', 'mode': 0o600, 'owner': 'a',
                'bytes': base64.b64encode(b'v').decode(), 'sha256': h.sha(b'v'), 'size': 1}
        for mutate in (lambda e: e.update(path='../../etc/passwd'), lambda e: e.update(path='/etc/passwd'),
                       lambda e: e.update(sha256='0' * 64), lambda e: e.update(kind='root-file'),
                       lambda e: e.update(path='NOPE/x'), lambda e: e.update(mode=0o4777)):
            entry = dict(good)
            mutate(entry)
            payload = {'format': h.STATE_FORMAT, 'source_commit': COMMIT, 'entries': [entry]}
            self.expect('STATE_CORRUPT', h.validate_payload, payload, COMMIT)
        self.expect('STATE_CORRUPT', h.validate_payload, {'format': h.STATE_FORMAT, 'source_commit': 'b' * 40, 'entries': []}, COMMIT)

    def test_empty_ledger_is_never_synthesised_by_a_save(self):
        out = self.cp.save(self.PASSWORD)
        envelope, cipher = self.backend.data[out['commit']]
        payload = h.decrypt(envelope, cipher, h.derive(self.PASSWORD, base64.b64decode(envelope['salt'])))
        self.assertEqual([e for e in payload['entries'] if e['kind'].startswith('ledger')], [])
        self.assertFalse((self.root / 'var/lib/astra/control' / h.LEDGER_DB).exists())


class CliTests(PackTest):
    def test_main_requires_root_and_reports_bounded_errors(self):
        with patch.object(h.os, 'geteuid', return_value=1000), patch('builtins.print') as out:
            self.assertEqual(h.main(['verify']), 1)
        printed = json.loads(out.call_args[0][0])
        self.assertEqual(printed, {'status': 'HOLD', 'reason': 'ROOT_REQUIRED'})

    def test_hold_output_is_bounded_and_never_echoes_arbitrary_exceptions(self):
        self.assertEqual(h.hold(RuntimeError('token sk-ant-secret in message')), {'status': 'HOLD', 'reason': 'HOSTPACK_ERROR'})
        self.assertEqual(h.hold(h.PackError('INSTALLATION_DRIFT', '/opt/astra/bin/x')),
                         {'status': 'HOLD', 'reason': 'INSTALLATION_DRIFT', 'detail': '/opt/astra/bin/x'})

    def test_build_manifest_command_matches_the_committed_manifest(self):
        with patch('builtins.print') as out:
            self.assertEqual(h.main(['build-manifest', '--root', str(ENGINEERING)]), 0)
        self.assertEqual(json.loads(out.call_args[0][0]), self.manifest)

    def test_runner_launcher_is_a_pinned_root_script_without_boot_hooks(self):
        text = (ENGINEERING / 'hostpack/astra-runner-launch').read_text()
        for forbidden in ('systemctl', 'crontab', '/etc/rc', 'nohup', 'at now', '.bashrc', 'update-rc.d'):
            self.assertNotIn(forbidden, text)
        self.assertIn('verify-install', text)
        self.assertIn('check-env', text)
        self.assertIn('boundary-expected.json', text)
        self.assertNotIn('/runner/.env', text)
        # --writable-check asks "can this account write there": it must run as the runner, never as root
        self.assertRegex(text, r'(?m)^as_runner /usr/bin/python3 -I "\$BOUNDARY/control_plane_boundary.py" verify-install')
        self.assertNotRegex(text, r'(?m)^/usr/bin/python3 -I "\$BOUNDARY/control_plane_boundary.py" verify-install')
        self.assertIn('--writable-check', text)
        self.assertNotIn('--env-file', text)  # a runner .env is job-writable: refuse it instead of passing it
        self.assertIn('/.env exists; remove it', text)

    def test_hostpack_launcher_checks_the_whole_path_and_adds_no_boot_hook(self):
        text = (ENGINEERING / 'hostpack/aiops-hostpack').read_text()
        self.assertTrue(text.startswith('#!/usr/bin/python3 -I'))
        for parent in ("'/'", "'/opt'", "'/opt/aiops'", "'/opt/aiops/lib'"):
            self.assertIn(parent, text)
        self.assertIn('O_NOFOLLOW', text)
        self.assertIn('st_uid != 0', text)
        for forbidden in ('systemctl', 'crontab', 'subprocess', 'os.system', 'os.fork'):
            self.assertNotIn(forbidden, text)
        self.assertTrue(os.access(ENGINEERING / 'hostpack/aiops-hostpack', os.X_OK))

    def test_launcher_refuses_an_unprotected_module_path(self):
        import subprocess
        import sys
        done = subprocess.run([sys.executable, '-I', str(ENGINEERING / 'hostpack/aiops-hostpack'), 'verify'],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout), {'reason': 'INSTALLATION_DRIFT', 'status': 'HOLD'})

    def doc_block(self):
        text = (ENGINEERING / 'docs/HOSTPACK_KO.md').read_text()
        match = re.search(r"<<'AIOPS_HOSTPACK_PY'\n(.*?)\nAIOPS_HOSTPACK_PY\n", text, re.S)
        self.assertIsNotNone(match)
        return text, match.group(1)

    def test_bootstrap_hash_table_matches_the_files_it_pins(self):
        _, block = self.doc_block()
        table = dict(re.findall(r'"(engineering/[^"]+)": "([0-9a-f]{64})"', block.split('INSTALL = {')[0]))
        self.assertEqual(set(table), {'engineering/scripts/control_plane_hostpack.py', 'engineering/hostpack/aiops-hostpack',
                                      'engineering/hostpack/manifest.json', 'engineering/hostpack/hostpack.example.json'})
        for name, digest in table.items():
            self.assertEqual(digest, h.sha((ENGINEERING.parent / name).read_bytes()), name)

    def test_bootstrap_block_compiles_and_refuses_placeholders(self):
        text, block = self.doc_block()
        compile(block, 'bootstrap', 'exec')
        self.assertIn("[[ \"$AIOPS_HOSTPACK_COMMIT\" =~ ^[0-9a-f]{40}$ ]] || { echo 'PIN_COMMIT_REQUIRED'; exit 1; }", text)
        self.assertIn("AUDIT_HOST_BOOTSTRAP_REQUIRED", text)
        for forbidden in ('systemctl', 'crontab', 'rc.local', 'enable --now'):
            self.assertNotIn(forbidden, block)

    def test_bootstrap_config_values_pass_the_modules_own_validation(self):
        example = json.loads((ENGINEERING / 'hostpack/hostpack.example.json').read_text())
        example.update(source_commit=COMMIT, manifest_sha256=h.sha(h.canonical(self.manifest)), boundary_evidence_pointer=EVIDENCE)
        h.validate_config(example, h.sha(h.canonical(self.manifest)))
        self.assertTrue(h.evidence_url(example['boundary_evidence_pointer']))

    def test_module_adds_no_daemon_timer_or_boot_hook(self):
        text = (ENGINEERING / 'scripts/control_plane_hostpack.py').read_text()
        for forbidden in ('systemctl', 'crontab', 'enable --now', 'rc.local', 'daemon(', 'os.fork', 'sched.scheduler'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(len(re.findall(r'subprocess\.run\(', text)), 2)  # useradd and visudo -cf only


if __name__ == '__main__':
    unittest.main()
