'use strict';
/* =========================================================================
   모아타 운영 페이지: 24시간 모니터링
   - 맨 위: 긴급 신고 · AI 판단 이상 경보(높음은 빨간색 + 소리)
   - 지도, 지표, 시간대별 그래프, 진행 중 그룹(안내 확인 현황)
   - AI 자동 매칭 스위치, 수동 매칭, AI 상태·요약·결정 로그, 기사님, 데모 도구
   ========================================================================= */
const A_POLL_MS = 3000;
const A = { map: null, ov: null, seenHigh: new Set(JSON.parse(sessionStorage.getItem('moata.seenHigh') || '[]')), sound: false, sig: {} };

const KIND_KO = { emergency: '긴급·안전', driving: '운행', fare: '요금', companion: '동승자' };
const STATUS_KO = { waiting: '매칭 대기', proposed: '수락 대기', confirmed: '확정', riding: '운행 중', dropped: '하차', received: '접수', in_progress: '처리 중', resolved: '처리 완료' };
const METHOD_KO = { ai: 'AI 판단', 'cp-sat': '최적화(CP-SAT)', greedy: '탐욕 매칭', 'greedy(fallback)': '탐욕(대체)', solo: '단독 전환', assign: '배차', summary: '요약' };
const t = (stamp) => (stamp || '').slice(11, 16);

async function bootAdmin() {
  await loadConfig();
  const me = await requireLogin();
  A.me = me;
  A.map = await createMap($('#map'));
  $('#map-full').addEventListener('click', () => toggleMapFull());
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#ops-map').classList.contains('full')) toggleMapFull(false); });
  await renderAdmin();
  setInterval(() => { if (!document.hidden) renderAdmin().catch(() => {}); }, A_POLL_MS);
}

function changed(key, value) {
  const s = JSON.stringify(value);
  if (A.sig[key] === s) return false;
  A.sig[key] = s;
  return true;
}

async function renderAdmin() {
  const ov = await api('/api/admin/overview');
  A.ov = ov;
  drawTop(ov);
  if (changed('alerts', [ov.alerts, ov.recent_acked_alerts])) drawAlerts(ov);
  if (changed('reports', ov.reports)) drawReports(ov);
  if (changed('kpis', [ov.counts, ov.metrics])) { drawKpis(ov); drawChart(ov); }
  if (changed('live', [A.filter, A.sel, ov.groups.map((g) => [g.id, g.status, g.progress, g.driver, g.offer_pending, g.members.map((m) => [m.accepted, m.notice_ack, m.status, m.paid])]),
    ov.completed.map((g) => g.id), ov.requests.map((r) => [r.id, r.status]), ov.reports.length])) drawLive(ov);
  if (changed('control', [ov.auto_match, ov.last_match?.at])) drawControl(ov);
  if (changed('ai', [ov.ai_status, ov.ai_summary?.id, ov.ai_enabled, ov.ai_orders])) drawAi(ov);
  if (changed('decisions', ov.ai_decisions.map((d) => d.id))) drawDecisions(ov);
  if (changed('drivers', ov.drivers.map((d) => [d.user_id, d.state, d.lat, d.lng]))) drawDrivers(ov);
  if (!A.sig.demo) { A.sig.demo = '1'; drawDemo(); }
  if (changed('map', [A.sel, ov.requests.map((r) => [r.id, r.status]), ov.groups.map((g) => [g.id, g.status, g.progress, g.driver?.lat, g.driver?.lng]), ov.drivers.map((d) => [d.lat, d.lng, d.state])])) drawMap(ov);
  if (A.drv) drawDriverCard(ov);   // 열려 있는 기사님 정보는 3초마다 최신으로
  checkNewHigh(ov);
}

/* ---------- 상단 ---------- */
function drawTop(ov) {
  const open = ov.alerts.filter((a) => a.severity === 'high').length;
  document.title = open ? `(${open}) 긴급 · 모아타 운영` : '모아타 운영';
  const top = $('#top');
  const html = `
    <span class="brand">${logoMark()}모아타 운영</span>
    <span class="badge gray">${icon('clock', 'xs')}${esc(ov.now.replace('T', ' '))}</span>
    <span class="badge ${ov.ai_enabled ? '' : 'gray'}">${icon('sparkles', 'xs')}${ov.ai_enabled ? `AI ${ov.ai_status.map((p) => p.provider).join(' → ')}` : 'AI 키 없음(최적화만)'}</span>
    <span class="spacer"></span>
    <button class="switch" role="switch" aria-checked="${ov.auto_match}" id="auto">AI 자동 매칭<span class="track" aria-hidden="true"></span></button>
    <button class="btn sm outline" id="sound" aria-pressed="${A.sound}">${icon('volume', 'xs')}${A.sound ? '소리 켜짐' : '소리 켜기'}</button>
    <button class="btn sm outline" id="logout">${icon('logout', 'xs')}로그아웃</button>`;
  if (top.dataset.sig === html) return;
  top.dataset.sig = html;
  top.innerHTML = html;
  $('#auto').addEventListener('click', async () => {
    try {
      const r = await api('/api/admin/settings', { method: 'POST', body: { auto_match: !A.ov.auto_match } });
      toast(r.auto_match ? 'AI 자동 매칭을 켰어요' : 'AI 자동 매칭을 껐어요. 이제 수동으로만 매칭해요');
      A.sig = {}; await renderAdmin();
    } catch (e) { toast(e.message, true); }
  });
  $('#sound').addEventListener('click', () => { A.sound = !A.sound; if (A.sound) beep(); top.dataset.sig = ''; drawTop(A.ov); });
  $('#logout').addEventListener('click', logout);
}

