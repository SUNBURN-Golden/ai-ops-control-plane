# Mac 앱 자율 개발 모드 — 2026-10-02 사용자 결정의 구현 후보

## 사용자 요구

2026-10-02 이 개발 세션에서 사용자는 다음을 명시했다.

- AIOPS 실행 기반을 Grok 봇 VM에서 자신의 Mac으로 옮긴다.
- Dot, Grok, OpenClaw, Hermes 중 특정 봇에 종속될 필요가 없다.
- 설치 이후 레포와 목표를 주면 계획도 스스로 작성하고, 산출물을 완성할 때까지 개발·감사·수정을 진행한다.
- 통상적인 개발 판단 때문에 사용자를 호출하지 않는다. 필수적인 계정·권한·중요 결정만 질문하고 최종 결과를 사용자가 검수한다.
- 개발·감사·감리 모델을 사용자가 설정한다. 특정 Opus/Fable 모델을 앱에 강제하지 않는다.
- 직관적인 UI와 봇에서 호출하기 쉬운 인터페이스를 제공한다.
- 복구 페이지·복구 설치 패키지는 추가하지 않는다.
- 후속 명시 지시로 Codex·Claude뿐 아니라 Cursor, GLM, Grok Build, Devin도 포함한다. 기존 AIOPS 저장소의 실제 어댑터 구조를 바탕으로 Mac 소프트웨어를 계속 개발한다.

이 기록은 새 Mac 앱 소스의 요구사항 근거다. 기존 Linux 호스트의 활성화 파일·보호 서비스 승인 기록을 대체하거나 실제 설치 완료를 주장하는 문서가 아니다.

## 구현 범위

`engineering/mac_app`은 사용자 소유 Mac에서 실행하는 추가 draft-delivery 모드다. 기존 핵심 원칙인 한 작업 소유자, 개발과 독립 검토의 분리, 정확한 HEAD의 증거, 미확정 실행 중복 금지를 적용한다. 기존 Linux 전용 privilege boundary를 macOS에서 흉내 내거나 보호된 Fable PASS를 합성하지 않는다. 이 앱의 모델 결과는 **Mac 앱의 작업 검증 기록**이다.

이번 사용자 요구에 맞추어 이 앱의 작업 실행 루프와 역할별 모델 설정을 구현한다. 기존 `AGENTS.md`의 고정 모델·루틴·기계적 lane 배정 계약이 적용되는 **레거시 프로그램 모드**는 변경하지 않는다. 레거시 실행에 사용되는 `control_plane_program.py`, `control_plane_host.py`, 활성화 기록 및 호스트 정책은 그대로다. 새 모드 도입·기존 호스트 소유권 이관은 독립적인 정확한 소스 검토와 실제 Mac 설치 확인 뒤 수행해야 한다.

최초 버전은 한 서비스 프로세스와 한 활성 모델 세션을 사용한다. 단일 작업 안의 단계는 같은 체크아웃·브랜치에 누적한다. 여러 독립 작업을 제출하면 대기열에 넣는다. 알 수 없는 실행이 남으면 다른 작업에도 같은 실행 자원을 재할당하지 않는다.

## 공급자 확장 — 0.2

기존 `adapters/imported/astra-{devin,grok,glm}-supervisor`와 `adapters/cursor/astra-cursor-supervisor`, `docs/BUILDER_LANES.md`, 프로그램 배정·소유권 코드 및 중앙 프로젝트 등록을 확인했다. 기존 네 lane은 각각 Devin 로컬 CLI, xAI Grok Build CLI, OpenCode + Z.AI Coding Plan, Cursor CLI이다. Mac 앱에서도 이 구분을 유지하고 Codex·Claude와 함께 여섯 실행 도구를 제공한다. 이름이 비슷한 모델을 다른 공급자 자격으로 실행하지 않는다.

