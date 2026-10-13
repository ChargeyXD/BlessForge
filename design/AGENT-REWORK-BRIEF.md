# BlessForge: node agent rework brief

The work, in order, for whoever picks this up (written for a cloud session on
`github.com/ChargeyXD/BlessForge`, branch `main`). Line numbers are
approximate; search for the names given.

## Before you start

- **There is no access to the owner's server** (it's on a home LAN). Test with
  Docker in your own sandbox if it has Docker. If it doesn't, say so and rely
  on the unit suites. The one-box `docker-compose.yml` runs as-is with
  `docker compose up -d --wait` (that's how CasaOS installs it, and a container
  with no healthcheck fails that wait).
- **CurseForge tests are mandatory** for anything that touches installs
  (`dev/tools/install_on_agent.py`, `dev/tools/pack_matrix.py --docker`); Modrinth
  is secondary. They need `CURSEFORGE_API_KEY` in the environment. If it isn't
  set, say so in your report rather than skipping silently. Never print a key
  and never commit one.
- `docker-compose.yml` carries the owner's own key in `CURSEFORGE_API_KEY_B64`
  (panel and worker), on purpose for now, and
  `dev/tools/test_blocked_downloads.py` has one check that fails until it's
  blanked. Leave both alone.
- Suites to keep green: `test_agent_security.py` (197 checks, plus `--live`
  where Docker is available), `test_agent_api.py`, `test_local_node.py`,
  `test_engine.py`, `test_repair.py`, `test_perf.py`,
  `test_blocked_downloads.py` (apart from the key check), and
  `python dev/tools/check_frontend.py` after any change under `app/static/`.
- `agent/containers.py: assert_safe()` is the security boundary for game
  containers: no host network, `Memory == MemorySwap`, CPU and PID limits, one
  bind mount, no root, ports only on `game_bind_ip`, same number on both sides,
  never below 1024. Keep it refusing all of that. Where a feature below needs a
  rule relaxed, make it an explicit agent setting, off by default, with tests
  both ways.
- One-box layout: the panel (`blessforge`), the `worker`, the `agent`,
  `postgres` and `redis` share one compose network. Game servers are separate
  containers the agent creates on Docker's default bridge, publishing their
  ports on the host. The agent's stored address is `http://agent:8443`, a name
  that only resolves inside the compose network (`app/nodes.py: ensure_local`).
- UI work must follow the existing theme and `design/UI-REWORK-BRIEF.md`; there
  is a separate UI rework coming, so keep new controls small and functional.

---

## 0. The database password is written three times

`POSTGRES_PASSWORD` on the `postgres` service and the password embedded in
`DATABASE_URL` on `blessforge` and on `worker` all have to match. In the CasaOS
dialog the two inside a URL are easy to miss, and Postgres keeps whatever it
was first created with, so a changed password crash-loops the panel and the
worker with `password authentication failed`.

**Fix:**
- `app/config.py`: read `POSTGRES_PASSWORD`. When it's set and `DATABASE_URL` is
  a postgres URL, replace the URL's password with it, URL-encoded with
  `urllib.parse.quote(pw, safe='')`.
- `docker-compose.yml`: `DATABASE_URL: "postgresql+asyncpg://blessforge@postgres:5432/blessforge"`
  (no password), plus a `POSTGRES_PASSWORD` field on `blessforge`, `worker` and
  `postgres`. Add an `x-casaos` `envs` entry, and update both
  `tips.before_install` texts to say "the same value on all three".
- `app/db/__init__.py: init()`: when `migrate()` fails with "password
  authentication failed", log one plain sentence saying the database keeps the
  password it was first created with, and naming the three fields to make
  match.
- `README.md` configuration table: add a `POSTGRES_PASSWORD` row.
- This was prototyped once: the stack came up healthy with the password
  `p@ss w:rd` on all three services.

---

## 1. Heap: the user decides, as before the commercial rework

**The rule:** the heap a person types is the heap the server gets. That's how
it worked before the commercial rework (WP-01..06). BlessForge advises, warns
and sizes everything around that number, but never clamps it, shrinks it or
refuses it.

**What's wrong today:**
- The container's memory limit (`memory_mb`, enforced by the agent as
  `Memory == MemorySwap`) is set **once**, at install time (`app/installer.py`
  around lines 895 and 1080: `specs.container_memory_mb(heap, mod_count)`).
  **Nothing resizes it afterwards.** Tune's heap change
  (`app/optimizer.py: set_command_memory`, `app/perf.py: apply` with
  `set_heap_mb`) only rewrites `-Xmx`. There's no UI or API anywhere to set
  `memory_mb`. A bigger heap then gets OOM-killed, or `perf.check_jvm` flags it
  "does not fit the container".
