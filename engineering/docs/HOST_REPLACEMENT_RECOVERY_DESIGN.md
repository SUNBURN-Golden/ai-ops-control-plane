# 감사 호스트 한 줄 복구 — HR-D3 구현 후보

상태: **소스 구현·미설치 / NOT_READY: 실호스트 시운전 전**.
이 PR에서는 모델·감사·호스트 작업·설치·병합을 실행하지 않는다.
범위는 aiops-fable 감사 호스트다. 레인·runner 자동 재개 QUALIFIED_RESUME는 범위 밖이다.

기준 소스는 병합된 main `a964c0d72285a752cb567c39bfc4a7fb83ff3eae`다.
원본 aiops-fable 파일 바이트와 EFFORT=low는 보존한다. #49 기존 HEAD와 main의
두 부모 merge 뒤 일반 커밋으로 수정하며 rebase·force-push는 사용하지 않는다.
대표 결정은 [HR-D3 원문](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/49#issuecomment-5933778620)이다.

## 새 VM에서 시작

[BOOTSTRAP_KO.md](../recovery/BOOTSTRAP_KO.md)의 고정 블록을 root가 직접 붙여 넣는다.
신뢰 기준은 병합된 문서의 소스 커밋과 파일별 SHA256이다. 8개 파일만 고정
커밋의 GitHub API에서 받고, 해시와 기존 파일을 검증한 뒤 보호 위치에 복사한다.
mutable 워크스페이스 스크립트를 sudo로 실행하지 않는다. curl|sh, latest,
자동 업데이트, 새 sudo 규칙은 없다. 필요한 keyutils 등 OS 패키지는 apt로 설치한다.

GH_TOKEN은 read -rs로만 받는다. 토큰은 화면·인자·환경 덤프에 출력하지 않는다.
최초에는 README가 있는 비공개 aiops-state 저장소만 필요하다. 상태 브랜치가
없으면 마지막에 `aiops-recover --enroll`, 있으면 `aiops-recover`를 실행한다.
복원 실패를 최초 등록으로 바꾸지 않는다. 병합 뒤 승인된 소스 SHA를 블록의
한 자리에 넣고, 파일 바이트가 같으면 해시 표는 그대로 둔다.

## 고정 설치와 실제 VM 호환

실측 대상은 Debian 13 / x86_64 / Python 3.13.5 / glibc 2.41이다. 설치는
Python 3.13 및 해당 OS·아키텍처·glibc를 검증한다. CLI는 2.1.286 linux-x64와
파일 SHA256을 고정한다. pyte 0.8.2, wcwidth 0.9.1, cryptography 46.0.0,
cffi 2.0.0(cp313 wheel), pycparser 3.0도 wheel·설치 파일별로 고정한다.
wheel은 검증된 바이트에서 필요한 파일만 복사하며 setup.py를 실행하지 않는다.

기존 파일이 다르면 drift로 멈추고 덮어쓰지 않는다. 기존 파일과 신규 입력
전체를 검사한 뒤 같은 열린 입력 바이트로 원자적으로 설치한다. 완료 receipt가
없거나 다르면 감사 진입을 막는다. 중단 설치는 동일 매니페스트의 누락 파일만 채운다.

aiops-auditor의 UID/GID는 숫자로 고정하지 않는다. 없으면 useradd --system
--user-group으로 만들고, 0이 아닌 UID/GID·자기 그룹 하나·nologin·sudo 없음·보호된
home을 검사한다. 기존 uid/gid 996도 이 성질을 만족하면 받아들인다. 체크포인트는
root와 AUDITOR라는 소유 역할을 기록하고, 복원 때 현재 숫자 UID/GID로 바꾼다.

## 토큰 발급과 복원

정상 `aiops-recover`는 상태 암호를 한 번 입력하여 원장과 Claude 토큰을
함께 복원한다. 암호는 저장하지 않는다. 파생 키는 root 전용 커널 keyring에
보관하며, persistent keyring을 사용할 수 없으면 root 0700 tmpfs 디렉터리의
0600 파일에 둔다. 재부팅하면 tmpfs 키는 사라진다.

최초 발급 및 만료 재발급은 `aiops-recover --reissue-token`의 foreground 실행이다.
pyte 버전, 커서 이동·스크롤을 포함한 추출기 자체 시험, script, PTY 안의
stty cols 500, CLI 버전을 URL 표시 전에 검증한다. CLI 화면은 pyte로 재생하고
**로그인 URL만 한 줄로 출력**한다. 접힌 URL은 이어 붙인다. 브라우저 자동
열기를 하지 않는다. 대표는 브라우저에서 승인하고 받은 코드를 붙여 넣고 Enter한다.
승인 코드는 마스킹하여 입력하고 CLI의 다른 화면·토큰은 출력하지 않는다.

원시 기록은 root 0600 `/dev/shm/aiops-recover-token/typescript`에 둔다. 캡처가
완료되어 토큰을 추출한 뒤 최소 검증만 실패(취소·네트워크·과금 신호)하면 같은
기록을 재생하므로 재승인하지 않는다. 캡처 자체가 실패(코드 오입력·빈 입력·CLI
비정상 종료·로그인 화면 없음)하거나 추출이 실패하면 토큰이 없는 기록이므로
`failed-N.typescript`로 옮겨 보존하고, 다음 명시적 `--reissue-token`/`--enroll`에서
새 로그인 URL을 표시한다. 고정된 추출기는 결정적이어서 같은 바이트를 다시 재생해도
성공할 수 없기 때문이다. 401 실패 기록도 `rejected-N`으로 보존하고, 다음
명시적 재발급 명령에서 새 발급을 시작한다. 같은 실행에서 자동 재발급하지 않는다.
형식·길이 및 y/N 확인 뒤의 최소 `claude -p ok` 성공을 확인하고, 암호화
체크포인트 확정 뒤에만 기록을 삭제한다. 발급 시각과 일 년 만료를 기록하며
30일 전부터 경고한다.

401은 구조화된 오류 유형·HTTP 상태 또는 원본 도구의 구조화된 failure로만
판정한다. session UUID, 해시, 줄 번호, 일반 결과 텍스트의 401은 인증 오류가 아니다.
감사 출력은 그대로 표시하고, 인증 오류일 때 길이·접두사 진단과 재발급 명령만 추가한다.

## 과금과 진입점

비모델 helper와 billing_probe 설정은 제거했다. 과금 판단은 #47의 원본
aiops-fable 스트림 가드에 맡긴다. rate_limit_event가 실제 overage면
OVERAGE_NOT_BLOCKED, 판정할 수 없으면 OVERAGE_UNVERIFIED로 중단한다.
토큰 최소 검증도 같은 overage_policy로 스트림 신호를 검사한다.
preflight와 최소 검증은 y/N 기본 N이며, 일반 복구에서는 모델을 호출하지 않는다.

| 방법 | 해시 영향 | 선택 |
| --- | --- | --- |
| 원본 진입점 내부 수정 | 원본 감사 도구 해시와 배포 핀 변경 | 사용하지 않음 |
| 고정 root wrapper | 원본 바이트·low 유지, wrapper·전체 설치 매니페스트는 새 해시 | 추천·구현 |

wrapper가 설치 해시·receipt·원격 최신 상태·CLI·토큰을 점검한 뒤 검증된
원본 도구 바이트를 실행한다. PA-1 sudo 명령은 기존 1개 그대로이고 새 규칙은
없다. 이 감사 전용 호스트를 program bridge/runner 재개 경로로 사용하지 않는다.

## 원장, 최초 등록, UNKNOWN

전체 `/var/lib/aiops-fable`의 실행 기록, 소비 claim, quota 및 감사 journal을
AES-256-GCM/scrypt로 암호화하여 private aiops-state 한 브랜치에 저장한다.
원격 기대 이전 HEAD가 맞을 때만 non-force fast-forward CAS로 갱신한다.
로컬 binding보다 최신 원격이 앞서면 STATE_ROLLBACK_DETECTED, 누락·손상은
STATE_CORRUPT다. AEAD 실패는 암호 오류와 무결성 오류를 구분할 수 없으므로
STATE_PASSWORD_OR_INTEGRITY_ERROR로 멈춘다. 오래된 백업·빈 원장 fallback은 없다.

체크포인트는 감사마다 다시 암호화해 올리므로 감사 전용 범위에서 다시 읽지 않는
대용량 run 산출물은 넣지 않는다. 이 파일들은 program bridge의
`verify_failure_evidence()`만 다시 읽으며, 감사 전용 wrapper는 program 명령을 거부한다.

| 경로(`/var/lib/aiops-fable` 기준) | 체크포인트 | 이유 |
| --- | --- | --- |
| `runs/<id>/claude-output.jsonl` | 제외 | 모델 원출력(최대 256MiB). 판정은 PR 댓글·run.json에 있음 |
| `runs/<id>/claude-stderr.txt` | 제외 | 진단 출력. 감사 전용 경로에서 재사용 없음 |
| `runs/<id>/work/` 전체 | 제외 | 감사 입력 사본(diff.patch 등)·모델 작업 폴더. 매 실행 새로 만듦 |
| 그 밖의 run 증거(request-intent, model-attempt, failure-evidence, guard-stop, publish-intent/response, run.json, comment.md) | 포함 | 중복·재실행 판단과 감사 기록 |
| `recovery-audits.json`, claim, quota, 초기 등록 marker 등 나머지 | 포함 | 중복 감사·불명 실행 차단의 근거 |

복원 뒤 같은 HEAD 감사는 PR의 ASTRA_AUDIT_V1 댓글과 journal RESULT로 다시
호출하지 않는다. 제외 파일이 없어도 이 판단은 바뀌지 않는다. 합계가 한도를 넘으면
가장 큰 파일 경로·크기와 함께 STATE_TOO_LARGE로 멈춘다. 이 감사 호스트에서
program bridge를 다시 쓰게 되면 이 표를 그 범위에 맞게 다시 정해야 한다.

이 도구가 원장을 바꾸기 직전(journal 기록, 체크포인트 저장)에는 root 0600
`/etc/aiops/recovery-pending.json`에 현재 binding 커밋을 기록하고, 체크포인트를
binding에 묶은 뒤 지운다. 업로드가 중간에 실패해도 다음 명령에서 처리한다.
암호에서 파생한 키가 있는 `aiops-fable`과 `aiops-recover`가 모델 호출 없이 처리한다.

- 원격 HEAD가 binding과 같고 로컬 원장만 앞서 있으면, 그 원장을 다음 버전으로 다시 올린다.
- 원격 HEAD가 binding의 바로 다음 커밋이면 binding만 갱신한다. 이 커밋은 인증된
  previous_commit·salt·version이 맞고 내용이 로컬 원장과 같아야 한다.
- 최초 등록에서 브랜치를 만든 뒤 binding을 잃었으면 `aiops-recover`가 같은 원장을 확인하고 binding만 만든다.
- marker가 없는 로컬 변경, 남이 만든 커밋, 그 밖의 불일치는 지금처럼 멈춘다.
  각각 STATE_CORRUPT, STATE_ROLLBACK_DETECTED, UNKNOWN_REPRESENTATIVE_DECISION_REQUIRED다.

binding이 바뀐 뒤 남은 marker는 소진된 것으로 보고 지운다. 결과 저장이 실패해도
감사 출력은 화면에 그대로 보여 준다.

최초 --enroll은 브랜치가 없을 때만 encrypted commit과 ref를 생성한다.
동시에 다른 등록이 생성했거나 이미 브랜치가 있으면 생성 전용 CAS가 실패한다.
enrollment_commit 설정은 없다. 기존 원장이 없거나 비었으면 다음 확인을 받는다:
“기존 기록 없이 시작합니다. 중복 감사는 PR 댓글로만 막습니다. 계속할까요? [y/N]”.
y일 때만 초기 등록 marker와 runs 폴더를 만들어 암호화한다. 복원은 이 동작을 하지 않는다.
등록은 상태 암호를 두 번 받아 일치할 때만 진행한다. 체크포인트를 만든 직후 같은 키로
다시 내려받아 복호화하고 원장과 대조한 뒤에야 성공을 보고한다.

감사 전 STARTED를 원격에 확정한다. 종료 기록은 원본 결과에 따른다:

- 실제 PASS/PASS_WITH_NOTES/FAIL/DECISION_REQUIRED 판정, consult의 ANSWERED/USER_REQUIRED,
  완료된 preflight(status=PASS와 run): RESULT.
- BUSY, model_attempted=false, PRE_MODEL_FAILED: NOT_STARTED, 동일 명령 재실행 가능.
- 모델을 시도했으나 판정이 없는 그 밖의 실행: UNKNOWN, 자동 재실행 없음.

같은 HEAD의 기존 ASTRA_AUDIT_V1 댓글은 재호출 없이 결과를 보여 준다.
대표가 동일 `audit` 또는 `consult` 명령에 --again을 붙이면 이전 상태를 표시하고
“다시 실행할까요? [y/N]”을 묻는다. y일 때만 새 STARTED를 저장하고 원본
aiops-fable에 --again을 넘겨 한 번 실행한다. 이전 journal 상태는 history에 보존한다.
preflight는 매번 y/N 뒤 실행하며 같은 토큰으로 여러 번 실행할 수 있다.

## 남은 상태와 대표 작업

NOT_READY 사유는 **실호스트 시운전 전**이다. 소스의 일반 A3·병합 절차는 유지한다.
추가 대표 작업은 병합 뒤 소스 커밋 핀 갱신(이번 요청에서 병합 금지), 최초
private README 저장소 생성(저장소 생성 권한은 코드에 위임하지 않음)이다.
GitHub 토큰 저장은 기본 꺼짐이므로 새 셸의 GH_TOKEN 입력은 필요하다.
실호스트·토큰 발급·모델·감사·설치·병합은 이번 작업에서 실행하지 않았다.
