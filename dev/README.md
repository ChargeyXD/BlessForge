# dev/ — test tooling

None of this ships in the image (the Dockerfile only copies `app/`). It exists
because the front end has no build step and therefore no test runner, and
because two of the nastiest bugs in this project were invisible from the
outside — a `/data` nobody could write to, and a Crafty instance with no
launcher. These are the checks that catch them.

Everything runs in a container, so the host needs nothing installed but Docker.

## ui-tests/ — jsdom harnesses

**The four numbered harnesses were retired on 2026-08-29** when the front end
was replaced; they live in `ui-tests/retired/` with a note on what each still
specifies, to be ported as its screen is rebuilt.

They load the real `index.html` + `app.js` into jsdom and drive them against a
**running BlessForge**, so they exercise the actual DOM wiring rather than a
mock. Writes to Crafty are stubbed; nothing is installed or modified on a real
server.

One-time setup:

```bash
cd dev/ui-tests
docker run --rm -v "$PWD":/w -w /w --network host node:20-alpine \
  sh -c "npm init -y >/dev/null && npm i jsdom --silent"
```

Then:

```bash
docker run --rm --network host \
  -v "$PWD":/w -v "$HOME/blessforge/app/static":/static:ro -w /w \
  -e BF_URL=http://127.0.0.1:8710 \
  -e BF_SERVER_ID=<a-crafty-instance-with-mods> \
  node:20-alpine node 01-shell-and-views.mjs
```

| file | what it covers |
|---|---|
| `smoke.mjs` | the rebuilt shell: it boots, the systems panel reports real facts (Crafty latency, free disk, the actual AI endpoint — the design hard-coded a local Ollama and the wrong model), routing between views rebuilds the right tab strip, the command palette opens, filters and can be escaped, and `confirmSheet` returns the answer that was actually clicked. 23 checks. |
| `05-roulette.mjs` | the Mod Roulette screen against the live backend: the seed, the three segmented controls, sliders, nine cycling categories, four toggles, the pool counter, a real deal, holds, a single-slot reroll, and that accepting a hand writes nothing until it is confirmed. 27 checks. |
| `06-situation.mjs` | the Situation screen: the power card and its four tiles across running and stopped, the stat that is Crafty's own string versus the one that is a percentage, the java fact and when it is red, the “needs you” list and where each finding routes, the pack card managed and unmanaged, both removal buttons and the typed-name gate, that a declined confirm writes nothing, escaping, and that the live poll stops when you leave. 58 checks. |
| ~~`01-shell-and-views.mjs`~~ (retired) | boot, health pill, every view and tab, all five tab panes, config editor gutter + dirty guard, add-mod and import modals, dropzone keyboard/drag states, theme toggle. 57 checks. |
| ~~`02-mod-list.mjs`~~ (retired) | the windowed mod list: spacers, repaint on scroll, selection surviving unmount, in-place row patching, `.stale` marking. Needs `BF_SERVER_ID` pointing at an instance with >120 mods. 20 checks. |
| ~~`04-terminal-and-review.mjs`~~ (retired) | the Terminal tab (stream, severity colouring, HTML escaping, filter, command echo + post, disabled-while-stopped, teardown on tab change), client-side mod tagging and filter, the dependency view, the install review sending `disable_files` + `client_reasons` + the typed port, and the Activity instance chip. Drives a fake `EventSource`; every write is intercepted. 37 checks. |
| ~~`03-jobs.mjs`~~ (retired) | the job registry: phase strip, log levels and `×N` dedupe, stream pane, Run in Background → Watch re-attach with replay, completion summary, `result.problems[]` rendering. Drives a fake `EventSource`; starts no real job. 39 checks. |

`06-situation.mjs` uses **fixtures** for the instance payloads, because Crafty
has no servers on this machine. They are shaped from the real contract -- Crafty's
own `ServerStats` columns, and the `state` / `java` / `uptime_s` blocks
`/api/instances/{id}` computes -- and everything else in the page is the live
backend. Set `BF_SERVER_ID` and the reads pass through to that instance instead.
Writes are intercepted either way, so it never starts, stops or deletes anything.

Cold-start timing matters: the first `/api/instances` against a freshly started
container can take several seconds, and the harnesses assert after a fixed
wait. If checks fail with "0 cards", raise the `sleep` at the top.

## ui-tests/screenshot*.mjs — real Chrome

jsdom applies no CSS, so it cannot see a layout break. These use headless
Chrome for that.

```bash
docker run --rm --network host -v "$PWD":/w -w /home/pptruser \
  ghcr.io/puppeteer/puppeteer:latest \
  sh -c "cp /w/screenshot.mjs . && node screenshot.mjs && cp *.png /w/"
```

`screenshot.mjs` shoots the rebuilt shell and the Situation screen at 1440, 1000,
560 and 360 px and reports what only a rendering engine knows: whether the page
overflows sideways, whether any card collapsed to nothing, and what the tokens
resolved to. It intercepts the instance payloads for the same reason
`06-situation.mjs` does. The puppeteer image runs as its own user, so give it a
world-writable directory for the PNGs rather than the repo:

```bash
mkdir -p /tmp/shots && chmod 777 /tmp/shots
cd dev/ui-tests
docker run --rm --network host -v "$PWD":/w:ro -v /tmp/shots:/out -w /home/pptruser \
  ghcr.io/puppeteer/puppeteer:latest \
  sh -c "cp /w/screenshot.mjs . && node screenshot.mjs && cp *.png /out/"
```

`screenshot-install-flow.mjs` is **stale**: it walks the Browse -> pack -> wizard
-> preflight flow of the front end replaced on 2026-08-29, and its selectors no
longer match anything. Rewrite it when the Catalogue screen lands (§5D).

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
| `test_loader_detection.py` | the loader state machine, the `run.sh` → launch-command rewrite, which Java a launch command actually invokes (Crafty stores no `java_version`, so it has to be parsed back out), and that uptime is read from Crafty's UTC `started` rather than against the local clock — this host runs IST and the Crafty container runs UTC. 28 checks. |
| `test_job_stream.py` | that a job's SSE frames carry its **result**. Regression test for the bug that made the client-only review silently do nothing: the browser closes its stream on the first frame reporting a terminal status, and there is more than one such frame. 10 checks. |
| `test_roulette.py` | Mod Roulette: that the PRNG is a faithful port of the design's JavaScript (shared seeds are worthless otherwise), that a seed plus constraints reproduces a hand exactly, that a pool refresh barely disturbs one, that every constraint does what its label claims, and that the CurseForge export it writes can be re-imported by this app's own importer. Synthetic pool; no network. 34 checks. |
| `test_install_decisions.py` | that the port typed at install is written *after* the pack overlay (every instance on this machine is on 25565 because it was not), that client-only mods are installed `.disabled` and recorded rather than deleted, that `skip_client_only=false` really installs them enabled, and that `exclude_files` still deletes. Stubs Crafty entirely. 16 checks. |

```bash
cd ~/blessforge
.venv/bin/python dev/tools/test_job_stream.py
.venv/bin/python dev/tools/test_install_decisions.py
```
