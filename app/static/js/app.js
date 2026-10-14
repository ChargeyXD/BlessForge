/* ============================================================
   app — the shell, the router, and boot.

   Routes are hashes so the whole thing is one static document
   that any reverse proxy will serve without rewrite rules:

     #/                     the fleet
     #/groups               the racks — the fleet, grouped
     #/create               create an instance
     #/discover             the catalogue and imports
     #/roulette             mod roulette
     #/settings             health, decisions, cache, assistant
     #/i/<id>/<tab>         one instance

   Every screen is reachable by URL, so a link to a crashed
   server's Diagnose tab is a link someone can send.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, $, $$,
  readTheme, applyTheme, prefs, loadingFox, empty, pill, ago, duration,
  pageSkeleton, heldFor, dissolve, mascot, debounce,
} from './core.js';
import {
  adoptRunning, onJobsChanged, jobsRunning, openDrawer, activeJobs, mountTray,
} from './jobs.js';
import { startPetals } from './petals.js';
import { refreshSession, loginScreen, session, may, isAdmin, logout }
  from './auth.js';

/* --- shared application state -------------------------------- */

export const state = {
  health: null,
  instances: [],
  instanceById: new Map(),
  route: { name: 'fleet', params: {} },
  loadersCatalogue: null,
  aiStatus: null,
  // The racks, and what hangs on which. Central state, not per-instance --
  // see `refreshGroups` for why.
  groups: { groups: [], assign: {}, motifs: [], updated_at: 0, persisted: true },
  // What the fleet poll has witnessed: when each server was last seen up.
  // The node cannot answer that for a server that is currently down, so it is
  // remembered here instead. Keyed by server_id, values are epoch seconds.
  activity: {},
};

const subscribers = new Set();
export function onState(fn) { subscribers.add(fn); return () => subscribers.delete(fn); }
function emit() { subscribers.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } }); }

/* --- routing -------------------------------------------------- */

/* Each route carries the SHAPE it is about to become, so the frame after a
   click already has that shape in it. See `pageSkeleton` in core.js for why
   this lives here rather than in the view module: the module itself has to
   be fetched over the network on first visit, and a skeleton that waits for
   its own module to arrive has missed the moment it existed for. */
const routes = [
  { name: 'fleet', pattern: /^\/?$/, load: () => import('./views/fleet.js'),
    skel: { head: 1, bar: 1, kpis: 4, cards: 4 } },
  { name: 'groups', pattern: /^\/groups$/, load: () => import('./views/groups.js'),
    skel: { head: 1, bar: 1, racks: 2 } },
  { name: 'create', pattern: /^\/create$/, load: () => import('./views/create.js'),
    skel: { head: 1, split: [4] } },
  { name: 'discover', pattern: /^\/discover$/, load: () => import('./views/discover.js'),
    skel: { head: 1, bar: 1, cards: 6 } },
  { name: 'roulette', pattern: /^\/roulette$/, load: () => import('./views/roulette.js'),
    skel: { head: 1, panels: 2 } },
  { name: 'settings', pattern: /^\/settings$/, load: () => import('./views/settings.js'),
    skel: { head: 1, panels: 3 } },
  // The Gate: everything that is about the whole install rather than about
  // you -- accounts, nodes, the status page, tokens, the ledger. Admins only;
  // the server refuses the calls behind it to anybody else regardless.
  { name: 'gate', pattern: /^\/gate(?:\/([a-z-]+))?$/, load: () => import('./views/gate.js'),
    params: (m) => ({ section: m[1] || '' }), skel: { head: 1, panels: 2 } },
  {
    name: 'instance',
    pattern: /^\/i\/([^/]+)(?:\/([^/]+))?$/,
    load: () => import('./views/instance.js'),
    params: (m) => ({ id: m[1], tab: m[2] || 'overview' }),
    skel: { instanceHead: 1, rows: 6 },
  },
];

/* --- the mobile drawer ---------------------------------------
   Opening it has to LOCK THE PAGE BEHIND IT, and that is not a
   nicety. The rail is 300px of fixed overlay; the document
   behind it is still scrollable; and the rail itself is not a
   scroller. So on a touchscreen a drag that starts anywhere on
   the open drawer chains straight through to the document and
   scrolls the PAGE, while the drawer sits still under the
   finger. It reads exactly like a frozen drawer, and it is
   invisible to both a screenshot and a hit test -- every point
   inside the drawer really is the topmost element, and every tap
   really does land. Only a drag shows it.

   `position:fixed` on body rather than `overflow:hidden`,
   because iOS Safari ignores overflow:hidden on the body for
   touch scrolling. Fixing it needs the scroll offset preserved
   and restored, or dismissing the drawer jumps you to the top of
   a long fleet -- which is its own bug, and the reason the
   offset is captured rather than assumed to be zero.        */
let railScrollY = 0;

export function setRailOpen(open) {
  const body = document.body;
  const was = body.classList.contains('rail-open');
  if (open === was) return;
  if (open) {
    railScrollY = window.scrollY || document.documentElement.scrollTop || 0;
    body.classList.add('rail-open', 'scroll-locked');
    body.style.top = `-${railScrollY}px`;
  } else {
    body.classList.remove('rail-open', 'scroll-locked');
    body.style.top = '';
    // `instant`: a smooth scroll here animates the page back up behind a
    // drawer that has already gone, which looks like the app lurching.
    window.scrollTo({ top: railScrollY, behavior: 'instant' });
  }
}

let currentView = null;

export function go(path, { replace } = {}) {
  const target = `#${path}`;
  if (location.hash === target) { route(); return; }
  if (replace) location.replace(target);
  else location.hash = path;
}

