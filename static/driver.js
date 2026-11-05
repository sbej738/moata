'use strict';
/* =========================================================================
   모아타 기사님 앱: 배차 요청 → 운행(정차 지점마다) → 요금 입력 → 정산 요청
   운전 중 확인용이라 글씨·버튼을 크게(버튼 62px) 그린다.
   ========================================================================= */
const D_POLL_MS = 2500;
const D = { map: null, s: null, sig: null, fare: null, toll: 0, preview: null, seenDone: Number(localStorage.getItem('moata.driverSeen') || 0), watch: null };

async function bootDriver() {
  await loadConfig();
  await requireLogin();
  D.map = await createMap($('#map'));
  window.addEventListener('resize', () => D.map.resize());
  await renderDriver(true);
  setInterval(() => { if (!document.hidden) renderDriver(false).catch(() => {}); }, D_POLL_MS);
  setInterval(dTick, 1000);
}

function dSheet(body, actions = '', { split = false, stack = false, tall = false } = {}) {
  const s = $('#sheet');
  s.className = `sheet${tall ? ' tall' : ''}`;
  s.innerHTML = `${handleHtml()}<div class="sheet-body">${body}</div>
    ${actions ? `<div class="sheet-actions${split ? ' split' : ''}${stack ? ' stack' : ''}">${actions}</div>` : ''}`;
  wireSheet(s, D.view);
  return s;
}

async function dAct(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); D.sig = null; await renderDriver(true); return r; }
  catch (e) { toast(e.message, true); D.sig = null; await renderDriver(true); return null; }
}

function drawDriverTop(d) {
  $('#topbar').innerHTML = `
    <span class="pill">${logoMark()}<span>모아타 기사님</span></span>
    <div class="row">
      <button class="switch" role="switch" aria-checked="${d.available}" id="avail">운행 가능<span class="track" aria-hidden="true"></span></button>
      <button class="round-btn" id="menu" aria-label="내 정보">${icon('user')}</button>
    </div>`;
  $('#avail').addEventListener('click', toggleAvailable);
  $('#menu').addEventListener('click', () => openDriverMenu(d));
}

async function toggleAvailable(e) {
  if (D.toggling) return;
  D.toggling = true;
  const on = !D.s.driver.available;
  const sw = $('#avail');
  if (sw) sw.setAttribute('aria-checked', String(on));   // 누르는 즉시 바뀐 모습을 보여 준다
  try {
    // 위치를 기다리지 않고 바로 바꾼다(예전에는 위치 권한 창에서 멈춰 스위치가 안 눌리는 것처럼 보였다)
    await dAct(() => api('/api/driver/availability', { method: 'POST', body: { available: on } }),
      on ? '운행 가능으로 바꿨어요. 배차 요청을 기다려요' : '운행을 쉬어요');
  } finally { D.toggling = false; }
  if (on) {
    locateMe(8000)
      .then((p) => api('/api/driver/location', { method: 'POST', body: { lat: p.lat, lng: p.lng } }))
      .catch(() => { /* 위치가 없으면 마지막 정차 지점을 쓴다 */ });
  }
  watchLocation(on);
}

/* 휴대폰 위치(HTTPS에서만): 15초마다 서버에 알린다 */
function watchLocation(on) {
  if (!navigator.geolocation || !window.isSecureContext) return;
  if (on && D.watch == null) {
    let last = 0;
    D.watch = navigator.geolocation.watchPosition((p) => {
      if (Date.now() - last < 15000) return;
      last = Date.now();
      api('/api/driver/location', { method: 'POST', body: { lat: p.coords.latitude, lng: p.coords.longitude } }).catch(() => {});
    }, () => {}, { enableHighAccuracy: true, maximumAge: 10000 });
  } else if (!on && D.watch != null) {
    navigator.geolocation.clearWatch(D.watch);
    D.watch = null;
  }
}

