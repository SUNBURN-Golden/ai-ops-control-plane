# Mac 실행 호스트 채택의 조건

사용자 결정 [D-2026-10-06-MAC-HOST](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/77#issuecomment-6008874154)는
[ea5f3c08 A3 FAIL](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/77#issuecomment-6008601206)의
F1·F2에 대한 결정이다. Mac은 정식 실행 호스트 후보이며 다음 조건을 유지한다.

두 호스트가 같은 작업의 승인 기록을 공유하는 admission authority는 아직 구현하지
않았다. 따라서 두 보호 runtime workflow는 기존 main 상태로 복원하고 `aiops-macbook`
라우팅을 제거했다. relay는 `current`가 기본이며 명시적인 `macbook` 요청도 전송과
전달 예약 전에 거절한다. runner 이름을 dispatcher 입력과 비교하는 것은 권한 증거가
아니다. 공유 승인 설계·정확 설치 SHA 감사·qualification 없이 경로를 활성화하지 않는다.

MAC-* generation의 `decision`에는 위 결정 댓글의 정확 URL을 넣어야 한다. 앱이
인증된 GitHub API로 댓글 ID, 이슈, 사용자 actor ID/login, 본문 SHA256을 확인한다.
그 증거는 immutable generation과 work hash에 고정하고 시작과 claim 직전에 다시
읽는다. 자유 텍스트, 다른 댓글, 편집·삭제된 결정과 기존 무검증 generation은 거절한다.
기존 기록은 자동으로 승인하거나 수정·이관하지 않는다.

원래 프로그램 노드의 열린 canonical Linux 이슈는 소유권 hold다. owner 선언이
본문 밖의 보호 원장·댓글에 있거나 node를 판별할 수 없는 열린 이슈도 fail-closed로
거절한다. adoption, 시작 전, checkout 준비 후 claim 전에 전체 GitHub 관측을 대조한다.
같은 원래 노드에 두 번째 Mac generation/등록 task도 SQLite single-writer transaction
안에서 canonical 대소문자를 정규화해 양쪽 등록 순서 모두 거절한다.
GitHub 관측은 cross-host atomic lock 증거가 아니며 공유 admission authority를
구현했다고 주장하지 않는다. 이 검사는 명시적 Linux 이관·기존 실행 종료 증거를 만들지 않는다.

새 source HEAD의 A3 재감사는 별도로 필요하다. 이 문서는 감사 PASS, 실제 설치,
VM 활성화 또는 보호 호스트 admission qualification을 주장하지 않는다.
