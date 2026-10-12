/* ============================================================
   Overview — the situation, in one screen.

   Deliberately answers the four questions someone opens a server
   panel to ask: is it up, is anything wrong with it, who is on
   it, and what is it made of. Everything else is a tab.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, bar, bytes, num,
  duration, ago, loadingFox, empty, copyText, frag,
} from '../core.js';
import { run } from '../jobs.js';

export async function render(ctx) {
  const node = h('div', { style: { display: 'grid', gap: '18px' } });
  const diagHost = h('div');
  const playerHost = h('div');
  const packHost = h('div');

  function paint() {
    const m = ctx.manifest;
    const java = ctx.detail.java || {};
    const problems = m.problems || [];

    mount(node,
      ctx.detail.state === 'incomplete'
        ? h('div.note.warn', { role: 'alert' },
          h('b', 'This instance was never finished'),
          'An install stopped part-way and left it behind. Nothing here will '
          + 'start until the loader is installed — use Finish setup above, '
          + 'which retries the loader without touching anything else.')
        : null,

      java.ok === false
        ? h('div.note.bad', { role: 'alert' },
          h('b', `Running Java ${java.major}, needs Java ${java.required}`),
          'Minecraft refuses to start on the wrong Java and writes no log '
          + 'explaining why, which is the single most confusing failure this '
          + 'app sees. ',
          h('button.btn.xs', {
            style: { marginTop: '8px' },
            onclick: fixJava,
          }, hl(), h('span', `Pin Java ${java.required}`)))
        : null,

      h('div.split.wide-aside',
        h('div', { style: { display: 'grid', gap: '18px' } },
          /* --- facts ---------------------------------------- */
          h('div.panel',
            h('header', icon('info', 15), h('h3', 'This server'),
              h('span.sp', ctx.server.server_id?.slice(0, 8))),
            h('div.pad',
              h('div.kpis',
                factCard('State', ctx.detail.state,
                  ctx.running ? `up ${duration(ctx.detail.uptime_s)}` : 'not running',
                  ctx.running ? 'ok' : ''),
                factCard('Players',
                  ctx.running ? `${ctx.stats.online ?? 0}` : '—',
                  ctx.running ? `of ${ctx.stats.max ?? '?'} slots` : 'server is down'),
                factCard('World',
                  ctx.stats.world_size || '—', ctx.stats.world_name || 'no world yet'),
                factCard(ctx.plugins ? 'Plugins' : 'Mods',
                  String((m.mods || []).length || '—'),
                  m.mods?.length ? 'recorded at install' : 'nothing recorded'),
              ),
              h('div', { style: { marginTop: '16px', display: 'grid', gap: '8px' } },
                detailRow('Loader',
                  `${ctx.loader || 'unknown'}${m.loader_version ? ` ${m.loader_version}` : ''}`),
                detailRow('Minecraft', ctx.minecraft || 'unknown'),
                detailRow('Java', java.major
                  ? `${java.major}${java.required ? ` (needs ${java.required})` : ''}`
                  : 'not stated in the launch command'),
                detailRow('Port', String(ctx.server.server_port || '—')),
                detailRow('Directory', ctx.server.path || '—', true),
                detailRow('Mod folder', `${ctx.modDir}/`),
                m.installed_at
                  ? detailRow('Installed', ago(m.installed_at)) : null,
                m.pack?.source
                  ? detailRow('Source', m.pack.source === 'empty'
                    ? 'created empty in BlessForge' : m.pack.source) : null,
              ),
            )),

          /* --- what is wrong -------------------------------- */
          h('div.panel', { id: 'diagpanel' },
            h('header', icon('wrench', 15), h('h3', 'Health'),
              h('span.sp', 'checked on demand'),
              h('button.btn.xs.ghost', {
                style: { marginLeft: 'auto' },
                onclick: () => loadDiagnostics(true),
              }, icon('refresh', 12), h('span', 'Check now'))),
            h('div.pad', diagHost)),
        ),

        /* --- aside ------------------------------------------ */
        h('div.sticky', { style: { display: 'grid', gap: '18px' } },
          h('div.panel',
            h('header', icon('users', 15), h('h3', 'Who is on')),
            h('div.pad', playerHost)),

          h('div.panel',
            h('header', icon('bolt', 15), h('h3', 'Quick actions')),
            h('div.pad', { style: { display: 'grid', gap: '9px' } },
              quickBtn('terminal', 'Open the console',
                'Live output, and a box that types into the server',
                () => ctx.go('console')),
              quickBtn('folder', 'Browse the files',
                'Everything in the server directory, editable',
                () => ctx.go('files')),
              quickBtn(ctx.plugins ? 'puzzle' : 'box',
                ctx.plugins ? 'Manage plugins' : 'Manage mods',
                ctx.plugins ? 'Install from Modrinth, toggle, remove'
                  : 'Install, toggle, update, remove',
                () => ctx.go(ctx.plugins ? 'plugins' : 'mods')),
              quickBtn('shieldCheck', 'Scan for client-only mods',
                'Read every jar and say which cannot run here',
                () => ctx.go('diagnose'), ctx.plugins),
              quickBtn('sliders', 'Tune for this host',
                'Heap, JVM flags, server.properties',
                () => ctx.go('tune')),
              quickBtn('refresh', 'Undo something',
                'Snapshots taken before destructive changes',
                () => ctx.go('backups')),
            )),

          h('div.panel', { id: 'packpanel' },
            h('header', icon('archive', 15), h('h3', 'CurseForge pack')),
            h('div.pad', packHost)),

          h('div.panel',
            h('header', icon('flask', 15), h('h3', 'Boot test')),
            h('div.pad',
              h('p.muted', { style: { fontSize: '13px' } },
                'Start it, watch what happens, stop it, and report. The right '
                + 'first move for a server that has never produced a log.'),
              h('button.btn.sm.block', {
                disabled: ctx.running,
                onclick: smokeTest,
              }, hl(), icon('play', 13),
                h('span', ctx.running ? 'Already running' : 'Run a boot test')))),
        ),
      ),
    );
  }

  function detailRow(label, value, copyable) {
    return h('div', { style: { display: 'flex', gap: '12px',
      justifyContent: 'space-between', fontSize: '13.5px', alignItems: 'baseline' } },
      h('span.muted', label),
      h('span', { style: { textAlign: 'right', minWidth: 0 } },
        h('span.wrapany.mono', value),
        copyable && value !== '—' ? h('button.btn.icon.xs.ghost', {
          'aria-label': `Copy ${label}`,
          style: { marginLeft: '6px' },
          onclick: () => copyText(value, label),
        }, icon('copy', 11)) : null));
  }

  function factCard(label, value, sub, tone = '') {
    return h('div.kpi',
      h('div.lbl', label),
      h(`div.v${tone ? `.${tone}` : ''}`, { style: { fontSize: '19px' } }, value),
      sub && h('div.sub', sub));
  }

  function quickBtn(ico, title, sub, onclick, hidden) {
    if (hidden) return null;
    return h('button.card.hoverable', {
      type: 'button', onclick,
      style: { textAlign: 'left', cursor: 'pointer', padding: '11px 13px',
        gap: '2px', flexDirection: 'row', alignItems: 'center' },
    },
      icon(ico, 17),
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('div.h-num', { style: { fontSize: '13.5px' } }, title),
        h('div.mono.muted', { style: { fontSize: '10px' } }, sub)),
      icon('chevronRight', 14));
  }

  async function fixJava() {
    try {
      const res = await api.post(`/api/instances/${ctx.id}/fix/java`, {});
      toast(res.changed
        ? `Pinned Java ${res.java_major}.`
        : (res.reason || 'Nothing needed changing.'), 'ok');
      await ctx.reload();
      paint();
    } catch (e) { toastError(e, 'Could not set the Java version'); }
  }

  function smokeTest() {
    run(`/api/instances/${ctx.id}/smoke-test`, { timeout: 300 }, {
      title: `Boot test — ${ctx.name}`,
      onEnd: (rec) => {
        if (rec.status !== 'done') return;
        const r = rec.result || {};
        toast(r.verdict || 'The boot test finished.',
          r.ok ? 'ok' : 'warn', { title: 'Boot test', timeout: 12000 });
        loadDiagnostics(true);
      },
    });
  }

  /* --- health, loaded lazily ---------------------------------- */
  let diagLoaded = false;
  async function loadDiagnostics(force) {
    if (diagLoaded && !force) return;
    diagLoaded = true;
    mount(diagHost, loadingFox('Checking'));
    try {
      const d = await api.get(`/api/instances/${ctx.id}/diagnose`);
      const findings = d.findings || [];
      if (!findings.length) {
        mount(diagHost, h('div.note.ok',
          h('b', 'Nothing looks wrong'),
          d.has_logs
            ? 'The checks pass and nothing in the log stands out.'
            : 'This server has produced no log yet, so there was nothing to '
              + 'read — run a boot test to find out how it actually starts.'));
        return;
      }
      const bySeverity = { critical: [], warn: [], info: [] };
      for (const f of findings) {
        (bySeverity[f.severity] || bySeverity.info).push(f);
      }
      mount(diagHost,
        h('div.chiprow', { style: { marginBottom: '12px' } },
          bySeverity.critical.length
            ? pill(`${bySeverity.critical.length} critical`, 'bad') : null,
          bySeverity.warn.length
            ? pill(`${bySeverity.warn.length} warnings`, 'warn') : null,
          bySeverity.info.length
            ? pill(`${bySeverity.info.length} notes`, 'info') : null),
        ...findings.slice(0, 6).map(findingRow),
        findings.length > 6
          ? h('button.btn.sm.ghost.block', {
            style: { marginTop: '10px' },
            onclick: () => ctx.go('diagnose'),
          }, h('span', `See all ${findings.length} findings`))
          : h('button.btn.sm.ghost.block', {
            style: { marginTop: '10px' },
            onclick: () => ctx.go('diagnose'),
          }, h('span', 'Open Diagnose')));
    } catch (e) {
      mount(diagHost, h('div.note.bad', e.message));
    }
  }

  function findingRow(f) {
    const tone = f.severity === 'critical' ? 'bad'
      : f.severity === 'warn' ? 'warn' : 'info';
    return h(`div.note.${tone}`, { style: { marginBottom: '8px' } },
      h('b', f.title),
      h('div.wrapany', { style: { fontSize: '13px' } }, f.detail));
  }

  /* --- the pack this server is, as a file ----------------------
     Two different things wearing one name, and the difference
     matters. A server the roulette built has an *archive on disk*
     — the exact zip it was installed from, kept at install time —
     and handing that back is a file copy. Any other server can
     only be described from its install manifest, which means
     rebuilding a manifest and pulling every non-CurseForge jar
     back off the node. The panel offers whichever it has, and
     says which one it is giving you. */
  let packLoaded = false;
  async function loadPack(force) {
    if (packLoaded && !force) return;
    packLoaded = true;
    mount(packHost, h('div.skel', { style: { height: '54px' } }));
    let info = { rolled: false };
    try {
      info = await api.get(`/api/instances/${ctx.id}/roll`);
    } catch { /* not fatal — the rebuild path works regardless */ }
    const roll = info.roll || {};

    const rebuild = h('button.btn.sm.ghost.block', {
      style: { marginTop: '9px' },
      onclick: exportPack,
    }, icon('archive', 13),
      h('span', info.export_ready ? 'Rebuild from what is installed now'
        : 'Export as a CurseForge pack'));

    if (!info.rolled) {
      mount(packHost,
        h('p.muted', { style: { fontSize: '13px' } },
          'Write this server out as a CurseForge modpack zip — a manifest of '
          + 'every mod it was installed with, plus any jar that has no '
          + 'CurseForge entry bundled inside it. It imports into the '
          + 'CurseForge app, and back into BlessForge.'),
        rebuild);
      return;
    }

    mount(packHost,
      h('div.chiprow', { style: { marginBottom: '10px' } },
        pill('rolled', 'gold', 'Built by the Mod Roulette'),
        roll.intensity ? pill(roll.intensity, 'plum') : null,
        roll.adopted
          ? pill(`${roll.adopted} brought in`, 'ok',
            'Mods that came in because one of their add-ons was rolled')
          : null),
      h('div', { style: { display: 'grid', gap: '7px', marginBottom: '10px' } },
        detailRow('Rolled', roll.rolled_at ? ago(roll.rolled_at * 1000) : '—'),
        detailRow('Rolled mods', String(roll.hand ?? '—')),
        detailRow('Jars installed', String(roll.installed ?? '—')),
        detailRow('In the manifest', `${roll.curseforge_mods ?? '?'} CurseForge`
          + (roll.bundled_mods ? `, ${roll.bundled_mods} bundled` : ''))),
      info.export_ready
        ? frag(
          h('a.btn.sm.block', {
            href: info.download,
            download: '',
          }, hl(), icon('download', 13),
            h('span', `Download the rolled pack${roll.export_bytes
              ? ` · ${bytes(roll.export_bytes)}` : ''}`)),
          h('p.muted', { style: { fontSize: '12px', marginTop: '7px' } },
            'The exact zip this server was installed from, kept since the '
            + 'roll. Not rebuilt — byte for byte what went in.'))
        : h('div.note.warn',
          h('b', 'The rolled pack is no longer on disk'),
          'It was written when the roll was installed and something has '
          + 'removed it since. Rebuilding from the install manifest gives '
          + 'you the same mods.'),
      rebuild);
  }

  function exportPack() {
    run(`/api/instances/${ctx.id}/export`, { bundle_others: true }, {
      title: `Export — ${ctx.name}`,
      onEnd: (rec) => {
        if (rec.status !== 'done') return;
        const r = rec.result || {};
        toast(`${r.curseforge_mods ?? '?'} mods in the manifest`
          + ((r.bundled || []).length ? `, ${r.bundled.length} bundled.` : '.'),
        'ok', { title: 'Pack written', timeout: 10000 });
        if (r.download) {
          const a = h('a', { href: r.download, download: '' });
          document.body.append(a);
          a.click();
          a.remove();
        }
      },
    });
  }

  /* --- who is on ---------------------------------------------- */
  async function loadPlayers() {
    mount(playerHost, h('div.skel', { style: { height: '54px' } }));
    try {
      const p = await api.get(`/api/instances/${ctx.id}/players`);
      if (!ctx.running) {
        mount(playerHost, h('p.muted', { style: { fontSize: '13px' } },
          'The server is down. ',
          h('b', String(p.counts.known)), ' players are known to it, ',
          h('b', String(p.counts.ops)), ' of them operators.'),
          h('button.btn.sm.ghost.block', {
            style: { marginTop: '10px' }, onclick: () => ctx.go('players'),
          }, icon('users', 13), h('span', 'Manage players')));
        return;
      }
      if (!p.online_names.length) {
        mount(playerHost,
          h('p.muted', { style: { fontSize: '13px' } },
            p.online ? `${p.online} online, but the server did not name them.`
              : 'Nobody is connected right now.'),
          h('button.btn.sm.ghost.block', {
            style: { marginTop: '10px' }, onclick: () => ctx.go('players'),
          }, icon('users', 13), h('span', 'Manage players')));
        return;
      }
      mount(playerHost,
        h('div', { style: { display: 'grid', gap: '8px' } },
          h('p.muted', { style: { fontSize: '12px', margin: 0 } },
            `${p.online} of ${p.max_players || '?'} on`
            + (p.player_source === 'ping' || p.player_source === 'list'
              ? ' \u00b7 confirmed with the server' : '')),
          ...p.players.filter((x) => x.online).slice(0, 8).map((x) =>
            h('div', { style: { display: 'flex', gap: '9px', alignItems: 'center' } },
              h('img', { src: x.avatar, alt: '', loading: 'lazy',
                style: { width: '26px', height: '26px', border: '2px solid var(--rule)',
                  imageRendering: 'pixelated' } }),
              h('span', { style: { flex: 1, minWidth: 0 } },
                h('span.h-num', { style: { fontSize: '13.5px' } }, x.name),
                x.online_since
                  ? h('span.muted', { style: { fontSize: '11.5px', marginLeft: '8px' } },
                    `${x.online_since_exact === false ? '\u2265 ' : ''}`
                    + duration(Date.now() / 1000 - x.online_since))
                  : null),
              x.op ? pill('op', 'gold') : null)),
          p.online > p.players.filter((x) => x.online).length
            ? h('p.muted', { style: { fontSize: '12px', margin: 0 } },
              `+${p.online - p.players.filter((x) => x.online).length} the server did `
              + 'not name')
            : null),
        h('button.btn.sm.ghost.block', {
          style: { marginTop: '10px' }, onclick: () => ctx.go('players'),
        }, icon('users', 13), h('span', 'Manage players')));
    } catch (e) {
      mount(playerHost, h('p.muted', { style: { fontSize: '13px' } },
        'Could not read the player files.'));
    }
  }

  paint();
  loadDiagnostics(false);
  loadPlayers();
  loadPack(false);

  return {
    node,
    onRunningChanged: () => { paint(); loadPlayers(); },
  };
}
