/* ============================================================
   Mod Roulette — an offering, not a form.

   Two rewrites in one. The controls were a column of range
   sliders with labels, which is the shape of every settings
   panel ever built and told you nothing about what a setting
   would do. They are now dials that show their own consequence:
   the recklessness ladder lights up like a temperature gauge,
   the categories are tri-state crests you can see the state of
   from across the room, and the pool counter under them updates
   as you change your mind.

   And "pull the lever" was a button that said "pull the lever".
   It is now a suzu rope you actually drag — grab, pull, release,
   and it snaps back and rings. A gesture-only control is one
   nobody on a phone or a keyboard can use, so Enter and a plain
   tap pull it too; the drag is the flourish, never the mechanism.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, num, compact,
  empty, loadingFox, spinner, confirmDialog, modal, close, frag, segmented,
  field, toggle, prefs, copyText, debounce, countUp,
} from '../core.js';
import { run, runAwait } from '../jobs.js';
import { state, go, topActions, refreshInstances, bannerIfUnhealthy } from '../app.js';

const INTENSITY_BLURB = [
  'Only mods that are popular, maintained and small. A pack you could hand to a friend.',
  'Well-trodden ground, with a little room for something odd.',
  'The honest middle. Big mods allowed, stale ones are not.',
  'Abandoned mods are back on the table. Expect to read a crash log.',
  'Anything the catalogue will hand over. This is the point of the feature.',
];

export async function render() {
  const node = h('div.wrap.wide');
  mount(node, spinner('Reading the roulette'));

  let meta;
  try {
    meta = await api.get('/api/roulette/meta');
  } catch (e) {
    mount(node, empty('chargey-failed.png', 'The roulette will not load', e.message));
    return { node };
  }

  const c = { ...meta.defaults, ...prefs.get('roulette.constraints', {}) };
  c.categories = { ...(c.categories || {}) };
  c.toggles = { ...meta.defaults.toggles, ...(c.toggles || {}) };
  let seed = meta.seed;
  let roll = null;
  let pool = null;
  let rolling = false;
  const holds = new Set();

  const left = h('div');
  const right = h('div');

  topActions(h('a.btn.sm.ghost', { href: '#/discover' },
    icon('compass', 14), h('span', 'Browse instead')));

  mount(node, bannerIfUnhealthy(),
    h('div.sec-head.mon',
      h('div',
        h('p.eyebrow', 'Reproducible · ', h('b', 'and honest about the odds')),
        h('h2', 'Mod Roulette')),
      h('div.grow'),
      h('img', {
        src: '/assets/lucky-block.webp', alt: '', width: 46, height: 46,
        style: { imageRendering: 'pixelated' },
        class: 'fox',
      })),
    h('div.roul', left, right));

  /* ============================================================
     the rope
     ============================================================ */

  function buildRope() {
    const bay = h('div.ropebay');
    const cord = h('div.cord');
    const rope = h('button.rope', {
      type: 'button',
      'aria-label': 'Pull the rope to deal a hand',
      title: 'Pull the rope — or press Enter',
    }, cord, h('div.tassel'));
    const hint = h('div.hint', 'Pull the rope');
    const suzu = h('div.suzu', { 'aria-hidden': 'true' });
    const burst = h('div.burst', { 'aria-hidden': 'true' });

    const REST = 150;
    const MAX = 96;             // how far it will stretch
    const TRIGGER = 58;         // how far counts as a pull
    let dragging = false;
    let startY = 0;
    let pulled = 0;

    const setLen = (px) => cord.style.setProperty('--rope-len', `${px}px`);
    setLen(REST);

    const begin = (e) => {
      if (rolling) return;
      dragging = true;
      pulled = 0;
      startY = e.clientY;
      rope.classList.add('dragging');
      rope.setPointerCapture?.(e.pointerId);
    };
    const move = (e) => {
      if (!dragging) return;
      // Resistance: the further it goes the harder it pulls, so the rope has
      // weight instead of tracking the cursor one-to-one.
      const raw = Math.max(0, e.clientY - startY);
      pulled = MAX * (1 - Math.exp(-raw / 90));
      setLen(REST + pulled);
      hint.textContent = pulled > TRIGGER ? 'Let go' : 'Keep pulling';
      bay.classList.toggle('pulled', pulled > TRIGGER);
    };
    const end = () => {
      if (!dragging) return;
      dragging = false;
      rope.classList.remove('dragging');
      const fired = pulled > TRIGGER;
      setLen(REST);
      bay.classList.remove('pulled');
      hint.textContent = 'Pull the rope';
      if (fired) ring();
    };

    rope.addEventListener('pointerdown', begin);
    rope.addEventListener('pointermove', move);
    rope.addEventListener('pointerup', end);
    rope.addEventListener('pointercancel', end);
    // Keyboard and any pointer that never moved: a plain activation pulls it.
    rope.addEventListener('click', (e) => {
      if (pulled > TRIGGER) return;    // the drag already handled it
      e.preventDefault();
      ring();
    });

    function ring() {
      if (rolling) return;
      bay.classList.add('ringing');
      setTimeout(() => bay.classList.remove('ringing'), 700);
      clear(burst);
      for (let i = 0; i < 14; i++) {
        const petal = h('i');
        const angle = (Math.PI * 2 * i) / 14 + Math.random() * 0.4;
        petal.style.setProperty('--bx', `${Math.cos(angle) * (60 + Math.random() * 70)}px`);
        petal.style.setProperty('--by', `${Math.sin(angle) * (60 + Math.random() * 70)}px`);
        petal.style.setProperty('--br', `${Math.random() * 720 - 360}deg`);
        petal.style.animationDelay = `${Math.random() * 0.1}s`;
        burst.append(petal);
      }
      setTimeout(() => clear(burst), 1000);
      pull();
    }

    mount(bay, burst, suzu, rope, hint);
    return bay;
  }

  /* ============================================================
     the dials
     ============================================================ */

  function paintControls() {
    const catCount = Object.values(c.categories).filter((v) => v === 1).length;
    const banned = Object.values(c.categories).filter((v) => v === 2).length;

    mount(left, h('div', { style: { display: 'grid', gap: '16px' } },
      h('div.panel',
        h('header', icon('dice', 15), h('h3', 'The offering'),
          h('span.sp', rolling ? 'rolling…' : 'ready')),
        h('div.pad', { style: { display: 'grid', gap: '12px' } },
          buildRope(),
          h('div.seedbox', {
            title: 'Re-enter this seed with the same constraints for the same hand',
          }, seed),
          h('div.btnrow', { style: { justifyContent: 'center' } },
            h('button.btn.xs.ghost', { onclick: () => copyText(seed, 'Seed') },
              icon('copy', 12), h('span', 'Copy')),
            h('button.btn.xs.ghost', { onclick: editSeed },
              icon('edit', 12), h('span', 'Enter one')),
            h('button.btn.xs.ghost', {
              onclick: () => { seed = mintSeed(); paintControls(); },
            }, icon('refresh', 12), h('span', 'New'))),
        )),

      h('div.panel',
        h('header', icon('sliders', 15), h('h3', 'Constraints'),
          pool ? h('span.sp', `${pool.eligible} eligible`) : null),
        h('div.pad',
          h('div.dials',
            /* --- where -------------------------------------- */
            h('div.dial',
              h('div.lbl', 'Where it lands'),
              h('div.grid.g2', { style: { gap: '10px' } },
                h('div.field', { style: { margin: 0 } },
                  h('label', 'Minecraft'),
                  h('input.inp', {
                    value: c.minecraft,
                    oninput: (e) => { c.minecraft = e.target.value.trim(); save(); },
                    onchange: () => { pool = null; paintControls(); },
                  })),
                h('div.field', { style: { margin: 0 } },
                  h('label', 'Loader'),
                  h('select.inp', {
                    onchange: (e) => {
                      c.loader = e.target.value; pool = null; save(); paintControls();
                    },
                  }, ...meta.loaders.map((l) => h('option', {
                    value: l, selected: l === c.loader,
                  }, l)))))),

            /* --- how many ----------------------------------- */
            h('div.dial',
              h('div.lbl', 'How many mods', h('b', String(c.count))),
              h('input', {
                type: 'range', min: 5, max: 300, step: 5, value: c.count,
                'aria-label': 'Number of mods',
                oninput: (e) => {
                  c.count = Number(e.target.value);
                  left.querySelector('.dial .lbl b').textContent = String(c.count);
                  save();
                },
              }),
              h('div.why', 'Dependencies come on top and do not count against '
                + 'this. A hand of 120 usually installs around 400 jars.')),

            /* --- recklessness ------------------------------- */
            h('div.dial',
              h('div.lbl', 'Recklessness',
                h('b', meta.intensity[c.intensity - 1])),
              h('div.rungs', { role: 'group', 'aria-label': 'Recklessness' },
                ...meta.intensity.map((label, i) => h('button.rung', {
                  type: 'button',
                  dataset: { lit: i < c.intensity ? '1' : '0' },
                  'aria-pressed': String(i + 1 === c.intensity),
                  onclick: () => { c.intensity = i + 1; save(); paintControls(); },
                }, label))),
              h('div.why', INTENSITY_BLURB[c.intensity - 1])),

            /* --- quality floor ------------------------------ */
            h('div.dial',
              h('div.lbl', 'How well-trodden',
                h('b', c.quality ? `${c.quality}M+` : 'anything')),
              h('input', {
                type: 'range', min: 0, max: 30, step: 1, value: c.quality,
                'aria-label': 'Minimum downloads in millions',
                oninput: (e) => {
                  c.quality = Number(e.target.value);
                  const b = left.querySelectorAll('.dial .lbl b')[2];
                  if (b) b.textContent = c.quality ? `${c.quality}M+` : 'anything';
                  save();
                },
              }),
              h('div.why', c.quality
                ? `Only mods past ${c.quality} million downloads. Safe, and it `
                  + 'throws away most of what makes a roll interesting.'
                : 'No floor. Obscure mods are exactly as likely as famous ones.')),

            /* --- categories --------------------------------- */
            h('div.dial',
              h('div.lbl', 'Flavour',
                h('b', banned ? `${catCount}↑ ${banned}✕`
                  : catCount ? `${catCount} preferred` : 'no preference')),
              h('div.catgrid', ...meta.categories.map(catCrest)),
              h('div.why', 'Click once to weight a category toward the hand, '
                + 'again to ban it, again to forget it.')),

            /* --- talismans ---------------------------------- */
            h('div.dial',
              h('div.lbl', 'Talismans'),
              h('div', { style: { display: 'grid', gap: '9px' } },
                toggle('Bring dependencies', c.toggles.deps,
                  (v) => { c.toggles.deps = v; save(); },
                  { help: 'A rolled mod without its library is a crash, not a '
                    + 'surprise.' }),
                toggle('Allow client-only mods', c.toggles.client,
                  (v) => { c.toggles.client = v; save(); },
                  { help: 'Off by default. The single most common reason a '
                    + 'rolled pack does not boot.' }),
                toggle('Allow abandoned mods', c.toggles.conflict,
                  (v) => { c.toggles.conflict = v; save(); },
                  { help: 'Only has an effect at Reckless or above.' }),
                toggle('Cap enormous jars', c.toggles.cap,
                  (v) => { c.toggles.cap = v; save(); }))),
          ),

          h('div.btnrow', { style: { marginTop: '14px' } },
            h('button.btn.sm.ghost.block', { onclick: buildPool },
              icon('layers', 13), h('span', 'Rebuild the candidate pool'))),
          pool ? h('p.muted', { style: { fontSize: '12px', marginTop: '8px' } },
            pool.note) : null,
        )),
    ));
  }

  function catCrest(cat) {
    const st = c.categories[cat.key] || 0;
    return h('button.catmon', {
      type: 'button',
      dataset: { state: String(st) },
      'aria-label': `${cat.title}: ${['neutral', 'preferred', 'banned'][st]}`,
      onclick: () => {
        c.categories[cat.key] = (st + 1) % 3;
        save();
        paintControls();
      },
    },
      h('span.crest', { 'aria-hidden': 'true' }, cat.glyph),
      h('span', cat.title),
      pool?.by_category?.[cat.key]
        ? h('span.n', String(pool.by_category[cat.key])) : null);
  }

  const save = debounce(() => prefs.set('roulette.constraints', c), 400);

  async function editSeed() {
    const value = await new Promise((resolve) => {
      let settled = false;
      const answer = (v) => { if (!settled) { settled = true; resolve(v); } };
      const input = h('input.inp', {
        value: seed, maxLength: 12,
        style: { fontFamily: 'var(--mono)', letterSpacing: '.18em',
          textTransform: 'uppercase', textAlign: 'center', fontSize: '18px' },
      });
      modal({
        title: 'Enter a seed',
        body: frag(
          h('p.muted', { style: { fontSize: '13.5px' } },
            'The same seed with the same constraints deals the same hand, '
            + 'exactly. Change any constraint and the hand changes with it — '
            + 'which is why the seed alone is not the whole recipe, and why a '
            + 'roll you send someone should come with the settings.'),
          h('div.field', { style: { marginTop: '12px' } },
            h('label', 'Seed'), input)),
        footer: frag(
          h('button.btn.ghost', { onclick: () => { answer(null); close(); } },
            h('span', 'Cancel')),
          h('button.btn.primary', {
            onclick: () => { answer(input.value); close(); },
          }, hl(), h('span', 'Use it'))),
        onClose: () => answer(null),
      });
    });
    if (value) { seed = value.toUpperCase(); paintControls(); }
  }

  function mintSeed() {
    const alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
    const p = () => alphabet[Math.floor(Math.random() * alphabet.length)];
    return `${p()}${p()}${p()}-${p()}${p()}${p()}-${p()}${p()}`;
  }

  /* ============================================================
     the hand
     ============================================================ */

  function paintHand() {
    if (rolling) {
      mount(right, h('div.panel',
        h('header', icon('dice', 15), h('h3', 'Dealing')),
        h('div.pad', { style: { display: 'grid', gap: '14px',
          placeItems: 'center', padding: '40px 20px' } },
          h('img', { src: '/assets/lucky-block.webp', alt: '', width: 84,
            height: 84, class: 'fox', style: { imageRendering: 'pixelated' } }),
          h('p.muted', { style: { textAlign: 'center', maxWidth: '46ch' } },
            'Every candidate is being checked against what its publisher '
            + 'actually declares — the exact Minecraft version, and a loader '
            + 'this server can run. Anything that fails is replaced rather '
            + 'than quietly leaving you short.'))));
      return;
    }
    if (!roll) {
      mount(right, h('div.panel',
        h('header', icon('dice', 15), h('h3', 'Nothing dealt yet')),
        h('div.pad', { style: { display: 'grid', gap: '12px',
          placeItems: 'center', textAlign: 'center', padding: '40px 22px' } },
          h('div', { style: { position: 'relative' } },
            h('div.fox-halo'),
            h('img.fox', { src: '/assets/lucky-block.webp', alt: '',
              width: 108, height: 108,
              style: { imageRendering: 'pixelated' } })),
          h('h3.h-disp', { style: { fontSize: '20px' } }, 'Pull the rope'),
          h('p.muted', { style: { maxWidth: '48ch' } },
            'A hand is dealt from the live CurseForge and Modrinth '
            + 'catalogues, every mod is verified to publish a build for '
            + 'exactly this loader and version, and whatever fails is '
            + 'replaced.'))));
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
            stat('Mods', (roll.hand || []).length,
              roll.short ? `${roll.short} short of ${roll.constraints.count}`
                : 'as asked'),
            stat('Verified', compat.verified ?? 0,
              `declare ${roll.constraints.minecraft} explicitly`, 'ok'),
            stat('Download', bytes(s.total_bytes || 0),
              `${s.est_jars ?? '?'} jars with dependencies`, '', true),
            stat('Heap needed', `${s.heap_need ?? '?'} GB`,
              s.ceiling ? `this host can give ${s.ceiling} GB` : '',
              s.heap_need > s.ceiling ? 'bad' : 'ok', true)),
          (compat.lines || []).filter(Boolean).length
            ? h('p.muted', { style: { fontSize: '12.5px', marginTop: '10px' } },
              compat.lines.filter(Boolean).join(' ')) : null,
          roll.short
            ? h('div.note.warn', { style: { marginTop: '12px' } },
              h('b', 'The pool ran out'), roll.fill_note) : null,
          h('div.btnrow', { style: { marginTop: '14px' } },
            h('button.btn.primary', { onclick: install },
              hl(), icon('download', 14), h('span', 'Install this hand')),
            h('button.btn.ghost', { onclick: previewExport },
              icon('archive', 13), h('span', 'What is in the export?')),
            roll.dropped?.length
              ? h('button.btn.ghost', { onclick: showDropped },
                icon('info', 13), h('span', `${roll.dropped.length} dropped`))
              : null))),

      h('div.panel',
        h('header', icon('layers', 15), h('h3', 'The hand'),
          h('span.sp', `${(roll.hand || []).length} mods · ${holds.size} held`)),
        h('div.reel.dealing', ...(roll.hand || []).map(handRow))),
    ));
  }

  function stat(label, value, sub, tone = '', plain = false) {
    const v = h(`div.v${tone ? `.${tone}` : ''}`, { style: { fontSize: '19px' } },
      plain ? String(value) : '0');
    if (!plain) countUp(v, value);
    return h('div.kpi', h('div.lbl', label), v, sub ? h('div.sub', sub) : null);
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
            ? pill('verified', 'ok',
              `Publishes a build for exactly ${roll.constraints.minecraft}`)
            : m.version_match === 'untagged'
              ? pill('untagged', 'ghost',
                'Carries no version tag — normally a library, which is expected')
              : null,
          m.release_type && m.release_type !== 'release'
            ? pill(m.release_type, 'warn') : null,
          m.dependency_of ? pill('dependency', 'plum') : null)),
      h('div', { style: { display: 'flex', gap: '5px', flex: 'none' } },
        h('button.btn.icon.xs.ghost', {
          'aria-label': held ? `Release ${m.name}`
            : `Hold ${m.name} through the next pull`,
          title: held ? 'Held through the next pull' : 'Hold through the next pull',
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

  /* ============================================================
     actions
     ============================================================ */

  async function pull() {
    if (rolling) return;
    rolling = true;
    paintControls();
    paintHand();
    try {
      roll = await runAwait('/api/roulette/roll', {
        constraints: c, seed, holds: [...holds],
      }, { title: `Rolling ${seed}`, quiet: true });
      pool = roll.pool;
      seed = roll.seed;
      if (roll.short) toast(roll.fill_note, 'warn', { timeout: 12000 });
      else toast(`${roll.hand.length} mods dealt.`, 'ok', { timeout: 3000 });
    } catch (e) {
      toastError(e, 'The roll failed');
    } finally {
      rolling = false;
      paintControls();
      paintHand();
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

  const KIND_LABEL = {
    incompatible: 'No build for this exact version',
    'no-builds': 'Nothing published for this loader',
    'client-only': 'Client-only, and you asked for server mods',
    'lookup-failed': 'The catalogue would not answer',
    'no-file': 'No usable file',
  };

  function showDropped() {
    const byKind = {};
    for (const d of roll.dropped) (byKind[d.kind || 'other'] ||= []).push(d);
    modal({
      title: `${roll.dropped.length} candidates were dropped`,
      wide: true,
      body: frag(
        h('p.muted', { style: { fontSize: '13.5px', marginBottom: '12px' } },
          'Each of these was dealt and then failed verification, so a '
          + 'replacement was dealt in its place. This is the check that stops '
          + 'a pack installing cleanly and failing on its first boot.'),
        ...Object.entries(byKind).map(([kind, list]) => h('div',
          { style: { marginBottom: '14px' } },
          h('h4.h-num', { style: { marginBottom: '6px' } },
            `${KIND_LABEL[kind] || kind} — ${list.length}`),
          h('div.modlist', ...list.slice(0, 40).map((d) => h('div.modrow',
            h('div.who', h('div.nm', d.name),
              h('div.fn.wrapany', d.reason)))))))),
      footer: h('button.btn.ghost', { onclick: () => close() },
        h('span', 'Close')),
    });
  }

  async function previewExport() {
    try {
      const p = await api.post('/api/roulette/preview-export',
        { hand: roll.hand, constraints: c, seed });
      modal({
        title: 'The export',
        body: frag(
          h('div.kpis', { style: { marginBottom: '12px' } },
            h('div.kpi', h('div.lbl', 'In the manifest'),
              h('div.v', String(p.files)), h('div.sub', 'CurseForge projects')),
            h('div.kpi', h('div.lbl', 'Bundled'),
              h('div.v', String(p.bundled)), h('div.sub', 'Modrinth-only jars'))),
          h('p', p.note)),
        footer: h('button.btn.ghost', { onclick: () => close() },
          h('span', 'Close')),
      });
    } catch (e) { toastError(e); }
  }

  async function install() {
    const used = new Set(state.instances.map((x) => x.port));
    let port = 25565;
    while (used.has(port) && port < 25700) port += 1;

    const nameInput = h('input.inp', { value: `Roulette ${seed}`, maxLength: 60 });
    const portInput = h('input.inp', {
      type: 'number', value: port, min: 1024, max: 65535,
    });
    const opts = { optimize: true };

    modal({
      title: 'Install this hand',
      body: frag(
        h('p.muted', { style: { fontSize: '13.5px', marginBottom: '12px' } },
          'The hand is written as a CurseForge modpack zip and installed '
          + 'through the ordinary import path — same client-only review, same '
          + 'loader ladder, same manifest. The export is kept, so you can '
          + 'share it or open it in the CurseForge app.'),
        h('div.field', h('label', 'Server name'), nameInput),
        h('div.field', h('label', 'Port'), portInput),
        toggle('Tune the JVM for this host', true, (v) => { opts.optimize = v; })),
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
