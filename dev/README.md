# dev/ — test tooling

None of this ships in the image (the Dockerfile only copies `app/`). It exists
because the front end has no build step and therefore no test runner, and
because two of the nastiest bugs in this project were invisible from the
outside — a `/data` nobody could write to, and a Crafty instance with no
launcher. These are the checks that catch them.

Everything runs in a container, so the host needs nothing installed but Docker.

## ui-tests/ — the front end, in a real browser

The four jsdom harnesses that used to live here are **retired**, and so are the
three that replaced them. The UI is now the Claude Design canvas driven by its
own React runtime (`app/static/support.js`), so a DOM-only fake can neither
render it nor see a layout break. `ui.mjs` drives headless Chrome against a
running BlessForge instead.

It only **reads**. Nothing in it starts, stops, installs or deletes anything --
a suite that might run unattended has no business touching someone's fleet.

```bash
cd dev/ui-tests
docker run --rm --network host -v "$PWD":/w -w /home/pptruser \
  ghcr.io/puppeteer/puppeteer:latest sh -c "cp /w/ui.mjs . && node ui.mjs"
```

| file | what it covers |
|---|---|
| `ui.mjs` | 39 checks: the canvas boots under its own runtime; React and the fonts are served from this box rather than a CDN (a LAN install has no route to unpkg); the systems panel reports Crafty's real latency and the real AI endpoint; the fleet spine; every instance tab loads its data; all four Tune sub-tabs, including that every JVM flag and every server.properties group is listed rather than a sample; the catalogue with real pack logos; Mod Roulette's seed, reels and full version list; import; the command palette; and no horizontal overflow at 1600 / 1280 / 900 px. |

It asserts against the **corrections** as well as the features -- no TPS tile,
no `/srv/minecraft`, the real published port range -- because those are the
places the design asserted something untrue and a regression there would be
silent.

Some checks need a server in the fleet. With an empty Crafty the instance-tab
checks fail honestly rather than being skipped.

## tools/

| file | what it does |
|---|---|
| `test_loader_detection.py` | offline unit test for the loader state machine and the `run.sh` parse that produces the launch command. No network. `python3 dev/tools/test_loader_detection.py` |
| `sweep_instances.py` | lists every Crafty instance and flags any whose `executable` is missing from disk — i.e. the "empty server, no launcher" state. Needs the env from `.env`. |
| `make_icon.py` | regenerates `app/static/icon.png` (pure stdlib, no Pillow on this host). Run from the repo root. |

```bash
set -a; . ./.env; set +a
.venv/bin/python dev/tools/sweep_instances.py
```


## tools/ — offline Python tests

No Crafty, no network, no filesystem writes.

| file | what it covers |
|---|---|
| `check_bindings.py` | that every `{{ binding }}` the canvas reads is produced by `renderVals()`. A name it does not return renders as nothing at all — no error, no console message — which is the easiest way to break this front end. Needs Docker, no network. |
| `audit_placeholders.py` | every literal in the canvas that reads like a *value* rather than chrome: a count, a size, a version, a path. The canvas was drawn against mock data, so each one is a number someone will eventually believe. No Docker, no network. |
| `test_loader_detection.py` | the loader state machine, the `run.sh` → launch-command rewrite, which Java a launch command actually invokes (Crafty stores no `java_version`, so it has to be parsed back out), and that uptime is read from Crafty's UTC `started` rather than against the local clock — this host runs IST and the Crafty container runs UTC. 28 checks. |
| `test_job_stream.py` | that a job's SSE frames carry its **result**. Regression test for the bug that made the client-only review silently do nothing: the browser closes its stream on the first frame reporting a terminal status, and there is more than one such frame. 10 checks. |
| `test_roulette.py` | Mod Roulette: that the PRNG is a faithful port of the design's JavaScript (shared seeds are worthless otherwise), that a seed plus constraints reproduces a hand exactly, that a pool refresh barely disturbs one, that every constraint does what its label claims, and that the CurseForge export it writes can be re-imported by this app's own importer. Synthetic pool; no network. 34 checks. |
| `test_install_decisions.py` | that a client-only jar another mod depends on is held back rather than stripped (the review had this on one code path and not the other, and the gap stopped a pack booting), that the port typed at install is written *after* the pack overlay (every instance on this machine is on 25565 because it was not), that client-only mods are installed `.disabled` and recorded rather than deleted, that `skip_client_only=false` really installs them enabled, and that `exclude_files` still deletes. Stubs Crafty entirely. 16 checks. |

```bash
cd ~/blessforge
.venv/bin/python dev/tools/test_job_stream.py
.venv/bin/python dev/tools/test_install_decisions.py
```
