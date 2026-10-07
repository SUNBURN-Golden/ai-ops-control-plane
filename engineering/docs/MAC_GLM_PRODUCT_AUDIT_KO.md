# Mac GLM 제품 감사 생산자

사용자 결정6031603785는 기존 KIX job20531604498943e1 / PR121의 제품 감사에 실제
독립 `glm-5.3`을 추가로 허용한다. 원래 ASTRA_FABLE 생산자는 유지한다. 원문 질문·답변과
relay 출처는 `MAC_GLM_PRODUCT_AUDIT_DECISION_20261007.md` 및 RECORD.json에 보존한다.

## 후보 구현 계약

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
