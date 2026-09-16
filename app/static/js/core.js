/* ============================================================
   core — the pieces every screen is built from.

   Everything is real DOM built by h(), never innerHTML from a
   template string. That is a deliberate reversal of the previous
   front end, where markup was assembled as text and bound by id
   afterwards: handlers on static elements were attached once and
   never rebound, so a control that closed over a render-time
   value kept using the first one forever. Building nodes means a
   handler is created with the row it belongs to and cannot go
   stale, and it means no user-supplied string is ever parsed as
   markup.
   ============================================================ */

/* --- DOM ---------------------------------------------------- */

const SVG_NS = 'http://www.w3.org/2000/svg';

export function h(tag, props, ...kids) {
  // h('div.card.hoverable#id') — the shorthand saves a class prop on
  // nearly every call site, which is most of what this file does.
  const m = /^([a-zA-Z][\w-]*)?(.*)$/.exec(tag);
  const name = m[1] || 'div';
  const el = document.createElement(name);
  const extra = m[2];
  if (extra) {
    for (const token of extra.split(/(?=[.#])/)) {
      // Call sites build these with template literals, so an interpolated
      // empty variant leaves `.thin ` — a class with a trailing space, which
      // classList.add rejects outright and takes the whole screen with it.
      // Splitting on whitespace here means one tolerant place instead of a
      // rule every call site has to remember.
      const body = token.slice(1).trim();
      if (!body) continue;
      if (token[0] === '.') el.classList.add(...body.split(/\s+/));
      else if (token[0] === '#') el.id = body.split(/\s+/)[0];
    }
  }
  if (props && (typeof props !== 'object' || props.nodeType || Array.isArray(props))) {
    kids.unshift(props);
    props = null;
  }
  if (props) applyProps(el, props);
  append(el, kids);
  // Anything that carries the wiggling polygon gets its layer here rather
  // than at the call site. Forgetting it is invisible until someone hovers,
  // which is the worst kind of bug to leave to discipline -- and the layer
  // must be the FIRST child, because the card paints its own fill at
  // z-index 1 and the blob at 0.
  if ((el.classList.contains('card') || el.classList.contains('btn'))
      && !el.querySelector(':scope > .p5-hl')) {
    el.prepend(hl());
  }
  return el;
}

function applyProps(el, props) {
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class' || key === 'className') {
      const extra = String(value).trim();
      if (!extra) continue;
      el.className = el.className ? `${el.className} ${extra}` : extra;
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(el.style, value);
    } else if (key === 'dataset') {
      Object.assign(el.dataset, value);
    } else if (key === 'html') {
      el.innerHTML = value;                 // only ever our own SVG strings
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in el && key !== 'list' && typeof value !== 'object'
               && !key.startsWith('aria')) {
      try { el[key] = value; } catch { el.setAttribute(key, value); }
    } else {
      el.setAttribute(key, value === true ? '' : String(value));
    }
  }
}

function append(el, kids) {
  for (const kid of kids.flat(6)) {
    if (kid === null || kid === undefined || kid === false || kid === true) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
}

export function frag(...kids) {
  const f = document.createDocumentFragment();
  append(f, kids);
  return f;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount(el, ...kids) {
  /* Focus survives the rebuild, when the focused thing was inside what is
     being rebuilt. See the note on `mountKeepingFocus` below -- this used
     to be an opt-in helper, and making it the default is the honest call:
     every filter bar in the app repaints on `input`, so every one of them
     needed it, and the next one somebody writes will too.

     The guard is narrow on purpose. Focus is only restored when the
     active element was a descendant of THIS container, so a repaint of
     one panel can never steal the caret out of another, and a caller that
     wants focus somewhere else still wins by moving it afterwards. */
  const active = document.activeElement;
  if (!active || active === el || !el.contains(active)) {
    clear(el);
    append(el, kids);
    return el;
  }
  return mountKeepingFocus(el, ...kids);
}

/* A repaint that does not throw the caret out of the box you are typing in.
   ------------------------------------------------------------
   Every filter bar in this app repaints on `input`, because the thing
   beside the box genuinely changes as you type -- "All 245" becomes "All
   3", a segment appears, a count moves. `mount` is clear-then-append, so
   the <input> is destroyed and rebuilt with it, and removing a focused
   element blurs it. The value survives (it is re-rendered from state) but
   the FOCUS does not: you type one character, the caret leaves, and the
   next character goes nowhere. Reported, accurately, as the search bar
   cancelling itself on every keystroke.

   Restoring focus is not enough on its own -- putting the caret back at
   the end would make it impossible to edit the middle of a query, which
   is a subtler version of the same bug -- so the selection range comes
   back too.

   Give the input a stable `data-keep` and it is found again after the
   rebuild. Without one, an input that happens to be reused by identity
   still gets its focus back, which covers the simple cases for free. */
export function mountKeepingFocus(el, ...kids) {
  const active = document.activeElement;
  const inside = !!active && el.contains(active) && active !== el;
  const key = inside ? active.getAttribute('data-keep') : null;
  let range = null;
  if (inside) {
    try { range = [active.selectionStart, active.selectionEnd, active.selectionDirection]; }
    catch { range = null; }          // number/email inputs throw on selectionStart
  }

  clear(el);
  append(el, kids);

  if (!inside) return el;
  const next = active.isConnected ? active
    : (key ? el.querySelector(`[data-keep="${CSS.escape(key)}"]`) : null);
  if (!next) return el;
  next.focus({ preventScroll: true });
  if (range && typeof next.setSelectionRange === 'function') {
    try { next.setSelectionRange(range[0], range[1], range[2] || 'none'); }
    catch { /* not a text input any more; focus alone is the win */ }
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* The wiggling-polygon highlight, as a child. It must be the FIRST child of
   a host that isolates its own stacking context, so the layer order is
   blob (0) / fill (1) / content (2) — see THE WIGGLING POLYGON in theme.css.

   Every blob gets its own motion. One shared keyframe set made the whole
   fleet screen wiggle in lockstep, which reads as one animation applied
   many times rather than a page where things are alive. Six shapes, a
   continuous duration, a random starting phase and a random direction give
   roughly 6 x 4 x infinity distinct motions, so no two blobs on screen
   match and none has a loop you can see.

   Stamped here rather than with :nth-child because nth-child is positional:
   the third card in every grid would share its motion, and a re-render
   would deal every card a different one. Stamped at creation, it is the
   element's own for as long as it lives.

   The animation itself only exists while the host is hovered, so an idle
   screen of forty cards runs nothing at all. */
const P5_SHAPES = 6;
const P5_INNER = 4;
const rand = (lo, hi) => lo + Math.random() * (hi - lo);

export function hl() {
  const span = document.createElement('span');
  span.className = 'p5-hl';
  span.setAttribute('aria-hidden', 'true');
  const s = span.style;
  s.setProperty('--p5-a', `p5-w${1 + Math.floor(Math.random() * P5_SHAPES)}`);
  s.setProperty('--p5-b', `p5-i${1 + Math.floor(Math.random() * P5_INNER)}`);
  s.setProperty('--p5-dur', `${rand(0.52, 1.05).toFixed(2)}s`);
  s.setProperty('--p5-dur-b', `${rand(0.7, 1.4).toFixed(2)}s`);
  // A negative delay starts the animation part-way through its cycle, so
  // two blobs with the same duration still do not move together.
  s.setProperty('--p5-delay', `-${rand(0, 1).toFixed(2)}s`);
  s.setProperty('--p5-delay-b', `-${rand(0, 1.4).toFixed(2)}s`);
  s.setProperty('--p5-dir', Math.random() < 0.5 ? 'normal' : 'reverse');
  return span;
}

/* --- icons --------------------------------------------------
   One family, one stroke width, drawn inline so a LAN box with no
   route to the internet still paints. Decorative by default: a
   button that carries only an icon must pass its own aria-label. */

const PATHS = {
  server: 'M3 5.5h18v5H3zM3 13.5h18v5H3zM6.5 8h.01M6.5 16h.01',
  play: 'M6 4l14 8-14 8z',
  stop: 'M6 6h12v12H6z',
  restart: 'M20 11a8 8 0 1 0-2.3 6M20 5v6h-6',
  power: 'M12 3v9M6.5 6.5a8 8 0 1 0 11 0',
  box: 'M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8',
  folder: 'M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2.5h7A1.5 1.5 0 0 1 19 9v8.5a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 17.5z',
  file: 'M6 3h8l4 4v14H6zM14 3v4h4',
  fileText: 'M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6',
  archive: 'M3 5h18v4H3zM5 9v10h14V9M10 13h4',
  image: 'M3 5h18v14H3zM3 15l5-4 4 3 3-3 6 5M8.5 9.5h.01',
  users: 'M16 19v-1.5a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4V19M9.5 9.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM21 19v-1.5a4 4 0 0 0-3-3.9M16 3.6a4 4 0 0 1 0 7.8',
  terminal: 'M5 5h14v14H5zM8.5 9.5l2.5 2.5-2.5 2.5M13 15h3',
  sliders: 'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4',
  wrench: 'M15.5 4.5a4.5 4.5 0 0 0-5.9 5.9L4 16v4h4l5.6-5.6a4.5 4.5 0 0 0 5.9-5.9L17 11l-3-3z',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4',
  dice: 'M4 4h16v16H4zM9 9h.01M15 9h.01M9 15h.01M15 15h.01M12 12h.01',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  gear: 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7.5 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3 14a2 2 0 1 1 0-4 1.6 1.6 0 0 0 1.6-1.1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 10 3.6V3a2 2 0 1 1 4 0v.1A1.6 1.6 0 0 0 16.5 4.6l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.6 1.6 0 0 0 20.4 10H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  x: 'M6 6l12 12M18 6L6 18',
  check: 'M4.5 12.5l5 5 10-11',
  trash: 'M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M10 11v6M14 11v6',
  download: 'M12 3v12M7 11l5 5 5-5M4 20h16',
  upload: 'M12 21V9M7 13l5-5 5 5M4 4h16',
  refresh: 'M20 11a8 8 0 1 0-2.3 6M20 5v6h-6',
  edit: 'M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z',
  eye: 'M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6-10-6-10-6zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  alert: 'M12 3l9 17H3zM12 10v4M12 17h.01',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01',
  shield: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z',
  shieldCheck: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM8.5 12l2.5 2.5 4.5-5',
  chevronRight: 'M9 5l7 7-7 7',
  chevronDown: 'M5 9l7 7 7-7',
  chevronLeft: 'M15 5l-7 7 7 7',
  arrowLeft: 'M20 12H4M10 6l-6 6 6 6',
  menu: 'M4 7h16M4 12h16M4 17h16',
  sun: 'M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM12 2v2M12 20v2M4.2 4.2l1.5 1.5M18.3 18.3l1.5 1.5M2 12h2M20 12h2M4.2 19.8l1.5-1.5M18.3 5.7l1.5-1.5',
  moon: 'M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z',
  cpu: 'M7 7h10v10H7zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7.5V12l3 2',
  save: 'M5 4h11l3 3v13H5zM8 4v5h7V4M8 20v-6h8v6',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  link: 'M10 13a4 4 0 0 0 6 .5l2-2a4 4 0 0 0-5.7-5.7l-1.2 1.2M14 11a4 4 0 0 0-6-.5l-2 2A4 4 0 0 0 11.7 18l1.2-1.2',
  filter: 'M3 5h18l-7 8v6l-4 2v-8z',
  star: 'M12 3.5l2.6 5.6 6 .8-4.4 4.2 1.1 6-5.3-3-5.3 3 1.1-6L3.4 9.9l6-.8z',
  ban: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM5.6 5.6l12.8 12.8',
  crown: 'M4 18h16M4 18l-1-9 5 3 4-7 4 7 5-3-1 9',
  bolt: 'M13 3L5 14h6l-1 7 8-11h-6z',
  puzzle: 'M10 4h4v2.5a1.5 1.5 0 1 0 3 0V4h3v4h-2.5a1.5 1.5 0 1 0 0 3H20v5h-3v-2.5a1.5 1.5 0 1 0-3 0V16h-4v-3h2.5a1.5 1.5 0 1 0 0-3H10z',
  layers: 'M12 3l9 5-9 5-9-5zM3 13l9 5 9-5M3 17l9 5 9-5',
  db: 'M12 7c5 0 8-1.3 8-2s-3-2-8-2-8 1.3-8 2 3 2 8 2zM4 5v14c0 .7 3 2 8 2s8-1.3 8-2V5M4 12c0 .7 3 2 8 2s8-1.3 8-2',
  heart: 'M12 20s-7-4.5-7-9.5A4 4 0 0 1 12 8a4 4 0 0 1 7-.5C19 15.5 12 20 12 20z',
  compass: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM15.5 8.5l-2 5-5 2 2-5z',
  flask: 'M9 3h6M10 3v6L5 19a1.5 1.5 0 0 0 1.3 2h11.4A1.5 1.5 0 0 0 19 19l-5-10V3M7.5 14h9',
};

export function icon(name, size = 16, extra = {}) {
  const el = document.createElementNS(SVG_NS, 'svg');
  el.setAttribute('viewBox', '0 0 24 24');
  el.setAttribute('width', size);
  el.setAttribute('height', size);
  el.setAttribute('fill', 'none');
  el.setAttribute('stroke', 'currentColor');
  el.setAttribute('stroke-width', extra.weight || '1.8');
  el.setAttribute('stroke-linecap', 'round');
  el.setAttribute('stroke-linejoin', 'round');
  // Decorative unless a caller says otherwise: the label lives on the
  // control, and a duplicate reading of it in the accessibility tree is
  // noise for anyone using a screen reader.
  el.setAttribute('aria-hidden', 'true');
  el.setAttribute('focusable', 'false');
  const p = document.createElementNS(SVG_NS, 'path');
  p.setAttribute('d', PATHS[name] || PATHS.info);
  el.append(p);
  if (extra.class) el.setAttribute('class', extra.class);
  return el;
}

/* --- formatting --------------------------------------------- */

export function bytes(n) {
  if (!n && n !== 0) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n;
  for (const u of units) {
    v /= 1024;
    if (v < 1024 || u === 'TB') {
      return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u}`;
    }
  }
  return `${n}`;
}

export function num(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return Number(n).toLocaleString();
}

export function compact(n) {
  if (!n && n !== 0) return '—';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return String(n);
}

export function duration(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

export function ago(epochOrIso) {
  if (!epochOrIso) return '—';
  const t = typeof epochOrIso === 'number'
    ? epochOrIso * (epochOrIso > 1e12 ? 1 : 1000)
    : Date.parse(epochOrIso);
  if (Number.isNaN(t)) return String(epochOrIso);
  const diff = (Date.now() - t) / 1000;
  if (diff < 45) return 'just now';
  if (diff < 5400) return `${Math.round(diff / 60)} min ago`;
  if (diff < 172800) return `${Math.round(diff / 3600)} h ago`;
  return `${Math.round(diff / 86400)} d ago`;
}

export const titleCase = (s) =>
  String(s || '').replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

/* --- the API ------------------------------------------------
   One place that knows how the server reports a failure, so every
   screen shows the same sentence rather than "[object Object]". */

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(method, path, { body, form, signal, raw } = {}) {
  const opts = { method, signal, headers: {} };
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError(
      'Could not reach BlessForge. The container may be restarting.', 0, null);
  }
  if (raw) {
    if (!res.ok) throw new ApiError(`${res.status} ${res.statusText}`, res.status);
    return res;
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }
  if (!res.ok) {
    const detail = data && (data.detail || data.error || data.message);
    throw new ApiError(
      typeof detail === 'string' ? detail : `${res.status} ${res.statusText}`,
      res.status, data);
  }
  return data;
}

export const api = {
  get: (p, o) => request('GET', p, o),
  post: (p, body, o) => request('POST', p, { ...o, body: body ?? {} }),
  put: (p, body, o) => request('PUT', p, { ...o, body: body ?? {} }),
  del: (p, o) => request('DELETE', p, o),
  upload: (p, form, o) => request('POST', p, { ...o, form }),
  raw: (p, o) => request('GET', p, { ...o, raw: true }),
};

export function qs(params) {
  const out = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === '') continue;
    out.set(k, String(v));
  }
  const s = out.toString();
  return s ? `?${s}` : '';
}

/* --- toasts -------------------------------------------------- */

let toastHost = null;

export function toast(message, kind = 'ok', { title, timeout } = {}) {
  if (!toastHost) {
    toastHost = h('div#toasts', { 'aria-live': 'polite', 'aria-atomic': 'false' });
    document.body.append(toastHost);
  }
  const node = h(`div.toast.${kind}`, { role: kind === 'bad' ? 'alert' : 'status' },
    icon(kind === 'ok' ? 'check' : kind === 'bad' ? 'alert' : 'info', 18),
    h('div.msg', title && h('b', title), message),
    h('button.btn.icon.xs.ghost', {
      'aria-label': 'Dismiss', onclick: () => dismiss(node),
    }, icon('x', 13)),
  );
  toastHost.append(node);
  const ms = timeout ?? (kind === 'bad' ? 9000 : 4500);
  const timer = setTimeout(() => dismiss(node), ms);
  node.addEventListener('mouseenter', () => clearTimeout(timer));
  return node;
}

function dismiss(node) {
  if (!node.isConnected) return;
  node.classList.add('leaving');
  setTimeout(() => node.remove(), 200);
}

export const toastError = (e, title) =>
  toast(e instanceof Error ? e.message : String(e), 'bad', { title });

/* --- modals --------------------------------------------------
   One host, one open dialog. Focus moves into the dialog, Escape
   and the scrim both close it, and focus returns to whatever
   opened it — a modal you cannot get out of with the keyboard is
   the most common way an interface traps someone. */

let modalHost = null;
let openModal = null;

export function modal({ title, body, footer, wide, onClose, dismissable = true }) {
  if (!modalHost) {
    modalHost = h('div#modalhost');
    document.body.append(modalHost);
  }
  close();

  const opener = document.activeElement;
  const panel = h(`div.modal${wide ? '.wide' : ''}`, {
    role: 'dialog', 'aria-modal': 'true', 'aria-label': title || 'Dialog',
  },
    h('header',
      h('h3', title || ''),
      dismissable && h('button.btn.icon.sm.ghost', {
        'aria-label': 'Close', onclick: () => close(),
      }, icon('x', 15)),
    ),
    h('div.body', body),
    footer && h('footer', footer),
  );

  mount(modalHost, dismissable
    ? h('div.scrim', { onclick: () => close() })
    : h('div.scrim'), panel);
  modalHost.classList.add('on');

  const onKey = (e) => {
    if (e.key === 'Escape' && dismissable) { e.preventDefault(); close(); }
    if (e.key === 'Tab') trapFocus(e, panel);
  };
  document.addEventListener('keydown', onKey);

  openModal = {
    panel,
    close() {
      document.removeEventListener('keydown', onKey);
      modalHost.classList.remove('on');
      clear(modalHost);
      openModal = null;
      onClose?.();
      if (opener && opener.isConnected) opener.focus();
    },
  };

  requestAnimationFrame(() => {
    const first = panel.querySelector(
      'input:not([type=hidden]),select,textarea,button.primary,button');
    first?.focus();
  });
  return openModal;
}

export function close() {
  openModal?.close();
}

function trapFocus(e, panel) {
  const items = $$('a[href],button:not([disabled]),input:not([type=hidden]),'
    + 'select,textarea,[tabindex]:not([tabindex="-1"])', panel)
    .filter((n) => n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
}

export function confirmDialog({
  title, message, confirmLabel = 'Confirm', danger, detail, requireText, art,
}) {
  return new Promise((resolve) => {
    // `close()` runs onClose, which resolves false — so the answer has to be
    // decided BEFORE the dialog is torn down. Getting this backwards made
    // every confirmation in the app resolve false and silently do nothing,
    // which reads as a dead button rather than as a bug.
    let settled = false;
    let input = null;
    const answer = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const go = () => {
      if (requireText && input.value.trim() !== requireText) return;
      answer(true);
      close();
    };
    if (requireText) {
      input = h('input.inp', {
        placeholder: requireText, autocomplete: 'off',
        'aria-label': `Type ${requireText} to confirm`,
        oninput: () => { ok.disabled = input.value.trim() !== requireText; },
        onkeydown: (e) => { if (e.key === 'Enter') go(); },
      });
    }
    const ok = h(`button.btn.${danger ? 'danger' : 'primary'}`,
      { onclick: go, disabled: !!requireText }, hl(), h('span', confirmLabel));

    modal({
      title: title || 'Are you sure?',
      body: frag(
        // A picture on a destructive dialog is not decoration: it is half a
        // second of "wait, what am I about to do" before the muscle memory
        // reaches the confirm button.
        art ? h('div', { style: { textAlign: 'center', marginBottom: '10px' } },
          h('img', { src: `/assets/${art}`, alt: '',
            style: { width: 'min(190px,46vw)' } })) : null,
        h('p', message),
        detail && h('div.note.warn', { style: { marginTop: '12px' } }, detail),
        requireText && h('div.field', { style: { marginTop: '14px' } },
          h('label', `Type ${requireText} to confirm`), input),
      ),
      footer: frag(
        h('button.btn.ghost', { onclick: () => { answer(false); close(); } },
          h('span', 'Cancel')),
        ok,
      ),
      onClose: () => answer(false),
    });
  });
}

export function promptDialog({ title, label, value = '', placeholder, help,
                               confirmLabel = 'Save' }) {
  return new Promise((resolve) => {
    // Same ordering trap as confirmDialog: decide, then close.
    let settled = false;
    const answer = (v) => { if (!settled) { settled = true; resolve(v); } };
    const input = h('input.inp', { value, placeholder, autocomplete: 'off' });
    const go = () => {
      const v = input.value.trim();
      if (!v) { input.focus(); return; }
      answer(v);
      close();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    modal({
      title,
      body: h('div.field', h('label', label), input,
        help && h('div.help', help)),
      footer: frag(
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', { onclick: go }, hl(), h('span', confirmLabel)),
      ),
      onClose: () => answer(null),
    });
  });
}

/* --- small shared widgets ------------------------------------ */

export function empty(art, title, message, ...actions) {
  return h('div.empty.anim-rise',
    h('div', { style: { position: 'relative' } },
      h('div.fox-halo'),
      h('img.fox', { src: `/assets/${art}`, alt: '' })),
    h('h3', title),
    h('p', message),
    actions.length ? h('div.btnrow', { style: { justifyContent: 'center' } },
      ...actions) : null,
  );
}

export function loadingFox(label = 'Working') {
  return h('div.loading-fox',
    h('img', { src: '/assets/fox-loading.gif', alt: '' }),
    h('span', label));
}

/* The shape of a screen, before its data exists.
   ------------------------------------------------------------
   Navigating used to mean: blank the page, show a spinner, wait
   for a dynamic import AND a network round trip, then drop the
   entire finished screen in at once. Two jarring moments per
   navigation, and the second one moves everything.

   A skeleton removes both. It paints SYNCHRONOUSLY -- before the
   view module has even been fetched -- so the frame after a click
   already has the right layout in it, and when the real content
   arrives it replaces boxes that are already the right size and
   in the right place. Nothing jumps, because nothing moves.

   The spec is deliberately tiny. A skeleton that tries to be an
   exact copy of a screen is a second implementation of that
   screen, and it rots the moment someone edits the real one.
   Matching the SHAPE -- a header, four tiles, a grid of cards --
   is all that is needed for the swap to read as continuous. */
const SKEL_PARTS = {
  head: () => h('div.sk-head',
    h('div.skel', { style: { width: '180px', height: '11px' } }),
    h('div.skel', { style: { width: 'min(340px,62%)', height: '34px' } })),

  bar: () => h('div.sk-bar',
    ...Array.from({ length: 4 }, (_, i) => h('div.skel', {
      style: { width: `${[68, 54, 62, 88][i]}px`, height: '30px' },
    }))),

  kpis: (n) => h('div.kpis', ...Array.from({ length: n }, () =>
    h('div.sk-kpi',
      h('div.skel', { style: { width: '58%', height: '9px' } }),
      h('div.skel', { style: { width: '42%', height: '24px' } }),
      h('div.skel', { style: { width: '70%', height: '10px' } })))),

  cards: (n) => h('div.grid.gauto', ...Array.from({ length: n }, () =>
    h('div.skelcard',
      h('div.skel', { style: { width: '55%', height: '18px' } }),
      h('div.skel', { style: { width: '82%' } }),
      h('div.skel', { style: { width: '40%' } }),
      h('div.skel', { style: { width: '68%', marginTop: 'auto' } })))),

  rows: (n) => h('div.sk-rows', ...Array.from({ length: n }, (_, i) =>
    h('div.sk-row',
      h('div.skel', { style: { width: '26px', height: '26px', flex: 'none' } }),
      h('div.skel', { style: { width: `${38 + ((i * 13) % 44)}%` } }),
      h('div.skel', { style: { width: '64px', marginLeft: 'auto' } })))),

  panels: (n) => h('div.sk-panels', ...Array.from({ length: n }, () =>
    h('div.sk-panel',
      h('div.sk-panelhd', h('div.skel', { style: { width: '140px', height: '11px' } })),
      h('div.sk-panelbody',
        h('div.skel', { style: { width: '72%' } }),
        h('div.skel', { style: { width: '48%' } }),
        h('div.skel', { style: { width: '86%' } }))))),

  instanceHead: () => h('div.sk-inst',
    h('div.sk-insthd',
      h('div.skel', { style: { width: '52px', height: '52px', flex: 'none' } }),
      h('div.sk-insttitle',
        h('div.skel', { style: { width: 'min(280px,54%)', height: '26px' } }),
        h('div.skel', { style: { width: 'min(200px,38%)', height: '12px' } }))),
    h('div.sk-tabs', ...Array.from({ length: 9 }, (_, i) =>
      h('div.skel', { style: { width: `${[74, 62, 58, 70, 76, 68, 56, 82, 60][i]}px`,
        height: '20px' } })))),

  racks: (n) => h('div.sk-racks', ...Array.from({ length: n }, () =>
    h('div.sk-rack',
      h('div.sk-lintel'),
      h('div.sk-plaques', ...Array.from({ length: 3 }, () =>
        h('div.sk-plaque')))))),
};

export function pageSkeleton(spec = {}) {
  const wrap = h(`div.wrap${spec.wide ? '.wide' : ''}.sk`);
  if (spec.head) wrap.append(SKEL_PARTS.head());
  if (spec.instanceHead) wrap.append(SKEL_PARTS.instanceHead());
  if (spec.bar) wrap.append(SKEL_PARTS.bar());
  if (spec.kpis) wrap.append(SKEL_PARTS.kpis(spec.kpis));
  if (spec.panels) wrap.append(SKEL_PARTS.panels(spec.panels));
  if (spec.racks) wrap.append(SKEL_PARTS.racks(spec.racks));
  if (spec.cards) wrap.append(SKEL_PARTS.cards(spec.cards));
  if (spec.rows) wrap.append(SKEL_PARTS.rows(spec.rows));
  if (spec.split) {
    wrap.append(h('div.sk-split',
      h('div', SKEL_PARTS.cards(spec.split[0] || 4)),
      h('div', SKEL_PARTS.panels(1))));
  }
  return wrap;
}

/* A skeleton that flashes past is worse than none: the eye catches the
   change and reads it as a glitch. Once one is on screen it stays for at
   least this long, so a fast screen and a slow one arrive with the same
   rhythm instead of one of them stuttering. */
export const SKEL_MIN_MS = 140;

export function heldFor(startedAt, minimum = SKEL_MIN_MS) {
  const left = minimum - (Date.now() - startedAt);
  return left > 0 ? new Promise((r) => setTimeout(r, left)) : Promise.resolve();
}

/* Put the real content in, under the skeleton, and dissolve the skeleton off
   it.
   ------------------------------------------------------------
   Replacing one with the other in a single frame is a hard cut: the layout
   is right, but every box changes from grey to text at once and the eye
   reads the change rather than the content. Laying the skeleton over the
   top and fading it away turns that cut into a dissolve, and because the
   two have the same shape, the content appears to develop in place.

   It fades the SKELETON OUT rather than fading the content IN, and that is
   the whole safety argument. Fading content in means starting it at
   opacity 0, and a document timeline that never advances would leave the
   page permanently blank -- the failure this codebase has hit twice. Here a
   stalled timeline leaves a skeleton sitting on top instead, which is why
   the removal is ALSO on a timer: whichever of the two fires first wins,
   so the skeleton is gone within 400ms no matter what the compositor does. */
export function dissolve(host, next) {
  const old = host.firstElementChild;
  if (!old || !old.classList.contains('sk')) {
    mount(host, next);
    return;
  }
  const h0 = host.getBoundingClientRect().height;
  host.prepend(next);                    // content first: it defines the size
  old.classList.add('sk-out');
  // Hold the old height for one beat so the page cannot jolt while the
  // skeleton is still on top of a taller or shorter screen.
  if (h0 > 0) {
    host.style.minHeight = `${Math.round(h0)}px`;
    setTimeout(() => { host.style.minHeight = ''; }, 260);
  }
  let done = false;
  const drop = () => {
    if (done) return;
    done = true;
    old.remove();
  };
  old.addEventListener('transitionend', drop, { once: true });
  setTimeout(drop, 400);
}

/* A catalogue that keeps going.
   ------------------------------------------------------------
   Both catalogue screens paged with Prev/Next, which is the wrong
   shape for browsing: you cannot compare page 1 with page 3, the
   position resets on every step, and "page 4 of ?" is a question
   the CurseForge API cannot answer anyway -- it reports neither a
   total nor a last page, so a Next button is always enabled and
   the only way to discover the end is to walk off it.

   This appends instead. Four things it has to get right, each of
   which is a real failure rather than a nicety:

     * A NEW SEARCH MUST CANCEL AN IN-FLIGHT PAGE. Type "create",
       then "cre" -- without a token the slower first request
       lands after the second and the grid fills with results for
       a query nobody asked for.
     * DUPLICATES. CurseForge pages by offset over a set it
       re-ranks per request, so the same project genuinely does
       arrive twice. Two cards with the same key is a React-style
       bug in plain DOM: the second click acts on the first.
     * THE END. A page shorter than `size` is the last one. Say so
       once and stop observing; an observer left watching a
       sentinel at the bottom of a finished list re-fires on every
       resize.
     * CLEANUP. The observer outlives the screen unless something
       disconnects it, and a route change does not.

   The manual button is not a fallback nobody sees: it is the
   keyboard path, and it is what works when the sentinel never
   intersects because the pane is hidden or the list is shorter
   than the viewport. */
export function endlessFeed(opts) {
  const {
    container, size = 24, fetchPage, itemsOf = (d) => d.items || d.data || [],
    keyOf = (m) => m.id ?? m.project_id ?? m.slug ?? JSON.stringify(m),
    render, empty, onFirstPage,
  } = opts;

  const seen = new Set();
  const foot = h('div.feed-foot');
  const sentinel = h('div.feed-sentinel', { 'aria-hidden': 'true' });
  const status = h('div.sr-only', { role: 'status', 'aria-live': 'polite' });

  let index = 0;
  let busy = false;
  let done = false;
  let token = 0;
  let io = null;

  const more = h('button.btn.sm.ghost', {
    onclick: () => load(),
  }, hl(), icon('chevronDown', 14), h('span', 'Load more'));

  function setFoot(...kids) { mount(foot, ...kids); }

  async function load(reset = false) {
    if (busy || (done && !reset)) return;
    if (reset) {
      token += 1; index = 0; done = false; seen.clear();
      clear(container);
    }
    const mine = token;
    busy = true;
    setFoot(spinner(index === 0 ? 'Searching the catalogue' : 'Fetching more'));
    try {
      const data = await fetchPage(index, size);
      if (mine !== token) return;              // a newer search won
      const batch = itemsOf(data) || [];
      const fresh = batch.filter((m) => {
        const k = String(keyOf(m));
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      });
      if (index === 0) {
        onFirstPage?.(data, batch);
        if (!batch.length) {
          setFoot();
          mount(container, empty ? empty() : h('div.note', 'Nothing matches that.'));
          done = true;
          stop();
          return;
        }
      }
      fresh.forEach((m) => container.append(render(m)));
      index += batch.length;
      // A short page is the last page. `fresh` cannot be used for this --
      // a full page of duplicates would look like the end.
      if (batch.length < size) {
        done = true;
        stop();
        setFoot(h('div.feed-end',
          h('span.mono', `${seen.size} shown · that is everything`)));
      } else {
        setFoot(more);
      }
      status.textContent = `${seen.size} results loaded`;
    } catch (e) {
      if (mine !== token) return;
      setFoot(h('div.note.bad', { style: { margin: 0 } },
        e.message || 'That page would not load.',
        h('div.btnrow', { style: { marginTop: '10px' } },
          h('button.btn.sm', { onclick: () => load() },
            hl(), h('span', 'Try again')))));
    } finally {
      if (mine === token) busy = false;
    }
  }

  function stop() {
    io?.disconnect();
    io = null;
  }

  /* The sentinel has to be watched against whatever actually scrolls it.
     The catalogue screen scrolls the viewport, but the add-mod browser is
     inside a modal with its own `overflow-y:auto` -- and a sentinel inside
     an overflow container never intersects the VIEWPORT once it is scrolled
     past, so a viewport-rooted observer silently never fires there. Walking
     up to the nearest scrollable ancestor covers both without the caller
     having to know which it is in. */
  function scrollRoot(el) {
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      const oy = getComputedStyle(n).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && n.scrollHeight > n.clientHeight) {
        return n;
      }
    }
    return null;      // null means the viewport
  }

  function start() {
    stop();
    if ('IntersectionObserver' in window) {
      io = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) load();
      }, { root: scrollRoot(sentinel), rootMargin: '700px 0px' });
      io.observe(sentinel);
    }
    return load(true);
  }

  return {
    nodes: [foot, sentinel, status],
    start,
    reset: () => load(true),
    dispose: stop,
  };
}

export function skeletonGrid(n = 6, cls = 'gauto') {
  return h(`div.grid.${cls}`, ...Array.from({ length: n }, () =>
    h('div.skelcard',
      h('div.skel', { style: { width: '55%', height: '18px' } }),
      h('div.skel', { style: { width: '82%' } }),
      h('div.skel', { style: { width: '40%' } }),
      h('div.skel', { style: { width: '68%', marginTop: 'auto' } }))));
}

export function pill(text, kind = '', title) {
  return h(`span.pill${kind ? `.${kind}` : ''}`, title ? { title } : null, text);
}

export function bar(percent, kind = '') {
  const pct = Math.max(0, Math.min(100, Number(percent) || 0));
  const cls = String(kind).trim().split(/\s+/).filter(Boolean)
    .map((k) => `.${k}`).join('');
  return h(`div.bar${cls}`, {
    role: 'progressbar', 'aria-valuenow': Math.round(pct),
    'aria-valuemin': '0', 'aria-valuemax': '100',
  }, h('i', { style: { width: `${pct}%` } }));
}

export function field(label, control, help, error) {
  return h(`div.field${error ? '.bad' : ''}`,
    h('label', label), control,
    help && h('div.help', help),
    error && h('div.err', { role: 'alert' }, error));
}

export function toggle(label, checked, onchange, { disabled, help } = {}) {
  const input = h('input', {
    type: 'checkbox', checked: !!checked, disabled,
    onchange: (e) => onchange(e.target.checked),
  });
  return h('label.switch', input, h('span.track'),
    h('span', label, help && h('div.help', help)));
}

export function segmented(options, value, onchange, ariaLabel) {
  /* The pressed segment moves on click, here, rather than waiting for the
     caller to re-render the bar it lives in.

     It used to be drawn purely from the `value` passed at build time,
     which is correct only for callers that repaint the whole bar. The
     add-mod browser does not -- its bar is built once inside the modal and
     only the RESULTS repaint -- so switching CurseForge to Modrinth
     fetched the right catalogue and the control stayed visibly on
     CurseForge. The results changed and the switch did not, which reads as
     a switch that does nothing.

     Optimistic, and safe to be: a caller that does repaint renders after
     this and wins, and a caller that rejects the change can pass the old
     value back the same way. */
  const group = h('div.seg', { role: 'group', 'aria-label': ariaLabel || 'Options' },
    ...options.map((o) => h('button', {
      type: 'button',
      'aria-pressed': String(o.value === value),
      onclick: (e) => {
        const me = e.currentTarget;
        for (const b of group.children) {
          b.setAttribute('aria-pressed', String(b === me));
        }
        onchange(o.value);
      },
    }, o.label)));
  return group;
}

export function debounce(fn, ms = 260) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function copyText(text, what = 'Copied') {
  const done = () => toast(`${what} to the clipboard`, 'ok');
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallback(text, done));
  } else fallback(text, done);
}

function fallback(text, done) {
  const ta = h('textarea', { value: text, style: { position: 'fixed', top: '-1000px' } });
  document.body.append(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch { /* nothing to offer */ }
  ta.remove();
}

/* --- theme --------------------------------------------------- */

const THEME_KEY = 'bf.theme';

export function readTheme() {
  try { return localStorage.getItem(THEME_KEY) || 'system'; } catch { return 'system'; }
}

export function applyTheme(mode) {
  const root = document.documentElement;
  // Suppress transitions across the swap. Every colour token changes at once
  // and a `transition: color` on a label would animate it from one palette to
  // the other — which reads as the text briefly becoming unreadable rather
  // than as a tasteful crossfade.
  root.classList.add('theme-switching');
  if (mode === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  // Two frames: one for the new values to be computed, one for them to paint.
  requestAnimationFrame(() => requestAnimationFrame(
    () => root.classList.remove('theme-switching')));
  // A timer as well, because rAF does not fire in a tab that is not being
  // painted and the class must never be left on.
  setTimeout(() => root.classList.remove('theme-switching'), 120);
  try { localStorage.setItem(THEME_KEY, mode); } catch { /* private window */ }
}

/* --- tiny persistent preferences -----------------------------
   Per-viewer conveniences only: a remembered tab, a sort order.
   Nothing here is allowed to matter — every read tolerates a
   browser that refuses storage entirely. */

export const prefs = {
  get(key, fallbackValue) {
    try {
      const raw = localStorage.getItem(`bf.${key}`);
      return raw === null ? fallbackValue : JSON.parse(raw);
    } catch { return fallbackValue; }
  },
  set(key, value) {
    try { localStorage.setItem(`bf.${key}`, JSON.stringify(value)); }
    catch { /* private window, or storage disabled */ }
  },
};

/* ============================================================
   Interaction behaviours
   ------------------------------------------------------------
   Small, opt-in, and all of them no-ops under reduced motion or
   on a touch device — a magnetic button is a pointer affordance
   and on a finger it is just jitter.
   ============================================================ */

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');
const FINE_POINTER = window.matchMedia('(hover: hover) and (pointer: fine)');
const wantsMotion = () => !REDUCED.matches && FINE_POINTER.matches;

/* The label leans toward the cursor while it is over the control. Two or
   three pixels — enough that the button feels like it noticed you, not
   enough to move the hit target out from under the finger. */
export function magnetic(el, strength = 4) {
  if (!wantsMotion()) return el;
  const inner = el.querySelector(':scope > span') || el;
  let raf = null;
  const move = (e) => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      const r = el.getBoundingClientRect();
      const dx = ((e.clientX - r.left) / r.width - 0.5) * strength * 2;
      const dy = ((e.clientY - r.top) / r.height - 0.5) * strength;
      inner.style.transform = `translate(${dx.toFixed(2)}px,${dy.toFixed(2)}px)`;
    });
  };
  const reset = () => {
    if (raf) { cancelAnimationFrame(raf); raf = null; }
    inner.style.transform = '';
  };
  el.addEventListener('pointermove', move);
  el.addEventListener('pointerleave', reset);
  el.addEventListener('blur', reset);
  return el;
}

/* A card tips very slightly toward the cursor. The maximum is deliberately
   under 4 degrees: past that it stops reading as depth and starts reading as
   a gimmick, and text on a tilted plane gets harder to read. */
export function tilt(el, max = 3.2) {
  if (!wantsMotion()) return el;
  let raf = null;
  const move = (e) => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      const r = el.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5;
      const py = (e.clientY - r.top) / r.height - 0.5;
      el.style.transform =
        `perspective(900px) rotateX(${(-py * max).toFixed(2)}deg) `
        + `rotateY(${(px * max).toFixed(2)}deg) translate3d(0,-3px,0)`;
    });
  };
  const reset = () => {
    if (raf) { cancelAnimationFrame(raf); raf = null; }
    el.style.transform = '';
  };
  el.addEventListener('pointermove', move);
  el.addEventListener('pointerleave', reset);
  return el;
}

/* A number that arrives by counting rather than by appearing. Only worth it
   for the handful of figures a person actually reads — a KPI, a total — and
   never for a value inside a list. */
export function countUp(el, to, { decimals = 0, duration = 700, suffix = '' } = {}) {
  const target = Number(to);
  if (!Number.isFinite(target)) { el.textContent = String(to); return el; }
  if (!wantsMotion() || target === 0) {
    el.textContent = target.toFixed(decimals) + suffix;
    return el;
  }
  const from = 0;
  const started = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - started) / duration);
    // easeOutExpo — fast, then settles, which reads as a mechanical counter
    const eased = t === 1 ? 1 : 1 - 2 ** (-10 * t);
    el.textContent = (from + (target - from) * eased).toFixed(decimals) + suffix;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return el;
}

/* Reveal on scroll, shared observer. Cheaper than one per element and it
   unobserves on first reveal, so a long list does not keep a callback alive
   per row for the life of the screen. */
let revealObserver = null;
export function reveal(el, delay = 0) {
  if (REDUCED.matches) return el;
  if (!revealObserver) {
    revealObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add('revealed');
        revealObserver.unobserve(entry.target);
      }
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.06 });
  }
  el.classList.add('to-reveal');
  if (delay) el.style.setProperty('--reveal-delay', `${delay}ms`);
  revealObserver.observe(el);
  return el;
}

/* The press, and what it throws off.
   ------------------------------------------------------------
   A ripple was the wrong gesture. A circle spreading from the
   contact point is the house style of every material-design app
   on earth, and it fires on pointerdown — so it plays while the
   finger is still going down, which reads as the button leaking
   rather than reacting.

   This fires on CLICK, which is the moment the button actually
   does something, and it fires keyboard activations too (a
   keyboard click reports detail 0 and no coordinates, so it
   bursts from the element's centre instead).

   Three flavours, because a button that deletes a world should
   not celebrate:

     default   sakura petals knocked off a branch — they scatter
               in every direction and fall
     primary   gold leaf, the same foil the display numbers use
     danger    the fill coming apart: more particles, smaller,
               drifting UP and shrinking to nothing

   The shards live in a fixed layer on <body>, NOT inside the
   button. Inside, they would inherit the button's overflow, sit
   inside its stacking context, and collide with the
   `.btn > *:not(.p5-hl)` rule that owns z-index 2 there. Outside,
   they are free of all three and the same code works for rail
   links and fleet rows. */

let burstLayer = null;
function layer() {
  if (!burstLayer || !burstLayer.isConnected) {
    burstLayer = document.createElement('div');
    burstLayer.id = 'burstlayer';
    burstLayer.setAttribute('aria-hidden', 'true');
    document.body.append(burstLayer);
  }
  return burstLayer;
}

const PETAL_INK = ['var(--sakura)', 'var(--rose)', 'var(--sakura-deep)', 'var(--rose-hi)'];
const GOLD_INK = ['var(--gold)', '#FFF6D2', 'var(--gold)', 'var(--sakura)'];
const ASH_INK = ['var(--danger)', 'var(--danger)', 'var(--faint)', 'var(--mid)'];

/* Every node gets a timeout as well as an animation callback. `finished`
   only settles while the document timeline is advancing, and a click
   landing just as the tab is hidden would otherwise leave the shard in the
   DOM forever. Same hazard the entrance animations are written around. */
function sweep(node, ms) {
  const go = () => node.remove();
  node.getAnimations?.().forEach((a) => { a.finished.then(go, go); });
  setTimeout(go, ms + 400);
}

function pop(el, x, y) {
  const host = layer();
  const danger = el.classList.contains('danger');
  const rich = el.classList.contains('primary') || el.classList.contains('gold');
  const ink = danger ? ASH_INK : rich ? GOLD_INK : PETAL_INK;
  const count = danger ? 15 : 9;

  const ringSize = danger ? 92 : 66;
  const ring = document.createElement('span');
  ring.className = 'popring';
  ring.style.width = ring.style.height = `${ringSize}px`;
  ring.style.left = `${x - ringSize / 2}px`;
  ring.style.top = `${y - ringSize / 2}px`;
  if (danger) ring.style.borderColor = 'var(--danger)';
  host.append(ring);
  ring.animate([
    { transform: 'scale(.25) rotate(0deg)', opacity: .9 },
    { transform: 'scale(1) rotate(26deg)', opacity: 0 },
  ], { duration: 400, easing: 'cubic-bezier(.22,1,.36,1)' });
  sweep(ring, 400);

  for (let i = 0; i < count; i += 1) {
    const w = rand(danger ? 3 : 5, danger ? 7 : 11);
    const hgt = w * rand(0.6, 1);
    const sh = document.createElement('span');
    sh.className = 'shard';
    sh.style.width = `${w}px`;
    sh.style.height = `${hgt}px`;
    sh.style.left = `${x - w / 2}px`;
    sh.style.top = `${y - hgt / 2}px`;
    sh.style.background = ink[i % ink.length];
    host.append(sh);

    const angle = danger ? (-Math.PI / 2) + rand(-0.95, 0.95) : rand(0, Math.PI * 2);
    const dist = danger ? rand(34, 92) : rand(20, 62);
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist + (danger ? rand(-30, -10) : rand(8, 30));
    const ms = rand(340, 640);
    sh.animate([
      { transform: 'translate(0,0) rotate(0deg) scale(1)', opacity: 1 },
      {
        transform: `translate(${dx.toFixed(1)}px,${dy.toFixed(1)}px) `
          + `rotate(${rand(-280, 280).toFixed(0)}deg) scale(${danger ? 0.15 : 0.5})`,
        opacity: 0,
      },
    ], { duration: ms, easing: 'cubic-bezier(.18,.7,.3,1)' });
    sweep(sh, ms);
  }
}

function installPress() {
  document.addEventListener('click', (e) => {
    if (REDUCED.matches) return;
    const el = e.target.closest?.('.btn, .navlink, .fleetrow, .tabs button');
    if (!el) return;
    if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') return;
    // detail 0 means the activation did not come from a pointer — Enter or
    // Space on a focused button. There is no click point, so use the middle.
    const r = el.getBoundingClientRect();
    const fromPointer = e.detail > 0 && (e.clientX || e.clientY);
    pop(el, fromPointer ? e.clientX : r.left + r.width / 2,
        fromPointer ? e.clientY : r.top + r.height / 2);
  }, { passive: true, capture: true });
}
installPress();

/* The shrine spinner: a sakura mon turning. Used wherever a wait is short
   enough that the fox would be too much. */
export function spinner(label) {
  return h('div.shrine-wait',
    h('span.mon-spin', { 'aria-hidden': 'true' }),
    label ? h('span', label) : null);
}

/* Which half of the game a mod runs on, as the loudest chip in the row.
   `server_side` / `client_side` follow Modrinth's vocabulary: required,
   optional, unsupported, unknown. */
export function sideTag(mod) {
  const server = (mod.server_side || '').toLowerCase();
  const client = (mod.client_side || '').toLowerCase();
  if (!server && !client) {
    return mod.client_only
      ? pill('client only', 'side-client',
        (mod.client_only_reasons || []).join('; ') || 'flagged client-only')
      : null;
  }
  const serverOk = server === 'required' || server === 'optional';
  const clientOk = client === 'required' || client === 'optional';

  if (server === 'unsupported') {
    return pill('client only', 'side-client',
      'Its author states server_side: unsupported — a server cannot use it.');
  }
  if (client === 'unsupported') {
    return pill('server only', 'side-server',
      'Its author states client_side: unsupported — players need nothing.');
  }
  if (serverOk && clientOk) {
    const optional = server === 'optional' || client === 'optional';
    return pill(optional ? 'both (optional)' : 'both sides', 'side-both',
      optional
        ? `server_side: ${server}, client_side: ${client} — it runs on both, `
          + 'and one of the two is optional.'
        : 'Required on the server and on every client that joins.');
  }
  if (serverOk) {
    return pill('server side', 'side-server', `server_side: ${server}`);
  }
  if (clientOk) {
    return pill('client side', 'side-client', `client_side: ${client}`);
  }
  return pill('side unknown', 'side-unknown',
    'Neither catalogue states which side this runs on.');
}


/* The catalogue a thing came from, as its own mark rather than the word.
   Two sources, two very recognisable logos — a text pill saying "modrinth"
   is strictly less legible at a glance than the logo everyone already
   knows. */
const SOURCE_ART = {
  curseforge: { src: '/assets/src-curseforge.png', label: 'CurseForge' },
  modrinth: { src: '/assets/src-modrinth.png', label: 'Modrinth' },
};

export function sourceMark(source) {
  const art = SOURCE_ART[(source || '').toLowerCase()];
  if (!art) return source ? pill(source, 'ghost') : null;
  return h('span.srcmark', { title: art.label },
    h('img', { src: art.src, alt: '', loading: 'lazy', width: 13, height: 13 }),
    h('span', art.label));
}
