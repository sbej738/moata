'use strict';
/* =========================================================================
   모아타 승객 앱
   동승 요청(빠른/예약) → 매칭 중 → 동승 제안 → 탑승 전 안내 → 운행 중 → 정산
   ========================================================================= */
const POLL_MS = 3000;
const PRED_MS = 30000;

const P = {
  user: null, map: null, st: null, sig: null, view: null,
  viewing: null,                 // 프로필에서 연 지난 정산
  dismissed: Number(localStorage.getItem('moata.dismissed') || 0),
  pickTarget: null, predAt: 0, pred: null,
  form: { type: 'quick', origin: null, dest: null, quote: null, date: null, start: null, dur: 30,
          seats: 1, companion: false, detourMin: null, fallback: 'solo' },
  me: null,                      // 지도에 표시한 내 위치
  autoLocated: false,
};
const DETOUR_OPTIONS = [3, 5, 10, 15, 20, 30];

function save(key, v) { try { localStorage.setItem(key, String(v)); } catch (e) { /* 저장 못 해도 동작 */ } }

/* ---------- 시작 ---------- */
async function boot() {
  await loadConfig();
  P.user = await requireLogin();
  P.form.detourMin = CONFIG.params.default_detour_min || 5;
  P.map = await createMap($('#map'));
  P.map.onClick(onMapClick);
  window.addEventListener('resize', () => P.map.resize());
  drawTopbar();
  await render(true);
  setInterval(() => { if (!document.hidden) render(false).catch(() => {}); }, POLL_MS);
  setInterval(tick, 1000);
}

function drawTopbar(mode = 'normal') {
  const tb = $('#topbar');
  if (mode === 'hidden') { tb.innerHTML = ''; return; }
  tb.innerHTML = `
    <span class="pill">${icon('pin', 'sm')}${esc(CONFIG.region)}</span>
    <span class="row"><button class="round-btn" id="locate-btn" aria-label="내 위치로 이동">${icon('crosshair')}</button>
      <button class="round-btn" id="me-btn" aria-label="내 정보">${icon('user')}</button></span>`;
  $('#me-btn').addEventListener('click', openProfile);
  $('#locate-btn').addEventListener('click', showMyLocation);
}

function sheet(body, actions = '', { split = false, stack = false, tall = false } = {}) {
  const s = $('#sheet');
  s.className = `sheet${tall ? ' tall' : ''}`;
  s.innerHTML = `${handleHtml()}<div class="sheet-body">${body}</div>
    ${actions ? `<div class="sheet-actions${split ? ' split' : ''}${stack ? ' stack' : ''}">${actions}</div>` : ''}`;
  wireSheet(s, P.view);
  return s;
}

/* ---------- 현재 위치 ---------- */
function drawMe() { if (P.me) P.map.pin(P.me, '<div class="pin-me" title="내 위치"></div>'); }

async function showMyLocation() {
  const btn = $('#locate-btn');
  btn?.classList.add('spin');
  try {
    P.me = await locateMe();
    P.map.center(P.me, 16);
    drawMe();
    if (P.view === 'form' && !P.form.origin) await useMyLocationAsOrigin();
  } catch (e) { toast(e.message, true); }
  finally { btn?.classList.remove('spin'); }
}

async function useMyLocationAsOrigin() {
  if (!P.me) P.me = await locateMe();
  const addr = await P.map.reverse(P.me);
  await setPlace('origin', { lat: P.me.lat, lng: P.me.lng, name: addr ? `현재 위치 · ${addr}` : '현재 위치' });
}

/* 위치 권한을 이미 허용했다면 처음 요청 화면에서 출발지를 현재 위치로 채운다(권한 창은 띄우지 않는다) */
async function autoLocate() {
  if (P.autoLocated) return;
  P.autoLocated = true;
  if (!window.isSecureContext || !(await geoGranted())) return;
  try { P.me = await locateMe(6000); if (!P.form.origin && P.view === 'form') await useMyLocationAsOrigin(); }
  catch (e) { /* 조용히 넘어간다 */ }
}
function setFloat(html) { $('#float').innerHTML = html; }

async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); P.sig = null; await render(true); return r; }
  catch (e) { toast(e.message, true); return null; }
}

/* ---------- 렌더 ---------- */
async function render(force) {
  const st = await api('/api/me/state');
  P.st = st;
  syncClock(st.ts);
  const view = pickView(st);
  const a = st.active, g = a?.group, r = a?.request;
  const sig = JSON.stringify([view, r?.id, r?.status, r?.note, r?.notice_ack_at, g?.id, g?.status, g?.progress,
    g?.members.map((m) => [m.request_id, m.accepted, m.share, m.paid, m.notice_ack]), g?.driver, g?.arriving_at,
    g?.expires_at, st.history.map((h) => [h.request.id, h.request.paid_at]), P.viewing?.request.id]);
  if (!force && sig === P.sig) {
    if (view === 'matching' && Date.now() - P.predAt > PRED_MS) updatePrediction(r);
    return;
  }
  P.sig = sig;
  P.view = view;
  drawTopbar(view === 'riding' ? 'hidden' : 'normal');
  setFloat('');
  switch (view) {
    case 'settle': return viewSettlement(P.viewing);
    case 'settle-fresh': return viewSettlement(st.history[0], true);
    case 'form': return viewForm();
    case 'matching': return viewMatching(a);
    case 'proposal': return viewProposal(a);
    case 'notice': return viewNotice(a);
    case 'boarding': return viewBoarding(a);
    default: return viewRiding(a);
  }
}

function pickView(st) {
  if (P.viewing) return 'settle';
  const a = st.active;
  if (!a) {
    const last = st.history[0];
    return last && last.request.status === 'done' && last.group?.status === 'completed' && last.request.id > P.dismissed
      ? 'settle-fresh' : 'form';
  }
  switch (a.request.status) {
    case 'waiting': return 'matching';
    case 'proposed': return 'proposal';
    case 'confirmed': return a.request.notice_ack_at ? 'boarding' : 'notice';
    default: return 'riding';
  }
}

/* 1초마다: 남은 시간 표시 */
function tick() {
  $$('[data-countdown]').forEach((el) => {
    const s = secondsUntil(el.dataset.countdown);
    el.textContent = mmss(s);
    if (s === 0 && !el.dataset.done) { el.dataset.done = '1'; setTimeout(() => render(true), 1500); }
  });
}

/* =========================================================================
   1. 동승 요청
   ========================================================================= */
