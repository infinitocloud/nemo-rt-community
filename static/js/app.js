// --- Auth ---
// The API key is NEVER stored in JS / localStorage / URL. Login posts the key
// to /api/auth/login which sets an HttpOnly+Secure+SameSite cookie. All
// subsequent fetches and the WebSocket handshake send the cookie automatically
// via { credentials: 'include' } (same-origin).
// If anyone hits a URL with ?key=... server-side middleware returns 400 and
// nothing is logged.
if (new URLSearchParams(location.search).has('key')) {
  // Strip the param from address bar before doing anything else.
  history.replaceState(null, '', location.pathname);
}

function _authFetch(path, opts = {}) {
  return fetch(path, { credentials: 'same-origin', ...opts });
}

function showLogin(msg) {
  document.getElementById('login-overlay').style.display = 'flex';
  const err = document.getElementById('login-error');
  if (msg) { err.textContent = msg; err.style.display = 'block'; }
  else { err.style.display = 'none'; }
  document.getElementById('login-key').focus();
}

function hideLogin() {
  document.getElementById('login-overlay').style.display = 'none';
}

async function submitLogin() {
  const input = document.getElementById('login-key');
  const val = input.value.trim();
  if (!val) return;
  const btn = document.getElementById('login-submit');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: val }),
    });
    if (!res.ok) {
      let msg = 'Invalid API key';
      try { const j = await res.json(); if (j && j.error) msg = j.error; } catch {}
      showLogin(msg);
      return;
    }
    const data = await res.json();
    currentRole = data.role || null;
    // The cookie is now set; wipe the raw key from memory and the input.
    input.value = '';
    hideLogin();
    applySidebar();
    // Close any prior WS but DO NOT reopen — the WS opens lazily on Tap to
    // connect or sendChatText. Logging in from a dashboard tab must not
    // bump the "Connected" counter.
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    _authFetch('/settings').then(r => r.json()).then(s => {
      if (s && Object.keys(s).length > 0) applySettings(s);
    }).catch(() => {});
    waitForReady();
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function logout() {
  try { await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }); } catch {}
  // Force a reload — the next page load has no cookie and will show login.
  location.reload();
}

// --- Platform: role & sidebar ---
let currentRole = null; // 'admin' | null  (Community is single-tenant)
let currentSection = 'realtime';
let _adminDefaultApplied = false;

async function resolveRole() {
  try {
    const res = await _authFetch('/api/auth', { method: 'POST' });
    if (res.ok) {
      const data = await res.json();
      currentRole = data.role || null;
    } else {
      currentRole = null;
    }
  } catch {
    currentRole = null;
  }
  applySidebar();
}

function applySidebar() {
  const sidebar = document.getElementById('sidebar');
  const roleEl = document.getElementById('sidebar-role');
  // Show/hide admin-only items (Community: shown once authenticated)
  sidebar.querySelectorAll('[data-admin]').forEach(el => {
    el.style.display = currentRole === 'admin' ? '' : 'none';
  });
  // Role badge
  if (currentRole === 'admin') {
    roleEl.textContent = 'Admin';
    roleEl.className = 'sidebar-role role-admin';
  } else {
    roleEl.textContent = '';
  }
  // Community edition: single-tenant — only the Realtime section exists.
  if (currentRole === 'admin' && !_adminDefaultApplied) {
    _adminDefaultApplied = true;
    switchSection('realtime');
  }
}

function switchSection(name) {
  currentSection = name;
  // Toggle active section
  document.querySelectorAll('.section').forEach(el => el.classList.remove('active'));
  const target = document.getElementById('section-' + name);
  if (target) target.classList.add('active');
  // Toggle active sidebar item
  document.querySelectorAll('.sidebar-item').forEach(el => {
    el.classList.toggle('active', el.dataset.section === name);
  });
  // Close mobile sidebar
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('open');
}

// Sidebar click handlers
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.sidebar-item').forEach(btn => {
    btn.addEventListener('click', () => switchSection(btn.dataset.section));
  });
  // Hamburger
  document.getElementById('hamburger').addEventListener('click', () => {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('sidebar-overlay').classList.toggle('open');
  });
  // Overlay click closes sidebar
  document.getElementById('sidebar-overlay').addEventListener('click', () => {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-overlay').classList.remove('open');
  });
});

// --- DOM refs ---
const playBtn = document.getElementById('play-btn');
const playRing = document.getElementById('play-ring');
const stopBtn = document.getElementById('stop-btn');
const muteBtn = document.getElementById('mute-btn');
const sessionControls = document.getElementById('session-controls');
const micHint = document.getElementById('mic-hint');
const statusEl = document.getElementById('status');
const chatEl = document.getElementById('chat');
const emptyState = document.getElementById('empty-state');
const speakerInput = document.getElementById('speaker');
const voiceSelect = document.getElementById('voice-select');
const testVoiceBtn = document.getElementById('test-voice');
const genderSelect = document.getElementById('gender-select');
const countrySelect = document.getElementById('country-select');
const voiceCount = document.getElementById('voice-count');
const voicePanel = document.getElementById('voice-panel');
const voiceToggle = document.getElementById('voice-toggle');
const loadingBar = document.getElementById('loading-bar');
const promptInput = document.getElementById('system-prompt');
const tempSlider = document.getElementById('temperature');
const tempValue = document.getElementById('temp-value');
const clearBtn = document.getElementById('clear-chat');
const silenceSlider = document.getElementById('silence-slider');
const silenceValue = document.getElementById('silence-value');
const thresholdSlider = document.getElementById('threshold-slider');
const thresholdValue = document.getElementById('threshold-value');
const maxTokensSlider = document.getElementById('max-tokens-slider');
const maxTokensValue = document.getElementById('max-tokens-value');
const speakerEnInput = document.getElementById('speaker-en');
const voiceSelectEn = document.getElementById('voice-select-en');
const chatInput = document.getElementById('chat-input');
const chatSendBtn = document.getElementById('chat-send');
const bargeinToggle = document.getElementById('bargein-toggle');
const bargeinLabel = document.getElementById('bargein-label');
const pauseMicReplyToggle = document.getElementById('pause-mic-reply-toggle');
const pauseMicReplyLabel = document.getElementById('pause-mic-reply-label');

