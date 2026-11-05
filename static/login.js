'use strict';
/* 로그인: 학생 탭(학생 → 승객 앱, 운영자 → 운영 페이지) · 기사님 탭(기사님 앱) · 회원가입 */

const L_STATE = { tab: 'student', logged: [] };
const APP_LABEL = { passenger: '승객 앱', driver: '기사님 앱', admin: '운영 페이지' };

async function bootLogin() {
  await loadConfig();
  // 이미 로그인된 앱은 바로 열 수 있게 링크로 보여 준다(같은 브라우저에서 승객·기사님 앱을 함께 시연할 수 있게)
  for (const app of ['passenger', 'driver', 'admin']) {
    try {
      const me = await api('/api/auth/me', { app });
      L_STATE.logged.push({ app, name: me.user.nickname });
    } catch (e) { /* 로그인 안 됨 */ }
  }
  drawLogin();
  // 배경 지도(group 모드): 캠퍼스 정문 → 터미널 → 삼척역 예시 경로
  try {
    const map = await createMap($('#map'));
    const p = Object.fromEntries(CONFIG.demo_places.map((x) => [x.name, x]));
    const pts = [p['강원대학교 삼척캠퍼스 정문'], p['삼척종합버스터미널'], p['삼척역']];
    map.line(pts, { color: BLUE, weight: 6 });
    map.pin(pts[0], pinHtml('o'));
    map.pin(pts[1], pinHtml('n', '2'));
    map.pin(pts[2], pinHtml('n', '3', '', 'drop'));
    map.fit(pts, { top: 40, bottom: 440 });
  } catch (e) { /* 지도 없이도 로그인은 된다 */ }
}

function drawLogin() {
  const student = L_STATE.tab === 'student';
  const card = $('#card');
  card.innerHTML = `
    <div class="login-logo">${logoMark()}<div><b id="login-title">모아타</b><span class="tag">방향이 같으면, 같이 탑니다</span></div></div>
    <div class="seg mt16" role="tablist" aria-label="사용자 유형">
      <button role="tab" aria-selected="${student}" data-tab="student">학생</button>
      <button role="tab" aria-selected="${!student}" data-tab="driver">기사님</button>
    </div>
    <form id="login-form" novalidate>
      <label class="field"><span>${student ? '학교 이메일 또는 아이디' : '아이디'}</span>
        <input class="input" id="email" type="text" autocomplete="username" required autocapitalize="none" spellcheck="false"
          placeholder="${student ? `학번@${esc(CONFIG.student_email_domain)} 또는 user1` : '가입한 아이디'}"></label>
      <label class="field"><span>비밀번호</span>
        <input class="input" id="password" type="password" autocomplete="current-password" required placeholder="비밀번호"></label>
      <button class="btn primary block mt16" type="submit">로그인</button>
    </form>
    <div class="links small">
      <button class="link-btn" id="to-signup">${student ? '학교 메일로 가입하기' : '기사님 가입하기'}</button>
      <span aria-hidden="true">·</span>
      <button class="link-btn muted" id="forgot">비밀번호 찾기</button>
    </div>
    ${L_STATE.logged.length ? `<p class="foot">로그인됨: ${L_STATE.logged.map((l) => `<a href="/${l.app}">${esc(l.name)} · ${APP_LABEL[l.app]} 열기</a>`).join(' · ')}</p>` : ''}
    <p class="foot">강원대학교 삼척캠퍼스 학생 · 등록 기사님 전용${student ? ' · 운영자는 이메일 칸에 운영자 아이디' : ''}</p>`;
  $$('[data-tab]', card).forEach((b) => b.addEventListener('click', () => { L_STATE.tab = b.dataset.tab; drawLogin(); }));
  $('#login-form', card).addEventListener('submit', onLogin);
  $('#to-signup', card).addEventListener('click', () => (student ? openStudentSignup() : openDriverSignup()));
  $('#forgot', card).addEventListener('click', () => toast('이번 버전에서는 운영팀에 문의해 비밀번호를 다시 받으세요'));
}

async function onLogin(e) {
  e.preventDefault();
  const btn = $('button[type=submit]', e.target);
  const login = $('#email').value.trim();
  const password = $('#password').value;
  if (!login || !password) { toast(L_STATE.tab === 'driver' ? '아이디와 비밀번호를 입력하세요' : '이메일과 비밀번호를 입력하세요', true); return; }
  btn.disabled = true;
  try {
    const r = await api('/api/auth/login', { method: 'POST', body: { login, password, tab: L_STATE.tab } });
    location.href = r.redirect;
  } catch (err) { toast(err.message, true); btn.disabled = false; }
}

