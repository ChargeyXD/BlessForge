# BlessForge — session handover

Written 2026-08-29, revising the 2026-08-27 original. Everything here was
verified on this machine, not recalled. Where something was *not* verified, it
says so.

Start a new session with: **"Read `~/blessforge/HANDOVER.md`, then <the thing
you want done."**

---

## 0. Where this is right now

**The front end is mid-rebuild.** A new interface, designed from scratch in
Claude Design, is replacing the old one. The shell and one screen are
finished; nine screens are placeholders that say so on screen.

**Nothing is committed.** Three sessions of work sits in the working tree —
`git diff` and `git status` are the change set. It is all deployed and running
(the container is built from the local tree, not from GHCR), and every test
passes, but no commit has been made. Committing is the user's call; ask.

**The next job is §5D: finish the remaining screens.** Read §4 for how a
screen is built, §5D for what each one needs, and `design/README.md` for the
places the design guessed wrong about this app — those corrections are not
optional, they are the difference between the UI telling the truth and
inventing numbers.

| | |
|---|---|
| Deployed | `http://<host>:8710`, healthy, ~70 MB under a 1 GB cap |
| Tests | **121 checks**, all passing (see §9) |
| Servers in Crafty | **none** — the fleet is empty, so instance screens have nothing to render against. Roll one from Discover → Mod Roulette. |

---

## 1. What this is

A self-hosted web app that installs CurseForge modpacks into **Crafty
Controller** and then manages those Minecraft servers: mods, config files,
`server.properties`, JVM tuning, crash diagnostics, a live server console, and
an LLM troubleshooting assistant running on a **remote** Ollama endpoint. One
person self-hosting servers for friends.

It talks to Crafty **only over Crafty's HTTP API** — it never touches Crafty's
files or its AppData directory. That matters more than it sounds; see §8.

- Repo: `~/blessforge` → `git@github.com:ChargeyXD/BlessForge.git` (branch `main`)
- Image: `ghcr.io/chargeyxd/blessforge:latest` (amd64 + arm64, built by
  `.github/workflows/docker-publish.yml` on every push to `main`)
- Runs on: `http://<host>:8710`

---

## 2. How it is deployed on this machine

| thing | value |
|---|---|
| container | `blessforge`, from `ghcr.io/chargeyxd/blessforge:latest` |
| compose file in use | `/var/lib/casaos/apps/blessforge/docker-compose.yml` |
| data | bind mount `/DATA/AppData/blessforge/data` → `/data` |
| Crafty | container `big-bear-crafty`, v4.10.8, `https://192.168.68.120:8443` |
| secrets | `~/blessforge/.env` (`CRAFTY_TOKEN`, `CRAFTY_URL`) |

**The CasaOS copy is not the repo copy.** The file under `/var/lib/casaos/apps/`
is generated from the repo's `docker-compose.yml` with three edits: the real
`CRAFTY_URL`, the real `CRAFTY_TOKEN`, and `pull_policy: never` so it runs the
locally built image. Regenerate it like this (the file is root-owned `600`, but
the *directory* is world-writable, so delete-and-rewrite works without sudo —
`sudo` on this host always prompts for a password and is unusable
non-interactively):

```bash
cd ~/blessforge
rm -f /var/lib/casaos/apps/blessforge/docker-compose.yml
python3 - <<'PY'
import pathlib
env = dict(l.strip().split("=", 1) for l in open(".env")
           if l.strip() and not l.startswith("#") and "=" in l)
c = open("docker-compose.yml").read()
c = c.replace('CRAFTY_URL: "https://CHANGE-ME:8443"', f'CRAFTY_URL: "{env["CRAFTY_URL"].strip(chr(34))}"')
c = c.replace('CRAFTY_TOKEN: ""', f'CRAFTY_TOKEN: "{env["CRAFTY_TOKEN"].strip(chr(34))}"')
c = c.replace("    image: ghcr.io/chargeyxd/blessforge:latest",
              "    image: ghcr.io/chargeyxd/blessforge:latest\n    pull_policy: never")
pathlib.Path("/var/lib/casaos/apps/blessforge/docker-compose.yml").write_text(c)
PY
chmod 600 /var/lib/casaos/apps/blessforge/docker-compose.yml
docker compose -f /var/lib/casaos/apps/blessforge/docker-compose.yml -p blessforge up -d --force-recreate
```

**Why that path.** CasaOS decides an app is *its own* purely by where the
container's compose project file lives. A container created by `docker compose
up` from a home directory shows up as a **legacy / unmanaged** app no matter
how complete its `x-casaos:` block is. Check with:

```bash
docker inspect blessforge --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
# must print /var/lib/casaos/apps/blessforge/docker-compose.yml
```

### Build and redeploy loop

```bash
cd ~/blessforge
docker compose build                      # docker-compose.override.yml adds build: .
docker compose -f /var/lib/casaos/apps/blessforge/docker-compose.yml -p blessforge up -d --force-recreate
curl -s http://127.0.0.1:8710/api/health | python3 -m json.tool
```

`docker-compose.yml` is the file CasaOS imports (pulls GHCR, two edits needed).
`docker-compose.override.yml` is picked up automatically from a checkout and
swaps in `build: .` plus a raw-format `.env` read. CasaOS never sees the
override.

---

## 3. Repository layout

```
app/
  main.py         1758  FastAPI: every route, static serving, cache-busting fingerprint
  installer.py    1281  the install pipeline; loader wait + repair (§6.3)
  diagnostics.py  1165  health checks, crash-log parsing, findings
  crafty.py        794  Crafty API client — the only thing that talks to Crafty
  mods.py          641  mod listing, toggle, delete, identify
  ai.py           1114  Ollama client, action vocabulary, re-validation
  packs.py         468  pack plans, archive shape detection, loader mapping
  preflight.py     522  client-only mod review
  properties.py    422  server.properties model
  specs.py         430  host specs, heap sizing
  curseforge.py    415  CurseForge API + download cache
  deps.py          332  recursive dependency resolution
  modrinth.py      232  Modrinth API
  jarmeta.py       232  jar fingerprinting
  uploads.py       246  imported .zip exports
  roulette.py     1030  Mod Roulette: pool, seeded deal, export  [2026-08-28]
  optimizer.py     222  Aikar flags, heap, property presets
  jobs.py          266  job model + SSE
  configs.py       168  config file tree
  config.py        164  env parsing, paths
  static/         index.html (86) · app.js (1367) · style.css (696)
  static/img/     the design's assets, downscaled — see design/README.md