let bargeInEnabled = localStorage.getItem('bargeIn') !== 'false'; // default: on
bargeinToggle.checked = bargeInEnabled;
bargeinLabel.textContent = bargeInEnabled ? 'On' : 'Off';

// Pause mic during AI playback — prevents acoustic echo loop (mic captures speaker, re-injects)
let pauseMicReplyEnabled = localStorage.getItem('pauseMicReply') !== 'false'; // default: on
pauseMicReplyToggle.checked = pauseMicReplyEnabled;
pauseMicReplyLabel.textContent = pauseMicReplyEnabled ? 'On' : 'Off';

const connDot = document.getElementById('conn-dot');
const emptyText = document.getElementById('empty-text');

let ws = null;
// Transport: 'ws' = native protocol (default) · 'realtime' = OpenAI Realtime API (/v1/realtime)
let TRANSPORT = localStorage.getItem('nemo_transport') || 'ws';
let modelsReady = false;
let sessionActive = false;
let autoReconnect = true;
let filterGender = 'all';
let filterCountry = 'all';
let hasMessages = false;
let shimmerEl = null;

// --- Streaming state ---
let muted = false;
let micActive = false;
let streamCtx = null;
let streamSource = null;
let streamProcessor = null;
let micStream = null;


// Apply settings from server (single source of truth)
function applySettings(s) {
  if (s.speaker != null) speakerInput.value = s.speaker;
  if (s.system_prompt) promptInput.value = s.system_prompt;
  if (s.temperature != null) { tempSlider.value = s.temperature; tempValue.textContent = s.temperature; }
  if (s.silence_ms != null) { silenceSlider.value = s.silence_ms; silenceValue.textContent = s.silence_ms + 'ms'; }
  if (s.threshold != null) { thresholdSlider.value = s.threshold; thresholdValue.textContent = s.threshold; }
  if (s.max_tokens != null) { maxTokensSlider.value = s.max_tokens; maxTokensValue.textContent = s.max_tokens; }
  if (s.speaker_en != null) speakerEnInput.value = s.speaker_en;
  renderVoiceGrid();
  if (typeof renderVoiceGridEn === 'function') renderVoiceGridEn();
}

// NOTA: localStorage para settings persistidos en server fue eliminado (2026-04-25).
// Single source of truth = server. Los valores se cargan via /settings al inicio.
// La API key tampoco se guarda en localStorage (2026-06-07): la sesion se
// mantiene con cookie HttpOnly tras POST /api/auth/login. Solo `bargeIn`
// (preferencia pura del cliente) sigue en localStorage.

// Login events
document.getElementById('login-submit').addEventListener('click', submitLogin);
document.getElementById('login-key').addEventListener('keydown', e => { if (e.key === 'Enter') submitLogin(); });

// Cargar settings desde server (source of truth). NO conectamos WS aqui:
// el WS solo debe abrirse cuando el user hace "Tap to connect" (playBtn ->
// startSession -> connect) o cuando manda texto (sendChatText auto-reconnect).
// Si el usuario abre el Dashboard, NUNCA toca el WS — asi el contador
// "Connected" del dashboard refleja conversaciones reales, no tabs viendo
// métricas.
let settingsLoaded = false;
_authFetch('/settings').then(r => {
  if (r.status === 401) { showLogin(window.t('api_required')); return null; }
  return r.json();
}).then(s => {
  if (!s) return;
  if (s.error === 'unauthorized') { showLogin(window.t('api_required')); return; }
  if (Object.keys(s).length > 0) {
    applySettings(s);
  }
  // Authenticated page load (e.g. a refresh with a valid session cookie) skips
  // the login flow, so start model-readiness polling here too — otherwise the
  // status badges stay stuck on "Iniciando…" until the next fresh login.
  waitForReady();
}).catch(() => console.log('[SETTINGS] Server unavailable')
).finally(() => { settingsLoaded = true; resolveRole(); });

function saveSpeaker(id) {
  speakerInput.value = id;
}

