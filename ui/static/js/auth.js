/* =========================================================================
   MODULE: Auth — login / logout / session init
   ========================================================================= */
const Auth = {
  init() {
    document.getElementById('api-base').value     = State.apiBase;
    document.getElementById('relay-ws-url').value = State.relayWsUrl;
    ['login-user','login-pass','login-totp'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('keydown', e => { if (e.key === 'Enter') this.doLogin(); });
    });
    if (State.accessToken) { this._enterApp(); Poller.refresh(); }
  },
  async doLogin() {
    const btn = document.getElementById('login-btn');
    State.apiBase    = document.getElementById('api-base').value.replace(/\/+$/, '');
    State.relayWsUrl = document.getElementById('relay-ws-url').value.replace(/\/+$/, '');
    localStorage.setItem('nexus_api_base',    State.apiBase);
    localStorage.setItem('nexus_relay_ws_url', State.relayWsUrl);
    const user = document.getElementById('login-user').value.trim();
    const pass = document.getElementById('login-pass').value;
    const totp = document.getElementById('login-totp').value.trim();
    const errEl = document.getElementById('login-error');
    // Reset both error boxes at the start of every attempt
    const certHelper = document.getElementById('cert-helper');
    if (certHelper) certHelper.style.display = 'none';
    if (!user || !pass) { errEl.textContent = 'Username and password are required.'; errEl.style.display = 'block'; return; }
    Utils.setBtnLoading(btn, true, 'Authenticating...');
    errEl.style.display = 'none';
    try {
      const res = await fetch(State.apiBase + '/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user, password: pass, totp_code: totp || null }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({ detail: 'Invalid credentials' }));
        throw new Error(errData.detail || 'Authentication failed');
      }
      const data = await res.json();
      State.accessToken  = data.access_token;
      State.refreshToken = data.refresh_token;
      State.currentUser  = user;
      sessionStorage.setItem('nexus_access_token',  State.accessToken);
      sessionStorage.setItem('nexus_refresh_token', State.refreshToken);
      sessionStorage.setItem('nexus_user', State.currentUser);
      this._enterApp();
      Utils.toast('Authenticated successfully', false, true);
      Poller.refresh();
    } catch (e) {
      errEl.textContent = e.message; errEl.style.display = 'block';
    } finally { Utils.setBtnLoading(btn, false); }
  },
  _enterApp() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app').classList.add('active');
    document.getElementById('conn-label').textContent =
      State.currentUser ? `authenticated · ${State.currentUser}` : 'authenticated';
  },
  doLogout() {
    State.wsIntentionalClose = true;
    State.accessToken = State.refreshToken = State.currentUser = null;
    ['nexus_access_token','nexus_refresh_token','nexus_user'].forEach(k => sessionStorage.removeItem(k));
    if (State.relayWS) { State.relayWS.close(); State.relayWS = null; }
    State.pendingSessionRequest = null;
    State.activeSessionId = State.activeDeviceId = State.activeTerminalId = null;
    State.inputCaptureActive = false;
    document.getElementById('app').classList.remove('active');
    document.getElementById('login-screen').style.display = 'flex';
  },
};

