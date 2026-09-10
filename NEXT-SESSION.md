# BlessForge — the state of the code, for the next session

Rewritten 2026-09-10, after the front end was rebuilt from scratch, six
features were added, and the UI was then reworked a second time for weight
and speed. Read `HANDOVER.md` first for how the app is put together;
this is the list of what is *incomplete*, separated so you can tell which is
which.

The previous version of this file described the design-canvas runtime and its
five traps. **All of that is gone** — no `<x-dc>`, no `support.js`, no
vendored React, no binding contract. Do not go looking for them.

---

## 1. What changed in this session

| area | what it is now |
|---|---|
| Front end | Rebuilt: plain ES modules, real DOM from `h()`, hash router, one module per screen loaded on demand. The pink shrine fox theme from the Project Fox landing page — same tokens, same wiggling polygon, same shrine ornament. |
| Loader failures | A three-rung ladder (Crafty → Crafty's index → the loader's own project) shared by blank creates and modpack installs, plus **Finish setup** to retry without losing the instance. |
| Blank instances | `New server`: seven loaders, real upstream version lists, the right mod folder for each. |
| Paper | A first-class creatable loader, with a Modrinth plugin catalogue, compatibility checking, a dependency preview, a starter list and a `plugins/` audit. |
| File manager | The whole server directory: browse, edit, upload, download, rename, create, delete, extract, search, per-folder sizes. Streamed both ways. |
| Players | Ops, whitelist, bans, IP bans and who is online, routed by whether the server is running and saying which route it took. |
| Client-only detection | Rebuilt as scored evidence with four new signals that work on Forge/NeoForge. One implementation, used by the install review *and* by a scan of a live instance. |
| Decisions | The whitelist is now allow **and** block, matched by project id → mod id → version-stripped stem, global or per-instance, exportable. |
| Roulette | Every pinned build verified against what its publisher declares; short hands topped up in seeded rounds instead of shipped short. |

## 1A. The second pass — how it looks, and how fast

The rebuild was correct and read flat, so it was reworked the same day. No
endpoint moved; three server-side changes exist only to make the page load
faster.

| area | what it is now |
|---|---|
| Layering | Explicit, and machine-checked. Every card, button and loader tile isolates its own stacking context and holds three layers: blob at 0, fill and border at 1, content at 2. The blob bleeds ~17 px past the card edge, behind the fill. |
| Load time | `/api/health` runs its three upstream checks under `asyncio.gather` behind an 8 s cache, and boot no longer awaits it. DOMContentLoaded 248 → 118 ms, load 589 → 387 ms, health 1.3 s → 665 ms cold and 2 ms warm. |
| Motion | Every entrance animation is translate-only and gated on `html.js-motion`; `prefers-reduced-motion` turns it all off. Hover lifts, magnetic buttons, ripples, counting numbers, staggered reveals. |
| Mod side tags | Every mod row says `client only` / `server only` / `both`, from the same scored evidence the install review uses. A jar with nothing recorded says so instead of guessing. |
| Config editor | Side by side in the tab rather than in a modal. Unsaved text survives switching files; leaving with unsaved work warns. |
| Roulette | The constraint system is rebuilt — a recklessness ladder, an intensity dial, a live catalogue monitor. The lever is now a rope you drag, with resistance, release and a petal burst; clicking still works for keyboards and phones. |
| Diagnose | Returns named passes with counts, a `complete` flag and de-duplicated findings, so "nothing found" means the checks ran. |
| Chrome | Fox app icon and favicon, sidebar buttons with state and motion, evenly sized loader tiles, Paper and Purpur marks, source logos on mod rows, art in the job drawer and on the delete confirm. |

**The layering is the part worth understanding.** The polygon used to be a
`z-index:-1` pseudo-element, which paints behind its parent's background *only
while the parent is not a stacking context*. The entrance animation put a
`transform` on every card, so every card became one, and the blob started
painting over the card and under the text. Relying on that trick was the bug;
the three explicit layers are the fix, and `check_frontend.check_layering`
enforces them — including that no blanket `.card > *` rule exists, because
that selector outranks `.p5-hl`'s own `position:absolute` on specificity and
collapses the blob to a zero-size element in flow.

Two more traps from this pass, both now guarded:

- **Never animate *from* `opacity:0` with `fill:both`.** If the document
  timeline stalls — a hidden tab, a throttled preview pane — the animation
  never starts and the content is permanently invisible. Every entrance
  animation is translate-only for that reason.
- **Cache control is header-based** (`_StaticCache`), not `?v=` fingerprints.
  ES module imports carry no query string, so a fingerprint on the entry point
  does nothing for anything it imports.

## 2. Verified in this session

Driven against a mock Crafty (`dev/`-adjacent, not committed) and against the
live CurseForge, Modrinth, Mojang, FabricMC, NeoForged, PaperMC and PurpurMC
APIs:

