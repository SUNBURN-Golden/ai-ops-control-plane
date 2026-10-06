# Mac 자체 AIOPS 호스트 — 소스 후보

2026-10-04 사용자는 개발 실행을 먼저 중지한 뒤 **Mac 자체를 AIOPS 호스트로 사용**하도록 지시했다. 새 Mac 작업의 원장·예약·실행·종료 관측은 이 Mac에서 수행한다. VM 전달이나 보호 호스트 이관 adapter의 가용성은 새 Mac 작업의 선행 조건이 아니다. 기존 외부 실행을 중지·종료·이관했다고 간주하는 결정은 포함하지 않는다.

이 변경은 수리용 소스 후보다. 설치, Mac 모드 초기화, 정확한 설치 SHA의 독립 검토·A3/권한 경계 검증, 실제 계정 호출과 실제 제품 세션은 각각 별도 근거가 필요하다. `supported`는 초기화된 로컬 adapter가 있다는 뜻이며 production qualification 또는 모든 작업의 실행 허가가 아니다. 자동 dispatch는 활성화하지 않는다.

## 원장과 시작 경로

기존 private `app.sqlite3`에 `mac_host_*` 테이블을 추가한다. 기존 jobs/events/settings, 토큰, relay 영수증을 수정하지 않는다. 서비스의 파일 잠금, SQLite `BEGIN IMMEDIATE`, 활성 예약의 unique index, 작업별 원래 binding을 함께 사용한다. 한 Mac에서 새로운 예약은 하나만 활성일 수 있다. 같은 request ID의 재전송은 상태 조회이며 새 worker를 시작하지 않는다. 다른 ID도 활성·UNKNOWN 예약을 넘어갈 수 없다.

`host initialize --mode MAC --decision <사용자 결정 포인터>`는 owner 전용 로컬 API다. relay token과 일반 모델 출력은 이 기능을 호출할 권한이 없다. 기존 일반 작업이나 예약이 남으면 초기화를 거절한다. 초기화는 mode와 Mac 원장 UUID를 기록하며, 이전 외부 호스트의 소유권을 강제로 가져오거나 종료 증거를 생성하지 않는다. 논리적인 `source_host=mac-…`, `target_host=mac-…-worker`는 같은 물리 Mac의 원장과 worker를 구분한다.

`host register --repo <owner/repo>`는 기존 인증된 GitHub 읽기 경로로 기본 브랜치의 정확한 HEAD, `.aiops/program.json` 원본과 Git blob, 전체 작업 등록을 읽는다. HTTP로 사용자가 만든 plan/history JSON을 주입할 수 없다. schema-v1 원래 명세·node·dependency·gate를 그대로 보관하며 planner가 새 계획을 만들어 대체하지 않는다. 전체 작업 ID와 frozen scope의 지원 한도를 넘으면 범위를 줄이지 않고 거절한다.

새 로컬 task의 canonical pointer는 `mac-host:<host-id>:<repo>:<task-id>`이다. `authority_kind=MAC_LOCAL`과 양의 **로컬 원장 행 ID**를 함께 묶는다. 이 ID를 GitHub 이슈 번호나 기존 VM의 materialization ID로 표시하지 않는다. GitHub 원본 계획의 승인 포인터와 기존 외부 이슈 ID는 provenance로 보존하며 원자적 예약으로 취급하지 않는다. 이 namespace 변경은 정확한 코드 SHA의 권한 경계 검토 대상이다.

`host start --repo … --task … --request-id <32자리 hex>`는 등록된 binding을 기계적으로 선택한다. 시작 전과 체크아웃 준비 후에 원래 계획·작업 등록을 다시 읽는다. HEAD/blob 변경이나 새 외부 claim은 시작을 차단한다. 계획과 profile은 자동 재지정하지 않는다. 의존 작업의 검증된 Mac `ACCEPTED` 기록과 A3/Astra 전달 경로의 사용자 결정 검증이 없으면 다음 작업을 시작하지 않는다. 선언된 A3/MILESTONE/ARCHITECTURE/RELEASE를 임의로 약화하지 않는다. 아래 Mac-only 예외는 admission의 결정 검증과 후보 HEAD의 실제 감사 수용을 구분하며, 감사가 없으면 감리·검수·병합을 보류한다.

