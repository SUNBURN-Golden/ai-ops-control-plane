# agents-scope-sync의 제한된 User 승인 checkpoint

설치 main `a9287d888101ae6ae887d915988e209889af4498`의 checkpoint는
AGENTS.md 변경을 일괄 차단하며 roadmap-sync의 별도 두 줄 변경만 수용한다.
원래 agents-scope-sync는 AGENTS §5에 이미 승인된 R-4/R-6/R-2 pointer를 반영하는
노드인데 이 지원 case가 없어 complete/exit0 산출물이 AUTHORITY_EDIT_NEEDS_USER로 멈췄다.

User 메시지 `Sentinel_c63c8cb4e6f88191b0d5faa594be58a5`가 제품 산출물·commit/push/PR을,
`Sentinel_18579c8bc67c8191a70e193d38fbd27e`가 선행 Mac source 수리를 명시 승인했다.
아래 case는 기존 일반 보호를 해제하거나 새 제품 범위를 만들지 않는다.

| 고정 scope | 값 |
| --- | --- |
| repo / node / job | BeautifulMind-JT/kix-protocol / agents-scope-sync / 20531604498943e1 |
| branch / path | aiops/native-2972272cbeb64071 / AGENTS.md |
| 원래 plan commit | 7481b0e16ce9b903abbffa62249bb91cd9e63cfe |
| plan blob | ff0f39a8129ca8b8d30818cce35c3d4e588872fc |
| 전체 spec SHA-256 | df27b538c6a1decf216505fe516cd906d31471289d45ca13d316b344b83fc579 |
| 변경 전 Git blob | 5ef2f06dcfe35f3a53ea8e7d021aba8bc38f121e |
| 변경 후 Git blob | 190bcae4f0c7d60666b942dd7df9e87749a09297 |

정확한 전체 파일 bytes를 고정해 AGENTS §5의 보존된 29줄 밖 변경도 거절한다.
repo/job/branch/MAC_LOCAL program-node tuple/plan/spec/path/blob, 원래 A3·user_merge와
auto_merge=false를 검증한다. 다른 job/node/file/revision, 혼합 변경, UNKNOWN/활성 attempt,
symlink/hardlink/실행 mode, 변형된 bytes는 수용하지 않는다.

## 승인 입력

실제 User의 [승인 댓글 6018278031](https://github.com/BeautifulMind-JT/kix-protocol/issues/92#issuecomment-6018278031)을
authenticated GitHub GET으로 읽어 원문 348 UTF-8 bytes와 SHA-256
`000fa33005152da022795f01ef3ab91b3d4d085a81454b41907f8fd44488e951`을 직접 계산했다.
actor는 BeautifulMind-JT / 263336091 / User, 생성·수정은 모두 2026-10-06T14:18:45Z다.
[정확한 API 본문 사본](MAC_AGENTS_SCOPE_APPROVAL_6018278031.md)과
[별도 metadata](MAC_AGENTS_SCOPE_APPROVAL_6018278031_RECORD.json)를 보존했다.
ID·repo/issue URL·actor login/numeric ID/type·생성/수정 시각·본문 bytes/hash를 고정하고
checkpoint와 push 직전에 다시 읽는다. 수정·삭제·외부 actor·다른 issue·읽기 실패는
AUTHORITY_EDIT_NEEDS_USER 보류를 유지한다. 자유 resume answer나 모델 text, fixture는
승인 입력이 아니다. 기존 #92 body/control/confirmation은 수정하거나 닫지 않는다.

댓글에 실제 있는 범위는 node/job/branch, AGENTS.md §5, 전후 blob, plan commit,
commit/push/PR 승인과 A3 감사·병합 유지다. plan blob·spec hash·일반 Mac authority
예외는 댓글에 없으므로 댓글 원문으로 주장하지 않는다. source 수리 승인은 위 별도
명시 User 메시지에, plan blob/spec는 보존된 원래 plan과 source의 추가 축소 조건에 따른다.
User 메시지 `Sentinel_189daa51ad4481918cf45ff2f1306cc0`은 이 실제 댓글 pin 후
같은 source 브랜치의 commit/push/CI/독립 검토 진행을 명시 승인했다.

이는 지정 User 계정의 승인 권한을 신뢰하는 Mac 한정 policy 입력이다.
본문 hash는 불변성을 검증하며 host 서명이나 Linux ledger 읽기 증거가 아니다.
계정 token 보유자가 댓글을 위조할 수 있다는 기존 actor 신뢰 한계가 남는다.
새 credential/key/계정 권한 또는 Linux admission 예외를 만들지 않는다.

GitHub 조회 후 binding/scope/변경 경로와 실제 파일 bytes를 다시 확인해 네트워크
대기 중 추가된 변경을 수용하지 않는다. 30초 공유 API read budget을 사용한다.
checkpoint 후와 게시 전에는 실제 commit tree의 AGENTS.md blob·100644 mode와
원래 plan 대비 단독 변경도 확인한다. Git filter·assume-unchanged·core.filemode가
working tree를 clean으로 보여도 commit tree 검증을 대신할 수 없다.
게시 경로는 repository 조회 후 승인 댓글을 마지막으로 읽고, 이후 정확한 job HEAD·
clean tree·binding/scope/file을 재검증한다. push source를 이동 가능한 HEAD가 아닌
그 검증된 SHA로 고정해 뒤의 ref 이동이 다른 commit을 게시하지 못하게 한다.
이 read는 기존 checkpoint/게시 경로이며 모든 변경·예약과 GitHub 댓글을 원자적으로
잠그는 새 장치가 아니다. checkpoint 성공이 실제 GitHub 게시 완료나 감사 PASS를 뜻하지 않는다.

## 정상 회수

이 수리는 기존 Store.action('resume')의 checkpoint_retry 및
Engine.retry_checkpoint를 그대로 사용한다. source merge·정상 지원 업데이트 뒤 제품
owner가 기존 job을 resume하면 같은 private 완료 request/receipt·HEAD·plan/task·profile/
session·종료 증거를 재검증한다. 기존 writer와 호출 수를 보존하며 builder를 재호출하지 않는다.
독립 검토·Draft PR·동일 HEAD CI·A3 감사·User merge는 각각 정상 gate로 남는다.
제품 checkout/원장/기존 영수증을 수동 수정하거나 source gate를 우회하지 않는다.

Linux protected host/hostpack/boundary/sudoers/Fable, 기존 prestart 판정·UNKNOWN·ownership
fence와 roadmap-sync 예외는 변경하지 않는다. 설치·제품 재개는 제품 owner 담당이다.
