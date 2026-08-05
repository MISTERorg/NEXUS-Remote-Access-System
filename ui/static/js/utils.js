/* =========================================================================
   MODULE: Utils — shared helpers
   ========================================================================= */
const Utils = {
  escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  },
  toast(msg, isErr=false, isOk=false, isWarn=false) {
    const wrap = document.getElementById('toast-wrap');
    const el   = document.createElement('div');
    el.className = 'toast' + (isErr ? ' err' : isOk ? ' ok' : isWarn ? ' warn' : '');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3500);
  },
  setBtnLoading(btn, loading, label) {
    if (!btn) return;
    if (loading) {
      btn.dataset.origHtml = btn.innerHTML;
      btn.innerHTML = `<span class="spinner"></span> ${label || 'Working...'}`;
      btn.disabled = true;
    } else {
      if (btn.dataset.origHtml) btn.innerHTML = btn.dataset.origHtml;
      btn.disabled = false;
    }
  },
  formatBytes(n) {
    if (n === undefined || n === null) return '—';
    const units = ['B','KB','MB','GB'];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  },
  capBadges(caps) {
    if (!caps) return '';
    const map = [
      ['screen_share', '🖥 Screen'], ['remote_input', '⌨ Input'],
      ['terminal','⚡ Shell'], ['file_transfer','📁 Files'], ['clipboard','📋 Clip'],
      ['camera','📹 Cam'], ['audio','🎙 Audio'],
    ];
    return `<div class="cap-badges">${map.map(([k,label]) =>
      `<span class="cap-badge ${caps[k] ? 'on' : ''}">${label}</span>`
    ).join('')}</div>`;
  },
  arrayBufferToBase64(buf) {
    let binary = '';
    const bytes = new Uint8Array(buf);
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk)
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    return btoa(binary);
  },
  hexToBytes(hex) {
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < bytes.length; i++)
      bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    return bytes;
  },
  uuid() {
    return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
  },
  sleep(ms) { return new Promise(r => setTimeout(r, ms)); },
};