- **On an OOM kill, repair shrinks the heap to fit the container**
  (`app/repair.py: _fit_memory`). For an extra-large pack that guarantees a
  heap too small to load. Its message points the user to Tune, which can't
  change the container.
- **The ceiling is the host's free memory at that moment**
  (`app/specs.py: recommend_memory`: `room = min(total - reserve, available - 0.5)`,
  minus overhead), and `app/optimizer.py: _clamp_heap` refuses anything above
  it. On an 11 GB box running other apps, a pack that asks for 10 GB gets about
  3 GB.

**What to do:**
1. **One helper, `set_heap(server_id, heap_mb, *, by_user: bool)`**, used by
   every path that sets a heap: installer, Tune, optimizer, `perf.apply`,
   repair. It writes `-Xmx`/`-Xms` (user_jvm_args.txt or the command) **and**
   `memory_mb = specs.container_memory_mb(heap_gb, mod_count)` together, so the
   container always follows the heap. Store `heap_set_by_user` on the record.
2. **No clamping of user input.** The install form and Tune accept any heap
   from 512 MB up to the host's total RAM. Above about 85% of the total, show a
   clear warning ("this machine has 11 GB; other apps use about 6"), never a
   silent change. `_clamp_heap` and `recommend_memory` stay as the
   **suggestion** for the default value; they are not a limit.
3. **Installs use the pack's recommended RAM** (or what the user typed) by
   default. Show the "currently free" figure as a warning beside it, not as a
   cap.
4. **Repair never lowers a heap the user set.** On an OOM kill it **grows the
   container** (heap plus measured overhead, one step larger: +25% overhead, at
   least +1 GB), up to the host's total. If the host is already at its total,
   report it and stop; don't touch the heap. Only a heap BlessForge chose itself
   (`heap_set_by_user` false) may be lowered, and the log says so.
5. **Tune gets a Memory control:** the heap, the resulting container limit and
   the overhead, saved through the helper.
6. **A self-hosted escape hatch**, as an agent setting (`agent.toml`, off by
   default): `memory_mode = "soft"` sets `MemoryReservation` and no hard limit,
   for people who want Crafty-like behaviour. `assert_safe` refuses it unless
   the setting is on. Hosted nodes never turn it on.

**Done when:** a 400+ mod pack with a 10 GB heap typed in on a 16 GB node
installs with that heap and a container sized for it, and boots. Changing the
heap in Tune resizes the container. An OOM on first boot grows the container,
and the user's heap is unchanged. Tests in `test_repair.py`, `test_perf.py` and
`test_engine.py` cover each of these.

---

## 2. The console doesn't update live; it only refreshes on page reload

**Confirmed defects (read from the code):**

1. **The browser is sent to an address it can't reach.**
   `GET /api/instances/{id}/console/socket` (`app/main.py` around line 1378)
   returns `drv.socket_url(server_id, token, drv.url)`
   (`app/engine/agent.py: socket_url`). In the one-box install `drv.url` is
   `http://agent:8443`, which only resolves inside Docker, and port 8443 isn't
   published either.
2. **Over HTTPS (the owner uses a Cloudflare tunnel) it breaks outright.**
   `new WebSocket('ws://…')` from an `https:` page throws a `SecurityError`
   synchronously inside the Promise executor in
   `app/static/js/views/tab-console.js: connectSocket()`. The promise rejects,
   `connect()` is called with no `catch`, and the fallback `EventSource` stream
   is never opened. The page shows its first snapshot and nothing after.
3. **The polled fallback degrades once the buffer is full.** The agent keeps a
   2000-line ring (`agent/servers.py: CONSOLE_LINES`), and the panel asks for
   2000 lines every 0.2 s (`app/engine/agent.py: console_lines`). Once full, the
   window slides on every new line, so `app/main.py: console_stream` finds no
   common prefix and sends `reset` plus all 2000 lines on every poll: about
   200 KB per poll per viewer, and a full DOM rebuild.
4. Every five seconds the fallback loop also waits on `engine.get_stats()`,
   which costs a 1–2 s Docker stats call (see 6.2), so output arrives in
   clumps.

**What to do:**
- **Proxy the console through the panel:** `WS /api/instances/{id}/console/ws`
  on the panel opens the agent's websocket server-side and relays frames both
  ways, enforcing `console.read` and `console.command` with the panel's own
  auth. The browser only ever talks to the panel's origin, so `wss://` works
  behind any proxy. Share **one upstream agent socket per server** across all
  browser viewers (fan-out in the panel). Keep direct-to-node only for nodes
  with a configured public URL.
