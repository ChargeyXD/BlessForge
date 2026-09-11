/* ============================================================
   Racks — the fleet as ema plaques hung on hooks.

   A grouping screen is normally an accordion, and an accordion
   of servers tells you nothing you could not read from a list.
   This is the shrine's own filing system instead: each group is
   a torii lintel with a rope strung under it, and every server
   is a wooden ema plaque hanging off that rope by a cord. The
   plaques sway, each at its own angle and phase, so a rack reads
   as hand-hung rather than as a grid — and a plaque STRAIGHTENS
   when you point at it, the way you would tilt one up to read
   what is written on the back.

   Ungrouped servers hang in "the open yard", which is a real
   rack with a ghost lintel rather than a hidden bucket, because
   the thing you most want to see on this screen is what you have
   not filed yet.

   Three things this screen has to survive, and how:

     * A server deleted, renamed or added by somebody else while
       you are looking. Nothing here holds its own copy of the
       fleet. Membership is resolved at paint time by looking up
       `assign[server_id]` against whatever `state.instances`
       currently says, and app.js repaints this screen on every
       poll. A deleted server stops appearing, a renamed one
       paints its new name (assignment is keyed on Crafty's
       server_id, which a rename does not change), and a new one
       appears in the open yard.
     * A second tab editing the same racks. Every write returns
       the whole document and the client adopts it wholesale, so
       the loser of a race corrects itself on the next poll.
     * /data not being writable. The API says `persisted:false`
       and this screen says so out loud rather than pretending
       the change was kept.
   ============================================================ */

import {
  h, hl, frag, mount, icon, api, toast, toastError, empty, titleCase,
  confirmDialog, modal, close as closeModal, segmented, prefs, skeletonGrid,
} from '../core.js';
import {
  state, refreshInstances, refreshGroups, adoptGroups, go, topActions,
  bannerIfUnhealthy, onState, MOTIFS, motif, lastRan,
} from '../app.js';

/* The open yard is not a stored group — it is whatever is left over. Given
   an id anyway so every code path below can treat it as one more rack
   instead of branching on "is this the leftovers" six times. */
const YARD = '__yard__';

let dragging = null;         // ids in flight, for the duration of a drag

/* --- the screen ----------------------------------------------- */

