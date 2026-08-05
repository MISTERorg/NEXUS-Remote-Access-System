/* =========================================================================
   MODULE: State — single mutable object; no scattered globals
   ========================================================================= */
const State = {
  apiBase:     localStorage.getItem('nexus_api_base')     || 'http://localhost:8080',
  relayWsUrl:  localStorage.getItem('nexus_relay_ws_url') || 'ws://localhost:7000',
  accessToken:  sessionStorage.getItem('nexus_access_token')  || null,
  refreshToken: sessionStorage.getItem('nexus_refresh_token') || null,
  currentUser:  sessionStorage.getItem('nexus_user')          || null,

  // Relay WebSocket (single, shared)
  relayWS:             null,
  relayConnectPromise: null,
  wsReconnectAttempts: 0,
  wsIntentionalClose:  false,

  // Session tracking
  pendingSessionRequest: null,  // { deviceId, sessionId, resolve, reject }
  activeSessionId:  null,
  activeDeviceId:   null,
  activeTerminalId: null,

  // Caches
  devicesCache:  [],
  sessionsCache: [],

  // File manager
  stagedLocalFiles:   [],
  remoteCurrentPath:  null,
  selectedRemoteFile: null,

  // Desktop streaming
  frameTimestamps:     [],
  lastFrameObjectUrl:  null,
  inputCaptureActive:  false,
  canvasHandlersBound: false,

  // Terminal — NEW
  termTargetIds: new Set(),   // session IDs to send to; empty = active only; 'broadcast' = all
  termMaintMode: false,       // maintenance command mode
};

