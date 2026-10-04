#!/usr/bin/env python3
"""Vinyl AirPlay: AirPlay 2 transport via Music Assistant's cliairplay binary.

Spawns one cliairplay process per receiver, feeds raw s16le 44.1 kHz stereo PCM
on stdin, and drives session lifecycle over a named command pipe. A shared
``cliairplay --ptp-daemon`` provides the PTP grandmaster used for multi-room
and stereo-pair sync (UDP 319/320).

The binary is GPL-3 and is downloaded at install time (not vendored). See
docs/airplay2.md.
"""

from __future__ import annotations

import json
import os
import platform
import re
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

SAMPLE_RATE = 44100
CHANNELS = 2
BITS = 16

CLIAIRPLAY_VERSION = "v0.5.5"
CLIAIRPLAY_REPO = "music-assistant/airplay-cli"
CREDENTIALS_FILE = Path("data/airplay2_credentials.json")

# AirPlay features bits (same as pyatv / MA): AP2 if either is set.
_SUPPORTS_UNIFIED_MEDIA_CONTROL = 1 << 38
_SUPPORTS_COREUTILS_PAIRING = 1 << 48
_SUPPORTS_PTP = 1 << 45

_CREDENTIALS_RE = re.compile(r"^CREDENTIALS:\s*([0-9A-Fa-f]{192})\s*$")
_STATUS_CONNECTED_RE = re.compile(r"\[STATUS\]\s+connected")
_STATUS_STARTED_RE = re.compile(r"\[STATUS\]\s+started")
_STATUS_CLOCK_READY_RE = re.compile(
    r"\[STATUS\]\s+clock_ready\b.*\bstate=(ready|probing|cold|stalled)"
)


def parse_airplay_features(features_value: str | None) -> int:
    """Parse an AirPlay features TXT value into an int bitmask."""
    if not features_value:
        return 0
    try:
        parts = str(features_value).split(",")
        features = int(parts[0], 16)
        if len(parts) > 1:
            features |= int(parts[1], 16) << 32
        return features
    except (TypeError, ValueError):
        return 0


def supports_airplay2(features_value: str | None) -> bool:
    """True when mDNS features advertise AirPlay 2 capability."""
    features = parse_airplay_features(features_value)
    return bool(
        (features & _SUPPORTS_UNIFIED_MEDIA_CONTROL)
        or (features & _SUPPORTS_COREUTILS_PAIRING)
    )


def supports_ptp(features_value: str | None) -> bool:
    return bool(parse_airplay_features(features_value) & _SUPPORTS_PTP)


def serialize_txt(properties: dict | None) -> str:
    """Serialize mDNS TXT properties for cliairplay ``--txt``."""
    if not properties:
        return ""
    pairs = []
    for key, value in properties.items():
        if value is None:
            continue
        k, v = str(key), str(value)
        if any(c.isspace() for c in k) or any(c.isspace() for c in v):
            continue
        pairs.append(f"{k}={v}")
    return " ".join(pairs)


def _binary_asset_name() -> str | None:
    system = platform.system().lower().replace("darwin", "macos")
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64" if system == "macos" else "aarch64"
    else:
        return None
    if system not in ("linux", "macos"):
        return None
    return f"cliairplay-{system}-{arch}"


def candidate_binary_paths() -> list[Path]:
    """Ordered search paths for the cliairplay binary."""
    asset = _binary_asset_name() or "cliairplay"
    here = Path(__file__).resolve().parent
    paths = [
        here / "bin" / "cliairplay",
        here / "bin" / asset,
        Path("/opt/vinyl-streamer/bin/cliairplay"),
        Path("/usr/local/bin/cliairplay"),
        Path("/usr/bin/cliairplay"),
    ]
    env = os.environ.get("CLIAIRPLAY_PATH")
    if env:
        paths.insert(0, Path(env))
    return paths


