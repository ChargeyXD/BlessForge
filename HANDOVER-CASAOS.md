# BlessForge 2.1 — deploy and verify on the CasaOS box

Written 2026-09-10, for the Claude instance that has a **real Crafty, real
servers and a real CurseForge key**. That is the whole reason this document
exists: the session that produced 2.1 had none of those, so a specific list of
things could not be proven and they are all listed in §5.

Read `NEXT-SESSION.md` for the state of the code and `HANDOVER.md` §3–§4 for
how it is put together. This file is only two things: how to get 2.1 onto the
box, and what to press once it is there.

---

## 0. The one thing that will bite you

**Recreate the container. Do not restart it.**

`entrypoint.sh` chowns the directories this app owns *by name*, and 2.1 adds
four to that list (`state`, `backups`, `exports`, `roulette-exports`). A
restart re-runs the entrypoint, so in practice a restart is enough — but a
`docker compose up -d` that decides nothing changed will not restart it at
all, and then those directories stay root-owned. Every write into them is
wrapped in a try/except, so the symptom is not an error: it is the client-only
decision list silently never persisting.

```bash
docker compose up -d --build --force-recreate
```

Check it took, from the host:

```bash
docker exec blessforge sh -c 'ls -ld /data/state /data/backups /data/exports /data/roulette-exports'
```

Every one should be owned by `studio` (uid 1000). If any says `root`, the
entrypoint did not run as root — check that no `user:` key was added to the
compose service.

The app now reports this itself. `Settings → Connections → Storage` goes red
with a sentence naming the directory, so if you skip the check above the app
will tell you anyway.

---

## 1. What changed

Nothing in the deployment contract moved: same image name, same port 8710,
same environment variables, same `/data` mount, same CasaOS compose. No new
environment variable is *required*. `STATE_DIR` exists and defaults to
`/data/state`; there is no reason to set it.

| Area | Files | What to know |
|---|---|---|
| **Front end** | `app/static/**` (all of it) | Rebuilt from scratch. The design-canvas runtime is gone — no `<x-dc>`, no `support.js`, no vendored React. Plain ES modules. Pink shrine fox theme shared with the Project Fox page. |
| **Loader failures** | `provision.py` (new), `installer.py` | A three-rung ladder: Crafty → Crafty's jar index → the loader's own project. Shared by blank creates and modpack installs. Plus **Finish setup** to retry without losing the instance. |
| **Blank instances** | `provision.py`, `loaders.py` (new) | `New server`: seven loaders, real upstream version lists, the right mod folder each. |
| **Paper plugins** | `plugins.py` (new) | Modrinth catalogue, compatibility checking, dependency preview, starter list, `plugins/` audit. |
| **File manager** | `files.py` (new) | Whole server directory. Streamed both ways so a big file cannot blow the 1 GB `mem_limit`. |
| **Players** | `players.py` (new) | Ops, whitelist, bans, who is on. Routed by whether the server is running, and says which route it took. |
| **Client-only detection** | `clientscan.py` (new), `preflight.py` (gutted to a shell) | Scored evidence, four new signals that work on Forge/NeoForge. One implementation, used by the install review *and* a scan of a live instance. |
| **Decisions** | `whitelist.py` (rewritten) | Allow **and** block. Stored at `/data/state/`, migrated from `/data/` on first read. |
| **Roulette** | `roulette.py` | Every pinned build verified against what its publisher declares; short hands topped up in seeded rounds. |
| **Packaging** | `Dockerfile`, `entrypoint.sh`, `config.py`, `main.py` | The four directories above, and the storage health check that reports them. |

Three external contracts moved and are already handled — mentioned because if
a version list comes back empty these are the first things to check:

- **PaperMC sunset its v2 API.** Everything under `api.papermc.io/v2` answers
  410 now. `loaders.py` speaks v3 at `fill.papermc.io/v3`.
- **Minecraft's versions are year-based now** (`26.2`, `26.1.2`) alongside the
  legacy `1.21.x` line, and NeoForge followed (`26.2.0.84` is four components
  where `21.1.250` was three). Both readings are computed and reconciled
  against Mojang's own list.
- **Modrinth wants `project_type:plugin`** in a search facet, while the hits it
  returns still call themselves `mod`. Getting that backwards returns an empty
  catalogue that looks like an outage.

### Removed

`app/static/support.js` and the two vendored React files are **deleted**. So
are `dev/tools/check_bindings.py` and `dev/tools/audit_placeholders.py` — they
audited the runtime that no longer exists. `dev/ui-tests/ui.mjs` is retired and
**will fail**; it asserts against markup that is gone. Do not treat that as a
regression.

---

## 2. Deploy

From a checkout on the box (this builds your working tree — the override file
makes Compose do that automatically):

```bash
cd /path/to/blessforge
git pull                       # or copy the tree across
docker compose up -d --build --force-recreate
docker compose logs -f blessforge     # watch it come up, then Ctrl-C
```