function quickWindow() {
  const now = st().now;
  return { start: now, end: addMin(now, CONFIG.params.quick_window_min) };
}
function st() { return P.st; }
function reservedDefaults() {
  const f = P.form;
  if (!f.date || !f.start) {
    const d = fromIso(addMin(st().now, CONFIG.params.reserve_min_lead_min + 20));
    d.setMinutes(Math.ceil(d.getMinutes() / 10) * 10);
    const iso = toIso(d);
    f.date = iso.slice(0, 10);
    f.start = iso.slice(11, 16);
  }
}
function formWindow() {
  const f = P.form;
  if (f.type === 'quick') return quickWindow();
  reservedDefaults();
  const start = `${f.date}T${f.start}`;
  return { start, end: addMin(start, f.dur) };
}
function deadlineOf(win) { return addMin(win.end, -CONFIG.params.deadline_min); }

function viewForm() {
  const f = P.form;
  const quick = f.type === 'quick';
  const win = formWindow();
  const q = f.quote;
  const timeText = quick ? `지금~${CONFIG.params.quick_window_min}분` : `${md(win.start)} ${hhmm(win.start)}–${hhmm(win.end)}`;
  const s = sheet(`
    <div class="seg" role="tablist" aria-label="매칭 종류">
      <button role="tab" aria-selected="${quick}" data-type="quick">${icon('bolt', 'sm')}빠른 매칭</button>
      <button role="tab" aria-selected="${!quick}" data-type="reserved">${icon('calendar', 'sm')}예약 매칭</button>
    </div>
    <h1 class="title mt16">어디로 가세요?</h1>
    <div class="places">
      <button class="place-row" data-pick="origin"><span class="dot-o" aria-hidden="true"></span>
        <span class="grow"><span class="lbl">출발</span><span class="val ${f.origin ? '' : 'unset'}">${esc(f.origin?.name || '출발지를 골라 주세요')}</span></span>${icon('chevron', 'sm')}</button>
      <button class="place-row" data-pick="dest"><span class="dot-d" aria-hidden="true"></span>
        <span class="grow"><span class="lbl">도착</span><span class="val ${f.dest ? '' : 'unset'}">${esc(f.dest?.name || '도착지를 골라 주세요')}</span></span>${icon('chevron', 'sm')}</button>
    </div>
    <div class="conds">
      <button class="cond" id="c-time" ${quick ? 'disabled' : ''} aria-label="시간대 ${timeText}"><span class="k">시간대</span><span class="v">${timeText}</span></button>
      <button class="cond" id="c-seats" aria-label="인원 ${f.seats}명, 눌러서 바꾸기"><span class="k">인원</span><span class="v">${f.seats}명</span></button>
      <button class="cond" id="c-detour" aria-label="우회 허용 최대 ${f.detourMin}분, 눌러서 바꾸기"><span class="k">우회 허용</span><span class="v">최대 ${f.detourMin}분</span></button>
    </div>
    ${f.seats > 1 ? `<label class="notice-line" style="color:var(--text)"><input type="checkbox" id="companion" ${f.companion ? 'checked' : ''}> 동행인도 저와 같은 성별이에요</label>` : ''}
    <div class="card info mt12">
      <div class="quote"><div><span class="small">혼자 타면 예상 요금</span><div class="amount money">${q ? won(q.taxi_fare) : '—'}</div></div>
        <span class="small">${q ? `${kmShort(q.distance_m)} · 약 ${mins(q.duration_s)}` : '출발지와 도착지를 고르면 계산해요'}</span></div>
    </div>
    <p class="notice-line">${icon('shield-check', 'xs')}같은 성별끼리만 매칭돼요 · 요청 마감 ${hhmm(deadlineOf(win))}</p>
    <div class="between mt12 small"><span class="muted">동승자가 없으면</span>
      <div class="chips" style="margin:0" role="radiogroup" aria-label="미매칭 시">
        <button class="chip" role="radio" aria-checked="${f.fallback === 'solo'}" data-fb="solo">혼자 타기</button>
        <button class="chip" role="radio" aria-checked="${f.fallback === 'cancel'}" data-fb="cancel">요청 취소</button></div></div>`,
  `<button class="btn primary block" id="submit" ${f.origin && f.dest ? '' : 'disabled'}>동승 요청하기</button>`, { tall: true });

  $$('[data-type]', s).forEach((b) => b.addEventListener('click', () => { f.type = b.dataset.type; viewForm(); }));
  $$('[data-pick]', s).forEach((b) => b.addEventListener('click', () => openPlacePicker(b.dataset.pick)));
  $('#c-time', s).addEventListener('click', openTimePicker);
  $('#c-seats', s).addEventListener('click', () => { f.seats = f.seats === 1 ? 2 : 1; viewForm(); });
  $('#c-detour', s).addEventListener('click', openDetourPicker);
  $('#companion', s)?.addEventListener('change', (e) => { f.companion = e.target.checked; });
  $$('[data-fb]', s).forEach((b) => b.addEventListener('click', () => { f.fallback = b.dataset.fb; viewForm(); }));
  $('#submit', s).addEventListener('click', submitRequest);
  drawFormMap();
  autoLocate();
}

function drawFormMap() {
  const f = P.form;
  drawRequestMode(P.map, f.origin, f.dest, f.quote?.polyline);
  drawMe();
  const pts = [f.origin, f.dest].filter(Boolean);
  P.map.fit(pts.length ? pts : [P.me || CONFIG.demo_center], { top: 70, bottom: 520 });
}

/* 우회 허용(분) 고르기 */
function openDetourPicker() {
  const f = P.form;
  const solo = f.quote ? Math.max(1, Math.round(f.quote.duration_s / 60)) : null;
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = `<div class="panel fit" role="dialog" aria-modal="true" aria-labelledby="dt-title">
    <div class="panel-head"><div><h2 class="title" id="dt-title">얼마나 더 돌아가도 괜찮나요?</h2>
      <p class="subtitle">동승자를 태우고 내려 주느라 혼자 갈 때보다 더 걸리는 시간이에요${solo ? ` (혼자 가면 약 ${solo}분)` : ''}</p></div>
      <button class="close-btn" data-close aria-label="닫기">${icon('x')}</button></div>
    <div class="panel-body"><div class="opt-list" role="radiogroup" aria-labelledby="dt-title">
      ${DETOUR_OPTIONS.map((m) => `<button class="opt" role="radio" aria-checked="${m === f.detourMin}" data-min="${m}">
        <span>최대 ${m}분 더<small>${m < 5 ? '거의 바로 가요 · 매칭이 조금 어려워요' : m === 5 ? '보통 · 추천' : m <= 10 ? '조금 여유 있게' : m <= 20 ? '여유 있게 · 매칭이 쉬워져요' : '많이 돌아가도 괜찮아요'}</small></span>
        ${m === f.detourMin ? icon('check', 'sm') : ''}</button>`).join('')}
    </div></div></div>`;
  $('#root').appendChild(ov);
  const close = () => ov.remove();
  $('[data-close]', ov).addEventListener('click', close);
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  $$('[data-min]', ov).forEach((b) => b.addEventListener('click', () => { f.detourMin = Number(b.dataset.min); close(); viewForm(); }));
}