- In `tab-console.js`, wrap `connectSocket()` in try/catch, treat a thrown
  constructor as a failure, and always fall back to the stream.
- **Number the console lines:** each line in the agent gets a monotonically
  increasing sequence number, with
  `GET /v1/servers/{id}/console?after=<seq>` returning only newer lines plus
  the current sequence. The fallback stream sends only new lines; `reset` only
  when the requested sequence has fallen out of the ring.
- Take stats out of the console loop: separate events driven by the cached
  stats from 6.2.

**Done when:** a console opened over `https://` through a reverse proxy shows
each new line within about 300 ms during a 2000+ line boot, and panel↔agent
traffic per viewer is proportional to the new output.

---

## 3. Modpack and CurseForge export downloads

### 3a. The node downloads the mods itself

Today every install runs: the worker downloads each mod into `/data/cache` →
re-zips them into 64 MB batches → uploads each batch over HTTP to the agent →
the agent extracts it **synchronously on its event loop** → the zip is deleted
(`app/installer.py` around lines 700–735; `agent/main.py: file_op` "extract").
Every byte is written three times, and a big extract freezes the whole node:
consoles, stats and every other server.

The agent can already download one URL with a hash check
(`agent/servers.py: _download`, for loader installers). Extend it:
- A new install step, `fetch_many`: a list of `{url, path, sha1, size}`. The
  agent downloads them in parallel (default concurrency 8, one shared
  `httpx.AsyncClient`) straight into the server folder through the file jail.
  It verifies SHA-1 and size before renaming into place, retries with backoff,
  and streams progress lines back.
- **A node-level download cache keyed by SHA-1**, so two servers with the same
  pack, or a reinstall, download nothing twice. Copy from the cache into the
  server folder (or reflink where the filesystem supports it), **not a hard
  link**: the jail refuses files with `st_nlink > 1` (`agent/files.py: open_file`).
- A host allowlist on the agent for the CDNs the URLs use (CurseForge and
  Modrinth), so a compromised panel can't point a node anywhere else.
- Keep the panel's batch path only for files the node can't fetch: uploads,
  hand-downloaded jars and overrides from the pack zip. Move the agent's
  `extract` off the event loop (`asyncio.to_thread`).

### 3b. Mods whose authors turned off third-party distribution

Not in scope: rebuilding forgecdn addresses for files that have no
`downloadUrl` (it was removed on purpose in `1bc0ebb`; see the docstrings of
`app/curseforge.py` and `app/blocked.py`). What *is* in scope is making the
user's own download almost free:
- After resolving a pack, show one "N mods need a one-time download" panel with
  each file's CurseForge page link and an "Open all" button (tabs in batches of
  about 5), plus the expected file name.
- A drop zone that takes many files or a whole folder, matches each jar to its
  slot by SHA-1 / CurseForge fingerprint (the content matching from `6f15278`),
  files them into the right server, and resumes the install automatically when
  the last one lands.
- Matched jars go into the node cache from 3a, so no second server or
  reinstall ever asks again.
- Widen the Modrinth substitution (by SHA-1, then by slug) that already exists
  in `app/blocked.py`.
- If the pack has an official **server pack** file that's downloadable through
  the API, prefer it: the pack author chose to distribute those contents.

---

## 4. Ports held by Crafty: use whatever is free right now

**Facts:**
- Crafty's compose publishes the whole range `25500-25600` (and 8123,
  19132/udp, 8443). Docker holds every one of those host ports for Crafty's
  container whether or not a Crafty server uses it. No other container can
  publish them while Crafty runs; that can't be "ignored" from BlessForge. The
  owner plans to remove Crafty.
- BlessForge's default `GAME_PORT_RANGE` is the same `25500-25600`, and
  `dev/tools/import_from_crafty.py` keeps each server's Crafty port.
- Since `8e74228`, a start whose game port is taken fails with a plain 409, and
  an add-on port that's taken is dropped with a console line
  (`agent/containers.py: DockerRuntime.start`, `port_in_use`).

**What to do:**
- **An agent endpoint for ports in use:** `GET /v1/ports` lists host ports
  published by any container (Docker API `/containers/json` → `Ports`; that
  covers Crafty and other apps) plus this node's running BlessForge servers.
  Cover host processes that aren't containers by trying the start and reading
  Docker's error, as `port_in_use` already does.
