/* ============================================================
   jobs — long operations, and the drawer that watches them.

   The rule this is built around: a job owns its stream, the
   drawer is only a view onto it. Closing the drawer closes the
   view, never the EventSource, so an install keeps running and
   keeps collecting its log while you go and do something else.
   Several jobs can be in flight at once and all of them are
   followed — the previous front end followed one and silently
   stopped showing the second.

   Every terminal frame carries the result, because a client
   closes its EventSource on the first frame reporting a terminal
   status and there is more than one such frame.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, bar, duration, $,
} from './core.js';

const live = new Map();      // job_id -> record
const listeners = new Set();
let drawerEl = null;
let bodyEl = null;
let titleEl = null;
let collapsed = false;

/* --- following a job ----------------------------------------- */

export function follow(jobId, { title, onEnd, onFrame, quiet } = {}) {
  if (live.has(jobId)) return live.get(jobId);

  const record = {
    id: jobId,
    title: title || 'Working',
    status: 'running',
    step: 'Starting',
    percent: 0,
    log: [],
    stream: '',
    result: null,
    error: null,
    startedAt: Date.now(),
    serverId: null,
    serverName: null,
    quiet: !!quiet,
    handlers: { onEnd: onEnd ? [onEnd] : [], onFrame: onFrame ? [onFrame] : [] },
    source: null,
    node: null,
  };
  live.set(jobId, record);

  const es = new EventSource(`/api/jobs/${jobId}/events`);
  record.source = es;

  es.onmessage = (event) => {
    let frame;
    try { frame = JSON.parse(event.data); } catch { return; }
    applyFrame(record, frame);
    render();
    record.handlers.onFrame.forEach((fn) => { try { fn(frame, record); } catch { /* a
      view that has been torn down must not kill the stream */ } });

    if (['done', 'error', 'cancelled'].includes(frame.status)) {
      finish(record, frame);
    }
  };

  es.onerror = () => {
    // A dropped connection is not a failed job: the server may simply have
    // restarted. Ask once for the truth rather than reporting a failure that
    // did not happen.
    if (record.status !== 'running') return;
    es.close();
    api.get(`/api/jobs/${jobId}`)
      .then((snap) => {
        applyFrame(record, snap);
        if (['done', 'error', 'cancelled'].includes(snap.status)) {
          finish(record, snap);
        } else {
          record.step = 'Reconnecting…';
          render();
          setTimeout(() => { live.delete(jobId); follow(jobId, { title, onEnd, quiet }); },
            2500);
        }
      })
      .catch(() => {
        record.status = 'error';
        record.error = 'Lost contact with the server while this was running. '
          + 'It may still have finished — reload to check.';
        finish(record, record);
      });
  };

  openDrawer();
  render();
  return record;
}

function applyFrame(record, frame) {
  if (frame.status) record.status = frame.status;
  if (frame.step) record.step = frame.step;
  if (typeof frame.percent === 'number') record.percent = frame.percent;
  if (frame.server_id) record.serverId = frame.server_id;
  if (frame.server_name) record.serverName = frame.server_name;
  if (frame.title) record.title = frame.title;
  if (frame.result !== undefined && frame.result !== null) record.result = frame.result;
  if (frame.error) record.error = frame.error;
  if (frame.event === 'log' && frame.message) {
    record.log.push({ level: frame.level || 'info', message: frame.message });
    if (record.log.length > 220) record.log.splice(0, record.log.length - 220);
  }
  if (Array.isArray(frame.log)) {
    record.log = frame.log.map((l) => ({ level: l.level, message: l.message }));
  }
  if (frame.event === 'stream' && frame.message) record.stream += frame.message;
  else if (typeof frame.stream === 'string' && frame.stream.length > record.stream.length) {
    record.stream = frame.stream;
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

  if (!record.quiet) {
    if (record.status === 'done') {
      toast(record.title, 'ok', { title: 'Finished' });
    } else if (record.status === 'error') {
      toast(record.error || 'It failed.', 'bad', { title: record.title });
    }
  }
  notify();
  render();
}

/* Start something and follow it in one call. Every endpoint that returns a
   job id goes through here, so no screen has to remember the two-step. */
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
  return follow(started.job_id, opts);
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

export function dismissJob(id) {
  const rec = live.get(id);
  if (rec?.status === 'running') return;
  live.delete(id);
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
  titleEl = h('h3', 'Activity');
  drawerEl = h('div#drawer', { hidden: true },
    h('header', {
      role: 'button', tabIndex: 0,
      'aria-label': 'Collapse or expand the activity drawer',
      onclick: () => toggleCollapsed(),
      onkeydown: (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCollapsed(); }
      },
    },
      icon('activity', 16), titleEl,
      h('button.btn.icon.xs.ghost', {
        'aria-label': 'Close the activity drawer',
        onclick: (e) => { e.stopPropagation(); closeDrawer(); },
      }, icon('x', 13)),
    ),
    bodyEl,
  );
  document.body.append(drawerEl);
}