export async function render() {
  const node = h('div.wrap.wide');
  let mode = prefs.get('racks.mode', 'rack');
  const picked = new Set();
  let unsub = null;

  const ctx = {
    get mode() { return mode; },
    picked,
    toggle(id) {
      if (picked.has(id)) picked.delete(id); else picked.add(id);
      paint();
    },
    open: (s) => go(`/i/${s.server_id}/overview`),
    hang: async (servers) => { await openHangPicker(servers); paint(); },
    shift: async (s, delta) => { await shiftRack(s, delta); paint(); },
    paint: () => paint(),
  };

  topActions(
    h('a.btn.sm.ghost', { href: '#/' }, icon('server', 14),
      h('span', 'Fleet grid')),
    h('button.btn.sm.primary', {
      onclick: async () => { if (await newRack()) paint(); },
    }, hl(), icon('plus', 14), h('span', 'New rack')),
  );

  function racks() {
    // Stored racks in their own order, then the leftovers. The yard is last
    // on purpose: it is the pile you are working THROUGH, and a pile you are
    // emptying belongs at the bottom of the page, not the top.
    const all = state.instances;
    const assign = state.groups.assign || {};
    const byId = new Map((state.groups.groups || []).map((g) => [g.id, []]));
    const yard = [];
    for (const s of all) {
      const list = byId.get(assign[s.server_id]);
      if (list) list.push(s); else yard.push(s);
    }
    const out = (state.groups.groups || []).map((g) => ({
      group: g, members: sortMembers(byId.get(g.id) || []),
    }));
    out.push({
      group: { id: YARD, name: 'The open yard', motif: 'yuki' },
      members: sortMembers(yard),
    });
    return out;
  }

  function paint() {
    const all = state.instances;
    const groups = state.groups.groups || [];
    const assign = state.groups.assign || {};
    const hung = all.filter((s) => assign[s.server_id]).length;

    // Anything picked that has since been deleted in Crafty must not keep a
    // ghost in the tray and must not be sent to the API on the next action.
    for (const id of [...picked]) {
      if (!state.instanceById.has(id)) picked.delete(id);
    }

    mount(node,
      bannerIfUnhealthy(),

      h('div.sec-head.mon',
        h('div',
          h('p.eyebrow', 'Shrine grounds · ',
            h('b', groups.length
              ? `${groups.length} rack${groups.length > 1 ? 's' : ''}`
              : 'nothing hung yet')),
          h('h2', 'The rack')),
        h('div.grow'),
        groups.length ? segmented([
          { value: 'rack', label: 'Plaques' },
          { value: 'list', label: 'Hooks' },
        ], mode, (v) => { mode = v; prefs.set('racks.mode', v); paint(); },
          'How to draw each rack') : null,
      ),

      !all.length
        ? (state.health?.ready
          ? empty('fox-mascot.png', 'Nothing to organise yet',
            'Racks group servers, and Crafty has none. Make one, or install '
            + 'a modpack, and it will turn up in the open yard.',
            h('a.btn.primary', { href: '#/create' }, hl(),
              h('span', 'Create a server')),
            h('a.btn', { href: '#/discover' }, hl(),
              h('span', 'Browse modpacks')))
          : empty('chargey-peek.png', 'Not connected to Crafty yet',
            'Set CRAFTY_URL and CRAFTY_TOKEN, then reload. Settings lists '
            + 'exactly what is missing.',
            h('a.btn.primary', { href: '#/settings' }, hl(),
              h('span', 'Settings'))))
        : !groups.length
          ? firstRack(all, paint)
          : frag(
            h('p.rack-hint',
              icon('info', 13),
              h('span',
                h('b', String(hung)), ` of ${all.length} hung. `,
                'Point at a plaque and press ',
                h('kbd', 'G'), ' to move it, ', h('kbd', 'Space'),
                ' to pick it up, ', h('kbd', 'Enter'),
                ' to open the server. Dragging works too.')),
            h('div.racks.stagger', ...racks().map((r) =>
              rack(r.group, r.members, ctx))),
          ),

      picked.size ? tray(picked, ctx) : null,
    );
  }

  if (!state.instances.length && state.health?.ready) {
    mount(node, skeletonGrid(4));
    await Promise.all([
      refreshInstances({ quiet: true }).catch(() => {}),
      refreshGroups(),
    ]);
  } else {
    refreshGroups();
  }
  paint();
  unsub = onState(paint);

  return { node, dispose: () => { unsub?.(); dragging = null; } };
}

/* Running first, then by name. A rack is something you scan, and the one
   question you scan it for is which of these is up. */
function sortMembers(list) {
  const rank = (s) => (s.state === 'running' ? 0
    : ['crashed', 'orphan', 'incomplete'].includes(s.state) ? 1 : 2);
  return [...list].sort((a, b) => rank(a) - rank(b)
    || String(a.name || '').localeCompare(String(b.name || '')));
}

/* --- one rack -------------------------------------------------- */

