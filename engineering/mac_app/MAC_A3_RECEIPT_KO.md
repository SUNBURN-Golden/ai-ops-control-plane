# Mac A3 감사 댓글 전달 계약

근거는 사용자 결정 [D-2026-10-06-MAC-A3-RECEIPT](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/78#issuecomment-6011271646)다.
작성자는 `BeautifulMind-JT` / numeric ID `263336091`, 댓글 ID는 `6011271646`,
본문 SHA-256은 `46dc2cf0fdd02eeabbc2ac234ffcda0d410820f169d6e87f2f904d7ad939f132`다.
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
   일치를 검증한다. Contract-change YES와 같은 HEAD의 상충 결과는 거절한다.
5. 원장은 댓글 ID/URL/본문 SHA-256/생성·수정 시각 및 검증된 좁은 필드를 정확한 요청에
   한 번만 묶는다. 같은 댓글 또는 audit request/run ID는 다른 revision/job/native
   attempt/writer/review 요청에 재사용할 수 없다. 도구 run 시작도 요청 기록 이후여야
   하므로 과거 본문을 새 댓글로 다시 게시해 승격할 수 없다. raw 댓글·모델 transcript·
   비밀은 영수증에 저장하지 않는다.

## 재검증과 상태 전이

감리 시작, 감사 소비, 최종 inspect/accept, 사용자 Ready 전환과 병합 직전마다 결정,
직접 댓글 GET, 전체 댓글 목록, PR HEAD를 다시 조회한다. 기존 영수증은 같은 ID와
불변 본문 해시로 검증하며 새 댓글로 덮어쓰지 않는다. 수정·삭제·조회 실패·모호한
schema·상충 결과·stale HEAD가 있으면 같은 작업을 보류한다.

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

변경은 Mac source/tests/docs다. `control_plane_host*`, hostpack, boundary, sudoers와
`aiops-fable`은 그대로다. 합성 GitHub 응답과 private fixture process/receipt로 admission,
감사 대기, 감리와 inspect·사용자 merge의 gate를 회귀검증한다. 이 테스트는 실제 Linux
감사 실행, Mac 설치, 인증 provider/제품/VM 실행의 qualification 증거가 아니다.
실제 설치·A3 실행·병합은 각 단계의 사용자 승인과 정확 증거를 별도로 확인한다.
