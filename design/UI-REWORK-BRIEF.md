# BlessForge UI rework — brief for Claude Design

Written 2026-10-12, at the end of the setup-hardening work (branch
`setup-hardening`). This is the document to read **before** redrawing any
screen. It says what every screen is for, which endpoints it talks to and
what they return, how the older options sit on the new engine, which
rendering bugs were seen and why, and which assets the redesign needs.

The current front end is plain ES modules and real DOM (`app/static/js`,
`app/static/css`). Nothing in `design/BlessForge.dc.html` describes it — that
file is historical (see `design/README.md`).

---

## 0. Non-negotiables

1. **Theme: the pink shrine fox.** A shrine at night: plum ink, sakura, gold
   leaf and paper. The pink/rose accent stays the accent. Tokens live in
   `app/static/css/theme.css` (`:root`, `@media (prefers-color-scheme:dark)`,
   `:root[data-theme="dark"]`). The core ones:

   | role | light | notes |
   |---|---|---|
   | paper / page | `--paper #FBF3F0`, `--paper-2 #F5E9E5`, `--paper-3 #EFE0DB` | woven `--washi` texture, no gloss |
   | card | `--card #FFFFFF`, `--card-2 #FDF8F7` | |
   | ink | `--text #2E2432`, `--mid #6E5C68`, `--faint #7C687A` | |
   | slab (dark bands, headers) | `--slab-bg #241B28`, `--slab-fg #FBF3F0` | |
   | sakura | `--sakura #E9A0B4`, `--sakura-soft #F8DCE3`, `--sakura-deep #D9788F` | |
   | **accent** | `--rose #B23F5C`, `--rose-hi #D4587A`, `--rose-soft #F7E2E7` | the pink accent |
   | shrine | `--shrine #B23F5C`, `--shrine-deep #8E3049` | torii red-pink |
   | gold leaf | `--gold #C9A227`, `--gold-leaf` gradient | "finish setup", premium moments |
   | states | `--sage` ok, `--amber-ink` warn, `--danger` bad, `--plum` orphaned, `--info` | |

   Type: Archivo Black (display), Plus Jakarta Sans (UI) from Google Fonts;
   Space Grotesk and JetBrains Mono vendored in `app/static/vendor/fonts`.
   Light, dark and system themes all exist and all must keep working.

2. **Themed objects, not stock controls.** The owner rejects anything that
   reads "generic template", "plain", "static" or "bland". The current UI
   turns controls into shrine objects: the CPU vital is a torii standing in a
   tide, memory is a paper lantern that fills, the roulette lever is a shrine
   rope you drag, racks are ema plaques on hooks, petals fall in the
   background. Keep that spirit; redraw freely.

3. **Motion is expected everywhere — under three rules that fail silently
   when broken** (each cost a session to find):
   - **Never animate *from* a placeholder.** If the document timeline stalls
     (background tab, throttled pane), the first frame never runs. An
     entrance from `opacity:0` with `fill:both` stays invisible forever; a
     `countUp` from 0 shows "0 mods" for a 120-mod pack. Land on the true
     value first, then animate. Entrances are translate-only, gated on
     `html.js-motion`.
   - **Layering is explicit.** The wiggling highlight polygon is a layer
     *inside* its host: blob z0, fill z1, content z2, `isolation:isolate` on
     the host. No `z-index:-1`; no blanket `.card > *` rule.
   - **A rAF simulation must be correct at rest** and built once, then moved
     by `mount` — never rebuilt by its own repaint.

   `python dev/tools/check_frontend.py` enforces most of this (230 checks).

4. **Lists answer to their own width, not the viewport's.** The file list
   broke at a 1024 px window because its fixed columns were sized by viewport
   media queries while the list itself sat beside two sidebars. Anything that
   lives in a panel next to other panels uses container queries
   (`container-type:inline-size` on the panel). See §6, bug 6.

5. **Unknown is not zero.** "No reading" and "0" must look different, and
   "?" in a monospace font read as "7" in testing. Use an em dash or a
   distinct "unknown" pill for missing values.

6. **Focus survives repaints.** Every filter bar repaints on `input`;
   `mount()` restores focus, caret and selection when the focused element was
   inside what is being rebuilt. A new rendering layer must keep that, or
   search boxes lose their caret after one character (this was a reported
   bug, §6).

