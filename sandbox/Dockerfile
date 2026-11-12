# 샌드박스 이미지. 모델이 쓴 코드와 파일 파서가 이 안에서만 돈다.
# 실행할 때 네트워크 없음, 읽기 전용 루트, 비루트 사용자, 자원 제한을 건다(app/sandbox.py).
# 패키지는 외부 색인 없이 wheels/에서만 설치한다(scripts/bundle.sh가 채운다).
FROM python:3.13-slim
RUN --mount=type=bind,source=wheels,target=/wheels --mount=type=bind,source=sandbox/requirements.txt,target=/r.txt \
    pip install --no-cache-dir --no-index --find-links /wheels -r /r.txt
COPY sandbox/parse.py /opt/parse.py
# 작업 폴더의 모듈은 어느 위치의 스크립트에서든 import된다
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp PYTHONPATH=/work
WORKDIR /work
