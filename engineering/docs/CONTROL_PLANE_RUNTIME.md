# Control Plane Runtime — 제한된 실행 어댑터

정본: `AGENTS.md`, `TASKS/TEMPLATE.md`, `RUNBOOKS/DISPATCH.md`.
이 구현은 명시적인 GitHub workflow command → 단일 builder launch만 담당한다.
Slack intake, CI/review/audit 전달, READY_FOR_MERGE 계산은 아직 구현하지 않았다.
그 gate는 기존 수동 절차를 따른다. 자동 merge·polling·상주 reasoning은 없다.

## 호출과 비용

- `preflight`: 활성 lane의 설치·인증·제어 경계를 읽기 전용 검사한다.
- `dispatch`: canonical issue의 `BUILDER_STANDARD` / 지정 `BUILDER_ID`를 실행한다.
- `enabled_builders`의 초기값은 `DEVIN` 하나다. 다른 두 adapter는 지원 후보로 유지하며,
  별도 preflight·승인 없이 자동 선택하거나 fallback하지 않는다.
- 동일 task의 기존 owner는 provider preflight 전에 반환한다. transcript를 읽지 않는다.
- builder는 조사·구현·test/fix/retest·PR을 소유한다. 일반 A1/A2 작업의 Astra/Grok
  호출을 추가하지 않는다. 독립 review, A3 승격, milestone gate는 기존 규칙을 유지한다.
- 초기 host policy 예시는 한 project/lane, 동시 세션 1개이며 일일 신규 launch 상한은 없다.
  `max_launches_per_24h: null`은 unlimited를 의미한다. builder 내부 수정·재시험 횟수도 제한하지 않는다.
- 동시 세션 상한은 중복 writer/runaway launch 방지용이며 금액/토큰 상한이 아니다. provider의 실제 과금 상한은
  별도로 설정·검증한다. 완료 task당 Astra/Grok 사용량, 전체 provider 비용, 재작업과
  독립 review finding을 함께 측정하며 관측 전 절감률을 주장하지 않는다.

## 보호된 host admission

`self-hosted`, `astra-control-plane` runner는 제어 command 전용이다.
product PR 코드/테스트를 이 runner에서 실행하지 않는다. runner 개수는 활성 builder
개수의 상한이 아니다. session은 Actions job이 끝난 후에도 살아 있기 때문이다.

관리자가 검토한 `scripts/control_plane_host.py`를 다음 고정 경로에 설치한다.

```text
/opt/astra/bin/astra-host-control         root 소유, runner/builder 쓰기 금지
/etc/astra/control-plane-host.json       root 소유, runner/builder 쓰기 금지
/var/lib/astra/control/                  astra-control 소유, 0700
/var/lib/astra/control/admission.sqlite3  astra-control 소유, 0600
```

설치 경로·상위 디렉터리의 소유권/쓰기 권한/심볼릭 링크를 검사한다.
`astra-control`, runner, 각 local builder는 서로 다른 비-root Unix UID를 사용한다.
worktree 분리는 credential 경계가 아니다. builder는 control DB, runner 작업 디렉터리,
GitHub 제어 토큰, 다른 builder의 credentials에 접근할 수 없어야 한다.
control 계정의 HOME/secret store도 builder에 열지 않는다.

runner의 sudo 허용은 고정 helper의 `launch`, 정확한 세 builder별
`preflight --builder-id ...` command, 읽기 전용
`status --launch-request-id <24자리 hex>` command만으로 제한한다(program mode는
아래 "runner가 sudo로 부를 수 있는 새 명령"의 `status --lanes`, `task-status`, `reap`,
`materialize-*`를 더한다). `status`는 sudoers에서
그 인수 형식만 허용하고(가능하면 정규식), ledger를 바꾸지 않는다. 이 항목이 없으면
host-preflight와 모든 재시도가 fail-closed로 거부된다. shell, 임의 Python, 임의 인수,
`init`, `reconcile`, 일반 root 실행을 허용하지 않는다. builder에는 이 sudo 권한이 없다.
추가 `aiops-fable program` root 예외는 아래 후보 절에서 별도로 정의하며,
실제 User security-boundary 채택과 독립 exact-HEAD A3/host qualification,
protected service authorization 전에는 기존 금지가 적용된다. 후보 example을
추가한 사실은 설치·허가가 아니다. 예외가 채택되어도 shell·임의 Python/인수,
init/reconcile나 builder root를 허용하지 않는다.
관리자만 DB를 최초 `init`한다. 손실된 DB를 빈 DB로 재생성해 복구하지 않는다.
원 ledger와 외부 session을 대사하기 전 dispatch를 재개하지 않는다.
최초 활성화 전에도 ledger에 없는 기존 writer/session이 없는지 확인한다.

