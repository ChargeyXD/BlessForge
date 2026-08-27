# BlessForge — session handover

Written 2026-08-27. Everything below was verified on this machine at that
date, not recalled. Where something was *not* verified, it says so.

Start a new session with: **"Read `~/blessforge/HANDOVER.md`, then <the thing
you want fixed>."**

---

## 1. What this is

A self-hosted web app that installs CurseForge modpacks into **Crafty
Controller** and then manages those Minecraft servers: mods, config files,
`server.properties`, JVM tuning, crash diagnostics, and a local-LLM
troubleshooting assistant. One person self-hosting servers for friends.

It talks to Crafty **only over Crafty's HTTP API** — it never touches Crafty's
files or its AppData directory. That matters more than it sounds; see §7.

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
  main.py         1060  FastAPI: every route, static serving, cache-busting fingerprint
  installer.py    1049  the install pipeline; loader wait + repair (§5.3)
  diagnostics.py   745  health checks, crash-log parsing, findings
  crafty.py        658  Crafty API client — the only thing that talks to Crafty
  mods.py          548  mod listing, toggle, delete, identify
  ai.py            532  Ollama client, action vocabulary, re-validation
  packs.py         468  pack plans, archive shape detection, loader mapping
  preflight.py     424  client-only mod review
  properties.py    422  server.properties model
  specs.py         413  host specs, heap sizing
  curseforge.py    380  CurseForge API + download cache
  deps.py          332  recursive dependency resolution
  modrinth.py      232  Modrinth API
  jarmeta.py       232  jar fingerprinting
  uploads.py       231  imported .zip exports  [added this session]
  optimizer.py     217  Aikar flags, heap, property presets
  jobs.py          180  job model + SSE
  configs.py       168  config file tree
  config.py        157  env parsing, paths
  static/         index.html · style.css · app.js · icon.png
dev/              test tooling — see dev/README.md   [added this session]
design-brief.md   the GUI brief the front end was rebuilt from — §4.3 is a contract
entrypoint.sh     starts root, chowns /data subdirs, drops to uid 1000
```

Backend is FastAPI + httpx, no database. State lives in Crafty (a
`crafty_managed.txt` manifest per instance) and in `/data`.

---

## 4. Front end — the rules that are not obvious

**Three files, no build step.** `app/static/{index.html,style.css,app.js}` are
served verbatim. No npm, no bundler, no framework — plain ES2020 in one IIFE,
every icon inline SVG, the web font loaded non-blocking (`media="print"` swap)
so a LAN box with no internet still paints instantly.

**Do not add a fourth file.** `main.py`'s cache-busting fingerprint only hashes
and rewrites those three names; anything else would be served stale forever.

**`app.js` binds by `id` and `[data-*]` and renders by assigning `innerHTML`
from template literals.** Those names are a contract — `design-brief.md` §4.3
lists every hook that must survive a redesign. Renaming `#modList` breaks the
app silently.

**Everything interpolated goes through `esc()`.** Keep it that way.

Three mechanisms are more intricate than they look:

- **The mod list is windowed above 120 rows** (`VIRT_THRESHOLD`, `app.js:595`).
  Selection lives in `modSel`, a `Set` of filenames, *not* in the DOM, because
  a row scrolled out of the window is unmounted and would lose its checkbox.
  Events are delegated to `#modList` once (`bindModList`), so remounted rows
  never need rebinding. `applyModState` patches the data array first and the
  DOM row only if it happens to be mounted.
- **A job owns its stream; the modal is only a view onto it** (`jobRegistry`,
  `app.js:3116`). *Run in Background* closes the view, not the `EventSource`.
  Activity's `[data-watch]` button re-attaches and replays the buffered log. A
  job that finishes resolves into a summary the user dismisses — that is where
  the warnings predicting a failed first boot, and `result.problems[]`, finally
  get rendered.
- **The config editor's gutter is a plain `<pre>`** sharing the textarea's font
  metrics with `scrollTop` mirrored. No highlighting overlay — it drifts on
  wrap and is a maintenance trap.

---

## 5. What changed this session (8 commits, `23b38e7..c62d607`)

### 5.1 `22a2d70` — import modpacks exported from the CurseForge app

A self-hoster's pack is usually one they built themselves, and a catalogue
search can never find it. Uploading a profile export now goes through the same
preflight review and install path. The three install endpoints take **either**
`{mod_id, file_id}` **or** `{upload_id}`, never a mix.

Two archive-shape traps handled: a client export's `overrides/mods/` looks
exactly like a server pack (manifest is checked first now), and jars bundled
in `overrides/mods` have no project id so they can never be version-checked —
surfaced as `bundled` in the review rather than treated as catalogue mods.

