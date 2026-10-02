# 전체 호스트 설치·복구 패키지 — 구성 요소 목록과 단계 (제안)

상태: **DRAFT / 제안 / 미채택**. 코드가 아니다. 이 문서는 구현 전에 대표 승인을 받기 위한
구성 요소 목록이다. 설치·활성화·병합을 승인하지 않고, 어떤 호스트도 건드리지 않는다.

## 1. 왜 필요한가

그록봇 VM이 리셋되면서 감사 도구만 사라진 것이 아니다. 개발 레인, 호스트 원장, runner, 코디네이터가
쓰던 호스트 쪽 구성도 함께 사라졌다. #50~#53은 이 중 **감사 서버 하나**만 한 줄로 되살린다
(`aiops-recover`, 체크포인트 범위는 `/var/lib/aiops-fable`뿐이다: `control_plane_recover.py` `STATE`).
나머지를 같은 방식으로 묶어 "전체 한 줄 복구"를 만드는 것이 이 패키지의 목표다.

## 2. 완성 기준 (먼저 못 박는다)

새 VM에서 아래가 한 번에 되면 완성이다.

1. 한 줄 명령으로 승인된 커밋의 구성 요소가 모두 설치되고 해시가 맞는다.
2. 보존된 상태(원장, 감사 기록)가 롤백·손상 없이 복원된다. 빈 원장으로 시작하지 않는다.
3. 레인 **1개**가 작은 작업 1건을 끝까지(발송 → 구현 → 리뷰 → 완료 판정) 통과한다.
4. 복구 전의 불명 실행은 자동 재실행되지 않고 멈춘 채 대표에게 보고된다.

"만들었다"가 아니라 "실제 VM에서 이 4가지가 확인됐다"가 완성이다.

## 3. 구성 요소 목록

출처는 이 저장소의 문서·코드이며, 확인하지 못한 항목은 "확인 필요"로 적었다.

