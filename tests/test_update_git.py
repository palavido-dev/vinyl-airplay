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