역할별 도구·모델 선택은 새 Mac 모드의 사용자 설정이다. 기존 Linux 프로그램의 `DEVIN → GROK_BUILD → GLM → CURSOR` 순서, 활성화 파일, 보호된 lane 정책을 변경하지 않는다. 기존 CLI 명령 의미를 참고하되 Linux의 UID·`/proc`·`sudo`·host signer를 Mac에서 작동한다고 가정하지 않는다. Mac의 공통 worker가 CLI 하나를 소유하고 종료·결과를 기록한다.

Cursor에는 `ask` 모드, GLM에는 역할별 OpenCode 권한, Grok에는 읽기 전용 sandbox/도구 차단, Devin에는 역할별 config의 쓰기·exec 차단을 적용한다. 실제 CLI의 제한 강도는 동일하지 않다. GLM 권한은 도구 정책이며 OS sandbox가 아니다. 모든 읽기 역할은 CLI 성공·오류와 관계없이 HEAD/작업트리 변경을 검사하고 변경이 있으면 소유권을 해제하지 않는다. 개인 Mac 동일 사용자 신뢰와 실제 호스트 qualification 경계는 그대로다.

결과는 Cursor의 terminal 성공과 마지막 완료 assistant 메시지, OpenCode의 최종 `step_finish=stop` 메시지, Grok의 `end_turn` 및 정확한 세션 ID, Devin ATIF export의 마지막 agent 메시지에서만 읽는다. 로그 안의 임의 JSON·도구 출력·이전 단계·잘린 응답을 통과 결과로 읽지 않는다. 모든 결과는 공통 JSON 계약과 현재 HEAD 검증을 통과해야 한다. 새 worker 영수증에는 실제 실행 경로, 요청한 모델, 관측한 provider 세션 ID를 남긴다. 요청한 모델 이름을 실제 계정 모델 qualification 증거로 표시하지 않는다.

실행·권한·형식별 표와 실기기 확인 항목은 `mac_app/PROVIDERS_KO.md`를 참조한다.

## 자동 진행과 멈춤의 의미

1. 앱은 GitHub 레포의 현재 기본 브랜치를 전용 체크아웃·브랜치로 가져온다. 이 준비는 모델 실행을 승인하지 않는다. 매 모델 실행 직전에 기존 보호 호스트 관리 범위를 확인한다. 단순 `aiops-task` 라벨만 있고 canonical 표식이 없는 일반 레포는 새 Mac 작업을 진행할 수 있다. 등록된 보호 호스트 프로젝트, 고정된 프로그램 계획, canonical task/owner/launch 표식이 있는 레포는 `HOST_ADMISSION_REQUIRED`로 멈춘다. 이슈가 닫혔거나 실행이 끝났다는 GitHub 표시는 canonical 소유권 해제 증거가 아니다.
2. 계획 모델은 문서를 읽고 source path, 의존성 순서, acceptance가 있는 계획을 반환한다. source blob은 시작 SHA에 고정한다. 기존 승인 계약 안의 계획은 사용자에게 재승인을 묻지 않고 진행한다.
3. 개발 모델만 코드를 수정한다. 단계마다 로컬 커밋을 남긴다. 정책·AGENTS·결정 경로와 자격증명 파일 변경은 자동 커밋하지 않는다.
4. 새 감사 세션이 전체 diff와 근거를 확인한다. 새 감리 세션이 원래 산출물과 사용자 목표까지 확인한다. 두 결과는 현재 HEAD와 각각의 실행 ID에 묶인다. 미해결 finding, 누락 task, 빈 검증 근거, 다른 HEAD의 결과는 통과가 아니다.
5. 실패하면 같은 개발 작업으로 돌아간다. 새 커밋이 생기면 이전 검토 결과를 제거하고 다시 검토한다. 최신 기본 브랜치 통합이 필요해도 같은 수정 흐름을 이용한다.
6. 앱이 GitHub에 draft PR을 게시하고 관측된 CI를 확인한다. 중앙 등록 레포의 `program_required_checks` 누락은 대기, 필수 검사 skip/neutral은 실패다. 실패는 개발자로 돌아간다.
7. `ready`는 **최종 사용자 검수 준비**, `accepted`는 **표시된 HEAD의 사용자 검수 확인**이다. 병합·운영 배포·릴리스 승인·레거시 A3 충족을 뜻하지 않는다. 제품의 별도 릴리스 계약은 계속 적용된다.

