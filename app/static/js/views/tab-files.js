/* ============================================================
   Files — the whole server directory, browsable and editable.

   Two guards run the whole screen, and both exist because this
   is the one tab that can destroy a world:

     * `world/`, `backups/`, `libraries/` and friends are listed
       (knowing a world is 4 GB is exactly what this is for) but
       a write or a delete inside one needs a second, explicit
       confirmation.
     * `server.properties`, `eula.txt` and the launcher jar are
       marked, because deleting one breaks the server in a way
       that only shows up at the next start.

   Upload, download, rename, mkdir, extract and a real editor
   with a line gutter. The gutter is a plain <pre> sharing the
   textarea's metrics with scrollTop mirrored — no highlighting
   overlay, which drifts on wrap and is a maintenance trap.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, num,
  empty, loadingFox, confirmDialog, promptDialog, modal, close, frag,
  debounce, prefs, copyText, qs,
} from '../core.js';

const KIND_ICON = {
  folder: 'folder', mod: 'box', text: 'fileText', archive: 'archive',
  image: 'image', world: 'db', binary: 'file',
};

export async function render(ctx) {
  const node = h('div.fm');
  const sideHost = h('div.fm-side');
  const mainHost = h('div.fm-main');
  mount(node, sideHost, mainHost);

  let path = prefs.get(`files.path.${ctx.id}`, '.');
  let view = null;
  let usage = null;
  let searching = null;
  const selected = new Set();

  async function loadUsage() {
    mount(sideHost,
      h('div.hd', 'Folders'),
      h('div', { style: { padding: '10px' } },
        h('div.skel', { style: { height: '38px' } })));
    try {
      usage = await api.get(`/api/instances/${ctx.id}/files/usage`);
    } catch { usage = { folders: [] }; }
    paintSide();
  }

  function paintSide() {
    const quick = [
      { name: '.', label: 'Server root', ico: 'server' },
      ...(usage?.folders || []).map((f) => ({
        name: f.name, label: f.name, ico: KIND_ICON.folder,
        size: f.size, protected: f.protected,
      })),
    ];
    mount(sideHost,
      h('div.hd', 'Folders'),
      ...quick.map((q) => h('button', {
        type: 'button',
        'aria-current': (q.name === path) ? 'true' : null,
        onclick: () => open(q.name),
      },
        icon(q.ico || 'folder', 15),
        h('span', { style: { minWidth: 0, overflow: 'hidden',
          textOverflow: 'ellipsis' } }, q.label),
        q.protected ? icon('shield', 12) : null,
        q.size ? h('span.sz', q.size) : null)),
      h('div', { style: { padding: '10px' } },
        h('button.btn.sm.ghost.block', { onclick: loadUsage },
          icon('refresh', 13), h('span', 'Recount sizes'))),
    );
  }

  async function open(next, { quiet } = {}) {
    path = next;
    prefs.set(`files.path.${ctx.id}`, path);
    selected.clear();
    searching = null;
    if (!quiet) mount(mainHost, loadingFox('Reading the directory'));
    try {
      view = await api.get(`/api/instances/${ctx.id}/files` + qs({ path }));
    } catch (e) {
      mount(mainHost, h('div.fm-bar', crumbs()),
        h('div', { style: { padding: '18px' } }, h('div.note.bad', e.message)));
      return;
    }
    paint();
    paintSide();
  }

  function crumbs() {
    const list = view?.crumbs || [{ name: 'server root', path: '.' }];
    const out = [];
    list.forEach((c, i) => {
      const last = i === list.length - 1;
      if (i) out.push(h('span', '/'));
      out.push(last
        ? h('span.cur', c.name)
        : h('button', { onclick: () => open(c.path) }, c.name));
    });
    return h('div.crumbline', { 'aria-label': 'Path' }, ...out);
  }

  function paint() {
    const rows = searching ? searching.results : (view?.entries || []);
    const isSearch = !!searching;

    mount(mainHost,
      h('div.fm-bar',
        view?.parent !== null && view?.parent !== undefined && !isSearch
          ? h('button.btn.icon.sm.ghost', {
            'aria-label': 'Up one folder', onclick: () => open(view.parent),
          }, icon('chevronLeft', 15))
          : null,
        crumbs(),
        h('input.inp', {
          type: 'search', placeholder: 'Find a file…', 'data-search': '1',
          'aria-label': 'Search files under this folder',
          style: { maxWidth: '210px', minHeight: '36px' },
          oninput: debounce(async (e) => {
            const q = e.target.value.trim();
            if (q.length < 2) { searching = null; paint(); return; }
            try {
              searching = await api.get(`/api/instances/${ctx.id}/files/search`
                + qs({ q, root: path }));
            } catch (err) { toastError(err); searching = null; }
            paint();
          }, 320),
        }),
        h('button.btn.icon.sm.ghost', {
          'aria-label': 'Refresh', title: 'Refresh',
          onclick: () => open(path),
        }, icon('refresh', 14)),
        h('button.btn.sm.ghost', { onclick: newFolder },
          icon('plus', 13), h('span', 'Folder')),
        h('button.btn.sm.ghost', { onclick: newFile },
          icon('file', 13), h('span', 'File')),
        h('button.btn.sm.primary', { onclick: pickUpload },
          hl(), icon('upload', 13), h('span', 'Upload')),
      ),

      view?.protected
        ? h('div.note.warn', { style: { margin: '0', borderRadius: 0 } },
          h('b', 'This is world or runtime data'),
          'Listing is fine. Editing or deleting anything in here asks for a '
          + 'second confirmation, because a mis-click costs a save.')
        : null,

      selected.size
        ? h('div.fm-bar', { style: { background: 'var(--rose-soft)' } },
          h('b', `${selected.size} selected`),
          h('div', { style: { flex: 1 } }),
          h('button.btn.sm.danger', { onclick: deleteSelected },
            icon('trash', 13), h('span', 'Delete')),
          h('button.btn.sm.ghost', {
            onclick: () => { selected.clear(); paint(); },
          }, h('span', 'Clear')))
        : null,

      isSearch
        ? h('div.fm-bar', { style: { background: 'var(--paper-2)' } },
          h('span.mono', `${searching.count} match`
            + `${searching.count === 1 ? '' : 'es'} under ${searching.root}/`
            + (searching.truncated ? ' (showing the first 300)' : '')),
          h('div', { style: { flex: 1 } }),
          h('button.btn.sm.ghost', {
            onclick: () => { searching = null; paint(); },
          }, h('span', 'Clear search')))
        : null,

      rows.length
        ? h('div.fm-list',
          h('div.fmrow', { style: { position: 'sticky', top: 0,
            background: 'var(--slab-bg)', color: 'var(--slab-fg)',
            fontFamily: 'var(--mono)', fontSize: '10px',
            letterSpacing: '.12em', textTransform: 'uppercase', zIndex: 2 } },
            h('span'), h('span'),
            h('span', isSearch ? 'Path' : 'Name'),
            h('span', { style: { textAlign: 'right' } }, 'Size'),
            h('span', { style: { textAlign: 'right' } }, 'Modified'),
            h('span')),
          ...rows.map(entryRow))
        : h('div', { style: { padding: '30px' } },
          h('div.note', isSearch ? 'Nothing matched.'
            : 'This folder is empty.')),

      view && !isSearch
        ? h('div.fm-bar', { style: { borderTop: '2px solid var(--rule)',
          borderBottom: 0, fontFamily: 'var(--mono)', fontSize: '11px',
          color: 'var(--faint)' } },
          `${view.folders} folder${view.folders === 1 ? '' : 's'}, `
          + `${view.files} file${view.files === 1 ? '' : 's'} · ${view.size}`)
        : null,
    );
  }

  function entryRow(e) {
    const isSel = selected.has(e.path);
    return h(`div.fmrow${isSel ? '.sel' : ''}`,
      h('input', {
        type: 'checkbox', checked: isSel,
        'aria-label': `Select ${e.name}`,
        onchange: (ev) => {
          if (ev.target.checked) selected.add(e.path); else selected.delete(e.path);
          paint();
        },
      }),
      h(`span.kind-${e.kind}`, icon(KIND_ICON[e.kind] || 'file', 17)),
      h('div.nm',
        h('button', {
          onclick: () => (e.dir ? open(e.path) : openFile(e)),
          title: e.path,
        }, e.name),
        e.critical ? pill('needed to start', 'warn',
          'Deleting this stops the server booting') : null,
        e.protected && !e.dir ? icon('shield', 12) : null,
        e.disabled ? pill('disabled', 'dead') : null),
      h('span.sz', e.dir ? '' : (e.size || bytes(e.bytes))),
      h('span.md', e.modified || ''),
      h('div.ac',
        !e.dir && e.editable ? h('button.btn.icon.xs.ghost', {
          'aria-label': `Edit ${e.name}`, title: 'Edit',
          onclick: () => openFile(e),
        }, icon('edit', 12)) : null,
        !e.dir ? h('button.btn.icon.xs.ghost', {
          'aria-label': `Download ${e.name}`, title: 'Download',
          onclick: () => download(e),
        }, icon('download', 12)) : null,
        e.kind === 'archive' && e.name.toLowerCase().endsWith('.zip')
          ? h('button.btn.icon.xs.ghost', {
            'aria-label': `Extract ${e.name}`, title: 'Extract here',
            onclick: () => extract(e),
          }, icon('box', 12)) : null,
        h('button.btn.icon.xs.ghost', {
          'aria-label': `Rename ${e.name}`, title: 'Rename',
          onclick: () => rename(e),
        }, icon('edit', 12, { weight: 1.4 })),
        h('button.btn.icon.xs.ghost', {
          'aria-label': `Delete ${e.name}`, title: 'Delete',
          onclick: () => deleteOne(e),
        }, icon('trash', 12)),
      ));
  }

  /* --- operations ---------------------------------------------- */

  async function newFolder() {
    const name = await promptDialog({
      title: 'New folder', label: 'Folder name',
      placeholder: 'datapacks', confirmLabel: 'Create',
    });
    if (!name) return;
    try {
      await api.post(`/api/instances/${ctx.id}/files/create`,
        { parent: path, name, directory: true });
      toast(`Created ${name}/`, 'ok');
      open(path, { quiet: true });
    } catch (e) { toastError(e); }
  }

  async function newFile() {
    const name = await promptDialog({
      title: 'New file', label: 'File name',
      placeholder: 'notes.txt', confirmLabel: 'Create',
      help: 'It is created empty; open it to write something.',
    });
    if (!name) return;
    try {
      await api.post(`/api/instances/${ctx.id}/files/create`,
        { parent: path, name, directory: false });
      toast(`Created ${name}`, 'ok');
      open(path, { quiet: true });
    } catch (e) { toastError(e); }
  }

  function pickUpload() {
    const input = h('input', {
      type: 'file', multiple: true, style: { display: 'none' },
      onchange: (e) => uploadFiles([...e.target.files]),
    });
    document.body.append(input);
    input.click();
    setTimeout(() => input.remove(), 60000);
  }

  async function uploadFiles(files) {
    if (!files.length) return;
    let done = 0;
    const t = toast(`Uploading 0 of ${files.length}…`, 'ok', { timeout: 600000 });
    for (const file of files) {
      const form = new FormData();
      form.append('file', file);
      try {
        const res = await api.upload(
          `/api/instances/${ctx.id}/files/upload${qs({ folder: path })}`, form);
        done += 1;
        t.querySelector('.msg').textContent =
          `Uploading ${done} of ${files.length}…`;
        if (res.archive) {
          const ok = await confirmDialog({
            title: `Extract ${file.name}?`,
            message: 'That is an archive. Extracting unpacks it into this '
              + 'folder; Crafty does the work in the background.',
            confirmLabel: 'Extract it',
          });
          if (ok) await extract({ path: res.path, name: file.name });
        }
      } catch (e) {
        toastError(e, `${file.name} failed`);
      }
    }
    t.remove();
    toast(`${done} of ${files.length} uploaded.`, done === files.length ? 'ok' : 'warn');
    open(path, { quiet: true });
  }

  async function download(e) {
    // Streamed through the API rather than linked directly, so the Crafty
    // token never has to reach the browser.
    try {
      const res = await api.raw(`/api/instances/${ctx.id}/files/download`
        + qs({ path: e.path }));
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = h('a', { href: url, download: e.name, style: { display: 'none' } });
      document.body.append(a);
      a.click();
      setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 4000);
    } catch (err) { toastError(err, `Could not download ${e.name}`); }
  }

  async function extract(e) {
    try {
      const res = await api.post(`/api/instances/${ctx.id}/files/extract`,
        { path: e.path });
      toast(res.note, 'ok', { timeout: 7000 });
      setTimeout(() => open(path, { quiet: true }), 3500);
    } catch (err) { toastError(err); }
  }

  async function rename(e) {
    const name = await promptDialog({
      title: `Rename ${e.name}`, label: 'New name', value: e.name,
      confirmLabel: 'Rename',
      help: 'Renaming a mod jar to end in .disabled is how you turn it off.',
    });
    if (!name || name === e.name) return;
    try {
      await api.post(`/api/instances/${ctx.id}/files/rename`,
        { path: e.path, new_name: name });
      toast('Renamed.', 'ok');
      open(path, { quiet: true });
    } catch (err) { toastError(err); }
  }

  async function deleteOne(e) {
    const ok = await confirmDialog({
      title: `Delete ${e.name}?`,
      message: e.dir
        ? 'The folder and everything inside it is removed from the server.'
        : 'The file is removed from the server.',
      detail: e.critical
        ? 'The server needs this file to start. Without it the next start will '
          + 'fail, and it will not say why.'
        : e.protected
          ? 'This is world or runtime data. Deleting it cannot be undone from '
            + 'here.' : null,
      confirmLabel: 'Delete', danger: true,
      requireText: (e.critical || e.protected) ? 'DELETE' : undefined,
    });
    if (!ok) return;
    try {
      const res = await api.post(`/api/instances/${ctx.id}/files/delete`,
        { paths: [e.path], allow_world: e.protected });
      if (res.warning) toast(res.warning, 'warn', { timeout: 10000 });
      else toast('Deleted.', 'ok');
      open(path, { quiet: true });
    } catch (err) { toastError(err); }
  }

  async function deleteSelected() {
    const paths = [...selected];
    const rows = (searching ? searching.results : view.entries)
      .filter((e) => selected.has(e.path));
    const risky = rows.some((e) => e.protected || e.critical);
    const ok = await confirmDialog({
      title: `Delete ${paths.length} item${paths.length > 1 ? 's' : ''}?`,
      message: 'They are removed from the server directory.',
      detail: paths.slice(0, 10).join('\n')
        + (paths.length > 10 ? `\n…and ${paths.length - 10} more` : ''),
      confirmLabel: 'Delete', danger: true,
      requireText: risky ? 'DELETE' : undefined,
    });
    if (!ok) return;
    try {
      const res = await api.post(`/api/instances/${ctx.id}/files/delete`,
        { paths, allow_world: risky });
      if (res.warning) toast(res.warning, 'warn', { timeout: 10000 });
      else toast(`${paths.length} deleted.`, 'ok');
      selected.clear();
      open(path, { quiet: true });
    } catch (err) { toastError(err); }
  }

  /* --- the editor ---------------------------------------------- */

  async function openFile(e) {
    if (!e.editable) {
      const ok = await confirmDialog({
        title: `${e.name} is not text`,
        message: 'This file cannot be opened in the editor. Download it '
          + 'instead?',
        confirmLabel: 'Download',
      });
      if (ok) download(e);
      return;
    }
    const host = h('div', loadingFox('Opening'));
    modal({ title: e.name, body: host, wide: true });
    let doc;
    try {
      doc = await api.get(`/api/instances/${ctx.id}/files/read` + qs({ path: e.path }));
    } catch (err) {
      mount(host, h('div.note.bad', err.message));
      return;
    }
    showEditor(ctx, doc, () => open(path, { quiet: true }));
  }

  // Sizing the sidebar walks the whole server directory through Crafty --
  // ten seconds on a 253-mod instance -- while the listing this tab exists
  // for comes back in under thirty milliseconds. Awaiting both in turn left
  // the tab on its skeleton for fourteen seconds. loadUsage() already paints
  // its own placeholder and repaints when it lands, so let it run alongside
  // rather than in front.
  const sizing = loadUsage().catch(() => {});
  await open(path);
  sizing.then(() => {});
  return { node };
}