dev/              test tooling — see dev/README.md
design/           the Claude Design canvas the new UI is built from, and its
                  README — READ IT, it lists what the design got wrong about
                  this app. The previous front end is in git history, not here.
design-prompt.md  the brief that produced that design  [2026-08-28]
design-brief.md   HISTORICAL. Described the front end replaced on 2026-08-29;
                  its §4.3 DOM contract no longer applies to anything. Kept
                  only because it explains why the old UI looked as it did.
entrypoint.sh     starts root, chowns /data subdirs, drops to uid 1000
```

Backend is FastAPI + httpx, no database. State lives in Crafty (a
`crafty_managed.txt` manifest per instance) and in `/data`.

---

## 4. Front end — the rules that are not obvious

**Three files, no build step.** `app/static/{index.html,style.css,app.js}` are
served verbatim. No npm, no bundler, no framework — plain ES2020 in one IIFE,
the web fonts loaded non-blocking (`media="print"` swap) so a LAN box with no
route out still paints instantly and stays legible without them.

**Do not add a fourth *code* file.** `main.py`'s cache-busting fingerprint only
hashes and rewrites those three names; a fourth would be served stale forever.
`static/img/` is fine — images are addressed by name and do not change.

**`index.html` is a frame, not the app.** It holds the spine, the topbar, the
canvas and four empty hosts (`#drawerHost`, `#overlays`, `#toasts`, `#tabs`).
Every screen is built in JavaScript and rendered into `#canvas`.

**A screen is an entry in `RENDER`, keyed `view:tab`** (`instance:mods`,
`discover:roulette`), returning a detached element. `go()` swaps it in.
Screens that need to find themselves by id set `el.__mount`, which runs
**after** the element is in the document — doing that work in a microtask
finds nothing, because the element is still detached.

**Screens must clean up after themselves.** `onLeave(fn)` registers a
teardown that `go()` runs on the way out. A console stream or an observer left
running is invisible until it is polling Crafty forever.

**Everything interpolated goes through `esc()`.** Log lines, crash reports,
mod names and config files are all attacker-adjacent text.

**A job owns its stream; a view onto it is disposable.** "Run in background"
closes the drawer, not the `EventSource`. Only the explicit `end` frame ends a
stream — a job also emits a final step whose status already reads `done`, and
treating that as the end closes the connection one frame before the result
arrives (§5.1).

**The design system lives in `:root`.** Colours, type, spacing and radii are
tokens; the two font families carry meaning (mono for anything a machine
produced, Space Grotesk for prose) and that split is the visual system rather
than a preference.

## 5. What changed on 2026-08-27/28 — the nine reported faults

Nine faults the user reported, plus three new features. Sections 5A–5D cover
what came after. **None of it is committed** (§0).

### 5.1 The bug behind "the review never creates an instance"

One missing field, and it explains a feature that appeared to do nothing.

Every job emits an `end` event when its worker returns. That payload carried
status but no **result**. Worse, the runner sets `status = "done"` and *then*
calls `set_step("Finished")`, so a **step** event already reporting `done`
reached the browser first — and the browser closed its `EventSource` on the
first frame that said the job was over. So `showReview(undefined)` threw,
inside a `catch` that logged to a console nobody had open.

The job succeeded. The log was perfect. The feature silently did nothing.

Fixed at both ends: `Job.emit` attaches `result`/`error` to any frame whose
status is terminal (keyed on status, not on the event name, so no future emit
between "finished" and "end" can reintroduce it), and the browser now closes
only on the explicit `end`. `dev/tools/test_job_stream.py` is the regression
test — it caught the second half of this bug after the first half was fixed.

This affected **every** job whose result drives the next screen: the
client-only review, AI analysis, deep scans.

### 5.2 Health checks that cried wolf about mod versions

The version check was a filename scan: any jar whose name contained a
version-shaped token that was not the instance's version got reported. On this
machine that was **22 mods on AllOfCreate and 39 on Better MC** — every one a
false positive, because `alexsmobs-1.22.9.jar` and
`coroutil-neoforge-1.21.0-1.3.9.jar` carry their own version numbers, not the
game's.

The filename is now only a *candidate filter*. Nothing is reported until the
claim is checked against what the publisher says the file supports, via two
bulk calls (CurseForge `get_files`, Modrinth versions) for the whole instance.
Confirmed mismatches are an `error` naming the declared versions; jars that
cannot be checked get one `info` line that says so honestly instead of
accusing them. Both instances now report **zero**, and a deliberately
mismatched run (1.20.1 jars, instance told it is 1.21.1) still reports all 60.

### 5.3 Client-only mods: disabled and tagged, not deleted

- The review's decisions now travel as `disable_files` (+ `client_reasons`),
  and the installer writes those jars as `<name>.jar.disabled` with
  `client_only: true` and the evidence recorded in the instance manifest. The
  Mods tab tags them **client-side**, explains why on hover, and says they are
  safe to re-enable. `exclude_files` still deletes, for anyone who wants that.
- **Unticking "Review client-only mods" now means it.** The front end never
  sent `skip_client_only`, and the backend defaulted it to `true` — so the
  unticked path stripped client mods anyway. It sends `false` now.
