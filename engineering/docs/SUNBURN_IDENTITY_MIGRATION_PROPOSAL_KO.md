# SUNBURN 저장소 identity migration 후보

상태: EXACT-SCOPE USER APPROVAL RECEIVED / 독립 GLM5.3 A3 설계 분석 및 내구성 기록 진행 중. 아직 구현·운영 적용 전.
기준: ai-ops-control-plane main `77327d3a5f177aca81952ca8e24b5c695f086a23`.
이 문서와 후보 branch는 운영 적용·새 authority 예외·새 receipt producer·설치·제품 재개를 승인하지 않는다.

## 확인한 ID와 정확 전환 범위

개인 owner `BeautifulMind-JT` / ID263336091 → 기존 조직 `SUNBURN-Golden` / ID338877516.
개인 User actor BeautifulMind-JT/263336091/User는 그대로다.

| repo ID | 원래 full_name | 새 full_name |
| --- | --- | --- |
| 1373567344 | BeautifulMind-JT/ai-ops-control-plane | SUNBURN-Golden/ai-ops-control-plane |
| 1365416872 | BeautifulMind-JT/kix-protocol | SUNBURN-Golden/kix-protocol |
| 1373217962 | BeautifulMind-JT/ZARI | SUNBURN-Golden/ZARI |
| 1365377662 | BeautifulMind-JT/film-unit-mv-studio | SUNBURN-Golden/film-unit-mv-studio |
| 1388268331 | BeautifulMind-JT/kix-commerce-apps | SUNBURN-Golden/kix-commerce-apps |

다섯 이전 모두 직접 authenticated GET으로 같은 ID/name/새 owner/private/main을 확인했다. Commerce의 열린 PR23/25 HEAD, 보존한 22개 branch refs와 backup bundle SHA-256도 일치했다. 이 mapping은 전체 owner 치환이나 aiops-state·다른 제품 이전 범위 확대가 아니다. 기존 maeum-gyeol target과 개인 User actor는 새 조직 identity 전환 대상이 아니다.

## 일반 참조와 authority 계약을 구분한다

README의 현재 repo/clone 링크, 설명용 Homepage/User-Agent, UI/CLI 예시는 현재 full_name으로 갱신할 수 있다. 역사 User 결정·audit/activation pointer·과거 program/plan/receipt·개인 actor·locked blob은 provenance로 남긴다.

실행 registry/allowlist, API full_name 판정, pinned 승인 URL, canonical task identity, receipt binding, control owner/workflow boundary 변경은 authority 계약이다. 단순 문자열 치환으로 허용하지 않는다. `engineering/AGENTS.md` §8의 consequential 변경 절차(독립 Astra 분석 → User 결정 → durable task/revision → 적용)를 거친다. runtime은 계속 동결한다.

## 최소 설계: canonical provenance와 transport 전환을 분리

1. 원래 job ID·task revision·generation·plan commit/blob·spec hash·owner lane·request/attempt·원본 binding·receipt bytes/digest·UNKNOWN/hold·단일 writer는 바꾸지 않는다. 원래 receipt를 새 binding의 완료 증거로 복사하지 않는다.
2. 별도 migration authority record는 schema/version, 위 exact mapping, canonical subject(job/task/generation 등), base/source SHA, 실제 User decision ID/actor/body bytes/hash/created/updated를 결합한다. 원래 원장 row를 직접 rewrite하지 않고 승인된 operation만 새 append-only 전환 기록을 만든다. 이 operation은 아직 구현·채택되지 않았다.
3. 각 transport 동작은 기존 인증된 bounded API에서 repo ID·현재 full_name·owner ID·private를 다시 읽어 검증한다. caller JSON, redirect 자체, full_name 문자열이나 repo ID 단독으로 권한을 부여하지 않는다.
4. 역사 승인 URL은 원문 그대로 보존한다. 새 API의 동일 comment ID·issue/PR ID·동일 User actor·body bytes/hash·created/updated가 exact mapping의 같은 repo에 속할 때만 원래 결정과 새 transport의 연결을 검증한다. 어느 proof도 변경되면 기존 hold를 유지한다.
5. 기존 canonical record를 통한 재개는 승인된 전환 subject에만 적용한다. 새 owner에서 새 task를 생성할 때는 새 plan/task/generation scope를 정상 승인·검증하며 old-name alias가 새 admission을 만들지 않는다.
6. 이번 승인 stage는 다섯 exact repository identity에 한정한 registry/control_repository/target owner 검증, authenticated 동일 ID 확인과 append-only subject 연결을 준비한다. 기존 job/승인/receipt 원문과 원장은 rewrite하지 않는다. Mac receipt parser/새 producer, Linux 보호 파일/Fable 및 credential/권한 변경은 범위 밖이다. source CI 성공은 설치·host qualification·runtime activation을 의미하지 않는다.

