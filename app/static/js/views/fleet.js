/* ============================================================
   Fleet — every server on every node, at a glance.

   The one word each card leads with is `state`, computed on the
   server so the rail, this grid and any activity chip can never
   disagree about what a server is doing. `orphan` is the one
   worth naming: a record whose files have gone, or whose node
   is not answering, fails every request about it, which looks
   like a broken app unless it is said out loud.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, bar, bytes, num,
  empty, skeletonGrid, confirmDialog, segmented, prefs, duration, titleCase,
  mascot, seal, stateInfo, lanternMeter, rope,
} from '../core.js';
import { run } from '../jobs.js';
import {
  state, refreshInstances, refreshGroups, go, topActions, bannerIfUnhealthy,
  onState, onLive, groupOf, motif, lastRan, engineLabel, NEEDS_YOU,
} from '../app.js';
import { may, isAdmin } from '../auth.js';

const LOADER_ART = {
  fabric: '/assets/loader-fabric.png',
  forge: '/assets/loader-forge.png',
  neoforge: '/assets/loader-neoforge.png',
  quilt: '/assets/loader-quilt-light.png',
  vanilla: '/assets/loader-vanilla.svg',
  paper: '/assets/loader-paper.png',
  purpur: '/assets/loader-purpur.png',
  folia: '/assets/loader-folia.webp',
  spigot: '/assets/loader-spigot.png',
  bukkit: '/assets/loader-bukkit.png',
};
export const loaderArt = (loader) => LOADER_ART[(loader || '').toLowerCase()] || null;

/* Why a server is in "Needs you", in one line, and the one fix for it. */
function trouble(s) {
  if (s.state === 'crashed') {
    return { why: 'It stopped on its own. Diagnose reads the crash and names what broke.',
      fix: 'diagnose' };
  }
  if (s.state === 'incomplete') {
    return { why: 'The loader install stopped half-way. Finishing keeps the name, port and '
      + 'anything uploaded.', fix: 'finish' };
  }
  if (s.state === 'orphan') {
    return { why: 'Its node is not answering, or cannot read its folder. The record is '
      + 'kept until you decide.', fix: 'node' };
  }
  if ((s.problems || 0) > 0) {
    return { why: `${s.problems} problem${s.problems > 1 ? 's' : ''} found by the last check.`,
      fix: 'diagnose' };
  }
  return null;
}
const urgent = (s) => NEEDS_YOU.has(s.state) || (s.problems || 0) > 0;

const SORTS = {
  urgent: (a, b) => Number(urgent(b)) - Number(urgent(a))
    || Number(b.state === 'running') - Number(a.state === 'running')
    || String(a.name || '').localeCompare(String(b.name || '')),
  name: (a, b) => String(a.name || '').localeCompare(String(b.name || '')),
  busy: (a, b) => (b.players || 0) - (a.players || 0) || (b.cpu || 0) - (a.cpu || 0),
};