async function submitRequest(e) {
  const f = P.form;
  if (f.seats > 1 && !f.companion) { toast('동행인도 같은 성별인지 확인해 주세요', true); return; }
  e.currentTarget.disabled = true;
  const win = formWindow();
  const body = { type: f.type, origin: f.origin, dest: f.dest, seats: f.seats, detour_min: f.detourMin,
    fallback: f.fallback, companion_same_gender: f.companion };
  if (f.type === 'reserved') Object.assign(body, { window_start: win.start, window_end: win.end });
  await act(() => api('/api/requests', { method: 'POST', body }), f.type === 'quick' ? '빠른 매칭을 시작했어요' : '예약 매칭을 접수했어요');
  P.predAt = 0;
}

async function setPlace(key, place) {
  P.form[key] = { lat: place.lat, lng: place.lng, name: place.name };
  P.form.quote = null;
  if (P.form.origin && P.form.dest) {
    try { P.form.quote = await api('/api/quote', { method: 'POST', body: { origin: P.form.origin, dest: P.form.dest } }); }
    catch (e) { toast(e.message, true); }
  }
  if (P.view === 'form') viewForm();
}

/* 장소 선택 */
function openPlacePicker(key) {
  const ov = document.createElement('div');
  ov.className = 'overlay';
  const label = key === 'origin' ? '출발지' : '도착지';
  ov.innerHTML = `<div class="panel" role="dialog" aria-modal="true" aria-labelledby="pp-title">
    <div class="panel-head"><h2 class="title" id="pp-title">${label} 고르기</h2><button class="close-btn" data-close aria-label="닫기">${icon('x')}</button></div>
    <div class="panel-body">
      <label class="sr-only" for="pp-q">장소 검색</label>
      <input class="input" id="pp-q" placeholder="${P.map.kind === 'kakao' ? '장소·주소 검색' : '장소 이름 (예: 터미널, 삼척역)'}" autocomplete="off">
      <div class="row mt8"><button class="btn sm outline" id="pp-map">${icon('map', 'xs')}지도에서 고르기</button>
        <button class="btn sm outline" id="pp-here">${icon('crosshair', 'xs')}현재 위치</button></div>
      <ul class="list mt8" id="pp-list"></ul>
    </div></div>`;
  $('#root').appendChild(ov);
  const close = () => ov.remove();
  $('[data-close]', ov).addEventListener('click', close);
  let timer;
  const show = async () => {
    const results = await P.map.search($('#pp-q', ov).value);
    $('#pp-list', ov).innerHTML = results.length ? results.map((r, i) => `<li><button class="item" data-i="${i}">${icon('pin', 'sm')}
      <span class="grow"><b style="font-weight:500">${esc(r.name)}</b><br><span class="small muted">${esc(r.addr || '')}</span></span></button></li>`).join('')
      : '<li class="empty">검색 결과가 없어요. 지도에서 직접 골라 보세요.</li>';
    $$('[data-i]', ov).forEach((b) => b.addEventListener('click', () => { setPlace(key, results[Number(b.dataset.i)]); close(); }));
  };
  $('#pp-q', ov).addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(show, 250); });
  $('#pp-map', ov).addEventListener('click', () => { close(); startPinPick(key); });
  $('#pp-here', ov).addEventListener('click', async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try {
      P.me = await locateMe();
      const addr = await P.map.reverse(P.me);
      close();
      await setPlace(key, { lat: P.me.lat, lng: P.me.lng, name: addr ? `현재 위치 · ${addr}` : '현재 위치' });
    } catch (err) { toast(err.message, true); b.disabled = false; }
  });
  show();
  $('#pp-q', ov).focus();
}

/* 지도에서 핀으로 고르기: 가운데 고정 핀에 맞춰 지도를 움직이고 '이 위치로 설정' */
function startPinPick(key) {
  const label = key === 'origin' ? '출발지' : '도착지';
  const f = P.form;
  P.pickTarget = key;
  $('#sheet').classList.add('hidden');
  drawTopbar('hidden');
  P.map.clear();
  drawMe();
  const other = key === 'origin' ? f.dest : f.origin;
  if (other) P.map.pin(other, pinHtml(key === 'origin' ? 'd' : 'o'));
  const startAt = f[key] || P.me || other || CONFIG.demo_center;
  P.map.center(startAt, 16);
  const root = $('#root');
  const wrap = document.createElement('div');
  wrap.id = 'pick-ui';
  wrap.innerHTML = `
    <div class="float-card"><span class="small">${icon('map', 'xs')} 지도를 움직여 핀을 ${label}에 맞추세요</span>
      <button class="round-btn" id="pick-here" aria-label="내 위치로 이동" style="box-shadow:none;width:36px;height:36px">${icon('crosshair', 'sm')}</button></div>
    <div class="center-pin ${key === 'dest' ? 'dest' : ''}" aria-hidden="true"><span class="head"></span><span class="stem"></span><span class="shadow"></span></div>
    <div class="pick-bar" role="dialog" aria-label="${label} 정하기">
      <span class="small muted">${label}</span><div class="addr" id="pick-addr" aria-live="polite">주소를 찾는 중…</div>
      <div class="sheet-actions split" style="padding:12px 0 0"><button class="btn secondary" id="pick-cancel">취소</button>
        <button class="btn primary" id="pick-ok">이 위치로 설정</button></div>
    </div>`;
  root.appendChild(wrap);
  let addr = null, seq = 0;
  const lookup = async (c) => {
    const my = ++seq;
    $('#pick-addr').textContent = '주소를 찾는 중…';
    const a = await P.map.reverse(c);
    if (my !== seq || !$('#pick-addr')) return;
    addr = a;
    $('#pick-addr').textContent = a || `선택한 지점 (${c.lat.toFixed(5)}, ${c.lng.toFixed(5)})`;
  };
  const stopIdle = P.map.onIdle(lookup);
  lookup(P.map.getCenter());
  const finish = () => { stopIdle(); wrap.remove(); P.pickTarget = null; $('#sheet').classList.remove('hidden'); drawTopbar('normal'); };
  $('#pick-cancel').addEventListener('click', () => { finish(); viewForm(); });
  $('#pick-ok').addEventListener('click', async () => {
    const c = P.map.getCenter();
    finish();
    await setPlace(key, { lat: c.lat, lng: c.lng, name: addr || '지도에서 고른 지점' });
    if (P.view === 'form') viewForm();
  });
  $('#pick-here').addEventListener('click', async () => {
    try { P.me = await locateMe(); P.map.center(P.me, 16); } catch (e) { toast(e.message, true); }
  });
}