function parse() {
  const raw = decodeURIComponent(location.hash.replace(/^#/, '')) || '/';
  for (const r of routes) {
    const m = r.pattern.exec(raw);
    if (m) return { def: r, params: r.params ? r.params(m) : {} };
  }
  return { def: routes[0], params: {} };
}

let routeToken = 0;
async function route() {
  const token = ++routeToken;
  const { def, params } = parse();
  state.route = { name: def.name, params };

  try { currentView?.dispose?.(); } catch (e) { console.error(e); }
  currentView = null;

  paintNav();
  const main = $('#main');
  /* Synchronous, before anything is awaited. This is the whole point: the
     old sequence blanked the page, showed a spinner, then waited on a
     dynamic import AND a network round trip before dropping the finished
     screen in all at once -- two jarring moments per navigation, the
     second of which moved every element on the page. Now the layout is
     already there and the content fills into it. */
  const shownAt = Date.now();
  mount(main, pageSkeleton(def.skel || { head: 1, rows: 5 }));
  main.classList.remove('routing');
  void main.offsetWidth;             // restart the entrance, not resume it
  main.classList.add('routing');
  setRailOpen(false);

  let mod;
  try {
    mod = await def.load();
  } catch (e) {
    console.error(e);
    mount(main, h('div.wrap',
      empty('hurt', 'That screen would not load',
        'A part of the interface failed to download. A reload usually fixes '
        + 'it; if it does not, the container may be mid-restart.',
        h('button.btn.primary', { onclick: () => location.reload() },
          hl(), h('span', 'Reload')))));
    return;
  }
  if (token !== routeToken) return;    // a newer navigation won

  try {
    const view = await mod.render({ params, main });
    if (token !== routeToken) { view?.dispose?.(); return; }
    // A skeleton that appears and vanishes inside 60ms reads as a glitch,
    // so a fast screen waits out the remainder of the minimum. It costs
    // nothing anybody notices and it makes every navigation feel the same.
    await heldFor(shownAt);
    if (token !== routeToken) { view?.dispose?.(); return; }
    currentView = view || null;
    // The skeleton stays on top and dissolves off the real content, so the
    // swap reads as the screen developing rather than as a cut.
    if (view?.node) dissolve(main, view.node);
    // `settling`, not `routing`: the page already slid in with the
    // skeleton. Sliding the whole thing a second time would undo the
    // continuity the skeleton just bought. This is a much smaller move.
    main.classList.remove('routing');
    main.classList.remove('settling');
    void main.offsetWidth;
    main.classList.add('settling');
    main.scrollTop = 0;
    window.scrollTo({ top: 0, behavior: 'instant' });
  } catch (e) {
    console.error(e);
    if (token !== routeToken) return;
    mount(main, h('div.wrap',
      empty('hurt', 'This screen could not be drawn',
        e.message || String(e),
        h('button.btn.primary', { onclick: () => route() },
          hl(), h('span', 'Try again')))));
  }
}

/* ============================================================
   racks — the fleet, grouped
   ------------------------------------------------------------
   A group is a property of the COLLECTION, not of any one
   server, so it is kept centrally under /data/state rather than
   in each instance's .blessforge.json. Three reasons, in order
   of how much they cost to get wrong:

     * An empty rack, a rack's name and a rack's mark have
       nowhere to live in a per-instance file. Delete the last
       member and the rack itself would vanish; rename one and
       you would be writing N files to change one word.
     * Reading groups out of manifests is one round trip to the node
       per server just to lay out a screen. The fleet poll
       already costs two per server and a layout choice does not
       get to add a third.
     * An instance BlessForge did not create has no manifest at
       all, and an unmanaged fleet is exactly the one that most
       needs organising.

   What that costs is staleness, and it is paid for on the read
   side: assignments are keyed on the server_id (so a
   rename is free), and the server prunes assignments for ids
   the fleet no longer contains -- but only when the fleet read
   actually succeeded, so an unreachable node never erases
   anybody's racks.
   ============================================================ */

/* Each rack carries a shrine mark rather than a colour picked out of a
   wheel. Every value here is an existing theme token, which is what makes
   the marks follow light/dark and stay legible without anyone checking a
   contrast ratio -- and it is why this feature adds no new palette. */
export const MOTIFS = {
  sakura: { glyph: '桜', label: 'Sakura', ink: 'var(--rose)', soft: 'var(--rose-soft)' },
  kitsune: { glyph: '狐', label: 'Fox', ink: 'var(--gold-ink)', soft: 'var(--gold-soft)' },
  torii: { glyph: '鳥', label: 'Torii', ink: 'var(--shrine-deep)', soft: 'var(--shrine-soft)' },
  matsu: { glyph: '松', label: 'Pine', ink: 'var(--sage)', soft: 'var(--sage-soft)' },
  tsuki: { glyph: '月', label: 'Moon', ink: 'var(--plum)', soft: 'var(--plum-soft)' },
  mizu: { glyph: '水', label: 'Water', ink: 'var(--info)', soft: 'var(--info-soft)' },
  kaminari: { glyph: '雷', label: 'Thunder', ink: 'var(--amber-ink)', soft: 'var(--amber-soft)' },
  yuki: { glyph: '雪', label: 'Snow', ink: 'var(--faint)', soft: 'var(--paper-2)' },
};

export function motif(name) { return MOTIFS[name] || MOTIFS.sakura; }

/* The rack a server hangs on, or null for the open yard. Resolved against
   the CURRENT fleet every time it is asked, never cached onto an instance,
   so a server deleted or added behind the page's back is simply absent or
   simply ungrouped rather than wrong. */
export function groupOf(serverId) {
  const id = (state.groups.assign || {})[serverId];
  if (!id) return null;
  return (state.groups.groups || []).find((g) => g.id === id) || null;
}

/* Adopt a write's response wholesale. Every mutation returns the entire
   document, so two tabs editing at once resolve to last-write-wins and the
   loser corrects itself on its next read rather than drifting. */
export function adoptGroups(doc) {
  if (!doc || !Array.isArray(doc.groups)) return state.groups;
  state.groups = {
    groups: doc.groups,
    assign: doc.assign || {},
    motifs: doc.motifs || Object.keys(MOTIFS),
    updated_at: doc.updated_at || 0,
    persisted: doc.persisted !== false,
  };
  paintFleetRail();
  emit();
  return state.groups;
}

export async function refreshGroups({ quiet = true } = {}) {
  try {
    const doc = await api.get('/api/fleet/groups');
    // Adopting emits, and emitting repaints whatever screen is open. The
    // racks change when somebody changes them, which is far less often than
    // once a minute, so an unchanged document is dropped on the floor rather
    // than redrawing the page for nothing.
    if (doc.updated_at && doc.updated_at === state.groups.updated_at) {
      return state.groups;
    }
    adoptGroups(doc);
  } catch (e) {
    if (!quiet) toastError(e, 'Could not read the racks');
  }
  return state.groups;
}

/* One sentence about when a server last actually ran.
   ------------------------------------------------------------
   The node reports `started` for a RUNNING server and nothing at
   all for a stopped one, so "when did this last run" is not a
   question the node can answer -- and it is the question the rail
   has to sort on. It is answered from the activity record the
   fleet poll writes (see `recordSeen`): `last_running_at` is
   the last poll at which the server was observed up, which is
   an honest answer whether or not BlessForge was the thing that
   started it. */
export function lastRan(s) {
  const a = (state.activity || {})[s.server_id] || {};
  if (s.state === 'running') {
    // Only claimed when the down -> up edge was actually witnessed. On the
    // first sighting of an already-running server there was no "down" to
    // witness, and inventing an uptime there would be a lie the user could
    // catch in one glance at the console.
    return a.last_started && a.last_started_precision === 'observed'
      ? `up ${duration(Date.now() / 1000 - a.last_started)}`
      : 'up now';
  }
  if (a.last_running_at) return `ran ${ago(a.last_running_at)}`;
  if (s.created) return `added ${ago(s.created)}`;
  if (a.first_seen) return `first seen ${ago(a.first_seen)}`;
  return 'never seen up';
}

/* Which servers the rail shows, and in exactly what order.
   ------------------------------------------------------------
   Three tiers, then most-recent-first inside each:

     0  running now          — newest start first
     1  seen up at some point — most recently up first
     2  never seen up        — most recently added first

   The sort key for tier 1 is `last_running_at` rather than
   `last_started`, because the last poll at which a server was
   up is known for every server this app has ever watched, while
   a start time is only known when the transition happened to be
   witnessed. Tier 2 falls back to the record's own `created`, and
   then to the first time BlessForge saw the server at all.

   Ties break on name so the list cannot shuffle under the
   cursor between two polls.

   The server you currently have open is always kept, even when
   it does not earn a place: losing the highlight in the rail
   the moment you open an idle server is worse than showing one
   fewer recent. */
export function recentFleet(limit = 5) {
  const act = state.activity || {};
  const at = (s) => act[s.server_id] || {};
  const added = (s) => {
    const t = s.created ? Date.parse(s.created) : NaN;
    return Number.isNaN(t) ? (at(s).first_seen || 0) * 1000 : t;
  };
  const rank = (s) => (s.state === 'running' ? 0
    : (typeof at(s).last_running_at === 'number' ? 1 : 2));
  const key = (s) => {
    const a = at(s);
    if (s.state === 'running') return (a.last_started || a.first_seen || 0) * 1000;
    if (typeof a.last_running_at === 'number') return a.last_running_at * 1000;
    return added(s);
  };

  const ordered = [...state.instances].sort((x, y) => rank(x) - rank(y)
    || key(y) - key(x)
    || String(x.name || '').localeCompare(String(y.name || '')));

  const rows = ordered.slice(0, Math.max(1, limit));
  const openId = state.route.name === 'instance' ? state.route.params.id : null;
  if (openId && !rows.some((s) => s.server_id === openId)) {
    const open = ordered.find((s) => s.server_id === openId);
    if (open) rows.splice(rows.length - 1, 1, open);
  }
  return { rows, more: Math.max(0, ordered.length - rows.length),
    total: ordered.length };
}

/* --- the fleet, polled ---------------------------------------- */

let fleetTimer = null;
let groupTick = 0;

export async function refreshInstances({ quiet } = {}) {
  try {
    const data = await api.get('/api/instances');
    state.instances = data.items || [];
    state.instanceById = new Map(state.instances.map((i) => [i.server_id, i]));
    paintFleetRail();
    emit();
    // Deliberately not awaited: this is bookkeeping, and the fleet must not
    // wait a second round trip to appear.
    recordSeen();
    // The racks change far less often than the fleet does and cost a local
    // file read, so they are re-read once a minute rather than every poll --
    // and immediately after any write, which goes through adoptGroups().
    if (groupTick++ % 4 === 0) refreshGroups();
    return state.instances;
  } catch (e) {
    if (!quiet) toastError(e, 'Could not read the fleet');
    throw e;
  }
}

/* Hand this poll's observation to the server, which timestamps it with its
   own clock and hands back the whole record. One local round trip; no extra
   traffic to the node. If it fails the rail simply orders by what it can see
   right now -- running first, then the fleet's own order. */
async function recordSeen() {
  if (!state.instances.length) return;
  try {
    const res = await api.post('/api/fleet/seen', {
      full: true,
      items: state.instances.map((s) => ({
        server_id: s.server_id,
        name: s.name || '',
        running: s.state === 'running',
        created: s.created || null,
      })),
    });
    const first = !Object.keys(state.activity || {}).length;
    state.activity = res.servers || {};
    paintFleetRail();
    // Only the first answer is worth a full repaint. After that the rail
    // updates on its own and every screen picks the new values up on the
    // next poll, rather than every screen redrawing twice every 15 seconds.
    if (first) emit();
  } catch { /* ordering falls back; nothing here is worth a toast */ }
}

function startFleetPolling() {
  const tick = () => {
    // Polling a node that is not there just fills the log with failures,
    // and a hidden tab has nobody to show the result to.
    if (document.hidden || !state.health?.ready) return;
    refreshInstances({ quiet: true }).catch(() => {});
  };
  clearInterval(fleetTimer);
  fleetTimer = setInterval(tick, 15000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) tick();
  });
}

