# AIOPS Mac

GitHub 레포와 목표를 한 번 주면 계획 → 개발·테스트 → 독립 감사 → 최종 감리 → 결과 검수까지 진행하는 개인용 개발 작업실입니다. 일반적인 실패와 검토 지적은 같은 개발 작업으로 돌려보냅니다. 사용자는 마지막 결과를 검수하거나 수정 의견을 남깁니다.

## 현재 제공되는 것

- Mac의 `.app` 실행 아이콘과 로컬 웹 UI. 기본 브라우저에서 열립니다.
- 앱 창을 닫아도 실행하는 사용자별 `launchd` 서비스.
- 레포별 한 작업 소유자, 전체 한 실행 세션, 별도 체크아웃과 전용 작업 브랜치.
- 레포 문서에서 계획 작성, 단계별 개발, 전체 diff 감사, 원래 산출물 전체 감리.
- 시작 SHA의 `.aiops/program.json`을 고정하고 원래 노드·명세·로컬 의존성의 누락을 검사합니다. 숫자 ID와 최대 256개 작업을 지원하며 원래 명세는 앱이 직접 붙입니다.
- 계획·개발·감사·감리 각각 Codex CLI, Claude Code, Cursor, GLM, Grok Build, Devin과 모델 ID 설정.
- 새 작업의 모델 구성 고정, 독립 검토 세션, 정확한 HEAD에 묶인 결과.
- 작업 기록, 단계 사이 일시정지, 검수 후 재수정, 사용자가 정하는 선택적 실행 한도.
- 실행 시간·종료 한도·최근 출력 관측·다음 재시도·다른 작업의 미확정 실행 대기를 구분해 표시합니다.
- 결과 준비 및 필수 질문에 대한 Mac 알림. 알림 표시는 macOS 알림 설정을 따릅니다.
- GitHub 검수용 draft PR 생성과 CI 확인. 앱의 검수 완료는 자동 병합·배포 명령이 아닙니다.
- 사용자가 검수한 특정 HEAD의 병합을 명시적으로 승인하면 Owner 전용 `merge` 동작으로 Ready 전환·실제 CI 확인·일반 merge commit 병합을 수행합니다. 승인은 HEAD에 고정하고 불명확한 응답은 재전송하지 않습니다. 병합 후 실제 CI·ancestry·잠금 blob 확인 뒤에만 canonical ACCEPTED를 기록합니다.
- Mac canonical 노드는 독립 검토 → Draft 후보 게시 → 정확한 HEAD의 제품 CI → 필수 감사 적용 범위 → 최종 감리 순서로 진행합니다. Draft 게시만으로 완료·A3 PASS·사용자 검수 완료가 되지 않습니다.
- 어떤 봇에서도 호출할 수 있는 로컬 CLI와 stdio MCP.
- 기존 프로그램의 `handoff inspect/prepare/list/status` 인계 준비 경로. 원래 계획과 기록을 보존하며 실행 권한을 변경하지 않습니다. [사용과 한계](../docs/MAC_HANDOFF_PREPARATION_KO.md)를 확인하세요.
- 종료된 로컬 이력·단순 등록과 canonical 실행 승인을 구분합니다. [차단 조건과 남은 호스트 계약](../docs/MAC_OWNERSHIP_GUARD_KO.md)을 확인하세요.
- 명시적인 Mac 자체 호스트 초기화, 로컬 단일 예약, 원래 계획 고정, 작업별 외부 충돌 보존, 정상 중지와 같은 node의 감사·감리·PR·CI 연결 후보를 제공합니다. [Mac 호스트 계약과 완료 조건](../docs/MAC_NATIVE_HOST_KO.md)을 확인하세요. 기존 일반 UI의 시작 경로와 구분하며 설치/자동 활성화를 의미하지 않습니다.

복구 페이지나 복구 설치 패키지를 추가하지 않습니다. SQLite 작업 기록과 미확정 실행의 중복 방지는 정상 실행 기능입니다.

**출시 상태:** 설치 가능한 소스 후보입니다. 임시 원장·HTTP·Git·UI·실제 Mac 격리 검증과 실제 계정/제품의 종단 검증은 구분합니다. 이 문서나 앱 화면만으로 설치·호스트 이관·실제 모델 실행을 완료했다고 판단하지 마세요. Mac 자체 호스트 후보는 사용자 검수의 `INSPECTED`와 실제 병합·병합 후 CI를 확인한 node `ACCEPTED`를 구분합니다. 최종 SHA의 독립 검토·필요한 권한 경계 감사·실제 계정 검증 및 자동 dispatch 활성화는 별도입니다.

