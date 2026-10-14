# What Crafty does that BlessForge should

A read of Crafty 4 (`gitlab.com/crafty-controller/crafty-4`, master at `fe65b37`)
against BlessForge `main`, on how each manages servers.

**License:** Crafty is **GPL-3.0** and BlessForge is MIT. Borrow ideas only, never
code: anything copied would put BlessForge under the GPL. Everything below is
written to be reimplemented from the behaviour, not ported.

---

## Where BlessForge is already ahead

Don't trade these away while adding the rest:

- **Isolation.** Each server is its own locked-down container (memory, CPU and PID
  limits, no capabilities, read-only root). Crafty runs every server as a
  plain process inside one container, so one server can starve or reach the
  others.
- **Modpacks.** BlessForge does CurseForge and Modrinth installs, removes client
  mods by evidence (and now by the authors' own declared sides), and runs
  boot-and-repair until the pack comes up. Crafty installs a jar and leaves the
  rest to you.
- **Memory.** A heap that the container is sized to, OOM detection, and
  growth that stays inside free memory.
- **Fleet behaviour.** A boot queue, servers re-adopted when the agent
  restarts, automatic ports, servers sharing a port with a "switch", and
  committed-memory accounting.
- **Players.** Online players come from log lines and server pings, with join
  times.

---

## Gaps, most valuable first

### 1. World backups (BlessForge has none)

The agent's backup endpoints return 501 (`agent/main.py`, "arrive with WP-11").
The `backups` table exists in `app/db/models.py` but nothing uses it.
`app/backups.py` is only undo for mod-set changes; it never copies a world.

What Crafty does (`app/classes/shared/backup_mgr.py`, `server.py: backup_server`):

- **Several backup configs per server**, each with a location (another disk
  is fine), **excluded folders**, **compression on/off**, and **keep the last N**
  (`max_backups`).
- **Two kinds:**
  - **Zip:** one archive per run.
  - **Snapshot:** content-addressed. Every file is stored once by hash, plus a
    small manifest per backup, so ten backups of an 8 GB world cost
    little more than one.
- **Consistency hooks:** an optional **shutdown during the backup**
  (stop, back up, start again if it was running), or **before/after console
  commands** (`save-off` / `save-all flush` … `save-on`) for a live backup.
- **A default backup taken automatically before an update.** The update is
  cancelled if the backup fails.
- **Restore**, either in place or as a copy, plus a download link.

For BlessForge:
- The agent does the work, as everything else on the node does.
- **Snapshot style first.** Modded worlds are large and mostly unchanged from
  one backup to the next. Deduplicate with SHA-256 by file (or by chunk for
  region files) into a store on the node, keeping a manifest per backup.
  restic/borg are options, but they bring a binary along.
- Use `save-off` → `save-all flush` → copy → `save-on` through the console the
  agent already holds, so a running server never has to stop.
- **Wire it into BlessForge's own risky actions:** pack version switches,
  repairs, Roulette and imports take a backup first. The rename-based undo in
  `app/backups.py` stays for mod-set changes.
- Backups count toward the server's disk limit, or a separate one, so the
  disk watchdog includes them.

### 2. Scheduled tasks

BlessForge only schedules the update-check sweep (`app/watcher.py`). The
`schedules` table (cron, action, payload, next_run) exists but is unused.

What Crafty does (`app/classes/shared/tasks.py`, model `Schedules`):

- **Actions:** command, start, stop, restart, backup.
- **Timing:** an interval (minutes, hours, days), a **cron string**, or **one-time**.
- **Chained tasks:** a child task runs a set delay after its parent. That's how
  "say Restarting in 5 minutes" → 5 min → "say 1 minute" → 1 min → restart is
  built.
- **`next_run` shown**, "run now", and each run written to the audit log.

For BlessForge:
- The worker (arq) already runs a cron for the sweep. Add one per-node
  scheduler loop that reads due `schedules` rows and calls the existing power,
  command and backup paths.
- Ship **presets**, not just a cron box. BlessForge's own perf notes already
  recommend "a daily restart keeps the heap from creeping up", so offer
  *Daily restart at 05:00 with 5-minute and 1-minute warnings* as one click.
  Also *backup every 6 h, keep 8* and *announce a message*.
- Chaining as Crafty does it (parent → delay → child), shown as one timeline
  in the UI rather than separate rows.

### 3. Notifications (webhooks)

Crafty sends events to **Discord, Slack, Teams, Mattermost** or a custom URL
(`app/classes/web/webhooks/*`). A decorator on the server methods fires on
start, stop, crash, kill and backup, with the result attached (for example,
backup size and a download link).

BlessForge already has every event this needs: the agent's events (`status`,
`crash`, `crash_loop`, `disk_full`, `port_changed`, install phases) reach
`/api/agent/events`. What's missing is a "Notifications" settings section that
maps events to Discord or Slack URLs. That's an afternoon of work for a large
gain for a self-hoster, and especially valuable for crash loops and full disks.

