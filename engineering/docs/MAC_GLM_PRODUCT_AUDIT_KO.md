# Mac GLM 제품 감사 생산자

사용자 결정6031603785는 기존 KIX job20531604498943e1 / PR121의 제품 감사에 실제
독립 `glm-5.3`을 추가로 허용한다. 원래 ASTRA_FABLE 생산자는 유지한다. 원문 질문·답변과
relay 출처는 `MAC_GLM_PRODUCT_AUDIT_DECISION_20261007.md` 및 RECORD.json에 보존한다.

## 실행 및 검증 계약

- 기존 canonical 감사 요청, requested_auditor 값 및 request digest는 바꾸지 않는다.
  별도 `MAC_GLM53` 실행·승인·댓글 기록을 기존 요청 SHA에 묶는다. 이 예외는 기존 KIX
  job/PR의 ARCHITECTURE/A3에만 적용하며 다른 제품·RELEASE를 자동 위임하지 않는다.
- 실행 전에 live 사용자 결정, 저장소 numeric ID/owner, 현재 PR/HEAD, 원래 작업 binding,
  private 독립 검토, 실제 Actions CI 및 로컬 clean HEAD를 검증한다.
- 지원되는 단발 Mac 감사 명령은 서비스 lock을 잡고 실제 Claude CLI의 기존 Z.AI 경로에서
  새 `glm-5.3` 세션을 실행한다. 원래 product builder를 호출하거나 제품 tree를 수정하지 않는다.
  모델에는 Git object에서 추출한 감사 범위 원문·diff·검증 근거만 전달한다. credential/runtime
  원장·원시 worker transcript는 전달하지 않는다. 모델 tools/hooks/MCP와 fallback은 차단한다.
- 실제 init/assistant/modelUsage의 모델·세션, 종료, 구조화 판정과 HEAD/gate/depth/request/
  packet hash를 검증한다. private 실행 증거와 계보별 감사자 독립성 없이는 PASS를 소비하지 않는다.
- 댓글 표지는 기존 Fable과 별개다. 본문 해시와 actor/시각/PR/HEAD를 live API로 재확인하며,
  모델이 출력한 형식·계정 댓글만으로 실행 증거를 대신하지 않는다. private 실행 증거 또는
  관측한 댓글의 변경·삭제, 같은 scope의 부정 판정은 fail-closed로 유지한다.
- 시작·publication 불명확 상태는 durable fence로 남겨 재실행/중복 게시를 하지 않는다.
  회복은 보존된 실제 실행·댓글을 재조회하는 지원 경로를 사용한다. 기존 원장 수기 수정,
  기존 부정 hold 해제 또는 다른 producer의 위장은 허용하지 않는다.
- 소스 exact-HEAD CI·독립 A3·병합/main CI·지원 업데이트 후 기존 제품 담당자가 감사 명령과
  원래 job 재개를 실행한다. 새 제품 builder는 금지하며 완료 영수증·29줄 결과·PR121을 보존한다.

Mac host 계정과 그 private 실행 파일은 신뢰 경계다. 새 서명키나 보호 Linux 증명을
주장하지 않는다. 기존 계정/credential/endpoint를 변경하지 않으며 Linux 보호 경로는 범위 밖이다.

## 지원 명령과 회복

기존 서비스가 정상 종료되고 진행 중인 provider가 없는 상태에서 기존 제품 담당자가
설치된 앱의 CLI로 `aiops.py audit 20531604498943e1`을 호출한다. `--data-dir`는 기존
AIOPS 디렉터리를 그대로 사용한다. 이 명령은 새 credential을 만들지 않으며 service lock을
얻지 못하면 실행하지 않는다. 검증·감사·댓글 게시·수집 후에도 제품 job을 자동 재개하지 않는다.
성공하면 기존 job의 정상 resume으로 최종 감리를 이어간다.

이미 게시된 동일 요청은 새 모델/댓글 없이 재검증한다. 실제 실행이 끝난 RESULT는 보존된
실행 증거로 게시할 수 있다. 게시 결과가 불명확한 PUBLISHING은 같은 명령에서 댓글을
읽어 정확히 하나의 일치만 회복하며, 0개/중복이면 재게시하지 않고 fence를 유지한다.
RUNNING/FAILED는 새 모델을 자동 호출하지 않는다. 실행 종료·증거가 불명확하면 차단
원인을 보존하여 기존 담당자에게 보고한다. 다른 요청으로 같은 HEAD의 fence를 우회할 수 없다.

실제 모델 입력은 고정 Git object의 AGENTS.md와 원래 .aiops/program.json, 전체 해당 diff,
검증한 원래 권한 수정 승인과 신규 생산자 승인, 필요한 요청·독립 검토·live CI 근거다.
저장소의 다른 자료집/검증 transaction 파일, runtime 원장 및 원시 worker 로그는 보내지 않는다.
기존 Z.AI route가 아니거나 요구 문맥을 제공할 수 없으면 실행/판정을 통과시키지 않는다.
