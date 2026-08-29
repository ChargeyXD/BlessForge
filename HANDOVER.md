# BlessForge — session handover

Written 2026-08-29, revising the 2026-08-27 original. Everything here was
verified on this machine, not recalled. Where something was *not* verified, it
says so.

Start a new session with: **"Read `~/blessforge/HANDOVER.md`, then <the thing
you want done."**

---

## 0. Where this is right now

**The front end is the design canvas itself, wired to the API.** Earlier
sessions read `design/BlessForge.dc.html` as a *specification* and hand-wrote
an approximation of it. That was the wrong reading: the zip the user supplied
(`BlessForge Server New GUI.zip`) also contains **`support.js`**, the Claude
Design runtime — so the canvas is not a mockup, it is a runnable app whose only
missing piece was its data. On 2026-08-29 the hand-built front end was deleted
and `app/static/index.html` became that canvas with its markup untouched. See
§4, which is now about how that works.

**Three real servers exist and all three have been watched booting.** The
install pipeline, the client-only review, Mod Roulette and its CurseForge
export are all verified end to end against them (§5D).

| | |
|---|---|
| Deployed | `http://<host>:8710`, healthy |
| Tests | **137 checks** — 98 offline Python, 39 in a real browser (§9) |
| Servers in Crafty | **five**, §5D and §5F. `Perfect World` :25565 (NeoForge, 100), `Cozy Experience` :25566 (Fabric, 245), `Lucky Dip` :25567 (rolled, 44), `Roulette RB5-YR9-BC` :25568 (rolled, 72), `Tensura` :25569 (Forge 1.19.2, 223 — will not boot here, §5F) |

**Committed on branch `rebuild/ui-and-mod-roulette`**, not merged and not
pushed; `main` is untouched. The container runs the local tree, not GHCR, so
merging is a separate decision from deploying.

**What to read first:** `NEXT-SESSION.md` — a line-by-line review of what is
finished, what is unproven, and what is worth tidying. Then §4 for how the
front end is built (it is nothing like what §4 said before), §5D for what was
verified and what it cost, and
`design/README.md` for the places the design asserted something untrue about
this app. Those corrections are marked `DESIGN:` in the code.

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
  main.py         1838  FastAPI: every route, static serving, cache-busting fingerprint
  installer.py    1281  the install pipeline; loader wait + repair (§6.3)
  diagnostics.py  1165  health checks, crash-log parsing, findings
  crafty.py        850  Crafty API client — the only thing that talks to Crafty, and
                        the only place that interprets what it says back
  mods.py          657  mod listing, toggle, delete, identify
  ai.py           1114  Ollama client, action vocabulary, re-validation
  packs.py         468  pack plans, archive shape detection, loader mapping
  preflight.py     569  client-only mod review + dependency protection
  properties.py    422  server.properties model
  specs.py         441  host specs, heap sizing
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
  static/
    index.html    the Claude Design canvas, markup untouched, with its logic
                  rewritten to read the API. THE front end -- see §4.
    support.js    Claude Design's runtime, vendored from the zip. Never edit.
    vendor/       react@18.3.1 + react-dom + both fonts, served from this box
                  because a LAN install has no route to a CDN
    img/          the design's assets, downscaled; mounted at /assets so the
                  canvas keeps its own paths — see design/README.md
dev/              test tooling — see dev/README.md
design/           the Claude Design canvas the new UI is built from, and its
                  README — READ IT, it lists what the design got wrong about
                  this app. The previous front end is in git history, not here.
design-prompt.md  the brief that produced that design  [2026-08-28]
BlessForge Server New GUI.zip
                  the design delivery: the canvas, support.js and the original
                  full-size assets. Gitignored. It is the source of truth for
                  the front end -- see §5C.
design-brief.md   HISTORICAL. Described the front end replaced on 2026-08-29;
                  its §4.3 DOM contract no longer applies to anything. Kept
                  only because it explains why the old UI looked as it did.
