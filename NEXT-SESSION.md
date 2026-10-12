# BlessForge — the state of the code, for the next session

Updated 2026-10-12 for the commercial rebuild (§0). The rest was written
2026-09-10/11, after the front end was rebuilt from scratch, and is still
accurate about the front end — but where it mentions Crafty it describes the
`v2.1-crafty` deployment, not these branches. Read `HANDOVER.md` §0A first;
this is the list of what is *incomplete*, separated so you can tell which is
which.

The previous version of this file described the design-canvas runtime and its
five traps. **All of that is gone** — no `<x-dc>`, no `support.js`, no
vendored React, no binding contract. Do not go looking for them.

---

## 0. The commercial rebuild — WP-01 to WP-06 (2026-10-11/12)

**Read this section first; §1 onwards is the September UI work, and where it
mentions Crafty it describes the deployment the box still runs.**

### 0.1 Where it is

Six work packages of `COMMERCIAL-PLAN.md` (in the parent folder, never in
this repo) on six **stacked branches, committed locally, not pushed, not
merged**:

| branch | commit | package |
|---|---|---|
| `main` | `80a413c` | tagged `v2.1-crafty` — the last Crafty release, what the box runs |
| `wp-01-secrets` | `1bc0ebb` | WP-01 no shipped secrets, blocked downloads respected, no AI default |
| `wp-02-engine-interface` | `21a9ea1` | WP-02 the engine interface + mock; Crafty removed from `app/` |
| `wp-03-postgres` | `549b6d3` | WP-03 Postgres + Alembic |
| `wp-04-jobs` | `1d3a88e` | WP-04 Redis + arq job queue and a worker |
| `wp-05-agent` | `d048127` | WP-05 the node agent |
| `wp-06-templates` | the tip | WP-06 loader templates, real boots, the Crafty importer |

`git log --oneline v2.1-crafty..wp-06-templates` shows the lot. The plan's
rule is "merge only when Done-when passes", and the WP-05 and WP-06
Done-whens both name things that cannot be done on this machine (a Docker
node; the real Crafty servers), so **nothing is merged and nothing in
`COMMERCIAL-PLAN.md` §9 is ticked.** Whether to merge WP-01 to WP-04, whose
Done-whens are met, is the user's call.

### 0.2 What changed

- **WP-01.** No CurseForge key or AI endpoint ships (`CURSEFORGE_API_KEY_FILE`
  for a Docker secret; `OLLAMA_URL` blank turns the assistant off). A mod
  whose author blocked third-party downloads is **never fetched**: Modrinth by
  SHA-1, then by slug, else listed as *blocked by author* with a link
  (`app/blocked.py`, the CDN fallback deleted).
- **WP-02.** `app/engine/`: one `ServerEngine` protocol, an in-memory mock
  driver, a router by node. `app/crafty.py` is gone and nothing under `app/`
  mentions Crafty. The loader ladder (node fetches → BlessForge relays,
  checksum-checked → a plain failure) lives in `provision.py`.
- **WP-03.** Postgres (SQLite when `DATABASE_URL` is blank), Alembic migrated
  on start. Accounts, sessions, the audit log, racks and decisions moved
  there from JSON files. Two admins demoting each other at once could
  deadlock on Postgres; admin rows are now locked in id order first.
- **WP-04.** Redis + arq: installs and every long job run on `app/worker.py`,
  progress is a Redis stream per job with a snapshot, so a browser
  re-attaches after a panel restart with no gap and no repeat. Cancel, crash
  detection by heartbeat, and a sweep that marks orphans *interrupted*. Found
  later, running the full loop for WP-06: `close_redis()` never closed the
  arq submit pool, and with the test's Redis served from inside the test
  process that left `test_jobs_redis.py` hanging **at exit, after all 25
  checks had passed**, on some runs — which read as a stalled suite. Both
  connections are closed now, and the fake server's threads are daemon.
- **WP-05.** The node agent (`agent/`, `dev/agent/README.md`): one hardened
  container per server, a jailed file API, panel-signed Ed25519 browser
  tokens, crash backoff and crash-loop events, reconcile on start, a disk
  watchdog; runtime images for Java 8/17/21/25 and a systemd unit. Panel
  side: node registration (`app/nodes.py`), the agent driver, `/api/agent/*`
  (node token only), the console tab on the node's websocket. Its own tests
  found four bugs, all fixed: the Windows jail's identity check could be
  raced (a swap between check and open read the outside file — it now asks
  the open handle where it is, and the race test fails without that), search
  and disk usage walked into Windows junctions, `mkdir` refused a nested
  folder on Linux, and a planted link could not be deleted on Windows.
