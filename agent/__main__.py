"""`python -m agent`: run the node agent from its config file.

Listens where the config says (the node's WireGuard address, in
production), with TLS when a certificate is configured. The systemd unit in
agent/systemd/ runs exactly this.

Bundled with the panel (the one-box docker-compose.yml), the config does not
exist until the panel has registered this agent on its first start, so with
BLESSFORGE_AGENT_WAIT set it waits for the file instead of failing.
"""
import os
import time

import uvicorn

from agent.config import DEFAULT_PATH, load

if os.environ.get("BLESSFORGE_AGENT_WAIT"):
    waited = 0
    while not DEFAULT_PATH.exists():
        if waited % 30 == 0:
            print(f"waiting for the panel to write {DEFAULT_PATH} ...", flush=True)
        time.sleep(2)
        waited += 2

cfg = load()
uvicorn.run("agent.main:app", host=cfg.listen_host, port=cfg.listen_port,
            ssl_certfile=cfg.tls_cert or None, ssl_keyfile=cfg.tls_key or None,
            proxy_headers=False, server_header=False, log_level="info")
