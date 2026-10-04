# 사용자가 승인한 별도 Mac 실행 세대

0.3.5는 owner 인증을 받은 `host generation` 명령으로 원래 프로그램의 단일 의존성 없는 노드를 새 Mac task와 revision에 고정한다. mode 초기화, generation 채택, start는 별도 동작이다. 아무 것도 자동 채택하거나 시작하지 않는다.

사용자는 기존 VM 기록을 보존하면서 새 Mac 작업의 별도 실행 기록·격리 브랜치·자동 병합 금지를 승인했다. 이 승인은 과거 실행 종료·소유권 이전·A3 감사 통과를 뜻하지 않는다. 기존 scope를 승계하는 `register` 경로와 UNKNOWN scope annotation의 보수적 검사는 유지한다.

`host generation`은 repository/node/generation-id/decision/plan-commit/plan-blob을 받는다. 앱이 GitHub의 원문 프로그램과 전체 task 투영을 직접 읽고 정확한 HEAD/blob을 확인한다. caller가 원문 spec·legacy receipt·병합 policy를 주입할 수 없다. 원문 기술 spec, gate, 전체 프로그램 맥락 및 과거 task/revision은 provenance로 보존하며 새 task ID에는 전체 generation UUID를 포함한다. 원문 의존성이 있거나 audit_floor=A3 또는 astra_gate가 NONE이 아닌 노드는 이 최소 경로로 채택할 수 없다.

불변 generation record는 승인 시점의 기존 외부 투영 및 opaque receipt의 정확한 origin digest/state를 새 격리 범위의 경계로 기록한다. 기존 원장·receipt·owner·UNKNOWN 상태를 수정하거나 종료로 선언하지 않는다. 새로 나타난 또는 변경된 관련 외부 claim/receipt는 실행을 막는다. 원래 경로에서는 기존 barrier가 계속 실행을 막는다. 같은 generation/start 요청의 재전송은 기존 기록을 읽으며 새 writer를 만들지 않는다.

실행은 기존 Mac controller/worker/pipeline를 사용한다. 전역 single writer index 두 개와 execution_busy는 그대로다. 새 request의 별도 checkout과 `aiops/native-<request-prefix>` 브랜치를 만들며 기존 task branch/checkout/session을 재사용하지 않는다. provider는 host 원장·토큰·receipt·제어 코드를 읽거나 쓸 수 없고, 기존 `~/Documents/Codex` 작업 경로에는 쓸 수 없다. provider의 자체 workspace sandbox도 유지한다. 이는 모든 같은 UID 앱을 격리하는 OS 경계의 주장이 아니다.

원문 astra_auto_merge 값은 provenance로 남긴다. 새 실행 policy는 auto_merge=false, draft_pr=true, user_only_merge=true다. 앱은 Draft PR만 만들며 ready 전환·자동 병합·main push·tag 조작·VM task materialization은 하지 않는다. 기존 ready 또는 auto-merge 설정 PR을 재사용하지 않는다. 실제 terminal/독립 review/supervisor/CI/사용자 검수와 실제 merge 후 ACCEPTED의 구분은 기존 pipeline대로 유지한다.

지원 설치는 `Install.command --update`다. 설치가 꺼진 상태를 보존하는 검사를 통과한 뒤 해당 소스를 적용한다. 실제 service/host/generation/start 명령 결과, provider session ID, 실제 코드·test·Draft PR 근거를 fixture 결과와 구분해 보고한다. 새 credentials·보안 권한이 필요하면 그 구체적인 단계에서 사용자에게 전달한다.