- **Why Figura got through.** Modrinth states sides outright and is the
  strongest signal available, but it was only consulted for mods some *other*
  signal had already flagged — so it was never asked about the mods nobody had
  thought to put on a name list. Every jar is now looked up **by SHA-1 of the
  file** before anything is flagged (two bulk calls per pack). Verified
  directly: Figura's real jar hash resolves to `server_side: unsupported`,
  confidence `declared`. On the repo's own `CustomModpackexport.zip` (301
  mods) the name list finds 2 client mods; the hash check finds 6 — four that
  nothing had suspected, including `fusion` and `sodiumoptionsapi`, both
  correctly held back as `keep` because other mods depend on them.

### 5.4 The port

Every instance on this machine is on 25565, whatever was typed at install.
Crafty is told the port at creation and writes it into a fresh
`server.properties` — and then the pack's overrides land on top, and most packs
ship a `server.properties` saying 25565.

The port is now written **after** the overlay, through `properties.set_port`,
so Crafty's record and `server.properties` cannot disagree.
`dev/tools/test_install_decisions.py` pins the ordering.

### 5.5 Crash logs that name the mod

`diagnostics.attribute_crash` reads the whole report and ranks the jars it
implicates:

| score | signal |
|---|---|
| 100 | the loader wrote a per-mod failure block naming the jar |
| 90 | listed under `Suspected Mods` |
| 80 | one of its mixins failed to apply |
| 70 | it loaded a client-only class on a dedicated server |
| 40 | its classes own several frames of the stack |

Both loaders are parsed — Forge writes `-- MOD x --` with `Mod File:`,
NeoForge writes `-- Mod loading issue for: x --` with `Mod file:` and puts the
sentence that explains the crash on the *continuation* line. Every attribution
carries the line it came from, and all reasons are kept, not just the
strongest.

It also knows when the mod is not the culprit. On the real crash in ATMons,
Cobblemon refused to load because the instance runs Java 25 and the pack needs
21 — so it reports a Java fault and says outright that disabling Cobblemon will
only move the failure to the next mod that checks.

Health checks now surface this automatically whenever a crash report exists.

### 5.6 The assistant moved off this machine

`OLLAMA_URL` now defaults to `https://ai.shadowco.xyz`, model
`qwen3:4b-instruct` (2.5 GB, Q4_K_M, no thinking preamble, 256k context),
pulled and verified. `OLLAMA_API_KEY` is supported. New: **Review Crash Log**
(whole-report analysis, culprits merged with the regex pass) and **Review &
Fix** (applies only the reversible half of the vocabulary — EULA, Java, heap,
disable; never delete). Filenames the model mangles are reconciled against the
real file list rather than discarded.

**The one constraint that shapes all of it:** that endpoint evaluates prompts
at ~90 tokens/second and sits behind a proxy allowing 120s to first byte.
Streaming does not help — nothing is emitted until the whole prompt is read. So
evidence is *selected*, not truncated: 686k characters of crash + log + a
369-mod inventory becomes a 6.8k prompt naming the six jars the log actually
mentions. A first attempt that sent everything died with a Cloudflare 524.
`_size_context` also self-corrects, retrying once at the size Ollama reports it
needs.

### 5.7 Add-mod: live search, no dependency gate

- Results appear as you type (260 ms debounce, out-of-order responses
  discarded, spinner only if the answer is slow enough to warrant one). The
  Search button is gone.
- The dependency preview is gone. The resolver already skipped what the
  instance had, so the preview's only real output was three extra clicks;
  mods install with their dependencies directly. What it showed is recorded as
  mods are added and available on demand under **Dependencies** on the Mods
  tab.

### 5.8 Terminal tab

Live console per instance. `GET /api/instances/{id}/console/stream` polls
Crafty and forwards only new lines (the backend diffs, so a reconnect does not
repaint and the wire carries one line rather than five hundred);
`POST /api/instances/{id}/command` types into the server. Follow pauses on
scroll-up and resumes at the bottom; the stream is torn down when the tab is
left, or a forgotten `EventSource` would poll Crafty for the life of the page.

Crafty's `/logs` endpoint runs lines through `html.escape` unless `raw=true`,
and `raw=true` also stops it stripping ANSI — so ANSI is stripped in
`crafty.py` and the browser escapes. A log line containing `<img src=x
onerror=...>` is covered by a test.

### 5.9 Activity names its instance

Jobs carry `server_id`/`server_name` (set at creation, or mid-flight by
`job.set_instance` once Crafty has made the instance). Activity cards show a
chip that opens the instance.

### 5.10 Tests

`dev/tools/test_job_stream.py` (10) and `dev/tools/test_install_decisions.py`
(20, grown since). `dev/ui-tests/04-terminal-and-review.mjs` was written here
and retired with the front end on 2026-08-29 — see §5C. The suite today is
**121 checks**; §9 runs it.

`app.js` exposes `window.__bf` — a deliberately small test seam for entry
points with no clickable path that does not also depend on a live CurseForge
search returning a particular pack. Nothing in the app reads it.

---

## 5A. Memory: why it used ~2.3 GB, and what changed (2026-08-28)

Measured, not guessed. Idle baseline is **55 MB**; everything above that was
workload that never came back.

The install pipeline held bytes at every stage: the pack archive was
`read_bytes()` into a `BytesIO` (the Better MC server pack in this machine's
cache is **983 MB**), every downloaded jar was kept in a list until the last
upload batch finished (**2.8 GB** of jars are cached here), and each batch was
assembled in a `BytesIO` and then copied again by `getvalue()`. Freed memory
also stayed resident, because glibc does not return arenas on `free()`.

Now: `fetch_pack_archive` returns a **path** and `zipfile` reads members from
disk; `curseforge.cache_jar` returns a path instead of bytes; a `PackEntry` is
a *reference* (cache path, zip member, or bytes only for generated files);
batches are written to a scratch file and streamed by `crafty.upload_path`;
and `jobs.release_memory()` runs `malloc_trim` after heavy jobs, with
`MALLOC_ARENA_MAX=2` in the Dockerfile.