function signupPanel(title, sub, formHtml, onSubmit) {
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = `<div class="panel" role="dialog" aria-modal="true" aria-labelledby="su-title">
    <div class="panel-head"><div><h2 class="title" id="su-title">${title}</h2><p class="subtitle">${sub}</p></div>
      <button class="close-btn" data-close aria-label="닫기">${icon('x')}</button></div>
    <form class="panel-body" id="su-form" novalidate>${formHtml}
      <button class="btn primary block mt16" type="submit">가입하고 시작하기</button></form></div>`;
  $('#root').appendChild(ov);
  const close = () => ov.remove();
  $('[data-close]', ov).addEventListener('click', close);
  $('#su-form', ov).addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('button[type=submit]', e.target);
    btn.disabled = true;
    try { await onSubmit(ov); } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
  $('input', ov)?.focus();
}

function openStudentSignup() {
  signupPanel('학교 메일로 가입하기', '같은 성별끼리만 동승이 연결돼요. 성별은 가입 후 바꿀 수 없어요.', `
    <label class="field"><span>학교 이메일</span><input class="input" id="su-email" type="email" autocomplete="email" placeholder="학번@${esc(CONFIG.student_email_domain)}"></label>
    <label class="field"><span>비밀번호 (영문·숫자 8자 이상)</span><input class="input" id="su-pw" type="password" autocomplete="new-password"></label>
    <label class="field"><span>닉네임</span><input class="input" id="su-nick" maxlength="20" placeholder="예: 하늘"></label>
    <fieldset class="field" style="border:0;padding:0;margin-inline:0"><legend style="font-size:13px;color:var(--text-2);font-weight:500;margin-bottom:6px">성별</legend>
      <div class="gender"><label><input type="radio" name="g" value="F">여성</label><label><input type="radio" name="g" value="M">남성</label></div></fieldset>
    <p class="notice-line">${icon('info', 'xs')}이메일 형식만 확인해요. 인증 메일은 보내지 않아요.</p>`,
  async (ov) => {
    const gender = $('input[name=g]:checked', ov)?.value;
    if (!gender) throw new Error('성별을 선택하세요');
    const email = $('#su-email', ov).value.trim();
    if (!email.toLowerCase().endsWith('@' + CONFIG.student_email_domain)) throw new Error(`학교 이메일(@${CONFIG.student_email_domain})로 가입하세요`);
    const r = await api('/api/auth/signup', { method: 'POST', app: 'passenger',
      body: { email, password: $('#su-pw', ov).value, nickname: $('#su-nick', ov).value, gender } });
    location.href = r.redirect;
  });
}

function openDriverSignup() {
  signupPanel('기사님 가입하기', '등록한 차량 정보는 승객에게 탑승 전 안내로 보여요.', `
    <label class="field"><span>아이디 (영문 소문자·숫자·_ 4~20자)</span><input class="input" id="su-email" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="20"></label>
    <label class="field"><span>비밀번호 (영문·숫자 8자 이상)</span><input class="input" id="su-pw" type="password" autocomplete="new-password"></label>
    <label class="field"><span>이름</span><input class="input" id="su-name" maxlength="20"></label>
    <label class="field"><span>차량 번호</span><input class="input" id="su-car" maxlength="20" placeholder="예: 강원 31바 1234"></label>
    <div class="row"><label class="field grow"><span>차종</span><input class="input" id="su-model" maxlength="30" placeholder="쏘나타"></label>
      <label class="field grow"><span>색상</span><input class="input" id="su-color" maxlength="20" placeholder="흰색"></label></div>`,
  async (ov) => {
    const r = await api('/api/auth/signup/driver', { method: 'POST', app: 'driver',
      body: { login_id: $('#su-email', ov).value.trim(), password: $('#su-pw', ov).value, name: $('#su-name', ov).value,
        car_number: $('#su-car', ov).value, car_model: $('#su-model', ov).value, car_color: $('#su-color', ov).value } });
    location.href = r.redirect;
  });
}

bootLogin().catch((e) => { $('#card').innerHTML = `<p class="title">시작하지 못했어요</p><p class="subtitle">${esc(e.message)}</p>`; });