export async function render() {
  const node = h('div.wrap.fleet');
  let filter = prefs.get('fleet.filter', 'all');
  let sort = prefs.get('fleet.sort', 'urgent');
  let hangBy = prefs.get('fleet.hang', 'state');
  let query = '';
  // The rack filter is a SECOND axis, not a fifth segment: "running" and
  // "on the Survival rack" are different questions and you routinely want
  // to ask both at once. Remembered, but validated against the racks that
  // still exist -- a rack deleted from another tab must not leave this
  // screen permanently showing nothing.
  let rackId = prefs.get('fleet.rack', '');
  let unsub = null;
  let unsubLive = null;
  // Committed memory per node: the running servers' limits against its RAM.
  let memory = [];
  // Players across the fleet, last 24 hours (app/history.py).
  let trend = null;
  const live = new Map();     // server_id -> { cpu, ram, players } nodes, patched
  const loadMemory = () => api.get('/api/nodes/memory')
    .then((r) => { memory = r.nodes || []; paint(); }).catch(() => {});
  const loadTrend = () => api.get('/api/fleet/history?hours=24')
    .then((r) => { trend = r; paintTrend(); }).catch(() => {});
  const trendHost = h('div.kpi-spark');

  topActions(
    h('a.btn.sm.ghost', { href: '#/groups' }, icon('layers', 14),
      h('span', 'Racks')),
    may('server.create') ? h('a.btn.sm.primary', { href: '#/create' }, hl(), icon('plus', 14),
      h('span', 'New server')) : null,
  );

  function paintTrend() {
    const pts = (trend?.points || []).map((p) => ({ t: p.t, v: p.online }));
    mount(trendHost, pts.length > 1 ? sparkline(pts, {
      label: `Players over the last day: peak ${trend.peak_online}`,
    }) : null);
  }

  function paint() {
    const banner = bannerIfUnhealthy();
    const all = state.instances;
    const racks = state.groups.groups || [];
    const assign = state.groups.assign || {};
    if (rackId && rackId !== '__yard__'
        && !racks.some((g) => g.id === rackId)) {
      rackId = '';
      prefs.set('fleet.rack', '');
    }
    const inRack = (s) => (rackId === '__yard__'
      ? !assign[s.server_id]
      : assign[s.server_id] === rackId);
    const q = query.trim().toLowerCase();
    const shown = all.filter((s) => {
      if (rackId && !inRack(s)) return false;
      if (q && !`${s.name} ${s.pack?.name || ''} ${s.loader || ''} ${s.minecraft || ''} ${s.port}`
        .toLowerCase().includes(q)) return false;
      if (filter === 'all') return true;
      if (filter === 'running') return s.state === 'running';
      if (filter === 'stopped') return s.state !== 'running';
      if (filter === 'attention') return urgent(s);
      return true;
    }).sort(SORTS[sort] || SORTS.urgent);

    const counts = {
      running: all.filter((s) => s.state === 'running').length,
      players: all.reduce((n, s) => n + (s.running ? (s.players || 0) : 0), 0),
      attention: all.filter(urgent).length,
      mods: all.reduce((n, s) => n + (s.mod_count || 0), 0),
    };
    const nodes = state.health?.checks?.engine;
    live.clear();

    mount(node,
      banner,
      h('header.fleet-head',
        h('div.fh-words',
          h('p.eyebrow', `${engineLabel(nodes?.driver || state.health?.config?.engine)} · `,
            h('b', nodes?.ok ? `${nodes.nodes} node${nodes.nodes === 1 ? '' : 's'}`
              : 'not answering')),
          h('h1.brush.fh-title', 'The fleet'),
          h('p.lede', all.length
            ? `${all.length} server${all.length === 1 ? '' : 's'}, ${counts.running} up, `
              + `${counts.players} player${counts.players === 1 ? '' : 's'} online. `
              + 'Anything that crashed, stalled or lost its node hangs first.'
            : 'Nothing hangs here yet.')),
        mascot('fox-pounce', 92, { cls: 'fh-fox' })),

      all.length ? h('div.kpis.fleet-kpis.stagger',
        kpi('Servers', String(all.length), `${counts.running} up · ${all.length - counts.running} down`,
          '', 'server'),
        kpi('Players online', String(counts.players),
          counts.running ? 'across running servers' : 'nothing is up', '', 'users', trendHost),
        kpi('Mods managed', num(counts.mods) === '—' ? '0' : num(counts.mods),
          'recorded in instance manifests', '', 'box'),
        kpi('Needs you', String(counts.attention),
          counts.attention ? 'crashed, unfinished, lost or flagged' : 'all clear',
          counts.attention ? 'bad' : 'ok', 'bell', null, counts.attention > 0),
        ...memory.filter((m) => m.total_mb).map((m) => kpi(
          memory.length > 1 ? `Memory · ${m.node}` : 'Memory committed',
          `${(m.committed_mb / 1024).toFixed(1)} / ${(m.total_mb / 1024).toFixed(0)} GB`,
          m.committed_mb > m.total_mb ? 'more than the machine has'
            : 'running servers’ limits',
          m.committed_mb > m.total_mb ? 'warn' : '', 'cpu')),
      ) : null,

      needsBand(all.filter(urgent)),

      all.length ? h('div.fleet-bar',
        segmented([
          { value: 'all', label: `All ${all.length}` },
          { value: 'running', label: `Up ${counts.running}` },
          { value: 'stopped', label: `Down ${all.length - counts.running}` },
          { value: 'attention', label: `Needs you ${counts.attention}` },
        ], filter, (v) => { filter = v; prefs.set('fleet.filter', v); paint(); },
          'Filter servers'),
        h('label.fleet-search',
          icon('search', 15),
          h('input', {
            type: 'search', value: query, placeholder: 'Name, pack, loader or port',
            'data-keep': 'fleet-search', 'aria-label': 'Search the fleet',
            oninput: (e) => { query = e.target.value; paint(); },
          })),
        segmented([
          { value: 'urgent', label: 'Urgent first' },
          { value: 'name', label: 'A–Z' },
          { value: 'busy', label: 'Busiest' },
        ], sort, (v) => { sort = v; prefs.set('fleet.sort', v); paint(); }, 'Sort'),
        racks.length ? segmented([
          { value: 'state', label: 'By state' },
          { value: 'rack', label: 'By rack' },
        ], hangBy, (v) => { hangBy = v; prefs.set('fleet.hang', v); paint(); }, 'Hang by')
          : null,
      ) : null,

      racks.length ? h('div.rackbar', { role: 'group', 'aria-label': 'Rack' },
        h('span.rb-lbl.mono', 'Racks'),
        rackChip('', 'All', all.length, null, rackId, (v) => {
          rackId = v; prefs.set('fleet.rack', v); paint();
        }),
        ...racks.map((g) => rackChip(g.id, g.name,
          all.filter((s) => assign[s.server_id] === g.id).length,
          g.motif, rackId, (v) => {
            rackId = v; prefs.set('fleet.rack', v); paint();
          })),
        rackChip('__yard__', 'Open yard',
          all.filter((s) => !assign[s.server_id]).length, 'yuki', rackId,
          (v) => { rackId = v; prefs.set('fleet.rack', v); paint(); }),
        h('a.rb-more', { href: '#/groups' }, 'arrange…'),
      ) : null,

      ...portGroups(all),

      !all.length
        ? (state.health?.ready
          ? empty('placeblock', 'No servers yet',
            'Raise one from a loader and a version, install a modpack from the '
            + 'catalogue, or bring a CurseForge export you already have.',
            may('server.create')
              ? h('a.btn.primary', { href: '#/create' }, hl(), h('span', 'New server')) : null,
            h('a.btn', { href: '#/discover' }, hl(), h('span', 'Discover a modpack')))
          : empty('sneakwalk', 'No node is answering yet',
            'BlessForge cannot reach the node that runs the servers. '
            + 'Settings lists exactly what is missing.',
            h('a.btn.primary', { href: isAdmin() ? '#/gate/nodes' : '#/settings' },
              hl(), h('span', isAdmin() ? 'Check the nodes' : 'Settings'))))
        : shown.length
          ? groups(shown, hangBy, racks, assign)
          : h('div.note', rackId
            ? 'Nothing on that rack matches that filter.'
            : 'Nothing matches that.'),
    );
  }

  /* The servers hang in groups from ropes: "Needs you" first on a red cord,
     then what is up, then what is resting -- or one rope per rack. */
  function groups(list, by, racks, assign) {
    const sets = [];
    if (by === 'rack' && racks.length) {
      for (const g of racks) {
        const members = list.filter((s) => assign[s.server_id] === g.id);
        if (members.length) sets.push({ title: g.name, glyph: motif(g.motif).glyph, members });
      }
      const yard = list.filter((s) => !assign[s.server_id]);
      if (yard.length) sets.push({ title: 'The open yard', glyph: '庭', members: yard });
    } else {
      const bad = list.filter(urgent);
      const up = list.filter((s) => !urgent(s) && s.state === 'running');
      const rest = list.filter((s) => !urgent(s) && s.state !== 'running');
      if (bad.length) sets.push({ title: 'Needs you', glyph: '急', members: bad, kind: 'alarm' });
      if (up.length) sets.push({ title: 'Lit', glyph: '灯', members: up });
      if (rest.length) sets.push({ title: 'Resting', glyph: '休', members: rest, kind: 'quiet' });
    }
    return h('div.ropes', ...sets.map((set) => h(`section.ropegroup${set.kind ? `.${set.kind}` : ''}`,
      { 'aria-label': set.title },
      h('div.rg-tag', h('b', { 'aria-hidden': 'true' }, set.glyph), h('span', set.title),
        h('small', String(set.members.length))),
      rope(set.kind === 'alarm' ? 'alarm' : ''),
      h('div.plaques', ...set.members.map((s, i) => plaque(s, i))))));
  }

  function plaque(s, i) {
    const art = loaderArt(s.loader);
    const st = stateInfo(s.state);
    const open = () => go(`/i/${s.server_id}/overview`);
    const g = groupOf(s.server_id);
    const t = trouble(s);
    const up = s.state === 'running';
    const cpu = h('span.pv');
    const ram = h('span.pv');
    const players = h('span.pv');
    const cpuMeter = lanternMeter(up ? s.cpu : null);
    const ramMeter = lanternMeter(up ? s.mem : null, { warn: 70, hot: 88 });
    live.set(s.server_id, { cpu, ram, players, cpuMeter, ramMeter });

    const el = h(`article.ema-card.st-${s.state || 'stopped'}`, {
      role: 'link', tabIndex: 0, 'aria-label': `${s.name || 'Server'}: ${st.label}. Open it.`,
      onclick: (e) => { if (!e.target.closest('button,a')) open(); },
      onkeydown: (e) => {
        if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
          e.preventDefault(); open();
        }
      },
    },
      h('div.ema-face', h('div.ema-in',
      h('div.ema-top',
        h(`span.ema-lamp.${up ? 'lit' : ''}`, { 'aria-hidden': 'true' }),
        h('div.ema-id',
          h('h3.brush', s.name || s.server_id.slice(0, 8)),
          h('div.ema-meta',
            art ? h('img', { src: art, alt: '', width: 16, height: 16, loading: 'lazy' })
              : icon('box', 14),
            h('span', [titleCase(s.loader || ''), s.minecraft].filter(Boolean).join(' ')
              || 'unmanaged'),
            s.port ? h('span.mono', `:${s.port}`) : null)),
        seal(s.state || 'stopped', { size: 'sm' })),
      h('div.ema-pack.trunc', { title: s.pack?.name || '' },
        s.pack?.name ? `${s.pack.name}${s.pack.version ? ` ${s.pack.version}` : ''}`
          : s.managed === false ? 'not created by BlessForge' : 'no modpack'),
      t ? h(`div.ema-why.${s.state}`, t.why) : null,
      s.node_down ? h('div.ema-why.orphan', 'Its node is not answering: this is the last '
        + 'reading it sent.') : null,
      s.state === 'orphan' ? null : h('div.ema-meters',
        h('div.pm', { title: s.cpu_allowance && s.cpu_cores != null
          ? `${s.cpu_cores.toFixed(1)} of the ${s.cpu_allowance} cores it may use are busy`
          : 'A share of the cores this server may use' },
        h('span.pl', 'CPU'), cpuMeter, cpu),
        h('div.pm', { title: 'Its memory, against its own limit' },
          h('span.pl', 'RAM'), ramMeter, ram),
        h('div.pm', h('span.pl', 'Players'), players)),
      h('div.ema-foot',
        h('span.mono', lastRan(s)),
        (s.mod_count || 0) > 0 ? h('span.mono', `${s.mod_count} ${PLUGIN_LOADERS.has(
          (s.loader || '').toLowerCase()) ? 'plugins' : 'mods'}`) : null,
        h('button.racktag', {
          type: 'button',
          'aria-label': g ? `${s.name} is on the ${g.name} rack. Move it.`
            : `${s.name} is not on a rack. Hang it.`,
          onclick: (e) => { e.stopPropagation(); hang(s); },
        }, h('span.rt-g', { 'aria-hidden': 'true' }, g ? motif(g.motif).glyph : '—'),
        h('span.trunc', g ? g.name : 'open yard'))),
      h('div.ema-acts', ...actionsFor(s)),
    )));
    if (g) el.style.setProperty('--g-ink', motif(g.motif).ink);
    // Each plaque sways out of step with its neighbours. A custom property
    // has to go through setProperty; Object.assign on `style` drops it.
    el.style.setProperty('--sway-delay', `-${((i * 1.37) % 5).toFixed(2)}s`);
    patchOne(s);
    return el;
  }

  /* Live numbers are written into the plaque, never rebuilt -- a stats event
     every few seconds must not restart every plaque's sway. */
  function patchOne(s) {
    const v = live.get(s.server_id);
    if (!v) return;
    const up = s.state === 'running';
    setMeter(v.cpuMeter, up ? s.cpu : null, 60, 85);
    setMeter(v.ramMeter, up ? s.mem : null, 70, 88);
    v.cpu.textContent = up && s.cpu !== null && s.cpu !== undefined
      ? `${Math.round(s.cpu)}%` : '—';
    v.ram.textContent = up && s.mem_bytes ? bytes(s.mem_bytes) : '—';
    v.players.textContent = up && s.players !== null && s.players !== undefined
      ? `${s.players} / ${s.max_players ?? '—'}` : '—';
  }

  if (!state.instances.length && state.health?.ready) {
    mount(node, skeletonGrid(6));
    await refreshInstances({ quiet: true }).catch(() => {});
  }
  refreshGroups();
  paint();
  loadMemory();
  loadTrend();
  unsub = onState(() => { paint(); paintTrend(); });
  unsubLive = onLive(() => { state.instances.forEach(patchOne); });
  const memTimer = setInterval(loadMemory, 20000);
  const trendTimer = setInterval(loadTrend, 300000);

  return {
    node,
    dispose: () => {
      unsub?.(); unsubLive?.(); clearInterval(memTimer); clearInterval(trendTimer);
    },
  };
}

