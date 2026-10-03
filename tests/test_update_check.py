"""Update check must fetch before comparing to origin/main."""

from types import SimpleNamespace

import routes_system as sysroutes


def test_check_update_fetches_before_comparing(monkeypatch):
    calls = []

    def fake_fetch():
        calls.append("fetch")
        return True, ""

    def fake_commit():
        calls.append("head")
        return "aaa111"

    def fake_git(*args, timeout=15):
        calls.append(("git", *args))
        if args[:2] == ("rev-parse", "origin/main"):
            return SimpleNamespace(returncode=0, stdout="bbb222\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="0\n", stderr="")

    def fake_behind():
        calls.append("behind")
        return 3

    monkeypatch.setattr(sysroutes, "_fetch_origin", fake_fetch)
    monkeypatch.setattr(sysroutes, "_get_git_commit", fake_commit)
    monkeypatch.setattr(sysroutes, "_git", fake_git)
    monkeypatch.setattr(sysroutes, "_count_commits_behind", fake_behind)

    result = sysroutes._check_update_sync()
    assert calls[0] == "head"
    assert calls[1] == "fetch"
    assert result["available"] is True
    assert result["ok"] is True
    assert result["current_commit"] == "aaa111"
    assert result["latest_commit"] == "bbb222"
    assert result["commits_behind"] == 3


def test_check_update_reports_fetch_failure(monkeypatch):
    monkeypatch.setattr(sysroutes, "_fetch_origin", lambda: (False, "Could not resolve host"))
    monkeypatch.setattr(sysroutes, "_get_git_commit", lambda: "aaa111")
    monkeypatch.setattr(
        sysroutes,
        "_git",
        lambda *a, timeout=15: SimpleNamespace(returncode=0, stdout="aaa111\n", stderr=""),
    )
    monkeypatch.setattr(sysroutes, "_count_commits_behind", lambda: 0)

    result = sysroutes._check_update_sync()
    assert result["ok"] is False
    assert result["available"] is False
    assert "Could not resolve host" in result["fetch_error"]
