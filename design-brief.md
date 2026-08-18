# BlessForge — Full GUI Overhaul Brief

You are redesigning the complete front end of **BlessForge**, a self-hosted web
app that installs CurseForge modpacks into **Crafty Controller** and then manages
those Minecraft server instances: mods, config files, `server.properties`, JVM
tuning, crash diagnostics, and a local-LLM troubleshooting assistant.

This is a **visual and interaction-design overhaul only**. Every existing
feature must survive, every backend call must still fire with the same payload,
and the app must keep working the moment the new files are dropped in. Nothing
about the Python backend changes.

> **Revision — modpack import.** Since this brief was first written, the app
> gained a second way to get a pack in: **importing a `.zip` exported from the
> CurseForge desktop app**, for packs the user assembled themselves and never
> published. It is fully built and working in `app.js` / `index.html` /
> `style.css`; design it, do not rebuild it. Everything it added is marked
> **[import]** below — a Browse-view call to action and header button, an
> Instances-view action, an eleventh modal, two new shared components
> (dropzone, upload progress), a variant of the Modpack tab, four new API
> endpoints, and the DOM hooks in §4.3.

---

## 1. Hard technical constraints (read first — these are not negotiable)

**No build step.** FastAPI serves three files verbatim from `app/static/`:
`index.html`, `style.css`, `app.js`. There is no npm, no bundler, no
preprocessor, no framework. Plain HTML, plain CSS, plain ES2020 in one IIFE.

**Exactly three files.** Do not split into more. The server's cache-busting
fingerprint only rewrites `/static/app.js` and `/static/style.css` and only
hashes those three filenames — a fourth file would be served stale forever.

**Zero runtime network dependencies.** BlessForge runs on a LAN box that
frequently has no route to the internet. No CDN scripts, no external
stylesheets, no icon-font packages, no Google Fonts as a hard dependency.
Icons must be inline SVG. Web fonts, if used at all, must be loaded the way the
current `index.html` does it (non-blocking `media="print"` swap) with a system
font stack that looks deliberate on its own — first paint must never wait on
DNS. Remote mod/modpack artwork (`logo` URLs from CurseForge/Modrinth) is the
one exception and must degrade to a lettered placeholder tile when it fails.

**Vanilla imperative DOM.** `app.js` binds with `document.querySelector("#id")`
and `[data-attr]`, and renders most UI by assigning `innerHTML` from template
literals. Therefore:

> **Every DOM `id` and every `data-*` hook listed in §4 must exist in the new
> markup with the same name and the same semantics, or you must supply an
> explicit old → new rename table covering 100% of the changes.** A redesign
> that silently renames `#modList` to `#mod-list` breaks the app.

**Some behaviour is already built.** Six defects were fixed in code before this
brief (confirm dialogs, inline modal errors, in-place mod-row updates, the
config unsaved-changes guard, job cancellation, and a first ARIA/focus layer).
§5 lists the JS contracts they depend on. Design them; do not rebuild them.

**Security.** All interpolated values go through the existing `esc()` HTML
escaper. Keep every user/API-derived string escaped in your templates. Do not
introduce `innerHTML` on raw API text anywhere new.

**Dark is the default** and must stay excellent. A light theme is welcome as an
addition (see §6), not a replacement.

---

## 2. What the app is and who uses it

One person self-hosting Minecraft servers for friends. They are technical
enough to edit a `.toml` file but they are not reading source code. They come
to BlessForge in three moods:

1. **"Install me a modpack."** Browse → pick a pack → pick a release → review
   what gets stripped → watch a 3–8 minute install stream. **[import]** Or, for
   a pack they built themselves in the CurseForge app: export it → drop the
   `.zip` on BlessForge → same review → same install stream. This second route
   is not a niche case; a self-hoster's pack is usually their own, and a
   catalogue search can never find it.
2. **"Something's broken."** A server won't launch. They want the crash reason
   and a one-click fix, fast, without reading a 4,000-line log.
3. **"Tinker."** Toggle mods, bump the heap, change the port, edit a config,
   check for mod updates.

Design for those three journeys. Mood 2 is where the current UI is weakest and
where the product actually earns its keep.

---

## 3. Complete screen and component inventory

### 3.1 Global chrome

- **Header** (sticky): brand lockup (clicking returns to Instances) · nav with
  three destinations — *Instances*, *Browse Packs*, *Activity* (Activity carries
  an unread-dot when jobs are running) · right side: an **AI status pill**
  (Ready / Off, tooltip names the model) and a **health pill** (`Crafty · N
  Servers` / `Crafty Unreachable` / `Backend Offline`).
- **Setup banner**: appears under the header when `CRAFTY_URL`, `CRAFTY_TOKEN`
  or `CURSEFORGE_API_KEY` are missing or rejected. Lists each broken
  integration with its error and tells the user which env vars to set. This is
  the **first-run screen for most users** — currently a bare yellow strip. It
  deserves a real empty/onboarding treatment.
- **Toasts**: bottom-corner stack, three kinds (neutral / ok / err). Errors live
  8.5s, others 4.5s. They are currently the *only* surface for many failures.
- **Modal system**: one generic modal (title, close X, scrollable content, footer
  action row) with a `wide` variant (~1100px). Ten distinct modals use it — §3.6.

### 3.2 View — Instances (landing page)

Title + subtitle, `Refresh`, **[import]** `Import Export`, and `Install Modpack`
actions, then a card grid.

