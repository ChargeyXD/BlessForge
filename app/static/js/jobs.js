/* ============================================================
   jobs — long operations, and the shrine road that watches them.

   Three rules this file is built around.

   1. A JOB OWNS ITS STREAM; THE DRAWER IS ONLY A VIEW ONTO IT.
      Closing the drawer closes the view, never the EventSource,
      so an install keeps running and keeps collecting its log
      while you go and do something else. Several jobs are in
      flight at once and all of them are followed — the front end
      before this one followed one and silently stopped showing
      the second.

   2. THE VIEW IS PATCHED, NEVER REBUILT.
      This is the fix for the stutter. The old paint() ended in
          mount(bodyEl, ...jobs.map(jobCard))
      which is clear() + append(): every card, every log line,
      every <img>, every button was a BRAND NEW node on every
      frame. An install emits a log line per mod, so the whole
      list was thrown away and rebuilt many times a second, and
      every one of those rebuilds:
        * re-ran `.joblog div{animation:fade .2s both}` from
          opacity 0 on every line already on screen — that is
          the flash;
        * restarted the bee/pig GIF from frame 0, so the one
          thing on screen that proves the job is alive juddered;
        * handed every .btn a fresh wiggling-polygon blob with a
          fresh random phase, so the buttons twitched;
        * reset .body's and .joblog's scrollTop to the top;
        * replaced bar()'s <i>, so its width transition never
          got to run and the progress snapped instead of moving.
      A one-second setInterval re-ran that same full rebuild even
      when nothing whatsoever had changed.

      So: one node per job id, kept for the life of the job. Only
      the text, the widths and the classes that actually differ
      are written, and only NEW log lines are appended.

   3. A FRAME NEVER LIES ABOUT PROGRESS.
      Every terminal frame carries the result, because a client
      closes its EventSource on the first frame reporting a
      terminal status and there is more than one such frame. A
      dropped connection is not a failed job: the server is asked
      for the truth, on the SAME record, so the log and the
      elapsed clock survive the reconnect.
   ============================================================ */

import {
  h, clear, icon, api, toast, toastError, duration,
} from './core.js';

/* --- state --------------------------------------------------- */

const live = new Map();      // job id -> record   (the truth)
const views = new Map();     // job id -> handles  (the view onto it)
const listeners = new Set();

let drawerEl = null;
let bodyEl = null;
let summaryEl = null;
let sayEl = null;
let emptyEl = null;
let hdrFill = null;
let collapsed = false;
let userOpened = false;
let pendingSay = '';

/* A job that has said nothing for this long is reported as quiet. An
   install that spends four minutes inside one Crafty upload is
   indistinguishable from one that has hung, and "is this still going" is
   the only question the drawer exists to answer. */
const STALE_MS = 22000;
/* Finished jobs that pile up while the drawer is shut. */
const KEEP_FINISHED = 8;
/* Log lines held in the DOM per card. The record keeps more. */
const LOG_IN_DOM = 90;

/* --- the art a job wears while it runs -----------------------
   Not decoration. A thing that is visibly moving is the cheapest possible
   answer to "is this still going", and the charm that carries it rides the
   rope at the job's own percent, so the art is also the position. */

const ART_CARRY = '/assets/pig-loadbar.gif';   // something is being hauled
const ART_WORK = '/assets/bee-working.gif';    // something is being worked on

const CARRYING = /upload|download|fetch|batch|pack file|push|pull|carr/i;

function artFor(rec) {
  if (rec.status !== 'running') return null;
  return CARRYING.test(rec.step || '') ? ART_CARRY : ART_WORK;
}

/* --- the road a job walks ------------------------------------
   A modpack install runs for minutes through phases that look nothing like
   each other, and percent alone does not say which one you are in. The
   anchors are the server's own percents (see installer.py / provision.py),
   so a phase boundary is a real event and not a guess. */

const PHASES = {
  install: [
    { at: 0, name: 'Reading the pack' },
    { at: 12, name: 'Gathering mods' },
    { at: 55, name: 'Raising the instance' },
    { at: 58, name: 'Carrying it over' },
    { at: 92, name: 'Final blessing' },
  ],
  provision: [
    { at: 0, name: 'Creating the instance' },
    { at: 55, name: 'Installing the loader' },
    { at: 76, name: 'Preparing the directory' },
    { at: 84, name: 'Applying settings' },
    { at: 96, name: 'Final blessing' },
  ],
};
const ONE_PHASE = [{ at: 0, name: 'Working' }];

