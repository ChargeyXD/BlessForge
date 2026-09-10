/* ============================================================
   Create — a server with no modpack.

   Most servers do not start life as a downloaded pack: someone
   picks a loader, picks a version, and puts their own mods in.
   That was the one thing this app could not do.

   Paper is a first-class choice here rather than an afterthought,
   which is why the folder the wizard promises changes with the
   loader: `mods/` for Fabric, Forge and NeoForge, `plugins/` for
   Paper, Purpur and Folia. Putting a plugin in `mods/` does
   nothing at all and reports nothing, so the distinction is made
   loudly rather than left to be discovered.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, field, toggle, pill,
  loadingFox, empty, segmented, bytes, num,
} from '../core.js';
import { run } from '../jobs.js';
import { state, go, topActions, refreshInstances, bannerIfUnhealthy } from '../app.js';

export async function render() {
  const node = h('div.wrap');
  topActions(h('a.btn.sm.ghost', { href: '#/discover' },
    icon('compass', 14), h('span', 'Install a modpack instead')));

  mount(node, loadingFox('Reading the loader catalogue'));

  let catalogue;
  try {
    const data = await api.get('/api/loaders');
    catalogue = data.items || [];
  } catch (e) {
    mount(node, bannerIfUnhealthy() || null,
      empty('chargey-failed.png', 'Could not read the loader catalogue',
        e.message + ' — Crafty\'s jar index and the loaders\' own sites were '
        + 'both unreachable. Check this container\'s route to the internet.',
        h('button.btn.primary', { onclick: () => render().then((v) =>
          mount(document.querySelector('#main'), v.node)) },
          hl(), h('span', 'Try again'))));
    return { node };
  }
  if (!catalogue.length) {
    mount(node, empty('chargey-failed.png', 'No loaders are available',
      'Neither Crafty\'s jar index nor any upstream project answered.'));
    return { node };
  }

  const form = {
    loader: catalogue[0].key,
    minecraft: catalogue[0].versions[0],
    name: '',
    port: 25565,
    mem_max: 4,
    motd: '',
    difficulty: 'normal',
    gamemode: 'survival',
    max_players: 20,
    online_mode: true,
    optimize: true,
    starters: new Set(),
  };
  let starterPack = null;
  let starterLoading = false;
  let submitting = false;
  let errors = {};

  const usedPorts = new Set(state.instances.map((s) => s.port).filter(Boolean));
  // Offer a port nothing has claimed rather than making the user find out by
  // failing: two servers on one port is the classic silent broken server.
  while (usedPorts.has(form.port) && form.port < 25700) form.port += 1;

  const chosen = () => catalogue.find((c) => c.key === form.loader) || catalogue[0];

  async function loadStarters() {
    if (chosen().kind !== 'plugins') { starterPack = null; return; }
    starterLoading = true;
    paint();
    try {
      const data = await api.get(
        `/api/plugins/starter?family=${form.loader}&game_version=${form.minecraft}`);
      starterPack = data.items || [];
    } catch {
      starterPack = [];
    } finally {
      starterLoading = false;
      paint();
    }
  }

  function validate() {
    errors = {};
    if (!form.name.trim()) errors.name = 'Give the server a name.';
    else if (form.name.length > 60) errors.name = 'That name is too long.';
    if (!form.minecraft) errors.minecraft = 'Pick a Minecraft version.';
    const port = Number(form.port);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) {
      errors.port = 'A port between 1024 and 65535.';
    } else if (usedPorts.has(port)) {
      errors.port = 'Another server on this Crafty already uses that port.';
    }
    return Object.keys(errors).length === 0;
  }

  async function submit() {
    if (submitting) return;
    if (!validate()) {
      paint();
      // Focus the summary rather than a field: with several errors, the
      // summary is the thing that says how many there are.
      node.querySelector('#createerrors')?.focus();
      return;
    }
    submitting = true;
    paint();

    const seed = starterPack
      ? starterPack.filter((p) => form.starters.has(p.project_id) && p.file_id)
        .map((p) => ({ source: 'modrinth', project_id: p.project_id,
          file_id: p.file_id, name: p.name }))
      : [];

    try {
      await run('/api/provision/server', {
        name: form.name.trim(),
        loader: form.loader,
        minecraft: form.minecraft,
        port: Number(form.port),
        mem_max: Number(form.mem_max),
        motd: form.motd.trim() || null,
        difficulty: form.difficulty,
        gamemode: form.gamemode,
        max_players: Number(form.max_players),
        online_mode: form.online_mode,
        optimize: form.optimize,
        seed_mods: seed,
      }, {
        title: `Create ${form.name.trim()}`,
        onEnd: (rec) => {
          submitting = false;
          if (rec.status === 'done' && rec.result?.server_id) {
            refreshInstances({ quiet: true });
            toast(`${rec.result.name} is ready — an empty `
              + `${rec.result.mod_directory}/ folder is waiting for you.`, 'ok',
            { title: 'Server created' });
            go(`/i/${rec.result.server_id}/overview`);
          } else {
            paint();
          }
        },
      });
    } catch {
      submitting = false;
      paint();
    }
  }

  function paint() {
    const c = chosen();
    const plugins = c.kind === 'plugins';
    const folder = c.mod_directory;

    mount(node,
      bannerIfUnhealthy(),
      h('div.sec-head.mon',
        h('div',
          h('p.eyebrow', 'No modpack · ', h('b', 'just a loader and a version')),
          h('h2', 'New server')),
      ),

      Object.keys(errors).length
        ? h('div.note.bad', {
          id: 'createerrors', tabIndex: -1, role: 'alert',
          style: { marginBottom: '18px' },
        },
          h('b', `There ${Object.keys(errors).length === 1 ? 'is 1 problem'
            : `are ${Object.keys(errors).length} problems`} with this form`),
          h('ul', { style: { margin: '6px 0 0 18px', padding: 0 } },
            ...Object.entries(errors).map(([k, v]) =>
              h('li', h('a', {
                href: `#`,
                onclick: (e) => {
                  e.preventDefault();
                  node.querySelector(`[name="${k}"]`)?.focus();
                },
              }, v)))))
        : null,

      h('div.split.wide-aside',
        h('div', { style: { display: 'grid', gap: '18px' } },
          /* --- loader ------------------------------------------ */
          h('div.panel',
            h('header', icon('layers', 15), h('h3', 'Server software'),
              h('span.sp', `${catalogue.length} available`)),
            h('div.pad',
              h('div.loadergrid',
                ...catalogue.map((entry) => loaderCard(entry, form.loader,
                  () => {
                    form.loader = entry.key;
                    if (!entry.versions.includes(form.minecraft)) {
                      form.minecraft = entry.versions[0];
                    }
                    form.starters.clear();
                    loadStarters();
                    paint();
                  }))),
              h('div.note', { style: { marginTop: '14px' } },
                h('b', plugins ? 'This creates a plugins/ folder'
                  : 'This creates a mods/ folder'),
                plugins
                  ? 'Paper-family servers load Bukkit plugins, not mods. A mod '
                    + 'jar dropped into plugins/ is ignored silently — the '
                    + 'Plugins tab only ever offers you things that fit.'
                  : `${c.title} loads mods from mods/. The Mods tab searches `
                    + 'CurseForge and Modrinth filtered to this loader.'),
            )),

          /* --- identity ---------------------------------------- */
          h('div.panel',
            h('header', icon('server', 15), h('h3', 'Identity')),
            h('div.pad',
              field('Server name *',
                h('input.inp', {
                  name: 'name', value: form.name, maxLength: 60,
                  placeholder: 'Sakura SMP', autocomplete: 'off',
                  'aria-describedby': errors.name ? 'err-name' : null,
                  oninput: (e) => { form.name = e.target.value; },
                  onblur: () => { if (form.name.trim()) { delete errors.name; paint(); } },
                }),
                'What it is called in Crafty and here.', errors.name),

              h('div.grid.g2',
                field('Minecraft version *',
                  h('select.inp', {
                    name: 'minecraft',
                    onchange: (e) => {
                      form.minecraft = e.target.value;
                      form.starters.clear();
                      loadStarters();
                      paint();
                    },
                  }, ...c.versions.map((v) => h('option', {
                    value: v, selected: v === form.minecraft,
                  }, v,
                    c.crafty_versions.includes(v) ? '' : '  (fetched upstream)'))),
                  c.crafty_versions.includes(form.minecraft)
                    ? 'Crafty has this build cached.'
                    : 'Crafty\'s index does not have this one — it will be '
                      + 'fetched from the project itself.',
                  errors.minecraft),

                field('Port *',
                  h('input.inp', {
                    name: 'port', type: 'number', value: form.port,
                    min: 1024, max: 65535, inputMode: 'numeric',
                    oninput: (e) => { form.port = e.target.value; },
                    onblur: () => { validate(); paint(); },
                  }),
                  usedPorts.size
                    ? `In use: ${[...usedPorts].sort((a, b) => a - b).join(', ')}`
                    : 'Nothing else is using a port yet.',
                  errors.port),
              ),

              field('MOTD',
                h('input.inp', {
                  name: 'motd', value: form.motd, maxLength: 120,
                  placeholder: `A ${c.title} server`,
                  oninput: (e) => { form.motd = e.target.value; },
                }),
                'The line players see in their server list.'),
            )),

          /* --- world ------------------------------------------- */
          h('div.panel',
            h('header', icon('compass', 15), h('h3', 'World and players')),
            h('div.pad',
              h('div.grid.g3',
                field('Difficulty', h('select.inp', {
                  onchange: (e) => { form.difficulty = e.target.value; },
                }, ...['peaceful', 'easy', 'normal', 'hard'].map((d) =>
                  h('option', { value: d, selected: d === form.difficulty }, d)))),
                field('Game mode', h('select.inp', {
                  onchange: (e) => { form.gamemode = e.target.value; },
                }, ...['survival', 'creative', 'adventure', 'spectator'].map((d) =>
                  h('option', { value: d, selected: d === form.gamemode }, d)))),
                field('Max players', h('input.inp', {
                  type: 'number', min: 1, max: 500, value: form.max_players,
                  inputMode: 'numeric',
                  oninput: (e) => { form.max_players = e.target.value; },
                })),
              ),
              h('div', { style: { display: 'grid', gap: '10px' } },
                toggle('Online mode — verify accounts with Mojang',
                  form.online_mode, (v) => { form.online_mode = v; },
                  { help: 'Turn this off only for a LAN or cracked server. '
                    + 'With it off, usernames are not verified and anybody can '
                    + 'claim any name.' }),
                toggle('Tune the JVM for this host', form.optimize,
                  (v) => { form.optimize = v; },
                  { help: 'Sizes the heap against the memory this machine '
                    + 'actually has free, and writes the flags for it.' }),
              ),
            )),

          /* --- starter plugins --------------------------------- */
          plugins ? h('div.panel',
            h('header', icon('puzzle', 15), h('h3', 'Starter plugins'),
              h('span.sp', 'optional')),
            h('div.pad',
              starterLoading
                ? loadingFox('Asking Modrinth')
                : !starterPack || !starterPack.length
                  ? h('p.muted', 'Modrinth could not be reached, so there is '
                    + 'nothing to offer here. The Plugins tab will work once '
                    + 'the server exists.')
                  : h('div', { style: { display: 'grid', gap: '10px' } },
                    h('p.muted', { style: { fontSize: '13.5px' } },
                      'The handful every new Paper server ends up with. Each '
                      + 'is installed with its own dependencies.'),
                    ...starterPack.map((p) => starterRow(p, form, paint)))),
          ) : null,
        ),

        /* --- summary ------------------------------------------ */
        h('div.sticky',
          h('div.panel',
            h('header', icon('flask', 15), h('h3', 'What you get')),
            h('div.pad', { style: { display: 'grid', gap: '12px' } },
              h('div', { style: { position: 'relative', textAlign: 'center' } },
                h('div.fox-halo'),
                h('img.fox', {
                  src: '/assets/fox-mascot.png', alt: '',
                  style: { width: '130px', margin: '0 auto' },
                })),
              summaryRow('Software', `${c.title} ${form.minecraft}`),
              summaryRow('Mod folder', `${folder}/`, 'empty, ready for you'),
              summaryRow('Port', String(form.port)),
              summaryRow('Heap', `${form.mem_max} GB`),
              summaryRow('EULA', 'accepted at creation'),
              plugins && form.starters.size
                ? summaryRow('Plugins', `${form.starters.size} selected`)
                : null,
              h('div.field', { style: { margin: 0 } },
                h('label', `Heap size — ${form.mem_max} GB`),
                h('input', {
                  type: 'range', min: 1, max: 16, step: 1, value: form.mem_max,
                  'aria-label': 'Heap size in gigabytes',
                  oninput: (e) => { form.mem_max = Number(e.target.value); paint(); },
                }),
                h('div.help', 'Sized against this host when tuning is on; this '
                  + 'is the ceiling you are asking for.')),
              h('button.btn.primary.block', {
                class: submitting ? 'btn primary block busy' : 'btn primary block',
                disabled: submitting || !state.health?.ready,
                onclick: submit,
              }, hl(), icon('plus', 15),
                h('span', submitting ? 'Creating…' : 'Create the server')),
              !state.health?.ready
                ? h('div.note.bad', 'Not connected to Crafty.')
                : null,
            )),
          h('div.torii', { style: { marginTop: '14px' } }, toriiSvg()),
        ),
      ),
    );
  }

  paint();
  loadStarters();
  return { node };
}

