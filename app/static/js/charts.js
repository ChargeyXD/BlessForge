/* ============================================================
   charts — a time series, drawn by hand.

   No chart library for three lines. The plot is an SVG stretched
   to its box (preserveAspectRatio none, strokes that do not
   scale), and everything with words in it -- the axis labels,
   the readout -- is HTML laid over it, so text never stretches
   with the plot.

   Three rules from the rest of the app hold here too:
     * A gap is a gap. Minutes with no reading (BlessForge was
       down, the node did not answer) break the line; they are
       not drawn as zero.
     * Unknown is not zero: a point with no value is skipped,
       not plotted at the floor.
     * The readout works from the keyboard: the plot takes focus
       and the arrow keys walk it. A table with every point sits
       behind a disclosure for screen readers and for anyone who
       wants the numbers.
   ============================================================ */

import { h, mount } from './core.js';

const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

export function when(t, spanHours) {
  const d = new Date(t * 1000);
  if (spanHours <= 30) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' })
    + (spanHours <= 24 * 8 ? ` ${d.toLocaleTimeString([], { hour: '2-digit' })}` : '');
}

/* `points` [{t, ...}], `value(p)` the number or null, `fmt(v)` for labels.
   `limit(p)` draws a dashed ceiling (a memory limit). `stepMin` is the
   minutes between points, which is what a gap is measured against. */
export function timeChart({
  title, points, value, fmt = String, max = null, limit = null, stepMin = 1,
  spanHours = 24, tone = 'rose', summary = '',
}) {
  const vals = points.map(value);
  const known = vals.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  const limits = limit ? points.map(limit).filter((v) => v) : [];
  const top = max ?? Math.max(1, ...known, ...limits) * 1.12;
  const t0 = points.length ? points[0].t : 0;
  const t1 = points.length ? points[points.length - 1].t : 1;
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * 1000;
  const y = (v) => 100 - (Math.max(0, v) / top) * 100;
  const gap = stepMin * 60 * 2.5;

  // The line, broken wherever readings are missing or too far apart.
  let line = '';
  let area = '';
  let run = [];
  const flush = () => {
    if (!run.length) return;
    line += run.map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(2)}`).join('');
    if (run.length > 1) {
      area += `M${run[0][0].toFixed(1)},100`
        + run.map(([px, py]) => `L${px.toFixed(1)},${py.toFixed(2)}`).join('')
        + `L${run[run.length - 1][0].toFixed(1)},100Z`;
    }
    run = [];
  };
  points.forEach((p, i) => {
    const v = vals[i];
    if (v === null || v === undefined || Number.isNaN(v)) { flush(); return; }
    if (i && p.t - points[i - 1].t > gap) flush();
    run.push([x(p.t), y(v)]);
  });
  flush();

  const svg = svgEl('svg', { viewBox: '0 0 1000 100', preserveAspectRatio: 'none',
    class: `hc-svg tone-${tone}`, 'aria-hidden': 'true' });
  for (const g of [0, 50, 100]) {
    svg.append(svgEl('line', { x1: 0, x2: 1000, y1: g, y2: g, class: 'hc-grid' }));
  }
  if (area) svg.append(svgEl('path', { d: area, class: 'hc-area' }));
  if (limits.length) {
    const lp = points.map((p, i) => [x(p.t), limit(p)]).filter(([, v]) => v);
    svg.append(svgEl('path', {
      d: lp.map(([px, v], i) => `${i ? 'L' : 'M'}${px.toFixed(1)},${y(v).toFixed(2)}`).join(''),
      class: 'hc-limit' }));
  }
  if (line) svg.append(svgEl('path', { d: line, class: 'hc-line' }));

  const cross = h('div.hc-cross', { hidden: true });
  const tip = h('div.hc-tip', { hidden: true, role: 'status', 'aria-live': 'polite' });
  let at = -1;
  const show = (i) => {
    if (i < 0 || i >= points.length) return;
    at = i;
    const p = points[i];
    const v = vals[i];
    const left = (x(p.t) / 10);
    cross.hidden = false;
    tip.hidden = false;
    cross.style.left = `${left}%`;
    tip.style.left = `${Math.min(82, Math.max(0, left - 9))}%`;
    mount(tip, h('b', v === null || v === undefined ? '—' : fmt(v)),
      h('span', when(p.t, spanHours)),
      limit && limit(p) ? h('small', `of ${fmt(limit(p))}`) : null);
  };
  const hide = () => { cross.hidden = true; tip.hidden = true; };
  const nearest = (clientX, box) => {
    const frac = (clientX - box.left) / Math.max(1, box.width);
    const t = t0 + frac * (t1 - t0);
    let best = 0;
    for (let i = 1; i < points.length; i += 1) {
      if (Math.abs(points[i].t - t) < Math.abs(points[best].t - t)) best = i;
    }
    return best;
  };
  const plot = h('div.hc-plot', {
    tabIndex: points.length ? 0 : -1,
    role: 'img',
    'aria-label': `${title}. ${summary} Use the arrow keys to read individual points.`,
    onpointermove: (e) => show(nearest(e.clientX, e.currentTarget.getBoundingClientRect())),
    onpointerleave: hide,
    onkeydown: (e) => {
      if (e.key === 'ArrowRight') { e.preventDefault(); show(at < 0 ? points.length - 1 : Math.min(points.length - 1, at + 1)); }
      if (e.key === 'ArrowLeft') { e.preventDefault(); show(at < 0 ? points.length - 1 : Math.max(0, at - 1)); }
      if (e.key === 'Escape') hide();
    },
    onblur: hide,
  },
  h('div.hc-y', { 'aria-hidden': 'true' },
    h('span', { style: { top: '0%' } }, fmt(top)),
    h('span', { style: { top: '50%' } }, fmt(top / 2)),
    h('span', { style: { top: '100%' } }, fmt(0))),
  svg, cross, tip);

  const mid = points.length ? points[Math.floor(points.length / 2)].t : 0;
  const table = h('details.hc-data',
    h('summary', 'Show the numbers'),
    h('div.tablewrap', h('table.tbl',
      h('thead', h('tr', h('th', 'When'), h('th', title))),
      h('tbody', ...points.slice(-120).reverse().map((p, i) => {
        const v = value(p);
        return h('tr', h('td', when(p.t, spanHours)),
          h('td.num', v === null || v === undefined ? '—' : fmt(v)));
      })))));

  return h(`figure.hchart.tone-${tone}`,
    h('figcaption', h('b', title), summary ? h('span', summary) : null),
    plot,
    h('div.hc-x', { 'aria-hidden': 'true' },
      h('span', points.length ? when(t0, spanHours) : ''),
      h('span', points.length ? when(mid, spanHours) : ''),
      h('span', points.length ? when(t1, spanHours) : '')),
    table);
}
