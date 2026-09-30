# 프로그램 실행 고도화 — 구현 작업표 후보

상태: [설계 v1](PROGRAM_EXECUTION_EVOLUTION_DESIGN_KO.md)의 후속 구현 blueprint. 이 표는 실행 등록된 `.aiops/program.json`이 아니며 central project의 program eligibility나 sudo 권한을 만들지 않는다. 모든 작업은 승인 전 후보다. 실제 task envelope와 audit depth는 해당 구현 scope를 채택한 뒤 기존 coordinator 규칙으로 결정한다.

| ID | 선행 | 산출물과 범위 | 완료 검증 | 최소 검토 |
|---|---|---|---|---|
| CP-E01 typed-fable-failure | 중앙 #44 채택 | 지원 CLI adapter의 bool/error/event/run parsing과 versioned failure envelope. 기존 output/status 소비자 호환; raw error 보존 | 17:32 fixture, 누락/모순/중복 event, secret absence, unknown adapter 거절 | ARCHITECTURE/A3 |
| CP-E02 terminal-settlement | E01 | root-owned append-only failed-run settlement와 operator-only ERROR/UNKNOWN reconciliation. process/archive/comment ambiguity guard, ancestor unresolved fence; 실제 terminal/run/comment 증거 없이는 retry/resume settlement 금지 | crash/fsync/root permission/symlink, receipt 삭제·HEAD/plan 변경 우회 거절, 원래 admission/result 보존, operator reconciliation 권위와 ambiguous publish 차단 | ARCHITECTURE/A3 |
| CP-E03 quota-resume | E02 | UTC one-shot wake, CAS nonce, global-lock fresh preflight + one retry admission, queue fairness | duplicate/reboot/late wake/stale bindings/extra usage, RUNNING 미취소; no-cost host canary | ARCHITECTURE/A3 |
| CP-E04 completion-query | 중앙 #44 채택 | 기존 host pins/helper에 definition/task revision/default target/merge ancestry 검증 wrapper를 더한 공유 authority와 read-only program-state. 네 축, active/pending/candidate scope 분모 | closed issue/old HEAD/changed same-key definition/wrong-base manual merge 거절, unchanged historical completion 보존, pending 누락 방지, fake/live qualification 분리 | ARCHITECTURE/A3 |
| CP-E05 external-evidence | E04 | 중앙 보호 ledger 기반 upstream immutable tuple와 artifact selector verifier. 현재 capability qualification validity/revocation과 historical artifact 완료 분리. 지원 안 하는 host 차단 | forged comment, mismatched tuple/version/hash, unavailable/expired/revoked/device-changed qualification, host pin loss; historical DONE 보존 | ARCHITECTURE/A3 |
| CP-E06 combined-dag-proposal | E05 | local+external DAG validator와 deterministic proposal-only pending migration/CAS | cross-repo cycle, stale plan, scope/depth/lock alteration 거절, duplicate proposal | ARCHITECTURE/A3 |
| CP-E07 board-and-playbook | E03,E04,E06 | 실제 qualified API를 coordinator/board/playbook에 연결. issue heuristic 제거, quota/upstream 이유와 evidence 표시 | board와 gate 동일 state version, inactive API 호출 없음, full-target summary | ARCHITECTURE/A3 |
| CP-E08 host-qualification | E07 | 설치된 exact audited commit/hash, no-cost 두 저장소 fixture, runtime attestation/activation evidence. 기존 operator/User 권한 준수 | 실제 journal/dedupe/secret/global lock/proposal/no-dispatch/rollback, gate 불충족 시 activation 차단 | ARCHITECTURE/A3 + 별도 milestone 수용/User 채택 |

구현 PR은 review 가능한 크기로 나눌 수 있지만 required depth와 권한 경계는 낮추지 않는다. E03와 E04는 E02/E01 완료와 별도로 병렬 개발할 수 있다. E05는 artifact identity를 만들어낼 제품 작업이 아직 완료되지 않았으면 fake fixture만 통과한 상태를 별도로 적고 실제 upstream qualification을 주장하지 않는다.

공통 실패 처리: UNKNOWN과 unresolved admitted ERROR를 지워 재시작하지 않는다. 최종 User 결정·계정·billing·credential·chain/live funds/release scope는 기존 승인 경로다. 모델 한도나 서비스 부재는 BLOCKED/WAITING의 실제 이유로 남긴다.

채택 기록에 들어갈 항목: source/design HEAD, exact implementation HEAD, 독립 Fable gate/depth/receipt, User approval pointer, 필요한 CI/비작성자 리뷰, installed hashes와 실제 canary 결과, runtime attestation/activation version. 작성자의 문서 검사나 이전 #44의 122 tests PASS를 이 신규 구현의 검사로 보고하지 않는다.
