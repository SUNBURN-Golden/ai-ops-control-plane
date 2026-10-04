# Mac의 기존 프로그램 인계 준비

Mac 앱은 기존 `aiops-task` 등록이 있는 레포에 새 writer를 시작하지 않는다.
`handoff`는 이 상황에서 원래 프로그램과 작업 등록을 확인하고, 기존 로컬 실패
기록을 보존한 채 별도의 **인계 준비 기록**을 만드는 경로다. `prepared`는 실행
가능 상태가 아니다. 모든 결과는 `execution_allowed: false`이며 worker가
처리하는 `jobs`에 들어가지 않는다.

## 사용

새 코드가 포함된 앱 서비스가 실행 중일 때 앱의 `aiops.py`에 다음 명령을 전달한다.
기존 버전 서비스에는 이 API가 없다. 사용자 지정 데이터 경로는 `--data-dir 경로`를
`handoff` 앞에 지정한다.

```sh
python3 aiops.py handoff inspect --repo OWNER/REPOSITORY
python3 aiops.py handoff prepare --repo OWNER/REPOSITORY --request-id handoff-unique-request-001
python3 aiops.py handoff list
python3 aiops.py handoff status HANDOFF_ID
```

기존 Mac 작업과 연결하려면 `prepare`에 `--source-job JOB_ID`를 추가한다.
해당 레포에 속하는 기존 작업이어야 한다. 같은 레포의 다른 Mac 작업도 함께 관측한다.
같은 요청 ID와 같은 레포·원본 작업 조합으로 재시도하면 최초 기록을 반환하며 원격
상태를 다시 읽지 않는다. 새 관측은 `inspect` 또는 새 요청 ID의 `prepare`를 사용한다.
다른 조합에 같은 요청 ID를 재사용하면 `REQUEST_ID_CONFLICT`가 발생한다.

## 보관과 보존

- 조회 당시 기본 브랜치의 정확한 커밋과 `.aiops/program.json` Git blob을 고정한다.
  바이트로 blob을 검증한 뒤 기존 schema-v1 판독기를 적용한다. 원본 UTF-8 JSON
  전체를 그대로 저장하며 노드, 명세, 의존성, 감사·릴리스 조건을 변경하지 않는다.
  지원되지 않는 스키마는 오류로 종료한다.
- 열린 이슈와 닫힌 `aiops-task` 이슈의 모든 페이지에서 상태, 등록 포인터, 작업 키,
  본문 해시를 보관한다. 열린 등록의 중복·미확정 선언은 차단 사유이며 닫힌 과거
  등록의 선언은 `historical_notes`에 남긴다. GitHub 이슈 본문은 실행 권한이 아니다.
- 검증한 GitHub `full_name`의 대소문자를 저장하고 호스트 조회에도 사용한다.
  같은 요청 ID의 레포 비교는 대소문자를 구분하지 않으며 기존 기록을 변경하지 않는다.
- 같은 레포의 기존 Mac 작업 ID·상태·문서 해시, 전체 이벤트의 개수·해시를 기록한다.
  기존 작업·설정·계획·실패·시도·이벤트는 변경하지 않는다. 이 참조는 원래 DB 기록을
  대체하거나 별도 로그 백업을 생성하지 않는다.
- 관측 시점과 스냅샷 해시를 별도 `handoffs` 테이블에 원자적으로 저장한다.
  네트워크·검증 실패 때 부분 기록이나 새 작업을 만들지 않는다.

CLI/API/MCP는 원본 프로그램 텍스트를 제외한 요약을 반환한다. 원본은 앱의 비공개
SQLite DB에 보관된다. 여러 GitHub 조회와 로컬 조회는 전역 원자적 스냅샷이 아니며,
저장된 기록은 과거 관측이다. 최신 실행 권한으로 사용할 수 없다.

## 실행 차단

`registered_plan_projection`은 프로그램 노드와 연결되는 등록을 읽었다는 뜻이며,
owner 부재를 뜻하지 않는다. `BUILDER_ID`나 미확정 `LAUNCH_STATE` 선언, 잘못된
키, 중복 노드, 다른 프로그램 연결은 추가 차단 사유로 표시한다.