def find_cliairplay() -> str | None:
    """Return path to a working cliairplay binary, or None."""
    for path in candidate_binary_paths():
        if not path.is_file():
            continue
        try:
            result = subprocess.run(
                [str(path), "--check"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "cliairplay" in out.lower():
            return str(path)
    return None


def download_cliairplay(dest_dir: Path | None = None) -> str:
    """Download the release binary for this platform into dest_dir/bin.

    Raises RuntimeError when the platform is unsupported or the download fails.
    """
    asset = _binary_asset_name()
    if not asset:
        raise RuntimeError(
            f"Unsupported platform for cliairplay: "
            f"{platform.system()}/{platform.machine()}"
        )
    dest_dir = dest_dir or Path(__file__).resolve().parent
    bin_dir = dest_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / "cliairplay"
    url = (
        f"https://github.com/{CLIAIRPLAY_REPO}/releases/download/"
        f"{CLIAIRPLAY_VERSION}/{asset}"
    )
    print(f"[airplay2] Downloading {url}")
    tmp = dest.with_suffix(".download")
    try:
        urllib.request.urlretrieve(url, tmp)  # noqa: S310 — fixed release URL
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
    finally:
        with suppress(OSError):
            tmp.unlink()
    check = subprocess.run(
        [str(dest), "--check"], capture_output=True, text=True, timeout=15,
    )
    if check.returncode != 0:
        raise RuntimeError(f"cliairplay --check failed: {check.stderr or check.stdout}")
    print(f"[airplay2] Installed {dest} ({CLIAIRPLAY_VERSION})")
    return str(dest)


def ensure_cliairplay() -> str | None:
    """Find cliairplay, or attempt a one-shot download into ./bin."""
    found = find_cliairplay()
    if found:
        return found
    try:
        return download_cliairplay()
    except Exception as e:
        print(f"[airplay2] cliairplay unavailable: {e}")
        return None


# ── Credentials store ─────────────────────────────────────────────────────────

def load_credentials() -> dict:
    if not CREDENTIALS_FILE.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_FILE.read_text())
    except Exception:
        return {}


def save_credentials(store: dict) -> None:
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDENTIALS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n")
    os.replace(tmp, CREDENTIALS_FILE)


def get_credentials(device_id: str) -> dict | None:
    entry = load_credentials().get(device_id)
    if not entry or not entry.get("auth"):
        return None
    return entry


def set_credentials(device_id: str, *, auth: str, dacp_id: str,
                    address: str | None = None, port: int | None = None,
                    name: str | None = None) -> None:
    store = load_credentials()
    store[device_id] = {
        "auth": auth,
        "dacp_id": dacp_id,
        "address": address,
        "port": port,
        "name": name,
        "updated_at": time.time(),
    }
    save_credentials(store)


def stable_dacp_id(device_id: str) -> str:
    """Stable HAP/DACP identity for a device (must match pair-setup)."""
    existing = get_credentials(device_id)
    if existing and existing.get("dacp_id"):
        return existing["dacp_id"]
    # Prefer a hex-looking suffix from the pyatv identifier; else hash.
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", str(device_id))
    if len(cleaned) >= 12:
        return cleaned[-12:].upper()
    import hashlib
    return hashlib.sha1(str(device_id).encode()).hexdigest()[:12].upper()


def active_remote_id(dacp_id: str) -> str:
    """Active-Remote ID derived from the DACP id (uint32 of lower hex)."""
    hex_str = re.sub(r"[^0-9A-Fa-f]", "", dacp_id) or "0"
    try:
        return str(int(hex_str, 16) & 0xFFFFFFFF)
    except ValueError:
        return "1"


# ── PTP daemon ────────────────────────────────────────────────────────────────

class PtpDaemon:
    """Process-wide shared PTP grandmaster (one per host, refcounted)."""

    _lock = threading.Lock()
    _proc: subprocess.Popen | None = None
    _refs = 0
    _binary: str | None = None

    @classmethod
    def acquire(cls, binary: str) -> bool:
        with cls._lock:
            if cls._proc and cls._proc.poll() is None:
                cls._refs += 1
                return True
            # Restart if a previous daemon died
            cls._stop_unlocked()
            try:
                cls._proc = subprocess.Popen(
                    [binary, "--ptp-daemon"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except OSError as e:
                print(f"[airplay2] Failed to start PTP daemon: {e}")
                cls._proc = None
                return False
            # Brief settle — bind of 319/320 fails fast with exit 2 when
            # unprivileged / already bound.
            time.sleep(0.3)
            if cls._proc.poll() is not None:
                err = ""
                with suppress(Exception):
                    err = (cls._proc.stderr.read() or b"").decode("utf-8", "ignore")[:200]
                print(f"[airplay2] PTP daemon exited immediately "
                      f"(code={cls._proc.returncode}): {err.strip()}")
                cls._proc = None
                return False
            cls._binary = binary
            cls._refs = 1
            print("[airplay2] PTP daemon started")
            return True

    @classmethod
    def release(cls) -> None:
        with cls._lock:
            if cls._refs <= 0:
                return
            cls._refs -= 1
            if cls._refs == 0:
                cls._stop_unlocked()

    @classmethod
    def _stop_unlocked(cls) -> None:
        proc, cls._proc = cls._proc, None
        if not proc:
            return
        with suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)
        with suppress(Exception):
            if proc.poll() is None:
                proc.kill()
        print("[airplay2] PTP daemon stopped")

    @classmethod
    def ready(cls) -> bool:
        with cls._lock:
            return cls._proc is not None and cls._proc.poll() is None


# ── Stream sink ───────────────────────────────────────────────────────────────

class CliAirPlayStream:
    """PCM sink that streams to one AirPlay 2 (or auto) receiver via cliairplay.

    Same ``put()`` / ``stop()`` contract as ``LocalOutputStream`` /
    ``AsyncAudioStream`` so it can join the capture fan-out unchanged.
    """

    CONNECT_TIMEOUT_SECS = 12.0
    START_TIMEOUT_SECS = 8.0

    def __init__(
        self,
        host: str,
        *,
        port: int = 7000,
        volume: int = 80,
        name: str | None = None,
        device_id: str | None = None,
        auth: str | None = None,
        dacp_id: str | None = None,
        txt: str = "",
        protocol: str = "auto",
        use_ptp_shared: bool = True,
        binary: str | None = None,
        defer_start: bool = False,
        timing: str | None = None,
    ):
        self.host = host
        self.port = int(port or 7000)
        self.volume = max(0, min(100, int(volume)))
        self.name = name or host
        self.device_id = device_id or host
        self.auth = auth
        self.dacp_id = dacp_id or stable_dacp_id(self.device_id)
        self.txt = txt or ""
        self.protocol = protocol
        self.use_ptp_shared = use_ptp_shared
        self.binary = binary or find_cliairplay()
        self.defer_start = defer_start
        # Apple TVs often advertise SupportsPTP but never answer clock probes;
        # force NTP so audio actually renders (cliairplay --timing ntp).
        self.timing = timing

        self._proc: subprocess.Popen | None = None
        self._cmd_fd: int | None = None
        self._cmd_path: str | None = None
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._io_lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._started = threading.Event()
        self._ptp_held = False
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._art_path: str | None = None

    def start(self) -> None:
        if not self.binary:
            raise RuntimeError("cliairplay binary not found")
        if self._proc:
            return

        if self.use_ptp_shared:
            self._ptp_held = PtpDaemon.acquire(self.binary)

        self._tmpdir = tempfile.TemporaryDirectory(prefix="vs-ap2-")
        self._cmd_path = os.path.join(self._tmpdir.name, "cmdpipe")
        os.mkfifo(self._cmd_path)

        args = [
            self.binary,
            "--protocol", self.protocol,
            "--port", str(self.port),
            "--volume", str(self.volume),
            "--samplerate", str(SAMPLE_RATE),
            "--bitdepth", str(BITS),
            "--channels", str(CHANNELS),
            "--dacp", self.dacp_id,
            "--activeremote", active_remote_id(self.dacp_id),
            "--cmdpipe", self._cmd_path,
            "--name", self.name,
        ]
        if self.txt:
            args += ["--txt", self.txt]
        if self.timing in ("ptp", "ntp", "auto"):
            args += ["--timing", self.timing]
        if self.auth and len(self.auth) == 192:
            args += ["--auth", self.auth]
        elif self.protocol in ("airplay2", "auto"):
            # Transient pairing when we have no stored HAP credentials.
            args += ["--ap2-native"]
        # Shared PTP only when we are not forcing NTP (Apple TV escape hatch).
        if self._ptp_held and self.timing != "ntp":
            args += ["--ptp-shared"]
        args.append(self.host)

        print(f"[airplay2] Connecting to {self.name} ({self.host}:{self.port})…")
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Open the fifo for writing after the child is up so we don't block
        # forever if the binary exits before opening the read end.
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name=f"ap2-err-{self.name[:12]}"
        )
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name=f"ap2-out-{self.name[:12]}"
        )
        self._stderr_thread.start()
        self._stdout_thread.start()

        # Non-blocking open of the write end: retry until the child opens read.
        deadline = time.monotonic() + self.CONNECT_TIMEOUT_SECS
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"cliairplay exited before connect (code={self._proc.returncode})"
                )
            try:
                self._cmd_fd = os.open(self._cmd_path, os.O_WRONLY | os.O_NONBLOCK)
                # Switch to blocking writes for command lines.
                flags = fcntl_get_flags(self._cmd_fd)
                fcntl_set_flags(self._cmd_fd, flags & ~os.O_NONBLOCK)
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.stop()
            raise RuntimeError(f"Timed out opening cmdpipe for {self.name}")

        if not self._connected.wait(timeout=self.CONNECT_TIMEOUT_SECS):
            # Some builds emit connected only on stderr after route settle;
            # if the process is still alive, proceed and let START fail loudly.
            if self._proc.poll() is not None:
                self.stop()
                raise RuntimeError(f"cliairplay died connecting to {self.name}")
            print(f"[airplay2] No connected status from {self.name}; continuing")

        if not self.defer_start:
            self.command_start(start_unix_ms=0)

        # Prefill a little silence so the ring has content before real audio.
        with suppress(Exception):
            silence = b"\x00" * (int(SAMPLE_RATE * 0.05) * CHANNELS * 2)
            self.put(silence)
        print(f"[airplay2] Streaming to {self.name}")

    def command_start(self, start_unix_ms: int = 0, join: bool = False) -> None:
        """Anchor (or re-anchor) playback at the given wall-clock instant."""
        self._started.clear()
        lines = [f"START_UNIX_MS={int(start_unix_ms)}"]
        if join:
            lines.append("START_JOIN=1")
        lines.append("ACTION=START")
        if not self._write_cmd("\n".join(lines) + "\n"):
            raise RuntimeError(f"Failed to send START to {self.name}")
        if not self._started.wait(timeout=self.START_TIMEOUT_SECS):
            print(f"[airplay2] No started ack from {self.name} (continuing)")

    def put(self, pcm_bytes: bytes) -> None:
        if self._stop.is_set():
            return
        with self._io_lock:
            proc = self._proc
            if not proc or not proc.stdin:
                return
            try:
                proc.stdin.write(pcm_bytes)
            except (BrokenPipeError, OSError) as e:
                print(f"[airplay2] Write error to {self.name}: {e}")
                self._stop.set()

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        self._write_cmd(f"VOLUME={self.volume}\n")

    def set_metadata(
        self,
        *,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        artwork: bytes | None = None,
        duration: float | None = None,
    ) -> None:
        lines = []
        if title is not None:
            lines.append(f"TITLE={_cmd_escape(title)}")
        if artist is not None:
            lines.append(f"ARTIST={_cmd_escape(artist)}")
        if album is not None:
            lines.append(f"ALBUM={_cmd_escape(album)}")
        if duration is not None:
            lines.append(f"DURATION={int(duration)}")
        if artwork:
            art_path = self._stage_artwork(artwork)
            if art_path:
                lines.append(f"ARTWORKFILE={art_path}")
        if not lines:
            return
        lines.append("ACTION=SENDMETA")
        self._write_cmd("\n".join(lines) + "\n")

    def stop(self) -> None:
        if self._stop.is_set() and self._proc is None:
            return
        self._stop.set()
        with suppress(Exception):
            self._write_cmd("ACTION=STOP\n")
        with self._io_lock:
            proc, self._proc = self._proc, None
            if proc:
                with suppress(Exception):
                    if proc.stdin:
                        proc.stdin.close()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    with suppress(Exception):
                        proc.kill()
        if self._cmd_fd is not None:
            with suppress(Exception):
                os.close(self._cmd_fd)
            self._cmd_fd = None
        if self._ptp_held:
            PtpDaemon.release()
            self._ptp_held = False
        if self._tmpdir:
            with suppress(Exception):
                self._tmpdir.cleanup()
            self._tmpdir = None
        print(f"[airplay2] Closed {self.name}")

    def _stage_artwork(self, jpeg_bytes: bytes) -> str | None:
        if not self._tmpdir or not jpeg_bytes:
            return None
        # Cap at 5 MiB (binary limit)
        if len(jpeg_bytes) > 5 * 1024 * 1024:
            return None
        path = os.path.join(self._tmpdir.name, "artwork.jpg")
        try:
            Path(path).write_bytes(jpeg_bytes)
            self._art_path = path
            return path
        except Exception:
            return None

    def _write_cmd(self, text: str) -> bool:
        if self._cmd_fd is None or self._stop.is_set():
            return False
        try:
            os.write(self._cmd_fd, text.encode("utf-8"))
            return True
        except OSError as e:
            print(f"[airplay2] cmdpipe write failed for {self.name}: {e}")
            return False

    def _read_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "ignore").rstrip()
                if not line:
                    continue
                self._handle_status_line(line)
                if "error" in line.lower() or line.startswith("[STATUS]"):
                    print(f"[airplay2:{self.name}] {line}")
        except Exception:
            pass

    def _read_stdout(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "ignore").rstrip()
                if not line:
                    continue
                self._handle_status_line(line)
        except Exception:
            pass

    def _handle_status_line(self, line: str) -> None:
        if _STATUS_CONNECTED_RE.search(line):
            self._connected.set()
        if _STATUS_STARTED_RE.search(line):
            self._started.set()