const PLUGIN_LOADERS = new Set(['paper', 'purpur', 'folia', 'spigot', 'bukkit']);

/* Each state's buttons: what you would want to do next, and nothing else. */
function actionsFor(s) {
  const stop = (e) => e.stopPropagation();
  const enter = h('a.btn.sm.ghost', { href: `#/i/${s.server_id}/overview`, onclick: stop },
    h('span', 'Enter'), icon('arrowRight', 13));
  const powerOk = may('server.power', s.server_id);
  if (s.state === 'incomplete') {
    return [h('button.btn.sm.gold', {
      onclick: (e) => { stop(e); retryLoader(s); }, disabled: !may('mods.manage', s.server_id),
    }, hl(), icon('wrench', 13), h('span', 'Finish setup')), enter];
  }
  if (s.state === 'orphan') {
    return [isAdmin() ? h('a.btn.sm', { href: '#/gate/nodes', onclick: stop }, hl(),
      icon('node', 13), h('span', 'Check the node')) : null, enter];
  }
  if (s.state === 'crashed') {
    return [h('a.btn.sm.danger', { href: `#/i/${s.server_id}/diagnose`, onclick: stop },
      hl(), icon('wrench', 13), h('span', 'Diagnose')),
    h('button.btn.sm', { disabled: !powerOk, onclick: (e) => { stop(e); action(s, 'start_server'); } },
      hl(), icon('play', 13), h('span', 'Start')), enter];
  }
  if (s.state === 'running') {
    return [h('button.btn.sm.ghost', {
      disabled: !powerOk, onclick: (e) => { stop(e); action(s, 'stop_server'); },
    }, icon('stop', 13), h('span', 'Stop')),
    h('button.btn.icon.sm.ghost', {
      'aria-label': `Restart ${s.name}`, disabled: !powerOk,
      onclick: (e) => { stop(e); action(s, 'restart_server'); },
    }, icon('restart', 14)), enter];
  }
  return [h('button.btn.sm.primary', {
    disabled: !powerOk, onclick: (e) => { stop(e); action(s, 'start_server'); },
  }, hl(), icon('play', 13), h('span', 'Start')), enter];
}

