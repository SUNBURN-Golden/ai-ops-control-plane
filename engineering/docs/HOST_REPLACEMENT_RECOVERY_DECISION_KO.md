# 감사 호스트 복구 결정 기록

## HR-D3 — 2026-10-01

결정자: BeautifulMind-JT(대표). 근거는 [#49 직접 댓글](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/49#issuecomment-5933778620)이다.

- 이번 범위는 aiops-fable 감사 호스트다. 레인/runner 자동 재개 QUALIFIED_RESUME는 보류한다.
- 복구 때 상태 암호 한 번을 입력한다. Claude 브라우저 승인은 최초 발급과 토큰 만료 재발급 때만 한다.
- 토큰과 전체 원장을 비공개 aiops-state 한 브랜치의 암호화 체크포인트에 넣는다. 암호는 대표가 보관하고 도구는 저장하지 않는다.
- 시작 기록이 있고 결과가 불명확하면 UNKNOWN으로 멈춘다. 자동 재실행하지 않는다.
- 모델을 호출하는 preflight와 최소 토큰 검증은 y/N 확인 뒤에만 진행한다.
- 설치·activation·감사·병합을 이번 구현 작업의 권한으로 해석하지 않는다.

HR-D1/HR-D2에서 제안한 범용 호스트 복구와 QUALIFIED_RESUME는 역사적 설계다.
이번 감사 전용 구현에는 HR-D3를 적용하며, 보류 범위의 sender epoch·runner
재개·예약 해제 요구를 감사 전용 복구의 완료 조건으로 섞지 않는다.

기준 소스는 병합된 main `a964c0d72285a752cb567c39bfc4a7fb83ff3eae`다.
#49 브랜치에는 기존 HEAD를 보존한 main merge 위에 구현한다.
원본 aiops-fable과 low 설정은 보존하고 별도 고정 wrapper를 추천안으로 선택했다.
PA-1의 sudo 허용 명령 1개는 변경하지 않는다.

배포 전 대표 확인 항목: 초기 보호 bootstrap 이미지, GitHub 접근 수단,
검증된 비모델 과금 helper, 고정 UID/GID와 플랫폼, 최초 비어 있지 않은 원장
등록 및 최종 HEAD의 독립 A3. [설계](HOST_REPLACEMENT_RECOVERY_DESIGN.md)의
미해결 전제가 남아 있는 동안 상태는 NOT_READY다.
