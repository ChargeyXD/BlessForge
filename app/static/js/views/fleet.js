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
  empty, skeletonGrid, confirmDialog, segmented, prefs, duration,
} from '../core.js';
import { run } from '../jobs.js';
import {
  state, refreshInstances, refreshGroups, go, topActions, bannerIfUnhealthy,
  onState, groupOf, motif, lastRan, engineLabel,
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
  // The rack filter is a SECOND axis, not a fifth segment: "running" and
  // "on the Survival rack" are different questions and you routinely want
  // to ask both at once. Remembered, but validated against the racks that
  // still exist -- a rack deleted from another tab must not leave this
  // screen permanently showing nothing.
  let rackId = prefs.get('fleet.rack', '');
  let unsub = null;
  // Committed memory per node: the running servers' limits against its RAM.
  let memory = [];
  const loadMemory = () => api.get('/api/nodes/memory')
    .then((r) => { memory = r.nodes || []; paint(); }).catch(() => {});

  topActions(
    h('a.btn.sm.ghost', { href: '#/groups' }, icon('layers', 14),
      h('span', 'Racks')),
    h('a.btn.sm.ghost', { href: '#/discover' }, icon('compass', 14),
      h('span', 'Discover')),
    h('a.btn.sm.primary', { href: '#/create' }, hl(), icon('plus', 14),
      h('span', 'New server')),
  );

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

    const shown = all.filter((s) => {
      if (rackId && !inRack(s)) return false;
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
          h('p.eyebrow', `${engineLabel(state.health?.checks?.engine?.driver
            || state.health?.config?.engine)} · `,
            h('b', state.health?.checks?.engine?.ok
              ? `${state.health.checks.engine.nodes} node`
                + `${state.health.checks.engine.nodes === 1 ? '' : 's'}`
              : 'not answering')),
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

      all.length ? h('div.kpis.stagger', { style: { marginBottom: '20px' } },
        kpi('Servers', String(all.length), `${counts.running} running`),
        kpi('Players online', String(counts.players),
          counts.running ? 'across running servers' : 'nothing is up'),
        kpi('Mods managed', num(counts.mods) === '—' ? '0' : num(counts.mods),
          'recorded in instance manifests'),
        kpi('Needs attention', String(counts.attention),
          counts.attention ? 'crashed, orphaned or half-finished' : 'all clear',
          counts.attention ? 'bad' : 'ok'),
        ...memory.filter((m) => m.total_mb).map((m) => kpi(
          memory.length > 1 ? `Memory · ${m.node}` : 'Memory committed',
          `${(m.committed_mb / 1024).toFixed(1)} / ${(m.total_mb / 1024).toFixed(0)} GB`,
          m.committed_mb > m.total_mb ? 'more than the machine has'
            : 'running servers\' limits',
          m.committed_mb > m.total_mb ? 'warn' : '')),
      ) : null,

      ...portGroups(all),

      !all.length
        ? (state.health?.ready
          ? empty('fox-mascot.png', 'No servers yet',
            'There are no servers yet. Create an empty one with just a loader '
            + 'and a version, or install a modpack from the catalogue.',
            h('a.btn.primary', { href: '#/create' }, hl(), h('span', 'Create a server')),
            h('a.btn', { href: '#/discover' }, hl(), h('span', 'Browse modpacks')))
          : empty('chargey-peek.png', 'No node is answering yet',
            'BlessForge cannot reach the node that runs the servers. '
            + 'Settings lists exactly what is missing.',
            h('a.btn.primary', { href: '#/settings' }, hl(), h('span', 'Settings'))))
        : shown.length
          ? h('div.grid.gauto.bleed.stagger', ...shown.map(card))
          : h('div.note', rackId
            ? 'Nothing on that rack matches that filter.'
            : 'Nothing matches that filter.'),
    );
  }

  if (!state.instances.length && state.health?.ready) {
    mount(node, skeletonGrid(6));
    await refreshInstances({ quiet: true }).catch(() => {});
  }
  refreshGroups();
  paint();
  loadMemory();
  unsub = onState(() => { paint(); });
  const memTimer = setInterval(loadMemory, 20000);

  return { node, dispose: () => { unsub?.(); clearInterval(memTimer); } };
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
  const g = groupOf(s.server_id);

  const node = h('div.card.hoverable.svcard', {
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

    h('div.sv-foot',
      h('span.sv-ran.mono', lastRan(s)),
      h('button.racktag', {
        type: 'button',
        'aria-label': g
          ? `${s.name} is on the ${g.name} rack. Move it.`
          : `${s.name} is not on a rack. Hang it.`,
        onclick: (e) => { e.stopPropagation(); hang(s); },
      },
        h('span.rt-g', { 'aria-hidden': 'true' },
          g ? motif(g.motif).glyph : '—'),
        h('span.trunc', g ? g.name : 'open yard')),
    ),

    s.pack?.name
      ? h('div.mono.muted.trunc', { title: s.pack.name }, s.pack.name)
      : h('div.mono.muted', s.managed === false
        ? 'not created by BlessForge' : 'no modpack'),

    s.state === 'orphan'
      ? h('div.note.bad', { style: { fontSize: '12.5px' } },
        'There is a record for this server, but its node cannot read its '
        + 'files or is not answering. It will keep failing until the folder '
        + 'is restored, the node comes back, or the record is deleted.')
      : h('div.gauges',
        // A share of the cores this server may use; the counts are a hover away.
        gauge('CPU', s.cpu, `${(s.cpu ?? 0).toFixed?.(0) ?? 0}%`,
          s.cpu > 85 ? 'bad' : s.cpu > 60 ? 'warn' : '',
          s.cpu_allowance && s.cpu_cores != null
            ? `${s.cpu_cores.toFixed(1)} of the ${s.cpu_allowance} cores it may use are busy`
            : ''),
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

  if (g) node.style.setProperty('--g-ink', motif(g.motif).ink);
  return node;
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

function gauge(label, percent, text, tone = '', title = '') {
  const pct = Math.max(0, Math.min(100, Number(percent) || 0));
  return h('div.gauge', title ? { title } : null,
    h('span', label),
    bar(pct, `thin ${tone}`),
    h('span.tnum', { style: { textAlign: 'right' } }, text));
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