- **WP-06.** `templates/` — one JSON per loader, checked on load — now drives
  the create screen, the install rung, the startup command, the boot test's
  timeout, and the Java (Mojang's own `javaVersion`, rounded up to a runtime
  the nodes have, the offline table as fallback). Two fixes from running it
  for real: a **pre-1.17 Forge server's heap was never applied** (it went
  into `user_jvm_args.txt`, which a jar-launched Forge never reads; it now
  goes on the command line), and **a server pack with no `manifest.json` or
  `variables.txt` could not be installed at all** ("could not determine the
  Minecraft version") — it now reads the missing fields from the release's
  client manifest. `dev/tools/import_from_crafty.py` moves servers off
  Crafty, read-only towards it; its test caught it copying through a planted
  junction.

### 0.3 Verified

Two machines: this Windows dev box (no Docker), and then — on 2026-10-12,
with the user's go-ahead — the CasaOS box itself (Debian 12, 8 cores, 11 GB,
Docker 28, cgroup v2 with AppArmor and seccomp), in a throwaway folder
beside the live deployment, which was not touched.

| what | evidence |
|---|---|
| The offline loop (Windows) | 771 checks in 16 files, all green: 46 10 38 76 26 36 86 34 41 50 27 25 50 20 176 30 (HANDOVER §9) |
| HTTP suite | `run_http_tests.py` 45/45, on SQLite and on Postgres |
| Front end | `check_frontend.py` 219/219 |
| Postgres | `test_fleet_state`, `test_auth`, `test_policy`, `test_db` and the HTTP suite against Postgres 16 (the pgserver binaries, on the Windows box) |
| Job queue | `test_jobs_redis` 25/25: an API and a worker as processes, the API killed mid-job, re-attach, cancel, orphans |
| Panel ↔ agent | `test_agent_api` 30/30: two processes; register → create → start → console websocket + command → edit → crash audited → stop → delete; both directions refuse the wrong credential |
| **Agent security on Linux** | `test_agent_security.py --live` **189/189 on the CasaOS box**: the openat/`O_NOFOLLOW` jail (the production path), real symlinks, the swap race, and the live tier against real Docker — memory hog OOM-killed inside its own 256 MB (`OOMKilled: true`, exit 137), fork bomb stopped at the PID limit (`Cannot fork`) while the host still forked, read-only root, noexec `/tmp` filled to its size, no Docker socket, `CapEff` all zero, `NoNewPrivs: 1`, uid 1000, a neighbour's folder unreachable |
| **Every template, in Docker** | `boot_templates.py --docker` on the CasaOS box, 8/8 — each booted in its runtime image, then started again, sent `list` through the container attach, answered, and stopped by its own `stop` command (table below) |
| Every template, process runtime | `boot_templates.py` on the Windows box, 8/8 |
| **CurseForge packs, in Docker** | `install_on_agent.py --docker`, 3/3 on the CasaOS box, with the real CurseForge API: Just Create SMP (NeoForge 1.21.1, server pack, 54 mods, boot 91 s), Create: Perfect World (Forge 1.20.1, server pack, 114 mods, boot 197 s), Vanilla Perfected (Fabric 1.21, built from its manifest — every mod downloaded from CurseForge one by one, client-only ones stripped — 79 mods, boot 106 s) |
| **A CurseForge mod, added** | Waystones added to the running NeoForge pack through `/mods/add`; its required dependency Balm came with it; both jars landed and the server booted again (70 s) |
| **CurseForge export, re-imported** | that server exported as a CurseForge pack, uploaded as an import, installed as a second server (52 mods) and booted. Four fewer mods than went in: most likely the client-only mods the first install left disabled, which an export leaves out by default — not checked |
| Importer | `test_import_crafty` 20/20 against `dev/mock/crafty.py` |