/* ---------- 경보 ---------- */
function drawAlerts(ov) {
  const box = $('#alerts');
  box.innerHTML = `<h2>${icon('bell', 'sm')}경보<span class="badge ${ov.alerts.some((a) => a.severity === 'high') ? 'danger' : 'gray'} count">${ov.alerts.length}건</span></h2>
    ${ov.alerts.length ? ov.alerts.map((a) => `<div class="alert-item ${a.severity}" id="al-${a.id}">
      <div class="between"><span class="t">${esc(a.title)}</span>${a.count > 1 ? `<span class="badge gray">${a.count}회</span>` : ''}</div>
      <div class="d">${esc(a.detail)}</div>
      <div class="between mt8"><span class="meta">${esc({ high: '높음', medium: '중간', low: '낮음', info: '정보' }[a.severity])} · ${t(a.updated_at)}${a.group_id ? ` · 그룹 #${a.group_id}` : ''}</span>
        <span class="row">${a.kind.startsWith('ai_') ? `<button class="btn sm outline" data-rematch>수동 매칭</button>` : ''}<button class="btn sm primary" data-ack="${a.id}">확인</button></span></div></div>`).join('')
      : '<p class="empty">열린 경보가 없어요</p>'}
    ${ov.recent_acked_alerts.length ? `<details class="mt12"><summary class="small muted">최근 확인한 경보</summary><ul class="log">${ov.recent_acked_alerts.map((a) =>
      `<li><b>${esc(a.title)}</b><br><span class="meta">${t(a.created_at)} 발생 · ${t(a.acked_at)} 확인</span></li>`).join('')}</ul></details>` : ''}`;
  $$('[data-ack]', box).forEach((b) => b.addEventListener('click', async () => {
    try { await api(`/api/admin/alerts/${b.dataset.ack}/ack`, { method: 'POST' }); A.sig.alerts = null; await renderAdmin(); }
    catch (e) { toast(e.message, true); }
  }));
  $$('[data-rematch]', box).forEach((b) => b.addEventListener('click', () => runMatch(false)));
}

function checkNewHigh(ov) {
  const fresh = ov.alerts.filter((a) => a.severity === 'high' && !A.seenHigh.has(a.id));
  if (!fresh.length) return;
  fresh.forEach((a) => { A.seenHigh.add(a.id); $(`#al-${a.id}`)?.classList.add('flash'); });
  try { sessionStorage.setItem('moata.seenHigh', JSON.stringify([...A.seenHigh].slice(-200))); } catch (e) { /* 무시 */ }
  if (A.booted) { if (A.sound) beep(); toast(`긴급: ${fresh[0].title}`, true); }
  A.booted = true;
}
setTimeout(() => { A.booted = true; }, 4000);

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.25, 0.5].forEach((d) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.18, ctx.currentTime + d); g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + d + 0.2);
      o.start(ctx.currentTime + d); o.stop(ctx.currentTime + d + 0.21);
    });
  } catch (e) { /* 소리 못 냄 */ }
}