/* --- the rail -------------------------------------------------- */

/* `needs` hides a destination somebody cannot use. The server refuses the
   calls behind these screens regardless -- this only avoids offering a
   door that opens onto a permission error. */
const NAV = [
  { group: 'The shrine', items: [
    { name: 'fleet', path: '/', label: 'Fleet', icon: 'server' },
    { name: 'groups', path: '/groups', label: 'Racks', icon: 'layers' },
  ] },
  { group: 'Make', items: [
    { name: 'create', path: '/create', label: 'New server', icon: 'plus',
      needs: 'server.create' },
    { name: 'discover', path: '/discover', label: 'Discover', icon: 'compass' },
    { name: 'roulette', path: '/roulette', label: 'Mod Roulette', icon: 'dice',
      needs: 'roulette.use' },
  ] },
  { group: 'Keep', items: [
    { name: 'settings', path: '/settings', label: 'Settings', icon: 'gear' },
    { name: 'gate', path: '/gate', label: 'The Gate', icon: 'torii', admin: true },
  ] },
];
const NAV_ITEMS = NAV.flatMap((g) => g.items);

function lantern(glyph, second) {
  return h(`div.lantern${second ? '.l2' : ''}`, { 'aria-hidden': 'true' },
    h('div.cord'),
    h('div.body', h('div.glyph', glyph)));
}

