/* ============================================================
   Diagnose — what is wrong, why we think so, and what to do.

   Three passes, in increasing cost:

     checks       cheap state — EULA, Java, memory, mods on the
                  wrong game version
     log analysis latest.log and the newest crash report, with the
                  jars the trace actually implicates named
     deep scan    every jar downloaded and its declared
                  dependencies graphed, so missing and duplicate
                  mods surface before a launch

   Plus the reworked client-only scan, which is the same detector
   the pre-install review uses, pointed at what is on disk now.
   Every verdict shows the evidence that produced it and the
   points each piece of evidence contributed, because a verdict
   you cannot argue with is one you end up ignoring.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, num,
  empty, loadingFox, confirmDialog, modal, close, frag, segmented, copyText,
} from '../core.js';
import { run, runAwait } from '../jobs.js';

export async function render(ctx) {
  const node = h('div');
  const host = h('div');
  mount(node, host, loadingFox('Running the checks'));

  let diag = null;
  let ai = null;
  let scan = null;
  let view = 'findings';

  async function load(quiet) {
    if (!quiet) mount(host, loadingFox('Running the checks'));
    try {
      diag = await api.get(`/api/instances/${ctx.id}/diagnose`);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    api.get('/api/ai/status').then((s) => { ai = s; paint(); }).catch(() => {});
    paint();
  }

  function paint() {
    const findings = diag.findings || [];
    const crit = findings.filter((f) => f.severity === 'critical');
    const warn = findings.filter((f) => f.severity === 'warn');

    mount(host,
      h('div.listbar',
        segmented([
          { value: 'findings', label: `Findings ${findings.length}` },
          { value: 'client', label: 'Client-only scan' },
          { value: 'logs', label: 'Logs' },
        ], view, (v) => { view = v; paint(); }, 'Diagnose view'),
        h('div.grow'),
        h('button.btn.sm.ghost', { onclick: () => load() },
          icon('refresh', 13), h('span', 'Re-check')),
        h('button.btn.sm.ghost', { onclick: deepScan },
          icon('layers', 13), h('span', 'Deep scan')),
        ai?.available
          ? h('button.btn.sm.primary', { onclick: askAI },
            hl(), icon('flask', 13), h('span', 'Ask the assistant'))
          : null,
      ),

      view === 'findings' ? findingsView(findings, crit, warn)
        : view === 'client' ? clientView()
          : logsView(),
    );
  }

  /* --- findings ------------------------------------------------ */

  function findingsView(findings, crit, warn) {
    if (!findings.length) {
      return empty('parrot-vibing.gif', 'Nothing looks wrong',
        diag.has_logs
          ? 'The checks pass and nothing in the log stands out. If it is still '
            + 'misbehaving, a deep scan reads every jar\'s declared '
            + 'dependencies and builds the graph.'
          : 'This server has produced no log at all yet, so there was nothing '
            + 'to read. A boot test from Overview is the quickest way to find '
            + 'out what it actually does.',
        h('button.btn', { onclick: deepScan }, hl(), h('span', 'Deep scan')));
    }
    return frag(
      h('div.chiprow', { style: { marginBottom: '14px' } },
        crit.length ? pill(`${crit.length} critical`, 'bad') : null,
        warn.length ? pill(`${warn.length} warnings`, 'warn') : null,
        diag.crash?.culprits?.length
          ? pill(`${diag.crash.culprits.length} mods blamed by the crash`, 'plum')
          : null),
      diag.crash?.summary
        ? h('div.note.bad', { style: { marginBottom: '14px' } },
          h('b', 'The crash report says'), diag.crash.summary)
        : null,
      h('div', { style: { display: 'grid', gap: '12px' } },
        ...findings.map(findingCard)),
    );
  }

  function findingCard(f) {
    const tone = f.severity === 'critical' ? 'bad'
      : f.severity === 'warn' ? 'warn' : 'info';
    return h('div.card', { style: { borderColor: `var(--${
      tone === 'bad' ? 'danger' : tone === 'warn' ? 'gold' : 'info'})` } },
      h('header',
        pill(f.severity, tone),
        f.category ? pill(f.category, 'ghost') : null,
        h('h3', { style: { flex: 1, minWidth: 0 } }, f.title)),
      h('p.wrapany', f.detail),
      f.evidence
        ? h('details',
          h('summary', { style: { cursor: 'pointer', fontFamily: 'var(--mono)',
            fontSize: '11px', color: 'var(--faint)' } }, 'Evidence'),
          h('pre', {
            style: { background: 'var(--paper-2)', padding: '10px',
              overflow: 'auto', maxHeight: '220px', fontSize: '11.5px',
              fontFamily: 'var(--mono)', whiteSpace: 'pre-wrap',
              overflowWrap: 'anywhere', marginTop: '8px' },
          }, f.evidence))
        : null,
      f.fix ? h('div.btnrow', fixButton(f.fix)) : null);
  }

  function fixButton(fix) {
    const labels = {
      accept_eula: ['Accept the EULA', 'check'],
      set_java: ['Set the right Java', 'cpu'],
      set_ram: ['Fix the heap size', 'sliders'],
      disable_mods: [`Disable ${(fix.files || []).length} mod(s)`, 'minus'],
      fix_versions: ['Find compatible versions', 'layers'],
      search_dependency: ['Find the missing dependency', 'search'],
    };
    const [label, ico] = labels[fix.action] || [fix.action, 'wrench'];
    return h('button.btn.sm.primary', { onclick: () => applyFix(fix) },
      hl(), icon(ico, 13), h('span', label));
  }

  async function applyFix(fix) {
    try {
      if (fix.action === 'accept_eula') {
        await api.post(`/api/instances/${ctx.id}/fix/accept-eula`);
        toast('eula.txt rewritten in the exact form Crafty accepts.', 'ok');
      } else if (fix.action === 'set_java') {
        const r = await api.post(`/api/instances/${ctx.id}/fix/java`, {});
        toast(r.changed ? `Java ${r.java_major} pinned.`
          : (r.reason || 'No change needed.'), 'ok');
      } else if (fix.action === 'set_ram') {
        await api.post(`/api/instances/${ctx.id}/fix/set-ram`,
          { max_gb: fix.max_gb || 6 });
        toast('Heap size written.', 'ok');
      } else if (fix.action === 'disable_mods') {
        const files = fix.files || [];
        const ok = await confirmDialog({
          title: `Disable ${files.length} mod(s)?`,
          message: 'They are renamed to .jar.disabled — inert to the loader, '
            + 'and one click from coming back.',
          detail: files.join('\n'),
          confirmLabel: 'Disable them',
        });
        if (!ok) return;
        await api.post(`/api/instances/${ctx.id}/mods/bulk-toggle`,
          { files, enabled: false });
        toast(`${files.length} disabled.`, 'ok');
      } else if (fix.action === 'fix_versions') {
        const r = await api.post(`/api/instances/${ctx.id}/fix/versions`,
          { files: fix.files || [] });
        showVersionSuggestions(r);
        return;
      } else {
        toast('That fix has to be finished by hand — the Mods tab is where.',
          'warn');
        return;
      }
      await load(true);
    } catch (e) { toastError(e); }
  }

  function showVersionSuggestions(r) {
    modal({
      title: 'Compatible replacements',
      wide: true,
      body: (r.items || []).length
        ? h('div.modlist', ...r.items.map((i) => h('div.modrow',
          h('div.who',
            h('div.nm', i.name || i.file),
            h('div.fn', i.file),
            h('div.tags',
              i.suggestion
                ? pill(`→ ${i.suggestion.display_name}`, 'ok')
                : pill('no compatible build published', 'bad'))),
          h('div.acts', i.suggestion ? h('button.btn.xs.primary', {
            onclick: async () => {
              close();
              await run(`/api/instances/${ctx.id}/mods/add`, {
                source: i.source, project_id: i.project_id,
                file_id: i.suggestion.file_id, replace_file: i.file,
                name: i.name, with_dependencies: true,
              }, { title: `Swap ${i.name}`, onEnd: () => load(true) });
            },
          }, hl(), h('span', 'Swap')) : null))))
        : h('div.note', 'Nothing to suggest — none of those mods publish a '
          + 'build for this version.'),
      footer: h('button.btn.ghost', { onclick: () => close() },
        h('span', 'Close')),
    });
  }

  /* --- client-only scan ---------------------------------------- */

  function clientView() {
    if (ctx.plugins) {
      return h('div.note',
        h('b', 'This is a plugin server'),
        'The client-only question is a mod question — a Bukkit plugin only '
        + 'ever runs on the server. Use the Plugins tab\'s audit instead, '
        + 'which catches the actual mistake here: a mod jar dropped into '
        + 'plugins/, which does nothing at all and reports nothing.');
    }
    if (!scan) {
      return h('div.card', { style: { alignItems: 'flex-start' } },
        h('header', icon('shieldCheck', 16), h('h3', 'Client-only scan')),
        h('p', 'Reads every jar in mods/ and works out which of them have '
          + 'anything to run on a server. Same detector as the pre-install '
          + 'review, pointed at what is actually on disk — which is where it '
          + 'matters most, because that set includes jars added by hand and '
          + 'anything installed before these checks existed.'),
        h('ul', { style: { margin: '4px 0 0 18px', color: 'var(--mid)',
          fontSize: '13.5px' } },
          h('li', 'Every jar is checked against Modrinth by SHA-1, so the '
            + 'author\'s own statement is used where there is one'),
          h('li', 'Package layout, mixin targets and client-only library '
            + 'dependencies catch Forge and NeoForge mods, which declare no '
            + 'side at all'),
          h('li', 'Anything another staying mod requires is never disabled'),
          h('li', 'Nothing is deleted — a flagged jar is renamed to '
            + '.jar.disabled')),
        h('div.btnrow', { style: { marginTop: '12px' } },
          h('button.btn.primary', { onclick: runScan },
            hl(), icon('search', 14), h('span', 'Scan every jar'))));
    }

    const s = scan.summary;
    const actionable = scan.items.filter((i) =>
      i.verdict === 'client' && i.enabled && !i.already_handled);

    return frag(
      h('div.kpis', { style: { marginBottom: '16px' } },
        scanStat('Client-only', String(s.client), 'nothing to run here',
          s.client ? 'bad' : 'ok'),
        scanStat('Worth a look', String(s.review), 'weaker evidence',
          s.review ? 'warn' : ''),
        scanStat('Protected', String(s.protected),
          'another mod needs them', s.protected ? 'plum' : ''),
        scanStat('Fine', String(s.server), 'real server-side code', 'ok')),

      scan.note ? h('div.note', { style: { marginBottom: '14px' } }, scan.note)
        : null,

      actionable.length
        ? h('div.listbar',
          h('b', `${actionable.length} enabled jar(s) look client-only`),
          h('div.grow'),
          h('button.btn.sm.primary', {
            onclick: () => applyScan(actionable.map((i) => i.file_name), false),
          }, hl(), icon('minus', 13), h('span', 'Disable them all')))
        : null,

      h('div.modlist', ...scan.items
        .filter((i) => i.verdict !== 'server')
        .map(scanRow)),

      h('details', { style: { marginTop: '14px' } },
        h('summary', { style: { cursor: 'pointer', fontFamily: 'var(--mono)',
          fontSize: '11.5px', color: 'var(--faint)' } },
          `${s.server} jars judged fine — show them`),
        h('div.modlist', { style: { marginTop: '10px' } },
          ...scan.items.filter((i) => i.verdict === 'server').map(scanRow))),
    );
  }

  function scanStat(label, value, sub, tone = '') {
    return h('div.kpi', h('div.lbl', label),
      h(`div.v${tone ? `.${tone}` : ''}`, value), h('div.sub', sub));
  }

  const VERDICT = {
    client: ['bad', 'client-only'],
    review: ['warn', 'worth a look'],
    keep: ['plum', 'protected'],
    server: ['ok', 'fine'],
  };

  function scanRow(i) {
    const [tone, label] = VERDICT[i.verdict];
    return h(`div.modrow${i.enabled ? '' : '.off'}`,
      i.logo ? h('img.ico', { src: i.logo, alt: '', loading: 'lazy' })
        : h('div.ico', (i.name || i.file_name).slice(0, 2).toUpperCase()),
      h('div.who',
        h('div.nm', i.name || i.file_name),
        h('div.fn', i.file_name),
        h('div.tags',
          pill(label, tone),
          i.confidence ? pill(i.confidence, 'ghost') : null,
          !i.enabled ? pill('already disabled', 'dead') : null,
          i.whitelisted ? pill('you allowed this', 'ok') : null,
          i.operator?.verdict === 'block' ? pill('you blocked this', 'bad') : null,
          !i.readable ? pill('unreadable jar', 'dead',
            'Judged on its name alone') : null)),
      h('div.acts',
        h('button.btn.icon.xs.ghost', {
          'aria-label': `Why ${i.name || i.file_name} was judged ${label}`,
          title: 'Show the evidence',
          onclick: () => showEvidence(i),
        }, icon('info', 12)),
        i.verdict !== 'server' && i.enabled
          ? h('button.btn.xs', {
            onclick: () => applyScan([i.file_name], false),
          }, hl(), h('span', 'Disable'))
          : !i.enabled
            ? h('button.btn.xs.ghost', {
              onclick: () => applyScan([i.file_name], true),
            }, h('span', 'Enable'))
            : null));
  }

  function showEvidence(i) {
    modal({
      title: i.name || i.file_name,
      body: frag(
        h('div.chiprow', { style: { marginBottom: '12px' } },
          pill(VERDICT[i.verdict][1], VERDICT[i.verdict][0]),
          pill(`score ${i.score}`, 'ghost'),
          pill(i.confidence, 'ghost'),
          i.loader ? pill(i.loader, 'ghost') : null,
          i.classes ? pill(`${i.classes} classes`, 'ghost') : null,
          i.client_ratio
            ? pill(`${Math.round(i.client_ratio * 100)}% client code`, 'ghost')
            : null),
        h('p.muted', { style: { fontSize: '13px' } },
          'Each line is a piece of evidence and the points it contributed. '
          + 'Positive points argue the jar is client-only, negative that it '
          + 'is not. 70 or more acts; 30 or more asks you.'),
        h('div', { style: { display: 'grid', gap: '8px', marginTop: '12px' } },
          ...(i.findings || []).map((f) => h('div', {
            style: { display: 'flex', gap: '11px', alignItems: 'flex-start',
              padding: '8px 10px', border: '2px solid var(--rule)',
              background: f.points > 0 ? 'var(--danger-soft)'
                : f.points < 0 ? 'var(--sage-soft)' : 'var(--paper-2)' },
          },
            h('b.mono', { style: { minWidth: '44px', textAlign: 'right' } },
              f.points > 0 ? `+${f.points}` : String(f.points)),
            h('div', { style: { minWidth: 0 } },
              h('div.wrapany', { style: { fontSize: '13px' } }, f.why),
              h('div.mono.muted', { style: { fontSize: '10px' } }, f.source))))),
        i.required_by_others?.length
          ? h('div.note', { style: { marginTop: '12px' } },
            h('b', 'Held back as a dependency'),
            `${i.required_by_others.join(', ')} require this, so it is never `
            + 'disabled automatically.')
          : null,
        i.modrinth_url
          ? h('p', { style: { marginTop: '12px' } },
            h('a', { href: i.modrinth_url, target: '_blank', rel: 'noopener' },
              'Open on Modrinth'))
          : null,
      ),
      footer: frag(
        h('button.btn.sm.ghost', {
          onclick: async () => {
            close();
            await mark(i, 'allow');
          },
        }, icon('shieldCheck', 13), h('span', 'Always allow this mod')),
        h('button.btn.sm.ghost', {
          onclick: async () => { close(); await mark(i, 'block'); },
        }, icon('ban', 13), h('span', 'Always block it')),
        h('div.grow'),
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Close')),
      ),
    });
  }

  async function mark(i, verdict) {
    try {
      await api.post('/api/whitelist', {
        file: i.file_name, name: i.name, verdict,
        mod_id: i.mod_id, project_id: i.modrinth_id || i.project_id,
        source: i.source,
        evidence: (i.reasons || []).slice(0, 4),
        reason: verdict === 'allow'
          ? 'the operator decided it is safe on a server'
          : 'the operator marked it client-only',
      });
      toast(verdict === 'allow'
        ? 'It will not be flagged again, on any server.'
        : 'It will always be disabled by this scan.', 'ok');
      runScan(true);
    } catch (e) { toastError(e); }
  }

  function runScan(quiet) {
    return run(`/api/instances/${ctx.id}/client-scan?directory=${ctx.modDir}`, {}, {
      title: `Client-only scan — ${ctx.name}`,
      quiet,
      onEnd: (rec) => {
        if (rec.status !== 'done') return;
        scan = rec.result;
        view = 'client';
        paint();
      },
    });
  }

  async function applyScan(files, enabled) {
    try {
      const res = await api.post(`/api/instances/${ctx.id}/client-scan/apply`,
        { files, enabled, directory: ctx.modDir });
      toast(res.note, 'ok', { timeout: 7000 });
      runScan(true);
    } catch (e) { toastError(e); }
  }

  /* --- logs ---------------------------------------------------- */

  function logsView() {
    if (!diag.has_logs) {
      return empty('shulker-guard.gif', 'There is no log to read',
        'This instance has produced neither a log nor a crash report. That is '
        + 'itself the finding: the server was rejected before the JVM started, '
        + 'which means the EULA gate, the Java version, or memory.',
        h('button.btn.primary', { onclick: () => ctx.go('overview') },
          hl(), h('span', 'Run a boot test')));
    }
    return frag(
      h('div.grid.g2',
        logPane('latest.log', diag.log_path, diag.log_tail),
        logPane('crash report', diag.crash_path, diag.crash_tail)),
    );
  }

  function logPane(title, path, text) {
    if (!text) {
      return h('div.panel', h('header', icon('fileText', 15), h('h3', title)),
        h('div.pad', h('p.muted', 'Nothing here.')));
    }
    return h('div.panel',
      h('header', icon('fileText', 15), h('h3', title),
        h('span.sp', path || ''),
        h('button.btn.icon.xs.ghost', {
          style: { marginLeft: 'auto' }, 'aria-label': `Copy ${title}`,
          onclick: () => copyText(text, title),
        }, icon('copy', 12))),
      h('div.term', { style: { height: 'min(52vh,520px)' } },
        ...text.split('\n').map((l) => h('div.ln', l))));
  }

  /* --- deep scan and the assistant ------------------------------ */

  function deepScan() {
    run(`/api/instances/${ctx.id}/deep-scan`, {}, {
      title: `Deep dependency scan — ${ctx.name}`,
      onEnd: (rec) => {
        if (rec.status !== 'done') return;
        const r = rec.result || {};
        modal({
          title: 'Deep scan',
          wide: true,
          body: frag(
            h('div.kpis', { style: { marginBottom: '14px' } },
              scanStat('Jars read', String(r.scanned ?? 0), 'downloaded and opened'),
              scanStat('Missing', String((r.missing || []).length),
                'required and not present', (r.missing || []).length ? 'bad' : 'ok'),
              scanStat('Duplicates', String((r.duplicates || []).length),
                'same mod id twice',
                (r.duplicates || []).length ? 'warn' : 'ok')),
            (r.missing || []).length
              ? h('div.panel', h('header', h('h3', 'Missing dependencies')),
                h('div.pad', h('div.modlist', ...r.missing.map((m) =>
                  h('div.modrow', h('div.who',
                    h('div.nm', m.id || m.name),
                    h('div.fn', `required by ${(m.required_by || []).join(', ')}`)))))))
              : null,
            (r.duplicates || []).length
              ? h('div.panel', { style: { marginTop: '14px' } },
                h('header', h('h3', 'Duplicates')),
                h('div.pad', h('div.modlist', ...r.duplicates.map((d) =>
                  h('div.modrow', h('div.who',
                    h('div.nm', d.id),
                    h('div.fn', (d.files || []).join(', '))))))))
              : null,
            r.note ? h('div.note', { style: { marginTop: '14px' } }, r.note) : null,
          ),
          footer: h('button.btn.ghost', { onclick: () => close() },
            h('span', 'Close')),
        });
        load(true);
      },
    });
  }

  function askAI() {
    const endpoint = diag.crash_path
      ? `/api/instances/${ctx.id}/ai/crash-review`
      : `/api/instances/${ctx.id}/ai/analyse`;
    const streamHost = h('pre', {
      style: { background: 'var(--paper-2)', padding: '12px', overflow: 'auto',
        maxHeight: '340px', fontFamily: 'var(--mono)', fontSize: '12px',
        whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
    }, '');
    modal({
      title: diag.crash_path ? 'Crash review' : 'Assistant',
      wide: true,
      body: frag(
        h('div.note', { style: { marginBottom: '12px' } },
          h('b', 'The deterministic checks run first'),
          'They are handed to the model as evidence, so this still gives a '
          + 'useful answer when the endpoint is slow or unreachable. Anything '
          + 'it invents — including filenames not in your instance — is '
          + 'discarded.'),
        streamHost),
      footer: h('button.btn.ghost', { onclick: () => close() },
        h('span', 'Close')),
    });

    run(endpoint, {}, {
      title: 'AI analysis',
      quiet: true,
      onFrame: (frame, rec) => {
        if (frame.event === 'stream' || frame.event === 'snapshot') {
          streamHost.textContent = rec.stream;
          streamHost.scrollTop = streamHost.scrollHeight;
        }
      },
      onEnd: (rec) => {
        if (rec.status !== 'done') {
          streamHost.textContent += `\n\n${rec.error || 'It failed.'}`;
          return;
        }
        const r = rec.result || {};
        streamHost.textContent = r.summary || rec.stream || '(no answer)';
        if (r.culprits?.length) {
          streamHost.after(h('div', { style: { marginTop: '14px' } },
            h('h4.h-num', { style: { marginBottom: '8px' } }, 'Jars it blames'),
            h('div.modlist', ...r.culprits.slice(0, 10).map((c) =>
              h('div.modrow',
                h('div.who',
                  h('div.nm', c.file),
                  h('div.fn.wrapany', c.why || ''),
                  h('div.tags', pill(c.confidence || 'medium',
                    c.confidence === 'high' ? 'bad' : 'warn'),
                  c.from ? pill(c.from === 'log' ? 'from the log'
                    : 'from the model', 'ghost') : null)),
                h('div.acts', h('button.btn.xs', {
                  onclick: () => applyFix({ action: 'disable_mods',
                    files: [c.file] }),
                }, hl(), h('span', 'Disable'))))))));
        }
      },
    });
  }

  await load();
  return { node };
}
