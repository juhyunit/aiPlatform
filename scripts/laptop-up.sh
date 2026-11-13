#!/bin/sh
# GPU 없는 노트북에서 플랫폼 전체(모델 서버 포함)를 한 번에 띄운다. WSL2(Ubuntu) 또는 Linux에서 실행한다.
#   scripts/laptop-up.sh                      띄우기만
#   scripts/laptop-up.sh hong 'S3cret!'       띄운 뒤 계정 hong(internal 등급)을 만든다
# 처음 한 번은 인터넷이 필요하다: 파이썬 휠, Docker 이미지, 모델 GGUF(약 5.3GB)를 받는다.
set -e
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.cpu.yml"

docker info >/dev/null 2>&1 || { echo "Docker에 연결할 수 없습니다. Docker Desktop(WSL 통합 켜기) 또는 docker 서비스를 먼저 띄우세요."; exit 1; }

if [ ! -f .env ]; then
    rand() { python3 -c "import secrets; print(secrets.token_hex($1))"; }
    cat > .env <<EOF
DB_OWNER_PASSWORD=$(rand 16)
DB_APP_PASSWORD=$(rand 16)
SECRET_KEY=$(rand 32)
# llama.cpp 스레드 수. 속도가 기대보다 느리면 6·8·10 등으로 바꿔 scripts/smoke.py 시간을 비교한다
LLM_THREADS=8
LLM_THREADS_BATCH=12
EMBED_THREADS=4
EOF
    echo ".env를 만들었습니다(비밀번호·키는 무작위 값)."
fi

if [ -z "$(ls wheels/*.whl 2>/dev/null)" ]; then
    echo "파이썬 휠을 받습니다(scripts/bundle.sh)..."
    sh scripts/bundle.sh
fi

$COMPOSE up -d --build

echo "모델 서버 준비를 기다립니다. 처음에는 모델을 내려받느라 수 분~수십 분 걸립니다."
echo "  진행 상황: $COMPOSE logs -f llm"
for svc in embed llm; do
    until [ "$($COMPOSE ps --format '{{.Health}}' "$svc")" = healthy ]; do
        state=$($COMPOSE ps --format '{{.State}}' "$svc")
        [ "$state" = running ] || { echo "$svc 컨테이너가 멈췄습니다($state). 로그: $COMPOSE logs $svc"; exit 1; }
        sleep 5
    done
    echo "  $svc 준비됨"
done

if [ -n "$1" ] && [ -n "$2" ]; then
    $COMPOSE exec -T api python -m app.admin adduser "$1" "$2" internal
    echo "계정 $1 을(를) 만들었습니다."
fi

echo
echo "준비 완료. 브라우저에서 http://localhost:8000 을 여세요."
echo "점검: python3 scripts/smoke.py --user <아이디> --password <비밀번호>"
