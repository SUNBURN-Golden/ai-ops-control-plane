# 기존 AIOPS Linux 호스트를 Mac 소유 VM으로 실행하는 호환 도구

간단한 앱과 자율 개발 화면은 `../mac_app/README_KO.md`에 있다. 이 디렉터리는 기존 `/proc`·`setpriv` 기반 AIOPS 보호 엔진을 그대로 유지하려는 **별도 호환 경로**다. macOS 네이티브 실행·실제 설치 완료를 주장하지 않는다.

Apple Silicon에는 ARM64 Debian 13 VM, Intel에는 x86_64 Debian 13 VM을 사용한다. Apple Silicon에서 기존 x86_64 복구 manifest/바이너리를 그대로 재사용하지 않는다. 복구 페이지나 복구 패키지를 추가하지 않는다.

## 읽기 전용 확인과 VM 설정 생성

```bash
python3 engineering/mac_host/host.py doctor
python3 engineering/mac_host/host.py vm-config \
  --image /absolute/path/to/verified-debian-13.qcow2 \
  --image-sha256 VERIFIED_SHA256 --arch aarch64 > aiops-lima.json
# 준비한 Linux VM 내부에서
python3 engineering/mac_host/host.py guest-check
```

`vm-config`는 사용자가 준비한 **로컬 이미지 파일과 SHA-256**을 확인하고 Lima용 JSON/YAML 설정을 출력한다. 다운로드·설치·VM 시작·권한 변경·모델 호출은 하지 않는다. 호스트 디렉터리 공유, SSH agent forwarding, 자동 포트 전달을 설정하지 않는다. `plain`은 일반 Lima 편의 기능을 줄이는 설정이지 네트워크가 없다는 의미가 아니다. 기존 host/CLI/lane 설치와 실제 qualification은 별도다.

기존 호스트를 중단하고 active/UNKNOWN·보호 원장의 실행 소유권을 이관해야 한다. 새 VM의 빈 DB로 작업을 다시 시작해서는 안 된다. 기존 runtime 활성화 기록, 독립 감사 및 정확한 설치 hash 검증을 생략하지 않는다.

## 고정 명령 릴레이

```bash
python3 engineering/mac_host/relay.py prepare --request engineering/mac_host/examples/lanes.json
python3 engineering/mac_host/relay.py submit --request engineering/mac_host/examples/lanes.json
# 실제 설치·runner qualification·사용자 인증을 완료한 다음에만 전송
python3 engineering/mac_host/relay.py submit --request request.json --execute
```

전송에는 환경의 `GH_TOKEN`이 필요하다. 토큰을 명령문·JSON·저장소에 넣지 않는다. 실제 전송은 root workflow의 `execution_host=macbook`과 `expected_runner_name`을 지정한다. runner는 `self-hosted`, `astra-control-plane`, `aiops-macbook` 라벨과 정확한 runner 이름, Linux OS 검사를 모두 통과해야 한다.

`prepare`와 `submit` 기본 동작은 미리보기다. `--execute`만 한 번 전송한다. SQLite 전달 기록은 앱·레거시 엔진의 task admission 원장을 대신하지 않는다. 재전송에는 같은 request ID를 사용한다. 응답을 잃으면 UNKNOWN을 보관하고 자동으로 다시 전송하지 않는다. `status --refresh`는 확인된 workflow run 한 건을 한 번 조회하며 workflow 성공을 task DONE으로 해석하지 않는다.

`start.json`은 미완성 값이 있는 예제다. 실제 승인된 plan commit, node, canonical task issue를 넣기 전에는 거절한다. 릴레이는 임의 셸·모델 선택·merge·reset을 제공하지 않는다.

- [Lima plain 모드](https://lima-vm.io/docs/config/plain/)
- [Lima CPU architecture](https://lima-vm.io/docs/config/multi-arch/)
- [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows)
