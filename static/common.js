'use strict';
/* =========================================================================
   모아타(MOATA) 공통 스크립트: API, 선 아이콘, 지도(카카오맵 / Leaflet 대체), 신고하기, PWA
   승객·기사님·운영 페이지가 함께 쓴다. 각 페이지는 window.MOATA_APP을 먼저 정한다.
   ========================================================================= */

const APP = window.MOATA_APP || 'passenger';
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const won = (n) => `${Math.round(n ?? 0).toLocaleString('ko-KR')}원`;
const km = (m) => `${((m || 0) / 1000).toFixed(1)}km`;
const kmShort = (m) => { const v = (m || 0) / 1000; return `${v >= 10 ? Math.round(v) : Math.round(v * 10) / 10}km`; };
const mins = (s) => `${Math.max(1, Math.round((s || 0) / 60))}분`;
const hhmm = (iso) => (iso || '').slice(11, 16);
const pad = (n) => String(n).padStart(2, '0');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function toIso(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fromIso(iso) {
  const [date, time = '00:00:00'] = iso.split('T');
  const [y, m, d] = date.split('-').map(Number);
  const [hh, mm, ss = 0] = time.split(':').map(Number);
  return new Date(y, m - 1, d, hh, mm, ss);
}
function addMin(iso, n) { const d = fromIso(iso); d.setMinutes(d.getMinutes() + n); return toIso(d); }
function md(iso) { const d = fromIso(iso); return `${d.getMonth() + 1}/${d.getDate()}`; }

/* 서버 시각과 브라우저 시각 차이(남은 시간 카운트다운용) */
let clockSkew = 0;
function syncClock(serverStamp) { if (serverStamp) clockSkew = fromIso(serverStamp).getTime() - Date.now(); }
function serverNow() { return new Date(Date.now() + clockSkew); }
function secondsUntil(stamp) { return stamp ? Math.max(0, Math.round((fromIso(stamp).getTime() - serverNow().getTime()) / 1000)) : 0; }
function mmss(s) { return `${Math.floor(s / 60)}:${pad(s % 60)}`; }

async function api(path, { method = 'GET', body, app = APP } = {}) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { 'X-Moata-App': app, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(Array.isArray(data.detail) ? '입력값을 확인하세요' : (data.detail || '요청을 처리하지 못했어요'));
    err.status = res.status;
    if (res.status === 401 && APP !== 'login') { location.href = '/'; }
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  let t = $('#toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
  t.textContent = msg;
  t.className = `toast${isError ? ' error' : ''}`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

/* ---------- 확인 창 ----------
   브라우저 기본 confirm()은 앱 안 브라우저·일부 PWA에서 뜨지 않고 바로 '취소'가 되므로 화면 안에 직접 띄운다.
   사용: if (!(await askConfirm('지울까요?', { ok: '지우기', danger: true }))) return; */
function askConfirm(message, { ok = '확인', cancel = '취소', danger = false } = {}) {
  return new Promise((resolve) => {
    const back = document.createElement('div');
    back.className = 'confirm-back';
    back.innerHTML = `<div class="confirm-box" role="alertdialog" aria-modal="true" aria-labelledby="cf-msg">
      <p id="cf-msg">${esc(message)}</p>
      <div class="confirm-actions"><button class="btn sm outline" data-v="0">${esc(cancel)}</button>
        <button class="btn sm ${danger ? 'danger' : 'primary'}" data-v="1">${esc(ok)}</button></div></div>`;
    const prev = document.activeElement;
    const done = (v) => { document.removeEventListener('keydown', onKey, true); back.remove(); prev?.focus?.(); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(false); } };
    back.addEventListener('click', (e) => { if (e.target === back) done(false); const b = e.target.closest('[data-v]'); if (b) done(b.dataset.v === '1'); });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(back);
    back.querySelector('[data-v="1"]').focus();
  });
}

/* ---------- 선 아이콘(이모지 없음) ---------- */
const ICONS = {
  pin: '<path d="M12 21s-7-6.2-7-11a7 7 0 1 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7"/><path d="M18.5 14.8c1.6.8 2.7 2.5 3 5.2"/>',
  route: '<circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19h8a3.5 3.5 0 0 0 0-7H8a3.5 3.5 0 0 1 0-7h8"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4.5-6 8-6s7 2 8 6"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  'check-circle': '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.8 2.8L16.5 9.5"/>',
  alert: '<path d="M12 3.5L2.5 20h19L12 3.5z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/>',
  car: '<path d="M4 16.5l1.6-5.3A2 2 0 0 1 7.5 9.8h9a2 2 0 0 1 1.9 1.4l1.6 5.3"/><path d="M3.5 16.5h17v2.5a1 1 0 0 1-1 1h-1.5a1 1 0 0 1-1-1v-.5h-10v.5a1 1 0 0 1-1 1H4.5a1 1 0 0 1-1-1z"/><path d="M7 13.5h.01M17 13.5h.01"/>',
  'car-crash': '<path d="M4 17l1.4-4.6A2 2 0 0 1 7.3 11h7.4"/><path d="M3.5 17h12v2.5a1 1 0 0 1-1 1H4.5a1 1 0 0 1-1-1z"/><path d="M17 4l1 3 3 .5-2.5 2 1 3-2.5-1.7L15.5 12l.5-3-2-2 3-.5z"/>',
  share: '<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="M8.2 10.8l7.6-4.4M8.2 13.2l7.6 4.4"/>',
  shield: '<path d="M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6z"/>',
  'shield-alert': '<path d="M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6z"/><path d="M12 8v4M12 15.5v.01"/>',
  'shield-check': '<path d="M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6z"/><path d="M8.5 12l2.5 2.5 4.5-4.5"/>',
  wallet: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 14.5h2"/>',
  'wallet-x': '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M14 13l4 4M18 13l-4 4"/>',
  receipt: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
  gauge: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-5"/>',
  meter: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 10h8M8 14h4"/>',
  toll: '<path d="M4 20V9l8-5 8 5v11"/><path d="M9 20v-6h6v6"/>',
  'message-x': '<path d="M4 5h16v11H9l-5 4z"/><path d="M10 8.5l4 4M14 8.5l-4 4"/>',
  'message-alert': '<path d="M4 5h16v11H9l-5 4z"/><path d="M12 7.5v4M12 13.5v.01"/>',
  'heart-pulse': '<path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/><path d="M8 12h2l1-2 2 4 1-2h2"/>',
  'user-x': '<circle cx="10" cy="8" r="4"/><path d="M3 21c1-4 4-6 7-6 1.5 0 3 .4 4.2 1.2"/><path d="M17 15l4 4M21 15l-4 4"/>',
  'user-clock': '<circle cx="10" cy="8" r="4"/><path d="M3 21c1-4 4-6 7-6"/><circle cx="17.5" cy="17.5" r="3.5"/><path d="M17.5 16v1.6l1 .8"/>',
  sparkles: '<path d="M12 4l1.8 4.2L18 10l-4.2 1.8L12 16l-1.8-4.2L6 10l4.2-1.8z"/><path d="M19 15l.8 1.7 1.7.8-1.7.8L19 20l-.8-1.7-1.7-.8 1.7-.8z"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  bolt: '<path d="M13 3L5 13h6l-1 8 8-10h-6z"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
  logout: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/>',
  bell: '<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  map: '<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>',
  crosshair: '<circle cx="12" cy="12" r="7"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  volume: '<path d="M4 9.5h3.5L12 5v14l-4.5-4.5H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  moata: '<circle cx="7" cy="8" r="2.6"/><circle cx="17" cy="8" r="2.6"/><path d="M4 19c0-3 1.4-5 3-5.6M20 19c0-3-1.4-5-3-5.6"/><path d="M8.5 19.5c.6-2.7 1.9-4.5 3.5-4.5s2.9 1.8 3.5 4.5"/><circle cx="12" cy="11.3" r="2.3"/>',
};
function icon(name, cls = '') {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[name] || ICONS.info}</svg>`;
}
const logoMark = () => `<span class="logo-mark" aria-hidden="true"><svg viewBox="0 0 512 512" width="70%" height="70%" fill="#fff">
  <circle cx="160" cy="196" r="46"/><circle cx="352" cy="196" r="46"/><circle cx="256" cy="228" r="58"/><rect x="104" y="318" width="304" height="72" rx="36"/></svg></span>`;

/* =========================================================================
   지도: 카카오맵(키가 있을 때) / Leaflet + OSM(카카오 실패·키 없음)
   모드: request(점선) · group(파란 실선 + 정차 번호) · driver(파란 실선 + 차량)
   ========================================================================= */
const BLUE = '#0B6FE8';
const toLL = (p) => (Array.isArray(p) ? { lat: p[0], lng: p[1] } : p);

function pinHtml(kind, label = '', title = '', extra = '') {
  if (kind === 'o') return `<div class="pin pin-o" title="${esc(title)}"></div>`;
  if (kind === 'd') return `<div class="pin pin-d" title="${esc(title)}"></div>`;
  if (kind === 'car') return `<div class="pin pin-car ${extra}" title="${esc(title)}">${icon('car', 'sm')}</div>`;
  if (kind === 'dot') return `<div class="pin pin-dot" style="--c:${extra}" title="${esc(title)}"></div>`;
  return `<div class="pin pin-n ${extra}" title="${esc(title)}">${esc(label)}</div>`;
}

class LeafletAdapter {
  constructor(el, center) {
    this.kind = 'leaflet';
    this.map = L.map(el, { zoomControl: false, attributionControl: true }).setView([center.lat, center.lng], 14);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap' }).addTo(this.map);
    this.layer = L.layerGroup().addTo(this.map);
  }
  onClick(cb) { this.map.on('click', (e) => cb({ lat: e.latlng.lat, lng: e.latlng.lng })); }
  clear() { this.layer.clearLayers(); }
  pin(p, html, onClick = null) {   // onClick이 있으면 그 핀을 눌렀을 때 부른다
    const icon_ = L.divIcon({ html, className: onClick ? 'pin-click' : '', iconSize: [0, 0] });
    const m = L.marker([p.lat, p.lng], { icon: icon_, keyboard: false, interactive: !!onClick, zIndexOffset: onClick ? 500 : 0 }).addTo(this.layer);
    if (onClick) m.on('click', (e) => { L.DomEvent.stopPropagation(e); onClick(); });
  }
  line(points, { color = BLUE, dashed = false, weight = 5, opacity = 0.9 } = {}) {
    const ll = points.map(toLL).map((p) => [p.lat, p.lng]);
    L.polyline(ll, { color, weight, opacity, dashArray: dashed ? '2 10' : null, lineCap: 'round' }).addTo(this.layer);
  }
  fit(points, pad = { top: 80, bottom: 380 }) {
    const ll = points.map(toLL).filter(Boolean).map((p) => [p.lat, p.lng]);
    if (!ll.length) return;
    if (ll.length === 1) { this.map.setView(ll[0], 15); return; }
    this.map.fitBounds(L.latLngBounds(ll), { paddingTopLeft: [40, pad.top], paddingBottomRight: [40, pad.bottom], maxZoom: 16 });
  }
  center(p, zoom = 15) { this.map.setView([p.lat, p.lng], zoom); }
  getCenter() { const c = this.map.getCenter(); return { lat: c.lat, lng: c.lng }; }
  onIdle(cb) { const h = () => cb(this.getCenter()); this.map.on('moveend', h); return () => this.map.off('moveend', h); }
  resize() { this.map.invalidateSize(); }
  getView() { return { center: this.getCenter(), zoom: this.map.getZoom() }; }
  setView(v) { this.map.setView([v.center.lat, v.center.lng], v.zoom, { animate: false }); }
  async search(q) {
    const key = q.trim();
    return (key ? CONFIG.demo_places.filter((p) => p.name.includes(key)) : CONFIG.demo_places).map((p) => ({ ...p, addr: '삼척시 주요 지점' }));
  }
  async reverse() { return null; }
}

class KakaoAdapter {
  static load(key) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${encodeURIComponent(key)}&libraries=services&autoload=false`;
      const timer = setTimeout(() => reject(new Error('카카오맵 응답이 늦어요')), 8000);
      s.onload = () => { clearTimeout(timer); window.kakao && kakao.maps ? kakao.maps.load(resolve) : reject(new Error('카카오맵 SDK 응답이 올바르지 않아요')); };
      s.onerror = () => { clearTimeout(timer); reject(new Error('카카오맵을 불러오지 못했어요(JS 키·도메인 등록 확인)')); };
      document.head.appendChild(s);
    });
  }
  constructor(el, center) {
    this.kind = 'kakao';
    this.map = new kakao.maps.Map(el, { center: new kakao.maps.LatLng(center.lat, center.lng), level: 5 });
    this.items = [];
    this.places = new kakao.maps.services.Places();
    this.geocoder = new kakao.maps.services.Geocoder();
  }
  ll(p) { const q = toLL(p); return new kakao.maps.LatLng(q.lat, q.lng); }
  onClick(cb) { kakao.maps.event.addListener(this.map, 'click', (e) => cb({ lat: e.latLng.getLat(), lng: e.latLng.getLng() })); }
  clear() { this.items.forEach((i) => i.setMap(null)); this.items = []; }
  pin(p, html, onClick = null) {   // onClick이 있으면 그 핀을 눌렀을 때 부른다
    const holder = document.createElement('div');
    holder.innerHTML = html;
    const el = holder.firstElementChild;
    if (onClick) {
      el.classList.add('pin-click');
      el.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
    }
    const o = new kakao.maps.CustomOverlay({ position: this.ll(p), content: el, xAnchor: 0, yAnchor: 0,
      zIndex: onClick ? 5 : 3, clickable: !!onClick });
    o.setMap(this.map);
    this.items.push(o);
  }
  line(points, { color = BLUE, dashed = false, weight = 5, opacity = 0.9 } = {}) {
    const l = new kakao.maps.Polyline({ path: points.map((p) => this.ll(p)), strokeWeight: weight, strokeColor: color, strokeOpacity: opacity, strokeStyle: dashed ? 'shortdot' : 'solid' });
    l.setMap(this.map);
    this.items.push(l);
  }
  fit(points, pad = { top: 80, bottom: 380 }) {
    const pts = points.map(toLL).filter(Boolean);
    if (!pts.length) return;
    if (pts.length === 1) { this.map.setCenter(this.ll(pts[0])); this.map.setLevel(4); return; }
    const b = new kakao.maps.LatLngBounds();
    pts.forEach((p) => b.extend(this.ll(p)));
    this.map.setBounds(b, pad.top, 40, pad.bottom, 40);
  }
  center(p, zoom) { this.map.setCenter(this.ll(p)); this.map.setLevel(zoom && zoom >= 16 ? 3 : 4); }
  getCenter() { const c = this.map.getCenter(); return { lat: c.getLat(), lng: c.getLng() }; }
  onIdle(cb) {
    const h = () => cb(this.getCenter());
    kakao.maps.event.addListener(this.map, 'idle', h);
    return () => kakao.maps.event.removeListener(this.map, 'idle', h);
  }
  resize() { this.map.relayout(); }
  getView() { return { center: this.getCenter(), zoom: this.map.getLevel() }; }
  setView(v) { this.map.setLevel(v.zoom); this.map.setCenter(this.ll(v.center)); }
  search(q) {
    return new Promise((resolve) => {
      if (!q.trim()) { resolve(CONFIG.demo_places.map((p) => ({ ...p, addr: '삼척시 주요 지점' }))); return; }
      this.places.keywordSearch(q, (data, status) => {
        if (status !== kakao.maps.services.Status.OK) { resolve([]); return; }
        resolve(data.slice(0, 10).map((d) => ({ name: d.place_name, lat: Number(d.y), lng: Number(d.x), addr: d.road_address_name || d.address_name })));
      }, { location: new kakao.maps.LatLng(CONFIG.demo_center.lat, CONFIG.demo_center.lng), radius: 20000 });
    });
  }
  reverse(p) {
    return new Promise((resolve) => {
      this.geocoder.coord2Address(p.lng, p.lat, (r, status) => {
        if (status !== kakao.maps.services.Status.OK || !r.length) { resolve(null); return; }
        resolve(r[0].road_address?.address_name || r[0].address?.address_name || null);
      });
    });
  }
}