// Static speaker metadata from NeMo (OpenSLR datasets)
const SPEAKER_DATA = [
  [0,"F","AR"],[1,"F","AR"],[2,"F","AR"],[3,"F","AR"],[4,"F","AR"],[5,"F","AR"],
  [6,"F","AR"],[7,"F","AR"],[8,"F","AR"],[9,"F","AR"],[10,"F","AR"],[11,"F","AR"],
  [12,"F","AR"],[13,"F","AR"],[14,"F","AR"],[15,"F","AR"],[16,"F","AR"],[17,"F","AR"],
  [18,"F","AR"],[19,"F","AR"],[20,"F","AR"],[21,"F","AR"],[22,"F","AR"],[23,"F","AR"],
  [24,"F","AR"],[25,"F","AR"],[26,"F","AR"],[27,"F","AR"],[28,"F","AR"],[29,"F","AR"],
  [30,"F","AR"],
  [31,"M","AR"],[32,"M","AR"],[33,"M","AR"],[34,"M","AR"],[35,"M","AR"],[36,"M","AR"],
  [37,"M","AR"],[38,"M","AR"],[39,"M","AR"],[40,"M","AR"],[41,"M","AR"],[42,"M","AR"],
  [43,"M","AR"],
  [44,"F","CL"],[45,"F","CL"],[46,"F","CL"],[47,"F","CL"],[48,"F","CL"],[49,"F","CL"],
  [50,"F","CL"],[51,"F","CL"],[52,"F","CL"],[53,"F","CL"],[54,"F","CL"],[55,"F","CL"],
  [56,"F","CL"],
  [57,"M","CL"],[58,"M","CL"],[59,"M","CL"],[60,"M","CL"],[61,"M","CL"],[62,"M","CL"],
  [63,"M","CL"],[64,"M","CL"],[65,"M","CL"],[66,"M","CL"],[67,"M","CL"],[68,"M","CL"],
  [69,"M","CL"],[70,"M","CL"],[71,"M","CL"],[72,"M","CL"],[73,"M","CL"],[74,"M","CL"],
  [75,"F","CO"],[76,"F","CO"],[77,"F","CO"],[78,"F","CO"],[79,"F","CO"],[80,"F","CO"],
  [81,"F","CO"],[82,"F","CO"],[83,"F","CO"],[84,"F","CO"],[85,"F","CO"],[86,"F","CO"],
  [87,"F","CO"],[88,"F","CO"],[89,"F","CO"],[90,"F","CO"],
  [91,"M","CO"],[92,"M","CO"],[93,"M","CO"],[94,"M","CO"],[95,"M","CO"],[96,"M","CO"],
  [97,"M","CO"],[98,"M","CO"],[99,"M","CO"],[100,"M","CO"],[101,"M","CO"],[102,"M","CO"],
  [103,"M","CO"],[104,"M","CO"],[105,"M","CO"],[106,"M","CO"],[107,"M","CO"],
  [108,"F","PE"],[109,"F","PE"],[110,"F","PE"],[111,"F","PE"],[112,"F","PE"],[113,"F","PE"],
  [114,"F","PE"],[115,"F","PE"],[116,"F","PE"],[117,"F","PE"],[118,"F","PE"],[119,"F","PE"],
  [120,"F","PE"],[121,"F","PE"],[122,"F","PE"],[123,"F","PE"],[124,"F","PE"],[125,"F","PE"],
  [126,"M","PE"],[127,"M","PE"],[128,"M","PE"],[129,"M","PE"],[130,"M","PE"],[131,"M","PE"],
  [132,"M","PE"],[133,"M","PE"],[134,"M","PE"],[135,"M","PE"],[136,"M","PE"],[137,"M","PE"],
  [138,"M","PE"],[139,"M","PE"],[140,"M","PE"],[141,"M","PE"],[142,"M","PE"],[143,"M","PE"],
  [144,"M","PE"],[145,"M","PE"],
  [146,"F","PR"],[147,"F","PR"],[148,"F","PR"],[149,"F","PR"],[150,"F","PR"],
  [151,"F","VE"],[152,"F","VE"],[153,"F","VE"],[154,"F","VE"],[155,"F","VE"],[156,"F","VE"],
  [157,"F","VE"],[158,"F","VE"],[159,"F","VE"],[160,"F","VE"],[161,"F","VE"],
  [162,"M","VE"],[163,"M","VE"],[164,"M","VE"],[165,"M","VE"],[166,"M","VE"],[167,"M","VE"],
  [168,"M","VE"],[169,"M","VE"],[170,"M","VE"],[171,"M","VE"],[172,"M","VE"],[173,"M","VE"],
];
const COUNTRIES = {AR:"Argentina",CL:"Chile",CO:"Colombia",PE:"Peru",PR:"Puerto Rico",VE:"Venezuela"};
const allSpeakers = SPEAKER_DATA.map(([id,gender,cc]) => ({id,gender,country_code:cc,country:COUNTRIES[cc]}));

// --- Audio playback queue ---
class AudioQueue {
  constructor() { this.ctx = null; this.queue = []; this.playing = false; this.nextTime = 0; this._chain = Promise.resolve(); this._currentSource = null; }
  init() {
    if (!this.ctx) this.ctx = new AudioContext({ sampleRate: 44100 });
    if (this.ctx.state === 'suspended') this.ctx.resume();
  }
  add(base64Wav) {
    this._chain = this._chain.then(async () => {
      const bin = atob(base64Wav);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const buf = await this.ctx.decodeAudioData(bytes.buffer.slice(0));
      this.queue.push(buf);
      if (!this.playing) this._next();
    });
  }
  _next() {
    if (!this.queue.length) { this.playing = false; this.nextTime = 0; this._currentSource = null; return; }
    this.playing = true;
    const buf = this.queue.shift();
    const src = this.ctx.createBufferSource();
    this._currentSource = src;
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const t = Math.max(this.ctx.currentTime, this.nextTime);
    src.start(t);
    this.nextTime = t + buf.duration;
    src.onended = () => { this._currentSource = null; this._next(); };
  }
  stop() {
    this.queue = []; this.playing = false; this.nextTime = 0; this._chain = Promise.resolve();
    if (this._currentSource && this.ctx) {
      try {
        // Fade out over 80ms for smooth cutoff
        const gain = this.ctx.createGain();
        this._currentSource.disconnect();
        this._currentSource.connect(gain);
        gain.connect(this.ctx.destination);
        gain.gain.setValueAtTime(1, this.ctx.currentTime);
        gain.gain.linearRampToValueAtTime(0, this.ctx.currentTime + 0.08);
        this._currentSource.stop(this.ctx.currentTime + 0.08);
      } catch(e) {}
      this._currentSource = null;
    }
  }
}
const player = new AudioQueue();

