"""`python -m agent`: run the node agent from its config file.

Listens where the config says (the node's WireGuard address, in
production), with TLS when a certificate is configured. The systemd unit in
agent/systemd/ runs exactly this.
"""
import uvicorn

from agent.config import load

cfg = load()
uvicorn.run("agent.main:app", host=cfg.listen_host, port=cfg.listen_port,
            ssl_certfile=cfg.tls_cert or None, ssl_keyfile=cfg.tls_key or None,
            proxy_headers=False, server_header=False, log_level="info")
