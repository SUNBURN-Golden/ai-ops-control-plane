# 감사 호스트 복구 결정 기록

## HR-D3 — 2026-10-01

결정자: BeautifulMind-JT(대표). 근거는 [#49 직접 댓글](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/49#issuecomment-5933778620)이다.

- 범위는 aiops-fable 감사 호스트다. QUALIFIED_RESUME는 보류한다.
- 정상 복구는 상태 암호 1회. Claude 브라우저 승인은 최초 및 만료 재발급 때만 한다.
- 전체 원장과 토큰은 private aiops-state 한 브랜치에 암호화한다. 암호는 저장하지 않는다.
- 결과 불명확 실행은 UNKNOWN으로 멈추며 자동 재실행하지 않는다.
- 모델 preflight와 최소 토큰 검증은 y/N 뒤에만 진행한다.
- 구현 권한은 호스트·설치·감사·병합·activation 실행 권한이 아니다.

## 8e40fe0 검증 후 F1~F7 수정 지시

이번 사용자 수정 지시는 다음 예외·구체화를 승인한다.

- 과금은 원본 Fable stream 가드에 맡긴다. 비모델 helper 요구는 삭제한다.
- 로그인 URL은 출력할 수 있다. 접힌 URL은 pyte로 이어 붙이고 승인 코드를
  마스킹 입력한다. 토큰과 나머지 CLI 화면은 출력하지 않는다.
- 401은 구조화된 오류로만 판단한다. 정상 결과에 포함된 401은 그대로 둔다.
- RESULT/NOT_STARTED/UNKNOWN은 실제 결과·모델 시도로 분류한다. --again은
  대표의 y/N 확인 뒤 1회 실행 권한이며 자동 재실행 권한이 아니다.
- preflight는 매번 확인 후 실행한다.
- root 직접 고정 bootstrap 블록을 제공한다. 새로운 sudo 규칙은 없다.
- 최초 등록은 브랜치를 생성 전용 CAS로 만든다. 기존 기록 없이 등록할 때는
  별도 y/N을 받는다. restore에서 빈 원장 fallback을 하지 않는다.
- Debian13/Python3.13/glibc2.41에 맞추고 숫자 UID/GID 대신 감사 소유 역할을
  저장한다. keyring 불가 시 root 전용 tmpfs 파생 키를 사용한다.

기준은 main `a964c0d72285a752cb567c39bfc4a7fb83ff3eae`이며 원본 Fable 바이트·low
설정·PA-1 sudo 명령 1개를 보존한다. HR-D1/HR-D2의 넓은 runner 복구는 역사적
설계이며 이번 범위에 섞지 않는다.

NOT_READY 사유: 실호스트 시운전 전. 대표의 남은 작업은 병합 뒤 커밋 핀 갱신
(병합은 이번 요청에서 금지), private README 저장소 최초 생성(코드에 생성
권한을 위임하지 않음)이다. 독립 A3·병합은 기존 저장소 절차를 따른다.
