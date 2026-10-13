# agent/ — the BlessForge node agent

One agent runs on every machine that hosts game servers. BlessForge (the
panel) never touches a node's disk or Docker itself: it tells the agent what
should exist, and the agent makes it so and reports back.

```
browser ──https──▶ panel (BlessForge API) ──node token──▶ agent /v1 ──▶ Docker
   │                    ▲                                    │
   └──── console websocket, file transfers (panel-signed) ───┘
                        └──── /api/agent/* (node token) ◀────┘  events, desired state
```

## The three credentials

| who → whom | credential | checked in |
|---|---|---|
| panel → agent | the **node token**, `Bearer <token_id>.<token>`, minted at registration, stored encrypted (Fernet, keyed from `BLESSFORGE_SECRET`) | `agent/auth.py: check_node_token`, constant time |
| agent → panel | the same node token | `app/nodes.py: verify_agent` via `app/agent_api.py`; the routes are `NODE` in `app/policy.py`, so a browser session never opens them |
| browser → agent | a **five-minute Ed25519 token** the panel signs, naming the user, the server, the node, the permission and (for a transfer) the one file | `agent/auth.py: BrowserTokens.verify`; `alg` pinned to EdDSA, lifetime capped at 600 s |

The agent holds only the panel's **public** key, so a node that is broken
into cannot mint a token for any other node or server.

## Isolation (agent/containers.py)

Every server is one container built by `container_spec()`, and
`assert_safe()` refuses any create body that breaks a rule — it runs before
every create, so a bug in the builder becomes a refused start rather than an
escape. The rules: no Docker socket, not privileged, no added capabilities
and `CapDrop: ALL`, `no-new-privileges`, no `*=unconfined`, never the host's
network/PID/IPC/UTS/user/cgroup namespace, `Memory == MemorySwap`, a CPU
quota, a PID limit, a read-only root filesystem with only a noexec `/tmp`,
uid 1000, exactly one bind mount (the server's own folder at `/server`),
the game port published on the node's game address only, and no Docker
restart policy (the agent restarts servers itself, so it sees crash loops).
The JVM's temp folder is `/server/.blessforge-tmp` (`JAVA_TOOL_OPTIONS`),
emptied at each start: JNA, Netty and friends unpack native libraries
into the temp folder and load them, which the noexec `/tmp` refuses.

Every file operation goes through `agent/files.py`, jailed to the server
folder: on Linux each path component is opened `O_NOFOLLOW` relative to its
already-open parent, so there is no check-then-use gap to race; links and
hard links are refused; archives are checked member by member before
anything is extracted.

## Running one on a dev box (no Docker)

The **process runtime** runs a server as a plain child process with **no
isolation at all**. It exists so the whole chain can be exercised on a
laptop; the config loader refuses it unless `allow_unsafe_process_runtime`
is set. Never on a node that hosts anybody else.

```bash
# 1. the panel, on the agent engine
ENGINE=agent BLESSFORGE_SECRET=dev-only PUBLIC_API_URL=http://127.0.0.1:8710 \
  python -m uvicorn app.main:app --port 8710

# 2. register a node (as an admin) -- the answer holds its config, once
curl -b cookies -X POST http://127.0.0.1:8710/api/admin/nodes \
  -H 'content-type: application/json' -d '{
    "name": "Laptop", "fqdn": "127.0.0.1", "agent_port": 8443, "scheme": "http",
    "runtime": "process",
    "agent": {"allow_unsafe_process_runtime": true, "listen_port": 8443,
              "servers_root": "./.agent/servers", "state_dir": "./.agent/state"}}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["config"])' > agent.toml

# 3. the agent
BLESSFORGE_AGENT_CONFIG=agent.toml python -m agent
```

`agent.toml` holds the node token in the clear; it and `.agent/` are
gitignored. A leaked one is fixed by registering the node again, which mints
a new token.

## On a real node

Linux, Docker, WireGuard to the control plane. `agent/systemd/` has the unit
(with the install steps in its header) and `agent/images/` the runtime images:
one per Java major — 8, 17, 21 and 25 (Minecraft 26.x requires Java 25) —
published by CI as `ghcr.io/chargeyxd/blessforge-runtime:javaN`. The agent
pulls the ones its servers use in the background when it starts, and builds
one itself from `agent/images/` when it cannot pull it (the server's console
says "Preparing Java 21 (first time only)" while a start waits on that);
`agent/images/build.sh` builds all four.

## Tests

```bash
python dev/tools/test_agent_security.py          # offline: jail, archives, tokens, spec
python dev/tools/test_agent_security.py --live   # + real containers (Linux + Docker)
python dev/tools/test_agent_api.py               # panel + agent end to end (process runtime)
```

`--live` starts containers that try to get out — a memory hog, a fork bomb,
a disk filler, a read of a neighbour's folder, a write to the root
filesystem, a look for the Docker socket — and checks each is contained.
It needs the runtime images (`BF_TEST_IMAGES='{"21": "repo:tag"}'` to point it
at others). It passed 189/189 on a Debian 12 box with Docker 28 (2026-10-12),
along with `boot_templates.py --docker` 8/8; see NEXT-SESSION.md §0.3.