If the CasaOS-installed container is the one running, this takes it over —
same container name. To hand it back:

```bash
docker compose -p blessforge -f /var/lib/casaos/apps/blessforge/docker-compose.yml \
  up -d --force-recreate
```

Then open `http://<box>:8710`.

**Expect a hard reload the first time.** The old front end is in every
browser's cache. 2.1 serves code as `no-cache, must-revalidate` so this is a
one-time cost, but the *first* load after the upgrade may still come from the
old cache. Ctrl-Shift-R once.

---

## 3. Verification, in order

Ordered by risk, cheapest first. Roughly 45 minutes end to end. Tick as you go
and report §4.

### A — it starts, and nothing is obviously wrong (5 min)

```bash
curl -s localhost:8710/api/healthz
curl -s localhost:8710/api/health | python3 -m json.tool
```

- [ ] `healthz` returns `{"status":"ok","app":"BlessForge"}`
- [ ] `health` shows `ready: true`, `crafty.ok: true` with a latency, and
      `curseforge.ok: true`
- [ ] `health.checks.storage.ok` is **true** — if false, §0
- [ ] The fleet loads in a browser and every existing server appears, with the
      right loader, version, port and running state
- [ ] The rail lists them with the right state dot (green running, grey
      stopped, red crashed, plum orphaned, gold half-finished)
- [ ] No errors in the browser console

If the page is blank or unstyled: you got the cached old shell. Ctrl-Shift-R.

### B — the existing fleet still reads correctly (10 min)

This is the regression surface. Your instances were created by 2.0 and their
`.blessforge.json` manifests are unchanged, but everything that reads them was
touched.

Pick your **largest** modded instance:

- [ ] **Overview** — loader, Minecraft version, Java (with the ✓/✗ against
      what the version requires), port, mod count, pack name all correct
- [ ] **Mods** — every jar listed, icons present, versions right, disabled ones
      shown disabled, client-only ones tagged with their reason
- [ ] **Configs** — files grouped by owning mod; open one, it has content
- [ ] **Tune** — Host RAM shows a **real number**, not `unknown`. If it says
      unknown, `/proc/meminfo` is unreadable and you should set `HOST_RAM_GB`.
      Current heap should match `user_jvm_args.txt`.
- [ ] **Console** — on a running server, live lines arrive and the source pill
      says "Crafty's live buffer"; scroll up and Following flips to Paused
- [ ] **Diagnose** — findings render; if the instance has a crash report, the
      jars it blames are named
- [ ] **Undo** — existing snapshots listed

Anything wrong here is a regression, not a missing feature. Say so loudly.

### C — the new features (20 min)

**File manager** — on any instance:

- [ ] Sidebar shows real folder sizes (mods, world, logs…)
- [ ] Navigate into `config/`, breadcrumbs work, going up works
- [ ] Open a `.toml` or `.properties` file — the editor opens with a line
      gutter that scrolls with the text
- [ ] Edit it, Save. **A confirmation appears for `server.properties`** and
      the save actually lands (reopen and check). This is the bug that was
      fixed — if a confirmation does nothing, something regressed badly.
- [ ] The save toast mentions a snapshot; check **Undo** has it
- [ ] Try to delete something inside `world/` — it must demand you type
      `DELETE`
- [ ] Download a file — it should arrive intact
- [ ] Upload a small file into `config/` — it appears
- [ ] Search for a filename, get hits with paths

**Players** — on a **running** instance:

- [ ] Counts are right (online, ops, whitelisted, banned)
- [ ] Online players are listed with faces
- [ ] Op somebody, then de-op them. The toast must say **"applied to the
      running server"** and the change must be visible in-game immediately.
- [ ] Add a player by name — the Mojang lookup resolves a real UUID

On a **stopped** instance:

- [ ] The banner says changes go to the files and apply next start
- [ ] Whitelist somebody; the toast says **"takes effect the next time this
      server starts"**; `whitelist.json` on disk actually contains them
- [ ] Kick is refused with an explanation rather than failing silently

**Client-only scan** — on your largest modded instance, Diagnose → Client-only
scan:

- [ ] It reads every jar and finishes (a 200-mod instance takes a minute or two)
- [ ] The four buckets add up to the mod count
- [ ] Open the evidence on a few. **Sanity-check the verdicts against what you
      know.** This is the one that most needs a human: a false "client-only"
      on a mod you know is server-side is the failure mode that matters.
- [ ] Anything already disabled shows as already handled, not as new
- [ ] Mark one "always allow", re-run — it comes back as `server`
- [ ] Settings → the decision appears; export it, it downloads

**Roulette** — pick a version you know is well served (1.21.1 NeoForge):

- [ ] Pull the lever; the hand is the size you asked for, or it says why not
- [ ] Most rows show a **verified** badge
- [ ] "N dropped" lists real reasons grouped by kind
- [ ] The same seed + same constraints pulls the identical hand

