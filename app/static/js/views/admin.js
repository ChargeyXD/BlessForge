/* ============================================================
   admin — accounts, what they may touch, and what they did.

   Rendered inside Settings rather than as its own route: it is
   administration of this instance, and putting it behind a
   separate destination in the rail would offer a door most
   people cannot open.

   Two halves. THE REGISTER is the accounts, drawn as ofuda —
   paper talismans on a shrine wall, one per person, each with
   its own seal. THE LEDGER is the audit log.

   Everything here is a courtesy: the server refuses these calls
   from a non-admin whatever this file does.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, ago, confirmDialog,
  modal, close, field, segmented, debounce, spinner, prefs, duration,
} from '../core.js';
import { session } from '../auth.js';
import { state } from '../app.js';

let meta = null;          // permissions, roles, role->permissions

async function loadMeta() {
  if (meta) return meta;
  meta = await api.get('/api/admin/permissions');
  return meta;
}

/* --- the register ------------------------------------------------ */

export async function adminPanel() {
  const node = h('div.adm');
  const usersHost = h('div');
  const ledgerHost = h('div');

  mount(node,
    h('div.adm-head',
      h('div',
        h('h3.adm-title', 'The register'),
        h('p.adm-sub.mono', 'Who may open this shrine, and how far in')),
      h('button.btn.sm.primary', { onclick: () => editUser(null, paintUsers) },
        hl(), icon('plus', 13), h('span', 'New account'))),
    usersHost,
    h('div.adm-head', { style: { marginTop: '26px' } },
      h('div',
        h('h3.adm-title', 'The ledger'),
        h('p.adm-sub.mono', 'Everything anybody did'))),
    ledgerHost);

  async function paintUsers() {
    mount(usersHost, spinner('Reading the register'));
    try {
      await loadMeta();
      const { items } = await api.get('/api/admin/users');
      mount(usersHost, h('div.ofuda-wall.stagger',
        ...items.map((u) => userCard(u, paintUsers))));
    } catch (e) {
      mount(usersHost, h('div.note.bad', e.message));
    }
  }

  paintUsers();
  mount(ledgerHost, ledger());
  return node;
}

function userCard(u, repaint) {
  const me = u.username === session.user?.username;
  const locked = (u.locked_until || 0) * 1000 > Date.now();
  const scope = u.scope || {};
  const reach = u.is_admin ? 'every server'
    : scope.all ? 'every server'
      : [
        scope.servers?.length ? `${scope.servers.length} server${scope.servers.length === 1 ? '' : 's'}` : null,
        scope.racks?.length ? `${scope.racks.length} rack${scope.racks.length === 1 ? '' : 's'}` : null,
      ].filter(Boolean).join(' · ') || 'nothing yet';

  return h(`div.ofuda-user${u.disabled ? '.off' : ''}${u.is_admin ? '.boss' : ''}`,
    h('span.ou-seal', { 'aria-hidden': 'true' },
      (u.display || u.username).slice(0, 1).toUpperCase()),
    h('div.ou-body',
      h('div.ou-top',
        h('span.ou-name.trunc', { title: u.username }, u.display || u.username),
        u.is_admin ? pill('admin', 'gold') : pill(u.role, 'ghost'),
        me ? pill('you', 'plum') : null,
        u.disabled ? pill('suspended', 'dead') : null,
        locked ? pill('locked out', 'bad') : null),
      h('div.ou-line.mono',
        h('span', `@${u.username}`),
        h('span.ou-dot', '·'),
        h('span', reach),
        h('span.ou-dot', '·'),
        h('span', u.sessions
          ? `${u.sessions} signed in` : 'not signed in')),
      h('div.ou-line.mono.muted',
        u.last_login
          ? `last seen ${ago(u.last_login)}`
          : 'never signed in',
        u.must_change_password ? ' · must set a new password' : ''),
      h('div.ou-acts',
        h('button.btn.xs.ghost', { onclick: () => editUser(u, repaint) },
          icon('edit', 11), h('span', 'Edit')),
        h('button.btn.xs.ghost', { onclick: () => resetPassword(u, repaint) },
          icon('key', 11), h('span', 'Set password')),
        locked
          ? h('button.btn.xs.gold', {
            onclick: async () => {
              try {
                await api.post(`/api/admin/users/${u.username}`, { unlock: true });
                toast(`${u.username} unlocked`, 'ok');
                repaint();
              } catch (e) { toastError(e); }
            },
          }, icon('unlock', 11), h('span', 'Unlock'))
          : null,
        u.sessions
          ? h('button.btn.xs.ghost', {
            onclick: async () => {
              try {
                const r = await api.del(`/api/admin/users/${u.username}/sessions`);
                toast(`${r.ended} session${r.ended === 1 ? '' : 's'} ended`, 'ok');
                repaint();
              } catch (e) { toastError(e); }
            },
          }, icon('x', 11), h('span', 'Sign out everywhere'))
          : null,
        me ? null : h('button.btn.xs.ghost', {
          onclick: async () => {
            try {
              await api.post(`/api/admin/users/${u.username}`,
                { disabled: !u.disabled });
              toast(u.disabled ? `${u.username} restored`
                : `${u.username} suspended`, 'ok');
              repaint();
            } catch (e) { toastError(e); }
          },
        }, icon(u.disabled ? 'check' : 'pause', 11),
        h('span', u.disabled ? 'Restore' : 'Suspend')),
        me ? null : h('button.btn.xs.danger', {
          onclick: () => removeUser(u, repaint),
        }, icon('trash', 11), h('span', 'Delete')))));
}