let CONFIG = null;
async function loadConfig() { CONFIG = await api('/api/config'); return CONFIG; }

async function createMap(el) {
  const center = CONFIG.demo_center;
  if (CONFIG.map_provider === 'kakao') {
    try {
      await KakaoAdapter.load(CONFIG.kakao_js_key);
      return new KakaoAdapter(el, center);
    } catch (e) {
      toast(`${e.message}. 기본 지도로 바꿔 보여 드려요`, true);
    }
  }
  return new LeafletAdapter(el, center);
}

/* 지도 그리기 도우미 */
function drawRequestMode(map, origin, dest, polyline) {
  map.clear();
  const pts = polyline && polyline.length > 1 ? polyline : [origin, dest].filter(Boolean);
  if (origin && dest) map.line(pts, { color: '#0F172A', dashed: true, weight: 4, opacity: 0.75 });
  if (origin) map.pin(origin, pinHtml('o', '', '출발'));
  if (dest) map.pin(dest, pinHtml('d', '', '도착'));
}
function drawGroupMode(map, g, { car = null, myRequest = null } = {}) {
  map.clear();
  const pts = g.polyline && g.polyline.length > 1 ? g.polyline : g.stops.map((s) => [s.lat, s.lng]);
  map.line(pts, { color: BLUE, weight: 6, opacity: 0.9 });
  mergeStops(g.stops).forEach((m, i) => {
    const s = m.stops[0];
    const kind = i === 0 ? 'o' : null;
    const cls = s.reached ? 'done' : (m.stops.every((x) => x.kind === 'dropoff') ? 'drop' : '');
    map.pin(s, kind && !car ? pinHtml('o', '', s.name) : pinHtml('n', String(i + 1), s.name, cls));
  });
  if (car) map.pin(car, pinHtml('car', '', '택시'));
}