### 승인된 Mac-only A3 결과 전달 예외

사용자 결정 [D-2026-10-06-MAC-A3-RECEIPT](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/78#issuecomment-6011271646)는
`authority_kind=MAC_LOCAL`에 한해 인증된 GitHub 감사 댓글 경로를 승인했다.
댓글 ID `6011271646`, 작성자 `BeautifulMind-JT` / numeric ID `263336091`,
생성·수정 시각 `2026-10-06T07:10:27Z`, 본문 SHA-256
`46dc2cf0fdd02eeabbc2ac234ffcda0d410820f169d6e87f2f904d7ad939f132`다.
[정확한 결정 원문](MAC_A3_RECEIPT_DECISION_20261006.md)을 저장소에 보존한다.
사본은 감사 입력이며 런타임의 직접 인증 API 조회를 대체하지 않는다.

Linux root 소유 고정 `aiops-fable`이 감사하고 Mac은 원래 결정과 표시된 PR 댓글을
직접 다시 읽는다. 실제 actor·repo/PR/정확 delivery HEAD·PASS/PASS_WITH_NOTES·요구
depth·schema·불변 body hash를 모두 검증한 결과만 private task/revision/request에
귀속한다. 상세 재사용·삭제·상충·보류 조건은 [Mac receipt 계약](../mac_app/MAC_A3_RECEIPT_KO.md)을 따른다.

이 방식은 **운영자 계정의 댓글을 고정 감사 도구의 결과로 신뢰**한다. 해당 계정 token
보유자는 같은 형식의 댓글을 위조할 수 있으며 hash는 불변 내용의 증거이지 root 도구
작성의 암호학적 증명이 아니다. Mac이 Linux 보호 원장을 확인했다는 의미가 아니다.
이는 기존 사용자 결정의 한정된 신뢰 경계이며 새 key·credential·actor·공유 admission
authority를 승인하지 않는다. Linux host/Fable/hostpack/boundary/sudoers 및 Linux
program gate의 보호 receipt 계약은 그대로다. 정확 HEAD CI·독립 검토·감리·User merge와
RELEASE 예약도 유지한다. 이 문서 기록은 새로운 결정, 실제 설치 또는 감사 PASS가 아니다.

읽기 → 체크아웃 준비 → 로컬 예약 → claim → worker 시작 → 종료 관측 순서다. source 또는 worker 응답을 잃으면 `UNKNOWN`을 유지한다. 재시작은 새 시작을 허용하는 근거가 아니다. 나중에 같은 binding의 종료 영수증이 도착하면 종료만 관측할 수 있다. 종료 write 응답만 잃은 경우 `canonical reconcile`은 저장된 terminal을 읽어 대조하며 시작/예약을 재전송하지 않는다.

## 기존 실행과 UNKNOWN

GitHub의 canonical/불명확한 작업 투영은 기존 이슈 상태와 상관없이 외부 conflict barrier로 보관한다. 노드를 정확히 식별하면 해당 repo/task만 차단하며, 식별할 수 없는 등록은 해당 repo를 차단한다. 최신 읽기는 barrier를 추가할 수 있지만 삭제하거나 release할 수 없다. 이슈 closed, FAILED 텍스트, 이슈 삭제, 새로운 plan 또는 새 request ID는 기존 실행의 종료 증거가 아니다.

구형 `relay/requests.sqlite3`의 UNKNOWN/SUBMITTED는 원본에 repo/task binding이 없다. 연관성이 확인되지 않으면 새 Mac 시작도 보류한다. owner 전용 `host receipt-scope --association <JSON>`은 **원본 request ID와 정확한 payload SHA**에 repo/task 및 사용자 결정 근거를 연결하는 별도 불변 기록이다. 이 연관성은 실행 종료 또는 소유권 해제 증거가 아니다. 원본 영수증을 수정하지 않는다. 연관성이 같은 repo/task에 겹치면 계속 차단하고, 다른 작업이면 새 Mac 작업을 불필요하게 전역 차단하지 않는다. `task_id="*"`는 repo 전체를 보류한다. 단순 request 이름에서 임의의 연관성을 추정하지 않는다.

외부 실행의 종료를 검증하는 adapter는 이번 Mac 모드에 없다. 미확인 외부 실행은 해당 작업에서 계속 미확인으로 남는다. 별도의 Linux `native-transfer` fixed-operation 소스 후보는 이전 계약 준비 코드이며, Mac factory가 설치하거나 호출하지 않는다. VM 정책·인증·workflow를 변경하지 않는다.

## 실행 격리와 정상 중지

기존 native CLI 역할별 sandbox/tools 설정에 더해, Mac mode의 provider 프로세스와 후손은 `/usr/bin/sandbox-exec` 격리를 사용한다. host 원장·owner/relay 토큰·종료/제어 영수증·설치된 제어 모듈을 읽거나 쓰지 못한다. 해당 실행의 isolated checkout과 prompt/schema/adapter 출력의 이름별 예외만 둔다. builder는 체크아웃의 제품 파일을 편집할 수 있지만 `.git` 제어 메타데이터는 수정하지 못한다. 감사·감리는 같은 체크아웃을 읽기 전용으로 검사한다. 실제 Mac 검사에서 체크아웃 편집은 허용되고 host 파일의 직접 접근과 체크아웃의 symlink를 통한 접근은 거절되어야 한다. 이 격리가 없으면 제한을 제거하는 fallback 없이 실패한다.

trusted worker가 provider process/exit와 binding·quiescence 영수증을 작성한다. 모델의 PASS/JSON은 terminal 권한이 아니다. Mac 원장은 private 파일의 소유자·권한·하드링크·binding·digest를 확인하고, provider가 시작된 경우 저장된 process/exit와 실제 process group 부재를 함께 확인한다. 파일이나 관측이 부족하면 예약을 유지한다. 모델은 host API 인증 토큰을 전달받지 않는다. native CLI의 기존 계정 저장소와 같은 UID의 다른 신뢰된 사용자 프로세스는 별도 보안 경계이므로, 이 기능을 OS 전체 격리나 별도 OS 사용자 권한과 동일시하지 않는다.

`host stop <request-id>`는 binding이 고정된 private 중지 요청만 쓴다. 실제로 그 child를 소유한 살아 있는 worker가 정상 SIGTERM을 전달한다. 이전 영수증의 PID로 임의 프로세스를 종료하지 않는다. 요청만으로 실행을 terminal로 바꾸지 않으며 종료와 process group quiescence가 확인될 때까지 예약을 유지한다. worker를 잃었거나 중지를 확인하지 못하면 UNKNOWN을 보존한다.

## 같은 작업의 완료 경로

provider 시작 전의 확정된 실패는 같은 frozen task/profile의 수동 재시도만 허용한다. 실제 builder 종료는 `REVIEW_REQUIRED` 또는 `REWORK_REQUIRED`다. 종료는 검토·CI·Astra·사용자 검수·병합 완료가 아니다. source와 app의 terminal 기록 및 private worker 영수증이 모두 일치해야 `mac_pipeline`이 기존 Mac 완료 경로에 한 번 전달한다. 같은 request·attempt·binding·체크아웃·원래 node와 전체 명세를 유지하며 planner를 다시 호출하지 않는다.

기존 host checkpoint가 원격·브랜치·이력·권한 파일 변경을 검사하고 커밋한다. builder의 정상 산출물은 기존 감사→감리→draft PR→CI 경로로 이어진다. 일반 개발 실패는 같은 owner의 보완 단계로 돌아간다. `USER_STOPPED`는 일시정지를 유지하고 로그인·필수 권한 오류는 사용자 확인을 기다린다. private 종료 증거 또는 checkpoint 검증이 실패하면 `DELIVERY_BLOCKED`를 보존한다. owner의 `host retry-delivery --repo … --task …`는 같은 terminal 계보의 증거를 다시 확인하는 제한된 재시도이며 새 builder를 만들지 않는다.

사용자 검수 시 현재 HEAD·clean checkout·원래 계획 및 작업 충돌을 다시 확인한다. 감사와 감리 각각의 private request/receipt, 실제 CLI session ID, role/profile/binding, 현재 HEAD, 원래 node 전체 coverage가 일치해야 한다. 두 검토 session은 서로와 모든 builder session에서 독립이어야 한다. CLI가 session ID를 제공하지 못하면 검수를 통과하지 못한다. 모델 PASS나 HTTP로 보낸 review/CI JSON은 이 증거를 대체하지 않는다.

Mac node 검수의 CI는 인증된 GitHub API가 반환한 실제 `github-actions` check와 연결된 workflow run의 repo·정확한 HEAD를 확인한다. 필수 check 누락, 미완료·실패, 검증되지 않은 check 또는 불완전한 페이지는 통과하지 않는다. 변경 없는 산출물이나 PR을 게시하지 않는 설정은 이 경로의 완료 근거가 될 수 없다.

앱의 `검수 완료`는 사용자가 확인한 HEAD를 기록하며 Mac node는 **`INSPECTED`**가 된다. `host reconcile-accepted --repo … --task …`는 사용자 검수 증거를 다시 확인하고 실제 PR 병합, 기본 브랜치 이력에 포함된 merge commit, 해당 merge HEAD의 실제 Actions CI 통과를 읽기 전용으로 관측한 뒤에만 node를 **`ACCEPTED`**로 바꾼다. 이 명령은 PR을 병합하지 않는다. `INSPECTED`만으로 선행 node 의존성을 충족하지 않는다.

선행 변경이 병합되어 기본 HEAD가 달라졌으면 owner의 `host advance-base --repo … --decision …`가 인증된 최신 원본을 읽는다. 원래 program blob·node revision·owner·명세·dependencies가 같고 미확정 실행이나 미검수 산출물이 없어야 다음 `READY` node의 base만 갱신한다. 이전 binding과 계보는 보존한다. 계획 변경·owner 변경·기존 외부 claim 해제 기능은 아니다.

설치/활성화 전에는 현재 최종 코드 SHA의 실제 독립 검토, 필요한 A3/권한 경계 검증, 정상 시작 경로의 실제 계정 검증, 기존 실행의 충돌 확인을 마쳐야 한다. 이 소스 후보는 위 결정에 따른 Mac A3 receipt 검증 경로를 제공하지만, 이것만으로 설치·runtime qualification이나 자동 제품 dispatch를 활성화했다고 주장하지 않는다. 제품 순서는 KIX → 커머스 → ZARI → Film이며 SoulBound·마음결은 제외한다. 같은 작업의 기존 실행이 미확인인 동안 제품 개발을 중복 시작하지 않는다.

업데이트는 기존 지원 `Install.command --update` 경로만 사용한다. 중지되어 있던 서비스와 로그인 자동 시작 비활성 설정을 유지하고 DB·토큰·기록을 보존한다. 일반 job이 모두 종료되어도 native 예약·UNKNOWN·감사/검수/병합 대기 node가 있으면 교체를 거절한다. 기본 factory는 초기화되지 않은 Mac adapter로 시작하며 첫 설치/업데이트만으로 mode를 초기화하거나 제품 개발을 시작하지 않는다.

## 검증

`test_control_plane_mac_authority.py`는 임시 원장에서 명시적 mode, fresh Mac 예약, 동시/중복 시작, immutable scope association, 기존 claim 추가, 계획 변경, dependency/Astra 유지, private terminal proof 및 정상 중지의 상태 경계를 검사한다. macOS에서는 실제 Seatbelt 프로세스와 가짜 대기 child를 사용한다. fixture 결과는 실제 모델 세션이나 제품 개발 결과가 아니다.

`test_control_plane_mac_pipeline.py`는 같은 계보의 단일 전달, 보완/중지/인증 오류, private 검토 독립성, 현재 HEAD 변경, 기존 engine의 두 검토→PR→CI→사용자 검수, 병합 이후의 node gate, 다음 base 갱신, 업데이트 거절을 검사한다. 임시 DB와 실제 짧은 Python child의 종료 기록을 사용하고 모델·GitHub 응답은 명시적인 fixture다. 이 결과는 실제 공급자 검토나 GitHub CI의 성공 기록이 아니다.

전체 Mac 회귀와 optional protected-source 계약 테스트는 구분한다. Linux 보호 UID와 Python 버전을 전제로 하는 전체 control-plane 테스트를 Mac에서 실패한 결과는 성공으로 바꿔 표시하지 않는다. 이 문서는 runtime 로그나 리뷰 transcript를 포함하지 않는다. 실제 검토/CI/설치 결과는 정확한 SHA에 연결된 PR 및 별도 private 실행 근거로 확인한다.