7. **Touch targets** are 44 px on touch layouts; the mobile drawer exists and
   must keep working (`HANDOVER.md` §5J).

---

## 1. What the UI sits on

```
browser ──HTTP/SSE──▶ panel (FastAPI, app/main.py) ──HTTP──▶ node agent (agent/main.py)
   │                                                       └── Docker runtime: one locked-down
   └──────────── WebSocket, signed token ─────────────────────▶ container per server
```

- **Crafty Controller is gone.** The panel talks to its own node agents
  (`ENGINE=agent`; `mock` for offline tests). A node runs servers in Docker
  containers (read-only root, no capabilities, CPU/memory/PID limits, one
  bind mount). The browser never talks to Docker.
- **Routing is hashes**: `#/` fleet, `#/groups`, `#/create`, `#/discover`,
  `#/roulette`, `#/settings`, `#/i/<server_id>/<tab>`. Keep deep links working
  — a link to a crashed server's Diagnose tab is something people send.
- **`api`** (`core.js`) wraps fetch; errors carry the server's `detail`.
  Show it — the backend writes human sentences on purpose.
- **Jobs.** Anything slow is a job: `POST` returns `{job_id}`; the drawer
  follows `GET /api/jobs/{id}/events` (SSE). A job snapshot is
  `{id, kind, title, server_id, server_name, owner, status, step, percent,
  log[], result, error, created, finished, elapsed, stream}` with `status` in
  `queued|running|done|error|cancelled`. `run()` starts a job and shows it;
  `runAwait()` starts one and resolves with its `result`. A job belongs to the
  server, not the page: closing the drawer never stops it, a reload adopts
  running jobs (`GET /api/jobs`), and `POST /api/jobs/{id}/cancel` cancels.
  Several jobs are followed at once.
- **Console**: `GET /api/instances/{id}/console/socket` →
  `{available, url, token, expires, may_type}`; the browser opens the node's
  WebSocket directly with that short-lived token. Fallbacks:
  `GET .../console/stream` (SSE) and `GET .../console` (buffer).
- **Permissions**: `GET /api/auth/session` carries the user, role and
  permission lists; every route is checked server-side (`app/policy.py`).
  Hide or disable what the user cannot do rather than letting them hit 403.
- **Boot never waits on health**: paint first, then `GET /api/health`
  (8 s cache) fills in connection state.
- **Caching**: `.js/.css/.html` are `no-cache, must-revalidate`; images and
  fonts `immutable`. New asset files need new names if they change.

---

## 2. The modules, screen by screen

For each: what it is for, its regions and states, its controls, and the
endpoints behind them. `{id}` is a server id. Jobs are marked **(job)**.

### 2.1 Shell (`app.js`, `jobs.js`, `core.js`, `petals.js`)
- **Rail**: brand + app mark, nav (Fleet, Racks, New server, Discover, Mod
  Roulette, Settings), the server list (state dot, loader, version, "N online"
  or "added X ago"), the signed-in user, theme switch, **Activity** button,
  connection badge (`CONNECTED` / `CHECKING`). Mobile: a drawer.
- **Topbar**: page title with a small mono subtitle, contextual actions.
- **Activity drawer**: every job, live progress, log tail, cancel.
- **Toasts, modals** (`confirmDialog`, `promptDialog`, `modal`), skeletons
  per route (`pageSkeleton`), `loadingFox(label)`, `empty(...)`.
- Endpoints: `GET /api/instances` (rail + fleet), `GET /api/fleet/groups`,
  `POST /api/fleet/seen` (records when servers were seen up), `GET /api/health`,
  `GET /api/jobs`.
- Backdrop art: `GET /assets/backdrop/{light|dark}` (an override image dropped
  into `app/static/img/` wins).

### 2.2 Sign-in (`auth.js`)
Username, password, Enter. Lockouts and the "default password in use" warning
come back in the session. `POST /api/auth/login`, `/logout`, `/password`,
`GET /api/auth/session`.

### 2.3 Fleet (`views/fleet.js`)
- Header "The fleet", filter (All / Up / Down / Needs you), four KPI tiles
  (servers, players online, mods managed, needs attention).
- **Server card**: icon (loader logo), name, state pill, loader, version, port,
  mod count, problems pill, "up 12m / ran 3 h ago / added 19 min ago",
  rack tag, pack name or "no modpack"/"not created by BlessForge", gauges (CPU,
  RAM, Players), Start/Stop, Restart, Console, **Finish setup** (gold, for a
  half-finished install).