function summaryRow(label, value, sub) {
  return h('div', {
    style: { display: 'flex', justifyContent: 'space-between', gap: '12px',
      alignItems: 'baseline', fontSize: '13.5px' },
  },
    h('span.muted', label),
    h('span', { style: { textAlign: 'right' } },
      h('b', { class: 'h-num' }, value),
      sub && h('div.mono.muted', { style: { fontSize: '10px' } }, sub)));
}

function loaderCard(entry, selected, onpick) {
  const on = entry.key === selected;
  // One fixed art box for every loader. The source marks are a 4 KB PNG, a
  // JPEG on a white ground, a 140 KB PNG and a 152 KB SVG at four different
  // aspect ratios — rendered at their natural sizes they read as four
  // different visual weights and the row looks like a ransom note.
  return h('button.loadercard', {
    type: 'button',
    'aria-pressed': String(on),
    onclick: onpick,
  },
    h('div.top',
      h('div.loader-art', entry.logo
        ? h('img', { src: entry.logo, alt: '', loading: 'lazy', decoding: 'async' })
        : icon(entry.kind === 'plugins' ? 'puzzle' : 'box', 20)),
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('div.nm', entry.title),
        h('div.meta', `${entry.mod_directory}/ · ${entry.versions.length} versions`)),
      h('span.tick', icon('check', 17))),
    h('p', entry.blurb),
  );
}

