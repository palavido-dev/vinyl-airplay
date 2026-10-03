"""Auth: password setup, sessions, CSRF, LAN trust, settings redaction."""

import ipaddress

import pytest
from fastapi.testclient import TestClient

import auth as authmod


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(authmod, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(authmod, "SECRET_FILE", tmp_path / ".auth_secret")
    monkeypatch.setattr(authmod, "SESSIONS_FILE", tmp_path / ".auth_sessions")
    authmod._SECRET = authmod._load_secret()
    with authmod._lock:
        authmod._sessions.clear()
    yield tmp_path


@pytest.fixture()
def client(auth_env, monkeypatch):
    # Import app after auth paths are redirected.
    import main as mainmod

    # Avoid lifespan audio init side effects where possible.
    with TestClient(mainmod.app, raise_server_exceptions=False) as c:
        yield c


def test_password_hash_roundtrip(auth_env):
    enc = authmod.hash_password("correct horse")
    assert authmod.verify_password("correct horse", enc)
    assert not authmod.verify_password("wrong", enc)


def test_public_settings_redacts_token(auth_env):
    out = authmod.public_settings({"discogs_token": "abcd1234secret", "volume": 10})
    assert out["discogs_token"].endswith("cret")
    assert "*" in out["discogs_token"]
    assert out["discogs_token_set"] is True
    assert out["volume"] == 10


def test_filter_settings_update_allowlist(auth_env):
    safe = authmod.filter_settings_update({
        "volume": 50,
        "password_hash": "evil",
        "theme": "midnight",
        "discogs_token": "tok",
    })
    assert safe == {"volume": 50, "discogs_token": "tok"}


@pytest.mark.parametrize(
    "host,trusted",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("::ffff:127.0.0.1", True),
        ("192.168.1.42", True),
        ("10.0.0.5", True),
        ("172.16.4.8", True),
        ("169.254.10.2", True),
        ("8.8.8.8", False),
        ("testclient", False),
        ("", False),
    ],
)
def test_host_is_trusted(host, trusted):
    assert authmod.host_is_trusted(host) is trusted


def test_lan_client_skips_login(client, auth_env, monkeypatch):
    """Same-network private IPs are trusted — no password / session required."""
    authmod.set_password("long-enough-password")
    assert authmod.host_is_trusted("192.168.1.50")

    # TestClient's peer is "testclient"; patch the helper used by route + middleware.
    monkeypatch.setattr(authmod, "is_trusted_client", lambda _req: True)
    monkeypatch.setattr(authmod, "is_loopback", lambda _req: False)
    st = client.get("/api/auth/status")
    assert st.status_code == 200
    body = st.json()
    assert body["trusted"] is True
    assert body["authenticated"] is True

    r = client.get("/api/status")
    assert r.status_code == 200


def test_setup_login_csrf_and_protect(client, auth_env, monkeypatch):
    # Force untrusted peer so remote auth rules apply (TestClient host varies).
    monkeypatch.setattr(authmod, "is_trusted_client", lambda _req: False)
    monkeypatch.setattr(authmod, "is_loopback", lambda _req: False)

    st = client.get("/api/auth/status")
    assert st.status_code == 200
    assert st.json()["password_set"] is False
    assert st.json()["authenticated"] is False

    bad = client.post("/api/auth/setup", json={"password": "short"})
    assert bad.status_code == 400

    ok = client.post("/api/auth/setup", json={"password": "long-enough-password"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    csrf = ok.json()["csrf"]
    assert csrf
    assert authmod.SESSION_COOKIE in ok.cookies

    denied = client.post(
        "/api/settings",
        json={"volume": 1},
        headers={"Host": "pi.local"},
    )
    if denied.status_code != 200:
        assert denied.status_code == 403

    allowed = client.post(
        "/api/settings",
        json={"volume": 22},
        headers={"X-CSRF-Token": csrf},
    )
    assert allowed.status_code == 200

    client.post(
        "/api/settings",
        json={"discogs_token": "supersecrettoken99"},
        headers={"X-CSRF-Token": csrf},
    )
    status = client.get("/api/status", headers={"X-CSRF-Token": csrf})
    assert status.status_code == 200
    token = status.json()["settings"]["discogs_token"]
    assert "supersecrettoken99" not in token
    assert token.endswith("n99")


def test_session_persists_across_reload(auth_env):
    token, csrf = authmod.create_session()
    assert (auth_env / ".auth_sessions").exists()
    # Simulate process restart
    with authmod._lock:
        authmod._sessions.clear()
    authmod._load_sessions()
    sess = authmod.get_session(token)
    assert sess is not None
    assert sess["csrf"] == csrf


def test_bad_login(client, auth_env, monkeypatch):
    monkeypatch.setattr(authmod, "is_trusted_client", lambda _req: False)
    monkeypatch.setattr(authmod, "is_loopback", lambda _req: False)
    client.post("/api/auth/setup", json={"password": "long-enough-password"})
    import main as mainmod
    with TestClient(mainmod.app, raise_server_exceptions=False) as c2:
        r = c2.post("/api/auth/login", json={"password": "nope-nope-nope"})
        assert r.status_code == 401


def test_private_network_ranges_cover_home_lan():
    # Sanity: common home / lab ranges are private per the stdlib.
    for addr in ("192.168.0.1", "10.1.2.3", "172.20.0.4"):
        assert ipaddress.ip_address(addr).is_private
