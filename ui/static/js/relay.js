/* =========================================================================
   MODULE: Relay — single persistent WebSocket, shared across all sessions
   ========================================================================= */
const Relay = {
  _showCertHelper(wssUrl) {
    // Convert wss://host:port → https://host:port so the user can open it
    // in a new tab and accept the self-signed cert exception.
    const httpsUrl = wssUrl.replace(/^wss:\/\//, 'https://').replace(/\/ws$/, '');
    const link = document.getElementById('cert-trust-link');
    const helper = document.getElementById('cert-helper');
    if (link) link.href = httpsUrl;
    if (helper) helper.style.display = 'block';
    // Also hide the generic error box so the amber helper is the focus
    const err = document.getElementById('login-error');
    if (err) err.style.display = 'none';
  },
  setConnPulse(state) {
    const el = document.getElementById('conn-pulse');
    el.className = 'conn-pulse' + (state === 'ok' ? '' : ' ' + state);
    document.getElementById('conn-label').textContent =
      state === 'ok' ? `connected · ${State.currentUser || ''}` :
      state === 'warn' ? 'connecting…' : 'disconnected';
  },
  ensure() {
    if (State.relayWS && State.relayWS.readyState === WebSocket.OPEN)
      return Promise.resolve(true);
    if (State.relayConnectPromise) return State.relayConnectPromise;
    State.relayConnectPromise = new Promise((resolve, reject) => {
      State.wsIntentionalClose = false;
      const ws = new WebSocket(State.relayWsUrl);
      State.relayWS = ws;
      this.setConnPulse('warn');
      let resolved = false;
      ws.onopen = () => { openedAt = Date.now(); ws.send(JSON.stringify({ type: 'auth.controller', token: State.accessToken })); };
      ws.onmessage = (evt) => {
        let data;
        try { data = JSON.parse(evt.data); }
        catch { console.error('Non-JSON WS frame:', evt.data); return; }
        if (data.type === 'auth.ok') {
          State.wsReconnectAttempts = 0;
          this.setConnPulse('ok');
          if (!resolved) { resolved = true; resolve(true); }
          return;
        }
        if (data.type === 'error' && !resolved) { resolved = true; reject(new Error(data.message || 'Relay auth failed')); return; }
        this._route(data);
      };
      let openedAt = null;
      ws.onerror = () => this.setConnPulse('bad');
      ws.onclose = (evt) => {
        this.setConnPulse('bad');
        State.relayWS = null; State.relayConnectPromise = null;
        if (!resolved) {
          resolved = true;
          // A close within ~800 ms of the attempt, before auth.ok, almost always
          // means the browser rejected the self-signed TLS certificate.
          const isWSS = State.relayWsUrl.startsWith('wss://');
          const quickClose = !openedAt || (Date.now() - openedAt) < 800;
          if (isWSS && quickClose) {
            Relay._showCertHelper(State.relayWsUrl);
            reject(new Error('Browser blocked the relay connection — self-signed certificate not trusted. See the helper below.'));
          } else {
            reject(new Error('Relay closed before authenticating'));
          }
        }
        if (State.wsIntentionalClose) return;
        if (State.activeSessionId && State.wsReconnectAttempts < 5) {
          State.wsReconnectAttempts++;
          const delay = Math.min(1000 * State.wsReconnectAttempts, 8000);
          Utils.toast(`Relay lost. Reconnecting in ${Math.round(delay/1000)}s…`, false, false, true);
          setTimeout(() => this.ensure().catch(() => {}), delay);
        } else if (State.activeSessionId) {
          Utils.toast('Could not reconnect to relay. Please reconnect manually.', true);
        }
      };
    }).finally(() => { State.relayConnectPromise = null; });
    return State.relayConnectPromise;
  },
  send(obj) {
    if (!State.relayWS || State.relayWS.readyState !== WebSocket.OPEN) {
      Utils.toast('Not connected to relay', true); return false;
    }
    State.relayWS.send(JSON.stringify(obj)); return true;
  },
  sendSession(type, payload, sessionId) {
    const sid = sessionId || State.activeSessionId;
    if (!sid) { Utils.toast('No active session', true); return false; }
    return this.send({ type, session_id: sid, payload: payload || {} });
  },
  _route(data) {
    switch (data.type) {
      case 'session.pending':
        if (State.pendingSessionRequest) State.pendingSessionRequest.sessionId = data.session_id;
        return;
      case 'session.active':
        if (State.pendingSessionRequest && State.pendingSessionRequest.sessionId === data.session_id) {
          const { resolve, deviceId, sessionId } = State.pendingSessionRequest;
          State.pendingSessionRequest = null;
          resolve({ sessionId, deviceId });
        } else {
          Utils.toast('Session established', false, true);
        }
        return;
      case 'session.rejected':
        if (State.pendingSessionRequest) {
          const { reject } = State.pendingSessionRequest;
          State.pendingSessionRequest = null;
          reject(new Error(data.reason || 'Agent rejected the session'));
        } else {
          Utils.toast('Agent rejected session: ' + (data.reason || 'unknown'), true);
        }
        return;
      case 'session.close':
        Sessions.onRemoteClosed(data.session_id, data.reason); return;
      case 'error':
        if (State.pendingSessionRequest) {
          const { reject } = State.pendingSessionRequest;
          State.pendingSessionRequest = null;
          reject(new Error(data.message || 'Relay error'));
        } else {
          Utils.toast('Relay error: ' + (data.message || 'unknown'), true);
        }
        return;
      default: this._handleSessionMsg(data);
    }
  },
  _handleSessionMsg(data) {
    switch (data.type) {
      case 'screen_frame':    RemoteDesktop.renderFrameHex(data.payload); break;
      case 'camera_frame':    AVControl.renderCameraFrame(data.payload);   break;
      case 'camera_list':     AVControl.onCameraList(data.payload);        break;
      case 'camera_snapshot': AVControl.onCameraSnapshot(data.payload);    break;
      case 'camera_ai_result':AVControl.onCameraAiResult(data.payload);    break;
      case 'av_error':        AVControl.onAvError(data.payload);           break;
      case 'audio_data':
        if (data.payload?.direction === 'agent_mic') AVControl.playAgentAudio(data.payload);
        break;
      case 'terminal_data':   Terminal.appendOutput(data.payload?.data ?? '', 'output'); break;
      case 'terminal_close':
        Terminal.appendOutput('\n[shell session ended]\n', 'system');
        State.activeTerminalId = null;
        document.getElementById('term-input').disabled    = true;
        document.getElementById('term-send-btn').disabled = true;
        break;
      case 'file_list':       FileMgr.renderRemote(data.payload); break;
      case 'file_download_chunk': FileMgr.onDownloadChunk(data.payload); break;
      case 'file_upload_end':     FileMgr.onUploadAck(data.payload);     break;
      case 'clipboard_get':   Clipboard.onRemoteGet(data.payload); break;
      case 'pong': break;
      // Transport-level RTT probe (see agents/adaptive_quality.py) —
      // echo the nonce straight back with no UI involvement so agent-side
      // stream quality tuning gets an accurate round-trip measurement.
      case 'net_probe':
        Relay.sendSession('net_probe_ack', { nonce: data.payload?.nonce }, data.session_id);
        break;
      default: console.debug('Unhandled session msg:', data.type);
    }
  },
};

