# BlessForge — session handover

Written 2026-08-29, revising the 2026-08-27 original. Everything here was
verified on this machine, not recalled. Where something was *not* verified, it
says so.

Start a new session with: **"Read `~/blessforge/HANDOVER.md`, then <the thing
you want done."**

---

## 0. Where this is right now

**The front end is plain ES modules, written from scratch.** The design canvas
and its runtime are *gone* — no `design/BlessForge.dc.html` as a live document,
no `support.js`, no vendored React, no 316-binding contract. An earlier session
read the canvas as a specification and hand-wrote an approximation; a later one
made the canvas itself the app. Both are history. On 2026-09-10 the whole front
end was rebuilt on `h()` and real DOM, then reworked a second time the same day
for weight and speed (§5G). See §4 for how it works now.

**Three real servers exist and all three have been watched booting.** The
install pipeline, the client-only review, Mod Roulette and its CurseForge
export are all verified end to end against them (§5D).

| | |
|---|---|
| Deployed | `http://<host>:8710`, healthy |
| Version | 2.1.0, ~131 routes |
| Tests | **171 backend checks** across six files, **197 front-end checks** (§9) |
| Servers in Crafty | **five**, §5D and §5F. `Perfect World` :25565 (NeoForge, 100), `Cozy Experience` :25566 (Fabric, 245), `Lucky Dip` :25567 (rolled, 44), `Roulette RB5-YR9-BC` :25568 (rolled, 72), `Tensura` :25569 (Forge 1.19.2, 223 — will not boot here, §5F) |

**Committed on `main`.** `1d00eb8` is the rebuild and the six new features and
is on `origin/main`; the UI pass (§5G) is the commit after it and may still be
local — check `git status -sb`. Pushing triggers the GHCR build; the container
on the box runs the *local tree*, not GHCR, so deploying and publishing stay
independent.

