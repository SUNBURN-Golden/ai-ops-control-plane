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
- 어떤 봇에서도 호출할 수 있는 로컬 CLI와 stdio MCP.

복구 페이지나 복구 설치 패키지를 추가하지 않습니다. SQLite 작업 기록과 미확정 실행의 중복 방지는 정상 실행 기능입니다.

**출시 상태:** 설치 가능한 소스 후보입니다. 오프라인 실행·HTTP·Git·UI 검증과 실제 Mac/실제 계정의 종단 검증은 구분합니다. 이 개발 환경에는 사용자의 Mac, CLI 로그인 및 기존 Grok 호스트가 연결되어 있지 않습니다. 이 문서나 앱 화면만으로 설치·호스트 이관·실제 모델 실행을 완료했다고 판단하지 마세요.

## 한 번만 준비하기

Mac에 Python 3.10 이상, Git, GitHub CLI(`gh`), 모델 설정에서 선택한 실행 도구가 필요합니다. 여섯 도구를 전부 설치할 필요는 없습니다. 각 도구는 공식 배포판을 사용합니다.

1. `gh auth login`으로 작업 레포에 접근할 GitHub 계정을 연결합니다.
2. 아래 표에 따라 선택한 CLI에 로그인합니다. 모델 사용량은 해당 계정 정책을 따릅니다. GLM의 Coding Plan 키는 OpenCode 자체 인증 저장소에 연결하며 AIOPS 화면에 입력하지 않습니다. 앱이 별도 API 과금 경로로 전환하거나 추가 사용량을 구매하지 않습니다.
3. 기존 AIOPS 호스트가 같은 레포를 실행 중이면, 기존 작업·미확정 실행을 확인하고 소유권 이관을 먼저 마칩니다. 이 앱은 다른 호스트의 보호 원장을 읽거나 이전 호스트를 종료할 권한을 가지고 있지 않습니다. 열려 있는 `aiops-task`가 발견되면 새 소유자를 만들지 않습니다.
4. 이 디렉터리의 `Install.command`를 실행합니다. 터미널에서는 다음과 같습니다.

```bash
bash engineering/mac_app/Install.command
```

설치 프로그램은 `~/Applications/AIOPS.app`과 현재 사용자용 LaunchAgent를 만듭니다. 관리자 권한, 인터넷 포트 개방, 별도 서버 계약은 필요하지 않습니다. Python과 선택한 CLI는 기존 설치를 사용하므로 설치 후 삭제하지 마세요. 앱 업데이트는 실행 작업이 없는 상태에서 진행해야 합니다.

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
