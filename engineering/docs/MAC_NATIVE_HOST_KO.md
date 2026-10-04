# Mac 자체 AIOPS 호스트 — 소스 후보

2026-10-04 사용자는 개발 실행을 먼저 중지한 뒤 **Mac 자체를 AIOPS 호스트로 사용**하도록 지시했다. 새 Mac 작업의 원장·예약·실행·종료 관측은 이 Mac에서 수행한다. VM 전달이나 보호 호스트 이관 adapter의 가용성은 새 Mac 작업의 선행 조건이 아니다. 기존 외부 실행을 중지·종료·이관했다고 간주하는 결정은 포함하지 않는다.

이 변경은 수리용 소스 후보다. 설치, Mac 모드 초기화, 정확한 설치 SHA의 독립 검토·A3/권한 경계 검증, 실제 계정 호출과 실제 제품 세션은 각각 별도 근거가 필요하다. `supported`는 초기화된 로컬 adapter가 있다는 뜻이며 production qualification 또는 모든 작업의 실행 허가가 아니다. 자동 dispatch는 활성화하지 않는다.

## 원장과 시작 경로

기존 private `app.sqlite3`에 `mac_host_*` 테이블을 추가한다. 기존 jobs/events/settings, 토큰, relay 영수증을 수정하지 않는다. 서비스의 파일 잠금, SQLite `BEGIN IMMEDIATE`, 활성 예약의 unique index, 작업별 원래 binding을 함께 사용한다. 한 Mac에서 새로운 예약은 하나만 활성일 수 있다. 같은 request ID의 재전송은 상태 조회이며 새 worker를 시작하지 않는다. 다른 ID도 활성·UNKNOWN 예약을 넘어갈 수 없다.

`host initialize --mode MAC --decision <사용자 결정 포인터>`는 owner 전용 로컬 API다. relay token과 일반 모델 출력은 이 기능을 호출할 권한이 없다. 기존 일반 작업이나 예약이 남으면 초기화를 거절한다. 초기화는 mode와 Mac 원장 UUID를 기록하며, 이전 외부 호스트의 소유권을 강제로 가져오거나 종료 증거를 생성하지 않는다. 논리적인 `source_host=mac-…`, `target_host=mac-…-worker`는 같은 물리 Mac의 원장과 worker를 구분한다.

`host register --repo <owner/repo>`는 기존 인증된 GitHub 읽기 경로로 기본 브랜치의 정확한 HEAD, `.aiops/program.json` 원본과 Git blob, 전체 작업 등록을 읽는다. HTTP로 사용자가 만든 plan/history JSON을 주입할 수 없다. schema-v1 원래 명세·node·dependency·gate를 그대로 보관하며 planner가 새 계획을 만들어 대체하지 않는다. 전체 작업 ID와 frozen scope의 지원 한도를 넘으면 범위를 줄이지 않고 거절한다.

새 로컬 task의 canonical pointer는 `mac-host:<host-id>:<repo>:<task-id>`이다. `authority_kind=MAC_LOCAL`과 양의 **로컬 원장 행 ID**를 함께 묶는다. 이 ID를 GitHub 이슈 번호나 기존 VM의 materialization ID로 표시하지 않는다. GitHub 원본 계획의 승인 포인터와 기존 외부 이슈 ID는 provenance로 보존하며 원자적 예약으로 취급하지 않는다. 이 namespace 변경은 정확한 코드 SHA의 권한 경계 검토 대상이다.

`host start --repo … --task … --request-id <32자리 hex>`는 등록된 binding을 기계적으로 선택한다. 시작 전과 체크아웃 준비 후에 원래 계획·작업 등록을 다시 읽는다. HEAD/blob 변경이나 새 외부 claim은 시작을 차단한다. 계획과 profile은 자동 재지정하지 않는다. 의존 작업의 Mac 검수 기록과 원래 A3/Astra gate가 없으면 다음 작업을 시작하지 않는다. 선언된 A3/MILESTONE/ARCHITECTURE/RELEASE를 임의로 약화하지 않는다.

읽기 → 체크아웃 준비 → 로컬 예약 → claim → worker 시작 → 종료 관측 순서다. source 또는 worker 응답을 잃으면 `UNKNOWN`을 유지한다. 재시작은 새 시작을 허용하는 근거가 아니다. 나중에 같은 binding의 종료 영수증이 도착하면 종료만 관측할 수 있다. 종료 write 응답만 잃은 경우 `canonical reconcile`은 저장된 terminal을 읽어 대조하며 시작/예약을 재전송하지 않는다.

## 기존 실행과 UNKNOWN

GitHub의 canonical/불명확한 작업 투영은 기존 이슈 상태와 상관없이 외부 conflict barrier로 보관한다. 노드를 정확히 식별하면 해당 repo/task만 차단하며, 식별할 수 없는 등록은 해당 repo를 차단한다. 최신 읽기는 barrier를 추가할 수 있지만 삭제하거나 release할 수 없다. 이슈 closed, FAILED 텍스트, 이슈 삭제, 새로운 plan 또는 새 request ID는 기존 실행의 종료 증거가 아니다.