function buildRail() {
  return h('nav#rail', { 'aria-label': 'Primary' },
    // The pink rope down the rail's edge, with its paper shide.
    h('span.rail-rope', { 'aria-hidden': 'true' }),
    h('a.rail-brand', { href: '#/' },
      mascot('fox-prowl', 44, { cls: 'rail-mark' }),
      h('span',
        h('span.wm', 'BlessForge'),
        h('span.tag', 'Shrine of servers')),
    ),
    h('div.rail-lanterns', lantern('狐'), lantern('祝', true)),
    h('div.rail-nav', { id: 'railnav' }),
    h('div.rail-fleet', { id: 'railfleet', 'aria-label': 'Servers' }),
    // The companion: patrols a strip of grass, and reacts to the work.
    buildCompanion(),
    // Who you are signed in as, and the way out. It sits above the
    // controls rather than in a menu behind an avatar: on a panel where
    // several people have different powers, "which of us am I right now"
    // is worth a line of its own.
    h('div.rail-who', { id: 'railwho' }),
    h('div.rail-foot',
      h('button.btn.icon.sm.ghost', {
        id: 'themebtn', 'aria-label': 'Switch between light and dark',
        onclick: cycleTheme,
      }, icon(readTheme() === 'light' ? 'sun' : 'moon', 15)),
      h('button.btn.sm.ghost', {
        id: 'activitybtn', onclick: () => openDrawer(),
      }, icon('activity', 14), h('span', 'Activity')),
      h('span.conn', { id: 'healthpill', title: 'Checking the connection' },
        h('span.conn-lamp', { 'aria-hidden': 'true' }), h('span', 'checking')),
    ),
  );
}

/* --- the companion -------------------------------------------
   A pixel shrine-keeper walking a strip of grass above your name. It is
   decoration, and says nothing only it knows: it mines while a job runs,
   celebrates when one finishes, winces when one fails, and does a little
   something when clicked. The patrol is a CSS animation, so if the timeline
   never runs it simply stands at its post. */
const EMOTES = [
  ['wave', 'Hello!'], ['jump', 'Hup!'], ['piglindance', 'A little dance.'],
  ['nod', 'All quiet here.'], ['eat', 'Snack break.'], ['skip', 'Tra-la.'],
  ['sneakspam', 'Sneaky.'], ['mcjump', 'Boing.'], ['surprise', 'Oh!'],
];
let companionTimer = null;

function buildCompanion() {
  const sprite = mascot('walk', 40, { cls: 'cmp-sprite' });
  const say = h('span.cmp-say', { 'aria-hidden': 'true' });
  const walker = h('span.cmp-walker', sprite, say);
  return h('div#companion.cmp', { 'aria-hidden': 'false' },
    h('button.cmp-btn', {
      type: 'button', 'aria-label': 'The shrine keeper (decoration)',
      onclick: () => {
        const [name, line] = EMOTES[Math.floor(Math.random() * EMOTES.length)];
        companionDo(name, line, 2600);
      },
    }, walker),
    h('span.cmp-grass', { 'aria-hidden': 'true' }));
}