| measurement | before | after |
|---|---|---|
| open the 983 MB server pack + stream its 40 largest members | **986 MB** | **4 MB** |
| preflight, 301 mods, warm cache (peak / settled) | 224 / 101 MB | 203 / **73 MB** |
| install, 91 mods, 260 MB uploaded (peak / settled) | — | 379 / **65 MB** |
| idle | 55 MB | 55 MB |

`docker-compose.yml` now sets **`mem_limit: 1g`**. That is a ceiling, not a
target — verified that a 301-mod preflight peaks near 205 MB under it without
an OOM kill. Raise it before installing packs far larger than that; the
container is killed, not throttled.

**A separate RAM bug, on the server side:** the tuned heap was only ever
written for Forge/NeoForge, which read `user_jvm_args.txt`. Fabric, vanilla
and Paper keep their JVM args in Crafty's `execution_command`, and nothing
rewrote it at install time — so a Fabric pack that asked for 6 GB kept 6 GB
regardless of the host. The Optimize tab already did this correctly
(`optimizer.set_command_memory`, previously private); the installer now calls
the same function rather than growing a second copy.

---

## 5B. Mod Roulette (2026-08-28) — backend, and its corrections

`app/roulette.py` + seven endpoints under `/api/roulette/*`. It builds a
modpack nobody chose: set constraints, pull a lever, get a hand, accept it and
BlessForge installs the server **and** hands back a CurseForge export zip.

**The seed contract is the feature.** A roll is identified by a short seed
(`QRT-8KM-4Z`, alphabet minus I/L/O/0/1 so it survives being read aloud), and
the same seed with the same constraints deals the same hand. The generator is
a port of the design prototype's JavaScript, pinned against its output in
`dev/tools/test_roulette.py` — change one half without the other and every
seed anyone has shared stops reproducing, silently.

Where the design's guesswork needed correcting:

- **It dealt 5–8 mods.** `want = max(5, min(8, count/18))` was an artifact of
  its 30-mod mock pool. A hand is now `count` mods (default 120).
- **Its draw depended on array order**, so refreshing the pool reshuffled
  unrelated picks and shared seeds decayed. Each mod is now scored
  independently from `seed + constraints + its own identity` and the top
  scores win; preference is `u ** (1/weight)`, the same thumb on the scale
  three tickets gave. A 30-mod catalogue update now disturbs nothing.
- **The quality floor only applied above 5** while its label read "5M dl" at
  5. It filters at every value now.
- **`latestFiles` is not a source of truth.** It frequently has no build for
  the version being asked about, and an early draft's fallback to `files[0]`
  put a *1.16.5 Forge* jar in a 1.21.1 NeoForge pool. Every mod in a dealt
  hand now has its real build resolved (`resolve_hand`), which is also where
  the true file size — and therefore the HEAVY flag and the heap estimate —
  comes from.
- **Client detection was impossible for a CurseForge-only roll.** Forge and
  NeoForge jars declare no environment, so nothing caught ItemZoom or Mouse
  Tweaks. CurseForge ships each file's SHA-1, so the hand is now settled
  against Modrinth by hash in two bulk calls — the same mechanism the
  pre-install review uses, and exact rather than a guess from a name.

**Verified live**, not just offline: a 3,035-mod NeoForge 1.21.1 pool (79 s
cold, cached 3 days, ~2.5 s per roll after); a 12-mod roll installed end to
end into `afbd2cef…` on port 25572 in 55 s; dependency resolution turning 8
rolled mods into 17 jars with correct attribution; and the export zip
re-imported by BlessForge's own `packs.analyse_archive`. Peak memory building
a fresh pool: **80 MB** under the 1 GB cap.

The screen that drives all of this was built the next day; see §5C.

---

## 5C. The UI rebuild (2026-08-29) — shell + roulette done

The front end is being replaced from `design/BlessForge.dc.html`. What exists:

**A new shell.** A fleet spine on the left (a card per server, with a state
rail, pack, loader/version/port and live CPU/memory bars), a topbar carrying
the tab strip for whatever is open, a canvas, a job drawer, and a ⌘K command
palette that reaches any server or any section of one.

**Screens are rendered by JavaScript into `#canvas`**, not held in
`index.html`. There are fourteen of them and most are entirely API-driven; as
markup the shell would be two thousand unreadable lines. `index.html` is now
86 lines and holds only the frame.

A screen is a function in `RENDER` keyed `view:tab`, returning an element.
If it needs to look itself up by id it sets `el.__mount`, which `go()` calls
**after** attaching — doing that work in a microtask finds nothing, which cost
an hour to spot.

**Done:** the shell, systems panel, palette, job drawer, toasts, sheets, and
**Mod Roulette** end to end (27 jsdom checks against the live backend).

**Not done:** Situation, Diagnose, Mods, Tune, Console, Configs, Activity,
Catalogue, Import. Each is a `soon()` placeholder that says so on screen.

Corrections made to the design's guesswork are listed in `design/README.md`.
The two that needed backend work: `/api/health` now measures Crafty's
round-trip latency and free disk, and `/api/instances` returns a computed
`state` (`running`/`stopped`/`crashed`/`orphan`/`incomplete`) so no surface
has to infer one. `incomplete` is real: the installer now stamps a manifest
with `complete: false` the moment Crafty creates the instance and sets it true
only at the end, so a half-finished install is visible instead of looking like
a healthy server with no mods.

A roll is now a **job** rather than a plain response: dealing 120 mods means
pinning 120 real builds, and fifteen seconds behind a bare spinner reads as a
hang. It streams `Pinning builds (40/85)` instead.

---

## 5D. What is left: nine screens