- **Choose a port at start, not at create.** A per-server setting "Port:
  automatic": at each start the agent uses the last port if it's free,
  otherwise the first free port in `GAME_PORT_RANGE`. It writes it into
  `server.properties` through the jail, emits `port_changed`, and the panel
  updates the record and shows "running on 25507". For a fixed port that's
  taken, the 409 offers "use the next free port (25507)".
- `app/properties.py: set_port`, the Tune port control and the `create.js`
  validation treat other BlessForge servers' ports as information (see 5), and
  show `/v1/ports` entries as "taken by another app on this machine".
- `import_from_crafty.py`: an option to give imported servers automatic ports.

---

## 5. Several servers on one port, switched between

Several servers share one port (one public address); one runs at a time, and
there's a way to switch.

**What blocks it today:** `agent/servers.py: put()` refuses a spec whose port
another server on the node has (409 "already used on this node"), whether
that server runs or not. `app/properties.py: set_port` refuses unless
`force=True`. `create.js` refuses ports in `usedPorts`.

**What to do:**
- Drop the uniqueness check at `put()`, `set_port` and in the create form.
  Show "also used by X (only one can run at a time)" instead.
- **Check at start:** `_start` refuses (409) when another server on this node
  is running with the same game port or an overlapping add-on port, and names
  it: `{"conflict": {"server": uuid, "name": ..., "port": ...}}`.
- **A switch action:** `POST /v1/servers/{id}/power {"action": "switch"}`
  gracefully stops whichever server holds the port (the normal `stop` with its
  save timeout), waits for it to exit, then starts this one. The UI gets a
  "Switch to this server" button wherever the conflict appears, and a port
  group line on the fleet page ("Port 25565: Survival ● running, Creative ○").
- Only one auto-start server per port: enforced on save, and on boot only that
  one starts.
- Status comes from the agent's per-server state, never from a ping of the
  port. `_reconcile_players` only pings for the running server, and that's
  correct.
- Tests: two specs on one port can be saved. Starting B while A runs gives a
  409 with the conflict. Switching stops A through its stop path and starts B.
  Two auto-start servers on one port are refused.

---

## 6. Agent inefficiencies

Ordered by impact.

1. **Blocking file operations on the agent's event loop.** In `agent/main.py`,
   `file_op` (copy, delete, extract, compress, move), `list_files`,
   `read_content` and `write_content` call the synchronous `files.*` functions
   inside `async def` handlers. Only `usage` and `search` use
   `asyncio.to_thread`. Extracting a batch, deleting a world or compressing a
   backup freezes every console, stats call and event flush on the node.
   **Fix:** every `files.*` call goes to a thread, on a small dedicated
   executor so a big compress can't starve the rest.
2. **Docker stats cost 1–2 s per call.** `DockerRunning.stats()` calls
   `/containers/{name}/stats?stream=false` without `one-shot=true`, on every
   `GET /v1/servers/{id}`, which the fleet page makes per server per poll.
   **Fix:** a background sampler in the agent keeps the latest CPU and memory
   per running server; `GET /v1/servers/{id}` returns the cached values; and
   a new `GET /v1/stats` returns all servers in one call for the fleet view.
3. **A new connection for every call.** `agent/containers.py: _docker_client()`
   builds a new `httpx.AsyncClient` per Docker call. The panel's
   `AgentEngine._call()` does the same towards the agent: a new TCP connection,
   plus a TLS handshake in production, per request, including five a second per
   console viewer. `agent/events.py: flush` does it too. **Fix:** one
   long-lived client per target, closed on shutdown.
4. **Player pings go to the wrong place in the one-box install.**
   `_reconcile_players` pings `127.0.0.1` when `game_bind_ip` is `0.0.0.0`, but
   in the compose the agent is a container, so every ping is refused. **Fix:**
   a `ping_host` agent setting; in container mode use `host.docker.internal`
   (add `extra_hosts: host-gateway` to the agent service) or the game
   container's bridge IP.
5. **The console path** (see 2).
6. **Installs** (see 3a).
7. **The disk watchdog walks every folder on the event loop.**
   `agent/servers.py: check_disks` runs `files.usage()` (a full tree walk)
   synchronously every 60 s for each running server with a disk limit.
   **Fix:** run it in a thread, cache sizes between passes, and use project
   quotas where the filesystem has them.
8. **Runtime images aren't published.** `.github/workflows/docker-publish.yml`
   builds only the root image. On a fresh machine, the first start of each Java
   version builds `blessforge-runtime:javaN` locally (`_ensure_image` →
   `_build_runtime`), which takes minutes, holds the server's lock, and shows
   nothing in the UI. **Fix:** a matrix job that pushes `java8/17/21/25`,
   multi-arch. The agent pre-pulls the versions its servers need in the
   background at startup, and the console says "preparing Java 21 (first time
   only)".
