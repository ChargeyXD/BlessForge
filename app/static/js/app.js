/* ============================================================
   app — the shell, the router, and boot.

   Routes are hashes so the whole thing is one static document
   that any reverse proxy will serve without rewrite rules:

     #/                     the fleet
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
  readTheme, applyTheme, prefs, loadingFox, empty, pill,
} from './core.js';
import { adoptRunning, onJobsChanged, jobsRunning, openDrawer, activeJobs } from './jobs.js';
import { startPetals } from './petals.js';

/* --- shared application state -------------------------------- */

export const state = {
  health: null,
  instances: [],
  instanceById: new Map(),
  route: { name: 'fleet', params: {} },
  loadersCatalogue: null,
  aiStatus: null,
};

const subscribers = new Set();
export function onState(fn) { subscribers.add(fn); return () => subscribers.delete(fn); }
function emit() { subscribers.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } }); }

/* --- routing -------------------------------------------------- */

const routes = [
  { name: 'fleet', pattern: /^\/?$/, load: () => import('./views/fleet.js') },
  { name: 'create', pattern: /^\/create$/, load: () => import('./views/create.js') },
  { name: 'discover', pattern: /^\/discover$/, load: () => import('./views/discover.js') },
  { name: 'roulette', pattern: /^\/roulette$/, load: () => import('./views/roulette.js') },
  { name: 'settings', pattern: /^\/settings$/, load: () => import('./views/settings.js') },
  {
    name: 'instance',
    pattern: /^\/i\/([^/]+)(?:\/([^/]+))?$/,
    load: () => import('./views/instance.js'),
    params: (m) => ({ id: m[1], tab: m[2] || 'overview' }),
  },
];

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
  mount(main, loadingFox('Loading'));
  document.body.classList.remove('rail-open');

  let mod;
  try {
    mod = await def.load();
  } catch (e) {
    console.error(e);
    mount(main, h('div.wrap',
      empty('chargey-failed.png', 'That screen would not load',
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
    currentView = view || null;
    if (view?.node) mount(main, view.node);
    main.scrollTop = 0;
    window.scrollTo({ top: 0, behavior: 'instant' });
  } catch (e) {
    console.error(e);
    if (token !== routeToken) return;
    mount(main, h('div.wrap',
      empty('chargey-failed.png', 'This screen could not be drawn',
        e.message || String(e),
        h('button.btn.primary', { onclick: () => route() },
          hl(), h('span', 'Try again')))));
  }
}

/* --- the fleet, polled ---------------------------------------- */

let fleetTimer = null;

export async function refreshInstances({ quiet } = {}) {
  try {
    const data = await api.get('/api/instances');
    state.instances = data.items || [];
    state.instanceById = new Map(state.instances.map((i) => [i.server_id, i]));
    paintFleetRail();
    emit();
    return state.instances;
  } catch (e) {
    if (!quiet) toastError(e, 'Could not read the fleet');
    throw e;
  }
}

function startFleetPolling() {
  const tick = () => {
    // Polling a Crafty that is not there just fills the log with failures,
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

const NAV = [
  { name: 'fleet', path: '/', label: 'Fleet', icon: 'server' },
  { name: 'create', path: '/create', label: 'New server', icon: 'plus' },
  { name: 'discover', path: '/discover', label: 'Discover', icon: 'compass' },
  { name: 'roulette', path: '/roulette', label: 'Mod Roulette', icon: 'dice' },
  { name: 'settings', path: '/settings', label: 'Settings', icon: 'gear' },
];

function lantern(glyph, second) {
  return h(`div.lantern${second ? '.l2' : ''}`, { 'aria-hidden': 'true' },
    h('div.cord'),
    h('div.body', h('div.glyph', glyph)));
}

function buildRail() {
  return h('nav#rail', { 'aria-label': 'Primary' },
    h('a.rail-brand', { href: '#/' },
      h('img', { src: '/assets/appmark-fox.png', alt: '' }),
      h('span',
        h('span.wm', 'BlessForge'),
        h('span.tag', 'Shrine of servers')),
    ),
    h('div.rail-lanterns', lantern('森'), lantern('狐', true)),
    h('div.rail-nav', { id: 'railnav' }),
    h('div.rail-fleet', { id: 'railfleet', 'aria-label': 'Servers' }),
    h('div.rail-foot',
      h('button.btn.icon.sm.ghost', {
        id: 'themebtn', 'aria-label': 'Switch between light and dark',
        onclick: cycleTheme,
      }, icon(readTheme() === 'light' ? 'sun' : 'moon', 15)),
      h('button.btn.sm.ghost', {
        id: 'activitybtn', onclick: () => openDrawer(),
      }, icon('activity', 14), h('span', 'Activity')),
      h('span.pill.ghost', { id: 'healthpill' }, 'checking'),
    ),
  );
}

function paintNav() {
  const host = $('#railnav');
  if (!host) return;
  mount(host,
    h('div.sec', 'BlessForge'),
    ...NAV.map((n) => h('a.navlink', {
      href: `#${n.path}`,
      'aria-current': state.route.name === n.name ? 'page' : null,
    }, hl(), icon(n.icon, 17), h('span', n.label),
      n.name === 'fleet' && state.instances.length
        ? h('span.count', String(state.instances.length)) : null)),
  );
  paintTopbar();
}

function paintFleetRail() {
  const host = $('#railfleet');
  if (!host) return;
  if (!state.instances.length) {
    mount(host, h('div.sec', 'No servers yet'));
    return;
  }
  const openId = state.route.name === 'instance' ? state.route.params.id : null;
  mount(host,
    h('div.sec', `Servers · ${state.instances.length}`),
    ...state.instances.map((s) => h('button.fleetrow', {
      type: 'button',
      'aria-current': s.server_id === openId ? 'true' : null,
      onclick: () => go(`/i/${s.server_id}/overview`),
    },
      hl(),
      h(`span.state.${s.state || 'stopped'}`, {
        'aria-hidden': 'true',
        title: s.state || 'stopped',
      }),
      h('span.nm', s.name || s.server_id.slice(0, 8),
        h('span.meta', [
          s.loader ? s.loader : null,
          s.minecraft || null,
          s.running && s.players !== null && s.players !== undefined
            ? `${s.players} online` : null,
        ].filter(Boolean).join(' · ') || 'unmanaged')),
    )),
  );
  paintNav();
}

function paintTopbar() {
  const crumbs = $('#crumbs');
  if (!crumbs) return;
  const { name, params } = state.route;
  const labels = {
    fleet: ['Fleet', 'every server Crafty knows about'],
    create: ['New server', 'a loader, a version, an empty mods folder'],
    discover: ['Discover', 'modpacks, mods and plugins'],
    roulette: ['Mod Roulette', 'deal a pack you did not choose'],
    settings: ['Settings', 'connections, decisions, cache'],
  };
  if (name === 'instance') {
    const inst = state.instanceById.get(params.id);
    mount(crumbs,
      h('a.btn.icon.sm.ghost', { href: '#/', 'aria-label': 'Back to the fleet' },
        icon('arrowLeft', 15)),
      h('span.t', inst?.name || 'Instance'),
      h('span.s', (params.tab || 'overview').replace(/-/g, ' ')));
  } else {
    const [t, s] = labels[name] || ['BlessForge', ''];
    mount(crumbs, h('span.t', t), s && h('span.s', s));
  }
}

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
  el.className = `pill ${ok ? 'ok' : 'bad'}`;
  clear(el);
  el.append(h('span.dot.live', { 'aria-hidden': 'true' }),
    ok ? 'connected' : 'setup needed');
  el.title = ok
    ? `Crafty answered in ${hs.checks?.crafty?.latency_ms ?? '?'} ms`
    : (hs?.checks?.crafty?.error || 'BlessForge cannot reach Crafty');
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
    h('div#shell',
      buildRail(),
      h('div#col',
        h('header#topbar',
          h('button.btn.icon.sm.ghost', {
            id: 'railtoggle', 'aria-label': 'Show the server list',
            style: { display: 'none' },
            onclick: () => document.body.classList.toggle('rail-open'),
          }, icon('menu', 16)),
          h('div.crumbs', { id: 'crumbs' }),
          h('div.btnrow', { id: 'topactions' }),
        ),
        h('main#main', { tabIndex: -1 }),
      ),
    ),
  );
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

async function boot() {
  applyTheme(readTheme());
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
  onJobsChanged(() => {
    const btn = $('#activitybtn');
    if (!btn) return;
    const n = jobsRunning();
    mount(btn, icon('activity', 14),
      h('span', n ? `Activity · ${n}` : 'Activity'));
    // A job that creates or destroys a server changes the fleet.
    refreshInstances({ quiet: true }).catch(() => {});
  });

  await refreshHealth();
  paintNav();
  route();

  if (state.health?.ready) {
    refreshInstances({ quiet: true }).catch(() => {});
    startFleetPolling();
  }
  adoptRunning();

  // Health is cheap and the answer changes when someone fixes their compose
  // file, so it is re-asked rather than being a one-shot at boot.
  setInterval(() => {
    if (document.hidden) return;
    refreshHealth().then(() => {
      if (state.health?.ready && !fleetTimer) startFleetPolling();
    });
  }, 60000);
}

/* A global keyboard route: '/' focuses the nearest search box, which is the
   one shortcut a list-heavy tool genuinely earns. */
document.addEventListener('keydown', (e) => {
  if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  const box = $('input[type=search], input[data-search]');
  if (box) { e.preventDefault(); box.focus(); box.select?.(); }
});

boot();