## 한 번만 준비하기

Mac에 Python 3.10 이상, Git, GitHub CLI(`gh`), 모델 설정에서 선택한 실행 도구가 필요합니다. 여섯 도구를 전부 설치할 필요는 없습니다. 각 도구는 공식 배포판을 사용합니다.

1. `gh auth login`으로 작업 레포에 접근할 GitHub 계정을 연결합니다.
2. 아래 표에 따라 선택한 CLI에 로그인합니다. 모델 사용량은 해당 계정 정책을 따릅니다. GLM의 Coding Plan 키는 OpenCode 자체 인증 저장소에 연결하며 AIOPS 화면에 입력하지 않습니다. 앱이 별도 API 과금 경로로 전환하거나 추가 사용량을 구매하지 않습니다.
3. 기존 AIOPS 호스트가 같은 canonical 작업을 관리하면 실행 승인·소유권을 먼저 확인해야 합니다. 앱은 전용 폴더에서 준비할 수 있지만, 등록된 보호 호스트 프로젝트·원래 프로그램·canonical 기록이 있으면 `HOST_ADMISSION_REQUIRED`로 모델 실행을 보류합니다. 단순 `aiops-task` 라벨은 소유권 증거가 아닙니다. 이 앱에는 보호 호스트의 원자적 admission·이관 경로가 아직 없습니다.
4. 이 디렉터리의 `Install.command`를 실행합니다. 터미널에서는 다음과 같습니다.

```bash
bash engineering/mac_app/Install.command
```

설치 프로그램은 `~/Applications/AIOPS.app`과 현재 사용자용 LaunchAgent를 만듭니다. 관리자 권한, 인터넷 포트 개방, 별도 서버 계약은 필요하지 않습니다. Python과 선택한 CLI는 기존 설치를 사용하므로 설치 후 삭제하지 마세요.

이미 설치했다면 다음 명령을 사용합니다.

```bash
bash engineering/mac_app/Install.command --update
```

업데이트는 기본적으로 모든 기존 작업이 **검수 완료 또는 취소됨**이고 실행 예약이 없을 때만 가능합니다. 새 Mac 세대의 종료된 대기·일시정지·검수 준비 작업은 아래 버전별 원본 영수증·소유권·독립 검토 조건을 모두 입증해야 예외적으로 허용합니다. 일반 대기·실행·종료 미확정 작업은 계속 거절합니다. 알려진 실행 작업이 있으면 기존 서비스를 멈추지 않습니다. 설치 파일 교체는 별도 staging에서 준비하며 실패하면 이전 앱과 LaunchAgent를 되돌립니다. 새 서비스가 이미 작업을 시작한 경우에는 소유권을 보존하고 자동 롤백을 멈춥니다. 작업 DB와 계정 토큰을 초기화하지 않습니다.

0.3.4부터 업데이트 전에 중지한 서비스는 업데이트 후에도 중지 상태로 유지합니다. 실행 중이던 서비스만 정상 종료 후 다시 시작하며, 로그인 자동 시작의 활성·비활성 설정은 변경하지 않습니다. 업데이트 성공은 제품 실행이나 기존 canonical 작업의 Mac 이관 완료를 의미하지 않습니다.

기존 일반 시작 경로는 Mac relay의 `UNKNOWN`·`SUBMITTED` 전달 영수증이 있으면 모델 실행을 보류합니다. Mac 자체 호스트 후보는 owner가 정확한 원본 digest의 작업 연관성을 보관한 경우 겹치는 작업만 보류합니다. 연관성이 없거나 원장을 읽을 수 없으면 안전하게 보류합니다. 두 경로 모두 원본을 읽기 전용으로 검사하며 새 요청 ID·앱 재시작·이슈 종료로 UNKNOWN을 해소하지 않습니다.

Claude Code는 기존 OAuth/키체인 로그인을 사용하는 일반 실행 모드에서 hooks·skills·MCP와 프로젝트 설정을 제외하고 역할별 도구 제한을 유지합니다. Devin은 설치된 로컬 CLI의 `auto` permission mode와 기존 역할별 allow/deny 설정을 사용합니다. 계정 토큰을 복사하거나 API 과금 경로로 전환하지 않습니다.