### 5.2 `6203473`, `5a5fb19`, `88b9535` — front-end rebuild + packaging

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

### 5.3 `5cbaadd` — the `/data` disaster

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

### 5.4 `eb65549`, `0a8cc85` — "empty server, no launcher"

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

### 5.5 `c62d607` — dev tooling

See `dev/README.md`. Three jsdom harnesses (57 + 20 + 39 checks), a
headless-Chrome screenshot pass, an offline unit test for the loader state
machine, and `sweep_instances.py` which flags any instance whose executable is
missing from disk.

---

## 6. Open issues — the likely reason you are reading this

### Not verified (do this before trusting it)

1. **Light theme and small screens were never looked at.** The Chrome
   extension was not connected; the only screenshots taken are dark theme at
   1440px. The toggle *wiring* is tested, the *rendering* is not. Run
   `dev/ui-tests/screenshot.mjs` with `data-theme="light"` forced and at 360px.
2. **No complete modpack install was ever run end to end this session.**
   Preflight is verified, the loader repair is verified against a real broken
   instance, mod upload/config write are unchanged from before — but nobody
   watched an install go Browse → review → install → server ready. That is the
   single highest-value thing to do next.
3. **The import route (`.zip` upload) was never exercised against a real
   export** this session. Its DOM wiring is tested; the upload/analyse path is
   not.

### Known rough edges

4. **`jobRegistry` is never pruned** (`app.js:3116`). Every job a tab has seen
   keeps its full log buffer until reload. Harmless for hours, unbounded in
   principle.
5. **`modSel` is not cleared when switching instances** — it is pruned in
   `loadMods` against the new instance's file list, so a mod with the *same
   filename* in both instances can appear pre-selected. Narrow, but real.
6. **`_wait_for_loader` now raises on timeout, leaving the created instance
   behind.** Deliberate (the user may want to retry into it) but it means
   failed installs accumulate orphan instances. Consider offering deletion in
   the failure summary.
7. **`/api/health` writes a probe file every call** (every 45s from the UI).
   Negligible I/O, but it is a write on a health check.
8. **`craftyservercreationlog.har` (8 MB) is committed** — it went in with a
   `git add -A` in `eb65549`. **No credentials in it** (checked: no
   `Authorization`, no cookies, no JWTs, no CurseForge key), but it contains
   LAN IPs and server names, and it bloats the repo. Removing it from HEAD does
   not shrink existing clones; history rewrite would.
9. **The CurseForge API key is in the public repo**, base64-encoded in
   `docker-compose.yml`. Base64 is encoding, not encryption. This predates this
   session and appears deliberate (the friend's install needs it), but treat
   the key as disclosed and rotate if that is not intended.

### Environment hazards

10. **Editing the app in the CasaOS UI rewrites
    `/var/lib/casaos/apps/blessforge/docker-compose.yml`** — comments stripped,
    root-owned `600` again, volumes replaced with whatever the dialog held. It
    also **deleted the old named volume**, taking previously imported `.zip`
    exports with it. After any UI edit, re-check the mount:
    `docker inspect blessforge --format '{{range .Mounts}}{{.Source}}{{end}}'`
11. **The instance "Better MC [FORGE] BMC4" (`67da752c…`) has a working Forge
    but no mods** — its install died before that step and was repaired by hand.
    Re-run the pack install into it or delete it.

---

## 7. Traps that will bite you (all confirmed on this machine)

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
- **Never ship a named volume to CasaOS** (§5.3).
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

## 8. Quick reference

```bash
# state of everything
docker ps --filter name=blessforge --format '{{.Names}} | {{.Image}} | {{.Status}}'
curl -s http://127.0.0.1:8710/api/health | python3 -m json.tool

# any instance with a missing launcher?
cd ~/blessforge && set -a && . ./.env && set +a
.venv/bin/python dev/tools/sweep_instances.py

# Crafty's side of a failed install
docker exec big-bear-crafty sh -c "grep -n '<server-id>' /crafty/logs/commander.log"

# JS syntax check (no node on the host)
docker run --rm -v "$PWD/app/static":/w:ro node:20-alpine node --check /w/app.js

# UI tests — see dev/README.md for setup
cd dev/ui-tests && docker run --rm --network host -v "$PWD":/w \
  -v "$HOME/blessforge/app/static":/static:ro -w /w \
  -e BF_URL=http://127.0.0.1:8710 -e BF_SERVER_ID=<instance-with-mods> \
  node:20-alpine node 01-shell-and-views.mjs
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