function companionDo(name, line, ms = 0) {
  const host = $('#companion');
  if (!host) return;
  const sprite = host.querySelector('.cmp-sprite');
  const say = host.querySelector('.cmp-say');
  sprite.src = `/assets/mascot/${name}.gif`;
  say.textContent = line || '';
  host.classList.toggle('talking', !!line);
  host.classList.toggle('still', name !== 'walk');
  clearTimeout(companionTimer);
  if (ms) companionTimer = setTimeout(companionIdle, ms);
}

function companionIdle() {
  if (jobsRunning()) companionDo('mine', 'Working on it…');
  else companionDo('walk', '');
}

function paintWho() {
  const host = $('#railwho');
  if (!host || !session.user) return;
  const u = session.user;
  mount(host,
    h('div.who-card',
      h('span.who-mark', { 'aria-hidden': 'true' },
        (u.display || u.username).slice(0, 1).toUpperCase()),
      h('div.who-body',
        h('div.who-name.trunc', { title: u.username }, u.display || u.username),
        h('div.who-role.mono', session.roles?.[u.role] ? u.role : u.role)),
      h('button.btn.icon.xs.ghost', {
        'aria-label': `Sign out of ${u.username}`, title: 'Sign out',
        onclick: () => logout(),
      }, icon('power', 12))));
}

function paintNav() {
  const host = $('#railnav');
  if (!host) return;
  paintWho();
  const allowed = (n) => (!n.needs || may(n.needs)) && (!n.admin || isAdmin());
  const needs = state.instances.filter((s) => NEEDS_YOU.has(s.state)).length;
  mount(host, ...NAV.map((g) => {
    const items = g.items.filter(allowed);
    if (!items.length) return null;
    return [
      h('div.sec', g.group),
      ...items.map((n) => h('a.navlink', {
        href: `#${n.path}`,
        'aria-current': state.route.name === n.name ? 'page' : null,
      }, hl(), icon(n.icon, 17), h('span', n.label),
        n.name === 'fleet' && needs
          ? h('span.count.alarm', { title: `${needs} need you` }, String(needs))
          : n.name === 'fleet' && state.instances.length
            ? h('span.count', String(state.instances.length))
            : n.name === 'groups' && state.groups.groups.length
              ? h('span.count', String(state.groups.groups.length)) : null)),
    ];
  }));
  paintTopbar();
}

/* States that put a server in the "Needs you" group, wherever it is shown. */
export const NEEDS_YOU = new Set(['crashed', 'incomplete', 'orphan']);

/* The rail is not the fleet.
   ------------------------------------------------------------
   It used to list every server, which is fine at three and a
   scrollbar at twenty -- and a scrollbar in a nav rail means the
   thing you wanted was never on screen. It shows at most five
   now: what is up, then what you last had up, then what you
   last added (see `recentFleet` for the exact rule), with the
   rest behind one row that goes to the fleet screen. Each row
   carries a tick in its rack's mark, so the rail and the rack
   screen agree at a glance about where a server lives. */
const RAIL_ROWS = 5;

function paintFleetRail() {
  const host = $('#railfleet');
  if (!host) return;
  if (!state.instances.length) {
    mount(host, h('div.sec', 'No servers yet'));
    paintNav();
    return;
  }
  const openId = state.route.name === 'instance' ? state.route.params.id : null;
  const { rows, more, total } = recentFleet(RAIL_ROWS);

  mount(host,
    h('div.sec', more ? `Recent · ${rows.length} of ${total}` : `Servers · ${total}`),
    ...rows.map((s) => {
      const g = groupOf(s.server_id);
      const row = h('button.fleetrow', {
        type: 'button',
        'aria-current': s.server_id === openId ? 'true' : null,
        title: g ? `${s.name} — ${g.name}` : (s.name || ''),
        onclick: () => go(`/i/${s.server_id}/overview`),
      },
        hl(),
        g ? h('span.gtick', { 'aria-hidden': 'true' }) : null,
        h(`span.state.${s.state || 'stopped'}`, {
          'aria-hidden': 'true',
          title: s.state || 'stopped',
        }),
        h('span.nm', s.name || s.server_id.slice(0, 8),
          h('span.meta', [
            s.loader ? s.loader : null,
            s.minecraft || null,
            s.running && s.players !== null && s.players !== undefined
              ? `${s.players} online` : lastRan(s),
          ].filter(Boolean).join(' · ') || 'unmanaged')),
      );
      if (g) row.style.setProperty('--g-ink', motif(g.motif).ink);
      return row;
    }),
    more ? h('button.fleetrow.more', {
      type: 'button',
      onclick: () => go('/'),
    }, hl(), h('span.plus', { 'aria-hidden': 'true' }, '+'),
      h('span.nm', `${more} more`,
        h('span.meta', 'open the whole fleet'))) : null,
  );
  paintNav();
}

const LABELS = {
  fleet: ['The fleet', 'every server on every node'],
  groups: ['Racks', 'every server on its hook'],
  create: ['New server', 'a loader, a version, an empty mods folder'],
  discover: ['Discover', 'modpacks, mods and plugins'],
  roulette: ['Mod Roulette', 'deal a pack you did not choose'],
  settings: ['Settings', 'you, your sign-in and your keys'],
  gate: ['The Gate', 'accounts, machines and the whole shrine'],
};

