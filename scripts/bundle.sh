#!/bin/sh
# 인터넷이 되는 쪽에서 실행한다. 이미지 빌드에 필요한 파이썬 휠을 wheels/에 모은다.
# 이후 이미지 빌드와 설치는 외부 색인 없이 이 폴더만 쓴다. 휠은 이미지와 같은 파이썬에서 만든다.
set -e
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD":/src -w /src python:3.13-slim \
    pip wheel -w wheels -r requirements.txt -r sandbox/requirements.txt
