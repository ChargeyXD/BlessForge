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
  * several servers share a port, one running at a time: a start while
    another holds it is refused naming it, and a switch stops that one
    through its own stop and starts this one (5);
  * ports another app holds (Crafty's whole range): GET /v1/ports lists
    them, Port: automatic moves a server to a free one at start, and a
    fixed port that is taken says which port is free next (4);
  * boots are queued: at most max_concurrent_boots at once, each holding a
    slot until "Done", the rest waiting with a console line (7.2);
  * installs take turns on their own pool, so an install never holds up a
    start, nor a start an install (7.3);
  * committed memory -- the running servers' limits -- is reported against
    the node's RAM, and only a node set to refuse overcommit refuses (7.5);
  * the panel asks each node once per poll for every server's stats, and a
    node that does not answer shows its last state, marked, in seconds (7.6);
  * a slow panel never blocks the node: events stay batched in a bounded
    queue, and what it drops is counted and reported (7.7);
  * player pings go where the game ports are: host.docker.internal for an
    agent in a container, not its own 127.0.0.1 (6.4);
  * the disk watchdog walks on a thread, and walks a server far from its
    limit less often (6.7);
  * reading a server's add-on configs at start is off the loop too (6.11);
  * the runtime images its servers use -- only those Java versions -- are
    pulled (or built) in the background when the agent starts, and a start
    that has to wait on one says so in its console before it waits (6.8);
  * each image is looked up once per process, not at every start, and one
    pruned since is fetched again when a create cannot find it (6.10);
  * the node downloads a pack's mods itself (fetch_many): in parallel, only
    from the hosts it allows (redirects too), checked by SHA-1 and size
    before anything lands, retried, written through the jail as copies of
    its own cache, never links (3a) -- and ten servers on the same pack cost
    one download (7.8);

The agent runs in this process (its real app, lifespan and all) on the
PROCESS runtime; the Docker parts that need a daemon run with --live.

    python dev/tools/test_agent_node.py
    python dev/tools/test_agent_node.py --live     # also against real Docker
"""
from __future__ import annotations

import asyncio
import json
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
        # A compress on the loop stalls it for its whole second; off it, what
        # is left is scheduling noise -- under 50 ms here alone, and up to
        # about 120 with every suite and a modpack boot sharing the CPU.
        check("the event loop never stalled for anything like a compress",
              probe.worst < 0.25, f"worst {probe.worst * 1000:.0f} ms")
        lag = (await c.get("/v1/system")).json().get("loop_lag_ms")
        check("the agent's own loop-lag probe saw it too, on /v1/system",
              lag is not None and lag < 250, lag)
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


# --- 5. several servers on one port, switched between ----------------------------------


async def test_shared_port():
    section("5. several servers on one port, one running at a time")
    a_id, b_id, c_id = ("ffffffff-0000-0000-0000-00000000000a",
                        "ffffffff-0000-0000-0000-00000000000b",
                        "ffffffff-0000-0000-0000-00000000000c")
    async with Agent(process_cfg("shared")) as a:
        c = a.client
        await fake_server(a, a_id, 30051, name="Survival", auto_start=True)
        await fake_server(a, b_id, 30051, name="Creative")
        check("two specs on one port can be saved",
              a.sup.servers[a_id].spec["port"] == a.sup.servers[b_id].spec["port"] == 30051)
        script = TMP / "fake_server.py"
        r = await c.put(f"/v1/servers/{c_id}", json={
            "name": "Skyblock", "port": 30051, "auto_start": True,
            "execution_command": f"{sys.executable} -u {script}"})
        check("a second server that starts by itself on the same port is refused, naming "
              "the first", r.status_code == 409
              and r.json().get("conflict", {}).get("server") == a_id, r.text)

        await c.post(f"/v1/servers/{a_id}/power", json={"action": "start"})
        await until(lambda: a.sup.servers[a_id].state == "running")
        r = await c.post(f"/v1/servers/{b_id}/power", json={"action": "start"})
        conflict = r.json().get("conflict") or {}
        check("starting B while A runs gives a 409 naming A, its name and the port",
              r.status_code == 409 and conflict.get("server") == a_id
              and conflict.get("name") == "Survival" and conflict.get("port") == 30051,
              r.text)
        check("...and B is not left half-started",
              a.sup.servers[b_id].running is None and a.sup.servers[b_id].state == "offline")
        r = await c.post(f"/v1/servers/{b_id}/power", json={"action": "switch"})
        sa, sb = a.sup.servers[a_id], a.sup.servers[b_id]
        await until(lambda: sb.state == "running")
        check("switching stops A through its own stop (it saved and exited 0)",
              r.status_code == 200 and sa.running is None
              and (sa.last_exit or {}).get("asked") and (sa.last_exit or {}).get("code") == 0
              and any("Stopping the server" in ln for ln in sa.console.lines),
              sa.last_exit)
        check("...then starts B", sb.running is not None and sb.state == "running")
        r = await c.get(f"/v1/servers/{a_id}")
        check("status comes from the agent's own state, not a ping of the port",
              r.json()["state"] == "offline" and (await c.get(f"/v1/servers/{b_id}"))
              .json()["state"] == "running")
        await c.post(f"/v1/servers/{b_id}/power", json={"action": "stop"})
        await until(lambda: sb.running is None)

    # On the agent's boot only one auto-start server per port starts, even
    # when an older record has two marked.
    from agent.events import Events
    from agent.main import _reconcile
    from agent.servers import Supervisor
    cfg = process_cfg("shared-boot")
    desired = [{"uuid": u, "name": n, "port": 30061, "auto_start": True,
                "execution_command": "java -jar server.jar"}
               for u, n in ((a_id, "Survival"), (b_id, "Creative"))]

    def panel(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"servers": desired})

    events = Events(cfg)
    events.panel = lambda: httpx.AsyncClient(transport=httpx.MockTransport(panel))
    runtime = FakeDocker({})
    sup = Supervisor(cfg, runtime, events)
    await _reconcile(cfg, sup, events)
    drift = [e for e in events.queue if e["type"] == "drift"]
    check("on boot only one auto-start server per port starts; the other is reported",
          len(runtime.started) == 1 and sup.servers[b_id].running is not None
          and sup.servers[a_id].running is None
          and any(e["server"] == a_id and "30061" in e["problem"] for e in drift),
          (len(runtime.started), drift))
    check("...and both records stay on the node", set(sup.servers) == {a_id, b_id})


# --- 4. ports another app holds -----------------------------------------------------------


class PortsDocker(FakeDocker):
    """Crafty publishes 25500-25600; a host program holds `bound` too."""

    def __init__(self, bound=()):
        super().__init__({})
        self.bound = set(bound)
        self.tried = []

    def container(self, uuid):
        return "bf-" + uuid

    async def host_ports(self):
        return {p: "big-bear-crafty" for p in range(25500, 25601)}

    async def start(self, server, folder, on_output, on_exit):
        from agent.containers import RuntimeError_
        self.tried.append(int(server["port"]))
        if int(server["port"]) in self.bound | set(range(25500, 25601)):
            raise RuntimeError_(f"Port {server['port']} is already in use on this machine",
                                409, taken=int(server["port"]))
        self.started.append(server["uuid"])
        return FakeRunning()


async def test_ports():
    section("4. ports another app holds: use whatever is free right now")
    from agent.servers import Supervisor
    auto_id, fixed_id = ("abababab-0000-0000-0000-00000000000a",
                         "abababab-0000-0000-0000-00000000000b")
    cfg = process_cfg("ports")
    runtime = PortsDocker(bound={25601})
    events = FakeEvents()
    sup = Supervisor(cfg, runtime, events)
    sup.put(auto_id, {"name": "Imported", "port": 25650, "port_auto": True,
                      "port_range": [25500, 25700], "execution_command": "java -jar s.jar"})
    sup.put(fixed_id, {"name": "Fixed", "port": 25550, "port_range": [25500, 25700],
                       "execution_command": "java -jar s.jar"})
    folder = sup.folder(auto_id)
    (folder / "server.properties").write_text("motd=hi\nserver-port=25550\n"
                                              "query.port=25550\n", encoding="utf-8")
    await sup.power(auto_id, "start")
    srv = sup.servers[auto_id]
    props = (folder / "server.properties").read_text(encoding="utf-8")
    moved = [d for k, u, d in events.sent if k == "port_changed" and u == auto_id]
    check("Port: automatic keeps a free port as it is", runtime.tried == [25650]
          and not moved and srv.spec["port"] == 25650, runtime.tried)

    sup.servers[auto_id].running = None
    sup.put(auto_id, {**srv.spec, "port": 25550})          # inside Crafty's range
    runtime.tried.clear()
    await sup.power(auto_id, "start")
    srv = sup.servers[auto_id]
    props = (folder / "server.properties").read_text(encoding="utf-8")
    moved = [d for k, u, d in events.sent if k == "port_changed" and u == auto_id]
    check("a taken port is swapped at start for the first free one in the range "
          "(past one a host program refused at bind)",
          srv.spec["port"] == 25602 and runtime.tried == [25601, 25602], runtime.tried)
    check("...written into server.properties through the jail, query.port with it",
          "server-port=25602" in props and "query.port=25602" in props
          and "motd=hi" in props, props)
    check("...reported as port_changed for the panel's record, and kept on disk",
          moved and moved[-1]["port"] == 25602 and moved[0]["previous"] == 25550
          and '"port": 25602' in (sup.specs_dir / f"{auto_id}.json").read_text())
    check("...and the console says where it runs",
          any("runs on 25602 instead" in ln for ln in srv.console.lines))

    try:
        await sup.power(fixed_id, "start")
        err = None
    except Exception as e:      # noqa: BLE001
        err = e
    check("a fixed port that is taken is refused with the next free port to offer "
          "(past Crafty's range and the server now running on 25602)",
          err is not None and getattr(err, "status", 0) == 409
          and err.extra.get("next_free") == 25601 and "25601" in str(err),
          getattr(err, "extra", err))

    async with Agent(process_cfg("ports-api")) as a:
        a.sup.runtime.host_ports = runtime.host_ports
        await fake_server(a, auto_id, 30081, name="Up")
        await a.client.post(f"/v1/servers/{auto_id}/power", json={"action": "start"})
        await until(lambda: a.sup.servers[auto_id].state == "running")
        body = (await a.client.get("/v1/ports")).json()
        by = {p["port"]: p for p in body["ports"]}
        check("GET /v1/ports lists every container's published ports and this node's "
              "running servers", by.get(25500, {}).get("by") == "big-bear-crafty"
              and by.get(30081, {}).get("server") == auto_id and len(by) == 102, len(by))
        await a.client.post(f"/v1/servers/{auto_id}/power", json={"action": "stop"})
        await until(lambda: a.sup.servers[auto_id].running is None)


async def test_ports_live():
    section("4 LIVE: another app's published ports, as Docker reports them")
    from agent.containers import DockerRuntime, _docker_client
    cfg = AgentConfig(node_id="node-live-ports", token_id="t", token="t",
                      panel_url="http://x", panel_public_key=PUB,
                      servers_root=TMP / "live-ports", state_dir=TMP / "live-ports-state",
                      game_bind_ip="127.0.0.1")
    name = "bf-test-other-app"
    async with _docker_client(cfg, 60) as c:
        await c.delete(f"/containers/{name}", params={"force": "true"})
        r = await c.post("/containers/create", params={"name": name}, json={
            "Image": "python:3.12-slim", "Cmd": ["sleep", "60"],
            "ExposedPorts": {"30091/tcp": {}, "30092/udp": {}},
            "HostConfig": {"PortBindings": {
                "30091/tcp": [{"HostIp": "127.0.0.1", "HostPort": "30091"}],
                "30092/udp": [{"HostIp": "127.0.0.1", "HostPort": "30092"}]}}})
        assert r.status_code < 400, r.text
        await c.post(f"/containers/{name}/start")
    try:
        held = await DockerRuntime(cfg).host_ports()
        check("LIVE host ports published by another container are listed, by name",
              held.get(30091) == name and held.get(30092) == name, held)
    finally:
        async with _docker_client(cfg, 30) as c:
            await c.delete(f"/containers/{name}", params={"force": "true"})


# --- 7.2 a start queue with staggered boots ------------------------------------------------


SLOW_BOOT = r'''
import sys, time
def say(m): print("[Server thread/INFO]: " + m, flush=True)
say("Starting minecraft server version fake-1.0")
time.sleep(float(sys.argv[1]))
say('Done (1.0s)! For help, type "help"')
for line in sys.stdin:
    if line.strip() == "stop":
        sys.exit(0)
'''


async def test_boot_queue():
    section("7.2 a start queue: at most two boots at once")
    script = TMP / "slow_boot.py"
    script.write_text(SLOW_BOOT, encoding="utf-8")
    ids = [f"babababa-0000-0000-0000-00000000000{i}" for i in range(4)]
    async with Agent(process_cfg("queue", max_concurrent_boots=2)) as a:
        c = a.client
        for i, sid in enumerate(ids):
            r = await c.put(f"/v1/servers/{sid}", json={
                "name": f"Pack {i}", "port": 30100 + i,
                "execution_command": f"{sys.executable} -u {script} 0.8"})
            assert r.status_code == 200, r.text
        t0 = time.perf_counter()
        answers = [await c.post(f"/v1/servers/{sid}/power", json={"action": "start"})
                   for sid in ids]
        took = time.perf_counter() - t0
        states = [a.sup.servers[sid].state for sid in ids]
        check("four starts answer at once; two boot and two wait their turn",
              all(r.status_code == 200 for r in answers) and took < 1.0
              and states == ["starting", "starting", "queued", "queued"], (states, took))
        check("...the waiting ones say so in their console",
              any("Waiting to start (2 servers booting)" in ln
                  for ln in a.sup.servers[ids[2]].console.lines))
        peak = 0
        done_at = {}
        end = time.time() + 15
        while time.time() < end and len(done_at) < 4:
            booting = sum(1 for sid in ids if a.sup.servers[sid].state == "starting")
            peak = max(peak, booting)
            for sid in ids:
                if a.sup.servers[sid].state == "running" and sid not in done_at:
                    done_at[sid] = time.perf_counter() - t0
            await asyncio.sleep(0.05)
        check("never more than two boot at once, and all four come up",
              peak <= 2 and len(done_at) == 4, (peak, done_at))
        check("...the queued two after the first two were done",
              min(done_at[ids[2]], done_at[ids[3]]) > max(done_at[ids[0]], done_at[ids[1]]),
              done_at)
        for sid in ids:
            await c.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
        await until(lambda: all(a.sup.servers[s].running is None for s in ids), 20)

        # A queued start can be called off.
        for sid in ids[:3]:
            await c.post(f"/v1/servers/{sid}/power", json={"action": "start"})
        r = await c.post(f"/v1/servers/{ids[2]}/power", json={"action": "stop"})
        await asyncio.sleep(2.0)
        check("stopping a queued server takes it out of the queue; it never starts",
              r.status_code == 200 and a.sup.servers[ids[2]].running is None
              and a.sup.servers[ids[2]].state == "offline")
        for sid in ids[:2]:
            await c.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
        await until(lambda: all(a.sup.servers[s].running is None for s in ids), 20)


# --- 7.3 installs on their own pool -------------------------------------------------------


async def test_install_slots():
    section("7.3 installs take turns on their own pool, apart from boots")
    ids = [f"cdcdcdcd-0000-0000-0000-00000000000{i}" for i in range(3)]
    async with Agent(process_cfg("installs", max_concurrent_installs=1,
                                 max_concurrent_boots=1)) as a:
        c = a.client
        for i, sid in enumerate(ids[:2]):
            await c.put(f"/v1/servers/{sid}", json={"name": f"I{i}", "port": 30200 + i})
        await fake_server(a, ids[2], 30202, name="Playing")
        jobs = []
        for sid in ids[:2]:
            r = await c.post(f"/v1/servers/{sid}/install", json={
                "run": [sys.executable, "-c", "import time; time.sleep(1.0)"], "timeout": 30})
            jobs.append((sid, r.json()["job_id"]))
        await asyncio.sleep(0.3)
        first = (await c.get(f"/v1/servers/{jobs[0][0]}/install/{jobs[0][1]}")).json()
        second = (await c.get(f"/v1/servers/{jobs[1][0]}/install/{jobs[1][1]}")).json()
        check("with one install slot, the second install waits and says so",
              first["status"] == "running" and second["status"] == "waiting"
              and any("waiting for another install" in ln for ln in second["output"]),
              (first["status"], second["status"]))
        t0 = time.perf_counter()
        r = await c.post(f"/v1/servers/{ids[2]}/power", json={"action": "start"})
        up = await until(lambda: a.sup.servers[ids[2]].state == "running", 5)
        check("...while a server starts and boots at once beside them",
              r.status_code == 200 and up and time.perf_counter() - t0 < 0.9,
              f"{time.perf_counter() - t0:.2f}s")
        done = await until(lambda: all(
            a.sup.installs[j]["status"] == "done" for _s, j in jobs), 10)
        check("...and both installs finish, one after the other",
              done and a.sup.installs[jobs[1][1]]["finished"]
              >= a.sup.installs[jobs[0][1]]["finished"] + 0.9)
        await c.post(f"/v1/servers/{ids[2]}/power", json={"action": "stop"})
        await until(lambda: a.sup.servers[ids[2]].running is None)


# --- 7.5 memory accounting without refusing -----------------------------------------------


async def test_committed_memory():
    section("7.5 committed memory: reported, and refused only where a node says so")
    import psutil
    ram_mb = psutil.virtual_memory().total // 1048576
    big, small = ("efefefef-0000-0000-0000-00000000000a", "efefefef-0000-0000-0000-00000000000b")
    for refuse in (False, True):
        async with Agent(process_cfg(f"commit-{refuse}", refuse_overcommit=refuse)) as a:
            c = a.client
            await fake_server(a, big, 30301, name="Big", memory_mb=ram_mb - 1024)
            await fake_server(a, small, 30302, name="Small", memory_mb=4096)
            await c.post(f"/v1/servers/{big}/power", json={"action": "start"})
            await until(lambda: a.sup.servers[big].running is not None)
            system = (await c.get("/v1/system")).json()
            r = await c.post(f"/v1/servers/{small}/power", json={"action": "start"})
            if not refuse:
                check("/v1/system reports committed memory: the running servers' limits",
                      system["committed_bytes"] == (ram_mb - 1024) * 1048576,
                      system["committed_bytes"])
                check("a start past the node's RAM is not refused by default (the panel "
                      "warns and asks)", r.status_code == 200, r.text)
            else:
                check("...a node with refuse_overcommit refuses it, saying how much",
                      r.status_code == 409 and r.json().get("overcommit", {})
                      .get("total_mb") == ram_mb, r.text)
            for sid in (big, small):
                await c.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
            await until(lambda: all(a.sup.servers[x].running is None for x in (big, small)))


# --- 7.6 the panel stays fast with many servers and nodes -------------------------------


async def test_panel_fleet():
    section("7.6 one stats call per node, and a dead node never holds the page")
    from types import SimpleNamespace
    from app.engine import agent as panel_side
    from app.engine.base import EngineError
    calls = []
    mode = {"down": False, "legacy": False}
    ids = [f"0a0a0a0a-0000-0000-0000-00000000000{i}" for i in range(8)]

    async def fake_call(method, path, *, timeout=60, **kw):
        calls.append((path, timeout))
        await asyncio.sleep(0.05)
        if mode["down"]:
            raise EngineError("node n1 is not answering (ConnectTimeout)", 503)
        if path == "/v1/stats":
            if mode["legacy"]:
                raise EngineError("Not Found", 404)
            return {"servers": {sid: {"running": True, "state": "running", "online": i}
                                for i, sid in enumerate(ids)}}
        return {"stats": {"running": False, "state": "offline"}}

    drv = panel_side.AgentEngine(SimpleNamespace(id="n-fleet"), "http://n1:8443", "Bearer x.y")
    drv._call = fake_call
    got = await asyncio.gather(*(drv.get_stats(sid) for sid in ids))
    check("eight servers' stats on one poll are one GET /v1/stats to their node",
          [p for p, _t in calls] == ["/v1/stats"] and [g["online"] for g in got]
          == list(range(8)), calls)
    check("...asked with a short timeout (a few seconds, not 15)",
          all(t <= 3 for _p, t in calls), calls)
    await drv.get_stats(ids[0])
    check("...and shared for a moment rather than asked again", len(calls) == 1)
    mode["down"] = True
    panel_side._stats["n-fleet"]["at"] = -1e9          # the next poll
    t0 = time.perf_counter()
    stale = await drv.get_stats(ids[3])
    check("a node that stops answering gives its last reading, marked down, at once",
          stale.get("node_down") and stale["online"] == 3
          and time.perf_counter() - t0 < 1, stale)
    try:
        await drv.get_stats("0a0a0a0a-0000-0000-0000-0000000000ff")
        unknown = None
    except EngineError as e:
        unknown = e.status
    check("...and a server it never reported is plainly not answering (503)", unknown == 503)
    mode.update(down=False, legacy=True)
    panel_side._stats["n-fleet"]["at"] = -1e9
    calls.clear()
    old = await drv.get_stats(ids[1])
    check("an older agent without /v1/stats is still asked per server",
          [p for p, _t in calls] == ["/v1/stats", f"/v1/servers/{ids[1]}"]
          and old["state"] == "offline", calls)
    panel_side._stats.pop("n-fleet", None)


# --- 6.8 runtime images ----------------------------------------------------------------


def docker_cfg(name: str) -> AgentConfig:
    return AgentConfig(node_id="node-test", token_id="t", token="t",
                       panel_url="http://x", panel_public_key=PUB,
                       servers_root=TMP / name / "servers",
                       state_dir=TMP / name / "state", game_bind_ip="127.0.0.1")


class FakeImages:
    """Docker's API as far as images go, with none of the runtime images yet.
    Containers cannot be created: a start stops there, after its image."""

    def __init__(self):
        self.calls = []
        self.present: set[str] = set()
        self.pullable = True
        self.buildable = True
        self.gate: asyncio.Event | None = None     # holds every pull while set
        self.on_pull = None

    def made(self, path: str) -> list[str]:
        return [f"{q['fromImage']}:{q['tag']}" if p == "/images/create" else q["t"]
                for _m, p, q in self.calls if p == path]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, dict(request.url.params)
        self.calls.append((request.method, p, q))
        if request.method == "GET" and p.startswith("/images/") and p.endswith("/json"):
            name = p[len("/images/"):-len("/json")]
            return httpx.Response(200 if name in self.present else 404, json={})
        if p == "/images/create":
            if self.on_pull:
                self.on_pull()
            if self.gate is not None:
                await self.gate.wait()
            if not self.pullable:
                return httpx.Response(500, json={"message": "denied"})
            self.present.add(f"{q['fromImage']}:{q['tag']}")
            return httpx.Response(200, text='{"status":"Downloaded newer image"}\n')
        if p == "/build":
            if not self.buildable:
                return httpx.Response(200, text='{"error":"base image not found"}\n')
            self.present.add(q["t"])
            return httpx.Response(200, text='{"stream":"Successfully built"}\n')
        if p == "/containers/create":
            image = json.loads(request.content)["Image"]
            if image not in self.present:
                return httpx.Response(404, json={"message": f"No such image: {image}"})
            return httpx.Response(500, json={"message": "no containers in this test"})
        return httpx.Response(404, json={"message": "no such container"})


async def test_runtime_images():
    section("6.8 runtime images: fetched ahead of a first start, and a wait is said")
    from agent import containers
    from agent.servers import Supervisor
    fake = FakeImages()

    def forget(image):
        """Gone from the machine, and from what this process remembers."""
        fake.present.discard(image)
        containers._images_ready.discard(image)
    # A fresh process, as far as images go (7.1's fake Docker had Java 21).
    containers._images_ready.clear()
    real_client = containers._docker_client
    containers._docker_client = lambda cfg, timeout=60: httpx.AsyncClient(
        transport=httpx.MockTransport(fake), base_url="http://docker")
    try:
        cfg = docker_cfg("images")
        sup = Supervisor(cfg, containers.DockerRuntime(cfg), FakeEvents())
        ids = [f"eeeeeeee-0000-0000-0000-00000000000{i}" for i in range(5)]
        for sid, port, java in ((ids[0], 30041, 17), (ids[1], 30042, 21),
                                (ids[2], 30043, None)):
            sup.put(sid, {"name": f"java{java}", "port": port, "java_major": java,
                          "execution_command": "java -jar server.jar"})
        await sup.prepare_images()
        check("the startup pre-pull fetches only the Java versions its servers use "
              "(17, and 21 for the one that names none -- not 8 or 25), each once",
              sorted(fake.made("/images/create")) == sorted([cfg.images[17], cfg.images[21]])
              and not fake.made("/build"), fake.made("/images/create"))

        fake.calls.clear()
        fake.pullable = False
        sup.put(ids[3], {"name": "legacy", "port": 30044, "java_major": 8,
                         "execution_command": "java -jar server.jar"})
        await sup.prepare_images()
        builds = [q for _m, p, q in fake.calls if p == "/build"]
        check("...one it cannot pull it builds from the agent's own Dockerfile instead",
              fake.made("/images/create") == [cfg.images[8]] and len(builds) == 1
              and builds[0]["t"] == cfg.images[8]
              and json.loads(builds[0]["buildargs"]) == {"JAVA": "8"}, builds)
        fake.calls.clear()
        await sup.prepare_images()
        check("...and a second pass fetches nothing",
              not fake.made("/images/create") and not fake.made("/build"), fake.calls)

        # A start whose image is missing says so before the pull starts.
        fake.pullable = True
        sup.put(ids[4], {"name": "newest", "port": 30045, "java_major": 25,
                         "execution_command": "java -jar server.jar"})
        srv = sup.servers[ids[4]]
        at_pull = []
        fake.on_pull = lambda: at_pull.append(list(srv.console.lines))
        try:
            await sup.power(ids[4], "start")
        except containers.RuntimeError_:
            pass                        # this fake Docker creates no containers
        fake.on_pull = None
        line = "[BlessForge] Preparing Java 25 (first time only)"
        check("a start that has to fetch its image says \"Preparing Java 25 (first time "
              "only)\" in its console before the pull begins",
              at_pull and line in at_pull[0] and fake.made("/images/create") == [cfg.images[25]],
              at_pull)
        check("...after its \"Starting\" line, so the console reads in order",
              [ln for ln in srv.console.lines if ln.startswith("[BlessForge]")][:2]
              == ["[BlessForge] Starting newest", line], list(srv.console.lines))
        try:
            await sup.power(ids[1], "start")
        except containers.RuntimeError_:
            pass
        check("a start whose image is already here says nothing about it",
              not any("Preparing" in ln for ln in sup.servers[ids[1]].console.lines),
              list(sup.servers[ids[1]].console.lines))

        # A start while the pre-pull is fetching its image waits for that pull.
        forget(cfg.images[17])
        fake.calls.clear()
        fake.gate = asyncio.Event()
        pre = asyncio.create_task(sup.prepare_images())
        await until(lambda: fake.made("/images/create"))
        start = asyncio.create_task(sup.power(ids[0], "start"))
        said = await until(lambda: any("Preparing Java 17" in ln
                                       for ln in sup.servers[ids[0]].console.lines))
        await asyncio.sleep(0.1)
        check("a start during the pre-pull of its image says so at once, and waits "
              "for that pull rather than starting another",
              said and not start.done() and fake.made("/images/create") == [cfg.images[17]],
              fake.made("/images/create"))
        fake.gate.set()
        fake.gate = None
        await pre
        try:
            await start
        except containers.RuntimeError_:
            pass
        check("...then carries on with it: one pull in all",
              fake.made("/images/create") == [cfg.images[17]]
              and any(p == "/containers/create" for _m, p, _q in fake.calls),
              fake.made("/images/create"))

        # The loader installer waits on its image the same way.
        forget(cfg.images[17])
        job = sup.start_install(ids[0], {"run": ["java", "-jar", "installer.jar"],
                                         "java_major": 17})
        await until(lambda: sup.installs[job]["status"] in ("done", "error"))
        out = sup.installs[job]["output"]
        check("...and so does an install's loader installer, in the install's output",
              "preparing Java 17 (first time only)" in out, out)
    finally:
        containers._docker_client = real_client


# --- 6.10 one image lookup per process ---------------------------------------------------


async def test_image_cache():
    section("6.10 each runtime image is looked up once per process, not per start")
    from agent import containers
    from agent.servers import Supervisor
    containers._images_ready.clear()
    fake = FakeImages()
    real_client = containers._docker_client
    containers._docker_client = lambda cfg, timeout=60: httpx.AsyncClient(
        transport=httpx.MockTransport(fake), base_url="http://docker")

    def asked(kind):
        return [p for m, p, _q in fake.calls
                if (kind == "lookup" and m == "GET" and p.startswith("/images/"))
                or (kind != "lookup" and p == kind)]

    async def start(sid):
        try:
            await sup.power(sid, "start")
        except containers.RuntimeError_:
            pass                        # this fake Docker creates no containers
    try:
        cfg = docker_cfg("imagecache")
        sup = Supervisor(cfg, containers.DockerRuntime(cfg), FakeEvents())
        ids = [f"edededed-0000-0000-0000-00000000000{i}" for i in range(3)]
        for sid, port, java in ((ids[0], 30051, 21), (ids[1], 30052, 21),
                                (ids[2], 30053, 17)):
            sup.put(sid, {"name": f"s{port}", "port": port, "java_major": java,
                          "execution_command": "java -jar server.jar"})
        fake.present.add(cfg.images[21])          # already on this machine
        for _ in range(3):
            await start(ids[0])
            await start(ids[1])
        check("six starts of two servers on one image ask Docker for it once, "
              "and an image already here is never pulled",
              asked("lookup") == [f"/images/{cfg.images[21]}/json"]
              and not asked("/images/create") and len(asked("/containers/create")) == 6,
              asked("lookup"))

        fake.calls.clear()
        fake.pullable = fake.buildable = False
        await start(ids[2])
        await start(ids[2])
        check("an image that could not be fetched is not remembered: the next start "
              "tries again",
              len(asked("lookup")) == 2 and len(asked("/images/create")) == 2
              and len(asked("/build")) == 2 and not asked("/containers/create"),
              fake.calls)
        fake.calls.clear()
        fake.pullable = True
        await start(ids[2])
        await start(ids[2])
        await sup.prepare_images()
        check("...one fetched once is: the next start and the pre-pull ask nothing",
              len(asked("lookup")) == 1 and len(asked("/images/create")) == 1
              and len(asked("/containers/create")) == 2, fake.calls)

        # Pruned behind the agent's back: the create cannot find it.
        fake.present.discard(cfg.images[21])
        fake.calls.clear()
        srv = sup.servers[ids[0]]
        srv.console.lines.clear()
        await start(ids[0])
        check("an image removed since (a prune) is noticed by the create, fetched "
              "again, saying so, and the create retried",
              len(asked("/containers/create")) == 2
              and fake.made("/images/create") == [cfg.images[21]]
              and "[BlessForge] Preparing Java 21 (first time only)" in srv.console.lines,
              (fake.calls, list(srv.console.lines)))
        fake.calls.clear()
        await start(ids[1])
        check("...and is remembered again after", not asked("lookup")
              and len(asked("/containers/create")) == 1, fake.calls)
    finally:
        containers._docker_client = real_client


# --- 7.7 bounded queues, counted drops ----------------------------------------------------


async def test_event_drops():
    section("7.7 a slow panel never blocks the node; drops are counted and told")
    from agent import events as events_mod
    from agent.events import Events
    sent = []

    def panel(request: httpx.Request) -> httpx.Response:
        sent.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"accepted": 1})

    ev = Events(process_cfg("events"))
    t0 = time.perf_counter()
    for i in range(events_mod.MAX_QUEUED + 250):
        ev.emit("stats", None, n=i)
    took = time.perf_counter() - t0
    check("emitting never waits on the panel, and the queue stays bounded",
          took < 0.5 and len(ev.queue) == events_mod.MAX_QUEUED, f"{took:.3f}s")
    check("...what it had to drop is counted", ev.dropped == 250, ev.dropped)
    ev.panel = lambda: httpx.AsyncClient(transport=httpx.MockTransport(panel))
    await ev.flush()
    told = [e for e in sent[0]["events"] if e["type"] == "events_dropped"]
    check("...and told to the panel with the next batch that gets through, once",
          told and told[0]["count"] == 250, told)
    await ev.flush()
    check("...not again with the batch after",
          not any(e["type"] == "events_dropped" for e in sent[1]["events"]))


# --- 6.4 where player pings go ------------------------------------------------------------


async def test_ping_target():
    section("6.4 player pings go where the game ports are")
    from agent import config as agent_config
    base = dict(node_id="n", token_id="t", token="t", panel_url="http://x",
                panel_public_key=PUB)
    real = agent_config.os.path.exists
    try:
        agent_config.os.path.exists = lambda p: p == "/.dockerenv"
        in_box = AgentConfig(**base, game_bind_ip="0.0.0.0").ping_target()
        agent_config.os.path.exists = lambda p: False
        on_host = AgentConfig(**base, game_bind_ip="0.0.0.0").ping_target()
    finally:
        agent_config.os.path.exists = real
    check("an agent in a container pings the host it published the ports on",
          in_box == "host.docker.internal", in_box)
    check("...one running on the host itself pings 127.0.0.1", on_host == "127.0.0.1")
    check("...a game address that is one address is pinged there",
          AgentConfig(**base, game_bind_ip="10.8.0.2").ping_target() == "10.8.0.2")
    check("...and ping_host, when set, wins",
          AgentConfig(**base, ping_host="192.168.1.5").ping_target() == "192.168.1.5")


# --- 6.7 the disk watchdog off the loop ---------------------------------------------------


async def test_disk_watchdog():
    section("6.7 the disk watchdog walks on a thread, and less often when far off")
    from agent.servers import Supervisor
    cfg = process_cfg("disk")
    far, near = ("d1d1d1d1-0000-0000-0000-00000000000a", "d1d1d1d1-0000-0000-0000-00000000000b")
    sup = Supervisor(cfg, FakeDocker({}), FakeEvents())
    sup.put(far, {"name": "Far", "port": 30401, "limits": {"disk_mb": 1000}})
    sup.put(near, {"name": "Near", "port": 30402, "limits": {"disk_mb": 1}})
    (sup.folder(near) / "world.dat").write_bytes(b"x" * 950_000)    # 91% of 1 MB
    for sid in (far, near):
        sup.servers[sid].running = FakeRunning()
    walks = []
    real = files.usage

    def slow_usage(folder, *a):
        walks.append(folder.name)
        time.sleep(0.4)          # a big pack's folder, as the loop would feel it
        return real(folder, *a)

    files.usage = slow_usage
    try:
        with LoopProbe() as probe:
            await sup.check_disks()
        first = list(walks)
        await sup.check_disks()
    finally:
        files.usage = real
    check("a pass walks each folder on a thread: the loop never waits on it",
          sorted(first) == sorted([far, near]) and probe.worst < 0.2,
          f"worst {probe.worst * 1000:.0f} ms")
    check("...and the next pass skips the one far from its limit, not the one near it",
          walks[len(first):] == [near], walks)


# --- 6.11 add-on detection off the loop ----------------------------------------------------


async def test_addons_off_loop():
    section("6.11 reading the add-ons' configs at start is off the loop")
    from agent import addons
    sid = "a11a11a1-0000-0000-0000-000000000001"
    real = addons.detect

    def slow_detect(folder, port):
        time.sleep(0.4)          # a folder with many configs, on a slow disk
        return real(folder, port)

    async with Agent(process_cfg("addons")) as a:
        await fake_server(a, sid, 30501)
        addons.detect = slow_detect
        try:
            with LoopProbe() as probe:
                r = await a.client.post(f"/v1/servers/{sid}/power", json={"action": "start"})
        finally:
            addons.detect = real
        check("a start reads the add-on configs on a thread: the loop never waits on it",
              r.status_code == 200 and probe.worst < 0.2, f"worst {probe.worst * 1000:.0f} ms")
        await until(lambda: a.sup.servers[sid].state == "running")
        await a.client.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
        await until(lambda: a.sup.servers[sid].running is None)


# --- 3a the node downloads the mods itself; 7.8 once per node ------------------------------


class CDN:
    """A stand-in for the mod sites, on 127.0.0.1, counting every request.

    /f/<name> serves the file; /flaky/<name> fails twice, then serves it;
    /bad/<name> serves it with a byte changed; /long/<name> with bytes too
    many; /hop/<name> redirects to /f/<name>; /out/<name> redirects to the
    same server under the name `localhost`, which the node does not allow;
    anything else is a 404."""

    def __init__(self):
        import collections
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.files: dict[str, bytes] = {}
        self.hits: collections.Counter = collections.Counter()
        self.delay = 0.0
        self.inflight = self.most = 0
        lock = threading.Lock()
        cdn = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=()):
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                with lock:
                    cdn.hits[self.path] += 1
                    n = cdn.hits[self.path]
                    cdn.inflight += 1
                    cdn.most = max(cdn.most, cdn.inflight)
                try:
                    time.sleep(cdn.delay)
                    _, route, name = (self.path.split("/", 2) + ["", ""])[:3]
                    data = cdn.files.get(name)
                    if data is None:
                        return self._send(404)
                    if route == "f" or (route == "flaky" and n > 2):
                        return self._send(200, data)
                    if route == "flaky":
                        return self._send(500)
                    if route == "bad":
                        return self._send(200, data[:-1] + bytes([data[-1] ^ 1]))
                    if route == "long":
                        return self._send(200, data + b"more")
                    if route == "hop":
                        return self._send(302, headers=[("Location", f"/f/{name}")])
                    if route == "out":
                        return self._send(302, headers=[(
                            "Location", f"http://localhost:{cdn.port}/f/{name}")])
                    return self._send(404)
                finally:
                    with lock:
                        cdn.inflight -= 1

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def entry(self, route: str, name: str, path: str | None = None,
              host: str = "127.0.0.1") -> dict:
        import hashlib
        data = self.files[name]
        return {"url": f"http://{host}:{self.port}/{route}/{name}",
                "path": path or f"mods/{name}",
                "sha1": hashlib.sha1(data).hexdigest(), "size": len(data)}

    def total(self) -> int:
        return sum(self.hits.values())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


async def install_done(c, sid: str, job: str, seconds: float = 60) -> dict:
    end = time.time() + seconds
    rec = {}
    while time.time() < end:
        rec = (await c.get(f"/v1/servers/{sid}/install/{job}")).json()
        if rec.get("status") in ("done", "error"):
            return rec
        await asyncio.sleep(0.05)
    return rec


async def fetch(c, sid: str, items: list[dict]) -> dict:
    r = await c.post(f"/v1/servers/{sid}/install", json={"fetch_many": items})
    assert r.status_code == 200, r.text
    return await install_done(c, sid, r.json()["job_id"])


def lands(folder: pathlib.Path, item: dict, data: bytes) -> bool:
    """On disk, byte for byte, read back through the jail -- which refuses a
    file with another hard link -- and with no other link to it."""
    try:
        got = files.read_bytes(folder, item["path"])
    except files.JailError:
        return False
    return got == data and (folder / item["path"]).stat().st_nlink == 1


async def test_fetch_many():
    section("3a the node downloads a pack's mods itself")
    import os
    import random
    from agent import downloads
    cdn = CDN()
    rng = random.Random(7)
    mods = [f"mod{i:02d}.jar" for i in range(16)]
    for i, name in enumerate(mods):
        cdn.files[name] = rng.randbytes(150_000 + i * 997)
    cdn.files["big.jar"] = rng.randbytes(3 * 1048576 + 17)     # several chunks
    for name in ("flaky.jar", "bad.jar", "long.jar", "hop.jar", "secret.jar", "far.jar",
                 "linked.jar", "gone.jar"):
        cdn.files[name] = rng.randbytes(40_000)
    saved = downloads.BACKOFF
    downloads.BACKOFF = (0.05, 0.05, 0.05)
    s1, s2 = "fefefefe-0000-0000-0000-000000000001", "fefefefe-0000-0000-0000-000000000002"
    try:
        async with Agent(process_cfg("fetch", download_hosts=["127.0.0.1"])) as a:
            c = a.client
            for i, sid in enumerate((s1, s2)):
                await c.put(f"/v1/servers/{sid}", json={"name": f"F{i}", "port": 30400 + i})
            one, two = a.sup.folder(s1), a.sup.folder(s2)
            pack = [cdn.entry("f", n) for n in mods] + [cdn.entry("f", "big.jar")]
            # The same jar twice under two names, as a pack can ship it.
            pack.append(cdn.entry("f", "mod00.jar", "mods/mod00.jar.disabled"))
            cdn.delay = 0.25
            # Made once per agent (its SSL context is the 60-100 ms 6.3 is
            # about); what is measured here is the fetching.
            downloads.client()
            with LoopProbe() as probe:
                rec = await fetch(c, s1, pack)
            cdn.delay = 0.0
            check("fetch_many: every file lands at its path, byte for byte",
                  rec.get("status") == "done" and all(
                      lands(one, e, cdn.files[e["url"].rsplit("/", 1)[1]]) for e in pack),
                  rec.get("error"))
            check("...several at once, never more than download_concurrency (8)",
                  4 <= cdn.most <= 8, f"{cdn.most} at once")
            check("...each downloaded once, even a jar the pack ships twice",
                  all(v == 1 for v in cdn.hits.values()) and len(cdn.hits) == len(mods) + 1
                  and rec["progress"]["downloaded"] == len(mods) + 1
                  and rec["progress"]["cached"] == 1, dict(rec.get("progress") or {}))
            check("...and the panel is told how many are in place", rec.get("fetched") == len(pack))
            check("...with progress in the install's output and the server's console",
                  any(ln.startswith("fetching 18 files") for ln in rec["output"])
                  and any("fetched 18 of 18 files: 17 downloaded, 1 from this node's cache"
                          in ln for ln in rec["output"])
                  and any(ln.startswith("[install] fetching")
                          for ln in a.sup.servers[s1].console.lines), rec["output"][-2:])
            check("...without stalling the event loop",
                  probe.worst < 0.25, f"worst {probe.worst * 1000:.0f} ms")
            cached = a.sup.cache.path(pack[0]["sha1"])
            served = one / pack[0]["path"]
            check("the server's copy is a copy, not a link to the node's cache",
                  cached.stat().st_nlink == 1 and served.stat().st_nlink == 1
                  and cached.stat().st_ino != served.stat().st_ino
                  and not served.is_symlink())

            before = cdn.total()
            rec = await fetch(c, s2, pack)
            check("a second server on the same pack downloads nothing at all",
                  rec.get("status") == "done" and cdn.total() == before
                  and rec["progress"]["cached"] == len(pack)
                  and all(lands(two, e, cdn.files[e["url"].rsplit("/", 1)[1]]) for e in pack),
                  dict(rec.get("progress") or {}))

            outside = TMP / "fetch-outside.jar"
            outside.write_bytes(b"not yours")
            os.symlink(outside, one / "mods" / "linked.jar")
            odd = [cdn.entry("flaky", "flaky.jar"), cdn.entry("hop", "hop.jar"),
                   cdn.entry("bad", "bad.jar"), cdn.entry("long", "long.jar"),
                   cdn.entry("f", "secret.jar", host="localhost"),
                   cdn.entry("out", "far.jar"), cdn.entry("f", "linked.jar"),
                   {**cdn.entry("f", "gone.jar"),
                    "url": f"http://127.0.0.1:{cdn.port}/f/nothing-here.jar"}]
            rec = await fetch(c, s1, odd)
            failed = {f["path"]: f["error"] for f in rec.get("failed") or []}
            check("a download that fails twice is retried with backoff and lands",
                  lands(one, odd[0], cdn.files["flaky.jar"])
                  and cdn.hits["/flaky/flaky.jar"] == 3, cdn.hits["/flaky/flaky.jar"])
            check("a redirect to an allowed host is followed",
                  lands(one, odd[1], cdn.files["hop.jar"]) and cdn.hits["/f/hop.jar"] == 1)
            check("a file whose SHA-1 does not match is refused after its tries, and "
                  "nothing lands", "SHA-1" in failed.get("mods/bad.jar", "")
                  and not (one / "mods/bad.jar").exists()
                  and cdn.hits["/bad/bad.jar"] == downloads.ATTEMPTS, failed.get("mods/bad.jar"))
            check("so is one longer than its size, cut off as soon as it is",
                  "larger than" in failed.get("mods/long.jar", "")
                  and not (one / "mods/long.jar").exists(), failed.get("mods/long.jar"))
            check("an address off the allowlist is refused without a request",
                  "download_hosts" in failed.get("mods/secret.jar", "")
                  and cdn.hits["/f/secret.jar"] == 0, failed.get("mods/secret.jar"))
            check("a redirect to a host off the allowlist is refused, not followed",
                  "localhost is not a download host" in failed.get("mods/far.jar", "")
                  and cdn.hits["/out/far.jar"] == 1 and cdn.hits["/f/far.jar"] == 0
                  and not (one / "mods/far.jar").exists(), failed.get("mods/far.jar"))
            check("a 404 is not retried", "404" in failed.get("mods/gone.jar", "")
                  and cdn.hits["/f/nothing-here.jar"] == 1, failed.get("mods/gone.jar"))
            check("a write onto a link in the folder is refused by the jail; the file "
                  "outside is untouched", "mods/linked.jar" in failed
                  and outside.read_bytes() == b"not yours", failed.get("mods/linked.jar"))
            check("the install fails naming what failed, the rest in place",
                  rec.get("status") == "error" and "6 of 8 files" in (rec.get("error") or "")
                  and rec.get("fetched") == 2, rec.get("error"))
            leftovers = [p.name for p in one.rglob("*") if ".tmp" in p.name or ".part" in p.name]
            leftovers += [p.name for p in a.sup.cache.tmp.glob("*")]
            check("no part or temp file is left behind, in the folder or the cache",
                  not leftovers, leftovers)

            good = cdn.entry("f", "mod01.jar")
            for bad in ({**good, "path": "../escape.jar"}, {**good, "sha1": ""},
                        {**good, "size": 0}, {**good, "url": "file:///etc/passwd"}):
                r = await c.post(f"/v1/servers/{s1}/install", json={"fetch_many": [bad]})
                if r.status_code != 400:
                    break
            r2 = await c.post(f"/v1/servers/{s1}/install", json={"fetch_many": [good, good]})
            check("a list with a path outside the folder, no SHA-1, no size, a non-http "
                  "address or a path twice is the request's 400",
                  r.status_code == 400 and r2.status_code == 400, (r.text, r2.text))

            spoiled = a.sup.cache.path(good["sha1"])
            spoiled.write_bytes(b"\0" * good["size"])
            rec = await fetch(c, s2, [{**good, "path": "mods/again.jar"}])
            check("a damaged cache entry is caught by the hash on the way in, and "
                  "fetched again", rec.get("status") == "done"
                  and lands(two, {"path": "mods/again.jar"}, cdn.files["mod01.jar"])
                  and cdn.hits["/f/mod01.jar"] == 2, rec.get("output", [])[-2:])

            a.sup.cache.limit = 0
            rec = await fetch(c, s2, [cdn.entry("f", "mod02.jar", "mods/kept.jar")])
            check("a cache over its limit is trimmed after an install, oldest first; "
                  "the server keeps its files", rec.get("status") == "done"
                  and not [p for p in a.sup.cache.root.glob("??/*")]
                  and lands(two, {"path": "mods/kept.jar"}, cdn.files["mod02.jar"]))
    finally:
        downloads.BACKOFF = saved
        cdn.close()


async def test_one_download_per_node():
    section("7.8 ten servers on the same pack cost one download")
    import random
    cdn = CDN()
    rng = random.Random(8)
    names = [f"pack{i:02d}.jar" for i in range(20)]
    for name in names:
        cdn.files[name] = rng.randbytes(60_000 + rng.randrange(40_000))
    pack = [cdn.entry("f", n) for n in names]
    sids = [f"abababab-0000-0000-0000-0000000000{i:02d}" for i in range(11)]
    cdn.delay = 0.05
    try:
        cfg = dict(download_hosts=["127.0.0.1"], max_concurrent_installs=10)
        async with Agent(process_cfg("dedupe", **cfg)) as a:
            c = a.client
            for i, sid in enumerate(sids[:10]):
                await c.put(f"/v1/servers/{sid}", json={"name": f"D{i}", "port": 30500 + i})
            recs = await asyncio.gather(*(fetch(c, sid, pack) for sid in sids[:10]))
            check("ten servers installing the same pack at once: each file is downloaded "
                  "once", all(r.get("status") == "done" for r in recs)
                  and len(cdn.hits) == len(names) and all(v == 1 for v in cdn.hits.values()),
                  f"{cdn.total()} requests for {len(names)} files"
                  + "".join(f"; {r['failed']}" for r in recs if r.get("failed")))
            check("...the other nine copy what the first fetched",
                  sum(r["progress"]["downloaded"] for r in recs) == len(names)
                  and sum(r["progress"]["cached"] for r in recs) == 9 * len(names))
            check("...and every server has every file, a copy of its own",
                  all(lands(a.sup.folder(sid), e, cdn.files[e["url"].rsplit("/", 1)[1]])
                      for sid in sids[:10] for e in pack))
        async with Agent(process_cfg("dedupe", **cfg)) as a:
            await a.client.put(f"/v1/servers/{sids[10]}", json={"name": "D10", "port": 30510})
            rec = await fetch(a.client, sids[10], pack)
            check("the cache outlives an agent restart: an eleventh server costs nothing",
                  rec.get("status") == "done" and cdn.total() == len(names)
                  and rec["progress"]["cached"] == len(names), cdn.total())
    finally:
        cdn.close()


async def main():
    await test_file_ops_off_loop()
    await test_stats_sampler()
    await test_shared_clients()
    await test_readopt()
    await test_console()
    await test_shared_port()
    await test_ports()
    await test_boot_queue()
    await test_install_slots()
    await test_committed_memory()
    await test_panel_fleet()
    await test_event_drops()
    await test_ping_target()
    await test_disk_watchdog()
    await test_addons_off_loop()
    await test_runtime_images()
    await test_image_cache()
    await test_fetch_many()
    await test_one_download_per_node()
    if LIVE:
        await test_readopt_live()
        await test_ports_live()
    else:
        print("\n(the Docker tier was not run; add --live where Docker is available)")
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
