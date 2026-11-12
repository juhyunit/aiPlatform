// 화면. 모델의 추론 전문이 아니라 실행 이벤트를 보여준다.
// 모델이 쓴 글은 textContent로 넣거나, md()처럼 먼저 이스케이프한 뒤에만 태그를 입힌다.
const $ = (s) => document.querySelector(s);
const GRADE = { public: '공개', internal: '내부 일반', restricted: '내부 중요', pseudonymized: '가명정보', personal: '개인(신용)정보' };
const form = $('#composer');
let me, session = null, images = [], files = [];
const uploaded = new Set();     // 사용자가 올린 파일. 답이 만든 파일과 구분한다

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.innerHTML = `<use href="#i-${name}"/>`;
  return svg;
}

// 답의 글 모양: 코드 블록, 인라인 코드, 굵게, 제목, 목록만 다룬다
function md(text) {
  const safe = text.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  return safe.split('```').map((part, i) => (i % 2
    ? `<pre><code>${part.replace(/^\w*\n/, '')}</code></pre>`
    : part.replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/^#{1,4} (.+)\n?/gm, '<h3>$1</h3>').replace(/^[-*] (.+)\n?/gm, '<li>$1</li>'))).join('');
}

async function api(path, options = {}) {
  if (options.json) options = { ...options, body: JSON.stringify(options.json), headers: { 'Content-Type': 'application/json' } };
  const r = await fetch(path, options);
  if (r.status === 401) { $('#app').hidden = true; $('#login').hidden = false; throw new Error('로그인이 필요합니다'); }
  if (!r.ok) throw new Error((await r.json()).detail);
  return r;
}

async function* events(response) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    for (let i; (i = buffer.indexOf('\n\n')) >= 0; buffer = buffer.slice(i + 2)) {
      const [type, data] = buffer.slice(0, i).split('\n');
      yield [type.slice(7), JSON.parse(data.slice(6))];
    }
  }
}

$('#login').onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api('/api/login', { method: 'POST', json: Object.fromEntries(new FormData(e.target)) });
    start();
  } catch (err) { $('#login-error').textContent = err.message; }
};

async function start() {
  me = await (await api('/api/me')).json();
  $('#login').hidden = true; $('#app').hidden = false;
  $('#user').textContent = `${me.id} · 열람 등급 ${GRADE[me.grades.at(-1)]}`;
  form.grade.replaceChildren(...me.grades.map((g) => el('option', { value: g, textContent: GRADE[g] })));
  form.model.replaceChildren(el('option', { value: 'auto', textContent: '모델 자동' }), ...me.models.map((m) => el('option', { value: m, textContent: m })));
  newChat(); loadSessions(); loadMemories();
}

// 새 채팅은 화면만 비운다. 대화는 첫 질문이나 첫 첨부 때 만든다
function newChat() {
  session = null; images = []; files = [];
  $('main').classList.add('empty'); $('#new').classList.add('on');
  $('#messages').replaceChildren(); $('#chips').replaceChildren();
  $('#title').textContent = ''; $('#grade').hidden = true; $('#files').hidden = true;
  form.grade.disabled = false; form.grade.value = me.grades.at(-1);
  form.message.focus(); loadSessions();
}
$('#new').onclick = newChat;

async function ensureSession(title) {
  if (session) return;
  session = await (await api('/api/sessions', { method: 'POST', json: { title, grade: form.grade.value } })).json();
  showSession(); loadSessions();
}

function showSession() {
  $('#new').classList.remove('on');
  $('#title').textContent = session.title;
  $('#grade').textContent = GRADE[me.grades[session.grade]]; $('#grade').hidden = false;
  form.grade.value = me.grades[session.grade]; form.grade.disabled = true;
}

async function loadSessions() {
  const rows = await (await api('/api/sessions')).json();
  $('#sessions').replaceChildren(...rows.map((s) => el('li', { className: session?.id === s.id ? 'on' : '', textContent: s.title, title: s.title, onclick: () => openSession(s) })));
}

