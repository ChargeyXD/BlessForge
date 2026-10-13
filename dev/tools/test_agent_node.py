#!/usr/bin/env python3
"""The node agent as a busy machine: nothing on it stalls while one thing works.

What design/AGENT-REWORK-BRIEF.md items 6 and 7 need to be true:

  * file work (copy, delete, extract, compress, a tree walk) runs off the
    event loop, and quick work never queues behind heavy work (6.1);

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
        check("the event loop never stalled for anything like a compress (1 s)",
              probe.worst < 0.3, f"worst {probe.worst * 1000:.0f} ms")
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


async def main():
    await test_file_ops_off_loop()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
