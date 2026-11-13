# 인수인계

이 프로젝트를 처음 맡는 사람이 코드를 읽기 전에 알아야 할 것을 모았다.
실행 방법, 구조 목록, 아직 없는 기능은 [README.md](README.md)에 있고 여기서 되풀이하지 않는다. 이 문서는 **왜 이렇게 만들었는지, 어디를 고치면 되는지, 무엇이 아직 불확실한지**를 다룬다.

## 1. 한 문단 요약

은행 폐쇄망에서 오픈 가중치 LLM으로 문서 분석·데이터 분석·프로그램 개발을 시키는 사내 에이전트 플랫폼이다. 2026-11-10 설계서의 0~1단계(기능 구현)까지 만들었다.
핵심 원칙은 세 가지다. **모델로 가는 길은 게이트웨이 하나**이고, **데이터 등급이 쓸 수 있는 모델을 먼저 거른다.** 작업의 **완료는 모델이 아니라 테스트·규칙 검사가 판정한다.**
코드는 약 1,500줄(app/)이고, 상시 프로세스는 API 서버·작업 워커·PostgreSQL·모델 서버 4개다.

## 2. 현재 상태 (2026-11-12)

| 항목 | 상태 |
|---|---|
| 기능 | 설계서 1단계 범위 구현 완료. 빠진 것은 README "아직 없는 것" |
| 테스트 | 통합 테스트 134개 통과. 네트워크를 끈 상태에서도 통과 |
| **실제 LLM 연동** | 2026-11-13 CPU 구성(llama.cpp + Gemma 4 E4B Q4_0)으로 Quick·Thinking·Agent 각 1~2회 확인. 게이트웨이 형식(스트리밍·도구 호출·JSON 스키마·추론 전환)은 맞았고, 검증 쪽 문제 2건과 소형 모델 품질 문제 1건이 나왔다. 결과는 [docs/laptop-cpu.md](docs/laptop-cpu.md). GPU 구성과 평가셋 기준선은 아직 |
| 답변 품질 | 모름. 평가셋 점수는 실제 모델을 붙여야 나온다 |
| 부하 | 500명 시나리오에서 API·DB 계층 오류 0건(가짜 모델 기준). GPU 처리량은 미측정 |
| 저장소 | `main` 브랜치. 이전에는 InfraPilot 저장소의 `agent-platform/`에서 개발했고 그 이력은 옮기지 않았다 |

## 3. 정해진 것과 안 정해진 것

| 항목 | 값 | 출처 |
|---|---|---|
| 목표 동시 사용자 | 500명 | 요청자 |
| 개발 PC GPU | RTX 3060 Ti 8GB → Gemma 4 E4B 4비트 GGUF, 컨텍스트 8K | 요청자. 실제로 올려 보지 못함 |
| 사내 GPU | **미정**. 2단계에서 24~48GB 1장, 80GB 1장 두 급으로 측정 예정 | 설계서 15장 |
| 데이터 등급 | 5단계 **초안**(docs/data-grades.md). 정보보호 부서 확정 필요 | 요청자가 "금융사의 현실적인 체계로" 위임 |
| 모델 출처 허용 기준 | **미정**. 중국 출처(Qwen, BGE 등)는 심사 전까지 후보에서 뺌 | 설계서 6·9장 |
| 평가 문항을 만들 현업 | **미정**. 지금 평가셋 56문항은 씨앗 | — |
| 합격선(`pass_line`), 한도·예산 값 | **출발값**. 2단계 실측 뒤 교체 | config/policy.yaml |

500명에 대한 주의: 응답 10초·생각 시간 30초면 동시 추론 약 125건이다. 설계서 전제(GPU 1~2장)와 차이가 커서, 2단계 결과로 도입 규모나 목표 인원 중 하나를 조정해야 할 가능성이 높다.

## 4. 요청 하나가 흐르는 길

이 흐름을 알면 코드의 80%가 읽힌다.