program mode의 sudoers 원문은 `.github/control-plane/sudoers-aiops-program.example`이다.
- 설치 위치는 `/etc/sudoers.d/aiops-program`이고, root:root 0440이다. 파일 이름에 점(`.`)이 있으면 sudo가 읽지 않는다.
- 바꾸는 곳은 `RUNNER_USER` 하나다. 기존 `status --launch-request-id` 규칙의 사용자 칸을 그대로 쓴다.
- 인수는 sudo 1.9.10 이상의 정규식(`^...$`)으로 제한한다. helper의 인수 검증과 같은 모양이다.
- runner 규칙은 `reap`의 `--pin-stdin` 형식만 허용한다. `migrate`, `reconcile`, `init`, `materialize-resolve`는 허용하지 않는다.
- control identity는 각 레인 adapter의 `--quiescence`만 레인 계정으로 실행한다. root로는 실행하지 않는다.
- program mode 테스트는 런타임이 helper에 보내는 모든 인수가 이 규칙에 맞는지 검사한다. 새 helper 호출을 더하면 이 파일도 같이 고쳐야 한다.
- 설치 후 확인: `diff <(sed 's/^RUNNER_USER /<runner 계정> /' sudoers-aiops-program.example) /etc/sudoers.d/aiops-program`

host policy 모양은 `.github/control-plane/host-policy.example.json`에 있다.
예시는 UID=0 / 빈 repo / PENDING evidence라서 그대로는 실행되지 않는다.
실제 policy와 ledger, runtime 상태는 source tree에 commit하지 않는다.

root 소유 wrapper는 control identity로 packet만 읽고, repository 작업 전에 지정한
builder identity/sandbox로 전환해야 한다. 이 helper는 adapter의 실제 UID 전환,
ACL/sudoers·process isolation·provider 권한을 자동 증명하지 않는다.
배포자는 builder 관점에서 제어 파일 접근 실패, 토큰 접근 실패, worktree 독립성,
Actions 종료 후 session 생존을 실측하고 `boundary_evidence_pointer`에 남긴다.
독립 host 검증 전 활성화하지 않는다. pointer 존재만으로 보안 PASS는 아니다.

## 세션 수와 중복 실행

모든 repo/runner가 같은 host ledger를 사용한다. 여러 host로 확장하려면 별도의
공유 admission authority가 필요하다. 이 SQLite 파일은 distributed lock이 아니다.

1. SQLite transaction이 request identity와 전체 packet을 대조한다.
2. repo + task의 활성 owner와 전체 활성 세션 수를 검사한다. 일일 신규 launch 횟수는 제한하지 않는다.
3. `SUBMITTING`을 durable commit한 후에만 wrapper를 호출한다.
4. `CONFIRMED`, `UNKNOWN`, crash 후 `SUBMITTING`은 계속 자리를 점유한다.
5. 같은 request는 저장된 결과만 반환한다. packet 변경이나 새 revision으로 기존
   활성 writer를 우회하지 못한다. 한도 초과도 그 request의 결과로 고정한다.
6. 확실한 `FAILED_PRESTART`만 즉시 자리를 반환한다. launch 기록은 audit history로 남지만 일일 상한에는 사용하지 않는다.

job 종료, timeout, 시간 경과, process 미발견은 해제 증거가 아니다.
User가 승인한 operator만 원 request의 terminal session/취소·fencing 근거를 확인하고
`reconcile --launch-request-id ... --session-id ... --evidence <GitHub URL>`을 실행한다.
UNKNOWN에는 추정 session을 넣지 않는다. 살아 있는 launch helper와 해제는 경쟁하지
못하도록 잠근다. 복구 가능한 기존 ticket은 owner를 유지한다. 해제는 재실행 허가가
아니며, 재시도/재배정은 아래 명시적 재시도 절차를 따른다.