/* 같은 곳(60m 안)에서 연달아 타고 내리는 정차는 화면에서 한 줄로 묶는다 */
function distM(a, b) {
  const R = 6371000, toR = (x) => (x * Math.PI) / 180;
  const dLat = toR(b.lat - a.lat), dLng = toR(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toR(a.lat)) * Math.cos(toR(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
function mergeStops(stops) {
  const out = [];
  stops.forEach((s) => {
    const last = out[out.length - 1];
    if (last && last.stops[0].kind === s.kind && distM(last.stops[0], s) < 60) last.stops.push(s);
    else out.push({ stops: [s] });
  });
  return out.map((m) => {
    const s = m.stops;
    const names = s.map((x) => x.nickname);
    const verb = s[0].kind === 'pickup' ? '탑승' : '하차';
    const who = names.length > 2 ? `${names.length}명 함께` : names.join('·');
    return { ...m, name: s[0].name, eta: s[0].eta, kind: s[0].kind, text: `${who} ${verb}`, reached: s.every((x) => x.reached),
      hasMe: names.includes('나'), indexes: s.map((x) => x.index) };
  });
}

/* =========================================================================
   신고하기(신고하기_화면.md): 탭 4개 × 카테고리 2×2, 긴급·안전은 112 바로 전화
   ========================================================================= */
function openReport({ root, groupId = null, subtitle = '', kind = 'emergency', category = null, targets = [], kinds = null } = {}) {
  const tabs = CONFIG.report_tabs.filter((t) => !kinds || kinds.includes(t.kind));
  let tab = tabs.find((t) => t.kind === kind) || tabs[0];
  let cat = category && tab.categories.some((c) => c.code === category) ? category : tab.categories[0].code;
  let target = targets.length === 1 ? targets[0].request_id : null;
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = `<div class="panel" role="dialog" aria-modal="true" aria-labelledby="rp-title"></div>`;
  (root || document.body).appendChild(ov);
  const panel = $('.panel', ov);
  const prevFocus = document.activeElement;

  function draw() {
    const danger = tab.kind === 'emergency';
    panel.className = `panel ${danger ? 'danger-mode' : ''}`;
    panel.innerHTML = `
      <div class="panel-head">
        <div><h2 class="title" id="rp-title">신고하기</h2>${subtitle ? `<p class="subtitle">${esc(subtitle)}</p>` : ''}</div>
        <button class="close-btn" data-close aria-label="닫기">${icon('x')}</button>
      </div>
      <div class="tabs ${danger ? 'danger' : ''}" role="tablist" aria-label="신고 종류">
        ${tabs.map((t) => `<button role="tab" id="rt-${t.kind}" aria-selected="${t.kind === tab.kind}" tabindex="${t.kind === tab.kind ? 0 : -1}" data-tab="${t.kind}">${esc(t.label)}</button>`).join('')}
      </div>
      <div class="panel-body" role="tabpanel" aria-labelledby="rt-${tab.kind}">
        ${danger ? `<div class="warn-box">${icon('alert')}<span>위험한 상황이면 먼저 112에 연락하세요. 신고하면 현재 위치와 차량 정보가 운영팀에 바로 전달돼요.</span></div>` : ''}
        <h3 class="section-title" id="cat-label">어떤 일이 있었나요?</h3>
        <div class="cats" role="radiogroup" aria-labelledby="cat-label">
          ${tab.categories.map((c) => `<button type="button" class="cat" role="radio" aria-checked="${c.code === cat}" tabindex="${c.code === cat ? 0 : -1}" data-cat="${c.code}">${icon(c.icon)}<span>${esc(c.label)}</span></button>`).join('')}
        </div>
        ${tab.kind === 'companion' && targets.length > 1 ? `
          <h3 class="section-title" id="tg-label">누구에 대한 신고인가요?</h3>
          <div class="chips" role="radiogroup" aria-labelledby="tg-label">
            ${targets.map((t) => `<button type="button" class="chip" role="radio" aria-checked="${t.request_id === target}" data-target="${t.request_id}">${esc(t.label)}</button>`).join('')}
          </div>` : ''}
        <label class="field"><span>상황 설명 (선택)</span><textarea class="input" id="rp-note" rows="3" maxlength="500" placeholder="필요하면 적어 주세요"></textarea></label>
        <p class="notice-line">${icon('info', 'xs')}차량 번호, 경로, 시각이 신고에 함께 첨부돼요</p>
      </div>
      <div class="sheet-actions ${danger ? 'stack' : ''}">
        ${danger
          ? `<a class="btn danger block" href="tel:112">${icon('phone', 'sm')}112에 바로 전화</a>
             <button class="btn danger-outline block" data-send>운영팀에 긴급 신고 접수</button>`
          : `<button class="btn primary block" data-send>신고 접수</button>`}
      </div>`;
    $('[data-close]', panel).addEventListener('click', close);
    $$('[data-tab]', panel).forEach((b) => b.addEventListener('click', () => {
      tab = tabs.find((t) => t.kind === b.dataset.tab);
      cat = tab.categories[0].code;            // 탭을 바꾸면 첫 번째 항목으로
      draw();
      $(`[data-tab="${tab.kind}"]`, panel).focus();
    }));
    $('[role="tablist"]', panel).addEventListener('keydown', (e) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      const i = tabs.indexOf(tab) + (e.key === 'ArrowRight' ? 1 : -1);
      tab = tabs[(i + tabs.length) % tabs.length];
      cat = tab.categories[0].code;
      draw();
      $(`[data-tab="${tab.kind}"]`, panel).focus();
    });
    $$('[data-cat]', panel).forEach((b) => b.addEventListener('click', () => {
      cat = b.dataset.cat;
      $$('[data-cat]', panel).forEach((x) => { x.setAttribute('aria-checked', String(x === b)); x.tabIndex = x === b ? 0 : -1; });
    }));
    $$('[data-target]', panel).forEach((b) => b.addEventListener('click', () => {
      target = Number(b.dataset.target);
      $$('[data-target]', panel).forEach((x) => x.setAttribute('aria-checked', String(x === b)));
    }));
    $('[data-send]', panel).addEventListener('click', send);
  }

  async function send(e) {
    const btn = e.currentTarget;
    if (tab.kind === 'companion' && targets.length > 1 && !target) { toast('누구에 대한 신고인지 골라 주세요', true); return; }
    btn.disabled = true;
    const body = { group_id: groupId, kind: tab.kind, category: cat, note: $('#rp-note', panel).value, target_request_id: target };
    if (tab.kind === 'emergency') {
      const pos = await currentPosition(3000);
      if (pos) Object.assign(body, pos);
    }
    try {
      const r = await api('/api/reports', { method: 'POST', body });
      toast(r.duplicate ? '이미 접수된 신고예요. 운영팀이 확인하고 있어요' : (tab.kind === 'emergency' ? '운영팀에 긴급 신고를 접수했어요' : '신고를 접수했어요'));
      close();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  }

  function close() { ov.remove(); document.removeEventListener('keydown', onKey); prevFocus?.focus?.(); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  draw();
  $('[data-close]', panel).focus();
}

/* 현재 위치. 권한 창에 답하지 않아도 timeout 뒤에는 반드시 끝난다(예전에는 여기서 멈춰 버튼이 안 눌리는 것처럼 보였다). */
function locateMe(timeout = 8000) {
  return new Promise((resolve, reject) => {
    if (!window.isSecureContext) {
      reject(new Error('휴대폰에서 위치를 쓰려면 https 주소로 접속해야 해요(개발 중에는 HTTPS 터널 주소 사용)'));
      return;
    }
    if (!navigator.geolocation) { reject(new Error('이 브라우저는 위치 기능을 지원하지 않아요')); return; }
    const timer = setTimeout(() => reject(new Error('위치를 가져오는 데 시간이 오래 걸려요. 위치 권한을 허용했는지 확인하세요')), timeout);
    navigator.geolocation.getCurrentPosition(
      (p) => { clearTimeout(timer); resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy }); },
      (err) => {
        clearTimeout(timer);
        reject(new Error(err.code === 1 ? '위치 권한이 꺼져 있어요. 브라우저 주소창의 자물쇠 아이콘에서 위치를 허용해 주세요'
          : err.code === 3 ? '위치를 가져오는 데 시간이 오래 걸려요' : '현재 위치를 찾지 못했어요. 실내라면 잠시 뒤 다시 시도하세요'));
      },
      { timeout, maximumAge: 15000, enableHighAccuracy: true });
  });
}
function currentPosition(timeout = 4000) { return locateMe(timeout).catch(() => null); }
async function geoGranted() {
  try { return (await navigator.permissions.query({ name: 'geolocation' })).state === 'granted'; } catch (e) { return false; }
}

/* ---------- 바텀시트 접기/펴기: 손잡이를 누르거나 위아래로 끈다 ---------- */
const SHEET_STATE = { collapsed: false, key: null };
function wireSheet(sheetEl, key) {
  if (SHEET_STATE.key !== key) { SHEET_STATE.key = key; SHEET_STATE.collapsed = false; }
  const btn = $('.handle-btn', sheetEl);
  const apply = () => {
    sheetEl.classList.toggle('collapsed', SHEET_STATE.collapsed);
    btn?.setAttribute('aria-expanded', String(!SHEET_STATE.collapsed));
    btn?.setAttribute('aria-label', SHEET_STATE.collapsed ? '내용 펼치기' : '내용 접기');
  };
  apply();
  if (!btn) return;
  let startY = null, moved = false;
  btn.addEventListener('pointerdown', (e) => { startY = e.clientY; moved = false; btn.setPointerCapture(e.pointerId); });
  btn.addEventListener('pointermove', (e) => {
    if (startY == null) return;
    const dy = e.clientY - startY;
    if (Math.abs(dy) > 24) {
      moved = true;
      const want = dy > 0;
      if (want !== SHEET_STATE.collapsed) { SHEET_STATE.collapsed = want; apply(); }
      startY = e.clientY;
    }
  });
  btn.addEventListener('pointerup', () => { if (!moved) { SHEET_STATE.collapsed = !SHEET_STATE.collapsed; apply(); } startY = null; });
  btn.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); SHEET_STATE.collapsed = !SHEET_STATE.collapsed; apply(); } });
}
const handleHtml = () => '<button class="handle-btn" type="button" aria-expanded="true" aria-label="내용 접기"><span class="handle"></span></button>';

