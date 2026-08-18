# BlessForge

A CurseForge-style front end for [Crafty Controller](https://craftycontrol.com/).
Search modpacks, install one into Crafty as a real server instance, then manage
the mods, configs, performance and startup problems of every instance from one
place.

Talks to Crafty over its HTTP API, so it can run on a different machine — no
shared volumes required.

![port 8710](https://img.shields.io/badge/port-8710-orange)

---

## What it does

### Install modpacks the way CurseForge does, but for servers

- Browse and search CurseForge modpacks, filtered by Minecraft version and loader.
- Install any version, not just the latest.
- Uses the official **server pack** when the version has one.
- When it doesn't — which is common — reads the pack's `manifest.json` and
  downloads every server-side mod individually to assemble a working server.
- Crafty installs the matching Forge / NeoForge / Fabric loader from its own
  catalogue, so the launch command is correct by construction.
- Picks the Java version the loader actually supports, and re-checks it on
  every start.

### Install a pack you built yourself

Most self-hosters run a pack they assembled, not one they downloaded — and a
private pack is in no catalogue, so no search will ever find it. Export it
instead:

1. In the CurseForge app: **My Modpacks** → the **…** menu on your profile →
   **Create Profile Export**. Tick Mods, Config, and any script folders
   (KubeJS, Open Loader).
2. In BlessForge: **Import Export** — on the Instances view, on Browse, or via
   the card on the Browse page — then drop the `.zip` in.

From there it is the same path as a catalogue pack: the archive is analysed
offline (loader, Minecraft version, mod count, the RAM the pack asks for), the
mods it lists are fetched from CurseForge, jars you added by hand in
`overrides/mods/` come straight out of the archive, and you get the same
client-only review before anything is written.

Notes:

- Client-only folders — `shaderpacks`, `resourcepacks`, `saves`, `options.txt`
  and friends — are dropped automatically, so it does no harm to leave them
  ticked when exporting.
- Hand-added jars are reviewed too. They have no CurseForge project behind
  them, so the jar's own metadata is the only evidence available — and it is
  the strongest signal BlessForge uses anyway.
- Archives are kept on the server (`/data/uploads`, most recent 12) so
  re-installing does not mean uploading a 900 MB zip a second time.
- To update an imported pack, export it again and use **Re-import an Updated
  Export** on the instance's Modpack tab. The world, name and port are kept;
  mods and pack configs are replaced.
- A client export names its mods by CurseForge id, so a CurseForge API key is
  required for this — the same one the rest of the app uses.

### Review what gets stripped, before it happens

Client-only mods have to come out of a server install, but doing that silently
is how a working pack turns into a missing-dependency crash. So BlessForge
shows you the list first, with the evidence behind each call:

| Verdict | Meaning |
|---|---|
| **remove** | The jar itself declares `environment=client`, or Modrinth says `server_side: unsupported`. Ticked by default. |
| **review** | Weaker signals — a known client-mod name, or it requires a client-only library like YACL. Left for you to judge. |
| **keep** | Looks client-only *but another mod in the pack requires it*. Never removed automatically. |

Every jar in the pack is inspected, not just ones with suspicious names — the
mods that break servers are usually the ones nobody thought to put on a list.
Mods Modrinth marks `server_side: required` are cleared outright. This runs for
imported packs as well, including jars that only exist inside the archive.

### Manage mods

- Icons, toggle switches, and the installed version shown next to a **Change…**
  picker listing every other build with its Minecraft/loader compatibility.
- **Dependencies are resolved and installed automatically**, recursively, with
  a preview you can untick. Mods already present — even under a different
  filename, even installed by a modpack — are detected so you never get two
  copies and a duplicate-mod crash.
- Add from **CurseForge or Modrinth**, filtered to the instance's loader.
- **Identify unknown jars** by hashing them: CurseForge murmur2 fingerprints,
  then Modrinth SHA-1, then the jar's own metadata. Works on servers that
  existed long before this app.
- Bulk enable/disable/delete, update checks, and whole-modpack version switching.

### Edit configs

Browse `config/`, `defaultconfigs/`, `kubejs/`, `scripts/` and the usual root
files, grouped by owning mod. World data and binaries are excluded from the
editor.

### Troubleshoot

- **Checks** — EULA state and format, Java/Minecraft mismatch, client-only mods,
  low memory, mods that failed to install, mods built for another game version.
- **Log analysis** — parses `latest.log` and the newest crash report for missing
  dependencies, duplicate mods, port conflicts, out-of-memory, wrong Java,
  mixin failures and client-only crashes. Where the trace names a jar, the
  finding names it too.
- **Deep scan** — downloads every jar, reads its declared dependencies and
  builds the graph, so missing and duplicate mods surface *before* a launch.
- **One-click fixes** — accept EULA, set Java, disable the offending mods,
  search for a missing dependency, or swap mods to a compatible version.

### AI assistant (optional)

Points at a local [Ollama](https://ollama.com/) model — a 1–2 GB instruct model
is plenty. It reads the deterministic findings plus the log and explains what
is most likely wrong, proposing a plan.

Deliberately constrained, because a 3B model is a good pattern-matcher and a
poor decision-maker:

- It **never executes anything**. Every action comes back for you to approve.
- Its output is validated against a fixed action vocabulary; anything it
  invents is discarded, including filenames not present in your instance.
- Actions are classified `safe` or `major`. Deleting mods, replacing versions
  and editing properties are **major** and need a second explicit confirmation.
- The deterministic checks run first and are fed in as evidence — the model
  explains and prioritises, it is not the detector.

```bash
ollama pull qwen2.5:3b-instruct-q4_K_M
```

### Optimize for the machine

Reads the host's RAM and CPU and the pack's recommended RAM, then proposes:

- A **heap size** capped at what the host can actually give. A pack asking for
  8 GB on a box with 5 GB free will die at startup with no log, so the ceiling
  comes from the host — and it tells you when it had to overrule the pack.
- **JVM flags** (Aikar's set, adjusted for heap size and core count), each one
  individually toggleable with a plain explanation of what it does and why it
  was suggested.
- **server.properties** performance values — view distance, simulation
  distance, watchdog and chunk-write settings.

Nothing is applied until you pick it. Flags you untick stay untouched.

The same tab also carries:

- **The server port.** Written to `server.properties` *and* Crafty's own
  record together — setting only one leaves the server running but permanently
  displayed as offline. It refuses a port another instance already claims
  (unless you override), and warns when the port falls outside the range
  Crafty's container publishes, where the server works inside Docker but is
  unreachable from your network.
- **The whole of `server.properties`**, every key editable, grouped and typed:
  toggles for booleans, dropdowns for enums, number fields for integers, with
  a short note on what each one does. Keys your mods add are shown too rather
  than hidden. `server-port` is deliberately read-only here and points at the
  port control above, so the two can never drift apart.

---

## Setup

### 1. Get the two credentials

**Crafty API token** — Crafty → your user → API keys → generate. The token's
user needs **Server Creation**, **Files**, **Commands** and **Config**.

**CurseForge API key** — <https://console.curseforge.com/> → API Keys.

### 2. Deploy

One compose file installs BlessForge everywhere — CasaOS or plain Docker. It
pulls the published multi-arch image, so nothing needs building on the target
machine:

```
ghcr.io/chargeyxd/blessforge:latest    (amd64 + arm64)
```

**On CasaOS** — App Store → **Custom Install** → **Import** → paste the
contents of `docker-compose.yml` → set `CRAFTY_URL` and `CRAFTY_TOKEN` in the
dialog → install. The CurseForge key is already filled in.

**On plain Docker:**

```bash
curl -O https://raw.githubusercontent.com/ChargeyXD/BlessForge/main/docker-compose.yml
$EDITOR docker-compose.yml     # CRAFTY_URL and CRAFTY_TOKEN
docker compose up -d
```

Open `http://<host>:8710`.

**From a checkout** (runs your working tree instead of the published image):

```bash
git clone https://github.com/ChargeyXD/BlessForge blessforge
cd blessforge
cp .env.example .env
$EDITOR .env                   # CRAFTY_URL, CRAFTY_TOKEN
docker compose up -d --build
```

`docker-compose.override.yml` is what makes that work: Compose picks it up
automatically from a checkout, swaps in `build: .` against the same image tag,
and reads `.env` in `raw` format so a CurseForge key containing `$` survives.
CasaOS never sees the override file — it imports `docker-compose.yml` alone.

> **The CurseForge key is passed as base64, and that is not optional.**
> CurseForge keys are bcrypt-style and contain `$`. Compose expands `$name`
> inside an `environment:` block, and CasaOS un-doubles `$$` when it stores an
> imported compose before handing it back to Compose, which expands it *again*
> — no amount of doubling survives both, and the key arrives truncated with
> every call 403ing and no error that says so. `CURSEFORGE_API_KEY_B64` has no
> `$` in it and passes through untouched. To use a different key:
> `echo -n '<your key>' | base64 -w0`. A plain `CURSEFORGE_API_KEY` still works
> if you would rather fight the escaping.

> **Why the cache is a named volume, not `/DATA/AppData`.** CasaOS creates
> bind-mount directories as root, but the container runs unprivileged as uid
> 1000 and could not write there — every download would fail to cache with a
> permission error. Docker seeds a fresh named volume with the image's own
> ownership, so `blessforge-cache` just works.

Anything still missing is listed in a banner at the top of the app.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CRAFTY_URL` | — | Crafty base URL with scheme and port, e.g. `https://192.168.1.10:8443` |
| `CRAFTY_TOKEN` | — | Crafty API token |
| `CURSEFORGE_API_KEY` | — | CurseForge Core API key |
| `CRAFTY_VERIFY_SSL` | `false` | Verify Crafty's certificate. Crafty is self-signed by default |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama endpoint for the AI assistant |
| `AI_MODEL` | `qwen2.5:3b-instruct-q4_K_M` | Local model to use |
| `AI_ENABLED` | `true` | Set false to hide the AI panel |
| `AI_TIMEOUT` | `180` | Seconds to wait for the model |
| `HOST_RAM_GB` / `HOST_CPU_COUNT` | auto | Describe the **Crafty** host when it is a different machine |
| `MODRINTH_ENABLED` | `true` | Offer Modrinth as a second mod source. Needs no key |
| `DOWNLOAD_CONCURRENCY` | `8` | Parallel mod downloads |
| `SERVER_READY_TIMEOUT` | `900` | Seconds to wait for a loader install |
| `DEFAULT_MEM_MIN` / `DEFAULT_MEM_MAX` | `2` / `6` | Fallback RAM in GB |
| `MAX_UPLOAD_MB` | `4096` | Size ceiling for a single imported pack archive |
| `MAX_UPLOADS` | `12` | Imported archives kept on disk; the oldest are pruned |
| `UPLOAD_DIR` | `/data/uploads` | Where imported archives are stored |

> **`CRAFTY_URL` must be reachable from inside the container.** Use the LAN IP.
> `127.0.0.1` points at the container itself.

> **If Crafty runs on another machine**, set `HOST_RAM_GB` and `HOST_CPU_COUNT`.
> The optimizer measures from inside its own container, and sizing a heap
> against the wrong machine is worse than not tuning at all.

`/data` is mostly a download cache — deleting `/data/cache` costs nothing but
bandwidth. `/data/uploads` is the exception: an imported pack export exists
nowhere else unless you still have the zip, so keep that if you keep anything.

---

## How an install works

1. Fetch the pack archive — server pack if one exists, otherwise the client zip.
   An imported pack skips this step: the archive is already on disk, and is
   opened from there rather than read into memory.
2. Read loader and Minecraft version from whichever manifest the archive ships
   (CurseForge, ServerPackCreator, Modrinth index, or `variables.txt`).
3. *(Optional)* Inspect every mod jar and present the client-only review.
4. Create the Crafty instance with the matching loader, so Crafty generates the
   launch command itself.
5. Wait for the loader install to finish **and for Crafty's server record to
   settle** — Crafty rewrites the launch command when its installer thread ends.
6. Overlay the pack's files in bounded batches: zip → upload → Crafty unzips.
   Memory stays flat no matter how large the pack is.
7. Accept the EULA, size the heap to the host, write JVM flags, pin Java.
8. Record everything in `.blessforge.json` inside the instance.

That manifest lives in the server directory, so it survives reinstalling this
app and an instance carries its own history with it. Instances created by the
previous version are still read via their old `.modpack-studio.json`.

---

## Notes and limits

- **Loader builds.** Crafty's catalogue offers one loader build per Minecraft
  version, usually the newest. A pack pinned to an older build normally runs
  fine on a newer one in the same line, and the install log states both.
- **Quilt** is not in Crafty's catalogue, so Quilt packs cannot be created
  automatically. Fabric, Forge and NeoForge all work.
- **Mods that block distribution.** Some authors disable third-party downloads
  and CurseForge returns no download URL. The deterministic CDN path is used as
  a fallback; anything that still fails is listed in the install report.
- **Client-only detection is evidence-based, not perfect.** A mod that declares
  `environment=*` while calling client-only code at runtime cannot be caught
  with certainty — that is why the review step exists, and why such mods land
  in **review** rather than being removed silently.
- **An imported pack has no version history.** A private export was never
  released, so there is no list of releases to move between and no update
  check for jars you added by hand. Re-import a fresh export to update it.
- **Archive paths are checked, not trusted.** Members of an imported zip whose
  path points outside the server directory are dropped, and the count is
  reported as a warning on the archive and in the install log.

### Two traps worth knowing about

**Your CurseForge key contains `$`.** Keys are bcrypt-style
(`$2a$10$D3Bo...`), and Docker Compose expands `$D3Bo...` inside environment
values, so the key arrives **truncated** and every call 403s with nothing to
explain it. This repo uses `env_file` with `format: raw` (Compose v2.24+) to
avoid it. If you paste the key into an `environment:` block instead — which is
what the **CasaOS dialog does** — double every `$`: `$$2a$$10$$D3Bo...`.
BlessForge detects a truncated key and says so rather than showing a bare 403.

**Crafty's EULA start-gate.** Crafty compares the *first line* of `eula.txt`
against an exact list (`eula=true`, `eula = true`, …) using `readline()`, which
keeps the trailing newline — so a file written as `"eula=true\n"` never matches.
Crafty then refuses to launch, writes **no log and no error**, and only pushes
an EULA prompt to its own web UI. BlessForge writes the byte-exact form, flags
the broken form on the Troubleshoot page, and normalises it on every start.

---

## API

The UI is a thin client over a plain HTTP API:

```
GET  /api/health
GET  /api/ai/status
GET  /api/host/specs

GET  /api/browse/modpacks?q=&game_version=&loader=
GET  /api/browse/mods?q=&source=curseforge|modrinth
GET  /api/modpacks/{id}/files

POST /api/uploads/modpack            multipart "file"       -> imported archive
GET  /api/uploads                                           -> archives on disk
DEL  /api/uploads/{upload_id}

POST /api/install/preflight          {mod_id, file_id} | {upload_id}
                                                            -> client-only review
POST /api/install/modpack            {mod_id, file_id} | {upload_id},
                                      server_name, port, exclude_files[], optimize
POST /api/instances/{id}/switch-pack-version
                                     {mod_id, file_id} | {upload_id}
GET  /api/jobs/{id}/events           server-sent progress

GET  /api/instances
GET  /api/instances/{id}
POST /api/instances/{id}/action/{start_server|stop_server|restart_server}

GET  /api/instances/{id}/mods
POST /api/instances/{id}/mods/toggle    {file, enabled}
POST /api/instances/{id}/mods/resolve   {source, project_id}  -> dependency plan
POST /api/instances/{id}/mods/add       {source, project_id, file_id,
                                         with_dependencies, skip_dependencies[]}
POST /api/instances/{id}/mods/identify
GET  /api/instances/{id}/mods/updates

GET  /api/instances/{id}/configs
GET  /api/instances/{id}/configs/read?path=
POST /api/instances/{id}/configs/write  {path, content}

GET  /api/instances/{id}/diagnose
POST /api/instances/{id}/deep-scan
POST /api/instances/{id}/ai/analyse     {question?}
POST /api/instances/{id}/ai/apply       {actions[], confirmed:true}
POST /api/instances/{id}/fix/{accept-eula|java|set-ram|versions}

GET  /api/instances/{id}/optimize       -> host specs + proposal
POST /api/instances/{id}/optimize       {heap_gb, flags[], properties{}}
```

Long operations return `{"job_id": ...}`; follow `/api/jobs/{id}/events`.

The three install endpoints take **either** CurseForge ids **or** an
`upload_id` from a previous import — never a mix.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.test && $EDITOR .env.test
./dev.sh start        # http://127.0.0.1:8710
./dev.sh restart|stop|status
```

### Front end

Three files, served verbatim from `app/static/`: `index.html`, `style.css`,
`app.js`. No build step, no framework, no runtime network dependency — plain
ES2020 in one IIFE, every icon inline SVG, and the web font loaded
non-blocking so a LAN box with no route to the internet still paints
immediately. `app.js` renders by assigning `innerHTML` from template literals
and binds by `id` and `[data-*]`, so those names are a contract:
`design-brief.md` §4.3 lists every hook that must survive a redesign.

Do not add a fourth file. The server's cache-busting fingerprint only hashes
and rewrites those three names, so anything else would be served stale
forever.

Three things in there are less obvious than they look:

* **The mod list is windowed above 120 rows.** Selection lives in a `Set` of
  filenames rather than in the DOM, because a row that scrolls out of the
  window is unmounted and would otherwise lose its checkbox. Toggling patches
  the data array first and the row only if it happens to be mounted.
* **A job owns its stream, the modal is only a view onto it.** *Run in
  Background* closes the view, not the `EventSource`; Activity's **Watch**
  button re-attaches and replays the buffered log. A job that finishes
  resolves into a summary the user dismisses, so the warnings that predict a
  failed first boot outlive the install.
* **The config editor's gutter is a plain `<pre>`** sharing the textarea's
  font metrics, with `scrollTop` mirrored. No highlighting overlay: it drifts
  on wrap and is a maintenance trap.

## Licence

MIT.