function rack(group, members, ctx) {
  const m = motif(group.motif);
  const yard = group.id === YARD;
  const titleId = `rackname-${group.id}`;

  const section = h('section.rack', {
    'aria-labelledby': titleId,
    'data-yard': yard ? '1' : null,
    ondragover: (e) => {
      if (!dragging) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      section.classList.add('drop-on');
    },
    ondragleave: (e) => {
      if (!section.contains(e.relatedTarget)) section.classList.remove('drop-on');
    },
    ondrop: async (e) => {
      e.preventDefault();
      section.classList.remove('drop-on');
      const ids = dragging;
      dragging = null;
      if (!ids || !ids.length) return;
      const servers = ids.map((id) => state.instanceById.get(id)).filter(Boolean);
      if (!servers.length) return;
      await assign(ids, yard ? null : group.id, group);
      ctx.picked.clear();
      ctx.paint();
    },
  },
    h('div.lintel',
      h('span.kasagi', { 'aria-hidden': 'true' }),
      h('span.nuki', { 'aria-hidden': 'true' }),
      h('span.post.l', { 'aria-hidden': 'true' }),
      h('span.post.r', { 'aria-hidden': 'true' }),
      h('span.shide', { 'aria-hidden': 'true' }),
      h('div.plate',
        h('span.motif-glyph', { 'aria-hidden': 'true' }, m.glyph),
        h('h3', { id: titleId }, group.name),
        h('span.tally.tnum', {
          title: `${members.length} server${members.length === 1 ? '' : 's'}`,
        }, String(members.length)),
        members.some((s) => s.state === 'running')
          ? h('span.lit', { title: 'something in here is up' },
            h('span.dot.live', { 'aria-hidden': 'true' }),
            `${members.filter((s) => s.state === 'running').length} up`)
          : null,
      ),
      h('div.rack-acts',
        members.length ? h('button.btn.xs.ghost', {
          onclick: () => ctx.hang(members),
          'aria-label': `Move all ${members.length} in ${group.name}`,
        }, icon('layers', 13), h('span', 'Move all')) : null,
        yard ? null : h('button.btn.icon.xs.ghost', {
          'aria-label': `Rename ${group.name}`,
          onclick: async () => {
            const next = await rackDialog({
              title: `Rename ${group.name}`, name: group.name,
              chosen: group.motif, confirmLabel: 'Save',
            });
            if (!next) return;
            await saveRack({ id: group.id, ...next });
            ctx.paint();
          },
        }, icon('edit', 13)),
        yard ? null : h('button.btn.icon.xs.ghost', {
          'aria-label': `Take down ${group.name}`,
          onclick: async () => {
            const ok = await confirmDialog({
              title: `Take down ${group.name}?`,
              message: members.length
                ? `The rack goes; its ${members.length} server`
                  + `${members.length === 1 ? '' : 's'} go back to the open `
                  + 'yard. Nothing is deleted in Crafty.'
                : 'The rack is empty, so this only removes the rack itself.',
              confirmLabel: 'Take it down',
              danger: true,
            });
            if (!ok) return;
            await deleteRack(group);
            ctx.paint();
          },
        }, icon('trash', 13)),
      ),
    ),

    members.length
      ? (ctx.mode === 'list'
        ? h('div.hooklist', ...members.map((s) => hookrow(s, ctx)))
        : h('div.plaques', ...members.map((s, i) => plaque(s, i, ctx))))
      : h('div.rack-bare', yard
        ? 'Every server is hung. The yard is swept.'
        : 'This rack is empty. Drag a plaque onto it, or pick one and press G.'),
  );
  section.style.setProperty('--g-ink', m.ink);
  section.style.setProperty('--g-soft', m.soft);
  return section;
}

/* --- one plaque ------------------------------------------------
   An ema is a small board with a short prayer on it, not a form.
   The board answers, in this order and from across the room:

     whose it is      the name, the biggest thing on the wood
     is it up         a burnt-in band, in WORDS, with its own
                      icon and its own weight of fill — never a
                      coloured dot on its own, because a green
                      dot and a grey dot are the same dot to a
                      lot of people
     how busy         players, but only while there is a server
                      running to have players on
     what it is       version and loader, then port and mods,
                      two short mono lines in the hand of
                      somebody who wrote it on with a brush
     does it want me  one line, worst first, and only when true

   Everything else this screen knows about a server — CPU, RAM,
   the modpack, the problems in detail — belongs to the fleet
   grid and to the instance screen. A rack is for standing back
   from, and a board you have to lean in to read is a table.
   ------------------------------------------------------------ */

/* The word is the state. The icon is a second, independent channel, and the
   band's fill is a third: solid and dark for the two states that mean
   something is happening, pale for the two that mean nothing is. */
const PLQ_STATE = {
  running: { word: 'running', icon: 'play' },
  stopped: { word: 'stopped', icon: 'stop' },
  crashed: { word: 'crashed', icon: 'alert' },
  orphan: { word: 'orphaned', icon: 'ban' },
  incomplete: { word: 'half-finished', icon: 'wrench' },
};

/* The one line on the board that is not a fact about the server but a fact
   about you: something here wants a hand. Worst first, and never more than
   one — a board that lists three faults is a bug report, not a plaque. The
   states that ARE the fault say so in the band already and are not repeated
   here. */
