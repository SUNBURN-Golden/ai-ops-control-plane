# 승인된 빈 attempt의 가역 복구

사용자 결정6032340624는 원래 KIX job20531604498943e1 아래 빈 디렉터리
`d8d194e55af64b0b8a44a487bd41fd94` 하나만 보존 격리하도록 허용한다.
원문과 relay 출처는 MAC_EMPTY_ATTEMPT_DECISION_20261007.md 및 RECORD.json에 있다.
GitHub 사본은 실제 API의 actor·시각·본문 hash 확인을 대체하지 않는다.

`mac_empty_attempt_recovery.py`는 설치 앱을 실행하지 않는 지원 복구 명령이다.
Mac의 기존 기본 app/data/installer 경로만 사용하며 job·attempt·경로 인자를 받지 않는다.
기존 per-user installer.lock과 service.lock을 순서대로 잡고 서비스 정지를 재확인한다.
설치 binding 검증이 실패하거나 다른 installer/service가 실행 중이면 적용하지 않는다.

검증 범위:

- live 사용자 결정 actor263336091/BeautifulMind-JT/User 및 변경 없는 원문 hash/시각
- 정확한 원래 job/HEAD/verifying 상태, calls=3, 전체 job active attempt 없음
- native_local/mac_host_attempts의 모든 실행 TERMINAL; 기존 완료 영수증과 process group 종료
- 두 SQLite 원장의 전체 논리 내용에서 UUID 참조 없음, 원장/영수증 SHA 보존
- 빈 0700 비-symlink 디렉터리, 소유자·birth/mtime·device/inode와 원경로
- 같은 시각의 setup_required 이벤트. 이 이벤트에는 UUID가 없으므로
  MISSING_PROVIDER와의 인과관계는 증거를 종합한 판단이며 직접 기록된 연결이 아니다.

독립 감사와 정확 HEAD CI를 통과한 checkout에서 기존 Python으로 실행한다:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 engineering/mac_app/mac_empty_attempt_recovery.py inspect
PYTHONDONTWRITEBYTECODE=1 python3 engineering/mac_app/mac_empty_attempt_recovery.py quarantine
```

inspect는 기존 잠금 파일을 이용하며 원장·제품 상태·격리 폴더를 변경하지 않는다.
quarantine은 기존 data 아래 recovery/empty-attempt-<고정 UUID>/evidence.json에
원경로·메타데이터·검증 근거·해시를 private하게 저장하고 같은 filesystem에서 원래
디렉터리를 attempt로 원자 이동한다. Mac renamex_np(RENAME_EXCL)로 기존 목적지를
덮어쓰지 않는다. 이동 전후 context가 같고 기존 install.idle_database가 그대로 통과해야
QUARANTINED가 된다. 검사 실패 시 원위치 복원을 시도하며 오류를 숨기지 않는다.
복원 목적지가 바뀌었으면 덮어쓰지 않고 양쪽 증거를 보존한 채 오류를 반환한다.

PREPARED 후 이동 여부가 불명확하면 같은 명령이 정확한 한 경로와 동일 inode/증거를
읽어 확인한다. unknown archive, 양쪽 경로 존재, 내용·원장·영수증·메타데이터 변화는
자동 정리하지 않는다. 기록 전 crash 등 증명이 부족한 상태는 차단 보고한다.
성공한 격리를 되돌릴 때만 명시적으로 restore를 사용한다. 원래 경로가 비어 있고
원장·영수증·대상 inode가 보존돼야 하며, 복원하면 원래 installer UPDATE_BUSY도 돌아온다.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 engineering/mac_app/mac_empty_attempt_recovery.py restore
```

원장/calls/installerguard 변경, 삭제, 다른 폴더 정리, 새 제품 builder는 없다.
기존 Mac 계정과 private 파일 신뢰 경계를 유지하며 새 서명/권한을 만들지 않는다.
복구 명령은 앱을 설치·시작하지 않는다. 전체 설치 readiness는 별도로 기존 installer가
판정한다. 기존 담당자에게 검증된 설치 commit을 전달하고 동시설치를 피한다.
