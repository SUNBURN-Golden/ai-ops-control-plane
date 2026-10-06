# Mac 0.3.20 선행 작업 완료 증거

원래 계획의 `depends_on`이 있는 A1/A2 노드도 정상 Mac 완료 기록으로
선행 조건을 확인한 뒤 같은 지원 generation/start 경로로 진행할 수 있다.
원래 노드, 계획 blob, 작업 revision, 소유권과 의존 목록은 그대로 고정한다.

선행 기록은 동일 계획 revision의 유일한 Mac generation이어야 하며,
canonical task와 delivery가 정상 `ACCEPTED`, 앱 job이 `accepted`여야 한다.
기존 inspection validator로 실제 원래 native receipt, 독립 검토와 감독,
동일 HEAD CI, 사용자 검수 및 lineage를 검증한다. 기존 merge verifier와
인증된 GitHub Actions 읽기로 실제 merge 부모·tree·계획 ancestry·완료 CI를
재확인한다. closed/merged 표시, 모델 PASS, 과거 legacy DONE은 증거가 아니다.

ACCEPTED는 실제 두 부모를 가진 merge commit만 지원한다. 첫 부모는 검증된
base SHA, 두 번째 부모는 검수한 정확한 PR HEAD여야 하며 tree도 일치해야 한다.
Squash 또는 rebase 병합은 ACCEPTED가 아니므로 후속 의존 작업을 열지 않는다.

검증 결과와 로컬 기록 fingerprint는 새 작업의 work에 고정한다. admission
직전과 start 시 다시 대조하며, 누락·변조·다른 revision/HEAD·부분 실패는
소유권 예약 전에 거부한다. 기존 완료 작업, 기록 및 UNKNOWN은 수정하지 않는다.
같은 generation 재전송은 읽기 전용이며 새 증거로 다시 고정하지 않는다.

지원 업데이트 후 원래 계획 순서의 후속 노드를 선택한다. 새 checkout이
실제 zero-model trust preflight에서 막히면 승인된 상시 등록 helper로 확정
job의 정확한 경로 한 곳만 등록하고 같은 Owner resume을 사용한다. A3,
legacy owner, UNKNOWN, 단일 writer, 검토·CI·감리 gate는 계속 적용된다.