def _cmd_escape(value: str) -> str:
    """Flatten metadata values to a single cmdpipe line."""
    return str(value).replace("\n", " ").replace("\r", " ")


def fcntl_get_flags(fd: int) -> int:
    import fcntl
    return fcntl.fcntl(fd, fcntl.F_GETFL)


def fcntl_set_flags(fd: int, flags: int) -> None:
    import fcntl
    fcntl.fcntl(fd, fcntl.F_SETFL, flags)


# ── Pairing ───────────────────────────────────────────────────────────────────

class AirPlay2PairingSession:
    """Interactive HAP pair-setup via ``cliairplay --pair-setup``."""

    def __init__(self, host: str, *, port: int = 7000, device_id: str,
                 name: str | None = None, binary: str | None = None):
        self.host = host
        self.port = port
        self.device_id = device_id
        self.name = name or host
        self.binary = binary or find_cliairplay()
        self.dacp_id = stable_dacp_id(device_id)
        self._proc: subprocess.Popen | None = None
        self._stderr: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def begin(self) -> None:
        if not self.binary:
            raise RuntimeError("cliairplay binary not found — install AirPlay 2 support")
        args = [
            self.binary, "--pair-setup",
            "--port", str(self.port),
            "--dacp", self.dacp_id,
            self.host,
        ]
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()
        # Give the binary a moment to POST /pair-pin-start
        time.sleep(0.5)
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"pair-setup exited early: {self._last_error()}"
            )

    def finish(self, pin: str, timeout: float = 60.0) -> str:
        if not self._proc:
            raise RuntimeError("Pairing not started")
        try:
            self._proc.stdin.write(f"{pin}\n".encode())
            self._proc.stdin.flush()
        except Exception as e:
            self.close()
            raise RuntimeError(f"Failed to send PIN: {e}") from e

        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            if self._proc.stdout:
                # Non-blocking-ish read with short select via timeout on read
                try:
                    import select
                    ready, _, _ = select.select([self._proc.stdout], [], [], 0.5)
                    if ready:
                        chunk = self._proc.stdout.read(256)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            raw_line, buf = buf.split(b"\n", 1)
                            line = raw_line.decode("utf-8", "ignore").strip()
                            m = _CREDENTIALS_RE.match(line)
                            if m:
                                auth = m.group(1)
                                set_credentials(
                                    self.device_id,
                                    auth=auth,
                                    dacp_id=self.dacp_id,
                                    address=self.host,
                                    port=self.port,
                                    name=self.name,
                                )
                                with suppress(Exception):
                                    self._proc.wait(timeout=5)
                                self.close()
                                return auth
                except Exception:
                    time.sleep(0.2)
            else:
                time.sleep(0.2)
            if self._proc.poll() is not None and not buf:
                break
        self.close()
        raise RuntimeError(f"Pairing failed: {self._last_error()}")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if not proc:
            return
        with suppress(Exception):
            proc.kill()
        with suppress(Exception):
            proc.wait(timeout=2)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "ignore").rstrip()
                if line:
                    self._stderr.append(line)
        except Exception:
            pass

    def _last_error(self) -> str:
        for line in reversed(self._stderr):
            cleaned = line.split("Enter the PIN shown on the device:")[-1].strip()
            if cleaned and cleaned != "Pairing failed.":
                return cleaned.rsplit("] ", 1)[-1]
        return "no error details reported"


