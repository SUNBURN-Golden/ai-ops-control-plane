# 사용자가 승인한 별도 Mac 실행 세대

0.3.5는 owner 인증을 받은 `host generation` 명령으로 원래 프로그램의 단일 의존성 없는 노드를 새 Mac task와 revision에 고정한다. mode 초기화, generation 채택, start는 별도 동작이다. 아무 것도 자동 채택하거나 시작하지 않는다.

사용자는 기존 VM 기록을 보존하면서 새 Mac 작업의 별도 실행 기록·격리 브랜치·자동 병합 금지를 승인했다. 이 승인은 과거 실행 종료·소유권 이전·A3 감사 통과를 뜻하지 않는다. 기존 scope를 승계하는 `register` 경로와 UNKNOWN scope annotation의 보수적 검사는 유지한다.

`host generation`은 repository/node/generation-id/decision/plan-commit/plan-blob을 받는다. 앱이 GitHub의 원문 프로그램과 전체 task 투영을 직접 읽고 정확한 HEAD/blob을 확인한다. caller가 원문 spec·legacy receipt·병합 policy를 주입할 수 없다. 원문 기술 spec, gate, 전체 프로그램 맥락 및 과거 task/revision은 provenance로 보존하며 새 task ID에는 전체 generation UUID를 포함한다. 원문 의존성이 있거나 audit_floor=A3 또는 astra_gate가 NONE이 아닌 노드는 이 최소 경로로 채택할 수 없다.

불변 generation record는 승인 시점의 기존 외부 투영 및 opaque receipt의 정확한 origin digest/state를 새 격리 범위의 경계로 기록한다. 기존 원장·receipt·owner·UNKNOWN 상태를 수정하거나 종료로 선언하지 않는다. 새로 나타난 또는 변경된 관련 외부 claim/receipt는 실행을 막는다. 원래 경로에서는 기존 barrier가 계속 실행을 막는다. 같은 generation/start 요청의 재전송은 기존 기록을 읽으며 새 writer를 만들지 않는다.

실행은 기존 Mac controller/worker/pipeline를 사용한다. 전역 single writer index 두 개와 execution_busy는 그대로다. 새 request의 별도 checkout과 `aiops/native-<request-prefix>` 브랜치를 만들며 기존 task branch/checkout/session을 재사용하지 않는다. provider는 host 원장·토큰·receipt·제어 코드를 읽거나 쓸 수 없고, 기존 `~/Documents/Codex` 작업 경로에는 쓸 수 없다. provider의 자체 workspace sandbox도 유지한다. 이는 모든 같은 UID 앱을 격리하는 OS 경계의 주장이 아니다.

원문 astra_auto_merge 값은 provenance로 남긴다. 새 실행 policy는 auto_merge=false, draft_pr=true, user_only_merge=true다. 앱은 Draft PR만 만들며 ready 전환·자동 병합·main push·tag 조작·VM task materialization은 하지 않는다. 기존 ready 또는 auto-merge 설정 PR을 재사용하지 않는다. 실제 terminal/독립 review/supervisor/CI/사용자 검수와 실제 merge 후 ACCEPTED의 구분은 기존 pipeline대로 유지한다.

지원 설치는 `Install.command --update`다. 설치가 꺼진 상태를 보존하는 검사를 통과한 뒤 해당 소스를 적용한다. 실제 service/host/generation/start 명령 결과, provider session ID, 실제 코드·test·Draft PR 근거를 fixture 결과와 구분해 보고한다. 새 credentials·보안 권한이 필요하면 그 구체적인 단계에서 사용자에게 전달한다.

0.3.6은 최초 native 응답이 exit0/needs_user여도 질문을 그대로 보존하여 needs_user에서 기다린다. 자동 rework 호출이나 제품 구현 완료 집계를 하지 않는다. 호스트가 매 실행 직전에 원래 base와 plan blob을 검증하고 git fetch를 수행하며, 새 remote HEAD를 자동 merge/rebase/repin하지 않는다. provider의 Git metadata 쓰기 금지는 유지한다.

지원 업데이트는 별도 generation의 needs_user/paused 작업도 허용할 수 있다. 불변 원문 scope와 delivery/native binding, 모든 private attempt receipt, 실제 provider 및 wrapper 프로세스 그룹 종료가 검증된 경우에만 허용한다. 작업·실패·owner·UNKNOWN 상태를 바꾸지 않고 기존 실행 lineage를 보존한다. 일반 legacy 대기/paused/UNKNOWN이나 부족한 종료 증거는 계속 UPDATE_BUSY로 거부한다.

실제 최초 Codex 실행은 outer Seatbelt 안의 내부 sandbox 재초기화가 code71로 실패하여 구현 없이 needs_user에 도달했다. fixture PASS는 이 런타임 호환 문제가 해결됐다는 증명이 아니다. 외부 sandbox 단독 경로는 기존 filesystem/read-only/network/credentials 효과가 동등하게 OS에서 강제되는 근거 없이는 적용하지 않는다. --dangerously 계열 우회는 사용하지 않는다.

`external_candidate_command`는 production worker에 연결하지 않은 검증 후보다. deny-default 정책으로 배정 checkout과 플랫폼 실행 코드만 읽을 수 있고, builder만 checkout/전용 tmp에 쓸 수 있다. reviewer는 코드 쓰기와 command 네트워크를 허용하지 않는다. AIOPS 원장·토큰·request·receipt·running 정보와 제어 코드, 계정 루트와 Keychain은 허용하지 않는다.

공식 CLI의 무인증 오프라인 초기화에는 매 attempt마다 새 0700 `provider-runtime`이 필요했다. 후보가 선택적으로 허용하는 쓰기는 `state`(SQLite/wal/shm), `tmp`, `empty-codex/.tmp`(plugin lock), `empty-codex/tmp`(arg0), `empty-codex/skills/.system`(공식 바이너리 번들), 정확한 `empty-codex/installation_id`뿐이다. 빈 HOME/임시 CODEX_HOME을 쓰고 실제 인증·설정·사용자 skill을 복사하지 않는다. reviewer도 이 새 scratch에는 쓸 수 있으므로 이전의 모든 파일 쓰기 금지와 같은 효과라고 주장하지 않는다. scratch 내용은 신뢰할 수 없는 provider 상태이며 완료 영수증·검수 증거로 읽지 않는다. 신뢰되는 private receipt는 scratch 바깥에서 host만 작성한다.

16개 실제 Mac 합성 OS 검사와 공식 App Server의 initialize/initialized/command/exec 검사에서 코드 읽기 전용, 원장/영수증 접근 금지, 자식 프로세스·symlink·hardlink·경로 이탈 방지가 통과했다. 시작 전에 기존 runtime 재사용과 link/shared inode를 거부한다. 합성 fixture는 소유 process 종료 후 그 새 tree만 정리한다. production scratch 보존/삭제와 인증된 모델 transport는 아직 구현·검증하지 않았으며, 이 결과로 제품 실행을 재개하지 않는다. 실제 제품 실패 원본과 기존 UNKNOWN을 보존한다.
