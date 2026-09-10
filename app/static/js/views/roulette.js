/* ============================================================
   Mod Roulette — deal a pack you did not choose.

   Two things make it more than a shuffle, and both are visible
   on this screen:

     * a pull is reproducible. Every roll carries a short seed;
       re-enter it with the same constraints and you get the same
       hand, exactly, so a roll is something you can send to
       someone.
     * every mod in the hand has been *verified* to publish a
       build for exactly this loader and this Minecraft version.
       The catalogue's own filter is looser than it looks — a
       file tagged 1.21 comes back for a 1.21.1 query, installs
       without complaint, and takes the server down on first boot.
       Anything that fails that check is dropped with a reason
       and a replacement is dealt.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, num, compact,
  empty, loadingFox, confirmDialog, modal, close, frag, segmented, field,
  toggle, prefs, copyText, debounce,
} from '../core.js';
import { run, runAwait } from '../jobs.js';
import { state, go, topActions, refreshInstances, bannerIfUnhealthy } from '../app.js';

export async function render() {
  const node = h('div.wrap.wide');
  mount(node, loadingFox('Reading the roulette'));

  let meta;
  try {
    meta = await api.get('/api/roulette/meta');
  } catch (e) {
    mount(node, empty('chargey-failed.png', 'The roulette will not load',
      e.message));
    return { node };
  }

  const c = {
    ...meta.defaults,
    ...prefs.get('roulette.constraints', {}),
  };
  c.categories = { ...(c.categories || {}) };
  c.toggles = { ...meta.defaults.toggles, ...(c.toggles || {}) };
  let seed = meta.seed;
  let roll = null;
  let pool = null;
  const holds = new Set();

  const left = h('div');
  const right = h('div');
  mount(node, bannerIfUnhealthy(),
    h('div.sec-head.mon',
      h('div',
        h('p.eyebrow', 'Reproducible · ', h('b', 'and honest about the odds')),
        h('h2', 'Mod Roulette')),
      h('div.grow'),
      h('a.btn.sm.ghost', { href: '#/discover' }, icon('compass', 14),
        h('span', 'Browse instead'))),
    h('div.roul', left, right));

  /* --- controls -------------------------------------------------- */

  function paintControls() {
    mount(left, h('div', { style: { display: 'grid', gap: '16px' } },
      h('div.panel',
        h('header', icon('dice', 15), h('h3', 'The pull')),
        h('div.pad', { style: { display: 'grid', gap: '12px' } },
          h('div.seedbox', { title: 'Re-enter this seed with the same '
            + 'constraints for the same hand' }, seed),
          h('div.btnrow',
            h('button.btn.sm.ghost', {
              onclick: () => copyText(seed, 'Seed'),
            }, icon('copy', 13), h('span', 'Copy')),
            h('button.btn.sm.ghost', { onclick: editSeed },
              icon('edit', 13), h('span', 'Enter a seed')),
            h('button.btn.sm.ghost', {
              onclick: () => { seed = mintSeed(); paintControls(); },
            }, icon('refresh', 13), h('span', 'New seed'))),
          h('button.btn.primary.block', { id: 'pullbtn', onclick: pull },
            hl(), icon('dice', 16),
            h('span', roll ? 'Pull again' : 'Pull the lever')),
        )),

      h('div.panel',
        h('header', icon('sliders', 15), h('h3', 'Constraints')),
        h('div.pad', { style: { display: 'grid', gap: '14px' } },
          h('div.grid.g2',
            field('Minecraft', h('input.inp', {
              value: c.minecraft,
              oninput: (e) => { c.minecraft = e.target.value.trim(); save(); },
            })),
            field('Loader', h('select.inp', {
              onchange: (e) => { c.loader = e.target.value; save(); },
            }, ...meta.loaders.map((l) => h('option', {
              value: l, selected: l === c.loader,
            }, l))))),

          field(`How many mods — ${c.count}`, h('input', {
            type: 'range', min: 5, max: 300, step: 5, value: c.count,
            'aria-label': 'Number of mods',
            oninput: (e) => {
              c.count = Number(e.target.value);
              node.querySelector('#countlbl').textContent =
                `How many mods — ${c.count}`;
              save();
            },
          }), 'Dependencies come on top and do not count against this.'),

          field(`Recklessness — ${meta.intensity[c.intensity - 1]}`,
            h('input', {
              type: 'range', min: 1, max: 5, step: 1, value: c.intensity,
              'aria-label': 'Recklessness',
              oninput: (e) => {
                c.intensity = Number(e.target.value);
                node.querySelector('#intlbl').textContent =
                  `Recklessness — ${meta.intensity[c.intensity - 1]}`;
                save();
              },
            }),
            'Gentle refuses anything stale or unpopular. Unhinged does not.'),

          field(`Quality floor — ${c.quality
            ? `${c.quality}M downloads` : 'none'}`,
          h('input', {
            type: 'range', min: 0, max: 30, step: 1, value: c.quality,
            'aria-label': 'Minimum downloads in millions',
            oninput: (e) => {
              c.quality = Number(e.target.value);
              node.querySelector('#qualbl').textContent =
                `Quality floor — ${c.quality ? `${c.quality}M downloads` : 'none'}`;
              save();
            },
          })),

          h('div',
            h('div.eyebrow', { style: { marginBottom: '8px' } },
              'Categories — click to prefer, again to ban'),
            h('div.catgrid', ...meta.categories.map(catButton))),

          h('div', { style: { display: 'grid', gap: '8px' } },
            toggle('Resolve dependencies', c.toggles.deps,
              (v) => { c.toggles.deps = v; save(); }),
            toggle('Allow client-only mods', c.toggles.client,
              (v) => { c.toggles.client = v; save(); },
              { help: 'Off by default. A client-only mod on a server is the '
                + 'most common way a rolled pack fails to boot.' }),
            toggle('Allow abandoned mods', c.toggles.conflict,
              (v) => { c.toggles.conflict = v; save(); },
              { help: 'Only has an effect at Reckless or above.' }),
            toggle('Cap enormous jars', c.toggles.cap,
              (v) => { c.toggles.cap = v; save(); })),

          h('button.btn.sm.ghost.block', { onclick: buildPool },
            icon('layers', 13), h('span', 'Rebuild the candidate pool')),
          pool ? h('p.muted', { style: { fontSize: '12px' } }, pool.note) : null,
        )),
    ));
    // Labels that a range handler rewrites need stable ids.
    const labels = node.querySelectorAll('.field > label');
    labels.forEach((l) => {
      if (l.textContent.startsWith('How many')) l.id = 'countlbl';
      if (l.textContent.startsWith('Recklessness')) l.id = 'intlbl';
      if (l.textContent.startsWith('Quality floor')) l.id = 'qualbl';
    });
  }

  function catButton(cat) {
    const stateVal = c.categories[cat.key] || 0;
    return h('button.catbtn', {
      type: 'button',
      dataset: { state: String(stateVal) },
      'aria-label': `${cat.title}: ${['neutral', 'preferred', 'banned'][stateVal]}`,
      onclick: () => {
        c.categories[cat.key] = (stateVal + 1) % 3;
        save();
        paintControls();
      },
    }, h('span.g', { 'aria-hidden': 'true' }, cat.glyph), h('span', cat.title));
  }

  const save = debounce(() => prefs.set('roulette.constraints', c), 400);

  async function editSeed() {
    const value = await new Promise((resolve) => {
      const input = h('input.inp', { value: seed, maxLength: 12,
        style: { fontFamily: 'var(--mono)', letterSpacing: '.16em',
          textTransform: 'uppercase' } });
      modal({
        title: 'Enter a seed',
        body: frag(
          h('p.muted', { style: { fontSize: '13.5px' } },
            'The same seed with the same constraints deals the same hand, '
            + 'exactly. Change any constraint and the hand changes with it — '
            + 'which is why the seed alone is not the whole recipe.'),
          h('div.field', { style: { marginTop: '12px' } },
            h('label', 'Seed'), input)),
        footer: frag(
          h('button.btn.ghost', { onclick: () => { close(); resolve(null); } },
            h('span', 'Cancel')),
          h('button.btn.primary', {
            onclick: () => { close(); resolve(input.value); },
          }, hl(), h('span', 'Use it'))),
        onClose: () => resolve(null),
      });
    });
    if (value) { seed = value.toUpperCase(); paintControls(); }
  }

  function mintSeed() {
    const alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
    const pick = () => alphabet[Math.floor(Math.random() * alphabet.length)];
    return `${pick()}${pick()}${pick()}-${pick()}${pick()}${pick()}-${pick()}${pick()}`;
  }

  /* --- the hand --------------------------------------------------- */

  function paintHand() {
    if (!roll) {
      mount(right, h('div.card', { style: { alignItems: 'center',
        textAlign: 'center', padding: '38px 22px' } },
        h('div', { style: { position: 'relative' } },
          h('div.fox-halo'),
          h('img.fox', { src: '/assets/lucky-block.png', alt: '',
            style: { width: '120px' } })),
        h('h3.h-disp', { style: { fontSize: '20px' } }, 'Pull the lever'),
        h('p.muted', { style: { maxWidth: '48ch' } },
          'A hand is dealt from the live CurseForge and Modrinth catalogues, '
          + 'every mod is checked against what its publisher actually declares, '
          + 'and whatever fails is replaced rather than quietly leaving you '
          + 'with a shorter pack.')));
      return;
    }

    const s = roll.summary || {};
    const odds = s.odds || {};
    const compat = roll.compatibility || {};

    mount(right, h('div', { style: { display: 'grid', gap: '16px' } },
      h('div.panel',
        h('header', icon('activity', 15), h('h3', 'The odds'),
          h('span.sp', roll.seed)),
        h('div.pad',
          h(`div.note.${odds.tone === 'bad' ? 'bad'
            : odds.tone === 'good' ? 'ok' : 'warn'}`,
            h('b', odds.title || 'This hand looks workable'),
            h('ul', { style: { margin: '4px 0 0 18px' } },
              ...(odds.reasons || []).map((r) => h('li', r)))),
          h('div.kpis', { style: { marginTop: '14px' } },
            stat('Mods', String((roll.hand || []).length),
              roll.short ? `${roll.short} short of ${roll.constraints.count}`
                : 'as asked'),
            stat('Verified', `${compat.verified ?? 0}`,
              `declare ${roll.constraints.minecraft} explicitly`, 'ok'),
            stat('Download', bytes(s.total_bytes || 0),
              `${s.est_jars ?? '?'} jars with dependencies`),
            stat('Heap needed', `${s.heap_need ?? '?'} GB`,
              s.ceiling ? `this host can give ${s.ceiling} GB` : '',
              s.heap_need > s.ceiling ? 'bad' : 'ok')),
          (compat.lines || []).length
            ? h('p.muted', { style: { fontSize: '12.5px', marginTop: '10px' } },
              compat.lines.filter(Boolean).join(' '))
            : null,
          roll.short
            ? h('div.note.warn', { style: { marginTop: '12px' } },
              h('b', 'The pool ran out'), roll.fill_note)
            : null,
          h('div.btnrow', { style: { marginTop: '14px' } },
            h('button.btn.primary', { onclick: install },
              hl(), icon('download', 14), h('span', 'Install this hand')),
            h('button.btn.ghost', { onclick: previewExport },
              icon('archive', 13), h('span', 'What would the export be?')),
            roll.dropped?.length
              ? h('button.btn.ghost', { onclick: showDropped },
                icon('info', 13),
                h('span', `${roll.dropped.length} dropped`))
              : null))),

      h('div.panel',
        h('header', icon('layers', 15), h('h3', 'The hand'),
          h('span.sp', `${(roll.hand || []).length} mods · `
            + `${holds.size} held`)),
        h('div.reel', ...(roll.hand || []).map(handRow))),
    ));
  }

  function stat(label, value, sub, tone = '') {
    return h('div.kpi', h('div.lbl', label),
      h(`div.v${tone ? `.${tone}` : ''}`, { style: { fontSize: '19px' } }, value),
      sub ? h('div.sub', sub) : null);
  }

  const FLAG = {
    CLIENT: ['bad', 'client-only'],
    'CLIENT?': ['warn', 'maybe client'],
    HEAVY: ['warn', 'heavy'],
    CHAOS: ['plum', 'abandoned'],
  };

  function handRow(m) {
    const held = holds.has(m.name);
    const flag = FLAG[m.flag];
    return h(`div.reelrow${held ? '.held' : ''}`,
      m.logo ? h('img.ico', { src: m.logo, alt: '', loading: 'lazy' })
        : h('div.ico', { style: { display: 'grid', placeItems: 'center' } },
          icon('box', 14)),
      h('div.who',
        h('div.nm', m.name),
        h('div.sub', [
          m.build_version || m.file_name,
          m.size ? bytes(m.size) : null,
          m.category,
          m.dependency_of ? `needed by ${m.dependency_of}` : null,
        ].filter(Boolean).join(' · ')),
        h('div.chiprow', { style: { marginTop: '3px' } },
          flag ? pill(flag[1], flag[0],
            (m.flag_why || {}).client || (m.flag_why || {}).heavy
            || (m.flag_why || {}).chaos) : null,
          m.version_match === 'exact'
            ? pill('verified', 'ok', `Publishes a build for exactly `
              + `${roll.constraints.minecraft}`)
            : m.version_match === 'untagged'
              ? pill('untagged', 'ghost', 'Carries no version tag — normally a '
                + 'library, which is expected')
              : null,
          m.release_type && m.release_type !== 'release'
            ? pill(m.release_type, 'warn') : null,
          m.dependency_of ? pill('dependency', 'plum') : null)),
      h('div', { style: { display: 'flex', gap: '5px', flex: 'none' } },
        h('button.btn.icon.xs.ghost', {
          'aria-label': held ? `Release ${m.name}` : `Hold ${m.name} through the next pull`,
          title: held ? 'Held through the next pull' : 'Hold',
          onclick: () => {
            if (held) holds.delete(m.name); else holds.add(m.name);
            paintHand();
          },
        }, icon(held ? 'star' : 'heart', 12)),
        h('button.btn.icon.xs.ghost', {
          'aria-label': `Reroll ${m.name}`, title: 'Reroll just this slot',
          onclick: () => rerollOne(m),
        }, icon('refresh', 12))));
  }

  /* --- actions ---------------------------------------------------- */

  async function pull() {
    const btn = node.querySelector('#pullbtn');
    btn?.classList.add('busy');
    try {
      roll = await runAwait('/api/roulette/roll', {
        constraints: c, seed, holds: [...holds],
      }, { title: `Rolling ${seed}` });
      pool = roll.pool;
      seed = roll.seed;
      paintControls();
      paintHand();
      if (roll.short) {
        toast(roll.fill_note, 'warn', { timeout: 12000 });
      }
    } catch (e) {
      toastError(e, 'The roll failed');
    } finally {
      btn?.classList.remove('busy');
    }
  }

  async function buildPool() {
    try {
      pool = await runAwait('/api/roulette/pool', { ...c, refresh: true },
        { title: 'Rebuilding the pool' });
      toast(pool.note, 'ok', { timeout: 8000 });
      paintControls();
    } catch (e) { toastError(e); }
  }

  async function rerollOne(m) {
    try {
      const res = await api.post('/api/roulette/reroll', {
        constraints: c, seed, hand: roll.hand, mod: m.name,
      });
      roll = { ...roll, hand: res.hand, summary: res.summary };
      paintHand();
    } catch (e) { toastError(e); }
  }

  function showDropped() {
    const byKind = {};
    for (const d of roll.dropped) {
      (byKind[d.kind || 'other'] ||= []).push(d);
    }
    modal({
      title: `${roll.dropped.length} candidates were dropped`,
      wide: true,
      body: frag(
        h('p.muted', { style: { fontSize: '13.5px', marginBottom: '12px' } },
          'Each of these was dealt and then failed verification, so a '
          + 'replacement was dealt in its place. This is the check that stops '
          + 'a pack installing cleanly and failing on first boot.'),
        ...Object.entries(byKind).map(([kind, list]) => h('div',
          { style: { marginBottom: '14px' } },
          h('h4.h-num', { style: { marginBottom: '6px' } },
            `${KIND_LABEL[kind] || kind} — ${list.length}`),
          h('div.modlist', ...list.slice(0, 40).map((d) => h('div.modrow',
            h('div.who', h('div.nm', d.name),
              h('div.fn.wrapany', d.reason))))))),
      ),
      footer: h('button.btn.ghost', { onclick: () => close() },
        h('span', 'Close')),
    });
  }

  const KIND_LABEL = {
    incompatible: 'No build for this exact version',
    'no-builds': 'Nothing published for this loader',
    'client-only': 'Client-only, and you asked for server mods',
    'lookup-failed': 'The catalogue would not answer',
    'no-file': 'No usable file',
  };

  async function previewExport() {
    try {
      const p = await api.post('/api/roulette/preview-export',
        { hand: roll.hand, constraints: c, seed });
      modal({
        title: 'The export',
        body: frag(
          h('div.kpis', { style: { marginBottom: '12px' } },
            stat('In the manifest', String(p.files), 'CurseForge projects'),
            stat('Bundled', String(p.bundled), 'Modrinth-only jars')),
          h('p', p.note)),
        footer: h('button.btn.ghost', { onclick: () => close() },
          h('span', 'Close')),
      });
    } catch (e) { toastError(e); }
  }

  async function install() {
    const used = new Set(state.instances.map((s) => s.port));
    let port = 25565;
    while (used.has(port) && port < 25700) port += 1;

    const nameInput = h('input.inp', { value: `Roulette ${seed}`, maxLength: 60 });
    const portInput = h('input.inp', { type: 'number', value: port,
      min: 1024, max: 65535 });
    const opts = { optimize: true };

    modal({
      title: 'Install this hand',
      body: frag(
        h('p.muted', { style: { fontSize: '13.5px', marginBottom: '12px' } },
          'The hand is written as a CurseForge modpack zip and then installed '
          + 'through the ordinary import path — same client-only review, same '
          + 'loader repair, same manifest. The export is kept so you can share '
          + 'it or open it in the CurseForge app.'),
        h('div.field', h('label', 'Server name'), nameInput),
        h('div.field', h('label', 'Port'), portInput),
        toggle('Tune the JVM for this host', true,
          (v) => { opts.optimize = v; })),
      footer: frag(
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', {
          onclick: () => {
            close();
            run('/api/roulette/install', {
              hand: roll.hand, constraints: c, seed,
              server_name: nameInput.value.trim() || `Roulette ${seed}`,
              port: Number(portInput.value),
              optimize: opts.optimize,
            }, {
              title: `Rolling ${nameInput.value.trim()}`,
              onEnd: (rec) => {
                if (rec.status !== 'done') return;
                refreshInstances({ quiet: true });
                const r = rec.result || {};
                toast(`${r.mods_installed ?? '?'} mods installed.`, 'ok',
                  { title: 'The roll is a server', timeout: 9000 });
                if (r.server_id) go(`/i/${r.server_id}/overview`);
              },
            });
          },
        }, hl(), icon('download', 14), h('span', 'Roll it')),
      ),
    });
  }

  paintControls();
  paintHand();
  return { node };
}