# ── Device helpers used by scan / stream routing ──────────────────────────────

def conf_airplay_properties(conf) -> dict:
    """Return AirPlay (or RAOP) TXT properties from a pyatv conf."""
    try:
        from pyatv.const import Protocol
        airplay = conf.get_service(Protocol.AirPlay)
        if airplay and airplay.properties:
            return dict(airplay.properties)
        raop = conf.get_service(Protocol.RAOP)
        if raop and raop.properties:
            return dict(raop.properties)
    except Exception:
        pass
    return {}


def conf_airplay_port(conf) -> int:
    try:
        from pyatv.const import Protocol
        airplay = conf.get_service(Protocol.AirPlay)
        if airplay and airplay.port:
            return int(airplay.port)
        raop = conf.get_service(Protocol.RAOP)
        if raop and raop.port:
            return int(raop.port)
    except Exception:
        pass
    return 7000


def conf_features_value(conf) -> str | None:
    props = conf_airplay_properties(conf)
    return props.get("features") or props.get("ft")


def conf_is_apple_tv(conf) -> bool:
    try:
        from pyatv.const import DeviceModel
        model = conf.device_info.model
        return model in {
            DeviceModel.Gen2, DeviceModel.Gen3, DeviceModel.Gen4, DeviceModel.Gen4K,
            DeviceModel.AppleTV4KGen2, DeviceModel.AppleTV4KGen3,
            DeviceModel.AppleTVGen1,
        } or "AppleTV" in str(getattr(conf.device_info, "raw_model", "") or "")
    except Exception:
        name = (conf.name or "").lower()
        return "apple tv" in name or "appletv" in name


