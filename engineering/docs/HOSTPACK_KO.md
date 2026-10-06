# 전체 호스트 설치·복구 묶음 `aiops-hostpack` — 운영 안내 (초안)

상태: **소스 구현 / 병합·활성화 전**. 대표 결정 기록: https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/54#issuecomment-5950850220
(① 단일 패키지 P1–P5 채택 ② 레인 로그인 4개 암호화 보관 허용 ③ 사라진 원장 1회 새 init 후 GitHub 기록으로 대사 ④ 이 도구를 계정 6개 생성·sudoers 설치의 승인된 경로로 인정. 활성화는 감사 PASS 뒤에만.)
이 문서와 결정 기록은 병합·활성화를 승인하지 않는다.
구성 요소의 근거와 단계는 [HOSTPACK_INVENTORY_KO.md](HOSTPACK_INVENTORY_KO.md)에 있다.

## 1. 무엇을 하나

감사 서버만 되살리는 `aiops-recover`(#50~#53) 위에서, 리셋으로 함께 사라진 호스트 구성을 같은 방식으로 복구한다.
모든 명령은 운영자 명령이다. **데몬, 타이머, 부팅 훅을 만들지 않는다.** 시험이 이를 고정한다.

| 명령 | 하는 일 |
|---|---|
| `verify` | 읽기 전용. 구성 요소별 OK/HOLD와 다음 단계를 JSON으로 낸다. 레인 로그인은 이 도구가 증명하지 못한다. 레인마다 `preflight: NOT_PROVEN_BY_THIS_TOOL`로 표시하고, 증명은 워크플로 preflight가 한다. 구조가 모두 맞으면 상태는 `READY_FOR_PREFLIGHT`다 |
| `install` | 없는 것만 설치한다. 계정, 보호 디렉터리, 호스트 helper, 레인 4개(wrapper, adapter, supervisor), 경계 hook, 러너 시작 스크립트, 호스트 정책, sudoers 2개. 바이트가 다른 기존 파일은 덮어쓰지 않고 `INSTALLATION_DRIFT`로 전체를 멈춘다 |
| `boundary-render --commit <main 커밋>` | 그 main 커밋 하나에 고정된 경계 정책을 만든다. hook·evaluator 해시는 고정 매니페스트에서 가져오며 디스크 파일에서 다시 계산하지 않는다 |
| `save` | 호스트 원장(SQLite 백업 API), 레인 설정, 선택한 레인의 로그인 파일을 암호화해 비공개 `aiops-state`의 `host-state` 브랜치에 올린다(CAS) |
| `restore` | 같은 체크포인트를 내려받아 복원한다. 살아 있는 원장은 절대 교체하지 않는다 |
| `build-manifest` | 개발용. 저장소 파일의 정확한 바이트 해시로 `hostpack/manifest.json`을 만든다 |

## 2. 하지 않는 것

- 원장 `init`을 자동으로 실행하지 않는다. 빈 원장을 만들지 않고, 오래된 백업으로 대신 복원하지도 않는다.
- 모델, 빌더, 레인 CLI를 시작하지 않는다. 로그인이 되어 있는지는 워크플로 `operation=preflight`가 증명한다.
- 활성화를 바꾸지 않는다. `control_runtime_enabled`는 항상 false로 쓴다.
- 새 sudo 권한을 만들지 않는다. 규칙은 기존 문서와 예제 파일에 있는 것만 렌더링한다
  (`aiops-base`: launch, preflight, status by id, 레인 adapter의 `--preflight`와 `--launch` / `aiops-program`: 예제 파일 그대로).
- PA-1 root 예외(`sudoers-aiops-program-astra.candidate`)는 설치하지 않는다.

## 3. 사람이 해야 하는 일

로그인은 자동화할 수 없다.

| 항목 | 시점 |
|---|---|
| GitHub 토큰 붙여넣기(`GH_TOKEN`) | 설치·체크포인트·경계 렌더링 때마다 |
| 상태 암호 입력 | `save`와 `restore`마다 |
| 레인 계정별 공급자 로그인(Devin, xAI, Z.AI, Cursor) | 최초와 만료 시. 체크포인트에 보관을 켠 레인은 복원된다 |
| Cursor 레인 설정 `/etc/astra/cursor-lane.json` | 로그인한 CLI로 모델 목록과 계정 정보를 확인한 뒤 작성 |
| GitHub Actions runner 등록(`config.sh`, 라벨 `astra-control-plane`) | `/opt/astra/runner`에서 최초 1회 |
| 저장소 비밀값 `ASTRA_CONTROL_GITHUB_TOKEN`, `ASTRA_COORDINATOR_ROUTINE_TOKEN`, 변수 `ASTRA_COORDINATOR_ROUTINE_ID` | GitHub 설정 |
| 현장 소장 Routine R1~R4 | claude.ai. 저장소에는 `COORDINATOR_PLAYBOOK.md`만 있다 |
| 활성화 재결합 | 새 A3 감사와 activation-only PR 병합. `RUNTIME_PATHS`가 바뀐 뒤에는 이것 없이 발송이 막힌다 |

## 4. 설치 순서 (그록봇)

1. 감사 서버 부트스트랩(`engineering/recovery/BOOTSTRAP_KO.md`)을 먼저 끝낸다. 이 묶음은 그 모듈(`control_plane_recover.py`)과 고정 암호 라이브러리를 재사용한다.
2. 아래 블록을 root로 붙여 넣는다. **병합 뒤 `AIOPS_HOSTPACK_COMMIT`(병합된 커밋 40자리)와 `AIOPS_BOUNDARY_EVIDENCE_URL`(실제 증거 URL) 두 자리만 채운다.**
   자리표시자가 남아 있으면 다운로드 전에 종료한다. 소스가 바뀌면 해시 표도 갱신한다(시험이 표와 파일을 대조한다).
   블록은 네 파일을 고정 커밋에서 받아 해시를 확인한 뒤 열린 바이트로 root 보호 위치에 복사하고, 기존 파일이 다르면 덮어쓰지 않고 멈춘다.
   `/etc/aiops/hostpack.json`은 예제에서 세 값(`source_commit`, `manifest_sha256`, `boundary_evidence_pointer`)만 채워 만든다.

```bash
(
set +x
set -euo pipefail
umask 077
[ "$(id -u)" = 0 ] || { echo 'ROOT_REQUIRED'; exit 1; }
# 병합 뒤 이 40자리 SHA와 증거 URL만 채운다. 소스가 바뀌면 아래 SHA256 표도 갱신한다.
export AIOPS_HOSTPACK_COMMIT='__MERGED_SOURCE_COMMIT_40HEX__'
export AIOPS_BOUNDARY_EVIDENCE_URL='__REAL_BOUNDARY_EVIDENCE_URL__'
[[ "$AIOPS_HOSTPACK_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo 'PIN_COMMIT_REQUIRED'; exit 1; }
[ -f /opt/aiops/lib/control_plane_recover.py ] || { echo 'AUDIT_HOST_BOOTSTRAP_REQUIRED'; exit 1; }
read -rs -p 'GH_TOKEN: ' GH_TOKEN
printf '\n'
export GH_TOKEN
/usr/bin/python3 -I - <<'AIOPS_HOSTPACK_PY'
import base64, hashlib, json, os, pathlib, re, stat, urllib.parse, urllib.request

COMMIT = os.environ['AIOPS_HOSTPACK_COMMIT']
EVIDENCE = os.environ['AIOPS_BOUNDARY_EVIDENCE_URL']
REPOSITORY = 'BeautifulMind-JT/ai-ops-control-plane'
PINNED_SHA256 = {
 "engineering/scripts/control_plane_hostpack.py": "27849816d3616dcd97eba963e37040a33b0859c7a6b80dc2de4877d5258e9dfd",
 "engineering/hostpack/aiops-hostpack": "5492b8d848348b381a69744499c4cd651a1cff2d8042ade9d9b4d8f3dc4b21b0",
 "engineering/hostpack/manifest.json": "1030c68e4f2300837734cb9d3aa3fa4579ee551e06e8ffb3864165327592047e",
 "engineering/hostpack/hostpack.example.json": "fd22710cce91cd77fcd09348b9598067e35975b85f9c0043e6696c620f9d6a31"
}
INSTALL = {
 'engineering/scripts/control_plane_hostpack.py': ('/opt/aiops/lib/control_plane_hostpack.py', 0o644),
 'engineering/hostpack/aiops-hostpack': ('/usr/local/bin/aiops-hostpack', 0o755),
 'engineering/hostpack/manifest.json': ('/etc/aiops/hostpack-manifest.json', 0o644),
}
class Stop(Exception): pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self, *args): raise Stop('BOOTSTRAP_REDIRECT_REJECTED')
def fetch(name):
 request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + '/contents/' + name + '?ref=' + COMMIT,
  headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'})
 with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
  item = json.loads(response.read(2 << 20))
 if item.get('encoding') != 'base64': raise Stop('BOOTSTRAP_ENCODING_REJECTED')
 data = base64.b64decode(item['content'])
 if hashlib.sha256(data).hexdigest() != PINNED_SHA256[name]: raise Stop('BOOTSTRAP_HASH_MISMATCH')
 return data
def protected(path):
 for parent in list(path.parents)[:-0 or None]:
  info = os.lstat(parent)
  if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022: raise Stop('BOOTSTRAP_PARENT_DRIFT')
def place(path, data, mode):
 protected(path)
 if os.path.lexists(path):
  info = os.lstat(path)
  if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode or path.read_bytes() != data:
   raise Stop('BOOTSTRAP_FILE_DRIFT')
  return
 temporary = path.parent / ('.bootstrap-' + path.name)
 descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
 try:
  with os.fdopen(descriptor, 'wb') as target:
   os.fchmod(target.fileno(), mode); target.write(data); target.flush(); os.fsync(target.fileno())
  os.link(temporary, path, follow_symlinks=False)
 finally:
  temporary.unlink(missing_ok=True)
try:
 if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', COMMIT): raise Stop('PIN_OR_ROOT_REQUIRED')
 parsed = urllib.parse.urlsplit(EVIDENCE)
 if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.hostname in ('localhost', 'example.com', 'example.org', 'example.net') or re.search(r'\s', EVIDENCE):
  raise Stop('EVIDENCE_URL_REQUIRED')
 fetched = {name: fetch(name) for name in PINNED_SHA256}
 manifest = json.loads(fetched['engineering/hostpack/manifest.json'])
 canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
 config = json.loads(fetched['engineering/hostpack/hostpack.example.json'])
 config.update(source_commit=COMMIT, manifest_sha256=hashlib.sha256(canonical).hexdigest(), boundary_evidence_pointer=EVIDENCE)
 config_bytes = (json.dumps(config, indent=2, sort_keys=True) + '\n').encode()
 for name, (destination, mode) in INSTALL.items(): place(pathlib.Path(destination), fetched[name], mode)
 place(pathlib.Path('/etc/aiops/hostpack.json'), config_bytes, 0o600)
 print('HOSTPACK_BOOTSTRAPPED: run aiops-hostpack verify, then aiops-hostpack install')
except Stop as error:
 print(str(error)); raise SystemExit(1)
except Exception:
 # Never echo provider errors, headers, credentials or response contents.
 print('BOOTSTRAP_FAILED'); raise SystemExit(1)
AIOPS_HOSTPACK_PY
)
```

3. `aiops-hostpack install` → 체크포인트가 있으면 `aiops-hostpack restore` → `aiops-hostpack boundary-render --commit <main 커밋>` → `/opt/astra/bin/astra-runner-launch` → 워크플로 `operation=preflight` → `aiops-hostpack verify`.
4. 원장이 아직 없고 체크포인트도 없으면, 대표님이 빈 원장을 승인한 뒤에만 관리자가 `init`을 한 번 실행한다.
5. 상태가 바뀔 때마다 `aiops-hostpack save`를 실행한다. 마지막 `save` 이후의 원장 변경은 리셋 때 사라진다.

## 5. 알려진 한계

- **레인 로그인 경로는 추정.** 공급자별 CLI 상태 폴더(`LANE_SPECS`의 `logins`)는 실제 호스트에서 확인이 필요하다. 없는 경로는 캡처하지 않는다.
- **원장 최신성.** 마지막 `save` 이후의 쓰기는 보호되지 않는다. 쓰기 직전 저장은 호스트 helper와 워크플로 변경이 필요하고, 둘 다 `RUNTIME_PATHS`라서 새 감사와 활성화 재결합을 부른다. 이번 범위에서 하지 않았다.
- **러너 설치·등록, 현장 소장 Routine, 활성화 재결합은 이 묶음이 하지 않는다.**
- 실호스트 시운전과 독립 A3 감사 전에는 NOT_READY다.

## 감사 반영 메모 (A3 1차)

- 체크포인트는 팩 커밋이 바뀌어도 복원된다(`source_commit`은 40자리 형식만 검사).
- 원장 스냅샷은 SUBMITTING 행을 담을 수 있다. 복원하면 그 행은 UNKNOWN으로 취급된다.
- 디렉터리가 이미 있고 모드·소유자가 다르면 덮어쓰지 않고 INSTALLATION_DRIFT로 멈춘다.
- `aiops-base` 규칙의 기준 예시는 `.github/control-plane/sudoers-aiops-base.example`이며 테스트가 모듈 템플릿과 같은지 비교한다.

## 설치 경로와 검증 메모 (A3 2차 반영)

- **설치 경로는 둘이다.** 호스트 전체는 이 도구가 기준이다. 기존 `control_plane_install.py`(비활성 pin 설치)는 sudoers를 복사하지 않는 별도 경로이며, 같은 호스트에는 한 경로만 쓴다. 활성화 때 `activated_runtime_sha`는 감사를 통과한 최종 head로 맞추고, 설치된 바이트와 다르면 host-preflight가 발송 전에 거부한다.
- 이 도구의 `verify`, `install`, `save`, `restore`는 모두 `GH_TOKEN`이 필요하다(고정 커밋의 파일을 GitHub에서 받아 확인한다). 토큰은 같은 셸에서 `read -rs`로만 넣는다.
- 새 규칙 파일(`sudoers-aiops-base.example`, `hostpack/manifest.json`, `control_plane_hostpack.py`, `hostpack/aiops-hostpack`, `hostpack/astra-runner-launch`)은 `RUNTIME_PATHS`에 들어 있다. 병합 뒤 새 A3 감사와 activation-only PR이 필요하다.
- 해시 표와 시험은 CI의 `offline` 잡이 바이트 단위로 확인한다(`test_bootstrap_hash_table_matches_the_files_it_pins`, `test_manifest_matches_repository_bytes`).
