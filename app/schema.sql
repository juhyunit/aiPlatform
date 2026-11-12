-- 저장소는 PostgreSQL 하나다: 업무 데이터, 벡터, 작업 큐, 추적 기록, 감사 로그.
-- DB 컨테이너가 처음 뜰 때 소유자 권한으로 한 번 실행된다(docker-entrypoint-initdb.d).
-- 앱은 권한이 제한된 app 역할로 접속한다. 행 수준 보안(RLS)은 앱 코드의 권한 필터 뒤에 있는 두 번째 방어선이다.

create extension if not exists vector;

\set pw `echo "$APP_DB_PASSWORD"`
create role app login password :'pw';

-- 요청마다 앱이 트랜잭션 안에서 set_config로 넣는 값. 넣지 않으면 null이라 아무 행도 보이지 않는다.
create function app_user() returns text language sql stable as
  $$ select nullif(current_setting('app.user', true), '') $$;
create function app_clearance() returns smallint language sql stable as
  $$ select nullif(current_setting('app.clearance', true), '')::smallint $$;
create function app_groups() returns text[] language sql stable as
  $$ select string_to_array(nullif(current_setting('app.groups', true), ''), ',') $$;

create table users (
  id text primary key,
  password_hash text not null,
  clearance smallint not null,             -- 열람할 수 있는 최고 데이터 등급 (policy.yaml grades의 순번)
  groups text[] not null default '{}'      -- 접근 그룹
);

create table sessions (
  id uuid primary key default gen_random_uuid(),
  user_id text not null references users,
  title text not null,
  grade smallint not null,                 -- 사용자가 선언한 세션 등급. 입력한 글의 등급으로 쓴다
  summary text not null default '',        -- 압축된 이전 대화. 원문은 messages에 남는다
  summarized_upto bigint not null default 0,
  created_at timestamptz not null default now()
);

create table messages (
  id bigserial primary key,
  session_id uuid not null references sessions on delete cascade,
  role text not null,
  content text not null,
  created_at timestamptz not null default now()
);

-- 첨부 파일(session), 사용자가 저장한 기억(memory), 사내 지식(org)을 한 표에 둔다.
create table documents (
  id uuid primary key default gen_random_uuid(),
  scope text not null check (scope in ('session', 'memory', 'org')),
  owner text not null references users,
  session_id uuid references sessions on delete cascade,
  name text not null,
  grade smallint not null,
  groups text[] not null default '{}',     -- org 문서의 접근 그룹. 비어 있으면 등급만 본다
  embed_model text not null,               -- 임베딩 모델을 바꾸면 재색인 대상을 찾는 데 쓴다
  created_at timestamptz not null default now()
);

create table chunks (
  id bigserial primary key,
  document_id uuid not null references documents on delete cascade,
  page int,
  heading text not null default '',
  text text not null,
  tsv tsvector not null,                   -- Kiwi 형태소 토큰
  embedding vector(1024) not null
);
create index on chunks using hnsw (embedding vector_cosine_ops);
create index on chunks using gin (tsv);

-- 요청 하나가 실행 하나다. Agent 모드에서는 작업 큐이자 워크플로 체크포인트다.
create table runs (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references sessions on delete cascade,
  user_id text not null references users,
  mode text not null,
  status text not null,                    -- queued | running | waiting | paused | done | failed | cancelled
  state jsonb not null default '{}',
  tokens int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index on runs (status, created_at);

-- 추적 표. 화면에 보내는 진행 이벤트와 같은 내용이다.
create table events (
  id bigserial primary key,
  run_id uuid not null references runs on delete cascade,
  type text not null,
  data jsonb not null,
  created_at timestamptz not null default now()
);
create index on events (run_id, id);

-- 감사 로그. 추가 전용이고 해시로 앞 행과 이어진다.
create table audit_log (
  id bigserial primary key,
  at timestamptz not null,
  user_id text not null,
  action text not null,
  detail jsonb not null,
  prev_hash text not null,
  hash text not null
);

grant select on users to app;
grant select, insert, update, delete on sessions, messages, documents, chunks, runs, events to app;
grant select, insert on audit_log to app;
grant usage on all sequences in schema public to app;

alter table sessions enable row level security;
create policy own on sessions using (user_id = app_user());

alter table messages enable row level security;
create policy own on messages using (exists (select 1 from sessions s where s.id = session_id));

alter table documents enable row level security;
create policy readable on documents using (
  grade <= app_clearance()
  and (owner = app_user() or (scope = 'org' and (groups = '{}' or groups && app_groups())))
);

alter table chunks enable row level security;
create policy readable on chunks using (exists (select 1 from documents d where d.id = document_id));

alter table runs enable row level security;
create policy own on runs using (user_id = app_user());

alter table events enable row level security;
create policy own on events using (exists (select 1 from runs r where r.id = run_id));

-- 작업 워커는 사용자 구분 없이 대기 중인 Agent 작업을 집어야 한다. 그 한 가지만 소유자 권한으로 연다.
create function claim_run() returns table (id uuid, user_id text) language sql security definer as $$
  update runs set status = 'running', updated_at = now()
  where runs.id = (select r.id from runs r where r.status = 'queued' order by r.created_at
                   for update skip locked limit 1)
  returning runs.id, runs.user_id
$$;

-- 워커가 죽었다 살아나면 실행 중이던 Agent 작업을 큐로 되돌린다. 마지막 체크포인트부터 재개된다.
create function requeue_runs() returns void language sql security definer as $$
  update runs set status = 'queued' where status = 'running' and mode = 'agent'
$$;