// --- WebSocket ---
function sendConfig() {
  // Send the form values to the server. They persist to the single config scope,
  // so the next session starts with the same values. To change the global server
  // defaults, use the admin "Server Config" panel (PUT /api/config).
  // If the WS is not open yet, the values will be flushed on the next
  // ws.onopen (we keep them in the DOM form).
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const config = {
    speaker: parseInt(speakerInput.value) || 50,
    speaker_en: parseInt(speakerEnInput.value) || 9017,
    temperature: parseFloat(tempSlider.value),
    max_tokens: parseInt(maxTokensSlider.value),
  };
  const prompt = promptInput.value.trim();
  if (prompt) config.system_prompt = prompt;
  ws.send(JSON.stringify(config));
  ws.send(JSON.stringify({
    type: 'vad_config',
    silence_ms: parseInt(silenceSlider.value),
    threshold: parseFloat(thresholdSlider.value),
  }));
}

function updateMicState() {
  const wsOk = ws && ws.readyState === WebSocket.OPEN;
  connDot.className = 'conn-dot ' + (wsOk ? 'connected' : 'disconnected');

  if (modelsReady && wsOk) {
    playBtn.disabled = false;
    if (!sessionActive) {
      micHint.textContent = window.t('hint_connect');
      playRing.style.display = '';
      sessionControls.style.display = 'none';
    } else if (muted) {
      micHint.textContent = window.t('hint_muted');
      playRing.style.display = 'none';
      sessionControls.style.display = '';
      muteBtn.classList.add('muted');
    } else {
      micHint.textContent = window.t('hint_listening');
      playRing.style.display = 'none';
      sessionControls.style.display = '';
      muteBtn.classList.remove('muted');
    }
    setStatus(window.t('st_ready'), 'ready');
  } else if (modelsReady && !wsOk) {
    if (!sessionActive) {
      // Disconnected, show play button
      playBtn.disabled = false;
      micHint.textContent = window.t('hint_connect');
      playRing.style.display = '';
      sessionControls.style.display = 'none';
      setStatus(window.t('st_disconnected'), 'ready');
    } else {
      playBtn.disabled = true;
      micHint.textContent = window.t('connecting');
    }
  }
}

function connect() {
  // If there's already a live WebSocket, reuse it instead of opening another.
  // Otherwise a Tap-to-connect after a sendChatText would leak the first WS
  // (it stays in CONNECTING/OPEN in background) and the server would log a
  // brand-new session — inflating both "Connected" and "Sessions today".
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    console.log('[WS] reusing existing connection');
    if (ws.readyState === WebSocket.OPEN) {
      // Already open: trigger the same side-effects onopen would (start mic
      // if a session was just requested, refresh UI).
      updateMicState();
      updateChatSendBtn();
      if (sessionActive && !micActive) {
        initMicStream();
        if (emptyText) emptyText.textContent = window.t('hint_listening');
      }
    }
    return;
  }
  // Defensive cleanup of any stale ref (e.g. CLOSING) to avoid double-close noise.
  if (ws) { try { ws.onclose = null; ws.close(); } catch {} ws = null; }
  if (TRANSPORT === 'realtime') { connectRealtime(); return; }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // Cookie auth: the browser ships the HttpOnly nemo_session cookie on the
  // WebSocket Upgrade request automatically (same origin), no URL param.
  const url = `${proto}//${location.host}/ws`;
  ws = new WebSocket(url);
  ws.onopen = () => {
    console.log('[WS] Connected');
    // Push the current Settings form values to the server so this session
    // uses what the user actually has on screen. The server applies them to
    // THIS session only — it no longer persists system_prompt globally
    // (Realtime Settings is a scratch pad, not the server-config editor).
    sendConfig();
    updateMicState();
    updateChatSendBtn();
    // Start mic if session is active
    if (sessionActive && !micActive) {
      initMicStream();
      if (emptyText) emptyText.textContent = window.t('hint_listening');
    }
  };
  ws.onclose = (e) => {
    console.log('[WS] Closed:', e.code, e.reason);
    updateMicState(); updateChatSendBtn();
    if (e.code === 4001) { showLogin('Invalid API key'); return; }
    if (e.code === 4003) { setStatus(window.t('st_origin_denied'), 'error'); return; }
    if (e.code === 4029) { setStatus(window.t('st_too_many'), 'error'); return; }
    if (autoReconnect) setTimeout(connect, 3000);
  };
  ws.onerror = (e) => console.error('[WS] Error', e);

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === 'stt' || msg.type === 'error') console.log('[WS]', msg.type, msg.text || msg.message || '');
    if (msg.type === 'status') {
      setStatus(msg.message, 'loading');
    } else if (msg.type === 'stt') {
      if (bargeInEnabled) player.stop();  // Limpiar cola de audio anterior
      removeShimmer();
      addMessage('user', msg.text, msg.lang);
      addShimmer();
      setStatus(window.t('st_generating'), 'loading');
    } else if (msg.type === 'audio') {
      removeShimmer();
      addMessage('bot', msg.sentence);
      player.add(msg.data);
    } else if (msg.type === 'voice_preview') {
      player.add(msg.data);
      testVoiceBtn.disabled = false;
      testVoiceBtn.textContent = window.t('test');
    } else if (msg.type === 'done') {
      removeShimmer();
      let info = '';
      if (msg.ttfa) info += `TTFA: ${msg.ttfa}s`;
      if (msg.total) info += ` | Total: ${msg.total}s`;
      if (info) addTiming(info);
      setStatus(window.t('st_ready'), 'ready');
    } else if (msg.type === 'vad') {
      if (msg.speaking) {
        if (bargeInEnabled) player.stop();
        document.body.classList.add('speaking');
      } else {
        document.body.classList.remove('speaking');
        addShimmer();
      }
    } else if (msg.type === 'history_cleared') {
    } else if (msg.type === 'error') {
      removeShimmer();
      setStatus(msg.message, 'error');
    }
  };
}