```
화면(web/app.js)
  │ POST /api/chat {session_id, message, mode, model, search, images}
  ▼
api.py chat()            사용량 한도 확인 → runs 행 생성 → Run 객체(추적 문맥)
  ▼
modes.route()            ① candidates(): 세션 등급과 첨부 등급 중 최고 → gateway.select()로 허용 모델 목록
                         ② Auto면 정규식 규칙으로 모드 판정, 못 가르면 모델에게 JSON으로 분류
  │
  ├─ quick/thinking → API 서버 안에서 바로 실행 (modes.chat)
  │     _messages(): 시스템 프롬프트 + 기억 + 이력(요약) + 첨부 전문 또는 검색 결과 + 질문을 예산 안에서 조립(context.assemble)
  │     quick: gateway.chat 1회 → 형식 검사 → 실패 시 thinking으로 승격
  │     thinking: runtime.run_step(도구 루프) → validator(인용·수치) → 실패 시 1회 수정
  │
  └─ agent → runs.status='queued', state=new_state() 저장 후 응답은 events 표를 따라가는 스트림(follow)
        worker.py가 claim_run()으로 집어 workflow.advance()를 한 칸씩 반복, 매번 state 저장
        spec → plan → (승인 대기 'waiting') → execute(단계마다) → verify → reconcile → finish
```

모든 단계는 `Run.emit()`으로 `events` 표에 남고, 같은 내용이 화면에 SSE로 간다. 화면의 진행 표시와 추적 기록과 평가 러너가 **같은 이벤트**를 본다.

이벤트 종류: `run`, `mode`, `token`(저장 안 함), `llm_call`, `tool_call`, `tool_result`, `check_result`, `issue`, `fix`, `plan`, `approval_required`, `step_started`, `step_done`, `run_done`, `status`(스트림 끝).

## 5. 핵심 설계 결정과 이유

코드를 "더 좋게" 고치기 전에 읽을 것. 대부분 의도된 단순함이다.

| 결정 | 이유 | 바꿀 때 주의 |
|---|---|---|
| 정책 필터 → 라우터 순서(`gateway.select`) | 등급이 허용 모델을 먼저 정하고 그 안에서만 고른다. 외부 상용 모델을 붙일 근거 | 순서를 바꾸면 권한 우회가 된다 |
| 등급은 태그에서만 읽는다 | 모델이 민감도를 추정하면 틀릴 때 막을 수 없다 | 태그 없는 파일 = 최고 등급(`TOP`) |
| 모델 실효 등급 = min(모델 clearance, 환경 상한) | 외부 모델을 실수로 높게 등록해도 `env_max.external`이 막는다 | `policy.yaml`의 `env_max` |
| 권한 필터는 검색 **조건**(`knowledge._VISIBLE`) + RLS | 검색 뒤에 거르면 순위·개수로 정보가 샌다. RLS는 두 번째 방어선 | SQL을 고치면 `test_knowledge`의 전 조합 테스트를 꼭 돌린다 |
| Workflow Engine 자체 구현(LangGraph 안 씀) | 직선형 5단계라 `runs.state` JSONB 한 칸이면 체크포인트·재개가 끝난다 | 분기·병렬이 필요해지면 그때 재검토 |
| 관문은 코드가 연다 | 소형 모델은 덜 끝내고 "완료"라고 한다 | Reviewer 지적은 재현 스크립트가 실패할 때만 실패로 친다 |
| 역할 3개(Planner/Worker/Reviewer)는 프롬프트 프로필 | 역할을 늘리면 컨텍스트 손실과 호출 수만 는다 | 프롬프트 파일만 다르고 루프는 같다 |
| 도구 호출 두 방식(`tools: native` / `schema`) | 로컬 모델마다 고유 형식 신뢰도가 다르다 | schema 방식은 `gateway._as_text`가 대화를 글로 바꾼다 |
| 파일 파싱도 샌드박스에서 | 파일은 신뢰할 수 없는 입력. 압축 폭탄·악성 문서 | `sandbox/parse.py`는 컨테이너 안에서만 돈다 |
| DB 하나(PostgreSQL) | 벡터·키워드·권한·큐·감사를 한 SQL로. 운영할 것이 하나 | Redis·벡터 DB 추가는 측정된 한계가 있을 때만 |
| 빌드 없는 화면 | npm 의존성 없이 반입이 단순 | 글꼴·아이콘은 `web/` 안에 있다(외부 호출 0) |
| 감사 로그 해시 연쇄 + advisory lock | 추가 전용, 위변조 탐지. 잠금은 연쇄가 갈라지지 않게 | 앱 역할은 insert/select만 가능 |

