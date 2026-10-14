/* ============================================================
   Backups — copies of the world, and everything around it.

   Snapshots are kept on the node, deduplicated: a file that has
   not changed since the last backup is stored once, so ten
   nightly backups of a 4 GB world cost a little over 4 GB
   (agent/worldbackup.py). Restoring stops the server, backs the
   world up as it is first, makes the folder exactly the snapshot,
   and starts it again.

   Drawn as a rope timeline: each backup hangs from it as a tag,
   newest first, with what it was for, how big it is and how much
   of it was new.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, ago, empty, loadingFox,
  confirmDialog, promptDialog, toggle, field, mascot,
} from '../core.js';
import { run } from '../jobs.js';
import { may } from '../auth.js';

export async function render(ctx) {
  const node = h('div.wbackups');
  const host = h('div');
  mount(node, host);
  let data = null;
  const canMake = may('backups.make', ctx.id);
  const canRestore = may('backups.restore', ctx.id);

  async function load(quiet) {
    if (!quiet) mount(host, loadingFox('Reading the backups', 'nod'));
    try {
      data = await api.get(`/api/instances/${ctx.id}/worldbackups`);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    paint();
  }

  function paint() {
    const snaps = data.snapshots || [];
    const st = data.settings || {};
    mount(host,
      h('div.listbar',
        h('b.brush', 'Backups'),
        h('span.muted', snaps.length
          ? `${snaps.length} kept · ${data.store_human || '—'} on disk, unchanged files stored once`
          : 'none yet'),
        h('div.grow'),
        h('button.btn.sm.primary', { onclick: take, disabled: !canMake },
          hl(), icon('save', 13), h('span', 'Back up now')),
        h('button.btn.icon.sm.ghost', { 'aria-label': 'Reload', onclick: () => load() },
          icon('refresh', 14))),

      (data.running || []).length ? h('div.note.info',
        h('b', 'A backup is being taken'), 'It shows here when it finishes.') : null,

      h('div.wb-grid',
        h('div',
          !snaps.length
            ? empty('nod', 'No backups yet',
              'Take one now, or let a schedule take them for you -- Schedules has a '
              + '"back up every few hours" preset. One is also taken by itself before '
              + 'anything risky, like switching the pack version.',
              h('button.btn.primary', { onclick: take, disabled: !canMake }, hl(),
                h('span', 'Back up now')),
              h('a.btn', { href: `#/i/${ctx.id}/schedules` }, hl(), h('span', 'Schedule them')))
            : h('ol.rope-line', ...snaps.map((s, i) => tag(s, i === 0)))),
        settingsCard(st)));
  }

  function tag(s, newest) {
    const when = new Date((s.created || 0) * 1000);
    return h(`li.rl-item${newest ? '.newest' : ''}`,
      h('span.rl-knot', { 'aria-hidden': 'true' }),
      h('div.rl-card',
        h('div.rl-top',
          h('b.brush', when.toLocaleDateString([], { day: 'numeric', month: 'short' })),
          h('span.mono', when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
          h('span.muted', ago(s.created)),
          newest ? h('span.pill.ok', 'newest') : null),
        h('div.rl-why', s.reason || 'manual'),
        h('div.chiprow',
          h('span.pill.ghost', s.size_human || '—'),
          s.files !== undefined ? h('span.pill.ghost', `${s.files} files`) : null,
          s.new_human ? h('span.pill.info', { title: 'What this backup added to the store' },
            `+${s.new_human} new`) : null,
          s.skipped ? h('span.pill.warn', { title: 'Links and hard-linked files are not copied' },
            `${s.skipped} skipped`) : null),
        h('div.btnrow',
          h('button.btn.xs.primary', { disabled: !canRestore, onclick: () => restore(s) },
            hl(), icon('undo', 12), h('span', 'Restore')),
          h('a.btn.xs.ghost', {
            href: `/api/instances/${ctx.id}/worldbackups/${encodeURIComponent(s.id)}/download`,
            download: '',
          }, icon('download', 12), h('span', 'Download zip')),
          h('button.btn.xs.ghost', { disabled: !canRestore, onclick: () => remove(s),
            'aria-label': `Delete the backup from ${ago(s.created)}` },
          icon('trash', 12)))));
  }

  function settingsCard(st) {
    const keep = h('input.inp', { type: 'number', min: 1, max: 500, value: st.keep ?? 8,
      'aria-label': 'Backups to keep' });
    const exclude = h('input.inp', { type: 'text', value: (st.exclude || []).join(', '),
      placeholder: 'dynmap, bluemap/web', 'aria-label': 'Folders to leave out' });
    let compress = !!st.compress;
    let beforeRisky = st.before_risky !== false;
    const save = h('button.btn.sm', { disabled: !canMake, onclick: async () => {
      save.classList.add('busy');
      try {
        await api.post(`/api/instances/${ctx.id}/worldbackups/settings`, {
          keep: Number(keep.value), exclude: exclude.value,
          compress, before_risky: beforeRisky,
        });
        toast('Backup settings saved.', 'ok');
        load(true);
      } catch (e) { toastError(e); } finally { save.classList.remove('busy'); }
    } }, hl(), icon('save', 13), h('span', 'Save'));
    return h('aside.panel.wb-settings',
      h('header', icon('gear', 15), h('h3', 'How backups are kept')),
      h('div.pad',
        field('Keep the newest', keep, 'Older ones go when a new one is taken.'),
        field('Leave out these folders', exclude,
          'Comma-separated, inside the server folder. Logs, crash reports and caches '
          + 'are always left out.'),
        toggle('Compress the stored files', compress, (v) => { compress = v; },
          { help: 'Smaller on disk, slower to take. Worth it for big worlds.' }),
        toggle('Back up before risky changes', beforeRisky, (v) => { beforeRisky = v; },
          { help: 'Switching the pack version, updating the server jar. If that backup '
            + 'fails, the change does not happen.' }),
        h('div.btnrow', { style: { marginTop: '12px' } }, save),
        h('div.wb-fox', mascot('nod', 48),
          h('span.muted', 'A backup of a running server saves the world first, so it is '
            + 'never caught half-written.'))));
  }

  async function take() {
    const reason = await promptDialog({
      title: 'Back up the world', label: 'What is it for?', value: 'manual',
      placeholder: 'before the dragon fight', confirmLabel: 'Back it up',
    });
    if (!reason) return;
    run(`/api/instances/${ctx.id}/worldbackups`, { reason }, {
      title: `Back up ${ctx.name}`,
      onEnd: (rec) => { if (rec.status === 'done') load(true); },
    }).catch(() => {});
  }

  async function restore(s) {
    let startAfter = true;
    const ok = await confirmDialog({
      title: 'Restore this backup?',
      message: `${ctx.name} is stopped, the world as it is now is backed up first, and `
        + `the folder is made exactly as it was ${ago(s.created)}.`,
      detail: `Taken for: ${s.reason || 'manual'}.`,
      confirmLabel: 'Restore', art: 'nod',
    });
    if (!ok) return;
    run(`/api/instances/${ctx.id}/worldbackups/${encodeURIComponent(s.id)}/restore`,
      { start_after: startAfter }, {
        title: `Restore ${ctx.name}`,
        onEnd: (rec) => { if (rec.status === 'done') { load(true); ctx.reload().catch(() => {}); } },
      }).catch(() => {});
  }

  async function remove(s) {
    const ok = await confirmDialog({
      title: 'Delete this backup?',
      message: `The backup from ${ago(s.created)} goes for good. Files other backups `
        + 'still share are kept.',
      confirmLabel: 'Delete it', danger: true, art: 'vanish',
    });
    if (!ok) return;
    try {
      await api.del(`/api/instances/${ctx.id}/worldbackups/${encodeURIComponent(s.id)}`);
      toast('Backup deleted.', 'ok');
      load(true);
    } catch (e) { toastError(e); }
  }

  await load();
  return { node };
}
