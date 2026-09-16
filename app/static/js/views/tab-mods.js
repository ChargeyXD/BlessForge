/* ============================================================
   Mods / Plugins — what is installed, and how to change it.

   One module serves both because the operations are identical;
   only the folder and the catalogue behind "Add" differ. That
   difference is not cosmetic: CurseForge has no plugin section
   at all, so a Paper server searches Modrinth and every result
   is checked against what Paper can actually load.

   Disabling renames a jar to `.jar.disabled`, which every loader
   ignores — the same convention Crafty and the CurseForge app
   use. Nothing here deletes without being asked twice.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, num, compact,
  empty, loadingFox, confirmDialog, modal, close, field, debounce, segmented,
  frag, prefs, qs, copyText, sideTag, spinner, reveal, sourceMark, ago,
  endlessFeed,
} from '../core.js';
import { run, runAwait } from '../jobs.js';

export async function render(ctx) {
  const dir = ctx.modDir;
  const node = h('div');
  // One delegated listener for the whole list rather than one per row: the
  // list is repainted on every filter keystroke and a per-row handler would
  // be rebuilt hundreds of times for a large mods folder.
  const listHost = komaHost(h('div'));
  const barHost = h('div');
  mount(node, barHost, listHost);
  mount(listHost, loadingFox(`Reading ${dir}/`));

  let listing = null;
  let updates = null;
  let query = '';
  let filter = prefs.get(`mods.filter.${dir}`, 'all');
  const selected = new Set();

  async function load(quiet) {
    if (!quiet) mount(listHost, loadingFox(`Reading ${dir}/`));
    try {
      listing = await api.get(`/api/instances/${ctx.id}/mods?directory=${dir}`);
    } catch (e) {
      mount(listHost, h('div.note.bad', e.message));
      return;
    }
    // A selection that survives a reload must not keep files that are gone.
    const present = new Set(listing.mods.map((m) => m.file));
    for (const f of [...selected]) if (!present.has(f)) selected.delete(f);
    paint();
  }

  const sideOk = (v) => v === 'required' || v === 'optional';

  const filtered = () => {
    const q = query.trim().toLowerCase();
    return (listing?.mods || []).filter((m) => {
      if (filter === 'enabled' && !m.enabled) return false;
      if (filter === 'disabled' && m.enabled) return false;
      if (filter === 'client'
        && !(m.client_only || m.server_side === 'unsupported')) return false;
      if (filter === 'server' && m.client_side !== 'unsupported') return false;
      if (filter === 'both'
        && !(sideOk(m.server_side) && sideOk(m.client_side))) return false;
      if (filter === 'unknown' && m.identified) return false;
      if (filter === 'updates') {
        const u = updates?.items?.find((x) => x.file === m.file);
        if (!u?.has_update) return false;
      }
      if (!q) return true;
      return (m.name || '').toLowerCase().includes(q)
        || m.file.toLowerCase().includes(q);
    });
  };

  function paint() {
    if (!listing) return;
    const rows = filtered();
    const all = listing.mods || [];

    mount(barHost, h('div.listbar',
      h('input.inp.grow', {
        type: 'search', 'data-keep': 'mods-filter', value: query,
        placeholder: `Search ${all.length} ${dir}…`,
        'aria-label': `Search ${dir}`,
        oninput: debounce((e) => { query = e.target.value; paint(); }, 180),
      }),
      segmented([
        { value: 'all', label: `All ${all.length}` },
        { value: 'enabled', label: `On ${all.filter((m) => m.enabled).length}` },
        { value: 'disabled', label: `Off ${all.filter((m) => !m.enabled).length}` },
        ...(updates?.items?.some((u) => u.has_update)
          ? [{ value: 'updates',
            label: `Updates ${updates.items.filter((u) => u.has_update).length}` }]
          : []),
        ...(all.some((m) => m.client_only || m.server_side === 'unsupported')
          ? [{ value: 'client', label: 'Client only' }] : []),
        ...(all.some((m) => m.client_side === 'unsupported')
          ? [{ value: 'server', label: 'Server only' }] : []),
        ...(all.some((m) => sideOk(m.server_side) && sideOk(m.client_side))
          ? [{ value: 'both', label: 'Both' }] : []),
        ...(all.some((m) => !m.identified) ? [{ value: 'unknown', label: 'Unknown' }] : []),
      ], filter, (v) => { filter = v; prefs.set(`mods.filter.${dir}`, v); paint(); },
        'Filter'),
      h('div.grow'),
      h('button.btn.sm.primary', { onclick: openAdd }, hl(), icon('plus', 13),
        h('span', ctx.plugins ? 'Add plugin' : 'Add mod')),
      h('button.btn.icon.sm.ghost', {
        'aria-label': 'Check for updates', title: 'Check for updates',
        onclick: checkUpdates,
      }, icon('refresh', 14)),
      !ctx.plugins && h('button.btn.icon.sm.ghost', {
        'aria-label': 'Identify unknown jars', title: 'Identify unknown jars',
        onclick: identify,
      }, icon('search', 14)),
      ctx.plugins && h('button.btn.icon.sm.ghost', {
        'aria-label': 'Audit the plugins folder', title: 'Audit plugins/',
        onclick: auditPlugins,
      }, icon('shieldCheck', 14)),
    ));

    if (selected.size) {
      barHost.append(h('div.listbar', { style: { marginTop: '-8px' } },
        h('b', `${selected.size} selected`),
        h('button.btn.sm.ghost', { onclick: () => bulk(true) },
          icon('check', 13), h('span', 'Enable')),
        h('button.btn.sm.ghost', { onclick: () => bulk(false) },
          icon('minus', 13), h('span', 'Disable')),
        h('button.btn.sm.danger', { onclick: bulkDelete },
          icon('trash', 13), h('span', 'Delete')),
        h('div.grow'),
        h('button.btn.sm.ghost', {
          onclick: () => { selected.clear(); paint(); },
        }, h('span', 'Clear'))));
    }

    if (!all.length) {
      mount(listHost, empty('fox-mascot.png',
        `Nothing in ${dir}/ yet`,
        ctx.plugins
          ? 'This is a Paper-family server, so it loads Bukkit plugins from '
            + 'plugins/. Modrinth is the source — CurseForge does not carry '
            + 'plugins at all.'
          : 'Add a mod from CurseForge or Modrinth, and its dependencies come '
            + 'with it automatically.',
        h('button.btn.primary', { onclick: openAdd }, hl(), icon('plus', 14),
          h('span', ctx.plugins ? 'Browse plugins' : 'Browse mods'))));
      return;
    }
    if (!rows.length) {
      mount(listHost, h('div.note', 'Nothing matches that filter.'));
      return;
    }

    const unknownSides = all.filter((m) => !m.server_side && !m.client_side).length;
    mount(listHost,
      unknownSides && !ctx.plugins
        ? h('div.note', { style: { marginBottom: '12px' } },
          h('b', `${unknownSides} of ${all.length} jars have no side recorded`),
          'Which half of the game a mod runs on is stated by its publisher, '
          + "and the only reliable way to ask is by the file's hash. ",
          h('button.btn.xs', {
            style: { marginTop: '8px' },
            onclick: () => detectSides(),
          }, icon('shieldCheck', 12), h('span', 'Look them up')))
        : null,
      h('div.modlist', ...rows.map(row)));
  }

  /* Reuses the client-only scan, which already downloads every jar and asks
     Modrinth by SHA-1 — and now writes the answer into the instance manifest,
     so this is a one-time cost per instance rather than per visit. */
  function detectSides() {
    run(`/api/instances/${ctx.id}/client-scan?directory=${dir}`, {}, {
      title: `Identify mod sides — ${ctx.name}`,
      onEnd: (rec) => {
        if (rec.status !== 'done') return;
        const n = (rec.result?.items || [])
          .filter((i) => i.modrinth_side).length;
        toast(n
          ? `${n} jars matched a Modrinth project and now carry a side tag.`
          : 'No jar could be matched — these are probably CurseForge '
            + 'exclusives, which publish no side information.',
        n ? 'ok' : 'warn', { timeout: 8000 });
        load(true);
      },
    });
  }

  function row(m) {
    const u = updates?.items?.find((x) => x.file === m.file);
    const isSel = selected.has(m.file);
    const title = m.name || m.file;
    const el = h(`div.modrow${m.enabled ? '' : '.off'}${isSel ? '.sel' : ''}`,
      h('input', {
        type: 'checkbox', checked: isSel,
        'aria-label': `Select ${m.name || m.file}`,
        onchange: (e) => {
          if (e.target.checked) selected.add(m.file); else selected.delete(m.file);
          paint();
        },
      }),
      m.logo
        ? h('img.ico', { src: m.logo, alt: '', loading: 'lazy' })
        : h('div.ico', (m.name || m.file).slice(0, 2).toUpperCase()),
      h('div.who',
        // A real button, not a clickable div: it keeps the row openable from
        // the keyboard without inventing a role, and it sits in the tab order
        // beside the controls instead of in front of them.
        h('div.nm', m.project_id
          ? h('button.koma-open', {
            type: 'button', 'aria-haspopup': 'dialog',
            title: `What ${title} is, from its ${SOURCE_LABEL[m.source] || 'catalogue'} page`,
          }, title)
          : title),
        h('div.fn', m.file, m.size ? ` · ${m.size}` : ''),
        h('div.tags',
          // The side tag leads, because it is the question this tab exists
          // to answer and every other chip is metadata by comparison.
          sideTag(m),
          m.version ? pill(m.version, 'ghost') : null,
          sourceMark(m.source),
          !m.enabled ? pill('disabled', 'dead') : null,
          u?.has_update ? pill(`update → ${u.latest_version || 'newer'}`, 'info') : null,
          !m.identified && !ctx.plugins ? pill('unidentified', 'dead') : null,
          m.required_by ? pill(`needed by ${m.required_by}`, 'plum') : null,
        )),
      h('div.acts',
        u?.has_update ? h('button.btn.xs.primary', {
          onclick: () => applyUpdate(m, u),
        }, hl(), icon('download', 12), h('span', 'Update')) : null,
        h('button.btn.icon.xs.ghost', {
          'aria-label': m.enabled ? `Disable ${m.name || m.file}`
            : `Enable ${m.name || m.file}`,
          title: m.enabled ? 'Disable' : 'Enable',
          onclick: () => toggleOne(m),
        }, icon(m.enabled ? 'eye' : 'minus', 13)),
        h('button.btn.icon.xs.ghost', {
          'aria-label': `More actions for ${m.name || m.file}`,
          onclick: () => rowMenu(m),
        }, icon('sliders', 13)),
      ));

    // An unidentified jar matches no catalogue project, so there is no source
    // page to open — it stays an ordinary row rather than a dead click.
    if (m.project_id) {
      komaRow(el, m, {
        minecraft: ctx.minecraft,
        loader: ctx.plugins ? null : ctx.loader,
        family: ctx.plugins ? (ctx.loader || 'paper') : null,
        kind: ctx.plugins ? 'plugin' : 'mod',
        installed: m,
        update: u,
        primary: {
          label: 'Change version', icon: 'layers',
          onclick: () => openVersions(m),
        },
      });
    }
    return el;
  }

  /* --- single operations --------------------------------------- */

  async function toggleOne(m) {
    try {
      await api.post(`/api/instances/${ctx.id}/mods/toggle`,
        { file: m.file, enabled: !m.enabled, directory: dir });
      toast(`${m.name || m.file} ${m.enabled ? 'disabled' : 'enabled'}. `
        + 'Restart for it to take effect.', 'ok', { timeout: 3000 });
      await load(true);
    } catch (e) { toastError(e); }
  }

  async function bulk(enabled) {
    const files = [...selected];
    try {
      const res = await api.post(`/api/instances/${ctx.id}/mods/bulk-toggle`,
        { files, enabled, directory: dir });
      toast(`${res.changed.length} ${enabled ? 'enabled' : 'disabled'}`
        + (res.snapshot ? ' — a snapshot was taken first, undo it from Undo.' : ''),
      res.errors.length ? 'warn' : 'ok');
      selected.clear();
      await load(true);
    } catch (e) { toastError(e); }
  }

  async function bulkDelete() {
    const files = [...selected];
    const ok = await confirmDialog({
      title: `Delete ${files.length} file${files.length > 1 ? 's' : ''}?`,
      message: 'The jars are removed from the server. Disabling is reversible; '
        + 'this is not.',
      detail: files.slice(0, 8).join('\n') + (files.length > 8
        ? `\n…and ${files.length - 8} more` : ''),
      confirmLabel: 'Delete', danger: true,
    });
    if (!ok) return;
    try {
      await api.post(`/api/instances/${ctx.id}/mods/delete`,
        { files, directory: dir });
      toast(`${files.length} deleted.`, 'ok');
      selected.clear();
      await load(true);
    } catch (e) { toastError(e); }
  }

  function rowMenu(m) {
    modal({
      title: m.name || m.file,
      body: frag(
        h('div.chiprow', { style: { marginBottom: '14px' } },
          m.version ? pill(m.version, 'ghost') : null,
          sourceMark(m.source),
          pill(m.enabled ? 'enabled' : 'disabled', m.enabled ? 'ok' : 'dead')),
        h('div.mono.wrapany.muted', { style: { marginBottom: '14px' } }, m.file),
        (m.client_only_reasons || []).length
          ? h('div.note.warn', h('b', 'Flagged as client-side because'),
            h('ul', { style: { margin: '4px 0 0 18px', padding: 0 } },
              ...m.client_only_reasons.map((r) => h('li', r))))
          : null,
        h('div', { style: { display: 'grid', gap: '9px', marginTop: '14px' } },
          m.project_id ? h('button.btn.sm', {
            onclick: () => { close(); openVersions(m); },
          }, hl(), icon('layers', 13), h('span', 'Change version')) : null,
          h('button.btn.sm', {
            onclick: () => { close(); markDecision(m, 'allow'); },
          }, hl(), icon('shieldCheck', 13),
            h('span', 'Always treat as server-safe')),
          h('button.btn.sm', {
            onclick: () => { close(); markDecision(m, 'block'); },
          }, hl(), icon('ban', 13), h('span', 'Always treat as client-only')),
          h('button.btn.sm.ghost', {
            onclick: () => copyText(m.file, 'Filename'),
          }, icon('copy', 13), h('span', 'Copy filename')),
          h('button.btn.sm.danger', {
            onclick: async () => {
              close();
              const ok = await confirmDialog({
                title: `Delete ${m.file}?`,
                message: 'Disabling is reversible. Deleting is not.',
                confirmLabel: 'Delete', danger: true,
              });
              if (!ok) return;
              await api.post(`/api/instances/${ctx.id}/mods/delete`,
                { files: [m.file], directory: dir });
              toast('Deleted.', 'ok');
              load(true);
            },
          }, icon('trash', 13), h('span', 'Delete this jar')),
        ),
      ),
      footer: h('button.btn.ghost', { onclick: () => close() }, h('span', 'Close')),
    });
  }

  async function markDecision(m, verdict) {
    try {
      await api.post('/api/whitelist', {
        file: m.file, name: m.name, verdict,
        project_id: m.project_id, source: m.source,
        reason: verdict === 'allow'
          ? 'marked safe on a server from the mod list'
          : 'marked client-only from the mod list',
      });
      toast(verdict === 'allow'
        ? `${m.name || m.file} will not be flagged again, on any server.`
        : `${m.name || m.file} will always be disabled by the client-only scan.`,
      'ok');
    } catch (e) { toastError(e); }
  }

  /* --- versions ------------------------------------------------ */

  async function openVersions(m) {
    const host = h('div', loadingFox('Reading versions'));
    modal({ title: `Versions of ${m.name || m.file}`, body: host, wide: true });
    try {
      const src = m.source || 'curseforge';
      const data = await api.get(
        `/api/mods/${src}/${m.project_id}/versions`
        + qs({ game_version: ctx.minecraft, loader: ctx.loader }));
      const items = data.items || [];
      if (!items.length) {
        mount(host, h('div.note.warn',
          `No build of this ${ctx.plugins ? 'plugin' : 'mod'} is published for `
          + `${ctx.loader} ${ctx.minecraft}.`));
        return;
      }
      mount(host, h('div.modlist', ...items.slice(0, 40).map((v) =>
        h('div.modrow',
          h('div.who',
            h('div.nm', v.display_name || v.version_number || v.file_name),
            h('div.fn', v.file_name, v.size ? ` · ${bytes(v.size)}` : ''),
            h('div.tags',
              pill(v.release_type || 'release',
                v.release_type === 'release' ? 'ok' : 'warn'),
              ...(v.game_versions || []).slice(0, 3).map((g) => pill(g, 'ghost')))),
          h('div.acts',
            v.file_name === m.file
              ? pill('installed', 'info')
              : h('button.btn.xs.primary', {
                onclick: () => switchVersion(m, v),
              }, hl(), h('span', 'Install')))))));
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
    }
  }

  async function switchVersion(m, v) {
    close();
    await run(`/api/instances/${ctx.id}/mods/add`, {
      source: m.source, project_id: m.project_id, file_id: v.file_id,
      directory: dir, replace_file: m.file, name: m.name,
      with_dependencies: true,
    }, {
      title: `Switch ${m.name} to ${v.version_number || v.display_name}`,
      onEnd: (rec) => { if (rec.status === 'done') load(true); },
    });
  }

  async function applyUpdate(m, u) {
    await run(`/api/instances/${ctx.id}/mods/add`, {
      source: m.source, project_id: m.project_id, file_id: u.latest_file_id,
      directory: dir, replace_file: m.file, name: m.name,
      with_dependencies: true,
    }, {
      title: `Update ${m.name}`,
      onEnd: (rec) => { if (rec.status === 'done') { updates = null; load(true); } },
    });
  }

  async function checkUpdates() {
    mount(listHost, loadingFox('Asking the catalogues'));
    try {
      updates = await api.get(`/api/instances/${ctx.id}/mods/updates`);
      const n = (updates.items || []).filter((u) => u.has_update).length;
      toast(n ? `${n} ${dir} have a newer build.` : 'Everything is current.',
        n ? 'warn' : 'ok');
      if (n) filter = 'updates';
    } catch (e) { toastError(e); }
    paint();
  }

  function identify() {
    run(`/api/instances/${ctx.id}/mods/identify?directory=${dir}`, {}, {
      title: 'Identify unknown jars',
      onEnd: (rec) => {
        if (rec.status !== 'done') return;
        const r = rec.result || {};
        toast(`${r.identified ?? 0} of ${r.total ?? 0} jars matched a catalogue `
          + 'project.', 'ok');
        load(true);
      },
    });
  }

  async function auditPlugins() {
    mount(listHost, loadingFox('Opening every jar in plugins/'));
    try {
      const a = await api.get(`/api/instances/${ctx.id}/plugins/audit`);
      paint();
      if (a.ok) { toast(a.note, 'ok'); return; }
      modal({
        title: `plugins/ — ${a.findings.length} problem(s)`,
        body: h('div', { style: { display: 'grid', gap: '10px' } },
          ...a.findings.map((f) => h(`div.note.${f.severity === 'critical' ? 'bad' : 'warn'}`,
            h('b', f.title), h('div.wrapany', f.detail)))),
        footer: h('button.btn.ghost', { onclick: () => close() }, h('span', 'Close')),
      });
    } catch (e) { toastError(e); paint(); }
  }

  /* --- add --------------------------------------------------- */

  function openAdd() {
    if (ctx.plugins) openPluginBrowser(ctx, () => load(true));
    else openModBrowser(ctx, () => load(true));
  }

  await load();
  return { node, dispose() { } };
}