async function renderDriver(force) {
  const s = await api('/api/driver/state');
  D.s = s;
  syncClock(s.now);
  const view = s.trip ? (s.trip.ready_to_complete ? 'fare' : 'trip') : s.offer ? 'offer'
    : (s.last_completed && s.last_completed.id > D.seenDone ? 'done' : 'idle');
  const sig = JSON.stringify([view, s.driver.available, Math.round(s.driver.lat * 100), Math.round(s.driver.lng * 100), s.offer?.id, s.offer?.offer_reason, s.offer?.offer_expires_at,
    s.trip?.id, s.trip?.progress, s.trip?.members.map((m) => [m.notice_ack, m.status]), s.trip?.wait_started_at, s.last_completed?.id, s.today,
    s.reservations.map((r) => [r.id, r.startable, r.members.map((m) => m.notice_ack)])]);
  if (!force && sig === D.sig) return;
  D.sig = sig;
  D.view = view;
  drawDriverTop(s.driver);
  $('#float').innerHTML = '';
  if (view === 'offer') return viewOffer(s.offer, s.driver);
  if (view === 'trip') return viewTrip(s.trip, s.driver);
  if (view === 'fare') return viewFare(s.trip);
  if (view === 'done') return viewDone(s.last_completed);
  return viewIdle(s);
}

function dTick() {
  $$('[data-countdown]').forEach((el) => {
    const left = secondsUntil(el.dataset.countdown);
    el.textContent = mmss(left);
    if (left === 0 && !el.dataset.done) { el.dataset.done = '1'; setTimeout(() => { D.sig = null; renderDriver(true); }, 1200); }
  });
}

/* ---------- 대기 ---------- */
function viewIdle(s) {
  const d = s.driver;
  dSheet(`
    <span class="badge ${d.available ? '' : 'gray'}">${d.available ? '<span class="pulse"></span>배차 대기 중' : '쉬는 중'}</span>
    <h1 class="title mt12">${d.available ? '배차 요청을 기다리고 있어요' : '운행 가능을 켜면 배차 요청을 받아요'}</h1>
    <p class="subtitle">${esc(d.car_number)} · ${esc([d.car_model, d.car_color].filter(Boolean).join(' · '))}</p>
    <div class="summary3"><div class="cond"><span class="k">오늘 운행</span><span class="v">${s.today.trips}건</span></div>
      <div class="cond"><span class="k">오늘 미터 요금</span><span class="v money">${won(s.today.fare)}</span></div>
      <div class="cond"><span class="k">상태</span><span class="v">${d.available ? '대기' : '쉼'}</span></div></div>
    ${farFromRegionHtml(d)}
    ${reservationsHtml(s.reservations)}
    <p class="notice-line">${icon('sparkles', 'xs')}가까운 동승 그룹을 수리 최적화로 배정하고, AI가 추천 이유를 알려 드려요</p>`,
  `<button class="btn ${d.available ? 'secondary' : 'primary'} block" id="toggle">${d.available ? '운행 쉬기' : '운행 시작하기'}</button>`);
  $('#toggle').addEventListener('click', toggleAvailable);
  $('#move-demo')?.addEventListener('click', async () => {
    watchLocation(false);               // 실제 GPS가 다시 덮어쓰지 않게 멈춘다
    const p = CONFIG.demo_places.find((x) => x.name.includes('버스터미널')) || CONFIG.demo_center;
    await dAct(() => api('/api/driver/location', { method: 'POST', body: { lat: p.lat, lng: p.lng } }), '시연용으로 위치를 삼척 터미널로 옮겼어요');
  });
  $$('[data-start]').forEach((b) => b.addEventListener('click', async () => {
    const r = s.reservations.find((x) => x.id === Number(b.dataset.start));
    if (!r.startable && !(await askConfirm(`${md(r.departure)} ${hhmm(r.departure)} 출발 예약이에요. 지금 운행을 시작할까요?`, { ok: '운행 시작' }))) return;
    dAct(() => api(`/api/driver/groups/${r.id}/start`, { method: 'POST' }), '예약 운행을 시작했어요. 첫 탑승 지점으로 가 주세요');
  }));
  D.map.clear();
  const me = { lat: d.lat, lng: d.lng };
  D.map.pin(me, pinHtml('car', '', '내 위치', d.available ? '' : 'off'));
  D.map.center(me, 14);
}

