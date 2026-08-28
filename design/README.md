# design/ — the Claude Design source for the 2026-08-28 UI rebuild

`BlessForge.dc.html` is the canvas export from the Claude Design project
`2a040811-41d8-40af-84c2-f94597e9d192` ("BlessForge Server Diagnostics"),
fetched through the DesignSync MCP. It is the **specification** for the new
front end; it is not shipped and is not loaded by the app.

It is a design-canvas template, not runnable app code: `{{ expr }}`,
`<sc-if>` and `<sc-for>` are rendered by Claude Design's own React runtime
(`support.js` in that project, deliberately not copied here). Everything in it
is driven by hard-coded mock data — `SERVERS`, `MODS`, `CHECKS`, `RPOOL` and
friends near the bottom of the file. Read it for layout, tokens, copy and
interaction; get the real data from BlessForge's API.

## What it specifies

Shell: a fleet **spine** (left), a **topbar**, a **canvas**, and a job
**drawer**, plus four overlays (client-only review, confirm dialog, command
palette, toast) and a failure card.

Eight canvas screens: **Situation**, **Diagnose**, **Mods**, **Tune**,
**Console**, **Configs**, **Activity**, **Discover**.

Discover holds three modes: the catalogue search, importing a CurseForge
profile export, and **Mod Roulette** — a new feature with no backend yet.

## Tokens

| role | value |
|---|---|
| page | `#0B0C0E` |
| surfaces | `#08090B` `#0E1013` `#121417` `#1A1D22` |
| text | `#ECEAE7` → `#D5D3D0` `#B3AFAB` `#9C9EA4` `#8C8F96` `#6A6D74` `#5F6269` |
| ember (accent) | `#FF6B35` |
| amber / green / red / blue / violet | `#FFC44D` `#56D9A3` `#FF3B5C` `#7AA7FF` `#C77DFF` |
| type | Space Grotesk (UI), JetBrains Mono (heavily used — the design is mono-forward) |

Assets live in `app/static/img/`.

## assets

`app/static/img/` holds the design's assets, downscaled to roughly twice the
largest size the prototype displays them at. That took the set from **2.0 MB
to 341 KB** — `lucky-block.png` alone was a 2500x2500 PNG shown at 58 px.
The untouched originals are **not** committed: 2 MB of duplicates of files that
already ship, in a repo already carrying more binary weight than it should.
They are in the design export the user supplied (`BlessForge Server New
GUI.zip`, gitignored), and re-downscaling is one Pillow `thumbnail()` call if a
screen ever needs one larger.

`loader-vanilla.svg` is left alone at 152 KB. It is vector, so it is correct at
any size, and rasterising the one asset that scales perfectly to save a
hundred kilobytes is a bad trade. `loader-neoforge.png` is also original: it
ships an indexed palette that beats anything a re-encode produces.

## Where the design guessed wrong

It was drawn from a written brief with no access to the running app, so some of
its detail is invention. These are the places the implementation must not copy,
recorded as they were found:

| The design says | What is actually true |
|---|---|
| Console: `websocket · 41 ms` | Crafty exposes no push channel. The backend polls it and forwards new lines over SSE. The honest labels are the stream source (`live console` vs `latest.log`) and whether the server is running. |
| Assistant: `qwen2.5:14b · ollama @ 127.0.0.1:11434` | `qwen3:4b-instruct` on a **remote** endpoint (`OLLAMA_URL`). Read it from `/api/ai/status`; never hard-code it. |
| Server files live in `/srv/minecraft/<id>` | `/crafty/servers/<id>`, and it is returned as `path` on each instance. |
| `8 of 34 flags shown` | There are **25** JVM flags, 24 enabled by default, in 6 groups. |
| Crafty publishes `25565–2557…` | `25500–25600` (`properties.CRAFTY_PUBLISHED_RANGE`). |
| `Crashed on boot · 3 attempts` | Nothing counts boot attempts. Crafty reports a `crashed` flag; that is all there is. |
| A roulette hand of 5–8 mods | An artifact of its 30-mod mock pool. A hand is the requested count. See §5B of the handover for the rest of the roulette corrections. |
| Fleet states invented wholesale | `running`, `stopped`, `orphan`, `crashed` and `incomplete` are now computed in `/api/instances` as `state`, from Crafty's own stats plus a `complete` marker the installer writes. The UI reads that field; it does not infer. |

Two of its claims were fair but unmeasured, so the backend now measures them:
Crafty round-trip latency and free disk, both on `/api/health`.

Everything else in it — the layout, the tone, the copy, the tokens — is the
specification and should be followed.

## the previous front end

Not kept here — git already has it. Several screens have not been ported yet
and the old implementations are the clearest statement of what they did,
particularly the virtualised mod list and the config editor's gutter:

```bash
git show <commit-before-the-rebuild>:app/static/app.js > /tmp/old-app.js
```

The rebuild commit's parent is the one you want.
