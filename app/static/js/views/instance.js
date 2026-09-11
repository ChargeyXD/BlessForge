/* ============================================================
   Instance — one server, and everything you can do to it.

   The tab set is not fixed: a Paper server gets Plugins where a
   Forge server gets Mods, because they are different folders
   with different catalogues behind them and pretending otherwise
   is how a plugin ends up in mods/ doing nothing.

   Live numbers come from /stats on a short poll while the tab is
   visible; everything else is read once per tab and refreshed on
   demand, so an idle browser is not a load on Crafty.

   The three live readings — CPU, memory, players — are not a
   corner of the header any more. They are a band of their own
   between the header and the tab strip, painted ONCE and then
   written into in place. See "THE VITALS RAIL" below; the
   distinction is load-bearing rather than tidiness.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, num,
  duration, empty, loadingFox, confirmDialog, prefs, ago,
  pageSkeleton, heldFor, dissolve,
} from '../core.js';
import { run } from '../jobs.js';
import { state, go, topActions, refreshInstances } from '../app.js';
import { action, retryLoader } from './fleet.js';

const TABS = [
  { key: 'overview', label: 'Overview', icon: 'server' },
  { key: 'mods', label: 'Mods', icon: 'box', when: (i) => !i.plugins },
  { key: 'plugins', label: 'Plugins', icon: 'puzzle', when: (i) => i.plugins },
  { key: 'files', label: 'Files', icon: 'folder' },
  { key: 'players', label: 'Players', icon: 'users' },
  { key: 'console', label: 'Console', icon: 'terminal' },
  { key: 'configs', label: 'Configs', icon: 'fileText' },
  { key: 'tune', label: 'Tune', icon: 'sliders' },
  { key: 'diagnose', label: 'Diagnose', icon: 'wrench' },
  { key: 'backups', label: 'Undo', icon: 'refresh' },
];

const LOADERS = {
  fabric: '/assets/loader-fabric.png',
  forge: '/assets/loader-forge.jpg',
  neoforge: '/assets/loader-neoforge.png',
  vanilla: '/assets/loader-vanilla.svg',
  paper: '/assets/loader-paper.png',
  purpur: '/assets/loader-purpur.png',
  folia: '/assets/loader-paper.png',
};
const PLUGIN_FAMILIES = new Set(['paper', 'purpur', 'folia', 'spigot', 'bukkit']);

/* ============================================================
   THE VITALS RAIL
   ------------------------------------------------------------
   Three readings, three different objects, one frame.

     CPU      a torii standing in the tide. The water climbs the
              pillars with load and the crest line IS the
              reading; at full it reaches the kasagi.
     MEMORY   a chochin. Unlit while the server is down, filling
              with light as memory fills, glowing harder the
              fuller it gets.
     PLAYERS  blossoms. A count out of a max is not a percentage,
              so it gets no level at all: one mark per seat, open
              where a player is here and a closed bud where none
              is.

   Four things this had to get right, none of them decoration.

   1. HONESTY. A stopped server's /stats answers cpu 0, mem 0,
      mem_percent 0. Those are not readings, they are the shape
      of the payload — and a gauge resting at 0% is a lie that
      looks exactly like data. So an absent number stays `null`
      the whole way through (see `reading`), "not running" and
      "no reading" are different states with different art, and
      only a running server with a real figure gets a level.

   2. THE POLL MUST NOT RESTART THE MOTION. `paintHead` rebuilds
      the header every six seconds, and anything rebuilt loses
      its running CSS transitions — a gauge inside it would snap
      to each new value or replay from empty on every tick. So
      the rail lives in its OWN host, is built once, and the poll
      only writes values into it. That is the whole reason it is
      not a third column of the header.

   3. A STALLED TIMELINE MUST NOT LEAVE A WRONG NUMBER. Levels
      move on CSS transitions, which need no frames from us. The
      numerals roll on rAF, which does not fire in a background
      tab — so `roll` writes the FINAL value synchronously first
      and only then walks back and animates towards it, and a
      setTimeout (which does fire when throttled) collapses any
      half-finished roll onto the real figure. The worst case is
      a number that is right and did not move.

   4. IT CARRIES NO WIGGLING POLYGON, deliberately. Nothing here
      is a .card or a .btn, so it declares no layer that could
      argue with the three-layer contract those have, and it
      introduces no rule that could outrank .p5-hl.
   ============================================================ */

