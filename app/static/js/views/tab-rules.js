/* ============================================================
   Rules — how this server stops, starts, updates, and who may
   touch it.

   Four ofuda, each one decision written down:
     * How it stops: the stop command, how long it gets to save
       before it is stopped for it, which exit codes are a clean
       stop rather than a crash, and whether a crash restarts it.
     * Starting with the machine (auto-start).
     * The server jar: Paper, Purpur and Folia follow new builds
       of the same Minecraft version, backed up first.
     * Who can reach it (admins): every account that can, how,
       and per-server grants on top of a role.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, loadingFox, field, toggle,
  segmented, pill, modal, close, titleCase,
} from '../core.js';
import { run } from '../jobs.js';
import { may, isAdmin } from '../auth.js';

export async function render(ctx) {
  const node = h('div.rules');
  const lifeHost = h('div');
  const jarHost = h('div');
  const accessHost = h('div');
  mount(node,
    h('div.listbar', h('b.brush', 'Rules'),
      h('span.muted', 'how it stops, starts, updates, and who may touch it')),
    h('div.rules-grid', lifeHost, jarHost),
    isAdmin() ? accessHost : null);

  const canTune = may('tune.apply', ctx.id);

  /* --- how it stops ------------------------------------------ */
  async function life() {
    mount(lifeHost, loadingFox('Reading its rules', 'nod'));
    let v;
    try { v = await api.get(`/api/instances/${ctx.id}/lifecycle`); } catch (e) {
      mount(lifeHost, h('div.note.bad', e.message)); return;
    }
    const cmd = h('input.inp.mono', { value: v.stop_command, maxlength: 64,
      placeholder: 'stop' });
    const grace = h('input', { type: 'range', min: 10, max: 600, step: 5,
      value: Math.min(600, v.stop_timeout), 'aria-label': 'Seconds it gets to stop' });
    const graceOut = h('b.tnum', `${v.stop_timeout} s`);
    grace.oninput = () => { graceOut.textContent = `${grace.value} s`; };
    const codes = h('input.inp.mono', { value: (v.ignored_exits || []).join(', '),
      placeholder: '0' });
    let restart = v.crash_restart_follows_autostart ? 'follow' : v.crash_restart ? 'yes' : 'no';
    let auto = !!ctx.server.auto_start;
    const save = h('button.btn.sm.primary', { disabled: !canTune }, hl(), icon('save', 13),
      h('span', 'Save'));
    save.onclick = async () => {
      save.classList.add('busy');
      try {
        await api.post(`/api/instances/${ctx.id}/lifecycle`, {
          stop_command: cmd.value.trim(),
          stop_timeout: Number(grace.value),
          ignored_exits: codes.value.split(/[ ,]+/).filter(Boolean).map(Number),
          crash_restart: restart === 'follow' ? null : restart === 'yes',
        });
        if (auto !== !!ctx.server.auto_start) {
          await api.post(`/api/instances/${ctx.id}/autostart`, { enabled: auto });
          ctx.server.auto_start = auto;
        }
        toast('Saved. It applies from the next start.', 'ok');
      } catch (e) { toastError(e); } finally { save.classList.remove('busy'); }
    };
    mount(lifeHost, h('section.panel.rule-card',
      h('header', h('span.rk', { 'aria-hidden': 'true' }, '止'), h('h3', 'How it stops')),
      h('div.pad',
        field('Stop command', cmd, 'Typed on the console to stop it the normal way, so it '
          + 'saves. "stop" for nearly every server; a proxy or a modded launcher may want '
          + 'another.'),
        h('div.field', h('label', 'Time to save before it is stopped for it'),
          h('div.rangerow', grace, graceOut),
          h('div.help', 'A big modded world can need minutes to save. Past this it is '
            + 'stopped without waiting.')),
        field('Exit codes that mean "stopped on purpose"', codes,
          'Anything else counts as a crash. 0 is a clean stop; a /restart plugin may exit '
          + 'with its own code.'),
        h('div.field', h('label', 'When it crashes'),
          segmented([
            { value: 'follow', label: 'Restart if auto-start is on' },
            { value: 'yes', label: 'Always restart' },
            { value: 'no', label: 'Leave it stopped' },
          ], restart, (x) => { restart = x; }, 'When it crashes')),
        toggle('Start with the machine', auto, (x) => { auto = x; },
          { help: 'When the node starts (after a reboot), this server starts too.' }),
        h('div.btnrow', { style: { marginTop: '12px' } }, save))));
  }

  /* --- the server jar ------------------------------------------ */
  async function jar() {
    let info;
    try { info = await api.get(`/api/instances/${ctx.id}/jar`); } catch { return; }
    if (!info.supported) {
      mount(jarHost, h('section.panel.rule-card',
        h('header', h('span.rk', { 'aria-hidden': 'true' }, '更'), h('h3', 'The server jar')),
        h('div.pad', h('p.muted', info.reason
          || 'Only Paper, Purpur and Folia jars are updated here.'))));
      return;
    }
    let schedule = null;
    try {
      const s = await api.get(`/api/instances/${ctx.id}/schedules`);
      schedule = (s.tasks || []).find((t) => t.action === 'update_jar') || null;
    } catch { /* the toggle just starts off */ }
    const canUpdate = may('mods.manage', ctx.id);
    const fam = titleCase(info.family);
    mount(jarHost, h('section.panel.rule-card',
      h('header', h('span.rk', { 'aria-hidden': 'true' }, '更'), h('h3', 'The server jar')),
      h('div.pad',
        h('div.jar-row',
          h('div', h('div.lbl', `${fam} ${info.minecraft}`),
            h('div.jar-build', h('span.mono', `build ${info.current || '?'}`),
              info.update_available ? h('span', ' → ', h('b.mono', `build ${info.latest}`)) : null)),
          info.update_available ? pill('newer build', 'gold') : pill('up to date', 'ok')),
        h('p.muted', 'The newest build for the same Minecraft version: never a different '
          + 'version, which would be a world upgrade. The world is backed up first, a running '
          + 'server is stopped and started again, and every JVM flag is kept.'),
        h('div.btnrow',
          h('button.btn.sm.primary', {
            disabled: !info.update_available || !canUpdate,
            onclick: () => run(`/api/instances/${ctx.id}/jar/update`, {}, {
              title: `Update ${fam} on ${ctx.name}`,
              onEnd: (r) => { if (r.status === 'done') { jar(); ctx.reload().catch(() => {}); } },
            }).catch(() => {}),
          }, hl(), icon('download', 13), h('span', info.update_available
            ? `Update to build ${info.latest}` : 'Nothing to update'))),
        toggle('Follow new builds every night', !!schedule, async (on) => {
          try {
            if (on) {
              await api.post(`/api/instances/${ctx.id}/schedules`, {
                action: 'update_jar', cron: '30 5 * * *', name: 'Follow new builds' });
              toast('It checks for a newer build every night at 05:30.', 'ok');
            } else if (schedule) {
              await api.del(`/api/instances/${ctx.id}/schedules/${schedule.id}`);
              toast('It no longer updates by itself.', 'ok');
            }
            jar();
          } catch (e) { toastError(e); jar(); }
        }, { disabled: !may('schedules.manage', ctx.id),
          help: 'A nightly task in Schedules. Change its time there.' }))));
  }

  /* --- who can reach it (admins) -------------------------------- */
  async function access() {
    if (!isAdmin()) return;
    mount(accessHost, loadingFox('Reading who can reach it', 'nod'));
    let data;
    try { data = await api.get(`/api/instances/${ctx.id}/access`); } catch (e) {
      mount(accessHost, h('div.note.bad', e.message)); return;
    }
    const HOW = { admin: 'admin — everything', 'every server': 'every server, by their scope',
      scope: 'this server is in their scope', rack: 'its rack is in their scope',
      grant: 'a grant on this server' };
    mount(accessHost, h('section.panel.rule-card.access',
      h('header', h('span.rk', { 'aria-hidden': 'true' }, '鍵'), h('h3', 'Who can reach it'),
        h('span.sp', `${data.items.length} account${data.items.length === 1 ? '' : 's'}`)),
      h('div.pad',
        h('p.muted', 'A grant gives an account permissions on this one server on top of its '
          + 'role — a guest everywhere can run the server you give them.'),
        h('div.tablewrap', h('table.tbl',
          h('thead', h('tr', h('th', 'Account'), h('th', 'How'), h('th', 'May'), h('th', ''))),
          h('tbody', ...data.items.map((u) => h('tr',
            h('td', h('b', u.display), h('div.mono.muted', `${u.username} · ${u.role}`)),
            h('td', HOW[u.how] || u.how),
            h('td', h('div.chiprow', ...u.permissions.slice(0, 6).map((p) => pill(p, 'ghost')),
              u.permissions.length > 6 ? pill(`+${u.permissions.length - 6}`, 'ghost') : null)),
            h('td', u.role === 'admin' ? null : h('button.btn.xs', {
              onclick: () => grantEditor(u.username, u.grant || [], data.grantable),
            }, hl(), icon('key', 12), h('span', u.how === 'grant' ? 'Change' : 'Grant')))))))),
        h('div.btnrow', { style: { marginTop: '12px' } },
          h('button.btn.sm', { onclick: () => pickAccount(data) }, hl(), icon('plus', 13),
            h('span', 'Give someone access'))))));
  }

  async function pickAccount(data) {
    let users = [];
    try { users = (await api.get('/api/admin/users')).items || []; } catch (e) { toastError(e); return; }
    const have = new Set(data.items.map((u) => u.username));
    const others = users.filter((u) => !have.has(u.username) && !u.is_admin);
    if (!others.length) { toast('Every account can already reach this server.', 'info'); return; }
    const sel = h('select.inp', ...others.map((u) => h('option', { value: u.username },
      `${u.display} (${u.username}, ${u.role})`)));
    modal({
      title: 'Give someone access',
      body: field('Account', sel),
      footer: h('div.btnrow', h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', { onclick: () => { close(); grantEditor(sel.value, [], data.grantable); } },
          hl(), h('span', 'Choose permissions'))),
    });
  }

  function grantEditor(username, current, grantable) {
    const chosen = new Set(current);
    const chips = h('div.chiprow');
    const paint = () => mount(chips, ...grantable.map((p) => h('button.chip', {
      type: 'button', 'aria-pressed': String(chosen.has(p)),
      onclick: () => { if (chosen.has(p)) chosen.delete(p); else chosen.add(p); paint(); },
    }, p)));
    paint();
    const save = async (perms) => {
      try {
        await api.post(`/api/admin/users/${encodeURIComponent(username)}/servers/${ctx.id}`,
          { permissions: perms });
        close();
        toast(perms.length ? `${username} can now ${perms.length} thing${perms.length === 1 ? '' : 's'} here.`
          : `${username}'s grant here is gone.`, 'ok');
        access();
      } catch (e) { toastError(e); }
    };
    modal({
      title: `What ${username} may do on ${ctx.name}`,
      body: h('div', h('p.muted', 'On top of their role. Seeing the server is always '
        + 'included. Server-wide rights (making servers, racks) cannot be given per server.'),
      chips),
      footer: h('div.btnrow',
        current.length ? h('button.btn.danger', { onclick: () => save([]) }, hl(),
          h('span', 'Take it away')) : null,
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', { onclick: () => save([...chosen]) }, hl(), h('span', 'Save'))),
    });
  }

  await Promise.all([life(), jar(), access()]);
  return { node };
}