/* 실제 GPS가 서비스 지역(삼척)에서 멀면 배차를 받을 수 없다는 걸 알려 준다 */
function farFromRegionHtml(d) {
  const km_ = distM(CONFIG.demo_center, { lat: d.lat, lng: d.lng }) / 1000;
  if (km_ < 30) return '';
  return `<div class="card warn mt12 small">${icon('pin', 'xs')} 지금 위치가 ${esc(CONFIG.region)}에서 약 ${Math.round(km_)}km 떨어져 있어 배차를 받을 수 없어요.
    ${CONFIG.demo_tools ? `<br><button class="btn sm outline mt8" id="move-demo">시연용: 삼척 터미널로 위치 옮기기</button>` : ''}</div>`;
}

function reservationsHtml(list) {
  if (!list.length) return '';
  return `<h2 class="section-title">받아 둔 예약 ${list.length}건</h2>
    <div class="pax">${list.map((r) => `<div class="card ${r.startable ? 'focus' : ''}">${icon('calendar')}
      <div class="grow"><b>${md(r.departure)} ${hhmm(r.departure)} 출발</b><br>
        <span class="small">${esc(r.stops[0].name)} → ${esc(r.stops[r.stops.length - 1].name)} · ${r.members.length}명 · ${won(r.est_fare)}</span></div>
      <button class="btn sm ${r.startable ? 'primary' : 'outline'}" data-start="${r.id}">운행 시작</button></div>`).join('')}</div>`;
}

/* ---------- 1. 배차 요청 ---------- */
function viewOffer(g, d) {
  const first = g.stops[0], last = g.stops[g.stops.length - 1];
  const merged = mergeStops(g.stops);
  const src = { gemini: 'Gemini', groq: 'Groq' }[g.offer_source];
  const reserve = fromIso(g.departure) > new Date(serverNow().getTime() + CONFIG.params.dispatch_lead_min * 60000);
  const s = dSheet(`
    <div class="between"><span class="badge solid">${reserve ? `${icon('calendar', 'xs')}예약 배차 · ${md(g.departure)} ${hhmm(g.departure)} 출발` : '새 동승 배차'}</span>
      <span class="small muted">남은 시간 <b class="timer" style="color:var(--text);font-size:18px" data-countdown="${g.offer_expires_at}">${mmss(secondsUntil(g.offer_expires_at))}</b></span></div>
    <h1 class="title mt12">${esc(first.name)} → ${esc(last.name)}</h1>
    <div class="summary3">
      <div class="cond"><span class="k">승객</span><span class="v">${g.members.reduce((n, m) => n + m.seats, 0)}명</span></div>
      <div class="cond"><span class="k">총 거리</span><span class="v">${kmShort(g.total_m)}</span></div>
      <div class="cond"><span class="k">예상 요금</span><span class="v money">${won(g.est_fare)}</span></div>
    </div>
    <h2 class="section-title">정차 지점</h2>
    <ol class="stops">${merged.map((m, i) => `<li><span class="num">${i + 1}</span>
      <span><span class="place">${esc(m.name)}</span><br><span class="what">${m.stops.length}명 ${m.kind === 'pickup' ? '탑승' : '하차'}</span></span>
      <span class="time">${hhmm(m.eta)}</span></li>`).join('')}</ol>
    <div class="card info ai-card"><div class="head">${icon('sparkles', 'sm')}${src ? `AI 추천 · ${src}` : '추천 이유'}</div>
      <p>${esc(g.offer_reason)}</p></div>`,
  `<button class="btn secondary" id="decline">거절</button><button class="btn primary" id="accept">배차 수락</button>`, { split: true, tall: true });
  $('#accept', s).addEventListener('click', () => dAct(() => api(`/api/driver/offers/${g.id}/accept`, { method: 'POST' }),
    reserve ? '예약 배차를 받았어요. 출발 때 예약 목록에서 운행을 시작하세요' : '배차를 수락했어요. 첫 탑승 지점으로 가 주세요'));
  $('#decline', s).addEventListener('click', () => dAct(() => api(`/api/driver/offers/${g.id}/decline`, { method: 'POST' }), '배차를 거절했어요'));
  drawGroupMode(D.map, g, { car: { lat: d.lat, lng: d.lng } });
  D.map.fit([...g.stops, { lat: d.lat, lng: d.lng }], { top: 80, bottom: 600 });
}