실제 provider의 로그인·quota·네트워크는 무한하지 않다. provider 오류는 같은 모델에서 대기 후 다시 시도하며, 앱이 모델·계정을 임의 교체하거나 paid fallback을 사용하지 않는다. 세션 시간 제한은 runaway 실행 경계이며 선택적 전체 호출 한도는 사용자가 설정한다. 해결되지 않은 실행, 사라진 프로세스의 미확정 결과, sandbox 불가, 계정 권한 및 중요한 계약 변경은 구체적인 확인이 필요하다. 이를 무조건 성공·재실행으로 바꾸는 기능은 없다.

## 실행 기록과 인터페이스

- `request_id`의 최초 예약과 레포 소유권 확인은 SQLite `BEGIN IMMEDIATE` 아래에서 수행한다.
- worker 실행 전 예약을 영속화한다. worker는 `O_EXCL`로 두 번째 시작을 차단한다.
- 종료 영수증은 실행 ID와 입력 binding을 포함해 원자적으로 기록한다. 창이나 서비스가 다시 열리면 기존 영수증을 사용한다. 없으면 새 writer를 시작하지 않는다.
- `launchd`는 사용자 로그인 세션에서 서비스를 관리한다. UI 종료와 작업 종료는 별개다.
- HTTP는 loopback만 허용하며 Host/Origin 검사, 인증, 동일 출처 세션 쿠키와 제한된 정적 asset 경로를 사용한다. raw 셸·임의 파일 읽기 API는 없다.
- 봇 토큰은 시작·읽기·일시정지에 사용한다. 앱에서만 모델 설정, 질문 답변, 검수 승인·수정 요청을 처리한다.
- CLI에는 계정의 기존 OAuth/keychain/도구 자체 인증 저장소를 사용한다. GLM의 Z.AI Coding Plan 키도 OpenCode 인증 저장소에서 사용한다. provider 자식 환경에 GitHub 토큰이나 API key를 넘기지 않는다. 이 설계는 **개인 Mac의 동일 사용자 계정**을 신뢰하며, 레거시 root-owned service와 동등한 적대적 다중 사용자 격리라고 주장하지 않는다.
- 작업 중 로컬 상태 화면을 갱신하는 동작은 모델을 다시 호출하지 않는다. PR 게시 뒤에는 그 PR의 CI만 60초 간격으로 확인한다. 작업이 ready/paused/needs_user가 되면 새 모델 호출은 없다.

## 검증과 실제 설치의 경계

오프라인 검증은 정상 완료, 독립 감사/감리의 실패 후 수정, stale HEAD·불완전 검증 거절, 중복 요청, 동시 시작, 프로세스 결과 미확정, 재시작 영수증 소비, 모델 고정, 일시정지, 사용자 최종 검수, HTTP 인증/CSRF·경로 경계, 실제 로컬 Git 체크포인트를 포함한다.

설치 확인은 실제 Mac에서 해야 한다: 선택한 CLI의 버전·로그인·모델 사용 가능 여부, 쓰기/읽기 도구 제한, 실제 종료 코드/JSON 결과, launchd와 브라우저 열기, 기존 호스트 fencing·소유권 이관, 테스트 레포 한 건의 계획→수정→PR→감리→사용자 검수. Linux 테스트나 mocked provider 결과는 이 확인을 대신하지 않는다. 이 변경의 작성 세션은 독립 A3 감사자로 표시하지 않는다.
