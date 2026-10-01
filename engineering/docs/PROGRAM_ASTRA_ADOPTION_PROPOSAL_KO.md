# Program Astra 실행 위임 — PA-1 채택 결정과 배포 조건

상태: **정책 채택 결정 기록 완료 / #47 재감사·설치·qualification·활성화 PENDING**. 기존 M1/M4/M5 기록은 보존하고 아래 별도 결정을 추가한다. 정책 선택은 설치 또는 운영 서비스 admission이 아니다.

## PA-1 채택 결정 — 2026-10-01 (A, Option C)

- 결정자: User 박준태.
- 결정 원문: 「대표님 결정(2026-10-01): Fable #46 감사의 질문에 **A, Option C 채택**. 결정 기록과 위치를 PROGRAM_ASTRA_ADOPTION_PROPOSAL_KO.md에 남긴다. 설치와 활성화는 아래 수정과 #47 재감사 뒤에 한다.」
- 결정 출처 및 durable pointer: [대표님 결정 원문 (2026-10-01)](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/47#issuecomment-5927605393). 원문은 A / Option C 채택에 위임 노드의 Fable PASS 후 자동 병합과 runner의 고정 root 명령 1개 예외가 포함됨을 명시한다. 계약 변경 YES·RELEASE·user_merge는 제외하며, 권한 사용은 설치·qualification·activation 뒤다. PA-1 bridge 설치 전에는 프로그램 모드를 켜지 않고 일반 재개도 이 bridge에 의존함을 수용한다. 이 절은 「수정 요청 3」의 직접 지시와 해당 원문 댓글을 연결하는 저장소 내 결정 기록이며, 감사 PASS나 실제 host qualification을 대신하지 않는다.
- 채택 범위: A 및 제한된 Option C의 정책 선택. 승인된 ordinary task 범위의 고정 감사·상담과 제한된 실행/병합 위임이며, 아래의 고정 root bridge 예외도 해당 경계 안에서만 적용한다. 계약 변경·RELEASE·User-only·승인 범위 밖의 판단은 계속 User 경로다.
- 선행 수정: F1은 protected receipt의 `contract_change == "NO"`를 요구하고 YES/누락을 USER_REQUIRED로 유지한다. F2는 설치 전 운영 경로인 M5 Slack 상담 요청을 보존한다. F3는 runner-root 예외를 일반 설치용 sudoers에서 분리하여 별도 채택 후보 파일로 둔다. F4~F9는 이 요청의 수정 범위 밖이다.
- 배포 조건: F1~F3 반영 → **중앙 #47 최종 exact HEAD 독립 재감사** → 승인된 버전의 설치와 실제 host qualification/보호된 service authorization → 별도 활성화. 이 절은 재감사 결과·설치 완료·qualification 증거·활성화 record를 대신하지 않는다. 현재는 어느 작업도 실행하지 않았다.
- 현재 운영: M5의 `ASTRA_CONSULT_REQUEST repo=<저장소> issue=<번호> comment=<질문 댓글 id>`를 Slack 결정 채널에 보내고 운영자가 고정 `aiops-fable consult`를 호출하는 경로가 유지된다. 후보 `astra-consult`는 본 채택 기록뿐 아니라 실제 설치·qualification·정상 authorization이 모두 확인된 뒤에만 쓴다.

## 문제와 수정 후보

[중앙 #44의 Fable 감사](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/44#issuecomment-5910103843)는 exact HEAD `3c7dd38d69d2992ed25fe97f39f6ed4a37360ea3`에 DECISION_REQUIRED/A3를 기록했다. F1–F4의 계약 변경 우회, runner root 권한 충돌, 운영 계약 불일치, RELEASE 위임 누락을 수정할 후보이며 이전 감사 결과를 새 HEAD에 전이하지 않는다.

후보의 범위는 승인된 ordinary task의 고정 감사·상담과 기존 scope 안의 자동 병합이다. `contract_change=YES`, RELEASE, User-only node, 새 권한/과금/계약/출시·위험 수용은 보호된 Fable PASS가 있어도 User 경로다. 계약 변경 YES hold는 새 승인 계획·task revision 및 새 independent review로 해소해야 하며 기존 리뷰를 덮어쓰거나 false로 바꾸지 않는다.

RELEASE는 A3 정규화 전에 자동 병합 위임을 거부하며 source gate를 보존한다. 요구된 A3 깊이는 유지하되 출시 gate를 ARCHITECTURE라는 이름으로 숨기지 않는다. 일반 Fable audit/consult를 User가 직접 운영 호스트에서 호출하는 기존 경로는 바꾸지 않는다.

## 채택한 권한 예외의 제한과 배포 조건

runner는 오직 `sudo -n /opt/aiops/bin/aiops-fable program` 고정 root bridge를 호출하는 후보 예외만 갖는다. bounded JSON stdin, 등록된 repository/profile, canonical host task/plan/writer/PR/head, 제한된 operation과 protected current activation/adoption/fingerprint를 확인한다. shell/임의 Python/임의 argv/환경 보존/root ledger reconcile/자격증명 읽기 권한을 주지 않는다. builder에는 이 예외가 없다.

원래 runner root 실행 금지의 예외에 대한 정책 선택은 위 PA-1 절에 기록했다. **[대표님 원문 durable pointer](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/47#issuecomment-5927605393), 이 절의 커밋된 기록, #47 최종 exact-HEAD 재감사, 실제 설치·qualification·protected authorization이 모두 필요**하다. 그 전에는 `.github/control-plane/sudoers-aiops-program-astra.candidate`의 예외를 설치하거나 자동 경로를 사용하지 않는다. 일반 설치용 sudoers 예시에는 이 runner-root 규칙을 넣지 않는다. 계정/서비스 설치는 실행하지 않았으며 기존 M5 운영자 경로를 유지한다.

## 설치 후에도 별도인 서비스 admission

root bridge는 outer workflow를 신뢰해 활성화를 생략하지 않는다. 설치된 activation을 직접 읽고 runtime_enabled와 activated runtime SHA를 확인하며, config 옆 실제 `program-astra-authorization.json`의 accepted commit과 fingerprint를 대조한다. fingerprint는 Fable wrapper, bridge/program/core, config/profiles/activation 및 권위 있는 policy 7개 파일의 protected 바이트를 포함한다. 정책 또는 활성화가 바뀌면 record가 stale이다.

record는 operator가 실제 exact-HEAD 독립 A3·사용자 결정·host qualification·정상 activation을 확인한 뒤 root-owned로 생성한다. JSON 필드를 채운 것만으로 실제 qualification을 만들지 않는다. repository의 `.example.json`은 PENDING 상태로 그대로 거부되며 실제 record·token·host 상태를 commit하지 않는다. scope/결과/모델/경로를 stdin으로 받아 권위를 만들지 않는다.

직접 program 호출, 누락/disabled/stale/PENDING record, activation 해제, 파일/정책 변경, 공격자가 만든 symlink/duplicate record는 GitHub 또는 모델 작업 전에 차단해야 한다. 원래 승인 main activation은 새로운 서비스 record를 대신하지 않는다. 현재 activation.json은 이 후보에서 그대로다.

## 잔여 위험과 미완료 항목

아래 잔여 위험은 정책 채택 후에도 남는다. 위 선택을 실제 설치·qualification의 완료 증거로 확대하지 않는다.

- M4의 단일 PAT를 가진 workflow 호출자가 후보 고정 root 프로세스를 시작할 수 있다. stdin 검증·root record·host binding으로 범위를 좁히지만 PAT 자체의 직접 GitHub 권한을 회수하지는 않는다.
- root bridge가 untrusted GitHub 자료를 읽고 모델을 호출한다. 기존 안전한 archive 추출, read-only 모델·비밀 필터·서로 다른 리뷰·scope receipt가 필요하며 prompt injection 가능성을 0으로 만들었다고 하지 않는다.
- runner timeout/cancel이 root audit의 종료를 증명하지 않는다. 요청은 계속 실행 중이거나 UNKNOWN이며 receipts/global model lock을 유지한다. 원본 audit를 3시간 전에 취소·재제출하지 않는다. runner 수와 task concurrency 대기는 운영 qualification에서 확인한다.
- `PROGRAM_FABLE_RECOVERY.md`의 보호된 journal 및 bounded operator terminal-failure settlement는 소스 후보로 구현했다. 실제 설치·qualification은 아직 없으며, 증거 없는 UNKNOWN에 대한 임의 override는 제공하지 않는다. 새 계획/HEAD/영수증 삭제를 reconciliation으로 사용하지 않는다. 실패는 계속 차단하고 수동 보고한다. 자동 복구를 포함한 운영 qualification은 [후속 설계](PROGRAM_EXECUTION_EVOLUTION_DESIGN_KO.md)의 reconcile 구현·독립 검증 전에는 완료로 표시할 수 없다.

코드의 fail-closed 수정과 문서 정리는 진행할 수 있다. 정책 채택은 위에 기록했지만 bootstrap 병합, 실호스트 설치·qualification·activation은 각각 실제 권한과 evidence를 갖춰야 하며 #47 최종 HEAD 재감사보다 앞서지 않는다. Codex self-check나 병렬 검토는 Fable gate나 host qualification을 대신하지 않는다.


Recovery source candidate update: `docs/PROGRAM_FABLE_RECOVERY.md` specifies the implemented protected admission journal and bounded terminal-failure reconciliation. Earlier statements that reconciliation is unimplemented describe the #46 checkpoint; it remains uninstalled/unqualified and grants no operator override for unproven UNKNOWN. Full host activation is still NOT_READY.
