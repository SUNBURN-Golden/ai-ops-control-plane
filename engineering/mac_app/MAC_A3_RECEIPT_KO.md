# Mac A3 감사 댓글 전달 계약

근거는 사용자 결정 [D-2026-10-06-MAC-A3-RECEIPT](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/78#issuecomment-6011271646)다.
작성자는 `BeautifulMind-JT` / numeric ID `263336091`, 댓글 ID는 `6011271646`,
본문 SHA-256은 `46dc2cf0fdd02eeabbc2ac234ffcda0d410820f169d6e87f2f904d7ad939f132`다.
생성·수정 시각은 `2026-10-06T07:10:27Z`이며 [정확한 UTF-8 결정 원문](../docs/MAC_A3_RECEIPT_DECISION_20261006.md)을
감사 입력으로 보존한다. 이 사본은 런타임 수용 입력이 아니며 인증 API 재검증을 대체하지 않는다.
수정·삭제되거나 인증 조회가 실패한 결정은 새 admission과 감사 소비를 허용하지 않는다.

이 결정은 **Mac local 경로에 한정된 GitHub 감사 댓글 수용 예외**다. 기존 Linux
program bridge의 보호 원장·scope receipt 계약을 바꾸지 않는다. Linux의 root 소유
고정 `aiops-fable`이 감사하며 Mac은 모델을 감사자로 실행하거나 결과를 만들지 않는다.
기존 인증 GitHub CLI만 사용하고 새 키·credential·actor·sudoers 권한을 만들지 않는다.
승인된 작성자의 댓글이 고정 도구가 게시한 결과라는 신뢰는 사용자 결정의 경계이며,
Mac이 별도 서명이나 Linux 원장을 확인했다고 주장하지 않는다.

## 요청과 수용

1. A3 또는 명시된 Astra gate의 admission은 고정 사용자 결정 댓글을 실시간 검증한다.
   기존 단일 작업·owner·원래 plan/blob·dependencies·native terminal/private review
   검증은 유지한다. Admission은 감사 PASS나 완료를 뜻하지 않는다.
2. 독립 검토, Draft 게시와 정확 HEAD CI 후 앱이 private SQLite 원장에 감사 요청을
   먼저 보존한다. 요청에는 canonical task/revision, 전체 binding, 원래 plan commit/blob,
   native request/attempt, job/branch/PR/HEAD, writer sessions와 private review digest가
   들어간다. A3는 ARCHITECTURE로 승격하고 RELEASE 예약과 사용자 병합을 유지한다.
3. **이 요청이 기록된 다음에** 운영자가 Linux 고정 도구로 해당 PR/HEAD와 gate/depth를
   감사한다. 도구 호출·감사 수행은 이 앱이 자동으로 하거나 대체하지 않는다. 요청 이전에
   게시된 감사는 새 task revision으로 소급 승격할 수 없다.
4. 앱은 인증 API로 해당 PR 댓글 목록 전체와 선택한 댓글을 직접 다시 조회한다.
   `<!-- aiops-fable-audit -->`와 둘째 줄 `ASTRA_AUDIT_V1`의 schema, 작성자 login/id,
   댓글 ID/URL/issue URL의 repo/PR, 실제 remote PR HEAD/branch/repo,
   PASS/PASS_WITH_NOTES·요구 이상의 depth·실제 auditor/session과 본문 감사 필드의
   일치를 검증한다. 같은 gate이며 검증 깊이가 요구 이상인 감사만 판단한다. 다른 gate나
   낮은 검증 depth의 감사는 무시한다. 해당 범위의 FAIL/DECISION_REQUIRED와 contract YES는 거절한다.
   모델명·attempt 번호·merge-base 출력은 수용 조건이 아니다. 본문 result/depth/head의
   반복 표시는 있으면 header와 대조하며, actual auditor/session은 header로 검증한다.
5. 원장은 댓글 ID/URL/본문 SHA-256/생성·수정 시각 및 검증된 좁은 필드를 정확한 요청에
   한 번만 묶는다. 같은 댓글 또는 audit request/run ID는 다른 revision/job/native
   attempt/writer/review 요청에 재사용할 수 없다. 도구 run 시작도 요청 기록 이후여야
   한다. 후속 사용자 지시에 따라 시계 비교에 아래 30초 오차를 허용한다. 이미 사용한
   run/댓글을 다시 게시해 다른 요청으로 승격할 수 없다. raw 댓글·모델 transcript·
   비밀은 영수증에 저장하지 않는다.

## 재검증과 상태 전이

감리 시작, 감사 소비, 최종 inspect/accept, 사용자 Ready 전환과 병합 직전마다 결정,
직접 댓글 GET, 전체 댓글 목록, PR HEAD를 다시 조회한다. 기존 영수증은 같은 ID와
불변 본문 해시로 검증하며 새 댓글로 덮어쓰지 않는다. 수정·삭제·조회 실패·모호한
schema·상충 결과·stale HEAD가 있으면 같은 작업을 보류한다.

인증 조회로 확인한 같은 repo/PR/HEAD/gate의 요구 이상 depth인 부정적 결과·contract YES와
댓글 관측은 private 원장에 보존한다. 목록에서 그 ID의 소실 또는 검증된 수정·상충은 해당 범위의
지속 보류가 된다. 앱 재시작, 댓글 삭제·원상 복구, 새 revision/request로 해제되지 않는다.
새 HEAD에서 새 요청·감사를 하거나 이후 명시적 사용자 결정으로 처리해야 한다.
다른 gate와 낮은 depth의 감사는 관측·보류 범위에 넣지 않는다. 새 댓글의 필수 필드가
불명확하거나 지원하지 않는 출력 형식이면 현재 전이를 미확인으로 차단하지만 영구
hold를 쓰지 않는다. 이미 검증한 댓글의 본문 hash 변경·삭제와 실제 FAIL은 이 재시도
허용에 포함되지 않는다. 기존 비범위 보류 원장은 역사로 유지하고, 입증된 실패·편집은
원래 요청의 gate/depth로 이관해 보존한다. owner/예약/UNKNOWN 원장을 해제하지 않는다.
원장은 private host directory의 별도 `mac-astra.sqlite3`에 동기화해 기록한다. 의존 작업
read/reserve의 실패가 `app.sqlite3`의 owner/admission 트랜잭션을 롤백해도 감사 관측과
보류는 지워지지 않으며, 이를 보존하기 위해 바깥 reservation을 commit하지 않는다.

목록에는 고정 댓글이 남아 있는데 직접 GET이 404·인증·네트워크 오류로 실패하면
`MAC_HOST_ASTRA_RECEIPT_UNVERIFIED`로 현재 수용·시작·검수·병합을 차단한다.
이 조회 실패만으로 실제 삭제를 확정하거나 영구 hold를 쓰지는 않는다. 같은 댓글의
목록 소실·편집·불변 hash 변경이 확인되어야 지속 보류가 된다. 따라서 목록이 오래된
응답을 반환하는 동안의 직접 GET 실패와, 목록으로 확인된 삭제의 persistence는 다르다.
같은 ID/본문이 다시 인증 조회되고 부정 관측이 없으면 일시적인 조회 실패 후 재검증할 수 있다.

감리는 실제로 검증한 receipt를 host verification snapshot에 포함하며 private request
digest로 보존한다. 최종 검수는 현재 다시 검증한 receipt와 감리가 본 snapshot이
일치해야 한다. 사용자 검수 기록도 receipt digest에 묶인다. 새 HEAD/revision/rework
review는 새 요청과 새 감사를 요구한다. 댓글의 PASS는 supervisor 결과나 사용자 병합
명령을 대신하지 않는다. 기존 current-head CI·독립 reviewer·supervisor·명시적 사용자
approval·정확 HEAD merge 보호는 유지한다.

실패/미확인은 `MAC_HOST_ASTRA_*` 코드로 즉시 보류한다. caller supplied receipt/JSON,
붙여넣은 본문, 저장된 job의 PASS, model request identity는 수용 입력이 아니다.
예상 body hash가 바뀌면 자동 복구/재바인딩하지 않는다. 새 감사나 사용자 결정이 필요하다.

## 범위와 검증의 한계

요청 시각은 Mac wall clock, audit run ID 시각은 Linux clock, 댓글 생성 시각은 GitHub
clock이다. 후속 사용자 지시에 따라 `Mac request - 30초 <= Linux run <= GitHub comment + 30초`,
`GitHub comment >= Mac request - 30초`로 확인한다. 같은 초도 허용한다. 초과하는
오차는 거절하며 시계 동기화를 증명하지 않는다. 허용 창 안의 아직 소비하지 않은 이전
run을 실제 순서와 구별하지 못하는 한계는 남는다. 이미 소비한 댓글/run의 다른 요청
재사용은 시간 오차와 무관하게 거절한다. 현재 run ID의 UTC 시각 prefix는 ordering에
필요하며 이를 해석할 수 없는 새 형식은 영구 보류 대신 재시도 가능한 미확인이다.

운영자 계정이 감사 mark로 시작하는 댓글을 남겼지만 둘째 header를 해석할 수 없으면
어느 HEAD/gate의 감사인지 구분하지 못해 `MAC_HOST_ASTRA_RECEIPT_UNVERIFIED`로
현재 전이를 차단한다. 후속 사용자 지시에 따라 이런 미확인 새 댓글은 영구 hold를
쓰지 않는다. 정정된 새 댓글 또는 불명확한 댓글 제거 후 다시 검증할 수 있다. 입증된
관련 FAIL과 이미 검증한 댓글의 수정·삭제를 정정으로 간주해 해제하지 않는다.

A3 admission과 의존 작업 live 재검증은 일부 경로에서 `Store.lock`과 `app.sqlite3`의
`BEGIN IMMEDIATE`를 유지한 채 GitHub를 읽는다. Mac admission, 원장 operation,
감사·inspect 검증은 monotonic clock의 합산 30초 API 조회 예산을 공유한다. 중첩 호출은
예산을 갱신하지 않으며 각 gh GET에는 남은 시간만 timeout으로 넘긴다. 늦게 도착한
응답도 거부한다. timeout은 재시도 가능한 미확인이고 바깥 원장 transaction은 rollback한다.
잠금 대기도 예산을 소모하지만 Python 잠금·SQLite 대기를 강제 중단하거나 전체 operation의
wall time이 30초 이하라고 보장하지 않는다. 로컬 검증 시간도 예산에 포함되며 budget 밖
일반 호출의 기존 120초 timeout은 유지한다. owner 직렬화와 예약 정책은 그대로다.

변경은 Mac source/tests/docs다. `control_plane_host*`, hostpack, boundary, sudoers와
`aiops-fable`은 그대로다. 합성 GitHub 응답과 private fixture process/receipt로 admission,
감사 대기, 감리와 inspect·사용자 merge의 gate를 회귀검증한다. 이 테스트는 실제 Linux
감사 실행, Mac 설치, 인증 provider/제품/VM 실행의 qualification 증거가 아니다.
실제 설치·A3 실행·병합은 각 단계의 사용자 승인과 정확 증거를 별도로 확인한다.
