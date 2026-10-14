/* ============================================================
   The Gate — everything about the whole install, for admins.

   Settings is yours: your password, your tokens, how it looks.
   The Gate is the shrine's: who may come in (Keepers), the
   machines the servers run on (Nodes), the public status page,
   every API token anyone has made, the connections, the
   assistant, upkeep, and the ledger of everything done.

   A torii entrance flanked by two kitsune seals, and a list of
   sections with their kanji. The server refuses every call
   behind this screen to a non-admin regardless of what it shows.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, ago, bytes, loadingFox,
  confirmDialog, modal, close, field, toggle, copyText, empty, mascot, spinner,
} from '../core.js';
import { state, go } from '../app.js';
import { isAdmin } from '../auth.js';

const SECTIONS = [
  { key: 'keepers', kanji: '守', label: 'Keepers', sub: 'accounts, roles, per-server rights' },
  { key: 'nodes', kanji: '機', label: 'Nodes', sub: 'the machines the servers run on' },
  { key: 'status', kanji: '告', label: 'Status page', sub: 'what the public sees' },
  { key: 'tokens', kanji: '鍵', label: 'Tokens', sub: 'every API token' },
  { key: 'connections', kanji: '繋', label: 'Connections', sub: 'database, queue, catalogues' },
  { key: 'assistant', kanji: '智', label: 'Assistant', sub: 'the troubleshooting model' },
  { key: 'upkeep', kanji: '掃', label: 'Upkeep', sub: 'cache, update sweeps, decisions' },
  { key: 'ledger', kanji: '帳', label: 'Ledger', sub: 'everything anybody did' },
];

export async function render({ params }) {
  const node = h('div.wrap.gate');
  if (!isAdmin()) {
    mount(node, empty('shake', 'The Gate is for admins',
      'Your account can do what Settings shows. An admin can give you more.',
      h('a.btn.primary', { href: '#/settings' }, hl(), h('span', 'Settings'))));
    return { node };
  }
  const section = SECTIONS.find((s) => s.key === params.section) || null;
  const body = h('div.gate-body');
  mount(node,
    h('header.gate-head',
      h('span.gate-guard.l', { 'aria-hidden': 'true' }, '狐'),
      h('div.gate-arch',
        h('span.ga-kasagi', { 'aria-hidden': 'true' }), h('span.ga-nuki', { 'aria-hidden': 'true' }),
        h('div.ga-words',
          h('p.eyebrow', 'Admins only'),
          h('h1.brush', 'The Gate'),
          h('p.lede', 'The whole shrine: who may enter, the machines, what the public '
            + 'sees, and the ledger.'))),
      h('span.gate-guard.r', { 'aria-hidden': 'true' }, '狐')),
    h('div.gate-layout',
      h('nav.gate-nav', { 'aria-label': 'Gate sections' },
        ...SECTIONS.map((s) => h('a.gate-link', {
          href: `#/gate/${s.key}`, 'aria-current': s.key === section?.key ? 'page' : null,
        }, h('span.gl-k', { 'aria-hidden': 'true' }, s.kanji),
        h('span', h('b', s.label), h('small', s.sub))))),
      body));

  if (!section) {
    mount(body, h('div.gate-welcome',
      mascot('blockwalk', 96),
      h('p', 'Pick a section. Settings (your own account and tokens) is ',
        h('a', { href: '#/settings' }, 'over here'), '.'),
      h('div.gate-cards', ...SECTIONS.map((s) => h('a.gate-card', { href: `#/gate/${s.key}` },
        h('span.gl-k', { 'aria-hidden': 'true' }, s.kanji), h('b', s.label), h('small', s.sub))))));
    return { node };
  }

  mount(body, loadingFox(`Opening ${section.label}`, 'nod'));
  try {
    mount(body, await SECTION_RENDER[section.key]());
  } catch (e) {
    mount(body, h('div.note.bad', e.message || String(e)));
  }
  return { node };
}

const SECTION_RENDER = {
  keepers: async () => (await import('./admin.js')).registerPanel(),
  ledger: async () => h('div.panel', h('header', icon('fileText', 15), h('h3', 'The ledger')),
    h('div.pad', (await import('./admin.js')).ledgerPanel())),
  connections: async () => (await (await import('./settings.js')).machine('connections')).node,
  assistant: async () => (await (await import('./settings.js')).machine('assistant')).node,
  upkeep: async () => (await (await import('./settings.js')).machine('upkeep')).node,
  nodes: nodesSection,
  status: statusSection,
  tokens: tokensSection,
};

/* --- Nodes ---------------------------------------------------------- */
async function nodesSection() {
  const node = h('div.nodes');
  const { items, engine } = await api.get('/api/admin/nodes');
  const cards = h('div.node-grid');
  mount(node,
    h('div.listbar', h('b.brush', 'Nodes'),
      h('span.muted', engine === 'mock' ? 'the in-memory test engine is running the servers'
        : `${items.length} registered`),
      h('div.grow'),
      h('button.btn.sm.primary', { onclick: registerNode }, hl(), icon('plus', 13),
        h('span', 'Register a node'))),
    items.length ? cards : empty('sneakwalk', 'No nodes registered',
      engine === 'mock' ? 'This install runs on the in-memory engine, which needs no node.'
        : 'Register the machine the servers run on: it gets an agent config, shown once.'));
  for (const n of items) {
    const sys = h('div.node-sys', spinner('Reading the machine'));
    const lit = n.state === 'online';
    cards.append(h(`section.node-card${lit ? '.lit' : '.dark'}`,
      h('div.nc-head',
        h('span.nc-torii', { 'aria-hidden': 'true' }, icon('torii', 30)),
        h('div', h('b.brush', n.name || n.id), h('div.mono.muted', `${n.fqdn}:${n.agent_port}`)),
        pill(lit ? 'answering' : n.state, lit ? 'ok' : 'dead')),
      h('div.nc-facts.mono',
        n.agent_version ? h('span', `agent ${n.agent_version}`) : null,
        n.last_seen ? h('span', `seen ${ago(n.last_seen)}`) : h('span', 'never seen')),
      sys,
      h('div.btnrow',
        h('button.btn.xs', { onclick: () => readSystem(n, sys) }, hl(), icon('refresh', 11),
          h('span', 'Read the system')))));
    readSystem(n, sys);
  }
  return node;
}