Each is a `soon()` placeholder in `app.js` today. Build them one at a time,
deploy, and drive each with a jsdom harness before moving on — that loop is
what caught every bug worth catching in the last three sessions.

The design source for all of them is `design/BlessForge.dc.html`; find a
screen by its `data-screen-label`. **Read `design/README.md` first** — it lists
what the design invented, and those inventions are copy-paste-ready mistakes.

### The order I would take them in

**1. Situation** (`data-screen-label="Situation"`) — the landing screen for a
server, and the one that makes the fleet worth clicking into. Needs
`GET /api/instances/{id}`, `/diagnose` for the "needs you" list, `/port`, and
`DELETE /api/instances/{id}?files=`. Its distinctive idea is that removing a
server is **two different buttons, deliberately not one**: forget the record,
or delete the world. The second demands the name be typed — `confirmSheet`
already supports `typeToConfirm`. This is also the only place the UI can offer
to clear an `orphan`, which is the state the user's own machine has hit twice.

**2. Mods** (`"Mods"`) — the densest screen and the most used. `GET
/mods`, `/mods/toggle`, `/bulk-toggle`, `/delete`, `/add`, `/identify`,
`/updates`, `/dependencies`, and `/browse/mods` for the add-mod search.
Carries over from the old UI, all of it still true and all of it in
`retired/02-mod-list.mjs` and `retired/04-terminal-and-review.mjs`:
virtualise above ~120 rows at `--row-h` (44 px in the new design), keep
selection in a `Set` of filenames rather than in the DOM because a scrolled-out
row is unmounted, bind events once on the container, and patch a toggled row in
place rather than repainting the list. Client-only mods must show the
**evidence** for the tag, not just the tag.

**3. Diagnose** (`"Diagnose"`) — `/diagnose`, `/crash-review`,
`/ai/crash-review`, `/ai/analyse`, `/ai/autofix`, `/ai/apply`, `/deep-scan`,
and the `/fix/*` family. The design's hero is a single ROOT CAUSE with a
confidence bar, then the blame list, then the assistant. That maps cleanly onto
what `crash_review` already returns. Two things it gets right and must survive:
every claim carries its evidence line, and the model's proposals are split into
what will be applied and what is held back for approval.

**4. Console** (`"Console"`) — `/console`, `/console/stream` (SSE),
`/command`. Straightforward, and `retired/04` has the assertions: severity
colouring, a filter over everything received, follow that pauses when you
scroll up, and a command box disabled with an explanation when the server is
stopped. **The design's "websocket · 41 ms" is wrong** — say what the stream
actually is.

**5. Tune** (`"Tune"`) — `/optimize` (GET plan, POST apply), `/properties`,
`/port`, `/host/specs`. Sub-navigation down the left with live values. The
port card is the good part: three sources that can disagree, named, with one
button that makes them agree.

**6. Configs** (`"Configs"`) — `/configs`, `/configs/read`, `/configs/write`.
The old editor's rules still hold: the gutter is a plain `<pre>` sharing the
textarea's font metrics with `scrollTop` mirrored, and there is a guard against
navigating away dirty. No highlighting overlay — it drifts on wrap.

**7. Activity** (`"Activity"`) — `/jobs`, and re-attach through `follow()`.
Small, and the job machinery is already built.

**8. Catalogue** (`"Discover"`, the search half) — `/browse/modpacks`,
`/modpacks/{id}/files`, `/install/preflight`, `/install/modpack`. This is where
the **client-only review** overlay lives (`"Client-only review"`), and that
overlay is the one screen the whole app is arguably for. `retired/04` asserts
the contract: it sends `disable_files` and `client_reasons`, never
`exclude_files`, because mods are installed disabled and tagged rather than
stripped.

**9. Import** (`"Discover"`, the import half) — `/uploads/modpack` with real
byte progress via `XMLHttpRequest` (`fetch` still cannot report upload
progress), `/uploads`, and re-import into an existing server through
`/switch-pack-version`.

### Then

- Port each retired harness as its screen lands, and delete it from
  `dev/ui-tests/retired/`.
- The light theme has never been looked at (§7.1). Every colour is a token on
  `:root`, so it is a `[data-theme="light"]` block, but it is real work.
- 360 px has never been looked at either. The breakpoints exist in §8 of
  `style.css`; nobody has opened them.

---

## 6. The session before that (8 commits, `23b38e7..c62d607`)

### 6.1 `22a2d70` — import modpacks exported from the CurseForge app

A self-hoster's pack is usually one they built themselves, and a catalogue
search can never find it. Uploading a profile export now goes through the same
preflight review and install path. The three install endpoints take **either**
`{mod_id, file_id}` **or** `{upload_id}`, never a mix.

Two archive-shape traps handled: a client export's `overrides/mods/` looks
exactly like a server pack (manifest is checked first now), and jars bundled
in `overrides/mods` have no project id so they can never be version-checked —
surfaced as `bundled` in the review rather than treated as catalogue mods.

### 6.2 `6203473`, `5a5fb19`, `88b9535` — front-end rebuild + packaging

Full replacement of `index.html` and `style.css` from the Claude Design export,
with `app.js` rewritten to emit the new markup. **Every** §4.3 hook survives
unchanged — the rename table is empty by design. Added: windowed mod list,
Optimizer sub-navigation with an `IntersectionObserver`, job phase strip and
completion summary, Watch/re-attach, light theme with a persisted toggle,
config gutter and dirty markers, tab counts, skeletons, real empty states.

Packaging: `docker-compose.yml` became the single file CasaOS imports (pulls
GHCR, full `x-casaos` block); building moved to `docker-compose.override.yml`;
`casaos-compose.yml` and its "use the other file" trap are gone. The store icon
was referenced but had never existed — `app/static/icon.png` is generated by
`dev/tools/make_icon.py`.

### 6.3 `5cbaadd` — the `/data` disaster

