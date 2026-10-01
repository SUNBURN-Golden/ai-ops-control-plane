"""HR-D3 offline fakes: no host install, provider request, or real credentials."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import control_plane_recover as r
import control_plane_recover_token as t

FAKE = 'sk-ant-oat01-' + 'A' * 96  # Synthetic, never issued.
TOKEN = {'value': FAKE, 'issued_at': 100, 'expires_at': 4000000000, 'verified': True}


class Backend:
    def __init__(self):
        self.h = 'a' * 40
        self.data = {}
        self.conflict = False
    def head(self):
        return self.h
    def load(self, h):
        if h not in self.data:
            r.fail('STATE_CORRUPT')
        return self.data[h]
    def publish(self, expected, envelope, cipher):
        if expected != self.h or self.conflict:
            r.fail('STATE_CAS_CONFLICT')
        self.h = format(int(self.h, 16) + 1, '040x')
        self.data[self.h] = envelope, cipher
        return self.h


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        # Offline filesystem under the current unprivileged CI user. Production
        # ownership checks have separate tests; never create a host account.
        self.uid, self.gid = os.getuid(), os.getgid()
        self.original_protected_parent = r.protected_parent
        original_read = r.read_regular
        def fake_read(path, **kw):
            kw['protected'] = False
            return original_read(path, **kw)
        for target, replacement in [('read_regular', fake_read), ('protected_parent', lambda *a, **k: None)]:
            patcher = patch.object(r, target, replacement); patcher.start(); self.addCleanup(patcher.stop)
        for target in ('chown', 'fchown'):
            patcher = patch.object(r.os, target, lambda *a: None); patcher.start(); self.addCleanup(patcher.stop)
        self.backend = Backend()
        self.state = self.root / 'ledger'
        self.binding = self.root / 'binding'
        self.tokenfile = self.root / 'token'
        self.cp = r.Checkpoints(self.backend, self.state, self.binding, self.tokenfile, self.uid, self.gid, self.root)
        self.salt = b'0123456789abcdef'
        self.key = r.derive('offline-fake-password', self.salt)
        self.payload = {'format': 'AUDIT_HOST_ONLY_V1', 'files': [
            {'path': 'claim', 'kind': 'file', 'uid': self.uid, 'gid': self.gid, 'mode': 0o600,
             'bytes': base64.b64encode(b'consumed').decode(), 'sha256': r.sha(b'consumed'), 'size': 8}],
             'claude_token': dict(TOKEN)}
        self.envelope, self.cipher = r.encrypt(self.payload, self.key, self.salt, 1, 'b' * 40)
        self.backend.data[self.backend.h] = self.envelope, self.cipher
    def tearDown(self):
        self.tmp.cleanup()
    def expect(self, code, fn, *args, **kwargs):
        with self.assertRaises(r.RecoveryError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(code, ctx.exception.code)
    def restore(self):
        return self.cp.restore('offline-fake-password')
    def manifest(self):
        return {'source_commit': r.SOURCE, 'scope': 'AUDIT_HOST_ONLY', 'files': [
            {'destination': '/opt/aiops/lib/test', 'cache_path': 'test', 'sha256': r.sha(b'fixed'), 'mode': 0o644}]}
    def install(self):
        # Fake root protected ancestors, not production paths/accounts.
        for x in ('opt', 'usr', 'usr/local', 'etc'):
            (self.root / x).mkdir(exist_ok=True)
        inst = r.Installer(self.manifest(), prefix=self.root)
        inst.install(lambda _: b'fixed')
        return inst
    def test_crypto_roundtrip(self):
        self.assertEqual(self.payload, r.decrypt(self.envelope, self.cipher, self.key))
    def test_wrong_password(self):
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR', self.cp.fetch, 'wrong')
    def test_corrupt_cipher(self):
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR', r.decrypt, self.envelope, self.cipher[:-1] + b'X', self.key)
    def test_authenticated_metadata(self):
        e = dict(self.envelope, version=2)
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR', r.decrypt, e, self.cipher, self.key)
    def test_missing_checkpoint(self):
        self.backend.data.clear()
        self.expect('STATE_CORRUPT', self.restore)
        self.assertFalse(self.state.exists())
    def test_restore_claim_and_token(self):
        self.restore()
        self.assertEqual(b'consumed', (self.state / 'claim').read_bytes())
        self.assertEqual(FAKE, self.tokenfile.read_text())
        self.assertEqual(0o600, self.tokenfile.stat().st_mode & 0o777)
    def test_restore_idempotent(self):
        self.restore()
        self.assertEqual(self.payload, self.restore()[0])
    def test_unbound_ledger_never_overwritten(self):
        self.state.mkdir()
        self.expect('STATE_CORRUPT', self.restore)
    def test_rollback(self):
        self.restore()
        self.backend.h = 'c' * 40
        self.expect('STATE_ROLLBACK_DETECTED', self.cp.current)
    def test_local_corruption(self):
        self.restore()
        (self.state / 'claim').write_bytes(b'changed')
        self.expect('STATE_CORRUPT', self.cp.current)
    def test_token_drift(self):
        self.tokenfile.write_text('different')
        self.tokenfile.chmod(0o600)
        self.expect('INSTALLATION_DRIFT', self.restore)
    def test_resume_after_rename(self):
        with patch.object(self.cp, 'restore_token', side_effect=r.RecoveryError('INTERRUPTED')):
            self.expect('INTERRUPTED', self.restore)
        self.assertTrue(self.cp.receipt.exists())
        self.restore()
        self.assertFalse(self.cp.receipt.exists())
        self.assertEqual(FAKE, self.tokenfile.read_text())
    def test_resume_after_token(self):
        with patch.object(self.cp, 'bind', side_effect=r.RecoveryError('INTERRUPTED')):
            self.expect('INTERRUPTED', self.restore)
        self.restore()
        self.cp.current()
    def test_crash_before_rename(self):
        with patch.object(r.os, 'rename', side_effect=OSError('fake')):
            with self.assertRaises(OSError):
                self.restore()
        self.assertFalse(self.state.exists())
        self.restore()
    def test_tampered_restore_receipt(self):
        self.state.mkdir()
        r.atomic(self.cp.receipt, r.canonical({'commit': 'f' * 40}))
        self.expect('STATE_CORRUPT', self.restore)
    def test_cas_conflict(self):
        self.restore()
        self.backend.conflict = True
        self.expect('STATE_CAS_CONFLICT', self.cp.save, self.key, TOKEN)
    def test_save_increments(self):
        self.restore()
        self.cp.save(self.key, TOKEN)
        self.assertEqual(2, self.cp.current()['version'])
        self.assertEqual(self.payload, self.cp.fetch(key=self.key)[2])
    def test_optional_github_encrypted(self):
        self.restore()
        self.cp.save(self.key, TOKEN, github_token='fake-only')
        self.assertNotIn(b'fake-only', self.backend.data[self.backend.h][1])
        self.assertEqual('fake-only', self.cp.fetch(key=self.key)[2]['github_token'])
    def test_empty_ledger_rejected(self):
        self.state.mkdir()
        self.expect('STATE_CORRUPT', r.inventory, self.state, self.uid, self.gid)
    def test_enrollment_requires_salt(self):
        self.state.mkdir()
        r.atomic(self.state / 'claim', b'consumed')
        self.expect('STATE_CORRUPT', self.cp.save, self.key, TOKEN, enrollment=True, expected=self.backend.h)
    def test_payload_path_traversal(self):
        p = copy.deepcopy(self.payload); p['files'][0]['path'] = '../claim'
        self.expect('STATE_CORRUPT', r.validate_payload, p, self.uid, self.gid)
    def test_payload_duplicate(self):
        p = copy.deepcopy(self.payload); p['files'] *= 2
        self.expect('STATE_CORRUPT', r.validate_payload, p, self.uid, self.gid)
    def test_payload_missing_parent(self):
        p = copy.deepcopy(self.payload); p['files'][0]['path'] = 'missing/claim'
        self.expect('STATE_CORRUPT', r.validate_payload, p, self.uid, self.gid)
    def test_payload_permissions(self):
        p = copy.deepcopy(self.payload); p['files'][0]['mode'] = 0o666
        self.expect('STATE_CORRUPT', r.validate_payload, p, self.uid, self.gid)
    def test_payload_owner(self):
        p = copy.deepcopy(self.payload); p['files'][0]['uid'] = 666
        self.expect('STATE_CORRUPT', r.validate_payload, p, self.uid, self.gid)
    def test_payload_hash(self):
        p = copy.deepcopy(self.payload); p['files'][0]['sha256'] = '0' * 64
        self.expect('STATE_CORRUPT', r.validate_payload, p, self.uid, self.gid)
    def test_payload_invalid_token(self):
        p = copy.deepcopy(self.payload); p['claude_token']['value'] = 'invalid'
        self.expect('STATE_CORRUPT', r.validate_payload, p, self.uid, self.gid)
    def test_inventory_symlink(self):
        self.state.mkdir(); (self.state / 'link').symlink_to('/etc/passwd')
        self.expect('STATE_CORRUPT', r.inventory, self.state, self.uid, self.gid)
    def test_inventory_hardlinked_claim(self):
        self.state.mkdir(); r.atomic(self.state / 'claim', b'consumed')
        os.link(self.state / 'claim', self.state / 'receipt')
        self.assertEqual(2, len(r.inventory(self.state, self.uid, self.gid)))
    def test_install_and_idempotence(self):
        inst = self.install(); inst.install(lambda _: self.fail('must reuse installed'))
        inst.verify()
    def test_install_hash_failure(self):
        for x in ('opt', 'usr', 'usr/local', 'etc'):
            (self.root / x).mkdir(exist_ok=True)
        inst = r.Installer(self.manifest(), prefix=self.root)
        self.expect('ARTIFACT_HASH_MISMATCH', inst.install, lambda _: b'bad')
        self.assertFalse(inst.path('/opt/aiops/lib/test').exists())
    def test_install_drift(self):
        inst = self.install(); inst.path('/opt/aiops/lib/test').write_bytes(b'bad')
        self.expect('INSTALLATION_DRIFT', inst.install, lambda _: b'fixed')
        self.assertEqual(b'bad', inst.path('/opt/aiops/lib/test').read_bytes())
    def test_install_receipt_incomplete(self):
        inst = self.install(); r.atomic(inst.path('/etc/aiops/recovery-install.json'), b'{}')
        self.expect('INSTALLATION_INCOMPLETE', inst.verify)
    def test_install_resume(self):
        inst = self.install(); inst.path('/opt/aiops/lib/test').unlink()
        r.atomic(inst.path('/etc/aiops/recovery-install.json'), b'{}')
        inst.install(lambda _: b'fixed'); inst.verify()
    def test_manifest_wrong_source(self):
        m = self.manifest(); m['source_commit'] = 'latest'
        self.expect('MANIFEST_REJECTED', r.Installer, m)
    def test_manifest_bad_destination(self):
        m = self.manifest(); m['files'][0]['destination'] = '/tmp/bad'
        self.expect('MANIFEST_REJECTED', r.Installer, m)
    def test_manifest_duplicate(self):
        m = self.manifest(); m['files'] *= 2
        self.expect('MANIFEST_REJECTED', r.Installer, m)
    def test_symlink_installed(self):
        inst = self.install(); p = inst.path('/opt/aiops/lib/test'); p.unlink(); p.symlink_to(self.root / 'binding')
        self.expect('INSTALLATION_DRIFT', inst.verify)
    def test_dedupe_fail_no_model(self):
        self.restore(); guard = r.AuditGuard(self.cp, self.key, TOKEN, None)
        v = guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40,
                         [{'body': 'ASTRA_AUDIT_V1 pr=36 head=' + 'd' * 40 + ' result=FAIL'}])
        self.assertEqual('FAIL', v['result']); self.assertFalse(guard.journal.exists())
    def test_started_checkpoint_before_call(self):
        self.restore(); guard = r.AuditGuard(self.cp, self.key, TOKEN, None)
        guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, [])
        remote = self.cp.fetch(key=self.key)[2]
        journal = next(x for x in remote['files'] if x['path'] == 'recovery-audits.json')
        self.assertIn(b'STARTED', base64.b64decode(journal['bytes']))
    def test_unknown_no_rerun(self):
        self.restore(); guard = r.AuditGuard(self.cp, self.key, TOKEN, None)
        guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, [])
        self.expect('UNKNOWN_REPRESENTATIVE_DECISION_REQUIRED', guard.before, 'BeautifulMind-JT/ZARI', 36, 'd' * 40, [])
    def test_unknown_with_partial_mutation(self):
        self.restore(); guard = r.AuditGuard(self.cp, self.key, TOKEN, None)
        guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, [])
        r.atomic(self.state / 'partial', b'partial')
        self.expect('UNKNOWN_REPRESENTATIVE_DECISION_REQUIRED', self.cp.current)
    def test_result_checkpoint_after_call(self):
        self.restore(); guard = r.AuditGuard(self.cp, self.key, TOKEN, None)
        guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, []); guard.after('BeautifulMind-JT/ZARI', 36, 'd' * 40, True)
        self.assertEqual('AUDIT_EXISTS', guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, [])['status'])
    def test_failed_call_is_unknown(self):
        self.restore(); guard = r.AuditGuard(self.cp, self.key, TOKEN, None)
        guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, []); guard.after('BeautifulMind-JT/ZARI', 36, 'd' * 40, False)
        self.expect('UNKNOWN_REPRESENTATIVE_DECISION_REQUIRED', guard.before, 'BeautifulMind-JT/ZARI', 36, 'd' * 40, [])
    def test_expiry_warning(self):
        self.assertTrue(r.token_warning({'expires_at': 30 * 86400}, now=1)['expires_within_30_days'])
    def test_expiry_no_warning(self):
        self.assertFalse(r.token_warning({'expires_at': 31 * 86400}, now=1)['expires_within_30_days'])
    def test_expired_token(self):
        self.expect('TOKEN_REISSUE_REQUIRED', r.token_warning, {'expires_at': 1}, now=2)
    def test_preflight_default_no(self):
        with patch.object(r, 'confirm_model', return_value=False), patch.object(r, 'config') as cfg:
            self.assertEqual(0, r.fable_main(['preflight'])); cfg.assert_not_called()
    def test_confirmation_explicit(self):
        for answer in ('', 'N', 'yes'):
            self.assertFalse(r.confirm_model(lambda _: answer))
        self.assertTrue(r.confirm_model(lambda _: 'y'))
    def test_token_replay_plain(self):
        self.assertEqual(FAKE, t.replay(FAKE.encode()))
    def test_token_replay_wrap(self):
        self.assertEqual(FAKE, t.replay((FAKE[:38] + '\r\n' + FAKE[38:]).encode(), columns=40))
    def test_token_replay_cursor(self):
        self.assertEqual(FAKE, t.replay((FAKE[:32] + '\x1b[12C' + FAKE[32:]).encode()))
    def test_token_replay_scroll(self):
        self.assertEqual(FAKE, t.replay((FAKE + '\r\n' + '!\r\n' * 90).encode(), lines=8))
    def test_token_replay_ambiguous(self):
        self.expect('TOKEN_EXTRACTION_FAILED_REUSE_TRANSCRIPT', t.replay, (FAKE + '\r\n!\r\n' + FAKE.replace('A','B')).encode())
    def test_extract_retry_same_record(self):
        self.expect('TOKEN_EXTRACTION_FAILED_REUSE_TRANSCRIPT', t.replay, b'incomplete')
        raw = (FAKE + '\r\n!').encode()
        self.assertEqual(t.replay(raw), t.replay(raw))
    def test_extractor_selftest(self):
        t.selftest()
    def test_validate_401_safe(self):
        fake_run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=b'401', stderr=b'')
        self.expect('TOKEN_REISSUE_REQUIRED', t.validate, FAKE, {}, run=fake_run, confirm=lambda: True)
    def test_validate_no_confirmation_no_call(self):
        with patch.object(t.subprocess, 'run') as run:
            self.expect('TOKEN_VALIDATION_CANCELLED_TRANSCRIPT_RETAINED', t.validate, FAKE, {}, confirm=lambda: False)
            run.assert_not_called()
    def test_validate_format_no_call(self):
        self.expect('TOKEN_FORMAT_INVALID', t.validate, 'bad', {}, confirm=lambda: self.fail('must not confirm'))
    def test_validate_success(self):
        def run(argv, **kw):
            self.assertNotIn(FAKE, argv)
            self.assertEqual('low', argv[argv.index('--effort') + 1])
            return types.SimpleNamespace(returncode=0, stdout=b'{"type":"result","is_error":false,"result":"ok"}', stderr=b'')
        self.assertEqual(FAKE, t.validate(FAKE, {}, run=run, confirm=lambda: True, now=100)['value'])
    def test_validate_missing_result(self):
        run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b'{}', stderr=b'')
        self.expect('TOKEN_VALIDATION_FAILED_TRANSCRIPT_RETAINED', t.validate, FAKE, {}, run=run, confirm=lambda: True)
    def test_billing_missing_fail_closed(self):
        self.expect('BILLING_PREFLIGHT_UNAVAILABLE', r.billing, {}, None, FAKE)
    def test_billing_unverified(self):
        self.expect('OVERAGE_UNVERIFIED', r.billing, {}, None, FAKE, probe=lambda: {})
    def test_billing_policy_pass(self):
        fable = types.SimpleNamespace(overage_policy=lambda x: (None, None) if x == {'isUsingOverage':False} else ('OVERAGE_UNVERIFIED', None))
        r.billing({}, fable, FAKE, probe=lambda: {'token_sha256': r.sha(FAKE.encode()), 'observed_at': int(r.time.time()), 'rate_limit_info': {'isUsingOverage':False}})
    def test_billing_overage_block(self):
        fable = types.SimpleNamespace(overage_policy=lambda _: ('OVERAGE_NOT_BLOCKED', None))
        self.expect('OVERAGE_NOT_BLOCKED', r.billing, {}, fable, FAKE,
                    probe=lambda: {'token_sha256':r.sha(FAKE.encode()),'observed_at':int(r.time.time()),'rate_limit_info':{}})
    def test_billing_stale(self):
        self.expect('OVERAGE_UNVERIFIED', r.billing, {}, None, FAKE,
                    probe=lambda: {'token_sha256':r.sha(FAKE.encode()),'observed_at':1})
    def test_private_repository_required(self):
        self.expect('STATE_REPOSITORY_NOT_PRIVATE', r.GithubState, 'BeautifulMind-JT/aiops-state','audit-host','fake', request=lambda *a: {'private':False})
    def test_redirect_never_forwards_credentials(self):
        self.expect('STATE_AUTHORITY_UNAVAILABLE', r.NoRedirect().redirect_request, None,None,302,'',{},'https://evil.invalid')
    def test_duplicate_json_rejected(self):
        self.expect('STATE_CORRUPT', r.strict_json, b'{"x":1,"x":2}')
    def test_scope_rejects_program_resume(self):
        with patch.object(r, 'config') as cfg:
            self.assertEqual(1, r.fable_main(['program'])); cfg.assert_not_called()

    def test_replay_does_not_append_instructions(self):
        self.assertEqual(FAKE, t.replay((FAKE + '\r\n\r\nSave this token in a safe place.').encode()))
    def test_prerequisites_width_before_login(self):
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=b'24 80', stderr=b'')
        self.expect('PTY_WIDTH_UNVERIFIED', t.prerequisites, {'claude_version':t.EXPECTED_CLI}, run=run)
        self.assertEqual(1, len(calls)); self.assertNotIn('setup-token', str(calls))
    def test_prerequisites_version_before_login(self):
        def run(argv, **kw):
            return types.SimpleNamespace(returncode=0, stdout=b'24 500' if 'script' in argv[0] else b'wrong', stderr=b'')
        self.expect('CLI_VERSION_DRIFT', t.prerequisites, {'claude_version':t.EXPECTED_CLI}, run=run)
    def test_prerequisites_success_no_model(self):
        def run(argv, **kw):
            self.assertNotIn('-p', argv)
            return types.SimpleNamespace(returncode=0, stdout=b'24 500' if 'script' in argv[0] else t.EXPECTED_CLI.encode(), stderr=b'')
        t.prerequisites({'claude_version':t.EXPECTED_CLI}, run=run)
    def test_failed_selftest_prevents_login(self):
        with patch.object(t, 'selftest', side_effect=r.RecoveryError('TOKEN_EXTRACTOR_SELFTEST_FAILED')):
            self.expect('TOKEN_EXTRACTOR_SELFTEST_FAILED', t.prerequisites, {}, run=lambda *a, **k:self.fail('no process'))
    def test_cleanup_only_verified_root_record(self):
        path = self.root / 'aiops-recover-token'; path.mkdir(mode=0o700)
        r.atomic(path / 'typescript', FAKE.encode())
        with patch.object(t, 'TRANSCRIPT', self.root / 'unused'), patch.object(t, 'read_regular', r.read_regular):
            t.cleanup({})
        self.assertFalse((path / 'typescript').exists())
    def test_reissue_reuses_transcript_without_capture(self):
        path = self.root / 'aiops-recover-token'; path.mkdir(mode=0o700)
        r.atomic(path / 'typescript', FAKE.encode())
        inst = types.SimpleNamespace(verify=lambda:None, manifest={'files':[
            {'destination':'/opt/aiops/lib/fable/control_plane_fable.py','sha256':'fake'}]})
        with patch.object(t, 'TRANSCRIPT', self.root / 'unused'), patch.object(t, 'prerequisites'), \
             patch.object(t, 'read_regular', r.read_regular), patch.object(t, 'capture') as capture, \
             patch.object(t, 'pinned_module', return_value=types.SimpleNamespace(overage_policy=lambda _:None)), \
             patch.object(t, 'billing'), patch.object(t, 'validate', return_value=TOKEN):
            self.assertEqual(TOKEN, t.reissue({}, inst)); capture.assert_not_called()
        self.assertTrue((path / 'typescript').exists())
    def test_audit_cas_conflict_before_model(self):
        self.restore(); self.backend.conflict=True
        guard=r.AuditGuard(self.cp,self.key,TOKEN,None)
        self.expect('STATE_CAS_CONFLICT', guard.before,'BeautifulMind-JT/ZARI',36,'d'*40,[])
        self.assertEqual('STARTED', next(iter(guard.entries().values()))['state'])
    def test_checkpoint_ack_lost_holds(self):
        self.restore()
        with patch.object(self.cp,'bind',side_effect=r.RecoveryError('ACK_LOST')):
            self.expect('ACK_LOST',self.cp.save,self.key,TOKEN)
        self.expect('STATE_ROLLBACK_DETECTED',self.cp.current)
    def test_current_inventory_permission_drift(self):
        self.restore(); (self.state/'claim').chmod(0o666)
        self.expect('STATE_CORRUPT',self.cp.current)
    def test_state_root_writable(self):
        self.restore(); self.state.chmod(0o777)
        self.expect('STATE_CORRUPT',self.cp.current)
    def test_barrier_contention(self):
        # Fake ownership on platforms where CI is not root; real flock semantics.
        original=r.os.fstat
        def stat_root(fd):
            s=original(fd)
            return types.SimpleNamespace(st_mode=s.st_mode,st_nlink=s.st_nlink,st_uid=0)
        with patch.object(r.os,'fstat',side_effect=stat_root):
            with r.barrier(self.root/'lock'):
                def second():
                    with r.barrier(self.root/'lock'):
                        self.fail('second owner')
                self.expect('RECOVERY_BUSY',second)
    def test_manifest_matches_committed_sources(self):
        root=Path(__file__).resolve().parents[2]
        m=json.loads((root/'engineering/recovery/manifest.json').read_text())
        for item in m['files']:
            if item['cache_path'].startswith('engineering/'):
                self.assertEqual(item['sha256'],r.sha((root/item['cache_path']).read_bytes()))
        self.assertEqual('low',m['effort'])
    def test_install_receipt_manifest_drift(self):
        inst=self.install()
        receipt=inst.path('/etc/aiops/recovery-install.json')
        r.atomic(receipt,r.canonical({'complete':True,'manifest_sha256':'0'*64}))
        self.expect('INSTALLATION_INCOMPLETE',inst.verify)
    def test_fixed_claude_launcher_updates_disabled(self):
        root=Path(__file__).resolve().parents[2]
        raw=(root/'engineering/recovery/claude').read_text()
        self.assertIn('DISABLE_AUTOUPDATER=1',raw); self.assertIn('DISABLE_UPDATES=1',raw)
        self.assertIn('claude-2.1.286',raw)
    def test_snapshot_never_contains_password(self):
        self.restore(); self.cp.save(self.key,TOKEN)
        envelope,cipher=self.backend.data[self.backend.h]
        self.assertNotIn(b'offline-fake-password',r.canonical(envelope)+cipher)
    def test_github_cas_never_force(self):
        requests=[]
        def request(method,path,data):
            requests.append((method,path,data))
            if method=='GET' and not path:return {'private':True}
            if method=='GET':return {'object':{'sha': ('b' if any(x[0]=='PATCH' for x in requests) else 'a')*40}}
            return {'sha':'b'*40}
        backend=r.GithubState('BeautifulMind-JT/aiops-state','audit-host','fake',request=request)
        backend.publish('a'*40,{'chunks':[r.sha(b'cipher')]},b'cipher')
        patch_req=next(x for x in requests if x[0]=='PATCH')
        self.assertIs(False,patch_req[2]['force'])
    def test_checkpoint_missing_chunk(self):
        def request(method,path,data):
            if not path:return {'private':True}
            if '/commits/' in path:return {'tree':{'sha':'a'*40}}
            if '/trees/' in path:return {'tree':[{'path':'checkpoint.json','mode':'100644','type':'blob','sha':'b'*40}]}
            return {'encoding':'base64','content':base64.b64encode(r.canonical({'chunks':['f'*64]})).decode()}
        backend=r.GithubState('BeautifulMind-JT/aiops-state','audit-host','fake',request=request)
        self.expect('STATE_CORRUPT',backend.load,'a'*40)
    def test_checkpoint_chunk_hash_corruption(self):
        def request(method,path,data):
            if not path:return {'private':True}
            if '/commits/' in path:return {'tree':{'sha':'a'*40}}
            if '/trees/' in path:return {'tree':[{'path':name,'mode':'100644','type':'blob','sha':digest*40} for name,digest in [('checkpoint.json','b'),('cipher-000000','c')]]}
            raw=r.canonical({'chunks':['f'*64]}) if path.endswith('b'*40) else b'cipher'
            return {'encoding':'base64','content':base64.b64encode(raw).decode()}
        backend=r.GithubState('BeautifulMind-JT/aiops-state','audit-host','fake',request=request)
        self.expect('STATE_CORRUPT',backend.load,'a'*40)
    def test_package_wheel_hash_failure(self):
        (self.root/'bad.whl').write_bytes(b'bad')
        self.expect('ARTIFACT_HASH_MISMATCH',r.fetch_artifact,
                    {'cache_path':'bad.whl','sha256':'f'*64,'wheel_sha256':'e'*64,'wheel_member':'module.py'},
                    {'artifact_cache':str(self.root)})

    def test_no_root_no_bootstrap(self):
        with patch.object(r.os,'geteuid',return_value=1001):
            self.expect('PRIVILEGED_EXECUTOR_UNAVAILABLE',r.config)
    def test_existing_account_identity_drift(self):
        cfg={'account':{'name':'aiops-auditor','uid':991,'gid':991,'home':'/var/lib/aiops-auditor'}}
        fake=types.SimpleNamespace(pw_uid=992,pw_gid=991,pw_dir='/var/lib/aiops-auditor',pw_shell='/usr/sbin/nologin')
        with patch.object(r.pwd,'getpwnam',return_value=fake),patch.object(r.subprocess,'run') as run:
            self.expect('INSTALLATION_DRIFT',r.ensure_account,cfg)
            run.assert_not_called()
    def test_audit_does_not_create_missing_account(self):
        cfg={'account':{'name':'aiops-auditor','uid':991,'gid':991,'home':'/var/lib/aiops-auditor'}}
        with patch.object(r.pwd,'getpwnam',side_effect=KeyError()),patch.object(r.subprocess,'run') as run:
            self.expect('INSTALLATION_DRIFT',r.ensure_account,cfg,create=False)
            run.assert_not_called()
    def test_binding_never_contains_token_value(self):
        self.restore()
        self.assertNotIn(FAKE.encode(),self.binding.read_bytes())
    def test_invalid_token_shape_is_corrupt(self):
        p=copy.deepcopy(self.payload);p['claude_token']='invalid'
        self.expect('STATE_CORRUPT',r.validate_payload,p,self.uid,self.gid)

    def test_reissue_401_preserves_raw_record_and_diagnostics(self):
        import io
        from contextlib import redirect_stdout
        path=self.root/'aiops-recover-token';path.mkdir(mode=0o700)
        r.atomic(path/'typescript',FAKE.encode())
        inst=types.SimpleNamespace(verify=lambda:None,manifest={'files':[
            {'destination':'/opt/aiops/lib/fable/control_plane_fable.py','sha256':'fake'}]})
        out=io.StringIO()
        with patch.object(t,'TRANSCRIPT',self.root/'unused'),patch.object(t,'prerequisites'), \
             patch.object(t,'read_regular',r.read_regular),patch.object(t,'pinned_module',return_value=types.SimpleNamespace(overage_policy=None)), \
             patch.object(t,'billing'),patch.object(t,'validate',side_effect=r.RecoveryError('TOKEN_REISSUE_REQUIRED')), \
             redirect_stdout(out):
            self.expect('TOKEN_REISSUE_REQUIRED',t.reissue,{},inst)
        self.assertNotIn(FAKE,out.getvalue());self.assertIn('length',out.getvalue())
        self.assertTrue((path/'typescript').exists());self.assertTrue((path/'reissue-needed.json').exists())

    def test_protected_parent_rejects_writable_ancestor(self):
        parent=self.root/'writable';parent.mkdir(mode=0o777);parent.chmod(0o777)
        self.expect('INSTALLATION_DRIFT',self.original_protected_parent,parent/'file',boundary=self.root,owner=self.uid)
    def test_protected_parent_rejects_directory_symlink(self):
        parent=self.root/'alias';parent.symlink_to(self.root,target_is_directory=True)
        self.expect('INSTALLATION_DRIFT',self.original_protected_parent,parent/'file',boundary=self.root,owner=self.uid)


if __name__ == '__main__':
    unittest.main()