function onMapClick() { /* 위치는 가운데 핀으로 고른다 */ }

/* 예약 시간 선택 */
function openTimePicker() {
  const f = P.form;
  reservedDefaults();
  const today = fromIso(st().now);
  today.setHours(0, 0, 0, 0);
  const last = new Date(today); last.setDate(last.getDate() + CONFIG.params.reserve_max_days);
  // 달력: 오늘이 있는 주 일요일부터 마지막 예약 가능일이 있는 주 토요일까지
  const first = new Date(today); first.setDate(first.getDate() - first.getDay());
  const end = new Date(last); end.setDate(end.getDate() + (6 - end.getDay()));
  const cells = [];
  for (const d = new Date(first); d <= end; d.setDate(d.getDate() + 1)) cells.push(new Date(d));
  const iso = (d) => toIso(d).slice(0, 10);
  const calHtml = (sel) => {
    let html = '', month = null;
    for (let i = 0; i < cells.length; i += 7) {
      const week = cells.slice(i, i + 7);
      const m = week.find((d) => d >= today && d <= last)?.getMonth();
      if (m != null && m !== month) { month = m; html += `<div class="cal-month">${week.find((d) => d.getMonth() === m).getFullYear()}년 ${m + 1}월</div><div class="cal-head" aria-hidden="true">${'일월화수목금토'.split('').map((w) => `<span>${w}</span>`).join('')}</div>`; }
      html += `<div class="cal">${week.map((d) => {
        const ok = d >= today && d <= last;
        const t = d.getTime() === today.getTime();
        return `<button type="button" data-date="${iso(d)}" ${ok ? '' : 'disabled'} class="${t ? 'today' : ''}" aria-pressed="${iso(d) === sel}"
          aria-label="${d.getMonth() + 1}월 ${d.getDate()}일${t ? ' 오늘' : ''}${ok ? '' : ' 선택 불가'}">${d.getDate()}${t ? '<small>오늘</small>' : ''}</button>`;
      }).join('')}</div>`;
    }
    return html;
  };
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = `<div class="panel fit" role="dialog" aria-modal="true" aria-labelledby="tp-title">
    <div class="panel-head"><div><h2 class="title" id="tp-title">언제 출발할까요?</h2>
      <p class="subtitle">예약은 ${CONFIG.params.reserve_min_lead_min}분 뒤부터 ${CONFIG.params.reserve_max_days}일 뒤까지 잡을 수 있어요</p></div>
      <button class="close-btn" data-close aria-label="닫기">${icon('x')}</button></div>
    <div class="panel-body">
      <div id="tp-cal" role="group" aria-label="날짜(오늘부터 ${CONFIG.params.reserve_max_days}일 뒤까지)">${calHtml(f.date)}</div>
      <label class="field"><span>출발 희망 시작 시각</span><input class="input" type="time" id="tp-start" value="${f.start}" step="300"></label>
      <div class="field"><span>시간대 길이</span><div class="chips" role="radiogroup" aria-label="시간대 길이">
        ${[20, 30, 60].map((m) => `<button class="chip" role="radio" aria-checked="${m === f.dur}" data-dur="${m}">${m}분</button>`).join('')}</div></div>
    </div>
    <div class="sheet-actions"><button class="btn primary block" data-ok>이 시간대로 정하기</button></div></div>`;
  $('#root').appendChild(ov);
  const close = () => ov.remove();
  $('[data-close]', ov).addEventListener('click', close);
  let date = f.date, dur = f.dur;
  $('#tp-cal', ov).addEventListener('click', (e) => {
    const b = e.target.closest('[data-date]');
    if (!b || b.disabled) return;
    date = b.dataset.date;
    $$('[data-date]', ov).forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  });
  $$('[data-dur]', ov).forEach((b) => b.addEventListener('click', () => { dur = Number(b.dataset.dur); $$('[data-dur]', ov).forEach((x) => x.setAttribute('aria-checked', String(x === b))); }));
  $('[data-ok]', ov).addEventListener('click', () => {
    const start = $('#tp-start', ov).value;
    if (!start) { toast('시작 시각을 고르세요', true); return; }
    if (`${date}T${start}` < addMin(st().now, CONFIG.params.reserve_min_lead_min)) {
      toast(`예약은 ${CONFIG.params.reserve_min_lead_min}분 뒤 출발부터 가능해요. 곧 출발하면 빠른 매칭을 쓰세요`, true);
      return;
    }
    Object.assign(f, { date, start, dur });
    close();
    viewForm();
  });
}

/* =========================================================================
   2. 매칭 중
   ========================================================================= */
function stepsHtml(active) {
  const names = ['요청', '매칭', '제안', '탑승'];
  return `<div class="steps" aria-label="진행 단계">${names.map((n, i) => `<span class="${i <= active ? 'on' : ''}">${n}</span>`).join('')}</div>`;
}

function viewMatching(a) {
  const r = a.request;
  const quick = r.type === 'quick';
  const s = sheet(`
    <div class="row"><span class="badge"><span class="pulse"></span>매칭 중</span>
      <span class="badge gray">${quick ? `${icon('bolt', 'xs')}빠른 매칭` : `${icon('calendar', 'xs')}예약 매칭`}</span></div>
    <h1 class="title mt12">같이 탈 사람을 찾고 있어요</h1>
    <p class="subtitle">${esc(r.origin.name)} → ${esc(r.dest.name)} · ${quick ? '' : `${md(r.window_start)} `}${hhmm(r.window_start)}–${hhmm(r.window_end)}</p>
    ${r.note ? `<div class="card warn mt12 small">${esc(r.note)}</div>` : ''}
    <div id="predict">${predictionHtml(P.pred && P.pred.rid === r.id ? P.pred.data : null, r)}</div>
    ${stepsHtml(1)}
    ${CONFIG.demo_tools ? `<div class="mt12 small muted">시연할 때: <button class="link-btn" id="seed">비슷한 방향 데모 동승자 만들기</button></div>` : ''}`,
  `<button class="btn secondary block" id="cancel">요청 취소</button>`);
  $('#cancel', s).addEventListener('click', () => act(() => api(`/api/requests/${r.id}/cancel`, { method: 'POST' }), '요청을 취소했어요'));
  $('#seed', s)?.addEventListener('click', () => act(() => api('/api/demo/seed-near', { method: 'POST' }), '데모 동승자 2명을 만들었어요'));
  drawRequestMode(P.map, r.origin, r.dest);
  P.map.fit([r.origin, r.dest], { top: 70, bottom: 470 });
  if (!P.pred || P.pred.rid !== r.id || Date.now() - P.predAt > PRED_MS) updatePrediction(r);
}

