# 감사 호스트 한 줄 복구 — HR-D3 구현 후보

상태: **소스 구현 / 미설치·실호스트 미검증 / NOT_READY**. 이번 PR은 설치,
모델 실행, 감사, 병합 또는 activation 승인이 아니다.

기준은 병합된 main `a964c0d72285a752cb567c39bfc4a7fb83ff3eae`다.
#49 기존 HEAD와 main을 두 부모 merge로 연결한 뒤 구현한다. rebase와 force-push는 사용하지 않는다.
대표 결정은 [HR-D3 원문](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/49#issuecomment-5933778620)이다.
HR-D1·HR-D2의 넓은 복구 설계보다 이번 범위에는 HR-D3가 우선한다.

## 범위와 사용자 동작

`aiops-recover`는 aiops-fable 감사 호스트의 고정 설치를 복원하고, 비공개
aiops-state의 지정 브랜치 최신 체크포인트를 암호로 복호화한다. 상태 암호를
한 번 입력하며, 정상 복구는 모델을 호출하지 않는다. Claude 토큰도 같은
암호화 체크포인트에서 복원한다. 암호는 저장하지 않고 파생 키만 root 전용
커널 keyring에 둔다. 새 부팅에서는 암호 입력이 다시 필요하다.

최초 등록은 기존 **비어 있지 않은 원장**과 보호 설정의 `enrollment_commit`을
검증한 `aiops-recover --enroll`로 한다. 신규 빈 원장을 만들지 않는다.
재발급은 `aiops-recover --reissue-token` 하나이며, 동기 foreground 흐름이다.
최초 발급과 만료 재발급에는 브라우저 승인이 필요하다. 최소 토큰 검증 호출과
`--preflight`에는 각각 y/N 확인이 필요하다. 기본 응답은 N이다.

레인·runner·dispatch의 QUALIFIED_RESUME, 호스트 epoch, 원격 빌더 fencing,
예약 해제, program reconcile 자동화는 **범위 밖**이다. 감사 전용 wrapper는
`audit`, `consult`, 확인된 `preflight`만 받는다. 기존 program bridge 설치나
PA-1 runner 경로를 이 감사 전용 호스트로 교체하지 않는다.

## 설치와 신뢰 경계

`recovery/manifest.json`은 main 원본 aiops-fable 파일 해시, 이번 복구 overlay
파일 해시, 고정 Claude CLI 2.1.286 linux-x64 바이너리 해시, 의존성 wheel 및
각 설치 파일 해시를 고정한다. 대상은 Python 3.12 / Linux x86_64 / glibc 2.34
이상이다. pyte 0.8.2, wcwidth 0.9.1, cryptography 46.0.0, cffi 2.0.0,
pycparser 3.0을 고정한다. wheel은 검증된 바이트에서 필요한 파일만 읽어
복사하며 setup.py나 pip를 root로 실행하지 않는다.

계정은 매니페스트에 고정한 aiops-auditor UID/GID 991, home
`/var/lib/aiops-auditor`, nologin이다. 기존 계정이나 파일이 다르면 drift로
중단한다. UID/GID 변경은 별도로 검토할 매니페스트 변경이며 자동 수정하지 않는다.
기존 파일과 신규 입력 전체를 검증한 뒤 열린 입력 바이트를 원자적으로 복사한다.
설치 완료 receipt가 없거나 다르면 감사 진입을 막는다. 중단 설치는 동일
매니페스트의 누락 파일만 복원한다. curl|sh, latest, 자동 업데이트,
워크스페이스 스크립트의 sudo 실행은 사용하지 않는다.

복구 명령 자체와 보호 설정·매니페스트의 신뢰는 VM 이미지/기존 운영자 설치
절차에서 공급해야 한다. **아무 root 도구도 없는 임의의 빈 VM을 이 PR이
권한 없이 부트스트랩하지 않는다.** 초기 이미지에는 두 root-protected
진입점, 복구 모듈, 설정, 승인된 매니페스트와 overlay cache가 필요하다.
CLI·wheel은 고정 HTTPS URL과 해시로 가져올 수 있다. Python, util-linux
script, stty, keyutils와 브라우저 연결도 이미지 전제다. 이후 복구 동작은
명령 한 줄과 상태 암호 입력 한 번이다.

## 상태와 중단

전체 `/var/lib/aiops-fable` 파일을 보존한다. 실행 기록, 소비한 claim,
quota, 결과 없는 run, 감사 시작·결과 journal을 포함한다. 기존 root 원장의
hardlink 파일도 각각 동일 바이트로 복원하므로 삭제·소비 상태를 잃지 않는다.
심볼릭 링크, 특수 파일, 쓰기 권한·소유자 drift는 거부한다.

AES-256-GCM과 scrypt(N=32768,r=8,p=1)를 사용한다. source/version/이전 커밋과
salt를 인증 데이터에 포함한다. 저장소에는 암호화 chunk와 해시·메타데이터만
올린다. 비밀값·nonce·원시 기록을 화면이나 오류 로그에 출력하지 않는다.
GitHub 저장소가 private가 아니면 중단한다. 갱신은 예상 이전 HEAD를 확인한
단일 부모 커밋 + non-force fast-forward ref 갱신으로 CAS한다.

로컬 binding보다 원격 최신이 앞서면 STATE_ROLLBACK_DETECTED다. 누락·구조 및
chunk 해시 손상은 STATE_CORRUPT다. AEAD 인증 실패는 암호 오류와 인증 데이터
손상을 구분할 수 없으므로 STATE_PASSWORD_OR_INTEGRITY_ERROR로 중단한다.
과거 백업으로 대신 복원하지 않는다. stage→원장 rename→토큰→binding 사이
중단은 보호 transaction receipt와 최신 상태의 정확한 일치 때만 이어 간다.
원격 CAS 성공 후 로컬 ACK를 잃으면 자동 승인하지 않고 대표 판단을 기다린다.

감사 STARTED를 모델 호출 **전** 원격 체크포인트로 확정한다. 감사 종료 뒤
RESULT 또는 UNKNOWN을 자동 체크포인트한다. 같은 PR·HEAD의 ASTRA_AUDIT_V1
댓글이 있으면 PASS/FAIL 결과를 보여 주고 호출하지 않는다. STARTED에 결과가
없으면 UNKNOWN_REPRESENTATIVE_DECISION_REQUIRED로 중단하며 자동 재실행하지 않는다.

## 진입점 선택과 과금

| 방법 | 장점 | 해시·권한 영향 | 선택 |
| --- | --- | --- | --- |
| 원본 aiops-fable 내부 수정 | 내부 호출까지 한 파일에서 점검 | a964의 감사 도구 해시가 바뀌고 재감사·배포 핀 갱신 필요 | 이번 구현에는 사용하지 않음 |
| 고정 root wrapper | 원본 바이트와 EFFORT=low를 보존하고 설치·상태·중복 점검을 선행 | 원본 SHA256은 유지; wrapper/복구 overlay/전체 설치 매니페스트 해시는 새 승인 대상 | 추천·구현 |

wrapper는 자신이 읽은 보호 매니페스트에 맞는 복구 모듈 바이트만 실행한다.
복구 모듈은 모든 설치 파일, 원격 최신 binding, 토큰, CLI 버전, 과금 신호를
검증한 뒤 원본 감사 모듈의 검증된 바이트를 실행한다. 원본 도구 파일은
`/opt/aiops/lib/fable/`에 일반 support 파일로 설치한다. 임의 직접 root
Python 호출은 기존 운영자 root 권한의 영역이며 새로운 runner 권한이 아니다.

PA-1의 `/opt/aiops/bin/aiops-fable program` 허용 규칙은 **기존 1개 그대로**다.
새 sudoers 파일·규칙을 만들지 않는다. 복구는 이미 승인된 운영자 root
실행 경로에서만 가능하다. 감사 전용 wrapper가 runner에게 복구 권한을 주지 않는다.

과금 점검은 현행 `overage_policy`를 그대로 호출한다. root 승인된 **비모델**
helper는 stdin으로만 토큰을 받아 다음 JSON을 반환해야 한다:
`token_sha256`, 최근 30초 이내 `observed_at`, 원래 형태의 `rate_limit_info`.
helper 경로는 `/opt/aiops/lib/` 아래, 파일 SHA256 고정, `non_model=true`여야 한다.
신호 없음/오래된 신호/overage 허용은 실행을 차단한다. 모델 출력에서 받은
과금 신호도 최소 검증 호출 결과에서 재확인한다.

**미해결 배포 전제:** Claude setup-token은 공식 문서상 모델 호출 용도로
제한된다. 현재 승인된 비모델 overage 조회 helper가 저장소에 없으므로,
예제 설정은 BILLING_PREFLIGHT_UNAVAILABLE로 닫혀 있다. 추정 API나 가짜
허용값을 넣지 않았다. 실제 subscription 계정에서 검증할 helper/인증
방식을 대표와 운영자가 확정해야 설치 qualification이 가능하다.

GitHub 토큰 보관은 기본 false다. opt-in이면 같은 체크포인트에 암호화하여
복원한다. 단, 체크포인트를 **처음 내려받을 GitHub 인증**은 VM identity 또는
현재 셸 GH_TOKEN/GITHUB_TOKEN으로 먼저 공급되어야 한다. 암호화 저장된
토큰만으로 자기 저장소를 인증하는 순환 의존은 해결됐다고 주장하지 않는다.

## 실행하지 않은 작업과 대표 확인

실호스트 설치·계정 변경·토큰 발급·모델 호출·Fable 감사·activation·병합은
하지 않았다. 배포 전에는 최종 HEAD 독립 A3, 초기 이미지의 신뢰 경로,
GitHub bootstrap 인증, 비모델 과금 신호, UID/GID 및 플랫폼, 최초 원장 등록을
확인해야 한다. 이번 시험은 모두 오프라인 가짜 환경이며 실호스트 검증을
대신하지 않는다. 시험 대응표는 [프로토콜](HOST_REPLACEMENT_RECOVERY_PROTOCOL.md)에 있다.
