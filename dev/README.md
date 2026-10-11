# dev/ — test tooling

None of this ships in the image (the Dockerfile only copies `app/`). It exists
because the front end has no build step and therefore no test runner, and
because the nastiest bugs in this project have all been invisible from the
outside — a `/data` nobody could write to, a server with no launcher,
a confirmation dialog that resolved `false` before it resolved `true`. These
are the checks that catch that kind of thing.

## tools/check_frontend.py — the front end, statically

```bash
python dev/tools/check_frontend.py
```

107 checks. Replaces `check_bindings.py` and `audit_placeholders.py`, which
audited the design-canvas runtime the UI no longer uses. It covers the four
things that break silently in a no-build-step ES-module front end, plus one
design invariant:

| check | why it exists |
|---|---|
| every module parses **as a module** | `node --check <file>` parses as a *script* and will quietly accept a file with a broken string literal in it. Only `node --input-type=module --check` is a real check. That distinction cost a screen in the session this was written. |
| every import resolves | there is no bundler, so a typo in a relative path is a 404 at runtime, on the one route that needs it and nowhere else |
| every asset exists | `/assets/NAME` is a mount over `static/img/`, so a renamed file leaves a broken image and no error anywhere |
| every API path is served | the contract between the front end and `main.py`. A screen calling a renamed endpoint fails at the moment somebody opens it |
| every `icon()` name is in the set | an unknown name silently falls back to the info glyph |
| nothing carrying the wiggling polygon is a stacking context | the highlight is a `z-index:-1` child; a `transform`, an `opacity` below 1, a `filter` or an `isolation` on its parent makes it paint between the card fill and the card text. Invisible until you hover, and it reads as a colour bug rather than a layering one. |

Needs `node` on PATH for the parse check; everything else is pure Python. It
skips the parse check rather than failing if node is absent.

## tools/ — offline Python tests

No network and nothing outside a temp directory. The engine is the in-memory
mock unless a file says otherwise.

| file | what it covers |
|---|---|
| `test_loader_detection.py` | the loader state machine, the `run.sh` → launch-command rewrite, which Java a launch command actually invokes (Crafty stores no `java_version`, so it has to be parsed back out), and that uptime is read from Crafty's UTC `started` rather than against the local clock — this host runs IST and the Crafty container runs UTC. 39 checks. |
| `test_job_stream.py` | that a job's SSE frames carry its **result**. Regression test for the bug that made the client-only review silently do nothing: the browser closes its stream on the first frame reporting a terminal status, and there is more than one such frame. 10 checks. |
| `test_install_decisions.py` | that a client-only jar another mod depends on is held back rather than stripped (the review had this on one code path and not the other, and the gap stopped a pack booting), that the port typed at install is written *after* the pack overlay, that client-only mods are installed `.disabled` and recorded rather than deleted — and the scoring itself: a name-list match alone is only ever a review, a declared `environment=client` is decisive, an author's `server_side: required` outranks every heuristic, an all-client package tree catches a Forge mod that declares nothing, and an operator's allow survives a version bump while a block overrides a clean score. 31 checks. |
| `test_roulette.py` | Mod Roulette: that the PRNG is a faithful port of the design's JavaScript (shared seeds are worthless otherwise), that a seed plus constraints reproduces a hand exactly, that a pool refresh barely disturbs one, that every constraint does what its label claims, and that the CurseForge export it writes can be re-imported by this app's own importer. Synthetic pool; no network. 35 checks. |
| `test_boot_verdict.py` | that a stack trace which did not actually kill a boot is not reported as the cause. 26 checks. |

```bash
for t in test_loader_detection test_job_stream test_install_decisions \
         test_roulette test_boot_verdict; do
  python dev/tools/$t.py | tail -1
done
```

## tools/ — the commercial rebuild (WP-01 to WP-06)

Offline, in the quick loop:

| file | what it covers |
|---|---|
| `test_blocked_downloads.py` | a mod whose author blocked third-party downloads is never fetched: a Modrinth substitute by SHA-1 or by slug, else *blocked by author*. 41 checks. |
| `test_engine.py` | the engine router, the mock driver's refusals, and the loader ladder (node fetches → BlessForge relays → a plain failure). 50 checks. |
| `test_db.py` | the schema, the migrations, and the SQLite / Postgres paths (`TEST_DATABASE_URL`). 27 checks. |
| `test_jobs_redis.py` | the job queue: an API and a worker as separate processes, the API killed mid-job, a browser re-attaching with no gap and no repeat, cancel, orphaned jobs. fakeredis over TCP unless `REDIS_TEST_URL` is set. 25 checks, about 25 s. |
| `test_templates.py` | the seven loader templates: well formed, the Java each version runs on, and each one created, tuned and boot-tested through the engine. 50 checks. |
| `test_import_crafty.py` | `import_from_crafty.py` against the Crafty mock: through the API and from disk, re-runs, port clashes, a planted link, Crafty only ever read. 20 checks. |
| `test_agent_security.py` | the node agent: the file jail (paths, links, junctions, a swap race, hard links), archives (zip slip, link members, bombs), browser and node tokens, one server's token against another, every container rule, the disk watchdog. 172 checks. `--live` adds real containers trying to get out — Linux and Docker only, **not yet run**. |
| `test_agent_api.py` | the panel and an agent as two processes: register a node, create → start → console websocket and a command → edit a file → crash → stop → delete, and both directions refusing the wrong credential. 30 checks. |

Live, not in the quick loop (network, minutes, real servers):

| file | what it does |
|---|---|
| `boot_templates.py` | downloads every loader from its own project and boots a real server for each, through a panel and a process-runtime agent with local JREs. |
| `install_on_agent.py` | installs a CurseForge modpack on the agent, boots it, exports it, imports the export back as a second server, and boots that. Needs your key (environment or `.env`; never printed). |
| `_agentstack.py` | the disposable panel + agent both of the above stand up. |

The importer itself:

| file | what it does |
|---|---|
| `import_from_crafty.py` | moves every server off the old Crafty deployment (README, "Moving servers off Crafty"). Read-only towards Crafty. |

## tools/ — operations

| file | what it does |
|---|---|
| `sweep_instances.py` | lists every server and flags any whose `executable` is missing from its folder — the "empty server, no launcher" state. The app has **Finish setup** for exactly this, so treat a hit here as something to click rather than something to fix by hand. |
| `repair_export_loaders.py` | rewrites the loader id in a pack export that named a family without a build. |
| `make_icon.py` | regenerates `app/static/icon.png` (pure stdlib, no Pillow on this host). Run from the repo root. |

```bash
set -a; . ./.env; set +a
python dev/tools/sweep_instances.py
```

## ui-tests/ — retired

`ui.mjs` drove headless Chrome against the design-canvas front end and asserts
against markup that no longer exists — `<x-dc>`, `support.js`, the vendored
React, the 316-binding contract. It is kept only as a record of what was
verified in August 2026; **it will not pass and is not expected to.**

The equivalent coverage now comes from `check_frontend.py` for the static
half, and from driving the real UI in a browser for the interactive half. The
two bugs that mattered most in the 2026-09-10 session — a confirmation dialog
that always resolved false, and a class token with a trailing space that threw
— were both found that way and neither would have been caught by reading the
code.
