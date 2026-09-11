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

### Start a server from nothing

Most servers do not begin life as a downloaded pack: somebody picks a loader,
picks a version, and puts their own mods in. **New server** does exactly that
— a loader, a Minecraft version, a port, and an empty folder waiting for you.

Seven choices, each with the version list its own project publishes: Fabric,
Forge, NeoForge, **Paper**, **Purpur**, **Folia** and Vanilla. The folder the
wizard promises changes with the loader, because that difference is the most
consequential one on the whole screen: Fabric, Forge and NeoForge read
`mods/`, and the Paper family reads `plugins/`. A plugin dropped into `mods/`
does nothing at all and reports nothing anywhere, so the distinction is made
loudly rather than left to be discovered.

The version list is not limited to what Crafty has cached. Crafty's own jar
index is asked first — a version it knows is one it can install with no
fallback needed — and the loaders' own sites fill in the rest, which on a
fresh Minecraft release is most of it.

### Plugins, for the Paper family

CurseForge has no plugin catalogue at all: its Minecraft section is mods,
modpacks and resource packs. So a Paper server shops on **Modrinth**, which
does carry plugins and needs no key.

Every result is checked against what the server can actually load, and the
answer is more nuanced than a yes or no:

| Verdict | Meaning |
|---|---|
| **exact** | The plugin declares this server by name. |
| **good** | Built for Spigot or Bukkit; Paper and Purpur run those unchanged. |
| **risky** | Declares something else. It may work; nothing here can promise it. |
| **blocked** | On Folia, a plugin that does not declare Folia support. Folia's regionised threading breaks plugins written for a single main thread — it is not untested there, it will fail at load. |

Dependencies are resolved on top and previewed before anything is installed,
and the build chosen is the one for *your* server: spark publishes Fabric,
Forge, NeoForge and Bukkit jars against the same Minecraft version, and
picking the first that merely fits the version puts a mod in `plugins/`.

New Paper servers are offered the short list every one ends up with —
LuckPerms, EssentialsX, CoreProtect, spark, WorldEdit, Chunky, ViaVersion —
with a sentence on why each one is there, and anything without a build for
the chosen version marked plainly rather than offered and then failing.

**Audit `plugins/`** opens every jar in the folder and reports the two
mistakes that are otherwise invisible: a mod jar sitting where Paper will
silently ignore it, and a jar with no `plugin.yml` that Paper will refuse to
load.

### A file manager

The whole server directory, browsable and editable: upload, download,
rename, create, delete, extract a zip, and a real editor with a line gutter
for anything that is text.

Two guards run the whole screen, because this is the one place that can
destroy a world:

- `world/`, `backups/`, `libraries/` and friends are **listed** — knowing a
  world is 4 GB is exactly what a file manager is for — but writing or
  deleting inside one needs a second, explicit confirmation.
- `server.properties`, `eula.txt`, the launcher jar and the instance
  manifest are **marked**, because deleting one breaks the server in a way
  that only shows up at the next start.

Uploads are spooled to disk and handed to Crafty in chunks, and downloads are
streamed straight through. Neither ever holds a whole file in memory, which
matters when the container is capped at 1 GB and a region folder is not.

A sidebar measures the folders worth measuring — `mods`, `world`, `logs`,
`backups` — so "where did the disk go" is one glance rather than an
investigation.

### Organise the fleet

Servers hang on **racks** — named groups, each wearing a shrine mark. The
screen is a row of torii gates with a rope strung under each and every server
hanging from it as an ema plaque, crooked at its own angle. Drag a plaque to
another rack, or point at it and press `G` if you would rather not drag.

With no racks yet it offers three cut from your own fleet — by loader, by
version, modded or plain — so the first one costs a tap rather than a form.
Servers you have not filed hang in **the open yard**, which is a real rack
with a ghost gate rather than a hidden bucket.

The sidebar shows at most five: everything running first, then whatever ran
most recently, then whatever was installed most recently, and a "+N more" row
to the rest. "Ran 20 minutes ago" is recorded by BlessForge itself — Crafty
does not keep it — and an uptime is only claimed when the down→up transition
was actually witnessed, so a server found already running says "up now"
rather than inventing a start time.