CasaOS's install dialog renders a **named volume** as a `/data` row with an
empty host path, and whoever clicks through fills the blank in by hand. On this
machine that put `/data` on `/DATA/AppData/big-bear-crafty`. The app runs as uid
1000, that directory is root's, and so every cache write, download and import
failed — silently. Preflight still finished; the install just had nowhere to
put anything.

Three parts to the fix: the compose file names the path outright; the container
starts as root, chowns only `cache/downloads/uploads` and drops to uid 1000 via
`setpriv` (`entrypoint.sh`); `/api/health` probes the cache directory and the
setup banner grows a row when it fails.

It deliberately does **not** chown `DATA_DIR` itself — pointing the mount
somewhere wrong should not become someone else's outage. A mount that looks
like a Crafty install is named in the log.

### 6.4 `eb65549`, `0a8cc85` — "empty server, no launcher"

Crafty creates a server, returns **201**, and downloads the loader jar
*afterwards* on a daemon thread. When that download fails it writes one line to
its own console and stops:

```
ERROR - file_helpers  - SSL File Get - Maximum retries reached. Download failed.
ERROR - import_helper - Unable to save jar to .../forge-installer-1.20.1.jar
```

Nothing reaches the API. The record still names an `executable`, the directory
holds only `eula.txt` + `server.properties`, and BlessForge waited out the full
900s `SERVER_READY_TIMEOUT` because it treated any non-empty
`execution_command` as progress — and Crafty sets that command *at creation
time* to run the installer.

`_wait_for_loader` now knows what finished looks like, and repairs rather than
waits:

1. Forge/NeoForge are installed only once Crafty has **replaced** the
   `--installServer` command; other loaders once the named jar exists.
2. If the jar has not appeared within `LOADER_DOWNLOAD_GRACE` (75s — Crafty's
   own downloader gives up in ~17s), or nothing in the directory has moved for
   `LOADER_STALL` (180s, catching a jar truncated mid-stream), repair:
   fetch the jar from `GET /api/v2/crafty/JarCache` (Crafty's own index — same
   bytes, **sha256 verified**), upload it, start the server once so the
   already-configured installer command runs, then rewrite `executable` and
   `execution_command` from the generated `run.sh` exactly as Crafty's own
   post-install step does. Skipping that last part would make the next start
   re-run the installer instead of the server.
3. If repair fails too, the job fails with the reason instead of timing out
   quietly.

Verified live against the instance this had stranded: jar fetched, installer
ran ("The server installed successfully"), resulting command identical in shape
to a hand-made server.

### 6.5 `c62d607` — dev tooling

See `dev/README.md`. Three jsdom harnesses (57 + 20 + 39 checks), a
headless-Chrome screenshot pass, an offline unit test for the loader state
machine, and `sweep_instances.py` which flags any instance whose executable is
missing from disk.

---

## 7. Open issues — the likely reason you are reading this

### Not verified (do this before trusting it)

1. **The new front end has only been seen through jsdom.** Nothing has been
   looked at in a browser. The layout, the spacing, the colours in practice,
   the animations, whether the spine holds fourteen servers gracefully — all
   unverified. jsdom asserts structure and behaviour, not that it looks right.
   `dev/ui-tests/screenshot.mjs` drives headless Chrome and is the fastest way
   to find out.
2. **Light theme does not exist yet.** Every colour is a token on `:root`, so
   it is a `[data-theme="light"]` block and nothing more — but nobody has
   written it, and the design only ever showed dark. There is also no toggle in
   the new shell.
3. **360 px has never been opened.** The breakpoints are in §8 of `style.css`
   and are guesses.
4. **The terminal has only been driven against a stopped server** — and its
   screen has not been rebuilt yet in any case. The console *read* path is
   verified live (70 buffered lines came back from a real instance), and the
   old stream/filter/echo logic was covered by jsdom against a fake
   EventSource, but nobody has watched output scroll from a *running* server
   and `POST /command` has been sent to one exactly once, successfully
   (`list` → `There are 0 of a max of 20 players online:`).
5. **Review & Fix has never actually applied anything.** The plan half is
   verified end to end against a real crash; `_apply_actions` is the same code
   path `/ai/apply` has always used, but the automatic route has not been
   watched making a change.
6. **A full modpack install is verified** (2026-08-28, "Enhanced Terrain",
   Fabric 1.21.1, 91 mods: preflight → review → install → booted in 11.8 s →
   `list` through the terminal → stopped; port 25571 written to all three
   places and reachable over TCP; 32 client-only mods installed `.disabled`
   with their evidence). **A rolled install is verified too** (§5B). Both test
   servers were deleted on 2026-08-29 at the user's request, which is why the
   fleet is empty.

### Mod Roulette — what is not done or not proven

18. ~~**There is no UI.**~~ Built on 2026-08-29 (§5C) and covered by
    `dev/ui-tests/05-roulette.mjs`.
19. **A pool costs 80–100 seconds to build cold** (about 110 catalogue
    requests) and is cached for three days per (version, loader, source).
    The first roll on a version nobody has tried is therefore a wait with no
    progress on screen until the UI renders the job. Rolls after it take
    ~2.5 s.
20. **A seed reproduces a hand against the pool it was dealt from.** Scoring
    per mod makes that robust — a 30-mod catalogue update disturbed nothing
    in testing — but a mod leaving the catalogue, or the version/loader
    changing, will change the hand. The export records the seed so a pack is
    reproducible even when the roll is not.
21. **`server_side` is only known for mods Modrinth has.** A CurseForge
    exclusive that is client-only and not on the curated name list can still
    reach a hand. It is then caught at install by the ordinary client-only
    review, one step later than ideal.
