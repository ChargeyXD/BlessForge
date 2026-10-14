/* ============================================================
   Schedules — restarts, commands and backups on a clock.

   A task is a slip on the rope: what it does, when (said in
   words, the cron underneath), when it runs next, and what
   happened last time. A chained step -- "say 1 minute", then
   restart -- hangs under its parent with the wait between them,
   because that is how the scheduler runs it (app/schedules.py:
   the parent's run starts its children's clocks).

   Three presets cover what most people want; the editor covers
   the rest, and shows the next five runs before anything is
   saved so a cron typo is caught by reading, not by waiting.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, ago, empty, loadingFox,
  confirmDialog, modal, close, field, toggle, segmented, debounce, pill, mascot,
} from '../core.js';
import { may } from '../auth.js';

const ACTIONS = {
  command: { label: 'Run a command', icon: 'terminal' },
  say: { label: 'Say something', icon: 'send' },
  start: { label: 'Start', icon: 'play' },
  stop: { label: 'Stop', icon: 'stop' },
  restart: { label: 'Restart', icon: 'restart' },
  backup: { label: 'Back up the world', icon: 'shield' },
  update_jar: { label: 'Update the server jar', icon: 'download' },
};
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const pad = (n) => String(n).padStart(2, '0');

/* A cron rule, said the way a person would say it. Anything it does not
   recognise is shown as the cron itself rather than guessed at. */
export function inWords(cron) {
  const c = String(cron || '').trim();
  if (!c) return 'after the step above';
  const alias = { '@hourly': 'every hour', '@daily': 'every day at midnight',
    '@midnight': 'every day at midnight', '@weekly': 'every Sunday at midnight',
    '@monthly': 'on the 1st of every month' }[c];
  if (alias) return alias;
  let m = /^@every\s+(\d+)\s*(m|min|minutes?|h|hours?)?$/.exec(c);
  if (m) {
    const n = Number(m[1]);
    const hours = (m[2] || 'm').startsWith('h');
    return `every ${n === 1 ? '' : `${n} `}${hours ? 'hour' : 'minute'}${n === 1 ? '' : 's'}`;
  }
  const f = c.split(/\s+/);
  if (f.length !== 5) return c;
  const [mi, hr, dom, mon, dow] = f;
  if (/^\*\/\d+$/.test(mi) && hr === '*' && dom === '*' && mon === '*' && dow === '*') {
    return `every ${mi.slice(2)} minutes`;
  }
  if (/^\d+$/.test(mi) && /^\*\/\d+$/.test(hr) && dom === '*' && mon === '*' && dow === '*') {
    return `every ${hr.slice(2)} hours at :${pad(mi)}`;
  }
  if (/^\d+$/.test(mi) && hr === '*' && dom === '*' && mon === '*' && dow === '*') {
    return `every hour at :${pad(mi)}`;
  }
  if (/^\d+$/.test(mi) && /^\d+$/.test(hr) && dom === '*' && mon === '*') {
    const at = `${pad(hr)}:${pad(mi)}`;
    if (dow === '*') return `every day at ${at}`;
    const days = dow.split(',').map((d) => {
      if (/^\d-\d$/.test(d)) return `${DAYS[d[0]]}–${DAYS[d[2]]}`;
      return DAYS[Number(d)] || d;
    });
    if (days.every(Boolean)) return `${days.join(', ')} at ${at}`;
  }
  return c;
}

