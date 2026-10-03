# 병합 후 완료 판정 보완

## 발견한 문제와 결과

2026-10-03 UTC에 GitHub 실행 증거를 다시 확인했다. 기존
`control_plane_program.delivery_completion`은 KIX 이외 제품에서 전달 PR의
head, merge SHA, 기본 브랜치 계보만 맞으면 바로 `DONE`을 반환했다.
따라서 병합 후 push CI가 실패해도 의존 노드가 시작될 수 있었다.

실제 [Film PR #27](https://github.com/BeautifulMind-JT/film-unit-mv-studio/pull/27)의
merge SHA는 `820756643a512f052da158e4adc72c21a5e1a249`이다.
정확히 이 SHA의 [push run 37077500037](https://github.com/BeautifulMind-JT/film-unit-mv-studio/actions/runs/37077500037)에서
`Python 3.11 / FFmpeg`, `Python 3.12 / FFmpeg`가 모두 실패했다.
병합은 산출물의 기본 브랜치 편입이며, 제품 검증을 통과한 완료와 구분해야 한다.

이번 소스는 모든 제품에 보호된 병합 후 검증 profile을 요구한다.
profile이 없거나 모순되면 완료하지 않는다. GitHub 이슈의 종료,
댓글의 `DONE`, 이전 PR head의 성공은 이 검증을 대신하지 않는다.

## 보호된 제품 profile

설정은 중앙 runtime의 `.github/control-plane/projects.json`에 있다.
호출자는 체크 이름, workflow 경로 또는 KIX locked-blob 기준을 입력할 수 없다.
`program_post_merge_required_checks`는 필수 체크 전체 목록이고,
`program_post_merge_workflows`는 각 체크를 발생시켜야 할 정확한 workflow 경로다.
두 목록이 빠짐없이 일치해야 하며 같은 체크를 중복 소유하면 거절한다.
이번 profile 선정 범위는 아래 네 제품이다. 등록된 다른 제품인
MAEUM_GYEOL에는 검토된 병합 후 profile이 없으므로 `MERGED_POST_VERIFY`로
보류한다. 기존의 무조건 DONE 처리를 유지하거나 검사 이름을 추측하지 않는다.

| 제품 | 정확한 push workflow | 모든 필수 체크 |
|---|---|---|
| ZARI | `.github/workflows/app-ci.yml` | `bridge` |
| Film | `.github/workflows/ci.yml` | `Python 3.11 / FFmpeg`, `Python 3.12 / FFmpeg` |
| KIX | `.github/workflows/protocol.yml` | `protocol` |
| KIX Commerce | `.github/workflows/ci.yml` | `Typecheck, build, and test (stub)`, `Check for the protocol read token`, `Adapter tests against the reviewed gate` |

2026-10-03에 확인한 기본 브랜치는 모두 `main`이다. ZARI, KIX,
Commerce의 위 workflow는 main push를 받는다. Film은 모든 브랜치의 push를
받지만 완료 증거는 기본 브랜치의 정확한 merge SHA로 제한한다.
이 push trigger들에는 path filter가 없다.

확인한 기본 브랜치와 workflow blob:

| 제품 | 확인한 main SHA | push workflow blob SHA |
|---|---|---|
| ZARI | `84036631c945a59fee4de325409f835246536b96` | `d96589f083f88cc6a39d3ce8aeb0de8a44991eed` |
| Film | `820756643a512f052da158e4adc72c21a5e1a249` | `913391cd73d9151840802d87267251e5370d5aa7` |
| KIX | `b6373c2448ac947d245851abf7c4d9a00e70f5c9` | `dc3794f989ad5d9822d6cd913357ea44de643c11` |
| Commerce | `7c2452645c50b8ede17bdd762794adeabe42319e` | `bfe0dee9095962b5093b3c3c7f6172ac80e69756` |

이 표는 profile 선정의 검토 근거이며 실행 시점의 완료 receipt가 아니다.
완료 판정은 매번 현재 GitHub 상태를 다시 조회한다.

Film의 `desktop-apps.yml`과 KIX의 `ktx-kernel.yml`에는 push trigger가 없다.
이들을 merge SHA의 필수 push workflow로 만들지 않았다.
KIX의 병합 전 `protocol`, `kernel` 요구와 병합 후 `protocol` 요구는 유지한다.
KIX의 두 locked-blob 기준도 변경하지 않았다.
Commerce에서 token probe가 성공했어도 adapter job이 skip되면 완료하지 않는다.

## 증거 결속과 상태

완료에는 다음 사실이 모두 필요하다.

1. host가 고정한 전달 PR의 head가 실제 병합 head와 같다.
2. 전달 PR은 이 작업의 브랜치와 저장소에서 기본 브랜치를 대상으로 한다.
3. 정확한 merge SHA가 기본 브랜치 계보에 있다.
4. 모든 필수 체크가 merge SHA에서 `completed/success`다.
5. profile의 각 workflow에서 최신 실행이 정확한 기본 브랜치의 `push`이고
   repository/head repository/merge SHA가 일치한다.
6. 그 실행의 최신 attempt가 성공했고, attempt 전용 jobs API가 각 필수
   체크의 job을 반환한다. job의 run ID, attempt, SHA, 브랜치가 일치해야 한다.
7. job의 `check_run_url`이 가리키는 실제 GitHub Actions check run은 같은
   이름과 SHA, 실행의 check-suite ID를 갖고 `completed/success`다.
8. KIX는 보호된 locked-blob 기준과 실제 Git blob 내용도 일치한다.

workflow를 삭제하고 다시 만들면 `run_number`가 초기화될 수 있으므로
최신 workflow는 증가하는 run ID와 같은 실행의 attempt 순서로 고른다.
재실행이 진행 중이거나 실패하거나 skip되면 예전 성공으로 완료하지 않는다.
성공한 attempt라도 현재 attempt의 job/check ID가 없으면 보류한다.
matrix에서는 모든 필수 leg가 성공해야 한다.
이전 attempt의 job 조회 중 재실행이 시작될 수 있으므로, job을 읽은 뒤
최신 workflow 목록을 다시 읽어 run/attempt/출처/상태 결속이 같은지 확인한다.
완료 outcome이 누락되면 확인된 실패로 부르지 않고 보류한다.
잘못된 최신 workflow 식별자는 예전 성공으로 되돌아갈 이유가 되지 않는다.

| 계산 상태 | 의미와 기존 처리 |
|---|---|
| `DONE` | exact merge의 현재 필수 검증을 모두 충족. 의존 노드가 준비될 수 있다. |
| `MERGED_POST_VERIFY` | 검증 대기, 누락, 잘못된 출처, skip/neutral 또는 잘못된 profile. 원래 writer 재발송과 의존 노드 시작을 막는다. |
| `POST_MERGE_FAILED` | 현재 CI 실패 또는 KIX locked blob 불일치. 원래 writer와 의존 노드를 계속 막고 운영자 경로를 사용한다. |

결과의 `verification_runs`는 읽은 workflow/run/attempt/suite/check ID를
표시하므로 보류·실패 원인을 실행에 연결할 수 있다. 이 값은 신규 audit
PASS, host receipt 또는 수정 작업을 생성하지 않는다.
기존 `start`의 병합 작업 종료 처리와 `pending_dependencies`/merge-check는
동일한 계산을 사용한다. 이미 병합한 원래 writer를 다시 실행하지 않는다.
실패 후 이슈를 닫아도 그 사실만으로 후속 노드가 풀리지 않는다.
보호된 병합 후 실패/수정 작업 발행기는 이번 범위에 추가하지 않았다.

## 검증과 배포 한계

기존 88개 runtime 테스트를 유지하고, 실제 ledger를 사용하는 테스트에
일반 제품 완료, Film의 실패한 병합, matrix leg 누락/skip/neutral,
Commerce adapter skip, 잘못된 profile, 잘못된 SHA/event/branch/repository,
job/attempt/check-suite 출처, 최신 재실행, workflow 재생성,
의존성 보류와 병합 writer 재발송 금지를 추가했다.
새 테스트를 포함한 104개 runtime 테스트와 isolated CLI 진입·구문 검증이 통과했다.
GitHub에서 직접 읽은 Film PR #27의 exact-merge 체크와 push 실행을
읽기 전용 snapshot으로 재생했을 때 새 계산은 `POST_MERGE_FAILED`를 반환했다.
이는 GitHub 증거 재생이며 실제 host pin을 새로 검증하거나 런타임을 호출한 결과는 아니다.

이 변경은 소스 후보이다. `activation.json`과 live host, sudoers,
기존 작업·레인·board를 변경하지 않았다.
현재 감사된 runtime SHA와 다른 소스이므로, VM에서 적용하려면 기존
정확한 SHA의 독립 감사·설치/자격 검증·activation 재결속 절차를 완료해야 한다.
소스 PR의 테스트 성공은 그 배포 절차를 대신하지 않는다.
Mac 앱의 소스 설치와 VM runtime 활성화는 별개다.