def conf_is_homepod(conf) -> bool:
    try:
        raw = str(getattr(conf.device_info, "raw_model", "") or "")
        if "AudioAccessory" in raw:
            return True
        from pyatv.const import DeviceModel
        return conf.device_info.model in {
            DeviceModel.HomePod, DeviceModel.HomePodMini, DeviceModel.HomePodGen2,
        }
    except Exception:
        return "homepod" in (conf.name or "").lower()


def conf_looks_grouped(conf) -> bool:
    props = conf_airplay_properties(conf)
    return bool(
        props.get("gid") or props.get("pgid") or props.get("gpname")
        or props.get("tsid")
    )


def should_use_airplay2(conf, device_id: str, *, binary_available: bool) -> bool:
    """Decide whether this target should stream via cliairplay instead of pyatv."""
    if not binary_available:
        return False
    if get_credentials(device_id):
        return True
    features = conf_features_value(conf)
    if supports_airplay2(features):
        return True
    if conf_looks_grouped(conf):
        return True
    if conf_is_apple_tv(conf):
        return True
    # AP2-only receivers: AirPlay service present, no RAOP.
    try:
        from pyatv.const import Protocol
        if conf.get_service(Protocol.AirPlay) and not conf.get_service(Protocol.RAOP):
            return True
    except Exception:
        pass
    return False


