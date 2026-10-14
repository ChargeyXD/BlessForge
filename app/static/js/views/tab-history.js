/* ============================================================
   History — how this server has been, not just how it is.

   Players, CPU and memory once a minute for two days, then in
   quarter hours for a month (app/history.py). The window is a
   choice of four; the step between points widens with it so a
   month is still a chart and not a smear.

   The summary tiles are the window's own numbers: uptime counts
   the minutes it was up of the minutes there were readings for,
   so a stretch where BlessForge itself was down does not count
   against the server.
   ============================================================ */

import {
  h, mount, api, bytes, empty, loadingFox, segmented, prefs, mascot,
} from '../core.js';
import { timeChart } from '../charts.js';

const WINDOWS = [
  { value: 6, label: '6 hours' },
  { value: 24, label: 'Day' },
  { value: 168, label: 'Week' },
  { value: 720, label: 'Month' },
];

export async function render(ctx) {
  const node = h('div.history');
  const body = h('div');
  let hours = prefs.get('history.hours', 24);
  let data = null;

  async function load() {
    mount(body, loadingFox('Reading the history', 'fox-crawl'));
    try {
      data = await api.get(`/api/instances/${ctx.id}/history?hours=${hours}`);
    } catch (e) {
      mount(body, h('div.note.bad', e.message));
      return;
    }
    paint();
  }

  function paint() {
    const pts = data.points || [];
    const s = data.summary || {};
    if (!pts.length) {
      mount(body, empty('idle', 'No history yet',
        'A reading is taken once a minute while BlessForge is running, for every '
        + 'server it can see. Come back in a few minutes and the first line is here.'));
      return;
    }
    const maxPlayers = Math.max(1, ...pts.map((p) => p.max_players || 0));
    mount(body,
      h('div.kpis.stagger',
        tile('Uptime', s.uptime === null || s.uptime === undefined ? '—' : `${s.uptime}%`,
          'of the minutes there were readings for', s.uptime >= 99 ? 'ok' : s.uptime < 80 ? 'warn' : ''),
        tile('Peak players', String(s.peak_online ?? 0), `in the last ${label(hours)}`),
        tile('Average CPU', s.avg_cpu === null || s.avg_cpu === undefined ? '—' : `${s.avg_cpu}%`,
          s.peak_cpu ? `peak ${Math.round(s.peak_cpu)}%` : 'while it was up'),
        tile('Average memory', s.avg_mem ? bytes(s.avg_mem) : '—',
          s.peak_mem ? `peak ${bytes(s.peak_mem)}` : 'while it was up')),
      h('div.hcharts',
        timeChart({
          title: 'Players online', points: pts, value: (p) => p.online,
          fmt: (v) => String(Math.round(v)), stepMin: data.step, spanHours: hours,
          max: Math.max(maxPlayers, ...pts.map((p) => p.online || 0)), tone: 'rose',
          summary: `Peak ${s.peak_online ?? 0}.`,
        }),
        timeChart({
          title: 'CPU', points: pts, value: (p) => (p.up ? p.cpu : null),
          fmt: (v) => `${Math.round(v)}%`, max: 100, stepMin: data.step, spanHours: hours,
          tone: 'gold', summary: s.avg_cpu === null || s.avg_cpu === undefined ? ''
            : `Average ${s.avg_cpu}% of the cores it may use.`,
        }),
        timeChart({
          title: 'Memory', points: pts, value: (p) => (p.up ? p.mem : null),
          fmt: (v) => bytes(v), limit: (p) => p.mem_limit, stepMin: data.step,
          spanHours: hours, tone: 'plum',
          summary: 'The dashed line is the most this server may use.',
        })),
      h('p.muted.hnote', mascot('nod', 28),
        `One point every ${data.step} minute${data.step === 1 ? '' : 's'}. Gaps are `
        + 'minutes with no reading -- BlessForge or the node was not answering -- not zeros.'),
    );
  }

  mount(node,
    h('div.listbar',
      h('b.brush', 'History'),
      h('span.muted', 'players, CPU and memory, recorded once a minute'),
      h('div.grow'),
      segmented(WINDOWS, hours, (v) => { hours = v; prefs.set('history.hours', v); load(); },
        'Time window')),
    body);
  await load();
  return { node };
}

function label(hours) {
  return WINDOWS.find((w) => w.value === hours)?.label.toLowerCase() || `${hours} hours`;
}

function tile(lbl, value, sub, tone = '') {
  return h('div.kpi',
    h('div.lbl', lbl),
    h(`div.v${tone ? `.${tone}` : ''}.tnum`, value),
    h('div.sub', sub));
}
