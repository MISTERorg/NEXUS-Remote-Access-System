/* =========================================================================
   MODULE: Terminal — shell, on-screen keyboard, multi-target, maintenance
   =========================================================================
   MULTI-TARGET DESIGN
   -------------------
   State.termTargetIds is a Set of session IDs to send commands to.
     - Empty set → only the current active session (SINGLE mode).
     - Set contains 'broadcast' → all known sessions (BROADCAST mode).
     - Set contains specific session IDs → MULTI mode.

   The "##" prefix still works as a one-shot broadcast regardless of mode.

   MAINTENANCE COMMAND MODE ("!>" prefix OR toolbar toggle)
   ---------------------------------------------------------
   Purpose: trace admin commands in shell history on the remote side.
   When enabled, every command sent is prefixed with a shell comment:
       # [NEXUS-MAINT] <ISO timestamp>
       <actual command>
   The comment is a no-op in any POSIX shell, but it appears in
   `history` output and any shell-level audit logs, making maintenance
   activity clearly distinguishable from normal operator input.

   Activate with the ⚙ Maint Mode toggle button OR by prefixing "!>" inline.
   ========================================================================= */
const Terminal = {
  _history: [],
  _histIdx: -1,
  _panelOpen: false,

  openShell() {
    if (!State.activeSessionId) {
      this.appendOutput('\n// Connect to a device first.\n', 'system'); return;
    }
    State.activeTerminalId = Utils.uuid();
    if (Relay.sendSession('terminal_open', { terminal_id: State.activeTerminalId })) {
      document.getElementById('term-input').disabled    = false;
      document.getElementById('term-send-btn').disabled = false;
      this.appendOutput(`\n// Shell attached to ${State.activeDeviceId}. Waiting for prompt…\n`, 'system');
    }
  },

  // ── On-screen keyboard ──────────────────────────────────────────────
  sendKey(keyName) {
    if (!State.activeSessionId || !State.activeTerminalId) {
      Utils.toast('Attach a shell first', true); return;
    }
    if (keyName.length === 1) {
      Relay.sendSession('terminal_data', { terminal_id: State.activeTerminalId, data: keyName });
    } else {
      Relay.sendSession('key_event', { action: 'press',   key: keyName });
      Relay.sendSession('key_event', { action: 'release', key: keyName });
    }
  },

  sendCombo(modifier, key) {
    if (!State.activeSessionId) { Utils.toast('No active session', true); return; }
    const ctrlChars = {
      c:'\x03', d:'\x04', z:'\x1A', l:'\x0C', r:'\x12',
      a:'\x01', e:'\x05', u:'\x15', w:'\x17', k:'\x0B',
    };
    if (modifier === 'ctrl' && ctrlChars[key] && State.activeTerminalId) {
      Relay.sendSession('terminal_data', { terminal_id: State.activeTerminalId, data: ctrlChars[key] });
      return;
    }
    if (modifier === 'alt' && key === '.' && State.activeTerminalId) {
      Relay.sendSession('terminal_data', { terminal_id: State.activeTerminalId, data: '\x1b.' });
      return;
    }
    Relay.sendSession('key_event', { action: 'press',   key: modifier });
    Relay.sendSession('key_event', { action: 'press',   key });
    Relay.sendSession('key_event', { action: 'release', key });
    Relay.sendSession('key_event', { action: 'release', key: modifier });
  },

  // ── Multi-target panel ──────────────────────────────────────────────
  toggleTargetPanel() {
    this._panelOpen = !this._panelOpen;
    document.getElementById('ttp-dropdown').classList.toggle('open', this._panelOpen);
    document.getElementById('ttp-caret').classList.toggle('open', this._panelOpen);
    if (this._panelOpen) {
      // Close when clicking outside
      const close = (e) => {
        if (!document.getElementById('ttp-wrapper').contains(e.target)) {
          this.closeTargetPanel();
          document.removeEventListener('click', close, true);
        }
      };
      setTimeout(() => document.addEventListener('click', close, true), 0);
    }
  },
  closeTargetPanel() {
    this._panelOpen = false;
    document.getElementById('ttp-dropdown').classList.remove('open');
    document.getElementById('ttp-caret').classList.remove('open');
  },

  // "All Sessions" checkbox toggled
  onAllTargetChange(chk) {
    if (chk.checked) {
      State.termTargetIds = new Set(['broadcast']);
      // Check all individual session boxes too
      document.querySelectorAll('#ttp-session-list input[type="checkbox"]')
        .forEach(el => { el.checked = true; el.disabled = true; });
    } else {
      State.termTargetIds = new Set();
      document.querySelectorAll('#ttp-session-list input[type="checkbox"]')
        .forEach(el => { el.checked = false; el.disabled = false; });
    }
    this._syncTargetBadge();
  },

  // Individual session checkbox toggled
  onSessionTargetChange(sessionId, chk) {
    if (chk.checked) State.termTargetIds.add(sessionId);
    else             State.termTargetIds.delete(sessionId);
    // Sync "all" checkbox
    const allChk = document.getElementById('ttp-check-all');
    const total  = State.sessionsCache.length;
    const selected = [...State.termTargetIds].filter(id => id !== 'broadcast').length;
    allChk.checked = selected === total && total > 0;
    this._syncTargetBadge();
  },

  _syncTargetBadge() {
    const badge    = document.getElementById('term-mode-badge');
    const display  = document.getElementById('ttp-display-text');
    const isBroadcast = State.termTargetIds.has('broadcast');
    const count    = isBroadcast ? State.sessionsCache.length
                   : State.termTargetIds.size;

    if (isBroadcast || count === State.sessionsCache.length && count > 0) {
      badge.className = 'term-mode-badge broadcast';
      badge.textContent = '📡 BROADCAST';
      display.textContent = `All ${State.sessionsCache.length} session${State.sessionsCache.length !== 1 ? 's' : ''}`;
    } else if (count === 0) {
      badge.className = 'term-mode-badge';
      badge.textContent = 'SINGLE';
      display.textContent = State.activeDeviceId ? `${State.activeDeviceId} (active)` : 'Active Session';
    } else {
      badge.className = 'term-mode-badge multi';
      badge.textContent = `MULTI (${count})`;
      const names = State.sessionsCache
        .filter(s => State.termTargetIds.has(s.session_id))
        .map(s => s.device_id).join(', ');
      display.textContent = names.length > 30 ? names.slice(0, 27) + '…' : names;
    }
    this._updateInputMode();
  },

  // Rebuild the session checkbox list
  _refreshTargetPanel() {
    const list = document.getElementById('ttp-session-list');
    if (!State.sessionsCache.length) {
      list.innerHTML = '<div class="ttp-empty">No sessions — connect to a device first.</div>';
      State.termTargetIds.clear();
      this._syncTargetBadge();
      return;
    }
    // Remove stale session IDs from targets
    const validIds = new Set(State.sessionsCache.map(s => s.session_id));
    for (const id of [...State.termTargetIds]) {
      if (id !== 'broadcast' && !validIds.has(id)) State.termTargetIds.delete(id);
    }
    const isBroadcast = State.termTargetIds.has('broadcast');
    list.innerHTML = State.sessionsCache.map(s => `
      <label class="ttp-item">
        <input type="checkbox"
          ${State.termTargetIds.has(s.session_id) || isBroadcast ? 'checked' : ''}
          ${isBroadcast ? 'disabled' : ''}
          onchange="Terminal.onSessionTargetChange('${Utils.escapeHtml(s.session_id)}', this)">
        <div>
          <div class="ttp-devname">${Utils.escapeHtml(s.device_id)}</div>
          <div class="ttp-sessid">${Utils.escapeHtml(s.session_id.slice(0, 8))}…</div>
        </div>
      </label>
    `).join('');
    document.getElementById('ttp-check-all').checked  = isBroadcast;
    document.getElementById('ttp-check-all').disabled = false;
    this._syncTargetBadge();
  },

  // ── Maintenance mode ────────────────────────────────────────────────
  toggleMaintMode() {
    State.termMaintMode = !State.termMaintMode;
    document.getElementById('btn-maint-toggle').classList.toggle('active', State.termMaintMode);
    this._updateInputMode();
    if (State.termMaintMode)
      Utils.toast('⚙ Maintenance mode ON — commands logged as # [NEXUS-MAINT]', false, false, true);
  },

  _updateInputMode() {
    const prompt  = document.getElementById('term-prompt-glyph');
    const sendBtn = document.getElementById('term-send-btn');
    const isBroadcast = State.termTargetIds.has('broadcast') ||
      (State.termTargetIds.size > 0 && !State.termTargetIds.has('broadcast') &&
       State.termTargetIds.size === State.sessionsCache.length && State.sessionsCache.length > 0);
    const isMulti = State.termTargetIds.size > 0 && !isBroadcast;
    const isMaint = State.termMaintMode;

    if (isMaint) {
      prompt.className  = 'term-prompt maint';
      prompt.textContent = '⚙';
      sendBtn.className  = 'term-send-btn maint';
      sendBtn.textContent = 'Maint';
    } else if (isBroadcast) {
      prompt.className  = 'term-prompt broadcast';
      prompt.textContent = '📡';
      sendBtn.className  = 'term-send-btn broadcast';
      sendBtn.textContent = 'Broadcast';
    } else if (isMulti) {
      prompt.className  = 'term-prompt';
      prompt.textContent = '⚡';
      sendBtn.className  = 'term-send-btn';
      sendBtn.textContent = `Send (${State.termTargetIds.size})`;
    } else {
      prompt.className  = 'term-prompt';
      prompt.textContent = '❯';
      sendBtn.className  = 'term-send-btn';
      sendBtn.textContent = 'Send';
    }
  },

  // ── Input box handlers ──────────────────────────────────────────────
  onInputKeydown(e) {
    if (e.key === 'Enter') { e.preventDefault(); this.send(); return; }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (this._histIdx < this._history.length - 1) {
        this._histIdx++;
        document.getElementById('term-input').value =
          this._history[this._history.length - 1 - this._histIdx] || '';
      }
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (this._histIdx > 0) {
        this._histIdx--;
        document.getElementById('term-input').value =
          this._history[this._history.length - 1 - this._histIdx] || '';
      } else {
        this._histIdx = -1;
        document.getElementById('term-input').value = '';
      }
    }
  },

  onInputChange(e) {
    const val = e.target.value;
    // Detect inline overrides
    const forceBroadcast = val.startsWith('##');
    const forceMaint     = val.startsWith('!>');
    if (forceBroadcast || forceMaint) this._updateInputMode(); // let mode show
  },

  send() {
    const inp = document.getElementById('term-input');
    const raw = inp.value;
    if (!raw.trim()) return;
    inp.value = '';
    this._histIdx = -1;

    // Detect inline prefix overrides
    const isBroadcastPrefix = raw.startsWith('##');
    const isMaintPrefix     = raw.startsWith('!>');
    let cmd = raw;
    if (isBroadcastPrefix) cmd = raw.slice(2).trimStart();
    if (isMaintPrefix)     cmd = raw.slice(2).trimStart();
    if (!cmd) return;

    if (!State.activeSessionId) {
      Utils.toast('No active session — connect to a device first', true); return;
    }
    if (!State.activeTerminalId) this.openShell();

    this._history.push(cmd);
    if (this._history.length > 100) this._history.shift();

    const broadcastMode = isBroadcastPrefix || State.termTargetIds.has('broadcast') ||
      (State.termTargetIds.size === State.sessionsCache.length && State.sessionsCache.length > 0);
    const maintMode = isMaintPrefix || State.termMaintMode;

    // Build the actual payload to send to the shell
    const shellPayload = maintMode
      ? `# [NEXUS-MAINT] ${new Date().toISOString()}\n${cmd}\n`
      : `${cmd}\n`;

    if (broadcastMode) {
      this._sendToSessions(shellPayload, State.sessionsCache.map(s => s.session_id), cmd, 'broadcast', maintMode);
    } else if (State.termTargetIds.size > 0) {
      const targets = State.sessionsCache
        .filter(s => State.termTargetIds.has(s.session_id))
        .map(s => s.session_id);
      this._sendToSessions(shellPayload, targets, cmd, 'multi', maintMode);
    } else {
      // Single / active session
      if (!State.activeTerminalId) return;
      Relay.sendSession('terminal_data', { terminal_id: State.activeTerminalId, data: shellPayload });
      const lineType = maintMode ? 'maint' : 'cmd';
      this.appendOutput(`${maintMode ? '⚙' : '❯'} ${cmd}\n`, lineType);
    }
  },

  _sendToSessions(shellPayload, sessionIds, displayCmd, mode, isMaint) {
    let sent = 0;
    for (const sid of sessionIds) {
      // Use activeTerminalId for the active session; others fall back to it
      const tid = State.activeTerminalId;
      if (!tid) continue;
      Relay.sendSession('terminal_data', { terminal_id: tid, data: shellPayload }, sid);
      sent++;
    }
    const prefix = mode === 'broadcast' ? `📡 [→ ${sent}]` : `⚡ [→ ${sent}]`;
    const lineType = mode === 'broadcast' ? 'broadcast' : 'multi';
    if (isMaint) this.appendOutput(`⚙ [MAINT ${prefix}] ${displayCmd}\n`, 'maint');
    else         this.appendOutput(`${prefix} ${displayCmd}\n`, lineType);
    if (sent === 0) Utils.toast('No shells open on any selected session — attach shells first', false, false, true);
  },

  appendOutput(text, type = 'output') {
    const out  = document.getElementById('term-output');
    const span = document.createElement('span');
    span.className = type === 'cmd'       ? 'term-line-cmd'
                   : type === 'broadcast' ? 'term-line-broadcast'
                   : type === 'multi'     ? 'term-line-multi'
                   : type === 'maint'     ? 'term-line-maint'
                   : type === 'system'    ? 'term-line-system'
                   : '';
    span.textContent = text;
    out.appendChild(span);
    out.scrollTop = out.scrollHeight;
  },
  clear() { document.getElementById('term-output').innerHTML = ''; },
};