async function readSystem(n, host) {
  mount(host, spinner('Reading the machine'));
  try {
    const s = await api.get(`/api/admin/nodes/${encodeURIComponent(n.id)}/system`);
    const memUsed = s.ram_bytes && s.ram_available_bytes !== null
      ? (s.ram_bytes - s.ram_available_bytes) / s.ram_bytes * 100 : null;
    const diskUsed = s.disk_total ? (s.disk_total - s.disk_free) / s.disk_total * 100 : null;
    const committed = s.ram_bytes ? (s.committed_bytes || 0) / s.ram_bytes * 100 : null;
    mount(host,
      meterRow('Memory in use', memUsed, s.ram_bytes
        ? `${bytes(s.ram_bytes - s.ram_available_bytes)} of ${bytes(s.ram_bytes)}` : '—'),
      meterRow('Promised to servers', committed, s.committed_bytes !== undefined
        ? `${bytes(s.committed_bytes)} of their limits` : '—'),
      meterRow('Disk', diskUsed, s.disk_total ? `${bytes(s.disk_free)} free` : '—'),
      h('div.chiprow', { style: { marginTop: '8px' } },
        pill(`${s.cpu_threads || '?'} threads`, 'ghost'),
        s.load_1m !== null && s.load_1m !== undefined ? pill(`load ${s.load_1m.toFixed(2)}`, 'ghost') : null,
        pill(`${s.running ?? 0} of ${s.servers ?? 0} running`, 'info'),
        s.runtime ? pill(s.runtime, 'plum') : null,
        s.error ? pill('runtime not answering', 'bad') : null),
      s.cpu_model ? h('div.mono.muted', { style: { fontSize: '11px', marginTop: '6px' } }, s.cpu_model)
        : null);
  } catch (e) {
    mount(host, h('div.note.bad', e.message));
  }
}

function meterRow(label, pct, text) {
  const v = pct === null || pct === undefined ? null : Math.max(0, Math.min(100, pct));
  const cells = 12;
  const lit = v === null ? 0 : Math.round((v / 100) * cells);
  const tone = v === null ? 'unknown' : v >= 90 ? 'hot' : v >= 70 ? 'warm' : 'calm';
  return h('div.pm',
    h('span.pl', label),
    h(`span.lmeter.${tone}`, ...Array.from({ length: cells }, (_, i) => h(`i${i < lit ? '.on' : ''}`))),
    h('span.pv', text));
}

function registerNode() {
  const name = h('input.inp', { placeholder: 'shrine-02' });
  const fqdn = h('input.inp', { placeholder: 'node2.lan or 192.168.1.40' });
  const port = h('input.inp', { type: 'number', value: 8443 });
  modal({
    title: 'Register a node',
    body: h('div',
      field('Name', name),
      field('Address the panel reaches it on', fqdn),
      field('Agent port', port)),
    footer: h('div.btnrow',
      h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
      h('button.btn.primary', {
        onclick: async () => {
          try {
            const made = await api.post('/api/admin/nodes', {
              name: name.value.trim(), fqdn: fqdn.value.trim(), agent_port: Number(port.value) });
            close();
            shownOnce(made);
          } catch (e) { toastError(e, 'The node was not registered'); }
        },
      }, hl(), h('span', 'Register it'))),
  });
}

/* The agent config holds the node's secret token: shown once, copied, and
   the dialog cannot be dismissed until you say it is saved. */