### Manage players

Ops, whitelist, bans, IP bans, and who is connected right now — merged from
the five JSON files Minecraft keeps them in, so the tab is a list of people
rather than four lists of files.

The thing this gets right is the one that is normally wrong. A running server
keeps its own copy of all of this in memory: editing `ops.json` while it is
up does nothing until it restarts, and running `/op` while it is down is
impossible. So every button picks its route from the server's actual state —
the console command when it is running, the file when it is not — and then
**says which route it took**. "Effective now" and "takes effect at the next
start" are different facts, and hiding the difference is exactly how "I
opped them and it didn't work" happens.

Names are resolved to UUIDs through Mojang. When that is unreachable, or the
server runs in offline mode, the offline UUID is derived locally with the
same algorithm the server itself uses — so an entry written here matches the
one the server would have written.

Whitelist enforcement is written to `server.properties` *and* applied to the
running server together, because writing only one is the classic way to have
a whitelist that quietly stops applying after a restart.

### When Crafty cannot install the loader

Crafty fetches loader jars from a single mirror on a daemon thread. When that
mirror is down — and it goes down — Crafty logs one line, gives up, and still
answers the create call with 201. The instance then exists with no launcher,
and nothing in the API ever says so.

There is a ladder instead of a hang:

```
Crafty's own download → Crafty's jar index, fetched by us
                      → the loader's own project (Mojang, FabricMC,
                        NeoForged, MinecraftForge, PaperMC, PurpurMC)
                      → say plainly what is broken, and which mirror
```

Only the last rung is a failure, and it names the mirror rather than blaming
the user. The instance is left in place either way, so **Finish setup**
retries the loader without throwing away the port, the name, or anything
already uploaded — which used to mean deleting it and starting again.

The same ladder serves modpack installs and blank instances, so a fix to
either helps both.

### Review the client-only mods, before it happens

Client-only mods have to be inert on a server, but removing them silently is
how a working pack turns into a missing-dependency crash — and how a mod you
wanted vanishes with nothing to say where it went. So BlessForge shows you the
list first, with the evidence behind each call, and then **disables rather
than deletes**: the jar is installed as `<name>.jar.disabled`, tagged
*client-side* on the Mods tab with the reason it was flagged, and is one click
from being turned back on.

**The detector was rebuilt around scoring rather than switching.** The old one
asked three yes/no questions — does Modrinth say `server_side: unsupported`,
does `fabric.mod.json` say `environment: client`, is the name on a list. That
works for Fabric and is close to useless for Forge and NeoForge, which declare
no side at all: roughly half of every real pack was being judged on its
filename. Four sources that *do* work on every loader were added, and
everything is now weighed on one axis:

| Evidence | Why it works |
|---|---|
| **Package layout** | What fraction of a jar's classes live under a `client` package. A jar that is 100% `.../client/...` has nothing to run on a server, whatever its manifest says — and this is true for Forge, NeoForge and Fabric alike. Costs nothing: only the zip's central directory is read. |
| **Mixin targets** | Both ecosystems declare mixins per environment. A jar whose every mixin config is registered client-only is client-only. |
| **Client-only libraries** | A hard dependency on YACL, ModMenu, Sodium, Iris. A mod cannot be server-side and require a library that only exists on a client. |
| **Content shape** | `assets/` with no datapack content. Weak alone; useful next to the others. |

Three rules override the score outright, because each is a fact rather than an
inference: an operator decision always wins, a mod another *staying* mod
hard-requires is never removed, and the author stating `server_side: required`
outranks every heuristic here.

| Verdict | Meaning |
|---|---|
| **client-only** | Score at or above 70 — a declared environment, an all-client package tree, or several signals agreeing. Ticked by default; installed disabled. |
| **worth a look** | 30 to 69. Left for you to judge. |
| **protected** | Looks client-only *but another mod that is staying requires it*. Never disabled automatically. |
| **fine** | Real server-side code. |

Every verdict shows **the points each piece of evidence contributed and the
sentence behind it**, so it can be argued with rather than only obeyed.

