# 전체 호스트 설치·복구 묶음 `aiops-hostpack` — 운영 안내 (초안)

상태: **DRAFT / 소스 구현·미설치·실호스트 미검증 / NOT_READY**. 설치·활성화·병합을 승인하지 않는다.
구성 요소의 근거와 단계는 [HOSTPACK_INVENTORY_KO.md](HOSTPACK_INVENTORY_KO.md)에 있다.

## 1. 무엇을 하나

감사 서버만 되살리는 `aiops-recover`(#50~#53) 위에서, 리셋으로 함께 사라진 호스트 구성을 같은 방식으로 복구한다.
모든 명령은 운영자 명령이다. **데몬, 타이머, 부팅 훅을 만들지 않는다.** 시험이 이를 고정한다.

| 명령 | 하는 일 |
|---|---|
| `verify` | 읽기 전용. 구성 요소별 OK/HOLD와 다음 단계를 JSON으로 낸다. 레인 로그인은 증명하지 못하므로 항상 `PREFLIGHT_NOT_PROVEN`이다 |
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
2. `/etc/aiops/hostpack.json`을 `hostpack/hostpack.example.json`에서 만든다. `source_commit`, `manifest_sha256`, `boundary_evidence_pointer`의 자리표시자를 모두 실제 값으로 바꾼다.
   자리표시자가 남아 있으면 설정 검증이 거부한다. `boundary_evidence_pointer`가 실제 URL이 아니면 `install`이 아무것도 쓰기 전에 멈춘다.
3. `install` → 체크포인트가 있으면 `restore` → `boundary-render` → 러너 시작 스크립트 실행 → 워크플로 `operation=preflight` → `verify`.
4. 원장이 아직 없고 체크포인트도 없으면, 대표님이 빈 원장을 승인한 뒤에만 관리자가 `init`을 한 번 실행한다.
5. 감사 호스트와 같은 방식으로, 상태가 바뀔 때마다 `save`를 실행한다. 마지막 `save` 이후의 원장 변경은 리셋 때 사라진다.

## 5. 알려진 한계

- **호출 스크립트 없음.** `aiops-hostpack` 호출 스크립트(`/usr/local/bin`)를 이 PR에 넣지 못했다. 설치된 모듈은 고정 라이브러리 경로를 직접 지정해야만 돌아간다.
  자동 모드 안전 장치가 해당 파일 생성을 거부했고(`Unauthorized Persistence`), 거부를 우회하지 않았다. 대표님의 권한 규칙 결정이 필요하다.
- **러너 시작 스크립트의 결함 하나.** `hostpack/astra-runner-launch`가 기대 해시 파일을 읽지 못했을 때 즉시 멈추지 않고 빈 해시로 다음 검사로 넘어간다.
  뒤의 경계 검사가 빈 해시를 거부하므로 열린 채로 시작되지는 않는다고 판단하지만 확인하지 못했다. 고치는 편집도 같은 안전 장치가 거부했다.
- **레인 로그인 경로는 추정.** 공급자별 CLI 상태 폴더(`LANE_SPECS`의 `logins`)는 실제 호스트에서 확인이 필요하다. 없는 경로는 캡처하지 않는다.
- **원장 최신성.** 마지막 `save` 이후의 쓰기는 보호되지 않는다. 쓰기 직전 저장은 호스트 helper와 워크플로 변경이 필요하고, 둘 다 `RUNTIME_PATHS`라서 새 감사와 활성화 재결합을 부른다. 이번 범위에서 하지 않았다.
- **러너 설치·등록, 현장 소장 Routine, 활성화 재결합은 이 묶음이 하지 않는다.**
- 실호스트 시운전과 독립 A3 감사 전에는 NOT_READY다.