entrypoint.sh     starts root, chowns /data subdirs, drops to uid 1000
```

Backend is FastAPI + httpx, no database. State lives in Crafty (a
`crafty_managed.txt` manifest per instance) and in `/data`.

---

## 4. Front end — how it actually works now

**`app/static/index.html` IS the design canvas.** It is
`design/BlessForge.dc.html` with the markup unchanged: every animation
(`bfReel`, `bfCrit`, `bfBreathe`, `bfLever`, `bfPeek`, `bfDraw`, `bfStripe`…),
every token, every piece of copy is the designer's. Do not rewrite it. If a
screen needs to say something different, change what the binding *returns*,
not the markup that renders it.

**The runtime is `app/static/support.js`**, shipped from the same zip. It is a
generated build of Claude Design's own renderer — treat it as a vendored
dependency and never edit it. It:

- parses `<x-dc>…</x-dc>` into a template supporting `{{ expr }}`, `<sc-if>`
  and `<sc-for list="{{ xs }}" as="x">`;
- evaluates the `<script type="text/x-dc" data-dc-script>` body with
  `new Function`, expecting it to define `class Component extends DCLogic`;
- re-renders `renderVals()` against the template on every `setState`.

**`renderVals()` is the whole contract.** The template reads **316 named
bindings** out of it. Every one must exist and keep its shape; a missing one
renders blank with no error and no console message. `dev/tools/check_bindings.py`
diffs the two and is the fastest way to catch one you forgot.

**Three traps in that runtime, each of which cost real time here:**

- **`componentDidUpdate(prevProps)` takes ONE argument, and it is props.**
  There is no `prevState`. Code that compared against a second argument
  returned early every time, and *no screen ever fetched anything* — every tab
  rendered its empty state and looked merely unfinished. Track what changed
  yourself; `this._seen` does that now.
- **Job frames carry `percent`, not `progress`.** Reading the wrong name
  leaves every progress bar at 0% while the job runs perfectly well.
- **The first SSE frame is a `snapshot`**, and for an already-finished job it
  reports a terminal status. Treat that as the end, or a view re-attached to a
  finished job sits at RUNNING forever.
- **A `<textarea>` takes `value`, not children.** An interpolated child renders
  as `[object Object]`.
- **A `<select>` nested inside an outer `<sc-for>` is dropped entirely.** Each
  one has to own the only loop in its subtree.
- **A static element's `onClick` is bound once and never rebound.** Attribute
  bindings update on every render; handlers do not, so a handler that closes
  over a render-time value keeps using the first one forever. Handlers on
  static elements must read `this.state` or ask the server. `<sc-for>` rows do
  get fresh closures. This one produced a bug that looked like a backend
  fault — suspect it whenever a control does nothing, or does the same thing
  twice.

**Offline by design.** This box is usually on a LAN with no route out, so
`react@18.3.1` + `react-dom` and both Google fonts are vendored under
`app/static/vendor/`. `support.js` falls back to unpkg only when
`window.React` is absent, and both vendored files match the SRI digests it
carries. Do not "simplify" this back to a CDN.

**Assets keep the canvas's own paths.** The template says `assets/NAME`;
`main.py` mounts `app/static/img` at `/assets`, so the markup never had to be
rewritten. The images there are the design's, downscaled.

**Cache busting** hashes `index.html`, `support.js` and the vendored files
(`_VERSIONED` in `main.py`). Add a served file, add it there.

**Where a screen's data comes from:** `loadFor(view)` fetches when a view is
opened, into `state.d.<key>`; `renderVals()` only reads. Never fetch from
`renderVals()` — it runs on every render.

**Corrections of fact are marked `DESIGN:`** in the logic, and only where the
canvas asserted something untrue (no TPS tile, Memory not Heap, the real
remote Ollama endpoint, the real published port range, the real server paths).
Layout, copy and motion are left alone — that was the whole point.

---

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

### 5.10 Tests  [superseded — see §5C/§5D]

`dev/tools/test_job_stream.py` (10) and `dev/tools/test_install_decisions.py`
(20, grown since). `dev/ui-tests/04-terminal-and-review.mjs` was written here
and retired with the front end on 2026-08-29 — see §5C. The suite today is
**137 checks**; §9 runs it.

(That `window.__bf` test seam went with the hand-built front end. The canvas
has no equivalent and does not need one: `ui.mjs` drives it by clicking, the
way a person does.)

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

## 5C. The front end, and the wrong turn that preceded it (2026-08-29)

Two sessions built a front end *from* `design/BlessForge.dc.html`, reading it
as a specification and hand-writing an approximation in plain ES2020. It was
honest work and it is gone, because the reading was wrong.

The zip the user supplied — `BlessForge Server New GUI.zip`, gitignored, still
in the repo root — contains **`support.js`** alongside the canvas. That is
Claude Design's own runtime. With it, the canvas is not a mockup that has to be
reimplemented; it is a runnable application whose only missing piece was its
data. Everything the hand-built version could not reproduce — the reel spin,
the crash-card pulse, the lever, the fox peeking in on a failed job, the
marquee bulbs — was already there.

So `app/static/index.html` is now that canvas, markup untouched, with only its
`<script data-dc-script>` rewritten: the original drove itself from hard-coded
arrays (`SERVERS`, `MODS`, `CHECKS`, `RPOOL`…), and those became API calls.
`app/static/app.js` and `style.css` are deleted; git has them.

**The lesson worth keeping:** when a design hands you a runtime, the design is
the app. Read the whole delivery before deciding what it is.

Three changes were made to the markup itself, all of them things the design
could not have known or that the user asked for:

- Mod Roulette's Minecraft version was three hard-coded options; it is now a
  select over every version the catalogue knows.
- Add-a-mod results carry the real mod icon, and the catalogue the real pack
  logo. The canvas drew a placeholder square in both slots because it had no
  data behind it.
- Roughly a dozen mock literals became bindings — the ROOT CAUSE hero, the
  console's `websocket · 41 ms`, `/srv/minecraft/<id>`, `41.2 GB`, and the
  hard-coded 387/241/371/34/61 counts on Mods, Configs and Tune.

---

## 5D. What was verified, and what it cost (2026-08-29)

Everything below was watched happening, not inferred.

**Three servers installed and booted.**

| server | port | loader | mods | how |
|---|---|---|---|---|
| Perfect World | 25565 | NeoForge 21.1.248 | 100 | catalogue → preflight → review → install |
| Cozy Experience | 25566 | Fabric 0.19.2 | 191 | same, from a real server pack |
| Lucky Dip | 25567 | NeoForge | 42 (25 rolled + 16 deps + 1 added) | Mod Roulette |

**The client-only review is the feature that earns its keep.** Perfect World's
preflight found 19 candidates in 100 mods — 15 confirmed client-only, 3 held
back because other mods depend on them. Installed with none of them disabled
(a bug in the *test script*, not the app), the server died on a client
rendering library it could not resolve. With them off, it boots. Cozy's review
found 5 in 191 and the one it marked "contradicted" rather than "remove" was
exactly the one that later needed disabling.

**Mod Roulette, end to end.** A 3,035-mod pool for 1.21.1/NeoForge (cached),
a 25-mod hand dealt in 9 s, the same seed re-dealing an identical hand, 25
rolled mods resolving to 41 jars, and a **CurseForge export** that BlessForge
re-imports as a valid manifest pack (40 files + 1 bundled override jar,
63 MB). The export's README records the seed, so the pack is reproducible even
when the roll is not.

**Two real faults found by doing this, both fixed:**

1. **The server-pack review had no dependency protection.** The rule that a mod
   other mods depend on is never stripped ran on the manifest path only. Athena
   is declared client-only by its own author and is a hard requirement of
   Chipped, which is not — so the review removed it with confidence and the
   pack stopped booting with *"requires version 4.0.0 or later of athena, which
   is missing"*. Both paths now share `preflight.decide_with_protection`; six
   offline checks pin it.
2. **`set_enabled` was not idempotent.** Callers name a mod by its enabled
   filename, so disabling something already disabled tried to rename a file
   that no longer exists: "disable these twelve" reported nine errors the
   second time it ran.

**One thing that is a data gap, not a bug.** `corpsecurioscompat`'s 1.21.1
NeoForge file declares no dependencies on CurseForge, though its 1.20.1 Forge
file does and the jar's own metadata requires `corpse`. Nothing BlessForge asks
before installing knows about it; the loader's error after installing does, and
Diagnose named it precisely. Catalogue file-level relations are not complete
and cannot be treated as authoritative.

**And one that is physics.** Cozy Experience would not boot while another
server was running: two 4 GB heaps with `AlwaysPreTouch` on an 11.6 GB host.
Alone, it boots. The heap ceiling the Tune screen computes was right.

---

## 5E. The reported bugs, and what fixing them found (2026-08-29, later)

Eighteen faults reported after using the app for an evening. All fixed; the
ones worth knowing about:

**The deep dependency scan was wrong, not slow.** It reported 16 missing
dependencies on a pack that boots. Modern packs ship dependencies *inside*
other jars (`META-INF/jars`, `META-INF/jarjar`) and `jarmeta` read only the
outer jar, so every bundled mod looked absent -- 52 of the 107 ids one test
pack provides come from nested jars. It also ignored `provides`, treated
NeoForge's `type = "optional"` as mandatory (only the older `mandatory = false`
was honoured), called a `FMLModType: LIBRARY` jar unreadable, and let an inline
TOML comment leak into a value, producing a dependency on the mod id
`neoforge" #mandatory`. Same pack now: **1 missing, and it is real.**

