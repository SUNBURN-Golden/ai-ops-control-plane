# AIOPS Mac 실행 도구 연결

2026-10-02 사용자의 Cursor·GLM·Grok Build·Devin 추가 요청을 반영한 **소스 구현 후보**다. 실제 계정 인증과 Mac 실기기 검증을 완료했다는 기록이 아니다.

## 기존 AIOPS에서 이어받는 구조

GitHub main `e5bd6983ef15c599fb84071c4bbc4468478ca9e1`의 전체 파일 목록과 PR #55 기준 소스 일치를 확인했다. 핵심 근거는 다음과 같다.

| 기존 소스 | 확인한 실행 계약 | Mac 연결 |
|---|---|---|
| `adapters/imported/astra-devin-supervisor` | 로컬 `devin`, 작업별 지속 supervisor, 같은 작업 소유자 | 로컬 Devin CLI. cloud flag/API 없음 |
| `adapters/imported/astra-grok-supervisor` | 네이티브 `grok`, prompt file, 명시적 UUID | 같은 경로. Mac 읽기/쓰기 sandbox와 native JSON 검증 |
| `adapters/imported/astra-glm-supervisor` | `opencode run`, Z.AI Coding Plan, JSON 이벤트 | `zai-coding-plan/glm-…` 명시 모델 + 역할별 inline 권한 |
| `adapters/cursor/astra-cursor-supervisor` | 공식 `agent`, 정확한 모델, 로컬 실행 | `agent`/`cursor-agent`, fresh print session, sandbox/ask mode |
| `AGENTS.md`, `docs/BUILDER_LANES.md`, `scripts/control_plane_program.py` | 한 작업 소유자, 독립 검토, 미확정 실행 금지, 공급자 구분 | 공통 SQLite 예약·worker·정확한 HEAD 검증 |

위 경로는 `engineering/` 기준이다. Linux 전용 wrapper/supervisor를 그대로 실행하거나 옛 protected receipt를 재사용하지 않는다. Mac 신규 모드의 역할 선택이 기존 등록 제품의 Linux lane enablement를 수정하지 않는다.

## 역할·실행·결과

여섯 도구 모두 계획·개발·감사·감리에서 선택할 수 있다. 개발 역할만 쓰기를 요청하고 나머지는 읽기 역할로 실행한다. 같은 도구나 모델을 여러 역할에 고르더라도 매번 새 세션이다. 자동 공급자 교체·계정 교체·유료 fallback은 없다. 역할별 선택은 작업 시작 때 고정한다.

| 도구 | 비대화형 실행 | 읽기 역할 제한 | 결과 출처 |
|---|---|---|---|
| Codex | `exec`, schema, last-message | `read-only`, approval never | 전용 마지막 응답 파일 |
| Claude | `--bare -p`, JSON/schema | Read/Glob/Grep, dontAsk | `structured_output` |
| Cursor | `--print`, stream-json, sandbox | `--mode ask`, builder만 force | 성공 terminal 확인 + 마지막 완료 `assistant` 메시지 |
| GLM | `opencode run --format json --agent aiops --auto` | 기본 deny + read/glob/grep, edit/bash/task 차단 | 최종 assistant 메시지와 `step_finish.reason=stop` |
| Grok Build | prompt file, UUID, JSON/schema | `--sandbox read-only`, Bash/Edit/MCPTool 차단 | `structuredOutput`, `stopReason=end_turn`, 요청과 같은 `sessionId` |
| Devin | prompt file, 전용 config, sandbox, ATIF export | edit/exec/모든 Write/MCP 차단 | ATIF 마지막 `source=agent` 메시지. tool/observation/복사된 문맥 불가 |

Cursor·GLM·Devin처럼 별도 JSON schema 강제가 없는 경로에도 공통 응답 schema를 프롬프트로 전달하고 앱이 엄격하게 검증한다. 불완전 응답·오류 envelope·세션 불일치는 통과가 아니다. Cursor는 마지막 완료 assistant 메시지와 terminal 성공을 함께 확인하며, 합쳐진 전체 설명을 JSON으로 해석하지 않는다. Grok의 `text`에는 도구 실행 전 설명도 섞이므로 schema로 검증된 `structuredOutput`만 사용한다. 동일 공급자의 출력 형식이 바뀌면 파서를 확인해야 하며, 로그 전체에서 우연히 발견한 PASS를 대신 사용하지 않는다.

새 worker 영수증에는 `provider`, `harness`, `model_requested`, `session_id`를 함께 기록한다. 요청한 모델은 계정이 실제 실행한 모델/effort/billing의 증명이 아니다. Codex의 ephemeral 실행 등 provider 세션 ID를 관측하지 않는 기존 경로는 값을 만들어내지 않는다.

