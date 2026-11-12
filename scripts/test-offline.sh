#!/bin/sh
# 네트워크 없는 테스트. 루프백만 있는 네트워크 네임스페이스에서 통합 테스트 전체를 돌린다.
# 첫 실행 때 무언가를 내려받으려는 라이브러리, 외부 주소를 부르는 코드가 여기서 걸린다. root 권한이 필요하다.
set -e
cd "$(dirname "$0")/.."
exec unshare -n sh -c 'ip link set lo up && exec python -m pytest tests -q "$@"' sh "$@"