async function openSession(s) {
  session = s; images = [];
  $('#chips').replaceChildren(); showSession();
  const [rows, runs] = await Promise.all([
    (await api(`/api/sessions/${s.id}/messages`)).json(),
    (await api('/api/runs')).json(),
  ]);
  const active = runs.find((r) => r.session_id === s.id && ['queued', 'running', 'waiting', 'paused'].includes(r.status));
  $('main').classList.toggle('empty', !rows.length && !active);
  $('#messages').replaceChildren(...rows.map((m) => el('div', { className: 'msg ' + m.role },
    m.role === 'user' ? el('div', { className: 'text', textContent: m.content }) : el('div', { className: 'text', innerHTML: md(m.content) }))));
  $('#messages').scrollTop = $('#messages').scrollHeight;
  loadSessions(); await loadFiles();
  if (active) resumeRun(active);
}

async function resumeRun(r) {
  const steps = el('details', { className: 'steps running', open: true }, el('summary', { textContent: '생각하는 중' }), el('ul'));
  const view = { steps, text: el('div', { className: 'text' }), extra: el('div'), chips: el('div', { className: 'chips' }),
    raw: '', request: { session_id: session.id, message: r.request || '' }, before: files, run: r.id, last: 0 };
  $('#messages').append(
    el('div', { className: 'msg user' }, el('div', { className: 'text', textContent: r.request || '' })),
    el('div', { className: 'msg assistant' }, steps, view.text, view.extra, view.chips));
  try {
    await render(view, await api(`/api/runs/${r.id}/events`));
  } catch (err) { line(view, err.message, 'bad'); settle(view, err.message, false); }
}

// 파일은 어디서든 눌러서 내려받는다: 머리줄의 파일 목록, 답 아래의 새 파일 칩
function download(name, className) {
  return el('a', { className, href: `/api/sessions/${session.id}/files/${encodeURI(name)}`, download: name.split('/').pop(), title: `${name} 내려받기` },
    icon('file'), el('span', { textContent: name }), icon('download'));
}

async function loadFiles() {
  files = await (await api(`/api/sessions/${session.id}/files`)).json();
  $('#files').hidden = !files.length;
  $('#file-count').textContent = files.length;
  $('#file-list').replaceChildren(...files.map((f) => el('li', {}, download(f, 'file'))));
  return files;
}
document.addEventListener('click', (e) => { if (!$('#files').contains(e.target)) $('#files').open = false; });

async function loadMemories() {
  const rows = await (await api('/api/memories')).json();
  $('#memories').replaceChildren(...rows.map((m) => el('li', {}, el('span', { textContent: m.text }), el('button', {
    className: 'icon', title: '삭제', onclick: async () => { await api(`/api/memories/${m.id}`, { method: 'DELETE' }); loadMemories(); },
  }, icon('x')))));
}

$('#new-memory').onsubmit = async (e) => {
  e.preventDefault();
  await api('/api/memories', { method: 'POST', json: { text: e.target.text.value } });
  e.target.reset(); loadMemories();
};

$('#collapse').onclick = $('#expand').onclick = () => $('#app').classList.toggle('collapsed');

$('#attach').onchange = async (e) => {
  for (const file of e.target.files) {
    const chip = el('span', { className: 'chip' }, icon('file'), el('span', { textContent: file.name }));
    $('#chips').append(chip);
    try {
      await ensureSession(file.name);
      const doc = await (await api(`/api/sessions/${session.id}/files/${encodeURIComponent(file.name)}?grade=${form.grade.value}`, { method: 'PUT', body: file })).json();
      uploaded.add(doc.name);
      if (/\.(png|jpe?g)$/i.test(doc.name)) images.push(doc.name);     // 이미지는 이번 질문에 붙여 보낸다
    } catch (err) { chip.classList.add('bad'); chip.lastChild.textContent = `${file.name} — ${err.message}`; }
  }
  e.target.value = ''; loadFiles();
};

form.message.oninput = () => { form.message.style.height = 'auto'; form.message.style.height = form.message.scrollHeight + 'px'; };
form.message.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } };

