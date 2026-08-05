/* =========================================================================
   MODULE: API — REST client with 401 → refresh → retry-once
   ========================================================================= */
const API = {
  async call(path, opts = {}, _retried = false) {
    const headers = opts.headers || {};
    if (State.accessToken) headers['Authorization'] = 'Bearer ' + State.accessToken;
    if (opts.body && !(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    let res;
    try { res = await fetch(State.apiBase + path, { ...opts, headers }); }
    catch (netErr) { throw new Error('Network error: ' + netErr.message); }
    if (res.status === 401 && State.refreshToken && !_retried) {
      const ok = await this._tryRefresh();
      if (ok) return this.call(path, opts, true);
      Auth.doLogout(); throw new Error('Session expired.');
    }
    if (!res.ok) {
      const errData = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(errData.detail || 'API request failed');
    }
    return res.status === 204 ? null : res.json();
  },
  async _tryRefresh() {
    try {
      const res = await fetch(State.apiBase + '/auth/refresh', {
        method: 'POST', headers: { 'Authorization': 'Bearer ' + State.refreshToken },
      });
      if (!res.ok) return false;
      const data = await res.json();
      State.accessToken = data.access_token;
      sessionStorage.setItem('nexus_access_token', State.accessToken);
      return true;
    } catch { return false; }
  },
};

