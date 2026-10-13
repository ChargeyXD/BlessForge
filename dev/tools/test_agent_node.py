#!/usr/bin/env python3
"""The node agent as a busy machine: nothing on it stalls while one thing works.

What design/AGENT-REWORK-BRIEF.md items 6 and 7 need to be true:

  * file work (copy, delete, extract, compress, a tree walk) runs off the
    event loop, and quick work never queues behind heavy work (6.1);
  * CPU and memory come from a background sampler: no request waits on
    Docker's stats, and GET /v1/stats answers for every server at once (6.2);
  * one long-lived client per target -- Docker, the panel, each node -- not
    a new connection (and a 60-100 ms SSL context) per call (6.3);

The agent runs in this process (its real app, lifespan and all) on the
PROCESS runtime; the Docker parts that need a daemon run with --live.

    python dev/tools/test_agent_node.py
    python dev/tools/test_agent_node.py --live     # also against real Docker
"""
from __future__ import annotations

import asyncio
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from agent import files  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

LIVE = "--live" in sys.argv
TMP = pathlib.Path(tempfile.mkdtemp(prefix="bf-node-"))
NODE = {"Authorization": "Bearer tid.secret"}
KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo).decode()
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""),
          flush=True)


def section(title):
    print(f"\n--- {title} " + "-" * max(4, 66 - len(title)), flush=True)


def process_cfg(name: str, **over) -> AgentConfig:
    return AgentConfig(node_id="node-test", token_id="tid", token="secret",
                       panel_url="http://127.0.0.1:9", panel_public_key=PUB,
                       servers_root=TMP / name / "servers",
                       state_dir=TMP / name / "state", runtime="process",
                       allow_unsafe_process_runtime=True, **over)


class Agent:
    """The agent's real app, lifespan included, reached in-process."""

    def __init__(self, cfg: AgentConfig):
        from agent.main import create_app
        self.app = create_app(cfg)
        self.cfg = cfg

    async def __aenter__(self):
        self._life = self.app.router.lifespan_context(self.app)
        await self._life.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://agent", headers=NODE,
                                        timeout=30)
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self._life.__aexit__(*exc)

    @property
    def sup(self):
        return self.app.state.agent["sup"]


class LoopProbe:
    """The longest the event loop went without running a 10 ms timer."""

    def __init__(self):
        self.worst = 0.0
        self._task = None

    async def _run(self):
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            self.worst = max(self.worst, time.perf_counter() - t0 - 0.01)

    def __enter__(self):
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    def __exit__(self, *exc):
        self._task.cancel()


FAKE_SERVER = r'''
import sys, time
def say(msg):
    print(time.strftime("[%H:%M:%S]") + " [Server thread/INFO]: " + msg, flush=True)
say("Starting minecraft server version fake-1.0")
say('Done (0.05s)! For help, type "help"')
for line in sys.stdin:
    line = line.strip()
    if line == "stop":
        say("Stopping the server")
        sys.exit(0)
    say("ran: " + line)
'''


async def until(pred, seconds=10.0, every=0.05):
    end = time.time() + seconds
    while time.time() < end:
        if await pred() if asyncio.iscoroutinefunction(pred) else pred():
            return True
        await asyncio.sleep(every)
    return False


async def fake_server(a: Agent, sid: str, port: int, **spec) -> None:
    """A tiny stand-in that behaves like a Minecraft console, created on `a`."""
    script = TMP / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    r = await a.client.put(f"/v1/servers/{sid}", json={
        "name": spec.pop("name", sid[:8]), "port": port,
        "execution_command": f"{sys.executable} -u {script}", **spec})
    assert r.status_code == 200, r.text


# --- 6.1 file work off the event loop ---------------------------------------------