`launchctl print`의 서비스 인자·PID와 실제 프로세스를 비교해 기존 설치의 소유권을 확인합니다. 출력 형태를 확인할 수 없으면 안전하게 거절합니다. 이 출력 형태와 LaunchAgent 시작은 실제 Mac에서 확인해야 합니다. Mac의 Codex에 넘길 설치 지시는 [MAC_INSTALL_HANDOFF_KO.md](../docs/MAC_INSTALL_HANDOFF_KO.md)에 있습니다.

`AIOPS.app`을 열고 **연결**에서 상태를 확인한 다음 **모델 설정**에서 사용할 도구와 모델을 정합니다. Codex·Claude·Grok Build·Devin은 모델 ID를 비우면 해당 CLI의 계정 기본 모델을 사용합니다. Cursor와 GLM은 정확한 모델 ID를 필수로 입력합니다. CLI 버전·모델 가용성·계정 권한은 실제 Mac에서 최종 확인해야 합니다. 연결 화면의 **설치됨**은 로그인·실제 모델 호출 성공을 의미하지 않습니다.

| 실행 도구 | 실제 실행 경로 | 최초 계정 연결 | 모델 확인 |
|---|---|---|---|
| Codex CLI | `codex` | `codex login` | CLI의 `/model` |
| Claude Code | `claude` | CLI의 `/login` | CLI의 `/model` |
| Cursor | `agent` (없으면 `cursor-agent`) | `agent login` | `agent models`의 정확한 ID. `auto`·`default` 불가 |
| GLM | `opencode` + Z.AI Coding Plan | `opencode auth login` → **Z.AI Coding Plan** | `opencode models zai-coding-plan`의 `zai-coding-plan/glm-…` |
| Grok Build | 공식 xAI `grok` CLI | `grok login` | `grok models` |
| Devin | `devin` 로컬 CLI | `devin auth login` | `devin models list` |

Cursor에서 Grok 모델을 고르는 것과 네이티브 Grok Build는 다른 실행 경로입니다. GLM에는 임의의 OpenCode 공급자나 일반 API 경로를 넣지 않습니다. Devin은 기존 AIOPS 어댑터와 같은 **로컬 CLI**를 사용하며 `--cloud`나 원격 세션 생성 API를 호출하지 않습니다. 최초 기본 설정은 Codex이며, 선택은 사용자가 바꿀 수 있습니다.

설치 버전은 비대화형 실행·권한 제한·출력 형식을 지원해야 합니다. 특히 Cursor의 sandbox/ask mode, Grok의 sandbox/세션/JSON schema 옵션, Devin의 sandbox/ATIF export, OpenCode의 inline permissions와 JSON 이벤트를 확인합니다. 지원하지 않는 옵션이나 sandbox는 제한을 제거해서 실행하지 않고 설치 확인을 요청합니다. 같은 작업에서 모델을 바꾸는 방법은 아래 **새 모델 설정 적용**을 참조하세요.

Mac 전원과 네트워크는 유지합니다. 작업 중에는 전원이 연결된 Mac의 유휴 잠자기를 `caffeinate`로 방지할 수 있습니다. 덮개를 닫거나 로그아웃·종료하면 실행 환경이 중단될 수 있습니다. LaunchAgent는 사용자 로그인 세션에서 실행됩니다.

## 사용법

**작업실 → 레포 → 목표 → 개발 시작**으로 시작합니다. 목표를 비우면 레포의 공식 문서에 적힌 산출물을 기준으로 계획합니다.

계획은 자동으로 작성하고 일반적인 구현 방법은 개발자가 정합니다. 실패한 테스트, 감사·감리 지적, 새로운 기준 브랜치 변경, 실패한 CI는 같은 브랜치의 개발 작업으로 돌아갑니다. 개발 세션이 끝났다는 사실만으로 검수 준비 상태가 되지 않습니다. 현재 HEAD의 감사와 감리, 게시한 PR의 관측된 CI 결과가 구분되어 표시됩니다.

모델이 필요한 계정·권한을 얻을 수 없거나 기존의 중요한 계약·권한·범위를 바꿔야 하면 구체적인 질문 하나를 남기고 멈춥니다. 결과를 알 수 없는 실행은 자동으로 재전송하지 않습니다. 이는 사용량을 끝없이 소모하거나 두 개발자가 같은 작업을 동시에 수행하지 않기 위한 실행 경계입니다.

