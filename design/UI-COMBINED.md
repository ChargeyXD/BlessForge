# BlessForge UI — the combined design

Written 2026-10-14. Two handoffs came back from Claude Design against
`UI-REWORK-BRIEF.md`:

- **v1** (`handoff/`, "old update") — today's structure, re-skinned: a
  labelled rail with a pixel-fox companion, a *Needs you* band, grouped
  instance tabs (Run · Build · Care), ink-bordered washi panels with hard
  offset shadows, Archivo Black slab titles. Pink throughout.
- **v2** (`handoff-v2/`, "redesign") — a restructure: shrines as ema plaques
  hanging from shimenawa ropes, a torii-framed *Altar* per server, kanji
  ofuda slips for modules, lantern-segment meters, a jobs tray along the
  bottom edge, a separate admin *Gate*. Torii vermilion, wood and a tan rope.

This document is what the build follows. It takes v2's objects and v1's
legibility, keeps the pink shrine fox palette, and adds homes for every
feature added since the handoffs (world backups, schedules, history, stop
and crash rules, 2FA, API tokens, per-server grants, status page, jar
updates, live fleet events).

## 1. Colour: pink, nothing new

v2's orange is converted, not kept. No new hex values: every new token is an
alias of one `theme.css` already has.

| v2 token | was | now |
|---|---|---|
| `--shrine` | vermilion `#C8432E` / `#E8644C` | rose `#B23F5C` / `#F2879F` (v1's value) |
| `--shrine-deep`, `--shrine-soft` | rust, peach | `#8E3049` / `#F3D7DE`, dark `#D9788F` / `#3C232D` |
| `--on-shrine` | cream | `var(--paper)`; dark `var(--slab-fg)` |
| `--wood`, `--wood-2`, `--wood-ink` (ema plaques) | tan, brown | `--ema` = sakura-soft over card-2; `--ema-edge` = sakura-deep; `--ema-ink` = text |
| `--rope` (shimenawa) | straw gold | twisted cord of `--sakura-soft` / `--sakura-deep` / `--rose` |

Gold leaf stays where it already was (Finish setup, premium moments). State
colours stay the existing sage / amber / danger / plum / info.

## 2. Type

- **Potta One** (v2's brush) for the few display moments: page titles,
  server names, section headers on ofuda. Fallback Archivo Black, then the
  vendored Space Grotesk.
- **Plus Jakarta Sans** (v1, current) for everything read closely.
- **JetBrains Mono** (vendored) for labels, ids, logs.
- **Archivo** 800 for numbers in KPI tiles and meters.

## 3. Shell

- **Rail** (v1): 252 px dark band, labelled nav grouped *The shrine* (Fleet,
  Racks) · *Make* (New server, Discover, Mod Roulette) · *Keep* (Settings,
  The Gate for admins). Brand row `fox-prowl.gif`, two lanterns 狐 祝, recent
  servers (state dot, meta line), the **companion** (`walk.gif` patrols; mines
  while a job runs, celebrates or hurts when one ends; click for an emote),
  your hanko + sign out, theme, connection lantern. A pink rope runs down its
  right edge (v2). Under 900 px it is the existing drawer.
- **Topbar**: crumbs, brush title, mono subtitle, **Jump to… ⌘K** palette
  (screens, servers, the current server's tabs).
- **Jobs tray** (v2): a rope along the bottom edge with one hanging slip per
  live job (title, %, bar); it sways while live and is stamped 済 / ✕ / 止
  when it ends. Clicking opens the Activity drawer (the existing one, kept).
- **Toasts**: paper slips that drop from a cord, with the state mascot
  (`celebrate` ok, `hurt` bad, `surprise` warn).
- **Confirm**: a paper sheet under a torii beam; mascot per action
  (`stab` kill, `vanish` delete, `nod` restore); type-the-name guard.
- **Default-password banner**: a gold ofuda strip.
- **Live fleet**: `GET /api/fleet/events` (server-sent events) — a start,
  stop or crash shows up at once; the 15 s poll stays as the fallback.

## 4. Screens

### Fleet `#/`
Brush title, line of counts, `fox-pounce.gif`. KPI tiles (servers, players
online + a 24 h sparkline from `/api/fleet/history`, mods managed, needs
you). The *Needs you* band (v1) with the one fix each. Filter bar (All · Up ·
Down · Needs you, search, sort). Servers are **ema plaques hanging from a
rope** (v2), grouped *Needs you* first: lantern state, brush name, loader logo
· version · port, a round **hanko seal** for the state (稼 running, 起
starting, 休 stopped, 倒 crashed, 未 unfinished, 迷 lost), pack line, the
reason for a bad state, **lantern-segment meters** for CPU and RAM, players,
uptime, rack tag, actions.

### Instance `#/i/<id>/<tab>`
- **Threshold header** (v2): the server framed by a torii, shide streamers
  (v1), loader tile, brush name, fact chips, join address with copy, a large
  stamped hanko seal for the state.
- **State banner** (an ofuda strip with the state's mascot) for every state
  but running.
- **Vitals** (kept): torii in the tide (CPU), paper lantern (memory),
  blossoms (players) — restyled.
- **Ofuda shelf** (v2 slips, v1 groups):
  - *Run* — Overview 祭 · Console 声 · Players 人 · **History 史** (new)
  - *Build* — Mods / Plugins 具 · Files 蔵 · Configs 巻
  - *Care* — Tune 調 · Diagnose 診 · **Backups 守** (new) ·
    **Schedules 暦** (new) · Undo 戻 · **Rules 札** (new)
  Counts on slips; the current slip drops and turns rose; tabs a state
  cannot use are greyed with the reason.

New tabs:
- **History** — 6 h / 24 h / 7 d / 30 d; seals for uptime, peak players,
  average CPU and memory; three hand-drawn SVG charts (players, CPU, memory)
  with a crosshair readout and a table for screen readers. Gaps are gaps.
- **Backups** — world snapshots (`/worldbackups`): *Back up now* (job), a
  rope timeline of snapshots (reason, when, size, files), Restore (safety
  backup first, start after), Download as zip, Delete; settings (keep N,
  extra excluded folders, compress, back up before risky changes).
- **Schedules** — tasks as hanging slips with the rule in words, next run,
  last result; chained steps indented under their parent with the delay;
  create / edit (action, when: every N minutes · daily at · weekly on ·
  cron, with the next five runs previewed), presets (daily restart with
  warnings, announcement, backup every N hours), run now, pause, delete.
- **Rules** — how it stops (stop command, grace period), what counts as a
  clean exit (codes), whether a crash restarts it; the server jar (Paper /
  Purpur / Folia build, *Update now*, *Follow new builds* = a nightly
  `update_jar` schedule); who can reach this server (admins: grants per
  account).

### Settings `#/settings` (yours)
Account (password), **Two-factor sign-in** (QR + key, confirm with a code,
recovery codes shown once with copy / download, turn off, new codes), **API
tokens** (yours: create with a permission subset, servers and expiry —
shown once; revoke), Appearance, what your role may do.

### The Gate `#/gate/<section>` (admins)
A torii entrance with two kitsune seals. Sections: **Keepers** (accounts, roles,
reset password, reset 2FA, per-server grants), **Nodes**, **Status page**
(on/off, title, blurb, which servers, address, show names; preview link),
**Tokens** (everyone's; revoke), Connections, Assistant, Upkeep, Ledger,
Sessions. These are the admin halves of today's Settings.

### Sign-in
The panel behind a torii; `wave` idle, `run` while checking, `hurt` on a wrong
password, `shake` when locked out. A second step for two-factor: one code
field (`autocomplete=one-time-code`), *Use a recovery code instead*.

### Public status page `/status`
Standalone (no app shell, no sign-in): title and blurb, each chosen server as
an ema plaque with its lantern, hanko seal, players, version, join address
(copy); refreshes every 30 s; light and dark; petals.

## 5. Rules carried over (binding)

The brief's §0 and both handoffs agree: translate/scale entrances only,
gated on `html.js-motion`; never animate from a placeholder; no count-ups;
blob 0 / fill 1 / content 2 with `isolation:isolate`; a rAF simulation is
correct at rest; focus survives repaints; unknown is not zero; 44 px touch
targets; container queries for panels beside panels; reduced motion
collapses everything. `python dev/tools/check_frontend.py` must pass.

## 6. Assets

Mascots → `app/static/img/mascot/` (served `/assets/mascot/<name>.gif`,
`image-rendering: pixelated`). New logos → `app/static/img/`. The old raster
art (`appmark-fox.png`, `fox-head.png`, `fox-mascot.png`, `fox-loading.gif`,
`chargey-*.png`, `allay-helping.png`, `bee-working.gif`, `parrot-vibing.gif`,
`pig-loadbar.gif`, `shulker-guard.gif`, `creeper-delete-2x.png`,
`loader-forge.jpg`) is replaced by the mascots and removed.