/* The "Needs you" band: one line per server in trouble and its one fix. */
function needsBand(list) {
  if (!list.length) return null;
  return h('section.needs-band', { 'aria-label': 'Needs you' },
    h('div.nb-head',
      mascot('surprise', 64, { cls: 'nb-fox' }),
      h('h2.brush', list.length === 1 ? 'One thing needs you'
        : `${['', '', 'Two', 'Three', 'Four', 'Five'][list.length] || list.length} things need you`)),
    h('div.nb-rows', ...list.map((s) => {
      const t = trouble(s) || { why: '', fix: 'diagnose' };
      const fix = t.fix === 'finish'
        ? h('button.btn.sm.gold', { onclick: () => retryLoader(s),
          disabled: !may('mods.manage', s.server_id) },
        hl(), icon('wrench', 13), h('span', 'Finish setup'))
        : t.fix === 'node'
          ? (isAdmin() ? h('a.btn.sm', { href: '#/gate/nodes' }, hl(), icon('node', 13),
            h('span', 'Check the node')) : null)
          : h('a.btn.sm.primary', { href: `#/i/${s.server_id}/diagnose` }, hl(),
            icon('wrench', 13), h('span', 'Diagnose'));
      return h('div.nb-row',
        seal(s.state || 'stopped', { size: 'sm', label: false }),
        h('div.nb-what',
          h('a.nb-name', { href: `#/i/${s.server_id}/overview` }, s.name || s.server_id),
          h('p', t.why)),
        h('div.btnrow', fix,
          s.state === 'crashed' && may('server.power', s.server_id)
            ? h('button.btn.sm.ghost', { onclick: () => action(s, 'start_server') },
              icon('play', 13), h('span', 'Start')) : null));
    })));
}