## 권한과 인증

- 공식 CLI의 기존 로그인/도구 자체 인증 저장소를 사용한다. 앱은 키를 수집하지 않는다. GLM Coding Plan 키는 OpenCode에 직접 연결한다.
- OpenCode는 명시적인 Z.AI Coding Plan 공급자·모델과 AIOPS agent를 지정한다. 사용자 환경의 다른 OpenCode 설정 변수를 상속하지 않고, 이 실행에 한정한 설정을 전달한다.
- Devin의 전용 prompt/config/export는 앱의 private attempt 디렉터리에 둔다. 사용자 홈의 글로벌 config나 제품 레포를 설정 파일로 덮어쓰지 않는다. 자동 업데이트·subagent 및 다른 도구의 설정 자동 import를 이 config에서 끈다.
- CLI 외부 셸 문자열을 조립하지 않는다. 명령과 모델은 argv로 전달하고, 요청 본문은 stdin 또는 0600 prompt file로 전달한다.
- Cursor force/Grok always-approve/OpenCode auto는 비대화형 처리를 위한 옵션이며 OS 보안 경계를 뜻하지 않는다. Devin sandbox는 exec-tool 프로세스에 적용되므로 직접 파일 도구에는 별도 permissions를 둔다. GLM에는 native OS sandbox를 구현한 것으로 표시하지 않는다.
- CLI별 개인 설정·조직 정책·플러그인 및 도구 권한의 실제 효과는 설치된 버전에서 확인해야 한다. 같은 Mac 사용자 자격증명은 완전한 적대적 격리가 아니다. 본 모드는 개인 Mac 사용을 전제로 하며 기존 Linux protected host qualification을 대체하지 않는다.
- CLI 오류·형식 오류가 있더라도 읽기 역할이 HEAD나 작업트리를 바꾸었다면 실행을 미확정 상태로 보존한다. 프로세스 전체 종료를 확인하지 못해도 다음 writer를 시작하지 않는다.

## 실기기 확인

설치 담당 로컬 Codex는 필요한 도구만 공식 배포판으로 설치하고 사용자에게 로그인/OS 승인만 요청한다. UI의 **연결**은 설치·버전 확인이며 provider 로그인 성공을 추정하지 않는다.

독립 소스 검토 후 실제 Mac에서 선택한 각 도구마다 다음을 확인한다.

1. 계정 로그인, 해당 계정의 정확한 모델 ID, 구독/과금 경로.
2. 격리된 테스트 레포에서 builder의 파일 변경·테스트와 읽기 역할의 수정 거절.
3. 명령 옵션 지원, 정상 종료 코드, 실제 JSON/ATIF 결과와 provider 세션 ID.
4. timeout/종료/앱 재시작 시 중복 모델 호출 금지.
5. 자동 계획 → 개발 → 실패 수정 → 독립 검토 → 최종 검수 흐름.

이 환경에서는 provider CLI를 흉내 낸 실제 로컬 subprocess와 공식 형식 fixture를 사용한다. 실계정 모델 호출·macOS sandbox 검증·기존 Grok 호스트 이관을 대체하지 않는다. 원래 레포의 독립 감사/병합/릴리스 계약도 계속 적용된다.

## 공식 인터페이스 근거

- [Cursor flags](https://cursor.com/docs/cli/reference/parameters), [출력 형식](https://cursor.com/docs/cli/reference/output-format)
- [OpenCode CLI](https://opencode.ai/docs/cli/), [권한](https://opencode.ai/docs/permissions/), [Z.AI Coding Plan](https://docs.z.ai/devpack/tool/opencode)
- [OpenCode run 소스](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/run.ts): stdin, sessionID, 최종 text 및 step_finish 이벤트
- [Grok Build CLI](https://docs.x.ai/build/cli/reference), [sandbox](https://docs.x.ai/build/features/sandbox), [headless 출력](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md), [구조화 출력 필드](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/src/headless/reducer/mod.rs)
- [Devin flags](https://docs.devin.ai/cli/reference/commands), [permissions](https://docs.devin.ai/cli/reference/permissions), [config](https://docs.devin.ai/cli/reference/configuration/config-file)
- [ATIF step 정의](https://github.com/harbor-framework/harbor/blob/main/src/harbor/models/trajectories/step.py), [trajectory 정의](https://github.com/harbor-framework/harbor/blob/main/src/harbor/models/trajectories/trajectory.py)
