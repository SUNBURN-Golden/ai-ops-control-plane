## Owner decision D-2026-10-06-MAC-A3-RECEIPT (박준태, 2026-10-06 16:10 KST)

Mac A3 감사 결과 전달 방식(PR 본문의 DECISION_REQUIRED 항목)을 다음과 같이 정한다.

1. **감사 주체(protected producer):** 리눅스 AIOPS 호스트의 보호된 감사 도구 `/opt/aiops/bin/aiops-fable`(root 소유)이 Mac PR을 감사한다. Mac 자체에서 감사 결과를 만들지 않는다.
2. **전달 경로(result channel):** 감사 도구가 해당 PR에 남기는 GitHub 댓글. 본문은 `<!-- aiops-fable-audit -->`로 시작하고 둘째 줄이 `ASTRA_AUDIT_V1 pr=<번호> head=<40자 sha> result=<결과> depth=<깊이> auditor=ASTRA_FABLE session=<id>` 형식이다.
3. **Mac 검증 규칙(전부 만족해야 수용, 하나라도 어긋나면 fail-closed):**
   - 인증된 GitHub API로 댓글을 직접 다시 읽는다(로컬 사본이나 붙여넣은 텍스트는 인정하지 않음).
   - 작성자 login/id가 `BeautifulMind-JT` / `263336091`이다.
   - `pr`과 `head`가 Mac 작업의 정확한 delivery HEAD와 PR 번호에 일치한다.
   - `result`가 `PASS` 또는 `PASS_WITH_NOTES`이고 `depth`가 요구 깊이 이상이다.
   - 댓글 id, URL, 본문 SHA-256을 receipt에 기록하고, 수용·시작·병합 직전마다 다시 읽어 본문 해시가 같은지 확인한다. 수정·삭제된 댓글, head 불일치, 같은 head에 대한 상충 결과는 거절한다.
4. 새 신뢰 키·credential·authority는 만들지 않는다. D-2026-10-06-MAC-HOST의 결정 댓글 바인딩과 같은 방식이다.
5. 리눅스 호스트 파일(`control_plane_host*`, hostpack, boundary, sudoers)과 aiops-fable 자체는 이 결정으로 바꾸지 않는다.
