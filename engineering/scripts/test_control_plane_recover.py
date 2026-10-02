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
    def head(self, *, missing=False):
        if self.h is None and not missing:
            r.fail('STATE_CORRUPT')
        return self.h
    def load(self, h):
        if h not in self.data:
            r.fail('STATE_CORRUPT')
        return self.data[h]
    def publish(self, expected, envelope, cipher):
        if expected != self.h or self.conflict:
            r.fail('STATE_CAS_CONFLICT')
        self.h = format(int(self.h or 'a'*40, 16) + 1, '040x')
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
            {'path': 'claim', 'kind': 'file', 'uid': 'AUDITOR' if self.uid else 0, 'gid': 'AUDITOR' if self.gid else 0, 'mode': 0o600,
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
        self.backend.h = None
        self.expect('STATE_CORRUPT', self.cp.save, self.key, TOKEN, enrollment=True)
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
        guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, []); guard.after('BeautifulMind-JT/ZARI', 36, 'd' * 40, {'result':'PASS'}, {'model_attempted':True})
        self.assertEqual('AUDIT_EXISTS', guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, [])['status'])
    def test_failed_call_is_unknown(self):
        self.restore(); guard = r.AuditGuard(self.cp, self.key, TOKEN, None)
        guard.before('BeautifulMind-JT/ZARI', 36, 'd' * 40, []); guard.after('BeautifulMind-JT/ZARI', 36, 'd' * 40, {'status':'ERROR'}, {'model_attempted':True})
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
        fake_run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=b'{"type":"result","is_error":true,"error":{"type":"authentication_error","status_code":401}}', stderr=b'')
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
             patch.object(t, 'validate', return_value=TOKEN):
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
        cfg={'account':{'name':'aiops-auditor','home':'/var/lib/aiops-auditor'}}
        fake=types.SimpleNamespace(pw_uid=0,pw_gid=996,pw_dir='/var/lib/aiops-auditor',pw_shell='/usr/sbin/nologin')
        with patch.object(r.pwd,'getpwnam',return_value=fake),patch.object(r.subprocess,'run') as run:
            self.expect('INSTALLATION_DRIFT',r.ensure_account,cfg)
            run.assert_not_called()
    def test_audit_does_not_create_missing_account(self):
        cfg={'account':{'name':'aiops-auditor','home':'/var/lib/aiops-auditor'}}
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
             patch.object(t,'validate',side_effect=r.RecoveryError('TOKEN_REISSUE_REQUIRED')), \
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

    def fake_wrapper(self, event=None, attempted=True, failure=None, comments=(), stream=None):
        """Real recovery admission/checkpoint and original stream guard, fake CLI/GH."""
        import io
        import contextlib
        import control_plane_fable as baseline
        cfg={'claude_version':t.EXPECTED_CLI,'account':{'name':'aiops-auditor','home':'/var/lib/aiops-auditor'}}
        ctx=types.SimpleNamespace(runner=types.SimpleNamespace(last_execution={}),last_failure=None)
        fake=types.SimpleNamespace(production_context=lambda **kw:(ctx,t.EXPECTED_CLI))
        fake.GitHub=lambda token:types.SimpleNamespace(pages=lambda path:list(comments))
        calls=[]
        def main(args):
            calls.append(list(args)); fake.production_context()
            ctx.runner.last_execution={'model_attempted':attempted};ctx.last_failure=failure
            actual=dict(event or {'kind':args[0],'result':'PASS','session':'f401a','output_sha256':'401'+'a'*61,'line':401})
            if stream is not None:
                _, stopped = baseline.read_stream(io.BytesIO(stream))
                if stopped:
                    actual = {'status':'ERROR','reason':stopped['error_code']}
            print(json.dumps(actual))
            return 1 if actual.get('status')=='ERROR' else 0
        fake.main=main
        from contextlib import ExitStack
        stack=ExitStack();self.addCleanup(stack.close)
        for target,value in [('config',lambda:cfg),('components',lambda cfg:(types.SimpleNamespace(verify=lambda:None),self.cp,{'destination':'fake','sha256':'fake'},'fake-gh')),
                             ('keyring',lambda *a:self.key),('pinned_module',lambda *a:fake),('TOKEN',self.tokenfile)]:
            stack.enter_context(patch.object(r,target,value))
        stack.enter_context(patch.object(r,'barrier',lambda *a:contextlib.nullcontext()))
        stack.enter_context(patch.object(r.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=t.EXPECTED_CLI.encode())))
        return calls
    def audit_args(self):
        return ['audit','--repository','BeautifulMind-JT/ZARI','--pr','36','--head','d'*40,'--gate','ARCHITECTURE','--depth','A3']
    def test_f1_audit_without_billing_mock(self):
        self.restore();calls=self.fake_wrapper()
        self.assertEqual(0,r.fable_main(self.audit_args()));self.assertEqual(1,len(calls))
        self.assertFalse(hasattr(r,'billing'))
    def test_f1_consult_without_billing_mock(self):
        self.restore();calls=self.fake_wrapper()
        self.assertEqual(0,r.fable_main(['consult','--repository','BeautifulMind-JT/ZARI','--issue','36','--comment','1']))
        self.assertEqual(1,len(calls))
    def test_f1_preflight_repeat_without_billing_mock(self):
        self.restore();calls=self.fake_wrapper(event={'status':'PASS'})
        with patch.object(r,'confirm_model',return_value=True):
            self.assertEqual(0,r.fable_main(['preflight']));self.assertEqual(0,r.fable_main(['preflight']))
        self.assertEqual(2,len(calls))
    def token_events(self,signal=None,**result):
        events=[] if signal is None else [{'type':'rate_limit_event','rate_limit_info':signal}]
        events.append({'type':'result','is_error':False,'result':'ok','session_id':'f401a','hash':'401'+'a'*61,**result})
        return b'\n'.join(r.canonical(x) for x in events)
    def test_f1_token_validate_stream_guard_without_billing_mock(self):
        import control_plane_fable as fable
        raw=self.token_events({'status':'allowed','isUsingOverage':False})
        run=lambda *a,**kw:types.SimpleNamespace(returncode=0,stdout=raw,stderr=b'')
        self.assertEqual(FAKE,t.validate(FAKE,{},run=run,confirm=lambda:True,policy=fable.overage_policy)['value'])
    def test_f1_token_validate_actual_overage(self):
        import control_plane_fable as fable
        raw=self.token_events({'status':'allowed','isUsingOverage':True})
        run=lambda *a,**kw:types.SimpleNamespace(returncode=0,stdout=raw,stderr=b'')
        self.expect('OVERAGE_NOT_BLOCKED',t.validate,FAKE,{},run=run,confirm=lambda:True,policy=fable.overage_policy)
    def test_f1_token_validate_unknown_overage(self):
        import control_plane_fable as fable
        run=lambda *a,**kw:types.SimpleNamespace(returncode=0,stdout=self.token_events({}),stderr=b'')
        self.expect('OVERAGE_UNVERIFIED',t.validate,FAKE,{},run=run,confirm=lambda:True,policy=fable.overage_policy)
    def test_f1_original_stream_guard_stops_overage(self):
        import io,control_plane_fable as fable
        _,guard=fable.read_stream(io.BytesIO(self.token_events({'isUsingOverage':True})))
        self.assertEqual('OVERAGE_NOT_BLOCKED',guard['error_code'])
    def test_f2_wrapped_url_reconstructed(self):
        import pyte
        screen=pyte.HistoryScreen(40,10,history=100);stream=pyte.Stream(screen)
        url='https://claude.ai/oauth/authorize?client_id=fake&code_challenge=fake&state=fake'
        stream.feed('Open:\r\n'+url[:35]+'\r\n'+url[35:]+'\r\n\r\nPaste code here:')
        self.assertEqual(url,t.login_url(screen))
    def test_f2_headless_capture_code_input_no_token_output(self):
        import io
        url='https://claude.ai/oauth/authorize?client_id=fake&state=fake'
        raw=('Open:\r\n'+url+'\r\n\r\nPaste code here:\r\n'+FAKE).encode()
        stdin=io.BytesIO();proc=types.SimpleNamespace(stdout=io.BytesIO(raw),stdin=stdin,wait=lambda **kw:0,terminate=lambda:None,kill=lambda:None)
        class Process:
            def __enter__(self):return proc
            def __exit__(self,*a):return False
        output=[]
        with patch.object(t,'check_tmpfs'),patch.object(t.os,'lstat',wraps=os.lstat):
            # Fake only root identity for the private transcript parent in CI.
            original=Path.lstat
            def owner(path):
                s=original(path)
                if str(path).endswith('aiops-recover-token'):
                    return types.SimpleNamespace(st_mode=s.st_mode,st_uid=0)
                return s
            with patch.object(Path,'lstat',owner):
                t.capture({},self.root/'unused',popen=lambda *a,**kw:Process(),read_code=lambda _: 'fake-approval-code',emit=output.append)
        self.assertEqual(b'fake-approval-code\n',stdin.getvalue());self.assertEqual(url,output[0])
        self.assertNotIn(FAKE,' '.join(output));self.assertFalse(hasattr(t,'webbrowser'))
    def test_f3_normal_401_identifiers_validate(self):
        run=lambda *a,**k:types.SimpleNamespace(returncode=0,stdout=self.token_events(None,line=401,uuid='401a'),stderr=b'401 in diagnostic ID')
        self.assertEqual(FAKE,t.validate(FAKE,{},run=run,confirm=lambda:True)['value'])
    def test_f3_normal_401_audit_result_preserved(self):
        import io
        from contextlib import redirect_stdout
        self.restore();self.fake_wrapper();out=io.StringIO()
        with redirect_stdout(out):self.assertEqual(0,r.fable_main(self.audit_args()))
        self.assertIn('f401a',out.getvalue());self.assertIn('"line": 401',out.getvalue());self.assertNotIn('TOKEN_REISSUE_REQUIRED',out.getvalue())
    def test_f3_typed_401_only(self):
        self.assertTrue(r.authentication_error({'type':'result','is_error':True,'error':{'status_code':401}}))
        self.assertFalse(r.authentication_error({'type':'result','is_error':False,'status_code':401}))
        self.assertFalse(r.authentication_error({'status':'ERROR','reason':'file line 401'}))
    def test_f4_busy_is_not_started_and_retryable(self):
        self.restore();calls=self.fake_wrapper(event={'status':'BUSY'},attempted=False)
        self.assertEqual(0,r.fable_main(self.audit_args()));self.assertEqual(0,r.fable_main(self.audit_args()))
        self.assertEqual(2,len(calls));entry=next(iter(r.AuditGuard(self.cp,self.key,TOKEN,None).entries().values()))
        self.assertEqual('NOT_STARTED',entry['state'])
    def test_f4_head_moved_is_not_started(self):
        self.restore();calls=self.fake_wrapper(event={'status':'ERROR','reason':'HEAD_MOVED: fake'},attempted=False)
        self.assertEqual(1,r.fable_main(self.audit_args()));self.assertEqual(1,r.fable_main(self.audit_args()))
        self.assertEqual(2,len(calls))
    def test_f4_pre_model_failure_is_not_started(self):
        self.assertEqual('NOT_STARTED',r.execution_state({'status':'ERROR'},{'model_attempted':False,'failure':{'error_code':'PRE_MODEL_FAILED'}}))
    def test_f4_unknown_again_no_no_call(self):
        self.restore();guard=r.AuditGuard(self.cp,self.key,TOKEN,None);guard.before('BeautifulMind-JT/ZARI',36,'d'*40,[])
        calls=self.fake_wrapper()
        with patch('builtins.input',return_value='n'):
            self.assertEqual(0,r.fable_main(self.audit_args()+['--again']))
        self.assertEqual([],calls)
    def test_f4_unknown_again_y_one_call_and_forwarded(self):
        self.restore();guard=r.AuditGuard(self.cp,self.key,TOKEN,None);guard.before('BeautifulMind-JT/ZARI',36,'d'*40,[])
        calls=self.fake_wrapper()
        with patch('builtins.input',return_value='y'):
            self.assertEqual(0,r.fable_main(self.audit_args()+['--again']))
        self.assertEqual(1,len(calls));self.assertIn('--again',calls[0])
        self.assertEqual('RESULT',next(iter(guard.entries().values()))['state'])
    def test_f4_again_after_partial_local_mutation(self):
        self.restore();guard=r.AuditGuard(self.cp,self.key,TOKEN,None);guard.before('BeautifulMind-JT/ZARI',36,'d'*40,[])
        r.atomic(self.state/'partial',b'partial');calls=self.fake_wrapper()
        with patch('builtins.input',return_value='y'):
            self.assertEqual(0,r.fable_main(self.audit_args()+['--again']))
        self.assertEqual(1,len(calls));self.cp.current()
    def test_f4_result_fail_is_result(self):
        self.assertEqual('RESULT',r.execution_state({'result':'FAIL'},{'model_attempted':True}))
    def test_f4_model_attempted_without_result_unknown(self):
        self.assertEqual('UNKNOWN',r.execution_state({'status':'ERROR'},{'model_attempted':True}))
    def test_f6_empty_enroll_n_no_ledger(self):
        self.backend.h=None
        self.expect('ENROLLMENT_CANCELLED',r.prepare_enrollment,self.cp,input_fn=lambda _:'n')
        self.assertFalse(self.state.exists())
    def test_f6_empty_enroll_y_marker_and_create_only_publish(self):
        self.backend.h=None;r.prepare_enrollment(self.cp,input_fn=lambda _:'y')
        self.cp.save(self.key,TOKEN,enrollment=True,salt=self.salt)
        self.assertTrue((self.state/'initial-registration.json').exists());self.cp.current()
        self.expect('STATE_CAS_CONFLICT',r.prepare_enrollment,self.cp,input_fn=lambda _:self.fail('no confirmation'))
    def test_f6_existing_branch_fails_before_call(self):
        self.expect('STATE_CAS_CONFLICT',r.prepare_enrollment,self.cp,input_fn=lambda _:self.fail('no confirmation'))
    def test_f6_existing_ledger_no_empty_confirmation(self):
        self.backend.h=None;self.state.mkdir();r.atomic(self.state/'claim',b'old')
        r.prepare_enrollment(self.cp,input_fn=lambda _:self.fail('nonempty needs no prompt'))
        self.assertEqual(b'old',(self.state/'claim').read_bytes())
    def test_f6_missing_branch_restore_no_empty_fallback(self):
        self.backend.h=None;self.expect('STATE_CORRUPT',self.restore);self.assertFalse(self.state.exists())
    def test_f6_github_initial_branch_create_cas(self):
        requests=[];created=[False]
        def request(method,path,data):
            requests.append((method,path,data))
            if not path:return {'private':True,'default_branch':'main'}
            if method=='GET' and path=='/git/ref/heads/audit-host':
                if not created[0]:r.fail('STATE_BRANCH_MISSING')
                return {'object':{'sha':'b'*40}}
            if method=='GET':return {'object':{'sha':'a'*40}}
            if method=='POST' and path=='/git/refs':created[0]=True
            return {'sha':'b'*40}
        backend=r.GithubState('BeautifulMind-JT/aiops-state','audit-host','fake',request=request)
        backend.publish(None,{'chunks':[r.sha(b'cipher')]},b'cipher')
        self.assertFalse(any(x[0]=='PATCH' for x in requests));self.assertTrue(created[0])
        self.expect('STATE_CAS_CONFLICT',backend.publish,None,{},b'cipher')
    def test_f7_python313_and_cp313_manifest(self):
        root=Path(__file__).resolve().parents[2];m=json.loads((root/'engineering/recovery/manifest.json').read_text())
        self.assertEqual([3,13],m['python_version']);self.assertNotIn('uid',m['account'])
        members=[x['cache_path'] for x in m['files'] if x['cache_path'].startswith('wheels/cffi-')]
        self.assertTrue(all('cp313-cp313' in x for x in members))
        with patch.object(r.sys,'version_info',(3,13,5)):
            inst=self.install();inst.manifest['python_version']=[3,13];inst.install(lambda _:b'fixed')
    def test_f7_symbolic_owner_restores_current_ids(self):
        p=copy.deepcopy(self.payload);p['files'][0]['uid']='AUDITOR';p['files'][0]['gid']='AUDITOR'
        # Restore chown uses the new VM's identity, not the old 996 allocation.
        cp=r.Checkpoints(self.backend,self.state,self.binding,self.tokenfile,997,997,self.root)
        e,c=r.encrypt(p,self.key,self.salt,1,'b'*40);self.backend.data[self.backend.h]=(e,c)
        with patch.object(r,'inventory',return_value=p['files']),patch.object(r.os,'fchown') as owner:
            cp.restore('offline-fake-password')
        self.assertTrue(any(x.args[1:]==(997,997) for x in owner.call_args_list))
    def test_f7_existing_uid996_accepted(self):
        import grp
        cfg={'account':{'name':'aiops-auditor','home':'/var/lib/aiops-auditor'}}
        account=types.SimpleNamespace(pw_uid=996,pw_gid=996,pw_dir=cfg['account']['home'],pw_shell='/usr/sbin/nologin')
        real=Path.lstat
        def info(path):
            if str(path)==cfg['account']['home']:
                return types.SimpleNamespace(st_mode=0o40700,st_uid=996,st_gid=996)
            return real(path)
        with patch.object(r.pwd,'getpwnam',return_value=account),patch.object(grp,'getgrgid',return_value=types.SimpleNamespace(gr_name='aiops-auditor')), \
             patch.object(r.os,'getgrouplist',return_value=[996]),patch.object(r.os.path,'lexists',return_value=True),patch.object(Path,'lstat',info), \
             patch.object(r.subprocess,'run',return_value=types.SimpleNamespace(returncode=1,stdout=b'not allowed to run sudo',stderr=b'')):
            self.assertEqual(996,r.ensure_account(cfg).pw_uid)
    def test_f7_keyctl_missing_uses_tmpfs_fallback(self):
        with patch.object(r.subprocess,'run',side_effect=FileNotFoundError()),patch.object(r,'tmpfs_key',return_value=self.key) as fallback:
            self.assertEqual(self.key,r.keyring('get'));fallback.assert_called_once_with('get',None)
    def test_f7_tmpfs_key_roundtrip_no_password(self):
        parent=self.root/'tmpfs';parent.mkdir();directory=parent/'aiops-recover-key'
        original=Path.lstat;original_read=Path.read_text
        def info(path):
            stat=original(path)
            if path==directory:return types.SimpleNamespace(st_mode=stat.st_mode,st_uid=0)
            return stat
        def text(path,*a,**kw):
            if str(path)=='/proc/self/mountinfo':return '1 0 0:1 / '+str(parent)+' rw - tmpfs tmpfs rw\n'
            return original_read(path,*a,**kw)
        with patch.object(r,'KEY_DIR',directory),patch.object(Path,'lstat',info),patch.object(Path,'read_text',text):
            r.tmpfs_key('set',self.key);self.assertEqual(self.key,r.tmpfs_key('get'))
        self.assertEqual(0o600,(directory/'derived-key').stat().st_mode&0o777)

    def test_f1_reissue_actual_validation_without_billing_mock(self):
        import control_plane_fable as fable
        path=self.root/'aiops-recover-token';path.mkdir(mode=0o700);r.atomic(path/'typescript',FAKE.encode())
        inst=types.SimpleNamespace(verify=lambda:None,manifest={'files':[
            {'destination':'/opt/aiops/lib/fable/control_plane_fable.py','sha256':'fake'}]})
        raw=self.token_events({'status':'allowed','isUsingOverage':False})
        with patch.object(t,'TRANSCRIPT',self.root/'unused'),patch.object(t,'prerequisites'),patch.object(t,'read_regular',r.read_regular), \
             patch.object(t,'pinned_module',return_value=fable),patch.object(t,'confirm_model',return_value=True):
            # validate's default confirmation/run are injected explicitly to preserve its real policy/body.
            original=t.validate
            def validate(token,cfg,**kw):
                return original(token,cfg,**kw,confirm=lambda:True,run=lambda *a,**k:types.SimpleNamespace(returncode=0,stdout=raw,stderr=b''))
            with patch.object(t,'validate',side_effect=validate):
                self.assertEqual(FAKE,t.reissue({},inst)['value'])
    def enrollment_existing(self, value, events, rc=0):
        import control_plane_fable as fable
        path=self.root/'existing-token';r.atomic(path,value.encode())
        os.utime(path,(100,100))
        inst=types.SimpleNamespace(verify=lambda:None,manifest={'files':[
            {'destination':'/opt/aiops/lib/fable/control_plane_fable.py','sha256':'fake'}]})
        original_stat=os.fstat
        def root_stat(fd):
            info=original_stat(fd)
            return types.SimpleNamespace(st_mode=info.st_mode,st_uid=0,st_nlink=info.st_nlink,st_mtime=info.st_mtime)
        original_validate=t.validate
        def validate(token,cfg,**kw):
            return original_validate(token,cfg,**kw,confirm=lambda:True,
                run=lambda *a,**k:types.SimpleNamespace(returncode=rc,stdout=events,stderr=b''))
        with patch.object(t,'protected_parent'),patch.object(t.os,'fstat',side_effect=root_stat), \
             patch.object(t,'pinned_module',return_value=fable),patch.object(t,'validate',side_effect=validate), \
             patch.object(t,'reissue',return_value=dict(TOKEN)) as reissue,patch.object(t,'capture') as capture:
            if rc == 1:
                reissue.side_effect=lambda *a: self.fail('401 must not reissue')
                capture.side_effect=lambda *a: self.fail('401 must not open login')
            result=t.enrollment_token({},inst,path)
            capture.assert_not_called()
            return result,reissue.call_count
    def test_enroll_existing_token_reused_without_login(self):
        result,calls=self.enrollment_existing(FAKE,self.token_events({'status':'allowed','isUsingOverage':False}))
        self.assertEqual(0,calls);self.assertEqual(FAKE,result['value'])
        self.assertEqual(100,result['issued_at']);self.assertEqual(100+365*86400,result['expires_at'])
        self.backend.h=None
        r.prepare_enrollment(self.cp,input_fn=lambda _:'y')
        self.cp.save(self.key,result,enrollment=True,salt=self.salt)
        self.assertEqual(result,self.cp.restore('offline-fake-password')[0]['claude_token'])
    def test_enroll_invalid_token_uses_new_issuance(self):
        result,calls=self.enrollment_existing('invalid',b'')
        self.assertEqual(1,calls);self.assertEqual(TOKEN,result)
    def test_enroll_existing_token_401_stops_without_reissue(self):
        events=json.dumps({'type':'result','is_error':True,'status_code':401}).encode()
        self.expect('TOKEN_REISSUE_REQUIRED',self.enrollment_existing,FAKE,events,1)

    def test_f7_exact_platform_check(self):
        import platform
        manifest={'python_version':[3,13],'platform':'debian-13-x86_64-glibc-2.41'}
        with patch.object(r.sys,'version_info',(3,13,5)),patch.object(platform,'freedesktop_os_release',return_value={'ID':'debian','VERSION_ID':'13'}), \
             patch.object(platform,'machine',return_value='x86_64'),patch.object(platform,'libc_ver',return_value=('glibc','2.41')):
            r.check_platform(manifest)
        with patch.object(r.sys,'version_info',(3,12,14)):
            self.expect('PYTHON_VERSION_DRIFT',r.check_platform,manifest)
    def test_f7_no_extra_group_or_sudo_allowed(self):
        import grp
        cfg={'account':{'name':'aiops-auditor','home':'/var/lib/aiops-auditor'}}
        account=types.SimpleNamespace(pw_uid=996,pw_gid=996,pw_dir=cfg['account']['home'],pw_shell='/usr/sbin/nologin')
        with patch.object(r.pwd,'getpwnam',return_value=account),patch.object(grp,'getgrgid',return_value=types.SimpleNamespace(gr_name='aiops-auditor')), \
             patch.object(r.os,'getgrouplist',return_value=[996,27]):
            self.expect('INSTALLATION_DRIFT',r.ensure_account,cfg)
        with patch.object(r.pwd,'getpwnam',return_value=account),patch.object(grp,'getgrgid',return_value=types.SimpleNamespace(gr_name='aiops-auditor')), \
             patch.object(r.os,'getgrouplist',return_value=[996]),patch.object(Path,'exists',return_value=True), \
             patch.object(r.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=b'(ALL) ALL',stderr=b'')):
            self.expect('AUDITOR_SUDO_FORBIDDEN',r.ensure_account,cfg)
    def bootstrap_script(self):
        root=Path(__file__).resolve().parents[2]
        doc=(root/'engineering/recovery/BOOTSTRAP_KO.md').read_text()
        return doc.split("<<'AIOPS_BOOTSTRAP_PY'\n",1)[1].split('\nAIOPS_BOOTSTRAP_PY',1)[0]
    def test_f5_bootstrap_pins_and_masked_input(self):
        import ast
        root=Path(__file__).resolve().parents[2];doc=(root/'engineering/recovery/BOOTSTRAP_KO.md').read_text()
        node=ast.parse(self.bootstrap_script())
        pins=next(ast.literal_eval(x.value) for x in node.body if isinstance(x,ast.Assign) and any(isinstance(y,ast.Name) and y.id=='PINNED_SHA256' for y in x.targets))
        self.assertEqual(8,len(pins))
        for name,digest in pins.items():self.assertEqual(digest,r.sha((root/name).read_bytes()))
        self.assertIn("os.dup2(terminal, 0)",doc);self.assertIn('read -rs',doc);self.assertIn('keyutils',doc);self.assertIn('__MERGED_SOURCE_COMMIT_40HEX__',doc)
        self.assertNotIn('enrollment_commit',self.bootstrap_script())
    def run_bootstrap_fake(self, bad_hash=False, missing=False):
        import ast,io,urllib.error
        from contextlib import redirect_stdout
        root=Path(__file__).resolve().parents[2];node=ast.parse(self.bootstrap_script())
        fixture=self.root/'bootstrap'
        class Rewrite(ast.NodeTransformer):
            def visit_Constant(_,n):
                if isinstance(n.value,str) and n.value.startswith(('/opt/aiops/','/usr/local/bin/','/etc/aiops/','/var/cache/aiops-recover')):
                    return ast.copy_location(ast.Constant(str(fixture)+n.value),n)
                return n
        node=ast.fix_missing_locations(Rewrite().visit(node))
        calls=[]
        class Response:
            def __init__(self,data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def read(self,*a):return r.canonical(self.data)
        def request(req,**kw):
            calls.append(req.full_url)
            if '/contents/' in req.full_url:
                name=req.full_url.split('/contents/',1)[1].split('?ref=',1)[0]
                raw=(root/name).read_bytes()
                if bad_hash:raw+=b'bad'
                return Response({'encoding':'base64','content':base64.b64encode(raw).decode()})
            if '/git/ref/' in req.full_url:
                if missing:raise urllib.error.HTTPError(req.full_url,404,'missing',None,None)
                return Response({'object':{'sha':'a'*40}})
            return Response({'private':True})
        class Executed(BaseException):
            def __init__(self,args):self.args_passed=args
        real_lstat=os.lstat;real_fstat=os.fstat;real_open=os.open
        def fake_open(path,*a,**kw):
            return real_open(os.devnull,os.O_RDWR) if str(path)=='/dev/tty' else real_open(path,*a,**kw)
        def owner(s):
            vals=list(s);vals[4]=0
            # Trusted fake ancestors of the temporary filesystem only.
            vals[0]&=~0o022
            return os.stat_result(vals)
        out=io.StringIO()
        with patch.dict(os.environ,{'AIOPS_BOOTSTRAP_COMMIT':'a'*40,'GH_TOKEN':'fake-only'}),patch.object(r.os,'geteuid',return_value=0), \
             patch.object(r.os,'lstat',side_effect=lambda *a,**kw:owner(real_lstat(*a,**kw))), \
             patch.object(r.os,'fstat',side_effect=lambda fd:owner(real_fstat(fd))), \
             patch.object(r.urllib.request,'build_opener',return_value=types.SimpleNamespace(open=request)), \
             patch.object(r.os,'open',side_effect=fake_open),patch.object(r.os,'dup2'), \
             patch.object(r.os,'execv',side_effect=lambda path,args:(_ for _ in ()).throw(Executed(args))),redirect_stdout(out):
            try:exec(compile(node,'offline-bootstrap','exec'),{})
            except Executed as result:return result.args_passed,out.getvalue(),fixture,calls
            except SystemExit:return None,out.getvalue(),fixture,calls
        self.fail('bootstrap must exec or stop')
    def test_f5_bootstrap_hash_failure_no_install(self):
        args,output,fixture,calls=self.run_bootstrap_fake(bad_hash=True)
        self.assertIsNone(args);self.assertIn('BOOTSTRAP_HASH_MISMATCH',output);self.assertFalse(fixture.exists())
        self.assertNotIn('fake-only',output)
    def test_f5_bootstrap_restore_and_first_enroll_paths(self):
        args,output,fixture,calls=self.run_bootstrap_fake()
        self.assertEqual(1,len(args));self.assertEqual(8,sum('/contents/' in x for x in calls));self.assertEqual('',output)
        # A separate fresh fake VM, never a fallback from failed restore.
        import shutil;shutil.rmtree(fixture)
        args,output,fixture,calls=self.run_bootstrap_fake(missing=True)
        self.assertEqual('--enroll',args[1]);self.assertEqual('',output)

    def test_f1_audit_and_preflight_actual_overage_stopped(self):
        self.restore();calls=self.fake_wrapper(stream=self.token_events({'isUsingOverage':True}))
        self.assertEqual(1,r.fable_main(self.audit_args()))
        with patch.object(r,'confirm_model',return_value=True):
            self.assertEqual(1,r.fable_main(['preflight']))
        self.assertEqual(2,len(calls))
    def test_f4_structured_401_error_diagnostic_keeps_original(self):
        import io
        from contextlib import redirect_stdout
        self.restore();self.fake_wrapper(event={'status':'ERROR','reason':'MODEL_EXECUTION_FAILED: HTTP 401'},failure={'api_error_status':401})
        output=io.StringIO()
        with redirect_stdout(output):self.assertEqual(1,r.fable_main(self.audit_args()))
        self.assertIn('MODEL_EXECUTION_FAILED: HTTP 401',output.getvalue());self.assertIn('TOKEN_REISSUE_REQUIRED',output.getvalue())

    def test_f5_bootstrap_restrictive_umask_keeps_auditor_traversal(self):
        previous=os.umask(0o077)
        try:
            inst=self.install()
            self.assertEqual(0o755,inst.path('/opt/aiops/bin').stat().st_mode&0o777)
            self.backend.h=None;r.prepare_enrollment(self.cp,input_fn=lambda _:'y')
            self.assertEqual(0o750,self.state.stat().st_mode&0o777)
            self.assertEqual(0o755,(self.state/'runs').stat().st_mode&0o777)
            args,output,fixture,calls=self.run_bootstrap_fake()
            self.assertEqual('',output)
            self.assertEqual(0o755,(fixture/'opt/aiops/bin').stat().st_mode&0o777)
        finally:os.umask(previous)

    # Re-audit fixes (manual Fable A3 at 43ac23f, F1-F5 and N2).
    def capture_with(self, raw, rc=0, approval='fake-approval-code'):
        import io
        proc=types.SimpleNamespace(stdout=io.BytesIO(raw),stdin=io.BytesIO(),wait=lambda **kw:rc,terminate=lambda:None,kill=lambda:None)
        class Process:
            def __enter__(self):return proc
            def __exit__(self,*a):return False
        output=[];original=Path.lstat
        def owner(path):
            s=original(path)
            if str(path).endswith('aiops-recover-token'):
                return types.SimpleNamespace(st_mode=s.st_mode,st_uid=0)
            return s
        with patch.object(t,'check_tmpfs'),patch.object(Path,'lstat',owner):
            path=t.capture({},self.root/'unused',popen=lambda *a,**kw:Process(),read_code=lambda _:approval,emit=output.append)
        return path,output
    def login_screen(self):
        url='https://claude.ai/oauth/authorize?client_id=fake&state=fake'
        return url,('Open:\r\n'+url+'\r\n\r\nPaste code here:\r\n').encode()
    def test_f2r_failed_issuance_set_aside_then_fresh_url(self):
        url,raw=self.login_screen();folder=self.root/'aiops-recover-token'
        self.expect('TOKEN_ISSUANCE_FAILED_RETRY',self.capture_with,raw,rc=1)
        self.assertFalse((folder/'typescript').exists());self.assertTrue((folder/'failed-0.typescript').exists())
        path,output=self.capture_with(raw+FAKE.encode())
        self.assertEqual(url,output[0]);self.assertEqual(folder/'typescript',path)
    def test_f2r_invalid_code_set_aside(self):
        _,raw=self.login_screen();folder=self.root/'aiops-recover-token'
        self.expect('LOGIN_CODE_INVALID_RETRY',self.capture_with,raw,approval='')
        self.assertFalse((folder/'typescript').exists());self.assertTrue((folder/'failed-0.typescript').exists())
    def test_f2r_no_prompt_set_aside(self):
        folder=self.root/'aiops-recover-token'
        self.expect('LOGIN_PROMPT_NOT_SHOWN_RETRY',self.capture_with,b'unexpected screen\r\n')
        self.assertFalse((folder/'typescript').exists())
    def test_f2r_extraction_failure_never_replayed_again(self):
        folder=self.root/'aiops-recover-token';folder.mkdir(mode=0o700)
        r.atomic(folder/'typescript',b'no credential on this screen')
        inst=types.SimpleNamespace(verify=lambda:None,manifest={'files':[
            {'destination':'/opt/aiops/lib/fable/control_plane_fable.py','sha256':'fake'}]})
        with patch.object(t,'TRANSCRIPT',self.root/'unused'),patch.object(t,'prerequisites'), \
             patch.object(t,'read_regular',r.read_regular),patch.object(t,'capture') as capture:
            self.expect('TOKEN_EXTRACTION_FAILED_RETRY',t.reissue,{},inst)
            capture.assert_not_called()
            self.assertTrue((folder/'failed-0.typescript').exists());self.assertFalse((folder/'typescript').exists())
            capture.side_effect=r.RecoveryError('FRESH_LOGIN_STARTED')
            self.expect('FRESH_LOGIN_STARTED',t.reissue,{},inst)
            capture.assert_called_once()
    def test_f2r_cleanup_removes_failed_records(self):
        folder=self.root/'aiops-recover-token';folder.mkdir(mode=0o700)
        r.atomic(folder/'failed-0.typescript',b'x');r.atomic(folder/'typescript',FAKE.encode())
        with patch.object(t,'TRANSCRIPT',self.root/'unused'),patch.object(t,'read_regular',r.read_regular):
            t.cleanup({})
        self.assertEqual([],list(folder.iterdir()))
    def run_artifacts(self):
        run=self.state/'runs'/'20261002T000000Z-abcdef12';(run/'work'/'audit').mkdir(parents=True)
        r.atomic(run/'run.json',b'{"result":"PASS"}');r.atomic(run/'comment.md',b'body')
        r.atomic(run/'claude-output.jsonl',b'x'*(3<<20));r.atomic(run/'claude-stderr.txt',b'stderr')
        r.atomic(run/'work'/'audit'/'diff.patch',b'y'*(2<<20))
        (run/'work'/'link').symlink_to('/etc/passwd')
        return run
    def test_f3r_large_run_artifacts_excluded(self):
        self.restore();run=self.run_artifacts()
        names={x['path'] for x in r.inventory(self.state,self.uid,self.gid)}
        prefix=str(run.relative_to(self.state))
        self.assertIn(prefix+'/run.json',names);self.assertIn(prefix+'/comment.md',names)
        for name in ('claude-output.jsonl','claude-stderr.txt','work','work/audit/diff.patch','work/link'):
            self.assertNotIn(prefix+'/'+name,names)
        h=self.cp.save(self.key,TOKEN);envelope,cipher=self.backend.data[h]
        self.assertLess(len(cipher),1<<20)
    def test_f3r_restore_after_exclusion_matches(self):
        self.restore();self.run_artifacts();self.cp.save(self.key,TOKEN)
        import shutil;shutil.rmtree(self.state);self.binding.unlink();self.tokenfile.unlink()
        self.restore();self.cp.current()
        self.assertTrue(any(p.name=='run.json' for p in self.state.rglob('*')))
        self.assertFalse(any(p.name=='claude-output.jsonl' for p in self.state.rglob('*')))
    def test_f3r_payload_with_excluded_artifact_rejected(self):
        payload=copy.deepcopy(self.payload)
        payload['files']=[{'path':'runs','kind':'dir','uid':0,'gid':0,'mode':0o755},
                          {'path':'runs/r','kind':'dir','uid':0,'gid':0,'mode':0o755},
                          {'path':'runs/r/claude-output.jsonl','kind':'file','uid':0,'gid':0,'mode':0o644,
                           'bytes':base64.b64encode(b'x').decode(),'sha256':r.sha(b'x'),'size':1}]
        self.expect('STATE_CORRUPT',r.validate_payload,payload,self.uid,self.gid)
    def test_f3r_too_large_names_largest_file(self):
        self.restore();r.atomic(self.state/'big',b'z'*4096)
        with patch.object(r,'MAX_STATE',3000):
            with self.assertRaises(r.RecoveryError) as ctx:
                r.inventory(self.state,self.uid,self.gid)
        self.assertEqual('STATE_TOO_LARGE',ctx.exception.code);self.assertIn('largest=big',ctx.exception.detail)
        self.assertEqual('STATE_TOO_LARGE',r.hold(ctx.exception)['reason'])
    def flaky_publish(self,failures=1,code='STATE_AUTHORITY_UNAVAILABLE'):
        original=self.backend.publish;left=[failures]
        def publish(*a):
            if left[0]:
                left[0]-=1;r.fail(code)
            return original(*a)
        self.backend.publish=publish
    def test_f4r_result_publish_failure_republished_without_model(self):
        self.restore();guard=r.AuditGuard(self.cp,self.key,TOKEN,None)
        guard.before('BeautifulMind-JT/ZARI',36,'d'*40,[]);r.atomic(self.state/'run-evidence',b'sealed')
        self.flaky_publish()
        self.expect('STATE_AUTHORITY_UNAVAILABLE',guard.after,'BeautifulMind-JT/ZARI',36,'d'*40,{'result':'PASS'},{'model_attempted':True})
        self.assertTrue(self.cp.pending.exists())
        self.expect('STATE_PUBLISH_PENDING',self.cp.current)
        self.cp.current(key=self.key)
        self.assertFalse(self.cp.pending.exists());self.cp.current()
        self.assertEqual({'status':'AUDIT_EXISTS','result':'PASS','head':'d'*40},guard.inspect('BeautifulMind-JT/ZARI',36,'d'*40,[]))
    def test_f4r_wrapper_shows_result_and_next_command_finishes(self):
        import io
        from contextlib import redirect_stdout
        self.restore();calls=self.fake_wrapper();original=self.backend.publish;count=[0]
        def publish(*a):
            count[0]+=1
            if count[0]==2:r.fail('STATE_AUTHORITY_UNAVAILABLE')
            return original(*a)
        self.backend.publish=publish;out=io.StringIO()
        with redirect_stdout(out):self.assertEqual(1,r.fable_main(self.audit_args()))
        self.assertIn('"result": "PASS"',out.getvalue());self.assertIn('STATE_AUTHORITY_UNAVAILABLE',out.getvalue())
        out=io.StringIO()
        with redirect_stdout(out):self.assertEqual(0,r.fable_main(self.audit_args()))
        self.assertEqual(1,len(calls));self.assertIn('AUDIT_EXISTS',out.getvalue());self.cp.current()
    def test_f4r_lost_ack_acknowledged_only_for_own_child(self):
        self.restore()
        with patch.object(self.cp,'bind',side_effect=r.RecoveryError('ACK_LOST')):
            self.expect('ACK_LOST',self.cp.save,self.key,TOKEN)
        self.expect('STATE_ROLLBACK_DETECTED',self.cp.current)
        local=self.cp.current(key=self.key)
        self.assertEqual(self.backend.h,local['commit']);self.assertFalse(self.cp.pending.exists())
    def test_f4r_foreign_child_still_rollback(self):
        self.restore();self.cp.mark_pending()
        other=copy.deepcopy(self.payload);other['files'][0]['bytes']=base64.b64encode(b'foreign').decode()
        other['files'][0]['sha256']=r.sha(b'foreign');other['files'][0]['size']=7
        child=format(int(self.backend.h,16)+1,'040x')
        self.backend.data[child]=r.encrypt(other,self.key,self.salt,2,self.backend.h);self.backend.h=child
        self.expect('STATE_ROLLBACK_DETECTED',self.cp.current,key=self.key)
    def test_f4r_unmarked_local_change_still_corrupt(self):
        self.restore();(self.state/'claim').write_bytes(b'changed')
        self.expect('STATE_CORRUPT',self.cp.current,key=self.key)
    def test_f4r_stale_marker_is_spent(self):
        self.restore();self.cp.mark_pending();self.cp.save(self.key,TOKEN)
        r.atomic(self.cp.pending,r.canonical({'format':'AUDIT_HOST_PENDING_V1','base':'e'*40}))
        self.assertIsNone(self.cp.read_pending());self.assertFalse(self.cp.pending.exists())
    def test_f4r_enrollment_bind_lost_resumed_by_restore(self):
        self.backend.h=None;r.prepare_enrollment(self.cp,input_fn=lambda _:'y')
        with patch.object(self.cp,'bind',side_effect=r.RecoveryError('ACK_LOST')):
            self.expect('ACK_LOST',self.cp.save,self.key,TOKEN,enrollment=True,salt=self.salt)
        self.assertFalse(self.binding.exists());self.assertIsNotNone(self.backend.h)
        self.expect('STATE_CAS_CONFLICT',r.prepare_enrollment,self.cp,input_fn=lambda _:self.fail('no prompt'))
        payload,_=self.cp.restore('offline-fake-password')
        self.cp.current();self.assertEqual(FAKE,self.tokenfile.read_text());self.assertFalse(self.cp.pending.exists())
    def test_f5r_password_confirmation(self):
        answers=iter(['secret-one','secret-two'])
        self.expect('STATE_PASSWORD_MISMATCH',r.new_state_password,prompt=lambda _:next(answers))
        answers=iter(['same-secret','same-secret'])
        self.assertEqual('same-secret',r.new_state_password(prompt=lambda _:next(answers)))
    def test_f5r_enrollment_read_back(self):
        self.backend.h=None;r.prepare_enrollment(self.cp,input_fn=lambda _:'y')
        self.cp.save(self.key,TOKEN,enrollment=True,salt=self.salt);r.verify_enrollment(self.cp,self.key)
        self.expect('STATE_PASSWORD_OR_INTEGRITY_ERROR',r.verify_enrollment,self.cp,r.derive('typo-password',self.salt))
    def test_n2_consult_and_preflight_success_are_results(self):
        self.assertEqual('RESULT',r.execution_state({'kind':'consult','result':'ANSWERED'},{'model_attempted':True}))
        self.assertEqual('RESULT',r.execution_state({'kind':'consult','result':'USER_REQUIRED'},{'model_attempted':True}))
        self.assertEqual('RESULT',r.execution_state({'status':'PASS','run':'20261002T000000Z-abcdef12'},{'model_attempted':True}))
        self.assertEqual('UNKNOWN',r.execution_state({'status':'PASS'},{'model_attempted':True}))
    def test_n2_consult_again_y_only(self):
        self.restore();calls=self.fake_wrapper(event={'kind':'consult','result':'ANSWERED'})
        consult=['consult','--repository','BeautifulMind-JT/ZARI','--issue','36','--comment','1']
        self.assertEqual(0,r.fable_main(consult));self.assertEqual(0,r.fable_main(consult));self.assertEqual(1,len(calls))
        with patch('builtins.input',return_value='n'):
            self.assertEqual(0,r.fable_main(consult+['--again']))
        self.assertEqual(1,len(calls))
        with patch('builtins.input',return_value='y'):
            self.assertEqual(0,r.fable_main(consult+['--again']))
        self.assertEqual(2,len(calls));self.assertIn('--again',calls[1])
    def test_n2_preflight_again_rejected(self):
        self.assertEqual(1,r.fable_main(['preflight','--again']))
    def test_f1r_bootstrap_normalizes_usr_local(self):
        text=(Path(__file__).resolve().parents[1]/'recovery'/'BOOTSTRAP_KO.md').read_text()
        shell=text.split("<<'AIOPS_BOOTSTRAP_PY'")[0]
        self.assertLess(shell.index('apt-get install'),shell.index('chown root:root /usr/local /usr/local/bin'))
        self.assertLess(shell.index('chmod 0755 /usr/local /usr/local/bin'),shell.index("read -rs -p 'GH_TOKEN: '"))


if __name__ == '__main__':
    unittest.main()