Every jar is checked against Modrinth **by SHA-1 of the file**, before anything
is flagged — not just the ones with suspicious names, and not by guessing a
project from a display name. That matters: the mods that take a server down are
the ones nobody thought to put on a name list. On a real 301-mod export, the
name list found two client mods and the hash check found six, four of which no
heuristic had suspected.

The same detector now also runs **against an instance that already exists**
(Diagnose → Client-only scan), which is where it matters most: that set
includes jars added by hand, jars from an import, and anything installed before
these checks existed. It reads every jar on disk, scores it, and offers to
disable what it finds — never deleting, and never touching anything another
mod requires.

Unticking the review means exactly that — the pack installs as published, with
nothing disabled.

### Decisions you only make once

The review is cautious, and cautious is sometimes wrong in both directions. A
mod whose author writes `server_side: unsupported` usually means "this adds
nothing on a server", not "this breaks one" — and a mod that declares nothing
at all can be pure client code that takes the server down on first boot.

So a decision made once is remembered, in **both** directions:

- **allow** — this is safe on a server, stop flagging it
- **block** — this is client-only whatever it claims, always disable it

Matched three ways, strongest first: the catalogue project id, the mod id out
of the jar, then the filename with its version stripped. Matching on the
project id is what makes a decision survive a rename; matching on the stem is
what makes it survive a version bump. Decisions are **global by default** —
the reason a mod is fine is a property of the mod, not of the server it
happens to be on — and can be scoped to one instance when they genuinely are
situational.

The list lives on the Settings screen, and exports and imports as JSON, so a
list built over months survives moving to another machine.

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
- **Every row says which side it runs on** — `client only`, `server only`
  or `both` — from the same scored evidence the install review uses, not from
  the name. A jar nobody has scanned yet says so rather than guessing, and one
  scan fills in the whole list.
- **Click a mod's name and its project page opens as a comic panel** —
  summary, the long description, categories, downloads, licence, links out,
  and which build fits this server. Clicking the toggle or the install button
  does not open it. Descriptions come from third parties, so they are never
  inserted as HTML and links inside them are dropped.
- Bulk enable/disable/delete, update checks, and whole-modpack version switching.

### Edit configs

Browse `config/`, `defaultconfigs/`, `kubejs/`, `scripts/` and the usual root
files, grouped by owning mod. World data and binaries are excluded from the
editor.

The editor is **beside the list, not on top of it**: click a file and it opens
in the other half of the tab with a line gutter and its type, size and line
count in the status line. Unsaved text survives switching to another file and
back — the row stays marked until you save it — and leaving the page with
unsaved work warns first.

### Troubleshoot

- **Checks** — EULA state and format, Java/Minecraft mismatch, client-only mods,
  low memory, mods that failed to install, and mods built for another game
  version. That last one is *verified* against what the publisher says the file
  supports, not guessed from the filename: `alexsmobs-1.22.9.jar` is not a
  1.22.9 mod, and a check that says otherwise on forty jars teaches you to
  ignore it.
- **Log analysis** — parses `latest.log` and the newest crash report for missing
  dependencies, duplicate mods, port conflicts, out-of-memory, wrong Java,
  mixin failures and client-only crashes. Where the trace names a jar, the
  finding names it too.
- **Deep scan** — downloads every jar, reads its declared dependencies and
  builds the graph, so missing and duplicate mods surface *before* a launch.
- **Crash attribution** — when a crash report exists, the whole of it is read
  and the jars it implicates are named, ranked by how directly the log points
  at each: a `-- MOD x --` failure block outranks a mixin failure, which
  outranks appearing in the stack trace. Each culprit carries the line that
  implicates it. Where the real cause is not a mod at all — a Java version the
  pack refuses to run on, say — it says so rather than blaming the first mod
  that noticed.
- **One-click fixes** — accept EULA, set Java, disable the offending mods,
  search for a missing dependency, or swap mods to a compatible version.
- **It tells you what it checked**, not just what it found. Every run reports
  the passes it ran and how many findings each produced, and says plainly when
  one could not complete — so a clean report means the checks ran and found
  nothing, rather than possibly meaning they never ran. Findings that several
  passes each notice are reported once.

### Mod Roulette