export async function render(ctx) {
  const node = h('div.schedules');
  const host = h('div');
  mount(node, host);
  let data = null;
  const canEdit = may('schedules.manage', ctx.id);

  async function load(quiet) {
    if (!quiet) mount(host, loadingFox('Reading the schedule', 'nod'));
    try {
      data = await api.get(`/api/instances/${ctx.id}/schedules`);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    paint();
  }

  function paint() {
    const tasks = data.tasks || [];
    const roots = tasks.filter((t) => t.parent === undefined || t.parent === null);
    const kids = (id) => tasks.filter((t) => t.parent === id)
      .sort((a, b) => (a.delay || 0) - (b.delay || 0));
    const chain = (t, depth = 0) => [slip(t, depth), ...kids(t.id).flatMap((k) => chain(k, depth + 1))];
    mount(host,
      h('div.listbar',
        h('b.brush', 'Schedules'),
        h('span.muted', `${roots.length} task${roots.length === 1 ? '' : 's'} · times in ${data.tz}`),
        h('div.grow'),
        h('button.btn.sm.primary', { disabled: !canEdit, onclick: () => editor(null) },
          hl(), icon('plus', 13), h('span', 'New task'))),
      h('div.sch-grid',
        h('div',
          roots.length ? h('div.sch-rope', h('span.sch-cord', { 'aria-hidden': 'true' }),
            ...roots.flatMap((t) => chain(t)))
            : empty('idle', 'Nothing on the clock yet',
              'A daily restart keeps the heap from creeping up; a backup every few hours '
              + 'means a bad night costs an hour, not a world. The presets set either up in '
              + 'one go.')),
        presets()));
  }

  function slip(t, depth) {
    const a = ACTIONS[t.action] || { label: t.action, icon: 'info' };
    const what = t.action === 'command' ? `/${t.command}`
      : t.action === 'say' ? `“${t.message}”`
        : t.action === 'backup' && t.keep ? `keeps ${t.keep}` : '';
    return h(`div.sch-slip${t.enabled ? '' : '.off'}${depth ? '.child' : ''}`,
      { style: { marginLeft: `${depth * 28}px` } },
      h('span.sch-ico', { 'aria-hidden': 'true' }, icon(a.icon, 16)),
      h('div.sch-body',
        h('div.sch-name', t.name || a.label, depth ? null : h('span.muted', ` · ${a.label}`)),
        what ? h('div.sch-what.mono', what) : null,
        h('div.sch-when',
          depth ? h('span', icon('clock', 12), ` ${wait(t.delay)} after the step above`)
            : h('span', icon('calendar', 12), ` ${inWords(t.cron)}`),
          t.next_run && t.enabled ? h('span.muted', ` · next ${until(t.next_run)}`) : null,
          !t.enabled ? pill('paused', 'dead') : null,
          t.one_time ? pill('once', 'ghost') : null),
        t.last_run ? h('div.sch-last',
          pill(t.last_ok ? 'ok' : 'failed', t.last_ok ? 'ok' : 'bad'),
          h('span.muted', ` ${ago(t.last_run)} — `), h('span', t.last_result || '')) : null),
      h('div.sch-acts',
        depth ? null : h('label.switch', { title: t.enabled ? 'Pause' : 'Resume' },
          h('input', { type: 'checkbox', checked: !!t.enabled, disabled: !canEdit,
            'aria-label': `${t.enabled ? 'Pause' : 'Resume'} ${t.name}`,
            onchange: (e) => patch(t, { enabled: e.target.checked }) }),
          h('span.track')),
        h('button.btn.xs', { disabled: !canEdit, onclick: () => runNow(t) },
          hl(), icon('play', 11), h('span', 'Run now')),
        h('button.btn.icon.xs.ghost', { disabled: !canEdit, 'aria-label': `Edit ${t.name}`,
          onclick: () => editor(t) }, icon('edit', 12)),
        h('button.btn.icon.xs.ghost', { disabled: !canEdit, 'aria-label': `Delete ${t.name}`,
          onclick: () => remove(t) }, icon('trash', 12))));
  }

  function presets() {
    const time = h('input.inp', { type: 'time', value: '05:00', 'aria-label': 'Restart time' });
    const warn = h('input.inp', { type: 'text', value: '5, 1',
      'aria-label': 'Warn this many minutes before' });
    const msg = h('input.inp', { type: 'text', placeholder: 'Join our Discord!',
      'aria-label': 'Announcement' });
    let every = '@every 2h';
    const hours = h('input.inp', { type: 'number', min: 1, max: 168, value: 6,
      'aria-label': 'Hours between backups' });
    const keep = h('input.inp', { type: 'number', min: 1, max: 500, value: 8,
      'aria-label': 'Backups to keep' });
    const add = async (body, btn) => {
      btn.classList.add('busy');
      try {
        const res = await api.post(`/api/instances/${ctx.id}/schedules/preset`,
          { tz: data.tz, ...body });
        toast(`Added ${res.tasks.length} step${res.tasks.length === 1 ? '' : 's'}.`, 'ok');
        load(true);
      } catch (e) { toastError(e); } finally { btn.classList.remove('busy'); }
    };
    const card = (title, kanji, help, fields, onAdd) => {
      const btn = h('button.btn.sm', { disabled: !canEdit }, hl(), icon('plus', 12),
        h('span', 'Add it'));
      btn.onclick = () => onAdd(btn);
      return h('div.preset',
        h('div.preset-hd', h('span.preset-k', { 'aria-hidden': 'true' }, kanji), h('b', title)),
        h('p.muted', help), ...fields, btn);
    };
    return h('aside.sch-presets',
      h('div.preset-head', mascot('nod', 40), h('h3.brush', 'Presets')),
      card('Daily restart', '朝', 'Warns players, then restarts. A fresh JVM every day keeps '
        + 'the heap from creeping up.',
      [field('At', time), field('Warn, minutes before', warn)],
      (btn) => add({ preset: 'daily_restart', time: time.value || '05:00',
        warn: warn.value.split(/[ ,]+/).map(Number).filter((n) => n > 0) }, btn)),
      card('Announcement', '告', 'A line in chat on a clock.',
        [field('Message', msg), segmented([
          { value: '@hourly', label: 'Hourly' }, { value: '@every 2h', label: '2 h' },
          { value: '@every 6h', label: '6 h' }], every, (v) => { every = v; }, 'How often')],
        (btn) => {
          if (!msg.value.trim()) { msg.focus(); return; }
          add({ preset: 'announce', message: msg.value.trim(), cron: every }, btn);
        }),
      card('Backups', '守', 'A world backup every few hours, the newest kept.',
        [field('Every (hours)', hours), field('Keep', keep)],
        (btn) => add({ preset: 'backup_every', hours: Number(hours.value),
          keep: Number(keep.value) }, btn)));
  }

  /* --- the editor ------------------------------------------- */
  function editor(task) {
    const t = task || { action: 'restart', cron: '0 5 * * *', enabled: true };
    const isChild = t.parent !== undefined && t.parent !== null;
    const name = h('input.inp', { value: t.name || '', placeholder: 'Nightly restart' });
    const action = h('select.inp', ...Object.entries(ACTIONS).map(([k, v]) =>
      h('option', { value: k, selected: k === t.action }, v.label)));
    const text = h('input.inp', { value: t.command || t.message || '' });
    const keepIn = h('input.inp', { type: 'number', min: 1, max: 500, value: t.keep || 8 });
    const cron = h('input.inp.mono', { value: t.cron || '', placeholder: '0 5 * * *',
      'aria-label': 'When, as cron' });
    const delay = h('input.inp', { type: 'number', min: 0, max: 1440,
      value: Math.round((t.delay || 0) / 60) });
    const tz = h('input.inp', { value: t.tz || data.tz, placeholder: 'Europe/London' });
    let oneTime = !!t.one_time;
    const preview = h('div.sch-preview', { role: 'status', 'aria-live': 'polite' });
    const extras = h('div');
    const showExtras = () => {
      const a = action.value;
      mount(extras,
        a === 'command' ? field('Command (no slash)', text) : null,
        a === 'say' ? field('Message', text) : null,
        a === 'backup' ? field('Keep the newest', keepIn) : null);
    };
    action.onchange = showExtras;
    showExtras();
    const look = debounce(async () => {
      if (isChild) return;
      const c = cron.value.trim();
      if (!c) { mount(preview); return; }
      try {
        const r = await api.get(`/api/schedules/preview?cron=${encodeURIComponent(c)}`
          + `&tz=${encodeURIComponent(tz.value.trim())}`);
        mount(preview, h('b', inWords(c)), h('ul',
          ...(r.next || []).map((s) => h('li.mono', new Date(s * 1000).toLocaleString()))));
      } catch (e) {
        mount(preview, h('span.err', e.message));
      }
    }, 300);
    cron.oninput = look;
    tz.oninput = look;
    look();
    const quick = (c) => h('button.chip', { type: 'button',
      onclick: () => { cron.value = c; look(); } }, inWords(c));

    const save = h('button.btn.primary', { onclick: async () => {
      const a = action.value;
      const payload = { name: name.value.trim() || ACTIONS[a].label, tz: tz.value.trim() || undefined,
        one_time: oneTime || undefined };
      if (a === 'command') payload.command = text.value.trim();
      if (a === 'say') payload.message = text.value.trim();
      if (a === 'backup') payload.keep = Number(keepIn.value);
      if (isChild) { payload.parent = t.parent; payload.delay = Number(delay.value) * 60; }
      save.classList.add('busy');
      try {
        if (task) {
          await api.patch(`/api/instances/${ctx.id}/schedules/${task.id}`,
            { action: a, cron: isChild ? '' : cron.value.trim(), payload });
        } else {
          await api.post(`/api/instances/${ctx.id}/schedules`,
            { action: a, cron: isChild ? '' : cron.value.trim(), ...payload });
        }
        close();
        toast(task ? 'Task saved.' : 'Task added.', 'ok');
        load(true);
      } catch (e) {
        save.classList.remove('busy');
        toastError(e, 'That task was not saved');
      }
    } }, hl(), h('span', task ? 'Save' : 'Add the task'));

    modal({
      title: task ? `Edit ${task.name || 'task'}` : 'A new task',
      body: h('div.sch-editor',
        field('Name', name),
        field('What', action),
        extras,
        isChild
          ? field('Minutes after the step above', delay)
          : h('div',
            field('When', cron, 'Five cron fields (minute hour day month weekday), '
              + '@daily, or @every 30m / @every 6h.'),
            h('div.chiprow', quick('0 5 * * *'), quick('@every 6h'), quick('@hourly'),
              quick('0 18 * * 5'), quick('*/30 * * * *')),
            field('Time zone', tz),
            toggle('Run once, then pause', oneTime, (v) => { oneTime = v; }),
            preview)),
      footer: h('div.btnrow', h('button.btn.ghost', { onclick: () => close() },
        h('span', 'Cancel')), save),
    });
  }

  async function patch(t, changes) {
    try {
      await api.patch(`/api/instances/${ctx.id}/schedules/${t.id}`, changes);
      load(true);
    } catch (e) { toastError(e); load(true); }
  }

  async function runNow(t) {
    try {
      const r = await api.post(`/api/instances/${ctx.id}/schedules/${t.id}/run`);
      toast(r.result || 'Ran it.', r.ok === false ? 'bad' : 'ok', { title: t.name });
      load(true);
    } catch (e) { toastError(e); }
  }

  async function remove(t) {
    const ok = await confirmDialog({
      title: `Delete ${t.name || 'this task'}?`,
      message: 'It stops running. Any steps chained under it go with it.',
      confirmLabel: 'Delete it', danger: true, art: 'vanish',
    });
    if (!ok) return;
    try {
      await api.del(`/api/instances/${ctx.id}/schedules/${t.id}`);
      load(true);
    } catch (e) { toastError(e); }
  }

  await load();
  return { node };
}

function wait(seconds) {
  const s = Number(seconds) || 0;
  if (s < 60) return `${s} s`;
  if (s % 3600 === 0) return `${s / 3600} h`;
  return `${Math.round(s / 60)} min`;
}

function until(epoch) {
  const d = epoch - Date.now() / 1000;
  if (d < 60) return 'in under a minute';
  if (d < 3600) return `in ${Math.round(d / 60)} min`;
  if (d < 86400) return `in ${Math.round(d / 3600)} h`;
  return new Date(epoch * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
}
