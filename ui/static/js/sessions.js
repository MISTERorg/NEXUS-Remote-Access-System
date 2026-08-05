/* =========================================================================
   MODULE: Sessions — request, close, tab management
   ========================================================================= */
const Sessions = {
  quickConnect() {
    const target = document.getElementById('qc-device-id').value.trim();
    if (!target) { Utils.toast('Enter a Device ID first', true); return; }
    const mode = document.querySelector('input[name="qc-mode"]:checked').value;
    this.startControl(target, mode);
  },
  async startControl(deviceId, mode = 'desktop') {
    const dev = State.devicesCache.find(d => d.device_id === deviceId);
    if (dev?.status === 'offline') { Utils.toast(`${deviceId} is offline`, true); return; }
    if (dev?.status === 'busy')    { Utils.toast(`${deviceId} is already in a session`, true); return; }
    if (State.pendingSessionRequest) { Utils.toast('A connection request is already in progress', true); return; }
    const btn = document.getElementById('qc-connect-btn');
    Utils.setBtnLoading(btn, true, 'Connecting...');
    try {
      await Relay.ensure();
      const { sessionId } = await this._requestSession(deviceId);
      State.activeSessionId  = sessionId;
      State.activeDeviceId   = deviceId;
      State.activeTerminalId = null;
      Utils.toast(`Session active: ${sessionId.slice(0,8)}…`, false, true);
      this._addTab(deviceId, sessionId);
      Terminal._refreshTargetPanel();
      if (mode === 'desktop') {
        Views.show('desktop');
        document.getElementById('dt-target-name').textContent = deviceId;
        RemoteDesktop.reset();
        RemoteDesktop.requestFrame();
      } else if (mode === 'files') {
        Views.show('files');
        document.getElementById('files-session-select').value = sessionId;
        FileMgr.onSessionChange();
      } else {
        Views.show('terminal');
      }
    } catch(e) {
      Utils.toast('Connection failed: ' + e.message, true);
    } finally {
      Utils.setBtnLoading(btn, false);
    }
  },
  _requestSession(deviceId) {
    return new Promise((resolve, reject) => {
      if (!State.relayWS || State.relayWS.readyState !== WebSocket.OPEN) {
        reject(new Error('Not connected to relay')); return;
      }
      State.pendingSessionRequest = { deviceId, sessionId: null, resolve, reject };
      State.relayWS.send(JSON.stringify({ type: 'session.request', device_id: deviceId }));
    });
  },
  onRemoteClosed(sessionId, reason) {
    document.getElementById(`tab-sess-${sessionId}`)?.remove();
    if (sessionId !== State.activeSessionId) { Poller.refresh(); return; }
    Utils.toast(`Session ended (${reason || 'closed'})`, reason === 'timeout');
    State.activeSessionId = State.activeDeviceId = State.activeTerminalId = null;
    State.inputCaptureActive = false;
    AVControl.onSessionEnd();
    document.getElementById('term-input').disabled    = true;
    document.getElementById('term-send-btn').disabled = true;
    RemoteDesktop.reset();
    RemoteDesktop.setInputCapture(false);
    Terminal._refreshTargetPanel();
    Poller.refresh();
    const onDesktop  = document.getElementById('view-desktop').classList.contains('active');
    const onTerminal = document.getElementById('view-terminal').classList.contains('active');
    if (onDesktop || onTerminal) Views.show('overview');
  },
  _addTab(deviceId, sessionId) {
    document.querySelectorAll('.tab-item').forEach(t => t.classList.remove('active'));
    const bar = document.getElementById('session-tabs-bar');
    if (document.getElementById(`tab-sess-${sessionId}`)) {
      document.getElementById(`tab-sess-${sessionId}`).classList.add('active'); return;
    }
    const tab = document.createElement('div');
    tab.className = 'tab-item active';
    tab.id = `tab-sess-${sessionId}`;
    tab.innerHTML = `<span>${Utils.escapeHtml(deviceId)}</span>
      <span class="close-tab" onclick="Sessions.closeTab('${Utils.escapeHtml(sessionId)}', event)">✕</span>`;
    tab.addEventListener('click', e => {
      if (e.target.classList.contains('close-tab')) return;
      this.switchTo(sessionId, deviceId);
    });
    bar.appendChild(tab);
  },
  switchTo(sessionId, deviceId) {
    State.activeSessionId  = sessionId;
    State.activeDeviceId   = deviceId;
    State.activeTerminalId = null;
    document.querySelectorAll('.tab-item').forEach(t => t.classList.remove('active'));
    document.getElementById(`tab-sess-${sessionId}`)?.classList.add('active');
    Views.show('desktop');
    document.getElementById('dt-target-name').textContent = deviceId;
    RemoteDesktop.reset();
    RemoteDesktop.requestFrame();
    Terminal._refreshTargetPanel();
  },
  closeTab(sessionId, ev) {
    if (ev) ev.stopPropagation();
    Relay.send({ type: 'session.close', session_id: sessionId });
    document.getElementById(`tab-sess-${sessionId}`)?.remove();
    if (sessionId === State.activeSessionId) {
      State.activeSessionId = State.activeDeviceId = State.activeTerminalId = null;
      RemoteDesktop.setInputCapture(false);
    }
    Utils.toast('Session terminated');
    Terminal._refreshTargetPanel();
    Poller.refresh();
    Views.show('overview');
  },
  disconnectActive() {
    if (State.activeSessionId) this.closeTab(State.activeSessionId);
  },
  populateSelects() {
    const opts = '<option value="">Select Target Session...</option>' +
      State.sessionsCache.map(s =>
        `<option value="${Utils.escapeHtml(s.session_id)}">${Utils.escapeHtml(s.device_id)} (${Utils.escapeHtml(s.session_id.slice(0,8))})</option>`
      ).join('');
    const sel = document.getElementById('files-session-select');
    const prev = sel.value;
    sel.innerHTML = opts;
    if (prev) sel.value = prev;
  },
};

