/* ============================================================
   Players — ops, whitelist, bans, and who is on right now.

   Minecraft keeps this in five JSON files and a running server
   keeps its own copy in memory. Editing ops.json while the server
   is up does nothing until it restarts; running /op while it is
   down is impossible. Every button here picks its route from the
   server's actual state and then says which route it took —
   "effective now" and "takes effect at the next start" are
   different facts and hiding the difference is how "I opped them
   and it didn't work" happens.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, empty, loadingFox,
  confirmDialog, promptDialog, modal, close, frag, debounce, segmented,
  toggle, field, ago, num,
} from '../core.js';

export async function render(ctx) {
  const node = h('div');
  const host = h('div');
  mount(node, host, loadingFox('Reading the player files'));

  let data = null;
  let filter = 'all';
  let query = '';
  const selected = new Set();

  async function load(quiet) {
    if (!quiet) mount(host, loadingFox('Reading the player files'));
    try {
      data = await api.get(`/api/instances/${ctx.id}/players`);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    for (const n of [...selected]) {
      if (!data.players.some((p) => p.name === n)) selected.delete(n);
    }
    paint();
  }

  const rows = () => {
    const q = query.trim().toLowerCase();
    return (data?.players || []).filter((p) => {
      if (filter === 'online' && !p.online) return false;
      if (filter === 'ops' && !p.op) return false;
      if (filter === 'whitelist' && !p.whitelisted) return false;
      if (filter === 'banned' && !p.banned) return false;
      if (!q) return true;
      return (p.name || '').toLowerCase().includes(q)
        || (p.uuid || '').toLowerCase().includes(q);
    });
  };

  function paint() {
    const c = data.counts;
    const s = data.settings;
    const list = rows();

    mount(host,
      !data.running
        ? h('div.note.warn', { style: { marginBottom: '16px' } },
          h('b', 'This server is not running'),
          'Changes are written straight to its player files and apply the '
          + 'next time it starts. Kicking somebody is the one thing that '
          + 'genuinely needs a running server.')
        : null,

      h('div.kpis', { style: { marginBottom: '18px' } },
        kpi('Online now', String(c.online), data.running
          ? `of ${data.max_players ?? '?'} slots` : 'server is down',
        data.running && c.online ? 'ok' : ''),
        kpi('Operators', String(c.ops), 'full command access', c.ops ? 'gold' : ''),
        kpi('Whitelisted', String(c.whitelisted),
          s.whitelist_enabled ? 'whitelist is ON' : 'whitelist is off',
          s.whitelist_enabled ? 'ok' : ''),
        kpi('Banned', String(c.banned),
          c.banned_ips ? `${c.banned_ips} IP bans too` : 'no IP bans',
          c.banned ? 'bad' : ''),
      ),

      h('div.split.wide-aside',
        h('div',
          h('div.listbar',
            h('input.inp.grow', {
              type: 'search', 'data-search': '1', value: query,
              placeholder: `Search ${c.known} known players…`,
              'aria-label': 'Search players',
              oninput: debounce((e) => { query = e.target.value; paint(); }, 160),
            }),
            segmented([
              { value: 'all', label: `All ${c.known}` },
              { value: 'online', label: `On ${c.online}` },
              { value: 'ops', label: `Ops ${c.ops}` },
              { value: 'whitelist', label: `List ${c.whitelisted}` },
              { value: 'banned', label: `Banned ${c.banned}` },
            ], filter, (v) => { filter = v; paint(); }, 'Filter players'),
            h('div.grow'),
            h('button.btn.sm.primary', { onclick: addPlayer },
              hl(), icon('plus', 13), h('span', 'Add player')),
          ),

          selected.size
            ? h('div.listbar', { style: { marginTop: '-8px' } },
              h('b', `${selected.size} selected`),
              h('button.btn.sm.ghost', { onclick: () => bulk('op') },
                icon('crown', 13), h('span', 'Op')),
              h('button.btn.sm.ghost', { onclick: () => bulk('whitelist') },
                icon('check', 13), h('span', 'Whitelist')),
              data.running && h('button.btn.sm.ghost', { onclick: () => bulk('kick') },
                icon('x', 13), h('span', 'Kick')),
              h('button.btn.sm.danger', { onclick: () => bulk('ban') },
                icon('ban', 13), h('span', 'Ban')),
              h('div.grow'),
              h('button.btn.sm.ghost', {
                onclick: () => { selected.clear(); paint(); },
              }, h('span', 'Clear')))
            : null,

          !c.known
            ? empty('fox-mascot.png', 'Nobody has been here yet',
              'Players appear once they connect. You can add someone to the '
              + 'whitelist or make them an operator before their first join.',
              h('button.btn.primary', { onclick: addPlayer }, hl(),
                h('span', 'Add a player')))
            : !list.length
              ? h('div.note', 'Nothing matches that filter.')
              : h('div.modlist', ...list.map(playerRow)),
        ),

        h('div.sticky', { style: { display: 'grid', gap: '18px' } },
          h('div.panel',
            h('header', icon('shield', 15), h('h3', 'Access')),
            h('div.pad', { style: { display: 'grid', gap: '12px' } },
              toggle('Whitelist — only listed players may join',
                s.whitelist_enabled, setWhitelistMode,
                { help: s.whitelist_enabled
                  ? 'On. Anyone not on the list is refused at login.'
                  : 'Off. Anyone who can reach the port can join.' }),
              data.running ? h('button.btn.sm.ghost.block', {
                onclick: reloadWhitelist,
              }, icon('refresh', 13),
                h('span', 'Make the server re-read whitelist.json')) : null,
              h('hr', { style: { margin: '4px 0' } }),
              stat('Online mode', s.online_mode ? 'on — accounts verified'
                : 'OFF — names are not verified',
              s.online_mode ? 'ok' : 'warn'),
              stat('Difficulty', s.difficulty || '—'),
              stat('PvP', s.pvp ? 'on' : 'off'),
              stat('Max players', String(s.max_players ?? '—')),
              h('button.btn.sm.ghost.block', { onclick: () => ctx.go('tune') },
                icon('sliders', 13), h('span', 'Edit server.properties')),
            )),

          h('div.panel',
            h('header', icon('ban', 15), h('h3', 'Banned IPs'),
              h('span.sp', String(data.banned_ips.length))),
            h('div.pad', { style: { display: 'grid', gap: '9px' } },
              ...(data.banned_ips.length
                ? data.banned_ips.map((b) => h('div', {
                  style: { display: 'flex', gap: '9px', alignItems: 'center',
                    fontSize: '13px' },
                },
                  h('span.mono', { style: { flex: 1, minWidth: 0,
                    overflowWrap: 'anywhere' } }, b.ip),
                  h('button.btn.icon.xs.ghost', {
                    'aria-label': `Unban ${b.ip}`,
                    onclick: () => ipBan(b.ip, false),
                  }, icon('x', 12))))
                : [h('p.muted', { style: { fontSize: '13px' } },
                  'No IP bans.')]),
              h('button.btn.sm.ghost.block', { onclick: () => ipBan(null, true) },
                icon('plus', 13), h('span', 'Ban an IP')),
            )),
        ),
      ),
    );
  }

  function kpi(label, value, sub, tone = '') {
    return h('div.kpi', h('div.lbl', label),
      h(`div.v${tone ? `.${tone}` : ''}`, value), h('div.sub', sub));
  }

  function stat(label, value, tone = '') {
    return h('div', { style: { display: 'flex', justifyContent: 'space-between',
      gap: '10px', fontSize: '13px' } },
      h('span.muted', label),
      h('span', { class: tone === 'warn' ? 'pill warn' : tone === 'ok' ? '' : '',
        style: { textAlign: 'right' } }, value));
  }

  function playerRow(p) {
    const isSel = selected.has(p.name);
    return h(`div.plrow${p.banned ? '.banned' : ''}`,
      h('input', {
        type: 'checkbox', checked: isSel,
        'aria-label': `Select ${p.name}`,
        onchange: (e) => {
          if (e.target.checked) selected.add(p.name); else selected.delete(p.name);
          paint();
        },
      }),
      h('img.face', {
        src: p.avatar, alt: '', loading: 'lazy',
        onerror: (e) => { e.target.style.visibility = 'hidden'; },
      }),
      h('div.who',
        h('div.nm', p.name),
        p.uuid ? h('div.uid', p.uuid) : null,
        h('div.tags',
          p.online ? pill('online', 'ok') : null,
          p.op ? pill(`op${p.op_level && p.op_level !== 4
            ? ` L${p.op_level}` : ''}`, 'gold') : null,
          p.whitelisted ? pill('whitelisted', 'info') : null,
          p.banned ? pill('banned', 'bad', p.ban_reason || 'banned') : null,
          p.note ? pill('note', 'plum', p.note) : null)),
      h('div.acts',
        p.online && data.running ? h('button.btn.icon.xs.ghost', {
          'aria-label': `Kick ${p.name}`, title: 'Kick',
          onclick: () => kick(p),
        }, icon('x', 12)) : null,
        h('button.btn.icon.xs.ghost', {
          'aria-label': p.op ? `Remove operator from ${p.name}`
            : `Make ${p.name} an operator`,
          title: p.op ? 'De-op' : 'Op',
          onclick: () => act(p.op ? 'deop' : 'op', p.name),
        }, icon('crown', 12)),
        h('button.btn.icon.xs.ghost', {
          'aria-label': p.whitelisted ? `Remove ${p.name} from the whitelist`
            : `Add ${p.name} to the whitelist`,
          title: p.whitelisted ? 'Remove from whitelist' : 'Whitelist',
          onclick: () => act(p.whitelisted ? 'unwhitelist' : 'whitelist', p.name),
        }, icon(p.whitelisted ? 'minus' : 'check', 12)),
        h('button.btn.icon.xs.ghost', {
          'aria-label': `More actions for ${p.name}`,
          onclick: () => playerMenu(p),
        }, icon('sliders', 12)),
      ));
  }

  /* --- actions -------------------------------------------------- */

  async function act(action, name, extra = {}) {
    try {
      const res = await api.post(`/api/instances/${ctx.id}/players/action`,
        { action, name, ...extra });
      toast(res.message, res.effective === 'now' ? 'ok' : 'warn',
        { timeout: 6000 });
      await load(true);
    } catch (e) { toastError(e); }
  }

  async function kick(p) {
    const reason = await promptDialog({
      title: `Kick ${p.name}`, label: 'Reason (shown to them)',
      value: 'Kicked by an operator', confirmLabel: 'Kick',
    });
    if (reason === null) return;
    act('kick', p.name, { reason });
  }

  async function bulk(action) {
    const names = [...selected];
    let reason = '';
    if (action === 'ban' || action === 'kick') {
      reason = await promptDialog({
        title: `${action === 'ban' ? 'Ban' : 'Kick'} ${names.length} players`,
        label: 'Reason', value: 'Banned by an operator',
        confirmLabel: action === 'ban' ? 'Ban them' : 'Kick them',
      });
      if (reason === null) return;
    }
    try {
      const res = await api.post(`/api/instances/${ctx.id}/players/bulk`,
        { action, names, reason });
      toast(`${res.count} done`
        + (res.failed.length ? `, ${res.failed.length} failed` : ''),
      res.failed.length ? 'warn' : 'ok');
      selected.clear();
      await load(true);
    } catch (e) { toastError(e); }
  }

  function playerMenu(p) {
    modal({
      title: p.name,
      body: frag(
        h('div', { style: { display: 'flex', gap: '12px', alignItems: 'center',
          marginBottom: '14px' } },
          h('img', { src: p.avatar, alt: '',
            style: { width: '56px', height: '56px',
              border: '2px solid var(--rule)', imageRendering: 'pixelated' } }),
          h('div', { style: { minWidth: 0 } },
            h('div.h-num', { style: { fontSize: '17px' } }, p.name),
            p.uuid ? h('div.mono.muted.wrapany', { style: { fontSize: '10.5px' } },
              p.uuid) : null)),
        p.banned ? h('div.note.bad', { style: { marginBottom: '12px' } },
          h('b', 'Banned'),
          `${p.ban_reason || 'no reason given'}`
          + (p.banned_by ? ` — by ${p.banned_by}` : '')
          + (p.banned_at ? ` on ${p.banned_at}` : '')) : null,
        h('div.field',
          h('label', 'Note (only you see this)'),
          h('input.inp', {
            id: 'plnote', value: p.note || '', maxLength: 500,
            placeholder: 'Builds well, asks a lot of questions',
          }),
          h('div.help', 'Stored with the server, not sent anywhere.')),
        h('div', { style: { display: 'grid', gap: '9px' } },
          h('button.btn.sm', {
            onclick: () => { close(); act(p.op ? 'deop' : 'op', p.name); },
          }, hl(), icon('crown', 13),
            h('span', p.op ? 'Remove operator' : 'Make operator')),
          h('button.btn.sm', {
            onclick: () => {
              close(); act(p.whitelisted ? 'unwhitelist' : 'whitelist', p.name);
            },
          }, hl(), icon('check', 13),
            h('span', p.whitelisted ? 'Remove from whitelist' : 'Add to whitelist')),
          p.banned
            ? h('button.btn.sm', {
              onclick: () => { close(); act('pardon', p.name); },
            }, hl(), icon('check', 13), h('span', 'Unban'))
            : h('button.btn.sm.danger', {
              onclick: async () => {
                close();
                const reason = await promptDialog({
                  title: `Ban ${p.name}`, label: 'Reason (shown to them)',
                  value: 'Banned by an operator', confirmLabel: 'Ban',
                });
                if (reason !== null) act('ban', p.name, { reason });
              },
            }, icon('ban', 13), h('span', 'Ban')),
        ),
      ),
      footer: frag(
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Close')),
        h('button.btn.primary', {
          onclick: async () => {
            const note = document.querySelector('#plnote')?.value || '';
            close();
            try {
              await api.post(`/api/instances/${ctx.id}/players/note`,
                { name: p.name, note });
              toast('Note saved.', 'ok');
              load(true);
            } catch (e) { toastError(e); }
          },
        }, hl(), h('span', 'Save note')),
      ),
    });
  }

  async function addPlayer() {
    let profile = null;
    const nameInput = h('input.inp', {
      placeholder: 'Notch', maxLength: 16, autocomplete: 'off',
      'aria-label': 'Minecraft username',
    });
    const status = h('div.help', 'Looked up on Mojang when you leave the box.');
    const opts = { op: false, whitelist: true };

    nameInput.addEventListener('blur', async () => {
      const name = nameInput.value.trim();
      if (!name) return;
      status.textContent = 'Looking up…';
      try {
        profile = await api.get(
          `/api/players/lookup?name=${encodeURIComponent(name)}`
          + `&online_mode=${data.settings.online_mode}`);
        status.textContent = profile.source === 'mojang'
          ? `Mojang account ${profile.uuid}`
          : `No Mojang lookup — using the offline UUID ${profile.uuid}, which is `
            + 'what this server would generate itself.';
      } catch (e) {
        profile = null;
        status.textContent = e.message;
      }
    });

    modal({
      title: 'Add a player',
      body: frag(
        h('div.field', h('label', 'Minecraft username'), nameInput, status),
        h('div', { style: { display: 'grid', gap: '10px' } },
          toggle('Add to the whitelist', true, (v) => { opts.whitelist = v; }),
          toggle('Make them an operator', false, (v) => { opts.op = v; },
            { help: 'Operators can run every command, including /stop.' })),
      ),
      footer: frag(
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', {
          onclick: async () => {
            const name = nameInput.value.trim();
            if (!name) { nameInput.focus(); return; }
            close();
            if (opts.whitelist) await act('whitelist', name);
            if (opts.op) await act('op', name);
            if (!opts.whitelist && !opts.op) {
              toast('Nothing was selected, so nothing changed.', 'warn');
            }
          },
        }, hl(), h('span', 'Add')),
      ),
    });
  }

  async function setWhitelistMode(on) {
    try {
      const res = await api.post(`/api/instances/${ctx.id}/players/whitelist-mode`,
        { enabled: on });
      toast(res.message, 'ok', { timeout: 6000 });
      await load(true);
    } catch (e) { toastError(e); load(true); }
  }

  async function reloadWhitelist() {
    try {
      const res = await api.post(`/api/instances/${ctx.id}/players/whitelist-mode`,
        { reload: true });
      toast(res.message, 'ok');
    } catch (e) { toastError(e); }
  }

  async function ipBan(ip, on) {
    let target = ip;
    let reason = '';
    if (on) {
      target = await promptDialog({
        title: 'Ban an IP address', label: 'IP address',
        placeholder: '192.0.2.10', confirmLabel: 'Continue',
        help: 'Bans everyone connecting from that address, whatever name they '
          + 'use.',
      });
      if (!target) return;
      reason = await promptDialog({
        title: `Ban ${target}`, label: 'Reason',
        value: 'Banned by an operator', confirmLabel: 'Ban',
      });
      if (reason === null) return;
    }
    try {
      const res = await api.post(`/api/instances/${ctx.id}/players/action`,
        { action: on ? 'ban-ip' : 'pardon-ip', ip: target, reason });
      toast(res.message, 'ok');
      await load(true);
    } catch (e) { toastError(e); }
  }

  await load();

  // Who is online changes on the scale of a join, so this refreshes while the
  // tab is open — but only while the server is up and the tab is visible.
  const timer = setInterval(() => {
    if (document.hidden || !ctx.running) return;
    load(true).catch(() => {});
  }, 20000);

  return {
    node,
    dispose: () => clearInterval(timer),
    onRunningChanged: () => load(true).catch(() => {}),
  };
}