/* The long multi-phase jobs, by the endpoint that starts them and by the
   `kind` the server gives them. Both, because `run()` knows the path and
   `adoptRunning()` knows the kind, and a reload must not downgrade a
   running install to a plain rope. */
const KIND_BY_PATH = [
  [/\/api\/install\/modpack/, 'install'],
  [/\/api\/roulette\/install/, 'install'],
  [/\/api\/provision\//, 'provision'],
];
const KIND_BY_SERVER = {
  install: 'install', roulette: 'install', provision: 'provision',
};

function kindForPath(path) {
  for (const [re, kind] of KIND_BY_PATH) if (re.test(path)) return kind;
  return null;
}

const phasesFor = (rec) => PHASES[rec.kind] || ONE_PHASE;

function phaseIndex(phases, percent) {
  let found = 0;
  for (let i = 0; i < phases.length; i += 1) {
    if (percent >= phases[i].at) found = i;
  }
  return found;
}

/* --- how big is this thing ------------------------------------
   The steps already carry the scale; nothing else knows it. "Downloading
   mods (142/301)" and "Uploading pack files (3/7, 214 MB)" are the only
   places the front end can learn how many of what there are, and how many
   are done — which is the difference between a percentage and a sense of
   how much work is left. */

const COUNT_RE = /\((\d[\d,]*)\s*\/\s*(\d[\d,]*)(?:\s*,\s*([\d.]+)\s*MB)?\)/;
const TOTAL_RE = /\b(\d[\d,]*)\s+(?:mod files|jars|starter)/i;
const HAUL_RE = /uploading\s+(\d[\d,]*)\s+files\s*\(([\d.]+)\s*MB\)/i;

const num = (s) => Number(String(s).replace(/,/g, '')) || 0;

function unitFor(step) {
  const s = (step || '').toLowerCase();
  if (/upload|pack file|batch/.test(s)) return 'batches';
  if (/jar/.test(s)) return 'jars';
  if (/mod/.test(s)) return 'mods';
  return 'steps';
}

function readScale(rec) {
  const step = rec.step || '';
  const m = COUNT_RE.exec(step);
  if (m) {
    rec.scale = {
      done: num(m[1]), total: num(m[2]), unit: unitFor(step),
      mb: m[3] ? Number(m[3]) : null,
    };
    return;
  }
  const t = TOTAL_RE.exec(step);
  if (t) rec.scale = { done: 0, total: num(t[1]), unit: unitFor(step), mb: null };
  // No count in this step does NOT mean the count is gone: "Preparing pack
  // files" sits between two "Uploading pack files (i/n)" steps. The tally is
  // cleared when the PHASE changes, not when one step happens to be silent.
}

/* --- following a job ----------------------------------------- */

export function follow(jobId, { title, onEnd, onFrame, quiet, kind } = {}) {
  if (live.has(jobId)) return live.get(jobId);

  const record = {
    id: jobId,
    kind: kind || null,
    title: title || 'Working',
    status: 'running',
    step: 'Starting',
    percent: 0,
    phase: 0,
    scale: null,
    haul: null,
    log: [],
    logTotal: 0,      // monotonic: the record trims its log, this never drops
    logEpoch: 0,      // bumped when the log is replaced wholesale
    stream: '',
    result: null,
    error: null,
    startedAt: Date.now(),
    lastFrameAt: Date.now(),
    eta: null,
    etaAt: 0,
    serverId: null,
    serverName: null,
    quiet: !!quiet,
    handlers: { onEnd: onEnd ? [onEnd] : [], onFrame: onFrame ? [onFrame] : [] },
    source: null,
  };
  live.set(jobId, record);

  openStream(record);
  showDrawer();
  render();
  notify();
  return record;
}

/* One place that knows how a record is wired to the server, so a reconnect
   can reattach to the SAME record. The old code deleted the record and
   called follow() again, which reset startedAt and threw the log away — a
   dropped connection made a twenty-minute install look like it had just
   started with nothing to show for itself. */
function openStream(record) {
  const es = new EventSource(`/api/jobs/${record.id}/events`);
  record.source = es;

  es.onmessage = (event) => {
    let frame;
    try { frame = JSON.parse(event.data); } catch { return; }
    applyFrame(record, frame);
    render();
    record.handlers.onFrame.forEach((fn) => {
      try { fn(frame, record); } catch { /* a view that has been torn down
        must not kill the stream */ }
    });
    if (['done', 'error', 'cancelled'].includes(frame.status)) finish(record, frame);
  };

  es.onerror = () => {
    // A dropped connection is not a failed job: the server may simply have
    // restarted. Ask once for the truth rather than reporting a failure that
    // did not happen.
    if (record.status !== 'running') return;
    es.close();
    api.get(`/api/jobs/${record.id}`)
      .then((snap) => {
        applyFrame(record, snap);
        if (['done', 'error', 'cancelled'].includes(snap.status)) {
          finish(record, snap);
        } else {
          record.step = 'Reconnecting…';
          render();
          setTimeout(() => {
            if (record.status === 'running' && live.has(record.id)) openStream(record);
          }, 2500);
        }
      })
      .catch(() => {
        record.status = 'error';
        record.error = 'Lost contact with the server while this was running. '
          + 'It may still have finished — reload to check.';
        finish(record, record);
      });
  };
  return es;
}

function applyFrame(record, frame) {
  record.lastFrameAt = Date.now();
  if (frame.kind && !record.kind) record.kind = KIND_BY_SERVER[frame.kind] || null;
  if (frame.status) record.status = frame.status;
  if (frame.step) record.step = frame.step;
  if (typeof frame.percent === 'number') record.percent = frame.percent;
  if (frame.server_id) record.serverId = frame.server_id;
  if (frame.server_name) record.serverName = frame.server_name;
  if (frame.title) record.title = frame.title;
  if (frame.result !== undefined && frame.result !== null) record.result = frame.result;
  if (frame.error) record.error = frame.error;

  if (frame.event === 'log' && frame.message) {
    pushLog(record, frame.level || 'info', frame.message);
  }
  if (Array.isArray(frame.log)) {
    // A snapshot after a reconnect replaces the whole log. The view has to
    // be told, or it would append the tail a second time.
    record.log = frame.log.map((l) => ({ level: l.level, message: l.message }));
    record.logTotal = record.log.length;
    record.logEpoch += 1;
  }
  if (frame.event === 'stream' && frame.message) record.stream += frame.message;
  else if (typeof frame.stream === 'string' && frame.stream.length > record.stream.length) {
    record.stream = frame.stream;
  }

  const phases = phasesFor(record);
  const phase = phaseIndex(phases, record.percent);
  if (phase !== record.phase) {
    record.phase = phase;
    record.scale = null;          // a new phase counts a new kind of thing
    if (record.status === 'running') say(`${record.title}: ${phases[phase].name}.`);
  }
  readScale(record);
  readHaul(record);
}

function pushLog(record, level, message) {
  record.log.push({ level, message });
  record.logTotal += 1;
  if (record.log.length > 220) record.log.splice(0, record.log.length - 220);
  readHaulFrom(record, message);
}

/* "Uploading 301 files (512 MB) to the instance" is logged once, and it is
   the only statement anywhere of how big this install actually is. */
function readHaulFrom(record, message) {
  const m = HAUL_RE.exec(message || '');
  if (m) record.haul = { files: num(m[1]), mb: Number(m[2]) };
}
function readHaul(record) {
  if (record.haul) return;
  for (let i = record.log.length - 1; i >= 0 && i > record.log.length - 12; i -= 1) {
    readHaulFrom(record, record.log[i].message);
    if (record.haul) return;
  }
}

function finish(record, frame) {
  record.source?.close();
  record.finishedAt = Date.now();
  record.status = frame.status || record.status;
  if (frame.result !== undefined && frame.result !== null) record.result = frame.result;
  if (frame.error) record.error = frame.error;

  const handlers = record.handlers.onEnd;
  record.handlers.onEnd = [];
  handlers.forEach((fn) => {
    try { fn(record); } catch (e) { console.error(e); }
  });

  if (record.status === 'done') say(`${record.title} finished.`);
  else if (record.status === 'cancelled') say(`${record.title} was cancelled.`);
  else if (record.status === 'error') say(`${record.title} failed.`);

  if (!record.quiet) {
    if (record.status === 'done') {
      toast(record.title, 'ok', { title: 'Finished' });
    } else if (record.status === 'error') {
      toast(record.error || 'It failed.', 'bad', { title: record.title });
    }
  }
  // Jobs that end while the drawer is shut are kept, so reopening it shows
  // what happened — but not without limit.
  trimFinished();
  notify();
  render();
}

function trimFinished() {
  const done = [...live.values()]
    .filter((j) => j.status !== 'running')
    .sort((a, b) => (a.finishedAt || a.startedAt) - (b.finishedAt || b.startedAt));
  while (done.length > KEEP_FINISHED) forget(done.shift().id);
}

/* Start something and follow it in one call. Every endpoint that returns a
   job id goes through here, so no screen has to remember the two-step — and
   it is also where the path tells us this is one of the long, many-phased
   jobs that get the full road. */
export async function run(path, body, opts = {}) {
  let started;
  try {
    started = await api.post(path, body);
  } catch (e) {
    if (!opts.quiet) toastError(e, opts.title || 'Could not start');
    throw e;
  }
  if (!started?.job_id) {
    // Some endpoints answer immediately rather than with a job. That is a
    // valid answer, not an error.
    opts.onEnd?.({ status: 'done', result: started });
    return { immediate: started };
  }
  return follow(started.job_id, { ...opts, kind: opts.kind || kindForPath(path) });
}

/* Await a job's result as a promise, for callers that genuinely cannot
   continue without it (a review before an install, a scan before a list). */
export function runAwait(path, body, opts = {}) {
  return new Promise((resolve, reject) => {
    run(path, body, {
      ...opts,
      onEnd: (record) => {
        opts.onEnd?.(record);
        if (record.status === 'done') resolve(record.result);
        else reject(new Error(record.error || 'The job was cancelled.'));
      },
    }).catch(reject);
  });
}

export const activeJobs = () => [...live.values()];
export const jobsRunning = () =>
  [...live.values()].filter((j) => j.status === 'running').length;

export function onJobsChanged(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function notify() { listeners.forEach((fn) => { try { fn(); } catch { /* ignore */ } }); }

/* Drop a record AND its node together. Anything that forgets a job has to
   go through here, or the view map leaks a card whose job no longer
   exists. */
function forget(id) {
  const rec = live.get(id);
  if (rec && rec.status !== 'running') rec.source?.close();
  live.delete(id);
  const view = views.get(id);
  if (view) { view.root.remove(); views.delete(id); }
}

export function dismissJob(id) {
  const rec = live.get(id);
  if (!rec || rec.status === 'running') return;
  forget(id);
  render();
  notify();
}

export async function cancelJob(id) {
  try {
    await api.post(`/api/jobs/${id}/cancel`);
    toast('Asked it to stop.', 'warn');
  } catch (e) { toastError(e); }
}

/* --- the drawer ---------------------------------------------- */

function ensureDrawer() {
  if (drawerEl) return;
  bodyEl = h('div.body');
  summaryEl = h('span.jb-sum', 'nothing yet');
  hdrFill = h('i');
  // The live region. It announces STATUS, never log lines: a drawer that
  // reads out three hundred download lines is not an accessible drawer, it
  // is a denial of service.
  sayEl = h('p.jb-say', {
    role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true',
  }, pendingSay);
  emptyEl = h('div.jb-empty.jb-hid',
    icon('activity', 22), h('span', 'Nothing is running right now.'));

  const header = h('header', {
    role: 'button', tabIndex: 0, 'aria-expanded': 'true',
    'aria-label': 'Collapse or expand the activity drawer',
    onclick: () => toggleCollapsed(),
    onkeydown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCollapsed(); }
    },
  },
    torii('start'),          // the gate to the road, at header scale
    h('h3', 'Activity'),
    summaryEl,
    h('span.jb-hdrrope', { 'aria-hidden': 'true' }, hdrFill),
    h('button.btn.icon.xs.ghost', {
      'aria-label': 'Close the activity drawer',
      onclick: (e) => { e.stopPropagation(); closeDrawer(); },
    }, icon('x', 13)),
  );

  drawerEl = h('div#drawer', {
    hidden: true, role: 'region', 'aria-label': 'Activity',
  }, header, sayEl, bodyEl);
  bodyEl.append(emptyEl);
  document.body.append(drawerEl);
}

