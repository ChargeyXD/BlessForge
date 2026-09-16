/* ============================================================
   Configs — the list and the editor, side by side.

   It used to open a file in a modal, which is the wrong shape
   for this job: comparing two mods' settings meant opening,
   reading, closing, opening. Now the list stays put and the
   editor lives beside it, so moving between files is one click
   and the thing you were comparing against never leaves the
   screen.

   Unsaved work survives switching files. A buffer is held per
   path and the row is marked, because losing an edit to a
   mis-click is the one failure this screen must not have.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, empty,
  loadingFox, spinner, debounce, frag, confirmDialog, copyText, segmented,
} from '../core.js';

export async function render(ctx) {
  const node = h('div');
  const listHost = h('div.cfg-list');
  const editorHost = h('div.cfg-editor');
  const barHost = h('div');
  mount(node, barHost, h('div.cfg', listHost, editorHost));

  let data = null;
  let query = '';
  let owner = '';
  let current = null;              // { path, name, content, language, ... }
  const buffers = new Map();       // path -> unsaved text
  let area = null;
  let gutter = null;

  async function load(quiet) {
    if (!quiet) mount(listHost, spinner('Walking the config folders'));
    try {
      data = await api.get(`/api/instances/${ctx.id}/configs`);
    } catch (e) {
      mount(listHost, h('div.note.bad', e.message));
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
    const dirty = buffers.size;

    mount(barHost, h('div.listbar',
      h('input.inp.grow', {
        type: 'search', 'data-keep': 'config-filter', value: query,
        placeholder: `Search ${data.count} config files…`,
        'aria-label': 'Search config files',
        oninput: debounce((e) => { query = e.target.value; paint(); }, 160),
      }),
      h('select.inp', {
        style: { maxWidth: '210px' }, 'aria-label': 'Filter by owning mod',
        onchange: (e) => { owner = e.target.value; paint(); },
      },
        h('option', { value: '' }, `Every mod (${groups.length})`),
        ...groups.map((g) => h('option', {
          value: g.owner, selected: g.owner === owner,
        }, `${g.owner} — ${g.count}`))),
      h('div.grow'),
      dirty
        ? h('span.pill.warn', `${dirty} unsaved`)
        : null,
      h('button.btn.sm.ghost', { onclick: () => ctx.go('files') },
        icon('folder', 13), h('span', 'Whole directory')),
      h('button.btn.icon.sm.ghost', {
        'aria-label': 'Reload the file list', onclick: () => load(),
      }, icon('refresh', 14)),
    ));

    if (!data.count) {
      mount(listHost, h('div', { style: { padding: '18px' } },
        h('div.note', h('b', 'No config files yet'),
          'Mods write their settings the first time the server starts. Start '
          + 'it once and they will appear here.')));
      mount(editorHost, emptySlot());
      return;
    }

    mount(listHost,
      h('div', { style: { padding: '10px 12px', borderBottom: '2.5px solid var(--rule)',
        fontFamily: 'var(--mono)', fontSize: '10px', letterSpacing: '.12em',
        textTransform: 'uppercase', color: 'var(--faint)' } },
        `${list.length} file${list.length === 1 ? '' : 's'}`),
      h('div.rows', ...(list.length
        ? list.slice(0, 800).map(fileRow)
        : [h('div', { style: { padding: '18px' } },
          h('p.muted', 'Nothing matches that.'))])));

    if (!current) mount(editorHost, emptySlot());
  }

  function emptySlot() {
    return frag(
      h('header', icon('fileText', 14), h('span.nm', 'No file open')),
      h('div.empty-slot',
        h('div', { style: { position: 'relative', display: 'inline-block' } },
          h('div.fox-halo'),
          h('img.fox', { src: '/assets/fox-head.png', alt: '',
            style: { width: '110px' } })),
        h('p', { style: { marginTop: '12px' } },
          'Pick a file on the left. It opens here, and stays here while you '
          + 'look at another one.')));
  }

  function fileRow(f) {
    const isOpen = current?.path === f.path;
    const isDirty = buffers.has(f.path);
    return h(`button.cfgrow${isDirty ? '.dirty' : ''}`, {
      type: 'button',
      'aria-current': isOpen ? 'true' : null,
      disabled: !f.editable,
      title: f.editable ? f.path : `${f.path} — not an editable file type`,
      onclick: () => open(f),
    },
      icon(f.editable ? 'fileText' : 'file', 15),
      h('span.nm', f.name, h('span.own', `${f.owner} · ${f.size || ''}`)));
  }

  async function open(f) {
    if (!f.editable) return;
    // Park whatever is on screen before leaving it.
    stash();
    current = { ...f, content: null };
    paint();
    mount(editorHost,
      h('header', icon('fileText', 14), h('span.nm', f.path)),
      spinner('Opening'));

    if (buffers.has(f.path)) {
      showEditor({ ...f, content: buffers.get(f.path), language: f.language },
        true);
      return;
    }
    try {
      const doc = await api.get(
        `/api/instances/${ctx.id}/configs/read?path=${encodeURIComponent(f.path)}`);
      if (current?.path !== f.path) return;    // they moved on while it loaded
      showEditor({ ...f, ...doc }, false);
    } catch (e) {
      mount(editorHost,
        h('header', icon('fileText', 14), h('span.nm', f.path)),
        h('div', { style: { padding: '18px' } }, h('div.note.bad', e.message)));
    }
  }

  /* Keep the current text if it differs from what was loaded, so switching
     files is never destructive. */
  function stash() {
    if (!current || !area) return;
    if (area.value !== current.original) buffers.set(current.path, area.value);
    else buffers.delete(current.path);
  }

  function showEditor(doc, fromBuffer) {
    current = { ...doc, original: fromBuffer ? current?.original ?? doc.content
      : doc.content };
    gutter = h('pre.gut', { 'aria-hidden': 'true' });
    area = h('textarea', {
      spellcheck: false, value: doc.content, wrap: 'off',
      'aria-label': `Contents of ${doc.name}`,
      oninput: () => { drawGutter(); markDirty(); },
      onscroll: () => { gutter.scrollTop = area.scrollTop; },
      onkeydown: (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(); }
        if (e.key === 'Tab') {
          e.preventDefault();
          const at = area.selectionStart;
          area.setRangeText('  ', at, area.selectionEnd, 'end');
          drawGutter(); markDirty();
        }
      },
    });

    const status = h('span.mono.muted', { style: { fontSize: '10.5px' } });

    mount(editorHost,
      h('header',
        icon('fileText', 14),
        h('span.nm', doc.path),
        doc.path === 'server.properties'
          ? pill('needed to start', 'warn') : null,
        h('button.btn.icon.xs.ghost', {
          'aria-label': 'Copy the path', onclick: () => copyText(doc.path, 'Path'),
        }, icon('copy', 11)),
        h('button.btn.xs.primary', { id: 'cfgsave', onclick: save },
          hl(), icon('save', 12), h('span', 'Save')),
      ),
      h('div.editor', gutter, area),
      h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center',
        padding: '9px 13px', borderTop: '2px solid var(--rule)', flexWrap: 'wrap' } },
        status,
        h('div', { style: { flex: 1 } }),
        h('button.btn.xs.ghost', { onclick: revert }, h('span', 'Revert')),
      ));

    drawGutter();
    updateStatus();

    function drawGutter() {
      const n = area.value.split('\n').length;
      gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join('\n');
    }
    function updateStatus() {
      const changed = area.value !== current.original;
      status.textContent = `${area.value.split('\n').length} lines · `
        + `${bytes(new Blob([area.value]).size)} · ${doc.language || 'text'}`
        + (changed ? ' · unsaved' : '');
      status.style.color = changed ? 'var(--gold-ink)' : 'var(--faint)';
    }
    function markDirty() {
      stash();
      updateStatus();
      // Only repaint the list when the dirty flag actually flips, or every
      // keystroke would rebuild several hundred rows.
      const row = listHost.querySelector('.cfgrow[aria-current="true"]');
      if (!row) return;
      const shouldBeDirty = buffers.has(current.path);
      if (row.classList.contains('dirty') !== shouldBeDirty) {
        row.classList.toggle('dirty', shouldBeDirty);
      }
    }
    function revert() {
      area.value = current.original;
      buffers.delete(current.path);
      drawGutter();
      updateStatus();
      paint();
    }
  }

  async function save() {
    if (!current || !area) return;
    const btn = editorHost.querySelector('#cfgsave');
    if (area.value === current.original) {
      toast('Nothing changed.', 'ok', { timeout: 1600 });
      return;
    }
    if (current.path === 'server.properties') {
      const ok = await confirmDialog({
        title: 'Save server.properties?',
        message: 'The server reads this file to start. A syntax error here '
          + 'means the next start fails, and it will not say why.',
        confirmLabel: 'Save anyway', danger: true,
      });
      if (!ok) return;
    }
    btn?.classList.add('busy');
    try {
      const res = await api.post(`/api/instances/${ctx.id}/files/write`, {
        path: current.path, content: area.value, allow_world: true,
      });
      current.original = area.value;
      buffers.delete(current.path);
      toast(`Saved ${current.name}`
        + (res.snapshot ? ' — the previous version is in Undo.' : ''), 'ok');
      paint();
    } catch (e) {
      toastError(e, 'Could not save');
    } finally {
      btn?.classList.remove('busy');
    }
  }

  /* Leaving the tab with unsaved buffers is worth one question. */
  const guard = (e) => {
    if (!buffers.size) return;
    e.preventDefault();
    e.returnValue = '';
  };
  window.addEventListener('beforeunload', guard);

  await load();
  return {
    node,
    dispose() { window.removeEventListener('beforeunload', guard); },
  };
}
