"""A disposable panel + node agent, for the live scripts.

    async with stack(tmp) as s:      # s.client is signed in as admin
        ...

The panel runs on the agent engine with a SQLite database under `tmp`; one
node is registered through the admin API, and its agent is started from the
config the panel handed back, on the PROCESS runtime (no Docker here), with
one local JRE per Java major standing in for the runtime images. Both are
real processes on spare ports, torn down on the way out.
"""
from __future__ import annotations

import asyncio
import glob
import os
import pathlib
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_java(major: int) -> str | None:
    """BF_JAVA_<major>, else a Temurin JRE where the installers put them."""
    env = os.environ.get(f"BF_JAVA_{major}")
    if env:
        return env
    for base in ("C:/Program Files/Eclipse Adoptium", "/usr/lib/jvm"):
        for hit in sorted(glob.glob(f"{base}/*-{major}*")) + sorted(
                glob.glob(f"{base}/jre-{major}.*")):
            exe = pathlib.Path(hit) / "bin" / ("java.exe" if os.name == "nt" else "java")
            if exe.exists():
                return str(exe)
    return None


def secrets_from_dotenv(names: tuple[str, ...]) -> dict:
    """Values from the gitignored .env, for the panel's environment only."""
    out = {}
    path = ROOT / ".env"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in names and value.strip():
            out[key.strip()] = value.strip()
    return out


async def wait_http(url: str, seconds: float = 60) -> bool:
    deadline = time.time() + seconds
    async with httpx.AsyncClient() as c:
        while time.time() < deadline:
            try:
                if (await c.get(url, timeout=2)).status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    return False


async def follow(c: httpx.AsyncClient, job_id: str, seconds: int) -> dict:
    """Poll a job to its end; status "timeout" if it does not get there."""
    deadline = time.time() + seconds
    snap: dict = {}
    while time.time() < deadline:
        snap = (await c.get(f"/api/jobs/{job_id}")).json()
        if snap.get("status") in ("done", "error", "cancelled"):
            return snap
        await asyncio.sleep(2)
    return {**snap, "status": "timeout"}


@dataclass
class Stack:
    tmp: pathlib.Path
    api: str
    agent: str
    client: httpx.AsyncClient
    javas: dict
    procs: list = field(default_factory=list)


@asynccontextmanager
async def stack(tmp: pathlib.Path, extra_env: dict | None = None):
    javas = {m: find_java(m) for m in (8, 17, 21, 25)}
    api_port, agent_port = free_port(), free_port()
    api = f"http://127.0.0.1:{api_port}"
    env = {**os.environ, "ENGINE": "agent",
           "DATA_DIR": str(tmp / "panel"), "STATE_DIR": str(tmp / "panel/state"),
           "CACHE_DIR": str(tmp / "panel/cache"),
           "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp / 'panel.db').as_posix()}",
           "BLESSFORGE_SECRET": "live-scripts-only", "PUBLIC_API_URL": api,
           "REDIS_URL": "", "OLLAMA_URL": "", "UPDATE_CHECK_HOURS": "0",
           **(extra_env or {})}
    log = open(tmp / "procs.log", "ab")
    procs = [subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(api_port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)]
    client = httpx.AsyncClient(base_url=api, timeout=180)
    try:
        if not await wait_http(api + "/api/healthz"):
            raise RuntimeError(f"the panel did not come up; see {tmp / 'procs.log'}")
        await client.post("/api/auth/login", json={"username": "admin",
                                                   "password": "password"})
        await client.post("/api/auth/password", json={"current": "password",
                                                      "password": "live scripts passphrase"})
        await client.post("/api/auth/login", json={"username": "admin",
                                                   "password": "live scripts passphrase"})
        made = (await client.post("/api/admin/nodes", json={
            "name": "Live node", "fqdn": "127.0.0.1", "agent_port": agent_port,
            "scheme": "http", "runtime": "process",
            "agent": {"allow_unsafe_process_runtime": True, "listen_port": agent_port,
                      "servers_root": str(tmp / "node/servers"),
                      "state_dir": str(tmp / "node/state"), "stop_timeout": 60,
                      "java_paths": {str(m): p for m, p in javas.items() if p}},
        })).json()
        cfg = tmp / "agent.toml"
        cfg.write_text(made["config"], encoding="utf-8")
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1",
             "--port", str(agent_port), "--log-level", "warning"],
            cwd=ROOT, env={**env, "BLESSFORGE_AGENT_CONFIG": str(cfg)},
            stdout=log, stderr=subprocess.STDOUT))
        agent = f"http://127.0.0.1:{agent_port}"
        if not await wait_http(agent + "/health"):
            raise RuntimeError(f"the agent did not come up; see {tmp / 'procs.log'}")
        yield Stack(tmp, api, agent, client, javas, procs)
    finally:
        await client.aclose()
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(30)
                except subprocess.TimeoutExpired:
                    p.kill()
        log.close()