/* ---------- 2. 운행 ---------- */
function viewTrip(g, d) {
  const merged = mergeStops(g.stops);
  const nextStop = g.stops[g.progress + 1];
  const nextM = merged.find((m) => m.indexes.includes(nextStop.index));
  const nextNo = merged.indexOf(nextM) + 1;
  const blocked = nextStop.kind === 'pickup' && !nextStop.notice_ack;
  $('#float').innerHTML = `<div class="float-card blue next-card" aria-live="polite">
    <div class="between"><span class="k">다음 정차 ${nextNo} / ${merged.length}</span><button class="link-btn" id="sos" style="color:#fff">${icon('shield-alert', 'sm')}신고</button></div>
    <div class="place">${esc(nextStop.name)}</div><div>${esc(nextStop.label)} ${nextStop.kind === 'pickup' ? '탑승' : '하차'}</div></div>`;
  const pax = [...g.members].sort((a, b) => a.pickup_index - b.pickup_index);
  const s = dSheet(`
    <h2 class="section-title" style="margin-top:4px">탑승 승객 ${pax.length}명</h2>
    <div class="pax">${pax.map((m) => {
      const focus = m.request_id === nextStop.request_id;
      const onboard = m.status === 'riding';
      const gone = m.status === 'dropped' || m.status === 'done';
      return `<div class="card ${focus ? 'focus' : ''}">${icon(gone ? 'check-circle' : 'user')}
        <div class="grow"><b>${esc(m.label)}</b> <span class="small muted">${gone ? '하차 완료' : onboard ? '탑승 중' : '탑승 전'}</span><br>
        <span class="small">${onboard || gone ? '하차' : '탑승'}: ${esc(onboard || gone ? m.dest.name : m.origin.name)}</span></div>
        ${!m.notice_ack && !onboard && !gone ? `<span class="badge warn">안내 미확인</span>` : ''}</div>`;
    }).join('')}</div>
    ${blocked ? `<div class="card warn mt12 small">${icon('info', 'xs')} ${esc(nextStop.label)} 승객이 아직 탑승 전 안내를 확인하지 않았어요. 승객 앱에서 확인하면 탑승 처리할 수 있어요.</div>` : ''}
    ${nextStop.kind === 'pickup' ? noShowHtml(g, nextStop) : ''}
    <div class="center"><button class="link-btn" id="arriving">${icon('bell', 'xs')} 승객에게 곧 도착 알림 보내기</button></div>`,
  `<button class="btn primary block" id="advance" ${blocked ? 'disabled' : ''}>${esc(nextStop.name)} 도착</button>`, { tall: false });
  $('#advance', s).addEventListener('click', () => dAct(() => api(`/api/driver/groups/${g.id}/advance`, { method: 'POST' })));
  $('#arrive', s)?.addEventListener('click', () => dAct(() => api(`/api/driver/groups/${g.id}/arrive`, { method: 'POST' }),
    `도착을 알렸어요. ${g.no_show_wait_min ?? CONFIG.params.no_show_wait_min}분이 지나도 안 오면 노쇼로 처리할 수 있어요`));
  $('#noshow', s)?.addEventListener('click', async () => {
    if (!(await askConfirm(`${nextStop.label} 승객을 노쇼로 처리할까요? 이 승객을 빼고 출발하고, 신뢰도가 내려가요.`, { ok: '노쇼 처리', danger: true }))) return;
    dAct(() => api(`/api/driver/groups/${g.id}/no-show`, { method: 'POST' }), '노쇼로 처리했어요. 남은 승객과 계속 운행해요');
  });
  $('#arriving', s).addEventListener('click', async () => {
    try { await api(`/api/driver/groups/${g.id}/arriving`, { method: 'POST' }); toast('승객에게 곧 도착한다고 알렸어요'); }
    catch (e) { toast(e.message, true); }
  });
  $('#sos').addEventListener('click', () => openReport({ root: $('#root'), groupId: g.id, subtitle: `운행 중 · ${d.car_number}`, kind: 'emergency', kinds: ['emergency'] }));
  drawGroupMode(D.map, g, { car: { lat: d.lat, lng: d.lng } });
  D.map.fit([{ lat: d.lat, lng: d.lng }, nextStop], { top: 170, bottom: 430 });
}