const REDUCED = window.matchMedia('(prefers-reduced-motion:reduce)');
const motionOn = () =>
  document.documentElement.classList.contains('js-motion') && !REDUCED.matches;

/* The one that matters. `Number(null)` is 0 and `Number('')` is 0, so the
   obvious cast turns "Crafty did not say" into "zero" — the exact lie this
   rail exists to stop telling. Absent stays absent. */
function reading(x) {
  if (x === null || x === undefined || x === '') return null;
  const v = Number(x);
  return Number.isFinite(v) ? v : null;
}

function toneFor(v, warn, hot) {
  if (v === null) return 'unknown';
  if (v >= hot) return 'hot';
  if (v >= warn) return 'warm';
  return 'calm';
}

const clamp01 = (v) => Math.max(0, Math.min(100, v));

function offReason(st, crashed) {
  if (crashed) return 'crashed';
  if (st === 'incomplete') return 'setup unfinished';
  if (st === 'orphan') return 'no server directory';
  return 'not running';
}

/* A figure that moves to its new value instead of cutting to it.

   The order is the safety argument. The true value is written FIRST, so a
   browser that never hands us a frame still shows the right number; the
   roll then steps back to the previous value and catches up. The timer is
   the second belt — setTimeout survives background throttling where rAF
   does not, so a tab hidden mid-roll cannot be left showing 2.1 GB of a
   3.4 GB reading. */
function roll(el, fmt) {
  let cur = null;
  let raf = 0;
  let timer = 0;
  const stop = () => {
    if (raf) cancelAnimationFrame(raf);
    clearTimeout(timer);
    raf = 0;
    timer = 0;
  };
  return {
    stop,
    set(v, format) {
      stop();
      const f = format || fmt;
      el.textContent = f(v);
      const from = cur;
      cur = v;
      if (v === null || from === null || from === v || !motionOn()) return;
      const ms = 620;
      const t0 = performance.now();
      const step = (now) => {
        const k = Math.min(1, (now - t0) / ms);
        el.textContent = f(from + (v - from) * (1 - (1 - k) ** 3));
        raf = k < 1 ? requestAnimationFrame(step) : 0;
      };
      raf = requestAnimationFrame(step);
      timer = setTimeout(() => { stop(); el.textContent = f(v); }, ms + 160);
    },
  };
}

/* Seats are drawn one per slot only while there are few enough to count by
   eye. Past this a 200-slot server becomes a grey field that says nothing,
   so the marks switch to one per PLAYER PRESENT and the max is carried by
   the text instead. Neither mode ever draws a mark standing for a fraction
   of a person. */
const SEAT_CAP = 40;

const intFmt = (v) => (v === null ? '—' : String(Math.round(v)));
const byteFmt = (v) => (v === null ? '—' : bytes(Math.max(0, Math.round(v))));

