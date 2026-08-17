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
Mods Modrinth marks `server_side: required` are cleared outright.

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

```bash
git clone <this repo> blessforge
cd blessforge
cp .env.example .env
$EDITOR .env          # CRAFTY_URL, CRAFTY_TOKEN, CURSEFORGE_API_KEY
docker compose up -d --build
```

Open `http://<host>:8710`.

### On CasaOS (including a second server)

Use **`casaos-compose.yml`**, not `docker-compose.yml`:

**App Store → Custom Install → Import** → paste the contents of
`casaos-compose.yml` → fill in the three environment variables → install.

It pulls the published image, so nothing needs building on the target machine:

```
ghcr.io/chargeyxd/blessforge:latest    (amd64 + arm64)
```

> **Why a separate file.** `docker-compose.yml` is for local development: it
> uses `build: .` with the local tag `blessforge:latest`. CasaOS has no source
> checkout, so it ignores `build:` and tries to *pull* `blessforge:latest` from
> Docker Hub — which does not exist. The pull fails, the container is never
> created, and CasaOS reports the app as **unhealthy**. That is the usual cause
> of a "legacy app" that refuses to rebuild.

> **Doubling the `$` in your CurseForge key is required here.** CasaOS writes
> settings into an `environment:` block, where Compose expands `$name`. An
> unescaped key arrives truncated and every CurseForge call 403s.
> `$2a$10$D3Bo...` becomes `$$2a$$10$$D3Bo...`. BlessForge un-doubles it on
> startup and warns you in the UI if the key still looks truncated.

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

> **`CRAFTY_URL` must be reachable from inside the container.** Use the LAN IP.
> `127.0.0.1` points at the container itself.

> **If Crafty runs on another machine**, set `HOST_RAM_GB` and `HOST_CPU_COUNT`.
> The optimizer measures from inside its own container, and sizing a heap
> against the wrong machine is worse than not tuning at all.

The `/data` volume is a download cache. Deleting it costs nothing but bandwidth.

---

## How an install works

1. Fetch the pack archive — server pack if one exists, otherwise the client zip.
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
POST /api/install/preflight          {mod_id, file_id}      -> client-only review
POST /api/install/modpack            {mod_id, file_id, server_name, port,
                                      exclude_files[], optimize}
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

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.test && $EDITOR .env.test
./dev.sh start        # http://127.0.0.1:8710
./dev.sh restart|stop|status
```

## Licence

MIT.