function toggleCollapsed() {
  collapsed = !collapsed;
  bodyEl.hidden = collapsed;
  drawerEl.classList.toggle('collapsed', collapsed);
  drawerEl.firstElementChild.setAttribute('aria-expanded', String(!collapsed));
  // The clock tick skips a collapsed drawer, so expanding one has to catch
  // it up rather than leave a stale elapsed time on screen for a second.
  if (!collapsed) render();
}

/* A job starting shows the drawer, but does not count as the user asking
   for it — so the drawer still gets out of the way once the last card is
   dismissed. */
function showDrawer() {
  ensureDrawer();
  drawerEl.hidden = false;
  collapsed = false;
  bodyEl.hidden = false;
  drawerEl.classList.remove('collapsed');
  drawerEl.firstElementChild.setAttribute('aria-expanded', 'true');
  render();
}

export function openDrawer() {
  userOpened = true;
  showDrawer();
}

/* CLOSING THE DRAWER TIDIES UP AFTER ITSELF.

   Everything that has finished, failed or been cancelled is forgotten. A
   RUNNING job is not touched: its EventSource stays open, it stays in
   `live`, and reopening the drawer finds it exactly where it was. The
   drawer is a view; closing a view must never be able to kill the work. */
export function closeDrawer() {
  userOpened = false;
  if (!drawerEl) return;
  drawerEl.hidden = true;
  let dropped = false;
  for (const rec of [...live.values()]) {
    if (rec.status === 'running') continue;
    forget(rec.id);
    dropped = true;
  }
  if (dropped) notify();
}