22. **The odds panel's heap estimate is a heuristic** (`1.5 + jars * 0.018 +
    heavy * 0.35`), calibrated against this machine rather than measured.
    It is honest about being an estimate; it is not a model.
23. **`install_roll` has no rollback.** If the install fails half-way the
    export is already written and the instance already exists, same as any
    other failed install (§7.7).

### The rebuilt front end — known gaps

24. **Nine screens are placeholders** (§5D). They render a card saying so
    rather than pretending to work.
25. **`app.js` is one 1,367-line file and growing.** It cannot be split — the
    cache-busting fingerprint hashes exactly three names (§4) — so the only
    lever is keeping sections marked and screens small.
26. **No screenshot has ever been taken of it.** See §7.1.

### Known rough edges

5. **The `jobs` Map in `app.js` is never pruned.** Every job a tab has seen
   keeps its full log buffer until reload. Harmless over an evening, unbounded
   in principle. (Carried over from the old front end, which had the same
   shape under the name `jobRegistry`.)
6. **Selection state must not live in the DOM on the Mods screen.** The old
   implementation kept it in a `Set` of filenames because a virtualised row
   that scrolls out is unmounted and loses its checkbox, and pruned that Set
   against the new instance's file list on load — which meant a mod with the
   *same filename* in two instances could appear pre-selected. Narrow, but
   real, and worth fixing rather than reproducing when §5D rebuilds it.
7. **`_wait_for_loader` raises on timeout, leaving the created instance
   behind.** Deliberate (you may want to retry into it) but failed installs
   accumulate orphan instances. Consider offering deletion in the failure
   summary.
8. **`/api/health` writes a probe file every call** (every 45s from the UI).
   Negligible I/O, but it is a write on a health check.
9. **The terminal polls Crafty every 1.5s per open tab.** Cheap, but it is a
   poll, and two browser tabs on the same instance double it. It stops when
   the tab is left.
10. **`_version_mismatch_findings` only checks mods it can identify.** A jar
    installed outside BlessForge and never run through Identify falls into the
    honest `info` bucket rather than being checked. That is the right trade —
    the alternative is the false-positive storm it replaced — but it does mean
    a genuinely wrong-version unidentified jar goes unreported until Identify
    runs.
11. **Crash attribution is regex-driven and loader-specific.** Forge and
    NeoForge block formats are both handled and tested; Fabric's mod-resolution
    errors are covered only through the generic patterns. A loader that changes
    its crash-report wording will quietly stop being parsed — the AI half still
    answers, which is exactly why both halves exist.
12. **`craftyservercreationlog.har` (8 MB) is committed** — it went in with a
    `git add -A` in `eb65549`. **No credentials in it** (checked: no
    `Authorization`, no cookies, no JWTs, no CurseForge key), but it contains
    LAN IPs and server names, and it bloats the repo. Removing it from HEAD
    does not shrink existing clones; history rewrite would.
13. **The CurseForge API key is in the public repo**, base64-encoded in
    `docker-compose.yml`. Base64 is encoding, not encryption. This predates
    these sessions and appears deliberate (the friend's install needs it), but
    treat the key as disclosed and rotate if that is not intended.
14. **`ai.shadowco.xyz` is now a hard-coded default** in `config`, compose and
    `.env.example`. It is someone's endpoint, reachable without a key at time
    of writing. If that changes, `OLLAMA_URL` and `OLLAMA_API_KEY` are the
    knobs; a blank `OLLAMA_URL` disables the assistant and nothing else
    breaks.

### Servers deleted 2026-08-28 — explained, not a fault

Between **01:10 and 01:20 IST** every server directory under
`/DATA/AppData/big-bear-crafty/data/servers/` disappeared, except ones created
afterwards. Crafty's log (UTC; host is IST = UTC+5:30) shows:

- `19:50:43Z` — `POST /api/v2/servers/<id>/files` already failing for **every**
  server. The files were gone by then.
- `19:59Z` — an install into `3deb933b…` nonetheless succeeded, so the mount
  was present and writable, just empty.
- `20:20:49Z` — Crafty restarted and logged
  `Unable to find server <name> at path /crafty/servers/<id>. Skipping` for all
  of them. It has no server objects for them since, so its API answers **500**
  and BlessForge cannot open them.
- `20:16–20:22Z` — six servers deleted through Crafty's panel, *after* the
  files were already gone.

The data is not recoverable from this machine: one ext4 filesystem, nothing
unmounted or hidden, and `data/backups/` holds nothing but the 2026-08-28 test
instance.

**Cause: the operator removed the instances by hand and the removal was
incomplete** -- the server directories went first, leaving Crafty holding
records it could no longer open. Confirmed by the owner. Nothing in BlessForge
was involved: its entire deletion history in Crafty's audit log is
`.studio-batch-*.zip` temp files and nothing else, and it has never issued a
server delete.

Worth keeping only for the failure *shape*, which is now handled: a Crafty
record whose directory is missing makes Crafty answer 500 for that server
forever after its next restart. Delete instances through Crafty (or
BlessForge), not by removing directories.

Twelve Crafty DB records survive with no files behind them. The UI no longer
breaks on them (`openInstance` failing leaves an explanation instead of an
unhandled `TypeError` in a tab loader), but they can only be tidied up in
Crafty itself.

### Environment hazards

15. **Editing the app in the CasaOS UI rewrites
    `/var/lib/casaos/apps/blessforge/docker-compose.yml`** — comments stripped,
    root-owned `600` again, volumes replaced with whatever the dialog held. It
    also **deleted the old named volume**, taking previously imported `.zip`
    exports with it. After any UI edit, re-check the mount:
    `docker inspect blessforge --format '{{range .Mounts}}{{.Source}}{{end}}'`
16. **The instance "Better MC [FORGE] BMC4" (`67da752c…`) has a working Forge
    but no mods** — its install died before that step and was repaired by hand.
    Re-run the pack install into it or delete it.

17. **`pkill -f "uvicorn app.main:app"` kills the container too.** Docker
    processes are visible in the host PID namespace, so a broad `pkill` while
    running a local dev server on 8719 takes down the deployed app as well.
    It restarts itself, but kill by port instead:
    `ss -lptn 'sport = :8719'`.
---

## 8. Traps that will bite you (all confirmed on this machine)

**Crafty**

- **EULA.** Crafty compares `eula.txt`'s first line via `readline()` against an
  exact list. `readline()` keeps the trailing newline, so `"eula=true\n"` never
  matches — Crafty then refuses to launch, writes **no log and no error**, and
  only pushes a prompt to its own web UI. Write `eula=true` with no newline.
  (`agree_to_eula: true` in the create payload makes Crafty write it correctly.)
- **Crafty rewrites `execution_command`** when its loader-installer thread
  finishes, silently undoing a `java_selection` set earlier. Re-assert Java
  immediately before every start.
- **`set_java_version` has a sharp edge**: if the requested path is not in
  Crafty's detected list, Crafty stores `None` as the execution command and the
  server can never start. `crafty.py` snapshots and restores it.
- **The two CasaOS Crafty apps disagree on ports.** The default store's "Crafty
  Controller" publishes `8111:8443`; big-bear-crafty publishes `8443`. Both
  speak HTTPS. AppData is `/DATA/AppData/crafty` vs
  `/DATA/AppData/big-bear-crafty` — irrelevant to BlessForge, which never
  touches Crafty's files.
- Server-create payload that works (from a HAR of Crafty's own UI):
  `create_type: minecraft_java` → `minecraft_java_create_data.create_type:
  download_jar` → `download_jar_create_data: {category: mc_java_servers, type:
  forge-installer|neoforge-installer|fabric, version, mem_min, mem_max,
  server_properties_port, agree_to_eula}`. Type names matter — `forge` is not
  `forge-installer`.

**Compose / CasaOS**

- **CurseForge keys contain `$`.** Compose expands `$name`, and CasaOS
  un-doubles `$$` when it stores an imported compose before handing it back to
  Compose, which expands it **again**. No amount of doubling survives both; the
  key arrives truncated and every call 403s with nothing saying why. Hence
  `CURSEFORGE_API_KEY_B64`.
- **Never ship a named volume to CasaOS** (§6.3).
- CasaOS creates bind directories as root, so an image doing `USER <uid>` in
  its Dockerfile can never fix ownership from inside. Start root, chown, drop.
- Docker's address pools run out after ~30 CasaOS apps (`all predefined address
  pools have been fully subnetted`), which is why this uses `network_mode:
  bridge` rather than a private network.

**Host**

- `sudo` always prompts for a password here — unusable from a non-interactive
  session. `/var/lib/casaos/apps/*/` is world-writable, which is the way in.
- No Node, no npm, no Pillow, no ImageMagick on the host. Everything runs in
  containers (`node:20-alpine`, `ghcr.io/puppeteer/puppeteer`).
- RAM is tight: the optimizer's safe heap ceiling came out at **2.5 GB** for a
  pack asking 4 GB. Don't casually start big servers to test.

---

## 9. Quick reference

```bash
# state of everything
docker ps --filter name=blessforge --format '{{.Names}} | {{.Image}} | {{.Status}}'
curl -s http://127.0.0.1:8710/api/health | python3 -m json.tool
curl -s http://127.0.0.1:8710/api/ai/status | python3 -m json.tool

# the whole suite: 121 checks
cd ~/blessforge
for t in test_loader_detection test_job_stream test_install_decisions test_roulette; do
  .venv/bin/python dev/tools/$t.py | tail -1
done
cd dev/ui-tests && for t in smoke 05-roulette; do
  docker run --rm --network host -v "$PWD":/w \
    -v "$HOME/blessforge/app/static":/static:ro -w /w \
    -e BF_URL=http://127.0.0.1:8710 node:20-alpine node "$t.mjs" | tail -1
done
# The jsdom harnesses drive the REAL backend. Writes are intercepted, but they
# do read live CurseForge and Modrinth, so they need the network.

# JS syntax check (there is no node on the host)
docker run --rm -v "$PWD/app/static":/w:ro node:20-alpine node --check /w/app.js

# build + redeploy (the container runs the LOCAL tree, not GHCR)
cd ~/blessforge && docker compose build
docker compose -f /var/lib/casaos/apps/blessforge/docker-compose.yml \
  -p blessforge up -d --force-recreate

# a dev server on 8719, without touching the deployed container.
# NEVER `pkill -f uvicorn` -- it kills the container's process too (§8.17).
cd ~/blessforge && set -a && . ./.env && set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8719
PID=$(ss -lptn 'sport = :8719' | grep -oP 'pid=\K[0-9]+' | head -1) && kill "$PID"

# any instance with a missing launcher?
cd ~/blessforge && set -a && . ./.env && set +a
.venv/bin/python dev/tools/sweep_instances.py

# Crafty's side of a failed install
docker exec big-bear-crafty sh -c "grep -n '<server-id>' /crafty/logs/commander.log"

# roll a server to have something to build instance screens against
curl -s -X POST http://127.0.0.1:8710/api/roulette/roll \
  -H 'Content-Type: application/json' -d '{"constraints":{"count":25}}'
# -> {"job_id": ...}; poll /api/jobs/<id>, then POST its result.hand to
#    /api/roulette/install with a server_name and port.
```

Local dev server (no container):

```bash
cd ~/blessforge && set -a && . ./.env && set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8719
```

CI: every push to `main` builds and publishes amd64 + arm64 to GHCR
(~4 minutes). The GitHub API rate-limits anonymous polling at 60/hour — check
the published image instead:

```bash
docker pull -q ghcr.io/chargeyxd/blessforge:latest
docker run --rm --entrypoint sh ghcr.io/chargeyxd/blessforge:latest -c 'grep -c LOADER_STALL /app/app/installer.py'
```