9. **The agent runs from the full panel image** under a 256 MB limit, while
   `agent/requirements.txt` lists six packages. **Fix:** a slim
   `blessforge-agent` image (multi-stage, `agent/` plus its requirements), used
   by the compose file's `agent` service.
10. Cache `_ensure_image` per process after the first success.
11. Run `agent/addons.py: detect` in a thread once item 1 moves file I/O off
    the loop.

---

## 7. Many servers at once: the node has to multitask

The target: a node running 5–10 servers, several of them large modpacks, with
installs, backups and file operations going on alongside, and nothing on the
node (console, stats, other servers) stalling while one of them is busy.

1. **Re-adopt running servers when the agent restarts.** This is a bug today.
   On startup the agent's reconcile (`agent/main.py`, the loop that calls
   `sup.power(..., "start")` for auto-start servers) knows nothing about game
   containers that are still running. For an auto-start server, `start` runs
   `_recreate`, which **force-deletes the running container**: the server is
   killed without saving. Servers that aren't auto-start keep running with no
   console and show as offline. An agent update (or a CasaOS app update) is
   enough to trigger this. **Fix:** at startup, list containers labelled
   `io.blessforge.node=<id>`. For each running one, re-attach (attach stream for
   output, stdin for commands), rebuild `DockerRunning`, set the state to
   running, and seed the console from `docker logs --tail 2000`. Never
   force-delete a running container in `start`; refuse with "already running"
   instead.
2. **A start queue with staggered boots.** Five modded JVMs booting at once
   saturate CPU and disk on a small box, and all of them boot slower than they
   would one after another. Add a node-level semaphore,
   `max_concurrent_boots` (default 2, configurable), held from container
   start until the server reports "Done" or times out. Auto-start on agent boot
   goes through the same queue in a stable order, and the console says "waiting
   to start (2 servers booting)".
3. **Install and job concurrency per node.** Installs, backups and loader
   installer containers go through their own semaphores on the agent
   (`max_concurrent_installs`, default 2), separate from boots, so an install
   never blocks a start and vice versa. Panel-side, `WORKER_CONCURRENCY` stays
   the cross-node limit.
4. **CPU: shares, not reservations.** Today each server's default is a hard
   quota of all cores (`limits_of`: `cpu_pct = cores × 100`), which is fine.
   Add `CpuShares`/cpu.weight so that under contention a busy server can't
   starve the others, and an optional per-server `cpuset` for pinning a big pack
   to specific cores. `BlkioWeight` already exists; keep it.
5. **Memory accounting without refusing.** With heap freedom (item 1), the
   node view shows **committed memory** (the sum of running containers' limits)
   against the host total. Starting a server that pushes it over the total
   gives a warning and a confirmation, not a refusal (hosted nodes may refuse,
   behind a setting).
6. **The panel stays fast with many servers and nodes.** The fleet page makes
   one `GET /v1/stats` per node (6.2) instead of two calls per server. Every
   per-node call gets a short timeout (2–3 s) and falls back to the last known
   state, with a "node not answering" badge, so one dead node can't hold the
   page for the current 15 s per server (`app/engine/agent.py: get_stats`,
   `timeout=15`). Panel loops over nodes run concurrently with a limit,
   never sequentially.
7. **Event and console fan-out.** One upstream agent websocket per server in
   the panel, shared by all viewers (item 2). Agent events stay batched
   (`agent/events.py`), and a slow panel never blocks the agent: the queues
   are bounded, and drops are counted and reported.
8. **Download dedupe across servers** (3a): one node cache by SHA-1, so ten
   servers on the same pack cost one download.
9. **A load test to prove it:** a script under `dev/tools/` that creates N
   servers on a test node with a tiny "server" command (e.g. a loop that prints
   a line every 100 ms and reads stdin), starts them through the queue, keeps a
   console open on each, runs an extract and a compress at the same time, and
   asserts: the console lag stays under 500 ms, `GET /v1/stats` answers in
   under 200 ms, and no event loop stall is longer than 100 ms. Measure the
   stalls with a loop-lag probe task in the agent, exposed on `/v1/system`.

---

## Suggested order

0 → 6.1, 6.2, 6.3 → 7.1 (it kills servers today) → 2 → 1 → 5, then 4 (they share
the start-time conflict check) → 7.2–7.6 → 3a and 7.8 → 3b → 6.4, 6.7–6.11 →
7.9.

Each item lands as its own commit with its tests. Run the suites listed at the
top before each push.
