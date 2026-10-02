# 새 Grok VM 고정 부트스트랩 — HR-D3

사용자가 요청한 **root 직접 붙여 넣기** 블록이다. 이 PR에서는 실행하지 않았다.
새 sudo 규칙을 만들지 않는다. 대상은 Debian 13, x86_64, Python 3.13,
glibc 2.41이다. aiops-fable 원본은 main a964c0d의 바이트와 low 설정을 유지한다.

**병합 뒤 `AIOPS_BOOTSTRAP_COMMIT`의 자리만 승인된 병합 커밋 40자리로 바꾼다.**
아래 SHA256 표는 이번 소스의 실제 파일 바이트에 고정되어 있다. 병합 과정에서
해당 파일 바이트가 바뀌면 그 파일 해시도 다시 적는다. placeholder 상태는
apt나 다운로드 전에 PIN_COMMIT_REQUIRED로 종료하므로 그대로 실행하지 않는다.
신뢰 기준은 병합된 이 문서의 커밋·파일 해시다. latest/브랜치 HEAD를 사용하지 않는다.

대표는 최초 한 번 README가 있는 **비공개** `BeautifulMind-JT/aiops-state` 저장소만
만든다. 별도 브랜치·enrollment_commit 설정은 필요 없다. GH_TOKEN은 아래
`read -rs`에서만 입력하며 출력·인자에 넣지 않는다. root 프로세스 내부 인증에만
사용하고 모델 자식 프로세스에는 전달하지 않는다. GitHub 토큰의 checkpoint
저장은 기본 꺼짐이다.

블록은 8개 파일을 GitHub API의 고정 커밋에서 받고, 모든 SHA256과 기존 파일
일치를 검사한 뒤 열린 바이트로 root 보호 위치 및 cache에 복사한다. wheel·CLI는
고정 매니페스트 URL/해시로 복구 코드가 설치한다. 기존 파일 drift는 덮어쓰지 않는다.
상태 브랜치가 있으면 복원, 없으면 생성 전용 CAS 등록을 실행한다. 최초 원장이
없으면 별도 y/N 확인을 받는다. 복원 실패를 빈 원장 등록으로 바꾸지 않는다.

apt 단계 뒤에 `/usr/local`, `/usr/local/bin`을 root:root 0755로 맞춘다. Debian은
이 둘을 root:staff 2775로 만들 수 있는데, 설치기·복구 wrapper·원본 aiops-fable은
group 쓰기 가능한 상위 디렉터리를 모두 거부하기 때문이다. 다른 디렉터리 권한은 바꾸지 않는다.

등록(`--enroll`)은 상태 암호를 두 번 받아 일치할 때만 진행하고, 올린 체크포인트를
같은 키로 다시 내려받아 복호화되는지 확인한다. 이 암호를 잃으면 체크포인트를 열 수 없다.