- States from `_instance_state`: `running`, `stopped`, `crashed`, `orphan`
  (record exists, node cannot read its folder or is not answering),
  `incomplete` (install stopped half-way).
- Endpoints: `GET /api/instances`, `POST /api/instances/{id}/action/{start_server|stop_server|restart_server|kill_server}`,
  `POST /api/instances/{id}/loader/reinstall` **(job)** for Finish setup.
- Fields per server (from `GET /api/instances`): `server_id, name, port, path,
  type, executable, auto_start, created` (ISO, UTC, `Z`), `pack, minecraft,
  loader, managed, incomplete, problems, mod_count, running, players,
  max_players, cpu, cpu_cores, cpu_allowance, mem, mem_bytes, world_size,
  crashed, reachable, state`. **`cpu` is now a 0–100 share of the cores the
  server may use** (see §4); `mem` is a share of the server's own limit.

### 2.4 Racks (`views/groups.js`)
Ema plaques on hooks: named groups of servers with a motif (Sakura, Fox,
Torii, Pine, Moon, Water, Thunder, Snow). Drag servers between racks, "Hang
in…", "Take down", "Move all", a plan preview before bulk moves, and the
"open yard" for unracked servers. Endpoints: `GET/POST /api/fleet/groups`,
`DELETE /api/fleet/groups/{group_id}`, `POST /api/fleet/groups/plan`,
`POST /api/fleet/assign`.

### 2.5 New server (`views/create.js`)
Server software (loader cards with logos and versions), identity (name,
port), world and players (MOTD, seed, difficulty, max players, online mode,
whitelist), heap slider (shrine slider) measured against the host, starter
plugins for Paper-family loaders, "What you get" summary. Endpoints:
`GET /api/loaders`, `GET /api/loaders/{family}/versions`, `GET /api/host/specs`,
`GET /api/plugins/starter?family=&game_version=`, `POST /api/provision/server`
**(job)**.

### 2.6 Discover (`views/discover.js`)
Three modes: **Modpacks** (CurseForge first, Modrinth), **Mods**, **Import an
export** (drop a CurseForge export zip). Catalogue cards with source mark,
downloads, categories; endless feed; a pack panel with versions; **Install**
opens the preflight review, then installs.
- Preflight review (`POST /api/install/preflight` **(job)**): RAM/Java fit and
  one row per candidate mod with a `recommendation`: `remove` ("client-only",
  ticked to disable), `review` ("worth a look"), `keep` ("protected", cannot be
  ticked) and `shipped` (**new** — "in the server pack": a server pack is
  trusted as its author built it, nothing is pre-ticked). Reported alongside,
  not as row statuses: files the author **blocks** from third-party download
  (`blocked_by_author`), files **substituted** from Modrinth by hash or by
  name + author (`substituted_for`), and wrong-loader jars.
- Install: `POST /api/install/modpack` **(job)**, now with `boot_check`
  (default on). The job's result carries `boot` (outcome `ready|timeout|failed`,
  `rounds`, `seconds`, `why`, `failure_line`) and `repairs` (what the boot
  check changed) — show both in the finish card.
- Uploads: `POST /api/uploads/modpack`, `GET /api/uploads`,
  `DELETE /api/uploads/{upload_id}`. Catalogue: `GET /api/browse/modpacks`,
  `GET /api/browse/mods`, `GET /api/modpacks/{mod_id}`,
  `GET /api/modpacks/{mod_id}/files`, `GET /api/meta/minecraft-versions`.

### 2.7 Mod Roulette (`views/roulette.js`)
The offering (constraints: version, loader, count, categories), the shrine
rope you drag to deal, the hand, the odds, lineage, "What is in the export?",
reroll, install the hand. Endpoints: `GET /api/roulette/meta`,
`POST /api/roulette/pool|roll|reroll|preview-export` (pool/roll are jobs via
`runAwait`), `POST /api/roulette/install` **(job)**,
`GET /api/roulette/export/{roll_id}`.