| template | Minecraft | Java | Docker: install / boot | process: install / boot |
|---|---|---|---|---|
| vanilla | 26.1 | 25 | 8 s / 54 s | 13 s / 19 s |
| paper | 1.21.4 | 21 | 8 s / 92 s | 10 s / 37 s |
| purpur | 1.21.1 | 21 | 8 s / 106 s | 127 s (slow mirror) / 31 s |
| folia | 1.21.4 | 21 | 8 s / 68 s | 10 s / 27 s |
| fabric | 1.20.1 | 17 | 4 s / 148 s | 8 s / 41 s |
| forge | 1.20.1 | 17 | 60 s (installer container) / 136 s | 38 s / 41 s |
| forge | 1.12.2 | 8 | 38 s (installer container) / 44 s | 25 s / 18 s |
| neoforge | 1.21.1 | 21 | 80 s (installer container) / 70 s | 47 s / 24 s |

Boots are slower on the CasaOS box than on the Windows dev box: it is a
smaller machine already running a dozen services. They are not limits.

**Found by the Docker run, and fixed:** `DockerRunning.stop()` wrote `stop`
to the console and then immediately called Docker's own stop, which sends
SIGTERM at once — not after the timeout, as its comment assumed. The two
shutdowns raced, and on Paper the signal won (the world saved by a JVM
shutdown hook rather than the server's own stop). It now waits up to the stop
timeout for the server to exit by itself and only then falls back; section
8b of `test_agent_security.py` pins it, and Paper then stopped cleanly.

### 0.4 Unproven — the honest gaps

1. **Disk quotas are not implemented.** Plan step WP-05.3 asks for XFS/ext4
   project quotas; there is only the watchdog, which checks every 60 s and
   stops a server after the fact. A fast filler can fill the disk between
   checks. (The CasaOS box is ext4, no project quotas enabled.)
2. **The systemd unit has never been installed**; its hardening
   (`ProtectSystem=strict` with the Docker socket writable, the capability
   set) is unverified. On the CasaOS box the agent ran as the `chargey` user
   from a venv, not as the service.
3. **No WireGuard, no TLS on the agent.** Panel ↔ agent ran over plain HTTP
   on localhost, on both machines.
4. **Redis has only ever been fakeredis over TCP.** arq's startup `INFO` call
   is stubbed in the test worker because fakeredis does not answer it.
5. **The compose files have never been brought up** — neither the root one
   nor `deploy/control-plane/`. Postgres was the pgserver binaries, not the
   `postgres:16-alpine` image.
6. **The importer has never met the real Crafty.** It speaks the endpoints
   the old client verified, and is tested against the mock only. No real
   server has been imported and Crafty has not been uninstalled — the plan
   says to; that is a manual step after a backup, and the report says when.
7. **Nothing is deployed.** The CasaOS box still runs `v2.1-crafty`. The
   tests there ran in `~/bf-wp06-test`, which was removed afterwards; the
   four runtime images (`ghcr.io/chargeyxd/blessforge-runtime:java8/17/21/25`,
   about 1.2 GB) were left built on the box for the next run.
8. **SFTP** (`/api/agent/sftp-auth` exists; the server is WP-11) and
   **backups on the agent** (501) are not built.
9. **There is no screen for nodes.** Registration is `POST /api/admin/nodes`;
   the admin views are WP-08.
10. **The `test_agent_api` websocket path has not run against Docker** — its
    stand-in server is a Python script and the runtime images have no Python.
    The Docker console was proven instead by the `list` round trip above.

### 0.5 Where the build differs from the plan

- The agent has no `console.py` or `stats.py`: the console ring buffer is in
  `agent/servers.py` and Docker stats in `agent/containers.py`.
- The Docker Engine API is spoken with `httpx` over the unix socket rather
  than aiodocker or the Docker SDK — one dependency fewer, and the hijacked
  attach is twenty lines.
- Runtime images are Java 8, 17, 21 **and 25**: the plan lists three, but
  Minecraft 26.x requires Java 25 (checked against Mojang's version files,
  and 26.1 booted on it above).
- Templates are JSON data read by `app/templates.py`; the install logic stays
  in `loaders.py` / `provision.py` as the plan asked.

### 0.6 Where to pick up

1. Disk quotas (XFS project quotas on a node's servers volume), or accept the
   watchdog as the WP-05 answer — a decision for the user.
2. Install the agent as the systemd service on a node and rerun
   `test_agent_security.py --live` and `boot_templates.py --docker` there.
3. `import_from_crafty.py --dry-run` against the real Crafty, then for real
   with `--from-dir`, then the merge decision.
4. WP-07 (public accounts) is next in the plan.

---

## 1. What changed in the September session

| area | what it is now |
|---|---|
| Front end | Rebuilt: plain ES modules, real DOM from `h()`, hash router, one module per screen loaded on demand. The pink shrine fox theme from the Project Fox landing page — same tokens, same wiggling polygon, same shrine ornament. |
| Loader failures | A three-rung ladder (Crafty → Crafty's index → the loader's own project) shared by blank creates and modpack installs, plus **Finish setup** to retry without losing the instance. |
| Blank instances | `New server`: seven loaders, real upstream version lists, the right mod folder for each. |
| Paper | A first-class creatable loader, with a Modrinth plugin catalogue, compatibility checking, a dependency preview, a starter list and a `plugins/` audit. |
| File manager | The whole server directory: browse, edit, upload, download, rename, create, delete, extract, search, per-folder sizes. Streamed both ways. |
| Players | Ops, whitelist, bans, IP bans and who is online, routed by whether the server is running and saying which route it took. |
| Client-only detection | Rebuilt as scored evidence with four new signals that work on Forge/NeoForge. One implementation, used by the install review *and* by a scan of a live instance. |
| Decisions | The whitelist is now allow **and** block, matched by project id → mod id → version-stripped stem, global or per-instance, exportable. |
| Roulette | Every pinned build verified against what its publisher declares; short hands topped up in seeded rounds instead of shipped short. |

## 1A. The second pass — how it looks, and how fast

The rebuild was correct and read flat, so it was reworked the same day. No
endpoint moved; three server-side changes exist only to make the page load
faster.

| area | what it is now |
|---|---|
| Layering | Explicit, and machine-checked. Every card, button and loader tile isolates its own stacking context and holds three layers: blob at 0, fill and border at 1, content at 2. The blob bleeds ~17 px past the card edge, behind the fill. |
| Load time | `/api/health` runs its three upstream checks under `asyncio.gather` behind an 8 s cache, and boot no longer awaits it. DOMContentLoaded 248 → 118 ms, load 589 → 387 ms, health 1.3 s → 665 ms cold and 2 ms warm. |
| Motion | Every entrance animation is translate-only and gated on `html.js-motion`; `prefers-reduced-motion` turns it all off. Hover lifts, magnetic buttons, ripples, counting numbers, staggered reveals. |
| Mod side tags | Every mod row says `client only` / `server only` / `both`, from the same scored evidence the install review uses. A jar with nothing recorded says so instead of guessing. |
| Config editor | Side by side in the tab rather than in a modal. Unsaved text survives switching files; leaving with unsaved work warns. |
| Roulette | The constraint system is rebuilt — a recklessness ladder, an intensity dial, a live catalogue monitor. The lever is now a rope you drag, with resistance, release and a petal burst; clicking still works for keyboards and phones. |
| Diagnose | Returns named passes with counts, a `complete` flag and de-duplicated findings, so "nothing found" means the checks ran. |
| Chrome | Fox app icon and favicon, sidebar buttons with state and motion, evenly sized loader tiles, Paper and Purpur marks, source logos on mod rows, art in the job drawer and on the delete confirm. |

**The layering is the part worth understanding.** The polygon used to be a
`z-index:-1` pseudo-element, which paints behind its parent's background *only
while the parent is not a stacking context*. The entrance animation put a
`transform` on every card, so every card became one, and the blob started
painting over the card and under the text. Relying on that trick was the bug;
the three explicit layers are the fix, and `check_frontend.check_layering`
enforces them — including that no blanket `.card > *` rule exists, because
that selector outranks `.p5-hl`'s own `position:absolute` on specificity and
collapses the blob to a zero-size element in flow.

Two more traps from this pass, both now guarded:

- **Never animate *from* `opacity:0` with `fill:both`.** If the document
  timeline stalls — a hidden tab, a throttled preview pane — the animation
  never starts and the content is permanently invisible. Every entrance
  animation is translate-only for that reason.
- **Cache control is header-based** (`_StaticCache`), not `?v=` fingerprints.
  ES module imports carry no query string, so a fingerprint on the entry point
  does nothing for anything it imports.

## 1B. The third pass — eleven reported items (2026-09-11)

| area | what it is now |
|---|---|
| Dark accent | `--slab-bg` was `#F3E4E8`, 15.2:1 against the page — white in all but name. Now `#C98FA2`, 7.1:1, with slab text still at 7:1. |
| Polygon | Six shapes, and `hl()` stamps each blob its own duration, phase and direction. Nothing runs while idle. |
| Ghost buttons | `.btn.ghost::after` is the fill layer and was transparent, so the blob painted behind the label at **1.53:1**. The bleed also reached into a smaller gap and covered neighbours. Both fixed. |
| Press | Press and release had one shared overshooting curve. Split; release throws petals, `.btn.danger` throws ash. |
| Shine | The panel sheen ran on every panel on a 7s loop forever. Replaced with a static asanoha weave at ~3%. |
| Racks | `#/groups`, `app/fleetgroups.py`. Torii lintels, ema plaques, the open yard. Rail shows 5. |
| Koma | A comic panel of a mod's project page, opened from the row but not its controls. |
| Silent tuning | `optimizer.advise()` clamps and validates a model answer into the deterministic plan; the ofuda says what moved. |
| Heap slider | Dragged in whole gigabytes; now a fifth of a snap, with a continuous strain curve toward the ceiling. |
| Backdrop | A blurred cherry tree per theme as SVG, resolved by `/assets/backdrop/{theme}` so a real photograph dropped in takes over. |

**A torn state file, found by a real power cut.** Mid-review the user
restarted the machine and the racks were gone: `fleet-state.json` came back
as 559 NUL bytes. Every state writer in the app wrote straight over the live
file with no temp file and no `fsync` — neither atomic nor durable. All nine
now go through `config.write_state` (temp → fsync → rename → fsync the
directory) and read through `config.read_state`, which treats a torn file as
absent. If you add a tenth writer, use the helper.

**Three more bugs found that nobody reported.** Worth knowing because each was
invisible until the exact wrong moment:

1. **Every modal animated from `opacity:0` with `fill:both` and was not
   gated on `html.js-motion`.** A stalled document timeline meant an
   invisible dialog over a live scrim. Every confirmation in the app is a
   modal.
2. **Five places painted `#fff` on `var(--rose)`** — 8:1 in light, **2.40:1**
   in dark. `--slab-rose-fg` existed for this and was not used.
3. **`check_api` compared call sites to routes by PREFIX**, in both
   directions, so `/api` prefixed everything and the suite was green with
   **ten routes missing**. It now compares normalised shapes exactly.

## 2. Verified in this session

Driven against a mock Crafty (`dev/`-adjacent, not committed) and against the
live CurseForge, Modrinth, Mojang, FabricMC, NeoForged, PaperMC and PurpurMC
APIs:

| area | evidence |
|---|---|
| Every route renders | 6 top-level screens + 9 instance tabs, zero console errors |
| Loader catalogue | All seven loaders resolved real version lists **with no Crafty at all** |
| Paper plugins | Search, compat labels, Folia strictness, dependency preview picking the *bukkit* build of spark rather than its NeoForge one |
| File manager | Browse, per-folder sizes, editor with gutter, edit → confirm → save → verified on disk → snapshot taken |
| Players | Both routes: console on a running server, JSON file on a stopped one, each saying which it took; Mojang UUID lookup |
| Client-only scoring | 6 targeted cases including the Forge mod that declares nothing and is caught by package layout |
| Backend suite | 171 checks across 6 test files (`test_fleet_state.py` is new: 30) |
| Front end | 194 checks (`dev/tools/check_frontend.py`) — the layering contract, plus exact API-shape matching and CSS-referenced assets |
| Layering | Read back from computed styles in a real browser: card `isolation:isolate`, blob `position:absolute` at z 0, fill at z 1, bleeding 17 px left and 15 px top |
| Load time | Measured, not estimated: shell 4 ms, health 541 ms non-blocking, instances 32 ms, DCL 118 ms, load 387 ms |
| The rope | Dragged and clicked; recklessness ladder lit to `11111` at Unhinged, crest cycling, the roll starting from both |
| Config editor | Opened a real TOML beside the list, typed into it, row marked unsaved, status line counting lines and bytes |

Two real bugs were found by driving the UI rather than reading it, and both
are fixed: `confirmDialog` resolved `false` before it resolved `true` (so
**every** confirmation silently did nothing), and a class token with a
trailing space threw and took the whole screen down.

## 3. Unproven — the honest gaps

1. **No install has been run end to end in this session.** The installer was
   refactored (the loader ladder moved to `provision.py`) and its tests pass,
   but a real modpack has not been installed against a real Crafty since. That
   is the biggest untested surface.
2. **`create_empty` has not created a real server.** Every part of it is
   exercised — the catalogue, the version lists, the form, the job — but the
   final `crafty.create_server` call has only been made against a mock.
3. **A plugin has not actually been downloaded into `plugins/`.** Resolution,
   compatibility and the preview are verified; the upload step is the same
   `mods.add_mod` path the mod flow uses, but it has not been run.
4. **The assistant still has not produced a plan here.** Unchanged from the
   previous session: `/ai/analyse` and `/ai/crash-review` are wired and the
   endpoint answers, but no analysis has been run to completion, so the plan
   list and `_apply_actions` remain untested against real model output.
5. **No destructive button has been pressed against a real server.** Delete,
   kill and delete-world are wired and their confirmations work (and the
   confirmation bug above is fixed, so they now actually fire).
6. **The rope has not been pulled with a real finger.** It is driven by
   pointer events, which is the right API for it, and it was dragged with a
   synthetic pointer — but a touchscreen has not been near it.
7. **Below 900 px is drawn but not driven.** The layout collapses correctly in
   CSS — the rail becomes a drawer, grids go to one column — but it has not
   been used on a phone.

## 4. Known gaps and rough edges

- **The mod list is not virtualised.** Fine at 245 rows. Past ~600 the DOM
  gets heavy; the search box is the current answer.
- **`main.py` is 2,575 lines** and mixes routing with logic. Splitting by
  resource (`routes/instances.py`, `routes/files.py`, …) would make it
  navigable; nothing depends on it being one file.
- **The file manager cannot move a file between folders.** Crafty exposes
  rename-within-a-directory and nothing else, so a move would have to be
  download-and-re-upload. Left undone rather than done badly.
- **Player notes live in the instance** (`.blessforge-players.json`), so
  deleting the server deletes them. That is the right trade — they are about
  that server — but it is worth knowing.
- **Quilt** is still not in Crafty's catalogue, so Quilt packs cannot be
  created. Fabric, Forge, NeoForge, Paper, Purpur, Folia and Vanilla all work.
- **`velocity` and `waterfall` are recognised as plugin families** but are not
  offered on the create screen; they are proxies, not servers, and the rest of
  the app assumes a world.
- The open instance polls `/stats` every 6 s while the tab is visible. Two
  browser tabs double it.
- CurseForge file-level dependency metadata is still incomplete — a file can
  declare none while the jar requires something.

## 5. Things that will change under you

Two external contracts moved during this session and will move again:

- **PaperMC sunset its v2 API.** Everything under `api.papermc.io/v2` now
  answers 410. v3 is `fill.papermc.io/v3`, on a different host, with grouped
  version lists and downloads keyed `server:default`. `loaders.py` speaks v3.
- **Minecraft's version scheme changed.** Releases are now year-based (`26.2`,
  `26.1.2`) alongside the legacy `1.21.x` line, and NeoForge followed —
  `26.2.0.84` is four components where `21.1.250` was three.
  `loaders._neoforge_candidates` returns *both* readings and keeps whichever
  Mojang actually publishes, so the next scheme change costs a list entry
  rather than a bug. `packs.mc_from_neoforge` handles both shapes too.

If a version list comes back empty, check those two first.

## 6. How to check your work

The whole loop — sixteen offline files, the HTTP suite, the front end, and
the database-backed ones again on Postgres — is `HANDOVER.md` §9. The short
version:

```bash
cd /path/to/blessforge
for t in test_loader_detection test_job_stream test_install_decisions \
         test_roulette test_boot_verdict test_fleet_state test_auth test_policy \
         test_blocked_downloads test_engine test_db test_jobs_redis \
         test_templates test_import_crafty test_agent_security test_agent_api; do
  python dev/tools/$t.py | tail -1
done
python dev/tools/run_http_tests.py
python dev/tools/check_frontend.py
```

`check_frontend.py` is the one that catches this front end's own failure
modes. Note in particular that `node --check <file>` is **not** a syntax check
for an ES module — it parses as a script and will accept a file with a broken
string literal. Only `node --input-type=module --check` is real, which is what
the tool uses, and that distinction cost a screen in this session.
