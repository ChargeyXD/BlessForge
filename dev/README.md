# dev/ — test tooling

None of this ships in the image (the Dockerfile only copies `app/`). It exists
because the front end has no build step and therefore no test runner, and
because two of the nastiest bugs in this project were invisible from the
outside — a `/data` nobody could write to, and a Crafty instance with no
launcher. These are the checks that catch them.

Everything runs in a container, so the host needs nothing installed but Docker.

## ui-tests/ — jsdom harnesses

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
| `01-shell-and-views.mjs` | boot, health pill, every view and tab, all five tab panes, config editor gutter + dirty guard, add-mod and import modals, dropzone keyboard/drag states, theme toggle. 57 checks. |
| `02-mod-list.mjs` | the windowed mod list: spacers, repaint on scroll, selection surviving unmount, in-place row patching, `.stale` marking. Needs `BF_SERVER_ID` pointing at an instance with >120 mods. 20 checks. |
| `03-jobs.mjs` | the job registry: phase strip, log levels and `×N` dedupe, stream pane, Run in Background → Watch re-attach with replay, completion summary, `result.problems[]` rendering. Drives a fake `EventSource`; starts no real job. 39 checks. |

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

`screenshot-install-flow.mjs` walks Browse → pack → wizard → job modal →
preflight review, stubbing the preflight job with a saved result, and shoots
each step.

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