**Instance card** shows: server name · pack name + version (or "Custom
Minecraft Server") · Running/Stopped pill · tag row (`BlessForge`-managed badge,
loader, MC version, port) · **when running**: players online, RAM % with a mini
gauge, CPU % with a mini gauge (gauges tint ok/mid/high at 65%/85%) · footer with
inline Start-or-Stop, Restart (running only), and a "Click to manage →" hint.
The whole card is a click target except the buttons.

**Empty state**: no instances found → illustration + "Browse Modpacks" CTA.

### 3.3 View — Browse Modpacks

Header carries one action: **[import]** `Import Export`.

Filter bar: search input · MC version select (populated from the API, ~60
entries) · loader select (NeoForge/Forge/Fabric) · sort select (Popularity /
Total Downloads / Recently Updated) · Search button. Then a results grid of
**pack cards** (art, name, summary, download count, up-to-3 version pills), and a
`Load More` button that hides when a page returns < 30 results.

**[import] Import call to action** (`#importCta`), sitting between the filter
card and the results grid: dashed-border card, upload glyph, "Built your own
pack?" headline, one line of explanation, and an `Import a pack export` button
(`#importCtaBtn`). It is deliberately *above* the results and always visible,
not an empty-state fallback — a user with a private pack gets zero results from
every search they can type, so the way out of that dead end has to be on screen
before they conclude the app cannot do it. This is a placement constraint, not
a styling suggestion; you may redesign the card freely but not demote it to an
empty state or a menu item.

### 3.4 View — Instance detail

Breadcrumb (`All Instances / <name>`) → **hero card**: server name, live status
pill, tag row (pack, loader, MC version, port, players online), and three power
buttons (Start / Stop / Restart). Then five tabs:

**Tab: Mods**
- Toolbar: filter input · state select (All / Enabled / Disabled / Unidentified)
  · `Identify Unknown` (fingerprints jars against CurseForge + Modrinth) ·
  `Check Updates` · `Add Mod`.
- List header: Select-All checkbox · count pill (`N mods (E enabled · D
  disabled)`) · bulk action bar that appears on selection (Enable / Disable /
  Delete + selected count).
- **Mod row**: select checkbox · icon or letter tile · name (+ `client?` pill for
  suspected client-only jars, + `dep` pill for auto-installed dependencies) ·
  jar filename + size · installed version + a `Change…` button (or `n/a` when the
  jar was never identified) · enable/disable toggle switch · delete button.
  Disabled rows render dimmed.
- Typical list length is **80–300 rows**. The current design does not cope.

**Tab: Configs**
- Two-pane split. Left: filter input + file list grouped by owning mod, with the
  active file highlighted. Right: file path, a status pill (`N lines · TOML`),
  `Save Changes` (disabled for read-only types), and a full-height plain
  `<textarea>` editor.

**Tab: Troubleshoot & AI**
- Action row: `Run Health Checks` · `Deep Dependency Scan` (downloads jars and
  builds the full dependency graph — slow, streams progress) · `Ask AI
  Assistant` · a hint line ("Preloading local model into RAM…").
- **AI report card**: confidence pill, model pill, summary paragraph, a
  performance line (`43s total — read 1,855 tokens in 22s (84/s) · wrote 310 in
  18s`), optional notes block, then **recommended actions** — each with a
  checkbox, description, "Safe"/"Major Action" pill, a *why*, and the raw
  `action(args)` in mono. A single `Apply Selected Fixes` button; major actions
  re-confirm.
- **Findings list**: each finding has a severity (critical / error / warning /
  info), title, detail, optional `<pre>` evidence excerpt, and an optional
  `Fix Automatically` button. Severity must be legible at a glance, sorted
  worst-first.
- **Log card**: path line + a monospace tail of `crash-report` and `latest.log`.

**Tab: Optimizer** (the densest screen — currently a wall of switches)
- Host hardware stat grid: total RAM, available RAM, CPU cores, installed mods,
  plus a CPU model line and optional note banner.
- Heap allocation: numeric input + a meter showing heap vs. total system RAM,
  the basis for the recommendation, pack-requested GB, safe ceiling, reserve,
  warning banners, and the currently applied `-Xms/-Xmx` + flag count.
- **Aikar's JVM flags**: grouped toggle rows (label, `Active` pill, the raw flag
  in mono, a plain-English *why*), with Select All / Select None / Reset to
  Recommended.
- **Server properties optimization**: toggle rows for performance-tuned
  key=value pairs, each showing `Active` or `Current: <value>` and a why.
- Apply bar: "takes effect on next restart" + `Apply Optimization Profile`.
- **Port card**: port number input, "also set query.port" checkbox, Save;
  explains that Crafty's record and `server.properties` must agree, warns about
  Crafty's published range (25500–25600) and ports claimed by other instances.
- **Full `server.properties` editor**: ~60 keys grouped by category, filterable,
  each row typed to the right control (switch / select / number / text) with a
  description, a `not set` / `changed from default` / `unsaved` pill, and
  guarded keys shown read-only with the reason. Sticky-ish `Save (N)`.

**Tab: Modpack** — two variants, chosen by `manifest.pack.source`.

- *Catalogue pack* (`source: "curseforge"`): pack name, installed-version pill,
  source pill (`Official Server Pack` vs `Assembled Manifest`), loader + MC
  pill, counts of excluded client mods and failed downloads, and `Switch
  Modpack Release Version` → a release table (release, MC version, Server Pack
  vs Manifest, Switch button).