form.onsubmit = async (e) => {
  e.preventDefault();
  const message = form.message.value.trim();
  if (!message || form.querySelector('.send').disabled) return;
  await ensureSession(message.slice(0, 40));
  const attached = [...$('#chips').children].filter((c) => !c.classList.contains('bad')).map((c) => c.textContent);
  send({ session_id: session.id, message, mode: form.mode.value, model: form.model.value, search: form.search.checked, images }, attached);
  form.message.value = ''; form.message.oninput(); images = []; $('#chips').replaceChildren();
};

// 요청 하나를 보내고, 답 자리에 진행 과정과 답과 새로 생긴 파일을 그린다
async function send(request, attached = []) {
  const steps = el('details', { className: 'steps running', open: true }, el('summary', { textContent: '생각하는 중' }), el('ul'));
  const view = { steps, text: el('div', { className: 'text' }), extra: el('div'), chips: el('div', { className: 'chips' }),
    raw: '', request, before: files, run: null, last: 0 };
  $('main').classList.remove('empty');
  $('#messages').append(
    el('div', { className: 'msg user' }, el('div', { className: 'text', textContent: request.message }),
      el('div', { className: 'chips' }, ...attached.map((name) => el('span', { className: 'chip' }, icon('file'), el('span', { textContent: name }))))),
    el('div', { className: 'msg assistant' }, steps, view.text, view.extra, view.chips));
  $('#messages').scrollTop = $('#messages').scrollHeight;
  form.querySelector('.send').disabled = true;
  try {
    await render(view, await api('/api/chat', { method: 'POST', json: request }));
  } catch (err) { line(view, err.message, 'bad'); settle(view, err.message, false); }
  form.querySelector('.send').disabled = false;
}

function follow(fn) {      // 맨 아래를 보고 있을 때만 따라 내려간다
  const m = $('#messages'), near = m.scrollHeight - m.scrollTop - m.clientHeight < 120;
  fn();
  if (near) m.scrollTop = m.scrollHeight;
}

function write(view, raw) { follow(() => { view.raw = raw; view.text.innerHTML = md(raw); }); }

function line(view, message, cls = '') {
  follow(() => {
    view.steps.lastChild.append(el('li', { className: cls, textContent: message }));
    if (view.steps.classList.contains('running')) view.steps.firstChild.textContent = message.slice(0, 80);
  });
}

function settle(view, summary, ok) {     // 진행 과정을 한 줄로 접는다
  view.steps.classList.remove('running'); view.steps.open = false;
  view.steps.firstChild.textContent = summary;
  view.steps.firstChild.style.color = ok ? '' : 'var(--bad)';
}

async function control(view, action, criteria) {
  view.extra.replaceChildren();
  view.steps.classList.add('running'); view.steps.open = true;
  try {
    await api(`/api/runs/${view.run}/control`, { method: 'POST', json: { action, criteria } });
    await render(view, await api(`/api/runs/${view.run}/events?after=${view.last}`));
  } catch (err) { line(view, err.message, 'bad'); settle(view, err.message, false); }
}

const button = (label, onclick, className = '') => el('button', { type: 'button', className, textContent: label, onclick });
const actions = (...buttons) => el('div', { className: 'actions' }, ...buttons);