function paintTopbar() {
  const crumbs = $('#crumbs');
  if (!crumbs) return;
  const { name, params } = state.route;
  if (name === 'instance') {
    const inst = state.instanceById.get(params.id);
    mount(crumbs,
      h('a.crumb-up', { href: '#/' }, icon('arrowLeft', 13), h('span', 'Fleet')),
      h('span.crumb-sep', { 'aria-hidden': 'true' }, '/'),
      h('span.t.brush', inst?.name || 'Server'),
      h('span.s', (params.tab || 'overview').replace(/-/g, ' ')));
  } else {
    const [t, sub] = LABELS[name] || ['BlessForge', ''];
    mount(crumbs, h('span.t.brush', t), sub && h('span.s', sub));
  }
}

/* --- jump to… (the palette) ----------------------------------
   Screens, every server you can see, and the current server's tabs, one
   keystroke away: Ctrl/⌘ K anywhere. Type to filter, Enter takes the first,
   arrows move, Escape closes. Built fresh each time it opens, so it never
   offers a server that has since been deleted. */
const INSTANCE_TABS = [
  ['overview', 'Overview'], ['console', 'Console'], ['players', 'Players'],
  ['history', 'History'], ['mods', 'Mods'], ['files', 'Files'], ['configs', 'Configs'],
  ['tune', 'Tune'], ['diagnose', 'Diagnose'], ['backups', 'Backups'],
  ['schedules', 'Schedules'], ['undo', 'Undo'], ['rules', 'Rules'],
];
let paletteEl = null;

function paletteItems() {
  const items = NAV_ITEMS.filter((n) => (!n.needs || may(n.needs)) && (!n.admin || isAdmin()))
    .map((n) => ({ label: n.label, hint: 'screen', icon: n.icon, path: n.path }));
  for (const s of state.instances) {
    items.push({ label: s.name || s.server_id, hint: [s.loader, s.minecraft, s.state]
      .filter(Boolean).join(' · '), icon: 'server', path: `/i/${s.server_id}/overview` });
  }
  if (state.route.name === 'instance') {
    const id = state.route.params.id;
    const inst = state.instanceById.get(id);
    for (const [key, label] of INSTANCE_TABS) {
      items.push({ label: `${inst?.name || 'This server'} › ${label}`, hint: 'tab',
        icon: 'chevronRight', path: `/i/${id}/${key}` });
    }
  }
  return items;
}

export function openPalette() {
  if (paletteEl) { paletteEl.querySelector('input')?.focus(); return; }
  const all = paletteItems();
  let shown = all;
  let at = 0;
  const list = h('ul.pal-list', { role: 'listbox', id: 'pal-list' });
  const input = h('input.pal-inp', {
    type: 'text', placeholder: 'Jump to a screen, a server or a tab…',
    'aria-label': 'Jump to', role: 'combobox', 'aria-expanded': 'true',
    'aria-controls': 'pal-list', autocomplete: 'off', spellcheck: 'false',
  });
  const opener = document.activeElement;
  const closeIt = () => {
    paletteEl?.remove();
    paletteEl = null;
    document.removeEventListener('keydown', onKey, true);
    if (opener?.isConnected) opener.focus();
  };
  const pick = (item) => { closeIt(); if (item) go(item.path); };
  const paint = () => {
    mount(list, ...(shown.length ? shown.slice(0, 40).map((it, i) => h('li', {
      role: 'option', 'aria-selected': String(i === at),
      onmousedown: (e) => { e.preventDefault(); pick(it); },
    }, icon(it.icon, 15), h('span.pal-lbl', it.label), h('span.pal-hint', it.hint)))
      : [h('li.pal-none', 'Nothing by that name.')]));
    list.children[at]?.scrollIntoView?.({ block: 'nearest' });
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); closeIt(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); at = Math.min(at + 1, shown.length - 1); paint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); at = Math.max(at - 1, 0); paint(); }
    else if (e.key === 'Enter') { e.preventDefault(); pick(shown[at]); }
  };
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    shown = !q ? all : all.filter((it) => `${it.label} ${it.hint}`.toLowerCase().includes(q));
    at = 0;
    paint();
  });
  paletteEl = h('div#palette', { role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Jump to' },
    h('div.pal-scrim', { onclick: closeIt }),
    h('div.pal-box',
      h('div.pal-head', icon('search', 16), input, h('kbd', 'Esc')),
      list));
  document.body.append(paletteEl);
  document.addEventListener('keydown', onKey, true);
  paint();
  setTimeout(() => input.focus(), 0);
}

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
    if (!$('#shell')) return;
    e.preventDefault();
    openPalette();
  }
});

function cycleTheme() {
  const order = ['system', 'dark', 'light'];
  const next = order[(order.indexOf(readTheme()) + 1) % order.length];
  applyTheme(next);
  const btn = $('#themebtn');
  if (btn) {
    mount(btn, icon(next === 'light' ? 'sun' : next === 'dark' ? 'moon' : 'star', 15));
    btn.setAttribute('aria-label', `Theme: ${next}. Click to change.`);
  }
  toast(`Theme: ${next}`, 'ok', { timeout: 1800 });
}

/* --- health -------------------------------------------------- */

export async function refreshHealth() {
  try {
    state.health = await api.get('/api/health');
  } catch {
    state.health = { ready: false, checks: {}, config: {}, unreachable: true };
  }
  paintHealthPill();
  emit();
  return state.health;
}

function paintHealthPill() {
  const el = $('#healthpill');
  if (!el) return;
  const hs = state.health;
  const ok = hs?.ready;
  el.className = `conn ${ok ? 'ok' : 'bad'}`;
  clear(el);
  el.append(h('span.conn-lamp', { 'aria-hidden': 'true' }),
    h('span', ok ? 'connected' : 'setup needed'));
  el.title = ok
    ? `${engineLabel(hs.checks?.engine?.driver)} answered in `
      + `${hs.checks?.engine?.latency_ms ?? '?'} ms`
    : (hs?.checks?.engine?.error || 'BlessForge cannot reach a node');
}

