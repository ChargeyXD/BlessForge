/* ============================================================
   Mod Roulette — an offering, not a form.

   Three things happen on this screen and it is now organised
   around them rather than around the API that serves them:
   you set the offering, you pull the rope, and you read what
   came back. The reading used to be spread over a panel, two
   modals and a toast; it is one panel with three faces now —
   the hand, its lineage, and what was turned away.

   The rope is the centrepiece and it is simulated, not
   animated. A CSS transition snapping between two heights is
   the thing that felt dead: it moved only while you touched
   it. This one hangs on a spring, sways on a breeze that never
   repeats, leans toward your cursor, and swings for a while
   after you let go. Keyboard and touch still pull it with a
   plain activation, because a gesture-only control is one
   nobody on a phone can use.
   ============================================================ */

import {
  h, hl, mount, clear, icon, api, toast, toastError, pill, bytes, compact,
  empty, spinner, modal, close, frag, segmented, toggle, prefs, debounce,
  countUp,
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

/* Why a mod was turned away, in the order a person cares about it. The
   three at the top are the vetting pass — the one that reads the hand as a
   set rather than as a list — and they are worded so the difference between
   "this mod is broken" and "this mod did not belong here" is visible. */
const KIND_LABEL = {
  'orphan-addon': 'Add-ons with nothing to add to',
  conflict: 'Cannot sit beside each other',
  library: 'Libraries, which are not a roll',
  incompatible: 'No build for this exact version',
  'no-builds': 'Nothing published for this loader',
  'client-only': 'Client-only, and you asked for server mods',
  'lookup-failed': 'The catalogue would not answer',
  'no-file': 'No usable file',
};
const KIND_ORDER = Object.keys(KIND_LABEL);

export async function render() {
  const node = h('div.wrap.wide');
  mount(node, spinner('Reading the roulette'));

  let meta;
  try {
    meta = await api.get('/api/roulette/meta');
  } catch (e) {
    mount(node, empty('hurt', 'The roulette will not load', e.message));
    return { node };
  }

  const c = { ...meta.defaults, ...prefs.get('roulette.constraints', {}) };
  c.categories = { ...(c.categories || {}) };
  c.toggles = { ...meta.defaults.toggles, ...(c.toggles || {}) };
  // A stored preference can name a loader that is no longer offered — Quilt
  // was withdrawn because the installer cannot set one up unattended — so it
  // is corrected here rather than sent to the server to be corrected there.
  if (!meta.loaders.includes(c.loader)) c.loader = meta.defaults.loader;

  let roll = null;
  let pool = null;
  let rolling = false;
  let face = 'hand';
  const holds = new Set();
  let rope = null;                 // the live one, so it can be stopped

  const left = h('div');
  const right = h('div');

  topActions(h('a.btn.sm.ghost', { href: '#/discover' },
    icon('compass', 14), h('span', 'Browse instead')));

  mount(node, bannerIfUnhealthy(),
    h('div.sec-head.mon',
      h('div',
        h('p.eyebrow', 'No seed, no second chances · ',
          h('b', 'and the hand has to hold together')),
        h('h2', 'Mod Roulette')),
      h('div.grow'),
      h('img', {
        src: '/assets/lucky-block.webp', alt: '', width: 46, height: 46,
        style: { imageRendering: 'pixelated' },
        class: 'fox',
      })),
    h('div.roul', left, right));

  /* ============================================================
     the rope — a spring, a breeze and a bell
     ============================================================ */

  function suzuRope() {
    const REST = 132;        // hanging length, also the CSS default
    const MAX = 104;         // how far it will stretch
    const TRIGGER = 54;      // how far counts as a pull

    const cord = h('div.cord', h('i.knot'), h('i.knot'), h('i.knot'));
    const tassel = h('div.tassel');
    const pullable = h('button.rope', {
      type: 'button',
      'aria-label': 'Pull the rope to deal a hand',
      title: 'Pull the rope — or press Enter',
    }, cord, tassel);
    const suzu = h('div.suzu', { 'aria-hidden': 'true' }, h('i.clapper'));
    const rig = h('div.rig', suzu, pullable);
    const hint = h('div.hint', 'Pull the rope');
    const burst = h('div.burst', { 'aria-hidden': 'true' });
    const bay = h('div.ropebay', burst, rig, hint);

    /* --- state -------------------------------------------------
       Everything below is in one integrator rather than in CSS
       transitions, because the interesting part is what happens
       AFTER you stop touching it. A transition has no opinion
       about momentum; a spring does. */
    let len = REST, lenV = 0, want = REST;
    let ang = 0, angV = 0;          // the rope's sway, degrees
    let tAng = 0, tV = 0;           // the tassel, which lags behind it
    let bell = 0, bellV = 0;        // the suzu's own swing
    let clap = 0;                   // the clapper, faster and looser
    let breeze = 0, phase = Math.random() * 1000;
    let lean = 0;                   // toward the cursor, when it is near
    let dragging = false, startY = 0, startX = 0, pulled = 0, drift = 0;
    let raf = 0, last = 0, alive = true;

    const spring = (x, v, target, k, damp, dt) => {
      const nv = v + ((target - x) * k - v * damp) * dt;
      return [x + nv * dt, nv];
    };

    function frame(now) {
      if (!alive) return;
      // Clamped at BOTH ends. The upper bound is the usual "do not integrate
      // a whole dropped second in one step"; the lower one matters just as
      // much, because a rAF timestamp that goes backwards — a restored tab,
      // a reset document timeline — makes dt negative, and a negative dt
      // integrates the spring in reverse and diverges. It took four frames
      // to reach eighty thousand degrees of sway when that happened.
      const dt = Math.max(0, Math.min(0.048, (now - (last || now)) / 1000));
      last = now;

      /* A breeze made of two sines whose periods do not divide into each
         other (5.6s and 14.7s), plus a random walk that decays. Nobody can
         find the loop in that, which is the whole point — a fixed swing
         reads as a GIF.

         The frequencies are the part that had to be measured rather than
         guessed. The first pass used 0.47 and 0.19 rad/s, which are periods
         of 13 and 33 seconds: the rope did move, by twenty pixels, spread
         over forty seconds. That is indistinguishable from not moving. A
         hanging rope this size swings in a couple of seconds, so the drive
         has to be in that range too. */
      phase += dt;
      breeze += (Math.random() - 0.5) * dt * 3.2;
      breeze -= breeze * dt * 1.4;
      const wind = Math.sin(phase * 1.13) * 0.62
        + Math.sin(phase * 0.4271 + 1.7) * 0.44
        + breeze;

      /* Two regimes, and the damping has to change with the stiffness or
         the rope fights your hand. While dragging it is stiff and very
         nearly critically damped (c ~ 2*sqrt(k)), so it sits where you put
         it. Let go and it drops to a soft, deliberately UNDER-damped spring,
         which is what gives the overshoot and the two or three bounces that
         read as weight. */
      const restAngle = dragging ? drift * 0.14 : wind * 3.4 + lean;
      [ang, angV] = spring(ang, angV, restAngle, dragging ? 90 : 26, 5.4, dt);
      [tAng, tV] = spring(tAng, tV, ang, 44, 6.6, dt);
      [len, lenV] = spring(len, lenV, want, dragging ? 900 : 118,
                           dragging ? 62 : 11, dt);
      [bell, bellV] = spring(bell, bellV, ang * 0.55, 30, 4.2, dt);
      clap += (bell * 1.9 - clap) * Math.min(1, dt * 14);

      cord.style.setProperty('--rope-len', `${len.toFixed(1)}px`);
      rig.style.setProperty('--sway', `${ang.toFixed(2)}deg`);
      tassel.style.setProperty('--sway-lag', `${(tAng - ang).toFixed(2)}deg`);
      suzu.style.setProperty('--bell', `${bell.toFixed(2)}deg`);
      suzu.style.setProperty('--clap', `${clap.toFixed(2)}px`);
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);

    /* --- the gesture ------------------------------------------- */
    const begin = (e) => {
      if (rolling) return;
      dragging = true;
      pulled = 0;
      startY = e.clientY;
      startX = e.clientX;
      pullable.classList.add('dragging');
      pullable.setPointerCapture?.(e.pointerId);
    };
    const move = (e) => {
      if (!dragging) return;
      // Resistance: the further it goes the harder it pulls, so the rope has
      // weight instead of tracking the cursor one-to-one.
      const raw = Math.max(0, e.clientY - startY);
      pulled = MAX * (1 - Math.exp(-raw / 90));
      drift = Math.max(-70, Math.min(70, e.clientX - startX));
      want = REST + pulled;
      hint.textContent = pulled > TRIGGER ? 'Let go' : 'Keep pulling';
      bay.classList.toggle('pulled', pulled > TRIGGER);
    };
    const end = () => {
      if (!dragging) return;
      dragging = false;
      pullable.classList.remove('dragging');
      const fired = pulled > TRIGGER;
      const force = Math.min(1, pulled / MAX);
      want = REST;
      // Let go of a stretched rope and it does not stop where it started.
      lenV -= 520 * force;
      angV += drift * 1.6 + (Math.random() - 0.5) * 40;
      drift = 0;
      bay.classList.remove('pulled');
      hint.textContent = 'Pull the rope';
      if (fired) ring(force);
      pulled = 0;
    };

    pullable.addEventListener('pointerdown', begin);
    pullable.addEventListener('pointermove', move);
    pullable.addEventListener('pointerup', end);
    pullable.addEventListener('pointercancel', end);
    // Keyboard, and any pointer that never moved: a plain activation pulls it.
    pullable.addEventListener('click', (e) => {
      if (pulled > TRIGGER) return;      // the drag already handled it
      e.preventDefault();
      want = REST + MAX * 0.8;
      setTimeout(() => { want = REST; lenV -= 430; ring(0.8); }, 110);
    });

    // Near the rope, it notices you.
    const watch = (e) => {
      if (dragging) return;
      const box = bay.getBoundingClientRect();
      if (!box.width) return;
      lean = Math.max(-3, Math.min(3,
        ((e.clientX - (box.left + box.width / 2)) / box.width) * 7));
    };
    const relax = () => { lean = 0; };
    bay.addEventListener('pointermove', watch);
    bay.addEventListener('pointerleave', relax);

    function ring(force = 1) {
      if (rolling) return;
      angV += (Math.random() < 0.5 ? -1 : 1) * (240 + 220 * force);
      bellV += (Math.random() < 0.5 ? -1 : 1) * (520 + 420 * force);
      clear(burst);
      const petals = Math.round(10 + force * 10);
      for (let i = 0; i < petals; i++) {
        const petal = h('i');
        const angle = (Math.PI * 2 * i) / petals + Math.random() * 0.5;
        const reach = (50 + Math.random() * 80) * (0.6 + force * 0.6);
        petal.style.setProperty('--bx', `${Math.cos(angle) * reach}px`);
        petal.style.setProperty('--by', `${Math.sin(angle) * reach}px`);
        petal.style.setProperty('--br', `${Math.random() * 720 - 360}deg`);
        petal.style.animationDelay = `${Math.random() * 0.12}s`;
        burst.append(petal);
      }
      setTimeout(() => clear(burst), 1100);
      pull();
    }

    bay.stop = () => {
      alive = false;
      cancelAnimationFrame(raf);
      bay.removeEventListener('pointermove', watch);
      bay.removeEventListener('pointerleave', relax);
    };
    return bay;
  }

  /* ============================================================
     the offering
     ============================================================ */

  function paintControls() {
    const catCount = Object.values(c.categories).filter((v) => v === 1).length;
    const banned = Object.values(c.categories).filter((v) => v === 2).length;

    /* Built once and moved, never rebuilt. `paintControls` runs on every
       category click and at both ends of a roll, and a rope rebuilt there is
       a rope that snaps back to dead-centre every time you touch a control —
       and, worse, one that is destroyed by the pull that started the roll, so
       the spring-back and the bell you just earned are thrown away a frame
       after they begin. `mount` moves an existing node rather than copying
       it, so the same rope simply keeps swinging through all of it. */
    rope = rope || suzuRope();

    mount(left, h('div', { style: { display: 'grid', gap: '16px' } },
      h('div.panel.offering',
        h('header', icon('dice', 15), h('h3', 'The offering'),
          h('span.sp', rolling ? 'dealing…' : 'ready')),
        h('div.pad', { style: { display: 'grid', gap: '10px' } },
          rope,
          h('p.muted.rope-note',
            'Every pull is a fresh draw. Nothing is saved and nothing '
            + 'repeats — hold the mods you want to keep before you pull '
            + 'again.'),
          pool
            ? h('div.poolcount',
              h('b', compact(pool.eligible)), h('span', 'mods pass'),
              h('i'), h('span', `${compact(pool.total)} in the pool`))
            : null)),

      h('div.panel',
        h('header', icon('sliders', 15), h('h3', 'Constraints'),
          h('span.sp', `${c.loader} ${c.minecraft}`)),
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
              h('div.why', 'Libraries, dependencies and any mod an add-on '
                + 'drags in come on top and do not count against this. A hand '
                + 'of 120 usually installs around 400 jars.')),

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
              icon('layers', 13), h('span', 'Rebuild the candidate pool')),
            h('button.btn.sm.ghost', {
              onclick: showRules, 'aria-label': 'What the vetting pass enforces',
              title: 'What the vetting pass enforces',
            }, icon('info', 13))),
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

  function showRules() {
    modal({
      title: 'What a hand has to survive',
      wide: true,
      body: frag(
        h('p.muted', { style: { fontSize: '13.5px' } },
          'Every mod is checked against what its publisher declares — the '
          + 'exact Minecraft version, a loader this server can run, and a '
          + 'server-side code path. Then the hand is read as a set rather '
          + 'than as a list, which is where these apply.'),
        h('div.rulelist', { style: { marginTop: '12px' } },
          ruleRow('box', 'A bare library is not a roll',
            'It adds nothing you can point at in game, and if the hand needs '
            + 'it, it is installed as a dependency anyway. Rolling one spends '
            + 'a slot on a jar you were getting for free.'),
          ruleRow('layers', 'An add-on brings its parent, or it leaves',
            'A hand of Create add-ons with no Create in it is not a pack. The '
            + 'parent is brought in where your constraints allow it — as a '
            + 'companion, not as one of your mods — and the add-on is dropped '
            + 'where they do not.'),
          ...(meta.families || []).map((f) => ruleRow(
            f.kind === 'overlap' ? 'activity' : 'alert',
            `${f.title} — at most ${f.limit}`, f.why)))),
      footer: h('button.btn.ghost', { onclick: () => close() },
        h('span', 'Close')),
    });
  }

  function ruleRow(ico, title, body) {
    return h('div.rulerow',
      h('div.rulemark', icon(ico, 14)),
      h('div', h('div.h-num', { style: { fontSize: '13.5px' } }, title),
        h('div.muted', { style: { fontSize: '12.5px' } }, body)));
  }

  /* ============================================================
     the outcome
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
            + 'actually declares, and then the hand is read as a whole: '
            + 'add-ons without their parent, libraries, and mods that refuse '
            + 'to sit beside each other are all taken back out and '
            + 'replaced.'))));
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
            + 'exactly this loader and version, and the set is checked for '
            + 'mods that do not belong together.'))));
      return;
    }

    const s = roll.summary || {};
    const odds = s.odds || {};
    const compat = roll.compatibility || {};
    const hand = roll.hand || [];
    const rolled = hand.filter((m) => !m.adopted_for);
    const brought = hand.length - rolled.length;
    const dropped = roll.dropped || [];

    mount(right, h('div', { style: { display: 'grid', gap: '16px' } },
      h('div.panel',
        h('header', icon('activity', 15), h('h3', 'The odds'),
          h('span.sp', `${rolled.length} rolled`)),
        h('div.pad',
          h(`div.note.${odds.tone === 'bad' ? 'bad'
            : odds.tone === 'good' ? 'ok' : 'warn'}`,
            h('b', odds.title || 'This hand looks workable'),
            h('ul', { style: { margin: '4px 0 0 18px' } },
              ...(odds.reasons || []).map((r) => h('li', r)))),
          h('div.kpis', { style: { marginTop: '14px' } },
            stat('Mods', rolled.length,
              roll.short ? `${roll.short} short of ${roll.constraints.count}`
                : brought ? `plus ${brought} brought in` : 'as asked'),
            stat('Verified', compat.verified ?? 0,
              `declare ${roll.constraints.minecraft} explicitly`, 'ok'),
            stat('Download', bytes((s.download_mb || 0) * 1048576),
              `${s.estimated_jars ?? '?'} jars with dependencies`, '', true),
            stat('Heap needed', `${s.heap_gb ?? '?'} GB`,
              s.heap_ceiling_gb
                ? `this host can give ${s.heap_ceiling_gb} GB` : '',
              s.heap_gb > s.heap_ceiling_gb ? 'bad' : 'ok', true)),
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
              icon('archive', 13), h('span', 'What is in the export?'))))),

      h('div.panel.outcome',
        h('header', icon('layers', 15), h('h3', 'What came back'),
          h('span.sp', `${hand.length} jars`)),
        h('div.facebar',
          segmented([
            { value: 'hand', label: `The hand · ${hand.length}` },
            { value: 'lineage', label: 'Lineage' },
            { value: 'dropped', label: `Turned away · ${dropped.length}` },
          ], face, (v) => { face = v; paintFace(); }, 'What came back')),
        h('div.faces', faceHost))));

    paintFace();
  }

  const faceHost = h('div');

  function paintFace() {
    if (!roll) return;
    if (face === 'lineage') mount(faceHost, lineageFace());
    else if (face === 'dropped') mount(faceHost, droppedFace());
    else mount(faceHost, h('div.reel.dealing', ...(roll.hand || []).map(handRow)));
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
    const companion = !!m.adopted_for;
    return h(`div.reelrow${held ? '.held' : ''}${companion ? '.companion' : ''}`,
      m.logo ? h('img.ico', { src: m.logo, alt: '', loading: 'lazy' })
        : h('div.ico', { style: { display: 'grid', placeItems: 'center' } },
          icon('box', 14)),
      h('div.who',
        h('div.nm', m.name),
        h('div.sub', [
          m.build_version || m.file_name,
          m.size ? bytes(m.size) : null,
          m.category,
        ].filter(Boolean).join(' · ')),
        h('div.chiprow', { style: { marginTop: '3px' } },
          companion
            ? pill(`brought in by ${m.adopted_for}`, 'gold',
              'Its add-on was rolled, so the mod it adds to came with it. '
              + 'It does not count against your mod count.')
            : null,
          m.dependency_of ? pill('dependency', 'plum') : null,
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
            ? pill(m.release_type, 'warn') : null)),
      h('div', { style: { display: 'flex', gap: '5px', flex: 'none' } },
        companion ? null : h('button.btn.icon.xs.ghost', {
          'aria-label': held ? `Release ${m.name}`
            : `Hold ${m.name} through the next pull`,
          title: held ? 'Held through the next pull' : 'Hold through the next pull',
          onclick: () => {
            if (held) holds.delete(m.name); else holds.add(m.name);
            paintFace();
          },
        }, icon(held ? 'star' : 'heart', 12)),
        companion ? null : h('button.btn.icon.xs.ghost', {
          'aria-label': `Reroll ${m.name}`, title: 'Reroll just this slot',
          onclick: () => rerollOne(m),
        }, icon('refresh', 12))));
  }

  /* --- lineage: the Inspector's read of the hand ---------------
     Borrowed from Modpack-Inspector, which answers these same
     questions about a pack that is already installed by reading
     the jars. Nothing is installed yet, so they are answered from
     what the publishers declared instead — which is thinner, and
     arrives early enough to change your mind. */

  function lineageFace() {
    const insp = roll.inspection || {};
    const families = insp.families || [];
    const libs = insp.libraries || [];
    const unresolved = insp.unresolved || [];
    const clashes = insp.conflicts || [];
    const hand = roll.hand || [];

    /* A partition, not three overlapping counts. Every mod in the hand is
       exactly one of these: something you rolled outright, something you
       rolled that turns out to be an add-on, or something that came in
       because one of its add-ons was rolled. The first version drew
       "content", "add-ons" and "brought in" as parallel bars, and since
       add-ons and companions are both content the three segments summed to
       158 of 128 — a bar that was longer than the thing it described. */
    const companions = new Set(hand.filter((m) => m.adopted_for).map((m) => m.name));
    const rows = insp.rows || [];
    const addons = rows.filter((r) => r.parent && !companions.has(r.name)).length;
    const brought = companions.size;
    const alone = Math.max(0, hand.length - addons - brought);

    return h('div.pad', { style: { display: 'grid', gap: '16px' } },
      h('div.compo', { 'aria-hidden': 'true' },
        compoBar('content', alone, hand.length, 'on their own'),
        compoBar('addons', addons, hand.length, 'add-ons'),
        compoBar('brought', brought, hand.length, 'brought in')),
      h('div.chiprow',
        pill(`${alone} on their own`, 'ok'),
        addons ? pill(`${addons} add-ons`, 'plum') : null,
        brought ? pill(`${brought} brought in`, 'gold') : null,
        libs.length ? pill(`${libs.length} libraries will follow`, 'info') : null,
        unresolved.length
          ? pill(`${unresolved.length} unread dependencies`, 'warn') : null),

      clashes.length
        ? section('Still clashing', 'alert',
          'Nothing could be dropped to resolve these, so they are yours to '
          + 'decide on.',
          ...clashes.map((x) => h('div.lin-row.bad',
            h('div.who', h('div.nm', x.title),
              h('div.fn.wrapany', `${x.why} — ${x.members.join(', ')}`)))))
        : null,

      families.length
        ? section('Families', 'layers',
          'An add-on and the mod it adds to. Every parent here is in the '
          + 'hand — that is what the vetting pass guarantees.',
          ...families.map((f) => h('div.lin-row',
            h('div.lin-parent',
              h('span.nm', f.parent),
              f.in_hand ? pill('in hand', 'ok') : pill('missing', 'bad')),
            h('div.lin-kids', ...f.members.map((m) => h('span.kid', m))),
            h('div.fn', f.how === 'declared'
              ? 'declared as a required dependency'
              : f.how === 'category'
                ? 'filed under this mod family on CurseForge'
                : 'named after it'))))
        : null,

      libs.length
        ? section('Libraries the install will add', 'box',
          'These do not count against your mod count and are not rolled — '
          + 'they come because something in the hand needs them.',
          h('div.libgrid', ...libs.slice(0, 40).map((l) => h('div.lib',
            h('span.nm', l.name),
            h('span.fn', l.needed_by.length === 1
              ? l.needed_by[0]
              : `${l.needed_by.length} mods need it`)))))
        : null,

      unresolved.length
        ? section('Dependencies nobody could read', 'info',
          'The catalogue would not say what these point at. They are still '
          + 'resolved at install time; this is only what could not be shown '
          + 'here.',
          ...unresolved.slice(0, 20).map((u) => h('div.lin-row',
            h('div.who', h('div.nm', u.name),
              h('div.fn.wrapany', u.refs.join(', '))))))
        : null,

      !families.length && !libs.length && !unresolved.length && !clashes.length
        ? h('p.muted', { style: { fontSize: '13px' } },
          'Nothing in this hand depends on anything else in it, and no mod '
          + 'here is an add-on of another. That is a perfectly good pack — '
          + 'it is just a flat one.')
        : null);
  }

  function section(title, ico, blurb, ...body) {
    return h('div.lin-sec',
      h('div.lin-head', icon(ico, 14), h('h4.h-num', title)),
      blurb ? h('p.muted', { style: { fontSize: '12.5px' } }, blurb) : null,
      ...body);
  }

  function compoBar(kind, n, total, label) {
    const pct = total ? Math.max(0, (n / total) * 100) : 0;
    return h(`div.compo-part.${kind}`, {
      // 0.0001 rather than 0 so an empty segment collapses to its min-width
      // instead of the browser dropping it and the colours shifting along.
      style: { flexGrow: String(Math.max(0.0001, pct)) },
      title: `${n} ${label} of ${total}`,
    });
  }

  function droppedFace() {
    const dropped = roll.dropped || [];
    if (!dropped.length) {
      return h('div.pad', h('p.muted', { style: { fontSize: '13px' } },
        'Nothing was turned away. Every mod dealt had a verified build and '
        + 'belonged in the hand — which on a well-supported version is '
        + 'normal, and on a fresh one is luck.'));
    }
    const byKind = {};
    for (const d of dropped) (byKind[d.kind || 'other'] ||= []).push(d);
    const kinds = Object.keys(byKind).sort(
      (a, b) => (KIND_ORDER.indexOf(a) + 99) % 99 - (KIND_ORDER.indexOf(b) + 99) % 99);
    return h('div.pad', { style: { display: 'grid', gap: '14px' } },
      h('p.muted', { style: { fontSize: '13px' } },
        'Each of these was dealt and then taken back out, and a replacement '
        + 'was dealt in its place. This is the check that stops a pack '
        + 'installing cleanly and coming apart on its first boot.'),
      ...kinds.map((kind) => h('div',
        h('div.lin-head', icon('minus', 13),
          h('h4.h-num', `${KIND_LABEL[kind] || kind} — ${byKind[kind].length}`)),
        h('div.modlist', ...byKind[kind].slice(0, 40).map((d) => h('div.modrow',
          h('div.who', h('div.nm', d.name),
            h('div.fn.wrapany', d.reason))))),
        byKind[kind].length > 40
          ? h('p.muted', { style: { fontSize: '12px' } },
            `…and ${byKind[kind].length - 40} more`) : null)));
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
        constraints: c, holds: [...holds],
      }, { title: 'Dealing a hand', quiet: true });
      pool = roll.pool;
      face = 'hand';
      if (roll.short) toast(roll.fill_note, 'warn', { timeout: 12000 });
      else {
        const brought = roll.adopted || 0;
        toast(`${(roll.hand || []).length - brought} mods dealt`
          + (brought ? `, and ${brought} brought in with them.` : '.'),
        'ok', { timeout: 4000 });
      }
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
        constraints: c, hand: roll.hand, mod: m.name,
      });
      roll = {
        ...roll,
        hand: res.hand,
        summary: res.summary,
        compatibility: res.compatibility || roll.compatibility,
        inspection: res.inspection || roll.inspection,
        dropped: [...(roll.dropped || []), ...(res.dropped || [])],
      };
      paintHand();
    } catch (e) { toastError(e); }
  }

  async function previewExport() {
    try {
      const p = await api.post('/api/roulette/preview-export',
        { hand: roll.hand, constraints: c, roll_id: roll.roll_id });
      modal({
        title: 'The export',
        body: frag(
          h('div.kpis', { style: { marginBottom: '12px' } },
            h('div.kpi', h('div.lbl', 'In the manifest'),
              h('div.v', String(p.files)), h('div.sub', 'CurseForge projects')),
            h('div.kpi', h('div.lbl', 'Bundled'),
              h('div.v', String(p.bundled)), h('div.sub', 'Modrinth-only jars'))),
          h('p', p.note),
          p.portable
            ? h('p.muted', { style: { fontSize: '12.5px' } },
              `The manifest targets ${p.loader_id}, so the CurseForge app `
              + 'will import it as well as BlessForge.')
            : h('div.note.warn',
              h('b', 'The CurseForge app will refuse this one'),
              `No ${roll.constraints.loader} build is listed for `
              + `${roll.constraints.minecraft}, so the manifest can only name `
              + 'the loader family. BlessForge imports it either way.'),
          h('p.muted', { style: { fontSize: '12.5px' } },
            'The zip is written when you install the hand, and kept — the '
            + "server's own Overview hands it back afterwards.")),
        footer: h('button.btn.ghost', { onclick: () => close() },
          h('span', 'Close')),
      });
    } catch (e) { toastError(e); }
  }

  async function install() {
    const used = new Set(state.instances.map((x) => x.port));
    let port = 25565;
    while (used.has(port) && port < 25700) port += 1;

    const stamp = new Date().toLocaleDateString(undefined,
      { month: 'short', day: 'numeric' });
    const nameInput = h('input.inp', { value: `Roulette ${stamp}`, maxLength: 60 });
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
          + 'loader ladder, same manifest. The zip is kept, and the server\'s '
          + 'Overview hands it back whenever you want it.'),
        h('div.field', h('label', 'Server name'), nameInput),
        h('div.field', h('label', 'Port'), portInput),
        toggle('Tune the JVM for this host', true, (v) => { opts.optimize = v; })),
      footer: frag(
        h('button.btn.ghost', { onclick: () => close() }, h('span', 'Cancel')),
        h('button.btn.primary', {
          onclick: () => {
            close();
            const name = nameInput.value.trim() || `Roulette ${stamp}`;
            run('/api/roulette/install', {
              hand: roll.hand, constraints: c, roll_id: roll.roll_id,
              server_name: name,
              port: Number(portInput.value),
              optimize: opts.optimize,
            }, {
              title: `Rolling ${name}`,
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
  return {
    node,
    // The rope runs a rAF loop for as long as it is on screen, so leaving
    // the screen has to stop it. Without this every visit to the roulette
    // leaves another integrator running for the life of the tab.
    dispose() { rope?.stop?.(); },
  };
}