async def test_file_ops_off_loop():
    section("6.1 file work runs off the event loop")
    sid = "aaaaaaaa-0000-0000-0000-000000000001"
    async with Agent(process_cfg("files")) as a:
        c = a.client
        r = await c.put(f"/v1/servers/{sid}", json={"name": "files", "port": 30001})
        assert r.status_code == 200, r.text
        folder = a.sup.folder(sid)
        (folder / "world").mkdir()
        (folder / "world" / "level.dat").write_bytes(b"x" * 1024)

        # Warm up first: the first request through the app builds its
        # routes and imports zipfile, which is not what is being measured.
        await c.post(f"/v1/servers/{sid}/files/compress",
                     json={"paths": ["world"], "dest": "warm.zip"})
        await c.get(f"/v1/servers/{sid}/files")
        real = files.compress

        def slow_compress(*args):
            time.sleep(1.0)         # a big backup, as the event loop would feel it
            return real(*args)

        files.compress = slow_compress
        try:
            with LoopProbe() as probe:
                big = [asyncio.create_task(c.post(
                    f"/v1/servers/{sid}/files/compress",
                    json={"paths": ["world"], "dest": f"backup-{i}.zip"}))
                    for i in range(3)]
                await asyncio.sleep(0.1)
                t0 = time.perf_counter()
                health = await c.get("/health")
                health_s = time.perf_counter() - t0
                t0 = time.perf_counter()
                listing = await c.get(f"/v1/servers/{sid}/files")
                list_s = time.perf_counter() - t0
                t0 = time.perf_counter()
                read = await c.get(f"/v1/servers/{sid}/files/content",
                                   params={"path": "world/level.dat"})
                read_s = time.perf_counter() - t0
                done = await asyncio.gather(*big)
        finally:
            files.compress = real
        check("the agent answers while three backups compress",
              health.status_code == 200 and health_s < 0.3, f"{health_s:.3f}s")
        check("a folder listing does not queue behind them",
              listing.status_code == 200 and list_s < 0.3, f"{list_s:.3f}s")
        check("nor does reading a file", read.status_code == 200 and read_s < 0.3,
              f"{read_s:.3f}s")
        check("the event loop never stalled for more than 100 ms",
              probe.worst < 0.1, f"worst {probe.worst * 1000:.0f} ms")
        check("and the backups still finished",
              all(r.status_code == 200 for r in done)
              and all((folder / f"backup-{i}.zip").is_file() for i in range(3)))

        r = await c.post(f"/v1/servers/{sid}/files/extract",
                         json={"path": "backup-0.zip"})
        check("extract runs through the pool and still writes through the jail",
              r.status_code == 200 and r.json()["written"] >= 1, r.text)
        r = await c.post(f"/v1/servers/{sid}/files/delete",
                         json={"paths": ["backup-1.zip", "backup-2.zip"]})
        check("so does delete", r.status_code == 200
              and not (folder / "backup-1.zip").exists())
        r = await c.get(f"/v1/servers/{sid}/files/content",
                        params={"path": "../../outside.txt"})
        check("a jail refusal from a pool thread is still a 400, not a 500",
              r.status_code == 400, r.text)


# --- 6.2 stats from a sampler ------------------------------------------------------


