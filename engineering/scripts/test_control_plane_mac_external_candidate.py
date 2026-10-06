"""Actual Mac synthetic OS fence checks; no real provider/account/model IO."""
from pathlib import Path
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac_app'))
import common
import mac_sandbox
import native_transfer


@unittest.skipUnless(sys.platform=='darwin','requires actual Mac Seatbelt')
class ExternalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='aiops-external-fixture-',dir=Path.home()/'Library/Caches')
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.state=common.private_directory(self.base/'state')
        self.folder=common.private_directory(self.state/'native/fixture/attempt')
        self.checkout=common.private_directory(self.folder/'checkout')
        self.runtime=mac_sandbox.prepare_candidate_runtime(self.folder)
        self.tmp=common.private_directory(self.folder/'provider-tmp')
        self.control=self.state/'synthetic-token';self.control.write_text('synthetic')
        self.credential=self.base/'synthetic-credential';self.credential.write_text('synthetic')
        self.outside=self.base/'synthetic-other-file';self.outside.write_text('synthetic')
    def run_code(self,script,*args,writing=True,scratch_write=False):
        argv=mac_sandbox.external_candidate_command([sys.executable,'-I','-S','-c',script,*map(str,args)],
                        self.state,self.folder,self.checkout,writing=writing,scratch_write=scratch_write)
        run=subprocess.run(argv,cwd=self.checkout,env={'PATH':'/usr/bin:/bin'},capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr[-1000:]);return run.stdout
    def test_builder_writes_only_assigned_checkout_and_task_temporary_directory(self):
        script='''from pathlib import Path
import sys
for p in map(Path,sys.argv[1:]):p.write_text('synthetic allowed')
'''
        self.run_code(script,self.checkout/'new.py',self.tmp/'scratch')
        self.assertEqual((self.checkout/'new.py').read_text(),'synthetic allowed')
    def test_arbitrary_user_file_and_credential_and_authority_are_unreadable_and_unwritable(self):
        script='''from pathlib import Path
import sys
for p in map(Path,sys.argv[1:]):
 for action in (p.read_text,lambda:p.write_text('bad')):
  try:action()
  except PermissionError:pass
  else:raise SystemExit(2)
'''
        for writing in (True,False):self.run_code(script,self.outside,self.credential,self.control,writing=writing)
        self.assertEqual(self.outside.read_text(),'synthetic');self.assertEqual(self.credential.read_text(),'synthetic')
    def test_reviewer_cannot_write_checkout_temporary_runtime_or_output_leaf(self):
        script='''from pathlib import Path
import sys
for p in map(Path,sys.argv[1:]):
 try:p.write_text('bad')
 except PermissionError:pass
 else:raise SystemExit(2)
'''
        self.run_code(script,self.checkout/'new.py',self.tmp/'scratch',self.runtime/'cache',self.folder/'last-message.json',writing=False)
    def test_children_inherit_the_same_write_and_read_boundaries(self):
        script='''from pathlib import Path
import subprocess,sys
code="from pathlib import Path;import sys;p=Path(sys.argv[1]);p.write_text('bad')"
assert subprocess.run([sys.executable,'-I','-S','-c',code,sys.argv[1]],capture_output=True).returncode!=0
'''
        for writing in (True,False):self.run_code(script,self.outside,writing=writing)
    def test_checkout_symlink_cannot_reopen_authority_or_external_credential(self):
        links=[]
        for index,target in enumerate((self.control,self.credential)):
            link=self.checkout/('link-'+str(index));link.symlink_to(target);links.append(link)
        script='''from pathlib import Path
import sys
for p in map(Path,sys.argv[1:]):
 for action in (p.read_text,lambda:p.write_text('bad')):
  try:action()
  except PermissionError:pass
  else:raise SystemExit(2)
'''
        self.run_code(script,*links)
    def test_hardlink_cannot_import_authority_or_external_credential(self):
        script='''from pathlib import Path
import os,sys
source,target=map(Path,sys.argv[1:])
try:os.link(source,target)
except PermissionError:pass
else:
 try:target.read_text()
 except PermissionError:pass
 else:raise SystemExit(2)
'''
        for index,target in enumerate((self.control,self.credential)):
            self.run_code(script,target,self.checkout/('hard-'+str(index)))
        self.assertEqual(self.control.stat().st_nlink,1);self.assertEqual(self.credential.stat().st_nlink,1)
    def test_git_and_agent_configuration_metadata_stay_read_only(self):
        targets=[]
        for name in ('.git','.agents','.codex','.aws'):
            path=common.private_directory(self.checkout/name)/'synthetic';path.write_text('synthetic');targets.append(path)
        script='''from pathlib import Path
import sys
for p in map(Path,sys.argv[1:]):
 assert p.read_text()=='synthetic'
 try:p.write_text('bad')
 except PermissionError:pass
 else:raise SystemExit(2)
'''
        self.run_code(script,*targets)
    def test_reviewer_network_is_denied_and_builder_matches_existing_network_allowance(self):
        listener=socket.socket();self.addCleanup(listener.close)
        listener.bind(('127.0.0.1',0));listener.listen(10);port=listener.getsockname()[1]
        script='''import socket,sys
try:c=socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=.5);c.close();ok=True
except OSError:ok=False
print(int(ok))
'''
        self.assertEqual(self.run_code(script,port,writing=False).strip(),'0')
        self.assertEqual(self.run_code(script,port,writing=True).strip(),'1')
    def test_real_control_path_or_arbitrary_runtime_scope_cannot_be_injected(self):
        with self.assertRaisesRegex(common.AppError,'SCOPE_INVALID'):
            mac_sandbox.external_candidate_command(['fixture'],self.state,self.folder,self.state)

    def test_scratch_grants_only_fixed_vendor_initialization_paths(self):
        allowed=[p if p.name=='installation_id' else p/'fixture' for p in mac_sandbox.candidate_scratch_paths(self.folder)]
        script='''from pathlib import Path
import sys
for p in map(Path,sys.argv[1:]):p.write_text('synthetic runtime')
'''
        self.run_code(script,*allowed,writing=False,scratch_write=True)
        denied=[self.runtime/'other',self.runtime/'empty-home/.profile',
                self.runtime/'empty-codex/auth.json',self.runtime/'empty-codex/config.toml',
                self.runtime/'empty-codex/skills/user-skill']
        script='''from pathlib import Path
import sys
for p in map(Path,sys.argv[1:]):
 try:p.write_text('bad')
 except PermissionError:pass
 else:raise SystemExit(2)
'''
        self.run_code(script,*denied,writing=False,scratch_write=True)

    def test_scratch_does_not_make_reviewer_source_or_authority_writable(self):
        files=[]
        for name in ('receipt.json','request.json','running.json','claimed.json','schema.json','stdout.log','last-message.json'):
            path=self.folder/name;path.write_text('synthetic private host evidence');files.append(path)
        code=self.checkout/'code.py';code.write_text('synthetic source')
        script='''from pathlib import Path
import sys
code=Path(sys.argv[1]);assert code.read_text()=='synthetic source'
try:code.write_text('bad')
except PermissionError:pass
else:raise SystemExit(2)
for p in map(Path,sys.argv[2:]):
 for action in (p.read_text,lambda:p.write_text('bad'),p.unlink):
  try:action()
  except PermissionError:pass
  else:raise SystemExit(3)
'''
        self.run_code(script,code,*files,self.control,self.credential,self.outside,writing=False,scratch_write=True)
        self.assertTrue(all(p.read_text()=='synthetic private host evidence' for p in files))
        self.assertEqual(code.read_text(),'synthetic source')

    def test_fake_completion_in_scratch_is_not_a_private_native_receipt(self):
        fake=self.runtime/'state/receipt.json'
        self.run_code("from pathlib import Path;import sys;Path(sys.argv[1]).write_text('{\"state\":\"TERMINAL\",\"status\":\"complete\"}')",
                      fake,writing=False,scratch_write=True)
        worker=native_transfer.NativeWorker(self.state,lambda:{'session_minutes':20})
        record={'request_id':'fixture','attempt':{'id':'attempt'},'updated':time.time()}
        self.assertIsNone(worker.receipt(record))
        self.assertFalse((self.folder/'receipt.json').exists())

    def test_scratch_symlink_hardlink_and_parent_escape_cannot_reopen_host_evidence(self):
        script='''from pathlib import Path
import os,sys
directory=Path(sys.argv[1])
for i,target in enumerate(map(Path,sys.argv[2:])):
 link=directory/('link-'+str(i))
 try:link.symlink_to(target)
 except PermissionError:pass
 else:
  for action in (link.read_text,lambda:link.write_text('bad')):
   try:action()
   except PermissionError:pass
   else:raise SystemExit(2)
 try:os.link(target,directory/('hard-'+str(i)))
 except PermissionError:pass
 else:raise SystemExit(3)
escape=directory/'../../../../../../../synthetic-other-file'
try:escape.write_text('bad')
except PermissionError:pass
else:raise SystemExit(4)
'''
        for writing in (False,True):
            directory=common.private_directory(self.runtime/'tmp'/('role-'+str(writing)))
            self.run_code(script,directory,self.control,self.credential,self.outside,writing=writing,scratch_write=True)
            # Validation forbids reusing a scratch tree containing links.
            for path in directory.iterdir():path.unlink()
        self.assertEqual(self.control.stat().st_nlink,1)
        self.assertEqual(self.credential.stat().st_nlink,1)
        self.assertEqual(self.outside.read_text(),'synthetic')

    def test_scratch_children_cannot_write_reviewed_code_or_use_network(self):
        listener=socket.socket();self.addCleanup(listener.close)
        listener.bind(('127.0.0.1',0));listener.listen(1)
        script='''import subprocess,sys
code="from pathlib import Path;import sys;Path(sys.argv[1]).write_text('bad')"
assert subprocess.run([sys.executable,'-I','-S','-c',code,sys.argv[1]],capture_output=True).returncode!=0
code="import socket,sys;socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=.5)"
assert subprocess.run([sys.executable,'-I','-S','-c',code,sys.argv[2]],capture_output=True).returncode!=0
'''
        self.run_code(script,self.checkout/'reviewed.py',listener.getsockname()[1],writing=False,scratch_write=True)

    def test_runtime_creation_refuses_reuse_or_linked_attempt(self):
        with self.assertRaisesRegex(common.AppError,'RUNTIME_EXISTS'):
            mac_sandbox.prepare_candidate_runtime(self.folder)
        alias=self.base/'alias';alias.symlink_to(self.folder)
        with self.assertRaisesRegex(common.AppError,'RUNTIME_INVALID'):
            mac_sandbox.prepare_candidate_runtime(alias)

    def test_scratch_validation_rejects_preexisting_shared_inode_or_symlink(self):
        target=self.runtime/'tmp/link';target.symlink_to(self.credential)
        with self.assertRaisesRegex(common.AppError,'RUNTIME_INVALID'):
            mac_sandbox.external_candidate_command(['fixture'],self.state,self.folder,self.checkout,scratch_write=True)
        target.unlink();target.hardlink_to(self.credential)
        with self.assertRaisesRegex(common.AppError,'RUNTIME_INVALID'):
            mac_sandbox.external_candidate_command(['fixture'],self.state,self.folder,self.checkout,scratch_write=True)


if __name__=='__main__':unittest.main()
