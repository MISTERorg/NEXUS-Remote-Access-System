/* =========================================================================
   MODULE: RemoteDesktop — canvas rendering + full keyboard/mouse capture
   =========================================================================
   KEY-CAPTURE DESIGN
   ------------------
   Capture is auto-enabled when the Desktop view opens (Views.show triggers
   setInputCapture(true) after 150ms). Clicking the canvas also enables it.
   Escape always releases capture back to the browser.

   KEY FORWARDING: ALL key combinations — including Ctrl+C, Ctrl+V, Alt+F4,
   etc. — are forwarded directly to the remote with NO interception. The
   remote machine receives exactly what you press, as if you were sitting
   in front of it. Clipboard sync is handled only by the explicit toolbar
   buttons (Pull Clipboard / Push Text), not by keyboard shortcuts.

   PHYSICAL KEY MAPPING (e.code, not e.key):
   e.code is the physical key location and never changes with modifiers.
   e.key is the produced character — Shift+A gives 'A', Shift+1 gives '!'
   — causing double-modifier glitches when the Shift event is also sent.
   Using e.code means we always send the base key ('a', '1') and let the
   agent's pynput combine it with whatever modifiers are already held on
   the remote side.
   ========================================================================= */
const RemoteDesktop = {
  _captureKeydown: null,
  _captureKeyup:   null,

  reset() {
    State.frameTimestamps = [];
    document.getElementById('canvas-empty-state').style.display = 'block';
    document.getElementById('dt-fps-hud').textContent     = '-- FPS';
    document.getElementById('dt-latency-hud').textContent = '-- ms';
    const canvas = document.getElementById('remote-canvas');
    canvas.getContext('2d').fillStyle = '#0F172A';
    canvas.getContext('2d').fillRect(0, 0, canvas.width, canvas.height);
    this._bindPointerHandlers();
    this._updateKbHint();
  },

  requestFrame() {
    Relay.sendSession('screen_request', { quality: 60, scale: 1.0 });
  },

  // ── Physical key mapping (uses e.code, not e.key) ──────────────────
  //
  // WHY e.code and not e.key:
  //   e.key  = the CHARACTER the key produces, which changes with modifiers.
  //            Shift+A → 'A', Shift+1 → '!'. When the dashboard separately
  //            sends a Shift press event AND then sends the character 'A',
  //            pynput on the agent sees: hold Shift, then press 'A'
  //            (which internally adds Shift again) → double-modifier glitch,
  //            wrong output or dropped keystrokes.
  //   e.code = the PHYSICAL KEY LOCATION, unaffected by modifier state.
  //            Shift+A → 'KeyA', Shift+1 → 'Digit1'. The agent presses the
  //            raw physical key ('a', '1') while Shift is already held on the
  //            remote → the OS produces the correct shifted character. ✓
  _keyName(e) {
    // Special keys by e.code
    const codeMap = {
      'Space':        'space',
      'Enter':        'enter',  'NumpadEnter': 'enter',
      'Backspace':    'backspace',
      'Delete':       'delete',
      'Tab':          'tab',
      'Escape':       'escape',
      'Insert':       'insert',
      'Home':         'home',   'End':      'end',
      'PageUp':       'page_up','PageDown': 'page_down',
      'ArrowUp':      'up',     'ArrowDown':  'down',
      'ArrowLeft':    'left',   'ArrowRight': 'right',
      'ShiftLeft':    'shift',  'ShiftRight':    'shift',
      'ControlLeft':  'ctrl',   'ControlRight':  'ctrl',
      'AltLeft':      'alt',    'AltRight':      'alt',
      'MetaLeft':     'cmd',    'MetaRight':     'cmd',
      'CapsLock':     'caps_lock',
      'F1':'f1','F2':'f2','F3':'f3','F4':'f4',
      'F5':'f5','F6':'f6','F7':'f7','F8':'f8',
      'F9':'f9','F10':'f10','F11':'f11','F12':'f12',
      // Numpad digits
      'Numpad0':'0','Numpad1':'1','Numpad2':'2','Numpad3':'3','Numpad4':'4',
      'Numpad5':'5','Numpad6':'6','Numpad7':'7','Numpad8':'8','Numpad9':'9',
      'NumpadMultiply':'*','NumpadAdd':'+','NumpadSubtract':'-',
      'NumpadDecimal':'.','NumpadDivide':'/',
      // Punctuation (base character, not shifted variant)
      'Backquote':'`','Minus':'-','Equal':'=',
      'BracketLeft':'[','BracketRight':']','Backslash':'\\',
      'Semicolon':';','Quote':"'",
      'Comma':',','Period':'.','Slash':'/',
    };
    if (codeMap[e.code]) return codeMap[e.code];
    // Letter keys: 'KeyA' → 'a'  (always lowercase — Shift already sent separately)
    if (/^Key([A-Z])$/.test(e.code)) return e.code[3].toLowerCase();
    // Digit keys: 'Digit3' → '3'  (base digit — Shift already sent separately)
    if (/^Digit(\d)$/.test(e.code)) return e.code[5];
    return null;
  },

  // ── Input capture toggle ────────────────────────────────────────────
  setInputCapture(active) {
    State.inputCaptureActive = active;
    document.getElementById('input-capture-badge').classList.toggle('visible', active);
    document.getElementById('btn-capture-toggle')?.classList.toggle('active', active);
    this._updateKbHint();

    if (active) {
      this._captureKeydown = (e) => {
        if (!State.inputCaptureActive) return;

        // Escape releases capture back to the browser — everything else
        // is forwarded to the remote as-is (no Ctrl+C/V interception).
        if (e.code === 'Escape') { this.setInputCapture(false); return; }

        e.preventDefault();

        // Skip browser key-repeat events: the agent already holds the key
        // down via the first press event; sending duplicate presses causes
        // pynput to fire extra keystrokes on some platforms.
        if (e.repeat) return;

        const key = this._keyName(e);
        if (key) Relay.sendSession('key_event', { action: 'press', key });
      };

      this._captureKeyup = (e) => {
        if (!State.inputCaptureActive) return;
        if (e.code === 'Escape') return;
        e.preventDefault();
        const key = this._keyName(e);
        if (key) Relay.sendSession('key_event', { action: 'release', key });
      };

      document.addEventListener('keydown', this._captureKeydown, true);
      document.addEventListener('keyup',   this._captureKeyup,   true);
    } else {
      if (this._captureKeydown) {
        document.removeEventListener('keydown', this._captureKeydown, true);
        document.removeEventListener('keyup',   this._captureKeyup,   true);
        this._captureKeydown = this._captureKeyup = null;
      }
    }
  },

  toggleInputCapture() { this.setInputCapture(!State.inputCaptureActive); },

  // Show/hide the keyboard hint overlay
  _updateKbHint() {
    const hint = document.getElementById('canvas-kb-hint');
    // Show hint when: session is active, stream is running, but capture is off
    const showHint = State.activeSessionId && !State.inputCaptureActive;
    hint?.classList.toggle('visible', !!showHint);
  },

  // ── Pointer handlers (bound once per canvas) ────────────────────────
  _bindPointerHandlers() {
    if (State.canvasHandlersBound) return;
    State.canvasHandlersBound = true;
    const canvas = document.getElementById('remote-canvas');
    const toRemote = (e) => {
      const rect = canvas.getBoundingClientRect();
      return {
        x: Math.round((e.clientX - rect.left) * (canvas.width  / rect.width)),
        y: Math.round((e.clientY - rect.top)  * (canvas.height / rect.height)),
      };
    };
    const btnName = (e) => e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left';
    canvas.addEventListener('mousedown', (e) => {
      const { x, y } = toRemote(e);
      Relay.sendSession('mouse_event', { action: 'press', x, y, button: btnName(e) });
      if (!State.inputCaptureActive) this.setInputCapture(true);
      canvas.focus();
    });
    canvas.addEventListener('mouseup', (e) => {
      const { x, y } = toRemote(e);
      Relay.sendSession('mouse_event', { action: 'release', x, y, button: btnName(e) });
    });
    canvas.addEventListener('mousemove', (e) => {
      const { x, y } = toRemote(e);
      Relay.sendSession('mouse_event', { action: 'move', x, y });
    });
    canvas.addEventListener('dblclick', (e) => {
      const { x, y } = toRemote(e);
      Relay.sendSession('mouse_event', { action: 'click', x, y, button: btnName(e), clicks: 2 });
    });
    canvas.addEventListener('contextmenu', (e) => e.preventDefault());
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      Relay.sendSession('mouse_event', { action: 'scroll', dx: Math.sign(e.deltaX), dy: -Math.sign(e.deltaY) });
    }, { passive: false });
  },

  // ── Frame rendering ─────────────────────────────────────────────────
  renderFrameHex(payload) {
    if (!payload?.frame) return;
    try {
      const bytes = Utils.hexToBytes(payload.frame);
      this._drawBlob(new Blob([bytes], { type: 'image/jpeg' }), payload.timestamp);
    } catch (e) { console.error('Frame decode error:', e); }
  },
  _drawBlob(blob, serverTs) {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      const canvas = document.getElementById('remote-canvas');
      const ctx    = canvas.getContext('2d');
      if (canvas.width !== img.width || canvas.height !== img.height) {
        canvas.width = img.width; canvas.height = img.height;
      }
      ctx.drawImage(img, 0, 0);
      document.getElementById('canvas-empty-state').style.display = 'none';
      this._updateKbHint();
      if (State.lastFrameObjectUrl) URL.revokeObjectURL(State.lastFrameObjectUrl);
      State.lastFrameObjectUrl = url;
      const now = performance.now();
      State.frameTimestamps.push(now);
      State.frameTimestamps = State.frameTimestamps.filter(t => now - t < 2000);
      document.getElementById('dt-fps-hud').textContent = `${Math.round(State.frameTimestamps.length / 2)} FPS`;
      if (serverTs) {
        const lat = Math.max(0, Math.round(Date.now() - serverTs * 1000));
        document.getElementById('dt-latency-hud').textContent = `${lat} ms`;
      }
    };
    img.onerror = () => URL.revokeObjectURL(url);
    img.src = url;
  },
  toggleFullscreen() {
    const el = document.getElementById('canvas-container');
    if (!document.fullscreenElement) el.requestFullscreen();
    else document.exitFullscreen();
  },
};