/* A small line of the last day, drawn by hand: no chart library for twenty
   points. The newest point is marked; the area under it is a sakura wash. */
export function sparkline(points, { width = 160, height = 34, label = '' } = {}) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('class', 'spark');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', label);
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const max = Math.max(1, ...points.map((p) => p.v || 0));
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * (width - 4) + 2;
  const y = (v) => height - 3 - ((v || 0) / max) * (height - 8);
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  const area = `${line}L${x(t1).toFixed(1)},${height}L${x(t0).toFixed(1)},${height}Z`;
  const a = document.createElementNS(NS, 'path');
  a.setAttribute('d', area);
  a.setAttribute('class', 'spark-area');
  const l = document.createElementNS(NS, 'path');
  l.setAttribute('d', line);
  l.setAttribute('class', 'spark-line');
  const last = points[points.length - 1];
  const dot = document.createElementNS(NS, 'circle');
  dot.setAttribute('cx', x(last.t).toFixed(1));
  dot.setAttribute('cy', y(last.v).toFixed(1));
  dot.setAttribute('r', '2.6');
  dot.setAttribute('class', 'spark-dot');
  svg.append(a, l, dot);
  return svg;
}

function setMeter(meter, value, warn, hot) {
  const v = value === null || value === undefined || Number.isNaN(Number(value))
    ? null : Math.max(0, Math.min(100, Number(value)));
  const cells = [...meter.children];
  const lit = v === null ? 0 : Math.max(v > 0 ? 1 : 0, Math.round((v / 100) * cells.length));
  cells.forEach((c, i) => c.classList.toggle('on', i < lit));
  const tone = v === null ? 'unknown' : v >= hot ? 'hot' : v >= warn ? 'warm' : 'calm';
  for (const t of ['unknown', 'hot', 'warm', 'calm']) meter.classList.toggle(t, t === tone);
  if (v === null) meter.removeAttribute('aria-valuenow');
  else meter.setAttribute('aria-valuenow', String(Math.round(v)));
}