export function showEditor(ctx, doc, onSaved) {
  let dirty = false;
  const gutter = h('pre.gut', { 'aria-hidden': 'true' });
  const area = h('textarea', {
    spellcheck: false, value: doc.content, wrap: 'off',
    'aria-label': `Contents of ${doc.name}`,
    oninput: () => { dirty = true; drawGutter(); },
    onscroll: () => { gutter.scrollTop = area.scrollTop; },
    onkeydown: (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(); }
      if (e.key === 'Tab') {
        e.preventDefault();
        const s = area.selectionStart;
        area.setRangeText('  ', s, area.selectionEnd, 'end');
        dirty = true;
      }
    },
  });

  function drawGutter() {
    const lines = area.value.split('\n').length;
    gutter.textContent = Array.from({ length: lines }, (_, i) => i + 1).join('\n');
  }

  const statusEl = h('span.mono.muted', { style: { fontSize: '11px' } },
    `${doc.lines} lines · ${bytes(doc.bytes)} · ${doc.language}`);

  async function save() {
    if (!dirty) { toast('Nothing changed.', 'ok', { timeout: 1600 }); return; }
    if (doc.protected || doc.critical) {
      const ok = await confirmDialog({
        title: `Save ${doc.name}?`,
        message: doc.critical
          ? 'The server reads this file to start. A syntax error here means '
            + 'the next start fails.'
          : 'This file is world or runtime data.',
        confirmLabel: 'Save anyway', danger: true,
      });
      if (!ok) return;
    }
    try {
      const res = await api.post(`/api/instances/${ctx.id}/files/write`, {
        path: doc.path, content: area.value, allow_world: true,
      });
      dirty = false;
      toast(`Saved ${doc.name}`
        + (res.snapshot ? ' — the previous version is in Undo.' : ''), 'ok');
      onSaved?.();
      close();
    } catch (e) { toastError(e, 'Could not save'); }
  }

  modal({
    title: doc.name,
    wide: true,
    dismissable: true,
    body: frag(
      h('div.chiprow', { style: { marginBottom: '10px' } },
        pill(doc.language, 'ghost'),
        doc.critical ? pill('needed to start', 'warn') : null,
        doc.protected ? pill('world data', 'plum') : null,
        h('span.mono.muted', { style: { fontSize: '11px', alignSelf: 'center' } },
          doc.path)),
      h('div.editor', gutter, area),
    ),
    footer: frag(
      statusEl,
      h('div.grow'),
      h('button.btn.ghost', {
        onclick: async () => {
          if (dirty) {
            const ok = await confirmDialog({
              title: 'Discard your changes?',
              message: `${doc.name} has unsaved edits.`,
              confirmLabel: 'Discard', danger: true,
            });
            if (!ok) return;
          }
          close();
        },
      }, h('span', 'Close')),
      h('button.btn.primary', { onclick: save }, hl(), icon('save', 14),
        h('span', 'Save')),
    ),
    onClose: () => {},
  });
  drawGutter();
}