function hideDrawer() {
  if (drawerEl) drawerEl.hidden = true;
}

function say(text) {
  pendingSay = text;
  if (sayEl && sayEl.textContent !== text) sayEl.textContent = text;
}

/* --- painting ------------------------------------------------
   rAF-throttled, but a rAF in a hidden tab NEVER FIRES. The old code left
   its `renderQueued` flag raised in that case and the drawer froze until
   the tab came back, then landed everything at once. A hidden tab is
   skipped outright instead, and the dirty flag survives until something can
   actually be seen. */

let dirty = false;
let queued = false;

function render() {
  dirty = true;
  schedule();
}

function schedule() {
  if (queued || !dirty) return;
  if (document.hidden) return;              // visibilitychange re-schedules
  if (!drawerEl || drawerEl.hidden) return; // showDrawer() re-schedules
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    if (!dirty) return;
    dirty = false;
    paint();
  });
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) schedule();
});

function paint() {
  ensureDrawer();
  // Views whose job is gone go first, so nothing below has to check.
  for (const id of [...views.keys()]) if (!live.has(id)) forget(id);

  const jobs = [...live.values()].sort((a, b) => b.startedAt - a.startedAt);
  if (!jobs.length && !userOpened) { hideDrawer(); return; }
  emptyEl.classList.toggle('jb-hid', jobs.length > 0);

  jobs.forEach((rec, i) => {
    let view = views.get(rec.id);
    if (!view) { view = buildCard(rec); views.set(rec.id, view); }
    patchCard(view, rec);
    // Order only ever changes when a job is added, and a new job sorts to
    // the top — so this moves exactly one node, and never re-inserts (and
    // so never restarts the entrance animation on) a card that is already
    // where it belongs.
    if (bodyEl.children[i] !== view.root) {
      bodyEl.insertBefore(view.root, bodyEl.children[i] || null);
    }
  });
  // Keep the empty line last without moving it on every paint: a needless
  // append is a real DOM mutation, and needless mutations are the thing
  // this rewrite exists to stop.
  if (bodyEl.lastElementChild !== emptyEl) bodyEl.append(emptyEl);

  paintHeader(jobs);
}

