/* =========================================================================
   MODULE: Clipboard — explicit toolbar-driven sync only
   =========================================================================
   No keyboard shortcuts are intercepted here. Ctrl+C / Ctrl+V on the canvas
   go straight to the remote machine like any other key combination.
   The two toolbar buttons cover the explicit sync cases:
     Pull Clipboard — reads remote clipboard and writes it to the local one.
     Push Text…    — prompt to type text, pushes it to the remote clipboard.
   ========================================================================= */
const Clipboard = {
  // Pull remote clipboard → write to local clipboard
  getRemote() {
    if (!Relay.sendSession('clipboard_get', {})) return;
    Utils.toast('Requesting remote clipboard…');
  },

  // Called when the agent sends back clipboard content
  onRemoteGet(payload) {
    if (!payload || typeof payload.text !== 'string') return;
    const text = payload.text;
    navigator.clipboard?.writeText(text)
      .then(() => Utils.toast(`Remote clipboard pulled locally (${text.length} chars)`, false, true))
      .catch(() => prompt('Remote clipboard content (copy manually):', text));
  },

  // Prompt for text to push to the remote clipboard
  pushPrompt() {
    const text = prompt('Text to push to the remote clipboard:');
    if (text === null) return;
    Relay.sendSession('clipboard_set', { text });
    Utils.toast('Text pushed to remote clipboard');
  },
};

