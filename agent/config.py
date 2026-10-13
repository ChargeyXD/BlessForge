"""The agent's configuration: one TOML file, written by BlessForge.

BlessForge generates this file when a node is registered (POST
/api/admin/nodes, as an admin) and the owner copies it to the node:

    /etc/blessforge-agent/agent.toml     owner root, mode 600

It holds the node's token in the clear -- the agent must present it to the
panel and check it on every request from the panel -- which is why the file
is root-only, and why a leaked one is fixed by re-registering the node
(which mints a new token) rather than by editing it.

Everything except the four identity fields has a default that is right for a
Linux node running Docker.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("BLESSFORGE_AGENT_CONFIG",
                                   "/etc/blessforge-agent/agent.toml"))

# The runtime image per Java major. Built from agent/images/; the tag is the
# Java major so the record's `java_major` picks one directly.
DEFAULT_IMAGES = {
    8: "ghcr.io/chargeyxd/blessforge-runtime:java8",
    17: "ghcr.io/chargeyxd/blessforge-runtime:java17",
    21: "ghcr.io/chargeyxd/blessforge-runtime:java21",
    25: "ghcr.io/chargeyxd/blessforge-runtime:java25",
}


@dataclass
class AgentConfig:
    # --- identity: written by BlessForge at registration --------------------
    node_id: str
    token_id: str
    token: str
    panel_url: str                       # https://api.<brand>
    panel_public_key: str                # Ed25519, PEM: verifies browser tokens

    # --- where it listens ----------------------------------------------------
    listen_host: str = "0.0.0.0"         # the WireGuard address, in production
    listen_port: int = 8443
    tls_cert: str = ""                   # blank: plain HTTP inside the tunnel
    tls_key: str = ""
    verify_panel_tls: bool = True

    # --- where servers live --------------------------------------------------
    servers_root: Path = Path("/srv/blessforge/servers")
    state_dir: Path = Path("/var/lib/blessforge-agent")

    # --- how they run --------------------------------------------------------
    runtime: str = "docker"              # docker | process (development only)
    docker_host: str = "unix:///var/run/docker.sock"
    images: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_IMAGES))
    container_uid: int = 1000
    container_gid: int = 1000
    pids_limit: int = 512
    game_bind_ip: str = "0.0.0.0"        # the WireGuard side: the edge forwards to it
    game_network: str = "bridge"         # never "host"; see containers.assert_safe
    tmp_size_mb: int = 256
    stop_timeout: int = 60               # seconds between "stop" and a kill
    disk_quota: str = "watchdog"         # xfs | ext4 | watchdog | none
    # "hard" (the default): each server's memory is a hard limit, swap equal
    # to it. "soft": a reservation and no hard limit, for a self-hosted box
    # whose owner wants Crafty-like behaviour -- a server may take more than
    # its share when the machine has it, and the kernel decides when it does
    # not. Never on a node that hosts anybody else.
    memory_mode: str = "hard"
    # How many servers boot at once (a slot is held from the container start
    # until "Done", an exit, or boot_timeout seconds); the rest wait their
    # turn. And how many installs -- loader installers, the node's own
    # downloads -- run at once, separately, so neither holds up the other.
    max_concurrent_boots: int = 2
    boot_timeout: int = 900
    max_concurrent_installs: int = 2
    # Starting a server that takes the node's committed memory (the sum of
    # its running servers' limits) past its RAM is warned about by the
    # panel and confirmed by a person -- not refused. A node that sells
    # servers can make it a refusal here.
    refuse_overcommit: bool = False
    # Where the agent pings its servers for who is online. Blank: the game
    # address, or -- when that is every address -- this machine, which for
    # an agent in a container (the one-box install) is host.docker.internal,
    # not 127.0.0.1: inside the container 127.0.0.1 is the container itself
    # and every ping was refused.
    ping_host: str = ""

    def ping_target(self) -> str:
        if self.ping_host:
            return self.ping_host
        if self.game_bind_ip and self.game_bind_ip not in ("0.0.0.0", "::"):
            return self.game_bind_ip
        return "host.docker.internal" if os.path.exists("/.dockerenv") else "127.0.0.1"

    # --- process runtime: development only -------------------------------------
    # Runs the server as a plain process with no isolation at all. Refused
    # unless explicitly allowed, and never on a node that sells servers.
    allow_unsafe_process_runtime: bool = False
    java_paths: dict[int, str] = field(default_factory=dict)

    def image_for(self, java_major: int | None) -> str:
        major = int(java_major or 21)
        if major in self.images:
            return self.images[major]
        newer = sorted(m for m in self.images if m >= major)
        return self.images[newer[0] if newer else max(self.images)]

    def server_dir(self, uuid: str) -> Path:
        return self.servers_root / uuid


def load(path: Path | str | None = None) -> AgentConfig:
    p = Path(path or DEFAULT_PATH)
    with open(p, "rb") as fh:
        raw = tomllib.load(fh)
    agent = dict(raw.get("agent") or raw)
    for key in ("servers_root", "state_dir"):
        if key in agent:
            agent[key] = Path(agent[key])
    if "images" in agent:
        agent["images"] = {int(k): v for k, v in agent["images"].items()}
    if "java_paths" in agent:
        agent["java_paths"] = {int(k): v for k, v in agent["java_paths"].items()}
    known = set(AgentConfig.__dataclass_fields__)
    unknown = set(agent) - known
    if unknown:
        raise ValueError(f"unknown settings in {p}: {', '.join(sorted(unknown))}")
    cfg = AgentConfig(**agent)
    if cfg.runtime not in ("docker", "process"):
        raise ValueError(f"runtime must be docker or process, not {cfg.runtime!r}")
    if cfg.memory_mode not in ("hard", "soft"):
        raise ValueError(f"memory_mode must be hard or soft, not {cfg.memory_mode!r}")
    if cfg.runtime == "process" and not cfg.allow_unsafe_process_runtime:
        raise ValueError(
            "runtime = \"process\" runs servers with NO isolation and is for "
            "development only. Set allow_unsafe_process_runtime = true to "
            "accept that, and never on a node that hosts anybody else.")
    return cfg
