/* ============================================================
   The public status page (/status).

   No sign-in, no shell: a title, a line, and the servers an admin
   chose, each an ema plaque with its lantern, its hanko seal,
   who is on and the address to join. Read from /api/status (one
   cached view for every visitor, app/statuspage.py) and read
   again every 30 seconds while the page is visible.

   Built from the same pieces as the panel -- the seals, the
   lantern, the rope -- so it looks like the shrine it belongs to.
   ============================================================ */

import {
  h, mount, api, mascot, seal, copyText, applyTheme, readTheme, duration,
} from './core.js';
import { startPetals } from './petals.js';

const STATE = {
  online: 'running', starting: 'starting', offline: 'stopped',
  crashed: 'crashed', unknown: 'stopped',
};
const WORD = {
  online: 'Online', starting: 'Starting', offline: 'Offline', crashed: 'Down',
  unknown: 'No reading',
};

const main = document.getElementById('status');
let timer = null;

async function load() {
  let view;
  try {
    view = await api.get('/api/status');
  } catch (e) {
    mount(main, h('div.sp-wrap', h('div.empty',
      mascot(e.status === 404 ? 'sneakwalk' : 'hurt', 120),
      h('h3', e.status === 404 ? 'There is no status page here' : 'The status could not be read'),
      h('p', e.status === 404 ? 'The people who run these servers have not switched it on.'
        : 'Try again in a moment.'))));
    return;
  }
  document.title = view.title || 'Server status';
  const up = view.online;
  mount(main, h('div.sp-wrap',
    h('header.stp-head',
      h('div',
        h('p.eyebrow', 'Server status · ', h('b', `${up} of ${view.servers.length} online`)),
        h('h1.brush.sp-title', view.title || 'Server status'),
        view.blurb ? h('p.lede', view.blurb) : null,
        h('p.sp-total', h('b.tnum', String(view.players)),
          ` player${view.players === 1 ? '' : 's'} on right now`)),
      mascot(view.players ? 'wave' : 'idle', 104, { cls: 'sp-fox' })),
    h('section.ropegroup', { 'aria-label': 'Servers' },
      h('div.sh-rope', { 'aria-hidden': 'true' },
        h('span.shide'), h('span.shide'), h('span.shide')),
      view.servers.length
        ? h('div.plaques', ...view.servers.map(plaque))
        : h('p.note', 'No servers are shown yet.')),
    h('footer.sp-foot', 'Read ',
      h('time', new Date(view.updated * 1000).toLocaleTimeString()),
      ' · refreshes by itself every 30 seconds · run with BlessForge')));
}

function plaque(s, i) {
  const key = STATE[s.state] || 'stopped';
  const up = s.state === 'online' || s.state === 'starting';
  const el = h(`article.ema-card.sp-card.st-${key}`,
    h('div.ema-face', h('div.ema-in',
      h('div.ema-top',
        h(`span.ema-lamp${up ? '.lit' : ''}`, { 'aria-hidden': 'true' }),
        h('div.ema-id',
          h('h2.brush', s.name),
          h('div.ema-meta', h('span', [s.loader, s.minecraft].filter(Boolean).join(' ') || 'Minecraft'))),
        seal({ ...sealFor(key), label: WORD[s.state] || 'Unknown' }, { size: 'sm' })),
      s.pack ? h('div.ema-pack', s.pack) : null,
      h('div.sp-players',
        h('b.tnum', up ? String(s.players) : '—'),
        h('span', up ? ` of ${s.max_players ?? '—'} on` : ` ${WORD[s.state] || 'offline'}`),
        s.uptime_s ? h('small.muted', ` · up ${duration(s.uptime_s)}`) : null),
      s.names?.length ? h('div.chiprow', ...s.names.map((n) => h('span.sp-name', n))) : null,
      s.address ? h('button.th-join', { type: 'button', title: 'Copy the address',
        onclick: () => copyText(s.address, 'The address') },
      h('span.mono', s.address), h('small', 'copy')) : null)));
  el.style.setProperty('--sway-delay', `-${((i * 1.37) % 5).toFixed(2)}s`);
  return el;
}

function sealFor(key) {
  return {
    running: { glyph: '稼', tone: 'ok' }, starting: { glyph: '起', tone: 'warn' },
    stopped: { glyph: '休', tone: 'dead' }, crashed: { glyph: '倒', tone: 'bad' },
  }[key] || { glyph: '休', tone: 'dead' };
}

function start() {
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) load(); }, 30000);
}

applyTheme(readTheme());
setTimeout(() => document.documentElement.classList.add('js-motion'), 0);
startPetals(document.getElementById('petals'));
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
load().then(start);