```bash
(
set +x
set -euo pipefail
umask 077
[ "$(id -u)" = 0 ] || { echo 'ROOT_REQUIRED'; exit 1; }
# 병합 뒤 이 40자리 SHA만 갱신한다. 소스가 바뀌면 SHA256 표도 갱신한다.
export AIOPS_BOOTSTRAP_COMMIT='__MERGED_SOURCE_COMMIT_40HEX__'
[[ "$AIOPS_BOOTSTRAP_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo 'PIN_COMMIT_REQUIRED'; exit 1; }
apt-get update
apt-get install -y --no-install-recommends python3 ca-certificates keyutils util-linux passwd
# Debian may create /usr/local and /usr/local/bin as root:staff 2775. The installer,
# the recovery wrappers and the original aiops-fable refuse group-writable parents.
chown root:root /usr/local /usr/local/bin
chmod 0755 /usr/local /usr/local/bin
read -rs -p 'GH_TOKEN: ' GH_TOKEN
printf '\n'
export GH_TOKEN
/usr/bin/python3 -I - <<'AIOPS_BOOTSTRAP_PY'
import base64, hashlib, json, os, pathlib, re, stat, urllib.error, urllib.parse, urllib.request

SOURCE_COMMIT = os.environ['AIOPS_BOOTSTRAP_COMMIT']
REPOSITORY = 'BeautifulMind-JT/ai-ops-control-plane'
PINNED_SHA256 = {
 "engineering/recovery/aiops-fable": "e59e29e068978b6e901653d40cdbd00f463c8b52ba44827d057c8a81f7579b97",
 "engineering/recovery/aiops-recover": "30cd439ab638631a475db6d4dbfb9ef4b1c2b3b08a0ce763a035ef8d26c30a29",
 "engineering/scripts/control_plane_recover.py": "7e74f7c77cbba557365234bcf7bc8b0975c094d5aa1d50626b86dd51d8f6d767",
 "engineering/scripts/control_plane_recover_token.py": "7d7168caedd17300a2d500aa67ffc0545e132f40e722197d6fc201b5328928f4",
 "engineering/recovery/claude": "bf32f8635cbaf9026b061584bc5a3f55cfd88d3742eae261a400f30a0ee8fc0f",
 "engineering/scripts/control_plane_fable.py": "b3d49498dc0364b10db9e4d034bd252b5fce1201475e4c7b29c692c7fb3fbfd6",
 "engineering/recovery/manifest.json": "eddff840e6995a45851072fa90c24ccd8dd82f5d70d19d536f639ca4430218d1",
 "engineering/recovery/recovery.example.json": "55b57b131721cc023207ea97ff36bb4291ae0f4dc8218e6416f61cac8c8fbaa5"
}
INSTALL = {
 'engineering/recovery/aiops-fable': ('/opt/aiops/bin/aiops-fable', 0o755),
 'engineering/recovery/aiops-recover': ('/usr/local/bin/aiops-recover', 0o755),
 'engineering/scripts/control_plane_recover.py': ('/opt/aiops/lib/control_plane_recover.py', 0o644),
 'engineering/scripts/control_plane_recover_token.py': ('/opt/aiops/lib/control_plane_recover_token.py', 0o644),
 'engineering/recovery/claude': ('/usr/local/bin/claude', 0o755),
 'engineering/scripts/control_plane_fable.py': ('/opt/aiops/lib/fable/control_plane_fable.py', 0o644),
 'engineering/recovery/manifest.json': ('/etc/aiops/recovery-manifest.json', 0o644),
 'engineering/recovery/recovery.example.json': ('/etc/aiops/recovery.json', 0o600),
}
CACHE = pathlib.Path('/var/cache/aiops-recover')
class Stop(Exception): pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self, *args): raise Stop('BOOTSTRAP_REDIRECT_REJECTED')
def api(suffix):
 request = urllib.request.Request('https://api.github.com/repos/' + suffix,
  headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'})
 with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
  return json.loads(response.read(2 << 20))
def directory(path):
 if not os.path.lexists(path):
  directory(path.parent); path.mkdir(mode=0o755); os.chmod(path, 0o755)
 info = os.lstat(path)
 if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
  raise Stop('BOOTSTRAP_PARENT_DRIFT')
def parent(path):
 directory(path.parent)
def read_file(path, mode):
 descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
 with os.fdopen(descriptor, 'rb') as source:
  info = os.fstat(source.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode:
   raise Stop('BOOTSTRAP_FILE_DRIFT')
  return source.read(2 << 20)
def check(path, data, mode):
 parent(path)
 if os.path.lexists(path) and read_file(path, mode) != data:
  raise Stop('BOOTSTRAP_FILE_DRIFT')
def copy(path, data, mode):
 if os.path.lexists(path): return
 temporary = path.parent / ('.bootstrap-' + path.name)
 descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
 try:
  with os.fdopen(descriptor, 'wb') as target:
   os.fchmod(target.fileno(), mode); target.write(data); target.flush(); os.fsync(target.fileno())
  os.link(temporary, path, follow_symlinks=False)
 finally:
  temporary.unlink(missing_ok=True)
try:
 if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', SOURCE_COMMIT): raise Stop('PIN_OR_ROOT_REQUIRED')
 # Fetch the eight pinned files only. No workspace script or unverified setup.py.
 fetched = {}
 for name, digest in PINNED_SHA256.items():
  item = api(REPOSITORY + '/contents/' + name + '?ref=' + SOURCE_COMMIT)
  if item.get('encoding') != 'base64': raise Stop('BOOTSTRAP_ENCODING_REJECTED')
  data = base64.b64decode(item['content'])
  if hashlib.sha256(data).hexdigest() != digest: raise Stop('BOOTSTRAP_HASH_MISMATCH')
  fetched[name] = data
 manifest = json.loads(fetched['engineering/recovery/manifest.json'])
 cfg = json.loads(fetched['engineering/recovery/recovery.example.json'])
 if hashlib.sha256(fetched['engineering/recovery/manifest.json']).hexdigest() != cfg['manifest_sha256']:
  raise Stop('BOOTSTRAP_MANIFEST_REJECTED')
 for item in manifest['files']:
  if item['cache_path'] in fetched and hashlib.sha256(fetched[item['cache_path']]).hexdigest() != item['sha256']:
   raise Stop('BOOTSTRAP_MANIFEST_REJECTED')
 # All input hashes and ALL existing destinations must pass before a byte is copied.
 jobs = []
 for name, data in fetched.items():
  destination, mode = INSTALL[name]
  jobs += [(pathlib.Path(destination), data, mode), (CACHE / name, data, 0o600)]
 for path, data, mode in jobs: check(path, data, mode)
 for path, data, mode in jobs: copy(path, data, mode)
 # Private state repository requires only an initial README commit from the owner.
 state_repo = api(cfg['state_repository'])
 if state_repo.get('private') is not True: raise Stop('STATE_REPOSITORY_NOT_PRIVATE')
 arguments = ['/usr/local/bin/aiops-recover']
 try:
  api(cfg['state_repository'] + '/git/ref/heads/' + cfg['state_branch'])
 except urllib.error.HTTPError as error:
  if error.code != 404: raise
  arguments.append('--enroll')
 # The Python source arrived on a heredoc; restore interactive stdin for y/N.
 terminal = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
 os.dup2(terminal, 0); os.close(terminal)
 os.execv(arguments[0], arguments)
except Stop as error:
 print(str(error)); raise SystemExit(1)
except Exception:
 # Never echo provider errors, headers, credentials or response contents.
 print('BOOTSTRAP_FAILED'); raise SystemExit(1)
AIOPS_BOOTSTRAP_PY
)
```

복구에서는 상태 암호 1회만 입력한다. 최초 토큰 발급과 만료 재발급에는
표시된 URL을 브라우저에서 승인하고 받은 코드를 붙여 넣는다. 토큰 자체와
그 밖의 CLI 화면은 표시하지 않는다. 최소 검증 모델 호출은 y/N 뒤에만 한다.
pyte·추출기 시험·PTY 폭·CLI 버전 검증은 URL 표시 전이다.

NOT_READY 사유는 **실호스트 시운전 전**이다. 남은 대표 작업은 병합 뒤 커밋
핀 갱신(미병합 상태에서 핀을 확정할 수 없음), 최초 private README 저장소 생성
(코드에 저장소 생성 권한을 주지 않음)이다. 독립 A3·병합은 기존 저장소 절차다.
