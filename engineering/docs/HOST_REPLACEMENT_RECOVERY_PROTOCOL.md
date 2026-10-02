# HR-D3 감사 전용 복구 프로토콜과 시험

상태: 소스 구현 후보 / 미설치·실호스트 미검증. 규범은
[HR-D3](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/49#issuecomment-5933778620)이며,
QUALIFIED_RESUME는 이번 범위 밖이다.

## 명령과 순서

1. 새 VM은 [고정 root bootstrap 블록](../recovery/BOOTSTRAP_KO.md)으로 시작한다.
   병합된 커밋·파일 해시를 고정하고 GH_TOKEN을 read -rs로 받는다. apt로
   keyutils 등을 설치한다. 새 sudo 규칙이나 workspace sudo 실행은 없다.
2. 상태 브랜치가 있으면 recover, 없으면 --enroll이다. --enroll은 encrypted
   branch를 생성 전용 CAS로 만든다. enrollment_commit 설정은 없다. 기록 없는
   등록은 별도 y/N 확인이며, restore가 실패했다고 빈 원장으로 바꾸지 않는다.
3. 복구는 상태 암호 1회로 전체 원장과 토큰을 복원한다. 감사 소유 역할은 현재
   UID/GID로 매핑한다. 파생 키는 root keyring 또는 root0600 tmpfs 파일에 둔다.
4. --reissue-token은 pyte·자체 시험·script·PTY cols500·CLI 버전을 먼저 확인한다.
   pyte로 복원한 로그인 URL만 한 줄로 표시하고 브라우저 승인 코드를 마스킹
   입력하여 foreground script에 전달한다. 자동 브라우저 열기·토큰 화면 복사는 없다.
5. raw transcript는 root0600 tmpfs에 보존한다. 추출 실패는 동일 기록 재생,
   401은 구조화된 유형·상태로만 판단한다. 같은 실행에서 자동 재발급하지 않는다.
   형식·길이와 y/N 뒤 최소 claude -p ok 및 원본 overage_policy로 검증한다.
   암호화 checkpoint 확정 뒤 기록을 삭제한다. 발급 시각·30일 전 경고를 유지한다.
6. 감사 wrapper는 설치·최신 상태·CLI·토큰을 검사하고 STARTED를 checkpoint한다.
   과금은 원본 Fable의 rate_limit_event guard에 맡긴다. 비모델 helper는 없다.
   실제 판정은 RESULT, BUSY/모델 미시도/PRE_MODEL_FAILED는 NOT_STARTED,
   나머지 불명 실행은 UNKNOWN이다. 정상 결과의 UUID·해시·줄 번호 401은 그대로 표시한다.
7. audit --again은 이전 상태를 표시하고 y/N을 받는다. y일 때만 1회 실행하고
   원본 도구에 --again을 전달한다. preflight는 매번 y/N 뒤 실행하며 1회 제한이 없다.

NOT_READY 사유는 실호스트 시운전 전이다. 병합 뒤 bootstrap 소스 핀 갱신과
최초 private README 저장소 생성은 대표 작업이다. 실호스트·모델·감사·설치는 미실행이다.

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
| T14 | `test_expired_token`, `test_expiry_warning`, `test_prerequisites_version_before_login`, `test_f1_token_validate_actual_overage`, `test_validate_401_safe` |
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
| T28 | `test_f1_token_validate_unknown_overage`, `test_prerequisites_success_no_model`, `test_validate_no_confirmation_no_call` |
| T29 | `test_scope_rejects_program_resume`, `test_manifest_matches_committed_sources`, `test_preflight_default_no`; program 전체 내부 호출 inventory는 범위 밖 |
| T30 | `test_duplicate_json_rejected`, `test_redirect_never_forwards_credentials`, `test_validate_401_safe`; bounded HOLD, 비밀 출력 없음 |

추가 R2 시험: `test_token_replay_wrap/cursor/scroll`, `test_extractor_selftest`,
`test_failed_selftest_prevents_login`, `test_reissue_reuses_transcript_without_capture`,
`test_replay_does_not_append_instructions`, `test_cleanup_only_verified_root_record`.

## 남은 qualification

오프라인 시험은 provider의 실제 로그인 화면·버전 변화, 토큰 만료·401,
브라우저 연결, keyutils/kernel 지원, 계정 UID/GID,
GitHub private 접근과 CAS 실제 권한을 대신 검증하지 않는다. 실제 호스트
설치·모델 실행·감사는 이 요청에서 금지되어 미실행이다. 자세한 전제는
[설계](HOST_REPLACEMENT_RECOVERY_DESIGN.md)의 설치·과금 절에 기록한다.

## 실호스트 시운전 1차(2026-10-02, Grok VM) 대응

| 현상 | 원인 | 수정 | 확인 |
| --- | --- | --- | --- |
| 등록 첫 단계 `AUDITOR_SUDO_FORBIDDEN` | sudo 1.9.15/1.9.16은 규칙이 없는 다른 사용자 조회에 종료 코드 0과 `User <name> is not allowed to run sudo`를 낸다. 코드는 종료 코드 1만 허용 | 종료 코드 0/1 + 해당 사용자 이름의 그 문장 + 규칙 목록 없음일 때만 통과 | 실제 sudo 1.9.15 출력 재현, 규칙 있음·다른 사용자·다른 종료 코드 거부 시험 |
| (같은 시운전 전 점검) 로그인 URL 미표시로 대기 | 고정 CLI 2.1.286 setup-token은 `https://claude.com/cai/oauth/authorize?...`를 표시. 허용 호스트에 claude.com이 없음 | 허용 호스트에 claude.com 추가(정확 일치) | 고정 CLI 실물로 승인 없이 URL·코드 입력 단계까지 재현, 유사 호스트 거부 시험 |

고정 CLI 바이너리와 wheel 6개의 SHA256, `claude --version` 문자열, PTY 폭 검사도 실물로 확인했다.

## 수동 Fable A3 재감사(43ac23f, FAIL) 대응

근거: [#49 감사 기록](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/49#issuecomment-5943407971).

| 지적 | 수정 | 회귀 시험 |
| --- | --- | --- |
| F1 `/usr/local` 그룹 쓰기 | 부트스트랩 apt 뒤 `/usr/local`, `/usr/local/bin`을 root:root 0755로 맞춤 | 블록 순서 확인 |
| F2 로그인 실패 뒤 영구 재생 | 캡처·추출 실패 기록은 `failed-N`으로 옮기고 다음 명시적 명령에서 새 URL | 오입력·비정상 종료·화면 없음·추출 실패·정리 |
| F3 체크포인트 무한 증가 | 대용량 run 산출물 제외, 초과 시 가장 큰 파일 표시 | 제외·복원 일치·제외 경로 거부·한도 메시지 |
| F4 업로드 중간 실패 | pending marker로 자기 원장 재발행·자기 다음 커밋 확인, 등록 중단 재개 | 결과 저장 실패·wrapper 출력 보존·ACK 유실·남의 커밋·marker 없는 변경·소진 marker·등록 재개 |
| F5 등록 암호 오타 | 두 번 입력 일치, 등록 직후 같은 키로 재복호화 확인 | 불일치 거부·오타 키 거부 |
| N2 consult·preflight 분류 | ANSWERED/USER_REQUIRED·완료 preflight를 RESULT로, consult `--again` y/N | 분류·consult 재상담 |

## F1~F7 검증 대응과 결과

| 수정 | 회귀 시험 |
| --- | --- |
| F1 | billing mock 없는 audit/consult/preflight/reissue 경로, 원본 stream overage 가드, 실제 overage·미확인 신호 차단 |
| F2 | headless URL 표시·코드 입력, 여러 줄 URL의 pyte 복원, 토큰 화면 미출력 |
| F3 | 정상 UUID·해시·줄 번호 401 통과, 구조화된 인증 오류만 진단 |
| F4 | BUSY/HEAD_MOVED/PRE_MODEL_FAILED→NOT_STARTED, 실제 판정→RESULT, UNKNOWN→--again y/N, preflight 반복 |
| F5 | 고정 8파일 해시, 해시 오류 시 설치 없음, restore/enroll 선택, heredoc 이후 입력 복원, umask077에서도 감사 계정 경로 접근 |
| F6 | 브랜치 없음/이미 있음 생성 CAS, 원장 없음 y/N, 기존 원장 보존, restore의 빈 원장 fallback 거부 |
| F7 | 실제 Python3.13.5와 고정 wheel로 시험, Debian13/glibc2.41 검사, uid996 수용·소유 역할 매핑, keyctl 부재 tmpfs 대체 |

- 중앙 전체 **778건 PASS** = 기존 중앙 645 + 복구 133.
- 이번 수정 회귀 40건 추가, 삭제한 비모델 helper 가정 시험 5건을 새 가드 시험으로 대체.
- 저장소 일반 시험 **47건 PASS**; 중앙 5개 target validate-repo 모두 PASS.
- 시험 환경은 scratch의 Python **3.13.5**, 고정 pyte0.8.2/wcwidth0.9.1/
  cryptography46.0.0/cffi2.0.0/pycparser3.0이다. root/account/CLI/GitHub는 가짜다.
- 원본 aiops-fable 바이트·low 유지. bootstrap shell syntax, Python compile,
  source/manifest/bootstrap SHA256 대응을 검증했다.
- 실제 Grok VM·설치·토큰 발급·모델·감사·병합은 실행하지 않았다.
