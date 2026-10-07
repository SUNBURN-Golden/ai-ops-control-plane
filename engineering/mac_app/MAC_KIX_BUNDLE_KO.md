# KIX 전용 신규 제품화 후보

이 Mac 경로는 기존 개발 노드 여러 개를 새 generation 하나, writer 하나, branch/PR 하나로 묶는다.
원본 program blob·각 노드 spec·내부 의존성은 보존한다. 기존 job/owner/receipt를 인수하거나
ACCEPTED로 바꾸지 않는다. Linux 호스트·보호 도구·credential은 변경하지 않는다.

## 제품별 future builder

owner CLI의 `host product-builder --input profile.json`에 다음을 전달한다.

```json
{"sunburn-golden/kix-protocol":{"provider":"codex","model":"gpt-6-astra","reasoning_effort":"high","service_tier":"fast"}}
```

전역 roles와 다른 제품은 그대로다. 신규 KIX 등록/generation에서만 builder 설정을 고정한다.
기존 실행의 설정·영수증은 소급 변경하지 않는다. 사용자 fast는 공식 CLI의 서비스 ID priority로
매핑한다. native 실행 전 model/list의 정확 model/high/priority 지원과 실제 config 값을 확인한다.
영수증의 requested 값과 private policy 근거는 요청·전달 증거이며 실제 과금이나 서버의 실행
모델을 증명한다는 뜻은 아니다. 지원되지 않으면 대체 모델/계정/서비스 없이 차단한다.

## 준비와 채택

`host bundle-prepare --input candidate.json`은 read-only 후보를 반환한다. 입력은 repository,
plan_commit, plan_blob, bundle이다. bundle은 id(`bundle-…`), title, 위상 순서의 nodes 배열이다.
정확한 live program에서 원본 명세를 가져오므로 입력으로 spec이나 dependency를 덮어쓸 수 없다.
원본 manifest의 이전 저장소 이름은 이 bundle reader에 한해 기존 live 동일 numeric ID/owner 및
이전 URL 승인 검증을 거쳐 읽는다. source_repository와 원래 bytes/blob를 보존하며 다른 제품과
일반 신규 admission의 이전 이름 금지는 유지한다. CANDIDATE_ONLY는 실행 승인이 아니다. 열린 원본 소유권, opaque UNKNOWN scope, 겹치는 Mac
작업, 미완료 외부 의존성을 blockers에 표시한다. 기존 원장을 변경하지 않는다.

채택은 기존 `host generation`의 정확한 plan pin·새 generation-id·기존 host decision에
`--node bundle-… --bundle bundle.json`을 더한다. 별도 start 명령 전까지 모델 실행은 없다.
채택과 start는 모든 원본 노드의 현재 소유권과 별도 사용자 결정6032181179의 정확한
계정·본문 hash·시각·PR를 live API로 다시 검사한다. 새로운 bundle 이름으로
UNKNOWN을 인정하거나 예외 처리하지 않는다. 원래 SDK 요청은 기존 담당자의 정상 소유권
확인 전까지 계속 차단된다. PR121/job20531604498943e1의29줄 결과는 이 묶음에 포함되지 않는다.

묶음 내부 의존성은 같은 writer가 순서대로 구현·검증한다. 묶음 밖 의존성은 기존 원본 revision의
정상 ACCEPTED 및 private/live 완료 근거를 요구한다. 순환, 중복, 누락, 거꾸로 된 순서는 거부한다.
채택의 transaction과 기존 전역 writer 예약을 유지하며, 단일 노드↔bundle 및 겹치는 bundle의
이중 채택도 양방향으로 막는다. 동일 입력 재전송은 기존 binding을 반환한다.

## 완료와 감사

builder의 complete에는 모든 원본 member ID를 순서대로 나열한 covered_tasks와 각 member의
`node:<id>:` 접두사를 가진 실제 검사 근거가 필요하다. 독립 reviewer와 supervisor에도 같은
범위 검사를 적용한다. 문자열 형식 검사는 검사 진실성을 증명하지 않으므로 실제 diff·실행된
테스트·독립 비작성자 검토·정확 HEAD CI·감사·User merge·post-merge 검증은 그대로 필수다.
needs_user, 일부 노드 실행, fixture 성공, 체크 목록만으로 제품 완료를 기록하지 않는다.

원본 각 gate를 보존하고 묶음은 최소 A3/ARCHITECTURE를 요구한다. 원본 중 RELEASE가 있으면
RELEASE를 유지한다. PR85의 MAC_GLM53 예외는 PR121의 원래 job에만 적용되므로 새 bundle은
그 예외를 상속하지 않는다. 새 bundle의 실제 제품 감사에는 기존 유효한 Fable 경로 또는 별도로
승인·구현된 생산자 범위가 필요하다. 이 소스 수리의 A3는 제품 감사를 대신하지 않는다.

정상 완료는 새 bundle 작업에만 귀속된다. 원래 8개 작업의 과거 상태를 수기로 바꾸거나 bundle의
결과를 각 과거 job의 완료로 투영하지 않는다. 후속 개별 노드의 의존성이 이 결과를 소비하는
경로는 아직 추가하지 않았으며 기존 ACCEPTED 원본 revision 경로로만 평가한다.

지원 업데이트 후 실제 product-builder 설정과 candidate 준비/채택/start는 부모·기존 제품 담당이
소유권 및 제품 감사 경로를 조율해 수행한다. 소스 테스트는 실제 제품 실행·설치 증거와 구분한다.
