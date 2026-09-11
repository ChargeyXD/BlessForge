/* ============================================================
   Settings — connections, decisions, cache, assistant.

   The allow/block list lives here because it is the one piece of
   state in this app that is genuinely global: the reason a mod
   is safe on a server is a property of the mod, not of the
   server it happens to be on. It is exportable for the same
   reason — a list you built over months should survive moving
   to another machine.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, bytes, num, ago,
  empty, loadingFox, confirmDialog, promptDialog, modal, close, frag,
  segmented, toggle, field, copyText, readTheme, applyTheme,
} from '../core.js';
import { run } from '../jobs.js';
import { state, refreshHealth, topActions } from '../app.js';

export async function render() {
  const node = h('div.wrap');
  mount(node, loadingFox('Reading settings'));

  const [health, cache, updates, ai, endpoints, decisions, tuning]
    = await Promise.all([
      refreshHealth(),
      api.get('/api/cache').catch(() => null),
      api.get('/api/updates').catch(() => null),
      api.get('/api/ai/status').catch(() => null),
      api.get('/api/ai/endpoints').catch(() => null),
      api.get('/api/whitelist').catch(() => ({ items: [], counts: {} })),
      api.get('/api/ai/tuning').catch(() => null),
    ]);
  // The toggle is the one thing on this screen the user can change that the
  // server owns, so it is mirrored locally rather than re-fetched: a repaint
  // must not undo a switch that has already been written.
  let tuneOn = !!tuning?.chosen;

  let list = decisions;
  let listFilter = '';
  const listHost = h('div');

  function paint() {
    mount(node,
      h('div.sec-head.mon',
        h('div',
          h('p.eyebrow', 'BlessForge · ', h('b', 'version 2.1')),
          h('h2', 'Settings'))),

      h('div.grid.g2', { style: { alignItems: 'start' } },
        /* --- connections ------------------------------------- */
        h('div.panel',
          h('header', icon('link', 15), h('h3', 'Connections')),
          h('div.pad', { style: { display: 'grid', gap: '12px' } },
            checkRow('Crafty Controller', health.checks?.crafty,
              health.config?.crafty_url,
              health.checks?.crafty?.ok
                ? `${health.checks.crafty.servers} servers · `
                  + `${health.checks.crafty.latency_ms} ms`
                : null),
            checkRow('CurseForge', health.checks?.curseforge,
              'api.curseforge.com'),
            checkRow('Modrinth', health.checks?.modrinth, 'api.modrinth.com'),
            checkRow('Storage', health.checks?.storage,
              health.checks?.storage?.path,
              health.checks?.storage?.free_gb
                ? `${health.checks.storage.free_gb} GB free` : null),
            h('p.muted', { style: { fontSize: '12.5px' } },
              'These are set from environment variables in the compose file — '
              + 'BlessForge never writes them, because a container that '
              + 'rewrites its own configuration loses it on the next pull.'),
            h('button.btn.sm.ghost.block', {
              onclick: async () => {
                await refreshHealth();
                toast('Re-checked.', 'ok');
                render().then((v) => mount(document.querySelector('#main'), v.node));
              },
            }, icon('refresh', 13), h('span', 'Check again')),
          )),

        /* --- appearance ------------------------------------- */
        h('div.panel',
          h('header', icon('sun', 15), h('h3', 'Appearance')),
          h('div.pad', { style: { display: 'grid', gap: '12px' } },
            h('div.field', { style: { margin: 0 } },
              h('label', 'Theme'),
              segmented([
                { value: 'system', label: 'System' },
                { value: 'light', label: 'Light' },
                { value: 'dark', label: 'Dark' },
              ], readTheme(), (v) => { applyTheme(v); paint(); }, 'Theme')),
            h('p.muted', { style: { fontSize: '12.5px' } },
              'Light and dark are both complete palettes, not one inverted — '
              + 'the dark set desaturates rather than flipping, so the pink '
              + 'stays pink instead of turning fluorescent.'),
            h('div', { style: { position: 'relative', textAlign: 'center',
              padding: '10px 0' } },
              h('div.fox-halo'),
              h('img.fox', { src: '/assets/fox-mascot.png', alt: '',
                style: { width: '120px', margin: '0 auto' } })),
          )),

        /* --- cache ------------------------------------------- */
        cache ? h('div.panel',
          h('header', icon('db', 15), h('h3', 'Download cache')),
          h('div.pad', { style: { display: 'grid', gap: '12px' } },
            h('div.kpis',
              h('div.kpi', h('div.lbl', 'Holding'),
                h('div.v', { style: { fontSize: '20px' } }, `${cache.used_gb} GB`),
                h('div.sub', `ceiling ${cache.limit_gb} GB`)),
              h('div.kpi', h('div.lbl', 'State'),
                h(`div.v${cache.over ? '.bad' : '.ok'}`,
                  { style: { fontSize: '20px' } },
                  cache.over ? 'over' : 'fine'),
                h('div.sub', cache.over ? 'the oldest go first'
                  : 'nothing to prune'))),
            h('p.muted', { style: { fontSize: '12.5px' } },
              'Everything in here is a copy of something CurseForge or '
              + 'Modrinth will hand out again, so deleting it costs bandwidth '
              + 'and nothing else. Imported exports are kept somewhere '
              + 'separate — those exist nowhere else.'),
            h('button.btn.sm.ghost.block', {
              onclick: async () => {
                try {
                  const r = await api.post('/api/cache/prune');
                  toast(`${r.pruned} files removed, ${bytes(r.freed)} freed.`,
                    'ok');
                } catch (e) { toastError(e); }
              },
            }, icon('trash', 13), h('span', 'Prune now')))) : null,

        /* --- updates ----------------------------------------- */
        updates ? h('div.panel',
          h('header', icon('refresh', 15), h('h3', 'Scheduled update checks')),
          h('div.pad', { style: { display: 'grid', gap: '12px' } },
            h('div.kpis',
              h('div.kpi', h('div.lbl', 'Mods with updates'),
                h('div.v', { style: { fontSize: '20px' } },
                  String(updates.total ?? 0)),
                h('div.sub', 'across every managed server')),
              h('div.kpi', h('div.lbl', 'Last sweep'),
                h('div.v', { style: { fontSize: '16px' } },
                  updates.checked_at ? ago(updates.checked_at) : 'never'),
                h('div.sub', `every ${updates.every_hours} h`))),
            h('button.btn.sm.ghost.block', {
              onclick: () => run('/api/updates/check', {}, {
                title: 'Checking every server for mod updates',
              }),
            }, icon('search', 13), h('span', 'Sweep now')))) : null,

        /* --- assistant --------------------------------------- */
        ai ? h('div.panel',
          h('header', icon('flask', 15), h('h3', 'Troubleshooting assistant')),
          h('div.pad', { style: { display: 'grid', gap: '12px' } },
            h('div.chiprow',
              pill(ai.available ? 'available' : 'unavailable',
                ai.available ? 'ok' : 'dead'),
              ai.model ? pill(ai.model, 'ghost') : null),
            h('p.muted', { style: { fontSize: '12.5px' } },
              ai.available
                ? `Pointed at ${ai.url}. The deterministic checks run first `
                  + 'and are fed in as evidence — the model explains and '
                  + 'prioritises, it is not the detector.'
                : (ai.reason || 'Not configured.')),
            ai.hint ? h('div.note.warn', ai.hint) : null,

            /* --- silent tuning ------------------------------------
               The only place this feature is visible when it is
               working. Everything else it does happens on the Tune
               screen without being asked. */
            tuning ? h('div', { style: { display: 'grid', gap: '10px',
              borderTop: '2px solid var(--rule)', paddingTop: '12px' } },
            toggle('Let it tune servers quietly', tuneOn, async (v) => {
              try {
                const res = await api.post('/api/ai/tuning', { enabled: v });
                // `chosen` is the switch; `enabled` is the switch AND a
                // container that has AI_ENABLED set. They differ, and the
                // toggle must show the switch.
                tuneOn = !!(res.chosen ?? res.enabled);
                toast(tuneOn
                  ? 'On. The Tune screen will fold its suggestions in as they '
                    + 'arrive.'
                  : 'Off. Tuning is deterministic again.',
                res.persisted === false ? 'warn' : 'ok');
                if (res.persisted === false) {
                  toast('Not written to disk — /data is not writable, so this '
                    + 'lasts until the container restarts.', 'warn',
                  { timeout: 9000 });
                }
              } catch (e) {
                tuneOn = !v;
                toastError(e);
              }
              paint();
            }, {
              disabled: !tuning.ai_enabled,
              help: 'On the Tune screen it quietly sharpens the heap, the GC '
                + 'flags and a few server.properties values for the pack that '
                + 'is actually installed, and says in one line why each number '
                + 'moved. It never blocks the page and never asks you '
                + 'anything.',
            }),
            h('p.muted', { style: { fontSize: '12px' } },
              'Everything it proposes is clamped against the same host '
              + 'arithmetic that produced the number in the first place — a '
              + 'heap can never exceed the measured ceiling, flags come from '
              + 'a fixed list with per-flag ranges, and anything outside that '
              + 'is dropped and counted. If the model is off, missing, '
              + `unreachable or slower than ${tuning.timeout_seconds ?? 25} s, `
              + 'the deterministic optimizer is what you get.'),
            !tuning.ai_enabled
              ? h('div.note.warn', 'AI_ENABLED is false in this container\'s '
                + 'environment, so this cannot be switched on here.')
              : null) : null,

            endpoints?.items?.length > 1
              ? h('div.field', { style: { margin: 0 } },
                h('label', 'Endpoint'),
                h('select.inp', {
                  onchange: async (e) => {
                    try {
                      await api.post('/api/ai/endpoint', { url: e.target.value });
                      toast('Endpoint switched, and remembered.', 'ok');
                    } catch (err) { toastError(err); }
                  },
                }, ...endpoints.items.map((i) => h('option', {
                  value: i.url, selected: i.url === endpoints.active,
                }, `${i.label || i.url}`))))
              : null,
            !ai.available && ai.models
              ? h('button.btn.sm.block', {
                onclick: () => run('/api/ai/pull', {}, { title: 'Pull the model' }),
              }, hl(), icon('download', 13), h('span', 'Pull the model')) : null,
          )) : null,
      ),

      /* --- the allow / block list -------------------------- */
      h('div.panel', { style: { marginTop: '18px' } },
        h('header', icon('shieldCheck', 15), h('h3', 'Client-only decisions'),
          h('span.sp', `${list.counts?.allow ?? 0} allowed · `
            + `${list.counts?.block ?? 0} blocked`)),
        h('div.pad', listHost)),

      h('div.torii', { style: { marginTop: '26px' } }, toriiSvg()),
    );
    paintList();
  }

  function paintList() {
    const items = (list.items || []).filter((i) => !listFilter
      || (i.name || '').toLowerCase().includes(listFilter.toLowerCase())
      || (i.key || '').includes(listFilter.toLowerCase()));

    mount(listHost,
      h('p.muted', { style: { fontSize: '13px', marginBottom: '12px' } },
        'Two directions, one list. ',
        h('b', 'Allow'), ' means "this is safe on a server whatever the check '
        + 'says"; ', h('b', 'block'), ' means "this is client-only whatever it '
        + 'claims". Both are matched by project id, then mod id, then the '
        + 'filename with its version stripped — so a decision survives a '
        + 'version bump and a rename. Global by default, because the reason a '
        + 'mod is safe is a property of the mod.'),

      h('div.listbar', { style: { position: 'static' } },
        h('input.inp.grow', {
          type: 'search', placeholder: 'Search decisions…',
          'aria-label': 'Search decisions',
          oninput: (e) => { listFilter = e.target.value; paintList(); },
        }),
        h('button.btn.sm.ghost', { onclick: addDecision },
          icon('plus', 13), h('span', 'Add')),
        h('button.btn.sm.ghost', { onclick: exportList },
          icon('download', 13), h('span', 'Export')),
        h('button.btn.sm.ghost', { onclick: importList },
          icon('upload', 13), h('span', 'Import')),
        (list.items || []).length
          ? h('button.btn.sm.danger', { onclick: clearAll },
            icon('trash', 13), h('span', 'Clear all'))
          : null),

      !(list.items || []).length
        ? h('div.note', 'Nothing recorded yet. Every time you tell a review '
          + '"this one is fine" or "always disable this", it lands here.')
        : !items.length
          ? h('div.note', 'Nothing matches that.')
          : h('div.modlist', ...items.map(decisionRow)));
  }

  function decisionRow(i) {
    return h('div.modrow',
      h('div.ico', icon(i.verdict === 'allow' ? 'shieldCheck' : 'ban', 15)),
      h('div.who',
        h('div.nm', i.name || i.key),
        h('div.fn', i.example_file || i.key),
        h('div.tags',
          pill(i.verdict, i.verdict === 'allow' ? 'ok' : 'bad'),
          pill(i.scope === 'global' ? 'every server' : 'one server', 'ghost'),
          i.project_id ? pill('by project id', 'info',
            'Matched on the catalogue project, so a rename cannot break it')
            : null,
          i.added_at ? pill(ago(i.added_at), 'ghost') : null),
        i.reason ? h('div.muted', { style: { fontSize: '12px', marginTop: '2px' } },
          i.reason) : null),
      h('div.acts',
        h('button.btn.icon.xs.ghost', {
          'aria-label': `Remove the decision about ${i.name || i.key}`,
          onclick: async () => {
            await api.del(`/api/whitelist/${encodeURIComponent(i.key)}`);
            list = await api.get('/api/whitelist');
            toast('Removed.', 'ok');
            paintList();
          },
        }, icon('trash', 12))));
  }

  async function addDecision() {
    let verdict = 'allow';
    const nameInput = h('input.inp', { placeholder: 'jei-1.21.1-19.21.0.jar' });
    const reasonInput = h('input.inp', { placeholder: 'why' });
    modal({
      title: 'Record a decision',
      body: frag(
        h('div.field', h('label', 'Jar filename or mod name'), nameInput,
          h('div.help', 'The version is stripped, so this covers future '
            + 'builds of the same mod.')),
        h('div.field', h('label', 'Decision'),
          segmented([
            { value: 'allow', label: 'Safe on a server' },
            { value: 'block', label: 'Always client-only' },
          ], verdict, (v) => { verdict = v; }, 'Decision')),
        h('div.field', h('label', 'Reason (optional)'), reasonInput)),
      footer: frag(
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', {
          onclick: async () => {
            const file = nameInput.value.trim();
            if (!file) { nameInput.focus(); return; }
            close();
            try {
              await api.post('/api/whitelist', {
                file, verdict, reason: reasonInput.value.trim(),
              });
              list = await api.get('/api/whitelist');
              toast('Recorded.', 'ok');
              paintList();
            } catch (e) { toastError(e); }
          },
        }, hl(), h('span', 'Record')))
    });
  }

  async function exportList() {
    try {
      const data = await api.get('/api/whitelist/export');
      const blob = new Blob([JSON.stringify(data, null, 2)],
        { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = h('a', { href: url, download: 'blessforge-decisions.json',
        style: { display: 'none' } });
      document.body.append(a);
      a.click();
      setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 3000);
      toast(`${data.entries.length} decisions exported.`, 'ok');
    } catch (e) { toastError(e); }
  }

  function importList() {
    const input = h('input', {
      type: 'file', accept: '.json', style: { display: 'none' },
      onchange: async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        try {
          const payload = JSON.parse(await file.text());
          const res = await api.post('/api/whitelist/import', { payload });
          list = await api.get('/api/whitelist');
          toast(`${res.imported} imported, ${res.total} total.`
            + (res.persisted ? '' : ' (not written to disk — /data is not '
              + 'writable)'), res.persisted ? 'ok' : 'warn');
          paintList();
        } catch (err) { toastError(err, 'That file could not be read'); }
      },
    });
    document.body.append(input);
    input.click();
    setTimeout(() => input.remove(), 60000);
  }

  async function clearAll() {
    const ok = await confirmDialog({
      title: 'Clear every decision?',
      message: 'Every "this is safe" and "always block this" is forgotten, and '
        + 'reviews go back to judging from evidence alone.',
      confirmLabel: 'Clear them', danger: true,
      requireText: 'CLEAR',
    });
    if (!ok) return;
    try {
      const r = await api.post('/api/whitelist/clear', {});
      list = await api.get('/api/whitelist');
      toast(`${r.removed} removed.`, 'ok');
      paintList();
    } catch (e) { toastError(e); }
  }

  function checkRow(label, check, sub, extra) {
    const ok = check?.ok;
    return h('div', { style: { display: 'flex', gap: '11px',
      alignItems: 'flex-start' } },
      h(`span.pill.${ok ? 'ok' : 'bad'}`, { style: { marginTop: '2px' } },
        ok ? 'ok' : 'down'),
      h('div', { style: { minWidth: 0, flex: 1 } },
        h('div.h-num', { style: { fontSize: '14px' } }, label),
        h('div.mono.muted.wrapany', { style: { fontSize: '11px' } },
          extra || sub || ''),
        !ok && check?.error
          ? h('div.wrapany', { style: { fontSize: '12.5px',
            color: 'var(--danger)', marginTop: '3px' } }, check.error)
          : null));
  }

  function toriiSvg() {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 120 100');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = '<g fill="currentColor">'
      + '<rect x="2" y="6" width="116" height="8" rx="1"/>'
      + '<rect x="12" y="20" width="96" height="6"/>'
      + '<rect x="26" y="26" width="10" height="74"/>'
      + '<rect x="84" y="26" width="10" height="74"/>'
      + '<rect x="20" y="0" width="80" height="5" rx="2"/></g>';
    return svg;
  }

  paint();
  return { node };
}