/* ============================================================
   Add-a-mod browser (CurseForge + Modrinth)
   ============================================================ */

export function openModBrowser(ctx, onDone, initial = {}) {
  // `initial` exists so the detail popup can put the operator back where they
  // were. Opening a mod's panel replaces this dialog — there is one modal
  // host — and coming back to an empty search box would lose the search.
  let source = initial.source || prefs.get('mods.source', 'curseforge');
  let query = initial.query || '';
  const results = komaHost(h('div'));

  let feed = null;

  const search = debounce(() => {
    feed?.dispose();
    const grid = h('div.grid.gauto');
    // `back` no longer carries a page number: returning from a mod's detail
    // re-opens the browser on the same query, which re-fetches from the top.
    // Restoring a scroll offset into a catalogue that re-ranks per request
    // would land somewhere arbitrary, so it does not pretend to.
    const back = () => openModBrowser(ctx, onDone, { query, source });
    feed = endlessFeed({
      container: grid,
      size: 20,
      fetchPage: (index, size) => api.get('/api/browse/mods' + qs({
        q: query, source, game_version: ctx.minecraft,
        loader: ctx.loader, index, page_size: size,
      })),
      render: (m) => catalogueCard(m, source, ctx, onDone, back),
      empty: () => h('div.note',
        `Nothing on ${source} matches that for ${ctx.loader} ${ctx.minecraft}.`),
    });
    mount(results, grid, ...feed.nodes);
    feed.start();
  }, 220);

  modal({
    title: `Add a mod to ${ctx.name}`,
    wide: true,
    body: frag(
      h('div.listbar', { style: { position: 'static', marginBottom: '14px' } },
        h('input.inp.grow', {
          type: 'search', placeholder: 'Search mods…', autofocus: true,
          value: query, 'aria-label': 'Search mods',
          oninput: (e) => { query = e.target.value; search(); },
        }),
        segmented([
          { value: 'curseforge', label: 'CurseForge' },
          { value: 'modrinth', label: 'Modrinth' },
        ], source, (v) => {
          source = v; prefs.set('mods.source', v); search();
        }, 'Source'),
      ),
      h('p.muted', { style: { fontSize: '12.5px', marginBottom: '10px' } },
        `Filtered to ${ctx.loader} ${ctx.minecraft}. Dependencies are resolved `
        + 'and installed with whatever you pick.'),
      results,
    ),
    footer: h('button.btn.ghost', { onclick: () => close() }, h('span', 'Close')),
  });
  search();
}

