# Program Astra 실행 위임 — 채택 결정 후보 PA-1

상태: **PENDING / 사용자 결정 미기록 / 설치·활성화 금지**. 2026-09-30 대표님의 작업 계속 지시에 따라 검토 가능한 수정안을 준비했다. 이 문서는 새로운 root 권한·병합 조건을 승인받았다는 기록이 아니다. 기존 M1/M4/M5 결정을 소급 변경하지 않는다.

## 문제와 수정 후보

[중앙 #44의 Fable 감사](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/44#issuecomment-5910103843)는 exact HEAD `3c7dd38d69d2992ed25fe97f39f6ed4a37360ea3`에 DECISION_REQUIRED/A3를 기록했다. F1–F4의 계약 변경 우회, runner root 권한 충돌, 운영 계약 불일치, RELEASE 위임 누락을 수정할 후보이며 이전 감사 결과를 새 HEAD에 전이하지 않는다.

후보의 범위는 승인된 ordinary task의 고정 감사·상담과 기존 scope 안의 자동 병합이다. `contract_change=YES`, RELEASE, User-only node, 새 권한/과금/계약/출시·위험 수용은 보호된 Fable PASS가 있어도 User 경로다. 계약 변경 YES hold는 새 승인 계획·task revision 및 새 independent review로 해소해야 하며 기존 리뷰를 덮어쓰거나 false로 바꾸지 않는다.

RELEASE는 A3 정규화 전에 자동 병합 위임을 거부하며 source gate를 보존한다. 요구된 A3 깊이는 유지하되 출시 gate를 ARCHITECTURE라는 이름으로 숨기지 않는다. 일반 Fable audit/consult를 User가 직접 운영 호스트에서 호출하는 기존 경로는 바꾸지 않는다.

## 명시적 채택에 포함될 권한 예외

runner는 오직 `sudo -n /opt/aiops/bin/aiops-fable program` 고정 root bridge를 호출하는 후보 예외만 갖는다. bounded JSON stdin, 등록된 repository/profile, canonical host task/plan/writer/PR/head, 제한된 operation과 protected current activation/adoption/fingerprint를 확인한다. shell/임의 Python/임의 argv/환경 보존/root ledger reconcile/자격증명 읽기 권한을 주지 않는다. builder에는 이 예외가 없다.

원래 runner root 실행 금지의 예외이므로 **대표님의 명시적 채택과 그 durable pointer가 필요**하다. 대안은 별도 비root 서비스와 protected ledger 접근 경계를 설계하는 것이다. 이 후보는 계정/서비스 설치를 실행하지 않으며 가장 작은 고정 bridge의 검토안을 제공한다. 사용자가 이 예외를 채택하지 않으면 자동 경로는 계속 비활성이고 기존 운영자 수동 경로를 사용한다.

## 설치 후에도 별도인 서비스 admission

root bridge는 outer workflow를 신뢰해 활성화를 생략하지 않는다. 설치된 activation을 직접 읽고 runtime_enabled와 activated runtime SHA를 확인하며, config 옆 실제 `program-astra-authorization.json`의 accepted commit과 fingerprint를 대조한다. fingerprint는 Fable wrapper, bridge/program/core, config/profiles/activation 및 권위 있는 policy 7개 파일의 protected 바이트를 포함한다. 정책 또는 활성화가 바뀌면 record가 stale이다.

record는 operator가 실제 exact-HEAD 독립 A3·사용자 결정·host qualification·정상 activation을 확인한 뒤 root-owned로 생성한다. JSON 필드를 채운 것만으로 실제 qualification을 만들지 않는다. repository의 `.example.json`은 PENDING 상태로 그대로 거부되며 실제 record·token·host 상태를 commit하지 않는다. scope/결과/모델/경로를 stdin으로 받아 권위를 만들지 않는다.

직접 program 호출, 누락/disabled/stale/PENDING record, activation 해제, 파일/정책 변경, 공격자가 만든 symlink/duplicate record는 GitHub 또는 모델 작업 전에 차단해야 한다. 원래 승인 main activation은 새로운 서비스 record를 대신하지 않는다. 현재 activation.json은 이 후보에서 그대로다.

## 잔여 위험과 미완료 항목

아래는 **채택 여부를 결정할 때 검토할 잔여 위험**이며 이미 수용됐다는 기록이 아니다.

- M4의 단일 PAT를 가진 workflow 호출자가 후보 고정 root 프로세스를 시작할 수 있다. stdin 검증·root record·host binding으로 범위를 좁히지만 PAT 자체의 직접 GitHub 권한을 회수하지는 않는다.
- root bridge가 untrusted GitHub 자료를 읽고 모델을 호출한다. 기존 안전한 archive 추출, read-only 모델·비밀 필터·서로 다른 리뷰·scope receipt가 필요하며 prompt injection 가능성을 0으로 만들었다고 하지 않는다.
- runner timeout/cancel이 root audit의 종료를 증명하지 않는다. 요청은 계속 실행 중이거나 UNKNOWN이며 receipts/global model lock을 유지한다. 원본 audit를 3시간 전에 취소·재제출하지 않는다. runner 수와 task concurrency 대기는 운영 qualification에서 확인한다.
- 현재 ERROR/UNKNOWN에 대한 보호된 operator-only append-only reconcile/settlement는 아직 구현되지 않았다. 새 계획/HEAD/영수증 삭제를 reconciliation으로 사용하지 않는다. 실패는 계속 차단하고 수동 보고한다. 자동 복구를 포함한 운영 qualification은 [후속 설계](PROGRAM_EXECUTION_EVOLUTION_DESIGN_KO.md)의 reconcile 구현·독립 검증 전에는 완료로 표시할 수 없다.

코드의 fail-closed 수정과 문서 후보 작성은 진행할 수 있다. 이 결정 후보의 채택, bootstrap 병합, 실호스트 설치·qualification·activation은 각각 실제 권한과 evidence를 갖춰야 한다. Codex self-check나 병렬 검토는 Fable gate나 대표님의 결정을 대신하지 않는다.
