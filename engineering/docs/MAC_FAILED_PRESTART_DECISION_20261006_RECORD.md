# Mac FAILED_PRESTART 비소유 예외의 인증 기록

[D-2026-10-06-MAC-FAILED-PRESTART-NONOWNERSHIP](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016578811)은
[PR81 공식 A3 DECISION_REQUIRED](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016559549)의
**A: 구현된 예외 승인** 결정이다. 제어 기록, 사용자 직접 확인 전달, 예외 정책 결정은
서로 다른 근거이며 하나의 기계 영수증으로 합치지 않는다.

| 댓글 | 본문 사본 | UTF-8 bytes | 직접 계산한 SHA-256 |
| --- | --- | --- | --- |
| kix-protocol #92 / 5956874897 | [제어 기록](MAC_FAILED_PRESTART_CONTROL_5956874897.md) | 526 | `867801b42e1644962560c4b6dd230e67f51b8d604e5970effacd00af03315960` |
| kix-protocol #92 / 6016190250 | [사용자 확인 전달](MAC_FAILED_PRESTART_CONFIRMATION_6016190250.md) | 2151 | `4369ebe024b764413ff4a9ca22bff6536a80f0369ddd6232a718af1d28bd1cc1` |
| ai-ops-control-plane PR81 / 6016578811 | [사용자 A 결정](MAC_FAILED_PRESTART_DECISION_20261006.md) | 1664 | `1fad73dade5435130c0576315939310e7559250d3813359fd21077aaedf103e7` |

세 댓글 모두 인증된 `gh api --method GET repos/<repo>/issues/comments/<id>`의
실제 `body`를 UTF-8로 인코딩해 직접 hash를 계산했다. Actor login
`BeautifulMind-JT`, numeric ID `263336091`, type `User`, comment ID,
html URL 및 issue URL의 repository/issue 또는 PR를 확인했다. 결정 댓글의
계산값은 사용자가 제시한 SHA-256과 정확히 일치했다. 사본에는 제목·metadata·
개행을 덧붙이지 않았으며 API 본문 bytes와 완전히 같다.
생성/수정 시각과 URL 등은 [별도 JSON metadata](MAC_FAILED_PRESTART_DECISION_20261006_RECORD.json)에 보존한다.

5956874897은 같은 task/revision/request/attempt의 FAILED_PRESTART와 명시적
null owner/session을 나타내는 GitHub 제어 projection이다. 6016190250은 사용자가
Linux를 직접 확인했다는 전달이며, 원출처는 부모 대화
`01a0f57c-0df9-72ce-bb48-d1aaf753d004`, 메시지
`Sentinel_a77166906c0c819184c3400304e90d93`이다. 6016578811은 해당 구현을 승인하고
인증된 Mac host read 없이 소유자 확인을 신뢰하는 위험을 수용하는 정책 결정이다.
Hash는 내용 불변성이지 보호 호스트 서명이 아니며 계정 토큰 보유자는 댓글을 위조할 수 있다.

이 기록은 [ownership guard](MAC_OWNERSHIP_GUARD_KO.md)와 `AGENTS.md` §11의
Mac-only 예외 및 공식 재감사 입력이다. 새 runtime decision pin이나 host authority를
만들지 않고 현재 issue/control/confirmation의 authenticated live 검증을 대체하지 않는다.
F3의 lock/network I/O 한계는 [Mac admission 문서](../mac_app/MAC_FAILED_PRESTART_ADMISSION_KO.md)에
정확히 기록했다. Linux 보호 코드·원장·서명·감사 receipt, 다른 task의 보류 및 정상
review/CI/Astra/User merge 계약은 유지한다. 재감사·병합·설치·제품 실행 완료를 뜻하지 않는다.
