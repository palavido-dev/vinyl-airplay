#!/usr/bin/env python3
"""Setup password + signed session cookies for the Vinyl Streamer control plane.

Trust model:
- Loopback clients (kiosk Chromium on 127.0.0.1 / ::1) are always allowed.
- LAN / remote clients need a setup password (first boot) and a session cookie.
- Mutating HTTP methods also require an X-CSRF-Token header matching the session.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

AUTH_FILE = Path("auth.json")
SECRET_FILE = Path(".auth_secret")

SESSION_COOKIE = "vs_session"
CSRF_COOKIE = "vs_csrf"
SESSION_TTL_SECS = 60 * 60 * 24 * 14  # 14 days
PBKDF2_ITERATIONS = 260_000

# Paths that never require a session (setup / login / static shell).
PUBLIC_PREFIXES = (
    "/api/auth/",
    "/static/",
    "/manifest.json",
    "/favicon",
)
PUBLIC_EXACT = {
    "/",
    "/api/auth/status",
}

_lock = threading.Lock()
_sessions: dict[str, dict] = {}  # token -> {expires, csrf}


def _load_secret() -> bytes:
    if SECRET_FILE.exists():
        raw = SECRET_FILE.read_text().strip()
        if raw:
            return raw.encode("utf-8")
    secret = secrets.token_hex(32)
    SECRET_FILE.write_text(secret + "\n")
    with contextlib.suppress(OSError):
        os.chmod(SECRET_FILE, 0o600)
    return secret.encode("utf-8")


_SECRET = _load_secret()


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def load_auth() -> dict:
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_auth(data: dict) -> None:
    _atomic_write_json(AUTH_FILE, data)


def password_is_set() -> bool:
    return bool(load_auth().get("password_hash"))


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters_s, salt_hex, digest_hex = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iters
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def set_password(password: str) -> None:
    password = (password or "").strip()
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    data = load_auth()
    data["password_hash"] = hash_password(password)
    data["updated_at"] = time.time()
    save_auth(data)
    # Rotate all sessions when the password changes.
    with _lock:
        _sessions.clear()


def create_session() -> tuple[str, str]:
    """Return (session_token, csrf_token)."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {
            "expires": time.time() + SESSION_TTL_SECS,
            "csrf": csrf,
        }
    return token, csrf


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def _purge_expired() -> None:
    now = time.time()
    dead = [k for k, v in _sessions.items() if v["expires"] < now]
    for k in dead:
        _sessions.pop(k, None)


def get_session(token: str | None) -> dict | None:
    if not token:
        return None
    with _lock:
        _purge_expired()
        sess = _sessions.get(token)
        if not sess:
            return None
        if sess["expires"] < time.time():
            _sessions.pop(token, None)
            return None
        # Sliding expiry
        sess["expires"] = time.time() + SESSION_TTL_SECS
        return dict(sess)


def is_loopback(request: Request) -> bool:
    client = request.client
    if not client:
        return False
    host = (client.host or "").lower()
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    # Some proxies present IPv4-mapped IPv6
    return bool(host.startswith("::ffff:") and host.endswith("127.0.0.1"))


def is_public_path(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def attach_session_cookies(response: Response, session: str, csrf: str) -> None:
    secure = False  # LAN HTTP kiosk; HTTPS optional via mkcert
    response.set_cookie(
        SESSION_COOKIE,
        session,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_TTL_SECS,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,  # JS must read for X-CSRF-Token
        samesite="lax",
        secure=secure,
        max_age=SESSION_TTL_SECS,
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def public_settings(settings: dict) -> dict:
    """Return settings safe for unauthenticated / general status responses."""
    out = dict(settings)
    token = out.get("discogs_token") or ""
    if token:
        out["discogs_token"] = ("*" * max(0, len(token) - 4)) + token[-4:]
        out["discogs_token_set"] = True
    else:
        out["discogs_token"] = ""
        out["discogs_token_set"] = False
    # Never expose password-related keys if they ever land in settings.
    out.pop("password", None)
    out.pop("password_hash", None)
    out.pop("auth_secret", None)
    return out


ALLOWED_SETTINGS_KEYS = {
    "saved_devices", "volume", "audio_device_index", "audio_device_card",
    "bass", "treble", "discogs_token", "discogs_username", "hidden_devices",
    "auto_stream_enabled", "auto_stream_device", "device_names",
    "audio_storage_path", "device_volumes", "http_stream_enabled",
    "http_stream_bitrate_kbps", "audio_detect_threshold",
    "max_browser_listeners", "app_name", "crossfade_secs",
    "adc_auto_gain_enabled", "adc_gain_db", "eq_bands", "eq_preset",
    "screensaver_minutes", "resume_playback",
}


def filter_settings_update(incoming: dict) -> dict:
    """Allowlist keys for blind settings merges (restore / bulk update)."""
    return {k: v for k, v in incoming.items() if k in ALLOWED_SETTINGS_KEYS}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path or "/"

        if is_public_path(path) or is_loopback(request):
            request.state.auth_ok = True
            request.state.auth_loopback = is_loopback(request)
            return await call_next(request)

        # Until a password is configured, only public + loopback are allowed.
        # Remote clients may only hit setup/login.
        if not password_is_set():
            return JSONResponse(
                {"ok": False, "error": "setup_required",
                 "message": "Set up a password from the kiosk or via /api/auth/setup"},
                status_code=401,
            )

        token = request.cookies.get(SESSION_COOKIE)
        sess = get_session(token)
        if not sess:
            return JSONResponse(
                {"ok": False, "error": "unauthorized",
                 "message": "Login required"},
                status_code=401,
            )

        method = request.method.upper()
        if method not in ("GET", "HEAD", "OPTIONS"):
            csrf_header = request.headers.get("x-csrf-token") or ""
            if not csrf_header or not hmac.compare_digest(csrf_header, sess["csrf"]):
                return JSONResponse(
                    {"ok": False, "error": "csrf",
                     "message": "Missing or invalid CSRF token"},
                    status_code=403,
                )

        request.state.auth_ok = True
        request.state.auth_loopback = False
        request.state.session_token = token
        return await call_next(request)