| area | evidence |
|---|---|
| Every route renders | 6 top-level screens + 9 instance tabs, zero console errors |
| Loader catalogue | All seven loaders resolved real version lists **with no Crafty at all** |
| Paper plugins | Search, compat labels, Folia strictness, dependency preview picking the *bukkit* build of spark rather than its NeoForge one |
| File manager | Browse, per-folder sizes, editor with gutter, edit → confirm → save → verified on disk → snapshot taken |
| Players | Both routes: console on a running server, JSON file on a stopped one, each saying which it took; Mojang UUID lookup |
| Client-only scoring | 6 targeted cases including the Forge mod that declares nothing and is caught by package layout |
| Backend suite | 141 checks across 5 test files |
| Front end | 125 checks (`dev/tools/check_frontend.py`), including the layering contract |
| Layering | Read back from computed styles in a real browser: card `isolation:isolate`, blob `position:absolute` at z 0, fill at z 1, bleeding 17 px left and 15 px top |
| Load time | Measured, not estimated: shell 4 ms, health 541 ms non-blocking, instances 32 ms, DCL 118 ms, load 387 ms |
| The rope | Dragged and clicked; recklessness ladder lit to `11111` at Unhinged, crest cycling, the roll starting from both |
| Config editor | Opened a real TOML beside the list, typed into it, row marked unsaved, status line counting lines and bytes |

Two real bugs were found by driving the UI rather than reading it, and both
are fixed: `confirmDialog` resolved `false` before it resolved `true` (so
**every** confirmation silently did nothing), and a class token with a
trailing space threw and took the whole screen down.

## 3. Unproven — the honest gaps

1. **No install has been run end to end in this session.** The installer was
   refactored (the loader ladder moved to `provision.py`) and its tests pass,
   but a real modpack has not been installed against a real Crafty since. That
   is the biggest untested surface.
2. **`create_empty` has not created a real server.** Every part of it is
   exercised — the catalogue, the version lists, the form, the job — but the
   final `crafty.create_server` call has only been made against a mock.
3. **A plugin has not actually been downloaded into `plugins/`.** Resolution,
   compatibility and the preview are verified; the upload step is the same
   `mods.add_mod` path the mod flow uses, but it has not been run.
4. **The assistant still has not produced a plan here.** Unchanged from the
   previous session: `/ai/analyse` and `/ai/crash-review` are wired and the
   endpoint answers, but no analysis has been run to completion, so the plan
   list and `_apply_actions` remain untested against real model output.
5. **No destructive button has been pressed against a real server.** Delete,
   kill and delete-world are wired and their confirmations work (and the
   confirmation bug above is fixed, so they now actually fire).
6. **The rope has not been pulled with a real finger.** It is driven by
   pointer events, which is the right API for it, and it was dragged with a
   synthetic pointer — but a touchscreen has not been near it.
7. **Below 900 px is drawn but not driven.** The layout collapses correctly in
   CSS — the rail becomes a drawer, grids go to one column — but it has not
   been used on a phone.

## 4. Known gaps and rough edges

- **The mod list is not virtualised.** Fine at 245 rows. Past ~600 the DOM
  gets heavy; the search box is the current answer.
- **`main.py` is 2,575 lines** and mixes routing with logic. Splitting by
  resource (`routes/instances.py`, `routes/files.py`, …) would make it
  navigable; nothing depends on it being one file.
- **The file manager cannot move a file between folders.** Crafty exposes
  rename-within-a-directory and nothing else, so a move would have to be
  download-and-re-upload. Left undone rather than done badly.
- **Player notes live in the instance** (`.blessforge-players.json`), so
  deleting the server deletes them. That is the right trade — they are about
  that server — but it is worth knowing.
- **Quilt** is still not in Crafty's catalogue, so Quilt packs cannot be
  created. Fabric, Forge, NeoForge, Paper, Purpur, Folia and Vanilla all work.
- **`velocity` and `waterfall` are recognised as plugin families** but are not
  offered on the create screen; they are proxies, not servers, and the rest of
  the app assumes a world.
- The open instance polls `/stats` every 6 s while the tab is visible. Two
  browser tabs double it.
- CurseForge file-level dependency metadata is still incomplete — a file can
  declare none while the jar requires something.

## 5. Things that will change under you

Two external contracts moved during this session and will move again:

- **PaperMC sunset its v2 API.** Everything under `api.papermc.io/v2` now
  answers 410. v3 is `fill.papermc.io/v3`, on a different host, with grouped
  version lists and downloads keyed `server:default`. `loaders.py` speaks v3.
- **Minecraft's version scheme changed.** Releases are now year-based (`26.2`,
  `26.1.2`) alongside the legacy `1.21.x` line, and NeoForge followed —
  `26.2.0.84` is four components where `21.1.250` was three.
  `loaders._neoforge_candidates` returns *both* readings and keeps whichever
  Mojang actually publishes, so the next scheme change costs a list entry
  rather than a bug. `packs.mc_from_neoforge` handles both shapes too.

If a version list comes back empty, check those two first.

## 6. How to check your work

```bash
cd /path/to/blessforge
for t in test_loader_detection test_job_stream test_install_decisions \
         test_roulette test_boot_verdict; do
  python dev/tools/$t.py | tail -1
done
python dev/tools/check_frontend.py
```

`check_frontend.py` is the one that catches this front end's own failure
modes. Note in particular that `node --check <file>` is **not** a syntax check
for an ES module — it parses as a script and will accept a file with a broken
string literal. Only `node --input-type=module --check` is real, which is what
the tool uses, and that distinction cost a screen in this session.
