/* =========================================================================
   MODULE: AVControl — Camera + bidirectional audio
   =========================================================================

   CAMERA  ─────────────────────────────────────────────────────────────────
   Sends camera_start → agent captures webcam → streams CAMERA_FRAME msgs
   (same hex-JPEG format as SCREEN_FRAME). Rendered onto a <canvas>.

   AUDIO  ─────────────────────────────────────────────────────────────────
   Two independent half-duplex channels, either or both can run at once:

   LISTEN (agent mic → my speaker)
     → sends audio_start { direction:'listen' }
     → agent streams AUDIO_DATA { direction:'agent_mic', data:<b64 pcm> }
     → AudioPlayer queues/schedules each Int16 PCM chunk via Web Audio API

   SPEAK (my mic → agent speaker)
     → requests getUserMedia({ audio: { echoCancellation:true } })
     → sends audio_start { direction:'speak' }
     → ScriptProcessor captures mic → converts float32→int16 → b64
     → sends AUDIO_DATA { direction:'controller_mic', data:<b64> } continuously

   AUDIO FORMAT (both directions)
     Sample rate : 16 000 Hz  (set via AudioContext constructor)
     Channels    : 1 (mono)
     Bit depth   : Int16 PCM, little-endian, base64-encoded per packet

   PUSH-TO-TALK
     While the AV view is active and speak mode is running, the Space bar
     toggles the outgoing mic mute (Space down = unmute, Space up = mute).
     When muted the mic processor still runs but packets are dropped.
   ========================================================================= */