function rackChip(value, label, count, motifName, current, onpick) {
  const chip = h('button.chip.rackchip', {
    type: 'button',
    'aria-pressed': String(current === value),
    onclick: () => onpick(current === value && value ? '' : value),
  },
    motifName
      ? h('span.rc-g', { 'aria-hidden': 'true' }, motif(motifName).glyph)
      : null,
    h('span', label),
    h('span.rc-n.tnum', String(count)),
  );
  if (motifName) chip.style.setProperty('--g-ink', motif(motifName).ink);
  return chip;
}

function kpi(label, value, sub, tone = '', glyph = '', extra = null, ring = false) {
  return h(`div.kpi${ring ? '.ringing' : ''}`,
    glyph ? h('span.kpi-ico', { 'aria-hidden': 'true' }, icon(glyph, 16)) : null,
    h('div.lbl', label),
    h(`div.v${tone ? `.${tone}` : ''}.tnum`, value),
    sub && h('div.sub', sub),
    extra);
}

/* The rack picker lives on the Racks screen and is loaded on demand, so a
   fleet that nobody groups never downloads it. One implementation either
   way -- the alternative is two dialogs that drift apart. */
async function hang(s) {
  try {
    const mod = await import('./groups.js');
    await mod.openHangPicker([s]);
  } catch (e) {
    toastError(e, 'Could not open the rack picker');
  }
}