function catalogueCard(m, source, ctx, onDone, back) {
  const card = h('div.card.hoverable.pkcard',
    h('div.head',
      m.logo ? h('img.art', { src: m.logo, alt: '', loading: 'lazy' })
        : h('div.art', { style: { display: 'grid', placeItems: 'center' } },
          icon('box', 20)),
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('h3', h('button.koma-open', {
          type: 'button', 'aria-haspopup': 'dialog',
          title: `What ${m.name} is, from its ${SOURCE_LABEL[source] || source} page`,
        }, m.name)),
        h('div.by', (m.authors || []).slice(0, 2).join(', ') || source))),
    h('p', m.summary || ''),
    h('div.foot',
      pill(`${compact(m.downloads)} downloads`, 'ghost'),
      source === 'modrinth' && m.server_side === 'unsupported'
        ? pill('client-only', 'bad', 'Its author says a server cannot use it')
        : null,
      h('div', { style: { flex: 1 } }),
      h('button.btn.sm.primary', {
        onclick: () => pickVersion(m, source, ctx, onDone),
      }, hl(), icon('plus', 13), h('span', 'Add'))));

  return komaRow(card, { ...m, source }, {
    minecraft: ctx.minecraft, loader: ctx.loader, kind: 'mod', back,
    primary: {
      label: 'Choose a build', icon: 'plus',
      onclick: () => pickVersion(m, source, ctx, onDone),
    },
  });
}