이 경로에는 보호 호스트의 권위 있는 조회·조정·admission 기능이 없다. 따라서
이슈가 없어도, 본문에 `RELEASED`가 있어도 `HOST_AUTHORITY_UNOBSERVED`와
`HANDOFF_ADMISSION_NOT_AVAILABLE`을 항상 유지한다. 로컬 `attempt` 또는
`unknown`은 `LOCAL_EXECUTION_UNRESOLVED`, 비종료 작업이나 진행 중 단계는
`LOCAL_JOB_NOT_TERMINAL`로 표시한다. 저장 직전 로컬 상태를 다시 관측하지만
해당 시도·소유권·예약을 해제하지 않는다.

`handoff execute/resume`은 없다. `force`, 사용자가 제출한 host receipt,
`execution_allowed` 요청 필드도 받지 않는다. 준비 기록 ID를 기존 작업의
`resume`에 전달해도 실행할 수 없다. 기존 `EXISTING_AIOPS_OWNER` 차단을 유지하며
인계 준비 기록 유무를 우회 조건으로 사용하지 않는다.

## 다음 운영 단계

운영자는 기존 보호 호스트의 공식 읽기 경로에서 program/node의 materialization,
task owner, launch request와 lane 상태를 비교해야 한다. GitHub 이슈, runner의
`busy: false`, 프로세스 부재만으로 활성·미확정 owner를 해제하지 않는다.

실제 writer 인계는 Mac과 기존 호스트가 같은 권위 있는 admission을 사용하고,
원래 owner의 종료·예약을 확인하며 두 writer를 동시에 승인하지 않는 지원된 전환
계약이 채택된 뒤 별도로 구현·검증해야 한다. 이 기능은 그 계약이나 protected A3,
완료·병합·릴리스 승인을 대신하지 않는다.

## API와 MCP

기존 loopback 인증·Host·Origin 경계를 적용한다. desktop과 relay 모두 조회·준비가
가능하며, 설정 변경이나 기존 작업 검수 권한은 늘리지 않는다.

| API | 동작 | MCP |
| --- | --- | --- |
| `POST /api/handoffs/inspect` | 레포 관측, 저장 없음 | `aiops_handoff_inspect` |
| `POST /api/handoffs` | 요청 ID별 준비 기록 저장 | `aiops_handoff_prepare` |
| `GET /api/handoffs` | 준비 기록 요약 목록 | `aiops_handoff_list` |
| `GET /api/handoffs/{id}` | 특정 기록 요약 | `aiops_handoff_status` |