Set constraints — Minecraft version, loader, how many mods, how reckless to be,
which of nine categories to prefer or ban — then **pull the shrine rope**. It
resists as you drag it, snaps back when you let go, and scatters petals. (It is
also just a button, for keyboards and anyone in a hurry.) BlessForge
deals a **hand**: a specific set of mods drawn from the live CurseForge and
Modrinth catalogues, which it can then install as a real server and hand back
as a **CurseForge modpack zip** you can share or open in the CurseForge app.

Three things make it more than a shuffle:

- **A pull is reproducible.** Every roll carries a short seed (`QRT-8KM-4Z`).
  Re-enter that seed with the same constraints and you get the same hand,
  exactly — so a roll is something you can send to someone.
- **Every mod in the hand is verified, not assumed.** The catalogue's own
  version filter is looser than it looks: a file tagged 1.21 comes back for a
  1.21.1 query, installs without complaint, and takes the server down on
  first boot with a registry error naming nothing useful. So each pinned
  build is checked against what its publisher actually declares — the exact
  Minecraft version and a loader this server can run — and anything that
  fails is dropped **with a reason you can read**.
- **A short hand is topped up rather than shipped short.** Dealing generously
  once and truncating works only while few mods drop out; on a version the
  catalogues have half-updated to, a third of a 120-mod roll can fail
  verification and you would get 80 mods having asked for 120, with nothing
  saying why. Instead it deals in rounds — verify, count what survived, deal
  replacements for exactly the shortfall — and if the pool genuinely runs out
  it says so and says what to loosen. Still deterministic: the round number
  goes into the seed.

The odds panel is computed from the same facts the installer acts on: real
file sizes, download counts, how long since anyone touched the mod, and
whether it has a server-side code path at all. Mods whose authors state
`server_side: unsupported` are dropped before they reach the hand, matched by
file hash rather than guessed from a name. Alongside it, a compatibility
report says how many of the hand declare your Minecraft version explicitly,
how many are untagged libraries, and how many are alpha or beta builds
because no stable release exists yet.

Dependencies are resolved on top of the hand and do not count against your mod
count. Rows can be **held** through the next pull, or rerolled one slot at a
time.

### Terminal

A live console per instance: Crafty's output streamed as it arrives, coloured
by severity, filterable, with a command box that types straight into the
server. Following pauses when you scroll up and resumes when you scroll back,
so reading scrollback while the server is chatty is actually possible.

Crafty has no push channel, so the backend polls it and forwards only the lines
that are new — the browser is never sent the last five hundred lines a second.

### AI assistant (optional)