0.3에서는 권한·로그인·설치 오류와 구독 사용 한도를 일시적 연결 장애와 구분합니다. 일시적 장애는 같은 모델로 간격을 늘려 자동 재시도합니다. 분류되지 않은 같은 환경 오류가 세 번 반복되면 구체적인 확인을 한 번 요청합니다. 일반적인 코드·테스트 실패에는 이 중단 기준을 적용하지 않으며 같은 개발자가 보완을 계속합니다. 같은 지적과 같은 HEAD가 반복되면 짧은 대기를 적용합니다. 계정 사용 한도가 갱신되거나 연결 문제가 해결되면 **연결 확인 후 계속**을 누릅니다.

미확정 실행에는 새 작업을 발사하지 않습니다. 나중에 동일 실행의 정상 종료 기록이 도착하면 연결·종료·공급자·출력의 일치를 다시 검증하고 기존 작업을 이어갑니다. 오류와 성공이 함께 있는 기록, 0이 아닌 종료 코드의 성공 보고, 미해결 지적이나 빈 검증 근거가 있는 완료 보고는 다음 단계로 통과하지 않습니다. 로그에 출력이 나타났다는 사실은 실제 개발 진척이나 완료 증명이 아닙니다.

프로그램 가져오기는 **로컬 명세의 coverage**입니다. 승인 포인터를 보호된 감사 영수증으로 해석하지 않으며, 외부 의존성 형식은 아직 지원하지 않습니다. 레포의 별도 승인 대기 카탈로그·실환경·A3·릴리스·병합된 선행 작업 조건은 유지됩니다. 앱의 단일 draft 산출물은 기존 호스트의 노드별 DONE/병합 절차와 동일한 실행이 아닙니다. 관련 경계와 회귀 검증은 [MAC_APP_RELIABILITY_KO.md](../docs/MAC_APP_RELIABILITY_KO.md)에 정리했습니다.

`검수 준비`에서 PR과 결과를 확인하고 **검수 완료** 또는 **수정 요청**을 선택합니다. CI 확인 중 및 검수 버튼을 누를 때 로컬 HEAD·독립 검토·최신 기준 브랜치를 다시 확인하고, 게시된 PR의 최신 CI를 검사합니다. 달라진 코드나 실패한 CI는 보완 단계로 돌아갑니다. `검수 완료`는 확인한 HEAD의 사용자 검수 기록입니다. 기존 AIOPS의 보호된 A3/릴리스 승인, 운영 배포, main 병합 및 병합 후 CI를 대체하지 않습니다.

기존 작업의 모델을 바꾸려면 먼저 일시정지하고 모델 설정을 저장한 뒤, 작업 상세의 **새 모델 설정 적용**을 누릅니다. 실행이 완전히 끝난 상태에서만 바꾸며 이전·새 설정을 기록하고 필요한 검토를 다시 합니다. 중요한 계약 결정 질문은 모델 변경만으로 해소되지 않습니다.

## 봇에서 시작하기

앱의 **연결** 화면에 실제 설치 경로를 반영한 명령과 MCP 설정이 표시됩니다. 같은 맥에서 실행할 수 있는 봇이면 종류와 관계없이 호출할 수 있습니다.

```bash
python3 engineering/mac_app/aiops.py start \
  --repo BeautifulMind-JT/ZARI \
  --goal '레포의 산출물을 완성하고 최종 검수할 수 있게 해 줘' \
  --request-id zari-delivery-001

python3 engineering/mac_app/aiops.py list
python3 engineering/mac_app/aiops.py status JOB_ID
python3 engineering/mac_app/aiops.py pause JOB_ID
```

같은 명령 전달을 재시도할 때는 **같은 `request-id`**를 사용합니다. 이미 있는 작업을 조회하며 새 작업을 만들지 않습니다. 같은 ID로 다른 목표를 보내면 거절합니다.

로컬 MCP에는 `aiops_start`, `aiops_list`, `aiops_status`, `aiops_pause`가 있습니다. 봇용 인터페이스에는 최종 검수 승인·모델 설정 변경·임의 셸·병합·배포 기능이 없습니다. 다른 기기의 봇은 사용자가 연결한 SSH로 맥의 CLI를 실행합니다. 서비스는 `127.0.0.1`에서만 수신합니다.

## 실행 구조와 기존 AIOPS의 관계

이 앱은 사용자가 2026-10-02 지정한 **Mac 로컬 개발 및 최종 검수**를 위한 추가 실행 모드입니다. 기존 Linux 보호 호스트의 `/proc`, `setpriv`, 고정 Fable 실행 파일·권한을 macOS로 옮긴 것으로 가장하지 않습니다. 네이티브 CLI의 sandbox와 역할별 도구 제한 안에서 별도 작업 브랜치를 개발하며, 기존 Linux 보호 서비스의 서명·A3 영수증을 발급하지 않습니다.

