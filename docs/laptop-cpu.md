# GPU 없는 노트북에서 시험하기

대상: Windows 노트북, 외장 GPU 없음(예: Intel Core Ultra 5 125H, 14코어 18스레드). 모델 서버까지 CPU로 돌려
**플랫폼이 실제 LLM과 끝까지 동작하는지** 확인하는 용도입니다. 속도·품질 기준선은 목표 GPU에서 따로 잽니다.

구성은 README의 개발 PC 구성과 같고, 모델 서버 2개(llama.cpp CPU 빌드)를 같은 Docker Compose 안에 넣은 점만 다릅니다.

| 파일 | 역할 |
|---|---|
| `docker-compose.cpu.yml` | 덧붙임 파일. `llm`(Gemma 4 E4B Q4_0, 8K 컨텍스트, 텍스트만), `embed`(KURE-v1 Q8_0) 컨테이너 추가. API·워커가 `models.cpu.yaml`을 쓰게 함 |
| `config/models.cpu.yaml` | 모델 레지스트리. endpoint가 `http://llm:8080/v1`, `http://embed:8081/v1` |
| `scripts/laptop-up.sh` | `.env` 생성(무작위 비밀번호) → 휠 번들 → 빌드·기동 → 모델 서버 준비 대기 → 계정 생성 |
| `scripts/smoke.py` | Quick → Thinking → Agent를 한 번씩 돌려 시간·토큰·도구 호출·답을 출력. 표준 라이브러리만 사용 |

## 준비물

- **메모리 16GB 이상.** 이 구성 전체가 약 5GB를 씁니다(측정값: llm 3.8GB, embed 0.5GB, API·워커·DB 합계 0.7GB). 샌드박스 컨테이너가 작업마다 최대 1GB를 더 씁니다.
- **디스크 여유 15GB 이상.** 모델 4.9GB, 이미지 약 3GB, 휠, DB.
- **처음 한 번은 인터넷.** Docker 이미지, 파이썬 휠, 모델 GGUF를 받습니다. 그 뒤에는 인터넷 없이 뜹니다(모델은 `models` 볼륨에 남음).

## 1. WSL2와 Docker (Windows에서 한 번)

PowerShell을 **관리자 권한**으로 열고:

```powershell
wsl --install -d Ubuntu
```

재부팅 후 Ubuntu 창에서 사용자 이름·비밀번호를 정합니다.

Docker는 둘 중 하나를 씁니다.

- **Docker Desktop**(간단): 설치 후 Settings → Resources → WSL integration에서 Ubuntu를 켭니다.
- **WSL 안의 Docker Engine**(Docker Desktop 라이선스가 걸리는 회사라면): Ubuntu 창에서 `curl -fsSL https://get.docker.com | sudo sh` 후 `sudo usermod -aG docker $USER`, 창을 닫았다 엽니다.

WSL2가 쓸 메모리는 기본값이 PC 메모리의 절반입니다. 16GB 노트북이면 8GB라 빠듯하므로 `C:\Users\<사용자>\.wslconfig`에 아래를 넣고 PowerShell에서 `wsl --shutdown` 후 다시 엽니다(32GB면 그대로 둬도 됩니다).

```ini
[wsl2]
memory=10GB
```

## 2. 코드 받기

**WSL의 Ubuntu 창 안에서** 받습니다. `C:\` 아래(`/mnt/c/...`)에 두면 파일 접근이 느리고, Windows Git이 줄바꿈을 CRLF로 바꿔 셸 스크립트가 깨질 수 있습니다.

```bash
cd ~ && git clone https://github.com/juhyunit/aiPlatform.git && cd aiPlatform
```

저장소가 비공개라면 GitHub 로그인(개인 액세스 토큰 또는 `gh auth login`)이 필요합니다.

## 3. 띄우기

```bash
scripts/laptop-up.sh hong 'S3cret!'
```

처음에는 휠·이미지·모델을 받느라 회선에 따라 10~30분 걸립니다. 모델 다운로드 진행은 다른 창에서 봅니다.

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml logs -f llm
```

끝나면 브라우저(Windows 쪽)에서 `http://localhost:8000` 을 열고 `hong` / `S3cret!` 로 로그인합니다.

## 4. 점검

```bash
python3 scripts/smoke.py --user hong --password 'S3cret!'
```

모드별로 이렇게 나오면 연동은 된 것입니다: Quick은 답 한 줄, Thinking은 `run_python` 도구 호출 뒤 답, Agent는 계획 → 승인(스크립트가 자동 승인) → 단계 실행 → 작업 폴더에 `palindrome.py`.
화면에서는 모드를 직접 골라 같은 질문을 해 보면 됩니다.

평가셋 전체(56문항)도 돌릴 수 있지만 CPU에서는 몇 시간 걸립니다. 유형 하나만 먼저 돌려 보세요.

```bash
sudo apt install -y python3-httpx && python3 -m evals.run --url http://localhost:8000 --user hong --password 'S3cret!' --model gemma-4-e4b --only quick
```

## 속도 기대치

클라우드 개발 환경(가상 CPU 4개, 15GB)에서 같은 구성으로 잰 값입니다. 노트북은 코어가 더 많아 이보다 빠를 것으로 보지만 재 보지는 못했습니다.

| 항목 | 측정값 |
|---|---|
| 생성 속도 | 초당 약 12토큰 |
| 프롬프트 처리 | 초당 약 90토큰(4,400토큰 입력에 49초) |
| Quick 1회 | 3초 |
| Thinking 1회(도구 호출 2번, 수정 1번 포함) | 213초 |
| Agent 1건(회문 함수) | 152~989초. 단계 수와 수정 횟수에 따라 크게 다름. 989초 걸린 건은 LLM 호출 37회, 입력 54K 토큰 |

