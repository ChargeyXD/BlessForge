/* ============================================================
   Tune — heap, JVM flags, the port, and server.properties.

   The heap ceiling comes from the host, not from the pack: a
   pack asking for 8 GB on a box with 5 GB free dies at startup
   with no log at all, and that is the single most confusing
   failure a self-hoster meets. Where the proposal had to
   overrule the pack, it says so.

   The port is written to server.properties *and* the server
   record together. Setting only one leaves a server that runs
   perfectly and cannot be reached, or shows as offline.

   ------------------------------------------------------------
   TWO THINGS WORTH KNOWING BEFORE EDITING THIS FILE

   1. The heap control is a `.sldr`, not a `<input type=range>`
      in a `.field`. Its `input` handler updates three text
      nodes and two custom properties — it must NEVER call
      paint(). Re-rendering the panel under an active pointer
      drag destroys the element being dragged, and that is
      exactly what made the old slider feel broken.

   2. The assistant may have moved some of these numbers. The
      GET returns the deterministic plan plus whatever advice
      was already cached, so this screen always paints at
      the node's speed; the model is only ever reached by the POST
      to /optimize/advice, fired AFTER the first paint and
      folded in when it lands. Every number the assistant moved
      is named on the ofuda and marked on its own row — silent
      means uninterrupting, not unaccountable. (An ofuda, not an
      ema: `.ema` is already the hanging wish plaque on the
      Racks screen, and this is a paper tag recording a
      decision.)
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, bytes, num, empty,
  loadingFox, debounce, segmented, frag, confirmDialog, field, toggle,
} from '../core.js';
import { runAwait } from '../jobs.js';

export async function render(ctx) {
  const node = h('div');
  const host = h('div');
  // Inside the host, which the first paint replaces; beside it, the
  // loading line stayed on the page for good.
  mount(node, host);
  mount(host, loadingFox('Measuring the host'));

  let plan = null;
  let props = null;
  let port = null;
  let heap = 0;
  const flagState = new Map();
  const propEdits = new Map();
  let propGroup = '';
  let propQuery = '';

  // What the user has taken over. Advice that lands after somebody has
  // started editing must never overwrite their choice.
  let touchedHeap = false;
  let touchedFlags = false;
  let adviceState = 'idle';       // idle | thinking | done
  let adviceError = '';
  let slider = null;
  const ofudaHost = h('div');
  // "How it is running": its own host, loaded after the first paint so the
  // reading (which reads files off the node) never holds the tab up.
  const perfHost = h('div');
  let perf = null;
  const perfPick = new Set();
  let pregenRadius = 2000;
  // Live echoes of the heap number, updated by the slider without a
  // repaint: the Proposed tile and the Apply summary both used to go stale
  // the moment the slider moved.
  let echoes = [];

  async function load() {
    try {
      [plan, props, port] = await Promise.all([
        api.get(`/api/instances/${ctx.id}/optimize`),
        api.get(`/api/instances/${ctx.id}/properties`),
        api.get(`/api/instances/${ctx.id}/port`),
      ]);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    heap = plan.memory?.heap_gb || 4;
    for (const f of plan.flags || []) flagState.set(f.flag, f.enabled ?? f.applied);
    paint();
  }

  /* --- the assistant, in the background ------------------------------
     Never awaited by load(). If it is slow, unreachable, off or talking
     nonsense, this screen is already painted and stays painted. */
  async function askAdvice(refresh = false) {
    const advice = plan.advice || {};
    if (!advice.enabled) return;
    if (!refresh && advice.state === 'ready') { adviceState = 'done'; return; }
    adviceState = 'thinking';
    adviceError = '';
    paintOfuda();
    try {
      const res = await api.post(`/api/instances/${ctx.id}/optimize/advice`,
        refresh ? { refresh: true } : {});
      adviceState = 'done';
      if (res?.plan) adopt(res.plan);
      else paintOfuda();
    } catch (e) {
      // Deliberately silent: no toast. The deterministic numbers on screen
      // are correct, and a toast would make a non-event look like a
      // failure. It is still recorded, because the ofuda has to be able to
      // say why nothing changed.
      adviceState = 'done';
      adviceError = e?.message || 'the request failed';
      paintOfuda();
    }
  }

  function adopt(next) {
    // Never yank the control out from under a live drag.
    if (slider?.busy()) { setTimeout(() => adopt(next), 300); return; }
    if (!touchedHeap) heap = next.memory?.heap_gb || heap;
    const keep = new Map(flagState);
    plan = next;
    flagState.clear();
    for (const f of plan.flags || []) {
      flagState.set(f.flag,
        touchedFlags && keep.has(f.flag) ? keep.get(f.flag)
          : (f.enabled ?? f.applied));
    }
    paint();
  }

  function paint() {
    const hostSpecs = plan.host || {};
    const mem = plan.memory || {};
    const current = plan.current || {};
    echoes = [];

    // On the unmeasured path `ceiling_gb` is not a ceiling — it is the
    // configured default wearing one, and capping the slider there is what
    // stopped anyone following the advice directly underneath it ("pick a
    // number you know the machine has"). With nothing measured there is
    // nothing to strain against either, so the rope simply runs longer.
    // The heap is the person's to set, up to everything the machine has:
    // the ceiling is a suggestion shown beside it, never the end of the
    // slider. The container follows the heap (app/heap.py), so the limit it
    // will get is shown live under it.
    const total = Number(hostSpecs.total_ram_gb) || 0;
    const top = mem.unmeasured || !total
      ? Math.max(16, Math.ceil(heap * 2)) : Math.max(1, Math.floor(total * 2) / 2);
    const box = plan.container || {};
    const containerLine = h('div.sldr-help');
    const sizeNote = () => {
      const o = box.overhead || {};
      const mb = heap * 1024;
      const byMods = Math.min(o.mods_max_mb ?? 3072,
        (o.min_mb ?? 768) + (o.per_mod_mb ?? 2.5) * (o.mod_count ?? plan.mod_count ?? 0));
      const extra = Math.max(o.min_mb ?? 768, mb * (o.share ?? 0.2), byMods);
      const gb = (mb + extra) / 1024;
      const used = total - (Number(hostSpecs.available_ram_gb) || total);
      const warn = total && gb > total * (box.warn_share ?? 0.85);
      containerLine.className = warn ? 'note warn' : 'sldr-help';
      containerLine.textContent = `Container: ${gb.toFixed(1)} GB — the heap plus `
        + `${(extra / 1024).toFixed(1)} GB for Java around it`
        + (o.mod_count ? ` with ${o.mod_count} mods` : '') + '.'
        + (warn ? ` This machine has ${total} GB`
          + (used >= 0.5 ? `; other apps use about ${used.toFixed(0)}` : '')
          + '. It is set as asked; it may be squeezed.' : '');
    };
    sizeNote();
    slider = shrineRange({
      label: 'Heap',
      min: 1,
      max: top,
      snap: 0.5,
      value: heap,
      ariaLabel: 'Heap size in gigabytes',
      format: (v) => `${v} GB`,
      capLabel: mem.unmeasured || !total ? 'no measured total' : `machine ${total} GB`,
      oninput: (v) => {
        heap = v;
        touchedHeap = true;
        for (const n of echoes) {
          n.textContent = n.dataset.fmt.replace('%', String(v));
        }
        sizeNote();
      },
    });

    mount(host,
      h('div.split.wide-aside',
        h('div', { style: { display: 'grid', gap: '18px' } },
          perfHost,
          /* --- memory ------------------------------------------ */
          h('div.panel',
            h('header', icon('cpu', 15), h('h3', 'Memory'),
              h('span.sp', `${hostSpecs.total_ram_gb ?? '?'} GB host`)),
            h('div.pad',
              mem.unmeasured
                ? h('div.note.bad', { style: { marginBottom: '14px' } },
                  h('b', "This machine's memory could not be read"),
                  (mem.warnings || []).join(' '))
                : (mem.warnings || []).length
                  ? h('div.note.warn', { style: { marginBottom: '14px' } },
                    h('b', mem.capped_by_host
                      ? 'The pack asks for more than this machine can give'
                      : 'Worth knowing'),
                    h('ul', { style: { margin: '4px 0 0 18px' } },
                      ...mem.warnings.map((w) => h('li', w))))
                  : null,
              h('div.kpis', { style: { marginBottom: '16px' } },
                stat('Host RAM',
                  hostSpecs.total_ram_gb
                    ? `${hostSpecs.total_ram_gb} GB` : 'unknown',
                  hostSpecs.total_ram_gb
                    ? `${hostSpecs.available_ram_gb ?? '?'} GB free now`
                    : 'set HOST_RAM_GB',
                  hostSpecs.total_ram_gb ? '' : 'warn'),
                stat('CPUs', String(hostSpecs.cpu_count ?? '?'),
                  hostSpecs.source === 'env' ? 'declared, not measured'
                    : 'measured in this container'),
                stat('Currently', current.xmx_mb
                  ? `${(current.xmx_mb / 1024).toFixed(1)} GB`
                  : 'not set', current.exists
                  ? 'from user_jvm_args.txt' : 'from the launch command'),
                stat('Proposed', echo('% GB'),
                  mem.source === 'ai' ? 'chosen by the assistant'
                    : (mem.basis || `${plan.mod_count} mods`),
                  mem.unmeasured ? 'warn' : 'rose'),
              ),
              slider.node,
              containerLine,
              h('div.sldr-help', mem.unmeasured
                ? "No ceiling could be computed, because this host's memory "
                  + 'is unreadable from here. Pick a number you know the '
                  + 'machine has.'
                : `Suggested up to ${mem.ceiling_gb} GB — what is free beyond the `
                  + `${mem.reserve_gb} GB kept for the OS and whatever else is on `
                  + 'this box. A suggestion, not a limit: the heap you set is the '
                  + 'one the server gets, and its container is resized with it.'),
              h('div', { style: { marginTop: '14px' } }, ofudaHost),
              !plan.jvm_file_supported
                ? h('div.note', { style: { marginTop: '10px' } }, plan.note)
                : null,
            )),

          /* --- flags -------------------------------------------- */
          h('div.panel',
            h('header', icon('bolt', 15), h('h3', 'JVM flags'),
              h('span.sp', `${(plan.flags || []).filter((f) =>
                flagState.get(f.flag)).length} of ${(plan.flags || []).length} on`)),
            h('div.pad',
              h('p.muted', { style: { fontSize: '13px', marginBottom: '12px' } },
                'Aikar\'s set, adjusted for this heap size and core count. '
                + 'Each one says what it does; anything you untick is left '
                + 'untouched rather than removed.'),
              h('div', { style: { display: 'grid', gap: '8px' } },
                ...(plan.flags || []).map(flagRow)),
              current.extra_flags?.length
                ? h('div.note', { style: { marginTop: '12px' } },
                  h('b', 'Flags already set that this proposal does not manage'),
                  current.extra_flags.join(' '))
                : null,
            )),

          /* --- properties --------------------------------------- */
          h('div.panel',
            h('header', icon('sliders', 15), h('h3', 'server.properties'),
              h('span.sp', `${props.count} keys`)),
            h('div.pad',
              h('div.listbar', { style: { position: 'static', marginBottom: '12px' } },
                h('input.inp.grow', {
                  type: 'search', placeholder: 'Search keys…', 'data-keep': 'prop-filter',
                  'aria-label': 'Search server.properties keys',
                  oninput: debounce((e) => { propQuery = e.target.value; paint(); }, 160),
                  value: propQuery,
                }),
                h('select.inp', {
                  style: { maxWidth: '200px' }, 'aria-label': 'Group',
                  onchange: (e) => { propGroup = e.target.value; paint(); },
                },
                  h('option', { value: '' }, 'Every group'),
                  ...(props.groups || []).map((g) => h('option', {
                    value: g.name, selected: g.name === propGroup,
                  }, `${g.name} (${g.count})`)))),
              h('div', { style: { display: 'grid', gap: '10px' } },
                ...visibleProps().slice(0, 200).map(propRow)),
              propEdits.size
                ? h('div.btnrow', { style: { marginTop: '14px' } },
                  h('button.btn.sm.primary', { onclick: saveProps },
                    hl(), icon('save', 13),
                    h('span', `Save ${propEdits.size} change`
                      + `${propEdits.size > 1 ? 's' : ''}`)),
                  h('button.btn.sm.ghost', {
                    onclick: () => { propEdits.clear(); paint(); },
                  }, h('span', 'Discard')))
                : null,
            )),
        ),

        /* --- aside -------------------------------------------- */
        h('div.sticky', { style: { display: 'grid', gap: '18px' } },
          h('div.panel',
            h('header', icon('link', 15), h('h3', 'Port')),
            h('div.pad',
              port.mismatch
                ? h('div.note.bad', { style: { marginBottom: '12px' } },
                  h('b', 'The server record and server.properties disagree'),
                  port.note)
                : null,
              h('div.field',
                h('label', 'Server port'),
                h('input.inp', {
                  id: 'portinput', type: 'number', min: 1024, max: 65535,
                  value: port.record_port ?? 25565, inputMode: 'numeric',
                }),
                h('div.help',
                  `Server record: ${port.record_port ?? '—'} · `
                  + `server.properties: ${port.properties_port ?? '—'}`)),
              port.in_use_by_others?.length
                ? h('p.muted', { style: { fontSize: '12px' } },
                  'Also in use: '
                  + port.in_use_by_others.map((o) => `${o.name} :${o.port}`)
                    .join(', '))
                : null,
              port.taken_by_apps?.length
                ? h('p.muted', { style: { fontSize: '12px' } },
                  'Taken by another app on this machine: '
                  + port.taken_by_apps.map((r) => (r.from === r.to
                    ? `${r.from}` : `${r.from}–${r.to}`) + ` (${r.by})`).join(', '))
                : null,
              h('label.check', { style: { display: 'flex', gap: '8px', margin: '8px 0' } },
                h('input', {
                  type: 'checkbox', checked: !!port.auto,
                  onchange: (e) => setAutoPort(e.target.checked),
                }),
                h('span', 'Port: automatic — at each start, this port if it is '
                  + 'free, otherwise the first free one (in the range when it can)')),
              h('label.check', { style: { display: 'flex', gap: '8px', margin: '8px 0' } },
                h('input', {
                  type: 'checkbox', checked: !!port.auto_start,
                  onchange: (e) => setAutoStart(e.target, e.target.checked),
                }),
                h('span', 'Start by itself — when this machine or its node comes up, '
                  + 'and again after a crash. One server per port.')),
              h('button.btn.sm.block', { onclick: savePort },
                hl(), icon('save', 13), h('span', 'Set the port in both places')),
              h('div.field', { style: { marginTop: '12px' } },
                h('label', 'Pin to cores (optional)'),
                h('input.inp', {
                  id: 'cpusetinput', placeholder: 'e.g. 0-3', inputMode: 'numeric',
                  value: plan.container?.cpuset || '',
                  onchange: (e) => saveCpuset(e.target.value),
                }),
                h('div.help', 'Keeps a big pack on these cores. Blank: any core.')),
              h('p.muted', { style: { fontSize: '11.5px', marginTop: '10px' } },
                `This node's edge forwards game ports `
                + `${(port.published_range || []).join('–')}. A port outside `
                + 'that binds on the node and is unreachable from outside.'),
            )),

          h('div.panel',
            h('header', icon('save', 15), h('h3', 'Apply')),
            h('div.pad', { style: { display: 'grid', gap: '10px' } },
              h('div.mono.muted', { style: { fontSize: '11.5px' } },
                echo('% GB heap'),
                ` · ${(plan.flags || []).filter((f) =>
                  flagState.get(f.flag)).length} flags`),
              h('button.btn.primary.block', { onclick: apply },
                hl(), icon('bolt', 14), h('span', 'Apply memory and flags')),
              h('p.muted', { style: { fontSize: '11.5px' } },
                'The server has to be restarted for JVM changes to take '
                + 'effect. Nothing here touches your world.'),
              h('hr'),
              h('button.btn.sm.ghost.block', {
                onclick: applyRecommendedProps,
                disabled: !(plan.properties || []).some((p) => !p.applied),
              }, icon('check', 13),
                h('span', `Apply ${(plan.properties || [])
                  .filter((p) => !p.applied).length} performance settings`)),
              ...(plan.properties || []).filter((p) => !p.applied).slice(0, 5)
                .map(proposedProp),
            )),
        ),
      ),
    );
    paintOfuda();
  }

  /* A text node that follows the heap without a repaint. `fmt` uses %
     as the placeholder so the same mechanism serves "6 GB" and
     "6 GB heap". */
  /* --- how it is running ------------------------------------- */

  /* The reading of what is in place (app/perf.py): JVM settings that stop
     the server or slow it, the pack's optimisation mods and what they
     change, the Paper/Spigot settings worth having, and spark's numbers.
     Faults are ticked for fixing by default; taste is not. */
  async function loadPerf() {
    mount(perfHost, h('div.panel', h('div.pad', h('div.skel', { style: { height: '64px' } }))));
    try {
      perf = await api.get(`/api/instances/${ctx.id}/perf`);
    } catch (e) {
      mount(perfHost, h('div.note.warn', `Could not read how it is running: ${e.message}`));
      return;
    }
    perfPick.clear();
    for (const i of perf.jvm.issues) if (i.fix && i.level === 'bad') perfPick.add(issueKey(i));
    paintPerf();
  }

  function issueKey(i) { return `jvm:${i.title}`; }

  function paintPerf() {
    const issues = perf.jvm.issues || [];
    const tone = (l) => ({ bad: 'bad', warn: 'warn', info: 'ghost', ok: 'ok' }[l] || 'ghost');
    const fixable = issues.filter((i) => i.fix);
    const paper = perf.paper || [];
    const modSettings = perf.mod_settings || [];
    const settings = paper.concat(modSettings);
    const s = perf.spark;
    mount(perfHost, h('div.panel',
      h('header', icon('activity', 15), h('h3', 'How it is running'),
        h('span.sp', perf.counts.bad ? `${perf.counts.bad} fault(s)`
          : perf.counts.warn ? `${perf.counts.warn} to look at` : 'nothing holding it back')),
      h('div.pad', { style: { display: 'grid', gap: '12px' } },
        s ? h('div.chiprow',
          s.tps ? pill(`${s.tps['1m'] ?? s.tps['10s']} TPS`,
            (s.tps['1m'] ?? 20) >= 19.5 ? 'ok' : 'warn') : null,
          s.mspt ? pill(`${s.mspt.p95} ms per tick (p95)`,
            s.mspt.p95 <= 40 ? 'ok' : s.mspt.p95 <= 50 ? 'warn' : 'bad') : null,
          s.memory_mb ? pill(`heap ${s.memory_mb.percent}% used`, 'ghost') : null,
          h('span.muted', { style: { fontSize: '12px' } },
            'measured by spark \u2014 Diagnose \u203a Profiler'))
          : null,
        issues.length
          ? h('div.modlist',
            ...issues.map((i) => h('div.modrow',
              i.fix ? h('input', {
                type: 'checkbox', checked: perfPick.has(issueKey(i)),
                'aria-label': `Fix: ${i.title}`,
                onchange: (e) => {
                  if (e.target.checked) perfPick.add(issueKey(i));
                  else perfPick.delete(issueKey(i));
                },
              }) : h('span', { style: { width: '16px' } }),
              h('div.who',
                h('div.nm', i.title),
                h('div.fn', i.detail),
                h('div.tags', pill(i.level === 'bad' ? 'fault' : i.level, tone(i.level)),
                  i.arg ? pill(i.arg, 'ghost') : null)))))
          : h('div.note.ok', h('b', 'The JVM settings are sound'),
            `Read from the ${perf.jvm.source}: nothing removed, conflicting or `
            + 'oversized for this server.'),
        (perf.mods || []).length
          ? h('div',
            h('div.muted', { style: { fontSize: '12px', marginBottom: '6px' } },
              'Optimisation mods in this pack'),
            h('div', { style: { display: 'grid', gap: '6px' } },
              ...perf.mods.map((m) => h('div', { style: { fontSize: '13px' } },
                h('b', m.name), ' \u2014 ', h('span.muted', m.what)))))
          : null,
        ...(perf.notes || []).map((n) => h('div.note', n)),
        settingList('Paper and Spigot settings', paper),
        settingList('Switches in the optimisation mods', modSettings),
        (fixable.length || settings.some((x) => !x.applied))
          ? h('div.btnrow', h('button.btn.sm.primary', { onclick: applyPerf },
            hl(), icon('check', 13), h('span', 'Apply the ticked fixes')),
          h('span.muted', { style: { fontSize: '12px' } }, 'Takes effect at the next start.'))
          : null,
        ...(perf.actions || []).map((a) => h('div.card', { style: { padding: '10px 12px' } },
          h('div', h('b', a.title), h('div.muted', { style: { fontSize: '12.5px' } }, a.detail)),
          a.id === 'pregen'
            ? h('div.btnrow',
              segmented([
                { value: 1000, label: '1k' }, { value: 2000, label: '2k' },
                { value: 4000, label: '4k' }, { value: 8000, label: '8k' },
              ], pregenRadius, (v) => { pregenRadius = v; paintPerf(); }, 'Radius in blocks'),
              h('button.btn.sm', { disabled: !ctx.running, onclick: pregen },
                icon('compass', 13), h('span', `Pre-generate ${pregenRadius} blocks`)))
            : h('div.btnrow', h('button.btn.sm', {
              onclick: () => installTool(a.id === 'install_chunky' ? 'chunky' : 'spark'),
            }, icon('download', 13), h('span', a.id === 'install_chunky'
              ? 'Install Chunky' : 'Install spark'))))),
      )));
  }

  /* Settings that live in a config file: Paper/Spigot's, and the switches
     the optimisation mods ship turned off. Never ticked by default -- each
     is a trade, and the reason is written next to it. */
  function settingList(title, items) {
    if (!items.length) return null;
    return h('div',
      h('div.muted', { style: { fontSize: '12px', margin: '4px 0 6px' } }, title),
      h('div.modlist', ...items.map((x) => h('div.modrow',
        h('input', {
          type: 'checkbox', checked: perfPick.has(x.id) || false,
          disabled: x.applied, 'aria-label': `Set ${x.key}`,
          onchange: (e) => {
            if (e.target.checked) perfPick.add(x.id); else perfPick.delete(x.id);
          },
        }),
        h('div.who',
          h('div.nm', `${x.mod ? `${x.mod}: ` : ''}${x.key}: ${x.current} \u2192 ${x.value}`),
          h('div.fn', x.why),
          h('div.tags', pill(x.file.split('/').pop(), 'ghost'),
            x.applied ? pill('already set', 'ok') : null,
            x.gameplay ? pill('changes gameplay a little', 'warn') : null))))));
  }

  async function applyPerf() {
    const jvm = (perf.jvm.issues || []).filter((i) => i.fix && perfPick.has(issueKey(i)))
      .map((i) => i.fix);
    const yaml = (perf.paper || []).concat(perf.mod_settings || [])
      .filter((x) => perfPick.has(x.id)).map((x) => x.id);
    if (!jvm.length && !yaml.length) { toast('Nothing is ticked.', 'warn'); return; }
    try {
      const r = await api.post(`/api/instances/${ctx.id}/perf/apply`,
        { jvm_fixes: jvm, yaml_ids: yaml });
      toast(r.applied.length ? `${r.applied.length} change(s) written. They take effect `
        + 'at the next start.' : 'Nothing needed changing.', 'ok', { timeout: 8000 });
    } catch (e) { toastError(e); }
    loadPerf();
  }

  async function installTool(tool) {
    try {
      await runAwait(`/api/instances/${ctx.id}/perf/install`, { tool },
        { title: `Install ${tool}` });
      toast(`${tool} installed. It loads at the next start.`, 'ok', { timeout: 7000 });
    } catch (e) { toastError(e); }
    loadPerf();
  }

  async function pregen() {
    const ok = await confirmDialog({
      title: `Pre-generate ${pregenRadius} blocks around spawn?`,
      message: 'Chunky generates the terrain now, while nobody is exploring it. '
        + 'It uses CPU and disk while it runs; players can stay on. Cancelling '
        + 'the job pauses it, and running it again carries on.',
      confirmLabel: 'Pre-generate',
    });
    if (!ok) return;
    try {
      await runAwait(`/api/instances/${ctx.id}/pregen`, { radius: pregenRadius },
        { title: `Pre-generating ${pregenRadius} blocks` });
      toast('Pre-generation finished.', 'ok');
    } catch (e) { toastError(e); }
  }

  function echo(fmt) {
    const span = h('span', { dataset: { fmt } }, fmt.replace('%', String(heap)));
    echoes.push(span);
    return span;
  }

  function stat(label, value, sub, tone = '') {
    return h('div.kpi', h('div.lbl', label),
      h(`div.v${tone ? `.${tone}` : ''}`, { style: { fontSize: '19px' } }, value),
      h('div.sub', sub));
  }

  /* --- provenance ----------------------------------------------------
     One plaque, at most one line per number that moved. Not a
     transcript: nobody opened this screen to read the model's prose. */
  function paintOfuda() {
    const a = plan.advice || {};
    if (adviceState === 'thinking') {
      mount(ofudaHost, h('div.ofuda.thinking',
        h('span.ofuda-seal', { 'aria-hidden': 'true' }),
        h('div', h('div.ofuda-hd', 'The assistant is reading this pack'),
          h('div.sldr-help', 'These numbers are already safe to apply. If it '
            + 'finds a better one it will be swapped in and explained here.'))));
      return;
    }

    const changes = a.changes || [];
    if (a.state === 'ready' && changes.length) {
      mount(ofudaHost, h('div.ofuda',
        h('span.ofuda-seal', { 'aria-hidden': 'true' }),
        h('div', { style: { minWidth: 0 } },
          h('div.ofuda-hd', `Adjusted by the assistant${a.model
            ? ` · ${a.model}` : ''}`),
          a.summary ? h('div.ofuda-sum', a.summary) : null,
          h('ul.ofuda-lines', ...changes.map(ofudaLine)),
          h('div.ofuda-foot',
            `${changes.length} number${changes.length > 1 ? 's' : ''} changed`
            + `${(a.rejected || []).length
              ? ` · ${a.rejected.length} suggestion`
                + `${a.rejected.length > 1 ? 's' : ''} refused as unsafe` : ''}`
            + ' · everything else came from the deterministic optimizer'),
          (a.rejected || []).length
            ? h('div.ofuda-foot', { title: (a.rejected || [])
              .map((r) => `${r.value}: ${r.why}`).join('\n') },
            'Hover for what was refused.')
            : null,
        )));
      return;
    }

    const why = a.state === 'off'
      ? 'Assisted tuning is off in Settings.'
      : a.state === 'ready'
        ? 'The assistant read this pack and found nothing worth changing.'
        : adviceError
          ? `The assistant could not be asked (${adviceError}).`
          : a.reason
          ? `The assistant was not usable (${a.reason}).`
          : 'The assistant has not been asked about this pack.';
    mount(ofudaHost, h('div.ofuda.plain',
      h('span.ofuda-seal', { 'aria-hidden': 'true' }),
      h('div', h('div.ofuda-hd', 'Every number here came from the '
        + 'deterministic optimizer'),
      h('div.sldr-help', why))));
  }

  function ofudaLine(c) {
    return h('li.ofuda-line',
      h('span.what', c.what),
      h('span.move', h('s', String(c.from)), String(c.to)),
      c.why ? h('span.why', c.why) : null,
      c.note ? h('span.ofuda-note', c.note) : null);
  }

  function flagRow(f) {
    const on = !!flagState.get(f.flag);
    const fromAi = f.source === 'ai';
    return h('label', {
      style: { display: 'flex', gap: '11px', alignItems: 'flex-start',
        border: `2px solid ${fromAi ? 'var(--gold)' : 'var(--rule)'}`,
        padding: '9px 11px', cursor: 'pointer',
        background: on ? 'var(--sakura-soft)' : 'var(--paper-2)' },
    },
      h('input', {
        type: 'checkbox', checked: on,
        onchange: (e) => {
          flagState.set(f.flag, e.target.checked);
          touchedFlags = true;
          paint();
        },
      }),
      h('div', { style: { minWidth: 0 } },
        h('div.mono.wrapany', { style: { fontSize: '12px', fontWeight: 700 } },
          f.flag),
        h('div.muted', { style: { fontSize: '12px' } }, f.why || f.description || ''),
        f.applied ? pill('already set', 'ok') : null,
        fromAi ? pill('assistant', 'gold',
          'The deterministic optimizer proposed something else here') : null,
        fromAi && f.optimizer_flag && f.optimizer_flag !== f.flag
          ? h('span.src-why', `The optimizer had ${f.optimizer_flag}.`)
          : null));
  }

  function proposedProp(p) {
    return h('div.mono.muted', { style: { fontSize: '11px' } },
      `${p.key} = ${p.value}`,
      p.source === 'ai' ? pill('assistant', 'gold') : null,
      h('div', { style: { opacity: .75 } }, p.ai_why || p.why || ''),
      p.source === 'ai' && p.optimizer_value !== p.value
        ? h('span.src-why', `The optimizer had ${p.optimizer_value}.`)
        : null);
  }

  function visibleProps() {
    const q = propQuery.trim().toLowerCase();
    return (props.items || []).filter((p) => {
      if (propGroup && p.group !== propGroup) return false;
      if (!q) return true;
      return p.key.includes(q) || (p.description || '').toLowerCase().includes(q);
    });
  }

  function propRow(p) {
    const edited = propEdits.has(p.key);
    const value = edited ? propEdits.get(p.key) : p.value;
    const guard = p.guarded;

    let control;
    if (guard) {
      control = h('div',
        h('input.inp', { value, disabled: true, 'aria-label': p.key }),
        h('div.help', guard));
    } else if (p.type === 'bool') {
      control = h('label.switch',
        h('input', {
          type: 'checkbox', checked: String(value) === 'true',
          'aria-label': p.key,
          onchange: (e) => {
            propEdits.set(p.key, e.target.checked ? 'true' : 'false');
            paint();
          },
        }), h('span.track'), h('span', String(value)));
    } else if (p.choices?.length) {
      control = h('select.inp', {
        'aria-label': p.key,
        onchange: (e) => { propEdits.set(p.key, e.target.value); paint(); },
      }, ...p.choices.map((c) => h('option', {
        value: c, selected: String(c) === String(value),
      }, c)));
    } else {
      control = h('input.inp', {
        value, type: p.type === 'int' ? 'number' : 'text',
        inputMode: p.type === 'int' ? 'numeric' : null,
        'aria-label': p.key,
        oninput: (e) => { propEdits.set(p.key, e.target.value); },
      });
    }

    return h('div', {
      style: { display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 210px',
        gap: '12px', alignItems: 'center', padding: '8px 0',
        borderBottom: '1.5px solid var(--rule-2)' },
    },
      h('div', { style: { minWidth: 0 } },
        h('div.mono.wrapany', { style: { fontSize: '12.5px', fontWeight: 700 } },
          p.key,
          edited ? pill('changed', 'warn') : null,
          p.absent ? pill('not in the file', 'dead') : null,
          !p.known ? pill('added by a mod', 'plum') : null),
        p.description
          ? h('div.muted', { style: { fontSize: '12px' } }, p.description)
          : null),
      control);
  }

  /* --- saving --------------------------------------------------- */

  async function apply() {
    const flags = [...flagState.entries()].filter(([, on]) => on).map(([f]) => f);
    try {
      const res = await api.post(`/api/instances/${ctx.id}/optimize`,
        { heap_gb: heap, flags });
      toast(`Applied ${heap} GB and ${flags.length} flags. Restart for it to `
        + 'take effect.', 'ok', { timeout: 7000 });
      if (res.warnings?.length) {
        res.warnings.forEach((w) => toast(w, 'warn', { timeout: 9000 }));
      }
      touchedHeap = false;
      touchedFlags = false;
      await load();
      askAdvice();
    } catch (e) { toastError(e, 'Could not apply'); }
  }

  async function applyRecommendedProps() {
    const updates = {};
    for (const p of (plan.properties || [])) {
      if (!p.applied) updates[p.key] = p.value;
    }
    try {
      await api.post(`/api/instances/${ctx.id}/optimize`, { properties: updates });
      toast(`${Object.keys(updates).length} performance settings written.`, 'ok');
      await load();
      askAdvice();
    } catch (e) { toastError(e); }
  }

  async function saveProps() {
    const updates = Object.fromEntries(propEdits);
    try {
      const res = await api.post(`/api/instances/${ctx.id}/properties`,
        { updates });
      const saved = Object.keys(res.saved || {}).length;
      toast(`${saved} saved${res.rejected?.length
        ? `, ${res.rejected.length} rejected` : ''}. Restart to apply.`,
      res.rejected?.length ? 'warn' : 'ok');
      (res.rejected || []).forEach((r) =>
        toast(`${r.key}: ${r.reason}`, 'warn', { timeout: 8000 }));
      propEdits.clear();
      await load();
      askAdvice();
    } catch (e) { toastError(e); }
  }

  async function saveCpuset(cpuset) {
    try {
      const res = await api.post(`/api/instances/${ctx.id}/cpu`, { cpuset });
      toast(res.cpuset ? `Pinned to cores ${res.cpuset} from the next start.`
        : 'Not pinned: any core, from the next start.', 'ok');
    } catch (e) { toastError(e); }
  }

  async function setAutoStart(box, enabled) {
    try {
      await api.post(`/api/instances/${ctx.id}/autostart`, { enabled });
      toast(enabled ? 'Starts by itself when its node comes up, and after a crash.'
        : 'Starts only when someone starts it.', 'ok');
      await load();
    } catch (e) {
      box.checked = !enabled;
      toastError(e);
    }
  }

  async function setAutoPort(auto) {
    try {
      await api.post(`/api/instances/${ctx.id}/port`, { auto });
      toast(auto ? 'Port: automatic. A taken port is swapped for a free one at start.'
        : 'Port: fixed.', 'ok');
      await load();
    } catch (e) { toastError(e); }
  }

  async function savePort() {
    const value = Number(node.querySelector('#portinput').value);
    try {
      const res = await api.post(`/api/instances/${ctx.id}/port`, { port: value });
      toast(`Port ${res.port} written to ${res.updated.join(', ')}.`, 'ok');
      (res.warnings || []).forEach((w) => toast(w, 'warn', { timeout: 10000 }));
      await load();
    } catch (e) {
      if (e.status === 400 && /already used/i.test(e.message)) {
        const ok = await confirmDialog({
          title: 'That port is taken',
          message: e.message,
          confirmLabel: 'Use it anyway', danger: true,
        });
        if (!ok) return;
        try {
          const res = await api.post(`/api/instances/${ctx.id}/port`,
            { port: value, force: true });
          toast(`Port ${res.port} set — only one server can actually bind it.`,
            'warn');
          await load();
        } catch (err) { toastError(err); }
        return;
      }
      toastError(e);
    }
  }

  await load();
  if (plan) askAdvice();          // after the first paint, never before it
  loadPerf();                     // its own host; never holds the tab up
  return { node };
}