- **[import]** *Imported pack* (`source: "upload"`): same card, but an
  `Imported pack` pill instead of a version pill, the archive filename in mono
  beneath the title, an explanatory banner, and the primary button reads
  `Re-import an Updated Export` — it opens the import modal in re-import mode
  instead of loading a release table. A private pack has no releases to move
  between, so there is nothing a version table could ever be filled with; the
  update path is "export again, drop it in". Both variants keep the id
  `#switchPack` on the primary button.
- Empty state when the instance wasn't installed by BlessForge.

### 3.5 View — Activity & Tasks

A list of job cards: title, status pill (pending/running/done/error/cancelled),
current step + percent, progress bar, error line, and a **Cancel** button on
every active job. The list self-refreshes every 2.5s while anything is running,
and the nav badge polls every 15s. Finished jobs are pruned after an hour.

Still missing, and worth designing for: once the job modal is dismissed with
*Run in Background* there is no way to reopen it from this list. A "Watch"
affordance per active job would close that loop — treat it as a new element.

### 3.6 Modals (all eleven)

1. **Job progress** (`followJob`) — used by *every* long operation. Step label,
   elapsed clock, progress bar, a collapsible **AI token stream** pane
   (character counter, auto-scroll that unpins when the user scrolls up), a
   de-duplicated log console with info/warn/error levels, a persistent inline
   error banner, and two footer actions: `Cancel Task` and `Run in Background`.
   Ends in a success, failure, or cancelled state.
2. **Pack detail** — art, summary, downloads, CurseForge link, and a release
   table (name, MC version, loader, channel pill, Server Pack/Manifest, Install).
3. **Install wizard** — server name, port, and three checkboxes: prefer official
   server pack (disabled when none exists), review & strip client-only mods,
   auto-tune JVM/memory. Cancel / Continue.
   **[import]** Shared with the import route, which changes three things: the
   title reads `Install Imported Pack — <name>`, a 4-up stat grid appears above
   the fields (runtime, mods, override files, RAM the pack asks for) followed by
   any archive warnings, and the server-pack checkbox is always disabled with
   its label replaced by a statement of what the archive is (`Imported archive
   is already a server pack — installed as-is` / `Imported export assembled from
   its manifest (mods fetched from CurseForge)`). Design that stat grid: it is
   the only moment the user gets to check the archive is the one they meant
   before a multi-minute install starts.
4. **Preflight review** — a 4-up stat grid (pack, runtime, total mods, tuned
   heap), warning banners, then a checkbox list of candidate client-only mods.
   Each row: icon, name, one of `Client-Only` / `Review Needed` / `Keep —
   Dependent Present`, the reasons that classified it, who requires it, and the
   jar name. Checked = excluded from the install. "Proceed & Install".
5. **Add Mod** — search box + source select (CurseForge/Modrinth) + a note about
   the active loader/MC filter, then a result grid. Each result shows an
   `Installed` pill and its current version when already present, with the
   primary button reading `Add latest` or `Update`, plus a `Versions` escape
   hatch.