### D — the refactored install path (the big one, 15 min)

**This is the largest untested surface.** `installer.py` had its loader ladder
moved out to `provision.py`. The unit tests pass and the logic is the same, but
no real install has run since.

- [ ] **Install a small modpack from the catalogue.** Pick something under 60
      mods so it is quick. Watch for: the client-only review appearing with
      evidence, the job progressing through its phases, the loader installing,
      the pack overlaying, and the instance **starting successfully afterwards**.
- [ ] The instance's port is the one you typed, not 25565
- [ ] Its `.blessforge.json` records the mods
- [ ] Client-only mods are present as `.jar.disabled`, not missing

- [ ] **Create a blank Fabric or NeoForge server.** Check `mods/` exists and is
      empty, the EULA is accepted, and it **starts**.
- [ ] **Create a blank Paper server.** Check `plugins/` exists (not `mods/`),
      and the tab says Plugins.
- [ ] **Install a plugin into it** — LuckPerms is the safe choice. Check the
      jar lands in `plugins/`, restart, and confirm the server loads it.
- [ ] Run the `plugins/` audit — it should say everything is fine.

### E — destructive and edge (5 min)

Use a throwaway instance for these.

- [ ] Stop a running server with players on it — it warns first
- [ ] Kill — it warns that the world will not save
- [ ] Delete an instance — it demands you type `DELETE`, and the instance
      really goes
- [ ] Open the app on a phone. The rail becomes a drawer behind the ☰ button;
      no horizontal scrolling anywhere.

### F — the loader ladder, if you want to prove it

Hard to trigger deliberately, and worth knowing how. Block Crafty's mirror
from the Crafty container (`jars.arcadiatech.org`) and create a blank server.
You should see the job log say Crafty's download did not complete, then that it
is falling back — and the instance should still end up with a working launcher.

If you would rather not, that is reasonable; the ladder's individual rungs are
each exercised by `/api/loaders`, which is already proven to work with **no
Crafty at all** (that is how the version lists in §C were produced).

---

## 4. What to report back

For each of A–E: passed, or what happened instead. For anything that failed:

```bash
docker compose logs --tail=200 blessforge
```

plus the browser console, and the job log from the Activity drawer if a job was
involved.

The three things most worth knowing either way:

1. **Did a real modpack install and then start?** (§D)
2. **Are the client-only verdicts right on a pack you know?** (§C)
3. **Did any existing instance read wrong?** (§B)

---

## 5. Known-unverified before you start

So you know what is being tested rather than re-tested. From
`NEXT-SESSION.md` §3:

| Not proven | Covered by |
|---|---|
| A real modpack install end to end | §D |
| `create_empty` against a real Crafty | §D |
| A plugin actually downloaded into `plugins/` | §D |
| Any destructive button pressed for real | §E |
| Below 900 px on a real phone | §E |
| The AI assistant producing a plan | not covered — unchanged since August, still untested |

Everything else in 2.1 was driven against a mock Crafty and the live
CurseForge, Modrinth, Mojang, FabricMC, NeoForged, PaperMC and PurpurMC APIs:
16 routes with zero console errors, both themes, 141 backend checks and 107
front-end checks.

Two real bugs were found by driving the UI rather than reading it, and both are
fixed. Worth knowing because they tell you what to be suspicious of:

- `confirmDialog` resolved `false` before it resolved `true`, so **every
  confirmation in the app silently did nothing**. If you find a confirm button
  that appears to work but changes nothing, suspect this pattern first.
- A class token with a trailing space (`bar(v, 'thin ')`) threw and took the
  whole screen down with "This screen could not be drawn".

---

## 6. If you need to go back

2.1 changes no data format. `.blessforge.json` is untouched; the decision list
is migrated *by moving the file*, so a rollback needs it moved back:

```bash
docker exec blessforge sh -c 'mv /data/state/client-only-whitelist.json /data/ 2>/dev/null'
docker exec blessforge sh -c 'mv /data/state/ai-endpoint.txt /data/ 2>/dev/null'
```

then run the previous image. Nothing else has to be undone — instances,
worlds, uploads, the cache and every manifest are compatible in both
directions.

---

## 7. Running the checks yourself

From a checkout on the box (needs the venv, not the container):

```bash
cd /path/to/blessforge
for t in test_loader_detection test_job_stream test_install_decisions \
         test_roulette test_boot_verdict; do
  .venv/bin/python dev/tools/$t.py | tail -1
done
.venv/bin/python dev/tools/check_frontend.py | tail -2
```

Expected: 39, 10, 31, 35, 26 backend checks and 107 front-end checks, all
passing. `check_frontend.py` wants `node` on PATH for its parse check and skips
it cleanly if node is absent — but that is the check worth having, because
`node --check <file>` alone parses an ES module as a *script* and will accept a
file with a broken string literal in it.