function predictionHtml(d, r) {
  const deadline = r.deadline;
  if (!d) {
    return `<div class="ring-wrap"><div class="ring" aria-hidden="true">${ringSvg(0)}<div class="pct">…</div></div>
      <div class="kv-list"><span>마감 전 매칭 확률을 계산하는 중이에요</span><span>요청 마감 <b>${hhmm(deadline)}</b></span></div></div>`;
  }
  const pct = Math.round(d.probability * 100);
  const src = d.source === 'gemini' ? 'Gemini 분석' : d.source === 'groq' ? 'Groq 분석' : '통계 추정';
  return `
    <div class="ring-wrap">
      <div class="ring" role="img" aria-label="마감 전 매칭 확률 ${pct}%">${ringSvg(pct)}<div class="pct"><div>${pct}%<small>마감 전 확률</small></div></div></div>
      <div class="kv-list">
        <span>${d.expected_minutes == null ? '지금 조건으로는 찾기 어려워요' : `예상 대기 <b>약 ${Math.max(1, Math.round(d.expected_minutes))}분</b>`}</span>
        <span>요청 마감 <b>${hhmm(deadline)}</b></span>
      </div>
    </div>
    <div class="card info ai-card">
      <div class="head">${icon('sparkles', 'sm')}AI 분석 · ${src}</div>
      <p>${esc(d.reason)}</p>
      ${d.tip ? `<p class="small">${esc(d.tip)}</p>` : ''}
      ${d.error ? `<p class="tiny" style="opacity:.8">AI를 쓰지 못해 통계 추정치를 보여 드려요</p>` : ''}
    </div>`;
}

function ringSvg(pct) {
  const r = 46, c = 2 * Math.PI * r;
  return `<svg viewBox="0 0 112 112"><circle cx="56" cy="56" r="${r}" fill="none" stroke="#E8F2FF" stroke-width="12"/>
    <circle cx="56" cy="56" r="${r}" fill="none" stroke="#0B6FE8" stroke-width="12" stroke-linecap="round"
      stroke-dasharray="${(c * pct) / 100} ${c}"/></svg>`;
}

async function updatePrediction(r) {
  P.predAt = Date.now();
  try {
    const d = await api(`/api/requests/${r.id}/prediction`);
    P.pred = { rid: r.id, data: d };
    const box = $('#predict');
    if (box && P.view === 'matching') box.innerHTML = predictionHtml(d, r);
  } catch (e) { /* 다음 주기에 다시 */ }
}

/* =========================================================================
   3. 동승 제안
   ========================================================================= */
function stopsTable(g, { withTime = true } = {}) {
  return `<ol class="stops">${mergeStops(g.stops).map((m, i) => `
    <li class="${m.hasMe ? 'me' : ''} ${m.reached ? 'done' : ''}"><span class="num">${i + 1}</span>
      <span><span class="place">${esc(m.name)}</span><br><span class="what">${esc(m.text)}</span></span>
      ${withTime ? `<span class="time">${hhmm(m.eta)}</span>` : '<span></span>'}</li>`).join('')}</ol>`;
}
function myMember(a) { return a.group.members.find((m) => m.request_id === a.request.id); }
function lastDestName(g) { const s = g.stops[g.stops.length - 1]; return s?.name || '도착지'; }

/* 동승자끼리는 닉네임 · 성별 · 신뢰도만 보여 준다 */
function companionsHtml(g) {
  const others = g.members.filter((m) => !m.is_me);
  if (!others.length) return '';
  return `<div class="co-list">${others.map((m) => `<div class="co ${m.low_trust ? 'low' : ''}">
    ${trustGauge(m.trust, { size: 'sm' })}
    <div class="grow"><span class="who">${esc(m.nickname)}</span> <span class="small muted">${m.gender === 'F' ? '여성' : '남성'}${m.seats > 1 ? ` · ${m.seats}명` : ''}</span><br>
      <span class="small">${m.surcharge > 0 ? `신뢰도 회복 중 · 이 분이 ${Math.round(m.surcharge * 100)}% 더 내요`
        : m.low_trust ? `신뢰도 ${m.trust}점 · 회복 필요` : `신뢰도 ${m.trust}점 · ${trustBand(m.trust).label}`}</span></div>
    ${g.status === 'proposed' ? (m.accepted ? '<span class="badge ok">수락</span>' : '<span class="badge gray">대기</span>') : ''}</div>`).join('')}</div>
    ${g.status === 'proposed' ? '<p class="notice-line">신뢰도를 보고 거절할 수 있어요. 거절해도 내 신뢰도는 내려가지 않아요</p>' : ''}`;
}

function viewProposal(a) {
  const { request: r, group: g } = a;
  const me = myMember(a);
  const waiting = g.members.filter((m) => !m.accepted).length;
  const s = sheet(`
    <div class="between"><span class="badge solid">동승 제안</span>
      ${g.expires_at ? `<span class="small muted">응답 남은 시간 <b class="timer" data-countdown="${g.expires_at}" style="color:var(--text)">${mmss(secondsUntil(g.expires_at))}</b></span>` : ''}</div>
    <h1 class="title mt12">동승 제안이 도착했어요</h1>
    <p class="subtitle">${g.members.length}명이 한 택시로 ${esc(lastDestName(g))} 방향으로 가요</p>
    <h2 class="section-title">함께 타는 사람</h2>
    ${companionsHtml(g)}
    <h2 class="section-title">타고 내리는 순서</h2>
    ${stopsTable(g)}
    <div class="card info mt12">
      <div class="between"><span class="small">내 예상 몫</span><b class="money" style="font-size:22px">${won(me.share)}</b></div>
      <p class="small mt8">혼자 탈 때 ${won(me.solo_fare)}보다 <b>${won(me.saving)}</b> 아껴요</p>
      ${me.trust_adjust < 0 ? `<p class="small mt8">신뢰도 회복 중인 동승자가 더 내서 <b>${won(-me.trust_adjust)}</b> 덜 내요</p>` : ''}
      ${me.trust_adjust > 0 ? `<p class="small mt8">내 신뢰도가 낮아 <b>${won(me.trust_adjust)}</b>을 더 내요(그래도 혼자보다 싸요). 문제없이 타면 신뢰도가 회복돼요</p>` : ''}
    </div>
    <p class="notice-line">${icon('wallet', 'xs')}${me.accepted ? `예약금 ${won(me.hold).replace('원', 'P')}를 잡아 뒀어요. 운행이 끝나면 실제 몫만 결제돼요`
      : `수락하면 예상 몫만큼 예약금(${won(me.share).replace('원', 'P')})을 잡아 둬요. 출발 ${CONFIG.params.late_cancel_min}분 이내에 취소하면 신뢰도가 내려가고, 남은 동승자에게 보상돼요`}</p>
    <p class="notice-line">${icon('info', 'xs')}최종 금액은 미터 요금을 같이 탄 구간별로 나눠 정해요</p>`,
  me.accepted
    ? `<button class="btn secondary block" disabled>수락했어요 · ${waiting}명의 응답을 기다리는 중</button>`
    : `<button class="btn secondary" id="reject">거절</button><button class="btn primary" id="accept">수락하기</button>`,
  { split: !me.accepted, tall: true });
  $('#accept', s)?.addEventListener('click', () => act(() => api(`/api/groups/${g.id}/accept`, { method: 'POST', body: { request_id: r.id } }), '수락했어요'));
  $('#reject', s)?.addEventListener('click', () => act(() => api(`/api/groups/${g.id}/reject`, { method: 'POST', body: { request_id: r.id } }), '거절했어요. 같은 사람과는 다시 묶지 않아요'));
  drawGroupMode(P.map, g);
  P.map.fit(g.stops, { top: 70, bottom: 560 });
}

