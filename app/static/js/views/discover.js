/* ============================================================
   Discover — the CurseForge catalogue, and your own exports.

   The important half of this screen is the review that happens
   between choosing a pack and installing it. Client-only mods
   have to be inert on a server, but removing them silently is
   how a working pack becomes a missing-dependency crash and how
   a mod you wanted vanishes with nothing to say where it went.
   So the list is shown first, with the evidence behind each
   call, and flagged jars are installed *disabled* rather than
   dropped.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, num, compact,
  empty, loadingFox, confirmDialog, modal, close, frag, debounce, segmented,
  field, toggle, prefs, qs, ago, skeletonGrid,
} from '../core.js';
import { run, runAwait } from '../jobs.js';
import { state, go, topActions, refreshInstances, bannerIfUnhealthy } from '../app.js';

export async function render() {
  const node = h('div.wrap.wide');
  let mode = prefs.get('discover.mode', 'packs');
  let query = '';
  let mcVersion = '';
  let loader = '';
  let page = 0;
  let versions = [];

  const results = h('div');

  topActions(
    h('a.btn.sm.ghost', { href: '#/roulette' }, icon('dice', 14),
      h('span', 'Roulette')),
    h('a.btn.sm.primary', { href: '#/create' }, hl(), icon('plus', 14),
      h('span', 'New server')),
  );

  api.get('/api/meta/minecraft-versions')
    .then((d) => { versions = d.items || []; paintBar(); })
    .catch(() => {});

  const barHost = h('div');

  function paintBar() {
    mount(barHost,
      h('div.sec-head.mon',
        h('div',
          h('p.eyebrow', 'CurseForge · Modrinth · ', h('b', 'your own exports')),
          h('h2', 'Discover')),
        h('div.grow'),
        segmented([
          { value: 'packs', label: 'Modpacks' },
          { value: 'mods', label: 'Mods' },
          { value: 'import', label: 'Import an export' },
        ], mode, (v) => {
          mode = v; prefs.set('discover.mode', v); page = 0; paint();
        }, 'What to browse')),

      mode !== 'import'
        ? h('div.listbar',
          h('input.inp.grow', {
            type: 'search', 'data-search': '1', value: query,
            placeholder: mode === 'packs' ? 'Search modpacks…' : 'Search mods…',
            'aria-label': 'Search',
            oninput: debounce((e) => { query = e.target.value; page = 0; search(); }, 260),
          }),
          h('select.inp', {
            style: { maxWidth: '160px' }, 'aria-label': 'Minecraft version',
            onchange: (e) => { mcVersion = e.target.value; page = 0; search(); },
          },
            h('option', { value: '' }, 'Any version'),
            ...versions.slice(0, 80).map((v) => h('option', {
              value: v, selected: v === mcVersion,
            }, v))),
          h('select.inp', {
            style: { maxWidth: '150px' }, 'aria-label': 'Loader',
            onchange: (e) => { loader = e.target.value; page = 0; search(); },
          },
            h('option', { value: '' }, 'Any loader'),
            ...['Forge', 'NeoForge', 'Fabric', 'Quilt'].map((l) => h('option', {
              value: l, selected: l === loader,
            }, l))),
          h('div.grow'),
          h('button.btn.icon.sm.ghost', {
            'aria-label': 'Previous page', disabled: page === 0,
            onclick: () => { page = Math.max(0, page - 1); search(); },
          }, icon('chevronLeft', 14)),
          h('span.mono.muted', `page ${page + 1}`),
          h('button.btn.icon.sm.ghost', {
            'aria-label': 'Next page',
            onclick: () => { page += 1; search(); },
          }, icon('chevronRight', 14)))
        : null,
    );
  }

  function paint() {
    mount(node, bannerIfUnhealthy(), barHost, results);
    paintBar();
    if (mode === 'import') renderImport(results);
    else search();
  }

  const search = debounce(async () => {
    mount(results, skeletonGrid(8));
    try {
      const path = mode === 'packs' ? '/api/browse/modpacks' : '/api/browse/mods';
      const data = await api.get(path + qs({
        q: query, game_version: mcVersion, loader,
        index: page * 24, page_size: 24,
      }));
      const items = data.items || data.data || [];
      if (!items.length) {
        mount(results, h('div.note',
          query ? 'Nothing matches that.'
            : 'The catalogue returned nothing — check the CurseForge key in '
              + 'Settings.'));
        return;
      }
      mount(results, h('div.grid.gauto.bleed.stagger',
        ...items.map((m) => packCard(m, mode))));
    } catch (e) {
      mount(results, h('div.note.bad', e.message));
    }
  }, 120);

  function packCard(m, kind) {
    return h('div.card.hoverable.pkcard',
      h('div.head',
        m.logo ? h('img.art', { src: m.logo, alt: '', loading: 'lazy' })
          : h('div.art', { style: { display: 'grid', placeItems: 'center' } },
            icon('box', 22)),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('h3', m.name),
          h('div.by', (m.authors || []).slice(0, 2).join(', ')))),
      h('p', m.summary || ''),
      h('div.chiprow',
        ...(m.categories || []).slice(0, 3).map((c) => pill(c, 'ghost'))),
      h('div.foot',
        pill(`${compact(m.downloads)} downloads`, 'ghost'),
        m.updated ? pill(ago(m.updated), 'ghost') : null,
        h('div', { style: { flex: 1 } }),
        kind === 'packs'
          ? h('button.btn.sm.primary', { onclick: () => openPack(m) },
            hl(), icon('download', 13), h('span', 'Install'))
          : h('a.btn.sm.ghost', {
            href: m.url, target: '_blank', rel: 'noopener',
          }, icon('link', 13), h('span', 'Open'))));
  }

  /* --- installing a pack --------------------------------------- */

  async function openPack(m) {
    const host = h('div', loadingFox('Reading releases'));
    modal({ title: m.name, body: host, wide: true });
    try {
      const data = await api.get(`/api/modpacks/${m.id}/files`
        + qs({ game_version: mcVersion, page_size: 40 }));
      const files = data.items || [];
      if (!files.length) {
        mount(host, h('div.note.warn', 'No releases are listed for that filter.'));
        return;
      }
      mount(host,
        h('p.muted', { style: { fontSize: '13px', marginBottom: '12px' } },
          'Any version, not just the latest. Where a release ships an official '
          + 'server pack it is used; where it does not — which is common — the '
          + 'manifest is read and every server-side mod is fetched '
          + 'individually.'),
        h('div.modlist', ...files.slice(0, 30).map((f) => h('div.modrow',
          h('div.who',
            h('div.nm', f.display_name),
            h('div.fn', f.file_name, f.size ? ` · ${bytes(f.size)}` : ''),
            h('div.tags',
              pill(f.release_type || 'release',
                f.release_type === 'release' ? 'ok' : 'warn'),
              ...(f.game_versions || []).slice(0, 2).map((g) => pill(g, 'ghost')),
              f.server_pack_file_id
                ? pill('has a server pack', 'info',
                  'The publisher ships a server build of this release')
                : pill('manifest install', 'plum',
                  'No server pack; the mods are fetched individually'))),
          h('div.acts', h('button.btn.xs.primary', {
            onclick: () => startInstall(m, f),
          }, hl(), h('span', 'Choose')))))));
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
    }
  }

  async function startInstall(pack, file) {
    close();
    let review;
    try {
      review = await runAwait('/api/install/preflight',
        { mod_id: pack.id, file_id: file.file_id }, {
          title: `Analysing ${pack.name}`,
        });
    } catch (e) {
      toastError(e, 'The pack could not be analysed');
      return;
    }
    showReview(review, { mod_id: pack.id, file_id: file.file_id },
      pack.name);
  }

  function showReview(review, ref, packName) {
    const candidates = review.review?.candidates || [];
    const exclude = new Set();
    const disable = new Set(candidates
      .filter((c) => c.recommendation === 'remove')
      .map((c) => c.file_name));

    const form = {
      name: packName.slice(0, 50),
      port: nextFreePort(),
      mem_max: review.suggested_mem_max || review.memory?.heap_gb || 6,
      motd: '',
      optimize: true,
      review: true,
    };

    const body = h('div');
    const paint = () => mount(body,
      h('div.kpis', { style: { marginBottom: '16px' } },
        stat('Minecraft', review.minecraft || '?'),
        stat('Loader', `${review.loader || '?'}`,
          review.loader_version || ''),
        stat('Mods', String(review.review?.total_mods ?? '?'),
          `${review.review?.server_mods ?? 0} server-side`),
        stat('Heap', `${form.mem_max} GB`,
          review.memory?.capped_by_host ? 'capped by this host' : 'from the pack',
          review.memory?.capped_by_host ? 'warn' : '')),

      (review.warnings || []).length
        ? h('div.note.warn', { style: { marginBottom: '14px' } },
          h('b', 'Worth knowing before you commit'),
          h('ul', { style: { margin: '4px 0 0 18px' } },
            ...review.warnings.map((w) => h('li', w))))
        : null,

      h('div.grid.g2', { style: { marginBottom: '16px' } },
        field('Server name', h('input.inp', {
          value: form.name, maxLength: 60,
          oninput: (e) => { form.name = e.target.value; },
        })),
        field('Port', h('input.inp', {
          type: 'number', value: form.port, min: 1024, max: 65535,
          oninput: (e) => { form.port = e.target.value; },
        }))),

      candidates.length
        ? frag(
          h('h4.h-num', { style: { marginBottom: '6px' } },
            `${candidates.length} mods look client-only`),
          h('p.muted', { style: { fontSize: '13px', marginBottom: '10px' } },
            'Ticked mods are installed but left disabled — the jar is written '
            + 'as .jar.disabled, tagged on the Mods tab with the reason, and '
            + 'is one click from coming back. Untick the lot and the pack '
            + 'installs exactly as published.'),
          h('div.modlist', { style: { maxHeight: '340px', overflow: 'auto' } },
            ...candidates.map((c) => reviewRow(c, disable, exclude, paint))))
        : h('div.note.ok',
          h('b', 'Nothing in this pack looks client-only'),
          'Every jar was read and checked against Modrinth by file hash.'),

      h('div', { style: { display: 'grid', gap: '10px', marginTop: '16px' } },
        toggle('Tune the JVM for this host', form.optimize,
          (v) => { form.optimize = v; })),
    );
    paint();

    modal({
      title: `Install ${packName}`,
      wide: true,
      body,
      footer: frag(
        h('span.mono.muted', { style: { marginRight: 'auto', fontSize: '11px' } },
          `${disable.size} disabled · ${exclude.size} skipped`),
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', {
          onclick: () => {
            close();
            run('/api/install/modpack', {
              ...ref,
              server_name: form.name.trim() || packName,
              port: Number(form.port),
              mem_max: Number(form.mem_max),
              motd: form.motd || null,
              optimize: form.optimize,
              exclude_files: [...exclude],
              disable_files: [...disable],
              client_reasons: Object.fromEntries(candidates
                .filter((c) => disable.has(c.file_name))
                .map((c) => [c.file_name, c.reasons || []])),
            }, {
              title: `Install ${form.name || packName}`,
              onEnd: (rec) => {
                if (rec.status !== 'done') return;
                refreshInstances({ quiet: true });
                const r = rec.result || {};
                toast(`${r.mods_installed} mods installed into ${r.name}.`,
                  'ok', { title: 'Installed', timeout: 9000 });
                if (r.server_id) go(`/i/${r.server_id}/overview`);
              },
            });
          },
        }, hl(), icon('download', 14), h('span', 'Install')),
      ),
    });
  }

  function reviewRow(c, disable, exclude, paint) {
    const tone = { remove: 'bad', review: 'warn', keep: 'plum' }[c.recommendation];
    const label = { remove: 'client-only', review: 'worth a look',
      keep: 'protected' }[c.recommendation];
    return h('div.modrow',
      h('input', {
        type: 'checkbox', checked: disable.has(c.file_name),
        disabled: c.recommendation === 'keep',
        'aria-label': `Install ${c.name} disabled`,
        onchange: (e) => {
          if (e.target.checked) disable.add(c.file_name);
          else disable.delete(c.file_name);
          paint();
        },
      }),
      c.logo ? h('img.ico', { src: c.logo, alt: '', loading: 'lazy' })
        : h('div.ico', (c.name || '?').slice(0, 2).toUpperCase()),
      h('div.who',
        h('div.nm', c.name),
        h('div.fn', c.file_name),
        h('div.tags',
          pill(label, tone),
          c.confidence ? pill(c.confidence, 'ghost') : null,
          c.score !== undefined ? pill(`score ${c.score}`, 'ghost') : null,
          c.whitelisted ? pill('you allowed this', 'ok') : null),
        (c.reasons || []).length
          ? h('div.muted', { style: { fontSize: '12px', marginTop: '3px' } },
            c.reasons[0])
          : null,
        c.required_by_others?.length
          ? h('div.muted', { style: { fontSize: '12px' } },
            `Needed by ${c.required_by_others.join(', ')}`)
          : null),
      h('div.acts',
        h('button.btn.icon.xs.ghost', {
          'aria-label': `Why ${c.name} was flagged`,
          onclick: () => showWhy(c),
        }, icon('info', 12)),
        h('button.btn.icon.xs.ghost', {
          'aria-label': `Do not install ${c.name} at all`,
          title: exclude.has(c.file_name) ? 'Will be skipped' : 'Skip entirely',
          onclick: () => {
            if (exclude.has(c.file_name)) exclude.delete(c.file_name);
            else { exclude.add(c.file_name); disable.delete(c.file_name); }
            paint();
          },
        }, icon(exclude.has(c.file_name) ? 'check' : 'trash', 12))));
  }

  function showWhy(c) {
    modal({
      title: c.name,
      body: frag(
        h('div.chiprow', { style: { marginBottom: '12px' } },
          pill(c.recommendation, 'ghost'),
          pill(`score ${c.score ?? '?'}`, 'ghost'),
          pill(c.confidence || 'low', 'ghost')),
        h('div', { style: { display: 'grid', gap: '8px' } },
          ...(c.findings || []).map((f) => h('div', {
            style: { display: 'flex', gap: '10px', padding: '8px 10px',
              border: '2px solid var(--rule)',
              background: f.points > 0 ? 'var(--danger-soft)'
                : f.points < 0 ? 'var(--sage-soft)' : 'var(--paper-2)' },
          },
            h('b.mono', { style: { minWidth: '40px', textAlign: 'right' } },
              f.points > 0 ? `+${f.points}` : String(f.points)),
            h('div.wrapany', { style: { fontSize: '13px' } }, f.why)))),
      ),
      footer: h('button.btn.ghost', { onclick: () => close() },
        h('span', 'Close')),
    });
  }

  function stat(label, value, sub, tone = '') {
    return h('div.kpi', h('div.lbl', label),
      h(`div.v${tone ? `.${tone}` : ''}`, { style: { fontSize: '19px' } }, value),
      sub ? h('div.sub', sub) : null);
  }

  function nextFreePort() {
    const used = new Set(state.instances.map((s) => s.port));
    let p = 25565;
    while (used.has(p) && p < 25700) p += 1;
    return p;
  }

  /* --- import ---------------------------------------------------- */

  async function renderImport(hostEl) {
    mount(hostEl, loadingFox('Reading imported archives'));
    let uploads;
    try {
      uploads = await api.get('/api/uploads');
    } catch (e) {
      mount(hostEl, h('div.note.bad', e.message));
      return;
    }

    const drop = h('div.drop', { tabIndex: 0, role: 'button',
      'aria-label': 'Choose a modpack export to upload' },
      h('img', { src: '/assets/allay-helping.png', alt: '',
        style: { width: '90px', marginBottom: '10px' } }),
      h('h3', 'Drop a CurseForge export here'),
      h('p.muted', { style: { fontSize: '13.5px', maxWidth: '56ch',
        margin: '0 auto' } },
        'In the CurseForge app: My Modpacks → the … menu on your profile → '
        + 'Create Profile Export. Tick Mods, Config and any script folders. '
        + 'Client-only folders are dropped automatically, so it does no harm '
        + 'to leave them ticked.'),
      h('div.btnrow', { style: { justifyContent: 'center', marginTop: '14px' } },
        h('button.btn.primary', { onclick: pick }, hl(), icon('upload', 14),
          h('span', 'Choose a .zip'))));

    function pick() {
      const input = h('input', {
        type: 'file', accept: '.zip', style: { display: 'none' },
        onchange: (e) => upload(e.target.files[0]),
      });
      document.body.append(input);
      input.click();
      setTimeout(() => input.remove(), 60000);
    }

    drop.addEventListener('dragover', (e) => {
      e.preventDefault(); drop.classList.add('over');
    });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (e) => {
      e.preventDefault();
      drop.classList.remove('over');
      const file = e.dataTransfer.files[0];
      if (file) upload(file);
    });
    drop.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
    });

    async function upload(file) {
      if (!file) return;
      if (!file.name.toLowerCase().endsWith('.zip')) {
        toast('That is not a .zip — a CurseForge export is always a zip.', 'bad');
        return;
      }
      const t = toast(`Uploading ${file.name} (${bytes(file.size)})…`, 'ok',
        { timeout: 900000 });
      const form = new FormData();
      form.append('file', file);
      try {
        const rec = await api.upload('/api/uploads/modpack', form);
        t.remove();
        toast(`${rec.file_name} stored.`, 'ok');
        renderImport(hostEl);
        installUpload(rec);
      } catch (e) {
        t.remove();
        toastError(e, 'The upload failed');
      }
    }

    mount(hostEl, drop,
      h('h3.h-num', { style: { margin: '22px 0 10px' } },
        `Archives on the server (${(uploads.items || []).length} of ${uploads.limit})`),
      (uploads.items || []).length
        ? h('div.modlist', ...uploads.items.map(uploadRow))
        : h('div.note', 'Nothing imported yet. An archive stays here so '
          + 're-installing does not mean uploading 900 MB a second time.'));

    function uploadRow(u) {
      const s = u.summary || {};
      return h('div.modrow',
        h('div.ico', icon('archive', 15)),
        h('div.who',
          h('div.nm', s.name || u.file_name),
          h('div.fn', `${u.file_name} · ${bytes(u.size)} · ${ago(u.uploaded_at)}`),
          h('div.tags',
            s.minecraft ? pill(s.minecraft, 'ghost') : null,
            s.loader ? pill(s.loader, 'ghost') : null,
            s.mod_count ? pill(`${s.mod_count} mods`, 'ghost') : null,
            s.kind ? pill(s.kind.replace('_', ' '), 'plum') : null)),
        h('div.acts',
          h('button.btn.xs.primary', { onclick: () => installUpload(u) },
            hl(), h('span', 'Install')),
          h('button.btn.icon.xs.ghost', {
            'aria-label': `Delete ${u.file_name}`,
            onclick: async () => {
              const ok = await confirmDialog({
                title: `Delete ${u.file_name}?`,
                message: 'A private export exists nowhere else unless you '
                  + 'still have the zip.',
                confirmLabel: 'Delete', danger: true,
              });
              if (!ok) return;
              await api.del(`/api/uploads/${u.upload_id}`);
              toast('Deleted.', 'ok');
              renderImport(hostEl);
            },
          }, icon('trash', 12))));
    }
  }

  async function installUpload(u) {
    let review;
    try {
      review = await runAwait('/api/install/preflight',
        { upload_id: u.upload_id }, { title: `Analysing ${u.file_name}` });
    } catch (e) {
      toastError(e, 'The archive could not be analysed');
      return;
    }
    showReview(review, { upload_id: u.upload_id },
      review.pack?.name || u.file_name.replace(/\.zip$/i, ''));
  }

  paint();
  return { node };
}