async function pickVersion(m, source, ctx, onDone) {
  const host = h('div', loadingFox('Reading builds'));
  modal({ title: `Add ${m.name}`, body: host, wide: true });
  try {
    const data = await api.get(`/api/mods/${source}/${m.id}/versions`
      + qs({ game_version: ctx.minecraft, loader: ctx.loader }));
    const items = data.items || [];
    if (!items.length) {
      mount(host, h('div.note.warn',
        `${m.name} publishes no build for ${ctx.loader} ${ctx.minecraft}.`));
      return;
    }
    mount(host,
      h('p.muted', { style: { marginBottom: '12px', fontSize: '13px' } },
        'Required dependencies are installed alongside; mods already present '
        + 'are detected even under a different filename, so you never get two '
        + 'copies.'),
      h('div.modlist', ...items.slice(0, 25).map((v) => h('div.modrow',
        h('div.who',
          h('div.nm', v.display_name || v.version_number),
          h('div.fn', v.file_name, v.size ? ` · ${bytes(v.size)}` : ''),
          h('div.tags', pill(v.release_type || 'release',
            v.release_type === 'release' ? 'ok' : 'warn'))),
        h('div.acts', h('button.btn.xs.primary', {
          onclick: async () => {
            close();
            await run(`/api/instances/${ctx.id}/mods/add`, {
              source, project_id: m.id, file_id: v.file_id,
              directory: ctx.modDir, name: m.name, with_dependencies: true,
            }, {
              title: `Install ${m.name}`,
              onEnd: (rec) => { if (rec.status === 'done') onDone?.(); },
            });
          },
        }, hl(), h('span', 'Install')))))));
  } catch (e) {
    mount(host, h('div.note.bad', e.message));
  }
}

