#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "이 설치 파일은 Mac에서 실행해 주세요." >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 이상을 설치한 뒤 다시 실행해 주세요." >&2
  exit 2
fi
python3 install.py
