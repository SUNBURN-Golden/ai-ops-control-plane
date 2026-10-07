"""Real directory moves and SQLite snapshots; narrow recovery cannot bypass gates."""
from pathlib import Path
from types import SimpleNamespace
import copy
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac_app'))
import mac_empty_attempt_recovery as recovery
from common import AppError, atomic_json


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)/'state';self.state.mkdir(mode=0o700)
        self.root=self.state/'jobs'/recovery.JOB;self.root.mkdir(parents=True,mode=0o700)
        self.original=self.root/recovery.ATTEMPT;self.original.mkdir(mode=0o700)
        self.archive=self.state/'recovery'/('empty-attempt-'+recovery.ATTEMPT)
        self.target=self.archive/'attempt'
        self.proof={'databases':{'app':'unchanged'},'receipts':{'original':'unchanged'},'job_sha256':'unchanged'}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(recovery,'approval').start()
        mock.patch.object(recovery.mac_glm_audit,'require_idle').start()
        mock.patch.object(recovery,'context',return_value=self.proof).start()
        self.guard=mock.patch.object(recovery.install,'idle_database').start()
        self.raw_directory=recovery.directory
        def info(path):
            st=self.raw_directory(path)
            values={name:getattr(st,name) for name in ('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_mtime_ns')}
            values.update(st_birthtime=recovery.BIRTH,st_mtime=recovery.BIRTH)
            return SimpleNamespace(**values)
        mock.patch.object(recovery,'directory',side_effect=info).start()

    def call(self,op='quarantine'):return recovery.recover_locked(self.state,op)

    def test_inspect_has_no_quarantine_mutation(self):
        self.assertEqual(self.call('inspect')['result'],'ELIGIBLE')
        self.assertTrue(self.original.exists());self.assertFalse(self.archive.exists());self.guard.assert_not_called()

    def test_atomic_move_preserves_inode_and_evidence_and_normal_guard(self):
        before=self.original.stat();result=self.call()
        self.assertFalse(self.original.exists());self.assertEqual(self.target.stat().st_ino,before.st_ino)
        evidence=json.loads((self.archive/'evidence.json').read_text())
        self.assertEqual(evidence['original_path'],str(self.original));self.assertEqual(evidence['context'],self.proof)
        self.assertEqual(evidence['state'],'QUARANTINED');self.assertEqual(result['installer_guard'],'PASS')
        self.guard.assert_called_once_with(self.state)
        self.assertEqual(self.call()['result'],'QUARANTINED')
        self.assertEqual(self.target.stat().st_ino,before.st_ino)

    def test_restore_is_explicit_reversible_and_preserves_original_inode(self):
        inode=self.original.stat().st_ino;self.call();self.assertEqual(self.call('restore')['result'],'RESTORED')
        self.assertEqual(self.original.stat().st_ino,inode);self.assertFalse(self.target.exists())
        with self.assertRaises(AppError):self.call()

    def test_normal_installer_refusal_rolls_back_without_hiding_failure(self):
        inode=self.original.stat().st_ino;self.guard.side_effect=AppError('UPDATE_BUSY')
        with self.assertRaisesRegex(AppError,'UPDATE_BUSY'):self.call()
        self.assertEqual(self.original.stat().st_ino,inode);self.assertFalse(self.target.exists())
        self.assertEqual(json.loads((self.archive/'evidence.json').read_text())['state'],'PREPARED')
        self.guard.side_effect=None;self.assertEqual(self.call()['result'],'QUARANTINED')

    def test_nonempty_or_symlink_target_refuses_before_evidence(self):
        (self.original/'request.json').write_text('new execution')
        with self.assertRaises(AppError):self.call()
        self.assertFalse(self.archive.exists());(self.original/'request.json').unlink()
        self.original.rmdir();self.original.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(AppError):self.call()
        self.assertFalse(self.archive.exists())

    def test_crash_after_move_is_read_recovered_without_second_move(self):
        real= recovery.atomic_json
        def crash(path,value):
            if value['state']=='QUARANTINED':raise OSError('simulated crash after rename')
            return real(path,value)
        with mock.patch.object(recovery,'atomic_json',side_effect=crash):
            with self.assertRaises(OSError):self.call()
        self.assertTrue(self.target.exists());self.assertFalse(self.original.exists())
        self.assertEqual(self.call()['result'],'QUARANTINED')

    def prepared_after_move(self):
        real=recovery.atomic_json
        def crash(path,value):
            if value['state']=='QUARANTINED':raise OSError('crash before commit')
            return real(path,value)
        with mock.patch.object(recovery,'atomic_json',side_effect=crash):
            with self.assertRaises(OSError):self.call()

    def test_recovered_pending_move_guard_failure_restores_original(self):
        self.prepared_after_move();self.guard.side_effect=AppError('UPDATE_BUSY')
        with self.assertRaisesRegex(AppError,'UPDATE_BUSY'):self.call()
        self.assertTrue(self.original.exists());self.assertFalse(self.target.exists())
        self.assertEqual(json.loads((self.archive/'evidence.json').read_text())['state'],'PREPARED')

    def test_explicit_restore_recovers_prepared_after_move(self):
        self.prepared_after_move()
        self.assertEqual(self.call('restore')['result'],'RESTORED')
        self.assertTrue(self.original.exists());self.assertFalse(self.target.exists())

    def test_changed_ledger_or_receipt_blocks_retry_and_restore(self):
        self.call();new=copy.deepcopy(self.proof);new['job_sha256']='changed'
        with mock.patch.object(recovery,'context',return_value=new):
            for op in ('quarantine','restore'):
                with self.assertRaises(AppError):self.call(op)
        self.assertTrue(self.target.exists())

    def test_new_data_in_quarantine_blocks_all_operations(self):
        self.call();(self.target/'unknown').write_text('preserve')
        for op in ('inspect','quarantine','restore'):
            with self.assertRaises(AppError):self.call(op)
        self.assertTrue((self.target/'unknown').exists())

    def test_changed_context_during_move_rolls_back(self):
        with mock.patch.object(recovery,'context',side_effect=[self.proof,{'changed':True}]):
            with self.assertRaises(AppError):self.call()
        self.assertTrue(self.original.exists());self.assertFalse(self.target.exists())

    def test_unknown_archive_or_both_paths_refuse(self):
        self.archive.parent.mkdir(mode=0o700);self.archive.mkdir(mode=0o700)
        with self.assertRaises(OSError):self.call()
        self.archive.rmdir();self.call();self.original.mkdir(mode=0o700)
        with self.assertRaises(AppError):self.call()
        self.assertTrue(self.original.exists());self.assertTrue(self.target.exists())

    def test_atomic_move_never_overwrites_existing_destination(self):
        other=self.root/'existing';other.mkdir(mode=0o700)
        before=other.stat().st_ino
        with self.assertRaises(FileExistsError):recovery.move_exclusive(self.original,other)
        self.assertEqual(other.stat().st_ino,before);self.assertTrue(self.original.exists())

    def test_restore_never_replaces_a_broken_symlink(self):
        self.call();self.original.symlink_to(self.root/'missing')
        with self.assertRaises(AppError):self.call('restore')
        self.assertTrue(self.original.is_symlink());self.assertTrue(self.target.exists())

    def test_unapproved_decision_refuses_before_move(self):
        with mock.patch.object(recovery,'approval',side_effect=AppError('NO_APPROVAL')):
            with self.assertRaises(AppError):self.call()
        self.assertTrue(self.original.exists());self.assertFalse(self.archive.exists())

    def test_snapshot_checks_entire_sqlite_content_and_is_read_only(self):
        for name in ('app.sqlite3','mac-astra.sqlite3'):
            p=self.state/name
            with sqlite3.connect(p) as db:
                db.execute('CREATE TABLE evidence(k TEXT, value BLOB)');db.execute('INSERT INTO evidence VALUES (?,?)',('x',b'unchanged'))
            p.chmod(0o600)
        before={p.name:p.read_bytes() for p in self.state.glob('*.sqlite3')}
        result=recovery.database_snapshot(self.state);self.assertEqual(len(result),2)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.state.glob('*.sqlite3')})
        with sqlite3.connect(self.state/'mac-astra.sqlite3') as db:
            db.execute('INSERT INTO evidence VALUES (?,?)',('hidden',recovery.ATTEMPT))
        with self.assertRaises(AppError):recovery.database_snapshot(self.state)

    def test_global_installer_lock_refuses_before_recovery(self):
        home=Path(self.tmp.name)/'home';home.mkdir(mode=0o700)
        state=home/'Library/Application Support/AIOPS';state.mkdir(parents=True,mode=0o700)
        installer=home/'Library/Application Support/AIOPS Installer';installer.mkdir(mode=0o700)
        lock=installer/'installer.lock';lock.write_text('');lock.chmod(0o600)
        fd=os.open(lock,os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            with mock.patch.object(recovery.sys,'platform','darwin'),mock.patch.object(recovery.Path,'home',return_value=home),mock.patch.object(recovery,'recover_locked') as apply:
                with self.assertRaisesRegex(AppError,'INSTALLER_ALREADY_RUNNING'):recovery.command('quarantine')
                apply.assert_not_called()
        finally:os.close(fd)



class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)/'state';self.state.mkdir(mode=0o700)
        self.job={'id':recovery.JOB,'head':recovery.HEAD,'state':'needs_user','phase':'verifying','attempt':None,'calls':3,'native_lineage':{'request_id':'a'*32,'attempt_id':'b'*32}}
        self.folders=[self.state/'native'/('a'*32)/('b'*32), self.state/'jobs'/recovery.JOB/'f10a3b1d95804b94b8eb8390fe98e290',self.state/'jobs'/recovery.JOB/('c'*32)]
        for f in self.folders:
            f.mkdir(parents=True,mode=0o700)
            for p in [f,*f.parents]:
                if p==self.state.parent:break
                p.chmod(0o700)
            atomic_json(f/'request.json',{'attempt_id':f.name,'host_directory':str(self.state),'binding':'binding'})
            atomic_json(f/'receipt.json',{'test':'already verified private receipt'})
        self.source_receipt=hashlib.sha256((self.folders[1]/'receipt.json').read_bytes()).hexdigest()
        self.db=self.state/'app.sqlite3'
        with sqlite3.connect(self.db) as d:
            d.execute('CREATE TABLE jobs(document TEXT)');d.execute('INSERT INTO jobs VALUES (?)',(json.dumps(self.job),))
            d.execute('CREATE TABLE events(job_id TEXT,kind TEXT,created REAL,message TEXT)')
            d.execute('INSERT INTO events VALUES (?,?,?,?)',(recovery.JOB,'setup_required',recovery.EVENT_TIME,'선택한 실행 도구가 설치되어 있지 않습니다. 연결 화면을 확인해 주세요.'))
            for t in ('native_local','mac_host_attempts'):
                d.execute('CREATE TABLE '+t+'(state TEXT)');d.execute('INSERT INTO '+t+' VALUES (?)',('TERMINAL',))
        self.db.chmod(0o600)
        with sqlite3.connect(self.state/'mac-astra.sqlite3') as d:d.execute('CREATE TABLE evidence(value TEXT)')
        (self.state/'mac-astra.sqlite3').chmod(0o600)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(recovery,'BUILDER_RECEIPT_SHA',self.source_receipt).start()
        self.receipt=mock.patch.object(recovery.LocalSource,'private_receipt').start()

    def test_original_receipts_are_all_verified_and_preserved(self):
        value=recovery.context(self.state)
        self.assertEqual(self.receipt.call_count,3);self.assertEqual(len(value['receipts']),3)
        self.assertEqual(value['attribution'],'INFERRED_PRE_ADMISSION_MISSING_PROVIDER; event does not name UUID')

    def test_any_active_attempt_or_nonterminal_canonical_fences(self):
        for field,value in [('attempt',{'id':'d'*32}),('calls',4),('head','e'*40)]:
            altered=dict(self.job,**{field:value})
            with sqlite3.connect(self.db) as d:d.execute('UPDATE jobs SET document=?',(json.dumps(altered),))
            with self.assertRaises(AppError):recovery.context(self.state)
        with sqlite3.connect(self.db) as d:
            d.execute('UPDATE jobs SET document=?',(json.dumps(self.job),));d.execute("UPDATE native_local SET state='UNKNOWN'")
        with self.assertRaises(AppError):recovery.context(self.state)

    def test_live_process_group_or_missing_receipt_fences(self):
        atomic_json(self.folders[2]/'running.json',{'pid':12345})
        with mock.patch.object(recovery.os,'killpg',return_value=None):
            with self.assertRaises(AppError):recovery.context(self.state)
        with mock.patch.object(recovery.os,'killpg',side_effect=ProcessLookupError):
            self.assertEqual(len(recovery.context(self.state)['receipts']),3)
        self.receipt.side_effect=AppError('PRIVATE_RECEIPT_INVALID')
        with self.assertRaises(AppError):recovery.context(self.state)

    def test_changed_event_or_other_job_active_fences(self):
        with sqlite3.connect(self.db) as d:d.execute("UPDATE events SET message='other error'")
        with self.assertRaises(AppError):recovery.context(self.state)
        with sqlite3.connect(self.db) as d:d.execute('INSERT INTO jobs VALUES (?)',(json.dumps({'id':'other','attempt':{'id':'x'}}),))
        with self.assertRaises(AppError):recovery.context(self.state)

class ApprovalTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'docs'
        record=json.loads((root/'MAC_EMPTY_ATTEMPT_DECISION_20261007_RECORD.json').read_text())
        self.value={k:record[k] for k in ('id','created_at','updated_at','html_url','issue_url')}
        self.value.update(user=record['actor'],body=(root/'MAC_EMPTY_ATTEMPT_DECISION_20261007.md').read_text())

    def test_pinned_original_decision_is_accepted(self):
        with mock.patch.object(recovery.handoff,'api',return_value=self.value):recovery.approval()

    def test_changed_body_actor_time_or_subject_is_rejected(self):
        changes=[('body',self.value['body']+' '),('updated_at','later'),('issue_url','https://api.github.com/repos/other/issues/85'),('id',1),('user',{'id':263336091,'login':'other','type':'User'})]
        for key,value in changes:
            with self.subTest(key=key),mock.patch.object(recovery.handoff,'api',return_value=dict(self.value,**{key:value})):
                with self.assertRaises(AppError):recovery.approval()


if __name__=='__main__':unittest.main()
