#!/usr/bin/env python3
"""BlessForge driving a real node agent, end to end, over the network.

WP-05's "done when": from BlessForge, create -> start -> console stream and a
command -> edit a file -> stop -> delete, on a node running the agent. Two
real processes -- the panel (ENGINE=agent) and the agent -- talking HTTP:

  * the node is registered through the admin API, and the config file the
    panel hands back is the one the agent is started with;
  * the agent reconciles from the panel and reports in (events);
  * the browser's console is the panel's own websocket, relaying the
    agent's: one upstream socket per server however many watch, and never
    for a page from another site -- and a token for one server cannot open
    another's on the agent;
  * the polled fallback sends only the lines after the last one it sent;
  * neither side accepts the other without the node token, and a browser
    session cookie never opens /api/agent/*.

The node runs the agent's PROCESS runtime (development only, no isolation)
because this machine has no Docker; the server is a tiny stand-in that
behaves like a Minecraft console. The container side -- the spec, the
isolation rules -- is covered by test_agent_security.py, and a Docker node
is still to be proven (NEXT-SESSION.md).
"""
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import websockets  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="bf-agent-"))
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""),
          flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
    if line == "crash":
        say("Crashing on purpose")
        sys.exit(3)
    if line.startswith("join "):
        say(line[5:] + " joined the game")
        continue
    if line.startswith("spam "):
        for i in range(int(line[5:])):
            say("spam line %d" % (i + 1))
        continue
    say("ran: " + line)
