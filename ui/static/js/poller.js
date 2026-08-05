/* =========================================================================
   MODULE: Poller — background refresh
   ========================================================================= */
const Poller = {
  _inFlight: false,
  async refresh() {
    if (this._inFlight) return;
    this._inFlight = true;
    try {
      const health = await API.call('/health');
      document.getElementById('stat-online').textContent   = health.devices_online;
      document.getElementById('stat-total').textContent    = health.devices_total;
      document.getElementById('stat-sessions').textContent = health.active_sessions;
      const devs = await API.call('/devices');
      State.devicesCache = devs.devices;
      document.getElementById('nav-devices-count').textContent = devs.total;
      Devices.renderOverviewTable(devs.devices);
      Devices.renderDevicesTable(devs.devices);
      const sess = await API.call('/sessions');
      State.sessionsCache = sess.sessions;
      document.getElementById('nav-sessions-count').textContent = sess.sessions.length;
      Sessions.populateSelects();
      Terminal._refreshTargetPanel(); // NEW: update multi-target checkboxes
      if (document.getElementById('view-sessions').classList.contains('active'))
        Devices.renderSessionsList();
    } catch (e) {
      Utils.toast('Sync error: ' + e.message, true);
    } finally { this._inFlight = false; }
  },
};