function paintHeader(jobs) {
  const running = jobs.filter((j) => j.status === 'running');
  const done = jobs.filter((j) => j.status === 'done').length;
  const bad = jobs.length - running.length - done;
  const parts = [];
  if (running.length) parts.push(`${running.length} running`);
  if (done) parts.push(`${done} done`);
  if (bad) parts.push(`${bad} failed`);
  setText(summaryEl, parts.join(' · ') || 'nothing yet');
  drawerEl.classList.toggle('busy', running.length > 0);
  const p = running.length
    ? running.reduce((a, j) => a + (j.percent || 0), 0) / running.length
    : 100;
  setStyle(hdrFill, 'width', `${Math.round(p)}%`);
}

/* --- writing only what changed -------------------------------
   Every write to the DOM here is guarded. An unguarded textContent write is
   not free: it destroys and recreates the text node, which is enough to
   drop a selection and to make the browser re-lay-out a line that did not
   change. */

function setText(el, text) {
  const value = text == null ? '' : String(text);
  if (el.textContent !== value) el.textContent = value;
}
function setStyle(el, prop, value) {
  if (el.style[prop] !== value) el.style[prop] = value;
}
function setHid(el, hidden) { el.classList.toggle('jb-hid', !!hidden); }

const stateOf = (rec) => (rec.status === 'done' ? 'done'
  : rec.status === 'error' || rec.status === 'cancelled' ? 'error'
    : 'running');

