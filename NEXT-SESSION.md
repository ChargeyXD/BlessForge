# BlessForge — the state of the code, for the next session

Written 2026-08-29 after a line-by-line pass over the whole structure. Read
`HANDOVER.md` first for how the app is put together; this is the list of what
is *incomplete*, in the order I would take it.

Nothing here is broken. Everything below is either unfinished, unproven, or
worth tidying — separated so the next session can tell which is which.

---

## 1. Verified working (do not re-litigate)

Driven end to end against five real servers on this machine:

| area | evidence |
|---|---|
| Install, manifest path | Tensura: 223 mods, Forge 1.19.2, 1032 files, no problems |
| Install, server-pack path | Cozy Experience: 191 mods, 4 client-only disabled |
| Client-only review | 30 candidates on Tensura; 4 held back as dependencies |
| Identification at install | 223/223 identified with icons, no Identify pass |
| Mod add + dependencies | Sophisticated Storage pulled sophisticatedcore |
| Mod row menu | version picker, whitelist, delete, copy |
| Configs | opens, types, saves, read-only files say so |
| Tune | heap + 25 flags written to user_jvm_args.txt, verified on disk |
| server.properties | 62 keys, 6 groups, editable, saves |
| Ports | mismatch detected and fixed |
| Diagnose | crash attribution named the real culprits on two packs |
| Deep scan | 16 false "missing" → 1 real |
| Console | live stream, follow, command box |
| Jobs | live percent, phases, log, completion state |
| Mod Roulette | deterministic roll, install, CurseForge export, re-import |
| Catalogue | search, filters, paging, install |
| Import | upload with progress, archive facts, install |
| Assistant endpoint toggle | switches both ways, remembered |

Suite: `137 offline checks + 39 in a real browser`. `dev/tools/check_bindings.py`
and `dev/tools/audit_placeholders.py` both come back clean.

---

## 2. Unproven — the honest gaps

1. **The assistant has never produced a plan here.** `/ai/analyse` and
   `/ai/crash-review` are wired and the endpoint answers, but no analysis was
   run to completion in these sessions. The Assistant tab's plan list, its
   apply buttons and `_apply_actions` are therefore untested against real
   output. This is the largest untested surface in the app.
2. **No destructive button has been pressed in the browser.** Stop, restart,
   kill, delete-record and delete-world are wired and their confirm dialogs
   work; every one was exercised through the API instead. `ui.mjs` deliberately
   never touches them.
3. **`switch-pack-version` has never run.** The Situation screen's "Switch
   release" and "Re-import export" buttons navigate to Discover rather than
   calling it, so the endpoint is unreached from the UI.
4. **Below 900px is unopened.** `ui.mjs` checks 1600/1280/900. The canvas has
   fixed widths (a 298px spine, a 428px drawer) that will need real work on a
   phone.
5. **The light theme does not exist.** The toggle says so rather than
   pretending.

---

## 3. Endpoints with no front-end caller

Not dead — each is reachable and several are useful — but nothing in the UI
calls them, so they are untested from the outside:

| endpoint | what it is for |
|---|---|
| `POST /api/instances/{id}/switch-pack-version` | the "switch release" path (see §2.3) |
| `POST /api/instances/{id}/ai/autofix` | Review & Fix; the plan-and-apply route |
| `POST /api/instances/{id}/mods/resolve` | dependency preview before adding |
| `GET  /api/instances/{id}/mods/dependencies` | what each mod pulled in |
| `GET  /api/modpacks/{id}/files/{fid}/plan` | pack shape before committing |
| `POST /api/roulette/preview-export` | export a hand without installing |
| `POST /api/ai/warm` | pre-load the model |
| `GET  /api/ai/models` | what the endpoint has |
| `GET  /healthz`, `/api/healthz` | container health probes (used by Docker) |

`mods/dependencies` is the one worth wiring: the Mods screen promises
"Dependencies" in the design and does not show them.

---

## 4. Runtime traps (all cost real time; all confirmed)

The canvas's renderer is not React and does not behave like it in four places:

1. **`componentDidUpdate(prevProps)` takes one argument, and it is props.**
   There is no prevState. Track what changed yourself.
2. **Job frames carry `percent`, not `progress`.**
3. **A `<textarea>` takes `value`, not children.** As a child it renders
   `[object Object]`.
4. **A `<select>` nested inside an outer `<sc-for>` is dropped entirely.** Each
   must own the only loop in its subtree.
5. **A static element's `onClick` is bound once and never rebound.** Attribute
   bindings update on every render; handlers do not. A handler that closes over
   a render-time value keeps using the first one forever. Handlers on static
   elements must read `this.state` (or ask the server). `<sc-for>` rows do get
   fresh closures.

Trap 5 is the subtle one and it produced a bug that looked like a backend
fault. Suspect it whenever a control "does nothing" or does the same thing
twice.

---

## 5. Code worth tidying (no behaviour change)

- **`app/main.py` is 1,897 lines** and mixes routing with logic. The instance
  routes alone are ~600 lines. Splitting by resource (`routes/instances.py`,
  `routes/roulette.py`, …) would make it navigable; nothing depends on it
  being one file.
- **`app/static/index.html` is 4,806 lines** — ~1,500 of canvas markup and
  ~3,300 of logic. It cannot be split (the runtime wants template and script
  in one document) but the logic half has grown section comments that are the
  only navigation. Keep them accurate.
- **`renderVals()` returns 405 bindings in one object literal.** It is honest
  but long. It could be assembled from per-screen helpers
  (`situationVals()`, `modsVals()`, …) merged at the end, which would also make
  each screen's bindings testable in isolation.
- **`ai.py` is 1,179 lines**, most of it prompt text and the action
  vocabulary. The prompts could live in their own module.
- **Two `TODO`-shaped gaps**: `mods/dependencies` has no UI (§3), and the
  Situation screen's pack buttons navigate rather than act (§2.3).

---

## 6. Known rough edges carried forward

- The UI follows one job at a time; a second install keeps running but stops
  being shown. Activity re-attaches.
- The mod list is not virtualised. Fine at 245 rows; the previous front end
  windowed at ~120 and that code is in git.
- The open instance polls `/stats` every 6s while running. Two browser tabs
  double it.
- CurseForge file-level dependency metadata is incomplete — a file can declare
  none while the jar requires something. Nothing asked before installing knows;
  the loader's error afterwards does.
- Two servers cannot both hold a 4 GB heap on this 11.6 GB host. The Tune
  ceiling is computed from what is free *now*, so it moves.
- `Tensura` will not boot here: six of its mods need the full AWT and the
  Crafty image ships a headless JRE. Diagnose says so and names them.

---

## 7. How to check your work

```bash
cd ~/blessforge
for t in test_loader_detection test_job_stream test_install_decisions test_roulette; do
  .venv/bin/python dev/tools/$t.py | tail -1
done
python3 dev/tools/check_bindings.py      # every {{ binding }} is produced
python3 dev/tools/audit_placeholders.py  # no canvas literal reads like data
cd dev/ui-tests && docker run --rm --network host -v "$PWD":/w -w /home/pptruser \
  ghcr.io/puppeteer/puppeteer:latest sh -c "cp /w/ui.mjs . && node ui.mjs"
```

The two audits are the ones that catch the failure modes unique to this
front end: a binding that renders blank, and a number that came from the mock.