## 6. 자주 할 일: 어디를 고치나

| 하고 싶은 것 | 고칠 곳 |
|---|---|
| 모델 추가·교체 | `config/models.yaml` 항목 하나. 서버의 모델 이름(`--alias`)을 `id`와 같게. 업무 코드 수정 없음 |
| 모델별 프롬프트 차이 | `prompts/patches/<모델 ID>/<프롬프트 이름>.md` (기본 프롬프트 뒤에 덧붙음) |
| 외부 상용 모델 | `models.yaml`에 `env: external` 항목 + `policy.yaml`의 `env_max.external` 상향. OpenAI 호환이 아니면 `gateway._call`에 어댑터 |
| 등급·한도·예산 | `config/policy.yaml` |
| 하네스 기법 켜고 끄기 | `policy.yaml`의 `harness`. 2단계에서 켬/끔 점수를 비교하고, 효과 없으면 **코드째** 지운다 |
| 도구 추가 | `app/tools.py`의 `TOOLS`에 `_tool(...)` 한 줄 + 함수. 쓸 프로필은 `modes.THINKING_TOOLS` / `workflow.WORKER_TOOLS`. 외부에 영향을 주는 도구면 승인 절차를 먼저 만든다 |
| 업무 템플릿 추가 | `workflow.TEMPLATES`, `SPEC` 스키마의 enum, `prompts/worker_<이름>.md`, `planner_plan.md`의 규칙 |
| 파일 형식 추가 | `sandbox/parse.py`의 `PARSERS` + `app/files.py`의 `MAGIC` |
| 평가 문항 추가 | `evals/cases.py`. 채점기는 코드, `ref`(모범 답) 필수. `tests/test_evals.py`가 모범 답 통과를 검사 |
| 사내 인증(OIDC) | `app/auth.py`의 `current_user`, `api.login` 두 곳 |

## 7. 개발하며 만난 함정

같은 데서 다시 시간을 쓰지 않도록.

- **DB 준비 대기.** pgvector 컨테이너는 초기화 중 소켓으로만 뜨는 임시 서버를 먼저 띄운다. 거기 붙으면 스키마가 아직 없다. 테스트는 `pg_isready -h 127.0.0.1`(TCP)로 기다린다.
- **샌드박스 출력 상한.** 출력이 1MB를 넘으면 읽기를 멈추는데, 프로세스를 먼저 `kill`하지 않으면 파이프가 막혀 멈춘다(`sandbox.run`).
- **읽기 전용 컨테이너의 pytest.** `/tmp`가 없으면 pytest가 시작조차 못 한다. `--tmpfs /tmp` 필수.
- **내부 파일은 `.agent/`에.** 실행용 스크립트·큰 도구 결과·Reviewer 재현 스크립트를 작업 폴더 최상단에 두면 린트·테스트 대상이 되어 엉뚱한 실패가 난다. 숨김 폴더 `.agent/`에 두고 린트에서 뺀다.
- **로그인 해시가 이벤트 루프를 막음.** scrypt는 CPU를 오래 써서 500명 부하에서 로그인 지연이 5초까지 갔다. `asyncio.to_thread`로 옮겨 0.4초.
- **관리 명령이 끝나지 않음.** DB 풀을 열고 닫지 않으면 프로세스가 안 끝난다. `async with db.pool:` 사용.
- **Docker 볼륨 경로.** API 컨테이너가 샌드박스를 띄울 때 경로는 호스트 기준이다. 그래서 compose에서는 이름 있는 볼륨(`agent-platform-data`)의 세션 하위 폴더를 붙인다(`SANDBOX_VOLUME`).
- **장애 전환 차단은 30초.** 테스트에서 모델을 내리면 이후 테스트에 영향이 남는다. 그래서 장애 테스트 파일은 이름이 `test_zz_`로 맨 끝에 돈다.
- **Auto 판정 규칙.** "만들어/작성해/파일로" 같은 말이 있으면 Agent로 간다. 시험할 때 의도와 다른 모드로 가면 모드를 지정한다.
- **한글 파일명 다운로드.** 로캘이 없는 환경의 헤드리스 브라우저는 파일명을 `download`로 잡는다. 앱 문제가 아니다(헤더는 RFC 5987로 정상).

