#!/usr/bin/env python3
"""Auth setup / login / logout / status routes."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import auth as authmod

router = APIRouter()


@router.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "ok": True,
        "password_set": authmod.password_is_set(),
        "authenticated": bool(
            authmod.is_loopback(request)
            or (
                authmod.password_is_set()
                and authmod.get_session(request.cookies.get(authmod.SESSION_COOKIE))
            )
        ),
        "loopback": authmod.is_loopback(request),
        "csrf": (
            (authmod.get_session(request.cookies.get(authmod.SESSION_COOKIE)) or {}).get("csrf")
            if authmod.password_is_set()
            else None
        ),
    }


@router.post("/api/auth/setup")
async def auth_setup(request: Request, body: dict):
    """First-boot password creation. Allowed from loopback always; from LAN only
    when no password exists yet."""
    if authmod.password_is_set() and not authmod.is_loopback(request):
        return JSONResponse(
            {"ok": False, "error": "already_configured"},
            status_code=403,
        )
    # If already configured, only loopback (kiosk) may reset via this endpoint
    # when also sending the current password.
    if authmod.password_is_set():
        current = str(body.get("current_password") or "")
        stored = authmod.load_auth().get("password_hash", "")
        if not authmod.verify_password(current, stored):
            return JSONResponse(
                {"ok": False, "error": "bad_password"},
                status_code=403,
            )
    password = str(body.get("password") or "")
    try:
        authmod.set_password(password)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    session, csrf = authmod.create_session()
    resp = JSONResponse({"ok": True, "csrf": csrf})
    authmod.attach_session_cookies(resp, session, csrf)
    return resp


@router.post("/api/auth/login")
async def auth_login(body: dict):
    if not authmod.password_is_set():
        return JSONResponse(
            {"ok": False, "error": "setup_required"},
            status_code=400,
        )
    password = str(body.get("password") or "")
    stored = authmod.load_auth().get("password_hash", "")
    if not authmod.verify_password(password, stored):
        return JSONResponse(
            {"ok": False, "error": "bad_password", "message": "Incorrect password"},
            status_code=401,
        )
    session, csrf = authmod.create_session()
    resp = JSONResponse({"ok": True, "csrf": csrf})
    authmod.attach_session_cookies(resp, session, csrf)
    return resp


@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(authmod.SESSION_COOKIE)
    authmod.destroy_session(token)
    resp = JSONResponse({"ok": True})
    authmod.clear_session_cookies(resp)
    return resp


@router.post("/api/auth/change-password")
async def auth_change_password(request: Request, body: dict):
    if not authmod.password_is_set():
        return JSONResponse({"ok": False, "error": "setup_required"}, status_code=400)
    # Middleware already authenticated non-loopback; loopback still needs current pw
    current = str(body.get("current_password") or "")
    stored = authmod.load_auth().get("password_hash", "")
    if (not authmod.is_loopback(request) or current) and not authmod.verify_password(current, stored):
        return JSONResponse(
            {"ok": False, "error": "bad_password"},
            status_code=403,
        )
    try:
        authmod.set_password(str(body.get("password") or ""))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    session, csrf = authmod.create_session()
    resp = JSONResponse({"ok": True, "csrf": csrf})
    authmod.attach_session_cookies(resp, session, csrf)
    return resp
