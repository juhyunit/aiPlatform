# API 서버와 작업 워커가 쓰는 이미지. 패키지는 외부 색인 없이 wheels/에서만 설치한다(scripts/bundle.sh가 채운다).
FROM python:3.13-slim
# 샌드박스 컨테이너를 띄우는 데 쓰는 docker 명령
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
RUN --mount=type=bind,source=wheels,target=/wheels --mount=type=bind,source=requirements.txt,target=/r.txt \
    pip install --no-cache-dir --no-index --find-links /wheels -r /r.txt
COPY app app
COPY config config
COPY prompts prompts
COPY web web