/* ---------- 신고 ---------- */
function drawReports(ov) {
  const box = $('#reports');
  const open = ov.reports.filter((r) => r.status !== 'resolved').length;
  box.innerHTML = `<h2>${icon('shield-alert', 'sm')}신고<span class="badge ${open ? 'warn' : 'gray'} count">처리 전 ${open}건</span></h2>
    ${ov.reports.length ? ov.reports.map((r) => `<div class="alert-item ${r.kind === 'emergency' && r.status !== 'resolved' ? 'high' : 'low'}">
      <div class="between"><span class="t">${esc(r.kind_label)} · ${esc(r.category_label)}</span><span class="badge gray">${esc(STATUS_KO[r.status])}</span></div>
      <div class="d">${r.note ? `"${esc(r.note)}" · ` : ''}${esc(r.snapshot.car_number || '차량 정보 없음')}${r.snapshot.route ? ` · ${esc(r.snapshot.route.filter(Boolean).slice(0, 4).join(' → '))}` : ''}</div>
      <div class="d">${r.lat != null ? `위치 ${r.lat.toFixed(5)}, ${r.lng.toFixed(5)} (${r.location_source === 'gps' ? 'GPS' : '마지막 정차 지점'})` : '위치 정보 없음'}${r.target_request_id ? ` · 대상 요청 #${r.target_request_id}` : ''}</div>
      <div class="between mt8"><span class="meta">${t(r.created_at)} · 신고자 #${r.reporter_id}${r.group_id ? ` · 그룹 #${r.group_id}` : ''}</span>
        <span class="row">${r.kind === 'companion' && r.target_request_id ? (r.confirmed ? '<span class="badge danger">사실 확인됨</span>'
          : `<button class="btn sm outline" data-confirm="${r.id}" title="신고 대상 신뢰도 −15">사실 확인</button>`) : ''}
        <select class="input sm" data-report="${r.id}" aria-label="처리 상태">${['received', 'in_progress', 'resolved'].map((s) => `<option value="${s}" ${s === r.status ? 'selected' : ''}>${STATUS_KO[s]}</option>`).join('')}</select></span></div></div>`).join('')
      : '<p class="empty">접수된 신고가 없어요</p>'}`;
  $$('[data-confirm]', box).forEach((b) => b.addEventListener('click', async () => {
    if (!(await askConfirm('이 신고를 사실로 확인할까요? 신고 대상의 신뢰도가 15점 내려가요.', { ok: '사실로 확인', danger: true }))) return;
    try { await api(`/api/admin/reports/${b.dataset.confirm}/confirm`, { method: 'POST' }); toast('사실로 확인했어요. 대상 신뢰도를 낮췄어요'); A.sig.reports = null; await renderAdmin(); }
    catch (e) { toast(e.message, true); }
  }));
  $$('[data-report]', box).forEach((sel) => sel.addEventListener('change', async () => {
    try { await api(`/api/admin/reports/${sel.dataset.report}`, { method: 'POST', body: { status: sel.value } }); toast('처리 상태를 바꿨어요'); A.sig.reports = null; }
    catch (e) { toast(e.message, true); }
  }));
}