/* --- making and editing an account ------------------------------- */

function editUser(existing, repaint) {
  const isNew = !existing;
  const form = {
    username: existing?.username || '',
    display: existing?.display || '',
    role: existing?.role || 'member',
    password: '',
    permissions: new Set(existing?.permissions || []),
    scopeAll: !!existing?.scope?.all,
    servers: new Set(existing?.scope?.servers || []),
    racks: new Set(existing?.scope?.racks || []),
    mustChange: true,
  };

  const permHost = h('div');
  const scopeHost = h('div');

  const paintPerms = () => {
    const base = new Set(meta.role_permissions[form.role] || []);
    mount(permHost, h('div.permgrid',
      ...Object.entries(meta.permissions).map(([key, what]) => {
        const fromRole = base.has(key);
        const on = fromRole || form.permissions.has(key);
        return h(`label.permrow${on ? '.on' : ''}${fromRole ? '.byrole' : ''}`,
          h('input', {
            type: 'checkbox', checked: on, disabled: fromRole || form.role === 'admin',
            onchange: (e) => {
              if (e.target.checked) form.permissions.add(key);
              else form.permissions.delete(key);
              paintPerms();
            },
          }),
          h('span.pr-key.mono', key),
          h('span.pr-what', what),
          fromRole ? h('span.pr-src.mono', 'from role') : null);
      })));
  };

  const paintScope = () => {
    const fleet = state.instances || [];
    const racks = state.groups?.groups || [];
    mount(scopeHost,
      h('label.scope-all',
        h('input', {
          type: 'checkbox', checked: form.scopeAll,
          onchange: (e) => { form.scopeAll = e.target.checked; paintScope(); },
        }),
        h('span', 'Every server, including ones made later')),
      form.scopeAll ? null : h('div.scope-pick',
        h('div.sp-head.mono', 'By rack'),
        racks.length
          ? h('div.chiprow', ...racks.map((g) => h('button.chip', {
            type: 'button',
            'aria-pressed': String(form.racks.has(g.id)),
            onclick: () => {
              form.racks.has(g.id) ? form.racks.delete(g.id) : form.racks.add(g.id);
              paintScope();
            },
          }, h('span', g.name))))
          : h('p.muted', { style: { fontSize: '12px' } }, 'No racks yet.'),
        h('div.sp-head.mono', { style: { marginTop: '12px' } }, 'By server'),
        fleet.length
          ? h('div.chiprow', ...fleet.map((s) => h('button.chip', {
            type: 'button',
            'aria-pressed': String(form.servers.has(s.server_id)),
            onclick: () => {
              form.servers.has(s.server_id)
                ? form.servers.delete(s.server_id)
                : form.servers.add(s.server_id);
              paintScope();
            },
          }, h('span', s.name || s.server_id))))
          : h('p.muted', { style: { fontSize: '12px' } }, 'No servers yet.')));
  };

  const body = h('div.adm-form',
    isNew
      ? field('Username', h('input.inp', {
        value: form.username, autocapitalize: 'none', spellcheck: 'false',
        placeholder: 'lowercase, no spaces',
        oninput: (e) => { form.username = e.target.value; },
      }), '2-32 characters: letters, digits, dot, dash or underscore.')
      : h('div.adm-static',
        h('span.mono.muted', 'Username'), h('strong', existing.username)),
    field('Display name', h('input.inp', {
      value: form.display, placeholder: 'optional',
      oninput: (e) => { form.display = e.target.value; },
    })),
    isNew
      ? field('First password', h('input.inp', {
        type: 'password', autocomplete: 'new-password',
        oninput: (e) => { form.password = e.target.value; },
      }), 'At least 10 characters. They will be made to change it on first '
        + 'sign-in.')
      : null,
    field('Role', h('div.rolepick',
      ...Object.entries(meta.roles).map(([key, what]) =>
        h(`button.rolecard${form.role === key ? '.on' : ''}`, {
          type: 'button',
          onclick: () => { form.role = key; paintRole(); paintPerms(); },
        }, h('span.rc-name', key), h('span.rc-what', what))))),
    h('div.adm-sec.mono', 'What they may do'),
    permHost,
    h('div.adm-sec.mono', 'What they may touch'),
    scopeHost);

  const paintRole = () => {
    [...body.querySelectorAll('.rolecard')].forEach((b) => {
      b.classList.toggle('on', b.querySelector('.rc-name').textContent === form.role);
    });
  };

  paintPerms();
  paintScope();

  const { close: shut } = modal({
    title: isNew ? 'A new account' : `Edit ${existing.username}`,
    wide: true,
    body,
    footer: h('div.btnrow',
      h('button.btn.ghost', { onclick: () => shut() }, h('span', 'Cancel')),
      h('button.btn.primary', {
        onclick: async () => {
          const payload = {
            display: form.display,
            role: form.role,
            permissions: [...form.permissions],
            scope: {
              all: form.scopeAll,
              servers: [...form.servers],
              racks: [...form.racks],
            },
          };
          try {
            if (isNew) {
              await api.post('/api/admin/users', {
                ...payload, username: form.username,
                password: form.password, must_change: true,
              });
              toast(`${form.username} may now sign in`, 'ok');
            } else {
              await api.post(`/api/admin/users/${existing.username}`, payload);
              toast(`${existing.username} updated`, 'ok');
            }
            shut();
            repaint();
          } catch (e) { toastError(e); }
        },
      }, hl(), h('span', isNew ? 'Create the account' : 'Save'))),
  });
}