// --- OpenAI Realtime API transport (/v1/realtime) ------------------------
// Streaming PCM player (raw pcm16 deltas, unlike the WAV-per-sentence AudioQueue).
class PCMPlayer {
  constructor(rate) { this.rate = rate; this.ctx = null; this.next = 0; this.srcs = []; }
  init() { if (!this.ctx) this.ctx = new AudioContext({ sampleRate: this.rate }); if (this.ctx.state === 'suspended') this.ctx.resume(); }
  get playing() { return this.srcs.length > 0; }
  enqueue(int16) {
    if (!this.ctx) this.init();
    const f = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f[i] = int16[i] / 32768;
    const buf = this.ctx.createBuffer(1, f.length, this.rate);
    buf.copyToChannel(f, 0);
    const s = this.ctx.createBufferSource(); s.buffer = buf; s.connect(this.ctx.destination);
    const t = Math.max(this.ctx.currentTime, this.next); s.start(t); this.next = t + buf.duration;
    this.srcs.push(s); s.onended = () => { this.srcs = this.srcs.filter(x => x !== s); };
  }
  stop() { this.srcs.forEach(s => { try { s.stop(); } catch (e) {} }); this.srcs = []; this.next = 0; }
}
const rtPlayer = new PCMPlayer(24000);

function _abToB64(ab) { const b = new Uint8Array(ab); let s = ''; const CH = 0x8000; for (let i = 0; i < b.length; i += CH) s += String.fromCharCode.apply(null, b.subarray(i, i + CH)); return btoa(s); }
function _b64ToInt16(b64) { const bin = atob(b64); const bytes = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i); return new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2)); }

function connectRealtime() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // Same-origin: the nemo_session cookie authenticates (browser WS can't set headers).
  ws = new WebSocket(`${proto}//${location.host}/v1/realtime`, ['realtime']);
  ws.onopen = () => {
    console.log('[RT] connected /v1/realtime');
    const sess = { type: 'realtime', output_modalities: ['audio'],
      audio: { input: { format: { type: 'audio/pcm', rate: 16000 }, turn_detection: { type: 'server_vad' } },
               output: { format: { type: 'audio/pcm', rate: 24000 } } },
      temperature: parseFloat(tempSlider.value) };
    const inst = promptInput.value.trim(); if (inst) sess.instructions = inst;
    ws.send(JSON.stringify({ type: 'session.update', session: sess }));
    rtPlayer.init(); updateMicState(); updateChatSendBtn();
    if (sessionActive && !micActive) { initMicStream(); if (emptyText) emptyText.textContent = window.t('hint_listening'); }
  };
  ws.onclose = (e) => {
    console.log('[RT] closed', e.code, e.reason); updateMicState(); updateChatSendBtn();
    if (e.code === 4001) { showLogin('Invalid API key'); return; }
    if (autoReconnect) setTimeout(connect, 3000);
  };
  ws.onerror = (e) => console.error('[RT] error', e);
  ws.onmessage = (evt) => { let m; try { m = JSON.parse(evt.data); } catch (e) { return; } handleRealtimeMsg(m); };
}

function handleRealtimeMsg(m) {
  switch (m.type) {
    case 'session.updated': setStatus(window.t('st_ready'), 'ready'); break;
    case 'input_audio_buffer.speech_started':
      if (bargeInEnabled) rtPlayer.stop();
      document.body.classList.add('speaking'); break;
    case 'input_audio_buffer.speech_stopped':
      document.body.classList.remove('speaking'); addShimmer(); break;
    case 'conversation.item.input_audio_transcription.completed':
      if (m.transcript) { removeShimmer(); addMessage('user', m.transcript); addShimmer(); setStatus(window.t('st_generating'), 'loading'); }
      break;
    case 'response.output_audio.delta':
      if (m.delta) rtPlayer.enqueue(_b64ToInt16(m.delta)); break;
    case 'response.output_audio_transcript.done':
      if (m.transcript) { removeShimmer(); addMessage('bot', m.transcript); } break;
    case 'response.done': removeShimmer(); setStatus(window.t('st_ready'), 'ready'); break;
    case 'error': removeShimmer(); setStatus((m.error && m.error.message) || 'error', 'error'); break;
  }
}