function vitalsRail() {
  const timers = new Set();
  const after = (ms, fn) => {
    const t = setTimeout(() => { timers.delete(t); fn(); }, ms);
    timers.add(t);
  };

  /* --- the frame all three share ---------------------------- */
  function shell(key, label, art, below) {
    const val = h('span.vt-val', '—');
    const unit = h('span.vt-unit', { hidden: true });
    const sub = h('div.vt-sub');
    const node = h(`div.vital.vital-${key}`, { dataset: { tone: 'unknown' } },
      h('i.vt-edge', { 'aria-hidden': 'true' }),
      h('div.vt-row',
        art,
        h('div.vt-read',
          h('div.vt-lbl', h('i.vt-dot', { 'aria-hidden': 'true' }),
            h('span', label)),
          h('div.vt-num', val, unit),
          sub)),
      below);
    return {
      node,
      val,
      tone(t) { node.dataset.tone = t; },
      say(unitText, subText, title) {
        unit.textContent = unitText || '';
        unit.hidden = !unitText;
        sub.textContent = subText;
        node.title = title;
      },
    };
  }

  /* --- CPU: a torii standing in the tide --------------------
     .vt-tide spans exactly the water's range — ground line to
     the underside of the kasagi — so translating it by its own
     height maps 0..100 onto the gate with no hard-coded pixel
     anywhere. The gate paints OVER the water, so the silhouette
     stays crisp at every level. */
  const tide = h('i.vt-tide');
  const cpuArt = h('div.vt-art.vt-torii', { 'aria-hidden': 'true' },
    tide,
    h('i.vt-mark'),
    h('i.vt-ground'),
    h('div.vt-gate',
      h('i.kasagi'), h('i.shimaki'), h('i.gakuzuka'), h('i.nuki'),
      h('i.post.l'), h('i.post.r')),
    h('i.vt-nod'));
  const cpu = shell('cpu', 'CPU', cpuArt, null);
  const cpuRoll = roll(cpu.val, intFmt);

  /* --- Memory: a chochin that fills with light --------------
     .body clips with overflow + its own border-radius, so the
     light is cut to the paper barrel without a mask or a
     clip-path id to collide with anything else on the page. */
  const lit = h('i.lit');
  const glow = h('i.glow');
  const memArt = h('div.vt-art.vt-chochin', { 'aria-hidden': 'true' },
    glow,
    h('i.cord'),
    h('i.cap.top'),
    h('div.body', lit, h('i.ribs')),
    h('i.cap.bot'),
    h('i.tassel'),
    h('i.vt-nod'));
  const mem = shell('mem', 'Memory', memArt, null);
  const memRoll = roll(mem.val, intFmt);

  /* --- Players: one blossom per seat ------------------------ */
  const field = h('div.vt-petals', { 'aria-hidden': 'true' });
  const more = h('span.vt-more', { hidden: true });
  const players = shell('players', 'Players', null,
    h('div.vt-seats', field, more));
  const playersRoll = roll(players.val, intFmt);

  /* Marks are reused, never rebuilt. Rebuilding them would replay the
     opening on every poll — twenty blossoms popping every six seconds for
     no reason at all. Only a seat that actually changed hands moves. */
  function seats(slots, filled) {
    while (field.childElementCount > slots) field.lastElementChild.remove();
    while (field.childElementCount < slots) field.append(h('i.vt-petal'));
    let i = 0;
    for (const petal of field.children) {
      const on = i < filled;
      if (on && !petal.classList.contains('on')) {
        petal.classList.add('on', 'just');
        /* `.just` comes off on a timer rather than on animationend: a
           throttled tab never fires animationend, and a petal abandoned
           mid-pop would sit at 40% scale for good. setTimeout still
           fires, so the mark is always returned to its real size. */
        after(520, () => petal.classList.remove('just'));
      } else if (!on) {
        petal.classList.remove('on', 'just');
      }
      i += 1;
    }
  }

  const node = h('div.vitals.anim-rise', {
    role: 'group', 'aria-label': 'Live vital signs',
  }, cpu.node, mem.node, players.node);

  let first = true;

  function update(stats, st) {
    const s = stats || {};
    const up = !!s.running;
    const crashed = !!s.crashed;
    const down = offReason(st, crashed);
    /* The opening reading arrives; it does not fill up to itself. Animating
       from empty to 46% on load animates a change that never happened, and
       on a stalled timeline that opening transition would never run — every
       level stuck at zero beside a numeral reading 46%. Suppressed for this
       one pass, so the first state is right under any timeline and only
       real changes move afterwards. */
    if (first) node.classList.add('vt-still');

    /* ---- CPU ------------------------------------------------ */
    const c = up ? reading(s.cpu) : null;
    cpu.tone(up ? toneFor(c, 60, 85) : crashed ? 'hot' : 'off');
    cpuRoll.set(c);
    cpu.say(c === null ? '' : '%',
      c === null ? (up ? 'no reading' : down)
        : c >= 85 ? 'straining' : c >= 60 ? 'busy' : 'steady',
      c === null
        ? `CPU — ${up ? 'Crafty reported no CPU figure' : down}`
        : `CPU ${c.toFixed(1)}%, as Crafty reports it. The tide is level `
          + 'with the reading; the dashed mark on the pillars is 85%.');
    /* `is-blank` is the hatch that means "running, but Crafty sent no
       figure" — NOT the same thing as a stopped server, which keeps its
       art and is paled by data-tone="off" instead. Two different silences
       that must not look alike. */
    cpuArt.classList.toggle('is-blank', up && c === null);
    /* Level on a transform transition: no frame of ours is needed, so it
       still arrives correctly on a throttled tab. */
    tide.style.transform = `translateY(${100 - clamp01(c ?? 0)}%)`;

    /* ---- Memory --------------------------------------------- */
    const pct = up ? reading(s.mem_percent) : null;
    const raw = up ? reading(s.mem) : null;
    /* Bytes are the concrete figure and the percentage is the pressure, so
       bytes take the big numeral whenever Crafty sent them and the share
       goes underneath. With only a percentage the percentage is promoted,
       rather than a byte count being invented for the sake of symmetry. */
    const useBytes = raw !== null && raw > 0;
    const known = useBytes || pct !== null;
    mem.tone(up ? toneFor(pct, 70, 88) : crashed ? 'hot' : 'off');
    memRoll.set(useBytes ? raw : pct, useBytes ? byteFmt : intFmt);
    mem.say(useBytes || !known ? '' : '%',
      !up ? down
        : !known ? 'no reading'
          : pct === null ? 'share unknown'
            : useBytes ? `${Math.round(pct)}% of host`
              : 'of the host',
      !up ? `Memory — ${down}`
        : !known ? 'Memory — Crafty reported no memory figure'
          : `${useBytes ? bytes(raw) : '?'} resident`
            + `${pct === null ? '' : `, ${pct.toFixed(1)}% of the machine`}`
            + '. The lantern fills with that share.');
    memArt.classList.toggle('is-blank', up && !known);
    /* With bytes but no share there is no level to draw, so the lantern
       shows a low ember rather than pretending to a percentage. */
    lit.style.transform =
      `translateY(${100 - clamp01(pct ?? (up && useBytes ? 8 : 0))}%)`;
    glow.style.opacity = up && pct !== null
      ? (0.16 + 0.5 * (clamp01(pct) / 100)).toFixed(3) : '0';

    /* ---- Players -------------------------------------------- */
    const online = reading(s.online);
    const max = reading(s.max);
    const seatMode = max !== null && max > 0 && max <= SEAT_CAP;
    let slots = 0;
    let filled = 0;
    if (online !== null) {
      if (seatMode) {
        slots = max;
        filled = Math.min(online, max);
      } else {
        filled = Math.min(online, SEAT_CAP);
        slots = Math.max(1, filled);
      }
    }
    seats(slots, filled);
    const spare = online !== null && max !== null
      ? Math.max(0, max - online) : null;
    const overflow = online !== null && !seatMode && online > SEAT_CAP;
    more.hidden = !overflow;
    more.textContent = overflow ? `+${online - SEAT_CAP}` : '';
    field.dataset.mode = online === null ? 'none' : seatMode ? 'seats' : 'heads';
    players.tone(
      online === null ? 'unknown'
        : !up ? 'off'
          : max === null || max <= 0 ? 'calm'
            : online >= max ? 'hot'
              : online / max >= 0.75 ? 'warm' : 'calm');
    playersRoll.set(online);
    players.say(max === null || max <= 0 ? '' : `/ ${max}`,
      online === null ? 'no reading'
        : !up ? 'offline'
          : max === null || max <= 0 ? 'no slot limit'
            : spare === 0 ? 'full'
              : online === 0 ? 'nobody online'
                : `${spare} seat${spare === 1 ? '' : 's'} free`,
      online === null ? 'Players — Crafty reported no player count'
        : `${!up ? `Nobody can join while the server is ${down}. ` : ''}${
          seatMode
            ? `One blossom for each of the ${max} slots; an open blossom is `
              + `a player who is here, and ${filled} of them are.`
            : `One blossom for each player online${
              max === null ? '' : `, of ${max} slots`}.`}`);

    if (first) {
      /* Forcing layout here is the point: it commits everything written
         above while transitions are still off, so removing the class cannot
         animate any of it. A reflow, not a frame — nothing here waits on
         requestAnimationFrame. */
      void node.offsetHeight;
      node.classList.remove('vt-still');
      first = false;
    }
  }

  return {
    node,
    update,
    stop() {
      cpuRoll.stop();
      memRoll.stop();
      playersRoll.stop();
      for (const t of timers) clearTimeout(t);
      timers.clear();
    },
  };
}