Points at an [Ollama](https://ollama.com/) endpoint — **not** this machine, by
default. The box running Crafty needs its CPU for the Minecraft servers.

Three things it does:

- **Ask** — reads the deterministic findings plus the log and explains what is
  most likely wrong.
- **Review Crash Log** — reads the whole crash report, not a tail of it, and
  names the jars it implicates with the line that implicates each. The regex
  pass runs first and is passed to the model as evidence, so this still gives
  a useful answer when the endpoint is unreachable.
- **Review & Fix** — applies the fixes it is confident about, confined to the
  reversible half of the vocabulary: the EULA file, the Java version, the heap,
  and disabling mods the crash blames. Nothing is ever deleted automatically.

Deliberately constrained, because a small model is a good pattern-matcher and a
poor decision-maker:

- Its output is validated against a fixed action vocabulary; anything it
  invents is discarded, including filenames not present in your instance.
  Near-misses are reconciled against the real file list rather than thrown
  away — a small model reproduces a long jar name imperfectly.
- Actions are classified `safe` or `major`. Deleting mods, replacing versions
  and editing properties are **major** and need explicit confirmation; they are
  never in the automatic half.
- The deterministic checks run first and are fed in as evidence — the model
  explains and prioritises, it is not the detector.

The default model is `qwen3:4b-instruct` (2.5 GB, no thinking preamble,
reliable JSON, 256k context). If the endpoint is reachable but does not have
it, the **AI** pill in the header offers to pull it.

```bash
curl -X POST https://your-ollama-host/api/pull -d '{"model":"qwen3:4b-instruct"}'
```

**Prompt size is a real constraint.** The endpoint evaluates about 90
tokens/second and typically sits behind a proxy that allows 120 seconds to
first byte; streaming does not help, because nothing is emitted until the whole
prompt has been read. So the evidence is *selected*, not truncated — a 369-mod
inventory and a 67k crash report become a 7k prompt naming the six jars the log
actually mentions.

### Optimize for the machine

If an AI endpoint is configured you can let it **tune quietly in the
background** (`Settings → AI`). It does not open a chat: it folds a better
heap, GC and flag choice for the actual modpack into the numbers the
deterministic optimizer already produced. Everything it suggests is validated
and clamped — a heap it proposes can never exceed what the host can safely
give — and every failure path, including it being slow or unreachable, falls
back to the deterministic answer.

Silent does not mean unaccountable. The Tune screen shows an *ofuda* naming
the model and listing every value it moved, with the old number struck
through beside the new one, and it says so plainly when nothing was changed.


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
| `OLLAMA_URL` | `https://ai.shadowco.xyz` | Ollama endpoint for the AI assistant (blank disables it) |
| `OLLAMA_API_KEY` | *(empty)* | Bearer token, if that endpoint requires one |
| `AI_MODEL` | `qwen3:4b-instruct` | Model to use |
| `ROULETTE_POOL_PAGES` | `4` | Catalogue pages per category when building a roulette pool |
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

The UI is a thin client over a plain HTTP API. Long operations return
`{"job_id": ...}`; follow `/api/jobs/{id}/events` for progress.

```
GET  /api/health                     connections, storage, key warnings
GET  /api/host/specs
GET  /api/ai/status

--- catalogue -------------------------------------------------------------
GET  /api/browse/modpacks?q=&game_version=&loader=
GET  /api/browse/mods?q=&source=curseforge|modrinth
GET  /api/browse/plugins?q=&family=paper&game_version=&category=
GET  /api/modpacks/{id}/files
GET  /api/mods/{source}/{project_id}/versions
GET  /api/plugins/meta                categories and families
GET  /api/plugins/{project_id}/versions?family=&game_version=
GET  /api/plugins/starter?family=&game_version=

--- creating --------------------------------------------------------------
GET  /api/loaders                     what can be created, with version lists
GET  /api/loaders/{family}/versions
POST /api/provision/server            {name, loader, minecraft, port, ...}
POST /api/instances/{id}/loader/reinstall     retry a failed loader install

--- imports and installs --------------------------------------------------
POST /api/uploads/modpack             multipart "file"    -> imported archive
GET  /api/uploads
DEL  /api/uploads/{upload_id}
POST /api/install/preflight           {mod_id, file_id} | {upload_id}
POST /api/install/modpack             + server_name, port, exclude_files[],
                                        disable_files[], optimize
POST /api/instances/{id}/switch-pack-version

--- instances -------------------------------------------------------------
GET  /api/instances
GET  /api/instances/{id}
GET  /api/instances/{id}/stats        just the live numbers
POST /api/instances/{id}/action/{start_server|stop_server|restart_server|kill_server}
DEL  /api/instances/{id}?files=true

--- mods and plugins ------------------------------------------------------
GET  /api/instances/{id}/mods?directory=mods|plugins
POST /api/instances/{id}/mods/toggle          {file, enabled}
POST /api/instances/{id}/mods/bulk-toggle     {files[], enabled}
POST /api/instances/{id}/mods/delete          {files[]}
POST /api/instances/{id}/mods/resolve         -> dependency plan
POST /api/instances/{id}/mods/add             {source, project_id, file_id,
                                               with_dependencies, replace_file}
POST /api/instances/{id}/mods/identify
GET  /api/instances/{id}/mods/updates
GET  /api/instances/{id}/mods/dependencies
POST /api/instances/{id}/plugins/resolve      {project_id}  -> install plan
POST /api/instances/{id}/plugins/add          {project_id, file_id?}
GET  /api/instances/{id}/plugins/audit        every jar in plugins/, opened

--- files -----------------------------------------------------------------
GET  /api/instances/{id}/files?path=
GET  /api/instances/{id}/files/read?path=
POST /api/instances/{id}/files/write          {path, content, allow_world?}
POST /api/instances/{id}/files/create         {parent, name, directory}
POST /api/instances/{id}/files/rename         {path, new_name}
POST /api/instances/{id}/files/delete         {paths[], allow_world?}
POST /api/instances/{id}/files/upload?folder= multipart "file"
GET  /api/instances/{id}/files/download?path= streamed
POST /api/instances/{id}/files/extract        {path}   .zip only
GET  /api/instances/{id}/files/search?q=&root=
GET  /api/instances/{id}/files/usage          per-folder sizes

--- players ---------------------------------------------------------------
GET  /api/instances/{id}/players
POST /api/instances/{id}/players/action       {action, name, reason?}
       action: op | deop | whitelist | unwhitelist | ban | pardon | kick
             | ban-ip | pardon-ip
POST /api/instances/{id}/players/bulk         {action, names[], reason?}
POST /api/instances/{id}/players/whitelist-mode  {enabled} | {reload:true}
POST /api/instances/{id}/players/note         {name, note}
GET  /api/players/lookup?name=&online_mode=

--- configs and tuning ----------------------------------------------------
GET  /api/instances/{id}/configs
GET  /api/instances/{id}/configs/read?path=
POST /api/instances/{id}/configs/write        {path, content}
GET  /api/instances/{id}/properties
POST /api/instances/{id}/properties           {updates{}}
GET  /api/instances/{id}/port
POST /api/instances/{id}/port                 {port, force?}
GET  /api/instances/{id}/optimize             host specs + proposal
POST /api/instances/{id}/optimize             {heap_gb, flags[], properties{}}

--- diagnosis -------------------------------------------------------------
GET  /api/instances/{id}/diagnose
POST /api/instances/{id}/deep-scan
POST /api/instances/{id}/client-scan?directory=      every jar, scored
POST /api/instances/{id}/client-scan/apply    {files[], enabled}
POST /api/instances/{id}/smoke-test           boot once, watch, stop, report
GET  /api/instances/{id}/crash-review         deterministic, no model needed
POST /api/instances/{id}/ai/analyse           {question?}
POST /api/instances/{id}/ai/crash-review
POST /api/instances/{id}/ai/apply             {actions[], confirmed:true}
POST /api/instances/{id}/fix/{accept-eula|java|set-ram|versions}

--- console ---------------------------------------------------------------
GET  /api/instances/{id}/console
GET  /api/instances/{id}/console/stream       server-sent, diffed
POST /api/instances/{id}/command              {command}

--- undo, roulette, decisions ---------------------------------------------
GET  /api/instances/{id}/backups
POST /api/instances/{id}/backups              {reason}
POST /api/instances/{id}/backups/{snap}/restore
GET  /api/roulette/meta
POST /api/roulette/pool | /roll | /reroll | /install | /preview-export
GET  /api/roulette/export/{roll_id}
GET  /api/whitelist?server_id=&verdict=       the allow / block list
POST /api/whitelist                           {file, verdict, scope, reason}
DEL  /api/whitelist/{key}
GET  /api/whitelist/export
POST /api/whitelist/import                    {payload, replace?}
POST /api/whitelist/clear

--- jobs ------------------------------------------------------------------
GET  /api/jobs
GET  /api/jobs/{id}
GET  /api/jobs/{id}/events                    server-sent progress
POST /api/jobs/{id}/cancel
```

The three install endpoints take **either** CurseForge ids **or** an
`upload_id` from a previous import — never a mix.

Everything under `files/` refuses an absolute path, a drive letter, a NUL, or
any `..` segment before the request reaches Crafty. Crafty does reject a
traversal, but it does it with a 500 and a traceback, and "the component
downstream happens to refuse" is not a guard.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.test && $EDITOR .env.test
./dev.sh start        # http://127.0.0.1:8710
./dev.sh restart|stop|status
```

### Front end

Plain ES modules, served verbatim from `app/static/`. No build step, no
framework, no bundler:

```
index.html          the shell — a <link> for each stylesheet, one module script
css/theme.css       tokens, ornament, motion — shared with the Project Fox page
css/app.css         the shell and the screens
js/core.js          h(), icons, the API client, toasts, modals, formatting
js/app.js           the router, the rail, boot
js/jobs.js          long operations and the activity drawer
js/petals.js        the falling sakura
js/views/*.js       one module per screen, loaded on demand
```

Every screen is a dynamic `import()`, so opening the app downloads the shell
and one view rather than the whole application.

**Caching is a header, not a filename.** A `?v=` fingerprint cannot work
here: a module's own `import './core.js'` carries no query string and nothing
rewrites it. So `.js`, `.css` and `.html` are served `no-cache,
must-revalidate` — a few hundred bytes of 304s on a LAN — and fonts and
images are served `immutable` for a year. Redeploy the container and the
browser has the new code; there is no stale-asset failure mode to reason
about.

Six things in there are less obvious than they look:

* **Nothing is built from an HTML string.** `h()` returns real DOM nodes and
  handlers are attached to the node they belong to. The previous front end
  assembled markup as text and bound by `id` afterwards, which meant a
  handler on a static element was attached once and never rebound — a control
  that closed over a render-time value kept using the first one forever, and
  the symptom was a button that "did nothing" or did the same thing twice.
* **A job owns its stream; the drawer is only a view onto it.** Closing the
  drawer closes the view, never the `EventSource`, and several jobs are
  followed at once. Every terminal frame carries the result, because a client
  closes its stream on the first frame reporting a terminal status and there
  is more than one such frame.
* **The wiggling polygon lives in an explicit layer**, and the layer order is
  the whole trick. Each card, button and loader tile isolates its own stacking
  context and holds three: the blob at `z-index:0` bleeding past the element's
  edges, the fill and border at `1`, everything you read at `2`. It was a
  `z-index:-1` child once — which paints behind the parent's background only
  while the parent is *not* a stacking context, so the first animated
  `transform` put the blob in front of the fill and under the text. Two rules
  keep it honest and `dev/tools/check_frontend.py` enforces both: no blanket
  `.card > *` selector (it outranks `.p5-hl`'s own `position:absolute` and
  collapses the blob to nothing), and no new `z-index:-1` anywhere.
* **No entrance animation starts from `opacity:0`.** They translate, they are
  gated on `html.js-motion`, and `prefers-reduced-motion` turns them off. If a
  document timeline stalls — a background tab, a throttled preview — a
  `fill:both` animation that begins invisible never runs, and the content is
  invisible for good. A page that does not animate beats a page that is blank.
* **Boot does not wait on the network.** `paintNav()` and `route()` run first
  and `refreshHealth()` resolves into the UI afterwards; `/api/health` runs its
  three upstream probes concurrently behind an 8-second cache. Awaiting it made
  the shell wait on CurseForge and Modrinth before drawing anything, which cost
  about 200 ms of DOMContentLoaded and a second of perceived load.
* **The editor's gutter is a plain `<pre>`** sharing the textarea's font
  metrics with `scrollTop` mirrored. No highlighting overlay: it drifts on
  wrap and is a maintenance trap.

The theme is shared with the Project Fox landing page — the same tokens, the
same wiggling-polygon highlight, the same shrine motifs (torii, shimenawa,
chōchin lanterns, sakura mon, ema plaques, falling petals). Archivo Black and
Plus Jakarta Sans are fetched from Google Fonts on a non-blocking link; when
that link cannot be reached, the vendored Space Grotesk and JetBrains Mono
carry the page and `app.js` stamps `no-archivo` on `<html>` so the fallback
takes the display weight. A box with no route to the internet paints
immediately rather than waiting out a DNS timeout on a font.

### Checks

```bash
for t in test_loader_detection test_job_stream test_install_decisions \
         test_roulette test_boot_verdict; do
  python dev/tools/$t.py | tail -1
done
python dev/tools/check_frontend.py
```

`check_frontend.py` is the one that catches this front end's own failure
modes: a module that only parses as a script (`node --check` accepts a broken
string literal in one — only `--input-type=module` is a real check), an
import or an asset that does not resolve, an API path no route serves, an
icon name that is not in the set, and any change that breaks the polygon's
layering contract. 125 checks.

## Licence

MIT.