'''

API_PORT, AGENT_PORT = free_port(), free_port()
API = f"http://127.0.0.1:{API_PORT}"
ENV = {
    **os.environ,
    "ENGINE": "agent",
    "DATA_DIR": str(TMP / "panel"), "STATE_DIR": str(TMP / "panel" / "state"),
    "CACHE_DIR": str(TMP / "panel" / "cache"),
    "DATABASE_URL": f"sqlite+aiosqlite:///{(TMP / 'panel.db').as_posix()}",
    "BLESSFORGE_SECRET": "test-secret-for-the-agent-suite",
    "PUBLIC_API_URL": API,
    "REDIS_URL": "", "CURSEFORGE_API_KEY": "", "CURSEFORGE_API_KEY_B64": "",
    "OLLAMA_URL": "", "MODRINTH_ENABLED": "false", "UPDATE_CHECK_HOURS": "0",
}
os.environ.update({k: ENV[k] for k in ("ENGINE", "DATA_DIR", "STATE_DIR", "CACHE_DIR",
                                        "DATABASE_URL", "BLESSFORGE_SECRET",
                                        "PUBLIC_API_URL", "REDIS_URL")})
LOG = open(TMP / "procs.log", "ab")


async def wait_http(url, seconds=40):
    deadline = time.time() + seconds
    async with httpx.AsyncClient() as c:
        while time.time() < deadline:
            try:
                if (await c.get(url, timeout=2)).status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.3)
    return False


async def until(pred, seconds=20, every=0.3):
    deadline = time.time() + seconds
    while time.time() < deadline:
        v = await pred()
        if v:
            return v
        await asyncio.sleep(every)
    return None


async def main():
    from app import db, engine as eng
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host",
                            "127.0.0.1", "--port", str(API_PORT), "--log-level", "warning"],
                           cwd=ROOT, env=ENV, stdout=LOG, stderr=subprocess.STDOUT)
    agent = None
    try:
        check("the panel comes up on the agent engine", await wait_http(API + "/api/healthz"))
        c = httpx.AsyncClient(base_url=API, timeout=60)
        await c.post("/api/auth/login", json={"username": "admin", "password": "password"})
        await c.post("/api/auth/password", json={"current": "password",
                                                 "password": "a long test passphrase"})
        r = await c.post("/api/auth/login", json={"username": "admin",
                                                  "password": "a long test passphrase"})
        health = (await c.get("/api/health?fresh=true")).json()["checks"]["engine"]
        check("with no node registered, health says so plainly",
              health["ok"] is False and "no node is registered" in health["error"],
              health)

        # --- register the node; the panel writes the agent's config --------------
        made = (await c.post("/api/admin/nodes", json={
            "name": "Test node", "fqdn": "127.0.0.1", "agent_port": AGENT_PORT,
            "scheme": "http", "runtime": "process",
            "agent": {"allow_unsafe_process_runtime": True,
                      "listen_port": AGENT_PORT,
                      "servers_root": str(TMP / "node" / "servers"),
                      "state_dir": str(TMP / "node" / "state"),
                      "stop_timeout": 10},
        })).json()
        check("registering a node returns its config, with the token in it",
              "token = " in made.get("config", "") and made["node"]["id"] == "test-node",
              made.get("node"))
        listed = (await c.get("/api/admin/nodes")).json()["items"]
        check("the token is not in the node list afterwards",
              not any("token" in k and k != "token_id" for k in listed[0]))
        cfg_path = TMP / "agent.toml"
        cfg_path.write_text(made["config"], encoding="utf-8")
        agent = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1",
             "--port", str(AGENT_PORT), "--log-level", "warning"],
            cwd=ROOT, env={**ENV, "BLESSFORGE_AGENT_CONFIG": str(cfg_path)},
            stdout=LOG, stderr=subprocess.STDOUT)
        AGENT = f"http://127.0.0.1:{AGENT_PORT}"
        check("the agent starts from that config", await wait_http(AGENT + "/health"))

        async def online():
            items = (await c.get("/api/admin/nodes")).json()["items"]
            return items[0] if items and items[0]["state"] == "online" else None
        node = await until(online, 30)
        check("the agent reports in, and the panel marks the node online",
              node is not None, node)

        # --- the two directions are both authenticated ---------------------------------
        async with httpx.AsyncClient() as raw:
            check("the agent refuses a request with no node token",
                  (await raw.get(AGENT + "/v1/system")).status_code == 401)
            check("...and one with the wrong token",
                  (await raw.get(AGENT + "/v1/system",
                                 headers={"Authorization": "Bearer abc.def"})).status_code == 401)
            check("the panel's /api/agent/* refuses no token",
                  (await raw.get(API + "/api/agent/servers")).status_code == 401)
        check("...and refuses a browser session -- a cookie never opens it",
              (await c.get("/api/agent/servers")).status_code == 401)
        system = (await c.get("/api/admin/nodes/test-node/system")).json()
        check("the panel reaches the agent with the node token",
              system.get("node") == "test-node"
              and str(system.get("runtime")).startswith("process"),
              system)

        # --- create a server, as the install pipeline would ------------------------------
        await db.init(ENV["DATABASE_URL"])
        eng.configure("agent")
        await eng.load_nodes()
        sid = await eng.create_server(name="Agent Test", port=free_port(), memory_mb=1024)
        await eng.upload_file(sid, ".", "fake_server.py", FAKE_SERVER.encode())
        await eng.update_server(sid, {"execution_command":
                                      f'"{sys.executable}" -u fake_server.py',
                                      "executable": "fake_server.py"})
        listing = (await c.get(f"/api/instances/{sid}/files")).json()
        check("a server created on the node is listed with its file",
              any(e["name"] == "fake_server.py" for e in listing["entries"]))
        fleet = (await c.get("/api/instances")).json()["items"]
        check("...and appears in the panel's fleet", any(i["server_id"] == sid for i in fleet))

        # --- start, and follow the console over the agent's websocket -----------------------
        r = await c.post(f"/api/instances/{sid}/action/start_server")
        check("the panel starts it", r.status_code == 200, r.text[:200])
        sock = (await c.get(f"/api/instances/{sid}/console/socket")).json()
        check("the panel hands the browser its own socket, on the page's origin",
              sock.get("available") and sock.get("path") == f"/api/instances/{sid}/console/ws"
              and not sock.get("url"), sock)
        ws_url = f"ws://127.0.0.1:{API_PORT}{sock['path']}"
        cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
        browser = {"Cookie": cookie, "Origin": API}
        lines = []
        async with websockets.connect(ws_url, additional_headers=browser) as ws, \
                websockets.connect(ws_url, additional_headers=browser) as ws2:
            first = json.loads(await ws.recv())
            second = json.loads(await ws2.recv())
            lines += first.get("lines", [])
            check("each viewer starts from the backlog, and may type",
                  first["type"] == second["type"] == "backlog" and first.get("may_type"))
            live = await eng.for_server(sid)._call("GET", f"/v1/servers/{sid}")
            check("two viewers in the panel are one socket on the node",
                  live.get("console_followers") == 1, live.get("console_followers"))

            async def read_until(text, seconds=15):
                deadline = time.time() + seconds
                while time.time() < deadline:
                    if any(text in ln for ln in lines):
                        return True
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), 1))
                    except asyncio.TimeoutError:
                        continue
                    lines.extend(msg.get("lines", []))
                return any(text in ln for ln in lines)

            check("the console streams the server's output", await read_until("Done ("),
                  lines[-3:])
            t0 = time.time()
            await ws.send(json.dumps({"type": "command", "command": "say hello"}))
            check("a command typed into the socket reaches the server",
                  await read_until("ran: say hello"), lines[-3:])
            check("...and its output is back through the relay within 300 ms",
                  time.time() - t0 < 0.3, f"{time.time() - t0:.3f}s")
            r = await c.post(f"/api/instances/{sid}/command", json={"command": "join Steve"})
            check("a command sent through the panel reaches it too",
                  r.status_code == 200 and await read_until("Steve joined the game"))
            seen2 = []
            deadline = time.time() + 5
            while time.time() < deadline and not any("Steve joined" in ln for ln in seen2):
                try:
                    seen2 += json.loads(await asyncio.wait_for(ws2.recv(), 1)).get("lines", [])
                except asyncio.TimeoutError:
                    pass
            check("...and the second viewer sees the same lines",
                  any("ran: say hello" in ln for ln in seen2)
                  and any("Steve joined" in ln for ln in seen2))

            # A boot's worth of output, more than the node's 2000-line ring.
            t0 = time.time()
            await ws.send(json.dumps({"type": "command", "command": "spam 2500"}))
            ok = await read_until("spam line 2500", 20)
            took = time.time() - t0
            spam = [ln for ln in lines if "spam line" in ln]
            check("2500 lines in a burst all arrive, in order, none twice",
                  ok and len(spam) == 2500
                  and spam[-1].endswith("spam line 2500") and spam[0].endswith("spam line 1"),
                  f"{len(spam)} lines")
            check("...the last of them within a second of the command", took < 1.0,
                  f"{took:.2f}s")

        for label, headers in (
                ("a page on another site", {"Cookie": cookie, "Origin": "http://evil.example"}),
                ("no session", {"Origin": API})):
            try:
                async with websockets.connect(ws_url, additional_headers=headers) as ws:
                    await ws.recv()
                refused = False
            except Exception:       # noqa: BLE001 -- refused before it was accepted
                refused = True
            check(f"the console socket refuses {label}", refused)

        # --- the polled fallback: only what is new -----------------------------------------
        async with c.stream("GET", f"/api/instances/{sid}/console/stream") as r:
            frames = []
            stream_lines = r.aiter_lines()

            async def frame():
                async for raw in stream_lines:
                    if raw.startswith("data: "):
                        return json.loads(raw[6:])

            frames.append(await asyncio.wait_for(frame(), 10))
            frames.append(await asyncio.wait_for(frame(), 10))
            check("the stream opens with one reset and the node's whole ring",
                  frames[0]["event"] == "reset" and frames[1]["event"] == "lines"
                  and len(frames[1]["lines"]) == 2000
                  and frames[1]["lines"][-1].endswith("spam line 2500"),
                  len(frames[1].get("lines") or []))
            await c.post(f"/api/instances/{sid}/command", json={"command": "say polled"})
            got = []
            deadline = time.time() + 5
            while time.time() < deadline and not any("ran: say polled" in ln for ln in got):
                f = await asyncio.wait_for(frame(), 5)
                if f["event"] == "reset":
                    break
                got += f.get("lines") or []
            check("...then only the lines after the last one it sent, with the ring full",
                  any("ran: say polled" in ln for ln in got) and len(got) <= 3, got)
        stats = (await c.get(f"/api/instances/{sid}/stats")).json()
        check("stats come from the node: running, with the player who joined",
              stats.get("running") and stats.get("online") == 1, stats)

        # --- two servers on one port: one at a time, and a switch -----------------------------
        port = (await eng.get_server(sid))["server_port"]
        twin = await eng.create_server(name="Twin", port=port, memory_mb=1024)
        await eng.upload_file(twin, ".", "fake_server.py", FAKE_SERVER.encode())
        await eng.update_server(twin, {"execution_command":
                                       f'"{sys.executable}" -u fake_server.py'})
        r = await c.post(f"/api/instances/{twin}/action/start_server")
        check("a second server on the same port can be made, and starting it while the "
              "first runs is a 409 naming the first",
              r.status_code == 409 and r.json().get("conflict", {}).get("server") == sid
              and r.json()["conflict"].get("name") == "Agent Test", r.text[:300])
        r = await c.post(f"/api/instances/{twin}/action/switch_server")
        twin_up = await until(lambda: _running(c, twin), 20)
        first_down = not (await c.get(f"/api/instances/{sid}/stats")).json().get("running")
        check("switching stops the first and starts the second", r.status_code == 200
              and twin_up and first_down, r.text[:200])
        r = await c.post(f"/api/instances/{sid}/action/switch_server")
        check("...and back", r.status_code == 200 and await until(lambda: _running(c, sid), 20))
        r = await c.post(f"/api/instances/{twin}/port", json={"auto": True})
        r2 = await c.post(f"/api/instances/{twin}/action/start_server")

        async def moved():
            p = (await c.get(f"/api/instances/{twin}/port")).json()
            return p if p.get("record_port") != port else None
        now = await until(moved, 15)
        check("with Port: automatic it starts beside the first instead, on a free port "
              "the panel's record follows", r.status_code == 200 and r2.status_code == 200
              and now and now["auto"] and now["record_port"] != port
              and str(now["properties_port"]) == str(now["record_port"]), (r2.text[:200], now))
        await c.post(f"/api/instances/{twin}/action/stop_server")
        await until(lambda: _stopped(c, twin), 20)
        await eng.delete_server(twin)

        # --- a token is for one server and one node -----------------------------------------
        other = await eng.create_server(name="Other", port=free_port(), memory_mb=1024)
        from app import nodes as panel_nodes
        token, _exp = await panel_nodes.sign_browser_token(
            user={"id": "1"}, server_uuid=sid, node_id="test-node", perms=["console.read"])
        wrong = f"ws://127.0.0.1:{AGENT_PORT}/v1/servers/{other}/ws?token={token}"
        try:
            async with websockets.connect(wrong) as ws:
                await ws.recv()
            refused = False
        except Exception:       # noqa: BLE001 -- closed before it was accepted
            refused = True
        check("a console token for one server does not open another's", refused)

        # --- edit a file through the panel; it lands on the node --------------------------------
        r = await c.post(f"/api/instances/{sid}/files/write",
                         json={"path": "server.properties",
                               "content": "motd=Edited through BlessForge\n"})
        check("a file edited in the panel is written on the node",
              r.status_code == 200 and (await eng.read_file(sid, "server.properties"))
              .startswith("motd=Edited"), r.text[:200])
        r = await c.post(f"/api/instances/{sid}/files/write",
                         json={"path": "../outside.txt", "content": "x"})
        check("...and a path outside the folder is refused",
              r.status_code == 400 and not (TMP / "node" / "servers" / "outside.txt").exists())

        # --- search runs on the node, in one request -----------------------------------------
        deep = TMP / "node" / "servers" / sid / "libraries" / "a" / "b" / "c"
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "needle-1.0.jar").write_bytes(b"x")
        r = await c.get(f"/api/instances/{sid}/files/search", params={"q": "needle"})
        hits = r.json().get("results") or [] if r.status_code == 200 else []
        check("a file search finds a file deep in the tree, through the node's own search",
              [h["path"] for h in hits] == ["libraries/a/b/c/needle-1.0.jar"]
              and hits[0]["bytes"] == 1, r.text[:200])
        logs = TMP / "node" / "servers" / sid / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "latest.log", "w", encoding="utf-8") as fh:
            fh.write(("[12:00:00] [main/INFO]: " + "x" * 80 + "\n") * 46000)
            fh.write('[12:04:17] [Server thread/INFO]: Done (257.518s)! For help, type "help"\n')
        tail = await eng.console_lines(sid, from_file=True)
        check("a log past the whole-read limit is read from its end, not returned empty",
              len(tail) > 1000 and tail[-1].endswith('type "help"'), len(tail))
        (logs / "latest.log").unlink()
        r = await c.get(f"/api/instances/{sid}/files/usage")
        libs = next((f for f in (r.json().get("folders") or []) if f["name"] == "libraries"), None)
        check("folder sizes are measured on the node too", libs is not None
              and libs["files"] >= 1 and libs["bytes"] >= 1, r.text[:200])

        # --- a crash is noticed and recorded ------------------------------------------------
        await c.post(f"/api/instances/{sid}/command", json={"command": "crash"})

        async def crashed():
            s2 = (await c.get(f"/api/instances/{sid}/stats")).json()
            return s2 if s2.get("crashed") else None
        check("a server that dies on its own is marked crashed", await until(crashed, 15))

        async def audited():
            items = (await c.get("/api/admin/audit?action=agent.crash")).json()["items"]
            return items or None
        check("...and the crash reaches the panel's audit log, from the node",
              await until(audited, 15))

        # --- stop, then delete ------------------------------------------------------------
        await c.post(f"/api/instances/{sid}/action/start_server")
        await until(lambda: _running(c, sid), 15)
        r = await c.post(f"/api/instances/{sid}/action/stop_server")
        stopped = await until(lambda: _stopped(c, sid), 30)
        check("stop is graceful: the server is told to stop and does",
              r.status_code == 200 and stopped is not None)
        log = await eng.console_lines(sid)
        check("...having said so in its own console", any("Stopping the server" in ln for ln in log))
        folder = TMP / "node" / "servers" / sid
        check("the server's folder exists on the node until it is deleted", folder.is_dir())
        r = await c.request("DELETE", f"/api/instances/{sid}")
        check("delete removes it from the node...", r.status_code == 200 and not folder.exists())
        fleet = (await c.get("/api/instances")).json()["items"]
        check("...and from the panel", not any(i["server_id"] == sid for i in fleet))

        # --- the agent reconciles what it is missing -----------------------------------------
        agent.terminate()
        agent.wait(10)
        (TMP / "node" / "state" / "servers" / f"{other}.json").unlink()
        agent = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1",
             "--port", str(AGENT_PORT), "--log-level", "warning"],
            cwd=ROOT, env={**ENV, "BLESSFORGE_AGENT_CONFIG": str(cfg_path)},
            stdout=LOG, stderr=subprocess.STDOUT)
        await wait_http(AGENT + "/health")

        async def restored():
            return (TMP / "node" / "state" / "servers" / f"{other}.json").exists() or None
        check("an agent that lost a server's spec gets it back from the panel on start",
              await until(restored, 20))
        await c.aclose()
        await db.dispose()
    finally:
        for proc in (agent, api):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


async def _running(c, sid):
    s = (await c.get(f"/api/instances/{sid}/stats")).json()
    return s if s.get("running") else None


async def _stopped(c, sid):
    s = (await c.get(f"/api/instances/{sid}/stats")).json()
    return s if not s.get("running") and not s.get("crashed") else None


if __name__ == "__main__":
    ok = asyncio.run(main())
    LOG.close()
    sys.exit(0 if ok else 1)
