#!/usr/bin/env python3
"""Vinyl AirPlay: system routes (TLS cert download/info/generation, self-update, wifi portal).

Self-contained: the git/update helpers live here too, since only these routes
use them. Shares the AppState broadcast helper via app_state.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from app_state import broadcast

router = APIRouter()


def _repo_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _pip_cmd() -> list[str]:
    """Return a pip argv that targets this app's venv, never system pip.

    Raspberry Pi OS Bookworm marks the system Python as externally managed
    (PEP 668). Bare `pip3` then fails during self-update. Prefer the venv
    next to the repo (install.sh uses ``venv/``; some checkouts use ``.venv/``),
    then fall back to ``python -m pip`` for the interpreter that is already
    running the service.
    """
    base = _repo_dir()
    for rel in ("venv/bin/pip", ".venv/bin/pip", "venv/bin/pip3", ".venv/bin/pip3"):
        candidate = os.path.join(base, rel)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]
    return [sys.executable, "-m", "pip"]


def _install_requirements() -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_pip_cmd(), "install", "-r", "requirements.txt"],
        capture_output=True,
        text=True,
        cwd=_repo_dir(),
        timeout=180,
    )


def _git(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=_repo_dir(),
        timeout=timeout,
    )


def _get_git_commit() -> str:
    try:
        result = _git("rev-parse", "HEAD", timeout=5)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _fetch_origin() -> tuple[bool, str]:
    """Fetch origin so origin/main is fresh. Returns (ok, error_message)."""
    try:
        # Prune stale remote refs; fail loudly so the UI does not claim
        # "up to date" when we never reached GitHub.
        result = _git("fetch", "--prune", "origin", timeout=30)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "git fetch failed").strip()
            return False, err
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "git fetch timed out"
    except Exception as e:
        return False, str(e)


def _count_commits_behind() -> int:
    try:
        result = _git("rev-list", "--count", "HEAD..origin/main", timeout=5)
        if result.returncode == 0:
            return int(result.stdout.strip() or "0")
        return 0
    except Exception:
        return 0


@router.get("/api/system/cert")
async def download_cert():
    """Serve the CA root certificate for mobile trust installation."""
    base = os.path.dirname(os.path.abspath(__file__))
    # Prefer CA root (mkcert) over server cert (legacy self-signed)
    ca_path = os.path.join(base, "certs", "rootCA.pem")
    if os.path.exists(ca_path):
        return FileResponse(ca_path, media_type="application/x-pem-file",
                            filename="vinyl-streamer-ca.pem")
    cert_path = os.path.join(base, "certs", "cert.pem")
    if os.path.exists(cert_path):
        return FileResponse(cert_path, media_type="application/x-pem-file",
                            filename="vinyl-streamer.pem")
    return JSONResponse({"error": "No certificate found"}, status_code=404)


@router.get("/api/system/cert-info")
async def cert_info():
    """Get certificate status info for the settings UI."""
    base = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(base, "certs", "cert.pem")
    ca_path = os.path.join(base, "certs", "rootCA.pem")
    info = {
        "has_cert": os.path.exists(cert_path),
        "has_ca": os.path.exists(ca_path),
        "hostname": socket.gethostname(),
        "ip": None,
        "expiry": None,
        "sans": [],
    }
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        info["ip"] = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    if info["has_cert"]:
        try:
            out = subprocess.run(
                ["openssl", "x509", "-in", cert_path, "-noout", "-enddate", "-ext", "subjectAltName"],
                capture_output=True, text=True, timeout=5
            ).stdout
            for line in out.splitlines():
                if "notAfter" in line:
                    info["expiry"] = line.split("=", 1)[-1].strip()
                if "DNS:" in line or "IP:" in line:
                    info["sans"] = [s.strip() for s in line.split(",") if "DNS:" in s or "IP:" in s]
        except Exception:
            pass
    return info


@router.post("/api/system/generate-certs")
async def generate_certs():
    """Regenerate HTTPS certificates with current hostname and IP."""
    base = os.path.dirname(os.path.abspath(__file__))
    cert_dir = os.path.join(base, "certs")
    os.makedirs(cert_dir, exist_ok=True)

    mkcert = shutil.which("mkcert")
    if not mkcert:
        return JSONResponse({"error": "mkcert not installed. Run the install script first."}, status_code=500)

    hostname = socket.gethostname()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"

    env = os.environ.copy()
    env["CAROOT"] = cert_dir

    # Create CA if it doesn't exist
    if not os.path.exists(os.path.join(cert_dir, "rootCA.pem")):
        subprocess.run([mkcert, "-install"], env=env, capture_output=True, timeout=30)

    # Generate server cert
    result = subprocess.run(
        [mkcert, "-cert-file", os.path.join(cert_dir, "cert.pem"),
         "-key-file", os.path.join(cert_dir, "key.pem"),
         f"{hostname}.local", hostname, ip, "localhost", "127.0.0.1"],
        env=env, capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        return JSONResponse({"error": f"mkcert failed: {result.stderr}"}, status_code=500)

    return {"ok": True, "message": f"Certificates generated for {hostname}.local, {ip}. Restart the app to use them."}


def _check_update_sync() -> dict:
    """Always fetch first, then compare HEAD to origin/main.

    The old path compared against a possibly-stale local origin/main and only
    fetched when they already differed — so a Pi that had never fetched after
    a merge always reported "up to date".
    """
    current = _get_git_commit()
    fetched, fetch_error = _fetch_origin()

    latest = current
    try:
        result = _git("rev-parse", "origin/main", timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            latest = result.stdout.strip()
    except Exception:
        pass

    behind = _count_commits_behind() if fetched else 0
    available = fetched and latest != current and behind > 0
    # Treat any divergence after a successful fetch as an update, even if
    # rev-list count is weird (e.g. rewritten history).
    if fetched and latest != current:
        available = True
        if behind == 0:
            behind = 1

    return {
        "ok": fetched,
        "available": available,
        "current_commit": current,
        "latest_commit": latest,
        "commits_behind": behind,
        "fetch_error": fetch_error or None,
    }


@router.get("/api/system/check-update")
async def check_update():
    # Git fetch/rev-list block for up to ~20s; keep them off the event loop.
    return await asyncio.to_thread(_check_update_sync)


_update_rollback_hash = None

@router.post("/api/system/update")
async def perform_update():
    global _update_rollback_hash
    try:
        _update_rollback_hash = _get_git_commit()
        await broadcast("update_status", {
            "status": "pulling",
            "message": "Fetching latest code..."
        })

        result = subprocess.run(
            ["git", "pull", "origin", "main"],
            capture_output=True,
            text=True,
            cwd=_repo_dir(),
            timeout=60,
        )
        if result.returncode != 0:
            raise Exception(f"git pull failed: {result.stderr}")

        await broadcast("update_status", {
            "status": "installing",
            "message": "Installing dependencies..."
        })

        # Prefer on-disk helper after pull so a future change to install logic
        # is picked up even before the process restarts. Fall back to in-process
        # helper if import fails for any reason.
        result = subprocess.run(
            [sys.executable, "-c",
             "from routes_system import _install_requirements; "
             "r=_install_requirements(); "
             "import sys; "
             "sys.stderr.write(r.stderr or ''); "
             "sys.stdout.write(r.stdout or ''); "
             "sys.exit(r.returncode)"],
            capture_output=True,
            text=True,
            cwd=_repo_dir(),
            timeout=180,
        )
        if result.returncode != 0:
            # In-process fallback (same interpreter the service is running).
            result = _install_requirements()
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "pip install failed").strip()
            raise Exception(f"pip install failed: {err}")

        await broadcast("update_status", {
            "status": "restarting",
            "message": "Restarting application..."
        })

        subprocess.run(
            ["systemctl", "restart", "vinyl-airplay"],
            timeout=5
        )

        return {"status": "success", "message": "Update complete. Application restarting..."}

    except Exception as e:
        error_msg = str(e)
        await broadcast("update_status", {
            "status": "error",
            "message": f"Update failed: {error_msg}"
        })

        if _update_rollback_hash and _update_rollback_hash != _get_git_commit():
            try:
                subprocess.run(
                    ["git", "reset", "--hard", _update_rollback_hash],
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    timeout=10
                )
                await broadcast("update_status", {
                    "status": "rolled_back",
                    "message": "Rolled back to previous version"
                })
            except Exception:
                pass

        return {"status": "error", "message": error_msg}


@router.post("/api/wifi/reconfigure")
async def wifi_reconfigure():
    try:
        subprocess.run(
            ["systemctl", "start", "vinyl-wifi-setup"],
            timeout=5
        )
        return {"status": "portal_started"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