/* What is actually running the servers, in words. `mock` is the in-memory
   stand-in -- worth saying plainly, because a fleet full of example servers
   should never be mistaken for real ones. */
export function engineLabel(driver) {
  if (driver === 'mock') return 'The in-memory test engine';
  if (driver === 'agent') return 'The node agents';
  return 'The engine';
}

function setupBanner() {
  const hs = state.health;
  if (!hs || hs.ready) return null;
  const problems = [];
  for (const [key, check] of Object.entries(hs.checks || {})) {
    if (check && check.ok === false) problems.push(`${key}: ${check.error}`);
  }
  return h('div.note.bad', { role: 'alert', style: { marginBottom: '18px' } },
    h('b', 'BlessForge is not fully connected yet'),
    h('ul', { style: { margin: '6px 0 0 18px', padding: 0 } },
      ...problems.map((p) => h('li', p))),
    h('div.btnrow', { style: { marginTop: '10px' } },
      h('a.btn.sm', { href: '#/settings' }, hl(), h('span', 'Open settings'))));
}

export function bannerIfUnhealthy() { return setupBanner(); }

/* --- boot ----------------------------------------------------- */

function buildShell() {
  document.body.append(
    h('div.grain', { 'aria-hidden': 'true' }),
    h('canvas#petals', { 'aria-hidden': 'true' }),
    h('div#shell', {
      // The scrim is #shell's own ::after, so a tap on the dimmed area lands
      // on #shell itself. Without this the drawer could only be dismissed by
      // navigating: once it is open it covers the toggle that opened it.
      onclick: (e) => {
        if (!document.body.classList.contains('rail-open')) return;
        if (e.target.closest('#rail, #railtoggle')) return;
        setRailOpen(false);
      },
    },
      buildRail(),
      h('div#col',
        h('div#banner'),
        h('header#topbar',
          h('button.btn.icon.sm.ghost', {
            id: 'railtoggle', 'aria-label': 'Show the server list',
            style: { display: 'none' },
            onclick: () => setRailOpen(
              !document.body.classList.contains('rail-open')),
          }, icon('menu', 16)),
          h('div.crumbs', { id: 'crumbs' }),
          h('div.btnrow', { id: 'topactions' }),
          h('button.jump', {
            type: 'button', onclick: () => openPalette(),
            'aria-label': 'Jump to a screen, server or tab (Ctrl K)',
          }, icon('search', 14), h('span', 'Jump to…'), h('kbd', 'Ctrl K')),
        ),
        h('main#main', { tabIndex: -1 }),
      ),
    ),
    h('div#tray'),
  );
  mountTray($('#tray'));
  paintBanner();
}

/* The shipped password is still in use: a gold ofuda across the top of every
   screen until it is changed, because it is the one risk a fresh install
   carries and nothing else on the page would mention it. */
function paintBanner() {
  const host = $('#banner');
  if (!host) return;
  if (!session.defaultPasswordInUse) { clear(host); return; }
  mount(host, h('div.ofuda-banner', { role: 'alert' },
    h('span.ob-seal', { 'aria-hidden': 'true' }, '警'),
    h('span', h('b', 'The default admin password is still in use. '),
      'Anyone who knows BlessForge knows it. Change it before this is reachable '
      + 'from outside your network.'),
    isAdmin() ? h('a.btn.xs', { href: '#/settings' }, hl(), h('span', 'Change it')) : null));
}

/* --- live fleet ------------------------------------------------
   The nodes report a start, a stop, a crash or a port change the moment it
   happens (app/fleetbus.py). The fleet used to find out on its next poll,
   up to fifteen seconds later; now it re-reads at once. The live numbers
   in a stats event are written straight into the fleet, so the gauges move
   without a request. The poll stays as the fallback for when the stream is
   down. */
let fleetEvents = null;

function startFleetEvents() {
  if (fleetEvents || typeof EventSource === 'undefined') return;
  const reread = debounce(() => refreshInstances({ quiet: true }).catch(() => {}), 350);
  const es = new EventSource('/api/fleet/events');
  fleetEvents = es;
  const onChange = () => reread();
  for (const kind of ['status', 'crash', 'crash_loop', 'port_changed', 'power', 'lagged']) {
    es.addEventListener(kind, onChange);
  }
  es.addEventListener('crash', (ev) => {
    try {
      const data = JSON.parse(ev.data);
      const name = state.instanceById.get(data.server)?.name || 'A server';
      toast(`${name} crashed${data.oom ? ' -- killed for its memory limit' : ''}.`, 'bad',
        { title: 'Crash' });
      companionDo('hurt', 'Ouch.', 3000);
    } catch { /* the re-read shows it anyway */ }
  });
  es.addEventListener('stats', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    let changed = false;
    for (const [sid, st] of Object.entries(data.servers || {})) {
      const inst = state.instanceById.get(sid);
      if (!inst) continue;
      if (st.cpu !== undefined) inst.cpu = st.cpu;
      if (st.mem_percent !== undefined) inst.mem = st.mem_percent;
      if (st.mem !== undefined) inst.mem_bytes = st.mem;
      if (st.online !== undefined) inst.players = st.online;
      if (st.max !== undefined && st.max !== null) inst.max_players = st.max;
      changed = true;
    }
    if (changed) emitLive();
  });
  es.onerror = () => {
    // EventSource reconnects by itself; a closed one (the panel restarted
    // with a different build) is opened again on the next health check.
    if (es.readyState === EventSource.CLOSED) fleetEvents = null;
  };
}

