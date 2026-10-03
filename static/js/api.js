/* Vinyl Streamer — CSRF-aware fetch + auth bootstrap */
(function () {
  'use strict';

  var CSRF_COOKIE = 'vs_csrf';
  var _authMode = null; // 'login' | 'setup' | null
  var _authReady = false;

  function readCookie(name) {
    var parts = (';' + document.cookie).split('; ' + name + '=');
    if (parts.length < 2) return '';
    return decodeURIComponent(parts.pop().split(';').shift() || '');
  }

  function getCsrfToken() {
    return readCookie(CSRF_COOKIE) || '';
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
            _authReady = false;
            showAuthGate({ password_set: body.error !== 'setup_required', authenticated: false, loopback: false });
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
      if (blurb) blurb.textContent = 'Create a password to protect access from other devices on your network. The kiosk on this machine stays unlocked.';
      if (pwLabel) pwLabel.textContent = 'New password';
      if (confirmRow) confirmRow.style.display = '';
      if (currentRow) currentRow.style.display = 'none';
      if (pw) pw.autocomplete = 'new-password';
    } else {
      if (blurb) blurb.textContent = 'Sign in to control Vinyl Streamer from this device.';
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
      // First boot: require password setup even on the kiosk (loopback).
      // After a password exists, loopback stays unlocked without a login form.
      if (!status.password_set) {
        showAuthGate(status);
        return status;
      }
      if (status.authenticated || status.loopback) {
        hideAuthGate();
        _authReady = true;
        if (typeof window.startApp === 'function') window.startApp();
        return status;
      }
      showAuthGate(status);
      return status;
    } catch (e) {
      // If status endpoint is unreachable, still try to boot (dev / offline).
      hideAuthGate();
      _authReady = true;
      if (typeof window.startApp === 'function') window.startApp();
      return { ok: false, authenticated: true, loopback: true };
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