/* =========================================================================
   신뢰도 속도계: 낮으면 빨강, 높으면 초록. 0~100
   ========================================================================= */
const TRUST_BANDS = [
  { max: 20, color: '#D92D20', label: '회복 필요' },
  { max: 40, color: '#F79009', label: '주의' },
  { max: 60, color: '#EAAA08', label: '보통' },
  { max: 80, color: '#66C61C', label: '좋음' },
  { max: 101, color: '#12B76A', label: '매우 좋음' },
];
function trustBand(v) { return TRUST_BANDS.find((b) => (v ?? 0) < b.max) || TRUST_BANDS[TRUST_BANDS.length - 1]; }

function trustGauge(value, { size = 'lg' } = {}) {
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  const cx = 60, cy = 62, r = 48;
  const pt = (p) => { const a = Math.PI * (1 - p / 100); return [cx + r * Math.cos(a), cy - r * Math.sin(a)]; };
  const arc = (a, b) => { const [x1, y1] = pt(a), [x2, y2] = pt(b); return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${r} ${r} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`; };
  let lo = 0;
  const segs = TRUST_BANDS.map((b) => {
    const hi = Math.min(100, b.max);
    const bg = `<path d="${arc(lo, hi)}" stroke="${b.color}" stroke-opacity=".22" stroke-width="11" fill="none"/>`;
    const on = v > lo ? `<path d="${arc(lo, Math.min(v, hi))}" stroke="${b.color}" stroke-width="11" fill="none"/>` : '';
    lo = hi;
    return bg + on;
  }).join('');
  const [nx, ny] = pt(v);
  const band = trustBand(v);
  const big = size === 'lg';
  return `<span class="gauge ${size}" role="img" aria-label="신뢰도 ${v}점, ${band.label}">
    <svg viewBox="0 0 120 ${big ? 78 : 70}" aria-hidden="true">${segs}
      <line x1="${cx}" y1="${cy}" x2="${(cx + (nx - cx) * 0.78).toFixed(2)}" y2="${(cy + (ny - cy) * 0.78).toFixed(2)}" stroke="#0F172A" stroke-width="3" stroke-linecap="round"/>
      <circle cx="${cx}" cy="${cy}" r="5" fill="#0F172A"/>
      ${big ? '<text x="12" y="76" font-size="8" fill="#94A3B8">0</text><text x="100" y="76" font-size="8" fill="#94A3B8">100</text>' : ''}
    </svg>
    <span class="gauge-val" style="color:${band.color}">${v}${big ? `<small>${band.label}</small>` : ''}</span></span>`;
}

/* ---------- 로그인 확인 · 로그아웃 ---------- */
async function requireLogin() {
  try {
    const me = await api('/api/auth/me');
    return me.user;
  } catch (e) {
    location.href = '/';
    throw e;
  }
}
async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* 무시 */ }
  location.href = '/';
}

/* ---------- PWA ---------- */
let installPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installPrompt = e; document.dispatchEvent(new Event('moata:installable')); });
function canInstall() { return !!installPrompt; }
async function promptInstall() {
  if (!installPrompt) { toast('브라우저 메뉴의 "홈 화면에 추가"로 설치할 수 있어요'); return; }
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
}
if ('serviceWorker' in navigator && window.isSecureContext && APP !== 'admin') {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {}));
}
