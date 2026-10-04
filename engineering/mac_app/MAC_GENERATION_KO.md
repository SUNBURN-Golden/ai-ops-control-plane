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

0.3.7의 Mac-local Codex worker는 별도 outer sandbox 대신 공식 App Server의 named permissionProfile을 사용한다. trusted adapter가 기존 CLI의 정상 계정을 이용하며 credential을 추출·복사·수정하거나 login/reset을 호출하지 않는다. :root deny + :minimal read, AIOPS 전체 상태/control code deny와 정확한 checkout 예외를 구성하고 Git/agent 설정은 read로 유지한다. builder만 checkout과 새 전용 tmp에 쓸 수 있다. builder command network는 기존 허용을 유지하며 다른 role은 command network를 허용하지 않는다. trusted server의 인증/model traffic은 command 권한과 분리된다.

실제 raw/effective profile 내용·list 허용·비활성 feature/MCP를 모델 호출 전에 검증한다. legacy sandbox 설정, 외부 hook/notify/MCP, profile/feature 불일치는 그대로 보존하고 fail-closed한다. thread/start와 turn/start 모두 정확한 profile/checkout/runtime roots/approvalPolicy=never와 원래 model/schema를 고정한다. fresh ephemeral thread의 실제 activePermissionProfile을 검증한다. 각 command의 session/cwd와 정책을 재검증하고 file-change의 role/path를 검사한다. 권한 확대·외부 callback·동적 도구·subagent·browser·web 사용은 거절하고 정상 중지한다.

정상 중지는 살아 있는 trusted adapter에만 먼저 signal을 보내 App Server의 turn/interrupt와 backgroundTerminals clean/list, server 종료를 확인하게 한다. worker는 실제 소유 process group도 확인한다. 종료 근거가 불완전하면 결과를 완료로 쓰지 않고 UNKNOWN 경계를 유지한다. host만 private policy/command evidence와 last-message를 작성하며 최종 JSON report는 기존 validate_report를 통과해야 한다. exit0만으로 완료하지 않고 needs_user 질문은 추가 호출 없이 보존한다. 권한/정책/프로토콜/세션 제한은 needs_user에서 대기한다.

합성 stdio/실패/중지/timeout 검사와 실제 공식 CLI의 빈 계정 preflight가 통과했다. 그 무인증 검사는 thread/turn/model 0이며 로그인 필요 오류와 실제 group 종료를 확인했다. 인증된 제품 실행의 thread/turn·코드·test 결과는 지원 업데이트와 같은 lineage owner resume 후 별도로 확인한다. 오래된 원본 실패/UNKNOWN 및 별도 실행 세대는 바꾸지 않는다. production source 수리 적용과 실제 제품 구현 성공은 구분한다.

0.3.7 설치 후 같은 job의 owner resume 한 번은 profile 사전 검사에서 차단됐다. named profile 내용과 선택은 일치했으나 실제 loaded config의 legacy sandbox/MCP/notify 존재가 경계와 달랐다. thread/turn/model과 제품 command는 0이며 typed needs_user, 정상 server/group 종료를 확인했다. 0.3.8 소스는 inline notify=[]를 추가해 process별 외부 notify를 차단하고 legacy/MCP guard는 유지한다. 실제 빈 계정/합성 설정 4case와 회귀가 통과했다. 이 후속 소스는 아직 설치하지 않았다.

설치 CLI의 --ignore-user-config는 exec에서만 지원하며 app-server에서는 지원하지 않는다. official native exec의 합성 Responses fixture에서 사용자 config 제외와 explicit named profile을 검증했다. 코드/원장/토큰/Git/agent 설정 경계와 role별 network, 정상 SIGINT 및 command 자식 종료가 확인됐다. 다만 builder는 사용자 CODEX_HOME/config.toml에 project trust를 기록한다. canonical 경로, skip-git-repo-check, read-only 기반 explicit-write profile에서도 재현됐다. 사용자 config 수정 금지 조건 때문에 이 후보는 production에 연결하지 않았다. 모델 없는 합성 SID/결과는 실제 계정 또는 제품 개발 근거가 아니다. 정확한 단일 checkout trust 기록의 사용자 승인과 실제 job 재개 승인은 별도로 필요하다.