/* --- one card ------------------------------------------------

   The road, top to bottom:

     ⛩──◆────◆────◆──⛩      a shimenawa rope strung between two torii,
        the travelled length in gold leaf, a diamond knot at every phase
        boundary, and the working charm — the bee, or the pig when
        something is being hauled — walking it at the job's own percent.

   Not a rectangle with a fill in it: where the charm IS on the rope is the
   progress, which is legible from across the room, and the knots say how
   much of the road is a different kind of work. */

function torii(side) {
  return h(`span.jb-torii.${side}`, { 'aria-hidden': 'true' },
    h('i.kas'), h('i.nuk'), h('i.pst.l'), h('i.pst.r'));
}

function buildCard(rec) {
  const phases = phasesFor(rec);
  const v = {
    state: '', artSrc: '', logEpoch: -1, seen: 0, tallyN: 0, tallyOn: -1,
    logOpen: true, phase: -1,
  };

  v.title = h('span.jb-name');
  v.mon = h('span.jb-mon', { 'aria-hidden': 'true' });
  v.pct = h('b.jb-pct');
  v.clock = h('span.jb-clock', '0s');
  v.eta = h('span.jb-eta.jb-hid');

  v.iconStop = icon('x', 12);
  v.iconDone = icon('check', 12);
  v.act = h('button.btn.icon.xs.ghost', {
    onclick: () => {
      const now = live.get(rec.id);
      if (!now) return;
      if (now.status === 'running') cancelJob(now.id); else dismissJob(now.id);
    },
  }, v.iconStop, v.iconDone);

  // --- the road
  v.art = h('img.jb-art.jb-hid', { alt: '', width: 26, height: 26 });
  v.charm = h('span.jb-charm', v.art, h('i.jb-bead'));
  v.fill = h('i.jb-rope-done');
  v.knots = phases.slice(1).map((p) => h('b.jb-knot', {
    style: { left: `${p.at}%` }, title: p.name, 'aria-hidden': 'true',
  }));
  v.track = h('span.jb-track', h('span.jb-rope', v.fill), ...v.knots, v.charm);
  v.rail = h('div.jb-rail', {
    role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
    'aria-valuenow': '0', 'aria-label': 'Progress',
  }, torii('start'), v.track, torii('end'));

  // --- what is happening right now
  v.phaseEl = h('span.jb-phase');
  v.phaseNo = h('span.jb-phaseno.jb-hid');
  v.step = h('span.jb-step');
  v.quiet = h('span.jb-hush.jb-hid');

  // --- how much of it there is
  v.tally = h('div.jb-tally', { 'aria-hidden': 'true' });
  v.count = h('span.jb-count');
  v.haul = h('span.jb-haul.jb-hid');
  v.scaleRow = h('div.jb-scale.jb-hid', v.tally, h('div.jb-nums', v.count, v.haul));

  // --- the log
  v.logn = h('span.jb-logn', '0 lines');
  v.logTog = h('button.jb-logtog', {
    type: 'button', 'aria-expanded': 'true',
    onclick: () => {
      v.logOpen = !v.logOpen;
      setHid(v.log, !v.logOpen);
      v.logTog.classList.toggle('shut', !v.logOpen);
      v.logTog.setAttribute('aria-expanded', String(v.logOpen));
    },
  }, icon('chevronDown', 12), v.logn);
  // role=log would make this a live region by default, and it must not be
  // one: announcing every download line is how a screen reader is made
  // useless. Status is announced once, in the drawer's own live region.
  v.log = h('div.jb-log', { 'aria-live': 'off', tabIndex: 0 });
  v.logWrap = h('div.jb-logwrap.jb-hid', v.logTog, v.log);

  v.root = h('article.jobcard.running',
    h('div.jb-head', v.mon, v.title, v.pct, v.act),
    v.rail,
    h('div.jb-now', v.phaseEl, v.phaseNo, v.step),
    h('div.jb-meta', v.clock, v.eta, v.quiet),
    v.scaleRow,
    v.logWrap,
  );
  return v;
}

