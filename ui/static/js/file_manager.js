/* =========================================================================
   MODULE: FileMgr — dual-pane file manager
   ========================================================================= */
const FileMgr = {
  _CHUNK: 256 * 1024,

  // Active download state — keyed by path so partial chunks can be
  // assembled as they arrive from the agent's streaming dispatch.
  _downloads: {},

  onSessionChange() {
    const sessionId = document.getElementById('files-session-select').value;
    if (!sessionId) return;
    const sess = State.sessionsCache.find(s => s.session_id === sessionId);
    if (sess && sessionId !== State.activeSessionId) {
      Sessions.switchTo(sessionId, sess.device_id);
      Views.show('files');
    }
    State.remoteCurrentPath = null;
    this.loadRemote();
  },

  loadRemote(path) {
    const sessionId = document.getElementById('files-session-select').value;
    if (!sessionId) { Utils.toast('Select a session first', true); return; }
    if (sessionId !== State.activeSessionId) { Utils.toast('Switch to this session first', true); return; }
    if (Relay.sendSession('file_list', { path: path || State.remoteCurrentPath || undefined }))
      Utils.toast('Refreshing remote directory…');
  },

  renderRemote(payload) {
    if (!payload) return;
    State.remoteCurrentPath = payload.path;
    document.getElementById('remote-path').innerHTML =
      State.remoteCurrentPath
        ? `<span class="up-dir" onclick="FileMgr.navigateUp()">⬆ up</span> · ${Utils.escapeHtml(State.remoteCurrentPath)}`
        : '—';
    const list    = document.getElementById('remote-file-list');
    const entries = payload.entries || [];
    if (!entries.length) { list.innerHTML = '<div class="empty-note">Empty directory.</div>'; return; }
    list.innerHTML = entries.map(e => `
      <div class="file-item" onclick="FileMgr.onRemoteClick('${Utils.escapeHtml(e.name)}', ${!!e.is_dir}, event)">
        <span class="fname">${e.is_dir ? '📁' : '📄'} ${Utils.escapeHtml(e.name)}</span>
        <span class="fmeta">${e.is_dir ? 'DIR' : Utils.formatBytes(e.size)}</span>
      </div>
    `).join('');
  },

  navigateUp() {
    if (!State.remoteCurrentPath) return;
    const isWin = State.remoteCurrentPath.includes('\\');
    const sep   = isWin ? '\\' : '/';
    const parts = State.remoteCurrentPath.split(sep).filter(Boolean);
    parts.pop();
    const parent = isWin ? (parts.join(sep) + sep) : ('/' + parts.join(sep));
    this.loadRemote(parent || sep);
  },

  onRemoteClick(name, isDir, ev) {
    const sep = (State.remoteCurrentPath || '').includes('\\') ? '\\' : '/';
    if (isDir) {
      const base = (State.remoteCurrentPath || '').replace(/[\/\\]+$/, '');
      this.loadRemote(base + sep + name); return;
    }
    document.querySelectorAll('#remote-file-list .file-item').forEach(el => el.classList.remove('selected'));
    ev.currentTarget.classList.add('selected');
    State.selectedRemoteFile = name;
  },

  onLocalFilesPicked(evt) {
    State.stagedLocalFiles = State.stagedLocalFiles.concat(Array.from(evt.target.files || []));
    this._renderLocal(); evt.target.value = '';
  },

  _renderLocal() {
    const list = document.getElementById('local-file-list');
    if (!State.stagedLocalFiles.length) {
      list.innerHTML = '<div class="empty-note">No files staged. Click "Add Files" to select files.</div>'; return;
    }
    list.innerHTML = State.stagedLocalFiles.map((f, i) => `
      <div class="file-item">
        <span class="fname">📄 ${Utils.escapeHtml(f.name)}</span>
        <span class="fmeta">${Utils.formatBytes(f.size)}</span>
        <span class="close-tab" onclick="FileMgr.removeStagedFile(${i})">✕</span>
      </div>
    `).join('');
  },

  removeStagedFile(i) { State.stagedLocalFiles.splice(i, 1); this._renderLocal(); },

  async transfer(direction) {
    const sessionId = document.getElementById('files-session-select').value;
    if (!sessionId) { Utils.toast('Select a session first', true); return; }
    if (sessionId !== State.activeSessionId) { Utils.toast('Switch to this session first', true); return; }
    if (direction === 'upload') {
      if (!State.stagedLocalFiles.length) { Utils.toast('Stage files first', true); return; }
      for (const file of State.stagedLocalFiles) await this._uploadFile(file);
      State.stagedLocalFiles = []; this._renderLocal(); return;
    }
    if (!State.selectedRemoteFile) { Utils.toast('Select a remote file first', true); return; }
    const sep   = (State.remoteCurrentPath || '').includes('\\') ? '\\' : '/';
    const rpath = (State.remoteCurrentPath || '').replace(/[\/\\]+$/, '') + sep + State.selectedRemoteFile;
    this._startDownload(rpath);
  },

  // ── UPLOAD ────────────────────────────────────────────────────────────────
  // Sends file_upload_start → N×file_upload_chunk → file_upload_end, then
  // waits for the agent's file_upload_end acknowledgement before reporting
  // success. Previously this fired-and-forgot and always reported success
  // regardless of what happened on the agent.

  async _uploadFile(file) {
    const tid   = Utils.uuid();
    const pbar  = document.getElementById('transfer-progress');
    const pfill = document.getElementById('transfer-progress-fill');
    const sep   = (State.remoteCurrentPath || '').includes('\\') ? '\\' : '/';
    const dest  = (State.remoteCurrentPath || '').replace(/[\/\\]+$/, '') + sep + file.name;

    pbar.style.display = 'block';
    pfill.style.width  = '0%';

    // Register a promise that resolves when we receive the ack
    let _resolve, _reject;
    const ack = new Promise((res, rej) => { _resolve = res; _reject = rej; });
    State.pendingUploadAcks = State.pendingUploadAcks || {};
    State.pendingUploadAcks[tid] = { resolve: _resolve, reject: _reject };

    Relay.sendSession('file_upload_start', {
      transfer_id: tid, path: dest, size: file.size, name: file.name,
    });

    const totalChunks = Math.ceil(file.size / this._CHUNK) || 1;
    for (let i = 0; i < totalChunks; i++) {
      const slice = file.slice(i * this._CHUNK, (i + 1) * this._CHUNK);
      const buf   = await slice.arrayBuffer();
      Relay.sendSession('file_upload_chunk', {
        transfer_id: tid, index: i, data: Utils.arrayBufferToBase64(buf),
      });
      pfill.style.width = `${Math.round(((i + 1) / totalChunks) * 90)}%`;
      // yield to the event loop so the UI stays responsive while chunking
      await new Promise(r => setTimeout(r, 0));
    }

    Relay.sendSession('file_upload_end', { transfer_id: tid });
    pfill.style.width = '95%';

    try {
      // Wait up to 30 s for the agent to confirm it wrote the file
      const timeout = new Promise((_, rej) =>
        setTimeout(() => rej(new Error('Upload ack timeout')), 30000));
      const result = await Promise.race([ack, timeout]);
      pfill.style.width = '100%';
      if (result && result.ok) {
        Utils.toast(`Uploaded ${file.name} (${Utils.formatBytes(file.size)})`, false, true);
      } else {
        Utils.toast(`Upload failed: ${result?.path || file.name}`, true);
      }
    } catch (e) {
      Utils.toast(`Upload error: ${e.message}`, true);
    } finally {
      delete (State.pendingUploadAcks || {})[tid];
      setTimeout(() => { pbar.style.display = 'none'; pfill.style.width = '0%'; }, 800);
    }
  },

  // Called by relay.js when a file_upload_end ack arrives from the agent
  onUploadAck(payload) {
    const tid = payload?.transfer_id;
    const acks = State.pendingUploadAcks || {};
    if (tid && acks[tid]) {
      acks[tid].resolve(payload);
      delete acks[tid];
    }
  },

  // ── DOWNLOAD ──────────────────────────────────────────────────────────────
  // Sends file_download_start, then assembles incoming file_download_chunk
  // messages (dispatched here via onDownloadChunk) until done=true, then
  // triggers a browser Save-As download. Previously the chunks were never
  // handled by the frontend at all — they fell into relay.js's default
  // console.debug() case and were silently discarded.

  _startDownload(remotePath) {
    const filename = remotePath.split(/[/\\]/).pop() || 'download';
    this._downloads[remotePath] = { chunks: [], total: null, filename };
    Utils.toast(`Downloading ${filename}…`);
    Relay.sendSession('file_download_start', { path: remotePath });

    const pbar  = document.getElementById('transfer-progress');
    const pfill = document.getElementById('transfer-progress-fill');
    pbar.style.display = 'block';
    pfill.style.width  = '0%';
  },

  // Called by relay.js when a file_download_chunk arrives
  onDownloadChunk(payload) {
    if (!payload) return;
    const path = payload.path;
    const dl   = this._downloads[path];
    if (!dl) return;  // unexpected chunk for a path we're not tracking — ignore

    if (payload.error) {
      Utils.toast(`Download failed: ${payload.error}`, true);
      delete this._downloads[path];
      const pbar = document.getElementById('transfer-progress');
      if (pbar) pbar.style.display = 'none';
      return;
    }

    if (payload.data) {
      dl.chunks.push(Utils.hexToBytes(payload.data));
      dl.total = payload.total;
      const received = dl.chunks.reduce((s, c) => s + c.length, 0);
      const pct = dl.total ? Math.round((received / dl.total) * 100) : 0;
      const pfill = document.getElementById('transfer-progress-fill');
      if (pfill) pfill.style.width = `${pct}%`;
    }

    if (payload.done) {
      const blob = new Blob(dl.chunks);
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href     = url;
      a.download = dl.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      Utils.toast(`Downloaded ${dl.filename} (${Utils.formatBytes(blob.size)})`, false, true);
      delete this._downloads[path];
      const pbar  = document.getElementById('transfer-progress');
      const pfill = document.getElementById('transfer-progress-fill');
      if (pbar)  setTimeout(() => { pbar.style.display = 'none'; pfill.style.width = '0%'; }, 800);
    }
  },
};