### 2.8 Settings (`views/settings.js`, `views/admin.js`)
Your account (password), Connections (CurseForge key present, Modrinth,
updates — "Check again"), Appearance (System/Light/Dark), Download cache
(size, Prune now), Scheduled update checks (Sweep now), Troubleshooting
assistant (Ollama endpoint, model pull), Client-only decisions (allow/block
list, import/export/clear), Administration: users (the register), audit log
(the ledger), sessions. Endpoints: `/api/cache`, `/api/cache/prune`,
`/api/updates`, `/api/updates/check` **(job)**, `/api/ai/*`, `/api/whitelist*`,
`/api/admin/users*`, `/api/admin/audit*`, `/api/admin/permissions`.
**Missing: Nodes** — see §5.

### 2.9 Instance shell (`views/instance.js`)
- Header: name, state pill, loader, version, port, Java, uptime, pack line,
  Stop / Start / Restart / power (kill), Delete.
- **Vitals rail** (three themed objects):
  - CPU — torii in a tide; tone steady/busy (60)/straining (85). Value is the
    share of the server's CPU allowance; detail text names the cores
    (`cpu_cores` of `cpu_allowance`).
  - Memory — lantern; big number is resident bytes, under it
    "84% of 2.8 GB" (`mem_percent` of `mem_limit`).
  - Players — `online / max`, names with session length.
- Tabs: Overview, Mods **or** Plugins (by loader family), Files, Players,
  Console, Configs, Tune, Diagnose, Undo.
- Polls `GET /api/instances/{id}/stats` every 6 s while visible. Stats fields:
  `running, state (offline|starting|running|stopping|crashed|installing),
  crashed, started, online, max, players[], sessions{name:{since, exact}},
  player_source (log|list|ping), version, cpu, cpu_cores, cpu_allowance, mem,
  mem_limit, mem_percent, last_exit{code, oom, at, asked}`. `last_exit.oom`
  means the kernel killed it for its memory limit — say so in the header and
  point at Tune, never at a mod.