/* 노쇼: 탑승 지점에 도착했다고 알리고 NO_SHOW_WAIT_MIN분이 지나야 처리할 수 있다 */
function noShowHtml(g, stop) {
  const waitMin = g.no_show_wait_min ?? CONFIG.params.no_show_wait_min;   // 서버가 보낸 값을 먼저 쓴다
  if (g.wait_request_id !== stop.request_id || !g.wait_started_at) {
    return `<div class="row mt12"><button class="btn sm outline" id="arrive">${icon('pin', 'xs')}탑승 지점 도착(대기 시작)</button>
      <span class="small muted">도착 알림 뒤 ${waitMin}분이 지나면 노쇼 처리 가능</span></div>`;
  }
  const at = new Date(fromIso(g.wait_started_at).getTime() + waitMin * 60000);
  const openAt = `${toIso(at)}:${pad(at.getSeconds())}`;
  const left = secondsUntil(openAt);
  return `<div class="card mt12 between"><span class="small">${esc(stop.label)} 승객을 기다리는 중 · ${hhmm(g.wait_started_at)} 도착</span>
    ${left > 0 ? `<span class="small muted">노쇼 처리까지 <b class="timer" data-countdown="${openAt}">${mmss(left)}</b></span>`
      : '<button class="btn sm danger-outline" id="noshow">노쇼 처리</button>'}</div>`;
}

/* ---------- 3. 요금 입력 ---------- */
function viewFare(g) {
  if (D.fare == null || D.fareGroup !== g.id) { D.fare = g.est_fare; D.toll = 0; D.fareGroup = g.id; D.preview = null; }
  const s = dSheet(`
    <span class="badge gray">운행 종료</span>
    <h1 class="title mt12">최종 요금을 입력해 주세요</h1>
    <p class="subtitle">${esc(g.stops[0].name)} → ${esc(g.stops[g.stops.length - 1].name)} · 승객 ${g.members.length}명 · 정차 ${mergeStops(g.stops).length}곳</p>
    <label class="field"><span>미터 요금</span><input class="input big money" id="fare" type="number" inputmode="numeric" min="100" step="100" value="${D.fare}"></label>
    <label class="field"><span>통행료</span><input class="input money" id="toll" type="number" inputmode="numeric" min="0" step="100" value="${D.toll}"></label>
    <h2 class="section-title">승객별 정산 미리보기</h2>
    <div class="card preview" id="preview"><p class="small muted">계산하는 중이에요</p></div>
    <p class="notice-line">${icon('info', 'xs')}예상 요금(${won(g.est_fare)})과 30% 이상 다르면 승객에게 영수증 확인을 요청해요</p>`,
  `<button class="btn outline" id="receipt" aria-label="영수증 사진 첨부">${icon('camera', 'sm')}영수증</button>
   <button class="btn primary" id="send">정산 요청 보내기</button>`, { split: true, tall: true });
  let timer;
  const refresh = () => { clearTimeout(timer); timer = setTimeout(() => loadPreview(g), 250); };
  $('#fare', s).addEventListener('input', (e) => { D.fare = Number(e.target.value) || 0; refresh(); });
  $('#toll', s).addEventListener('input', (e) => { D.toll = Number(e.target.value) || 0; refresh(); });
  $('#receipt', s).addEventListener('click', () => toast('영수증 사진 첨부는 다음 버전에서 열려요'));
  $('#send', s).addEventListener('click', () => {
    if (!(D.fare > 0)) { toast('미터 요금을 입력하세요', true); return; }
    dAct(() => api(`/api/driver/groups/${g.id}/complete`, { method: 'POST', body: { final_fare: D.fare, toll: D.toll } }),
      '정산 요청을 보냈어요. 승객 화면이 정산으로 바뀌어요').then(() => { D.fare = null; });
  });
  loadPreview(g);
  drawGroupMode(D.map, g);
  D.map.fit(g.stops, { top: 70, bottom: 620 });
}