### 4. Crash and stop settings per server

What Crafty does that BlessForge doesn't:

- **`ignored_exits`:** exit codes treated as a clean stop, not a crash. For
  example, a pack whose restart script exits 1, or a mod that calls
  `System.exit` on `/restart`. BlessForge counts every unrequested exit as a
  crash.
- **`stop_command`:** a per-server stop command. `stop` is wrong for proxies
  (`end` on BungeeCord, `shutdown` on Velocity).
- **`shutdown_timeout` per server:** BlessForge has one `stop_timeout` (60 s)
  for the whole node, which is too short for a 450-mod pack saving a big
  world. If it runs over, the server gets killed mid-save.
- **`crash_detection` toggle:** restart-after-crash, separate from auto-start
  on boot. In BlessForge both hang off `auto_start`.

All four are small changes to the agent spec plus the Tune tab.

### 5. Log cleanup

Crafty's `logs_delete_after` deletes old logs after N days. A modded server's
`logs/` and `crash-reports/` grow without limit. A per-server "keep logs for N
days" setting, enforced by the agent's disk watchdog pass, is cheap.

### 6. History, not just live numbers

Crafty records a `ServerStats` row on a timer: players online, CPU, memory,
world size, version, MOTD. That drives history graphs and "player activity".
BlessForge's stats are live only (the agent's sampler). Store one row a minute
per running server, thinned to 15 min after a day, with 30 days kept. That's
enough for "players over the week", "memory creep since the last restart", and
evidence for the daily-restart preset.

### 7. Access control

Crafty has:
- **TOTP and passkey 2FA** for logins.
- **API keys** per user with a chosen subset of permissions, for scripts,
  Discord bots and Home Assistant.
- **Per-server permissions** for roles (terminal, commands, logs, schedules,
  backups, files, config, players).

BlessForge has global roles (admin, operator, member, guest) and route
policies (`app/policy.py`); 2FA is planned under WP-07. For a box shared with
friends, the two worth adding first are **TOTP** and **scoped API tokens**.
Per-server grants matter once friends run their own servers.

### 8. Smaller items

- **Public status page** (Crafty's `show_status`): a read-only page, optionally
  public, with the servers that opt in, their player counts and versions.
- **Server-jar updates with a backup first** (Crafty's "update executable"):
  for Paper and Purpur, follow new builds on the chosen MC version, update
  after a backup, and restart if it was running. BlessForge has this for mods
  (the sweep), not for the server jar.
- **Pushing fleet changes:** Crafty pushes status changes over a websocket.
  BlessForge's fleet page still polls. The agent events stream already exists,
  so relaying it to the browser over the panel's console proxy channel would
  make start, stop and crash show up instantly.

---

## Suggested order

1. Notifications (3) and per-server crash/stop settings (4). Small, and they
   make every incident visible.
2. Scheduled tasks with presets (2), which daily restarts need.
3. World backups (1), the biggest piece. Snapshot style, wired into
   BlessForge's risky actions.
4. Stats history (6) and log cleanup (5).
5. TOTP and API tokens (7), then the public status page and server-jar
   updates (8).
