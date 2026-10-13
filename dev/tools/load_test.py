#!/usr/bin/env python3
"""A node running many servers at once, with heavy file work alongside.

design/AGENT-REWORK-BRIEF.md item 7.9: the proof that nothing on a busy node
stalls. A real agent (its own process) gets N servers, each a tiny stand-in
that prints a time-stamped line every 100 ms and answers stdin. They are
started together, so they go through the boot queue; a console websocket is
kept open on every one; and while they run, a backup-sized folder is
compressed and then extracted on one of them. Throughout:

  * every console line arrives within 500 ms of being printed;
  * GET /v1/stats answers in under 200 ms;
  * the agent's own loop-lag probe (/v1/system loop_lag_ms) never sees a
    stall longer than 100 ms.

    python dev/tools/load_test.py                  # 8 servers
    python dev/tools/load_test.py --servers 10 --seconds 60

The stand-ins run on the agent's process runtime: plain processes, no
isolation, which is fine for a stand-in (the runtime images hold a JRE, not
Python). What is measured -- the agent's event loop, its sockets, its file
pools -- is the same with either runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import jwt  # noqa: E402
import websockets  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

STAND_IN = r'''
import sys, threading, time
def say(m): print("[Server thread/INFO]: " + m, flush=True)
say("Starting minecraft server version load-1.0")
time.sleep(0.5)
say('Done (0.5s)! For help, type "help"')
def tick():
    while True:
        say("tick %.6f" % time.time())
        time.sleep(0.1)
threading.Thread(target=tick, daemon=True).start()
for line in sys.stdin:
    line = line.strip()
    if line == "stop":
        say("Stopping the server")
        sys.exit(0)
    say("ran: " + line)
'''

results: list[bool] = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""),
          flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_backup_folder(folder: pathlib.Path, megabytes: int) -> None:
    """A world-sized folder: many region-like files, half compressible."""
    world = folder / "world" / "region"
    world.mkdir(parents=True, exist_ok=True)
    chunk = os.urandom(512 * 1024) + b"\0" * (512 * 1024)
    for i in range(megabytes):
        (world / f"r.{i}.0.mca").write_bytes(chunk)


async def main(args) -> bool:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bf-load-"))
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    port = free_port()
    cfg = tmp / "agent.toml"
    lines = ["[agent]", 'node_id = "load"', 'token_id = "tid"', 'token = "secret"',
             'panel_url = "http://127.0.0.1:9"',
             f'servers_root = "{(tmp / "servers").as_posix()}"',
             f'state_dir = "{(tmp / "state").as_posix()}"',
             f"listen_port = {port}", f"max_concurrent_boots = {args.boots}"]
    lines += ['runtime = "process"', "allow_unsafe_process_runtime = true"]
    lines.append(f'panel_public_key = """\n{pub.decode().strip()}\n"""')
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log = open(tmp / "agent.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env={**os.environ, "BLESSFORGE_AGENT_CONFIG": str(cfg)},
        stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    c = httpx.AsyncClient(base_url=base, timeout=60,
                          headers={"Authorization": "Bearer tid.secret"})
    sockets = []
    try:
        for _ in range(100):
            try:
                if (await c.get("/health")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        script = tmp / "stand_in.py"
        script.write_text(STAND_IN, encoding="utf-8")
        ids = [f"10ad0000-0000-0000-0000-{i:012d}" for i in range(args.servers)]
        command = f"{sys.executable} -u {script}"
        for i, sid in enumerate(ids):
            r = await c.put(f"/v1/servers/{sid}", json={
                "name": f"Load {i}", "port": 31000 + i, "memory_mb": 512,
                "execution_command": command})
            assert r.status_code == 200, r.text
        make_backup_folder(tmp / "servers" / ids[0], args.backup_mb)
        print(f"agent on {base}; {args.servers} servers; a {args.backup_mb} MB folder "
              f"to compress and extract; {args.seconds}s", flush=True)

        t_start = time.time()
        for sid in ids:
            r = await c.post(f"/v1/servers/{sid}/power", json={"action": "start"})
            assert r.status_code == 200, r.text

        lags: list[float] = []
        stats_ms: list[float] = []
        seen = {sid: 0 for sid in ids}

        def token(sid: str) -> str:
            now = int(time.time())
            return jwt.encode({"sub": "load", "srv": sid, "aud": "load",
                               "perm": ["console.read"], "iat": now, "exp": now + 600},
                              key, algorithm="EdDSA")

        async def follow(sid: str) -> None:
            url = f"ws://127.0.0.1:{port}/v1/servers/{sid}/ws?token={token(sid)}"
            async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
                sockets.append(ws)
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get("type") != "lines":
                        continue        # the backlog is from before we looked
                    now = time.time()
                    for ln in frame["lines"]:
                        if " tick " in ln:
                            lags.append(now - float(ln.rsplit(" ", 1)[1]))
                            seen[sid] += 1

        followers = [asyncio.create_task(follow(sid)) for sid in ids]

        async def poll_stats() -> None:
            while True:
                t0 = time.perf_counter()
                r = await c.get("/v1/stats")
                stats_ms.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200
                await asyncio.sleep(0.5)

        poller = asyncio.create_task(poll_stats())

        await asyncio.sleep(2)
        t0 = time.perf_counter()
        r = await c.post(f"/v1/servers/{ids[0]}/files/compress",
                         json={"paths": ["world"], "dest": "backup.zip"}, timeout=600)
        compress_s = time.perf_counter() - t0
        assert r.status_code == 200, r.text
        shutil.rmtree(tmp / "servers" / ids[0] / "world")
        t0 = time.perf_counter()
        r = await c.post(f"/v1/servers/{ids[0]}/files/extract",
                         json={"path": "backup.zip"}, timeout=600)
        extract_s = time.perf_counter() - t0
        assert r.status_code == 200, r.text
        print(f"compress {compress_s:.1f}s, extract {extract_s:.1f}s, while all ran",
              flush=True)
        remaining = args.seconds - (time.time() - t_start)
        if remaining > 0:
            await asyncio.sleep(remaining)

        system = (await c.get("/v1/system", params={"lag_since": t_start})).json()
        poller.cancel()
        for t in followers:
            t.cancel()
        states = (await c.get("/v1/stats")).json()["servers"]

        up = [sid for sid in ids if states[sid]["state"] == "running"]
        check(f"all {args.servers} servers came up through the boot queue "
              f"({args.boots} at a time)", len(up) == len(ids), f"{len(up)} running")
        check("every console was followed live", all(n > 0 for n in seen.values()),
              min(seen.values()))
        p99 = sorted(lags)[int(len(lags) * 0.99) - 1] if lags else None
        check("console lines arrive within 500 ms of being printed",
              lags and max(lags) < 0.5,
              f"{len(lags)} lines, median {statistics.median(lags) * 1000:.0f} ms, "
              f"p99 {p99 * 1000:.0f} ms, worst {max(lags) * 1000:.0f} ms" if lags else "none")
        check("GET /v1/stats answers in under 200 ms",
              stats_ms and max(stats_ms) < 200,
              f"{len(stats_ms)} calls, median {statistics.median(stats_ms):.0f} ms, "
              f"worst {max(stats_ms):.0f} ms" if stats_ms else "none")
        check("no event loop stall longer than 100 ms (the agent's own probe)",
              system["loop_lag_ms"] < 100, f"worst {system['loop_lag_ms']} ms")
        for sid in ids:
            await c.post(f"/v1/servers/{sid}/power", json={"action": "stop"})
    finally:
        await c.aclose()
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--servers", type=int, default=8)
    p.add_argument("--boots", type=int, default=2, help="max_concurrent_boots")
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--backup-mb", type=int, default=200)
    p.add_argument("--keep", action="store_true", help="leave the temp folder")
    sys.exit(0 if asyncio.run(main(p.parse_args())) else 1)