/* =========================================================================
   4. 탑승 전 안내 (신고 기능 안내 → 확인해야 탑승 처리 가능)
   ========================================================================= */
function vehicleCard(g) {
  if (!g.driver) {
    return `<div class="card"><div class="vehicle"><span class="car-ico">${icon('car')}</span>
      <div><b>기사님을 찾고 있어요</b><p class="small muted">배차되면 차량 번호와 기사님 정보가 여기 표시돼요</p></div></div></div>`;
  }
  const d = g.driver;
  return `<div class="card"><div class="vehicle"><span class="car-ico">${icon('car')}</span>
    <div><div class="plate">${esc(d.car_number)}</div><p class="small muted">${esc([d.car_model, d.car_color].filter(Boolean).join(' · '))} · ${esc(d.name)} 기사님</p></div></div></div>`;
}

function viewNotice(a) {
  const { request: r, group: g } = a;
  const others = g.members.filter((m) => !m.is_me).map((m) => m.nickname);
  const me = myMember(a);
  const pickup = g.stops[me.pickup_index];
  const s = sheet(`
    <span class="badge ok">${icon('check', 'xs')}${g.is_solo ? '단독 탑승 확정' : '동승 확정'}</span>
    <h1 class="title mt12">탑승 전에 확인해 주세요</h1>
    <p class="subtitle">${esc(pickup.name)}에서 ${hhmm(pickup.eta)} 탑승 예정${others.length ? ` · 함께 타는 사람: ${esc(others.join(', '))}` : ''}</p>
    <div class="mt12">${vehicleCard(g)}</div>
    <p class="small muted mt8">탈 때 차량 번호가 같은지 꼭 확인하세요.</p>
    <h2 class="section-title">문제가 생기면 이렇게 신고해요</h2>
    <div class="card">
      <p class="small">운행 중 화면 아래 <b style="color:var(--danger)">긴급신고</b> 버튼을 누르면 신고하기 화면이 열려요.</p>
      <ul class="list mt8 small">
        ${CONFIG.report_tabs.map((t) => `<li class="row" style="align-items:flex-start">${icon(t.kind === 'emergency' ? 'shield-alert' : t.categories[0].icon, 'sm')}
          <span><b>${esc(t.label)}</b> · ${t.kind === 'emergency' ? '112에 바로 전화하고, 운영팀에 위치와 차량 정보가 즉시 전달돼요'
            : esc(t.categories.slice(0, 2).map((c) => c.label).join(', ')) + ' 등'}</span></li>`).join('')}
      </ul>
    </div>
    <p class="notice-line">${icon('alert', 'xs')}허위 신고를 하면 이용이 제한될 수 있어요.</p>`,
  `<button class="btn primary block" id="ack">확인했어요</button>`, { tall: true });
  $('#ack', s).addEventListener('click', () => act(() => api(`/api/requests/${r.id}/notice-ack`, { method: 'POST' }), '안내를 확인했어요'));
  drawGroupMode(P.map, g, { car: g.driver });
  P.map.fit(g.driver ? [...g.stops, g.driver] : g.stops, { top: 70, bottom: 600 });
}

function arrivingSoon(g) {
  return g.arriving_at && (serverNow() - fromIso(g.arriving_at)) < 5 * 60 * 1000;
}

function viewBoarding(a) {
  const { request: r, group: g } = a;
  const me = myMember(a);
  const pickup = g.stops[me.pickup_index];
  const coming = g.status === 'riding';
  const booked = g.reservation_assigned;
  const companions = g.members.filter((m) => !m.is_me).map((m) => ({ request_id: m.request_id, label: m.nickname }));
  const s = sheet(`
    <span class="badge ${coming ? '' : booked ? 'ok' : 'gray'}">${coming ? `<span class="pulse"></span>배차 완료` : booked ? `${icon('check', 'xs')}예약 배차 완료` : '기사님 찾는 중'}</span>
    <h1 class="title mt12">${coming ? '택시가 오고 있어요' : booked ? `${md(g.departure)} ${hhmm(g.departure)}에 기사님이 출발해요` : '곧 기사님을 연결해 드릴게요'}</h1>
    <p class="subtitle">${esc(pickup.name)}에서 ${hhmm(pickup.eta)} 탑승 예정</p>
    ${arrivingSoon(g) ? `<div class="card info mt12 row">${icon('bell', 'sm')}<b>기사님이 곧 도착해요. 탑승 지점에서 기다려 주세요</b></div>` : ''}
    <div class="mt12">${vehicleCard(g)}</div>
    ${g.is_solo ? '' : `<h2 class="section-title">타고 내리는 순서</h2>${stopsTable(g)}`}`,
  `${companions.length ? '<button class="btn secondary" id="report">신고하기</button>' : ''}
   ${coming ? '' : '<button class="btn outline" id="cancel">요청 취소</button>'}`,
  { split: companions.length > 0 && !coming });
  $('#report', s)?.addEventListener('click', () => openReport({ root: $('#root'), groupId: g.id, subtitle: '탑승 전',
    kind: 'companion', category: 'no_show', targets: companions }));
  $('#cancel', s)?.addEventListener('click', async () => {
    if (!(await askConfirm('출발 30분 전부터는 취소하면 패널티가 쌓여요. 그래도 취소할까요?', { ok: '취소하기', cancel: '그대로 두기', danger: true }))) return;
    act(() => api(`/api/requests/${r.id}/cancel`, { method: 'POST' }), '요청을 취소했어요');
  });
  drawGroupMode(P.map, g, { car: g.driver });
  P.map.fit(g.driver ? [pickup, g.driver] : g.stops, { top: 70, bottom: 520 });
}

