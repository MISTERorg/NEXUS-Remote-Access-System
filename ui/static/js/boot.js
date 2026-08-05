/* =========================================================================
   BOOT
   ========================================================================= */
document.addEventListener('DOMContentLoaded', () => {
  Auth.init();
  setInterval(() => { if (State.accessToken) Poller.refresh(); }, 12000);
});