async function render(view, response) {
  for await (const [type, d] of events(response)) {
    view.last = d.event_id ?? view.last;
    if (type === 'run') view.run = d.id;
    else if (type === 'mode') {
      Object.assign(view, { mode: d.mode, model: d.model });
      line(view, `${d.promoted ? '더 깊게 다시 실행 · ' : ''}${d.mode} · ${d.model}`);
      if (d.mode === 'agent') view.extra.replaceChildren(actions(button('일시 정지', () => control(view, 'pause')), button('취소', () => control(view, 'cancel'))));
    }
    else if (type === 'token') write(view, d.reset ? '' : view.raw + d.text);
    else if (type === 'tool_call') { write(view, ''); line(view, `도구 ${d.tool} ${JSON.stringify(d.arguments).slice(0, 160)}`); }
    else if (type === 'tool_result') line(view, `${d.ok ? '결과' : '실패'}: ${d.preview.slice(0, 160)}`, d.ok ? '' : 'bad');
    else if (type === 'check_result') line(view, `검사 ${d.ok ? '통과' : '실패'}: ${d.check}${d.problems?.length ? ' — ' + d.problems.join('; ') : ''}`, d.ok ? 'ok' : 'bad');
    else if (type === 'issue') line(view, `${d.profile === 'Reviewer' ? (d.confirmed ? 'Reviewer 지적(재현됨)' : 'Reviewer 의견(참고)') : '문제'}: ${d.problems.join('; ')}`, d.confirmed === false ? 'note' : 'bad');
    else if (type === 'fix') line(view, d.ok ? '수정 후 재검증 통과' : `수정 후에도 실패: ${d.problems.join('; ')}`, d.ok ? 'ok' : 'bad');
    else if (type === 'step_started') line(view, `단계 ${d.step} 시작: ${d.task}`);
    else if (type === 'step_done') line(view, `단계 ${d.step} 완료: ${d.summary}`, 'ok');
    else if (type === 'plan') view.plan = d;
    else if (type === 'approval_required') approval(view);
    else if (type === 'status' && d.status === 'paused') { settle(view, '일시 정지됨', true); view.extra.replaceChildren(actions(button('재개', () => control(view, 'resume'), 'go'), button('취소', () => control(view, 'cancel')))); }
    else if (type === 'status' && d.status === 'cancelled') { view.extra.replaceChildren(); settle(view, '취소했습니다', true); }
    else if (type === 'run_done') await done(view, d);
  }
}

// 첫 번째 관문: 완료 조건과 계획을 보여주고 승인을 받는다. 조건은 고쳐서 승인할 수 있다
function approval(view) {
  settle(view, '계획을 세웠습니다. 실행 전에 확인해 주세요', true);
  const criteria = el('textarea', { rows: view.plan.criteria.length + 1, value: view.plan.criteria.map((c) => c.text).join('\n') });
  follow(() => view.extra.replaceChildren(el('div', { className: 'plan' },
    el('strong', { textContent: `${view.plan.template} 계획` }),
    el('p', { textContent: '완료 조건 — 한 줄에 하나. 고치면 계획을 다시 세웁니다' }), criteria,
    el('p', { textContent: '단계' }),
    el('ol', {}, ...view.plan.steps.map((s) => el('li', {}, s.task, el('small', { textContent: `${s.criteria.join(', ')} · 검사: ${s.check}` })))),
    actions(button('승인하고 실행', () => control(view, 'approve', criteria.value.split('\n').map((t) => t.trim()).filter(Boolean)), 'go'),
      button('취소', () => control(view, 'cancel'))))));
}

async function done(view, d) {
  view.extra.replaceChildren();
  let raw = d.report ?? view.raw;
  for (const s of d.sources ?? [])    // 인용 번호를 문서명과 쪽수로 바꿔 보여준다
    raw = raw.replaceAll(`[#${s.id}]`, `[${s.name}${s.page ? ' ' + s.page + '쪽' : ''}]`);
  write(view, raw);
  if (d.error && !d.report) line(view, d.error, 'bad');
  const label = { quick: 'Quick', thinking: 'Thinking', agent: 'Agent' }[view.mode] ?? '';
  settle(view, d.verified ? `${label} · ${view.model} · 검증: ${d.verified} ${d.ok ? '통과' : '실패'}`
    : d.report ? `${label} · ${d.ok ? '완료' : '미완료'}` : d.error ?? label, d.ok);
  if (view.mode === 'quick') view.extra.replaceChildren(actions(button('더 깊게', () => send({ ...view.request, mode: 'thinking' }))));
  const made = (await loadFiles()).filter((f) => !view.before.includes(f) && !uploaded.has(f));     // 이번 답에서 새로 생긴 파일
  follow(() => view.chips.replaceChildren(...made.map((f) => download(f, 'chip'))));
}

api('/api/me').then(start, () => {});