function needsEye(s) {
  if (s.reachable === false) return 'Crafty cannot reach it';
  const n = s.problems || 0;
  if (n > 0) return `${n} problem${n > 1 ? 's' : ''}`;
  return null;
}

/* Players, and only while it is running. A stopped server reporting 0/20
   reads as "nobody came", which is a sadder and quite different statement
   from "it is off". */
function crowd(s) {
  if (s.state !== 'running') return null;
  const n = Number(s.players);
  if (!Number.isFinite(n)) return null;
  return Number(s.max_players) > 0 ? `${n}/${s.max_players}` : String(n);
}

function plaque(s, i, ctx) {
  const m = motif(currentMotif(s));
  const chosen = ctx.picked.has(s.server_id);
  const key = PLQ_STATE[s.state] ? s.state : 'unknown';
  const st = PLQ_STATE[key] || { word: s.state || 'unknown', icon: 'alert' };
  const name = s.name || s.server_id.slice(0, 8);
  const here = crowd(s);
  const eye = needsEye(s);

  // Both written lines fall back rather than disappear: a board with a gap
  // where a line should be looks broken, and a board that says it does not
  // know looks deliberate.
  const built = [s.minecraft, s.loader].filter(Boolean).join(' · ');
  const refs = [
    s.port ? `:${s.port}` : null,
    s.mod_count > 0 ? `${s.mod_count} mods` : null,
  ].filter(Boolean);

  const el = h('div.ema-plaque', {
    role: 'link', tabIndex: 0,
    'aria-label': [
      `${name} — ${st.word}`,
      here ? `${here} players` : null,
      built || null,
      eye,
    ].filter(Boolean).join(', '),
    'data-state': key,
    'data-picked': chosen ? '1' : null,
    draggable: true,
    onclick: (e) => { if (!e.target.closest('button')) ctx.open(s); },
    onkeydown: (e) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === 'Enter') { e.preventDefault(); ctx.open(s); }
      else if (e.key === ' ') { e.preventDefault(); ctx.toggle(s.server_id); }
      else if (e.key === 'g' || e.key === 'G') {
        e.preventDefault();
        ctx.hang(ctx.picked.size ? pickedServers(ctx.picked) : [s]);
      } else if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowRight')) {
        e.preventDefault(); ctx.shift(s, 1);
      } else if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowLeft')) {
        e.preventDefault(); ctx.shift(s, -1);
      }
    },
    ondragstart: (e) => {
      dragging = chosen && ctx.picked.size ? [...ctx.picked] : [s.server_id];
      e.dataTransfer.effectAllowed = 'move';
      // Some browsers cancel a drag that carries no payload at all.
      try { e.dataTransfer.setData('text/plain', dragging.join(' ')); } catch { /* */ }
      el.classList.add('lifting');
    },
    ondragend: () => { dragging = null; el.classList.remove('lifting'); },
  },
    // The rope and the cord are SIBLINGS of the board, not part of it.
    // Everything that moves -- the hand-hung angle, the sway, the lift and
    // turn on hover -- is applied to `.plq-body`, so the strung rope above
    // a row of plaques is never inside a transform and simply cannot drift.
    // It was a child once, and three separate counter-transforms could not
    // cancel a parent's rotate, translate and scale exactly.
    //
    // `.hanger` is ONE ROW'S WHOLE ROPE, not this plaque's share of it. It
    // is positioned against `.plaques` with left and right set and `top`
    // left at auto, so it spans the rack horizontally and keeps its static
    // position vertically -- the top of the grid row this plaque landed in.
    // Every plaque in a row therefore draws the same rope in the same place
    // and they coincide exactly; a row that wraps gets its own; a rack with
    // one plaque gets one whole rope with one board on it. Nothing has to
    // know how many columns the grid wrapped at, which is the one thing no
    // amount of CSS could have told it.
    //
    // For that to hold, `.ema-plaque` must never become a containing block:
    // no position other than static, no transform, no perspective, no
    // filter. The cord cannot use the plaque as its origin either, so it
    // gets `.plq-rig` -- a zero-height marker at the top of the board that
    // is positioned, and is the only thing here that is.
    h('span.hanger', { 'aria-hidden': 'true' }),
    h('span.plq-rig', { 'aria-hidden': 'true' }, h('span.cord')),

    h('div.plq-body',
    hl(),                                   // blob layer 0; fill is ::after
    h('div.plq-top',
      h('span.burn', { 'aria-hidden': 'true' }, m.glyph),
    ),
    h('h4.plq-name', {
      title: s.pack?.name ? `${name} — ${s.pack.name}` : name,
    }, name),

    h(`div.plq-state.st-${key}`, {
      title: here ? `${here} players online` : st.word,
    },
      icon(st.icon, 11),
      h('span.sw', st.word),
      key === 'running'
        ? h('span.dot.live', { 'aria-hidden': 'true' }) : null,
      here ? h('span.sn.tnum', here) : null,
    ),

    h('div.plq-meta.mono.trunc', built || 'version not recorded'),
    refs.length ? h('div.plq-ref.mono.trunc', refs.join(' · ')) : null,
    eye
      ? h('div.plq-eye.mono', icon('alert', 11), h('span.trunc', eye))
      : null,

    h('div.plq-ran.mono.trunc', lastRan(s)),

    h('button.plq-pick', {
      type: 'button',
      'aria-pressed': String(chosen),
      'aria-label': `${chosen ? 'Put down' : 'Pick up'} ${s.name || 'this server'}`,
      onclick: (e) => { e.stopPropagation(); ctx.toggle(s.server_id); },
    }, icon(chosen ? 'check' : 'plus', 12)),

    h('button.plq-hang', {
      type: 'button',
      'aria-label': `Move ${s.name || 'this server'} to another rack`,
      onclick: (e) => { e.stopPropagation(); ctx.hang([s]); },
    }, icon('layers', 12)),
    ),
  );

  // Hand-hung, not stamped: a small deterministic angle per position, and a
  // sway phase that is not in step with its neighbour's.
  el.style.setProperty('--plq-rot', `${(((i * 37) % 9) - 4) * 0.45}deg`);
  el.style.setProperty('--plq-delay', `-${((i * 13) % 9) * 0.7}s`);
  el.style.setProperty('--g-ink', m.ink);
  el.style.setProperty('--g-soft', m.soft);
  return el;
}