구형 `relay/requests.sqlite3`의 UNKNOWN/SUBMITTED는 원본에 repo/task binding이 없다. 연관성이 확인되지 않으면 새 Mac 시작도 보류한다. owner 전용 `host receipt-scope --association <JSON>`은 **원본 request ID와 정확한 payload SHA**에 repo/task 및 사용자 결정 근거를 연결하는 별도 불변 기록이다. 이 연관성은 실행 종료 또는 소유권 해제 증거가 아니다. 원본 영수증을 수정하지 않는다. 연관성이 같은 repo/task에 겹치면 계속 차단하고, 다른 작업이면 새 Mac 작업을 불필요하게 전역 차단하지 않는다. `task_id="*"`는 repo 전체를 보류한다. 단순 request 이름에서 임의의 연관성을 추정하지 않는다.

외부 실행의 종료를 검증하는 adapter는 이번 Mac 모드에 없다. 미확인 외부 실행은 해당 작업에서 계속 미확인으로 남는다. 별도의 Linux `native-transfer` fixed-operation 소스 후보는 이전 계약 준비 코드이며, Mac factory가 설치하거나 호출하지 않는다. VM 정책·인증·workflow를 변경하지 않는다.

## 실행 격리와 정상 중지

기존 native CLI 역할별 sandbox/tools 설정에 더해, Mac mode의 provider 프로세스와 후손은 `/usr/bin/sandbox-exec` 격리를 사용한다. host 원장·owner/relay 토큰·종료/제어 영수증·설치된 제어 모듈을 읽거나 쓰지 못한다. 해당 실행의 isolated checkout과 prompt/schema/adapter 출력의 이름별 예외만 둔다. 실제 Mac 검사에서 체크아웃 편집은 허용되고 host 파일의 직접 접근과 체크아웃의 symlink를 통한 접근은 거절되어야 한다. 이 격리가 없으면 제한을 제거하는 fallback 없이 실패한다.

trusted worker가 provider process/exit와 binding·quiescence 영수증을 작성한다. 모델의 PASS/JSON은 terminal 권한이 아니다. Mac 원장은 private 파일의 소유자·권한·하드링크·binding·digest를 확인하고, provider가 시작된 경우 저장된 process/exit와 실제 process group 부재를 함께 확인한다. 파일이나 관측이 부족하면 예약을 유지한다. 모델은 host API 인증 토큰을 전달받지 않는다. native CLI의 기존 계정 저장소와 같은 UID의 다른 신뢰된 사용자 프로세스는 별도 보안 경계이므로, 이 기능을 OS 전체 격리나 별도 OS 사용자 권한과 동일시하지 않는다.

`host stop <request-id>`는 binding이 고정된 private 중지 요청만 쓴다. 실제로 그 child를 소유한 살아 있는 worker가 정상 SIGTERM을 전달한다. 이전 영수증의 PID로 임의 프로세스를 종료하지 않는다. 요청만으로 실행을 terminal로 바꾸지 않으며 종료와 process group quiescence가 확인될 때까지 예약을 유지한다. worker를 잃었거나 중지를 확인하지 못하면 UNKNOWN을 보존한다.

## 완료 조건과 현재 한계

provider 시작 전의 확정된 실패는 같은 frozen task/profile의 수동 재시도만 허용한다. 실제 builder 종료는 `REVIEW_REQUIRED` 또는 `REWORK_REQUIRED`다. 종료는 검토·CI·Astra·사용자 검수·병합 완료가 아니다. 이 후보에는 builder 산출물을 기존 Mac 감사/감리·PR·CI loop로 넘기고 검수된 노드를 `ACCEPTED`로 전환하는 연결이 아직 없다. 따라서 dependent task를 자동으로 개발하거나 프로그램을 완료했다고 보고할 수 없다. owner가 임의 PASS를 적어 이 조건을 지우는 API도 제공하지 않는다.

설치/활성화 전에는 이 연결, 현재 코드의 실제 독립 검토, 필요한 A3/권한 경계 검증, 기존 실행의 충돌 확인을 마쳐야 한다. 제품 순서는 KIX → 커머스 → ZARI → Film이며 SoulBound·마음결은 제외한다. 같은 작업의 기존 실행이 미확인인 동안 제품 개발을 중복 시작하지 않는다.

업데이트는 기존 지원 `Install.command --update` 경로만 사용한다. 중지되어 있던 서비스와 로그인 자동 시작 비활성 설정을 유지하고 DB·토큰·기록을 보존한다. 기본 factory는 초기화되지 않은 Mac adapter로 시작하며 첫 설치/업데이트만으로 mode를 초기화하거나 제품 개발을 시작하지 않는다.

## 검증

`test_control_plane_mac_authority.py`는 임시 원장에서 명시적 mode, fresh Mac 예약, 동시/중복 시작, immutable scope association, 기존 claim 추가, 계획 변경, dependency/Astra 유지, private terminal proof 및 정상 중지의 상태 경계를 검사한다. macOS에서는 실제 Seatbelt 프로세스와 가짜 대기 child를 사용한다. fixture 결과는 실제 모델 세션이나 제품 개발 결과가 아니다.

전체 Mac 회귀와 optional protected-source 계약 테스트는 구분한다. Linux 보호 UID와 Python 버전을 전제로 하는 전체 control-plane 테스트를 Mac에서 실패한 결과는 성공으로 바꿔 표시하지 않는다. 이 문서는 runtime 로그나 리뷰 transcript를 포함하지 않는다. 실제 검토/CI/설치 결과는 정확한 SHA에 연결된 PR 및 별도 private 실행 근거로 확인한다.