/* Servers sharing one port -- one address, switched between, one running
   at a time: a line per port, saying which is up. */
function portGroups(all) {
  const byPort = new Map();
  for (const s of all) {
    if (!s.port) continue;
    byPort.set(s.port, [...(byPort.get(s.port) || []), s]);
  }
  return [...byPort].filter(([, list]) => list.length > 1)
    .sort(([a], [b]) => a - b)
    .map(([port, list]) => h('div.note', { style: { marginBottom: '10px' } },
      h('b', `Port ${port}: `),
      list.map((s) => `${s.name} ${s.state === 'running' ? '● running' : '○'}`)
        .join(', ')));
}

export async function action(s, act) {
  const verb = { start_server: 'Starting', stop_server: 'Stopping',
    restart_server: 'Restarting', kill_server: 'Killing',
    switch_server: 'Switching to' }[act];
  if (act === 'stop_server' && s.players > 0) {
    const ok = await confirmDialog({
      title: `Stop ${s.name}?`,
      message: `${s.players} player${s.players > 1 ? 's are' : ' is'} connected. `
        + 'Stopping saves the world and disconnects them.',
      confirmLabel: 'Stop anyway',
      danger: true,
    });
    if (!ok) return;
  }
  try {
    const res = await api.post(`/api/instances/${s.server_id}/action/${act}`);
    toast(res.why || `${verb} ${s.name}…`, res.upgraded ? 'warn' : 'ok');
    setTimeout(() => refreshInstances({ quiet: true }), 1200);
    setTimeout(() => refreshInstances({ quiet: true }), 5000);
  } catch (e) {
    // Past the machine's memory: a warning to confirm, not a refusal.
    if (e?.status === 409 && e?.body?.overcommit && act === 'start_server') {
      const ok = await confirmDialog({
        title: `Start ${s.name} anyway?`,
        message: e.message,
        confirmLabel: 'Start anyway',
      });
      if (ok) {
        try {
          const res = await api.post(
            `/api/instances/${s.server_id}/action/${act}?confirm=true`);
          toast(res.why || `Starting ${s.name}…`, 'ok');
          setTimeout(() => refreshInstances({ quiet: true }), 1200);
        } catch (err) { toastError(err, `Could not start ${s.name}`); }
      }
      return;
    }
    // A fixed port another app holds: offer the next free one.
    const nextFree = e?.body?.next_free;
    if (e?.status === 409 && nextFree && act === 'start_server') {
      const ok = await confirmDialog({
        title: `Port ${e.body.taken} is taken`,
        message: `${e.message} Use the next free port (${nextFree}) for ${s.name}?`,
        confirmLabel: `Use port ${nextFree}`,
      });
      if (ok) {
        try {
          await api.post(`/api/instances/${s.server_id}/port`, { port: nextFree });
          await action(s, 'start_server');
        } catch (err) { toastError(err); }
      }
      return;
    }
    // Another server holds the port: offer to switch rather than only fail.
    const other = e?.body?.conflict;
    if (e?.status === 409 && other && act !== 'switch_server') {
      const ok = await confirmDialog({
        title: `Switch to ${s.name}?`,
        message: `${other.name} is running on port ${other.port}, which ${s.name} `
          + `uses too. Switching stops ${other.name} the normal way (it saves `
          + `its world) and then starts ${s.name}.`,
        confirmLabel: 'Switch to this server',
      });
      if (ok) await action(s, 'switch_server');
      return;
    }
    toastError(e, `Could not ${act.split('_')[0]} ${s.name}`);
  }
}

export function retryLoader(s) {
  return run(`/api/instances/${s.server_id}/loader/reinstall`, {}, {
    title: `Finish setting up ${s.name}`,
    onEnd: (rec) => {
      if (rec.status === 'done') {
        toast('The loader is installed. This server can start now.', 'ok');
        refreshInstances({ quiet: true });
      }
    },
  });
}
