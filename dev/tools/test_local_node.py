#!/usr/bin/env python3
"""The one-box install sets its own node up (app/nodes.ensure_local).

  * with ENGINE=agent and the LOCAL_* settings, the panel registers its
    bundled agent once, as node "local", and writes the agent's config where
    the agent reads it (mode 600) -- a config the agent's own loader accepts;
  * a second start changes nothing;
  * a config file that went missing is written again with a new token for the
    same node, not a second node, and the new token is the one that works;
  * with no LOCAL_* settings, or a mock engine, nothing happens;
  * a runtime image that cannot be pulled is built from the Dockerfile the
    agent ships with.

Offline: a temp database (dev/tools/_dbtest.py), a fake Docker API.
"""
import asyncio
import json
import os
import pathlib
import sys
import tarfile
import io

import _dbtest  # noqa: F401 -- before anything from app

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
NODE_DIR = _dbtest.TMP / "node"
os.environ.update({
    "ENGINE": "agent", "BLESSFORGE_SECRET": "local-node-test-secret",
    "LOCAL_AGENT_URL": "http://agent:8443",
    "LOCAL_AGENT_CONFIG": str(NODE_DIR / "agent.toml"),
    "LOCAL_SERVERS_ROOT": "/DATA/AppData/blessforge/servers",
    "LOCAL_PANEL_URL": "http://blessforge:8710",
})

import httpx  # noqa: E402

from agent import config as agent_config  # noqa: E402
from agent.containers import DockerRuntime  # noqa: E402
from app import config, nodes  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


async def main():
    await _dbtest.fresh()
    path = pathlib.Path(config.LOCAL_AGENT_CONFIG)

    made = await nodes.ensure_local()
    rows = await nodes.all_rows()
    check("the first start registers the bundled agent as one node",
          made and [r.id for r in rows] == ["local"]
          and rows[0].fqdn == "agent" and rows[0].agent_port == 8443
          and rows[0].scheme == "http", [(r.id, r.fqdn) for r in rows])
    cfg = agent_config.load(path)
    check("...and writes a config the agent's own loader accepts",
          cfg.node_id == "local" and cfg.panel_url == "http://blessforge:8710"
          and str(cfg.servers_root).replace("\\", "/") == "/DATA/AppData/blessforge/servers"
          and cfg.listen_port == 8443 and cfg.runtime == "docker"
          and cfg.game_bind_ip == "0.0.0.0", (cfg.node_id, cfg.panel_url, cfg.listen_port))
    if os.name != "nt":
        check("...readable by its owner only", oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
    node = await nodes.verify_agent(f"Bearer {cfg.token_id}.{cfg.token}")
    check("...holding a token the panel accepts from the agent", node.id == "local")

    check("a second start changes nothing", not await nodes.ensure_local()
          and agent_config.load(path).token == cfg.token)

    path.unlink()
    again = await nodes.ensure_local()
    fresh_cfg = agent_config.load(path)
    rows = await nodes.all_rows()
    check("a lost config is written again for the same node, with a new token",
          again and [r.id for r in rows] == ["local"] and fresh_cfg.token != cfg.token)
    ok_new = (await nodes.verify_agent(f"Bearer {fresh_cfg.token_id}.{fresh_cfg.token}")).id
    try:
        await nodes.verify_agent(f"Bearer {cfg.token_id}.{cfg.token}")
        old_refused = False
    except nodes.NodeError:
        old_refused = True
    check("...the new token works and the old one no longer does",
          ok_new == "local" and old_refused)

    config.LOCAL_AGENT_URL = ""
    check("with no bundled agent configured, nothing is registered",
          not await nodes.ensure_local())
    config.LOCAL_AGENT_URL, config.ENGINE = "http://agent:8443", "mock"
    path.unlink()
    check("...nor on the mock engine", not await nodes.ensure_local() and not path.exists())
    config.ENGINE = "agent"

    # --- a runtime image that cannot be pulled is built here -------------------------
    seen = {}

    def docker(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/json") and "/images/" in p:
            return httpx.Response(404, json={"message": "no such image"})
        if p.endswith("/images/create"):
            return httpx.Response(404, json={"message": "pull access denied"})
        if p.endswith("/build"):
            seen["params"] = dict(request.url.params)
            with tarfile.open(fileobj=io.BytesIO(request.content)) as tar:
                seen["files"] = sorted(tar.getnames())
            return httpx.Response(200, text='{"stream":"Successfully built"}\n')
        return httpx.Response(500)

    rt = DockerRuntime.__new__(DockerRuntime)
    async with httpx.AsyncClient(transport=httpx.MockTransport(docker),
                                 base_url="http://docker") as c:
        await rt._ensure_image(c, "ghcr.io/chargeyxd/blessforge-runtime:java17")
    check("an image that cannot be pulled is built from the agent's own Dockerfile",
          seen.get("params", {}).get("t") == "ghcr.io/chargeyxd/blessforge-runtime:java17"
          and json.loads(seen["params"]["buildargs"]) == {"JAVA": "17"}
          and {"Dockerfile", "bf-entry"} <= set(seen.get("files", [])), seen)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