def build_stream_for_conf(
    conf,
    *,
    volume: int,
    binary: str,
    use_ptp_shared: bool = True,
    defer_start: bool = False,
) -> CliAirPlayStream:
    device_id = conf.identifier
    creds = get_credentials(device_id)
    props = conf_airplay_properties(conf)
    txt = serialize_txt(props)
    # Prefer features from AirPlay; if missing, graft RAOP ft into txt.
    if "features=" not in txt and "ft=" not in txt:
        try:
            from pyatv.const import Protocol
            raop = conf.get_service(Protocol.RAOP)
            if raop and raop.properties and raop.properties.get("ft"):
                txt = f"{txt} ft={raop.properties['ft']}".strip()
        except Exception:
            pass
    auth = creds.get("auth") if creds else None
    dacp = (creds.get("dacp_id") if creds else None) or stable_dacp_id(device_id)
    port = conf_airplay_port(conf)
    is_atv = conf_is_apple_tv(conf)
    protocol = "airplay2" if supports_airplay2(conf_features_value(conf)) or is_atv else "auto"
    # Video-class Apple TVs advertise PTP but often never slave to our clock;
    # audio then plays as silence until the session dies. Force NTP for them.
    timing = "ntp" if is_atv else None
    use_shared = use_ptp_shared and timing != "ntp"
    return CliAirPlayStream(
        str(conf.address),
        port=port,
        volume=volume,
        name=conf.name,
        device_id=device_id,
        auth=auth,
        dacp_id=dacp,
        txt=txt,
        protocol=protocol,
        use_ptp_shared=use_shared,
        binary=binary,
        defer_start=defer_start,
        timing=timing,
    )