## 반드시 보존할 KIX 현재 subject

- job `20531604498943e1`, branch `aiops/native-2972272cbeb64071`.
- 원래 plan commit `7481b0e16ce9b903abbffa62249bb91cd9e63cfe`, blob `ff0f39a8129ca8b8d30818cce35c3d4e588872fc`.
- spec SHA-256 `df27b538c6a1decf216505fe516cd906d31471289d45ca13d316b344b83fc579`.
- AGENTS.md +29/-0, after blob `190bcae4f0c7d60666b942dd7df9e87749a09297`.
- 완료 builder attempt `f10a3b1d95804b94b8eb8390fe98e290`, 원본 receipt bytes SHA-256 `37ec496a3d73967fe92108b8fa09fc37b3cb1bd57e5607ebc7c55d1a865f17cb`.
- #92 comment6018278031: User263336091/BeautifulMind-JT, 348 UTF-8 bytes, SHA-256 `000fa33005152da022795f01ef3ab91b3d4d085a81454b41907f8fd44488e951`, created=updated2026-10-06T14:18:45Z.

원래 native failed attempt와 이후 완료 builder를 혼동하지 않는다. imported UNRESOLVED hold20개는 남기며 기존 Linux 소유권/종료 증거를 발명하거나 해소하지 않는다.

## 검토·회귀 기준

동일 exact ID/name/owner/subject와 불변 proof만 허용한다. foreign ID·owner·fork·옛 namespace 재생성, comment 수정/삭제·actor/time/body 변경, replay, 다른 job/generation/task, 새 HEAD, provider/session 변경, live process, UNKNOWN/hold를 거절한다. 원본 DB와 proof는 hash 대조로 불변을 확인한다. 자동 merge·protected host·새 credential·새 Mac receipt producer는 이 설계로 허용하지 않는다.

source/current owner 갱신 후보는 최신 main과 통합된 exact HEAD에서 기존 root/engineering suite와 hosted source CI를 실제 수행하고 독립 검토를 받는다. consequential stage는 실제 A3 분석·정확 User 결정·해당 HEAD 감사·필요 host qualification 후에만 merge/설치/재개한다. skipped 또는 옛 green을 필수 check 성공으로 승계하지 않는다.

## 실제 action-time User 승인 범위

부모 스레드 `01a0f57c-0df9-72ce-bb48-d1aaf753d004`의 요청 `Sentinel_f7b674ba9a9c81919e38f187ce65c345`와 사용자 답변 `Sentinel_e7564964233081918ed2d78f908f3712`에 결합한다. 사용자 답변 원문은 `승인할게`다. 요청은 새 조직 ID338877516과 위 다섯 동일 repo ID의 registry/control_repository/target 검증 변경, 기존 작업·승인·receipt 보존, 다른 저장소·새 토큰 권한 제외, 기존 Z.AI glm-5.3에 관련 비공개 설계·소스를 전달하는 독립 A3 분석 및 지속 신뢰 설정임을 명시했다.

이 문서는 그 전달된 승인 범위를 기록한다. API에서 사용자가 직접 작성한 댓글 또는 signed host receipt로 위장하지 않는다. durable GitHub 기록의 실제 actor/comment ID/body bytes/hash/time과 독립 분석의 실제 model/session/evidence SHA를 별도로 확인한다. PR82의 감사 결과·지정은 승계하지 않는다.

## 승인 범위의 계약 요약

> D-SUNBURN-REPOSITORY-IDENTITY-MIGRATION: 위 다섯 exact repo ID에 한해 BeautifulMind-JT/263336091에서 SUNBURN-Golden/338877516으로 이전된 동일 repository의 transport를 사용한다. 기존 job/task/generation/plan/spec/owner/request/attempt/receipt·UNKNOWN/hold는 원래 canonical provenance 그대로 보존하고, 별도 append-only migration record와 인증된 현재 repo identity 및 불변 User proof로 같은 subject의 주소 전환만 검증한다. 다른 repo/owner/fork/recreated namespace와 새 admission·새 receipt producer·감사/merge/locked-blob 우회는 승인하지 않는다. 구현 범위와 독립 A3 분석/정확 후보 HEAD를 이 결정에 연결하며 Linux 보호 파일/Fable 및 계정·credential 변경은 별도 승인한다. runtime 재개는 후속 qualification과 repo별 User 결정까지 동결한다.

독립 A3가 승인된 범위 안에서 PASS/PASS_WITH_NOTES를 내기 전 구현하지 않는다. 분석이 다른 consequential contract를 요구하거나 실제 diff가 승인 범위를 넘으면 그 변경을 분리해 보고한다. 정확 HEAD의 재검토·CI와 후속 qualification 전까지 runtime을 동결한다. 예시 comment ID/hash 또는 설계 문서 사본을 live 승인 proof로 사용하지 않는다.
