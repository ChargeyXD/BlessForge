#!/usr/bin/env python3
"""Boot a disposable BlessForge and run the over-HTTP suites against it.

`test_auth_http.py` walks the FIRST-RUN path, so it needs a server whose
state is empty -- and pointing it at a real instance would create and delete
accounts there. This starts one on a spare port with a fresh temporary
DATA_DIR / STATE_DIR (and, when DATABASE_URL is set, that database), runs
each suite, and tears the server down whatever happens.

    python dev/tools/run_http_tests.py              # every HTTP suite
    python dev/tools/run_http_tests.py test_auth_http

Exit status is non-zero if any suite failed or the server never came up.
"""
from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
SUITES = ["test_auth_http"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(url: str, seconds: float = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:      # noqa: BLE001 -- still starting
            time.sleep(0.4)
    return False


def main(argv: list[str]) -> int:
    suites = argv or SUITES
    data = pathlib.Path(tempfile.mkdtemp(prefix="bf-http-"))
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATA_DIR": str(data),
        "STATE_DIR": str(data / "state"),
        "CACHE_DIR": str(data / "cache"),
        "PORT": str(port),
        # Nothing reachable: the suites test the gate, not the integrations,
        # and a key or endpoint inherited from the shell would only slow
        # the health check down.
        "CURSEFORGE_API_KEY": "", "CURSEFORGE_API_KEY_B64": "",
        "OLLAMA_URL": "", "MODRINTH_ENABLED": "false",
    }
    log = open(data / "server.log", "wb")
    # The fleet routes need something to list. dev/mock/crafty.py is the
    # stand-in for Crafty, with two servers in it.
    mock_port = free_port()
    mock = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dev.mock.crafty:app",
         "--host", "127.0.0.1", "--port", str(mock_port), "--log-level", "warning"],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    env.update(CRAFTY_URL=f"http://127.0.0.1:{mock_port}", CRAFTY_TOKEN="mock")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    failed = 0
    try:
        if not wait_up(url):
            print(f"FAIL  the server never answered on {url}")
            print((data / "server.log").read_text(errors="replace")[-2000:])
            return 1
        for name in suites:
            print(f"== {name}")
            r = subprocess.run(
                [sys.executable, str(ROOT / "dev" / "tools" / f"{name}.py")],
                cwd=ROOT, env={**env, "BF_URL": url})
            failed += r.returncode != 0
    finally:
        for proc in (server, mock):
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