**Three shapes the canvas's runtime rejects.** Worth knowing before writing any
more markup:

- `componentDidUpdate(prevProps)` takes one argument and it is props (§4).
- A `<textarea>` whose text is an interpolated *child* renders
  `[object Object]`. React wants `value`.
- A `<select>` nested inside an outer `<sc-for>` is dropped entirely. Each one
  has to own the only loop in its subtree, which is why the four catalogue
  filter chips are written out rather than looped.

**Identification now happens at install.** The manifest path recorded the
project id but not the logo the catalogue had already handed it, so every mod
row fell back to two grey initials until someone ran Identify -- which then
re-fetched what was already known. A server pack listed no project ids at all;
its jars are matched from the local archive during the install instead, two
bulk calls for the whole pack. And `/mods/updates` no longer runs on open: on a
200-mod pack that was 200 catalogue requests for an answer nobody asked for.

**`app/whitelist.py`.** Mods the operator has decided are safe on a server,
whatever the review says -- `server_side: unsupported` often means "adds
nothing on a server", not "breaks one". Global rather than per instance,
matched on the jar's stem so a version bump does not undo the decision, and
held in memory as well as on disk so a decision still applies when `/data`
turns out not to be writable.

---

## 5F. The Tensura acceptance test (2026-08-29)