/* A record learns its kind at follow() time, but a reconnect snapshot can
   teach it one it did not have — and the knots were cut when the card was
   built. Re-cut them rather than leaving a five-phase install wearing a
   one-phase rope. */
function syncKnots(v, phases) {
  if (v.knots.length === phases.length - 1) return;
  v.knots.forEach((k) => k.remove());
  v.knots = phases.slice(1).map((p) => h('b.jb-knot', {
    style: { left: `${p.at}%` }, title: p.name, 'aria-hidden': 'true',
  }));
  v.knots.forEach((k) => v.track.insertBefore(k, v.charm));
  v.phase = -1;
}

function patchCard(v, rec) {
  const phases = phasesFor(rec);
  syncKnots(v, phases);
  const state = stateOf(rec);
  if (v.state !== state) {
    v.root.classList.remove('running', 'done', 'error');
    v.root.classList.add(state);
    v.state = state;
    setHid(v.iconStop, state !== 'running');
    setHid(v.iconDone, state === 'running');
    v.act.setAttribute('aria-label',
      state === 'running' ? `Cancel ${rec.title}` : `Dismiss ${rec.title}`);
  }
  setText(v.title, rec.title);

  const running = rec.status === 'running';
  const seeking = running && !(rec.percent > 0);
  const pct = rec.status === 'done' ? 100
    : Math.max(0, Math.min(100, rec.percent || 0));

  v.root.classList.toggle('seeking', seeking);
  setStyle(v.fill, 'width', seeking ? '26%' : `${pct}%`);
  setStyle(v.charm, 'left', `${pct}%`);
  setText(v.pct, running && seeking ? '' : `${Math.round(pct)}%`);
  if (v.rail.getAttribute('aria-valuenow') !== String(Math.round(pct))) {
    v.rail.setAttribute('aria-valuenow', String(Math.round(pct)));
  }

  const idx = Math.min(rec.phase, phases.length - 1);
  if (v.phase !== idx) {
    v.knots.forEach((k, i) => {
      k.classList.toggle('lit', idx >= i + 1);
      k.classList.toggle('now', idx === i + 1);
    });
    v.phase = idx;
  }
  if (rec.status === 'done') v.knots.forEach((k) => {
    k.classList.add('lit'); k.classList.remove('now');
  });

  // Once a job is over, the leg it happened to die on is not the headline —
  // "WORKING / Finished" is two labels disagreeing with each other.
  setText(v.phaseEl, running ? phases[idx].name
    : rec.status === 'done' ? 'Arrived'
      : rec.status === 'cancelled' ? 'Turned back' : 'Stopped short');
  setHid(v.phaseNo, !running || phases.length < 2);
  setText(v.phaseNo, `leg ${idx + 1} of ${phases.length}`);
  setText(v.step, rec.status === 'error' ? (rec.error || 'It failed.')
    : rec.status === 'cancelled' ? 'Stopped.' : rec.step);

  const src = artFor(rec);
  if (src) {
    // Only when it actually changes: assigning the same src to an <img>
    // restarts the GIF, and a mascot that restarts twice a second is the
    // stutter wearing a costume.
    if (v.artSrc !== src) { v.art.src = src; v.artSrc = src; }
    setHid(v.art, false);
  } else {
    setHid(v.art, true);
  }

  patchScale(v, rec);
  patchLog(v, rec);
  patchTime(v, rec);
}

function patchScale(v, rec) {
  const sc = rec.scale && rec.scale.total >= 2 ? rec.scale : null;
  const haul = rec.haul;

  setHid(v.scaleRow, !sc && !haul);
  setHid(v.tally, !sc);
  setText(v.count, sc
    ? `${sc.done} / ${sc.total} ${sc.unit}${sc.mb ? ` · ${Math.round(sc.mb)} MB` : ''}`
    : '');
  setHid(v.haul, !haul);
  if (haul) {
    setText(v.haul, `${haul.files} files · ${Math.round(haul.mb)} MB in the pack`);
  }
  if (!sc) return;

  // One tick per thing, up to a row that still reads as a row. This is what
  // says "three hundred of them" at a glance, which a percentage never does.
  const n = Math.max(6, Math.min(48, sc.total));
  if (v.tallyN !== n) {
    clear(v.tally);
    v.ticks = Array.from({ length: n }, () => h('b'));
    v.tally.append(...v.ticks);
    v.tallyN = n;
    v.tallyOn = -1;
  }
  const on = Math.max(0, Math.min(n, Math.round(n * (sc.done / sc.total))));
  if (v.tallyOn !== on) {
    const first = v.tallyOn < 0 ? 0 : Math.min(v.tallyOn, on);
    const last = v.tallyOn < 0 ? n : Math.max(v.tallyOn, on);
    for (let i = first; i < last; i += 1) v.ticks[i].classList.toggle('on', i < on);
    v.tallyOn = on;
  }
}