/* The compact rendering. Same rack, same rope, one row per hook — for the
   day the fleet is forty servers and forty swaying plaques is a wall. */
function hookrow(s, ctx) {
  const chosen = ctx.picked.has(s.server_id);
  const key = PLQ_STATE[s.state] ? s.state : 'unknown';
  const st = PLQ_STATE[key] || { word: s.state || 'unknown', icon: 'alert' };
  const here = crowd(s);
  return h('div.hookrow', {
    role: 'link', tabIndex: 0,
    'aria-label': `${s.name || 'server'} — ${st.word}`,
    'data-picked': chosen ? '1' : null,
    draggable: true,
    onclick: (e) => { if (!e.target.closest('button')) ctx.open(s); },
    onkeydown: (e) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === 'Enter') { e.preventDefault(); ctx.open(s); }
      else if (e.key === ' ') { e.preventDefault(); ctx.toggle(s.server_id); }
      else if (e.key === 'g' || e.key === 'G') {
        e.preventDefault();
        ctx.hang(ctx.picked.size ? pickedServers(ctx.picked) : [s]);
      }
    },
    ondragstart: (e) => {
      dragging = chosen && ctx.picked.size ? [...ctx.picked] : [s.server_id];
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', dragging.join(' ')); } catch { /* */ }
    },
    ondragend: () => { dragging = null; },
  },
    h('button.hook-pick', {
      type: 'button',
      'aria-pressed': String(chosen),
      'aria-label': `${chosen ? 'Put down' : 'Pick up'} ${s.name || 'this server'}`,
      onclick: (e) => { e.stopPropagation(); ctx.toggle(s.server_id); },
    }, icon(chosen ? 'check' : 'plus', 12)),
    // Same rule as the plaque: the state is a word, and the dot is only its
    // second channel. A row of identical grey circles is not a status.
    h(`span.hk-state.hk-${key}`, { title: st.word },
      h(`span.state.${s.state || 'stopped'}`, { 'aria-hidden': 'true' }),
      h('span.hk-w', st.word)),
    h('span.nm.trunc', { title: s.name || s.server_id },
      s.name || s.server_id.slice(0, 8)),
    h('span.mt.mono.trunc', [s.loader, s.minecraft].filter(Boolean).join(' · ')),
    h('span.rn.mono.trunc', here ? `${here} · ${lastRan(s)}` : lastRan(s)),
    h('button.btn.icon.xs.ghost', {
      'aria-label': `Move ${s.name || 'this server'} to another rack`,
      onclick: (e) => { e.stopPropagation(); ctx.hang([s]); },
    }, icon('layers', 13)),
  );
}

