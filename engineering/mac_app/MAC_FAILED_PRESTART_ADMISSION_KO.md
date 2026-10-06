# 사용자 확인이 결합된 FAILED_PRESTART 비소유 판정

열린 canonical issue 자체는 실행 소유자가 아니다. 기존
`control_plane_program.writer_rows`와 `host_observation.assess`는
`FAILED_PRESTART` writer를 소유자로 집계하지 않는다. Mac generation의
열린 이슈 보류에도 이 의미를 적용하되, GitHub 상태 문구만으로는 해제하지 않는다.

[PR81 공식 A3 DECISION_REQUIRED](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016559549) 뒤
사용자는 [D-2026-10-06-MAC-FAILED-PRESTART-NONOWNERSHIP](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016578811)의
A로 구현된 예외를 승인했다. 이 결정은 [기존 ownership guard](../docs/MAC_OWNERSHIP_GUARD_KO.md)와
`AGENTS.md` §11에 반영한다. 제어·확인·결정 댓글의 정확한 API 본문 사본 및 직접
계산한 hash는 [인증 기록](../docs/MAC_FAILED_PRESTART_DECISION_20261006_RECORD.md)에 보존한다.

이번 사용자 승인 범위는 `BeautifulMind-JT/kix-protocol` #92의
`KIX-AGENTS-SCOPE-SYNC`, revision `pc85e614245eb-CURSOR`, request
`077849e0e68f521245e7175f`, CURSOR attempt 1에 한정한다.
정확한 [제어 댓글](https://github.com/BeautifulMind-JT/kix-protocol/issues/92#issuecomment-5956874897)과
[사용자 직접 확인 전달 댓글](https://github.com/BeautifulMind-JT/kix-protocol/issues/92#issuecomment-6016190250)을
인증된 GitHub GET으로 다시 읽는다. 코드에 고정한 comment ID, issue URL,
actor login/numeric ID/type, UTF-8 body SHA-256을 모두 검증한다.

전달 댓글의 원출처는 부모 대화 `01a0f57c-0df9-72ce-bb48-d1aaf753d004`의
사용자 메시지 `Sentinel_a77166906c0c819184c3400304e90d93`이다. Linux 원장과
복원 이후 구간을 직접 대조했다고 밝힌 주체는 사용자이며, Mac은 그 읽기를
수행하지 않았다. 저장되는 증거는 `USER_DIRECT_CHECK_RELAYED`,
`mac_verified_host_read=false`, `signed_host_receipt=false`로 구분한다.
본문 hash는 내용 불변성 검증이며 호스트 서명이나 기계 terminal receipt가 아니다.
지정 계정 토큰 보유자가 GitHub 댓글을 위조할 수 있다는 기존 신뢰 한계가 남는다.

Mac은 같은 issue/program/node/materialization과 task/revision/request/attempt를
확인하고, 유일한 canonical 제어 기록이 `FAILED_PRESTART`이며 owner lane과
session이 명시적으로 null일 때만 이 보류를 제거한다. 누락·변경·중복·외부 actor,
UNKNOWN/SUBMITTING/CONFIRMED, session/owner 존재, 다른 issue나 revision,
불완전한 task history는 계속 차단한다. 사용자 확인 댓글에 고정된 복원 대조
근거와 다른 내용은 수용하지 않는다. API 읽기는 기존 30초 budget을 공유한다.

검증 결과는 generation의 immutable 증거에 함께 저장되고 adoption,
fresh preflight, 기존 외부 claim 확인에서 재검증한다. 아직 admission되지 않아
거절된 동일 generation 요청은 근거가 충족된 뒤 정상 owner 경로에서 재시도할 수 있다.
기존 opaque receipt와 외부 기록은 보존하며 새 claim이나 receipt는 기존 fence를 유지한다.

이 수리는 Linux 원장·제어 댓글·소유자를 변경하거나 cross-host lock을 만들지 않는다.
소유권 해제, 제품 설치·실행·기존 intent 재개는 제품 owner가 수행한다.
Mac fixture 관찰은 계속 실행 권한이 없고 일반 native admission도 기존 보류를 유지한다.
generation의 격리 checkout, Draft PR, 사용자 merge, 정상 review/CI/Astra gate는 유지한다.

## F3: 저장소 잠금 중 GitHub 조회의 가용성 한계

`mac_authority.LocalSource.check_external`은 `Store.lock` RLock 안에서
generation 외부 기록을 검사한다. `native_transfer.Controller.start`의 최초
외부 기록 검사와 `LocalSource.call`의 read/reserve/claim 검사에서는
`BEGIN IMMEDIATE` 쓰기 transaction도 열린 상태다. #92의 현재 issue와 전체
comment 페이지를 읽는 네트워크 I/O가 이 잠금/transaction 안에서 발생하므로,
GitHub가 느리면 다른 앱 thread와 DB writer가 기다릴 수 있다. HTTP GET이 실패하거나
예산을 넘으면 실행을 허용하지 않고 정상 예외·rollback 경로를 따른다.

각 `mac_prestart.verify`의 API 읽기는 30초 monotonic budget을 사용하고, 바깥
`LocalSource.call`·preflight 같은 budget이 있으면 그보다 남은 짧은 시간을 공유한다.
그러나 이것은 전체 `Store.lock` 점유 시간의 30초 보장이 아니다. RLock 취득은
시간 제한이 없고, 바깥 budget이 없는 여러 verification은 각 budget을 시작할 수
있으며, SQLite/로컬 작업 시간도 별도다. 외부 claim 반복이나 여러 독립 호출의
지연은 누적될 수 있다. 잠금 대기는 바깥 budget이 이미 시작된 경우에만 그 예산에
포함되며, budget 소진이 lock 취득 대기를 중단하지는 않는다.

이 F3 NOTE는 이번 변경에서 수리하지 않는다. 조회를 잠금 밖으로 옮기려면
예약·claim 및 worker 호출 직전의 현재 기록 재검증과, 그 사이 변경된 local
claim/receipt/binding을 serialized 상태에서 다시 검증하는 경계를 함께 다뤄야 한다.
증거 cache로 조회만 제거하면 준비 이후 owner/session/UNKNOWN 변화 감지를 약화할 수
있어 작은 문서·사본 수리로 처리하지 않았다. 기존 fresh 재검증과 충돌 보호를 유지한
상태의 정확한 가용성 한계이며, 이번 공식 재감사의 입력으로 남긴다.