6. **Version picker** (from a result, or a mod row's `Change…`) — "only
   compatible" checkbox + a release table; the installed release is highlighted
   as `Active`/`Installed`.
7. **Dependency preview** — the target mod, then every dependency the install
   will pull in (each individually deselectable), plus counts of
   already-satisfied deps and warnings, then `Install N Mod(s)`.
8. **Mod updates** — table of name / installed / latest with a per-row `Update`.
9. **Fix incompatible versions** — checkbox rows of `current → suggested` swaps,
   plus an "manual attention required" list for the ones that couldn't resolve.
10. **Find missing dependency** — CurseForge and Modrinth candidate sections for
    a mod id parsed out of a crash log; picking one hands off to Add Mod.
11. **[import] Import a CurseForge export** (`openImportModal`) — wide modal,
    two ways in and two modes.
    - **Dropzone** (`#impDrop`): click *or* drag-and-drop, keyboard-operable
      (`role="button"`, `tabindex="0"`, Enter/Space open the picker), with a
      `.dragging` state while a file is over it. It hides once a file is
      accepted.
    - **Upload progress** (`#impProgress`): a stage label, a percentage, and a
      progress bar driven by real `XMLHttpRequest` upload events. At 100% the
      label switches to "Reading the archive…" because the server then opens
      and analyses the zip, and a bar parked at 100% otherwise reads as a hang.
      Cancelling the modal aborts the transfer.
    - **Help disclosure** — a collapsed `<details>` with the four steps for
      producing an export in the CurseForge app. Most users have never made
      one; this is the difference between the feature working and the feature
      being mysterious.
    - **"Already imported" list** — archives still on the server, each with its
      pack name, filename, and a summary line (`forge 1.20.1 · 302 mods · 618
      override files · 3.1 MB`), a `Use this` button, and a delete button with
      a confirm. Re-installing a 900 MB export must not mean uploading it
      twice.
    - **Re-import mode** (opened from the Modpack tab with a server id) adds a
      banner explaining that the world, name and port are kept, and on success
      goes straight to preflight review rather than the install wizard.
    - Errors surface in the modal's own `.modal-error` banner, never as a
      toast: the dialog stays open so the user can pick a different file.

### 3.7 Shared components

Pills (neutral / ok / warn / err / info / accent, with an optional status dot) ·
banners (neutral, error) · cards · finding blocks (4 severities) · stat grids ·
meters · progress bars · log/console panes (plain + streaming) · toggle switches
· custom checkboxes · custom selects · search inputs with a leading icon ·
tables in a horizontal-scroll wrapper · option rows (`optrow`) · review rows ·
grouped list sidebars · spinners · empty states · mod-icon tiles with lettered
fallback · mini gauges · **confirm dialogs** (message + optional monospace
detail block + a danger variant) · **inline modal error banners** ·
**[import] file dropzone** (idle / hover / focus / `.dragging` / hidden-once-
accepted) · **[import] upload progress block** (stage label + percentage +
determinate bar) · **[import] promo card** (`.import-cta`: icon tile, headline,
sub, primary action).

---

## 4. Backend contract — the part that must not break

### 4.1 API surface (every call the UI makes)

```
GET    /api/health                                        health pill + setup banner
GET    /api/ai/status                                     AI pill
POST   /api/ai/warm                                       prewarm on Troubleshoot tab

GET    /api/meta/minecraft-versions                       MC version select
GET    /api/browse/modpacks?q,game_version,loader,sort,index,page_size
GET    /api/browse/mods?q,source,game_version,loader,page_size
GET    /api/modpacks/{mod_id}/files?page_size
GET    /api/mods/{source}/{project_id}/versions?game_version,loader

GET    /api/instances                                     instance cards
GET    /api/instances/{id}                                instance detail
POST   /api/instances/{id}/action/{start_server|stop_server|restart_server}

GET    /api/instances/{id}/mods
POST   /api/instances/{id}/mods/toggle          {file, enabled}
POST   /api/instances/{id}/mods/bulk-toggle     {files[], enabled}
POST   /api/instances/{id}/mods/delete          {files[]}
POST   /api/instances/{id}/mods/add             {source, project_id, file_id,
                                                 replace_file?, with_dependencies,
                                                 skip_dependencies[], name}  → {job_id}
POST   /api/instances/{id}/mods/resolve         {source, project_id, file_id}
POST   /api/instances/{id}/mods/identify        → {job_id}
POST   /api/instances/{id}/mods/icons
GET    /api/instances/{id}/mods/updates

GET    /api/instances/{id}/configs
GET    /api/instances/{id}/configs/read?path
POST   /api/instances/{id}/configs/write        {path, content}

GET    /api/instances/{id}/diagnose
POST   /api/instances/{id}/deep-scan            → {job_id}
POST   /api/instances/{id}/ai/analyse           → {job_id}
POST   /api/instances/{id}/ai/apply             {actions[], confirmed:true}
POST   /api/instances/{id}/fix/accept-eula
POST   /api/instances/{id}/fix/java             {minecraft}
POST   /api/instances/{id}/fix/versions         {files[]}
GET    /api/diagnose/dependency/{mod_id}?game_version,loader

GET    /api/instances/{id}/optimize
POST   /api/instances/{id}/optimize             {heap_gb, flags[], properties{},
                                                 xms_equals_xmx}
GET    /api/instances/{id}/properties
POST   /api/instances/{id}/properties           {updates{}}
GET    /api/instances/{id}/port
POST   /api/instances/{id}/port                 {port, update_query, force}

POST   /api/uploads/modpack                     multipart form field "file"   [import]
                                                → upload record (see §4.2)
GET    /api/uploads                             {items[], limit}              [import]
DELETE /api/uploads/{upload_id}                 {deleted}                     [import]

POST   /api/install/preflight                   {mod_id, file_id,
                                                 prefer_server_pack}
                                                | {upload_id}      → {job_id}
POST   /api/install/modpack                     {mod_id, file_id, server_name,
                                                 port, prefer_server_pack,
                                                 optimize, exclude_files[]}
                                                | {upload_id, server_name, port,
                                                   optimize, exclude_files[]}
                                                                   → {job_id}
POST   /api/instances/{id}/switch-pack-version  {mod_id, file_id}
                                                | {upload_id, exclude_files[]}
                                                                   → {job_id}

GET    /api/jobs
GET    /api/jobs/{job_id}/events                Server-Sent Events stream
POST   /api/jobs/{job_id}/cancel                (exists, currently unused by UI)
```

Errors come back as `{detail: "..."}` with a 4xx/5xx status; the UI throws and
surfaces `detail` verbatim.

**[import]** The three install endpoints take **either** CurseForge ids **or**
an `upload_id`, never a mix — send one shape or the other, never both halves of
each. Sending neither is a 400 (`either upload_id, or both mod_id and file_id,
are required`), as is an `upload_id` whose archive has been pruned. The upload
endpoint is the app's **only** `multipart/form-data` request and the only one
sent with `XMLHttpRequest` rather than `fetch`, because it is the only one whose
progress the user needs to see.

### 4.2 Key response shapes

```jsonc
// GET /api/instances → items[]
{ server_id, name, port, path, type, executable, auto_start, created,
  pack: {name, version, project_id, file_id, install_source,
         source, upload_id, archive}|null,
  minecraft, loader, managed, running, players, cpu, mem }
// pack.source ∈ "curseforge" | "upload"           [import]
// For "upload": project_id/file_id are null, version is usually "" (an export
// rarely names one), and `archive` is the zip filename. Anything rendering
// `pack.name + " " + pack.version` must survive an empty version.

// GET /api/instances/{id}/mods
{ directory, count, enabled, pack, minecraft, loader,
  mods: [{ file, path, enabled, size, modified, name, version, source,
           project_id, file_id, logo, required_by, client_only_guess,
           identified }] }

// GET /api/instances/{id}/configs
{ count, roots, groups: [{owner, count}],
  files: [{path, name, size, bytes, modified, editable, owner, root}] }

// GET /api/instances/{id}/configs/read
{ path, content, lines, editable, language }

// GET /api/instances/{id}/diagnose
{ findings: [{severity, category, title, detail, evidence,
              fix: {action, ...args}|null}],
  log_tail, crash_tail, log_path, crash_path, has_logs, minecraft, loader, pack }
// fix.action ∈ accept_eula | raise_ram | set_java | disable_mods |
//              install_dependency | fix_versions | retry_mod |
//              find_client_only | edit_file

// AI analyse result
{ available, ok, confidence: high|medium|low, model, summary, notes,
  actions: [{action, args, description, why, major}], rejected[],
  stats: {total_seconds, load_seconds, prompt_tokens, prompt_seconds,
          prompt_tokens_per_second, output_tokens, generate_seconds,
          output_tokens_per_second} }

// GET /api/instances/{id}/optimize
{ host: {total_ram_gb, available_ram_gb, cpu_count, cpu_model, note},
  pack, mod_count, minecraft, loader,
  memory: {heap_gb, requested_gb, ceiling_gb, reserve_gb, basis, warnings[]},
  current: {xmx_mb, xms_mb, flags[], exists, extra_flags},
  flags: [{flag, label, group, why, enabled, recommended, applied}],
  properties: [{key, value, why, enabled, applied, current}],
  jvm_file_supported, note, execution_command }

// GET /api/instances/{id}/properties
{ file, raw, count, groups,
  items: [{key, value, type: bool|int|enum|string, choices[], group,
           description, absent, modified, guarded}] }

// GET /api/instances/{id}/port
{ crafty_port, properties_port, query_port, in_use_by_others: [{port, name}],
  published_range: [25500, 25600], mismatch, note }

// Catalog item (CurseForge or Modrinth, normalised)
{ source, id, slug, name, summary, downloads, logo, url, server_side,
  latest_files: [{file_id, display_name, game_versions[], loaders[],
                  release_type, date, server_pack_file_id}] }

// Dependency resolve
{ root, dependencies: [{project_id, name, version, logo, size, required_by}],
  already_satisfied[], conflicts[], warnings[], total_downloads, total_bytes }

// Preflight review
{ mod_id, file_id, upload_id, java_version,
  pack: {name, version, install_source, source, archive},
  minecraft, loader, memory, host, warnings[],
  review: {total_mods, confirmed, uncertain, protected, server_mods,
           candidates: [{file_name, name, logo, recommendation: remove|review|keep,
                         reasons[], required_by_others[], bundled}]} }
// candidates[].bundled is true for a jar that shipped inside the archive's
// overrides/mods rather than being listed in the manifest -- it has no
// project_id, no logo, and can never be version-checked later.   [import]

// POST /api/uploads/modpack, and GET /api/uploads → items[]     [import]
{ upload_id, file_name, size, sha1, uploaded_at,
  summary: { kind: "manifest"|"server_pack", name, version, minecraft, loader,
             loader_version, crafty_loader, java_version, recommended_ram_mb,
             manifest_mods, override_jars, override_files, override_roots[],
             warnings[], needs_curseforge, installable } }
// summary is produced offline from the archive alone -- no CurseForge calls --
// so it is available the instant the upload finishes. `installable` is false
// when the loader or Minecraft version could not be determined; `warnings`
// carries things like dropped unsafe paths or a client-only-looking export.

// Job snapshot (also the SSE payload shape)
{ id, kind, title, status: pending|running|done|error|cancelled, step,
  percent, error, result, created, finished, elapsed,
  log: [{t, level, message}], stream }
// SSE events: snapshot | start | step | log | stream | end
```

### 4.3 DOM hooks that must survive (or be renamed with a table)

**IDs:** `homeBtn` `topNav` `activeJobsBadge` `aiPill` `health` `setup`
`view-instances` `view-browse` `view-instance` `view-jobs` `refreshInstances`
`gotoBrowse` `gotoImport` `instances` `emptyBrowseBtn` `q` `mcv` `loader` `sort`
`searchBtn` `importPackBtn` `importCta` `importCtaBtn`
`packs` `morePacks` `backToList` `crumbName` `instName` `instState` `instMeta`
`instTabs` `tab-mods` `tab-configs` `tab-troubleshoot` `tab-optimize` `tab-pack`
`modFilter` `modState` `identifyMods` `checkUpdates` `addModBtn` `selAll`
`modStats` `bulkBar` `selCount` `modList` `cfgFilter` `cfgList` `cfgPath`
`cfgStatus` `cfgSave` `cfgEditor` `runDiag` `runDeep` `runAI` `aiHint` `aiOut`
`diagOut` `logCard` `logPath` `logTail` `optOut` `packOut` `jobList` `toasts`
`modalRoot`

Plus IDs created at runtime inside rendered markup and modals: `optHeap` `optAll`
`optNone` `optRec` `optApply` `portCard` `portInput` `portQuery` `portSave`
`propsCard` `propFilter` `propsSave` `switchPack` `packVersions` `aiApplyAll`
`amQ` `amSrc` `amGo` `amResults` `depGo` `vOnlyCompat` `vBody` `fvBody` `fvGo`
`depBody` `iName` `iPort` `iServerPack` `iReview` `iOptimize` `verList` `jStep`
`jClock` `jBar` `jStreamWrap` `jStream` `jStreamMeta` `jLog` `amBack`
`impDrop` `impFile` `impProgress` `impStage` `impPct` `impBarWrap` `impBar`
`impRecent`

Plus IDs added by the pre-design fixes in §5: `jBarWrap` (the
`role="progressbar"` wrapper around `#jBar`) and `tabbtn-mods`
`tabbtn-configs` `tabbtn-troubleshoot` `tabbtn-optimize` `tabbtn-pack` (each
tab button, referenced by its pane's `aria-labelledby`).

**Data attributes:** `data-view` `data-tab` `data-open` `data-power`
`data-card-power` `data-sid` `data-bulk` `data-file` `data-del` `data-ver`
`data-fix` `data-add` `data-pick` `data-src` `data-name` `data-have` `data-up`
`data-sw` `data-pk` `data-dep` `data-i` `data-pid` `data-key` `data-value`
`data-flag` `data-rec` `data-x` `data-cancel` `data-imp` `data-impdel`

**Classes read by JS:** `active` `hidden` `modSel` `modToggle` `depSel` `rvSel`
`fvSel` `aiSel` `flagSel` `propSel` `propEdit` `view` `tabpane` `inst-card`
`modrow` `mod-sub` `switch` `spin` `modal` `modal-error` `content` `foot`
`cfg-status-pill` + its `dirty` modifier · **[import]** `dragging` (added to
`#impDrop` on dragenter/dragover, removed on dragleave/drop) and `import-row`
(the row `closest()`-ed and removed when an archive is deleted)

**Required ARIA structure** (added in §5, must be carried into the new markup):
`#instTabs` is `role="tablist"`; each tab button is `role="tab"` with
`aria-controls` + `aria-selected`; each `.tabpane` is `role="tabpanel"` with
`aria-labelledby` + `tabindex="0"`; `#toasts` is `role="status"
aria-live="polite"`; `#health`, `#aiPill` and `#setup` are `role="status"`;
`#homeBtn` is a `<button>`, not a `<div>`.

---

## 5. Behaviour already fixed in code — preserve it, don't re-solve it

Six defects were repaired in `app.js` / `index.html` / `style.css` **before**
this brief was written, and the import feature (§7 below) was built **after**
it. All of this behaviour exists and works; your job is to give it a proper
visual design, not to rebuild it. Each item comes with a JS contract the new
markup must keep honouring.

1. **Confirm dialogs replace `window.confirm()`.** All six destructive
   confirmations (delete one mod, delete selected mods, disable mods from a
   finding, apply major AI actions, reassign a claimed port, switch pack
   release) now route through:

   ```js
   await confirmDialog({ title, message, detail, confirmLabel,
                         cancelLabel, danger })  // → Promise<boolean>
   ```

   It builds on the standard `modal()`, renders `.confirm-message` plus an
   optional monospace `.confirm-detail` block (used for file lists), focuses
   **Cancel** by default, and resolves `false` on backdrop click, X, or Escape.
   **Design the confirm dialog and its `danger` variant as a first-class
   component.**

2. **Modals have a persistent inline error surface.** `modal()` returns
   `{ el, content, foot, buttons, close, error }`. `error(msg)` reveals a
   `.modal-error` element sitting between `.content` and `.foot`; `error("")`
   hides it. The version switcher, dependency preview, update list and job
   modal now report failures there instead of via a toast that vanishes.
   **Style `.modal-error`, and keep its position in the modal's DOM order.**

3. **The mod list updates in place.** Toggling no longer refetches and repaints
   the whole list. `applyModState(previousFile, newFile, enabled)` patches the
   single row — including its filename, since enable/disable is a rename
   between `.jar` and `.jar.disabled` — and `removeModRows(files)` drops
   deleted rows. Scroll position and checkbox selection survive. This requires
   the row markup to keep: `.modrow[data-file]` as the row, `.mod-sub` as the
   filename line, `.switch` as the toggle's label, and `[data-file]`,
   `[data-del]`, `[data-ver]` on the descendants that carry them.
   *Deliberate behaviour:* a row that no longer matches the active filter stays
   visible until the next load rather than vanishing under the cursor — give
   that state a visual treatment rather than designing it away.

4. **The config editor guards unsaved changes.** `state.cfgLoaded` holds the
   pristine copy; `isCfgDirty()` compares it to the textarea. Switching files,
   leaving the tab, leaving the view, opening another instance, and closing the
   browser tab all prompt first. `#cfgSave` is disabled until dirty, `#cfgStatus`
   gains a `.dirty` class and reads `Unsaved changes · N lines · TOML`, and
   read-only files set `readOnly` on the textarea.
   **Design the dirty state properly** — a `.dirty` status pill is the minimum;
   a dirty marker on the file in the left-hand list is the obvious addition and
   is *not* yet implemented, so treat it as a new element.

5. **Job cancellation is wired up.** `POST /api/jobs/{id}/cancel` is now reachable
   from two places: a `Cancel Task` footer action in the job modal
   (`m.buttons[0]`, auto-disabled once the job settles) and a
   `[data-cancel="<job id>"]` button on every active card in Activity. The
   Activity list self-refreshes every 2.5s while anything is running, the nav
   badge polls every 15s from any view, and `cancelled` is a first-class status
   alongside done/error. **Design the cancelled state and both cancel
   affordances.**

6. **A first accessibility layer exists** — build on it, don't replace it.
   `#instTabs` is a real `role="tablist"` with `aria-controls`/`aria-selected`
   kept in sync by `showTab()`; panes are `role="tabpanel"`; `showView()`
   maintains `aria-current="page"`; modals are `role="dialog" aria-modal="true"`
   with a labelled title, a Tab focus trap, Escape-to-close and focus restored
   to the opener; `#toasts` is a polite live region and error toasts carry
   `role="alert"`; the job bar is a `role="progressbar"` with a live
   `aria-valuenow` (`#jBarWrap`); `#homeBtn` is a real `<button>`; mod-row
   toggles, selects and delete buttons have `aria-label`s; and `style.css` ends
   with a `:focus-visible` ring plus a `prefers-reduced-motion` block.
   **The focus ring is currently a plain 2px amber outline — make it part of the
   design system, and carry the roles into every new component.**

7. **[import] Modpack import is built end to end.** Uploading, analysing,
   reviewing and installing a CurseForge profile export all work today. Four
   contracts the new markup must keep:
   - `#impDrop` is a keyboard-operable `role="button"` that opens the hidden
     `#impFile` input on click, Enter or Space, and toggles `.dragging` on
     dragenter/dragover and off on dragleave/drop. Never replace it with a
     bare `<input type="file">`: half the users drag the zip in.
   - The upload is an `XMLHttpRequest` so `upload.onprogress` can drive
     `#impBar` / `#impPct` / `#impBarWrap[aria-valuenow]`, and so cancelling
     the modal can `abort()` a half-sent 900 MB archive. `#impStage` switches
     to "Reading the archive…" at 100%.
   - `#impDrop` gets `.hidden` and `#impProgress` loses it when a transfer
     starts; a failure reverses both and writes the reason to the modal's
     `.modal-error` banner, leaving the dialog open.
   - The install wizard and the preflight review are **shared** with the
     catalogue route. They branch on one thing: an imported pack sends
     `{upload_id}` where a catalogue pack sends `{mod_id, file_id}`. Do not
     fork them into separate screens — the review step is the same decision
     either way, and duplicating it means two places to keep correct.

---

## 5b. What's still wrong with the current UI (this is your list)

1. **Density collapse.** A 250-mod list, a 60-key properties editor, and a
   40-flag optimizer all render as one flat, unvirtualised, equally-weighted
   stream of rows. There is no sticky header, no grouping affordance, no
   scannable rhythm. This is the single biggest failure and the main reason for
   the overhaul.
2. **The Optimizer tab is five unrelated tools stacked vertically** (host stats,
   heap, JVM flags, property presets, port, full properties editor) with no
   sub-navigation. Users scroll past the thing they wanted.
3. **The config editor is still a raw `<textarea>`** — no line numbers, no
   gutter, no syntax awareness. It is the most-used destructive surface in the
   app. (The unsaved-changes half of this is fixed; the editor itself is not.)
4. **The job progress modal is the app's most important screen** (installs run
   3–8 minutes, AI analysis 30–90 seconds on CPU) **and it's a bar plus a black
   log box.** It should feel like a real progress experience: named phases,
   what's downloading, what's left, and a genuinely readable streaming pane.
5. **Findings and AI actions look the same weight regardless of severity.** A
   `critical` "server cannot start" reads like an `info`.
6. **Almost no responsive design** — one 960px breakpoint. The instance detail
   tabs, split config pane, and wide tables have no small-screen story.
7. **Empty and error states are afterthoughts** — mostly a grey sentence. The
   setup banner in particular is the first thing a new user sees, and it is a
   bare strip of text.
8. **Loading states are a spinner and a sentence** everywhere, including places
   where skeletons would tell the user what's coming.
9. **Inline `style="..."` is scattered through both HTML and JS templates** —
   the design system has no real authority. The new CSS should make every
   inline style unnecessary.
10. **A dismissed job cannot be reopened.** *Run in Background* is a one-way
    door; Activity lists the job but offers no way back to its live stream.
11. **The instance card's click target is ambiguous** — the whole card navigates,
    except the buttons inside it, with only a "Click to manage →" hint to say so.
12. **A successful install throws away everything it just told you.** When a job
    reaches `done`, `followJob` calls `m.close()` and hands `result` to a
    callback that ignores it — the modal vanishes, the log is destroyed, and the
    user lands on the Instances view. Everything consequential goes with it.
    These are real lines from a real 300-mod install:

    ```
    [warn] The pack wants 8.3 GB but this host can only safely give 3.5 GB.
    [warn] Under 4 GB, large modded packs often fail to finish loading.
    [warn] Skipped 1 client-only mods: sound-physics-remastered-forge-1.20.1-1.5.1.jar
    [warn] 1 exclusion(s) matched no file in this pack and had no effect: …
    [info] Note: the export was built against Java 21; this instance runs Java 17.
    ```

    Every one of those still matters *after* the install — the first two predict
    the server failing to boot. The install `result` also carries a
    `problems[]` array of mods that could not be downloaded, and **nothing in
    the UI renders it**; its only trace is a count on the Modpack tab saying
    "check Troubleshoot". Two things are wanted here: warn/error log lines
    should be visually separable from progress chatter *while* the job runs,
    and a job that finishes should resolve into a **completion summary** the
    user dismisses themselves — what was installed, what was skipped and why,
    what failed, and what to do next — rather than a modal that disappears.
    This is the last screen of the app's single longest workflow, and right now
    it does not exist.

---

## 6. Design direction

Keep the current identity's *spirit* — a dark, forge-themed operator console with
an amber/orange accent — but raise it from "gradient-heavy dashboard template"
to something considered, quiet, and dense-but-calm. Think a well-made
infrastructure tool, not a gaming skin. Specifically:

- **Restrain the glassmorphism and the glow.** Depth should come from spacing,
  hierarchy, and one or two elevation levels — not from stacked radial
  gradients, colored shadows on every card, and translucency everywhere.
- **Amber is the action color, not the wallpaper.** Reserve saturation for
  things that are actionable or wrong.
- **A real type scale.** Right now nearly everything is 13–15px with ad-hoc
  inline overrides. Give headings, body, meta, and mono distinct, systematic
  roles.
- **A real spacing scale**, applied consistently, so a 250-row list has rhythm.
- **Semantic status colors** that survive colorblindness: never encode
  severity in hue alone — pair with icon, weight, or label.
- **Motion is functional and minimal**, and fully respects
  `prefers-reduced-motion`.
- **A light theme** as a genuine second theme (not an inverted dark), with a
  persisted toggle in the header. Dark remains the default.
- **Full accessibility pass**: roles and `aria-*` on tabs, dialogs, switches,
  progress bars and the toast region; visible focus rings; focus trap and
  restore in modals; Escape to close; every action reachable by keyboard.
- **Responsive from 360px to ultrawide.** Define what happens to the instance
  tab bar, the config split pane, and every wide table on a phone.

---

## 7. Deliverables

Produce a complete, self-contained design that I can port into the app:

1. **`style.css`** — the full replacement stylesheet. A documented token layer
   (color, type, space, radius, elevation, motion, z-index) for both themes,
   then every component in §3.7, then every screen's layout, then responsive
   rules, then the a11y layer (`:focus-visible`, reduced motion, high contrast).
   No inline styles should be needed by any template.

2. **`index.html`** — the new static shell: header, setup banner, the four view
   sections, the instance detail scaffold with all five tab panes, the toast
   region, and the modal root. Every `id` from §4.3 present, correct ARIA
   throughout.

3. **A markup reference for every JS-rendered fragment**, as ready-to-paste HTML
   with placeholder values, since `app.js` builds these as template literals.
   At minimum: instance card (running / stopped / unmanaged) · pack card · mod
   row (enabled / disabled / unidentified / client-flagged / dependency) · config
   list group + item · finding block (all four severities, with and without
   evidence and fix) · AI report card + AI action row · host stat grid · heap
   meter block · JVM flag row · property preset row · `server.properties` row
   (each of the four control types, plus guarded and unsaved states) · port card
   · pack tab card (**[import]** catalogue *and* imported variants) · job card
   (idle / active-with-cancel / done / error / cancelled) · **job completion
   summary** (success, success-with-warnings, success-with-failed-mods — see
   §5b.12) · release table row ·
   dependency preview row · preflight candidate row (remove / review / keep,
   **[import]** plus the `bundled` variant with no logo and no project link) ·
   **[import]** import call-to-action card · **[import]** dropzone (idle /
   hover / focus / dragging) · **[import]** upload progress block · **[import]**
   "already imported" archive row · toast (3 kinds) · confirm dialog (plain and
   `danger`, with and without the detail block) · `.modal-error` banner · every
   modal's body · every empty state · every loading state (skeletons where they
   replace spinners).

