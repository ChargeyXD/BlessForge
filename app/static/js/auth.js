/* ============================================================
   auth — who is signed in, and what the interface may offer.

   The gate is the SERVER. Everything here is about not showing
   somebody a button that will only tell them no: the middleware
   in main.py is what actually refuses, and this file could be
   deleted without opening a single hole.

   That order matters. A front end that hides a control is a
   courtesy; a front end that is the only thing stopping an
   action is a decoration over an open door.
   ============================================================ */

import { h, hl, icon, api, mount, clear, toast, toastError } from './core.js';

export const session = {
  authenticated: false,
  user: null,
  defaultPasswordInUse: false,
  roles: {},
  sessionDays: 30,
};

const listeners = new Set();
export function onSession(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function emit() { listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } }); }

export async function refreshSession() {
  try {
    const data = await api.get('/api/auth/session');
    session.authenticated = !!data.authenticated;
    session.user = data.user || null;
    session.defaultPasswordInUse = !!data.default_password_in_use;
    session.roles = data.roles || {};
    session.sessionDays = data.session_days || 30;
  } catch {
    // An unreachable server is not the same as a rejected session, and
    // guessing "signed out" here would throw somebody back to the login
    // screen every time the container restarts under them.
    session.authenticated = false;
    session.user = null;
  }
  emit();
  return session;
}

/* What the signed-in user may do. Both of these answer FALSE when nobody
   is signed in, so a screen that renders before the session lands shows
   the smaller version rather than flashing controls and taking them
   away. */
export const may = (permission) =>
  !!session.user && (session.user.permissions || []).includes(permission);

export const isAdmin = () => !!session.user && session.user.is_admin;

export function inScope(serverId) {
  const scope = session.user?.scope;
  if (!session.user) return false;
  if (session.user.is_admin || scope?.all) return true;
  return (scope?.servers || []).includes(serverId);
}

export async function logout() {
  try { await api.post('/api/auth/logout'); } catch { /* going anyway */ }
  session.authenticated = false;
  session.user = null;
  emit();
  location.reload();
}

/* --- the shrine gate -----------------------------------------
   The login screen is a torii with a shimenawa across it: the
   rope is drawn shut, and it opens when you are let through.
   It is the first thing anybody sees of this app, so it carries
   the theme rather than being a centred box on a grey field.  */

export function loginScreen({ onSignedIn, mustChangeFor = null } = {}) {
  const changing = !!mustChangeFor;
  const node = h('div#gate', { 'data-mode': changing ? 'change' : 'login' });

  const err = h('div.gate-err', { role: 'alert', hidden: true });
  const user = h('input.inp', {
    type: 'text', name: 'username', autocomplete: 'username',
    'data-keep': 'gate-user', placeholder: 'Username',
    'aria-label': 'Username', autocapitalize: 'none', spellcheck: 'false',
  });
  const pass = h('input.inp', {
    type: 'password', name: 'password',
    autocomplete: changing ? 'current-password' : 'current-password',
    'data-keep': 'gate-pass', placeholder: 'Password', 'aria-label': 'Password',
  });
  const next = h('input.inp', {
    type: 'password', name: 'new-password', autocomplete: 'new-password',
    'data-keep': 'gate-next', placeholder: 'New password',
    'aria-label': 'New password',
  });
  const again = h('input.inp', {
    type: 'password', name: 'confirm-password', autocomplete: 'new-password',
    'data-keep': 'gate-again', placeholder: 'New password again',
    'aria-label': 'Confirm the new password',
  });

  const submit = h('button.btn.primary.block', { type: 'submit' },
    hl(), h('span', changing ? 'Set it and continue' : 'Enter'));

  const fail = (message) => {
    err.textContent = message;
    err.hidden = false;
    node.classList.remove('shaking');
    void node.offsetWidth;                 // restart, not resume
    node.classList.add('shaking');
  };

  async function signIn(e) {
    e.preventDefault();
    err.hidden = true;
    submit.classList.add('busy');
    try {
      const body = { username: user.value.trim(), password: pass.value };
      const data = await api.post('/api/auth/login', body);
      node.classList.add('opening');
      // Let the rope finish parting before the app replaces the screen.
      // 520ms is the length of the animation; if the timeline is stalled
      // the timer still fires, so this cannot strand anybody.
      setTimeout(() => onSignedIn?.(data.user), 520);
    } catch (ex) {
      submit.classList.remove('busy');
      fail(ex.message || 'That did not work.');
      pass.value = '';
      pass.focus();
    }
  }

  async function change(e) {
    e.preventDefault();
    err.hidden = true;
    if (next.value !== again.value) {
      fail('The two new passwords do not match.');
      again.focus();
      return;
    }
    submit.classList.add('busy');
    try {
      await api.post('/api/auth/password', {
        current: pass.value, password: next.value,
      });
      node.classList.add('opening');
      setTimeout(() => onSignedIn?.(null), 520);
    } catch (ex) {
      submit.classList.remove('busy');
      fail(ex.message || 'That did not work.');
    }
  }

  const form = h('form.gate-form', { onsubmit: changing ? change : signIn },
    changing ? null : user,
    pass,
    changing ? next : null,
    changing ? again : null,
    err,
    submit);

  if (changing) {
    pass.placeholder = 'Current password';
    pass.setAttribute('aria-label', 'Current password');
  }

  mount(node,
    h('div.gate-sky', { 'aria-hidden': 'true' }),
    h('div.gate-card',
      h('div.gate-torii', { 'aria-hidden': 'true' },
        h('span.g-kasagi'), h('span.g-nuki'),
        h('span.g-post.l'), h('span.g-post.r'),
        h('span.g-rope'),
        h('span.g-shide.a'), h('span.g-shide.b'), h('span.g-shide.c')),
      h('div.gate-mark', { 'aria-hidden': 'true' },
        h('img', { src: '/assets/appmark-fox.png', alt: '', width: 54, height: 54 })),
      h('h1.gate-name', 'BlessForge'),
      h('p.gate-sub',
        changing
          ? `Set a password for ${mustChangeFor}. This one cannot be skipped.`
          : 'Shrine of servers'),
      form,
      changing
        ? h('p.gate-foot',
          'At least 10 characters. It cannot be the default.')
        : h('p.gate-foot',
          'No account? There is no sign-up — an admin makes accounts. ',
          h('br'),
          'Forgotten it? An admin can set you a new one.')),
  );

  // Focus the first thing you would type in, once the screen exists.
  setTimeout(() => (changing ? pass : user).focus(), 60);
  return node;
}