async function loadPreview(g) {
  const box = $('#preview');
  if (!box) return;
  if (!(D.fare > 0)) { box.innerHTML = '<p class="small muted">미터 요금을 입력하세요</p>'; return; }
  try {
    const p = await api(`/api/driver/groups/${g.id}/preview`, { method: 'POST', body: { final_fare: D.fare, toll: D.toll } });
    const fin = p.settlement.final;
    const rows = [...g.members].sort((a, b) => a.pickup_index - b.pickup_index)
      .map((m) => `<div class="between"><span>${esc(m.label)}</span><span class="money">${won(fin[String(m.request_id)])}</span></div>`).join('');
    const sum = Object.values(fin).reduce((a, b) => a + b, 0);
    box.innerHTML = `${rows}<div class="between"><span>합계</span><span class="money">${won(sum)}</span></div>
      ${Math.abs(p.settlement.deviation) >= 0.3 ? `<p class="small mt8" style="color:var(--warn)">예상 요금과 ${Math.round(p.settlement.deviation * 100)}% 달라요. 승객에게 영수증 확인을 요청해요</p>` : ''}`;
  } catch (e) { box.innerHTML = `<p class="small" style="color:var(--danger)">${esc(e.message)}</p>`; }
}

/* ---------- 운행 완료 ---------- */
function viewDone(g) {
  const s = dSheet(`
    <div class="center"><div class="done-ico">${icon('check', 'lg')}</div>
      <h1 class="title">정산 요청을 보냈어요</h1>
      <p class="subtitle">미터 요금 ${won(g.final_fare)} · 승객 ${g.members.length}명 · ${kmShort(g.total_m)}</p></div>
    <div class="card preview mt12">${[...g.members].sort((a, b) => a.pickup_index - b.pickup_index)
      .map((m) => `<div class="between"><span>${esc(m.label)}</span><span class="money">${won(g.settlement.final[String(m.request_id)])}</span></div>`).join('')}</div>`,
  `<button class="btn primary block" id="ok">다음 배차 기다리기</button>`);
  $('#ok', s).addEventListener('click', () => {
    D.seenDone = g.id;
    try { localStorage.setItem('moata.driverSeen', String(g.id)); } catch (e) { /* 무시 */ }
    D.sig = null;
    renderDriver(true);
  });
  drawGroupMode(D.map, g);
  D.map.fit(g.stops, { top: 70, bottom: 480 });
}

function openDriverMenu(d) {
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = `<div class="panel fit" role="dialog" aria-modal="true" aria-labelledby="dm-title">
    <div class="panel-head"><div><h2 class="title" id="dm-title">${esc(d.name)} 기사님</h2><p class="subtitle">아이디 ${esc(d.login_id || '')}</p></div>
      <button class="close-btn" data-close aria-label="닫기">${icon('x')}</button></div>
    <div class="panel-body">
      <div class="card"><div class="vehicle"><span class="car-ico">${icon('car')}</span><div><div class="plate">${esc(d.car_number)}</div>
        <p class="small muted">${esc([d.car_model, d.car_color].filter(Boolean).join(' · '))}</p></div></div></div>
      <div class="row mt16"><button class="btn sm outline" id="install">${icon('download', 'xs')}앱으로 설치</button>
        <button class="btn sm outline" id="logout">${icon('logout', 'xs')}로그아웃</button></div>
    </div></div>`;
  $('#root').appendChild(ov);
  $('[data-close]', ov).addEventListener('click', () => ov.remove());
  $('#logout', ov).addEventListener('click', logout);
  $('#install', ov).addEventListener('click', promptInstall);
}

bootDriver().catch((e) => {
  if (e.status === 401) return;
  dSheet(`<h1 class="title">앱을 시작하지 못했어요</h1><p class="subtitle">${esc(e.message)}</p>`);
});