| # | 구성 요소 | 설치 위치 / 계정 | 출처 | 상태 보존 | 사람이 해야 하는 로그인·비밀 | 현재 복구 범위 |
|---|---|---|---|---|---|---|
| A | 감사 서버 `aiops-fable`, Claude CLI, 감사 계정 `aiops-auditor` | `/opt/aiops/*`, `/usr/local/bin/claude`, `/etc/aiops/*`, 계정 `aiops-auditor` | `CONTROL_PLANE_RUNTIME.md` "Astra on the host", `engineering/recovery/*` | `/var/lib/aiops-fable` (암호화 체크포인트) | Claude 로그인 승인(최초·만료 시), 상태 암호, GitHub 토큰 | **있음 (#50~#53)** |
| B | 호스트 원장 helper | `/opt/astra/bin/astra-host-control`, `/etc/astra/control-plane-host.json`, 계정 `astra-control` | `CONTROL_PLANE_RUNTIME.md` "보호된 host admission", `scripts/control_plane_host.py` | `/var/lib/astra/control/` (`admission.sqlite3` 0600과 transactional side file) | 없음(설정은 비밀 아님) | 없음 |
| C | 프로그램 bridge 라이브러리와 정책 | `/opt/aiops/lib/program/`, `/opt/aiops/lib/.github/control-plane/` | `PROGRAM_ASTRA_AUTOMATION.md` "Rollout on the host" 2~3 | `/var/lib/aiops-fable` (A와 공유) | 없음 | 일부(`control_plane_fable.py`만) |
| D | GitHub self-hosted runner(라벨 `astra-control-plane`)와 경계 hook | runner 계정, `ACTIONS_RUNNER_HOOK_JOB_STARTED` hook, boundary 정책 | `CONTROL_PLANE_RUNTIME.md`, `scripts/control_plane_boundary*.py`, `boundary-policy.example.json` | runner 등록 정보(확인 필요) | runner 등록 토큰(GitHub) | 없음 |
| E | sudoers | `/etc/sudoers.d/aiops-program`, PA-1 후보 `/etc/sudoers.d/aiops-program-astra` | `sudoers-aiops-program.example`, `sudoers-aiops-program-astra.candidate` | 없음 | 없음 | 없음(PA-1 감사 호스트 규칙 1개만 별도) |
| F1 | 레인 DEVIN | 계정·래퍼·어댑터 `/opt/astra/bin/astra-builder-devin`, `/opt/astra/libexec/astra-devin-adapter` | `BUILDER_LANES.md`, `adapters/imported/*` | 작업 clone/worktree, 세션 ID(확인 필요) | Devin 계정 로그인 | 없음 |
| F2 | 레인 GROK_BUILD | `astra-builder-grok-build`, `astra-grok-adapter` | 같음 | 같음 | xAI 로그인 | 없음 |
| F3 | 레인 GLM (OpenCode 1.18.32 + Z.AI Coding Plan, 결정 M2) | `astra-builder-glm`, `astra-glm-adapter` | 같음 | 같음 | Z.AI 로그인 | 없음 |
| F4 | 레인 CURSOR (공식 `agent` CLI, 결정 M3·M6) | `astra-builder-cursor`, `astra-cursor-adapter`, `/etc/astra/cursor-lane.json` | `BUILDER_LANES.md` "CURSOR host acceptance", `adapters/cursor/README.md` | 같음 | Cursor 로그인 | 없음 |
| G | 레인 supervisor | `/opt/astra/libexec/astra-*-supervisor` | `adapters/*/SHA256SUMS*` (일부만 가져옴: 확인 필요) | 세션 생존 증거 | 없음 | 없음 |
| H | 설치 pin과 활성화 | `/opt/astra/RUNTIME_SOURCE_SHA.txt`, 저장소의 `activation.json` | `scripts/control_plane_install.py` (비활성 설치 전용) | 없음 | 활성화는 대표 병합 | 없음 |
| I | 코디네이터(Claude Sonnet Routine), opus-consult Routine | Claude 계정 쪽 Routine | `COORDINATOR_PLAYBOOK.md` | VM 밖으로 보임(확인 필요) | 없음 | VM 리셋 영향 없음(확인 필요) |
| J | Slack 송신 경로 | 확인 필요 | `ASTRA_SLACK.md` | 없음 | Slack 토큰(확인 필요) | 없음 |
| K | 단일 GitHub PAT와 저장소 범위 토큰 | control 계정의 secret store(확인 필요) | `AGENTS.md` §11 (결정 M4) | 없음 | GitHub 토큰 | 일부(GH_TOKEN 입력만) |

범위 밖: M7 감리 도구(#48, `aiops-inspect`)는 이 패키지에 넣지 않는다. 별도 PR로 한다.

## 4. 반드시 보존해야 하는 상태

- `/var/lib/astra/control/` — 호스트 원장. 잃어버리면 **빈 원장으로 재생성하지 않는다**
  (`CONTROL_PLANE_RUNTIME.md`). 이번 리셋으로 이미 사라졌다. 되살릴 수 없으므로
  새 원장을 관리자가 `init`하고, 진행 중이던 작업은 GitHub PR·이슈로 대사한다(대표 결정 필요, 7번).
- `/var/lib/aiops-fable/` 전체 — 감사 기록, 소비된 claim, quota ticket (A에서 이미 보존 중).
- 레인별 작업 clone/worktree와 공급자 세션 ID — 보존 방식 확인 필요.
- 자격 증명 참조 — 값은 저장소·로그·chat에 두지 않는다.

## 5. 사람이 해야 하는 일 (자동화할 수 없는 것)

로그인은 대표님이 해야 한다. 한 줄 복구가 줄이는 것은 "설치와 복원"이지 "로그인"이 아니다.

| 항목 | 빈도 | 비고 |
|---|---|---|
| Claude 로그인 승인 | 최초와 만료 시 | A에서 구현됨. 토큰은 암호화 체크포인트에 보관 |
| 상태 암호 입력 | 복구할 때마다 1회 | A에서 구현됨 |
| GitHub 토큰 붙여넣기 | 복구할 때마다 | 현재 방식 유지 |
| runner 등록 토큰 | 최초 또는 runner 교체 시 | D |
| 각 레인 공급자 로그인 | 최초와 만료 시 | F1~F4. 보관 가능 여부는 공급자별로 확인 필요 |

## 6. 단계 (한 번에 올리지 않는다)

| 단계 | 내용 | 통과 조건 (실제 VM) |
|---|---|---|
| P0 | 감사 서버 (A) | 완료. 실제 VM 시운전 중 |
| P1 | 호스트 원장 B + control 계정 + 상태 보존 확대 | 원장 복원 후 해시·체인 검증. 롤백·손상은 HOLD |
| P2 | runner D + 경계 hook + sudoers E + 프로그램 bridge C | 제어 job만 허용되고 거부 경로가 거부됨 |
| P3 | 레인 **1개** F + supervisor G | 작은 작업 1건 통과(완성 기준 3번) |
| P4 | 나머지 레인 | 레인마다 위 통과 조건 반복 |
| P5 | 설치 pin·활성화 H | 기존 활성화 절차(감사 → 대표 병합) 그대로 |

각 단계는 직전 단계가 실제 VM에서 통과한 뒤에만 시작한다. 첫 레인은 **CURSOR를 추천**한다
(설치 절차가 `adapters/cursor/README.md`에 가장 구체적이고, 결정 M6으로 이미 활성 허용된 레인이다).

## 7. 대표 결정이 필요한 것

1. **재부팅 뒤 자동 계속(상시 서비스)을 둘 것인가.**
   - 현재 규칙은 `AGENTS.md`의 "NO STANDING ROUTINES"(예외는 코디네이터 heartbeat 하나)이고,
     복구 설계(`HOST_REPLACEMENT_RECOVERY_DESIGN.md` §1)도 "daemon, polling, scheduled reinstall 없음"을 전제로 한다.
   - 따라서 이 패키지는 **명령 시 복구**(한 줄을 실행하면 전체가 준비됨)로 시작한다.
     재부팅 뒤 자동 시작 장치(systemd·cron 등)는 대표가 별도 결정을 GitHub에 직접 기록한 뒤에만 추가한다.
2. 첫 레인: CURSOR(추천) / 다른 레인.
3. 단계 순서 P1~P5 승인.
4. 레인 공급자 로그인을 암호화 체크포인트에 보관할지(공급자별로 가능 여부를 먼저 조사한 뒤 결정).
5. 리셋으로 사라진 호스트 원장은 새로 `init`하고 GitHub 기록으로 대사하는 방침.

## 8. 하지 않는 것

- 새 sudo 규칙, 계정, 자격 증명, 과금 설정을 이 문서만으로 만들지 않는다.
- 빈 원장 생성, 오래된 백업으로의 대체 복원, 불명 실행의 자동 재실행.
- 복구를 활성화로 취급하지 않는다. 활성화는 기존 절차(감사 → 대표 병합)를 따른다.
- 모델 호출, 호스트 접속, 설치는 이 문서의 작업에 포함되지 않는다.
