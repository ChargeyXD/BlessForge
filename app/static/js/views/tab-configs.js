/* ============================================================
   Configs — every mod's settings, grouped by the mod that owns
   them.

   This is the narrow, curated view: `config/`, `defaultconfigs/`,
   `kubejs/`, `scripts/` and the handful of loose files people
   legitimately edit. World data and binaries are excluded on
   purpose — the Files tab is where you go when you want the
   whole directory and are prepared to be careful.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, bytes, empty,
  loadingFox, debounce, segmented, frag, modal, close, confirmDialog,
} from '../core.js';
import { showEditor } from './tab-files.js';

export async function render(ctx) {
  const node = h('div');
  const host = h('div');
  mount(node, host, loadingFox('Walking the config folders'));

  let data = null;
  let query = '';
  let owner = '';

  async function load(quiet) {
    if (!quiet) mount(host, loadingFox('Walking the config folders'));
    try {
      data = await api.get(`/api/instances/${ctx.id}/configs`);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    paint();
  }

  const rows = () => {
    const q = query.trim().toLowerCase();
    return (data?.files || []).filter((f) => {
      if (owner && f.owner !== owner) return false;
      if (!q) return true;
      return f.path.toLowerCase().includes(q);
    });
  };

  function paint() {
    const list = rows();
    const groups = data.groups || [];

    mount(host,
      h('div.listbar',
        h('input.inp.grow', {
          type: 'search', 'data-search': '1', value: query,
          placeholder: `Search ${data.count} config files…`,
          'aria-label': 'Search config files',
          oninput: debounce((e) => { query = e.target.value; paint(); }, 160),
        }),
        h('select.inp', {
          style: { maxWidth: '220px' }, 'aria-label': 'Filter by owning mod',
          onchange: (e) => { owner = e.target.value; paint(); },
        },
          h('option', { value: '' }, `Every mod (${groups.length})`),
          ...groups.map((g) => h('option', {
            value: g.owner, selected: g.owner === owner,
          }, `${g.owner} — ${g.count}`))),
        h('div.grow'),
        h('button.btn.sm.ghost', { onclick: () => ctx.go('files') },
          icon('folder', 13), h('span', 'Whole directory')),
        h('button.btn.icon.sm.ghost', {
          'aria-label': 'Reload', onclick: () => load(),
        }, icon('refresh', 14)),
      ),

      !data.count
        ? empty('fox-mascot.png', 'No config files yet',
          'Mods write their configs the first time the server starts. Start '
          + 'it once and they will appear here.',
          h('button.btn.primary', { onclick: () => ctx.go('overview') },
            hl(), h('span', 'Back to Overview')))
        : !list.length
          ? h('div.note', 'Nothing matches that.')
          : h('div.modlist', ...list.slice(0, 600).map(fileRow)),

      list.length > 600
        ? h('p.muted', { style: { marginTop: '10px', fontSize: '12.5px' } },
          `Showing the first 600 of ${list.length}. Narrow it with the search `
          + 'box or the mod filter.')
        : null,
    );
  }

  function fileRow(f) {
    return h('div.modrow',
      h('div.ico', icon('fileText', 15)),
      h('div.who',
        h('div.nm', f.name),
        h('div.fn', f.path),
        h('div.tags',
          pill(f.owner, 'ghost'),
          f.size ? pill(f.size, 'ghost') : null,
          !f.editable ? pill('not editable', 'dead',
            'This file type is not opened in the editor') : null)),
      h('div.acts',
        h('button.btn.xs', {
          disabled: !f.editable,
          onclick: () => openConfig(f),
        }, hl(), icon('edit', 12), h('span', 'Edit'))));
  }

  async function openConfig(f) {
    const wait = h('div', loadingFox('Opening'));
    modal({ title: f.name, body: wait, wide: true });
    try {
      const doc = await api.get(
        `/api/instances/${ctx.id}/configs/read?path=${encodeURIComponent(f.path)}`);
      // The Files editor knows how to save through the files endpoint, which
      // snapshots first; using it here means one editor, not two.
      showEditor(ctx, {
        ...doc, name: f.name, bytes: f.bytes || 0,
        critical: f.path === 'server.properties',
      }, () => load(true));
    } catch (e) {
      mount(wait, h('div.note.bad', e.message));
    }
  }

  await load();
  return { node };
}