/* =========================================================================
   5. 운행 중
   ========================================================================= */
function viewRiding(a) {
  const { request: r, group: g } = a;
  const me = myMember(a);
  const myDrop = g.stops[me.dropoff_index];
  const fromM = g.progress >= 0 ? g.stops[g.progress].at_m : 0;
  const leftM = Math.max(0, myDrop.at_m - fromM);
  const speed = CONFIG.params.avg_speed_kmh * 1000 / 60;
  const leftMin = Math.max(1, Math.round(leftM / speed));
  const eta = new Date(serverNow().getTime() + leftMin * 60000);
  const dropped = r.status === 'dropped';
  const companions = g.members.filter((m) => !m.is_me).map((m) => ({ request_id: m.request_id, label: m.nickname }));
  setFloat(`<div class="float-card"><div><span class="small muted">${dropped ? '하차 완료' : '도착 예정'}</span>
      <div class="big money">${dropped ? hhmm(toIso(serverNow())) : `${pad(eta.getHours())}:${pad(eta.getMinutes())} · 약 ${leftMin}분`}</div></div>
    <span class="badge"><span class="pulse"></span>운행 중</span></div>`);
  const merged = mergeStops(g.stops);
  const nextIdx = merged.findIndex((m) => !m.reached);
  const s = sheet(`
    <h1 class="title">${dropped ? '내렸어요. 모든 동승자가 내리면 정산돼요' : `${esc(myDrop.name)}(으)로 가는 중`}</h1>
    <div class="mt12">${vehicleCard(g)}</div>
    <div class="progress" aria-label="정차 진행">${merged.map((m, i) => `<div class="p ${m.reached ? 'done' : ''} ${i === nextIdx ? 'next' : ''}"><i></i>${esc(m.name)}</div>`).join('')}</div>
    <p class="notice-line">${icon('info', 'xs')}긴급신고를 누르면 운영팀에 위치와 차량 정보가 바로 전달돼요</p>`,
  `<button class="btn outline" id="share">${icon('share', 'sm')}운행 정보 공유</button>
   <button class="btn danger-outline" id="sos">${icon('shield-alert', 'sm')}긴급신고</button>`, { split: false });
  $$('.sheet-actions > *', s).forEach((b) => { b.style.flex = '1'; });
  $('#share', s).addEventListener('click', () => shareTrip(g, myDrop, eta));
  $('#sos', s).addEventListener('click', () => openReport({ root: $('#root'), groupId: g.id,
    subtitle: `${myDrop.name}(으)로 가는 중 · ${g.driver?.car_number || '차량 정보 없음'}`, kind: 'emergency', targets: companions }));
  drawGroupMode(P.map, g, { car: g.driver });
  P.map.fit(g.driver ? [g.driver, myDrop] : g.stops, { top: 120, bottom: 420 });
}

async function shareTrip(g, drop, eta) {
  const text = `[모아타] ${g.driver?.car_number || ''} 택시로 ${drop.name}에 가는 중이에요. 도착 예정 ${pad(eta.getHours())}:${pad(eta.getMinutes())}`;
  try {
    if (navigator.share) await navigator.share({ title: '모아타 운행 정보', text });
    else { await navigator.clipboard.writeText(text); toast('운행 정보를 복사했어요'); }
  } catch (e) { /* 공유 취소 */ }
}

/* =========================================================================
   6. 정산
   ========================================================================= */