// --- Loading progress ---
let ttsReady = false;
const SVG_CHECK = '<svg viewBox="0 0 16 16"><polyline points="3.5 8 6.5 11 12.5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function updateLoadItem(model, state) {
  const el = document.getElementById('load-' + model);
  if (!el) return;
  el.className = 'load-item ' + (state === 'done' ? 'done' : state === 'loading' ? 'loading' : '');
  const check = el.querySelector('.check');
  if (state === 'done') {
    check.innerHTML = SVG_CHECK;
  } else if (state === 'loading') {
    check.innerHTML = '<div class="spinner"></div>';
  } else {
    check.innerHTML = '';
  }
}

async function waitForReady() {
  while (true) {
    try {
      const res = await _authFetch('/health');
      if (res.status === 401) { showLogin(window.t('api_required')); return; }
      const data = await res.json();
      if (data.error === 'unauthorized') { showLogin(window.t('api_required')); return; }
      if (data.models) {
        for (const [key, state] of Object.entries(data.models)) {
          updateLoadItem(key, state);
        }
      }
      if (data.tts_ready && !ttsReady) {
        ttsReady = true;
        testVoiceBtn.disabled = false;
      }
      if (data.ready) {
        modelsReady = true;
        // Hide loading bar with a delay
        setTimeout(() => loadingBar.classList.add('hidden'), 1500);
        updateChatSendBtn();
        updateMicState();
        return;
      }
    } catch {}
    await new Promise(r => setTimeout(r, 1500));
  }
}

// --- AudioWorklet PCM Streaming (hands-free mode) ---
// Processor captures at native rate, downsamples to 16kHz, outputs 512-sample int16 chunks
const PROCESSOR_CODE = `
class PCMProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = 16000;
    this.nativeRate = options.processorOptions.sampleRate || 48000;
    this.ratio = this.nativeRate / this.targetRate;
    // Output buffer: 512 samples at 16kHz = 32ms
    this.outBuffer = new Float32Array(512);
    this.outIndex = 0;
    // Resampling state: fractional position in input
    this.resamplePos = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0];

    // If native rate matches target, no resampling needed
    if (this.ratio <= 1.001) {
      for (let i = 0; i < ch.length; i++) {
        this.outBuffer[this.outIndex++] = ch[i];
        if (this.outIndex >= 512) { this._flush(); }
      }
      return true;
    }

    // Downsample: pick samples at intervals of this.ratio using linear interpolation
    for (let i = 0; i < ch.length; i++) {
      // Check if this input sample crosses the next output sample boundary
      while (this.resamplePos < i + 1) {
        const idx = Math.floor(this.resamplePos);
        const frac = this.resamplePos - idx;
        const s0 = idx < ch.length ? ch[idx] : 0;
        const s1 = idx + 1 < ch.length ? ch[idx + 1] : s0;
        this.outBuffer[this.outIndex++] = s0 + frac * (s1 - s0);
        this.resamplePos += this.ratio;
        if (this.outIndex >= 512) { this._flush(); }
      }
    }
    // Adjust resamplePos for next process() call
    this.resamplePos -= ch.length;

    return true;
  }
  _flush() {
    const int16 = new Int16Array(512);
    for (let j = 0; j < 512; j++) {
      const s = Math.max(-1, Math.min(1, this.outBuffer[j]));
      int16[j] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    this.port.postMessage(int16.buffer, [int16.buffer]);
    this.outBuffer = new Float32Array(512);
    this.outIndex = 0;
  }
}
registerProcessor('pcm-processor', PCMProcessor);
`;

async function initMicStream() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, voiceIsolation: true }
    });

    // Use native sample rate (avoids Firefox cross-rate error)
    streamCtx = new AudioContext();
    if (streamCtx.state === 'suspended') await streamCtx.resume();
    const nativeRate = streamCtx.sampleRate;

    // Load AudioWorklet processor, pass native rate for resampling
    const blob = new Blob([PROCESSOR_CODE], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    await streamCtx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);

    streamSource = streamCtx.createMediaStreamSource(micStream);
    streamProcessor = new AudioWorkletNode(streamCtx, 'pcm-processor', {
      processorOptions: { sampleRate: nativeRate }
    });

    // Tail delay (ms) after TTS playback ends — covers residual speaker decay.
    const PAUSE_MIC_TAIL_MS = 300;
    let micPausedUntil = 0;
    streamProcessor.port.onmessage = (e) => {
      const ap = (TRANSPORT === 'realtime') ? rtPlayer : player;
      // Skip mic audio while AI is speaking + a short tail after (anti-echo loop). Only when toggle ON.
      if (pauseMicReplyEnabled) {
        if (ap.playing) {
          micPausedUntil = Date.now() + PAUSE_MIC_TAIL_MS;
          return;
        }
        if (Date.now() < micPausedUntil) return;
      }
      if (muted || !ws || ws.readyState !== WebSocket.OPEN) return;
      if (TRANSPORT === 'realtime') {
        // e.data = Int16 PCM @16kHz (512 samples) → base64 → OpenAI append event
        ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: _abToB64(e.data) }));
      } else {
        ws.send(e.data);
      }
    };

    streamSource.connect(streamProcessor);
    // Don't connect to destination (no self-monitoring)

    micActive = true;
    muted = false;
    player.init();
    updateMicState();
  } catch (e) {
    console.error('[MIC] Error:', e);
    setStatus(window.t('st_mic_error'), 'error');
  }
}

function stopMicStream() {
  if (streamProcessor) { streamProcessor.disconnect(); streamProcessor = null; }
  if (streamSource) { streamSource.disconnect(); streamSource = null; }
  if (streamCtx) { streamCtx.close(); streamCtx = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  micActive = false;
  muted = false;
}

function toggleMute() {
  muted = !muted;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'mute', muted }));
  }
  updateMicState();
}

