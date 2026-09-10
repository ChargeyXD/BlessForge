/* ============================================================
   Undo — snapshots taken before destructive changes.

   These hold which mods existed and whether each was enabled,
   plus the previous bytes of any config that was edited. They do
   not hold the jars, so they cost kilobytes and restore in
   seconds — and they are emphatically not a world backup, which
   is said plainly rather than left to be discovered at the worst
   possible moment.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, ago, empty,
  loadingFox, confirmDialog, promptDialog, num,
} from '../core.js';

export async function render(ctx) {
  const node = h('div');
  const host = h('div');
  mount(node, host, loadingFox('Reading snapshots'));

  let data = null;

  async function load(quiet) {
    if (!quiet) mount(host, loadingFox('Reading snapshots'));
    try {
      data = await api.get(`/api/instances/${ctx.id}/backups`);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    paint();
  }

  function paint() {
    const items = data.items || [];
    mount(host,
      h('div.listbar',
        h('b', `${items.length} snapshot${items.length === 1 ? '' : 's'}`),
        h('span.muted', { style: { fontSize: '12.5px' } },
          `the newest ${data.limit} are kept`),
        h('div.grow'),
        h('button.btn.sm.primary', { onclick: take },
          hl(), icon('save', 13), h('span', 'Take one now')),
        h('button.btn.icon.sm.ghost', {
          'aria-label': 'Reload', onclick: () => load(),
        }, icon('refresh', 14))),

      h('div.note', { style: { marginBottom: '14px' } },
        h('b', 'This is not a world backup'),
        'A snapshot records which mods were installed and whether each was '
        + 'enabled, plus the previous contents of any config file edited here. '
        + 'It cannot bring back a deleted world. Use Crafty\'s own backups for '
        + 'that.'),

      !items.length
        ? empty('shulker-guard.gif', 'Nothing to undo yet',
          'Snapshots are taken automatically before a bulk disable, an '
          + 'assistant fix, a config edit or a pack version switch.',
          h('button.btn.primary', { onclick: take }, hl(),
            h('span', 'Take one now')))
        : h('div.modlist', ...items.map(row)),
    );
  }

  function row(s) {
    return h('div.modrow',
      h('div.ico', icon('refresh', 15)),
      h('div.who',
        h('div.nm', s.reason || 'snapshot'),
        h('div.fn', `${s.id} · ${ago(s.at)}`),
        h('div.tags',
          pill(`${s.mods} mods`, 'ghost'),
          pill(`${s.enabled} enabled`, 'ghost'),
          s.files ? pill(`${s.files} file${s.files > 1 ? 's' : ''}`, 'info') : null,
          s.partial ? pill('partial', 'warn',
            'Some of the instance could not be read at the time') : null,
          s.pack ? pill(s.pack, 'plum') : null)),
      h('div.acts',
        h('button.btn.xs.primary', { onclick: () => restore(s) },
          hl(), icon('refresh', 12), h('span', 'Restore'))));
  }

  async function take() {
    const reason = await promptDialog({
      title: 'Take a snapshot',
      label: 'What is this for?',
      value: 'manual',
      placeholder: 'before I try something',
      confirmLabel: 'Take it',
    });
    if (!reason) return;
    try {
      await api.post(`/api/instances/${ctx.id}/backups`, { reason });
      toast('Snapshot taken.', 'ok');
      load(true);
    } catch (e) { toastError(e); }
  }

  async function restore(s) {
    const ok = await confirmDialog({
      title: 'Restore this snapshot?',
      message: `Mods will be re-enabled or disabled to match how they were `
        + `${ago(s.at)}, and any config file recorded in it is written back.`,
      detail: s.reason ? `Taken ${s.reason}.` : null,
      confirmLabel: 'Restore',
    });
    if (!ok) return;
    try {
      const res = await api.post(
        `/api/instances/${ctx.id}/backups/${s.id}/restore`);
      toast(`${res.changed ?? 0} change${res.changed === 1 ? '' : 's'} applied`
        + `${res.files ? `, ${res.files} file(s) restored` : ''}. Restart for `
        + 'it to take effect.', 'ok', { timeout: 8000 });
      load(true);
    } catch (e) { toastError(e); }
  }

  await load();
  return { node };
}
