# Mac roadmap-sync 머리말 checkpoint — 0.3.19

원래 `roadmap-sync` 명세는
`docs/decisions/TOKEN_LAYER_AND_RIGHTS_SCALE_SCOPE_20260929.md`의 상태 머리말을
PR #79 병합 `5cf4168`의 기존 승인 기록에 맞추도록 명시한다. 설치된 0.3.18은
`docs/decisions/`의 모든 변경을 checkpoint 전에 거절해 이 산출물을 전달하지 못했다.

이 수리는 기존 결정의 상태 표시만 지원한다. 새 결정·권한·정책·게이트를 승인하지 않는다.
두 줄의 정합화는 설계·계획 문서화 승인만 표시하고 구현·배포·발행 미승인과
후속 조건부 범위를 유지한다. 결정 본문의 다른 바이트는 변경되지 않는다.

## 정확한 허용 범위

`gitops.ROADMAP_HEADER`는 다음 값을 모두 고정한다.

- 레포 `BeautifulMind-JT/kix-protocol`, Mac local 원래 노드 `roadmap-sync`.
- plan commit `5155ed307c71917ba3442fc5e1fc4cb950efefdc`.
- plan blob `ff0f39a8129ca8b8d30818cce35c3d4e588872fc`.
- 전체 원문 spec SHA256 `6a732abbfd4833c02d3e474088035417665ca27f7ae17aae9e8383537564fe41`.
- 원문 등급 A1, Astra gate NONE와 plan의 단일 원래 task.
- 위 결정 문서 한 파일의 변경 전 blob
  `ae63ff63d25c08e21bc61769d5a2c235b865afef`와 변경 후 blob
  `2ceb88df12d1344b7d8e8eeb7738526a3a150c6e`.

실제 pinned Git 원문과 저장된 native binding/plan/spec이 일치해야 한다.
일반 파일의 전체 변경 후 바이트도 일치해야 하므로 본문·다른 머리말·줄바꿈·추가 줄을
허용하지 않는다. symlink, hardlink, 실행 파일 모드도 거절한다.
다른 레포·plan·spec·노드·파일은 이 지원을 상속하지 않는다.
`AGENTS.md`, `.aiops/`, `RUNBOOKS/`와 나머지 결정 문서의 일반 보호는 유지한다.

## 정상 재개와 기존 산출물

native builder가 실제 complete/exit 0으로 종료했으나 checkpoint가
`AUTHORITY_EDIT_NEEDS_USER`로 멈춘 경우에만 정상 Owner resume이 checkpoint 재처리를
예약한다. 이 예약은 모델 실행 예약이 아니다.

앱은 기존 private request/receipt의 작업·attempt·원래 plan/task·builder role/profile,
checkout·host 경로·provider/harness/model·실제 session ID를 다시 바인딩 검증한다.
process group 종료 증거, 현재 HEAD, 기존 writer/terminal 기록도 일치해야 한다.
그 뒤 같은 host checkpoint를 거쳐 독립 검토로 전달한다. builder 호출 수나 writer
목록을 추가하지 않고 실패 영수증과 원래 terminal 기록을 보존한다.
HEAD·scope·profile·영수증이 달라지거나 종료가 미확인이면 재처리를 거절한다.
UNKNOWN은 정상 resume 대상이 아니며 삭제·강제 소유권 해제는 없다.

검토·Draft PR·동일 HEAD CI·최종 감리·사용자 검수·병합 후 ACCEPTED는 기존 경로와
각각의 검증을 유지한다. checkpoint 성공을 이 단계들의 성공으로 계산하지 않는다.
신뢰 등록, 격리, 인증 계정, 자동 시작 설정도 변경하지 않는다.

## 회귀 검증

`test_control_plane_mac_roadmap_checkpoint.py`는 고정 scope와 정확한 바이트만의 허용,
본문/머리말/줄바꿈/링크/모드 변조와 다른 scope 거절, 일반 authority 보호,
정상 resume에서 기존 영수증 재사용과 모델/writer 중복 방지,
HEAD/plan/profile/session/영수증 변조 및 UNKNOWN 거절을 검증한다.
fixture의 짧은 Python child는 fixture 종료 증거일 뿐 실제 제품 세션이나 제품 PASS가 아니다.
