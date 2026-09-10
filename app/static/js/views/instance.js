/* ============================================================
   Instance — one server, and everything you can do to it.

   The tab set is not fixed: a Paper server gets Plugins where a
   Forge server gets Mods, because they are different folders
   with different catalogues behind them and pretending otherwise
   is how a plugin ends up in mods/ doing nothing.

   Live numbers come from /stats on a short poll while the tab is
   visible; everything else is read once per tab and refreshed on
   demand, so an idle browser is not a load on Crafty.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bar, bytes, num,
  duration, empty, loadingFox, confirmDialog, prefs, ago,
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

export async function render({ params }) {
  const id = params.id;
  const node = h('div.wrap.wide');
  let detail = null;
  let tabView = null;
  let statsTimer = null;
  let disposed = false;

  const headHost = h('div');
  const tabsHost = h('div.tabs', { role: 'tablist', 'aria-label': 'Instance sections' });
  const bodyHost = h('div.tabbody');
  mount(node, headHost, tabsHost, bodyHost);
  mount(bodyHost, loadingFox('Reading the instance'));

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

      ctx.running ? h('div', { style: { display: 'grid', gap: '6px',
        minWidth: '220px', paddingTop: '10px' } },
        liveGauge('CPU', ctx.stats.cpu, `${Math.round(ctx.stats.cpu || 0)}%`),
        liveGauge('Memory', ctx.stats.mem_percent,
          ctx.stats.mem ? bytes(ctx.stats.mem) : '—'),
        liveGauge('Players',
          ctx.stats.max ? ((ctx.stats.online || 0) / ctx.stats.max) * 100 : 0,
          `${ctx.stats.online ?? 0} / ${ctx.stats.max ?? '?'}`),
      ) : null,
    ));
  }

  function liveGauge(label, pct, text) {
    const v = Math.max(0, Math.min(100, Number(pct) || 0));
    return h('div', { style: { display: 'grid',
      gridTemplateColumns: '58px 1fr 74px', gap: '8px', alignItems: 'center',
      fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--faint)' } },
      h('span', label),
      bar(v, `thin ${v > 88 ? 'bad' : v > 70 ? 'warn' : ''}`),
      h('span.tnum', { style: { textAlign: 'right' } }, text));
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

  try {
    const mod = await load();
    if (disposed) return { node, dispose: () => {} };
    tabView = await mod.render(ctx);
    mount(bodyHost, tabView.node);
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