## 8. 테스트 구조

`tests/conftest.py`가 세션 시작 때 **진짜 스택**을 띄운다: pgvector 컨테이너(네트워크 없음, 유닉스 소켓), 가짜 모델 서버(9100), API 서버(9101), 워커. 테스트는 HTTP로만 시험한다.

- 가짜 모델 서버: `POST /script`로 넣은 응답을 순서대로 돌려준다(`{"content"}`, `{"tool", "arguments"}`, `{"json"}`). `GET /calls`로 받은 요청을 확인. `/down`으로 모델 장애 흉내.
- 테스트 프로세스의 외부 연결은 `socket.connect`를 가로채 실패시킨다.
- 필요한 것: Docker, `agent-sandbox` 이미지, `pgvector/pgvector:pg17` 이미지. 전체 약 2분.
- 계정: alice(internal, retail), bob(personal, risk), carol(restricted), librarian(personal), dave(internal). 비밀번호는 모두 `pw`.

## 9. 다음 담당자가 할 일 (우선순위 순)

1. **실제 llama.cpp 서버 연동.** 개발 PC에서 Quick → Thinking → Agent 순으로. 스트리밍 조각, 도구 호출, `response_format`(JSON 스키마) 모양이 가정과 다르면 `gateway._call`만 고친다. Gemma 4의 추론 켜고 끄기(`chat_template_kwargs.enable_thinking`)도 실제로 먹는지 확인.
2. **평가 기준선.** `python -m evals.run`으로 첫 점수표. 계열이 다른 둘째 모델도 같은 평가.
3. **현업 문항 확보.** 업무 3~5개, 유형당 10~20문항. 이미지(3)·Agent(2) 문항이 특히 부족.
4. **정보보호 부서 확인**: 데이터 등급 초안, 모델 출처 기준, **Docker 소켓 마운트**(API 서버가 뚫리면 호스트까지 위험) 분리 방안, gVisor 적용.
5. **2단계**: 사내 GPU와 같은 급을 빌려 모델 확정, 500명 포화 지점 측정, `policy.yaml` 출발값 교체, 하네스 스위치별 효과 측정.

## 10. 참고 자료

- [docs/architecture.md](docs/architecture.md): 「폐쇄망 Local AI Agent Platform 설계서」(2026-11-10). 요구사항과 다르게 판단한 점, 라이선스·출처 근거가 여기 있다.
- 첨부 가이드: 「망분리 금융사의 LLM 활용 장기 플랜」. 등급·규제 판단의 근거.
- [docs/data-grades.md](docs/data-grades.md): 등급 초안과 분류 규칙.
- 라이선스 메모: psycopg 3은 LGPL-3.0(수정 없이 라이브러리로 사용). kiwipiepy는 설계서에 LGPL로 적혔으나 설치된 0.24.1 메타데이터는 Apache-2.0이라 저장소 LICENSE로 재확인 필요.
- 이전 개발 위치: InfraPilot 저장소(`juhyunit/InfraPilot`)의 `agent-platform/`. 이력은 옮기지 않았다.