**What to read first:** `NEXT-SESSION.md` — a line-by-line review of what is
finished, what is unproven, and what is worth tidying. Then §4 for how the
front end is built, §5G for the UI pass and the layering contract it turns on,
and §5D for what was verified and what it cost. `HANDOVER-CASAOS.md` is the
deploy-and-verify script for the box itself.
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
  main.py         2575  FastAPI: every route, static serving with cache headers
  diagnostics.py  1285  health checks, crash-log parsing, findings
  roulette.py     1274  Mod Roulette: pool, seeded deal, verify, top-up, export
  installer.py    1224  the install pipeline (loader ladder lives in provision)
  ai.py           1179  Ollama client, action vocabulary, re-validation
  crafty.py        898  Crafty API client — the only thing that talks to Crafty,
                        and the only place that interprets what it says back
  mods.py          765  mod listing, toggle, delete, identify
  clientscan.py    731  the client-only detector: evidence, scoring, overrides
  provision.py     606  create a blank instance; THE loader retry ladder
  players.py       588  ops, whitelist, bans — routed by whether it is running
  curseforge.py    573  CurseForge API + download cache
  files.py         518  the file manager: path guards, streaming, classification
  packs.py         498  pack plans, archive shape detection, loader mapping
  plugins.py       486  Paper-family plugins, from Modrinth, compatibility-checked
  specs.py         475  host specs, heap sizing
  loaders.py       458  upstream loader sources — Mojang, Fabric, NeoForged,
                        Forge, PaperMC, PurpurMC — and the version schemes
  properties.py    426  server.properties model
  jarmeta.py       409  jar fingerprinting
  deps.py          332  recursive dependency resolution
  whitelist.py     328  the operator's allow / block decisions
  preflight.py     277  pre-install review — a thin shell over clientscan now
  jobs.py          266  job model + SSE
  modrinth.py      253  Modrinth API
  uploads.py       246  imported .zip exports
  backups.py       223  undo snapshots
  optimizer.py     222  Aikar flags, heap, property presets
  config.py        185  env parsing, paths
  configs.py       176  config file tree
  exporter.py      157  write an instance out as a CurseForge pack
  smoketest.py     152  boot once, watch, stop, report
  watcher.py       101  the scheduled update sweep
  cache.py          69  download cache pruning
  static/
    index.html      the shell: stylesheet links and one module script
    css/            theme.css (tokens, ornament, motion) + app.css (the shell)
    js/             core.js, app.js, jobs.js, petals.js, views/*.js — plain
                    ES modules, no build step, every screen a dynamic import
    vendor/         Space Grotesk + JetBrains Mono, served from this box
                    because a LAN install has no route to a CDN
    img/            art, mounted at /assets
dev/                test tooling — see dev/README.md
design/             the 2026-08-28 Claude Design canvas. HISTORICAL: the front
                    end it specified was replaced on 2026-09-10. Kept for its
                    README, which lists what that design got wrong about this
                    app, and because the art in static/img came from it.
entrypoint.sh       starts root, chowns /data subdirs, drops to uid 1000
```

Backend is FastAPI + httpx, no database. State lives in Crafty (a
`.blessforge.json` manifest per instance) and in `/data`.

### The three modules added on 2026-09-10, and why each exists

* **`provision.py`** — "Crafty answered 201" and "this instance can start" are
  not the same claim, and the gap between them was a hang. The loader ladder
  moved here out of `installer.py` so a blank instance and a modpack install
  share one implementation; a second copy would have drifted.
* **`clientscan.py`** — the client-only decision was three yes/no questions
  split across two functions that disagreed with each other. It is now one
  scored judgement with the evidence attached, used by the pre-install review
  *and* by a scan against an instance that already exists.
* **`loaders.py`** — everything upstream of Crafty. It exists because Crafty's
  mirror going down should cost a retry, not an instance, and because
  Minecraft's version scheme changed underneath both of them (see §8).

---

## 4. Front end — how it actually works now

**Rebuilt from scratch on 2026-09-10.** The design-canvas runtime is gone:
no `<x-dc>`, no `support.js`, no vendored React, no 316-binding contract, and
none of the six runtime traps that used to be listed here. What replaced it is
plain ES modules and real DOM.

```
static/index.html   the shell — stylesheet links, one <script type="module">
static/css/theme.css   tokens, ornament, motion (shared with Project Fox)
static/css/app.css     the shell and the screens
static/js/core.js      h(), icons, api, toasts, modals, formatting
static/js/app.js       the router, the rail, boot
static/js/jobs.js      long operations and the activity drawer
static/js/petals.js    the falling sakura
static/js/views/*.js   one module per screen, loaded on demand
```

**Nothing is built from an HTML string.** `h(tag, props, ...kids)` returns
real nodes and handlers are attached to the node they belong to, which is the
direct answer to the worst trap of the old runtime: a handler on a static
element was bound once and never rebound, so a control that closed over a
render-time value kept using the first one forever. That bug class cannot
occur here — a row's handler is created with the row.

**Routing is hashes**, so the whole app is one static document any reverse
proxy serves without rewrite rules. `#/i/<id>/<tab>` means a link to a crashed
server's Diagnose tab is a link someone can send.

**Caching is a header, not a filename.** A `?v=` fingerprint cannot work for
ES modules: a module's own `import './core.js'` carries no query string and
nothing rewrites it. `_StaticCache` in `main.py` serves `.js`/`.css`/`.html`
as `no-cache, must-revalidate` and fonts/images as `immutable`. There is no
stale-asset failure mode left to reason about, and `_VERSIONED` and
`_asset_version()` are gone with it.

**The wiggling polygon has one hard rule, and it is the opposite of what it
used to be.** The highlight started as a `z-index:-1` child, which paints
behind its parent's *background* only while the parent is not a stacking
context — so the rule was that nothing carrying a highlight could take a
`transform`. That rule was one animated property away from breaking, and the
entrance animation on `.stagger > *` duly broke it: every card took a
`transform`, every card became a stacking context, and the blob started
painting between the card fill and the card text.

So the layering is explicit now, and each host is a stacking context **on
purpose**:

```
.p5-hl                    z-index 0    the blob, inset -15px -17px
.card::after              z-index 1    the fill and the border
.card > *:not(.p5-hl)     z-index 2    everything you read
```

with `isolation:isolate` on `.card`, `.btn` and `.loadercard` so the order is
ours to set rather than the nearest ancestor's. Transforms are free, which is
what lets the app move at all. Two things will silently break it:

- **A blanket `.card > *` rule.** At specificity (0,1,1) it outranks
  `.p5-hl`'s own (0,1,0) `position:absolute` and collapses the blob to a
  zero-size element in flow — which looks exactly like the highlight not
  working. The `:not(.p5-hl)` is load-bearing.
- **Any new `z-index:-1`.** It reintroduces the old dependence on the parent
  not being a stacking context.

`dev/tools/check_frontend.check_layering` enforces all of it, including both
of those.

**Never animate *from* `opacity:0`.** Entrance animations are translate-only,
gated on `html.js-motion`, which `app.js` stamps in a `setTimeout(…, 0)` after
first paint. The reason is blunt: if the document timeline stalls — a
background tab, a throttled preview pane, a stalled main thread — a `fill:both`
animation that starts at `opacity:0` never runs and the content is permanently
invisible. A blank screen is a worse failure than an unanimated one.

**Boot does not await health.** `paintNav()` and `route()` run first;
`refreshHealth()` resolves into the UI afterwards. `/api/health` itself runs
its three upstream checks under `asyncio.gather` behind an 8-second cache
(`_HEALTH_TTL`, `_health_lock`). Awaiting it before first paint was the whole
of "everything loads really slow" — the page was waiting on CurseForge and
Modrinth to answer before drawing anything. DOMContentLoaded went 248 → 118 ms
and load 589 → 387 ms on that change alone.

**Fonts degrade rather than block.** Archivo Black and Plus Jakarta Sans come
from Google Fonts on a non-blocking link; Space Grotesk and JetBrains Mono are
vendored. When the Google link cannot be reached, `app.js` finds Archivo Black
missing and stamps `no-archivo` on `<html>`, and the CSS gives the fallback
the display weight it needs. A LAN box with no route out paints immediately
instead of waiting out a DNS timeout on a font.

**A job owns its stream; the drawer is only a view onto it.** Closing the
drawer closes the view, never the `EventSource`, and several jobs are followed
at once — the old front end followed one and silently stopped showing the
second. A reload adopts whatever is still running server-side.

**Two ordering traps, both found by driving the real UI:**

- **`confirmDialog` must resolve before it closes.** `close()` runs `onClose`,
  which resolves `false`; calling `close()` first meant every confirmation in
  the app resolved false and silently did nothing. Both dialogs now decide,
  then tear down.
- **A class token with a trailing space kills the screen.** `bar(v, 'thin ')`
  from an empty interpolated variant produced `classList.add('thin ')`, which
  throws. `h()` now trims and splits tokens rather than trusting call sites.

**`dev/tools/check_frontend.py` replaces `check_bindings.py` and
`audit_placeholders.py`**, which audited the runtime that no longer exists. It
checks that every module parses *as a module* (`node --check` alone accepts a
file with a broken string literal in it — only `--input-type=module` is a real
check), that every import and asset resolves, that every API path the front
end names is a route the server serves, that every `icon()` name exists, and
the layering contract above. 125 checks.

---

## 5. What changed on 2026-08-27/28 — the nine reported faults

Nine faults the user reported, plus three new features. Sections 5A–5D cover
what came after. All of it is committed and pushed (§0).

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
**141 backend checks and 125 front-end checks**; §9 runs it.

(That `window.__bf` test seam went with the hand-built front end, and so did
`ui.mjs`, which drove the canvas that replaced it. Neither has a successor:
`check_frontend.py` covers the static half — parsing, imports, assets, API
paths, the layering contract — and the interactive half is driven in a real
browser, which is how both of the bugs in §5E were actually found.)

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

## 5G. The second UI pass (2026-09-10, later the same day)

The rebuild in §4 was correct and read flat. Reported back as: too plain, too
static, slow to load, the polygon in the wrong place, the sidebar bland, the
lever "just a button". All of that was true. Nothing below changes an endpoint
or a payload — the three server-side changes exist only to make the page paint
sooner.

### 5G.1 The polygon was painting in the wrong layer, and why

Covered in §4 as the contract; here is the diagnosis, because the failure mode
is instructive. The blob was `z-index:-1`, which paints behind the parent's
background *only while the parent is not a stacking context*. The entrance
animation `.stagger > *` set a `transform`. A transform creates a stacking
context. So `-1` stopped meaning "behind the card" and started meaning "behind
the card's own content but in front of its background" — the blob appeared
*inside* the card, under the text, which reads as a colour bug rather than a
layering one.

Reproduced by reading computed styles in a real browser rather than by
inspecting the CSS, which is the only way this one is visible. The fix is
three explicit layers with `isolation:isolate`; the verification is the same
computed styles read back: card isolates, blob `position:absolute` at z 0,
fill at z 1, bleeding 17 px left and 15 px top past the card edge.

Then a second, quieter failure: `.card > *` at specificity (0,1,1) beat
`.p5-hl`'s own (0,1,0) `position:absolute` and collapsed the blob into a
zero-height flow element. Hence `.card > *:not(.p5-hl)`, and hence the check
that forbids the naked form.

### 5G.2 "Everything loads really slow" was the health check, not the JS

Worth stating plainly because the instinct is to look at the front end. It was
`/api/health`: three upstream probes (Crafty, CurseForge, Modrinth) run in
sequence, and `app.js` **awaited** it before the first paint. The page waited
on two third-party APIs before drawing its own shell.

| | before | after |
|---|---|---|
| `/api/health` | 1.3 s sequential | 665 ms concurrent, 2 ms cached |
| DOMContentLoaded | 248 ms | 118 ms |
| load | 589 ms | 387 ms |

Three changes: `asyncio.gather` over `_check_crafty` / `_check_curseforge` /
`_check_modrinth`; an 8-second cache (`_HEALTH_TTL`, `_health_cache`,
`_health_lock`, with `?fresh=true` to bypass it); and boot no longer awaiting
it — `paintNav()` and `route()` run first, `refreshHealth()` resolves in
afterwards. `enrich()` got the same treatment, gathering the manifest read and
the stats call instead of doing them one after the other.

### 5G.3 The rest

- **Mod side tags.** `sideTag(mod)` renders `client only` / `server only` /
  `both` on every mod row, from `clientscan`'s scored evidence rather than a
  second guess. `_persist_sides` writes the verdict into the instance manifest
  so a row does not have to be re-scored on every render, and a jar with
  nothing recorded says exactly that instead of claiming "both".
- **The config editor is in the tab.** `tab-configs.js` is a two-column grid:
  file list beside a live editor with a matching line gutter, no modal. A
  `buffers` Map holds unsaved text per file so switching files does not lose
  it, the row is marked dirty, and `beforeunload` warns.
- **Mod Roulette's constraints, rebuilt.** A recklessness ladder (`data-lit`
  rungs, a blurb per level), an intensity dial and a live catalogue monitor
  replace the old form. The lever is a **rope**: `buildRope()` handles
  pointerdown/move/up with real resistance
  (`pulled = MAX * (1 - Math.exp(-raw / 90))`), a snap back, a `ring()` and a
  petal burst. `click` is still wired as a fallback — a rope you can only drag
  is unusable with a keyboard.
- **Diagnose is dependable rather than confident.** It now returns `passes[]`
  with per-pass counts, a `complete` flag and de-duplicated findings, so
  "nothing found" distinguishes between *the checks ran and found nothing* and
  *the checks did not run*.
- **The chrome.** Fox app icon and favicon (`make_icon.py` letterboxes the
  crop so the ears survive), sidebar buttons with state and motion, evenly
  sized loader tiles on **New server** with Paper and Purpur marks added,
  source logos on mod rows (`sourceMark()`), art in the job drawer keyed to
  what the job is doing (`jobArt()`), and the creeper on the delete confirm.
  Every previously-unused asset in `static/img/` is now referenced;
  `lucky-block.png` was deleted as superseded by the `.webp`.
- **Motion, safely.** `magnetic`, `tilt`, `countUp`, `reveal`, `installRipple`
  in `core.js`; all entrance animation translate-only and gated on
  `html.js-motion`; `prefers-reduced-motion` disables it; and
  `html.theme-switching` suppresses every transition for the duration of a
  theme flip so the whole page does not cross-fade.

### 5G.4 What this pass was checked against

125 front-end checks, 141 backend checks, all 15 routes rendered with zero
console errors in both themes, and the four new interactions driven rather
than read: the config editor opened a real TOML and marked it unsaved, the
recklessness ladder lit to `11111` at Unhinged, the rope started a roll from
both a drag and a click, and a mod row reported `client only` with the prompt
correctly saying "3 of 3 jars have no side recorded" beforehand.

One environment caveat, because it will waste your time otherwise: **a hidden
browser preview pane does not advance its document timeline.** CSS animations
never start (`startTime: null`) and screenshots return stale frames. Verify
motion-dependent things by forcing styles or reading computed values, not by
screenshotting a hidden pane.

---

## 5H. The third UI pass (2026-09-11) — eleven reported items

Reported as: still too plain, the polygon in the wrong layer, the sidebar
bland, the lever "just a button", the slider not smooth, the AI apparently
unused. Eleven items, all landed. The three that are worth understanding
rather than just listing:

### 5H.1 The polygon was painting through the buttons it bled onto

Two separate faults wearing the same symptom.

`.btn.ghost::after` — the FILL layer, z-index 1 — sets
`background:transparent`. With nothing between the z-0 blob and the z-2
label, the blob painted directly behind the text. Measured, not guessed:
cream `#F2E8EC` on the `#EFAEC0` blob is **1.53:1**.

And the blob bled 13px sideways out of a `.btnrow` whose gap was 10px, so a
hovered button painted over the label of the button beside it. Bleed is now
10px inside a 14px gap, and `.btn:hover` takes `z-index:1` so the button you
are pointing at is never underneath a later sibling.

### 5H.2 The dark theme's accent was white in all but name

`--slab-bg` in dark was `#F3E4E8` — **15.23:1** against the page, when the
theoretical maximum is 21. It is the card border, the hard offset shadow in
every `--lift-*`, the slab, the panel header and the switch track, so it set
the entire brightness of dark mode. Now `#C98FA2`, a dusty blush at 7.08:1
against the page with slab text still at 7.01:1.

Changing it exposed five places that painted `#fff` on `var(--rose)` — the
pill, the active rail item, the tab count, the file-manager selection, the
catalogue monitor. In light mode `--rose` is a deep crimson and that reads at
8:1. In dark it is a pale pink and the same white reads at **2.40:1**.
`--slab-rose-fg` already existed for exactly this (white in light, near-black
in dark); those five simply never used it. Now 6.92:1 dark, 5.59:1 light.

### 5H.3 Every modal in the app was one stalled frame from invisible

Found while reviewing an agent's work, and the most serious thing in the
pass. `.modal` carried `animation:pop .24s ... both`, `@keyframes pop` began
at `opacity:0`, and unlike every other entrance in the app it was **not**
gated on `html.js-motion`. A document timeline that never advances — a
background tab, a throttled preview, a stalled main thread — and `fill:both`
holds the FROM state forever: an invisible dialog, a live scrim over the
page, and no way out but Escape. Every confirmation in the app is a modal.

`pop` is scale-only now and the rule is gated. This is the third time the
opacity-from-zero rule has been broken in this codebase; it is written up in
§4 and it will happen again.

### 5H.4 The rest

| | |
|---|---|
| **Random polygon** | Six clip-path shapes, and `hl()` stamps each blob its own duration, starting phase and direction. Measured on the fleet screen: 30 blobs, all 6 shapes, 9 distinct durations in the first 10. Nothing runs while idle — the animation only exists under `:hover`. |
| **Press** | One transition served press *and* release, so an overshooting spring ran on the way DOWN. Press is 55ms and non-overshooting; release keeps the spring. On release it throws petals — gold for primary, and `.btn.danger` comes apart upward into ash. The shards live in a fixed layer on `<body>`, not inside the button, so they cannot collide with the layering contract. |
| **Shine → weave** | The panel-header sheen ran a light across *every* panel on a 7s loop, forever; card and KPI fills were diagonal two-tones. Replaced with a static asanoha lattice at ~3% alpha (`--washi`, three repeating gradients). Gold leaf on the few display numbers stays. |
| **Backdrop** | A cherry tree per theme, drawn as SVG with the blur baked in (39/79 KB) behind a paper scrim. `dev/tools/make_backdrop.py` generates both. `/assets/backdrop/{theme}` resolves server-side, so dropping a real photograph in as `bg-sakura-<theme>.<ext>` takes over with no code change. |
| **Heap slider** | The "not smooth" was the step: the input moved in whole gigabytes. It now runs at a fifth of a snap and settles the *committed* value to the snap. Strain is a continuous smoothstep (0 below 70%, 1.0 at the ceiling) driving a tremor, a heat wash and a colour shift — not a class flip at a threshold. The ceiling is drawn as a torii. |
| **Silent tuning** | `optimizer.advise()` folds a validated, clamped model answer into the deterministic plan, and degrades to it on every failure path. The Tune screen shows an **ofuda** naming the model and every value it moved, with the old number struck through. Silent means uninterrupting, not unaccountable. |
| **Racks** | `#/groups`. Torii lintels with a rope strung under each and servers hanging as ema plaques, crooked at their own angle. `app/fleetgroups.py` holds the layout and the activity record; the rail shows 5, running first. |
| **Koma** | Clicking a mod row — but not its controls — opens a comic panel of the project's own page. Untrusted upstream HTML is never `innerHTML`'d and links inside descriptions are dropped. |

### 5H.5 A torn state file, found by a real power cut

Reported mid-review as "the racks are gone after I restarted the device".
`fleet-state.json` was 559 bytes of NUL: the file length had been recorded
and the contents had not.

`fleetgroups._save` wrote a temp file and renamed it, which is **atomic**
but not **durable** -- the rename can be flushed before the data. Every
other state writer in the app was worse: `whitelist.py`, `ai.py`,
`optimizer.py`, `watcher.py`, `uploads.py`, `backups.py` and `roulette.py`
all wrote straight over the live file, so a crash part-way through left a
truncated file that `json.loads` then rejected, silently discarding the
data. There was no `fsync` anywhere in the codebase.

`config.write_state` now does temp -> fsync -> rename -> fsync the
directory, and `config.read_state` treats a missing file and a torn one as
the same thing. All nine call sites use it. `test_fleet_state.py` writes a
file of NULs over a saved layout and asserts the app starts clean and keeps
working.

### 5H.6 Route transitions, and the rope that would not stay still

Reported after the first review. Two more things, both fixed by moving
something rather than by adding something.

**Navigation flashed.** `route()` blanked the page, showed a spinner, then
awaited a dynamic import AND a network round trip before dropping the
finished screen in at once -- two jarring moments per navigation, the
second of which moved every element. Each route now declares the SHAPE it
is about to become (`skel` in the routes table) and `pageSkeleton` paints
it **synchronously**, before anything is awaited, so the frame after a
click already has the right layout in it. The skeleton lives in the routes
table rather than in the view module on purpose: the module itself has to
be fetched on first visit, and a skeleton that waits for its own module
has missed the moment it existed for. `heldFor` keeps it up for at least
170ms so a fast screen does not flicker. Instance tabs do the same per
tab, which also stops the tab strip jumping as the body under it changes
height.

**The rope moved with the plaques.** `.hanger` and `.cord` were children
of `.ema-plaque`, so every transform on the plaque -- the hand-hung angle,
the sway, the hover lift -- moved the rope too, and a row of plaques tore
the strung line into pieces that slid around independently. Three
counter-transforms got most of it: an equal-and-opposite rotate for the
angle, a mirrored sway on the same timing, and a counter-translate for the
lift. The first two were exact. The third could not be, because the hover
also turns the board in 3D, and a child cannot undo a parent's perspective
projection with a translate.

The fix was one div. `.plq-body` wraps everything that moves; the rope and
cord are its siblings. Measured by driving the sway animation frame by
frame: the board swings 1.16px and the rope sits at the same pixel at
every frame.

### 5H.7 What this pass was checked against

195 front-end checks, 171 backend across six files, both themes, zero console
errors. `dev/tools/test_fleet_state.py` is new (27 checks).

**`check_api` was wrong, and had been for a while.** It matched a call
against a route by PREFIX in either direction, so `/api/fleet/groups` passed
because `/api` prefixes everything — 194 checks were green with **ten routes
missing**. It now normalises both sides to the same shape and compares
exactly. If you add a route, this is the check that will tell you when the
front end and the server disagree.

---

## 5I. The fourth UI pass (2026-09-11, later) — seven reported items

### 5I.1 The mod install that was never broken

Reported as: `upload modernfix-neoforge-5.27.24+mc1.21.1.jar failed:
{'detail': 'Not Found'}`.

`{'detail': 'Not Found'}` is FastAPI's own 404 shape, and the client posts
to `/api/v2/servers/{id}/files/upload`, which is correct for Crafty 4. The
**mock** had no upload route, so it 404'd. BlessForge was right the whole
time.

The mock now implements the real chunked protocol, and implements it
strictly: a missing `fileId` / `fileName` / `location` / `fileSize` header
is a 400, and every chunk's SHA-256 is verified rather than trusted. A mock
that accepts anything proves nothing. It also moved into the repo at
`dev/mock/crafty.py`, because it had only ever existed in a scratch
directory and was being rebuilt from memory each session — and a gap in a
test harness reads exactly like a bug in the thing it is testing.

### 5I.2 Navigation, and why it flashed

`route()` blanked the page, showed a spinner, then awaited a dynamic import
AND a network round trip before dropping the finished screen in at once.
Each route now declares the SHAPE it will become and `pageSkeleton` paints
it **synchronously** — measured at 10-11ms after a click, versus a blank
page until the fetch landed. The skeleton then stays on top and **dissolves
off** the real content underneath.

The direction of that fade is the safety argument. Fading content IN means
starting it at `opacity:0`, which is the bug this codebase has shipped
twice. Fading the skeleton OUT means the worst case is a skeleton that
lingers — and it is removed on a 400ms timer regardless, so it cannot.

### 5I.3 Why the activity drawer stuttered

`paint()` did `mount(bodyEl, ...jobs.map(jobCard))` on **every frame** —
`mount` is clear-then-append, so every card, log line and `<img>` was a new
node many times a second. That restarted the log lines' fade-in from
`opacity:0`, restarted the working GIF from frame 0, dealt every button's
blob a fresh random phase, reset both scroll positions, and replaced the
progress bar's `<i>` so its `transition:width` never ran. A `setInterval`
re-render every second did the same thing again while nothing was
happening.

Cards are now patched in place. Measured over a whole job: the card node is
**identical** before and after, with 5 structural additions and the log
appended rather than rebuilt.

Three further faults fell out of reading it: the reconnect path built a
*new* record, resetting the elapsed clock to zero and discarding the log;
`h('div.joblog', { ref: 'log' })` was dead, `ref` not being a core prop;
and `#drawer .body{display:flex}` outranked the UA's `[hidden]`, so
`bodyEl.hidden = true` did nothing and **the collapse control had never
collapsed anything**.

### 5I.4 The rest

| | |
|---|---|
| **The shrine road** | A job is a walk between two torii with a shimenawa strung between them; the walked length is gold leaf, a knot sits at each phase boundary, and the working charm rides the rope at the job's own percent. A modpack install gets a five-leg road whose knots sit on `installer.py`'s own percent anchors, a tally of petal ticks parsed from `Downloading mods (142/301)`, an ETA, and — the thing the old drawer could never say — `quiet for 47s` when no frame has arrived. Working and hung no longer look identical. |
| **Finished jobs clear on close** | Verified: 3 jobs (2 done, 1 running) → closing the drawer leaves exactly the running one, with its `EventSource` still open. |
| **Vital signs** | CPU is a torii whose pillars rise with load; memory is a chōchin lantern filling with light; players are sakura petals, filled for occupied seats. They read from across the room instead of from three bars in the header's corner, and a stopped server shows no reading rather than a `0%` that looks like data. |
| **The rope** | One rope per ROW, drawn once, with cords dropping to each plaque — rather than a segment per plaque overhanging half a column gap and relying on the gap never changing. Aligned by construction. |
| **The plaque** | State as an icon AND a word (a green dot and a grey dot are the same dot to a colourblind reader), players, version, loader, port, mod count, last ran. Still a wooden board on a rope, not a card. |

### 5I.5 The catalogue keeps going

Both catalogue screens paged with Prev/Next, which is the wrong shape for
browsing and, worse, a shape CurseForge cannot support: its search reports
neither a total nor a last page, so Next was always enabled and the only way
to find the end was to walk off it.

`core.endlessFeed` appends instead. Four things it has to get right, each a
real failure rather than a nicety:

- **A new search cancels an in-flight page.** Type `create`, then `cre`:
  without a token the slower first request lands second and the grid fills
  with results for a query nobody asked for.
- **Duplicates.** CurseForge pages by offset over a set it re-ranks per
  request, so the same project genuinely arrives twice. Deduped by key.
- **The end** is a page shorter than `size` -- counted on the raw batch, not
  on the deduped one, or a full page of duplicates would read as the end.
- **Cleanup.** A route change does not disconnect an IntersectionObserver;
  the view's `dispose` does.

The sentinel is watched against the nearest scrollable ancestor rather than
the viewport, because the add-mod browser lives in a modal with its own
`overflow-y:auto` -- and a sentinel inside an overflow container never
intersects the viewport once scrolled past, so a viewport-rooted observer
would silently never fire there. Verified in both: Discover 72 -> 96 cards
from scrolling alone, the modal 20 -> 40.

The `Load more` button is not a fallback nobody sees. It is the keyboard
path, and it is what works when the sentinel cannot intersect anything --
which is exactly what happens in a hidden browser pane, where `innerHeight`
is 0 and no observer fires at all.

### 5I.6 What this pass was checked against

197 front-end checks, 171 backend across six files, nine routes with zero
console errors, both themes.

---

## 5J. The mobile drawer, and the half of it a hit test cannot see

The CasaOS instance found the first half and fixed it in `6a356a4`: the
scrim was `body.rail-open::after`, so its z-index competed with `#shell`'s 2
rather than with the rail's 60 — it painted over the open drawer and
swallowed every tap. That diagnosis is right, the fix is right, and
`v21-rail-mobile.mjs` is the right kind of test for it: hit-testing rather
than screenshotting, because the drawer looks perfect in a picture.

It was still reported as frozen, and the reason is that every check in that
suite is about a **single point** — is this pixel the topmost element, does
this tap land. The remaining fault was about **movement**.

The rail is a fixed overlay and is not itself a scroller, and the document
behind it was never locked. So a drag starting anywhere on the open drawer
chained straight through to the page: the background slid away under the
finger while the drawer sat still. From the other side of the screen that is
indistinguishable from a drawer that has seized up, and neither a screenshot
nor a hit test can see it.

`setRailOpen` now owns the drawer's whole state — the class, the lock, and
the scroll offset — and every one of the five places that used to poke
`rail-open` directly goes through it. `position:fixed` on body rather than
`overflow:hidden`, because iOS Safari honours the first and ignores the
second for touch scrolling; the offset is captured and restored, or
dismissing the drawer would jump you to the top of a long fleet. The rail
itself became a scroller with `overscroll-behavior:contain`, which is the
same fault one level down.

Measured: page at 156 → open → `position:fixed`, `top:-156px` → an attempt
to scroll to 2000 moves nothing → close → back to 156 exactly, across three
consecutive runs.

Two smaller things found with it: the rail footer's theme toggle and
Activity button were 36px touch targets at the very bottom edge of the
drawer, where a thumb is least accurate (`.btn.icon.sm` is (0,3,0), so the
obvious `.rail-foot .btn` at (0,2,0) loses to it — the override has to name
the same classes); and their test's `open()` helper poked `rail-open`
directly, which now leaves the body fixed with no way back, so it drives the
toggle instead.

Seven checks added to `v21-rail-mobile.mjs` covering the drag dimension.
They cannot be run from the Windows dev box — it has neither Docker nor
puppeteer — so they are verified there by the equivalent assertions in a
real browser and are **unrun on the box until someone runs that suite**.

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

24. **A broken ES module fails at the route that imports it, and nowhere
    else.** There is no build step, so a typo in `views/tab-mods.js` is a 404
    or a parse error the first time someone opens that tab. `python
    dev/tools/check_frontend.py` parses every module *as a module*, resolves
    every import and asset, and checks every API path the front end names
    against the routes the server actually serves. Run it after touching
    anything under `app/static/`.
25. **`node --check <file>` is not a syntax check for a module.** It parses as
    a *script* and will happily accept a file with a broken string literal in
    it. Only `node --input-type=module --check` is real. That distinction cost
    a screen once.
26. **Writing a state file is not the same as saving it.** Every state
    writer in this app was one `write_text` straight over the live file,
    which is neither atomic (a crash mid-write truncates it) nor durable
    (a rename can reach the disk before the bytes do). A hard restart on
    2026-09-11 brought `fleet-state.json` back as **559 NUL bytes** -- the
    right length, no contents. Use `config.write_state`, which fsyncs the
    temp file before renaming and fsyncs the directory after; and read
    through `config.read_state`, which treats a torn file as absent rather
    than letting it reach a parser. Nine call sites were converted; if you
    add a tenth, use the helper.
27. **The layering contract is invisible when it breaks.** A new
    `.card > *` rule, or any new `z-index:-1`, silently moves the wiggling
    polygon in front of the card fill. `check_frontend.check_layering` catches
    both; nothing else will, short of hovering a card and noticing.
28. **A CurseForge key sourced by bash loses its `$` segments.** Every
    key is bcrypt-shaped -- `$2a$10$...` -- and `dev.sh` does
    `set -a; . ./.env`, where `$2a` and `$10` are positional parameters.
    A 60-character key with three `$` segments arrives as 47 characters
    with none, and every request then fails with a 403 that reads exactly
    like a revoked key. **Single-quote the value in `.env`.**
    `config.curseforge_key_warning()` now catches it (it checks the key
    still starts with `$2`; it used to check only the length, which a
    fully-eaten key passes).
29. **A CSS animation naming a keyframe that does not exist is silent.**
    No warning, no error, no fallback -- the element keeps its static
    styles. Renaming `p5-wiggle-poly` to six numbered shapes left four
    rules in app.css naming the old one, and the blobs they belonged to
    stopped moving and sat at full opacity instead: the instance tabs lost
    their labels behind one and the rack plaques grew a grey box.
    `check_frontend.check_keyframes` now catches this.
30. **Two servers cannot both hold 4 GB on this host.** The Tune screen's
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

# the whole suite: 171 backend + 197 front-end checks
cd ~/blessforge
for t in test_loader_detection test_job_stream test_install_decisions          test_roulette test_boot_verdict test_fleet_state; do
  .venv/bin/python dev/tools/$t.py | tail -1
done
.venv/bin/python dev/tools/check_frontend.py | tail -1
# check_frontend.py wants `node` on PATH for its parse check and skips it
# cleanly without one -- but that is the check worth having (trap 25).

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

# JS syntax check without node on the host
docker run --rm -v "$PWD/app/static/js":/w:ro node:20-alpine sh -c '
for f in $(find /w -name "*.js"); do
  node --input-type=module --check < "$f" || echo "FAIL $f";
done; echo ok'

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