def start_synced_group(streams: list[CliAirPlayStream], lead_ms: int = 2000) -> None:
    """Connect every stream (defer_start), then fire a shared START anchor."""
    if not streams:
        return
    if len(streams) == 1:
        streams[0].defer_start = False
        streams[0].start()
        return
    for s in streams:
        s.defer_start = True
        s.start()
    anchor = int(time.time() * 1000) + max(500, int(lead_ms))
    for s in streams:
        s.command_start(start_unix_ms=anchor)
    print(f"[airplay2] Synced START at unix_ms={anchor} for {len(streams)} device(s)")


def resolve_airplay_outputs(
    airplay_targets: list[dict],
    found_confs: list,
    *,
    volume: int,
    airplay2_enabled: bool = True,
    binary: str | None = None,
) -> tuple[list, list]:
    """Split scanned devices into (pyatv_confs, CliAirPlayStream list).

    AP2 streams are started (including synced multi-room START). Caller owns
    teardown via ``stream.stop()``. Returns empty AP2 list when binary missing
    or start fails (errors are printed; caller may surface them).
    """
    id_to_conf = {d.identifier: d for d in found_confs}
    if airplay2_enabled:
        binary = binary or find_cliairplay()
    else:
        binary = None

    raop_confs = []
    ap2_confs = []
    for t in airplay_targets:
        conf = id_to_conf.get(t["id"])
        if not conf:
            continue
        if binary and should_use_airplay2(conf, t["id"], binary_available=True):
            ap2_confs.append(conf)
        else:
            raop_confs.append(conf)

    ap2_streams: list[CliAirPlayStream] = []
    if ap2_confs and binary:
        try:
            ap2_streams = [
                build_stream_for_conf(
                    c, volume=volume, binary=binary,
                    use_ptp_shared=True,
                    defer_start=len(ap2_confs) > 1,
                )
                for c in ap2_confs
            ]
            start_synced_group(ap2_streams)
        except Exception as e:
            print(f"[airplay2] Failed to start AP2 streams: {e}")
            for s in ap2_streams:
                with suppress(Exception):
                    s.stop()
            raise
    return raop_confs, ap2_streams


def push_metadata_to_streams(streams, track: dict, artwork: bytes | None = None) -> None:
    """Push now-playing metadata to every CliAirPlayStream sink."""
    if not streams:
        return
    title = track.get("track_title")
    artist = track.get("track_artist") or track.get("album_artist")
    album = track.get("album_title")
    for s in streams:
        with suppress(Exception):
            s.set_metadata(
                title=title, artist=artist, album=album, artwork=artwork,
            )