4. **A rename/addition table** — any DOM `id`, `data-*` hook, JS-read class, or
   ARIA relationship from §4.3 that you changed, plus any *new* hook you
   introduce that `app.js` will need to bind (for example: theme toggle,
   virtualised list container, editor gutter, Optimizer sub-nav, per-job "watch"
   button, dirty marker in the config file list). This table is how the overhaul
   gets wired back up without regressions; treat it as a required deliverable,
   not an appendix.

5. **Interaction notes** for behaviour the CSS can't carry on its own: list
   virtualisation threshold and how it coexists with the in-place row updates in
   §5.3, sub-navigation for the Optimizer tab, the job-progress phase model, the
   config editor's gutter/line-number approach, and how reopening a
   backgrounded job should work.

Show the work as rendered screens (all four views, all five tabs, the key
modals — **[import]** including the import modal in both its empty and
mid-upload states — plus mobile widths and both themes) so I can see it before
I wire it.

---

## 8. Acceptance criteria

- Every feature in §3 is still reachable, in one screen or fewer than today.
- Every endpoint in §4.1 still gets called with an unchanged payload.
- No new runtime network dependency; the app renders correctly with the machine
  offline.
- Still three files, no build step, no framework.
- Every hook in §4.3 exists or appears in the rename table.
- Every behaviour listed in §5 still works: confirm dialogs resolve, modal
  errors persist, mod rows patch in place, the config editor guards unsaved
  edits, jobs can be cancelled, and the ARIA/focus layer is intact.
- Keyboard-only and screen-reader users can install a modpack, toggle a mod,
  edit a config, and run diagnostics.
- A finished install tells the user what happened — mods skipped, mods that
  failed, and any warning that predicts the server not starting — without them
  having to have watched the log while it scrolled.
- **[import]** A user who has never published a pack can find the import route
  from either the Instances or the Browse view without being told it exists,
  drop a zip on it, and reach the same review screen a catalogue pack reaches.
  The dropzone is fully operable from the keyboard, and the upload reports real
  progress and can be cancelled mid-transfer.
- Usable at 360px wide.
