/* ============================================================
   Tune — heap, JVM flags, the port, and server.properties.

   The heap ceiling comes from the host, not from the pack: a
   pack asking for 8 GB on a box with 5 GB free dies at startup
   with no log at all, and that is the single most confusing
   failure a self-hoster meets. Where the proposal had to
   overrule the pack, it says so.

   The port is written to server.properties *and* Crafty's own
   record together. Setting only one leaves a server that runs
   perfectly and is permanently displayed as offline.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, pill, bytes, num, empty,
  loadingFox, debounce, segmented, frag, confirmDialog, field, toggle,
} from '../core.js';

export async function render(ctx) {
  const node = h('div');
  const host = h('div');
  mount(node, host, loadingFox('Measuring the host'));

  let plan = null;
  let props = null;
  let port = null;
  let heap = 0;
  const flagState = new Map();
  const propEdits = new Map();
  let propGroup = '';
  let propQuery = '';

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

  function paint() {
    const hostSpecs = plan.host || {};
    const mem = plan.memory || {};
    const current = plan.current || {};

    mount(host,
      h('div.split.wide-aside',
        h('div', { style: { display: 'grid', gap: '18px' } },
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
                stat('Proposed', `${heap} GB`, mem.basis || `${plan.mod_count} mods`,
                  mem.unmeasured ? 'warn' : 'rose'),
              ),
              h('div.field',
                h('label', `Heap — ${heap} GB`),
                h('input', {
                  type: 'range', min: 1,
                  max: Math.max(4, Math.ceil(mem.ceiling_gb || 16)),
                  step: 0.5, value: heap,
                  'aria-label': 'Heap size in gigabytes',
                  oninput: (e) => {
                    heap = Number(e.target.value);
                    node.querySelector('#heaplabel').textContent = `Heap — ${heap} GB`;
                  },
                }),
                h('div.help', mem.unmeasured
                  ? "No ceiling could be computed, because this host's memory "
                    + 'is unreadable from here. Pick a number you know the '
                    + 'machine has.'
                  : `The ceiling is ${mem.ceiling_gb} GB — total minus the `
                    + `${mem.reserve_gb} GB reserved for the OS, Crafty and `
                    + 'whatever else is on this box.')),
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
                  type: 'search', placeholder: 'Search keys…',
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
                  h('b', 'Crafty and server.properties disagree'),
                  port.note)
                : null,
              h('div.field',
                h('label', 'Server port'),
                h('input.inp', {
                  id: 'portinput', type: 'number', min: 1024, max: 65535,
                  value: port.crafty_port ?? 25565, inputMode: 'numeric',
                }),
                h('div.help',
                  `Crafty's record: ${port.crafty_port ?? '—'} · `
                  + `server.properties: ${port.properties_port ?? '—'}`)),
              port.in_use_by_others?.length
                ? h('p.muted', { style: { fontSize: '12px' } },
                  'Also in use: '
                  + port.in_use_by_others.map((o) => `${o.name} :${o.port}`)
                    .join(', '))
                : null,
              h('button.btn.sm.block', { onclick: savePort },
                hl(), icon('save', 13), h('span', 'Set the port in both places')),
              h('p.muted', { style: { fontSize: '11.5px', marginTop: '10px' } },
                `Crafty's container publishes ports `
                + `${(port.published_range || []).join('–')}. A port outside `
                + 'that works inside Docker and is unreachable from your '
                + 'network.'),
            )),

          h('div.panel',
            h('header', icon('save', 15), h('h3', 'Apply')),
            h('div.pad', { style: { display: 'grid', gap: '10px' } },
              h('div.mono.muted', { style: { fontSize: '11.5px' } },
                `${heap} GB heap · `
                + `${(plan.flags || []).filter((f) => flagState.get(f.flag)).length} flags`),
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
                .map((p) => h('div.mono.muted', { style: { fontSize: '11px' } },
                  `${p.key} = ${p.value}`, h('div', { style: { opacity: .75 } },
                    p.why || ''))),
            )),
        ),
      ),
    );
    // The label is replaced live by the range handler, so it needs an id.
    const lbl = node.querySelector('.field label');
    if (lbl && lbl.textContent.startsWith('Heap')) lbl.id = 'heaplabel';
  }

  function stat(label, value, sub, tone = '') {
    return h('div.kpi', h('div.lbl', label),
      h(`div.v${tone ? `.${tone}` : ''}`, { style: { fontSize: '19px' } }, value),
      h('div.sub', sub));
  }

  function flagRow(f) {
    const on = !!flagState.get(f.flag);
    return h('label', {
      style: { display: 'flex', gap: '11px', alignItems: 'flex-start',
        border: '2px solid var(--rule)', padding: '9px 11px', cursor: 'pointer',
        background: on ? 'var(--sakura-soft)' : 'var(--paper-2)' },
    },
      h('input', {
        type: 'checkbox', checked: on,
        onchange: (e) => { flagState.set(f.flag, e.target.checked); paint(); },
      }),
      h('div', { style: { minWidth: 0 } },
        h('div.mono.wrapany', { style: { fontSize: '12px', fontWeight: 700 } },
          f.flag),
        h('div.muted', { style: { fontSize: '12px' } }, f.why || f.description || ''),
        f.applied ? pill('already set', 'ok') : null));
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
      await load();
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
  return { node };
}
