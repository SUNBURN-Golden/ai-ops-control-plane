# Mac의 Codex에 전달할 설치 지시

아래 지시를 Mac에서 실행하는 Codex에 전달한다. GitHub PR #55는 AIOPS Mac 소스 후보이며, 기존 Grok 호스트의 실행 권한을 자동 이관하지 않는다.

> BeautifulMind-JT/ai-ops-control-plane의 PR #55 최신 소스를 확인하고 AIOPS Mac을 이 Mac에 설치해 줘. 작업 전 PR의 실제 HEAD와 해당 HEAD의 CI 통과 여부를 확인하고 정확한 SHA를 기록해. 기존 체크아웃에 내 변경이 있으면 덮어쓰지 말고 별도 디렉터리를 사용해. engineering/AGENTS.md, engineering/mac_app/README_KO.md, engineering/docs/MAC_APP_AUTONOMY_KO.md와 MAC_APP_RELIABILITY_KO.md를 읽고 진행해. Python 3.10+, Git, gh와 내가 선택한 CLI의 설치를 점검해. 기존 AIOPS.app이 있으면 설치 바인딩·작업 상태를 먼저 검사하고 지원하는 --update를 사용해. 새 설치면 Install.command를 실행해. sudo, 작업 DB 초기화, 임의 PID 종료, sandbox 제거, 다른 모델·계정으로의 전환은 하지 마. 서비스와 UI가 열리는지, 로컬 CLI/MCP가 응답하는지, 로그인 전/후 상태가 정확한지 확인해. 계정 로그인·사용량 구매·중요한 계약 결정처럼 내가 직접 해야 하는 것만 호출하고 나머지는 알아서 끝내 줘. 기존 Grok/VM 작업은 취소하거나 새로 발사하지 말고, 동일 레포의 실행 소유권이 이관되기 전에는 자동개발을 시작하지 마. 실제 Mac에서 확인한 사실과 아직 확인하지 않은 공급자·기존 호스트 이관을 구분해서 보고해.

직접 터미널에서 설치할 경우 새 디렉터리에서 진행한다.

```bash
gh repo clone BeautifulMind-JT/ai-ops-control-plane aiops-mac-source
cd aiops-mac-source
gh pr checkout 55
gh pr checks 55
git rev-parse HEAD
bash engineering/mac_app/Install.command
open ~/Applications/AIOPS.app
```

CI가 다른 HEAD를 검사했거나 실패·진행 중이면 설치를 멈추고 해당 결과를 확인한다. 기존 설치의 업데이트 명령은 `bash engineering/mac_app/Install.command --update`다. 실행 중·미확정·검수 준비 작업을 업데이트 때문에 취소하지 않는다.

앱의 **연결**에서 GitHub 로그인과 선택한 CLI 연결을 확인하고 **모델 설정**을 저장한다. 여섯 CLI를 모두 설치할 필요는 없다. 로그인 창에서의 계정 선택과 인증은 사용자가 직접 한다. 로그인 정보를 대화나 저장소에 복사하지 않는다. 이후 **작업실**에 레포·목표를 넣거나 연결 화면의 CLI/MCP 설정을 봇에 등록한다.

첫 실기기 검증은 기존 writer가 없는 테스트 레포 한 건으로 계획→개발·수정→독립 감사·감리→draft PR·CI→사용자 검수까지 확인한다. 이 확인 전에는 전체 네 레포의 무인 완주나 기존 보호 호스트 이관 완료를 선언하지 않는다.