기존 호스트 실행을 그대로 Mac 소유의 Linux VM으로 옮기는 호환 경로는 `../mac_host/README_KO.md`에 분리되어 있습니다. `.github/control-plane/activation.json`, 기존 모델 권한, 보호된 호스트 원장, 실제 runner 설정은 이 앱 설치로 변경되지 않습니다. 기존 호스트와 이 앱의 동일 레포 동시 실행은 지원하지 않습니다.

자세한 범위·사용자 결정·검증 기준은 `../docs/MAC_APP_AUTONOMY_KO.md`를 참조하세요.

## 개발·검증

0.3.14는 최초 Codex trust preflight가 모델 시작 전에 정상 종료됐다는 private 영수증·정확한 binding·SDK 근거가 모두 일치할 때만 빈 작성자 ID를 분류합니다. 실패 기록과 작업의 작성자 목록은 보존하고, 실제 작성 세션은 독립 검토에서 계속 제외합니다. 모델이 시작됐거나 종료·근거가 미확인인 빈 ID는 계속 차단합니다.

0.3.15는 native builder·reviewer에게 발행 전 구현·코드 검토의 범위를 명시합니다. 로컬 sandbox 거절이나 미지원 Python 검사는 PASS로 기록하지 않습니다. 독립 코드 검토 후 Draft와 실제 exact-head CI를 만들고 별도 최종 감리가 전체 검증을 확인하는 기존 순서를 유지하며, 모든 필수 검토·CI·Astra·사용자 게이트는 Ready와 완료를 계속 차단합니다.

0.3.16은 외부에서 먼저 일반 병합된 native PR의 정상 Owner 검수를 지원합니다. `accept`에 실제 사용자 승인 근거와 정확한 HEAD를 명시해야 하며, 원본 독립 리뷰·감리, 최신 exact-head CI, 병합 커밋의 두 번째 부모·main ancestry·잠금 blob·병합 후 CI를 모두 확인합니다. 원래 검토 HEAD를 변경하거나 main을 체크아웃에 합치지 않습니다. 승인만 `INSPECTED`를 기록하고 별도 정상 reconciliation이 `ACCEPTED`를 기록합니다. 미래 자동 승인 정책을 설정하지 않습니다. 종료된 새 Mac 세대의 `ready` 작업도 모든 원본 영수증·독립 검토·CI·깨끗한 HEAD가 입증된 경우에만 지원 업데이트를 허용하며 검수 상태와 소유권은 유지합니다.

0.3.17은 이미 병합된 PR의 Owner 검수에서 검토 HEAD의 GitHub Actions 결과를 직접 확인합니다. 닫힌 PR을 열린 Draft 후보로 조회해서 `STALE_REMOTE_HEAD`로 거절하던 경로를 고칩니다. 열린 후보의 Draft 조건, 명시적 승인, 독립 검토와 실제 병합 커밋의 ancestry·잠금·CI 검증은 유지합니다.

추가 Python 패키지 없이 앱 자체를 실행할 수 있습니다.

```bash
python3 engineering/mac_app/aiops.py --data-dir /tmp/aiops-local-test serve --port 8765
# 다른 터미널
python3 engineering/mac_app/aiops.py --data-dir /tmp/aiops-local-test open
python3 -m unittest discover -s engineering/scripts -p 'test_control_plane_mac*.py' -v
```

서비스의 상태 디렉터리는 사용자 전용이어야 합니다. 토큰·작업 DB·세션 기록·로컬 체크아웃은 저장소에 커밋하지 않습니다. 공식 CLI의 비대화형 실행 계약을 사용합니다. 공급자별 연결·결과 검증·기존 AIOPS와의 대응은 [PROVIDERS_KO.md](PROVIDERS_KO.md)에 설명합니다.

- [Codex 비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex CLI 옵션](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Claude Code 비대화형 실행](https://code.claude.com/docs/en/headless)
- [Claude Code CLI 옵션](https://code.claude.com/docs/en/cli-reference)
- [Claude Code sandbox](https://code.claude.com/docs/en/sandboxing)
- [Cursor CLI](https://cursor.com/docs/cli/overview)
- [Z.AI Coding Plan의 OpenCode 연결](https://docs.z.ai/devpack/tool/opencode)
- [Grok Build CLI](https://docs.x.ai/build/cli/reference)
- [Devin CLI](https://docs.devin.ai/cli)