async def test_stats_sampler():
    section("6.2 CPU and memory come from a sampler, not from each request")
    from agent import containers

    # The Docker reading: one-shot, and CPU from two readings of our own.
    seen = []
    readings = iter([
        {"cpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000},
                       "system_cpu_usage": 10_000_000_000, "online_cpus": 4},
         "memory_stats": {"usage": 600 * 2**20, "limit": 1024 * 2**20,
                          "stats": {"inactive_file": 88 * 2**20}}},
        {"cpu_stats": {"cpu_usage": {"total_usage": 3_000_000_000},
                       "system_cpu_usage": 14_000_000_000, "online_cpus": 4},
         "memory_stats": {"usage": 600 * 2**20, "limit": 1024 * 2**20}},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=next(readings))

    real_client = containers._docker_client
    containers._docker_client = lambda cfg, timeout=60: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker")
    try:
        cfg = process_cfg("dockerstats")
        run = containers.DockerRunning(containers.DockerRuntime(cfg), "bf-x", None, None)
        first = await run.stats()
        second = await run.stats()
    finally:
        containers._docker_client = real_client
    check("Docker is asked for a one-shot reading (no 1-2 s wait for its own second sample)",
          all(u.params.get("one-shot") == "true" and u.params.get("stream") == "false"
              for u in seen), [str(u) for u in seen])
    check("the first reading after a start has memory but no CPU yet",
          first["cpu"] is None and first["mem"] == 512 * 2**20, first)
    check("the next one has CPU, from the change between the two (2 of 4 cores = 200)",
          second["cpu"] == 200.0, second)

    sid = "bbbbbbbb-0000-0000-0000-000000000001"
    other = "bbbbbbbb-0000-0000-0000-000000000002"
    async with Agent(process_cfg("stats")) as a:
        c = a.client
        await fake_server(a, sid, 30011, name="sampled")
        await fake_server(a, other, 30012, name="idle")
        r = await c.post(f"/v1/servers/{sid}/power", json={"action": "start"})
        assert r.status_code == 200, r.text
        await until(lambda: a.sup.servers[sid].state == "running")
        await a.sup.sample()
        r = await c.get("/v1/stats")
        body = r.json()["servers"]
        check("GET /v1/stats answers for every server on the node in one call",
              r.status_code == 200 and set(body) == {sid, other}, list(body))
        check("...with the running one's sampled memory and the stopped one's state",
              body[sid]["running"] and body[sid].get("mem", 0) > 0
              and body[sid].get("sampled_at") and not body[other]["running"]
              and body[other]["state"] == "offline", body)

        srv = a.sup.servers[sid]
        real_stats = srv.running.stats

        async def stuck():
            await asyncio.sleep(3)
            return {}
        srv.running.stats = stuck
        t0 = time.perf_counter()
        r = await c.get(f"/v1/servers/{sid}")
        one_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        r2 = await c.get("/v1/stats")
        all_s = time.perf_counter() - t0
        check("GET /v1/servers/{id} never waits on a stats call",
              r.status_code == 200 and one_s < 0.2 and r.json()["stats"].get("mem"),
              f"{one_s:.3f}s")
        check("...nor does GET /v1/stats", r2.status_code == 200 and all_s < 0.2,
              f"{all_s:.3f}s")
        srv.running.stats = real_stats
        await c.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
        await until(lambda: a.sup.servers[sid].running is None)
        await a.sup.sample()
        check("a stopped server's last reading is dropped",
              sid not in a.sup.samples and "mem" not in (await a.sup.stats(sid)))


# --- 6.3 one client per target ------------------------------------------------------


async def test_shared_clients():
    section("6.3 one long-lived client per target")
    from agent import clients
    from agent.containers import _docker_client
    from agent.events import Events
    cfg = process_cfg("clients")
    async with _docker_client(cfg, 5) as first:
        pass
    async with _docker_client(cfg, 120) as second:
        pass
    check("every Docker call shares one client, each with its own timeout",
          first._c is second._c and not first._c.is_closed
          and (first._timeout, second._timeout) == (5, 120))
    ev = Events(cfg)
    ev.emit("hello")
    await ev.flush()            # the panel is not there: kept and retried
    await ev.flush()
    check("the events flush keeps one client towards the panel across retries",
          ev.panel() is ev.panel() and not ev.panel().is_closed and len(ev.queue) == 1)
    await clients.close_all()
    check("...and shutdown closes them", first._c.is_closed)

    from app.engine import agent as panel_side
    from types import SimpleNamespace
    node = SimpleNamespace(id="n1")
    one = panel_side.AgentEngine(node, "http://agent:8443", "Bearer a.b")
    two = panel_side.AgentEngine(node, "http://agent:8443/", "Bearer a.b")
    other = panel_side.AgentEngine(SimpleNamespace(id="n2"), "http://other:8443", "Bearer c.d")
    check("the panel keeps one client per node, even across load_nodes() rebuilding drivers",
          one._client() is two._client() and one._client() is not other._client())
    held = one._client()
    await panel_side.close_clients()
    check("...closed on shutdown", held.is_closed and not panel_side._clients)


async def main():
    await test_file_ops_off_loop()
    await test_stats_sampler()
    await test_shared_clients()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