느리면 `.env`의 `LLM_THREADS`(생성)·`LLM_THREADS_BATCH`(프롬프트 처리)를 바꿔 봅니다. 125H는 성능 코어 4개(8스레드)와 효율 코어 10개가 섞여 있어 스레드를 늘린다고 꼭 빨라지지 않습니다. 6·8·10 정도를 바꿔 가며 `smoke.py --only quick,thinking` 시간을 비교하고, 바꾼 뒤에는 다시 띄웁니다.

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d llm
```

**내장 GPU(선택).** 125H에는 Intel Arc 계열 내장 GPU가 있습니다. "외장 GPU가 없다"는 맞지만 llama.cpp의 Vulkan 빌드로 이 내장 GPU를 쓰면 프롬프트 처리가 빨라질 수 있습니다. 다만 WSL2·Docker 안에서는 쓰기 어렵고, Windows용 llama.cpp(Vulkan) 릴리스를 Windows에서 직접 띄워야 합니다. 그때는 이 덧붙임 파일 없이 README의 원래 구성(`config/models.yaml`, endpoint `host.docker.internal`)을 쓰면 됩니다. 시험하지 않은 경로라 CPU 구성으로 먼저 확인한 뒤에 시도하세요.

## 실제 모델로 확인한 것 (2026-11-13, 이 구성 그대로)

README가 "검증 못 함"으로 남겨 둔 실제 LLM 연동을 이 구성으로 처음 확인했습니다. 시험한 것은 `scripts/smoke.py`의 세 질문뿐이고, 평가셋은 돌리지 않았습니다.

| 항목 | 결과 |
|---|---|
| 스트리밍, 토큰 사용량, `finish_reason` | 게이트웨이 가정대로 옴 |
| 도구 호출(`tools: native`) | Gemma 4 E4B가 OpenAI 형식 `tool_calls`로 냄. `run_python`, `write_file`, `read_file`, `list_files` 정상 |
| 추론 켜고 끄기(`chat_template_kwargs.enable_thinking`) | 동작함. 켜면 `reasoning_content`로 따로 옴. 쉬운 질문에는 켜도 추론을 거의 안 함 |
| JSON 스키마 출력(`response_format`) | Agent의 명세·계획·Reviewer 단계에서 파싱 실패 없음 |
| 임베딩 | 1024차원, `/v1/embeddings` 정상 |
| 모델 파일 | ggml-org 저장소에는 Q4_K_M이 없어 Q4_0(4.6GB)을 씀. README의 Q4_K_M 기준과 다름 |
| 오프라인 기동 | 모델을 받은 뒤에는 인터넷 없이 캐시로 뜸 |
| Quick | 통과 |
| Thinking | 답은 맞았으나 **수치 검사 오탐**으로 `ok=false`. 질문의 "1,000만 원", "3.5%"를 모델이 코드에 10000000, 0.035로 쓴 값과, 실행 결과 12722792.6을 반올림한 12722793을 근거 없는 수치로 판정함. 고쳐 쓴 답이 사용자 대신 검증기에게 말하는 문장으로 시작함. 미수정 |
| Agent 1차 | 단계 3개와 린트 수정까지 통과한 뒤 Reviewer 단계에서 실패. Reviewer가 재현 코드로 파이썬 대신 셸 명령(`python -c "..."`)을 써서 문법 오류로 끝났는데, 이를 "문제 재현"으로 판정함. **고침**: 파이썬으로 컴파일되지 않는 재현 코드는 실행하지 않고 참고 의견으로 돌림(`app/workflow.py`, 테스트 추가) |
| Agent 2차 | Planner가 단계 검사 명령의 따옴표를 겹쳐 써서(`python -c "...is_palindrome("racecar")"`) 셸에서 깨짐. Worker는 계획의 검사 명령을 못 고치므로 수정 2회 뒤 미완료. 소형 모델의 품질 문제로 보고 미수정. 모델별 프롬프트 패치(`prompts/patches/gemma-4-e4b/planner_plan.md`) 후보 |

샘플링 값은 llama-server 기본값(Gemma 권장값: temperature 1.0, top_k 64, top_p 0.95)이라 같은 질문도 실행마다 결과가 달라집니다. 점수를 비교하려면 서버 쪽에서 값을 고정하세요(README "모델 서버" 참고).

## 끄기, 지우기

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml down
```

DB와 모델까지 지우려면 `down -v`. 모델을 다시 받아야 하니 주의합니다.

## 문제가 생기면

| 증상 | 확인 |
|---|---|
| `llm` 컨테이너가 계속 다시 시작 | `logs llm`. 메모리 부족이면 `.wslconfig` 메모리를 늘리거나 `.env`에 `LLM_CTX=4096` |
| 첫 실행에서 모델 다운로드 실패 | 회사 프록시·방화벽이 `huggingface.co`와 `*.hf.co`를 막는지. 인터넷이 되는 곳에서 한 번 받으면 볼륨에 남음 |
| 휠 다운로드 실패(`bundle.sh`) | `pypi.org`, `files.pythonhosted.org` 접근. 회사 프록시면 Docker에 프록시 설정 필요 |
| 화면에서 "응답할 수 있는 모델 서버가 없습니다" | 모델 서버가 아직 준비 중. `ps`에서 llm이 `healthy`인지 확인. 장애로 판정된 모델은 30초 동안 건너뜀 |
| 응답이 600초 넘게 걸려 끊김 | 게이트웨이의 호출당 제한(`app/gateway.py`의 600초). 긴 첨부 + Thinking에서 생길 수 있음. 스레드 조정 또는 짧은 질문으로 확인 |
