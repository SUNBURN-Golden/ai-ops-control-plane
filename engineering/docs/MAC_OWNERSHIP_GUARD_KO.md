# Mac 작업 이력과 실행 승인 구분 — 0.3.3 후보

**후속 Mac 자체 호스트 결정:** 2026-10-04 사용자는 Mac을 호스트로 사용하도록 지시했다. 아래는 기존 일반 작업 경로의 guard를 설명한다. 새 [Mac 자체 호스트 소스 후보](MAC_NATIVE_HOST_KO.md)는 새 Mac 작업에 VM 이관을 요구하지 않고 로컬 원장으로 예약한다. 기존 외부 claim/UNKNOWN은 해당 작업에서 계속 보존한다. 초기화·예약·종료와 같은 node의 감사/감리·CI·사용자 검수·실제 병합 관측 연결은 소스 후보이며, 최종 SHA의 독립 검토·권한 경계 감사·실제 계정 검증·설치 및 제품 실행은 별도 근거가 필요하다.

사용자는 2026-10-04 기존 작업 기록을 보존하면서 안전한 새 개발을 허용하도록 요청했다. 로컬 `accepted`·`cancelled` 작업은 이전 버전도 새 작업을 허용했다. 이번 수정은 그 동작을 회귀 검증하고, 종료 표시와 함께 미확정 attempt가 남은 예외는 차단한다. `unknown`, 실행 중, 일시정지, 사용자 확인 대기, 검수 준비 작업의 예약은 유지한다.

## 바뀐 경계

- 기존 `EXISTING_AIOPS_OWNER`는 열린 `aiops-task` 한 개를 소유권으로 단정하고 복제 준비부터 막았다. 이제 준비는 전용 폴더와 `aiops/mac-<job-id>` 브랜치에서 진행한다.
- 실행 직전에는 새 작업·재개 작업·모든 역할에 같은 검사를 적용한다. 준비된 체크아웃을 재사용해도 검사를 건너뛰지 않는다. 차단되면 provider 호출·실행 횟수 증가·worker 예약이 없다.
- 일반 Mac 작업은 모든 상태의 `aiops-task` 이슈와 댓글을 페이지 단위로 읽는다. 라벨만 있는 등록은 소유자라고 판단하지 않는다. 읽기 실패·불완전한 응답은 실행을 허용하지 않는다.
- `ASTRA_TASK_KEY_V1`, `ASTRA_CONTROL_RECORD_V1`, `TASK_ID`, `BUILDER_ID`, `LAUNCH_STATE`가 있는 기록은 canonical 범위로 취급한다. 종료 상태와 이슈 closed는 소유권 해제 근거가 아니다. 이 관측은 실제 활성 소유자가 있다는 주장이 아니다.
- 앱에 포함된 보호 호스트 프로젝트 등록, 시작 SHA의 원래 프로그램 계획, 기존 canonical 관측, 준비된 handoff도 실행 승인 필요 범위를 유지한다. 이전 Mac 작업 취소, 새 request ID, 이후 이슈 삭제로 이 범위를 지우지 않는다. 기존 작업·이벤트·실패·질문 기록을 변경하지 않는다. 백업 원장은 읽거나 신뢰하지 않는다.

## 남아 있는 계약

`HOST_ADMISSION_REQUIRED`는 보호 호스트의 권위 있는 실행 승인을 아직 확인할 수 없다는 뜻이다. 앱에는 이를 해결하는 인증된 호스트 조회·admission·이관 구현이 없다. KIX 등 canonical 프로그램의 실행을 허용하려면 다음을 별도 계약과 실제 호스트 근거로 확인해야 한다.

1. canonical task/control record 및 원래 계획의 정확한 revision·dependency·승인 범위.
2. 종료 뒤에도 유지되는 첫 소유자 lane과 전체 실행 계보.
3. 활성·UNKNOWN·미확정 세션의 실제 호스트 상태와 동일 작업 재개/이관의 원자적 승인.

일반 작업용 별도 브랜치만으로 이 조건을 대체하지 않는다. 아래 명시적 Mac 예외 밖에서는 프로세스 부재, GitHub 상태, 과거 백업, 사용자의 재시도 답변은 실행 권한 증거가 아니다. 일반 guard는 호스트 정책·원장·자격증명·보안 설정을 바꾸거나 기존 소유자를 해제하지 않는다.

## 사용자 승인 Mac FAILED_PRESTART 예외

[D-2026-10-06-MAC-FAILED-PRESTART-NONOWNERSHIP](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016578811)은
[PR81 공식 A3 DECISION_REQUIRED](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016559549)의
A를 승인했다. `authority_kind=MAC_LOCAL` generation의
`BeautifulMind-JT/kix-protocol#92` 열린 이슈 보류에 한정해, 정확한
FAILED_PRESTART 제어 기록과 사용자의 직접 확인 전달 댓글을 비소유 근거로 수용한다.
같은 `KIX-AGENTS-SCOPE-SYNC`, revision `pc85e614245eb-CURSOR`, request
`077849e0e68f521245e7175f`, CURSOR attempt 1, 명시적 null owner/session,
유일 control record 및 정확 program/node/materialization 결합을 유지한다.
UNKNOWN/SUBMITTING/CONFIRMED, 다른 issue/revision/request/attempt,
program/node 별칭, 누락·변경·충돌·불완전 기록은 계속 차단한다.

이 예외는 인증된 Mac의 Linux 원장 읽기 없이 지정 소유자 계정의 확인을 신뢰한다.
Linux를 직접 확인했다고 밝힌 주체는 사용자이며, 전달 원출처를 보존한다.
`USER_DIRECT_CHECK_RELAYED`, `mac_verified_host_read=false`,
`signed_host_receipt=false`는 기계 증명과 구분되는 증거 속성이다.
hash는 본문 불변성만 증명하며 계정 토큰 보유자는 댓글을 위조할 수 있다.
이는 사용자가 승인한 신뢰 한계이고 Linux admission 정책·원장·서명·감사 영수증을
변경하거나 cross-host 원자적 lock을 만들지 않는다. 일반 native guard와
다른 작업의 기존 canonical/opaque/UNKNOWN 보류는 그대로 유지한다.

세 댓글의 정확한 인증 API 본문 사본과 직접 계산한 hash는
[결정 인증 기록](MAC_FAILED_PRESTART_DECISION_20261006_RECORD.md)에 보존한다.
사본은 감사 입력이며 runtime의 현재 issue/control/confirmation 재검증을 대체하지 않는다.
자세한 [비소유 판정 및 F3 가용성 한계](../mac_app/MAC_FAILED_PRESTART_ADMISSION_KO.md)를 따른다.
정확 HEAD의 정상 review/CI/Astra gate와 User merge는 유지하며, 설치·소유권 조작·
제품 실행 및 기존 거절 intent 재개는 제품 owner가 수행한다.

## 설치

0.3.3은 로컬 소스 후보다. 게시 및 정확한 HEAD의 CI·독립 검토가 확인된 뒤 기존 지원 경로 `bash engineering/mac_app/Install.command --update`로 적용한다. 실행·미확정 작업이 있으면 업데이트를 위해 취소하지 않는다. 설치된 0.3.2와 작업 DB를 수동으로 덮어쓰지 않는다.