function resetPassword(u, repaint) {
  let value = '';
  let mustChange = true;
  const { close: shut } = modal({
    title: `Set a password for ${u.username}`,
    body: h('div.adm-form',
      h('p.muted', { style: { fontSize: '13px', marginBottom: '12px' } },
        'There is no self-service reset, so this is the way back in for '
        + 'somebody who has forgotten theirs. Every session they have open '
        + 'will end.'),
      field('New password', h('input.inp', {
        type: 'password', autocomplete: 'new-password', autofocus: true,
        oninput: (e) => { value = e.target.value; },
      }), 'At least 10 characters.'),
      h('label.switch', { style: { marginTop: '8px' } },
        h('input', {
          type: 'checkbox', checked: true,
          onchange: (e) => { mustChange = e.target.checked; },
        }),
        h('span.track'),
        h('span', 'Make them change it on their next sign-in'))),
    footer: h('div.btnrow',
      h('button.btn.ghost', { onclick: () => shut() }, h('span', 'Cancel')),
      h('button.btn.primary', {
        onclick: async () => {
          try {
            const r = await api.post(`/api/admin/users/${u.username}/password`,
              { password: value, must_change: mustChange });
            toast(`Password set. ${r.sessions_ended} session`
              + `${r.sessions_ended === 1 ? '' : 's'} ended.`, 'ok');
            shut();
            repaint();
          } catch (e) { toastError(e); }
        },
      }, hl(), h('span', 'Set it'))),
  });
}