/* --- the carry tray -------------------------------------------- */

function tray(picked, ctx) {
  const n = picked.size;
  return h('div.carry', { role: 'status' },
    h('span.carry-n.tnum', String(n)),
    h('span.carry-t', `plaque${n === 1 ? '' : 's'} in hand`),
    h('div.grow'),
    h('button.btn.sm.primary', {
      onclick: () => ctx.hang(pickedServers(picked)),
    }, hl(), icon('layers', 14), h('span', 'Hang in…')),
    h('button.btn.sm.ghost', {
      onclick: async () => {
        await assign([...picked], null, null);
        picked.clear();
        ctx.paint();
      },
    }, icon('x', 13), h('span', 'Take down')),
    h('button.btn.icon.sm.ghost', {
      'aria-label': 'Put them all back',
      onclick: () => { picked.clear(); ctx.paint(); },
    }, icon('refresh', 14)),
  );
}

function pickedServers(picked) {
  return [...picked].map((id) => state.instanceById.get(id)).filter(Boolean);
}

/* --- the empty state ------------------------------------------
   No racks yet. The worst version of this screen is a blank page
   with an "Add group" button, because naming eight groups from
   nothing IS configuration homework. So the fleet is read first
   and three racks are offered PRE-INKED — cut from the servers
   that are actually there — and each is one tap to hang.
   ------------------------------------------------------------ */

function firstRack(all, repaint) {
  const plans = suggestions(all);

  const card = (plan) => {
    const el = h('button.inked', {
      type: 'button',
      onclick: async () => {
        const doc = await api.post('/api/fleet/groups/plan', {
          groups: plan.groups,
        }).catch((e) => { toastError(e, 'Could not hang those'); return null; });
        if (!doc) return;
        adoptGroups(doc);
        toast(`${plan.groups.length} racks hung, `
          + `${plan.groups.reduce((n, g) => n + g.server_ids.length, 0)} `
          + 'plaques on them. Take any of them down if it is not how you '
          + 'think.', 'ok', { timeout: 6000 });
        repaint();
      },
    },
      h('span.ink-glyph', { 'aria-hidden': 'true' }, plan.glyph),
      h('span.ink-t', plan.title),
      h('span.ink-s', plan.groups.map((g) => g.name).slice(0, 4).join(' · ')
        + (plan.groups.length > 4 ? ' …' : '')),
      h('span.ink-n.mono', `${plan.groups.length} racks`),
    );
    el.style.setProperty('--g-ink', motif(plan.groups[0].motif).ink);
    el.style.setProperty('--g-soft', motif(plan.groups[0].motif).soft);
    return el;
  };

  return h('div.firstrack',
    h('div.fr-rope', { 'aria-hidden': 'true' }),
    h('h3', 'Nothing is hung yet'),
    h('p.lede',
      `All ${all.length} server${all.length === 1 ? '' : 's'} are loose in the `
      + 'open yard. A rack is a name and a mark — nothing is moved, copied or '
      + 'restarted by making one. These are cut from your own fleet, so the '
      + 'first one costs a single tap.'),
    plans.length
      ? h('div.inkgrid', ...plans.map(card))
      : h('p.muted', 'There is only one server, so there is nothing to sort '
        + 'it by yet. Name a rack anyway if you are about to add more.'),
    h('div.btnrow', { style: { justifyContent: 'center', marginTop: '18px' } },
      h('button.btn.ghost', {
        onclick: async () => { if (await newRack()) repaint(); },
      }, hl(), icon('edit', 14), h('span', 'Name one myself')),
    ),
  );
}

