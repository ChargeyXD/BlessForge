/* ============================================================
   Fleet — every server Crafty knows about, at a glance.

   The one word each card leads with is `state`, computed on the
   server so the rail, this grid and any activity chip can never
   disagree about what a server is doing. `orphan` is the one
   worth naming: Crafty keeps a record for a server whose files
   have gone and then fails every request about it forever, which
   looks like a broken app unless it is said out loud.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, bar, bytes, num,
  empty, skeletonGrid, confirmDialog, segmented, prefs, duration,
} from '../core.js';
import { run } from '../jobs.js';
import {
  state, refreshInstances, go, topActions, bannerIfUnhealthy, onState,
} from '../app.js';

const LOADER_ART = {
  fabric: '/assets/loader-fabric.png',
  forge: '/assets/loader-forge.jpg',
  neoforge: '/assets/loader-neoforge.png',
  vanilla: '/assets/loader-vanilla.svg',
  paper: '/assets/loader-paper.png',
  purpur: '/assets/loader-purpur.png',
  folia: '/assets/loader-paper.png',
};

const STATE_PILL = {
  running: ['ok', 'running'],
  stopped: ['dead', 'stopped'],
  crashed: ['bad', 'crashed'],
  orphan: ['plum', 'orphaned'],
  incomplete: ['warn', 'half-finished'],
};

export async function render() {
  const node = h('div.wrap');
  let filter = prefs.get('fleet.filter', 'all');
  let unsub = null;

  topActions(
    h('a.btn.sm.ghost', { href: '#/discover' }, icon('compass', 14),
      h('span', 'Discover')),
    h('a.btn.sm.primary', { href: '#/create' }, hl(), icon('plus', 14),
      h('span', 'New server')),
  );

  function paint() {
    const banner = bannerIfUnhealthy();
    const all = state.instances;
    const shown = all.filter((s) => {
      if (filter === 'all') return true;
      if (filter === 'running') return s.state === 'running';
      if (filter === 'stopped') return s.state === 'stopped';
      if (filter === 'attention') {
        return ['crashed', 'orphan', 'incomplete'].includes(s.state)
          || (s.problems || 0) > 0;
      }
      return true;
    });

    const counts = {
      running: all.filter((s) => s.state === 'running').length,
      players: all.reduce((n, s) => n + (s.players || 0), 0),
      attention: all.filter((s) => ['crashed', 'orphan', 'incomplete']
        .includes(s.state)).length,
      mods: all.reduce((n, s) => n + (s.mod_count || 0), 0),
    };

    mount(node,
      banner,
      h('div.sec-head.mon',
        h('div',
          h('p.eyebrow', 'Crafty Controller · ',
            h('b', state.health?.config?.crafty_url || 'not configured')),
          h('h2', 'The fleet')),
        h('div.grow'),
        segmented([
          { value: 'all', label: `All ${all.length}` },
          { value: 'running', label: `Up ${counts.running}` },
          { value: 'stopped', label: 'Down' },
          { value: 'attention', label: `Needs you ${counts.attention || ''}`.trim() },
        ], filter, (v) => { filter = v; prefs.set('fleet.filter', v); paint(); },
          'Filter servers'),
      ),

      all.length ? h('div.kpis.stagger', { style: { marginBottom: '20px' } },
        kpi('Servers', String(all.length), `${counts.running} running`),
        kpi('Players online', String(counts.players),
          counts.running ? 'across running servers' : 'nothing is up'),
        kpi('Mods managed', num(counts.mods) === '—' ? '0' : num(counts.mods),
          'recorded in instance manifests'),
        kpi('Needs attention', String(counts.attention),
          counts.attention ? 'crashed, orphaned or half-finished' : 'all clear',
          counts.attention ? 'bad' : 'ok'),
      ) : null,

      !all.length
        ? (state.health?.ready
          ? empty('fox-mascot.png', 'No servers yet',
            'Crafty has no instances. Create an empty one with just a loader '
            + 'and a version, or install a modpack from the catalogue.',
            h('a.btn.primary', { href: '#/create' }, hl(), h('span', 'Create a server')),
            h('a.btn', { href: '#/discover' }, hl(), h('span', 'Browse modpacks')))
          : empty('chargey-peek.png', 'Not connected to Crafty yet',
            'Set CRAFTY_URL and CRAFTY_TOKEN, then reload. Settings lists '
            + 'exactly what is missing.',
            h('a.btn.primary', { href: '#/settings' }, hl(), h('span', 'Settings'))))
        : shown.length
          ? h('div.grid.gauto.bleed.stagger', ...shown.map(card))
          : h('div.note', 'Nothing matches that filter.'),
    );
  }

  if (!state.instances.length && state.health?.ready) {
    mount(node, skeletonGrid(6));
    await refreshInstances({ quiet: true }).catch(() => {});
  }
  paint();
  unsub = onState(paint);

  return { node, dispose: () => unsub?.() };
}

function kpi(label, value, sub, tone = '') {
  return h('div.kpi',
    h('div.lbl', label),
    h(`div.v${tone ? `.${tone}` : ''}.tnum`, value),
    sub && h('div.sub', sub));
}

function card(s) {
  const [tone, label] = STATE_PILL[s.state] || ['dead', s.state || 'unknown'];
  const art = LOADER_ART[(s.loader || '').toLowerCase()];
  const open = () => go(`/i/${s.server_id}/overview`);

  return h('div.card.hoverable.svcard', {
    role: 'link', tabIndex: 0,
    'aria-label': `Open ${s.name || 'server'}`,
    onclick: (e) => { if (!e.target.closest('button')) open(); },
    onkeydown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        if (e.target !== e.currentTarget) return;
        e.preventDefault(); open();
      }
    },
    style: { cursor: 'pointer' },
  },
    h('div.top',
      h('div.loader-art', art
        ? h('img', { src: art, alt: '', loading: 'lazy' })
        : icon(s.loader ? 'puzzle' : 'box', 16)),
      h('h3', s.name || s.server_id.slice(0, 8)),
      pill(label, tone),
    ),

    h('div.chiprow',
      s.loader && h('span.pill.ghost', s.loader),
      s.minecraft && h('span.pill.ghost', s.minecraft),
      s.port && h('span.pill.ghost', `:${s.port}`),
      s.mod_count ? h('span.pill.ghost', `${s.mod_count} mods`) : null,
      (s.problems || 0) > 0
        ? h('span.pill.warn', `${s.problems} problem${s.problems > 1 ? 's' : ''}`)
        : null,
    ),

    s.pack?.name
      ? h('div.mono.muted.trunc', { title: s.pack.name }, s.pack.name)
      : h('div.mono.muted', s.managed === false
        ? 'not created by BlessForge' : 'no modpack'),

    s.state === 'orphan'
      ? h('div.note.bad', { style: { fontSize: '12.5px' } },
        'Crafty has a record for this server but cannot read its files. It '
        + 'will keep failing until the directory is restored or the record '
        + 'is deleted.')
      : h('div.gauges',
        gauge('CPU', s.cpu, `${(s.cpu ?? 0).toFixed?.(0) ?? 0}%`,
          s.cpu > 85 ? 'bad' : s.cpu > 60 ? 'warn' : ''),
        gauge('RAM', s.mem, s.mem_bytes ? bytes(s.mem_bytes) : `${s.mem ?? 0}%`,
          s.mem > 88 ? 'bad' : s.mem > 70 ? 'warn' : ''),
        s.running
          ? gauge('Players',
            s.max_players ? (s.players / s.max_players) * 100 : 0,
            `${s.players ?? 0}/${s.max_players ?? '?'}`)
          : null,
      ),

    h('div.btnrow', { style: { marginTop: 'auto', paddingTop: '10px' } },
      s.state === 'running'
        ? h('button.btn.sm.ghost', {
          onclick: (e) => { e.stopPropagation(); action(s, 'stop_server'); },
        }, icon('stop', 13), h('span', 'Stop'))
        : h('button.btn.sm.primary', {
          disabled: s.state === 'orphan',
          onclick: (e) => { e.stopPropagation(); action(s, 'start_server'); },
        }, hl(), icon('play', 13), h('span', 'Start')),
      s.state === 'running' && h('button.btn.icon.sm.ghost', {
        'aria-label': `Restart ${s.name}`,
        onclick: (e) => { e.stopPropagation(); action(s, 'restart_server'); },
      }, icon('restart', 14)),
      h('a.btn.sm.ghost', {
        href: `#/i/${s.server_id}/console`,
        onclick: (e) => e.stopPropagation(),
      }, icon('terminal', 13), h('span', 'Console')),
      s.state === 'incomplete' && h('button.btn.sm.gold', {
        onclick: (e) => { e.stopPropagation(); retryLoader(s); },
      }, hl(), icon('wrench', 13), h('span', 'Finish setup')),
    ),
  );
}

function gauge(label, percent, text, tone = '') {
  const pct = Math.max(0, Math.min(100, Number(percent) || 0));
  return h('div.gauge',
    h('span', label),
    bar(pct, `thin ${tone}`),
    h('span.tnum', { style: { textAlign: 'right' } }, text));
}

export async function action(s, act) {
  const verb = { start_server: 'Starting', stop_server: 'Stopping',
    restart_server: 'Restarting', kill_server: 'Killing' }[act];
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