`[Mega update] Akashic Records of Tensura` 2.0.1 -- 223 mods, Forge 1.19.2,
**manifest only, no server build** -- installed to exercise the harder path.

It worked: 232 mods inspected, 30 client-only candidates (21 confirmed, 5 for
review, **4 held back because other mods depend on them**), 223 jars installed
with 21 disabled and tagged, 1032 files uploaded, no problems. All 223 came out
identified with real icons straight from the install.

Three things it found:

1. **A start for a server Crafty already thinks is running is silently
   dropped.** Crafty's flag also lags a stop by a poll cycle and stays set for
   a process that died without it noticing, so the UI reported "Starting X" for
   a command that went nowhere. A start in that state is issued as a restart,
   which is right either way, and says so.
2. **The pack cannot run on this Crafty image.** Six of its mods -- including
   the pack's own headline mod -- initialise `java.awt` while registering, and
   the Crafty image ships a headless JRE with no `libawt_xawt.so`.
   `-Djava.awt.headless=true` is now in the flag set and offered as a one-click
   fix, and the finding names the mods and is honest that the flag only helps
   if nothing turns headless back off at runtime. Here, something does. The
   useful answer is "this pack needs a different Java on the Crafty host", and
   BlessForge says exactly that.
3. **The config rail was capped at 400 rows out of 810**, which cut off
   `server.properties` and `user_jvm_args.txt` -- the files someone opens the
   screen to edit. The server root leads the list now.

---

## 6. Earlier sessions (8 commits, `23b38e7..c62d607`)

History. Kept for the traps it records, which are all still live.

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

**The front-end half of this is history**: that build, and the one that
replaced it, are both gone (§5C). The packaging half still stands.

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

1. **Nobody has clicked a destructive button in the browser.** Start, stop,
   restart, delete-the-record and delete-the-world are wired and their confirm
   dialogs work, but every power and delete action here was driven through the
   API, and `ui.mjs` deliberately never touches them. Someone should press
   Stop, and press Delete on a server they do not want, once.
2. **Review & Fix has never applied anything.** The plan half is verified end
   to end against a real crash; `_apply_actions` is the same code path
   `/ai/apply` has always used, but the automatic route has not been watched
   making a change.
3. **The light theme does not exist.** The canvas only ever showed dark, and
   the toggle in the topbar says so rather than pretending.
4. **Below 900px is unopened.** `ui.mjs` checks 1600 / 1280 / 900. The canvas
   uses fixed pixel widths in places (a 298px spine, a 428px drawer) and will
   need real work on a phone.
5. **The assistant has been read, not exercised.** `/api/ai/status` is live and
   the endpoint answers, but no analysis was run in this session.

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

### The front end — known gaps

24. **A missing binding renders as nothing, silently.** No error, no console
    message, just a blank where a number should be. `python3
    dev/tools/check_bindings.py` diffs the 316 names the template reads against
    what `renderVals()` returns; run it after touching either.
25. **`index.html` is 3,955 lines** — about 1,500 of canvas markup and 2,400 of
    logic. It cannot be split: the runtime wants the template and its script in
    one document. Keep the section comments honest; they are the only
    navigation there is.