## 명시적 재시도

재시도는 dispatch 입력 `expected_attempt_id`에 control record의 `attempt_id + 1`을
적는 경우에만 일어난다. 빈 값은 첫 시도(1) 또는 현재 시도의 재개다. 새 시도는 새
`claim_id`/`launch_request_id`를 만들고, 이전 시도 요약을 `previous_attempts`에 남긴 뒤
NOT_STARTED → SUBMITTING 순서를 그대로 밟는다. 같은 재시도 dispatch를 다시 보내면
번호가 이미 현재 시도이므로 세 번째 시도가 생기지 않는다.

GitHub record는 projection이므로 재시도마다 host `status`로 이전 request를 확인한다.

| 이전 시도 상태 (record) | 재시도 허용 조건 (host ledger) |
|---|---|
| NOT_STARTED | request 없음, FAILED_PRESTART 또는 RECONCILED |
| FAILED_PRESTART | FAILED_PRESTART 또는 RECONCILED |
| SUBMITTING / UNKNOWN | RECONCILED 또는 FAILED_PRESTART |
| CONFIRMED | 거부 — 살아 있는 owner를 밀어내지 않는다 |

host가 CONFIRMED를 보고하면 record가 finalize 결과를 잃은 것이다. owner가 살아 있으므로
재시도·reconcile하지 않는다. SUBMITTING/UNKNOWN인데 host에 request가 없으면
(SUBMITTING 기록 후 launch 전 중단, sudo 거부 등) 그 request는 host에 도달하지 않았다.
어떤 sender도 더 보낼 수 없음을 operator가 확인한 뒤
`reconcile --launch-request-id <id> --no-session --sender-fenced --never-admitted --evidence <URL>`로
tombstone을 남긴다. 이후 그 request의 늦은 송신은 host가 거부하고, 재시도는 RECONCILED를 본다.
`host-preflight`는 읽기 전용 `status`가 응답하는지(sudoers 포함)도 확인한다.

재시도 envelope의 TASK_REVISION·BUILDER_ID는 dispatch 입력으로 고정된 현재 값이다.
TASK_ID는 바꿀 수 없다. host ledger는 여전히 task당 활성 reservation 하나만 허용한다.
session 생성 전 crash였으며 원 request의 미생성과 모든 sender의 fencing을
operator가 입증한 경우에만 `--session-id` 대신 `--no-session --sender-fenced`를 쓴다.
같은 exclusive lock과 durable evidence를 요구하며 알려진 session에는 사용할 수 없다.
최초 결과·request dedupe·신규 호출 계수는 그대로 보존한다. 단순 조회 실패나
timeout은 미생성 증거가 아니다. 해당 옵션은 operator의 증거 확인을 자동 대체하지 않는다.
GitHub task에는 reconciliation evidence와 control projection을 남긴다.

## GitHub credential과 발송 경로