function starterRow(p, form, paint) {
  const on = form.starters.has(p.project_id);
  return h('label.addon', {
    style: {
      display: 'flex', alignItems: 'center', gap: '12px', cursor: 'pointer',
      border: '2px solid var(--rule)', padding: '10px 12px',
      background: on ? 'var(--rose-soft)' : 'var(--paper-2)',
      opacity: p.available ? 1 : 0.5,
    },
  },
    h('input', {
      type: 'checkbox', checked: on, disabled: !p.available,
      onchange: (e) => {
        if (e.target.checked) form.starters.add(p.project_id);
        else form.starters.delete(p.project_id);
        paint();
      },
    }),
    p.logo ? h('img', {
      src: p.logo, alt: '', loading: 'lazy',
      style: { width: '30px', height: '30px', border: '2px solid var(--rule)' },
    }) : icon('puzzle', 20),
    h('div', { style: { flex: 1, minWidth: 0 } },
      h('div.h-num', { style: { fontSize: '14px' } }, p.name),
      h('div.mono.muted', { style: { fontSize: '10.5px', lineHeight: 1.45 } },
        p.available ? p.why : `No build for this version.`)),
    p.available && p.version
      ? h('span.pill.ghost', p.version) : h('span.pill.dead', 'n/a'),
  );
}

export function toriiSvg() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 120 100');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = '<g fill="currentColor">'
    + '<rect x="2" y="6" width="116" height="8" rx="1"/>'
    + '<rect x="12" y="20" width="96" height="6"/>'
    + '<rect x="26" y="26" width="10" height="74"/>'
    + '<rect x="84" y="26" width="10" height="74"/>'
    + '<rect x="20" y="0" width="80" height="5" rx="2"/></g>';
  return svg;
}
