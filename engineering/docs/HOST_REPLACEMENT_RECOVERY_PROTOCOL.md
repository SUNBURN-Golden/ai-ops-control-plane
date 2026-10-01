# HR-D3 감사 전용 복구 프로토콜과 시험

상태: 소스 구현 후보 / 미설치·실호스트 미검증. 규범은
[HR-D3](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/49#issuecomment-5933778620)이며,
QUALIFIED_RESUME는 이번 범위 밖이다.

## 명령과 순서

1. `aiops-recover`: 보호 설정·매니페스트 → 고정 파일·계정 → 원격 private/latest
   → 상태 암호 1회 → 복호화·전체 원장·토큰 복원 → root keyring. 모델 호출 없음.
2. `aiops-recover --reissue-token`: 설치 검증 → 현재 원장 → pyte 버전·추출기 자체
   시험 → script 확인 → PTY 내부 stty cols 500 확인 → CLI 버전 → foreground
   setup-token. 원시 화면을 복사하지 않고 script 기록을 pyte로 재생한다.
3. 기록은 tmpfs `/dev/shm/aiops-recover-token/typescript`, 부모 root 0700,
   파일 root 0600. 추출 실패·401·확인 취소 때 보존한다. 같은 명령을 다시
   실행하면 추출 실패 기록을 재추출하며 재승인하지 않는다. 401로 검증 실패한
   경우에는 다음 명시적 재발급 명령에서 기존 기록을 보존하고 새 발급을 한다.
   같은 실행 안에서 자동 재발급하지 않는다. 검증 및 체크포인트
   확정 뒤에만 삭제한다. 401은 길이·접두사 유효 여부와 재발급 명령만 출력한다.
4. 토큰 형식은 `^sk-ant-oat01-[A-Za-z0-9_-]{80,}$`, 최대 4096자.
   최소 `claude -p ok` 호출은 y/N 확인 뒤, aiops-auditor로 도구 없이 low로
   실행한다. 발급 기록의 시각으로 일 년 만료를 기록하고 30일 전부터 경고한다.
5. 감사 wrapper: 설치 receipt·전체 해시·최신 상태 → 같은 HEAD 기존 댓글/UNKNOWN
   점검 → CLI·토큰·과금 → STARTED checkpoint CAS → 원본 감사 → 결과 checkpoint CAS.
   같은 HEAD의 기존 결과는 FAIL이어도 재호출하지 않는다. --again은 허용하지 않는다.
6. `--preflight`와 직접 `aiops-fable preflight`: y/N 기본 N, 확인 없으면 실행하지 않는다.
   암호 오류, 손상, rollback, CAS 충돌 또는 UNKNOWN이 자동 retry 권한을 만들지 않는다.

모든 비밀 출력은 차단한다. 오류 메시지는 고정 reason 코드다. 비밀을 포함할 수
있는 provider stdout/stderr, auth URL, 원시 transcript는 오류에 붙이지 않는다.
선택 GH 토큰 저장은 기본 꺼짐이며 초기 원격 인증은 별도로 필요하다.

## T01~T30 범위 대응

실행 시험은 `scripts/test_control_plane_recover.py`의 오프라인 가짜 환경이다.
아래는 과거 T 시나리오를 HR-D3의 감사 전용 의미로 좁힌 대응이다. 실호스트
qualification이나 보류한 레인 재개까지 통과했다고 해석하지 않는다.

| ID | 이번 범위·시험 대응 |
| --- | --- |
| T01 | `test_install_and_idempotence`, `test_install_resume`, `test_restore_claim_and_token`; `test_existing_account_identity_drift`, `test_audit_does_not_create_missing_account`; 실제 계정 provisioning은 배포 qualification 미실행 |
| T02 | `test_restore_idempotent`, `test_dedupe_fail_no_model`, `test_result_checkpoint_after_call` |
| T03 | `test_barrier_contention`, `test_cas_conflict` |
| T04 | `test_install_receipt_incomplete`, `test_install_resume`, `test_crash_before_rename`, `test_resume_after_rename`, `test_resume_after_token` |
| T05 | `test_missing_checkpoint`, `test_local_corruption`, `test_empty_ledger_rejected`, `test_checkpoint_missing_chunk` |
| T06 | `test_rollback`, `test_checkpoint_ack_lost_holds` |
| T07 | `test_audit_cas_conflict_before_model`, `test_checkpoint_ack_lost_holds`; ACK 불명확 시 HOLD |
| T08 | 범위 밖: old/new runner sender epoch와 외부 빌더 fencing |
| T09 | `test_unknown_no_rerun`, `test_unknown_with_partial_mutation` — 감사 STARTED/UNKNOWN만 |
| T10 | `test_dedupe_fail_no_model`, `test_result_checkpoint_after_call`, `test_failed_call_is_unknown` |
| T11 | `test_install_hash_failure`, `test_manifest_bad_destination`, `test_symlink_installed`, `test_inventory_symlink`, `test_package_wheel_hash_failure` |
| T12 | `test_unbound_ledger_never_overwritten`, `test_state_root_writable`, `test_tampered_restore_receipt`; 범용 mount qualification은 범위 밖 |
| T13 | `test_payload_owner`, `test_payload_permissions`, `test_current_inventory_permission_drift`; 실제 UID·ACL qualification 미실행 |
| T14 | `test_expired_token`, `test_expiry_warning`, `test_prerequisites_version_before_login`, `test_billing_overage_block`, `test_validate_401_safe` |
| T15 | `test_manifest_matches_committed_sources`, `test_install_receipt_manifest_drift`, `test_scope_rejects_program_resume`; runner sender bypass는 범위 밖 |
| T16 | `test_manifest_wrong_source`, `test_manifest_matches_committed_sources`, `test_install_drift` |
| T17 | `test_preflight_default_no`, `test_confirmation_explicit`, `test_validate_no_confirmation_no_call` |
| T18 | 범위 밖인 실호스트 canary/플랫폼 replacement; 사용자 금지로 미실행 |
| T19 | 범위 밖: legacy lane reap 및 예약 유지 |
| T20 | 범위 밖: program operator reconcile/settlement |
| T21 | `test_no_root_no_bootstrap`: root 권한 없으면 PRIVILEGED_EXECUTOR_UNAVAILABLE; `test_scope_rejects_program_resume`로 runner 권한 확대 없음 확인; 실제 VM bootstrap 미실행 |
| T22 | `test_install_receipt_manifest_drift`, `test_rollback` — 감사 설치·상태 binding만; epoch는 범위 밖 |
| T23 | `test_barrier_contention`, `test_cas_conflict`; 범용 scheduler lock 순서는 범위 밖 |
| T24 | `test_manifest_wrong_source`, `test_manifest_matches_committed_sources`; 신뢰 root는 이미지 공급, 서명·revocation 서비스는 범위 밖 |
| T25 | `test_crash_before_rename`, `test_resume_after_rename`, `test_resume_after_token`, `test_checkpoint_ack_lost_holds`, `test_unknown_with_partial_mutation` |
| T26 | 범위 밖: partition된 old runner와 외부 task 소유권 |
| T27 | `test_missing_checkpoint`, `test_checkpoint_missing_chunk`, `test_checkpoint_chunk_hash_corruption`, `test_wrong_password`, `test_payload_owner` |
| T28 | `test_billing_missing_fail_closed`, `test_prerequisites_success_no_model`, `test_validate_no_confirmation_no_call` |
| T29 | `test_scope_rejects_program_resume`, `test_manifest_matches_committed_sources`, `test_preflight_default_no`; program 전체 내부 호출 inventory는 범위 밖 |
| T30 | `test_duplicate_json_rejected`, `test_redirect_never_forwards_credentials`, `test_validate_401_safe`; bounded HOLD, 비밀 출력 없음 |

추가 R2 시험: `test_token_replay_wrap/cursor/scroll`, `test_extractor_selftest`,
`test_failed_selftest_prevents_login`, `test_reissue_reuses_transcript_without_capture`,
`test_replay_does_not_append_instructions`, `test_cleanup_only_verified_root_record`.

## 남은 qualification

오프라인 시험은 provider의 실제 로그인 화면·버전 변화, 토큰 만료·401,
브라우저 연결, keyutils/kernel 지원, 계정 UID/GID, 비모델 과금 helper,
GitHub private 접근과 CAS 실제 권한을 대신 검증하지 않는다. 실제 호스트
설치·모델 실행·감사는 이 요청에서 금지되어 미실행이다. 자세한 전제는
[설계](HOST_REPLACEMENT_RECOVERY_DESIGN.md)의 설치·과금 절에 기록한다.

## 이번 구현 검증 결과

- 중앙 전체: **743건 PASS** (기존 645 + 신규 복구 98).
- 저장소 일반 시험: **47건 PASS**.
- 중앙 5개 target profile의 validate-repo: 모두 PASS.
- Python compile, Claude launcher shell syntax, whitespace, 매니페스트와
  커밋할 소스 바이트 일치: PASS. 원본 aiops-fable 변경 없음, EFFORT=low 유지.
- 시험 실행은 오프라인 가짜 상태 저장소·파일시스템·CLI 응답이다. 실제
  aiops-state 접근·토큰·브라우저 로그인·호스트 설치·모델 호출·감사·병합 없음.
