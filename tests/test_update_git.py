"""Update git calls must not wait on a password prompt, and the service worker
must not re-fetch /api (that turned a slow check into "Could not reach update server").
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import routes_system as sysroutes


def test_git_does_not_prompt_and_allows_safe_directory():
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    with patch.object(sysroutes.subprocess, "run", fake_run):
        sysroutes._git("fetch", "--prune", "origin", timeout=5)

    assert seen["argv"][:3] == ["git", "-c", "safe.directory=*"]
    assert seen["argv"][3:] == ["fetch", "--prune", "origin"]
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_pull_names_a_merge_strategy():
    calls = []

    def fake_git(*args, timeout=15, config=()):
        calls.append((args, config))
        if args[:1] == ("pull",):
            return subprocess.CompletedProcess(args, 0, stdout="merged\n", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="no merge")

    with patch.object(sysroutes, "_git", fake_git):
        result = sysroutes._integrate_origin_main()

    assert result.returncode == 0
    assert calls[0][0][:4] == ("pull", "--no-rebase", "--no-edit", "origin")
    assert calls[0][0][4] == "main"
    assert "user.email=listen@localhost" in calls[0][1]
    assert len(calls) == 1


def test_failed_pull_aborts_a_merge():
    calls = []

    def fake_git(*args, timeout=15, config=()):
        calls.append(args)
        if args[:1] == ("pull",):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="CONFLICT")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch.object(sysroutes, "_git", fake_git):
        result = sysroutes._integrate_origin_main()

    assert result.returncode == 1
    assert calls[1] == ("merge", "--abort")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_divergent_histories_merge_cleanly():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        origin = root / "origin.git"
        work = root / "work"
        other = root / "other"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "Tester")
        (work / "base.txt").write_text("base\n")
        _git(work, "add", "base.txt")
        _git(work, "commit", "-m", "base")
        _git(work, "push", "origin", "HEAD:main")
        (work / "local.txt").write_text("from the pi\n")
        _git(work, "add", "local.txt")
        _git(work, "commit", "-m", "local only")

        subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
        _git(other, "config", "user.email", "t@example.com")
        _git(other, "config", "user.name", "Tester")
        (other / "remote.txt").write_text("from github\n")
        _git(other, "add", "remote.txt")
        _git(other, "commit", "-m", "remote only")
        _git(other, "push", "origin", "HEAD:main")

        # Reproduce a Pi that never set pull.rebase.
        subprocess.run(["git", "config", "--unset", "pull.rebase"], cwd=work, capture_output=True)
        subprocess.run(["git", "config", "--unset", "pull.ff"], cwd=work, capture_output=True)

        plain = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=work,
            capture_output=True,
            text=True,
        )
        assert plain.returncode != 0
        assert "divergent branches" in (plain.stderr + plain.stdout)

        with patch.object(sysroutes, "_repo_dir", lambda: str(work)):
            merged = sysroutes._integrate_origin_main()
        assert merged.returncode == 0, merged.stderr
        assert (work / "local.txt").read_text() == "from the pi\n"
        assert (work / "remote.txt").read_text() == "from github\n"
        # The refusal path must not leave a merge in progress.
        pending = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=work,
            capture_output=True,
        )
        assert pending.returncode != 0


def test_service_worker_lets_the_browser_call_the_api():
    text = (Path(__file__).resolve().parent.parent / "templates" / "service-worker.js").read_text()
    start = text.index("url.pathname.startsWith('/api/')")
    end = text.index("// Network-first", start)
    block = text[start:end]
    assert "respondWith" not in block
    assert "return;" in block


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok", test.__name__)
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