User 결정(2026-09-28): GitHub credential은 User 개인 토큰 하나(`ASTRA_CONTROL_GITHUB_TOKEN`)이며
control record 작성자는 그 토큰의 login이다. 이 결정은 `AGENTS.md` §11의 전용 identity 권고보다 우선한다.
발송 경로는 self-hosted runner의 `control-plane-runtime.yml` 하나다. Actions 없는 발송 경로는
실행 소스를 승인된 GitHub commit에 결속하고 이 workflow와 같은 serialization/route fence를
공유하기 전에는 추가하지 않는다(PR #34 A3 review F2/F3).

## Program mode host 명령 (ledger v2)

`docs/PROGRAM_MODE.md` §3·§4·§6·§8.2, `RUNBOOKS/DISPATCH.md` §25.

**설치 순서**
1. 새 helper를 설치한다.
2. 곧바로 운영자가 `migrate --to 2`를 실행한다.
   - v1 ledger에서는 migrate를 뺀 모든 명령이 거부된다. 그래서 이전 전에는 발송이 fail-closed로 멈춘다.
   - migrate는 exclusive in-flight 잠금 아래에서 돈다. 먼저 `<ledger>.v1-backup-<ts>` 백업을 만들고, 한 트랜잭션 안에서 다음을 한다.
     - 열 추가(role, lane, review_key)
     - 기존 행의 lane 채우기
     - 인덱스 교체(`one_active_writer`, `one_active_review`, `one_active_per_lane`)
     - `materializations` 생성
   - 같은 레인에 활성 행이 둘이면 인덱스 생성이 실패해 전부 되돌아간다. 그때는 먼저 reconcile한다.

**runner가 sudo로 부를 수 있는 새 명령.** 인수 형식은 정규식으로 제한한다.
- `status --launch-request-id <id>`의 응답 필드가 늘었다.
  - 추가 필드: role, lane, repository, task, attempt_id, owner_lane, `reserved_at`(host 예약 시각, 초 단위 내림), resolution, pin, evidence, `released_at`. 리뷰어 행에는 review id, head, PR 번호도 붙는다.
  - packet과 서명 키는 절대 돌려주지 않는다.
  - reap은 `reserved_at`보다 앞선 산출물이나 리뷰를 증거로 받지 않는다.
- `task-status --repository <owner/repo> --task <TASK_ID>`: 한 작업의 모든 launch 행을 돌려준다(읽기 전용, 형식은 위와 같다). merge-check과 리뷰 발송의 근거다.
- `status --lanes`: 읽기 전용 레인 현황.
- `reap --launch-request-id <24 hex> --evidence <https URL> [--pin-stdin]`
  - CONFIRMED 행만, 그 레인 builder UID의 live process가 0개일 때 `RECONCILED/SESSION_TERMINAL_VERIFIED`로 바꾼다.
  - `--pin-stdin`이면 stdin으로 `{"pin": "<서명된 한 줄>"}`만 받는다. 서명 키가 있는 세션은 서명된 줄 없이 해제하지 않는다. 받는 줄은 전달 줄, 판정 줄, 또는 그 세션 launch id의 차단 줄(`ASTRA_BLOCKED_V1`)이다.
    - 저장된 packet의 `delivery_nonce` 또는 `review_nonce`로 HMAC을 검증한다.
    - 리뷰 판정은 그 세션의 review id와 head를 가리켜야 한다.
    - 다른 작업이 이미 고정한 PR을 전달물로 가리키면 거부한다.
    - 검증된 결과를 해제 기록에 고정한다.
  - **write-once:** 같은 요청을 다시 부르면 저장된 결과를 돌려준다. 이때 evidence와 pin이 처음과 같아야 하고, 다르면 거부한다.
  - 정지 확인은 바깥 `/proc` 스캔과, 레인 wrapper `--quiescence`(레인 계정이 자기 프로세스를 모두 SIGSTOP으로 멈추고 목록을 만든 뒤 SIGCONT로 푸는 경합 없는 점검)를 함께 쓴다. control identity가 wrapper를 실행하는 sudo 규칙에 `--quiescence`를 더한다.
  - 정지 확인은 fail-closed다. 아래 중 하나라도 해당하면 거부한다.
    - `/proc`에 hidepid가 있다.
    - helper의 PID namespace가 `/proc`의 namespace가 아니다(`NSpid`가 1단계가 아님).
    - 레인 UID에 `/etc/subuid` 또는 `/etc/subgid` 범위가 있다.
    - `/proc/<pid>/status`를 읽거나 해석할 수 없다.
  - 세션을 끄지 않는다.
  - adapter의 `status.json`은 빌더가 쓸 수 있는 곳에 있어 위조가 가능하다. 그래서 증거로 쓰지 않는다.
- `launch`는 adapter가 `FAILED_PRESTART`를 보고해도, 같은 정지 확인으로 레인이 비었음을 본 뒤에만(최대 10초) 슬롯을 돌려준다. 그렇지 않으면 `UNKNOWN`으로 기록한다.
- `launch`는 이미 해제된(RECONCILED) 요청을 다시 받으면 저장된 결과를 재생하지 않는다. 대신 `FAILED_PRESTART`("already released")를 돌려준다. 재개는 언제나 새 시도다.
- `materialize-begin --program <id> --node <id> --repository <owner/repo> --plan-commit <40 hex>`
- `materialize-finish --program <id> --node <id> --request <24 hex> --outcome CREATED|UNKNOWN [--issue N]`
- `materialize-status --program <id> --node <id>`
- `materialize-plan --program <id> --node <id> --from <40 hex> --to <40 hex>`: plan commit compare-and-swap.

**운영자 전용 명령.** runner는 금지다.
- `migrate --to 2`
- `materialize-resolve --program <id> --node <id> --request <24 hex> --not-created --evidence <URL>`
  - 미생성을 확인한 요청 ID를 봉인한다. 봉인된 요청은 다시 쓰지 않는다.

**레인당 세션 1개**
- 동시 세션 한도는 host `max_active_sessions`다. program mode에서는 켜진 레인 수까지 올린다.
- 같은 작업에서는 활성 WRITER와 REVIEWER가 서로 배제된다.

**레인 정리 (운영자)** (`docs/PROGRAM_MODE.md` §6 "레인 위생")
- 레인 계정에 세션 밖 프로세스가 남으면 그 레인은 해제되지 않는다. User 결정(2026-09-30)에 따라 자동 청소는 없고, 운영자가 손으로 정리한다.
- 먼저 `status --lanes`로 그 레인의 `active`를 확인한다. 아래 둘 중 하나일 때만 정리한다.
  1. `active`가 비었다. 대화형 로그인이나 수동 시험 뒤가 여기에 해당한다.
  2. `active`가 현장 소장이 정리를 요청한 그 세션 하나뿐이고(`needs-lane-cleanup`), 그 세션이 끝났다. 끝났다는 것은 아래 두 가지가 모두 참이라는 뜻이다.
     - `<레인 작업 루트>/.sessions/<launch_request_id>/status.json`의 `state`가 `EXITED` 또는 `SUPERVISOR_ERROR`다. 작업 루트는 `/opt/astra/worktrees/`의 `devin`, `grokbuild`, `glm`, `cursor`다.
     - `pgrep -u <lane user> -f 'libexec/astra-[a-z]+-supervisor'`가 아무것도 출력하지 않는다. 명령줄 전체는 출력하지 않는다.
- 다른 경우(작업이 둘 이상이거나, supervisor가 아직 돌거나, status가 `RUNNING`)에는 정리하지 않고 보고한다.
- 레인 계정으로만 TERM을 보내고, 10초 뒤 KILL을 보낸다. root가 `kill -1`을 보내면 host 전체가 멈춘다.
  ```
  sudo -u <lane user> /bin/sh -c '[ "$(id -u)" -ne 0 ] && exec /usr/bin/kill -s TERM -- -1'
  sleep 10
  sudo -u <lane user> /bin/sh -c '[ "$(id -u)" -ne 0 ] && exec /usr/bin/kill -s KILL -- -1'
  ```
- 다음 census가 `{"status": "OK", "live": []}`인지 확인한다. 2의 경우에는 이슈에 정리했다고 댓글을 남기고 `needs-lane-cleanup`을 뗀다. 다음 실행의 `reap`이 세션을 해제한다.
- census가 중간에 끊겨 레인 프로세스가 멈춘 채 남았으면, 레인 계정으로 `kill -s CONT -- -1`을 보낸다. 다음 census도 끝에서 CONT를 보내므로 저절로 풀린다.
- 수동 census는 dispatch가 없을 때만 한다. host의 census 잠금은 helper 밖의 수동 실행을 막지 못한다.

## Wrapper contract

```text
DEVIN      /opt/astra/bin/astra-builder-devin
GROK_BUILD /opt/astra/bin/astra-builder-grok-build
GLM        /opt/astra/bin/astra-builder-glm
CURSOR     /opt/astra/bin/astra-builder-cursor
```

비활성 lane의 미설치는 활성 lane을 막지 않는다. `ALL`은 활성 lane 전체를 의미한다.
wrapper는 runner 환경을 상속하지 않는다. 고정 PATH/LANG만 받고 provider 인증은
별도 보호된 store에서 가져온다. GitHub 제어 token, Actions command file, runner HOME을
builder에 전달하지 않는다. checkout도 `persist-credentials: false`다.

`--preflight`는 JSON 한 개와 exit 0을 반환한다.

```json
{"status":"PASS","builder_id":"DEVIN","launch_contract_version":2,"execution_mode":"REMOTE_SESSION","parallel_safe":true,"worktree_root":"/opt/astra/worktrees/devin"}
```

`execution_mode`는 `REMOTE_SESSION` 또는 `PERSISTENT_SUPERVISOR`다. 매 dispatch의
preflight에서 AI session을 새로 만들지 않는다. 실제 durable/parallel 진단은 배포 때
한 번 수행하고 binary/policy/credentials 경계 변경 시 다시 수행한다.
helper는 ledger/보호 경계를 검사하고 자기 source hash와 evidence pointer를 붙인다.
runtime은 설치된 helper hash와 검토 대상 source를 대조한다.

launch는 기존처럼 packet 파일 인수 하나를 받는다. helper가 보호된 디렉터리에
0600 파일을 만들며, wrapper 응답 후 제거한다. provider에 idempotency key가 있으면
`launch_request_id`를 쓴다. packet의 task/spec revision과 승인·정본 pointer를 따른다.
wrapper는 `ASTRA_HOST_INFLIGHT_FD`의 제어 lock을 송신을 수행하는 자식에게도
유지시키고, 모든 launch 송신이 끝난 후 닫는다. helper crash 뒤에도 송신 중에는
reconcile이 잠겨야 한다. 이 FD는 builder session/UID로 전달하지 않는다.

응답은 아래 identity 전체와 outcome을 함께 반환한다.

```text
repository, task_id, task_revision, builder_id, launch_request_id, attempt_id
CONFIRMED       + 실제 durable session_id
FAILED_PRESTART + nonempty reason (외부 session이 시작되지 않았다는 확증)
```

identity 누락/불일치, 응답 유실, malformed JSON, nonzero exit는 `UNKNOWN`이다.
blind retry하지 않는다. 단순 nohup이나 CLI 종료를 durable session 증거로 쓰지 않는다.
GitHub finalization도 정확한 launch request를 대조하며 이전 run의 결과 파일을 재사용하지 않는다.

## Astra on the host (Claude Fable, User 결정 M5)

User 결정 M5(2026-09-30)에 따라 Astra 역할은 Claude Fable(`claude-fable-5-1`)이 맡는다.
그록봇 컴퓨터에서 고정 도구 `scripts/control_plane_fable.py`(설치 이름 `aiops-fable`)로만 실행한다.
운영자(그록봇)는 요청이 적은 인자 그대로 실행하고, 도구가 올린 결과를 그대로 전한다.

설치 배치:

| 경로 | 소유와 모드 | 내용 |
|---|---|---|
| `/opt/aiops/bin/aiops-fable` | root, 0755 | `scripts/control_plane_fable.py` 그대로. sha256은 설치 지시서로 고정한다 |
| `/usr/local/bin/claude` 또는 `/usr/bin/claude` | root, group/other 쓰기 불가 (실제 파일도) | Claude Code CLI |
| `/etc/aiops/fable-claude-token` | root, 0600 | `claude setup-token`으로 만든 Claude 토큰. 감사 계정은 읽을 수 없다 |
| 계정 `aiops-auditor` | root가 아닌 system 계정, 홈 `/var/lib/aiops-auditor` (0700) | 모델을 실행하는 UID. GitHub 자격 증명이 없다 |
| `/var/lib/aiops-fable/runs` | root, 0755 | 실행 폴더. 각 실행은 `root:aiops-auditor` 0750 |

명령 (root. audit와 consult는 운영자의 GitHub 토큰을 `GH_TOKEN`으로 받는다. preflight는 GitHub을 쓰지 않는다):

```sh
sudo /opt/aiops/bin/aiops-fable preflight
sudo env GH_TOKEN="$(gh auth token)" /opt/aiops/bin/aiops-fable audit \
  --repository <owner/repo> --pr <n> --head <40자 SHA> --gate <ARCHITECTURE|MILESTONE|RELEASE> \
  --depth <A1|A2|A3> [--request-comment <id>]
sudo env GH_TOKEN="$(gh auth token)" /opt/aiops/bin/aiops-fable consult \
  --repository <owner/repo> --issue <n> --comment <질문 댓글 id> [--ref <branch|SHA>]
```

도구가 지키는 것:
- **정확한 대상:** PR이 열려 있고 head가 `--head`와 같아야 시작한다. 다르면 `HEAD_MOVED`로 멈춘다.
  질문 댓글과 요청 댓글은 그 이슈나 PR의 것이어야 한다. 저장소는 `BeautifulMind-JT/` 아래만 받는다.
- **복사본만 읽는다:** head와 merge base의 tarball을 실행 폴더에 푼다(링크·특수 파일은 건너뛰고 목록만 남김,
  크기 상한). 전체 diff는 host에서 만든다. 끝나면 두 트리를 지운다.
- **읽기 전용 모델:** 감사 계정 UID로 `claude -p`를 실행한다. `--restricted --safe-mode --tools Read,Grep,Glob
  --permission-mode dontAsk --strict-mcp-config`이므로 작업 폴더 밖을 읽지 못하고, 명령 실행·쓰기·웹 도구·
  훅·MCP·CLAUDE.md·저장소 설정이 없다. 환경 변수는 HOME, PATH, LANG, Claude 토큰, 자동 업데이트·비필수 통신 끄기뿐이다.
  `--fallback-model`은 쓰지 않고, 거절 시 자동 모델 전환도 끈다(`CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`).
  Fable을 쓸 수 없거나 거절하면 실패하고 기다린다.
- **추가 과금 차단:** CLI 출력(stream-json)의 `rate_limit_event`가 `overageStatus: "rejected"`, `isUsingOverage: false`여야 한다.
  초과 사용(extra usage)이 막혀 있다는 뜻이다. 다른 값이 한 번이라도 나오면 그 즉시 실행을 끊고 아무것도 올리지 않는다
  (`OVERAGE_NOT_BLOCKED`). 이 신호가 없으면 확인할 수 없으므로 역시 올리지 않는다(`OVERAGE_UNVERIFIED`).
  첫 요청 뒤에야 신호가 오므로, 초과 사용이 켜진 계정이라도 한 실행에서 새는 양은 첫 요청 하나로 제한된다.
  확실히 0원으로 하려면 claude.ai 설정 > 사용량(Usage)에서 추가 사용량(usage credits)과 자동 충전을 끈다.
  `ANTHROPIC_API_KEY`는 모델 환경에 넘기지 않으므로 API 종량 과금 경로도 없다.
- **결과 검사:** CLI 결과가 성공이고 `modelUsage`에 `claude-fable-5-1` **하나만** 있어야 한다.
  거절 후 다른 모델로 바뀐 기록(`model_refusal_fallback`)이 있으면 올리지 않는다(`MODEL_FALLBACK`). 구조화된 판정은
  일관돼야 한다(PASS는 finding 없음, PASS_WITH_NOTES는 NOTE만, FAIL은 BLOCKING 하나 이상,
  DECISION_REQUIRED는 질문 필수). 어긋나면 아무것도 올리지 않는다.
- **자격 증명 차단:** 올릴 글에 두 토큰 값이나 토큰 모양의 문자열이 있으면 올리지 않는다. `@` 멘션은 무력화한다.
- **중복 방지:** 같은 head의 감사, 같은 질문의 답이 이미 있으면 멈춘다. 다시 하려면 `--again`을 붙인다.

결과 형식:
- 감사: 첫 두 줄이 `<!-- aiops-fable-audit -->`와
  `ASTRA_AUDIT_V1 pr=<n> head=<sha> result=<판정> depth=<A1|A2|A3> auditor=ASTRA_FABLE session=<id>`이다.
  본문에 `DISPATCH.md` §14의 결과 필드와 finding이 있다. 요약은 쉬운 한국어다.
- 상담: 첫 두 줄이 `<!-- aiops-fable-consult -->`와
  `ASTRA_CONSULT_V1 result=<ANSWERED|USER_REQUIRED> by=ASTRA_FABLE question=<id> ref=<sha> session=<id>`이다.
- 도구는 이 첫 두 줄만 읽어 이전 결과를 찾는다. gate는 이 글을 읽지 않는다(M4).

증거: 실행 폴더에 packet, 원본 CLI 출력(`claude-output.jsonl`), 올린 글, `run.json`이 남는다.
댓글에는 host run id, 모델 세션, 도구 sha256, 원본 출력 sha256이 있다.

실패하면 `{"status": "ERROR", "reason": ...}`를 출력하고 아무것도 올리지 않는다. 운영자는 원문을 보고하고 멈춘다.
우회하거나 다른 모델로 다시 돌리지 않는다.

## 활성화와 검증

`runtime_enabled=false` 유지. 기본 branch의 workflow만 self-hosted job을 실행한다.
self-hosted Python 호출은 `-I`로 격리해 checkout의 동명 module/PYTHONPATH가
활성화 검사 전에 실행되지 않게 한다.
아직 미병합 branch의 임의 workflow가 host를 사용할 수 없도록 runner 접근 정책도 검증한다.
GitHub job 조건 하나를 권한 경계로 간주하지 않는다.

필수 순서: 독립 implementation audit → User merge → 보호된 host 설치/검증 →
기본 branch `preflight` → activation-only PR → User merge.
기존 User 승인 pointer는 보존하되, 새 HEAD에 과거 audit/CI 결과를 승계하지 않는다.
`activated_runtime_sha`는 감사한 implementation commit이다. activation-only commit은
그 뒤에 올 수 있지만 실행 코드/설정/workflow/governance는 그 SHA와 같아야 한다.
working tree의 제어 파일 변경도 거절한다. audit/host evidence와 활성화는 별도 gate다.

로컬 검증:

```sh
python3 scripts/control_plane.py self-test
python3 -m unittest discover -s scripts -p 'test_control_plane*.py'
python3 scripts/control_plane.py validate-repo
```

테스트는 중복·경쟁·UNKNOWN·stale receipt·환경 누출·activation 거절을 다룬다.
실제 provider 과금, host 격리, GitHub/Slack 종단 동작을 검증했다는 뜻은 아니다.
# Program Astra bridge (implementation candidate)

The protected `aiops-fable program` path, scoped audit receipts and automatic
consult invocation are a restricted Option C candidate in
[PROGRAM_ASTRA_AUTOMATION.md](PROGRAM_ASTRA_AUTOMATION.md). Its additional User
governance/security-boundary decision is PENDING; existing M1/M5 do not already
grant it. A declared RELEASE gate, current reviewer contract-change YES and
User-only node always hold automatic merge regardless of a scope receipt.

The proposed runner sudoers exception permits only the exact
`/opt/aiops/bin/aiops-fable program` command. Bounded stdin is validated by the
protected service, not a coordinator interpretation. Before GitHub/model work,
the service requires root-protected `program-astra-authorization.json` beside
its installed config. It binds accepted runtime commit, actual decision/audit/
qualification evidence and installed Fable/support/config/profile/policy hashes.
Missing/PENDING or changed fingerprints refuse direct calls as well as workflow
calls. Filling example pointer fields cannot establish actual qualification.

Install the same audited commit's seven policy documents (AGENTS, DISPATCH,
PROGRAM_MODE, CONTROL_PLANE_RUNTIME, COORDINATOR_PLAYBOOK,
PROGRAM_ASTRA_AUTOMATION and the adoption proposal) beneath the installed
`/opt/aiops/lib` root, together with the protected support/config/profile files.
Copied support is not assumed to be a git checkout. The privileged entry's
service attestation is separate from the normal checkout activation check;
both need actual qualification. Only an authorized operator installs sudoers,
and the exact example/source comparison above must include this adopted
exception rather than leave an undocumented extra root rule.

The candidate leaves activation unchanged and has no qualified protected Fable
receipt reconciler. Full automation remains NOT_READY until that separate
implementation/audit/actual qualification. Builder-session reconcile cannot
settle a Fable model request; no receipt deletion, new HEAD/plan/tool hash or
workflow cancellation substitutes for terminal evidence. Typed failure/journal recovery is now a source candidate documented in
`PROGRAM_FABLE_RECOVERY.md`; installed automatic recovery remains unqualified.


Recovery source candidate update: `docs/PROGRAM_FABLE_RECOVERY.md` specifies the implemented protected admission journal and bounded terminal-failure reconciliation. Earlier statements that reconciliation is unimplemented describe the #46 checkpoint; it remains uninstalled/unqualified and grants no operator override for unproven UNKNOWN. Full host activation is still NOT_READY.