function shownOnce(made) {
  const done = h('button.btn.primary', { disabled: true, onclick: () => { close(); go('/gate/nodes'); } },
    hl(), h('span', 'Done'));
  modal({
    title: `${made.node?.name || 'The node'} — its agent config, shown once`,
    dismissable: false,
    wide: true,
    body: h('div',
      h('div.note.warn', h('b', 'Shown once'),
        'This holds the node\'s secret token. Save it as the agent\'s config file on that '
        + 'machine now; BlessForge keeps only an encrypted copy and will not show it again.'),
      h('pre.token-once.mono', made.config),
      h('div.btnrow', { style: { margin: '10px 0' } },
        h('button.btn.sm', { onclick: () => copyText(made.config, 'The agent config') }, hl(),
          icon('copy', 12), h('span', 'Copy'))),
      toggle('I have saved it on the node', false, (v) => { done.disabled = !v; })),
    footer: h('div.btnrow', done),
  });
}

/* --- Status page ------------------------------------------------------ */
async function statusSection() {
  const node = h('div.statuscfg');
  const st = await api.get('/api/admin/status-page');
  const chosen = [...(st.servers || [])];
  const title = h('input.inp', { value: st.title, maxlength: 80 });
  const blurb = h('textarea.inp', { maxlength: 280, rows: 2 }, st.blurb || '');
  const address = h('input.inp.mono', { value: st.address || '', placeholder: 'play.example.com' });
  let enabled = !!st.enabled;
  let names = !!st.show_players;
  const list = h('div');
  const paintList = () => mount(list,
    ...state.instances.map((s) => h('label.sp-row',
      h('input', { type: 'checkbox', checked: chosen.includes(s.server_id),
        onchange: (e) => {
          const i = chosen.indexOf(s.server_id);
          if (e.target.checked && i < 0) chosen.push(s.server_id);
          if (!e.target.checked && i >= 0) chosen.splice(i, 1);
          paintList();
        } }),
      h('span', h('b', s.name), h('small.muted', ` ${[s.loader, s.minecraft].filter(Boolean).join(' ')}`)),
      chosen.includes(s.server_id) ? pill(`#${chosen.indexOf(s.server_id) + 1}`, 'ghost') : null)));
  paintList();
  const save = h('button.btn.primary', {
    onclick: async () => {
      save.classList.add('busy');
      try {
        await api.post('/api/admin/status-page', { enabled, show_players: names,
          title: title.value, blurb: blurb.value, address: address.value, servers: chosen });
        toast(enabled ? 'Saved. The page is public.' : 'Saved. The page is switched off.', 'ok');
      } catch (e) { toastError(e, 'Not saved'); } finally { save.classList.remove('busy'); }
    },
  }, hl(), icon('save', 14), h('span', 'Save'));
  mount(node,
    h('div.listbar', h('b.brush', 'Status page'),
      h('span.muted', 'a public page with the servers you choose'), h('div.grow'),
      h('a.btn.sm.ghost', { href: '/status', target: '_blank', rel: 'noopener' },
        icon('external', 13), h('span', 'Open it'))),
    h('div.grid.g2', { style: { alignItems: 'start' } },
      h('section.panel', h('header', icon('globe', 15), h('h3', 'The page')),
        h('div.pad',
          toggle('Public', enabled, (v) => { enabled = v; },
            { help: `Anyone with the link can open ${location.origin}/status. Off, it is a 404.` }),
          field('Title', title),
          field('A line under it', blurb),
          field('Join address', address, 'A host name, without a port: each server\'s own port is '
            + 'added. Blank hides the address.'),
          toggle('Show player names', names, (v) => { names = v; },
            { help: 'Off: only how many are on.' }),
          h('div.btnrow', { style: { marginTop: '12px' } }, save))),
      h('section.panel', h('header', icon('server', 15), h('h3', 'Which servers')),
        h('div.pad', h('p.muted', { style: { fontSize: '12.5px' } },
          'Ticked servers are shown, in the order you ticked them.'), list))));
  return node;
}

/* --- Tokens (everyone's) ------------------------------------------------ */
async function tokensSection() {
  const node = h('div');
  const paint = async () => {
    const { items } = await api.get('/api/admin/tokens');
    mount(node,
      h('div.listbar', h('b.brush', 'Tokens'),
        h('span.muted', `${items.length} across every account — each person makes their own in Settings`)),
      items.length ? h('div.modlist', ...items.map((t) => h('div.modrow',
        h('div.ico', icon('key', 15)),
        h('div.who',
          h('div.nm', `${t.name} `, h('span.muted', `· ${t.owner}`)),
          h('div.fn', `${t.prefix}… · made ${ago(t.created)}`
            + `${t.last_used ? ` · used ${ago(t.last_used)} from ${t.last_ip || '?'}` : ' · never used'}`),
          h('div.tags', ...t.permissions.map((p) => pill(p, 'ghost')))),
        h('div.acts', h('button.btn.xs.danger', {
          onclick: async () => {
            const ok = await confirmDialog({ title: `Revoke ${t.owner}'s ${t.name}?`,
              message: 'Anything using it stops working at once.', confirmLabel: 'Revoke it',
              danger: true, art: 'vanish' });
            if (!ok) return;
            try { await api.del(`/api/admin/tokens/${t.id}`); paint(); } catch (e) { toastError(e); }
          },
        }, icon('trash', 11), h('span', 'Revoke'))))))
        : empty('nod', 'No tokens', 'Nobody has made an API token yet.'));
  };
  await paint();
  return node;
}