/* ---------- 지표 ---------- */
function drawKpis(ov) {
  const m = ov.metrics, c = ov.counts;
  const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
  const items = [
    ['매칭 대기', c.waiting], ['수락 대기', c.proposed], ['확정·운행', c.confirmed + c.riding + c.dropped],
    ['24시간 요청', m.requests_24h], ['빠른 / 예약', `${m.quick_24h} / ${m.reserved_24h}`], ['매칭 그룹', m.matched_groups_24h],
    ['평균 대기', m.avg_wait_min == null ? '—' : `${m.avg_wait_min}분`], ['만료', m.expired_24h],
    ['AI 대체 비율', pct(m.ai_fallback_ratio), m.ai_fallback_ratio > 0.5], ['경로 추정 대체', pct(m.route_fallback_ratio)],
    ['정산 불일치', m.settlement_mismatch, m.settlement_mismatch > 0], ['열린 경보', m.open_alerts, m.open_alerts > 0],
  ];
  $('#kpis').innerHTML = items.map(([k, v, bad]) => `<div class="kpi ${bad ? 'bad' : ''}"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join('');
}

function drawChart(ov) {
  const h = ov.metrics.hourly;
  const max = Math.max(1, ...h.map((x) => Math.max(x.requests, x.matched)));
  const W = 720, H = 140, bw = W / h.length;
  const bars = h.map((x, i) => {
    const rh = (x.requests / max) * 100, mh = (x.matched / max) * 100;
    return `<rect x="${i * bw + 3}" y="${110 - rh}" width="${bw / 2 - 3}" height="${rh}" rx="2" fill="#B9D5FA"><title>${x.hour} 요청 ${x.requests}</title></rect>
      <rect x="${i * bw + bw / 2}" y="${110 - mh}" width="${bw / 2 - 3}" height="${mh}" rx="2" fill="#0B6FE8"><title>${x.hour} 매칭 ${x.matched}</title></rect>
      ${i % 3 === 0 ? `<text x="${i * bw + 2}" y="128">${x.hour}</text>` : ''}`;
  }).join('');
  $('#chart-box').innerHTML = `<h2>${icon('gauge', 'sm')}최근 24시간<span class="meta count"><i style="display:inline-block;width:10px;height:10px;background:#B9D5FA;border-radius:2px"></i> 요청 <i style="display:inline-block;width:10px;height:10px;background:#0B6FE8;border-radius:2px;margin-left:8px"></i> 매칭 그룹</span></h2>
    <svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="시간대별 요청과 매칭">${bars}<line x1="0" y1="110" x2="${W}" y2="110" stroke="#E2E8F0"/></svg>`;
}

/* ---------- 운행 목록: 골라서 자세히 보기 ---------- */
const TRIP_FILTERS = [
  { key: 'riding', label: '운행 중', test: (g) => g.status === 'riding' },
  { key: 'dispatch', label: '배차 대기', test: (g) => g.status === 'confirmed' },
  { key: 'proposed', label: '수락 대기', test: (g) => g.status === 'proposed' },
  { key: 'done', label: '최근 완료', test: () => true },
];
A.filter = sessionStorage.getItem('moata.tripFilter') || 'riding';
A.sel = Number(sessionStorage.getItem('moata.tripSel')) || null;

function tripsOf(ov, key) {
  const f = TRIP_FILTERS.find((x) => x.key === key);
  return key === 'done' ? ov.completed : ov.groups.filter(f.test);
}
function findTrip(ov, id) { return ov.groups.find((g) => g.id === id) || ov.completed.find((g) => g.id === id) || null; }
function tripState(g) {
  if (g.status === 'riding') return g.progress < 0 ? '픽업하러 가는 중' : `운행 중 ${g.progress + 1}/${g.stops.length}`;
  if (g.status === 'confirmed') return g.reservation_assigned ? '예약 배차 완료' : g.offer_pending ? '배차 제안 중' : '기사님 찾는 중';
  if (g.status === 'proposed') return `수락 ${g.members.filter((m) => m.accepted).length}/${g.members.length}`;
  if (g.status === 'completed') return '완료';
  return STATUS_KO[g.status] || g.status;
}
function selectTrip(id) {
  A.sel = id;
  try { id ? sessionStorage.setItem('moata.tripSel', String(id)) : sessionStorage.removeItem('moata.tripSel'); } catch (e) { /* 무시 */ }
  A.sig.live = null; A.sig.map = null;
  drawLive(A.ov); drawMap(A.ov);
}

function drawLive(ov) {
  const box = $('#live');
  const counts = Object.fromEntries(TRIP_FILTERS.map((f) => [f.key, tripsOf(ov, f.key).length]));
  const list = tripsOf(ov, A.filter);
  const sel = A.sel ? findTrip(ov, A.sel) : null;
  box.innerHTML = `<h2>${icon('route', 'sm')}운행 목록<span class="meta count">하나를 누르면 지도와 상세가 그 운행으로 바뀌어요</span></h2>
    <div class="seg" role="tablist" aria-label="운행 상태">${TRIP_FILTERS.map((f) => `<button role="tab" aria-selected="${f.key === A.filter}" data-filter="${f.key}">${f.label} ${counts[f.key]}</button>`).join('')}</div>
    <ul class="list mt8" role="listbox" aria-label="운행">${list.length ? list.map((g) => {
      const first = g.stops[0], last = g.stops[g.stops.length - 1];
      return `<li><button class="item trip ${g.id === A.sel ? 'on' : ''}" role="option" aria-selected="${g.id === A.sel}" data-trip="${g.id}">
        <span class="num" style="flex:none">${g.members.length}</span>
        <span class="grow"><b>#${g.id} ${esc(first.name)} → ${esc(last.name)}</b><br>
          <span class="meta">${esc(tripState(g))} · ${g.driver ? `${esc(g.driver.name)} (${esc(g.driver.car_number)})` : '기사님 없음'} · ${hhmm(g.departure)} 출발</span></span>
        ${icon('chevron', 'sm')}</button></li>`;
    }).join('') : `<li class="empty">${TRIP_FILTERS.find((f) => f.key === A.filter).label} 운행이 없어요</li>`}</ul>
    ${A.sel ? tripDetail(ov, sel) : ''}
    <h2 class="mt16">${icon('users', 'sm')}매칭 대기 요청<span class="badge gray count">${ov.requests.filter((r) => r.status === 'waiting').length}건</span></h2>
    ${ov.requests.filter((r) => r.status === 'waiting').map((r) => `<div class="between small" style="padding:6px 0;border-bottom:1px solid var(--border)">
      <span>${esc(r.nickname)}(${r.gender === 'F' ? '여' : '남'}) · ${esc(r.origin.name)} → ${esc(r.dest.name)}</span>
      <span class="meta">${r.type === 'quick' ? '빠른' : '예약'} · 마감 ${t(r.deadline)}</span></div>`).join('') || '<p class="empty">대기 중인 요청이 없어요</p>'}`;
  $$('[data-filter]', box).forEach((b) => b.addEventListener('click', () => {
    A.filter = b.dataset.filter;
    try { sessionStorage.setItem('moata.tripFilter', A.filter); } catch (e) { /* 무시 */ }
    A.sig.live = null; drawLive(A.ov);
  }));
  $$('[data-trip]', box).forEach((b) => b.addEventListener('click', () => {
    const id = Number(b.dataset.trip);
    selectTrip(A.sel === id ? null : id);
  }));
  $('#trip-close', box)?.addEventListener('click', () => selectTrip(null));
}

function tripDetail(ov, g) {
  if (!g) {
    return `<div class="card mt12"><div class="between"><b>#${A.sel} 운행</b><button class="btn sm outline" id="trip-close">선택 해제</button></div>
      <p class="small muted mt8">이 운행은 목록에서 사라졌어요(취소됐거나 오래전에 끝났어요).</p></div>`;
  }
  const reports = ov.reports.filter((r) => r.group_id === g.id);
  const fin = g.settlement?.final;
  const next = g.status === 'riding' ? g.stops[g.progress + 1] : null;
  return `<div class="card mt12" aria-live="polite">
    <div class="between"><span class="row"><b style="font-size:16px">#${g.id}</b><span class="badge ${g.status === 'riding' ? '' : g.status === 'completed' ? 'ok' : 'gray'}">${esc(tripState(g))}</span>
      ${g.is_solo ? '<span class="badge gray">단독</span>' : ''}</span>
      <button class="btn sm outline" id="trip-close">선택 해제</button></div>
    <div class="kpis" style="margin-top:12px;grid-template-columns:repeat(auto-fill,minmax(110px,1fr))">
      <div class="kpi"><b style="font-size:17px">${g.driver ? esc(g.driver.name) : '—'}</b><span>${g.driver ? `${esc(g.driver.car_number)} · ${esc([g.driver.car_model, g.driver.car_color].filter(Boolean).join(' '))}` : '기사님 없음'}</span></div>
      <div class="kpi"><b style="font-size:17px">${hhmm(g.departure)}</b><span>${md(g.departure)} 출발</span></div>
      <div class="kpi"><b style="font-size:17px">${km(g.total_m)}</b><span>총 거리</span></div>
      <div class="kpi"><b style="font-size:17px">${won(g.final_fare || g.est_fare)}</b><span>${g.final_fare ? '최종 요금' : '예상 요금'}</span></div>
    </div>
    ${next ? `<p class="small mt12">다음 정차: <b>${esc(next.name)}</b> · ${esc(next.nickname)} ${next.kind === 'pickup' ? '탑승' : '하차'}${next.kind === 'pickup' && !next.notice_ack ? ' <span class="badge warn">안내 미확인</span>' : ''}</p>` : ''}
    <h3 class="section-title">정차 순서</h3>
    <ol class="stops">${g.stops.map((s, i) => `<li class="${s.reached ? 'done' : ''} ${next && s.index === next.index ? 'me' : ''}"><span class="num">${i + 1}</span>
      <span><span class="place">${esc(s.name)}</span><br><span class="what">${esc(s.nickname)} ${s.kind === 'pickup' ? '탑승' : '하차'}${s.reached ? ' · 완료' : ''}</span></span>
      <span class="time">${hhmm(s.eta)}</span></li>`).join('')}</ol>
    <h3 class="section-title">승객</h3>
    <table class="tbl"><thead><tr><th>승객</th><th>종류</th><th>안내</th><th>${fin ? '낼 금액' : '예상 몫'}</th><th>상태</th></tr></thead><tbody>
      ${g.members.map((m) => `<tr><td>${esc(m.nickname)} (${m.gender === 'F' ? '여' : '남'}${m.seats > 1 ? `, ${m.seats}명` : ''})
        <br><b style="color:${trustBand(m.trust).color}">신뢰도 ${m.trust}</b>${m.low_trust ? ' · 추가 부담' : ''}${m.trust_adjust ? ` (${m.trust_adjust > 0 ? '+' : ''}${won(m.trust_adjust)})` : ''}${m.hold ? ` · 예약금 ${m.hold.toLocaleString('ko-KR')}P` : ''}</td>
        <td>${m.type === 'reserved' ? '예약' : '빠른'}</td><td>${m.notice_ack ? '확인' : '<span style="color:var(--warn)">미확인</span>'}</td>
        <td>${won(fin ? fin[String(m.request_id)] : m.share)}</td>
        <td>${esc(STATUS_KO[m.status] || (m.status === 'done' ? '완료' : m.status))}${m.paid ? ' · 송금 완료' : ''}${g.status === 'proposed' ? (m.accepted ? ' · 수락' : ' · 응답 대기') : ''}</td></tr>`).join('')}
    </tbody></table>
    <p class="meta mt8">매칭: ${esc(METHOD_KO[g.match_method] || g.match_method || '—')}${g.match_reason ? ` · ${esc(g.match_reason)}` : ''} · 경로 ${esc(g.provider)}</p>
    ${reports.length ? `<p class="small mt8" style="color:var(--danger)">${icon('shield-alert', 'xs')} 이 운행의 신고 ${reports.length}건: ${reports.map((r) => esc(r.category_label)).join(', ')}</p>` : ''}
  </div>`;
}

/* ---------- 매칭 제어 ---------- */
function drawControl(ov) {
  const lm = ov.last_match;
  const box = $('#control');
  box.innerHTML = `<h2>${icon('bolt', 'sm')}매칭 제어</h2>
    <p class="small muted">${ov.auto_match ? `AI 자동 매칭이 켜져 있어요. 빠른 요청은 들어오는 즉시, 예약 요청은 ${CONFIG.params.match_interval_sec}초마다 매칭해요.`
      : 'AI 자동 매칭이 꺼져 있어요. 아래 버튼으로만 매칭해요.'}</p>
    <div class="row mt12" style="flex-wrap:wrap">
      <button class="btn sm primary" id="m-ai">${icon('sparkles', 'xs')}지금 매칭 돌리기</button>
      <button class="btn sm outline" id="m-opt">AI 없이 최적화만</button>
      <button class="btn sm outline" id="m-dispatch">배차 돌리기</button></div>
    ${lm ? `<p class="meta mt12">마지막 매칭 ${t(lm.at)} (${esc({ manual: '수동', periodic: '주기', quick_request: '빠른 요청', reject: '거절 후', demo: '데모' }[lm.trigger] || lm.trigger)}) ·
      새 그룹 ${lm.created.length} · 단독 ${lm.solo.length} · 만료 ${lm.expired.length} · 남은 대기 ${lm.waiting}
      ${lm.optimizer ? `<br>최적화 ${esc(METHOD_KO[lm.optimizer.method] || lm.optimizer.method)} · 후보 ${lm.optimizer.candidates}개 · 절감 ${km(lm.optimizer.saving_m)} · ${lm.optimizer.elapsed_ms}ms
        (탐욕 대비 ${lm.optimizer.greedy_objective ? Math.round((lm.optimizer.objective / lm.optimizer.greedy_objective) * 100) : 100}%)` : ''}</p>` : ''}`;
  $('#m-ai').addEventListener('click', () => runMatch(true));
  $('#m-opt').addEventListener('click', () => runMatch(false));
  $('#m-dispatch').addEventListener('click', async () => {
    try { const r = await api('/api/admin/dispatch', { method: 'POST' }); toast(r.offers.length ? `배차 제안 ${r.offers.length}건을 보냈어요` : '지금 배차할 그룹이나 기사님이 없어요'); }
    catch (e) { toast(e.message, true); }
  });
}

async function runMatch(useAi) {
  try {
    const r = await api('/api/admin/match', { method: 'POST', body: { use_ai: useAi } });
    toast(r.created.length ? `${r.created.length}개 그룹을 만들었어요` : '지금 묶을 수 있는 동승자가 없어요');
    A.sig = {}; await renderAdmin();
  } catch (e) { toast(e.message, true); }
}

/* ---------- AI 상태 · 요약 ---------- */
function drawAi(ov) {
  const box = $('#ai');
  const s = ov.ai_summary;
  const out = s?.output || {};
  box.innerHTML = `<h2>${icon('sparkles', 'sm')}AI 상태</h2>
    ${ov.ai_enabled ? `<table class="tbl"><thead><tr><th>제공자</th><th>성공(10분)</th><th>호출</th><th>상태</th></tr></thead><tbody>
      ${ov.ai_status.map((p) => `<tr><td><b>${esc(p.provider)}</b><br><span class="meta">${esc(p.model)}</span></td>
        <td>${p.success_rate_10m == null ? '—' : `${Math.round(p.success_rate_10m * 100)}%`}</td><td>${p.ok}/${p.calls}</td>
        <td>${p.cooling_down_sec ? `<span class="badge warn">${p.cooling_down_sec}초 쉬는 중</span>` : '<span class="badge ok">정상</span>'}
          ${p.last_error ? `<br><span class="meta">${esc(p.last_error.slice(0, 80))}</span>` : ''}</td></tr>`).join('')}</tbody></table>`
      : '<p class="small muted">.env에 GEMINI_API_KEY나 GROQ_API_KEY가 없어 AI 없이 수리 최적화로만 매칭해요.</p>'}
    ${ov.ai_orders ? `<table class="tbl mt8"><thead><tr><th>작업</th><th>AI 순서(키 있는 것만)</th></tr></thead><tbody>
      ${[['dispatch', '배차 판단'], ['match', '매칭 판단'], ['predict', '매칭 예상'], ['summary', '운영 요약']].map(([k, label]) =>
        `<tr><td>${label}</td><td>${(ov.ai_orders[k] || []).join(' → ') || '없음'} → 최적화·통계</td></tr>`).join('')}</tbody></table>` : ''}
    <h2 class="mt16">${icon('info', 'sm')}AI 운영 요약<span class="meta count">${s ? t(s.created_at) : ''}</span></h2>
    ${s ? `<p class="small">${esc(s.summary)}</p>${(out.concerns || []).length ? `<ul class="small" style="padding-left:18px">${out.concerns.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}`
      : '<p class="small muted">한 시간마다 지난 지표를 요약해요.</p>'}
    ${ov.ai_enabled ? '<button class="btn sm outline mt8" id="sum">지금 요약하기</button>' : ''}`;
  $('#sum', box)?.addEventListener('click', async (e) => {
    e.currentTarget.disabled = true;
    try { await api('/api/admin/summary', { method: 'POST' }); A.sig.ai = null; await renderAdmin(); }
    catch (err) { toast(err.message, true); }
  });
}

function drawDecisions(ov) {
  $('#decisions').innerHTML = `<h2>${icon('route', 'sm')}AI 결정 로그</h2>
    ${ov.ai_decisions.length ? `<ul class="log">${ov.ai_decisions.map((d) => {
      const r = d.result || {};
      const tag = d.used_ai ? `<span class="badge">${esc(d.provider)}</span>`
        : d.error ? '<span class="badge warn">AI 실패</span>'
        : `<span class="badge gray">${esc(METHOD_KO[d.method] || d.method)}</span>`;
      return `<li><div class="between"><span class="row">${tag}<b>${d.kind === 'match' ? '매칭' : '배차'}</b></span><span class="meta">${t(d.created_at)}</span></div>
        <div class="mt8">${esc(d.summary || '')}</div>
        ${d.kind === 'match' ? `<div class="meta">제안 ${(r.created || []).length}개 · 대기 ${(r.waited || []).length}개 · 절감 ${km(r.final_saving_m)} / 최적 ${km(r.optimal_saving_m)}</div>` : ''}
        ${(r.violations || []).length ? `<div class="meta" style="color:var(--danger)">실행 안 함: ${esc(r.violations.join('; '))}</div>` : ''}
        ${(r.overridden || []).length ? `<div class="meta" style="color:var(--warn)">마감 임박으로 바로 제안: ${esc(r.overridden.join(', '))}</div>` : ''}
        ${d.error ? `<div class="meta">오류: ${esc(d.error.slice(0, 120))}</div>` : ''}</li>`;
    }).join('')}</ul>` : '<p class="empty">아직 매칭 판단 기록이 없어요</p>'}`;
}

/* ---------- 기사님 ---------- */
function drawDrivers(ov) {
  $('#drivers').innerHTML = `<h2>${icon('car', 'sm')}기사님<span class="badge gray count">${ov.drivers.filter((d) => d.available).length}명 운행 가능</span></h2>
    ${ov.drivers.length ? ov.drivers.map((d) => `<div class="between small" style="padding:7px 0;border-bottom:1px solid var(--border)">
      <span><b>${esc(d.name)}</b> · ${esc(d.car_number)}</span>
      <span class="badge ${d.state === '운행 중' ? '' : d.state === '쉬는 중' ? 'gray' : d.state === '배차 제안 중' ? 'warn' : 'ok'}">${esc(d.state)}</span></div>`).join('')
      : '<p class="empty">가입한 기사님이 없어요</p>'}`;
}

/* ---------- 데모 도구 ---------- */
function drawDemo() {
  const box = $('#demo');
  box.innerHTML = `<h2>${icon('users', 'sm')}데모 도구</h2>
    <p class="small muted">삼척시 곳곳에서 무작위로 출발·도착하는 요청을 2~3명씩 같은 방향으로 묶어 만들어요.</p>
    <div class="row mt8" style="flex-wrap:wrap">
      <select class="input sm" id="d-n" aria-label="인원">${[2, 3, 4, 6, 8].map((n) => `<option ${n === 4 ? 'selected' : ''}>${n}</option>`).join('')}</select>
      <select class="input sm" id="d-g" aria-label="성별"><option value="F">주로 여성</option><option value="M">주로 남성</option></select>
      <select class="input sm" id="d-t" aria-label="종류"><option value="quick">빠른</option><option value="reserved">예약</option></select>
      <button class="btn sm outline" id="d-seed">만들기</button></div>
    <button class="link-btn muted mt8" id="d-reset">운행 기록 모두 지우기(계정은 남김)</button>`;
  $('#d-seed').addEventListener('click', async () => {
    try {
      const r = await api('/api/admin/demo/seed', { method: 'POST', body: { count: Number($('#d-n').value), gender: $('#d-g').value, type: $('#d-t').value } });
      toast(`데모 요청 ${r.created}건을 만들었어요`); A.sig = {}; A.sig.demo = '1'; await renderAdmin();
    } catch (e) { toast(e.message, true); }
  });
  $('#d-reset').addEventListener('click', async () => {
    if (!(await askConfirm('요청, 그룹, 신고, 경보, AI 기록을 모두 지울까요? 로그인 계정은 남아요.', { ok: '모두 지우기', danger: true }))) return;
    try { await api('/api/admin/demo/reset', { method: 'POST' }); toast('운행 기록을 지웠어요'); A.sig = {}; A.sig.demo = '1'; await renderAdmin(); }
    catch (e) { toast(e.message, true); }
  });
}

/* ---------- 지도 전체화면(Esc로 닫기) ---------- */
function toggleMapFull(force) {
  const box = $('#ops-map');
  const on = force ?? !box.classList.contains('full');
  box.classList.toggle('full', on);
  document.body.style.overflow = on ? 'hidden' : '';
  const btn = $('#map-full');
  btn.textContent = on ? '전체화면 닫기 (Esc)' : '전체화면';
  btn.setAttribute('aria-pressed', String(on));
  // 크기만 다시 계산하고, 보던 위치와 확대 수준은 그대로 둔다
  const view = A.map.getView();
  setTimeout(() => { A.map.resize(); A.map.setView(view); }, 60);
}

/* ---------- 지도 위 기사님 정보(기사님 아이콘을 누르면 열림) ---------- */
function showDriverCard(userId) {
  A.drv = userId;
  drawDriverCard(A.ov);
}

function drawDriverCard(ov) {
  let card = $('#drv-card');
  const d = A.drv && ov ? ov.drivers.find((x) => x.user_id === A.drv) : null;
  if (!d) { if (card) card.remove(); A.drv = null; return; }
  if (!card) {
    card = document.createElement('div');
    card.id = 'drv-card';
    card.className = 'drv-card';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', '기사님 정보');
    $('#ops-map').appendChild(card);
  }
  const g = d.trip_id ? findTrip(ov, d.trip_id) : null;
  const car = [d.car_model, d.car_color].filter(Boolean).join(' ');
  const badge = d.state === '운행 중' ? '' : d.state === '쉬는 중' ? 'gray' : d.state === '배차 제안 중' ? 'warn' : 'ok';
  let trip = '';
  if (g) {
    const first = g.stops[0], last = g.stops[g.stops.length - 1];
    const next = g.stops[g.progress + 1];
    trip = `<div class="drv-trip">
      <div class="between"><b>#${g.id} 운행</b><span class="meta">${esc(tripState(g))}</span></div>
      <div class="small mt4">${esc(first.name)} → ${esc(last.name)}</div>
      <div class="meta mt4">승객 ${g.members.length}명 · ${hhmm(g.departure)} 출발${next ? ` · 다음 ${esc(next.nickname)} ${next.kind === 'pickup' ? '탑승' : '하차'}(${esc(next.name)})` : ''}</div>
      <button class="btn sm primary block mt8" id="drv-trip">운행 정보 보기</button></div>`;
  } else if (d.offer_group_id) {
    trip = `<p class="meta mt8">#${d.offer_group_id} 그룹에 배차를 제안하고 답을 기다리는 중이에요</p>`;
  }
  card.innerHTML = `<div class="between"><span class="row"><b style="font-size:16px">${esc(d.name)} 기사님</b>
      <span class="badge ${badge}">${esc(d.state)}</span></span>
      <button class="close-btn sm" id="drv-close" aria-label="닫기">${icon('x', 'sm')}</button></div>
    <div class="small mt4">${esc(d.car_number)}${car ? ` · ${esc(car)}` : ''}</div>
    ${d.reservations ? `<div class="meta mt4">예약 배차 ${d.reservations}건</div>` : ''}
    ${trip}`;
  $('#drv-close', card).addEventListener('click', () => { A.drv = null; card.remove(); });
  $('#drv-trip', card)?.addEventListener('click', () => {
    if ($('#ops-map').classList.contains('full')) toggleMapFull(false);   // 전체화면이면 닫고 아래 운행 상세를 보여 준다
    if (A.filter !== 'riding') { A.filter = 'riding'; try { sessionStorage.setItem('moata.tripFilter', 'riding'); } catch (e) { /* 무시 */ } }
    selectTrip(g.id);
    $('#live').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

/* ---------- 지도 ---------- */
function drawMap(ov) {
  const m = A.map;
  m.clear();
  const sel = A.sel ? findTrip(ov, A.sel) : null;
  if (sel) {   // 고른 운행만 크게: 경로, 정차 지점, 기사님 위치
    const line = sel.polyline && sel.polyline.length > 1 ? sel.polyline : sel.stops.map((s) => [s.lat, s.lng]);
    m.line(line, { color: BLUE, weight: 7, opacity: 0.95 });
    sel.stops.forEach((s, i) => m.pin(s, pinHtml('n', String(i + 1), `${s.nickname} ${s.kind === 'pickup' ? '탑승' : '하차'}`, s.reached ? 'done' : s.kind === 'dropoff' ? 'drop' : '')));
    const pts = [...sel.stops];
    if (sel.driver && sel.status !== 'completed') {
      const d = ov.drivers.find((x) => x.trip_id === sel.id);
      m.pin(sel.driver, pinHtml('car', '', `${sel.driver.name} 기사님`), d ? () => showDriverCard(d.user_id) : null);
      pts.push(sel.driver);
    }
    m.fit(pts, { top: 50, bottom: 50 });
    return;
  }
  const pts = [];
  ov.requests.filter((r) => r.status === 'waiting').forEach((r) => {
    m.line([r.origin, r.dest], { color: '#94A3B8', dashed: true, weight: 3, opacity: 0.8 });
    m.pin(r.origin, pinHtml('dot', '', `${r.nickname} 출발`, '#94A3B8'));
    m.pin(r.dest, pinHtml('dot', '', `${r.nickname} 도착`, '#0F172A'));
    pts.push(r.origin, r.dest);
  });
  ov.groups.forEach((g) => {
    const line = g.polyline && g.polyline.length > 1 ? g.polyline : g.stops.map((s) => [s.lat, s.lng]);
    m.line(line, { color: BLUE, weight: 5, opacity: g.status === 'proposed' ? 0.5 : 0.9 });
    g.stops.forEach((s, i) => m.pin(s, pinHtml('n', String(i + 1), `그룹 #${g.id} ${s.nickname}`, s.reached ? 'done' : s.kind === 'dropoff' ? 'drop' : '')));
    pts.push(...g.stops);
  });
  ov.drivers.forEach((d) => {
    m.pin({ lat: d.lat, lng: d.lng }, pinHtml('car', '', `${d.name} 기사님 · ${d.state}`, d.available ? '' : 'off'),
      () => showDriverCard(d.user_id));
    pts.push({ lat: d.lat, lng: d.lng });
  });
  m.fit(pts.length ? pts : [CONFIG.demo_center], { top: 40, bottom: 40 });
}

bootAdmin().catch((e) => {
  if (e.status === 401) return;
  document.body.insertAdjacentHTML('beforeend', `<p class="empty">운영 페이지를 시작하지 못했어요: ${esc(e.message)}</p>`);
});