function suggestions(all) {
  const out = [];
  const bucket = (keyOf, nameOf) => {
    const map = new Map();
    for (const s of all) {
      const k = keyOf(s);
      if (!k) continue;
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(s.server_id);
    }
    const keys = [...map.keys()].sort();
    return keys.map((k, i) => ({
      name: nameOf(k), motif: Object.keys(MOTIFS)[i % Object.keys(MOTIFS).length],
      server_ids: map.get(k),
    }));
  };

  const loaders = bucket((s) => s.loader, (k) => titleCase(k));
  if (loaders.length > 1) {
    out.push({ title: 'By loader', glyph: '工', groups: loaders });
  }
  const versions = bucket((s) => s.minecraft, (k) => `MC ${k}`);
  if (versions.length > 1) {
    out.push({ title: 'By Minecraft version', glyph: '版', groups: versions });
  }
  const kinds = bucket(
    (s) => ((s.mod_count || 0) > 0 || s.pack?.source === 'curseforge'
      || s.pack?.source === 'modrinth' ? 'modded' : 'plain'),
    (k) => (k === 'modded' ? 'Modded' : 'Plain'));
  if (kinds.length > 1) {
    out.push({ title: 'Modded and plain', glyph: '分', groups: kinds });
  }
  return out.slice(0, 3);
}

/* --- writes ----------------------------------------------------- */

function currentMotif(s) {
  const g = (state.groups.groups || [])
    .find((x) => x.id === (state.groups.assign || {})[s.server_id]);
  return g ? g.motif : 'yuki';
}

function saidPersisted(doc) {
  if (doc && doc.persisted === false) {
    toast('Kept for this session only — /data is not writable, so the racks '
      + 'will not survive a restart.', 'warn', { timeout: 7000 });
  }
}

async function assign(ids, groupId, group) {
  const live = ids.filter((id) => state.instanceById.has(id));
  if (!live.length) return null;
  try {
    const doc = await api.post('/api/fleet/assign', {
      server_ids: live, group_id: groupId || null,
    });
    adoptGroups(doc);
    saidPersisted(doc);
    toast(groupId
      ? `${live.length === 1 ? 'Hung on' : `${live.length} hung on`} `
        + `${group?.name || 'the rack'}`
      : `${live.length === 1 ? 'Back in' : `${live.length} back in`} the open yard`,
      'ok', { timeout: 2600 });
    return doc;
  } catch (e) {
    toastError(e, 'Could not move that');
    return null;
  }
}

async function saveRack({ id, name, motif: mo }) {
  try {
    const doc = await api.post('/api/fleet/groups', { id, name, motif: mo });
    adoptGroups(doc);
    saidPersisted(doc);
    return doc.group || null;
  } catch (e) {
    toastError(e, 'Could not save that rack');
    return null;
  }
}

async function deleteRack(group) {
  try {
    const doc = await api.del(`/api/fleet/groups/${encodeURIComponent(group.id)}`);
    adoptGroups(doc);
    saidPersisted(doc);
    toast(`${group.name} taken down`, 'ok', { timeout: 2600 });
  } catch (e) {
    toastError(e, `Could not take down ${group.name}`);
  }
}

export async function newRack() {
  const next = await rackDialog({});
  if (!next) return null;
  return saveRack(next);
}

/* Alt+Arrow on a focused plaque, so the whole screen is usable without ever
   opening a dialog. Order is the rack order with the open yard last, and it
   wraps, so repeated presses walk the whole shrine. */
async function shiftRack(s, delta) {
  const order = [...(state.groups.groups || []).map((g) => g.id), null];
  if (order.length < 2) {
    toast('There is only the open yard so far. Press G to make a rack.', 'warn');
    return;
  }
  const here = (state.groups.assign || {})[s.server_id] || null;
  const at = order.indexOf(here);
  const next = order[((at < 0 ? 0 : at) + delta + order.length) % order.length];
  const group = (state.groups.groups || []).find((g) => g.id === next);
  await assign([s.server_id], next, group);
}

/* --- dialogs ---------------------------------------------------- */

