/* Vinyl Streamer — CSRF-aware fetch + auth bootstrap */
(function () {
  'use strict';

  var CSRF_COOKIE = 'vs_csrf';
  var _authMode = null; // 'login' | 'setup' | null
  var _authReady = false;
  var _authGateOpen = false;
  var _authPromptAt = 0;
  var AUTH_PROMPT_COOLDOWN_MS = 8000;

  function readCookie(name) {
    var parts = (';' + document.cookie).split('; ' + name + '=');
    if (parts.length < 2) return '';
    return decodeURIComponent(parts.pop().split(';').shift() || '');
  }

  function getCsrfToken() {
    return readCookie(CSRF_COOKIE) || '';
  }

  function shouldPromptAuth() {
    if (_authGateOpen) return false;
    var now = Date.now();
    if (now - _authPromptAt < AUTH_PROMPT_COOLDOWN_MS) return false;
    return true;
  }

  function apiFetch(url, options) {
    options = options ? Object.assign({}, options) : {};
    var headers = new Headers(options.headers || {});
    var method = String(options.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' && method !== 'TRACE') {
      var csrf = getCsrfToken();
      if (csrf && !headers.has('X-CSRF-Token')) {
        headers.set('X-CSRF-Token', csrf);
      }
    }
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    options.headers = headers;
    options.credentials = options.credentials || 'same-origin';
    return fetch(url, options).then(function (resp) {
      if (resp.status === 401 || resp.status === 403) {
        return resp.clone().json().then(function (body) {
          if (body && (body.error === 'auth_required' || body.error === 'csrf' || body.error === 'unauthorized' || body.error === 'setup_required')) {
            // Trusted LAN clients never need the gate; re-check status before
            // interrupting the UI (avoids the every-few-seconds login thrash).
            if (_authReady && body.error === 'csrf') {
              return resp;
            }
            if (shouldPromptAuth()) {
              _authReady = false;
              ensureAuth();
            }
          }
          return resp;
        }).catch(function () { return resp; });
      }
      return resp;
    });
  }

  function showAuthError(msg) {
    var el = document.getElementById('auth-error');
    if (!el) return;
    if (msg) {
      el.textContent = msg;
      el.classList.add('open');
    } else {
      el.textContent = '';
      el.classList.remove('open');
    }
  }

  function showAuthGate(status) {
    var gate = document.getElementById('auth-gate');
    if (!gate) return;
    _authGateOpen = true;
    _authPromptAt = Date.now();
    document.body.classList.add('auth-blocked');
    gate.classList.add('open');
    gate.setAttribute('aria-hidden', 'false');

    var setup = !status.password_set;
    _authMode = setup ? 'setup' : 'login';

    var title = document.getElementById('auth-title');
    var blurb = document.getElementById('auth-blurb');
    var confirmRow = document.getElementById('auth-confirm-row');
    var currentRow = document.getElementById('auth-current-row');
    var pwLabel = document.getElementById('auth-password-label');
    var pw = document.getElementById('auth-password');

    if (title) title.textContent = 'Vinyl Streamer';
    if (setup) {
      if (blurb) blurb.textContent = 'Create a password for access from outside your home network. Devices on this Wi‑Fi stay unlocked.';
      if (pwLabel) pwLabel.textContent = 'New password';
      if (confirmRow) confirmRow.style.display = '';
      if (currentRow) currentRow.style.display = 'none';
      if (pw) pw.autocomplete = 'new-password';
    } else {
      if (blurb) blurb.textContent = 'Sign in to control Vinyl Streamer from outside your home network.';
      if (pwLabel) pwLabel.textContent = 'Password';
      if (confirmRow) confirmRow.style.display = 'none';
      if (currentRow) currentRow.style.display = 'none';
      if (pw) pw.autocomplete = 'current-password';
    }
    showAuthError('');
    setTimeout(function () {
      if (pw) pw.focus();
    }, 50);
  }

  function hideAuthGate() {
    var gate = document.getElementById('auth-gate');
    if (gate) {
      gate.classList.remove('open');
      gate.setAttribute('aria-hidden', 'true');
    }
    document.body.classList.remove('auth-blocked');
    _authMode = null;
    _authGateOpen = false;
    showAuthError('');
  }

  async function submitAuth() {
    showAuthError('');
    var pwEl = document.getElementById('auth-password');
    var confirmEl = document.getElementById('auth-confirm');
    var password = pwEl ? pwEl.value : '';
    if (!password) {
      showAuthError('Password is required.');
      return;
    }
    var url = _authMode === 'setup' ? '/api/auth/setup' : '/api/auth/login';
    var body = { password: password };
    if (_authMode === 'setup') {
      var confirm = confirmEl ? confirmEl.value : '';
      if (password !== confirm) {
        showAuthError('Passwords do not match.');
        return;
      }
      if (password.length < 8) {
        showAuthError('Use at least 8 characters.');
        return;
      }
    }
    var btn = document.getElementById('auth-submit');
    if (btn) btn.disabled = true;
    try {
      var resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(body),
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok || data.ok === false) {
        showAuthError((data && (data.message || data.error)) || 'Authentication failed');
        return;
      }
      hideAuthGate();
      _authReady = true;
      if (typeof window.startApp === 'function') window.startApp();
    } catch (e) {
      showAuthError('Network error — try again.');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function ensureAuth() {
    try {
      var resp = await fetch('/api/auth/status', {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
        cache: 'no-store',
      });
      var status = await resp.json();
      // Same-LAN / loopback: never show a login gate.
      if (status.authenticated || status.trusted || status.loopback) {
        hideAuthGate();
        _authReady = true;
        if (typeof window.startApp === 'function') window.startApp();
        return status;
      }
      // First boot on a remote client: ask for a password so WAN access is locked.
      showAuthGate(status);
      return status;
    } catch (e) {
      // If status endpoint is unreachable, still try to boot (dev / offline).
      hideAuthGate();
      _authReady = true;
      if (typeof window.startApp === 'function') window.startApp();
      return { ok: false, authenticated: true, trusted: true, loopback: true };
    }
  }

  window.apiFetch = apiFetch;
  window.getCsrfToken = getCsrfToken;
  window.submitAuth = submitAuth;
  window.ensureAuth = ensureAuth;

  document.addEventListener('DOMContentLoaded', function () {
    var pw = document.getElementById('auth-password');
    var confirm = document.getElementById('auth-confirm');
    function onEnter(ev) {
      if (ev.key === 'Enter') submitAuth();
    }
    if (pw) pw.addEventListener('keydown', onEnter);
    if (confirm) confirm.addEventListener('keydown', onEnter);
    ensureAuth();
  });
})();