function patchLog(v, rec) {
  if (v.logEpoch !== rec.logEpoch) {
    clear(v.log);
    v.logEpoch = rec.logEpoch;
    v.seen = rec.logTotal - rec.log.length;
  }
  setHid(v.logWrap, rec.log.length === 0);
  if (rec.logTotal === v.seen) return;

  // The record trims its own log, so "how many are new" is the difference
  // of two monotonic counters, never an index into an array that shifted.
  const fresh = Math.min(rec.logTotal - v.seen, rec.log.length);
  const box = v.log;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 26;
  for (let i = rec.log.length - fresh; i < rec.log.length; i += 1) {
    const line = rec.log[i];
    const tone = line.level === 'warn' ? '.warn' : line.level === 'error' ? '.bad' : '';
    box.append(h(`div.ln${tone}`, line.message));
  }
  v.seen = rec.logTotal;
  while (box.childElementCount > LOG_IN_DOM) box.removeChild(box.firstChild);
  // Follow the tail only if the reader was already at the tail. Someone
  // scrolled up is someone reading, and yanking them back is the rudest
  // thing a log pane can do.
  if (atBottom) box.scrollTop = box.scrollHeight;
  setText(v.logn, `${rec.logTotal} line${rec.logTotal === 1 ? '' : 's'}`);
}

/* The elapsed clock, the estimate and the quiet warning. Called once a
   second for running jobs — and it writes three short strings, where the
   old one-second tick rebuilt the entire drawer. */
function patchTime(v, rec, now = Date.now()) {
  const elapsed = ((rec.finishedAt || now) - rec.startedAt) / 1000;
  setText(v.clock, duration(elapsed));

  const eta = estimate(rec, now);
  setHid(v.eta, !eta);
  if (eta) setText(v.eta, `~${duration(eta / 1000)} left`);

  const silent = now - (rec.lastFrameAt || rec.startedAt);
  const hush = rec.status === 'running' && silent > STALE_MS;
  v.root.classList.toggle('hushed', hush);
  setHid(v.quiet, !hush);
  if (hush) setText(v.quiet, `quiet for ${duration(silent / 1000)}`);
}

/* Elapsed and percent are enough for an honest estimate, and an install
   that says "about four minutes left" is an install you can walk away from.
   Smoothed, because the server's percent moves in jumps and a number that
   flaps between 2m and 9m is worse than no number. */
function estimate(rec, now) {
  if (rec.status !== 'running') return null;
  const p = rec.percent;
  if (!(p >= 4) || p >= 99.5) return null;
  const elapsed = now - rec.startedAt;
  if (elapsed < 4000) return null;
  const raw = (elapsed * (100 - p)) / p;
  if (!Number.isFinite(raw) || raw > 6 * 3600e3) return null;
  if (now - rec.etaAt > 500) {
    rec.eta = rec.eta ? rec.eta * 0.72 + raw * 0.28 : raw;
    rec.etaAt = now;
  }
  return rec.eta;
}

/* Re-tick the clocks once a second while anything is running. Nothing is
   rebuilt and nothing is re-sorted: this walks the views that exist and
   writes the strings that moved. */
setInterval(() => {
  if (document.hidden || !drawerEl || drawerEl.hidden || collapsed) return;
  const now = Date.now();
  for (const [id, view] of views) {
    const rec = live.get(id);
    if (!rec || rec.status !== 'running') continue;
    patchTime(view, rec, now);
  }
}, 1000);

/* Adopt anything already running on the server — a job survives a browser
   reload, and a page that forgets about it looks like the work was lost.
   The server knows the kind, so an install adopted after a reload still
   gets its road and not a bare rope. */
export async function adoptRunning() {
  try {
    const { items } = await api.get('/api/jobs');
    for (const job of items || []) {
      if (job.status === 'running' && !live.has(job.id)) {
        follow(job.id, {
          title: job.title, quiet: true, kind: KIND_BY_SERVER[job.kind] || null,
        });
      }
    }
  } catch { /* the fleet list will report the outage on its own */ }
}