/* ============================================================
   Plugin browser — Modrinth only, compatibility checked
   ============================================================ */

export function openPluginBrowser(ctx, onDone, initial = {}) {
  let query = initial.query || '';
  let category = initial.category || '';
  let sort = initial.sort || 'relevance';
  const results = komaHost(h('div'));

  const search = debounce(async () => {
    mount(results, loadingFox('Searching Modrinth'));
    try {
      const data = await api.get('/api/browse/plugins' + qs({
        q: query, family: ctx.loader || 'paper',
        game_version: ctx.minecraft, category, sort, page_size: 24,
      }));
      const items = data.items || [];
      if (!items.length) {
        mount(results, h('div.note',
          `Nothing on Modrinth matches that for ${data.family || ctx.loader} `
          + `${ctx.minecraft}.`));
        return;
      }
      const back = () =>
        openPluginBrowser(ctx, onDone, { query, category, sort });
      mount(results, h('div.grid.gauto',
        ...items.map((p) => pluginCard(p, ctx, onDone, back))));
    } catch (e) {
      mount(results, h('div.note.bad', e.message));
    }
  }, 220);

  api.get('/api/plugins/meta').then((meta) => {
    catSelect.append(...(meta.categories || []).map((c) =>
      h('option', { value: c.key, selected: c.key === category }, c.title)));
  }).catch(() => {});

  const catSelect = h('select.inp', {
    style: { maxWidth: '190px' }, 'aria-label': 'Category',
    onchange: (e) => { category = e.target.value; search(); },
  }, h('option', { value: '' }, 'Every category'));

  modal({
    title: `Add a plugin to ${ctx.name}`,
    wide: true,
    body: frag(
      h('div.listbar', { style: { position: 'static', marginBottom: '12px' } },
        h('input.inp.grow', {
          type: 'search', placeholder: 'Search plugins…', autofocus: true,
          value: query, 'aria-label': 'Search plugins',
          oninput: (e) => { query = e.target.value; search(); },
        }),
        catSelect,
        segmented([
          { value: 'relevance', label: 'Best' },
          { value: 'downloads', label: 'Popular' },
          { value: 'updated', label: 'Recent' },
        ], sort, (v) => { sort = v; search(); }, 'Sort'),
      ),
      h('div.note', { style: { marginBottom: '12px' } },
        h('b', 'Modrinth, not CurseForge'),
        'CurseForge has no plugin catalogue at all. Everything here is checked '
        + `against what ${ctx.loader || 'Paper'} can actually load — Paper runs `
        + 'Spigot and Bukkit plugins unchanged, Folia only runs plugins that '
        + 'explicitly support it.'),
      results,
    ),
    footer: h('button.btn.ghost', { onclick: () => close() }, h('span', 'Close')),
  });
  search();
}

const COMPAT_TONE = { exact: 'ok', good: 'info', unknown: 'dead',
  risky: 'warn', blocked: 'bad' };

function pluginCard(p, ctx, onDone, back) {
  const card = h('div.card.hoverable.pkcard',
    h('div.head',
      p.logo ? h('img.art', { src: p.logo, alt: '', loading: 'lazy' })
        : h('div.art', { style: { display: 'grid', placeItems: 'center' } },
          icon('puzzle', 20)),
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('h3', h('button.koma-open', {
          type: 'button', 'aria-haspopup': 'dialog',
          title: `What ${p.name} is, from its Modrinth page`,
        }, p.name)),
        h('div.by', (p.authors || []).join(', ') || 'Modrinth'))),
    h('p', p.summary || ''),
    h('div.chiprow',
      pill(p.compat, COMPAT_TONE[p.compat] || 'dead', p.compat_note),
      ...(p.display_categories || []).slice(0, 2).map((c) => pill(c, 'ghost'))),
    h('div.foot',
      pill(`${compact(p.downloads)} downloads`, 'ghost'),
      h('div', { style: { flex: 1 } }),
      h('button.btn.sm.primary', {
        disabled: p.compat === 'blocked',
        title: p.compat === 'blocked' ? p.compat_note : null,
        onclick: () => resolvePlugin(p, ctx, onDone),
      }, hl(), icon('plus', 13), h('span', 'Add'))));

  return komaRow(card, p, {
    minecraft: ctx.minecraft, family: ctx.loader || 'paper', kind: 'plugin',
    compat: p.compat, compatNote: p.compat_note, back,
    primary: p.compat === 'blocked' ? null : {
      label: 'Plan the install', icon: 'plus',
      onclick: () => resolvePlugin(p, ctx, onDone),
    },
  });
}

