/* =========================================================================
   MODULE: Views — view switching, tab bar
   ========================================================================= */
const Views = {
  show(name) {
    document.querySelectorAll('.nav-item').forEach(n =>
      n.classList.toggle('active', n.dataset.view === name));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById('view-' + name).classList.add('active');
    document.getElementById('topbar-title').textContent = name.toUpperCase();
    document.querySelectorAll('.tab-item').forEach(t => t.classList.remove('active'));
    if (name === 'overview' || !State.activeSessionId)
      document.getElementById('tab-overview')?.classList.add('active');
    else
      document.getElementById(`tab-sess-${State.activeSessionId}`)?.classList.add('active');

    if (name === 'sessions') Devices.renderSessionsList();

    // When entering the AV tab with an active session, request the live
    // camera device list from the agent to populate the dropdown.
    if (name === 'av' && State.activeSessionId) {
      AVControl.refreshCameraList();
    }

    // FIX: Auto-enable keyboard capture when entering desktop view.
    // Previously, capture was NEVER auto-enabled, so keyboard appeared to
    // do nothing until the user discovered they had to click the canvas.
    if (name === 'desktop' && State.activeSessionId) {
      setTimeout(() => {
        RemoteDesktop.setInputCapture(true);
        Utils.toast('⌨ Keyboard captured — press Esc to release', false, false, true);
      }, 150);
    }
    if (name !== 'desktop') RemoteDesktop.setInputCapture(false);

    if (name === 'terminal' && State.activeSessionId && !State.activeTerminalId)
      Terminal.openShell();

    // Close target dropdown if open
    Terminal.closeTargetPanel();
  },
};