function toggleCollapsed() {
  collapsed = !collapsed;
  bodyEl.hidden = collapsed;
}

export function openDrawer() {
  ensureDrawer();
  drawerEl.hidden = false;
  collapsed = false;
  bodyEl.hidden = false;
}

export function closeDrawer() {
  if (drawerEl) drawerEl.hidden = true;
}

let renderQueued = false;
function render() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    paint();
  });
}

function paint() {
  ensureDrawer();
  const jobs = [...live.values()].sort((a, b) => b.startedAt - a.startedAt);
  if (!jobs.length) { closeDrawer(); return; }
  const running = jobs.filter((j) => j.status === 'running').length;
  titleEl.textContent = running
    ? `Activity — ${running} running`
    : `Activity — ${jobs.length} finished`;
  mount(bodyEl, ...jobs.map(jobCard));
}

/* The art a job wears while it runs. Not decoration: an install that spends
   four minutes on "Uploading pack files" looks identical to one that has
   hung, and a thing that is visibly moving is the cheapest possible answer
   to "is this still going". */
function jobArt(rec) {
  if (rec.status !== 'running') return null;
  const step = (rec.step || '').toLowerCase();
  const art = /upload|download|fetch|batch|pack file/.test(step)
    ? 'pig-loadbar.gif'          // something is being carried across
    : 'bee-working.gif';         // something is being worked on
  return h('img', {
    src: `/assets/${art}`, alt: '', width: 26, height: 26,
    style: { imageRendering: 'pixelated', flex: 'none' },
  });
}

function jobCard(rec) {
  const state = rec.status === 'done' ? 'done'
    : rec.status === 'error' ? 'error'
      : rec.status === 'cancelled' ? 'error' : '';
  const elapsed = ((rec.finishedAt || Date.now()) - rec.startedAt) / 1000;

  return h(`div.jobcard.${state}`,
    h('div.t',
      jobArt(rec),
      h('span.grow', rec.title),
      h('span.mono.muted', duration(elapsed)),
      rec.status === 'running'
        ? h('button.btn.icon.xs.ghost', {
          'aria-label': `Cancel ${rec.title}`, onclick: () => cancelJob(rec.id),
        }, icon('x', 12))
        : h('button.btn.icon.xs.ghost', {
          'aria-label': `Dismiss ${rec.title}`, onclick: () => dismissJob(rec.id),
        }, icon('check', 12)),
    ),
    h('div.step', rec.status === 'error' ? (rec.error || 'Failed') : rec.step),
    rec.status === 'running'
      ? bar(rec.percent)
      : bar(100, rec.status === 'done' ? 'ok' : 'bad'),
    rec.log.length
      ? h('div.joblog', { ref: 'log' },
        ...rec.log.slice(-40).map((l) =>
          h(`div${l.level === 'warn' ? '.warn' : l.level === 'error' ? '.error' : ''}`,
            l.message)))
      : null,
  );
}

/* Re-tick the elapsed clocks once a second while anything is running, so the
   drawer never looks frozen during a long silent phase. */
setInterval(() => {
  if ([...live.values()].some((j) => j.status === 'running')) render();
}, 1000);

/* Adopt anything already running on the server — a job survives a browser
   reload, and a page that forgets about it looks like the work was lost. */
export async function adoptRunning() {
  try {
    const { items } = await api.get('/api/jobs');
    for (const job of items || []) {
      if (job.status === 'running' && !live.has(job.id)) {
        follow(job.id, { title: job.title, quiet: true });
      }
    }
  } catch { /* the fleet list will report the outage on its own */ }
}