async function resolvePlugin(p, ctx, onDone) {
  // Resolve first, then build the dialog. Reaching into an open modal to
  // swap its footer works until it does not, and a plan with no Install
  // button is a dead end the user cannot get out of.
  const wait = h('div', loadingFox('Resolving dependencies'));
  modal({ title: `Add ${p.name}`, body: wait });

  let plan;
  try {
    plan = await api.post(`/api/instances/${ctx.id}/plugins/resolve`, {
      project_id: p.id, family: ctx.loader, game_version: ctx.minecraft,
    });
  } catch (e) {
    mount(wait, h('div.note.bad', e.message));
    return;
  }

  const skip = new Set();
  const install = async () => {
    close();
    await run(`/api/instances/${ctx.id}/plugins/add`, {
      project_id: p.id, name: p.name, family: ctx.loader,
      game_version: ctx.minecraft, with_dependencies: true,
      skip_dependencies: [...skip],
    }, {
      title: `Install ${p.name}`,
      onEnd: (rec) => {
        if (rec.status !== 'done') return;
        if (rec.result?.note) toast(rec.result.note, 'ok', { timeout: 9000 });
        onDone?.();
      },
    });
  };

  const rows = plan.plan.filter((x) => x.role !== 'missing');
  const toInstall = rows.filter((x) => !x.present);

  modal({
    title: `Add ${p.name}`,
    wide: true,
    body: frag(
      h('p.muted', { style: { fontSize: '13px', marginBottom: '12px' } },
        plan.dependencies.length
          ? `${p.name} needs ${plan.dependencies.length} other plugin`
            + `${plan.dependencies.length > 1 ? 's' : ''}. Untick anything you `
            + 'already have another way.'
          : `${p.name} has no dependencies.`),
      plan.missing.length
        ? h('div.note.bad', { style: { marginBottom: '12px' } },
          h('b', 'Some requirements have no build for this version'),
          plan.missing.map((mm) => mm.compat_note).join(' '))
        : null,
      h('div.modlist', ...rows.map((x) => h('div.modrow',
        x.role === 'dependency'
          ? h('input', {
            type: 'checkbox', checked: true,
            'aria-label': `Install ${x.name}`,
            onchange: (e) => {
              if (e.target.checked) skip.delete(String(x.project_id));
              else skip.add(String(x.project_id));
            },
          })
          : h('div', { style: { width: '18px' } }),
        x.logo ? h('img.ico', { src: x.logo, alt: '', loading: 'lazy' })
          : h('div.ico', icon('puzzle', 14)),
        h('div.who',
          h('div.nm', x.name),
          h('div.fn', x.file_name || '', x.size ? ` · ${bytes(x.size)}` : ''),
          h('div.tags',
            pill(x.role === 'requested' ? 'this plugin' : 'dependency',
              x.role === 'requested' ? 'ghost' : 'plum'),
            x.present ? pill('already installed', 'ok') : null,
            x.compat ? pill(x.compat, COMPAT_TONE[x.compat] || 'dead',
              x.compat_note) : null))))),
      h('div.note', { style: { marginTop: '12px' } },
        h('b', 'Paper does not hot-load plugins'),
        'Restart the server after installing, or the new jar sits there doing '
        + 'nothing.'),
    ),
    footer: frag(
      h('span.mono.muted', { style: { marginRight: 'auto', fontSize: '11px' } },
        `${toInstall.length} to download · ${bytes(plan.total_bytes || 0)}`),
      h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
      h('button.btn.primary', {
        disabled: !toInstall.length,
        onclick: install,
      }, hl(), icon('download', 14),
        h('span', toInstall.length ? 'Install' : 'Nothing to do')),
    ),
  });
}

/* ============================================================
   KOMA — a mod or plugin's source page, as a comic panel.

   A koma is one frame of a manga page, and that is what this
   is: a hard-edged panel with an offset shadow, action lines
   radiating from behind it, halftone under the body copy and
   the summary in a speech balloon with a torn tail. It is built
   out of the vocabulary the rest of the app already speaks —
   the 2.5px edge, the -8deg skew, gold leaf, sakura — rather
   than out of a generic comic template.

   Three things about it are load-bearing rather than styling:

   * It reuses core.js's `modal()`. That is where the focus
     trap, Escape, the scrim and the scroll lock live, and a
     second dialog implementation would have to get all four
     right again. The only thing added on top is a class on the
     returned panel.
   * The description NEVER arrives as markup. The server flattens
     both CurseForge's HTML and Modrinth's Markdown into typed
     text blocks, and every one of them is passed to `h()` as a
     string, which becomes a text node. There is no innerHTML on
     this path and no sanitiser allow-list to get wrong later.
   * It opens from a click anywhere on the row EXCEPT a control,
     and from the keyboard through a real button on the title —
     so the row gains a way in without the controls inside it
     losing their place in the tab order.
   ============================================================ */

const KOMA = new WeakMap();

const SOURCE_LABEL = { curseforge: 'CurseForge', modrinth: 'Modrinth' };

/* What a click inside a row may land on without opening the panel:
   anything interactive. The install button, the enable toggle, the select
   checkbox, a link out. `.koma-open` — the title — is the one exception,
   because it exists to open the panel. */
const KOMA_CONTROLS = 'a[href],button,input,select,textarea,label,summary,'
  + '[role="button"],[contenteditable]';

/* The one client-side gate on a third-party URL. The Python clients already
   filter these to http(s); doing it again at the point of use means a link
   in this panel can never become a `javascript:` because some later caller
   passed an unfiltered field. */
function httpUrl(url) {
  if (!url) return null;
  const text = String(url).trim();
  return /^https?:\/\//i.test(text) ? text : null;
}

/* Mark a row or card as carrying a detail panel. The payload is held in a
   WeakMap rather than in data- attributes: a row is a live object with an
   id, a side, a compat verdict and an installed file on it, and stringifying
   that into the DOM only to parse it back is a way to lose the types. */
export function komaRow(el, item, opts = {}) {
  const id = item && (item.project_id ?? item.id);
  if (id === undefined || id === null || id === '') return el;
  KOMA.set(el, { item, opts });
  el.dataset.koma = '1';
  return el;
}

/* One delegated listener per list, bound once. These lists repaint on every
   keystroke, and a handler per row would be rebuilt hundreds of times over
   for a large mods folder. */