async function removeUser(u, repaint) {
  const yes = await confirmDialog({
    title: `Delete ${u.username}?`,
    body: h('div',
      h('p', `Everything ${u.username} could reach stays exactly where it is `
        + '— this removes the account, not the servers.'),
      h('p.muted', 'They will be signed out immediately and cannot come back '
        + 'without a new account.')),
    confirm: 'Delete the account',
    danger: true,
    requireText: u.username,
  });
  if (!yes) return;
  try {
    await api.del(`/api/admin/users/${u.username}`);
    toast(`${u.username} deleted`, 'ok');
    repaint();
  } catch (e) { toastError(e); }
}

/* --- the ledger --------------------------------------------------- */

function ledger() {
  const node = h('div.ledger');
  const rows = h('div.led-rows');
  const filters = { actor: '', action: '', q: '', since: 0 };
  let actions = [];
  let actors = [];

  const load = debounce(async () => {
    mount(rows, spinner('Reading the ledger'));
    try {
      const qs = new URLSearchParams({ limit: '250' });
      if (filters.actor) qs.set('actor', filters.actor);
      if (filters.action) qs.set('action', filters.action);
      if (filters.q) qs.set('q', filters.q);
      if (filters.since) qs.set('since', String(filters.since));
      const { items } = await api.get(`/api/admin/audit?${qs}`);
      if (!items.length) {
        mount(rows, h('div.note', 'Nothing matches that.'));
        return;
      }
      mount(rows, h('div.led-list', ...items.map(entry)));
    } catch (e) {
      mount(rows, h('div.note.bad', e.message));
    }
  }, 160);

  const bar = h('div.led-bar',
    h('input.inp.grow', {
      type: 'search', placeholder: 'Search the ledger…',
      'data-keep': 'audit-search', 'aria-label': 'Search the audit log',
      oninput: debounce((e) => { filters.q = e.target.value; load(); }, 200),
    }),
    h('select.inp', {
      'aria-label': 'Filter by who',
      onchange: (e) => { filters.actor = e.target.value; load(); },
    }, h('option', { value: '' }, 'Anyone')),
    h('select.inp', {
      'aria-label': 'Filter by what',
      onchange: (e) => { filters.action = e.target.value; load(); },
    }, h('option', { value: '' }, 'Anything')),
    segmented([
      { value: '0', label: 'All time' },
      { value: '1', label: 'Today' },
      { value: '7', label: '7 days' },
    ], '0', (v) => {
      filters.since = v === '0' ? 0 : Date.now() / 1000 - Number(v) * 86400;
      load();
    }, 'Since'));

  api.get('/api/admin/audit/actions').then((d) => {
    actions = d.actions || [];
    actors = d.actors || [];
    const selects = bar.querySelectorAll('select');
    actors.forEach((a) => selects[0].append(h('option', { value: a }, a)));
    actions.forEach((a) => selects[1].append(h('option', { value: a }, a)));
  }).catch(() => {});

  mount(node, bar, rows);
  load();
  return node;
}

/* The shape of one line. Failures are marked, because a ledger where a
   refusal looks like an action is a ledger that hides the interesting
   half -- somebody trying to do what they may not is the entry an admin
   most wants to find. */
function entry(e) {
  const failed = e.ok === false;
  const server = e.server_id
    ? (state.instanceById?.get(e.server_id)?.name || e.server_id.slice(0, 8))
    : '';
  return h(`div.led-row${failed ? '.no' : ''}`,
    h('span.led-when.mono', { title: new Date(e.at * 1000).toLocaleString() },
      ago(e.at)),
    h('span.led-who.mono', e.actor),
    h('span.led-what.mono', e.action),
    server ? h('span.led-where', server) : h('span'),
    h('span.led-detail.trunc', { title: e.detail || '' }, e.detail || ''),
    failed ? pill('refused', 'bad') : null);
}