/* Live numbers repaint only the screens that asked for them, not every
   subscriber -- a stats event every few seconds must not rebuild a form. */
const liveSubscribers = new Set();
export function onLive(fn) { liveSubscribers.add(fn); return () => liveSubscribers.delete(fn); }
function emitLive() {
  liveSubscribers.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
}

export function topActions(...nodes) {
  const host = $('#topactions');
  if (host) mount(host, ...nodes);
}

function checkDisplayFont() {
  // Archivo Black ships one weight and must not be synthesised bold; Space
  // Grotesk, the vendored fallback, must be. Which one arrived is a runtime
  // fact, so it is read at runtime.
  const mark = () => {
    const ok = document.fonts?.check?.('700 24px "Archivo Black"');
    document.documentElement.classList.toggle('no-archivo', !ok);
  };
  mark();
  document.fonts?.ready?.then(mark).catch(() => {});
  setTimeout(mark, 2500);
}

/* Nothing is drawn until we know who is asking.
   ------------------------------------------------------------
   The server refuses unauthenticated requests on its own, so
   this is not the security boundary -- it is the difference
   between a login screen and a fleet page full of failed
   requests. `/api/auth/session` is public precisely so this
   question can be asked before anything else. */
async function gate() {
  await refreshSession();
  if (session.authenticated && !session.user?.must_change_password) return true;

  const host = document.createElement('div');
  document.body.append(host);
  return new Promise((resolve) => {
    const show = (mustChangeFor) => {
      mount(host, loginScreen({
        mustChangeFor,
        onSignedIn: async () => {
          await refreshSession();
          if (session.user?.must_change_password) {
            show(session.user.username);      // straight into the change
            return;
          }
          host.remove();
          resolve(true);
        },
      }));
    };
    show(session.authenticated && session.user?.must_change_password
      ? session.user.username : null);
  });
}

async function boot() {
  applyTheme(readTheme());
  // Entrance animations start from opacity 0 and would leave the page blank
  // if they never ran. Gating them on a class stamped from here means the
  // worst case is a page with no animation rather than a page with no
  // content. Stamped with a timer rather than rAF: a document that is not
  // being painted still runs timers, and that is exactly the case this
  // protects against.
  setTimeout(() => document.documentElement.classList.add('js-motion'), 0);
  // The gate paints its own screen, so js-motion is already on and the
  // torii animates. Everything below this line assumes a signed-in user.
  await gate();
  buildShell();
  checkDisplayFont();
  startPetals($('#petals'));

  // The skip link's href is a hash, and hashes are this app's routes -- so
  // letting it navigate would send someone who pressed Tab once straight back
  // to the fleet. It moves focus instead, which is what it was for.
  $('.skiplink')?.addEventListener('click', (e) => {
    e.preventDefault();
    const main = $('#main');
    main?.focus();
    main?.scrollIntoView({ block: 'start' });
  });

  window.addEventListener('hashchange', route);
  let runningBefore = 0;
  onJobsChanged(() => {
    const btn = $('#activitybtn');
    if (!btn) return;
    const n = jobsRunning();
    mount(btn, icon('activity', 14),
      h('span', n ? `Activity · ${n}` : 'Activity'));
    // The companion reacts: mines while something runs, celebrates or winces
    // when the last one ends.
    if (n && !runningBefore) companionDo('mine', 'Working on it…');
    if (!n && runningBefore) {
      const last = activeJobs().filter((j) => j.finishedAt)
        .sort((a, b) => b.finishedAt - a.finishedAt)[0];
      if (last?.status === 'error') companionDo('hurt', 'That one failed.', 3200);
      else companionDo('celebrate', 'Done!', 2800);
    }
    runningBefore = n;
    // A job that creates or destroys a server changes the fleet.
    refreshInstances({ quiet: true }).catch(() => {});
  });

  // Route FIRST. Health is three network round trips and the fleet is one
  // more, and awaiting them before the first paint is how a LAN box spends a
  // second and a half on a blank page — for a banner that, when everything is
  // configured, never appears. The screen draws now and fills in as answers
  // arrive; every view already renders its own loading state.
  paintNav();
  route();

  refreshHealth().then(() => {
    if (state.health?.ready) {
      refreshInstances({ quiet: true }).catch(() => {});
      startFleetPolling();
      startFleetEvents();
    }
  });
  // The racks are a local file read and do not depend on a node answering,
  // so they are asked for straight away rather than behind the health check.
  refreshGroups();
  adoptRunning();

  // Health is cheap and the answer changes when someone fixes their compose
  // file, so it is re-asked rather than being a one-shot at boot.
  setInterval(() => {
    if (document.hidden) return;
    refreshHealth().then(() => {
      if (state.health?.ready && !fleetTimer) startFleetPolling();
      if (state.health?.ready) startFleetEvents();
    });
  }, 60000);
  // Coming back to the tab after a while, the first thing worth knowing is
  // whether anything broke while you were away.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshHealth().catch(() => {});
  });
}

/* A global keyboard route: '/' focuses the nearest search box, which is the
   one shortcut a list-heavy tool genuinely earns. */
// Escape closes the rail drawer. A keyboard user has no scrim to tap, and
// the button that opened it is underneath the open drawer.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.body.classList.contains('rail-open')) {
    setRailOpen(false);
    document.getElementById('railtoggle')?.focus();
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  const box = $('input[type=search], input[data-search]');
  if (box) { e.preventDefault(); box.focus(); box.select?.(); }
});

boot();