## 검증과 적용

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s engineering/scripts -p 'test_control_plane_mac*.py'
```

테스트는 가짜 GitHub 응답, 임시 DB, 임시 localhost 서버를 사용하며 실제 보호
호스트 인계 검증이 아니다. 앱 갱신 전 비작성자 검토와 정확한 변경 버전의 검증을
수행하고 기존 설치 지침의 종료 상태 확인 및 `Install.command --update`를 따른다.
실행 중 앱에 일부 파일만 복사하여 적용하지 않는다.

## 보호 호스트 응답 대조와 기존 승인 경로 미리보기

```sh
python3 aiops.py handoff host-plan HANDOFF_ID
python3 aiops.py handoff check-fixture HANDOFF_ID --fixture host-observation.json
python3 aiops.py handoff preview-start HANDOFF_ID --fixture host-observation.json --node NODE --request-id request-001
```

`host-plan`은 고정 읽기 명령 목록을 반환한다. API는
`GET /api/handoffs/{id}/host-plan`, MCP는 `aiops_handoff_host_plan`이다.
실제 호스트 접속을 시작하지 않는다. 목록에는 프로그램 전체의 `materialize-list`,
각 노드의 `materialize-status`와 `task-status`, `status --lanes`가 들어간다.
관측한 모든 launch request에는 개별 `status --launch-request-id` 조회도 필요하다.
삭제된 이슈나 현재 계획 밖의 호스트 등록을 정상적인 owner 부재로 해석하지 않는다.

`host_observation.capture_fixture(record, read)`는 기존 v2 보호 호스트 공개 응답을
읽기 함수에 연결하는 개발용 어댑터다. 네트워크·SSH·토큰·sudo 실행 구현은 없으며,
연결이 없으면 `HOST_TRANSPORT_NOT_CONFIGURED`로 끝난다. 결과 envelope는 항상
`source: fixture`다. `check-fixture`는 로컬 파일을 판독하며 서비스 DB에 저장하지 않는다.
타입·조회 누락·중복·예상하지 않은 명령, 레포·program/node·issue·materialization
request 불일치와 호스트 조회 사이의 plan commit 불일치,
60초를 넘는 조회 구간/관측 노후화를 거부한다.
task 조회와 개별 launch·lane 조회가 달라졌으면 새 권한으로 간주하지 않고 보류한다.
각 노드의 이슈·materialization request가 호스트의 canonical 등록과 정확히 일치해야 한다.
닫힌 과거 이슈는 다른 열린 노드의 미리보기를 막지 않지만 선택해서 시작할 수 없다.
이슈 상태와 전체 이력 범위가 없는 이전 준비 기록은 `HANDOFF_TASK_HISTORY_INCOMPLETE`로
거부하며 새 요청 ID로 준비해야 한다. 저장된 원본 기록을 소급 수정하지 않는다.

호스트의 기록된 계획과 요청 계획이 다르면 두 커밋을 `required_plan_advances`에 표시한다.
미리보기는 계획 발전을 승인하지 않는다. 기존 `program.start`가 최신 계획 여부,
커밋 계보와 승인 조건을 검사하고 통과한 경우에만 호스트 등록을 갱신한다.

첫 writer의 lane은 종료 후에도 owner다. owner 변경 이력, 활성 writer/reviewer,
SUBMITTING·UNKNOWN, 확인되지 않은 종료, 로컬 미확정 상태는 보류한다.
호스트가 `FAILED_PRESTART`로 확정한 시도는 owner가 아니다. 실제 owner가 없고
마지막 실패 시도의 lane과 이슈 선언이 일치하는 경우 재시도 미리보기가 가능하지만,
이력 없는 선언이나 다른 lane 선언은 계속 보류한다. 실제 재시도는 기존 admission을 거친다.
`observations_consistent: true`도 실행 승인이나 보호 호스트 인증을 뜻하지 않는다.
모든 fixture 결과는 `execution_allowed: false`다.

`preview-start`는 일치하는 CREATED 노드와 열린 canonical 이슈에 한해 **기존 호스트에서 이어가기 위한**
relay 요청을 만든다. 직접 lane·모델·worker·launch packet을 지정하지 않는다.
이 요청의 `execution_host: current`는 기존 root workflow가 이미 지원하는 라우팅이다.
`engineering/mac_host/relay.py prepare`로 해당 요청을 추가 검증할 수 있다.
기존 relay의 기본값은 계속 `macbook`이며 그 모드는 정확한 runner 이름을 요구한다.
`current`에는 runner override를 넣을 수 없다.

실제 전송 단계에서는 기존 `control_plane_program.start`가 최신 프로그램·canonical
등록·의존성·기존 owner·의사결정·preflight를 다시 확인하고, 기존 호스트 원장이
원자적으로 승인한다. 로컬 관측 결과로 이 검사를 생략하지 않는다. 전달 응답 유실은
relay의 기존 UNKNOWN 보존과 동일 요청 비재전송 규칙을 따른다. Workflow 접수/성공은
task 승인·완료가 아니다. 이 경로는 Mac 네이티브 writer로 소유권을 옮기지 않는다.

현재 제공한 검증은 실제 program start/dispatch/finalize 로직과 **임시** 보호 원장,
가짜 GitHub·provider를 연결한다. 여러 클라이언트, 오래된 빈 관측, 동시 예약,
응답 유실, 원래 owner 재개를 검증한다. 실제 호스트 qualification 증거가 아니다.

운영 연결에는 정확한 보호 호스트/원장 식별, 인증된 고정 읽기 전송, 설치 helper의
버전·해시, 동일 원장을 사용하는 workflow runner의 qualification이 필요하다.
현재 `current` 라우팅을 보고 동일 호스트임을 추정하지 않는다. Mac 네이티브 writer
수용에는 기존 Linux UID·/proc 격리와 owner 유지 계약을 보존하는 별도 승인 설계 및
호스트 구현·검증이 필요하며, 여기서 policy나 보호 원장을 변경하지 않는다.
