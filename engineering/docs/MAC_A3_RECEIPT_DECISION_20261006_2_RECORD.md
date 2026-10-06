# Mac A3 영수증 후속 결정의 인증 기록

[D-2026-10-06-MAC-A3-RECEIPT-2](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/80#issuecomment-6015244412)는
[PR80 공식 A3 DECISION_REQUIRED](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/80#issuecomment-6014644349)에 대한
User의 **A: 세 완화 모두 승인** 결정이다. [원래 결정](MAC_A3_RECEIPT_DECISION_20261006.md)의 Mac 계약만 개정하고 Linux gate는 유지한다.

| 항목 | 인증 API에서 확인한 값 |
| --- | --- |
| 댓글 ID / PR | `6015244412` / `BeautifulMind-JT/ai-ops-control-plane#80` |
| Actor | `BeautifulMind-JT` / numeric ID `263336091` / `User` |
| 생성·수정 시각 | 모두 `2026-10-06T11:25:59Z` |
| 본문 UTF-8 크기 | `1843` bytes |
| 직접 계산한 SHA-256 | `3bea1df70664b5ce8713d627291e7a64f0c4b5162e7afee50697ac14a1479cb4` |
| 정확한 본문 사본 | [MAC_A3_RECEIPT_DECISION_20261006_2.md](MAC_A3_RECEIPT_DECISION_20261006_2.md) |

인증된 `gh api repos/BeautifulMind-JT/ai-ops-control-plane/issues/comments/6015244412`의 body를
UTF-8로 인코딩해 직접 hash를 계산했다. Actor ID/type, 댓글 URL과 issue URL의 repository/PR,
댓글 ID를 확인했고 계산값은 User가 제시한 hash와 일치했다. 사본은 본문 bytes와 정확히 같으며
제목·metadata·개행을 덧붙이지 않았다. 사본의 SHA-256도 위 값이다.

F1은 최대 30초 먼저 시작한 미소비 run 허용, F2는 다른 gate/요구보다 낮은 depth의 부정 결과까지
제외, F3는 지정한 기존 hold를 역사 기록으로 돌리는 이관 자체의 승인을 기록한다. B/C 대안을
적용하지 않는다. 세부 수용 조건과 F4/F5/F6/F8의 한계는 [Mac 전달 계약](../mac_app/MAC_A3_RECEIPT_KO.md)에 기록한다.

이 기록은 source 정책 변경의 durable 승인과 공식 재감사 입력이다. 앱의 runtime decision pin에
이 후속 문서를 새 수용 입력으로 추가하지 않으며, 공식 재감사·병합·실제 설치/제품 실행 결과를 뜻하지 않는다.