export function komaHost(host) {
  if (host.dataset.komaHost) return host;
  host.dataset.komaHost = '1';
  host.addEventListener('click', (e) => {
    const row = e.target.closest('[data-koma]');
    if (!row || !host.contains(row)) return;
    const control = e.target.closest(KOMA_CONTROLS);
    if (control && row.contains(control)
        && !control.classList.contains('koma-open')) return;
    const rec = KOMA.get(row);
    if (rec) openKoma(rec.item, rec.opts);
  });
  return host;
}

export function openKoma(item, opts = {}) {
  const source = String(item.source || opts.source || 'curseforge').toLowerCase();
  const id = item.project_id ?? item.id;
  const name = item.name || item.file || 'This project';

  // What the row already knew. The panel is drawn from this immediately and
  // upgraded when the catalogue answers — so the wait is never a blank box,
  // and a catalogue that never answers still leaves something readable.
  const seed = {
    source,
    kind: opts.kind || 'mod',
    name,
    summary: item.summary || '',
    logo: item.logo || null,
    authors: (item.authors || []).filter(Boolean),
    categories: item.display_categories || item.categories || [],
    downloads: item.downloads,
    date_modified: item.updated || null,
    url: httpUrl(item.url),
    client_side: item.client_side || null,
    server_side: item.server_side || null,
    client_only: item.client_only,
    client_only_reasons: item.client_only_reasons,
  };

  const host = h('div.koma');
  const state = { seed, detail: null, error: null, loading: true };

  const dialog = modal({
    title: name,
    wide: true,
    body: host,
    footer: komaFooter(opts),
  });
  dialog.panel.classList.add('koma-modal');

  const paint = () => mount(host, komaView(state, opts, load));

  async function load() {
    state.loading = true;
    state.error = null;
    paint();
    try {
      const detail = await api.get(
        `/api/mods/${source}/${encodeURIComponent(id)}/detail`
        + qs({
          game_version: opts.minecraft,
          loader: opts.loader,
          family: opts.family,
        }));
      // The operator may well have closed the panel while this was in
      // flight; painting into a detached node is harmless but pointless.
      if (!host.isConnected) return;
      state.detail = detail;
    } catch (e) {
      if (!host.isConnected) return;
      state.error = e.message || String(e);
    }
    state.loading = false;
    paint();
  }

  paint();
  load();
}

function komaFooter(opts) {
  const primary = opts.primary;
  return frag(
    // There is one modal host, so opening this panel replaced whatever was
    // behind it. Coming back to an empty search box would lose the search,
    // so the browser is reopened on the terms it was left on.
    opts.back
      ? h('button.btn.sm.ghost', { onclick: () => opts.back() },
        icon('arrowLeft', 14), h('span', 'Back to results'))
      : null,
    h('div.grow'),
    h('button.btn.sm.ghost', { onclick: () => close() }, h('span', 'Close')),
    primary
      ? h('button.btn.sm.primary', { onclick: primary.onclick }, hl(),
        icon(primary.icon || 'plus', 14), h('span', primary.label))
      : null,
  );
}

function komaView(st, opts, retry) {
  const d = st.detail || {};
  const s = st.seed;
  const wait = (node) => (st.loading
    ? h('span.skel', { style: { display: 'inline-block', width: '70px' } })
    : node);

  const name = d.name || s.name;
  const logo = d.logo_full || d.logo || s.logo;
  // The caller's word beats the catalogue's here: a Modrinth plugin
  // searches as `plugin` and reads back as `project_type: mod`, so a
  // panel that trusted the project would call every plugin a mod.
  const kind = opts.kind || d.kind || 'mod';
  const authors = (d.authors || []).length ? d.authors : s.authors;
  const cats = (d.categories || []).length ? d.categories : s.categories;
  const downloads = d.downloads ?? s.downloads;
  const updated = d.date_modified || s.date_modified;
  const offer = d.offer;
  const inst = opts.installed;
  const where = SOURCE_LABEL[s.source] || 'the catalogue';
  const glyph = kind === 'plugin' ? 'puzzle' : 'box';

  const cell = (label, value, small, extra) => h(`div.koma-cell${extra || ''}`,
    h('div.k', label), h(`div.v${small ? '.sm' : ''}`, value));

  const links = [
    ['Project page', d.url || s.url, 'link'],
    ['Source code', d.source_url, 'terminal'],
    ['Issues', d.issues_url, 'alert'],
    ['Wiki', d.wiki_url, 'fileText'],
    ['Discord', d.discord_url, 'users'],
  ].filter((row) => httpUrl(row[1]));

  return frag(
    h('div.koma-burst', { 'aria-hidden': 'true' }),
    h('div.koma-panel',

      h('div.koma-hero',
        h('div.koma-artwrap',
          logo
            ? h('img.koma-art', {
              src: logo, alt: '', loading: 'lazy', referrerPolicy: 'no-referrer',
              onerror: (e) => e.target.replaceWith(
                h('div.koma-art', icon(glyph, 26))),
            })
            : h('div.koma-art', icon(glyph, 26))),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('span.koma-kicker', h('span', `${where} ${kind}`)),
          h('h2.koma-name', name),
          h('div.koma-by',
            sourceMark(s.source),
            authors.length
              ? h('span', `by ${authors.slice(0, 3).join(', ')}`)
              : h('span.muted', 'no author published')))),

      h('div.koma-balloon', d.summary || s.summary
        || 'This project publishes no summary of itself.'),

      h('div.chiprow',
        sideTag({
          server_side: d.server_side ?? s.server_side,
          client_side: d.client_side ?? s.client_side,
          client_only: s.client_only,
          client_only_reasons: s.client_only_reasons,
        }),
        opts.compat
          ? pill(opts.compat, COMPAT_TONE[opts.compat] || 'dead', opts.compatNote)
          : null,
        ...cats.slice(0, 7).map((c) => pill(c, 'ghost'))),

      h('div.koma-strip',
        // Gold leaf needs a dark ground to read as gold rather than as pale
        // yellow, and --slab-bg inverts between themes. The lacquer cell is
        // dark in both, which is the only place in this panel the foil is
        // legible in light mode.
        cell('Downloads', downloads === undefined || downloads === null
          ? '—' : h('span.foil', compact(downloads)), false, '.lacquer'),
        s.source === 'modrinth'
          ? cell('Followers', wait(compact(d.follows)))
          : null,
        cell('Last updated', updated ? ago(updated) : '—', true),
        cell('Licence', wait(licenceText(d, s)), true),
        inst ? cell('Installed', inst.version || inst.file, true) : null,
        d.loaders?.length ? cell('Loaders', d.loaders.join(', '), true) : null),

      h('div.koma-h', 'The build on offer'),
      komaOffer(st, opts, offer, inst),

      h('div.koma-links',
        ...links.map(([label, href, glyphName]) => h('a.btn.sm.ghost', {
          href: httpUrl(href), target: '_blank', rel: 'noopener noreferrer',
        }, icon(glyphName, 13), h('span', label))),
        !links.length && !st.loading
          ? h('span.muted', { style: { fontSize: '12.5px' } },
            'This project links nowhere else.')
          : null),

      (d.gallery || []).length ? komaGallery(d.gallery) : null,

      st.error
        ? komaDead(st.error, s, retry)
        : frag(
          h('div.koma-h', `What ${where} says about it`),
          h('div.koma-read',
            st.loading
              ? h('div.koma-skel',
                spinner('Reading the source page'),
                ...[88, 96, 72, 92, 60].map((w) => h('div.skel', {
                  style: { width: `${w}%` },
                })))
              : (d.body_blocks || []).length
                ? frag(...d.body_blocks.map(komaBlock))
                : h('p.muted', 'This project publishes no description.'))),
    ),
  );
}

