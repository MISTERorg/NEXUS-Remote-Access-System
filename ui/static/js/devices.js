/* =========================================================================
   MODULE: Devices — table rendering & detail drawer
   ========================================================================= */
const Devices = {
  renderOverviewTable(devs) {
    const tbody = document.querySelector('#overview-table tbody');
    tbody.innerHTML = devs.slice(0, 8).map(d => `
      <tr onclick="Devices.openDrawer('${Utils.escapeHtml(d.device_id)}')">
        <td><div class="dev-name">${Utils.escapeHtml(d.name)}</div><div class="dev-id">${Utils.escapeHtml(d.device_id)}</div></td>
        <td>${Utils.escapeHtml(d.device_type)}</td>
        <td>${Utils.escapeHtml(d.os || '—')}</td>
        <td class="dev-id">${Utils.escapeHtml(d.ip_address || '—')}</td>
        <td>${Utils.capBadges(d.capabilities)}</td>
        <td><span class="status-pill ${Utils.escapeHtml(d.status)}">${Utils.escapeHtml(d.status)}</span></td>
        <td><button class="icon-btn" ${d.status === 'online' ? '' : 'disabled'}
              onclick="event.stopPropagation(); Sessions.startControl('${Utils.escapeHtml(d.device_id)}', 'desktop')">⚡ Control</button></td>
      </tr>
    `).join('') || `<tr><td colspan="7" class="empty-note">No devices registered yet.</td></tr>`;
  },
  renderDevicesTable(devs) {
    const tbody = document.querySelector('#devices-table tbody');
    tbody.innerHTML = devs.map(d => `
      <tr onclick="Devices.openDrawer('${Utils.escapeHtml(d.device_id)}')">
        <td class="dev-id">${Utils.escapeHtml(d.device_id)}</td>
        <td class="dev-name">${Utils.escapeHtml(d.name)}</td>
        <td>${Utils.escapeHtml(d.device_type)}</td>
        <td>${Utils.escapeHtml(d.os || '—')}</td>
        <td>${Utils.escapeHtml(d.ip_address || '—')}</td>
        <td>${Utils.capBadges(d.capabilities)}</td>
        <td><span class="status-pill ${Utils.escapeHtml(d.status)}">${Utils.escapeHtml(d.status)}</span></td>
        <td><button class="icon-btn" ${d.status === 'online' ? '' : 'disabled'}
              onclick="event.stopPropagation(); Sessions.startControl('${Utils.escapeHtml(d.device_id)}', 'desktop')">Control</button></td>
      </tr>
    `).join('') || `<tr><td colspan="8" class="empty-note">No devices registered yet.</td></tr>`;
  },
  renderSessionsList() {
    const container = document.getElementById('sessions-list');
    if (!State.sessionsCache.length) {
      container.innerHTML = '<div class="empty-note">No active sessions.</div>'; return;
    }
    container.innerHTML = State.sessionsCache.map(s => `
      <div class="hero-card" style="margin-bottom:12px;max-width:none;display:flex;justify-content:space-between;align-items:center;">
        <div>
          <h4 style="margin:0 0 4px;">Session: ${Utils.escapeHtml(s.session_id)}</h4>
          <div style="font-size:11.5px;color:var(--text-dim);">
            Device: <strong>${Utils.escapeHtml(s.device_id)}</strong>
            &nbsp;·&nbsp; Duration: ${Math.round(s.duration_s || 0)}s
            &nbsp;·&nbsp; ↑${Utils.formatBytes(s.bytes_sent)} ↓${Utils.formatBytes(s.bytes_received)}
          </div>
        </div>
        <div style="display:flex;gap:8px;">
          <button class="dt-btn" onclick="Sessions.switchTo('${Utils.escapeHtml(s.session_id)}', '${Utils.escapeHtml(s.device_id)}')">View Screen</button>
          <button class="dt-btn" style="color:var(--red);" onclick="Sessions.closeTab('${Utils.escapeHtml(s.session_id)}')">Terminate</button>
        </div>
      </div>
    `).join('');
  },
  openDrawer(deviceId) {
    const dev = State.devicesCache.find(d => d.device_id === deviceId);
    if (!dev) return;
    const caps = dev.capabilities || {};
    const canCtrl = dev.status === 'online' && caps.screen_share;
    document.getElementById('drawer-title').textContent = dev.name;
    document.getElementById('drawer-id').textContent    = dev.device_id;
    document.getElementById('drawer-content').innerHTML = `
      <div style="display:flex;flex-direction:column;gap:12px;font-size:12px;">
        <div><strong>OS:</strong> ${Utils.escapeHtml(dev.os || 'N/A')}</div>
        <div><strong>IP:</strong> ${Utils.escapeHtml(dev.ip_address || 'N/A')}</div>
        <div><strong>Type:</strong> ${Utils.escapeHtml(dev.device_type)}</div>
        <div><strong>Status:</strong> <span class="status-pill ${Utils.escapeHtml(dev.status)}">${Utils.escapeHtml(dev.status)}</span></div>
        <div><strong>Capabilities:</strong> ${Utils.capBadges(caps)}</div>
        <hr style="border:0;border-top:1px solid var(--line);margin:10px 0;">
        <button class="btn-connect" style="width:100%;justify-content:center;" ${canCtrl ? '' : 'disabled'}
          onclick="Sessions.startControl('${Utils.escapeHtml(dev.device_id)}', 'desktop'); Drawer.close();">
          ${canCtrl ? 'Launch Remote Desktop' : (dev.status !== 'online' ? 'Device Offline' : 'Screen Share Unsupported')}
        </button>
        <button class="icon-btn" style="justify-content:center;" ${dev.status === 'online' ? '' : 'disabled'}
          onclick="Sessions.startControl('${Utils.escapeHtml(dev.device_id)}', 'terminal'); Drawer.close();">⚡ Open Shell</button>
        <button class="icon-btn" style="justify-content:center;" ${dev.status === 'online' ? '' : 'disabled'}
          onclick="Sessions.startControl('${Utils.escapeHtml(dev.device_id)}', 'files'); Drawer.close();">📁 File Manager</button>
      </div>
    `;
    document.getElementById('drawer').classList.add('open');
    document.getElementById('drawer-overlay').classList.add('open');
  },
};

const Drawer = {
  close() {
    document.getElementById('drawer').classList.remove('open');
    document.getElementById('drawer-overlay').classList.remove('open');
  },
};

