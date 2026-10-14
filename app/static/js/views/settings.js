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
  segmented, toggle, field, copyText, readTheme, applyTheme, mascot, spinner,
} from '../core.js';
import { run } from '../jobs.js';
import { state, refreshHealth, topActions, engineLabel } from '../app.js';
import { session, isAdmin } from '../auth.js';

export async function render() {
  return build('you');
}

/* The machine-wide panels, for The Gate (views/gate.js): connections,
   assistant, upkeep. Same builder, so a panel is written once. */
export async function machine(section) {
  return build(section);
}

async function build(which = 'you') {
  const node = h('div.wrap');
  mount(node, loadingFox('Reading settings'));
  const mine = which === 'you';
  // Only the machine-wide sections need the machine's readings, and only an
  // admin may have them; Settings itself asks for nothing it does not show.
  let [health, cache, updates, ai, endpoints, decisions, tuning]
    = await Promise.all([
      mine ? null : refreshHealth(),
      which === 'upkeep' ? api.get('/api/cache').catch(() => null) : null,
      which === 'upkeep' ? api.get('/api/updates').catch(() => null) : null,
      which === 'assistant' ? api.get('/api/ai/status').catch(() => null) : null,
      which === 'assistant' ? api.get('/api/ai/endpoints').catch(() => null) : null,
      which === 'upkeep' ? api.get('/api/whitelist').catch(() => ({ items: [], counts: {} }))
        : { items: [], counts: {} },
      which === 'assistant' ? api.get('/api/ai/tuning').catch(() => null) : null,
    ]);
  const tokensHost = h('div');
  // The toggle is the one thing on this screen the user can change that the
  // server owns, so it is mirrored locally rather than re-fetched: a repaint
  // must not undo a switch that has already been written.
  let tuneOn = !!tuning?.chosen;

  let list = decisions;
  let listFilter = '';
  const listHost = h('div');

  function paint() {
    const panels = {
      account: () => h('div.panel',
        h('header', icon('user', 15), h('h3', 'Your account'),
          h('span.sp', session.user?.username || '')),
        h('div.pad',
          h('div.acct-row',
            h('span.acct-seal', { 'aria-hidden': 'true' },
              (session.user?.display || session.user?.username || '?').slice(0, 1).toUpperCase()),
            h('div',
              h('div.acct-name.brush', session.user?.display || session.user?.username),
              h('div.mono.muted',
                `${session.user?.role || 'member'} · signed in for up to `
                + `${session.sessionDays} days`)),
            h('button.btn.sm', { onclick: changeOwnPassword },
              hl(), icon('key', 13), h('span', 'Change password'))),
          session.user?.is_admin ? null : h('p.muted',
            { style: { fontSize: '12.5px', marginTop: '10px' } },
            'Forgotten your password, or need to reach another server? '
            + 'An admin can sort both out — there is no self-service reset '
            + 'here on purpose.'))),

      role: () => h('div.panel',
        h('header', icon('shield', 15), h('h3', 'What you may do')),
        h('div.pad',
          h('p.muted', { style: { fontSize: '12.5px' } },
            session.user?.is_admin ? 'An admin may do everything, on every server.'
              : 'Your role, plus anything an admin added for you. Grants on single '
                + 'servers are listed under them.'),
          h('div.chiprow.role-seals', ...(session.user?.permissions || []).map((p) =>
            h('span.role-seal', icon('check', 11), h('span', p)))),
          Object.keys(session.user?.server_permissions || {}).length
            ? h('div', { style: { marginTop: '12px' } },
              ...Object.entries(session.user.server_permissions).map(([sid, perms]) =>
                h('div.grant-line',
                  h('a', { href: `#/i/${sid}/overview` },
                    state.instanceById.get(sid)?.name || sid.slice(0, 8)),
                  h('span.muted', ` — ${perms.join(', ')}`))))
            : null)),

      tokens: () => h('div.panel', h('header', icon('key', 15), h('h3', 'API tokens'),
        h('span.sp', 'for scripts and bots')), h('div.pad', tokensHost)),

      gateLink: () => (isAdmin() ? h('a.gate-door', { href: '#/gate' },
        h('span.gd-torii', { 'aria-hidden': 'true' }, icon('torii', 30)),
        h('span', h('b.brush', 'The Gate'),
          h('span.muted', 'Accounts, nodes, the status page, every token, connections, '
            + 'the assistant, upkeep and the ledger live there.')),
        icon('arrowRight', 16)) : null),

      connections: () => h('div.panel',
        h('header', icon('link', 15), h('h3', 'Connections')),
        h('div.pad', { style: { display: 'grid', gap: '12px' } },
          checkRow('Servers', health.checks?.engine,
            engineLabel(health.checks?.engine?.driver || health.config?.engine),
            health.checks?.engine?.ok
              ? `${health.checks.engine.servers} servers on `
                + `${health.checks.engine.nodes} node`
                + `${health.checks.engine.nodes === 1 ? '' : 's'} · `
                + `${health.checks.engine.latency_ms} ms`
              : null),
          checkRow('Database', health.checks?.database,
            health.checks?.database?.url,
            health.checks?.database?.ok
              ? (health.checks.database.production_ready
                ? `Postgres · ${health.checks.database.url}`
                : 'SQLite fallback — fine for trying it, not for real '
                  + 'accounts. Set DATABASE_URL to a Postgres.')
              : null),
          checkRow('Job queue', health.checks?.queue,
            health.checks?.queue?.mode === 'worker' ? 'Redis + worker' : 'in-process',
            health.checks?.queue?.ok
              ? (health.checks.queue.mode === 'worker'
                ? `${health.checks.queue.workers} worker`
                  + `${health.checks.queue.workers === 1 ? '' : 's'} · `
                  + `${health.checks.queue.queued} waiting`
                : 'jobs run inside the API; a restart stops an install')
              : null),
          checkRow('CurseForge', health.checks?.curseforge, 'api.curseforge.com'),
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
              health = await refreshHealth();
              toast('Re-checked.', 'ok');
              paint();
            },
          }, icon('refresh', 13), h('span', 'Check again')),
        )),

      appearance: () => h('div.panel',
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
            + 'stays pink instead of turning fluorescent. Ctrl K jumps anywhere.'),
          h('div', { style: { position: 'relative', textAlign: 'center',
            padding: '10px 0' } },
            h('div.fox-halo'),
            mascot('piglindance', 110, { cls: 'fox' })),
        )),

      cache: () => (cache ? h('div.panel',
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
          }, icon('trash', 13), h('span', 'Prune now')))) : null),

      updates: () => (updates ? h('div.panel',
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
          }, icon('search', 13), h('span', 'Sweep now')))) : null),

      assistant: () => (ai ? assistantPanel() : h('div.note', 'The assistant is not configured.')),

      decisions: () => h('div.panel',
        h('header', icon('shieldCheck', 15), h('h3', 'Client-only decisions'),
          h('span.sp', `${list.counts?.allow ?? 0} allowed · `
            + `${list.counts?.block ?? 0} blocked`)),
        h('div.pad', listHost)),
    };

    const layouts = {
      you: [['account', 'role'], ['tokens'], ['appearance', 'gateLink']],
      connections: [['connections']],
      assistant: [['assistant']],
      upkeep: [['cache', 'updates'], ['decisions']],
    };
    const rows = layouts[which] || layouts.you;
    mount(node,
      which === 'you' ? h('header.page-head',
        h('div', h('p.eyebrow', 'BlessForge · ', h('b', 'yours')),
          h('h1.brush.ph-title', 'Settings'),
          h('p.lede', 'Your sign-in, your keys and how the shrine looks to you. '
            + (isAdmin() ? 'Everything about the whole install is behind The Gate.' : ''))),
        mascot('wave', 84, { cls: 'ph-fox' })) : null,
      ...rows.map((names) => h(`div.grid.${names.length > 1 ? 'g2' : 'g1'}.set-row`,
        { style: { alignItems: 'start' } }, ...names.map((n) => panels[n]()))),
    );
    if (rows.flat().includes('decisions')) paintList();
    if (rows.flat().includes('tokens')) paintTokens();
  }

  /* --- your API tokens ------------------------------------------
     A token acts as you with less: the permissions you tick, and only
     the servers you pick. It is shown once, when it is made; the list
     only ever has its first eleven characters. */
  async function paintTokens() {
    mount(tokensHost, spinner('Reading your tokens'));
    let data;
    try { data = await api.get('/api/auth/tokens'); } catch (e) {
      mount(tokensHost, h('div.note.bad', e.message)); return;
    }
    mount(tokensHost,
      h('p.muted', { style: { fontSize: '13px' } },
        'For scripts, Discord bots and Home Assistant: send it as ',
        h('code', 'Authorization: Bearer bf_…'),
        '. A token can never do more than you can, and cannot reach accounts, other '
        + 'tokens or the admin side. An account with two-factor sign-in uses one as its '
        + 'SFTP password.'),
      h('div.btnrow', { style: { margin: '10px 0 14px' } },
        h('button.btn.sm.primary', { onclick: () => newToken(data) }, hl(), icon('plus', 13),
          h('span', 'Make a token'))),
      data.items.length ? h('div.modlist', ...data.items.map((t) => h('div.modrow',
        h('div.ico', icon('key', 15)),
        h('div.who',
          h('div.nm', t.name),
          h('div.fn', `${t.prefix}… · made ${ago(t.created)}`
            + `${t.last_used ? ` · used ${ago(t.last_used)}` : ' · never used'}`
            + `${t.expires ? ` · expires ${new Date(t.expires * 1000).toLocaleDateString()}` : ''}`),
          h('div.tags', ...t.permissions.map((p) => pill(p, 'ghost')),
            t.servers.length ? pill(`${t.servers.length} server${t.servers.length === 1 ? '' : 's'}`, 'info')
              : pill('every server you can reach', 'ok'))),
        h('div.acts', h('button.btn.xs.danger', {
          onclick: async () => {
            const ok = await confirmDialog({ title: `Revoke ${t.name}?`,
              message: 'Anything using it stops working at once.', confirmLabel: 'Revoke it',
              danger: true, art: 'vanish' });
            if (!ok) return;
            try { await api.del(`/api/auth/tokens/${t.id}`); paintTokens(); } catch (e) { toastError(e); }
          },
        }, icon('trash', 11), h('span', 'Revoke'))))))
        : h('div.note', 'No tokens yet.'));
  }

  function newToken(data) {
    const name = h('input.inp', { placeholder: 'Discord bot' });
    const perms = new Set(['server.view']);
    const servers = new Set();
    let days = 90;
    const permChips = h('div.chiprow');
    const serverChips = h('div.chiprow');
    const paintChips = () => {
      mount(permChips, ...data.grantable.map((p) => h('button.chip', { type: 'button',
        'aria-pressed': String(perms.has(p)),
        onclick: () => { if (perms.has(p)) perms.delete(p); else perms.add(p); paintChips(); } }, p)));
      mount(serverChips, ...state.instances.map((s) => h('button.chip', { type: 'button',
        'aria-pressed': String(servers.has(s.server_id)),
        onclick: () => {
          if (servers.has(s.server_id)) servers.delete(s.server_id); else servers.add(s.server_id);
          paintChips();
        } }, s.name || s.server_id.slice(0, 8))));
    };
    paintChips();
    modal({
      title: 'Make an API token',
      wide: true,
      body: h('div',
        field('Name', name, 'So you know later what uses it.'),
        h('div.field', h('label', 'It may'), permChips),
        h('div.field', h('label', 'On these servers'), serverChips,
          h('div.help', 'None picked: every server you can reach.')),
        h('div.field', h('label', 'Expires'), segmented([
          { value: 7, label: '7 days' }, { value: 30, label: '30 days' },
          { value: 90, label: '90 days' }, { value: 365, label: 'A year' },
          { value: 0, label: 'Never' }], days, (v) => { days = v; }, 'Expires'))),
      footer: h('div.btnrow',
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', {
          onclick: async () => {
            if (!name.value.trim()) { name.focus(); return; }
            try {
              const made = await api.post('/api/auth/tokens', {
                name: name.value.trim(), permissions: [...perms], servers: [...servers],
                expires_days: days || null });
              showOnce(made);
              paintTokens();
            } catch (e) { toastError(e, 'No token was made'); }
          },
        }, hl(), h('span', 'Make it'))),
    });
  }

  function showOnce(made) {
    let saved = false;
    const done = h('button.btn.primary', { disabled: true, onclick: () => close() }, hl(),
      h('span', 'Done'));
    modal({
      title: 'Your new token — shown once',
      dismissable: false,
      body: h('div',
        h('div.note.warn', h('b', 'Copy it now'),
          'BlessForge keeps only its fingerprint. Close this and it cannot be shown again; '
          + 'you would make a new one.'),
        h('pre.token-once.mono', made.token),
        h('div.btnrow', { style: { margin: '10px 0' } },
          h('button.btn.sm', { onclick: () => copyText(made.token, 'The token') }, hl(),
            icon('copy', 12), h('span', 'Copy'))),
        toggle('I have saved it somewhere safe', false, (v) => { saved = v; done.disabled = !saved; })),
      footer: h('div.btnrow', done),
    });
  }

  function assistantPanel() {
    return h('div.panel',
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
        tuning ? h('div', { style: { display: 'grid', gap: '10px',
          borderTop: '2px solid var(--rule)', paddingTop: '12px' } },
        toggle('Let it tune servers quietly', tuneOn, async (v) => {
          try {
            const res = await api.post('/api/ai/tuning', { enabled: v });
            tuneOn = !!(res.chosen ?? res.enabled);
            toast(tuneOn
              ? 'On. The Tune screen will fold its suggestions in as they arrive.'
              : 'Off. Tuning is deterministic again.',
            res.persisted === false ? 'warn' : 'ok');
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
            + 'moved. It never blocks the page and never asks you anything.',
        }),
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
      ));
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
          type: 'search', placeholder: 'Search decisions…', 'data-keep': 'decision-filter',
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

/* Changing your own password. Needs the current one, which is what makes
   it different from an admin reset: an unattended, signed-in browser must
   not be a way to take the account over. */
function changeOwnPassword() {
  let current = '';
  let next = '';
  let again = '';
  const { close: shut } = modal({
    title: 'Change your password',
    body: h('div.adm-form',
      field('Current password', h('input.inp', {
        type: 'password', autocomplete: 'current-password', autofocus: true,
        oninput: (e) => { current = e.target.value; },
      })),
      field('New password', h('input.inp', {
        type: 'password', autocomplete: 'new-password',
        oninput: (e) => { next = e.target.value; },
      }), 'At least 10 characters.'),
      field('New password again', h('input.inp', {
        type: 'password', autocomplete: 'new-password',
        oninput: (e) => { again = e.target.value; },
      })),
      h('p.muted', { style: { fontSize: '12.5px' } },
        'Every other browser signed in as you will be signed out.')),
    footer: h('div.btnrow',
      h('button.btn.ghost', { onclick: () => shut() }, h('span', 'Cancel')),
      h('button.btn.primary', {
        onclick: async () => {
          if (next !== again) { toast('The two new passwords do not match.', 'bad'); return; }
          try {
            await api.post('/api/auth/password', { current, password: next });
            toast('Password changed. Other sessions signed out.', 'ok');
            shut();
          } catch (e) { toastError(e); }
        },
      }, hl(), h('span', 'Change it'))),
  });
}