/* The whole strip goes when the last screenshot fails, heading and all: a
   section rule with nothing under it reads as a broken page rather than as
   a project with no screens. */
function komaGallery(gallery) {
  const strip = h('div.koma-gal');
  const wrap = h('div.koma-galwrap',
    h('div.koma-h', 'From the project page'), strip);
  strip.append(...gallery.slice(0, 6).map((g) => h('img', {
    src: g.thumb || g.url, alt: g.title || '', loading: 'lazy',
    referrerPolicy: 'no-referrer', title: g.title || '',
    onerror: (e) => {
      e.target.remove();
      if (!strip.childElementCount) wrap.remove();
    },
  })));
  return wrap;
}

/* Every block is handed to h() as a STRING, which becomes a text node. That
   is the whole defence: a description written by a third party has no path
   to being parsed as markup here, because nothing on this path parses. */
function komaBlock(b) {
  if (b.t === 'rule') return h('hr');
  if (b.t === 'h') return h('h4', b.text);
  if (b.t === 'li') return h('div.koma-li', h('span', b.text));
  if (b.t === 'quote') return h('div.koma-q', b.text);
  if (b.t === 'code') return h('pre', b.text);
  return h('p', b.text);
}

function licenceText(d, s) {
  const lic = d.license;
  if (lic) return lic.name || lic.id || '—';
  if (!d.source) return '—';
  // Not a gap in this app: CurseForge's Core API carries no licence field on
  // a project at all, and an em dash there reads as a failed load.
  return s.source === 'curseforge' ? 'not in the CurseForge API' : 'none stated';
}

function komaOffer(st, opts, offer, inst) {
  if (st.loading) {
    return h('div.koma-offer',
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('div.skel', { style: { width: '46%', height: '15px' } }),
        h('div.skel', { style: { width: '70%', marginTop: '6px' } })));
  }
  if (st.error) {
    return h('div.note.warn',
      'Which build fits this server could not be checked while the '
      + 'catalogue is unreachable.');
  }
  if (!offer) {
    const target = [opts.family || opts.loader, opts.minecraft]
      .filter(Boolean).join(' ');
    return h('div.note.warn',
      h('b', 'Nothing published for this server'),
      target
        ? `No build of this ${st.seed.kind} is published for ${target}.`
        : 'No build of this project could be listed.');
  }
  const same = inst && String(offer.file_id) === String(inst.file_id);
  const update = opts.update;
  return h('div.koma-offer',
    h('div', { style: { flex: 1, minWidth: 0 } },
      h('div.nm', offer.display_name || offer.version_number
        || offer.file_name || 'A build'),
      h('div.fn',
        offer.file_name || '',
        offer.size ? ` · ${bytes(offer.size)}` : '',
        offer.date ? ` · ${ago(offer.date)}` : '')),
    pill(offer.release_type || 'release',
      offer.release_type === 'release' ? 'ok' : 'warn'),
    same ? pill('already installed', 'info') : null,
    !same && update?.has_update
      ? pill(`update → ${update.latest_version || 'newer'}`, 'plum')
      : null);
}

function komaDead(message, seed, retry) {
  const where = SOURCE_LABEL[seed.source] || 'The catalogue';
  const noKey = /CURSEFORGE_API_KEY/i.test(message || '');
  return h('div.koma-dead',
    h('span.koma-bang', h('span', 'No signal')),
    h('p', `${where} did not answer, so the description, the licence and the `
      + 'build on offer are all missing. Everything above came from the '
      + 'search result itself and is still true.'),
    noKey
      ? h('p.muted', 'This one is fixable here: CurseForge needs an API key, '
        + 'and there is a field for it in Settings.')
      : null,
    h('div.mono.wrapany.muted', { style: { fontSize: '11.5px' } }, message),
    h('div.btnrow',
      h('button.btn.sm', { onclick: retry }, hl(), icon('refresh', 13),
        h('span', 'Try again')),
      noKey
        ? h('a.btn.sm.ghost', { href: '#/settings', onclick: () => close() },
          icon('gear', 13), h('span', 'Open settings'))
        : null,
      httpUrl(seed.url)
        ? h('a.btn.sm.ghost', {
          href: httpUrl(seed.url), target: '_blank', rel: 'noopener noreferrer',
        }, icon('link', 13), h('span', `Open on ${where}`))
        : null));
}