// --- UI helpers ---
function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || '';
}

function hideEmptyState() {
  if (!hasMessages && emptyState) {
    emptyState.style.display = 'none';
    hasMessages = true;
  }
}

function addMessage(role, text, lang) {
  hideEmptyState();
  clearBtn.classList.add('show');
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = role === 'user' ? 'Tu' : 'Asistente';
  if (role === 'user' && lang) {
    const badge = document.createElement('span');
    badge.className = 'lang-badge lang-' + lang;
    badge.textContent = lang.toUpperCase();
    label.appendChild(badge);
  }
  const content = document.createElement('div');
  content.textContent = text;
  div.appendChild(label);
  div.appendChild(content);
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addTiming(text) {
  const div = document.createElement('div');
  div.className = 'timing';
  div.innerHTML = text.replace(/([\d.]+s)/g, '<span>$1</span>');
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addShimmer() {
  hideEmptyState();
  removeShimmer();
  shimmerEl = document.createElement('div');
  shimmerEl.className = 'shimmer';
  chatEl.appendChild(shimmerEl);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function removeShimmer() {
  if (shimmerEl) { shimmerEl.remove(); shimmerEl = null; }
}

// --- Clear conversation ---
clearBtn.addEventListener('click', () => {
  chatEl.querySelectorAll('.msg, .timing, .shimmer').forEach(el => el.remove());
  hasMessages = false;
  emptyState.style.display = '';
  player.stop();
  clearBtn.classList.remove('show');
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'clear_history'}));
  }
});

// --- Session connect/disconnect ---
function startSession() {
  console.log('[SESSION] Starting...');
  sessionActive = true;
  autoReconnect = true;
  if (emptyText) emptyText.textContent = window.t('connecting');
  // Reconnect WS + mic (ws.onopen will call initMicStream via sessionActive check)
  connect();
  updateMicState();
}

function endSession() {
  console.log('[SESSION] Ending...');
  sessionActive = false;
  document.body.classList.remove('speaking');
  try { stopMicStream(); } catch(e) { console.error('[SESSION] stopMic error:', e); }
  try { player.stop(); } catch(e) { console.error('[SESSION] player.stop error:', e); }
  // Clear chat
  chatEl.querySelectorAll('.msg, .timing, .shimmer').forEach(el => el.remove());
  hasMessages = false;
  if (emptyState) emptyState.style.display = '';
  clearBtn.classList.remove('show');
  removeShimmer();
  // Close WebSocket (no auto-reconnect)
  autoReconnect = false;
  if (ws) { ws.close(); ws = null; }
  if (emptyText) emptyText.textContent = window.t('empty');
  updateMicState();
}

// --- Events ---
playBtn.addEventListener('click', () => startSession());
stopBtn.addEventListener('click', () => endSession());
muteBtn.addEventListener('click', () => toggleMute());

// Voice panel toggle
voiceToggle.addEventListener('click', () => {
  voicePanel.classList.toggle('open');
});

function renderVoiceGrid() {
  const filtered = allSpeakers.filter(s => {
    if (filterGender !== 'all' && s.gender !== filterGender) return false;
    if (filterCountry !== 'all' && s.country_code !== filterCountry) return false;
    return true;
  });
  const currentId = parseInt(speakerInput.value) || 50;
  voiceCount.textContent = `${filtered.length} ${window.t('voices')}`;
  voiceSelect.innerHTML = '';
  for (const s of filtered) {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = `${s.country} · ${s.gender === 'F' ? window.t('woman') : window.t('man')} · #${s.id}`;
    if (s.id === currentId) opt.selected = true;
    voiceSelect.appendChild(opt);
  }
  // Si el id actual no está en el filtro, seleccionar el primero
  if (filtered.length && !filtered.some(s => s.id === currentId)) {
    voiceSelect.value = filtered[0].id;
    saveSpeaker(filtered[0].id);
  }
}

voiceSelect.addEventListener('change', () => {
  saveSpeaker(voiceSelect.value);
  sendConfig();
});

// EN voice grid (HiFi-TTS speakers)
const EN_SPEAKERS = [
  {id: 92, label: 'F', name: 'Cori Samuel'}, {id: 6097, label: 'M', name: 'Phil Benson'},
  {id: 6670, label: 'M', name: 'Mike Pelton'}, {id: 6671, label: 'M', name: 'Tony Oliva'},
  {id: 8051, label: 'F', name: 'Maria Kasper'}, {id: 9017, label: 'M', name: 'John Van Stan'},
  {id: 9136, label: 'F', name: 'Helen Taylor'}, {id: 11614, label: 'F', name: 'Sylviamb'},
  {id: 11697, label: 'F', name: 'Celine Major'}, {id: 12787, label: 'F', name: 'LikeManyWaters'},
];
function renderVoiceGridEn() {
  const currentId = parseInt(speakerEnInput.value) || 9017;
  voiceSelectEn.innerHTML = '';
  for (const s of EN_SPEAKERS) {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = `${s.name} · ${s.label === 'F' ? window.t('woman') : window.t('man')}`;
    if (s.id === currentId) opt.selected = true;
    voiceSelectEn.appendChild(opt);
  }
  const vce = document.getElementById('voice-count-en');
  if (vce) vce.textContent = `${EN_SPEAKERS.length} ${window.t('voices')}`;
}
renderVoiceGridEn();

voiceSelectEn.addEventListener('change', () => {
  speakerEnInput.value = voiceSelectEn.value;
  sendConfig();
});