function viewSettlement(bundle, fresh = false) {
  const { request: r, group: g } = bundle;
  const set = g.settlement;
  const me = g.members.find((m) => m.request_id === r.id);
  const myShare = set.final[String(r.id)];
  const saving = me.solo_fare - myShare;
  const segs = set.segments.filter((x) => x.length_m > 0);
  const total = segs.reduce((acc, x) => acc + x.length_m, 0) || 1;
  const members = [...g.members].sort((x, y) => (y.is_me - x.is_me) || (y.dropoff_index - x.dropoff_index));
  const paid = !!r.paid_at;
  const s = sheet(`
    <div class="center"><div class="done-ico">${icon('check', 'lg')}</div>
      <h1 class="title">정산이 끝났어요</h1>
      <p class="subtitle">미터 요금 ${won(g.final_fare)} · ${g.members.length}명 동승 · ${kmShort(g.total_m)}</p>
      <p class="small muted mt12">내가 낼 금액</p>
      <div class="pay-amount money">${won(myShare)}</div>
      ${saving > 0 && !g.is_solo ? `<span class="badge ok mt8">${won(saving)} 절약</span>` : ''}
    </div>
    ${g.is_solo ? '' : `
    <h2 class="section-title">같이 탄 구간만 나눠요</h2>
    <div class="seg-bars">${segs.map((x) => {
      const n = x.riders.length;
      const mine = x.riders.includes(r.id);
      const who = n === 1 && mine ? '나 혼자' : `${n}명`;
      return `<div class="seg-bar"><span class="small muted">${kmShort(x.from_m).replace('km', '')}–${kmShort(x.to_m)}</span>
        <div class="track"><div class="fill n${Math.min(3, n)}" style="width:${Math.max(18, Math.round((x.length_m / total) * 100))}%">${who}</div></div>
        <span class="r money small" style="text-align:right">${mine ? won(x.shares[String(r.id)]) : '—'}</span></div>`;
    }).join('')}</div>
    <table class="table mt12"><thead><tr><th>승객</th><th>하차</th><th class="r">탄 거리</th><th class="r">낼 금액</th></tr></thead>
      <tbody>${members.map((m) => `<tr class="${m.is_me ? 'me' : ''}"><td>${esc(m.nickname)}</td><td>${esc(m.dest.name)}</td>
        <td class="r">${kmShort(m.ride_m)}</td><td class="r">${won(set.final[String(m.request_id)])}</td></tr>`).join('')}</tbody></table>`}
    ${set.notes?.length ? `<ul class="small muted mt12" style="padding-left:18px">${set.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}
    <div class="row mt12" style="justify-content:center">
      <button class="link-btn" id="weird">요금이 이상해요</button><span class="muted" aria-hidden="true">·</span>
      <button class="link-btn" id="dispute">이의 제기</button></div>`,
  `<div class="card info center small" style="width:100%">${icon('wallet', 'xs')} ${paid ? `포인트로 <b>${won(myShare).replace('원', 'P')}</b> 결제했어요. 남은 예약금은 돌려받았어요` : '포인트 결제를 처리하는 중이에요'}</div>
   <button class="btn primary block" id="again">${fresh ? '확인했어요' : '닫기'}</button>`, { stack: true, tall: true });
  const companions = g.members.filter((m) => !m.is_me).map((m) => ({ request_id: m.request_id, label: m.nickname }));
  const openFare = (category) => openReport({ root: $('#root'), groupId: g.id, subtitle: `정산 ${won(myShare)}`, kind: 'fare', category, targets: companions });
  $('#weird', s).addEventListener('click', () => openFare('fare_mismatch'));
  $('#dispute', s).addEventListener('click', () => openFare('payment'));
  $('#again', s).addEventListener('click', () => {
    P.viewing = null;
    if (r.id > P.dismissed) { P.dismissed = r.id; save('moata.dismissed', r.id); }
    P.sig = null;
    render(true);
  });
  drawGroupMode(P.map, g);
  P.map.fit(g.stops, { top: 70, bottom: 600 });
}

/* =========================================================================
   내 정보
   ========================================================================= */
const TX_KO = { grant: '가입 포인트', topup: '데모 충전', hold: '예약금 잡아 둠', release: '예약금 돌려받음', pay: '동승 요금 결제',
  comp_in: '동승자 취소·노쇼 보상', comp_out: '내 취소·노쇼로 보상' };

async function openProfile() {
  const u = P.st.user;
  let w = null;
  try { w = await api('/api/me/wallet'); } catch (e) { /* 없어도 연다 */ }
  const done = P.st.history.filter((h) => h.group?.status === 'completed');
  const ov = document.createElement('div');
  ov.className = 'overlay';
  const trust = w ? w.trust : u.trust;
  ov.innerHTML = `<div class="panel" role="dialog" aria-modal="true" aria-labelledby="pf-title">
    <div class="panel-head"><div><h2 class="title" id="pf-title">${esc(u.nickname)}</h2><p class="subtitle">${esc(u.login_id ? `아이디 ${u.login_id}` : '')}${u.email ? ` · ${esc(u.email)}` : ''}</p></div>
      <button class="close-btn" data-close aria-label="닫기">${icon('x')}</button></div>
    <div class="panel-body">
      <div class="card center">
        <p class="small muted">내 신뢰도</p>
        ${trustGauge(trust)}
        <p class="small mt8">${trust < CONFIG.params.trust_low
          ? `회복 중이에요. 합승할 때 ${Math.round(CONFIG.params.trust_low_surcharge * 100)}%를 더 내지만 혼자 타는 것보다는 싸요. 문제없이 타면 1회마다 +3점`
          : '문제없이 탈수록 올라가요(1회 +1, 주 최대 +5, 10회마다 보너스)'}</p>
        <p class="tiny muted mt8">출발 ${CONFIG.params.late_cancel_min}분 이내 취소 −10 · 노쇼 −20 · 사실로 확인된 신고 −15 · ${CONFIG.params.trust_low}점 미만이면 추가 부담</p>
      </div>
      <div class="card"><div class="between"><span class="muted small">모아타 포인트</span><b class="money" style="font-size:20px">${w ? w.points.toLocaleString('ko-KR') : '—'}P</b></div>
        ${w && w.held ? `<p class="small muted mt8">예약금으로 잡아 둔 ${w.held.toLocaleString('ko-KR')}P는 운행이 끝나면 정산돼요</p>` : ''}
        ${CONFIG.demo_tools ? '<button class="btn sm outline mt8" id="topup">데모 충전 +100,000P</button>' : ''}
        ${w && w.tx.length ? `<details class="mt8"><summary class="small">포인트 내역</summary>${w.tx.map((t) => `<div class="wallet-row"><span>${esc(TX_KO[t.kind] || t.kind)}<br><span class="tiny muted">${esc(t.created_at.slice(5, 16).replace('T', ' '))}</span></span>
          <span class="money ${t.amount >= 0 ? 'plus' : 'minus'}">${t.amount >= 0 ? '+' : ''}${t.amount.toLocaleString('ko-KR')}P</span></div>`).join('')}</details>` : ''}
        ${w && w.trust_events.length ? `<details class="mt8"><summary class="small">신뢰도 내역</summary>${w.trust_events.map((t) => `<div class="wallet-row"><span>${esc(t.reason || t.kind)}<br><span class="tiny muted">${esc(t.created_at.slice(5, 16).replace('T', ' '))}</span></span>
          <span class="${t.delta >= 0 ? 'plus' : 'minus'}">${t.delta > 0 ? '+' : ''}${t.delta} → ${t.trust_after}</span></div>`).join('')}</details>` : ''}
      </div>
      <div class="card"><div class="between"><span class="muted small">성별</span><b>${u.gender === 'F' ? '여성' : '남성'}</b></div>
        <p class="tiny muted mt8">같은 성별끼리만 매칭돼요. 동승자에게는 닉네임·성별·신뢰도만 보여요.</p></div>
      <h3 class="section-title">지난 이용</h3>
      ${done.length ? `<ul class="list">${done.map((h) => {
        const me = h.group.members.find((m) => m.request_id === h.request.id);
        return `<li><button class="item" data-h="${h.request.id}">${icon('receipt', 'sm')}<span class="grow">
          <b style="font-weight:500">${esc(h.request.origin.name)} → ${esc(h.request.dest.name)}</b><br>
          <span class="small muted">${h.group.members.length}명 동승 · ${won(me.saving)} 절약${h.request.paid_at ? ' · 포인트 결제' : ''}</span></span>
          <span class="money">${won(me.share)}</span></button></li>`;
      }).join('')}</ul>` : '<p class="empty">아직 이용 기록이 없어요</p>'}
      <div class="row mt16" style="flex-wrap:wrap">
        <button class="btn sm outline" id="install">${icon('download', 'xs')}앱으로 설치</button>
        <button class="btn sm outline" id="logout">${icon('logout', 'xs')}로그아웃</button>
      </div>
    </div></div>`;
  $('#root').appendChild(ov);
  const close = () => ov.remove();
  $('[data-close]', ov).addEventListener('click', close);
  $('#logout', ov).addEventListener('click', logout);
  $('#install', ov).addEventListener('click', promptInstall);
  $('#topup', ov)?.addEventListener('click', async () => {
    try { await api('/api/me/points/topup', { method: 'POST' }); toast('100,000P를 충전했어요'); close(); openProfile(); }
    catch (e) { toast(e.message, true); }
  });
  $$('[data-h]', ov).forEach((b) => b.addEventListener('click', () => {
    P.viewing = P.st.history.find((h) => h.request.id === Number(b.dataset.h));
    close();
    P.sig = null;
    render(true);
  }));
}

boot().catch((e) => {
  if (e.status === 401) return;
  sheet(`<h1 class="title">앱을 시작하지 못했어요</h1><p class="subtitle">${esc(e.message)}. 서버가 켜져 있는지 확인하세요.</p>`);
});