/* ============================================================
   THE SHRINE SLIDER
   ------------------------------------------------------------
   Duplicated verbatim in views/create.js. core.js is owned by
   another pass and cannot take a new shared helper, so this
   lives in both call sites rather than in one place — if a
   third screen ever needs it, that is the moment to promote it.

   What it fixes, and why each part is necessary:

     * The drag is continuous because `step` is a fifth of the
       snap unit AND because `input` does not repaint anything.
       Either one alone still stutters.
     * The value the app commits is always on the snap grid.
       `input` reports the snapped number while `--pct` follows
       the raw one, so the fill glides while the readout counts
       in halves; `change` settles the thumb onto the committed
       value.
     * Arrow keys move by the snap unit, not by the fine step —
       otherwise a keyboard user needs a hundred presses to
       cross the rope.
     * `--strain` is a smoothstep of how far past 70% of the
       range the value sits. Every visual is multiplied by it,
       so the effect grows rather than switching on.
   ============================================================ */

function shrineRange(o) {
  const min = Number(o.min);
  const max = Math.max(Number(o.max), min + (Number(o.snap) || 1));
  const snap = Number(o.snap) || 1;
  const fine = Math.max(snap / 5, 0.01);
  const fmt = o.format || ((v) => String(v));
  const onset = 0.7;                   // strain starts here, as a fraction

  const settle = (v) => {
    if (!Number.isFinite(v)) return min;
    const stepped = Math.round(v / snap) * snap;
    const bounded = Math.min(max, Math.max(min, stepped));
    return Math.round(bounded * 1000) / 1000;
  };

  let value = settle(Number(o.value));
  // A drag in progress, and when it was last seen alive. The timestamp is
  // the safety net: `busy()` gates a deferred re-render, so a `dragging`
  // flag that got stuck true would defer it forever.
  let dragging = false;
  let dragAt = 0;

  const val = h('b.sldr-val', fmt(value));
  const input = h('input.sldr-in', {
    type: 'range', min, max, step: fine, value,
    'aria-label': o.ariaLabel || o.label,
  });
  input.setAttribute('aria-valuetext', fmt(value));

  const wrap = h('div.sldr',
    h('div.sldr-head', h('span.sldr-lbl', o.label), val),
    h('div.sldr-lane', input, h('span.sldr-heat', { 'aria-hidden': 'true' })),
    h('div.sldr-scale', { 'aria-hidden': 'true' }),
    h('div.sldr-ends',
      h('span', fmt(min)),
      h('span.sldr-cap', sldrTorii(), o.capLabel || fmt(max))));

  // Notches every snap unit, thinned until they are countable.
  let every = snap;
  while ((max - min) / every > 26) every *= 2;
  wrap.style.setProperty('--tick', `${(100 * every) / (max - min)}%`);

  function draw(raw) {
    const span = max - min || 1;
    const t = Math.min(1, Math.max(0, (raw - min) / span));
    const over = Math.min(1, Math.max(0, (t - onset) / (1 - onset)));
    const strain = over * over * (3 - 2 * over);      // smoothstep
    wrap.style.setProperty('--pct', String(t));
    wrap.style.setProperty('--strain', strain.toFixed(3));
    wrap.classList.toggle('straining', strain > 0.02);
  }

  input.addEventListener('input', () => {
    dragAt = Date.now();
    const raw = Number(input.value);
    draw(raw);
    const next = settle(raw);
    if (next === value) return;
    value = next;
    val.textContent = fmt(value);
    input.setAttribute('aria-valuetext', fmt(value));
    o.oninput?.(value, raw);
  });
  input.addEventListener('change', () => {
    // Land the bell on the committed number rather than wherever the
    // pointer happened to let go.
    dragging = false;
    input.value = String(value);
    draw(value);
    o.onchange?.(value);
  });
  // Every listener is on the input itself. A window-level pointerup would
  // be tidier to reason about and would also leak one listener per repaint,
  // and this panel repaints on every property edit.
  input.addEventListener('pointerdown', () => {
    dragging = true;
    dragAt = Date.now();
  });
  for (const done of ['pointerup', 'pointercancel', 'lostpointercapture',
    'blur']) {
    input.addEventListener(done, () => { dragging = false; });
  }
  input.addEventListener('keydown', (e) => {
    const by = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1,
      PageDown: -4, PageUp: 4 }[e.key];
    if (by === undefined) return;
    e.preventDefault();
    const next = settle(value + by * snap);
    if (next === value) return;
    value = next;
    input.value = String(value);
    val.textContent = fmt(value);
    input.setAttribute('aria-valuetext', fmt(value));
    draw(value);
    o.oninput?.(value, value);
    o.onchange?.(value);
  });

  draw(value);
  return {
    node: wrap,
    value: () => value,
    busy: () => dragging && Date.now() - dragAt < 2500,
    set(v) {
      value = settle(v);
      input.value = String(value);
      val.textContent = fmt(value);
      input.setAttribute('aria-valuetext', fmt(value));
      draw(value);
    },
  };
}

/* The gate at the end of the rope. Drawn at 24x20 with chunky members
   rather than reusing the page ornament, which is built for 120x100 and
   disappears at this size. */
function sldrTorii() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 20');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = '<g fill="currentColor">'
    + '<rect x="2" y="0" width="20" height="1.6" rx=".6"/>'
    + '<rect x="0" y="2.4" width="24" height="3"/>'
    + '<rect x="3.5" y="7" width="17" height="2.2"/>'
    + '<rect x="6" y="9.2" width="3.2" height="10.8"/>'
    + '<rect x="14.8" y="9.2" width="3.2" height="10.8"/></g>';
  return svg;
}