genderSelect.addEventListener('change', () => {
  filterGender = genderSelect.value;
  renderVoiceGrid();
  sendConfig();
});

countrySelect.addEventListener('change', () => {
  filterCountry = countrySelect.value;
  renderVoiceGrid();
  sendConfig();
});

speakerEnInput.addEventListener('change', () => {
  renderVoiceGridEn();
  sendConfig();
});

let promptTimer = null;
let promptSendTimer = null;
const promptSaved = document.getElementById('prompt-saved');
promptInput.addEventListener('input', () => {
  promptSaved.classList.add('show');
  clearTimeout(promptTimer);
  promptTimer = setTimeout(() => promptSaved.classList.remove('show'), 1500);
  // Enviar al servidor con debounce (500ms después de dejar de teclear)
  clearTimeout(promptSendTimer);
  promptSendTimer = setTimeout(() => {
    sendConfig();
  }, 500);
});

tempSlider.addEventListener('input', () => {
  tempValue.textContent = tempSlider.value;
  sendConfig();
});

maxTokensSlider.addEventListener('input', () => {
  maxTokensValue.textContent = maxTokensSlider.value;
  sendConfig();
});

testVoiceBtn.addEventListener('click', async () => {
  player.init();
  const speakerId = parseInt(speakerInput.value) || 50;
  testVoiceBtn.disabled = true;
  testVoiceBtn.textContent = '...';
  try {
    const res = await _authFetch(`/test_voice/${speakerId}`);
    const data = await res.json();
    if (data.error) {
      setStatus(data.error, 'error');
    } else {
      player.add(data.audio);
    }
  } catch (e) {
    setStatus(window.t('st_voice_test_error'), 'error');
  }
  testVoiceBtn.disabled = false;
  testVoiceBtn.textContent = window.t('test');
});

// Silence threshold slider
silenceSlider.addEventListener('input', () => {
  silenceValue.textContent = silenceSlider.value + 'ms';
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'vad_config', silence_ms: parseInt(silenceSlider.value), threshold: parseFloat(thresholdSlider.value) }));
  }
});

// VAD sensitivity (threshold) slider
thresholdSlider.addEventListener('input', () => {
  thresholdValue.textContent = thresholdSlider.value;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'vad_config', silence_ms: parseInt(silenceSlider.value), threshold: parseFloat(thresholdSlider.value) }));
  }
});

// --- Barge-in toggle ---
bargeinToggle.addEventListener('change', () => {
  bargeInEnabled = bargeinToggle.checked;
  bargeinLabel.textContent = bargeInEnabled ? 'On' : 'Off';
  localStorage.setItem('bargeIn', bargeInEnabled);
});

// --- Pause mic on reply toggle ---
pauseMicReplyToggle.addEventListener('change', () => {
  pauseMicReplyEnabled = pauseMicReplyToggle.checked;
  pauseMicReplyLabel.textContent = pauseMicReplyEnabled ? 'On' : 'Off';
  localStorage.setItem('pauseMicReply', pauseMicReplyEnabled);
});

// --- Chat text input ---
async function sendChatText() {
  const text = chatInput.value.trim();
  if (!text || !modelsReady) return;
  // If the user pressed Stop earlier the WS is closed. Re-open it on-demand
  // for a text-only turn (without forcing the mic on — that needs Tap to connect).
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    connect();
    const start = Date.now();
    while ((!ws || ws.readyState !== WebSocket.OPEN) && Date.now() - start < 5000) {
      await new Promise(r => setTimeout(r, 50));
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn('[CHAT] WS reconnect timeout — abort send');
      return;
    }
  }
  if (TRANSPORT === 'realtime') {
    addMessage('user', text); addShimmer();
    ws.send(JSON.stringify({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] } }));
    ws.send(JSON.stringify({ type: 'response.create' }));
    rtPlayer.init();
  } else {
    ws.send(JSON.stringify({type: 'chat', text}));
    player.init();
  }
  chatInput.value = '';
}

chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatText();
  }
});

chatSendBtn.addEventListener('click', sendChatText);

// Enable/disable send button based on state. WS open is NOT required — the
// send handler reopens it on-demand if needed (e.g. after pressing Stop).
function updateChatSendBtn() {
  chatSendBtn.disabled = !modelsReady;
}

// Transport toggle (native /ws  vs  OpenAI Realtime /v1/realtime)
const transportSelect = document.getElementById('transport-select');
if (transportSelect) {
  transportSelect.value = TRANSPORT;
  transportSelect.addEventListener('change', () => {
    TRANSPORT = transportSelect.value;
    localStorage.setItem('nemo_transport', TRANSPORT);
    if (ws) { try { ws.onclose = null; ws.close(); } catch {} ws = null; }
    player.stop(); rtPlayer.stop();
    if (sessionActive) connect();
    updateMicState();
  });
}

// Keepalive
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'ping'}));
  }
}, 25000);

// --- Logout ---
document.getElementById('logout-btn').addEventListener('click', async () => {
  // Tell the server to revoke the cookie + session.
  try { await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }); } catch {}
  // Clean local migration leftover from older builds (key used to be in localStorage).
  localStorage.removeItem('apiKey');
  currentRole = null;
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  chatEl.querySelectorAll('.msg, .timing, .shimmer').forEach(el => el.remove());
  hasMessages = false;
  emptyState.style.display = '';
  player.stop();
  clearBtn.classList.remove('show');
  applySidebar();
  switchSection('realtime');
  showLogin('Session closed');
});
