# BlessForge — UI design brief for Claude Design

> **Read this first.** This is a request for a *complete, from-scratch* UI
> design. BlessForge already exists and works, but its current interface is not
> a starting point and should not influence you. Do not try to preserve its
> navigation, its tab structure, its page splits, or its visual language.
> Nothing below describes "where things are today" — it describes **what the
> product does and what its user is trying to accomplish**. The information
> architecture is yours to invent.
>
> If you think three of the areas below belong on one screen, merge them. If
> you think one of them deserves to be the centre of the whole app, make it so.
> If the right answer is not a sidebar-and-tabs dashboard at all, don't build
> one.

---

## 1. What BlessForge is

A self-hosted web app for running **modded Minecraft servers**. It drives
[Crafty Controller](https://craftycontrol.com/) over its HTTP API and adds
everything Crafty doesn't do: installing CurseForge modpacks onto a server,
deciding which mods belong on a server versus a client, managing mods, editing
configs, tuning the JVM for a weak machine, diagnosing crashes, and watching
the console.

**It runs on a box in someone's house.** Typically a small home server on a
LAN, often behind a reverse proxy, frequently with modest hardware — the
reference machine has 8 threads and 11.6 GB of RAM shared with the Minecraft
servers themselves. It is not a SaaS product and has no multi-tenancy, no
billing, no onboarding funnel, and no marketing surface.

**One person uses it.** They run servers for a group of friends. They are
technical enough to know what a JVM heap is, but they are doing this for fun
after work and they do not want to read documentation. They are usually at a
desk, occasionally on a phone while something is going wrong.

**The emotional shape of the product matters.** Most sessions are one of:

- *"I want to play this modpack with my friends this weekend."* — hopeful,
  exploratory, wants it to be easy.
- *"It won't start and I don't know why."* — frustrated, needs answers not
  dashboards, is about to give up and reinstall everything.
- *"It's running badly."* — wants to understand and tune, will read detail if
  the detail is trustworthy.

A design that only serves the first case will fail the product.

---

## 2. The one principle that must survive

**Never assert without showing the evidence.**

This is the product's actual differentiator and it should be visible in the
design language, not just in the copy. Everywhere BlessForge makes a claim —
"this mod is client-only", "this jar crashed your server", "this mod targets
the wrong Minecraft version", "you should use a 4 GB heap" — it has a reason,
often several, and often of differing strength. The UI must be able to show:

- a **verdict** (with a confidence level: confirmed / likely / uncertain),
- the **reasons behind it**, as a list, because there are usually two or three,
- the **raw evidence** (a log line, a metadata field, an API statement),
- and **who said so** (the jar's own manifest, Modrinth's authors, a parsed
  crash report, or an AI assistant — these have very different authority).

Design a way to express "here is a claim, here is how sure we are, here is
why" that works at a glance *and* rewards reading. It appears dozens of times
across the app and is currently the weakest part of the interface.

---

## 3. Everything the product does

This is the full functional inventory. Treat it as raw material to organise,
not as a screen list.

### 3.1 Global state the user must always be able to see

- **Backend reachable?** The app is useless if Crafty is unreachable.
- **Crafty connection status** — reachable, plus how many servers it manages.
- **CurseForge API key status** — valid / missing / mangled (there is a
  specific, common failure where the key gets truncated by config plumbing and
  everything 403s with no explanation; the UI must be able to say that clearly
  and tell the user the fix).
- **Modrinth status** — enabled/disabled.
- **Storage status** — whether the app's data directory is writable. When it
  isn't, installs fail silently, so this needs to be loud.
- **AI assistant status** — ready (with which model, on which endpoint), off,
  unreachable, or *reachable but the model isn't installed* — which offers a
  one-click "pull the model" action that itself becomes a progress job.
- **Background work in progress** — a count/indicator, visible from anywhere.
- **Light and dark themes**, both first-class, user-toggled and remembered.

### 3.2 The server list

Every Minecraft server Crafty knows about. Per server:

- name; running or stopped; players online; CPU and memory use
- port
- the modpack installed (name + version), or "unmanaged" if BlessForge didn't
  install it
- Minecraft version; mod loader (Forge / NeoForge / Fabric / Quilt)
- auto-start flag, creation date, the jar it launches

Servers can be started, stopped and restarted directly from this list, not
only from inside a server's own view.

**Deleting a server belongs here and is currently missing.** The capability
exists in the backend (optionally deleting the files too), but there is no
interface for it, so people delete server directories by hand instead — which
leaves the controller holding records it can no longer open, permanently. Give
deletion a proper, clearly-worded home: it needs to distinguish "remove this
from the panel" from "remove this and its world from disk", and it must feel
appropriately serious about the second.

**Failure state that matters:** a server can exist in Crafty's records while
its files are gone from disk. Crafty then returns an error for it forever. The
UI must present that as an explained state with a route out of it (deleting
the orphaned record), not as a broken card or a crash.

**Failed installs leave debris.** When an install fails part-way, the created
server is deliberately left behind so it can be retried into — so over time
half-finished servers accumulate. The design should acknowledge them and offer
a tidy-up.

### 3.3 Finding and installing a modpack

- **Search the CurseForge modpack catalogue** — free-text query, filter by
  Minecraft version and category, sort, paginate ("load more" rather than page
  numbers). Modrinth is also available as a source.
- **Pack results** — icon, name, summary, download count, author, categories,
  last updated.
- **Pack detail** — the list of releases, each with: display name, Minecraft
  versions, loaders, release channel (release / beta / alpha), file size, date,
  and **whether an official server build exists** (this materially changes how
  the install works and the user should understand it).
- **Install setup** — server name, port, whether to prefer the official server
  build, whether to review client-only mods first, whether to auto-tune the JVM.
- **Import a pack you built yourself.** A modpack a user assembled in the
  CurseForge app is never published and cannot be searched for; the only handle
  on it is the `.zip` profile export. So: drag-and-drop upload (these run to
  hundreds of megabytes and need real byte-level upload progress), a summary of
  what the archive contains before committing (runtime, loader, mod count,
  bundled jars, override files, requested RAM, warnings), a list of previously
  imported archives that can be reused or deleted, and the ability to
  **re-import an updated export into an existing server, keeping the world**.

### 3.4 The client-only mod review (pre-install)

Distinctive and important. Before installing, the app works out which mods are
client-side and would crash or waste memory on a server, and presents them for
approval rather than acting silently.

Each candidate carries: icon, display name, jar filename, a verdict, a
confidence level, **a list of reasons**, and sometimes "required by these other
mods, so removing it would break them". Verdicts are roughly:

- **confirmed client-only** — the jar itself declares it, or the mod's author
  states it on Modrinth. Selected by default.
- **uncertain** — weaker signals (a known client-mod name, depends on a
  client-only library). Left for the user to judge.
- **protected** — looks client-only but another mod in the pack needs it.
  Never auto-selected.
- **bundled** — a jar shipped inside the archive rather than from a catalogue,
  so nothing can look it up.

Selected mods are **installed but disabled and tagged**, never deleted — so a
wrong call costs one click to undo. The design should make that reassurance
obvious, because users are nervous at this step.

Also shown here: pack name, runtime, total mods, the tuned heap size, and any
warnings (e.g. "this pack wants 8 GB and your machine can safely give 4").

### 3.5 Long-running work

Installs, scans and AI analyses take minutes and move hundreds of megabytes.
Every one of them is a background job with:

- a current step, a percentage, an elapsed clock, and a sense of **phase**
  (resolve → download → unpack → register → tune) because a bar alone can't
  convey that a 3-minute download is followed by four short steps
- a **live log** with severity levels, where repeated lines collapse with a
  count rather than flooding
- for AI jobs, a **live token stream** of the model writing its answer
- **cancel**, and **run in background** — which detaches the *view*, not the
  job
- a **completion summary** the user dismisses themselves: succeeded cleanly /
  succeeded with N warnings / failed / cancelled, plus stats, the warnings
  (some of which predict a failed first boot and must not be missed), and any
  mods that failed to install with a way to add them manually
- the ability to **re-attach** to a backgrounded job later and replay
  everything buffered

There is also an activity area listing all recent jobs — each showing which
server it acted on, with a way to jump to that server, plus watch and cancel.

### 3.6 A single server: what you can do to it

**Power** — start, stop, restart, with live status.

**Mods.** Often 300+ of them, so the list must stay fast and scannable.
Per mod: icon, display name, version, jar filename, size, enable/disable
toggle, delete, and a way to change version or identify it. Badges for:
client-side (with the evidence available), possibly-client-side, installed as
a dependency of something, "pulled in N dependencies", unidentified.
Filtering by text and by state (all / enabled / disabled / unidentified /
client-side). Multi-select with bulk enable, disable and delete. A running
count of total / enabled / disabled.

Plus:
- **Add a mod** — searching CurseForge and Modrinth *live as you type*, with
  results showing whether it's already installed, whether it's client-side
  only, download counts and summaries. Installing takes its dependencies with
  it automatically; picking a specific version is one click away.
- **Identify unknown mods** — hashes jars and matches them against CurseForge
  and Modrinth to recover names, versions and icons for jars that arrived with
  a pack.
- **Check for updates** — lists mods with newer builds, current versus latest,
  with per-mod update actions.
- **Dependency view** — which mods pulled in which, plus mods that arrived as
  someone's dependency, and a count of standalone mods.

**Config files.** Browse and filter the server's config files; edit them in a
plain text editor with line numbers, a dirty indicator, save, and a guard
against navigating away with unsaved changes.

**Diagnostics and repair.** The heart of the "it won't start" case:
- **Health checks** — a findings list, severity-ranked, each with a title,
  an explanation, raw evidence, and often a **one-click fix**. Fixes include:
  accept the EULA (in the exact byte-format the controller demands), pin the
  Java version, raise the heap, disable the offending mods, find and install a
  missing dependency, swap mods to compatible versions, retry a mod that
  failed to install, open the relevant config file, or run identification.
- **Deep dependency scan** — downloads every jar and builds the real dependency
  graph: duplicates, missing dependencies, jars that declare themselves
  client-side, jars with unreadable metadata.
- **Crash review** — reads an entire crash report and **names the jars it
  blames**, ranked by how directly the log implicates each one, every one
  carrying the log line that implicates it, with a disable action per culprit.
  It also handles the case where the mod named in the crash is *not* the cause
  (e.g. the machine is running the wrong Java version) and says so.
- **AI assistant** — explains what is most likely wrong and proposes a plan.
  Actions are classified safe or destructive; destructive ones need explicit
  confirmation; anything the model invents is rejected and shown as rejected.
  There is also a **"review and fix it"** mode that applies only the reversible
  actions on its own and hands back everything riskier for approval.
- **Raw logs** — the tail of the server log and the newest crash report.

**Performance tuning.** Sizeable and detailed:
- **Host facts** — total RAM, available RAM, CPU count and model, free disk,
  where the numbers were measured, and a note for when the controller runs on
  a different machine.
- **Heap** — recommended size, what the pack asked for, the ceiling this
  machine can safely give, the reserve held back, the reasoning in plain
  language, and warnings when the pack wants more than exists.
- **JVM flags** — a few dozen, grouped (garbage collection, memory, I/O,
  network, security, metadata), individually toggleable, each explained.
- **server.properties** — the complete file, ~60 keys, grouped (Network,
  Performance, Players, World, Misc, Other), each typed (boolean, integer,
  string, or a fixed set of choices), each with its default, a description, an
  indicator when it differs from default, and a few keys locked because they
  must be changed elsewhere.
- **Port management** — the port as the controller records it, as
  `server.properties` says, and the query port; a warning when they disagree;
  which other servers already use that port; and a warning when the port falls
  outside the range the controller actually publishes.
- Changes apply on next restart, and the user should never be confused about
  that.

**Terminal.** A live server console: streamed output, severity colouring,
a filter, clear, an auto-following view that pauses when the user scrolls up
to read and resumes at the bottom, an indicator of whether output is coming
from the live console or the log file, connection state, and a command input
that sends straight to the server (disabled, with an explanation, when the
server is stopped).

**Modpack management.** What pack is installed, switching to a different
release of it, or re-importing an updated private export — with the world
preserved and a clear warning that mods and configs are replaced.

### 3.7 Cross-cutting behaviours

- **Confirmation dialogs** with a title, a plain-language consequence, optional
  detail (e.g. the list of files about to be deleted), and a distinct
  destructive variant.
- **Toasts** for success, failure and neutral notices.
- **Loading states** for everything, since almost every read crosses a network.
- **Empty states** that explain what would fill them and how.
- **Error states** that name the cause and, where possible, the fix.

---

## 4. Hard constraints

These are real; a design that ignores them can't ship.

1. **Two themes, both first-class.** Dark is the default. Light must be
   genuinely designed, not an inversion.
2. **Desktop-first, but must work at 360 px.** People check on a failing
   server from their phone.
3. **Very long lists.** 300+ mods is normal, so the mod list is virtualised:
   rows need a predictable height and must not depend on measuring content.
4. **Live-updating regions must not shift layout.** Console output, job
   progress and streaming AI text all update continuously; nothing around them
   may jump.
5. **Severity is never carried by colour alone** — it needs a shape, a glyph or
   a label too. Same for status.
6. **Keyboard operable throughout**, with proper roles for tabs, progress bars,
   log regions and dialogs.
7. **Self-contained assets.** The machine may have no internet access. Any
   fonts or images the design relies on must be bundleable locally. Mod and
   pack icons *are* loaded from remote URLs and often fail — every icon needs a
   designed fallback.
8. **No fixed brand palette to obey.** You choose it. See §6.

---

## 5. Materials that will be provided

Some assets will be supplied alongside this brief, and the design should
define clear slots for them rather than inventing final artwork:

- **the BlessForge logo / app mark**, and a wordmark
- **a tab/favicon icon**
- **a small number of additional graphic elements** (to be provided) — treat
  these as decorative or illustrative accents and show where they'd sit

Please also specify the icon slots you need, so they can be produced to match:

- mod loader marks (Forge, NeoForge, Fabric, Quilt)
- source marks (CurseForge, Modrinth)
- fallback tiles for mods and packs whose remote icons fail to load
- empty-state and error-state illustrations, if your design uses them

Where a provided asset isn't available yet, use an obvious placeholder and
label it.

---

## 6. Creative latitude — please use it

**You are choosing the design, not decorating a spec.**

- **The visual language is yours.** Type, colour, density, depth, motion,
  shape. It should feel like a considered piece of software with a point of
  view. It should not look like a generic admin template, and it does not have
  to look like a "gamer" product either. The current app is a warm, dark,
  stone-coloured thing — feel free to go somewhere completely different.
- **The information architecture is yours.** The thirteen areas in §3 are
  capabilities, not screens. Group, split, nest or surface them however serves
  the three user situations in §1.
- **Navigation is yours.** Nothing requires a top nav, a sidebar, tabs, or a
  detail view per server.
- **You may invent surfaces this app doesn't have** — a first-run setup flow, a
  problem-focused overview, a command palette, an onboarding path for the first
  server, an at-a-glance health view across all servers. If something obvious
  is missing, add it and say why.
- **Density is a real decision.** This is a tool used by one expert person, not
  a consumer app. Consider whether it should be denser and more information-rich
  than a typical dashboard.

---

## 7. What to deliver

1. **Full screen designs** for every area of the product, in **both themes**.
2. **Key flows end to end** — at minimum: installing a modpack from search to a
   running server (including the review step and the progress experience), and
   diagnosing a server that won't start.
3. **A component inventory** — buttons, inputs, toggles, badges/pills, cards,
   list rows, tables, modals, toasts, progress, log/console, editors, empty
   states, skeletons.
4. **All the states**: loading, empty, error, partial, live-updating,
   disabled, destructive.
5. **Responsive behaviour** at desktop, tablet and 360 px.
6. **A design token system** — named colour, type, spacing, radius and
   elevation scales for both themes.
7. **Motion notes** — what animates, how much, and what must stay still.
8. **A short rationale** for the information architecture you chose, and
   anything you deliberately rejected.

The implementation will be adapted to fit whatever you design — so optimise
for the best interface, not for what would be easy to retrofit.