26. **`support.js` is a vendored build with no source here.** If it ever needs
    a fix, the fix belongs upstream in Claude Design, not in that file.
27. **Two servers cannot both hold 4 GB on this host.** The Tune screen's
    ceiling is computed from what is free *now*, so it drops while another
    server runs — which is correct, and does mean the same pack shows a
    different ceiling depending on what else is up.

### Known rough edges

5. **The UI follows one job at a time.** `follow()` replaces whatever stream
   was open, so starting a second install while the first runs leaves the first
   running (it is a server-side job) but stops showing it. Activity still lists
   it and re-attaches. Fine for one operator; wrong if two things matter at
   once.
6. **The mod list is not virtualised.** The canvas renders every row, and a
   400-mod instance is 400 rows of DOM. It is fast enough at 191; the previous
   front end windowed at ~120 rows and that machinery is in git if it is ever
   needed.
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

28. **The open instance polls `/stats` every 6 s while it is running.** One
    Crafty call per tick, and it stops for a stopped server — but it is a poll,
    and two browser tabs on the same instance double it (§7.9).
29. **CurseForge file-level dependencies are not complete.** A file can declare
    none while the jar's own metadata requires something; that is how Lucky Dip
    installed without `corpse` (§5D). Nothing asked before installing knows;
    the loader's error afterwards does.

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
curl -s http://127.0.0.1:8710/api/instances | python3 -m json.tool
curl -s http://127.0.0.1:8710/api/ai/status | python3 -m json.tool

# the whole suite: 137 checks
cd ~/blessforge
for t in test_loader_detection test_job_stream test_install_decisions test_roulette; do
  .venv/bin/python dev/tools/$t.py | tail -1
done
python3 dev/tools/check_bindings.py | tail -1     # 316 template bindings
cd dev/ui-tests && docker run --rm --network host -v "$PWD":/w -w /home/pptruser \
  ghcr.io/puppeteer/puppeteer:latest sh -c "cp /w/ui.mjs . && node ui.mjs" | tail -1
# ui.mjs drives a REAL browser against the REAL backend, and only reads.

# what it looks like — screenshots at four widths
mkdir -p /tmp/shots && chmod 777 /tmp/shots
cd ~/blessforge/dev/ui-tests
docker run --rm --network host -v "$PWD":/w:ro -v /tmp/shots:/out -w /home/pptruser \
  ghcr.io/puppeteer/puppeteer:latest \
  sh -c "cp /w/screenshot.mjs . && node screenshot.mjs && cp *.png /out/"

# build + redeploy (the container runs the LOCAL tree, not GHCR)
cd ~/blessforge && docker compose build
docker compose -f /var/lib/casaos/apps/blessforge/docker-compose.yml \
  -p blessforge up -d --force-recreate
# NEVER redeploy while an install is running -- the job registry is in memory
# and the container restart kills it mid-flight.

# JS syntax check on the canvas's logic (there is no node on the host)
docker run --rm -v "$PWD/app/static":/w:ro node:20-alpine node -e '
const fs=require("fs");const s=fs.readFileSync("/w/index.html","utf8");
const i=s.indexOf("data-dc-script");const j=s.indexOf(">",i)+1;
new Function("DCLogic","StreamableLogic","React", s.slice(j,s.lastIndexOf("</script>")));
console.log("ok");'

# a dev server on 8719, without touching the deployed container.
# NEVER `pkill -f uvicorn` -- it kills the container's process too (§8.17).
cd ~/blessforge && set -a && . ./.env && set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8719
PID=$(ss -lptn 'sport = :8719' | grep -oP 'pid=\K[0-9]+' | head -1) && kill "$PID"
# /data is not writable from the host, so the dev server's storage check fails.
# That is an artifact of running outside the container, not a fault.

# any instance with a missing launcher?
cd ~/blessforge && set -a && . ./.env && set +a
.venv/bin/python dev/tools/sweep_instances.py

# Crafty's side of a failed install
docker exec big-bear-crafty sh -c "grep -n '<server-id>' /crafty/logs/commander.log"

# roll a server from the API rather than the screen
curl -s -X POST http://127.0.0.1:8710/api/roulette/roll \
  -H 'Content-Type: application/json' \
  -d '{"constraints":{"count":25,"minecraft":"1.21.1","loader":"NeoForge"}}'
# -> {"job_id": ...}; poll /api/jobs/<id>, then POST its result to
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
