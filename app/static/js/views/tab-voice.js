/* ============================================================
   Voice — Simple Voice Chat, when this server has it.

   The slip only hangs on the shelf when the mod or plugin is in
   mods/ or plugins/ (app/voicechat.py). Its port is BlessForge's
   to hand out: every server with voice chat gets a UDP port of
   its own, so two of them never fight over 24454. The port can
   be moved by hand, or "Pick a free one" finds the lowest free.

   The rest are the mod's own settings, written into its
   voicechat-server.properties with every comment and every key
   this does not own left alone. Changes apply at the next start:
   the mod reads the file once.
   ============================================================ */

import {
  h, hl, mount, icon, api, toast, toastError, loadingFox, field, toggle,
  segmented, empty, copyText, mascot, pill,
} from '../core.js';
import { may } from '../auth.js';

const ORDER = [
  ['How far it carries', ['max_voice_distance', 'whisper_distance', 'broadcast_range']],
  ['Who may do what', ['enable_groups', 'allow_recording', 'spectator_interaction',
    'spectator_player_possession']],
  ['Players without the mod', ['force_voice_chat', 'login_timeout']],
  ['Network', ['codec', 'voice_host', 'allow_pings']],
];

export async function render(ctx) {
  const node = h('div.voice');
  const host = h('div');
  mount(node, host);
  const canEdit = may('config.edit', ctx.id);
  let st = null;

  async function load(quiet) {
    if (!quiet) mount(host, loadingFox('Reading the voice chat config', 'nod'));
    try {
      st = await api.get(`/api/instances/${ctx.id}/voicechat`);
    } catch (e) {
      mount(host, h('div.note.bad', e.message));
      return;
    }
    paint();
  }

  function paint() {
    if (!st.present) {
      mount(host, empty('idle', 'Simple Voice Chat is not installed',
        'Add it from Mods (search "Simple Voice Chat") -- or Plugins on a Paper server -- '
        + 'and this section appears, with its own UDP port already set up.'));
      return;
    }
    const values = { ...st.values };
    const hostname = location.hostname || 'this machine';
    const portIn = h('input.inp.mono', { type: 'number', min: 1024, max: 65535,
      value: st.port || '', 'aria-label': 'Voice chat UDP port' });
    const savePort = async (port) => {
      try {
        st = await api.post(`/api/instances/${ctx.id}/voicechat`, { port });
        toast(`Voice chat is on UDP ${st.port}. It applies at the next start.`, 'ok');
        paint();
      } catch (e) { toastError(e, 'The port was not changed'); }
    };

    const controls = {};
    const control = (key) => {
      const f = st.fields[key];
      const set = (v) => { values[key] = v; };
      if (f.kind === 'bool') {
        return toggle(f.label, values[key], set, { disabled: !canEdit });
      }
      if (f.kind === 'choice') {
        return field(f.label, segmented(f.choices.map((c) => ({ value: c,
          label: c === 'RESTRICTED_LOWDELAY' ? 'Low delay' : titleish(c) })),
        values[key], set, f.label),
        'VOIP suits talking; AUDIO keeps music and sound better; Low delay trades quality '
          + 'for the smallest delay.');
      }
      const input = h(`input.inp${f.kind === 'text' ? '' : '.mono'}`, {
        type: f.kind === 'text' ? 'text' : 'number', value: values[key] ?? '',
        min: f.min, max: f.max, step: f.kind === 'float' ? '0.5' : '1',
        placeholder: f.kind === 'text' ? `${hostname}:${st.port}` : String(f.default),
        disabled: !canEdit,
        oninput: (e) => {
          const raw = e.target.value;
          values[key] = f.kind === 'text' ? raw : (raw === '' ? f.default : Number(raw));
        },
      });
      controls[key] = input;
      return field(f.label, input, key === 'voice_host'
        ? 'Only when players reach the server through another address than the game\'s -- a '
          + 'proxy, a tunnel, a different forwarded port. Leave blank otherwise.'
        : `Default ${f.default}.`);
    };

    const save = h('button.btn.primary', { disabled: !canEdit }, hl(), icon('save', 14),
      h('span', 'Save the settings'));
    save.onclick = async () => {
      save.classList.add('busy');
      const changed = {};
      for (const [k, v] of Object.entries(values)) {
        if (v !== st.values[k]) changed[k] = v;
      }
      try {
        st = await api.post(`/api/instances/${ctx.id}/voicechat`, { values: changed });
        toast(Object.keys(changed).length
          ? 'Saved. Voice chat reads it at the next start.' : 'Nothing had changed.', 'ok');
        paint();
      } catch (e) { toastError(e, 'Not saved'); } finally { save.classList.remove('busy'); }
    };

    mount(host,
      h('div.voice-hero',
        h('div.vh-port',
          h('span.vh-lbl', 'Voice chat port'),
          h('b.brush.vh-num', st.port ? String(st.port) : '—'),
          h('span.mono.vh-proto', 'UDP'),
          st.clash ? pill(`also ${st.clash}`, 'bad') : pill('its own', 'ok')),
        h('div.vh-words',
          h('p', h('b', `Simple Voice Chat ${st.version || ''}`.trim()),
            ` · ${st.kind === 'plugin' ? 'plugin' : 'mod'} · `, h('span.mono', st.file)),
          h('p.muted', `Players join the game on ${hostname}${st.game_port && st.game_port !== 25565
            ? `:${st.game_port}` : ''} and their voice goes to UDP ${st.port || '—'}. From outside `
            + 'your network, forward that UDP port on your router as well as the game\'s TCP port.'),
          h('div.btnrow',
            h('button.btn.sm.ghost', { onclick: () => copyText(`${st.port}/udp`, 'The voice port') },
              icon('copy', 12), h('span', 'Copy the port')),
            !st.configured ? pill('config written by BlessForge before its first start', 'info')
              : null)),
        mascot('wave', 72, { cls: 'vh-fox' })),
      h('div.voice-grid',
        h('section.panel',
          h('header', icon('ping', 15), h('h3', 'Its port')),
          h('div.pad',
            h('p.muted', 'Every server with voice chat gets a UDP port of its own from 24454 '
              + 'up, skipping game ports and anything else on this machine. Checked again '
              + 'before every start.'),
            field('UDP port', portIn),
            h('div.btnrow',
              h('button.btn.sm', { disabled: !canEdit,
                onclick: () => savePort(Number(portIn.value)) }, hl(), h('span', 'Use this port')),
              h('button.btn.sm.ghost', { disabled: !canEdit, onclick: () => savePort('auto') },
                icon('refresh', 12), h('span', 'Pick a free one'))))),
        h('section.panel.voice-settings',
          h('header', icon('sliders', 15), h('h3', 'Its settings')),
          h('div.pad',
            ...ORDER.map(([title, keys]) => h('fieldset.vs-group',
              h('legend', title), ...keys.map(control))),
            h('div.btnrow', { style: { marginTop: '8px' } }, save,
              ctx.running ? h('span.muted', 'It is running: restart it for these to apply.')
                : null)))));
  }

  await load();
  return { node };
}

function titleish(s) {
  return s.charAt(0) + s.slice(1).toLowerCase();
}