/* Shared with the fleet grid: one implementation of "which rack does this
   go on", reachable from a plaque, from the tray, and from a server card. */
export function openHangPicker(servers) {
  const list = (servers || []).filter(Boolean);
  if (!list.length) return Promise.resolve(null);
  const ids = list.map((s) => s.server_id);
  const groups = state.groups.groups || [];
  const assignMap = state.groups.assign || {};
  const here = list.length === 1 ? assignMap[list[0].server_id] : null;
  const countIn = (gid) => state.instances
    .filter((s) => assignMap[s.server_id] === gid).length;

  return new Promise((resolve) => {
    let settled = false;
    const answer = (v) => { if (!settled) { settled = true; resolve(v); } };
    const done = (v) => { answer(v); closeModal(); };

    const rows = groups.map((g) => {
      const m = motif(g.motif);
      const row = h('button.hangrow', {
        type: 'button',
        'aria-current': g.id === here ? 'true' : null,
        onclick: async () => { done(await assign(ids, g.id, g)); },
      },
        h('span.hg', { 'aria-hidden': 'true' }, m.glyph),
        h('span.hn', g.name),
        h('span.hc.mono', `${countIn(g.id)}`),
        g.id === here ? h('span.pill.ghost', 'here') : null,
        icon('chevronRight', 15),
      );
      row.style.setProperty('--g-ink', m.ink);
      row.style.setProperty('--g-soft', m.soft);
      return row;
    });

    modal({
      title: list.length === 1
        ? `Hang ${list[0].name || 'this server'}`
        : `Hang ${list.length} plaques`,
      body: h('div.hangpick',
        rows.length
          ? h('div.hanglist', ...rows)
          : h('p.muted', 'There are no racks yet — make the first one.'),
        h('div.btnrow', { style: { marginTop: '14px' } },
          h('button.btn.sm', {
            onclick: async () => {
              const made = await newRack();
              if (!made) { answer(null); return; }
              done(await assign(ids, made.id, made));
            },
          }, hl(), icon('plus', 14), h('span', 'New rack…')),
          (here || list.length > 1) ? h('button.btn.sm.ghost', {
            onclick: async () => { done(await assign(ids, null, null)); },
          }, icon('x', 13), h('span', 'Back to the open yard')) : null,
        ),
      ),
      onClose: () => answer(null),
    });
  });
}

function rackDialog({ title = 'Hang a new rack', name = '', chosen = 'sakura',
                      confirmLabel = 'Hang it' } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    let mo = MOTIFS[chosen] ? chosen : 'sakura';
    const answer = (v) => { if (!settled) { settled = true; resolve(v); } };

    const input = h('input.inp', {
      value: name, maxLength: 48, autocomplete: 'off',
      placeholder: 'Survival · Creative · The testing bench',
    });

    const swatches = h('div.motifgrid', ...Object.entries(MOTIFS).map(([key, m]) => {
      const b = h('button.motifpick', {
        type: 'button', title: m.label,
        'aria-pressed': String(key === mo),
        onclick: () => {
          mo = key;
          for (const c of swatches.children) {
            c.setAttribute('aria-pressed', String(c.dataset.motif === key));
          }
        },
      }, h('span.g', { 'aria-hidden': 'true' }, m.glyph), h('span.n', m.label));
      b.dataset.motif = key;
      b.style.setProperty('--g-ink', m.ink);
      b.style.setProperty('--g-soft', m.soft);
      return b;
    }));

    const go = () => {
      const v = input.value.trim();
      if (!v) { input.focus(); return; }
      answer({ name: v, motif: mo });
      closeModal();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });

    modal({
      title,
      body: h('div',
        h('div.field',
          h('label', 'What this rack is called'), input,
          h('div.help', 'A rack only groups what you already have. Nothing is '
            + 'moved on disk and nothing restarts.')),
        h('div.field', { style: { marginBottom: 0 } },
          h('label', 'Its mark'), swatches)),
      footer: frag(
        h('button.btn.ghost', { onclick: () => closeModal() },
          h('span', 'Cancel')),
        h('button.btn.primary', { onclick: go }, hl(),
          h('span', confirmLabel))),
      onClose: () => answer(null),
    });
  });
}
