/* ============================================================
   Console — Crafty's output, live, with a command box.

   Crafty has no push channel, so the backend polls it and streams
   only the lines that are new. Two behaviours matter here and
   both are about being able to read scrollback on a chatty
   server: following pauses the moment you scroll up and resumes
   when you scroll back to the bottom, and the filter never
   discards lines, it only hides them.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, empty,
  loadingFox, debounce, segmented, prefs, copyText,
} from '../core.js';

const LEVEL = [
  [/\b(ERROR|FATAL|SEVERE|Exception|Caused by:)\b/i, 'err'],
  [/\bWARN(ING)?\b/i, 'warn'],
  [/\b(Done \(|Starting minecraft server|Preparing level)\b/i, 'ok'],
  [/<[^>]{1,20}>/, 'chat'],
  [/\bINFO\b/i, 'info'],
];

function levelOf(line) {
  for (const [re, cls] of LEVEL) if (re.test(line)) return cls;
  return '';
}

export async function render(ctx) {
  const node = h('div');
  const term = h('div.term', {
    role: 'log', 'aria-live': 'off', 'aria-label': 'Server console',
    tabIndex: 0,
  });
  const statusPill = h('span.pill.dead', 'connecting');
  const sourcePill = h('span.pill.ghost', '—');
  const countPill = h('span.pill.ghost', '0 lines');

  let follow = true;
  let filter = prefs.get('console.filter', 'all');
  let query = '';
  let lines = [];
  let source = null;
  let closed = false;

  const cmd = h('input.inp', {
    placeholder: ctx.running ? 'Type a command and press Enter (no leading /)'
      : 'The server is not running',
    disabled: !ctx.running,
    'aria-label': 'Send a command to the server',
    onkeydown: (e) => {
      if (e.key === 'Enter') { send(); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); history(-1); }
      if (e.key === 'ArrowDown') { e.preventDefault(); history(1); }
    },
  });

  let sent = prefs.get('console.history', []);
  let histIdx = sent.length;

  function history(delta) {
    if (!sent.length) return;
    histIdx = Math.max(0, Math.min(sent.length, histIdx + delta));
    cmd.value = sent[histIdx] ?? '';
    cmd.setSelectionRange(cmd.value.length, cmd.value.length);
  }

  async function send() {
    const line = cmd.value.trim();
    if (!line) return;
    cmd.value = '';
    sent = [...sent.filter((s) => s !== line), line].slice(-40);
    histIdx = sent.length;
    prefs.set('console.history', sent);
    try {
      await api.post(`/api/instances/${ctx.id}/command`, { command: line });
      push([`> ${line}`], true);
    } catch (e) {
      toastError(e, 'The server did not accept that');
    }
  }

  function matches(line) {
    if (query && !line.toLowerCase().includes(query.toLowerCase())) return false;
    if (filter === 'all') return true;
    const l = levelOf(line);
    if (filter === 'errors') return l === 'err';
    if (filter === 'warnings') return l === 'err' || l === 'warn';
    if (filter === 'chat') return l === 'chat';
    return true;
  }

  function repaint() {
    clear(term);
    const shown = lines.filter(matches);
    for (const line of shown) term.append(renderLine(line));
    countPill.textContent = query || filter !== 'all'
      ? `${shown.length} of ${lines.length}` : `${lines.length} lines`;
    if (follow) term.scrollTop = term.scrollHeight;
  }

  function renderLine(line) {
    const cls = levelOf(line);
    return h(`div.ln${cls ? `.${cls}` : ''}`, line);
  }

  function push(fresh, local) {
    if (!fresh.length) return;
    lines.push(...fresh);
    // The buffer is unbounded on the server side but the DOM is not: past a
    // few thousand nodes scrolling stutters, and nobody reads line 12,000.
    if (lines.length > 3000) lines = lines.slice(-2500);
    const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    for (const line of fresh) {
      if (matches(line)) term.append(renderLine(line));
    }
    while (term.childElementCount > 3000) term.firstElementChild.remove();
    countPill.textContent = `${lines.length} lines`;
    if (follow && atBottom) term.scrollTop = term.scrollHeight;
    if (local) term.scrollTop = term.scrollHeight;
  }

  term.addEventListener('scroll', () => {
    const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    if (atBottom !== follow) {
      follow = atBottom;
      followBtn.setAttribute('aria-pressed', String(follow));
      mount(followBtn, icon(follow ? 'eye' : 'minus', 13),
        h('span', follow ? 'Following' : 'Paused'));
    }
  }, { passive: true });

  const followBtn = h('button.btn.sm.ghost', {
    'aria-pressed': 'true',
    onclick: () => {
      follow = !follow;
      if (follow) term.scrollTop = term.scrollHeight;
      followBtn.setAttribute('aria-pressed', String(follow));
      mount(followBtn, icon(follow ? 'eye' : 'minus', 13),
        h('span', follow ? 'Following' : 'Paused'));
    },
  }, icon('eye', 13), h('span', 'Following'));

  function connect() {
    source?.close();
    source = new EventSource(`/api/instances/${ctx.id}/console/stream`);
    source.onmessage = (event) => {
      let frame;
      try { frame = JSON.parse(event.data); } catch { return; }
      if (frame.event === 'reset') {
        // Crafty's buffer is a ring; when it wraps, the tail no longer starts
        // where we left off and the honest answer is to repaint.
        lines = [];
        clear(term);
        return;
      }
      if (frame.event === 'error') {
        statusPill.className = 'pill bad';
        statusPill.textContent = 'stream error';
        return;
      }
      if (frame.source) {
        sourcePill.textContent = frame.source === 'buffer'
          ? "Crafty's live buffer" : 'logs/latest.log';
        sourcePill.title = frame.source === 'buffer'
          ? 'Crafty started this server and is holding its output'
          : 'Read from the log file — Crafty did not start this process, or '
            + 'was restarted since';
      }
      if (typeof frame.running === 'boolean') {
        statusPill.className = `pill ${frame.running ? 'ok' : 'dead'}`;
        statusPill.textContent = frame.running ? 'running' : 'stopped';
        cmd.disabled = !frame.running;
        cmd.placeholder = frame.running
          ? 'Type a command and press Enter (no leading /)'
          : 'The server is not running';
      }
      if (frame.event === 'lines') push(frame.lines || []);
    };
    source.onerror = () => {
      if (closed) return;
      statusPill.className = 'pill warn';
      statusPill.textContent = 'reconnecting';
      // EventSource retries on its own; this only reports it.
    };
  }

  mount(node,
    h('div.listbar',
      statusPill, sourcePill, countPill,
      h('input.inp', {
        type: 'search', placeholder: 'Filter lines…', 'data-keep': 'console-filter',
        'aria-label': 'Filter console lines',
        style: { maxWidth: '200px', minHeight: '36px' },
        oninput: debounce((e) => { query = e.target.value; repaint(); }, 150),
      }),
      segmented([
        { value: 'all', label: 'All' },
        { value: 'warnings', label: 'Warnings' },
        { value: 'errors', label: 'Errors' },
        { value: 'chat', label: 'Chat' },
      ], filter, (v) => {
        filter = v; prefs.set('console.filter', v); repaint();
      }, 'Filter by severity'),
      h('div.grow'),
      followBtn,
      h('button.btn.icon.sm.ghost', {
        'aria-label': 'Copy everything shown', title: 'Copy',
        onclick: () => copyText(lines.filter(matches).join('\n'), 'Console'),
      }, icon('copy', 14)),
      h('button.btn.icon.sm.ghost', {
        'aria-label': 'Clear the view', title: 'Clear the view (not the log)',
        onclick: () => { lines = []; clear(term); countPill.textContent = '0 lines'; },
      }, icon('trash', 14)),
    ),
    term,
    h('div.term-bar',
      icon('chevronRight', 15),
      cmd,
      h('button.btn.sm.primary', {
        onclick: send, disabled: !ctx.running,
      }, hl(), h('span', 'Send')),
    ),
    h('p.muted', { style: { fontSize: '12px', marginTop: '10px' } },
      'Commands go straight to the server\'s standard input, exactly as if '
      + 'they were typed at its console. No leading slash. Up and down arrows '
      + 'walk back through what you have sent.'),
  );

  // Seed with a snapshot so the pane is not empty for the first poll.
  try {
    const snap = await api.get(`/api/instances/${ctx.id}/console`);
    lines = snap.lines || [];
    repaint();
    sourcePill.textContent = snap.source === 'buffer'
      ? "Crafty's live buffer" : 'logs/latest.log';
    statusPill.className = `pill ${snap.running ? 'ok' : 'dead'}`;
    statusPill.textContent = snap.running ? 'running' : 'stopped';
    if (!lines.length) {
      term.append(h('div.ln.info',
        'No output yet. If this server has never started, its log does not '
        + 'exist — a boot test from Overview is the quickest way to find out '
        + 'what it does.'));
    }
  } catch (e) {
    term.append(h('div.ln.err', `Could not read the console: ${e.message}`));
  }
  connect();

  return {
    node,
    dispose() { closed = true; source?.close(); },
    onRunningChanged(running) {
      cmd.disabled = !running;
      statusPill.className = `pill ${running ? 'ok' : 'dead'}`;
      statusPill.textContent = running ? 'running' : 'stopped';
    },
  };
}