export async function render({ params }) {
  const id = params.id;
  const node = h('div.wrap.wide');
  let detail = null;
  let tabView = null;
  let statsTimer = null;
  let disposed = false;

  const headHost = h('div');
  /* Its own host, and never cleared once filled. That is what lets the
     levels transition across a poll instead of snapping — see the note on
     the vitals rail above. */
  const vitalsHost = h('div');
  const tabsHost = h('div.tabs', { role: 'tablist', 'aria-label': 'Instance sections' });
  const bodyHost = h('div.tabbody');
  mount(node, headHost, vitalsHost, tabsHost, bodyHost);
  mount(bodyHost, pageSkeleton({ kpis: 4, panels: 2 }));

  try {
    detail = await api.get(`/api/instances/${id}`);
  } catch (e) {
    mount(node, empty('chargey-failed.png', 'Crafty will not talk about this server',
      e.message + ' — if the server directory was removed, Crafty keeps its '
      + 'record and fails every request about it until that record is deleted.',
      h('a.btn.primary', { href: '#/' }, hl(), h('span', 'Back to the fleet')),
      h('button.btn.danger', { onclick: () => deleteInstance(id, 'this server') },
        hl(), icon('trash', 14), h('span', 'Delete the record'))));
    return { node };
  }

  const ctx = {
    id,
    detail,
    server: detail.server || {},
    manifest: detail.manifest || {},
    stats: detail.stats || {},
    get name() { return this.server.server_name || id.slice(0, 8); },
    get loader() { return (this.manifest.loader || inferLoader(this.server)) || ''; },
    get minecraft() { return this.manifest.minecraft || detail.java?.minecraft || ''; },
    get plugins() { return PLUGIN_FAMILIES.has((this.loader || '').toLowerCase()); },
    get modDir() { return this.plugins ? 'plugins' : 'mods'; },
    get running() { return !!this.stats.running; },
    reload: async () => {
      ctx.detail = await api.get(`/api/instances/${id}`);
      ctx.server = ctx.detail.server || {};
      ctx.manifest = ctx.detail.manifest || {};
      ctx.stats = ctx.detail.stats || {};
      paintHead();
      paintTabs();
    },
    go: (tab) => go(`/i/${id}/${tab}`),
  };

  const rail = vitalsRail();
  mount(vitalsHost, rail.node);

  function paintHead() {
    const art = LOADERS[(ctx.loader || '').toLowerCase()];
    const st = ctx.detail.state || 'stopped';
    const tone = { running: 'ok', stopped: 'dead', crashed: 'bad',
      orphan: 'plum', incomplete: 'warn' }[st] || 'dead';

    mount(headHost, h('div.inst-head',
      h('div.shimenawa', { 'aria-hidden': 'true',
        style: { opacity: st === 'running' ? '.85' : '.35' } }),
      h('div', { style: { paddingTop: '10px', display: 'flex', gap: '14px',
        alignItems: 'flex-start', flex: 1, flexWrap: 'wrap' } },
        h('div.loader-art', { style: { width: '46px', height: '46px' } },
          art ? h('img', { src: art, alt: '' })
            : icon(ctx.plugins ? 'puzzle' : 'box', 20)),
        h('div.idn',
          h('h1', ctx.name),
          h('div.chiprow',
            pill(st, tone),
            ctx.loader && pill(ctx.loader, 'ghost'),
            ctx.minecraft && pill(ctx.minecraft, 'ghost'),
            ctx.server.server_port && pill(`:${ctx.server.server_port}`, 'ghost'),
            ctx.detail.java?.major
              ? pill(`Java ${ctx.detail.java.major}`,
                ctx.detail.java.ok === false ? 'bad'
                  : ctx.detail.java.ok === true ? 'ok' : 'warn',
                ctx.detail.java.ok === false
                  ? `Minecraft ${ctx.minecraft} needs Java `
                    + `${ctx.detail.java.required}`
                  : null)
              : null,
            ctx.running && ctx.detail.uptime_s
              ? pill(`up ${duration(ctx.detail.uptime_s)}`, 'info') : null,
          ),
          ctx.manifest.pack?.name
            ? h('div.mono.muted', { style: { marginTop: '8px' } },
              ctx.manifest.pack.name,
              ctx.manifest.pack.version ? ` · ${ctx.manifest.pack.version}` : '')
            : null,
        ),
        h('div.inst-actions',
          ctx.running
            ? h('button.btn.sm', {
              onclick: () => act('stop_server'),
            }, hl(), icon('stop', 13), h('span', 'Stop'))
            : h('button.btn.sm.primary', {
              disabled: st === 'orphan',
              onclick: () => act('start_server'),
            }, hl(), icon('play', 13), h('span', 'Start')),
          ctx.running && h('button.btn.sm.ghost', {
            onclick: () => act('restart_server'),
          }, icon('restart', 13), h('span', 'Restart')),
          ctx.running && h('button.btn.icon.sm.danger', {
            'aria-label': 'Kill the process',
            title: 'Kill — stops it without saving',
            onclick: async () => {
              const ok = await confirmDialog({
                title: 'Kill the server process?',
                message: 'This stops the JVM without letting Minecraft save. '
                  + 'Recent world changes will be lost. Use Stop unless the '
                  + 'server has genuinely hung.',
                confirmLabel: 'Kill it', danger: true,
              });
              if (ok) act('kill_server');
            },
          }, icon('power', 14)),
          st === 'incomplete' && h('button.btn.sm.gold', {
            onclick: () => retryLoader({ server_id: id, name: ctx.name }),
          }, hl(), icon('wrench', 13), h('span', 'Finish setup')),
        ),
      ),
    ));

    /* Written into, not rebuilt. */
    rail.update(ctx.stats, st);
  }

  async function act(a) {
    await action({ server_id: id, name: ctx.name,
      players: ctx.stats.online || 0 }, a);
    setTimeout(() => ctx.reload().catch(() => {}), 1500);
  }

  function paintTabs() {
    const visible = TABS.filter((t) => !t.when || t.when(ctx));
    mount(tabsHost, ...visible.map((t) => h('button', {
      type: 'button', role: 'tab',
      'aria-selected': String(t.key === params.tab),
      onclick: () => ctx.go(t.key),
    }, hl(), icon(t.icon, 15), h('span', t.label),
      t.key === 'diagnose' && (ctx.manifest.problems || []).length
        ? h('span.n', String(ctx.manifest.problems.length)) : null)));
  }

  paintHead();
  paintTabs();

  /* --- the tab itself ---------------------------------------- */
  const loaders = {
    overview: () => import('./tab-overview.js'),
    mods: () => import('./tab-mods.js'),
    plugins: () => import('./tab-mods.js'),
    files: () => import('./tab-files.js'),
    players: () => import('./tab-players.js'),
    console: () => import('./tab-console.js'),
    configs: () => import('./tab-configs.js'),
    tune: () => import('./tab-tune.js'),
    diagnose: () => import('./tab-diagnose.js'),
    backups: () => import('./tab-backups.js'),
  };
  const load = loaders[params.tab] || loaders.overview;

  /* The head and the tab strip are already painted and do not change when
     you move between tabs, so only the body needs a placeholder -- and
     giving it the right shape is what stops the strip jumping as the body
     under it changes height. */
  const TAB_SHAPES = {
    overview: { kpis: 4, panels: 2 },
    mods: { bar: 1, rows: 8 },
    plugins: { bar: 1, rows: 8 },
    files: { bar: 1, rows: 10 },
    players: { kpis: 3, rows: 6 },
    console: { panels: 1 },
    configs: { bar: 1, rows: 8 },
    tune: { kpis: 4, panels: 2 },
    diagnose: { panels: 2 },
    backups: { bar: 1, rows: 5 },
  };
  const tabShownAt = Date.now();
  mount(bodyHost, pageSkeleton(TAB_SHAPES[params.tab] || { rows: 6 }));

  try {
    const mod = await load();
    if (disposed) return { node, dispose: () => {} };
    tabView = await mod.render(ctx);
    await heldFor(tabShownAt);
    if (disposed) { try { tabView?.dispose?.(); } catch { /* gone */ } return { node, dispose: () => {} }; }
    dissolve(bodyHost, tabView.node);
  } catch (e) {
    console.error(e);
    mount(bodyHost, empty('chargey-failed.png', 'That tab would not open',
      e.message || String(e),
      h('button.btn.primary', { onclick: () => ctx.go(params.tab) },
        hl(), h('span', 'Try again'))));
  }

  /* --- live stats --------------------------------------------- */
  statsTimer = setInterval(async () => {
    if (document.hidden || disposed) return;
    try {
      const s = await api.get(`/api/instances/${id}/stats`);
      ctx.stats = s;
      ctx.detail.stats = s;
      ctx.detail.uptime_s = s.uptime_s;
      const wasRunning = ctx.detail.state === 'running';
      ctx.detail.state = s.running ? 'running'
        : s.crashed ? 'crashed' : ctx.detail.state === 'incomplete'
          ? 'incomplete' : 'stopped';
      paintHead();
      if (wasRunning !== !!s.running) {
        tabView?.onRunningChanged?.(!!s.running);
        refreshInstances({ quiet: true }).catch(() => {});
      }
    } catch { /* a stats blip is not worth a toast every six seconds */ }
  }, 6000);

  topActions(
    h('button.btn.icon.sm.ghost', {
      'aria-label': 'Reload this instance',
      onclick: () => ctx.reload().then(() => toast('Reloaded', 'ok', { timeout: 1400 }))
        .catch((e) => toastError(e)),
    }, icon('refresh', 15)),
    h('button.btn.sm.danger', {
      onclick: () => deleteInstance(id, ctx.name),
    }, icon('trash', 14), h('span', 'Delete')),
  );

  return {
    node,
    dispose() {
      disposed = true;
      clearInterval(statsTimer);
      rail.stop();
      try { tabView?.dispose?.(); } catch (e) { console.error(e); }
    },
  };
}

function inferLoader(server) {
  const exe = (server.executable || '').toLowerCase();
  for (const fam of ['neoforge', 'forge', 'fabric', 'purpur', 'folia', 'paper']) {
    if (exe.includes(fam)) return fam;
  }
  return '';
}

export async function deleteInstance(id, name) {
  const keepFiles = { value: false };
  const ok = await confirmDialog({
    title: `Delete ${name}?`,
    message: 'This removes the server from Crafty and deletes its directory — '
      + 'the world, the mods, the configs, all of it. There is no undo for '
      + 'this one.',
    detail: 'If you only want to stop using it, stop the server instead. A '
      + 'stopped server costs nothing.',
    confirmLabel: 'Delete permanently',
    danger: true,
    requireText: 'DELETE',
    art: 'creeper-delete-2x.png',
  });
  if (!ok) return;
  try {
    await api.del(`/api/instances/${id}?files=true`);
    toast(`${name} was deleted.`, 'ok');
    await refreshInstances({ quiet: true });
    go('/');
  } catch (e) {
    toastError(e, 'Could not delete it');
  }
}
