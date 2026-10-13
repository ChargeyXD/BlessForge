#!/usr/bin/env python3
"""The node agent as a busy machine: nothing on it stalls while one thing works.

What design/AGENT-REWORK-BRIEF.md items 6 and 7 need to be true:

  * file work (copy, delete, extract, compress, a tree walk) runs off the
    event loop, and quick work never queues behind heavy work (6.1);
  * CPU and memory come from a background sampler: no request waits on
    Docker's stats, and GET /v1/stats answers for every server at once (6.2);
  * one long-lived client per target -- Docker, the panel, each node -- not
    a new connection (and a 60-100 ms SSL context) per call (6.3);
  * an agent restart takes back the servers that kept running, and never
    force-deletes a running container: a start refuses instead (7.1);
  * the console is numbered, so a follower asks only for what is new even
    once the ring is full, and the panel's relay follows each server once
    for every viewer (2; the end-to-end relay is in test_agent_api.py);

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


# --- 7.1 an agent restart re-adopts running servers ------------------------------------


class FakeRunning:
    def __init__(self, detachable=True):
        self.calls = []
        if not detachable:
            self.detach = None

    async def send(self, line):
        self.calls.append(("send", line))

    async def stop(self, timeout):
        self.calls.append(("stop", timeout))

    async def kill(self):
        self.calls.append(("kill",))

    async def stats(self):
        return {}

    async def detach(self):
        self.calls.append(("detach",))


class FakeDocker:
    """The runtime interface, with servers that kept running while the agent
    was away."""
    name = "docker"

    def __init__(self, running: dict[str, list[str]]):
        self.running = running
        self.started = []
        self.handles = {}

    async def running_servers(self):
        return {u: {} for u in self.running}

    async def adopt(self, server, on_output, on_exit, tail=2000):
        for ln in self.running[server["uuid"]]:
            on_output(ln)
        self.handles[server["uuid"]] = h = FakeRunning()
        return h, 1700000000.0

    async def start(self, *a, **kw):
        self.started.append(a)
        return FakeRunning()

    async def remove(self, uuid):
        pass


class FakeEvents:
    def __init__(self):
        self.sent = []

    def emit(self, kind, server=None, **data):
        self.sent.append((kind, server, data))


async def test_readopt():
    section("7.1 an agent restart takes back the servers that kept running")
    from agent.servers import Supervisor
    cfg = process_cfg("adopt")
    a_id, b_id, ghost = ("cccccccc-0000-0000-0000-00000000000a",
                         "cccccccc-0000-0000-0000-00000000000b",
                         "cccccccc-0000-0000-0000-0000000000ff")
    first = Supervisor(cfg, FakeDocker({}), FakeEvents())
    first.put(a_id, {"name": "Survival", "port": 30021, "auto_start": True,
                     "execution_command": "java -jar server.jar"})
    first.put(b_id, {"name": "Creative", "port": 30022,
                     "execution_command": "java -jar server.jar"})

    runtime = FakeDocker({
        a_id: ["[12:00:00] [Server thread/INFO]: Starting minecraft server",
               '[12:00:09] [Server thread/INFO]: Done (9.1s)! For help, type "help"',
               "[12:01:00] [Server thread/INFO]: Sakura joined the game"],
        b_id: ["[12:00:00] [Server thread/INFO]: Loading 412 mods"],
        ghost: ["not ours"]})
    events = FakeEvents()
    sup = Supervisor(cfg, runtime, events)
    sup.load()
    adopted = await sup.adopt_running()
    a, b = sup.servers[a_id], sup.servers[b_id]
    check("both servers that kept running are taken back",
          sorted(adopted) == sorted([a_id, b_id])
          and a.running is runtime.handles[a_id] and b.running is runtime.handles[b_id])
    check("...the console seeded from what they printed while the agent was away",
          any("Done (9.1s)" in ln for ln in a.console.lines)
          and any("Loading 412 mods" in ln for ln in b.console.lines))
    check("...one that had finished booting is running, one still booting is starting",
          a.state == "running" and b.state == "starting", (a.state, b.state))
    check("...with when it really started, and who joined",
          a.started_at == 1700000000.0 and a.players == {"Sakura"})
    check("a container for a server this node has no spec for is reported, not touched",
          any(k == "drift" and srv == ghost for k, srv, _ in events.sent)
          and ghost not in sup.servers)
    await sup.power(a_id, "start")
    check("auto-start after the restart does not start it again (its container is kept)",
          runtime.started == [] and a.running is runtime.handles[a_id])

    await sup.shutdown()
    check("an agent shutting down lets its Docker servers run on (detach, no stop)",
          runtime.handles[a_id].calls == [("detach",)]
          and runtime.handles[b_id].calls == [("detach",)])
    proc = Supervisor(cfg, FakeDocker({}), FakeEvents())
    proc.load()
    child = FakeRunning(detachable=False)
    proc.servers[a_id].running = child
    await proc.shutdown()
    check("...but a process runtime's children, which die with it, are stopped first",
          child.calls and child.calls[0][0] == "stop")

    # The Docker runtime itself: a start never force-deletes a running container.
    from agent import containers
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.startswith("/images/"):
            return httpx.Response(200, json={})
        if request.method == "GET" and request.url.path.endswith("/json"):
            return httpx.Response(200, json={"State": {"Running": True}})
        return httpx.Response(204)

    real_client = containers._docker_client
    containers._docker_client = lambda cfg, timeout=60: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker")
    try:
        dcfg = AgentConfig(node_id="node-test", token_id="t", token="t",
                           panel_url="http://x", panel_public_key=PUB,
                           servers_root=TMP / "docker" / "servers",
                           state_dir=TMP / "docker" / "state", game_bind_ip="127.0.0.1")
        folder = dcfg.server_dir(a_id)
        folder.mkdir(parents=True)
        rt = containers.DockerRuntime(dcfg)
        try:
            await rt.start({"uuid": a_id, "port": 30021,
                            "execution_command": "java -jar server.jar"},
                           folder, lambda ln: None, None)
            status = None
        except containers.RuntimeError_ as e:
            status = (e.status, str(e))
    finally:
        containers._docker_client = real_client
    check("a start whose container is still running is refused with 409 'already running'",
          status and status[0] == 409 and "already running" in status[1], status)
    check("...and the running container is never deleted",
          not any(m == "DELETE" for m, _ in seen), seen)


async def test_readopt_live():
    section("7.1 LIVE: a real container survives an agent restart, console and all")
    from agent.containers import _docker_client
    image = "python:3.12-slim"
    sid = "dddddddd-0000-0000-0000-000000000001"
    cfg = AgentConfig(node_id="node-live-adopt", token_id="tid", token="secret",
                      panel_url="http://127.0.0.1:9", panel_public_key=PUB,
                      servers_root=TMP / "live" / "servers",
                      state_dir=TMP / "live" / "state", game_bind_ip="127.0.0.1")
    name = "bf-" + sid
    loop_py = ("import sys,time,threading\n"
               "def say(m): print('[Server thread/INFO]: '+m, flush=True)\n"
               "say('Starting'); say('Done (0.1s)! For help, type \"help\"')\n"
               "def tick():\n"
               "    n=0\n"
               "    while True:\n"
               "        n+=1; say('tick %d' % n); time.sleep(0.2)\n"
               "threading.Thread(target=tick, daemon=True).start()\n"
               "for line in sys.stdin:\n"
               "    line=line.strip()\n"
               "    if line=='stop': say('Stopping the server'); sys.exit(0)\n"
               "    say('ran: '+line)\n")
    async with _docker_client(cfg, 60) as c:
        await c.delete(f"/containers/{name}", params={"force": "true"})
        r = await c.post("/containers/create", params={"name": name}, json={
            "Image": image, "Cmd": ["python", "-u", "-c", loop_py],
            "OpenStdin": True, "StdinOnce": False, "AttachStdin": True,
            "AttachStdout": True, "AttachStderr": True, "Tty": False,
            "Labels": {"io.blessforge.server": sid, "io.blessforge.node": cfg.node_id}})
        assert r.status_code < 400, r.text
        await c.post(f"/containers/{name}/start")
        cid = (await c.get(f"/containers/{name}/json")).json()["Id"]
    try:
        async with Agent(cfg) as a:
            await a.client.put(f"/v1/servers/{sid}", json={
                "name": "kept", "port": 30031, "auto_start": True,
                "execution_command": "java -jar server.jar"})
        await asyncio.sleep(1.0)
        async with Agent(cfg) as a:
            srv = a.sup.servers[sid]
            check("LIVE the restarted agent re-attaches the running container",
                  srv.running is not None and srv.state == "running", srv.state)
            check("LIVE ...its console seeded from docker logs",
                  any("tick" in ln for ln in srv.console.lines))
            before = len([ln for ln in srv.console.lines if "tick" in ln])
            await asyncio.sleep(0.8)
            after = len([ln for ln in srv.console.lines if "tick" in ln])
            check("LIVE ...and following it live", after > before, (before, after))
            r = await a.client.post(f"/v1/servers/{sid}/command",
                                    json={"command": "say hello"})
            ok = await until(lambda: any("ran: say hello" in ln for ln in srv.console.lines), 5)
            check("LIVE ...and a command reaches it again", r.status_code == 200 and ok)
            r = await a.client.post(f"/v1/servers/{sid}/power", json={"action": "start"})
            async with _docker_client(cfg, 10) as c:
                same = (await c.get(f"/containers/{name}/json")).json()["Id"] == cid
            check("LIVE start on it is a no-op; the container is the same one",
                  r.status_code == 200 and same)
        async with _docker_client(cfg, 10) as c:
            alive = ((await c.get(f"/containers/{name}/json")).json()
                     .get("State") or {}).get("Running")
        check("LIVE the agent stopping leaves the server running", alive)
        async with Agent(cfg) as a:
            srv = a.sup.servers[sid]
            check("LIVE a second restart takes it back again", srv.running is not None)
            r = await a.client.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
            ok = await until(lambda: srv.running is None, 15)
            check("LIVE stop goes through its own stop and it exits cleanly",
                  r.status_code == 200 and ok and (srv.last_exit or {}).get("code") == 0,
                  srv.last_exit)
    finally:
        async with _docker_client(cfg, 30) as c:
            await c.delete(f"/containers/{name}", params={"force": "true"})
        from agent import clients
        await clients.close_all()


# --- 2. the console: numbered lines, and the panel's relay -------------------------------


async def test_console():
    section("2. the console: numbered, and relayed once per server")
    from agent.servers import CONSOLE_LINES, Console
    con = Console()
    for i in range(CONSOLE_LINES + 500):
        con.feed(f"line {i + 1}")
    fresh, reset = con.since(con.seq - 3)
    check("with the ring full, a follower gets only the three lines it has not seen",
          fresh == [f"line {con.seq - 2}", f"line {con.seq - 1}", f"line {con.seq}"]
          and not reset, fresh)
    check("...nothing at all when it is up to date", con.since(con.seq) == ([], False))
    fresh, reset = con.since(con.seq - CONSOLE_LINES - 1)
    check("...and the whole ring with reset only once its line has left the ring",
          reset and len(fresh) == CONSOLE_LINES)
    check("a number from before an agent restart (ahead of the count) is a reset too",
          con.since(con.seq + 10)[1])

    sid = "eeeeeeee-0000-0000-0000-000000000001"
    async with Agent(process_cfg("console")) as a:
        await fake_server(a, sid, 30041)
        await a.client.post(f"/v1/servers/{sid}/power", json={"action": "start"})
        await until(lambda: a.sup.servers[sid].state == "running")
        first = (await a.client.get(f"/v1/servers/{sid}/console")).json()
        await a.client.post(f"/v1/servers/{sid}/command", json={"command": "say one"})
        await until(lambda: any("ran: say one" in ln
                                for ln in a.sup.servers[sid].console.lines))
        after = (await a.client.get(f"/v1/servers/{sid}/console",
                                    params={"after": first["seq"]})).json()
        check("GET /v1/servers/{id}/console?after=<seq> returns only the newer lines",
              after["lines"] == ["> say one", after["lines"][-1]]
              and "ran: say one" in after["lines"][-1] and not after["reset"]
              and after["seq"] == first["seq"] + 2, after)
        await a.client.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
        await until(lambda: a.sup.servers[sid].running is None)

    # The relay, with a fake agent socket.
    from app import console_hub

    class FakeSocket:
        def __init__(self, frames):
            self.frames = frames
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True

        async def recv(self):
            frame = await self.frames.get()
            if frame is None:
                raise ConnectionError("the agent went away")
            return __import__("json").dumps(frame)

    opened = []

    async def connect(server_id):
        frames = asyncio.Queue()
        frames.put_nowait({"type": "backlog", "lines": ["a", "b"], "seq": 2,
                           "state": "running"})
        opened.append(frames)
        return FakeSocket(frames)

    old_grace, old_queue = console_hub.GRACE, console_hub.VIEWER_QUEUE
    console_hub.GRACE, console_hub.VIEWER_QUEUE = 0.3, 5
    hub = console_hub.Hub(connect)
    try:
        one, two = hub.join(sid), hub.join(sid)
        b1 = await asyncio.wait_for(one.get(), 2)
        b2 = await asyncio.wait_for(two.get(), 2)
        check("every viewer starts from the backlog, from one upstream socket",
              b1["type"] == b2["type"] == "backlog" and b1["lines"] == ["a", "b"]
              and len(opened) == 1)
        opened[0].put_nowait({"type": "lines", "lines": ["c"], "seq": 3})
        l1 = await asyncio.wait_for(one.get(), 2)
        l2 = await asyncio.wait_for(two.get(), 2)
        check("each new line goes to every viewer", l1["lines"] == l2["lines"] == ["c"])
        late = hub.join(sid)
        b3 = await asyncio.wait_for(late.get(), 2)
        check("a viewer joining later gets the backlog so far, still one upstream",
              b3["lines"] == ["a", "b", "c"] and len(opened) == 1)
        hub.leave(sid, late)
        got = []

        async def keep_up():
            while True:
                got.extend((await one.get())["lines"])
        reader = asyncio.create_task(keep_up())
        for i in range(8):          # `one` keeps up; `two` reads nothing
            opened[0].put_nowait({"type": "lines", "lines": [f"x{i}"], "seq": 4 + i})
            await asyncio.sleep(0.02)
        await until(lambda: len(got) >= 8, 2)
        reader.cancel()
        frames = []
        while not two.empty():
            frames.append(two.get_nowait())
        feed = hub.feeds[sid]
        check("a slow viewer never holds up the others", got == [f"x{i}" for i in range(8)])
        check("...it is sent the backlog afresh instead of a console with a hole, "
              "and the drop is counted",
              frames and frames[0]["type"] == "backlog" and feed.dropped > 0
              and frames[-1].get("lines", [None])[-1] in ("x7",), frames[:1])
        opened[0].put_nowait(None)          # the agent restarts
        back = await asyncio.wait_for(one.get(), 5)
        while back["type"] != "backlog":
            back = await asyncio.wait_for(one.get(), 5)
        check("when the agent goes away the relay reconnects while anybody watches",
              len(opened) == 2 and back["type"] == "backlog")
        hub.leave(sid, one)
        hub.leave(sid, two)
        await asyncio.sleep(0.2)
        check("the upstream stays open for a moment after the last viewer leaves",
              sid in hub.feeds)
        await until(lambda: sid not in hub.feeds, 10)
        check("...then closes", sid not in hub.feeds and len(opened) == 2)
    finally:
        console_hub.GRACE, console_hub.VIEWER_QUEUE = old_grace, old_queue
        await hub.close()


async def main():
    await test_file_ops_off_loop()
    await test_stats_sampler()
    await test_shared_clients()
    await test_readopt()
    await test_console()
    if LIVE:
        await test_readopt_live()
    else:
        print("\n(the Docker tier was not run; add --live where Docker is available)")
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