const AVControl = (() => {

  // ── Internal state ────────────────────────────────────────────────────────
  const S = {
    // camera
    cameraActive:   false,
    camFrameTs:     [],            // for FPS calculation
    camLastObjUrl:  null,
    camRecorder:    null,          // MediaRecorder instance, while recording
    camRecordChunks: [],           // recorded blob chunks
    camRecording:   false,

    // listen (agent→me)
    listenActive:   false,
    audioPlayer:    null,          // AudioPlayer instance
    listenAnalyser: null,

    // speak (me→agent)
    speakActive:    false,
    micStream:      null,          // MediaStream
    micCtx:         null,          // AudioContext
    micProcessor:   null,          // ScriptProcessorNode
    micMuted:       false,         // PTT mute flag
    speakAnalyser:  null,

    // VU animation
    vuRafId:        null,
  };

  // ── Tiny Web Audio PCM player ─────────────────────────────────────────────
  class AudioPlayer {
    constructor(sampleRate = 16000) {
      this.ctx          = new (window.AudioContext || window.webkitAudioContext)({ sampleRate });
      this.nextPlayTime = 0;
      this.analyser     = this.ctx.createAnalyser();
      this.analyser.fftSize = 256;
      this.analyser.connect(this.ctx.destination);
    }

    enqueue(base64Pcm) {
      if (this.ctx.state === 'suspended') this.ctx.resume();
      try {
        // Decode base64 → ArrayBuffer → Int16 → Float32
        const raw     = atob(base64Pcm);
        const bytes   = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
        const i16     = new Int16Array(bytes.buffer);
        const f32     = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0;

        const buf = this.ctx.createBuffer(1, f32.length, this.ctx.sampleRate);
        buf.getChannelData(0).set(f32);

        const src = this.ctx.createBufferSource();
        src.buffer = buf;
        src.connect(this.analyser);

        const now   = this.ctx.currentTime;
        const start = Math.max(now, this.nextPlayTime);
        src.start(start);
        this.nextPlayTime = start + buf.duration;
      } catch (e) {
        console.error('AVControl.AudioPlayer.enqueue error:', e);
      }
    }

    getRMSLevel() {
      if (!this.analyser) return 0;
      const data = new Uint8Array(this.analyser.frequencyBinCount);
      this.analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) { const s = (v - 128) / 128; sum += s * s; }
      return Math.sqrt(sum / data.length);
    }

    close() { try { this.ctx.close(); } catch {} }
  }

  // ── VU meter animation ────────────────────────────────────────────────────
  function _vuTick() {
    // Listen VU — from player analyser
    if (S.listenActive && S.audioPlayer) {
      const lvl = Math.min(1, S.audioPlayer.getRMSLevel() * 6);
      const pct = Math.round(lvl * 100);
      document.getElementById('vu-listen').style.width      = pct + '%';
      document.getElementById('vu-listen-pct').textContent  = pct + '%';
    } else {
      document.getElementById('vu-listen').style.width     = '0%';
      document.getElementById('vu-listen-pct').textContent = '0%';
    }
    // Speak VU — from mic analyser
    if (S.speakActive && S.speakAnalyser) {
      const data = new Uint8Array(S.speakAnalyser.frequencyBinCount);
      S.speakAnalyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) { const s = (v - 128) / 128; sum += s * s; }
      const lvl = Math.min(1, Math.sqrt(sum / data.length) * 6);
      const pct = S.micMuted ? 0 : Math.round(lvl * 100);
      document.getElementById('vu-speak').style.width      = pct + '%';
      document.getElementById('vu-speak-pct').textContent  = pct + '%';
    } else {
      document.getElementById('vu-speak').style.width     = '0%';
      document.getElementById('vu-speak-pct').textContent = '0%';
    }
    S.vuRafId = requestAnimationFrame(_vuTick);
  }

  function _startVU() { if (!S.vuRafId) S.vuRafId = requestAnimationFrame(_vuTick); }
  function _stopVU()  {
    if (S.vuRafId) { cancelAnimationFrame(S.vuRafId); S.vuRafId = null; }
  }

  // ── UI helpers ────────────────────────────────────────────────────────────
  function _setBtn(id, on, label) {
    const b = document.getElementById(id);
    if (!b) return;
    b.classList.toggle('on', on);
    b.textContent = label;
  }

  function _setStatus(text) {
    const el = document.getElementById('audio-status-badge');
    if (el) el.textContent = text;
  }

  function _camStatus(text, level = 'info') {
    const el = document.getElementById('cam-status');
    if (!el) return;
    el.textContent = text;
    // Colour-code by severity so errors are immediately visible
    el.style.color = level === 'error' ? 'var(--red)'
                   : level === 'warn'  ? 'var(--amber)'
                   : level === 'ok'    ? 'var(--green)'
                   : 'var(--text-faint)';
  }

  // ── CAMERA ────────────────────────────────────────────────────────────────
  // Re-probes the connected device for cameras (never a static/hardcoded
  // list) and repopulates the dropdown via onCameraList() below. Called
  // automatically on entering the AV tab (see views.js) and by the ⟳
  // refresh button so newly-plugged-in cameras show up without reloading.
  function refreshCameraList() {
    if (!State.activeSessionId) { Utils.toast('Connect to a device first', true); return; }
    const sel = document.getElementById('cam-device');
    if (sel) sel.innerHTML = '<option value="0">Detecting cameras…</option>';
    Relay.sendSession('camera_list', {});
  }

  function cameraStart() {
    if (!State.activeSessionId) { Utils.toast('Connect to a device first', true); return; }
    const dev = parseInt(document.getElementById('cam-device')?.value || '0', 10);
    Relay.sendSession('camera_start', { device: dev });
    S.cameraActive = true;
    document.getElementById('btn-cam-start').disabled = true;
    document.getElementById('btn-cam-stop').disabled  = false;
    _camStatus('Opening camera… (may take a few seconds)', 'info');
    _startVU();
  }

  function cameraStop() {
    if (S.camRecording) _stopCameraRecording();
    Relay.sendSession('camera_stop', {});
    S.cameraActive = false;
    document.getElementById('btn-cam-start').disabled = false;
    document.getElementById('btn-cam-stop').disabled  = true;
    document.getElementById('cam-canvas').style.display = 'none';
    document.getElementById('cam-empty').style.display  = 'block';
    document.getElementById('cam-fps').textContent = '-- FPS';
    _camStatus('Inactive', 'info');
  }

  // Called by relay when the agent sends its camera device list
  function onCameraList(payload) {
    const cameras = payload?.cameras ?? [];
    const sel     = document.getElementById('cam-device');
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = cameras.length
      ? cameras.map(c => `<option value="${c.id}">${Utils.escapeHtml(c.name)}</option>`).join('')
      : '<option value="0">Camera 0 (default)</option>';
    // Restore previous selection if it still exists
    if (cameras.some(c => String(c.id) === String(prev))) sel.value = prev;
    if (cameras.length > 0) {
      _camStatus(`${cameras.length} camera${cameras.length > 1 ? 's' : ''} detected`, 'info');
    } else {
      _camStatus('No cameras detected on this device', 'warn');
    }
  }

  // Called by relay when the agent sends av_error
  function onAvError(payload) {
    const src = payload?.source ?? 'av';
    const msg = payload?.message ?? 'Unknown AV error';
    // Show error in the camera status area
    _camStatus(`⚠ ${msg}`, 'error');
    // Also re-enable the Start button so the user can retry
    if (src === 'camera') {
      S.cameraActive = false;
      document.getElementById('btn-cam-start').disabled = false;
      document.getElementById('btn-cam-stop').disabled  = true;
      if (S.camRecording) _stopCameraRecording();
    }
    Utils.toast(`AV error (${src}): ${msg}`, true);
    console.error('av_error from agent:', payload);
  }

  function renderCameraFrame(payload) {
    if (!payload?.frame) return;
    try {
      const bytes = Utils.hexToBytes(payload.frame);
      const blob  = new Blob([bytes], { type: 'image/jpeg' });
      const url   = URL.createObjectURL(blob);
      const img   = new Image();
      img.onload = () => {
        const canvas = document.getElementById('cam-canvas');
        const ctx    = canvas.getContext('2d');
        if (canvas.width !== img.width || canvas.height !== img.height) {
          canvas.width = img.width; canvas.height = img.height;
        }
        ctx.drawImage(img, 0, 0);
        canvas.style.display = 'block';
        document.getElementById('cam-empty').style.display = 'none';
        if (S.camLastObjUrl) URL.revokeObjectURL(S.camLastObjUrl);
        S.camLastObjUrl = url;
        // FPS counter
        const now = performance.now();
        S.camFrameTs.push(now);
        S.camFrameTs = S.camFrameTs.filter(t => now - t < 2000);
        document.getElementById('cam-fps').textContent =
          `${Math.round(S.camFrameTs.length / 2)} FPS`;
        _camStatus('Streaming', 'ok');
      };
      img.onerror = () => URL.revokeObjectURL(url);
      img.src = url;
    } catch (e) { console.error('AVControl.renderCameraFrame:', e); }
  }

  // ── Small local helper: trigger a browser file download from a Blob ────────
  function _downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function _timestampForFilename() {
    return new Date().toISOString().replace(/[:.]/g, '-');
  }

  // ── SNAPSHOT ─────────────────────────────────────────────────────────────
  // Requests one full-quality frame straight from the camera (independent
  // of the lower-res/lower-quality live stream) — see agents/camera.py's
  // CAMERA_SNAPSHOT handler. Works whether or not the stream is running.
  function cameraSnapshot() {
    if (!State.activeSessionId) { Utils.toast('Connect to a device first', true); return; }
    const dev = parseInt(document.getElementById('cam-device')?.value || '0', 10);
    Relay.sendSession('camera_snapshot', { device: dev });
    _camStatus('Capturing snapshot…', 'info');
  }

  // Called by relay when the agent responds with the captured frame
  function onCameraSnapshot(payload) {
    if (!payload?.frame) return;
    try {
      const bytes = Utils.hexToBytes(payload.frame);
      const blob  = new Blob([bytes], { type: 'image/jpeg' });
      _downloadBlob(blob, `nexus-snapshot-${_timestampForFilename()}.jpg`);
      _camStatus('Snapshot saved', 'ok');
      Utils.toast('Snapshot saved', false, true);
    } catch (e) {
      console.error('AVControl.onCameraSnapshot:', e);
      _camStatus('Snapshot failed', 'error');
    }
  }

  // ── RECORD ───────────────────────────────────────────────────────────────
  // Records exactly what's on screen: captures the <canvas> that
  // renderCameraFrame() is already drawing incoming CAMERA_FRAME messages
  // onto, via canvas.captureStream() + MediaRecorder. This needs no new
  // wire protocol or agent-side work — it records the live stream as
  // displayed, same idea as recording your own screen during a call.
  // (A higher-fidelity agent-side recording feature — saving straight from
  // the source camera rather than the compressed stream — would be a
  // separate, larger feature; see README.md's camera notes.)
  function toggleCameraRecording() {
    if (S.camRecording) {
      _stopCameraRecording();
    } else {
      _startCameraRecording();
    }
  }

  function _startCameraRecording() {
    if (!S.cameraActive) { Utils.toast('Start the camera before recording', true); return; }
    const canvas = document.getElementById('cam-canvas');
    if (!canvas || typeof canvas.captureStream !== 'function') {
      Utils.toast('Recording is not supported in this browser', true);
      return;
    }
    let stream;
    try {
      stream = canvas.captureStream(15); // match-ish the agent's ~10-15fps stream
    } catch (e) {
      Utils.toast('Could not start recording: ' + e.message, true);
      return;
    }
    const mimeType = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']
      .find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t));
    if (!mimeType) { Utils.toast('No supported recording format in this browser', true); return; }

    S.camRecordChunks = [];
    S.camRecorder = new MediaRecorder(stream, { mimeType });
    S.camRecorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) S.camRecordChunks.push(e.data); };
    S.camRecorder.onstop = () => {
      const blob = new Blob(S.camRecordChunks, { type: mimeType });
      S.camRecordChunks = [];
      _downloadBlob(blob, `nexus-recording-${_timestampForFilename()}.webm`);
      _camStatus('Recording saved', 'ok');
      Utils.toast('Recording saved', false, true);
    };
    S.camRecorder.start();
    S.camRecording = true;
    const btn = document.getElementById('btn-cam-record');
    if (btn) { btn.classList.add('on', 'red'); btn.textContent = '⏹ Stop Recording'; }
    _camStatus('Recording…', 'info');
  }

  function _stopCameraRecording() {
    if (S.camRecorder && S.camRecorder.state !== 'inactive') S.camRecorder.stop();
    S.camRecording = false;
    const btn = document.getElementById('btn-cam-record');
    if (btn) { btn.classList.remove('on', 'red'); btn.textContent = '⏺ Record'; }
  }

  // Called by relay when the agent sends per-frame AI metadata (see
  // agents/camera.py's AIFrameProcessor extension point). No AI processor
  // ships by default, so this is currently a no-op landing spot — wire up
  // a UI overlay/badge here once a real processor is registered agent-side.
  function onCameraAiResult(payload) {
    console.debug('camera_ai_result:', payload?.result);
  }

  // ── LISTEN (agent mic → my speaker) ──────────────────────────────────────
  async function toggleListen() {
    if (S.listenActive) {
      _stopListen();
    } else {
      await _startListen();
    }
  }

  async function _startListen() {
    if (!State.activeSessionId) { Utils.toast('Connect to a device first', true); return; }
    S.audioPlayer = new AudioPlayer(16000);
    S.listenActive = true;
    Relay.sendSession('audio_start', { direction: 'listen' });
    _setBtn('btn-listen-toggle', true, '■ Stop Listening');
    _setStatus(_speakRunning() ? 'DUPLEX' : 'LISTEN');
    _startVU();
    Utils.toast('Listening to agent mic…', false, true);
  }

  function _stopListen() {
    S.listenActive = false;
    if (S.audioPlayer) { S.audioPlayer.close(); S.audioPlayer = null; }
    Relay.sendSession('audio_stop', { direction: 'listen' });
    _setBtn('btn-listen-toggle', false, '▶ Start Listening');
    document.getElementById('vu-listen').style.width     = '0%';
    document.getElementById('vu-listen-pct').textContent = '0%';
    if (!_speakRunning()) { _setStatus('IDLE'); _stopVU(); }
  }

  function playAgentAudio(payload) {
    if (!S.listenActive || !S.audioPlayer) return;
    if (payload?.data) S.audioPlayer.enqueue(payload.data);
  }

  // ── SPEAK (my mic → agent speaker) ───────────────────────────────────────
  async function toggleSpeak() {
    if (S.speakActive) {
      _stopSpeak();
    } else {
      await _startSpeak();
    }
  }

  async function _startSpeak() {
    if (!State.activeSessionId) { Utils.toast('Connect to a device first', true); return; }
    try {
      S.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate:        16000,
          channelCount:      1,
          echoCancellation:  true,   // prevents feedback loop when agent speakers are loud
          noiseSuppression:  true,
          autoGainControl:   true,
        },
      });
    } catch (e) {
      Utils.toast('Mic access denied: ' + e.message, true); return;
    }

    // AudioContext at 16 kHz — ensures no resampling mismatch on the agent
    S.micCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const source = S.micCtx.createMediaStreamSource(S.micStream);

    // Analyser for VU meter
    S.speakAnalyser = S.micCtx.createAnalyser();
    S.speakAnalyser.fftSize = 256;
    source.connect(S.speakAnalyser);

    // ScriptProcessorNode captures float32 → convert to int16 → send
    // (deprecated API but universal; no external worklet file needed in single-HTML)
    S.micProcessor = S.micCtx.createScriptProcessor(1024, 1, 1);
    source.connect(S.micProcessor);
    S.micProcessor.connect(S.micCtx.destination);   // connect to destination (required)
    S.micProcessor.onaudioprocess = (e) => {
      if (!S.speakActive || S.micMuted) return;
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++)
        i16[i] = Math.max(-32768, Math.min(32767, Math.round(f32[i] * 32768)));
      // base64 encode Int16 bytes
      const bytes  = new Uint8Array(i16.buffer);
      let   binary = '';
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
      const b64 = btoa(binary);
      Relay.sendSession('audio_data', {
        data:        b64,
        direction:   'controller_mic',
        sample_rate: 16000,
        channels:    1,
      });
    };

    S.speakActive = true;
    S.micMuted    = false;
    Relay.sendSession('audio_start', { direction: 'speak' });
    _setBtn('btn-speak-toggle', true, '■ Stop Speaking');
    document.getElementById('ptt-hint').style.display = 'block';
    _setStatus(_listenRunning() ? 'DUPLEX' : 'SPEAK');
    _startVU();
    Utils.toast('Mic active — speaking to agent…', false, true);
  }

  function _stopSpeak() {
    S.speakActive = false;
    S.micMuted    = false;
    if (S.micProcessor) { try { S.micProcessor.disconnect(); } catch {} S.micProcessor = null; }
    if (S.speakAnalyser) { try { S.speakAnalyser.disconnect(); } catch {} S.speakAnalyser = null; }
    if (S.micCtx) { try { S.micCtx.close(); } catch {} S.micCtx = null; }
    if (S.micStream) { S.micStream.getTracks().forEach(t => t.stop()); S.micStream = null; }
    Relay.sendSession('audio_stop', { direction: 'speak' });
    _setBtn('btn-speak-toggle', false, '▶ Start Speaking');
    document.getElementById('ptt-hint').style.display = 'none';
    document.getElementById('vu-speak').style.width     = '0%';
    document.getElementById('vu-speak-pct').textContent = '0%';
    if (!_listenRunning()) { _setStatus('IDLE'); _stopVU(); }
  }

  // ── Convenience ───────────────────────────────────────────────────────────
  async function startBoth() {
    if (!S.listenActive) await _startListen();
    if (!S.speakActive)  await _startSpeak();
  }

  function stopAll() {
    cameraStop();
    _stopListen();
    _stopSpeak();
  }

  function _listenRunning() { return S.listenActive; }
  function _speakRunning()  { return S.speakActive; }

  // ── Push-To-Talk (Space bar while AV view is active) ─────────────────────
  document.addEventListener('keydown', (e) => {
    if (!document.getElementById('view-av')?.classList.contains('active')) return;
    if (e.code === 'Space' && S.speakActive && !e.repeat) {
      e.preventDefault();
      S.micMuted = false;
    }
  });
  document.addEventListener('keyup', (e) => {
    if (!document.getElementById('view-av')?.classList.contains('active')) return;
    if (e.code === 'Space' && S.speakActive) {
      S.micMuted = true;
    }
  });

  // Stop all AV when session ends
  function onSessionEnd() { stopAll(); }

  // Public API
  return {
    cameraStart, cameraStop, renderCameraFrame,
    onCameraList, onAvError, refreshCameraList,
    cameraSnapshot, onCameraSnapshot,
    toggleCameraRecording, onCameraAiResult,
    toggleListen, toggleSpeak,
    playAgentAudio,
    startBoth, stopAll,
    onSessionEnd,
  };
})();