- States to design: running, starting, stopping, stopped, crashed (with the
  crash-loop note), orphan ("no server directory"), incomplete ("setup
  unfinished" + Finish setup), installing.

### 2.10 Overview tab (`views/tab-overview.js`)
"This server" facts (state, players of slots, world size, mods, loader +
version, Minecraft, Java needed vs set, port, directory `node:uuid`, mod
folder, installed, source), Health (on demand: `GET .../diagnose`), **Who is
on** (count "x of y on · confirmed with the server", each name with session
length, "+N the server did not name"), Quick actions, CurseForge pack export
`POST .../export` **(job)**, Boot test `POST .../smoke-test` **(job)**, roll
lineage for rolled servers `GET .../roll`.

### 2.11 Mods / Plugins tab (`views/tab-mods.js`)
- List with side pills (Client only / Server only / Both / Unknown), source
  marks, versions, update available, enable/disable/delete, bulk actions,
  filters, search; "Look them up" (identify by CurseForge fingerprint, then
  Modrinth hash); change version; "Always treat as server-safe / client-only".
- Records carry `client_only`, `client_only_reasons`, `wrong_loader`,
  `switched_off_in_pack` (**new** — the pack's manifest marked it optional, so
  it was installed `.disabled`), `substituted_for`, and `repair` (**new** —
  what the boot check did to it and why).
- Add-mod browser: CurseForge / Modrinth, Best/Popular/Recent, choose a build,
  plan the install. Blocked-by-author files show the author's page and an
  upload button (`blockedByAuthor` in `core.js`).
- Endpoints: `GET .../mods?directory=`, `POST .../mods/toggle|bulk-toggle|delete`,
  `POST .../mods/add` **(job)**, `POST .../mods/identify?directory=` **(job)**,
  `GET .../mods/updates`, `GET /api/mods/{source}/{project_id}/versions`,
  `GET /api/browse/mods`, `POST .../client-scan?directory=` **(job)**;
  plugins: `GET /api/browse/plugins`, `GET /api/plugins/meta`,
  `GET /api/plugins/{project_id}/versions`, `GET .../plugins/audit`,
  `POST .../plugins/resolve`, `POST .../plugins/add` **(job)**.

### 2.12 Files tab (`views/tab-files.js`)
Folder column (top-level folders with sizes, "Recount sizes"), breadcrumb,
**search box** (persistent, keeps focus/caret, stale replies dropped),
New Folder/File, Upload (a `.zip` offers Extract; a `.jar` never does),
per-row download/edit/rename/delete, multi-select delete, an editor for text
files. Endpoints: `GET .../files?path=`, `GET .../files/read`,
`POST .../files/write|create|rename|delete|extract`, `POST .../files/upload`,
`GET .../files/download` (raw), `GET .../files/search?q=&root=` (**now runs on
the node, one request**), `GET .../files/usage` (**now measured on the node**).
Not wired yet: `POST .../files/transfer` (signed direct browser↔node link for
big files) and moving files between folders (the node supports move/copy now;
Crafty did not — the old "cannot move" limitation is gone).

### 2.13 Players tab (`views/tab-players.js`)
Everyone the server knows (online, ops, whitelisted, banned), each with avatar,
op level, note, ban reason/expiry and — **new** — `last_seen`, `playtime_s`,
`deaths`, `mob_kills`, `player_kills` from the world's own player files, and
`online_since` / `online_since_exact` for those online. Header line shows
the source (`player_source`: log / list / ping) and `server_version`. Access:
whitelist on/off, enforce, re-read whitelist.json; banned IPs. Actions: add,
whitelist, op, kick, ban, unban, note, bulk. Endpoints: `GET .../players`,
`POST .../players/action|bulk|note|whitelist-mode`,
`GET /api/players/lookup?name=`. When stopped, changes are written to files and
apply at next start (the snapshot's `note` says so).

### 2.14 Console tab (`views/tab-console.js`)
Live output (direct WebSocket), Following toggle, filter (All / Warnings /
Errors / Chat), command box with history, Send. Endpoints: §1 Console,
`POST .../command`.

### 2.15 Configs tab (`views/tab-configs.js`)
Config files grouped by mod/folder, "Whole directory", an editor with
Save/Revert. Endpoints: `GET .../configs`, `GET .../configs/read?path=`,
`POST .../files/write` (`.../configs/write` exists too).

### 2.16 Tune tab (`views/tab-tune.js`)
- **How it is running** (**new**, `GET .../perf`): spark chips (TPS, ms per
  tick p95, heap %), JVM faults and warnings with checkboxes (faults ticked by
  default), optimisation mods present and what each does, notes, Paper/Spigot
  settings, **switches in the optimisation mods** (ServerCore dynamic
  distances, villager lobotomy, activation range, fast biome lookups;
  Chunky continue-on-restart — each with "changes gameplay a little" where
  true), "Apply the ticked fixes" (`POST .../perf/apply`
  `{jvm_fixes[], yaml_ids[]}` → `{applied[], restart_needed}`), action cards:
  **Pre-generate** with radius 1k/2k/4k/8k (`POST .../pregen`
  `{radius, world}` **(job)**; cancelling pauses, running again continues),
  **Install Chunky / Install spark** (`POST .../perf/install {tool}` **(job)**).
  It takes 1–2 s to answer — design a real loading state.
- Memory: host RAM, current vs proposed heap, the shrine slider, ceiling.
- JVM flags: Aikar's set adjusted for heap and cores, each with a reason.
- server.properties: proposals with reasons; Port (set in both places).
- Apply: memory and flags (`POST .../optimize`), AI advice when configured
  (`POST .../optimize/advice`). Other: `GET .../optimize`,
  `GET/POST .../properties`, `GET/POST .../port`.

`GET .../perf` returns `{kind: vanilla|plugins|mods, loader, minecraft, java,
container_mb, cpus, jvm{args[{arg, from}], issues[{level bad|warn|info, title,
detail, arg?, fix?}], source}, mods[{key, name, file, what}],
actions[{id: pregen|install_chunky|install_spark, title, detail}], notes[],
paper[...], mod_settings[{id, file, key, current, value, why, gameplay, mod,
applied}], spark{tps, mspt, mspt_1m, cpu_system, cpu_process, memory_mb, at},
counts{bad, warn}}`.

### 2.17 Diagnose tab (`views/tab-diagnose.js`)
Views: **Findings**, **Client-only scan**, **Profiler** (**new**), **Logs**.
Buttons: Re-check, Deep scan **(job)**, **Boot and repair** (**new**, job),
Ask the assistant (when configured).
- Findings: severity, title, detail, one-click fixes (accept EULA, set Java,
  set RAM, fix versions, **Bring back X** — `POST .../fix/dependency
  {mod_id, required_by}` **(job)** installs the right build of a missing
  library or re-enables a disabled one). Missing dependencies, duplicates.
  Vanilla and plugin servers no longer show "No mods directory". Client-only
  mods are **not** flagged on name alone any more — only when a boot proved
  they break it.
- **First-boot card**: from the manifest's `boot_check` and `repairs`: outcome,
  each repair (disable / enable / install / set Java) with its reason and an
  Undo.
- **Boot and repair**: `POST .../repair` **(job)** → `{outcome ready|timeout|failed,
  rounds, repairs[], why, seconds, failure_line, findings}`. Boots, reads the
  crash, applies only what the evidence proves, boots again (≤ 8 rounds).
- **Profiler**: spark status (`GET .../spark` → `{loader, minecraft, installed,
  where, file, supported, why_not?, license "GPL-3.0", source, reports[]}`),
  credit line (spark by lucko, GPL-3.0, link to source), Install spark
  **(job)**, **Health snapshot** (`POST .../spark/health` **(job)**), profile
  length 30 s / 60 s / 2 min, upload toggle (**off by default**; on uploads to
  spark.lucko.me and the link is public to anyone holding it), **Record a
  profile** (`POST .../spark/profile {seconds, upload}` **(job)**). Report
  cards: kind, when, TPS, ms p95, CPU, heap %, players, verdict + advice,
  "Open in spark's viewer" (uploaded) or "Download the profile"
  (`GET .../files/download?path=`). Report: `{kind health|profile, at,
  numbers{tps{5s,10s,1m,5m,15m}, mspt{min,median,p95,max}, mspt_1m,
  cpu_system, cpu_process, memory_mb{used,max,percent}, gc[]}, players,
  advice[], file?, viewer_url?}`.
- Client-only scan: reads every jar, lists client-only ones with reasons,
  Disable them all / per row, Always allow / Always block.
- Endpoints also: `GET .../diagnose`, `POST .../deep-scan` **(job)**,
  `POST .../client-scan/apply`, `POST .../fix/accept-eula|java|set-ram|versions`,
  `POST .../mods/bulk-toggle`, `POST .../ai/analyse`, `POST .../ai/crash-review`.

### 2.18 Undo tab (`views/tab-backups.js`)
Snapshots taken before destructive changes, "Take one now", Restore.
`GET/POST .../backups`, `POST .../backups/{snap_id}/restore`.

---

## 3. Old options on the new engine

| Option (UI) | Then (Crafty) | Now (node agent) |
|---|---|---|
| Server record | Crafty server, absolute `path`, `executable` | panel DB row; `path` is `node:uuid`; `execution_command` still the startup command |
| Start / Stop / Restart / Kill | Crafty actions | same action names, sent to the node; stop is graceful (`stop` on the console, Docker stop only after the timeout) |
| Console | Crafty websocket | node WebSocket with a short-lived signed token; SSE and buffer fallbacks |
| Files | Crafty file API, rename only | node's jailed file API: search and usage on the node, tail reads, move/copy available, signed direct transfers |
| Stats | Crafty stats | node stats, CPU as a share of the allowance, memory against the limit, players from log + `list` + server-list ping |
| Java version | Crafty's Java path | `java_major` → the runtime image (Java 8/17/21/25); "Set the right Java" fix |
| Heap / flags | Crafty command edit | Tune writes the command, or `user_jvm_args.txt` when the command reads it |
| Orphaned server | Crafty lost the folder | node cannot read the folder or is not answering |
| Finish setup | — | retries the loader install on the node |
| Import from Crafty | live | one-time importer (`dev/tools/import_from_crafty.py`, DB importer) |
| CPU limit | one core by default (bug) | the node's cores unless a plan limit is set |
| Nodes | — | new: registered in the panel, run the agent; **no UI yet** |

---

## 4. What changed in this update that the UI must show

- **Install** returns a first-boot result and repairs (Discover finish card,
  Diagnose first-boot card, Mods `repair` badges).
- **Server packs are trusted**: review status `shipped`, no client-mod
  disabling on a server pack.
- **Optional pack mods** (`required:false`) arrive disabled, marked
  `switched_off_in_pack`.
- **Repair** can: disable a client-side mod a crash proves (including through
  Mixin's debug trail, language providers like GML, client-only mixins),
  disable a mod whose dependency is client-side, re-enable or install a
  missing library (CurseForge first, never a file the author blocked), set
  Java. It never undoes its own change in a session, never blames a library.
- **Players**: live count follows joins/leaves at once and is confirmed by
  server-list ping; sessions, source, version, world records.
- **spark** profiler and health; **perf** reading, JVM fixes, mod switches,
  pre-generation.
- **Stats semantics**: `cpu` share 0–100 of `cpu_allowance` cores; `mem_percent`
  of `mem_limit`; `created` is UTC with `Z`; `last_exit` says how it last
  stopped (an OOM kill included).
- **Out of memory**: the boot check names an OOM kill and the repair lowers
  the heap to fit (`fit_heap` repair, with "give the server N MB to keep the
  old heap"). Container sizes now leave room that grows with the mod count.
- **Containers**: the JVM's temp folder is `/server/.blessforge-tmp` (it will
  appear in the file list; consider hiding dot-folders by default).

---

## 5. Backend features with no UI yet

| Endpoint | What it is | Suggested home |
|---|---|---|
| `GET/POST /api/admin/nodes`, `GET /api/admin/nodes/{node_id}/system` | list/register nodes; registration returns the agent config **once** (it holds a secret token — show once, copy button, never again) | Settings › Nodes |
| `GET .../mods/dependencies` | which mod pulled in which | Mods › dependency view |
| `POST .../switch-pack-version` | move a pack install to another version | Overview / pack card |
| `GET .../logs?lines=` | log tail without the console | Diagnose › Logs |
| `POST .../files/transfer` | signed direct link for big files | Files (upload/download of worlds) |
| `POST .../mods/icons` | refresh mod icons | Mods |
| `POST .../ai/autofix`, `GET /api/ai/models`, `POST /api/ai/warm` | assistant extras | Diagnose / Settings |
| `GET /api/meta/categories` | catalogue categories | Discover filters |
| `GET /api/browse/modpacks/modrinth` | Modrinth pack search | Discover |
| `GET /api/instances/{id}/roll-export`, `GET /api/exports/{filename}` | downloads | Overview / Roulette |
| `GET /api/diagnose/dependency/{mod_id}` | look up a dependency | Diagnose |

**Blocked-mods helper.** The Mods tab lists the files an author blocks (each
with its CurseForge page) and takes the jars: `POST .../mods/blocked`
(multipart, one jar per request) matches each jar to the waiting file **by
content** (CurseForge fingerprint, SHA-1), files it under the name the pack
expects, and answers `{matched, name, uploaded_as, waiting}` — or 409 with
the list of what it is waiting for. The redesign should make this a real
**drop zone** (drop several jars at once, whichever row) on the Mods tab, the
install finish card and Diagnose.

---

## 6. Rendering bugs observed

Found in the browser pass on 2026-10-12 (local stack, Fabric 1.20.1 with
optimisation mods, two test clients connected). All fixed on this branch
unless marked open. The cause matters more than the fix — each one is a
pattern the redesign must not reintroduce.

1. **CPU "1223%" and "straining".** The node sent a core-sum (100 per core)
   and the UI showed it as a percent with thresholds at 60/85. Now a share of
   the allowance; the fleet card shows "N%" with the cores in the tooltip
   ("0.6 of 16 cores" did not fit its 46 px column).
2. **"added 6 h ago" for a server made a minute before.** `created` was a
   zone-less ISO string; the browser read it as local time (IST, UTC+5:30).
   Now `…Z`. Any new timestamp rendering must treat zone-less strings as a bug.
3. **Memory "85% of host".** The share is of the server's limit; now
   "84% of 2.8 GB".
4. **File search showed nothing, forever.** The box kept its text (the old
   caret bug was already fixed), but the request walked the tree a directory
   per request and ran past 45 s. Now one request on the node (0.4 s).
5. **Folder sizes column empty for minutes** — same cause, same fix.
6. **File names broke one letter per line at a 1024 px window** — fixed
   columns (22+26+96+132+96 px + gaps) sized by viewport queries, inside a
   486 px panel; the name got 24 px. Now container queries; the header row
   and data rows must share fixed column widths (an `auto` column per row
   misaligned the header).
7. **"Running the checks" / "Reading the player files" / "Measuring the
   host" / "Reading snapshots" stayed under the content for good.** The
   loading fox was mounted beside the content host instead of inside it.
8. **Tune's "How it is running" never appeared** until after an Apply (its
   load call sat in the wrong function).
9. **"Who is on" named two players while every count said 0.** A 30-second-old
   ping outranked the join lines. Counts and names must come from the same
   reading.
10. **"0/?" read as "0/7"** in the mono font (not a data bug — see §0.5).
11. **Open: Tune › Memory says "This machine's memory could not be read"** on a
    Windows dev box and proposes 6 GB for a 2 GB server. It measures the
    panel's host, not the node's; with a remote node it would measure the
    wrong machine. The node reports its own memory (`/api/admin/nodes/{id}/system`);
    the backend should switch to that.
12. **Open: slow readings.** `GET .../perf` ~1.7 s and `GET .../spark` ~2 s on
    a local stack. Design progressive loading, not a blank panel.
13. Earlier in this work: the **file search box cancelled after its first
    character** (focus lost on repaint) and **uploading a `.jar` asked to
    extract it, then failed** — both fixed and verified in the browser.

Historical, from `HANDOVER.md` §4–§5K (still binding): entrance from
`opacity:0` left content invisible; `countUp` from 0 lied; the polygon painted
through buttons; every modal was one stalled frame from invisible;
`confirmDialog` resolved false because it closed before deciding; a class token
with a trailing space threw and blanked a screen; the dark theme's accent was
white; the console arrived in clumps; the drawer stuttered.

---

## 7. Assets

**Already in `app/static/img/`** (served at `/assets/…`): `appmark-fox.png`,
`fox-head.png`, `fox-mascot.png`, `fox-loading.gif`, `chargey-failed.png`,
`chargey-peek.png`, `allay-helping.png`, `bee-working.gif`, `parrot-vibing.gif`,
`pig-loadbar.gif`, `shulker-guard.gif`, `creeper-delete-2x.png`,
`lucky-block.webp`, `sakura-light.svg`, `sakura-dark.svg`, loader logos
(`loader-fabric.png`, `loader-forge.jpg`, `loader-neoforge.png`,
`loader-paper.png`, `loader-purpur.png`, `loader-vanilla.svg`), source marks
(`src-curseforge.png`, `src-modrinth.png`). Icons are inline SVG paths in
`core.js` (`PATHS`, 60 names).

**What the redesign needs that is missing**

Logos (transparent PNG at 256 px, or SVG; light and dark variants where the
mark is dark):
- **Quilt**, **Folia** (it currently borrows Paper's), **Spigot**, **Bukkit**;
  optionally **Velocity** and **Waterfall** if proxies are ever offered.
- A higher-resolution **Forge** mark (the current one is a JPEG — no
  transparency).
- **CurseForge** and **Modrinth** marks from their official brand kits, in
  full colour and single-colour, so source pills read on both themes.
- Optional: **spark** and **Chunky** marks for the Profiler and pre-generation
  cards. If you use them, keep the credit line ("spark by lucko, GPL-3.0").

In-theme illustrations (fox / shrine style, transparent, ~2× display size):
- Mascot states: loading (exists), **empty** (no servers, nothing found),
  **error / crashed** (exists as chargey-failed — a fox version would match),
  **success / booted**, **repairing** (fox with a wrench or mending a talisman),
  **profiling** (fox with a stopwatch or a tide gauge), **pre-generating**
  (fox with a map or compass), **players** (fox at the torii greeting),
  **offline node** (a dark shrine), **blocked by author** (a sealed ofuda).
- Generic glyphs drawn in the theme for **Java / JVM** (please not the Oracle
  coffee-cup mark — it is a trademark), **node / machine**, **container**,
  **world**, **datapack**, **config**, **log**.
- App icon set: favicon 16/32/48, 180 px Apple touch, 192/512 px PWA, and a
  1200×630 social card.
- Backdrops: the light and dark scenes (`/assets/backdrop/{light|dark}` take
  an override image if one is dropped into `app/static/img/`).

Please avoid the Minecraft logo and Mojang artwork (brand guidelines). Loader
and platform logos are used to identify the software, which their projects
allow.

---

## 8. Checking a redesigned screen

1. `python dev/tools/check_frontend.py` — modules parse as modules, imports and
   assets resolve, every API path named exists on the server, icons exist,
   layering contract holds.
2. The offline test loop (`HANDOVER.md` §9).
3. A browser pass at 1440, 1024 and 375 px, in light and dark, with a running
   server: fleet, instance header, every tab, Discover install end to end, the
   Profiler (health + 30 s profile + download), Tune's reading, Files search,
   upload a jar, and two players joining (`dev/tools/test_players_live.py`
   covers the counting; a real join covers the screen).
