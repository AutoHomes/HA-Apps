'use strict';

// All URLs are relative: the page is served under Home Assistant's Ingress
// prefix (/api/hassio_ingress/<token>/), never at the site root.

const POLL_MS = 3000;
const LOGIN_POLL_MS = 3000;
const QR_LIFETIME_MS = 3 * 60 * 1000; // Smart Life QR codes are valid for about 3 minutes

const S = {
  status: null,      // last /api/status
  lastJson: '',
  loadError: null,   // couldn't load at all
  pollError: null,   // loaded before, but the last refresh failed
  userCode: '',
  loginSeenAt: null, // browser time the current QR code was first shown
  loginExpired: false,
  showOther: false,
  feedAll: false,    // Live activity: every device instead of just the locks
  busy: false,
};

const BELL = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 22a2.5 2.5 0 0 0 2.45-2h-4.9A2.5 2.5 0 0 0 12 22zm7-6v-5a7 7 0 0 0-5.5-6.84V3.5a1.5 1.5 0 0 0-3 0v.66A7 7 0 0 0 5 11v5l-2 2v1h18v-1l-2-2z"/></svg>';

// ------------------------------------------------------------- helpers

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === '') continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

function button(label, onclick, cls = '', type = 'button') {
  return h('button', { type, class: `btn ${cls}`, disabled: S.busy, onclick }, label);
}

const code = (text) => h('code', {}, text);

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const opts = { hour: '2-digit', minute: '2-digit', second: '2-digit' };
  if (d.toDateString() !== new Date().toDateString()) Object.assign(opts, { weekday: 'short', day: 'numeric', month: 'short' });
  return d.toLocaleString([], opts);
}

const fmtValue = (value) => JSON.stringify(value);

async function api(path, { method, body } = {}) {
  const init = { method: method || (body === undefined ? 'GET' : 'POST'), headers: {}, cache: 'no-store' };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (HTTP ${res.status})`);
  return data;
}

function toast(message, kind = 'ok') {
  const el = h('div', { class: `toast ${kind}`, role: 'status' }, message);
  document.body.append(el);
  setTimeout(() => el.classList.add('hide'), kind === 'err' ? 6000 : 3000);
  setTimeout(() => el.remove(), kind === 'err' ? 6400 : 3400);
}

// window.confirm() is unreliable inside the Home Assistant phone apps.
function confirmBox({ title, text, ok = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const close = (value) => { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(value); };
    const onKey = (e) => { if (e.key === 'Escape') close(false); };
    const okBtn = h('button', { type: 'button', class: `btn ${danger ? 'danger' : 'primary'}`, onclick: () => close(true) }, ok);
    const overlay = h('div', { class: 'overlay', onclick: (e) => { if (e.target === overlay) close(false); } },
      h('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
        h('h3', {}, title),
        text && h('p', {}, text),
        h('div', { class: 'actions' },
          h('button', { type: 'button', class: 'btn ghost', onclick: () => close(false) }, 'Cancel'),
          okBtn)));
    document.addEventListener('keydown', onKey);
    document.body.append(overlay);
    okBtn.focus();
  });
}

async function act(fn, okMessage) {
  if (S.busy) return false;
  S.busy = true;
  render();
  try {
    const result = await fn();
    if (okMessage) toast(okMessage);
    return result === undefined ? true : result;
  } catch (err) {
    toast(err.message, 'err');
    return false;
  } finally {
    S.busy = false;
    await refresh(true);
  }
}

// --------------------------------------------------------------- data

async function refresh(forceRender = false) {
  try {
    const data = await api('api/status');
    const json = JSON.stringify(data);
    const changed = json !== S.lastJson || S.pollError || S.loadError;
    S.status = data;
    S.lastJson = json;
    S.pollError = null;
    S.loadError = null;
    if (!data.tuya.login_pending) { S.loginSeenAt = null; S.loginExpired = false; } else if (!S.loginSeenAt) S.loginSeenAt = Date.now();
    if (changed || forceRender) render();
  } catch (err) {
    if (S.status) S.pollError = err.message; else S.loadError = err.message;
    render();
  }
}

async function pollLoop() {
  if (document.visibilityState === 'visible' && !S.busy) await refresh();
  setTimeout(pollLoop, POLL_MS);
}

async function loginLoop() {
  const tuya = S.status && S.status.tuya;
  if (tuya && tuya.login_pending && !S.loginExpired && document.visibilityState === 'visible') {
    if (S.loginSeenAt && Date.now() - S.loginSeenAt > QR_LIFETIME_MS) {
      S.loginExpired = true;
      render();
    } else {
      try {
        const result = await api('api/login/check', { body: {} });
        if (result.state === 'done') toast('Signed in to Smart Life');
        if (result.state !== 'waiting') await refresh(true);
      } catch (_) { /* the regular poll shows errors */ }
    }
  }
  setTimeout(loginLoop, LOGIN_POLL_MS);
}

// -------------------------------------------------------------- views

function header() {
  let dot = '', label = 'Loading…';
  const tuya = S.status && S.status.tuya;
  if (tuya) {
    if (tuya.state === 'signed_out') [dot, label] = ['', 'Not signed in'];
    else if (tuya.state === 'connecting') [dot, label] = ['warn', 'Connecting…'];
    else if (tuya.state === 'error') [dot, label] = ['err', 'Can\'t reach Tuya'];
    else if (tuya.live) [dot, label] = ['ok', 'Listening'];
    else [dot, label] = ['warn', 'Connecting…'];
  }
  const brand = h('div', { class: 'brand' }, 'Lock Doorbell');
  brand.insertAdjacentHTML('afterbegin', BELL);
  return h('div', { class: 'top' }, brand, h('div', { class: 'state' }, h('span', { class: `dot ${dot}` }), label));
}

function signInView() {
  const submit = (e) => {
    e.preventDefault();
    act(async () => {
      await api('api/login', { body: { user_code: S.userCode.trim() } });
      S.loginSeenAt = Date.now();
      S.loginExpired = false;
    });
  };
  return h('form', { class: 'card', onsubmit: submit },
    h('h3', {}, 'Sign in to Smart Life'),
    h('p', { class: 'sub' }, 'The app listens to your lock through the Smart Life (or Tuya Smart) account it\'s paired with, the same way Home Assistant\'s Tuya integration does. No Tuya developer account needed.'),
    h('ol', { class: 'steps' },
      h('li', {}, 'In the Smart Life app, open ', h('b', {}, 'Me → ⚙ Settings → Account and Security → User Code'), '.'),
      h('li', {}, 'Enter that code below and press ', h('b', {}, 'Show QR code'), '.'),
      h('li', {}, 'Scan the QR code with the Smart Life app and confirm.')),
    h('label', { class: 'field' },
      h('span', {}, 'User code'),
      h('div', { class: 'input-row' },
        h('input', {
          type: 'text', value: S.userCode, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false',
          placeholder: 'e.g. A1b2C3d', required: true, oninput: (e) => { S.userCode = e.target.value; },
        }),
        button('Show QR code', null, 'primary', 'submit'))));
}

function qrView(tuya) {
  const again = () => act(async () => {
    await api('api/login', { body: { user_code: S.userCode.trim() } });
    S.loginSeenAt = Date.now();
    S.loginExpired = false;
  });
  const cancel = () => act(() => api('api/login/cancel', { body: {} }));
  return h('div', { class: 'card' },
    h('h3', {}, 'Scan with the Smart Life app'),
    h('p', { class: 'sub' }, 'In the Smart Life app, tap the scan icon (top right of the Home tab), scan this code and confirm the sign-in.'),
    h('div', { class: 'qr-wrap' },
      h('img', { class: 'qr', src: `api/login/qr.svg?v=${encodeURIComponent(tuya.login_started)}`, alt: 'Smart Life sign-in QR code' }),
      S.loginExpired
        ? h('div', { class: 'notice warn' }, 'This QR code has probably expired.')
        : h('div', { class: 'waiting' }, h('span', { class: 'spinner' }), 'Waiting for you to confirm in the app…')),
    h('div', { class: 'actions' },
      S.loginExpired && S.userCode.trim() && button('New QR code', again, 'primary'),
      button(S.loginExpired ? 'Start again' : 'Cancel', cancel, 'ghost')));
}

function connectionNotice(tuya) {
  if (tuya.state === 'connecting') {
    return h('div', { class: 'notice info' }, 'Loading your Smart Life devices… This can take up to a minute if you have a lot of them.');
  }
  if (tuya.state === 'error') {
    const secs = tuya.retry_at ? Math.max(0, Math.round(tuya.retry_at - Date.now() / 1000)) : null;
    return h('div', { class: 'notice err' }, tuya.error || 'Couldn\'t connect to Tuya.',
      secs != null ? ` Trying again in about ${secs} s.` : '',
      ' If this doesn\'t clear up, sign out and sign in again.');
  }
  if (!tuya.live) return h('div', { class: 'notice info' }, 'Connecting to Tuya\'s live feed…');
  return null;
}

function scriptCard(settings) {
  const script = settings.script;
  let badge;
  let notice = null;
  if (!script.entity_id) {
    badge = h('span', { class: 'badge warn' }, 'Not set');
    notice = h('div', { class: 'notice warn' }, 'No script is set, so a press only fires the ', code('tuya_doorbell_pressed'),
      ' event. Set ', h('b', {}, 'Script'), ' on the app\'s Configuration tab, then restart the app.');
  } else if (script.exists === false) {
    badge = h('span', { class: 'badge err' }, 'Not found');
    notice = h('div', { class: 'notice err' }, 'Home Assistant has no ', code(script.entity_id),
      '. Create it under Settings → Automations & scenes → Scripts, or fix the name on the Configuration tab.');
  } else if (script.exists === null) {
    badge = h('span', { class: 'badge neutral' }, 'Unknown');
  } else {
    badge = h('span', { class: 'badge ok' }, 'Found');
  }
  const test = () => act(async () => {
    const result = await api('api/test', { body: {} });
    if (!result.ok) throw new Error(result.result);
    toast(result.result);
  });
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, 'When the doorbell is pressed'),
        h('div', { class: 'sub' }, 'Set on the app\'s Configuration tab.'))),
    h('dl', { class: 'kv' },
      h('dt', {}, 'Run script'),
      h('dd', {}, script.entity_id ? [code(script.entity_id), ' ', script.name ? `(${script.name}) ` : '', badge] : badge),
      h('dt', {}, 'Doorbell codes'),
      h('dd', {}, settings.doorbell_codes.map((c, i) => [i ? ' ' : '', code(c)])),
      h('dt', {}, 'Cooldown'),
      h('dd', {}, settings.cooldown_seconds ? `${settings.cooldown_seconds} s between rings per lock` : 'Off'),
      h('dt', {}, 'Also fires'),
      h('dd', {}, code('tuya_doorbell_pressed'), ' event')),
    notice && h('div', { style: 'margin-top:12px' }, notice),
    h('div', { class: 'actions' }, button('Ring now (test)', test)));
}

function lastMessage(device) {
  if (!device.messages) return 'No messages from Tuya for it yet';
  return `${device.messages} message${device.messages === 1 ? '' : 's'} from Tuya, last at ${fmtTime(device.last_message_at)}`;
}

function deviceRow(device, settings) {
  const hits = new Set(device.doorbell_codes);
  const dps = Object.entries(device.doorbell_dps || {});
  return h('li', {},
    h('div', { class: 'row-head' },
      h('span', { class: 'row-title' }, device.name),
      h('span', { class: `badge ${device.online ? 'ok' : 'neutral'}` }, device.online ? 'Online' : 'Offline / asleep')),
    h('div', { class: 'sub' }, [device.product, device.category && `category ${device.category}`].filter(Boolean).join(' · ')),
    h('div', { class: 'sub mono' }, device.id),
    device.watched && h('div', { class: 'sub' }, lastMessage(device),
      dps.length ? [' · doorbell is ', dps.map(([c, dp], i) => [i ? ', ' : '', code(c), ` (DP ${dp})`])] : ''),
    device.watched && !hits.size && h('p', { class: 'hint' },
      'Tuya doesn\'t list ', settings.doorbell_codes.map((c, i) => [i ? ' or ' : '', code(c.split('=')[0])]),
      ' for this lock. Press its doorbell button and watch ', h('b', {}, 'Live activity'), ' below to see what it reports.'),
    !device.watched && device.reason && h('p', { class: 'hint' }, 'New since the app started. Press ', h('b', {}, 'Refresh devices'), ' to listen to it.'),
    device.codes.length > 0 && h('div', { class: 'codes', title: 'Data points Tuya lists for this device' },
      device.codes.map((c) => h('code', { class: hits.has(c) ? 'hit' : '' }, c))));
}

function locksCard(devices, settings) {
  const watched = devices.filter((d) => d.watched || d.reason);
  const refreshDevices = () => act(() => api('api/devices/refresh', { body: {} }), 'Reloading your Smart Life devices…');
  let body;
  if (watched.length) {
    body = h('ul', { class: 'rows' }, watched.map((d) => deviceRow(d, settings)));
  } else if (S.status.tuya.state !== 'connected') {
    body = null;
  } else if (settings.locks.length) {
    body = h('div', { class: 'empty' }, 'None of your Smart Life devices match ',
      settings.locks.map((l, i) => [i ? ', ' : '', code(l)]),
      ' from ', h('b', {}, 'Locks'), ' on the Configuration tab. Use a device\'s exact name or ID from the list below.');
  } else {
    body = h('div', { class: 'empty' }, `None of your ${devices.length} Smart Life device(s) look like a lock. `,
      'Find yours under "Other Smart Life devices" below and add its name or ID under ', h('b', {}, 'Locks'),
      ' on the Configuration tab, then restart the app.');
  }
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, 'Locks'),
        h('div', { class: 'sub' }, settings.locks.length ? 'The devices listed under Locks on the Configuration tab.' : 'Found automatically on your Smart Life account.'))),
    body,
    h('div', { class: 'actions' }, button('Refresh devices', refreshDevices)));
}

function feedStatus(tuya) {
  const feed = tuya.feed;
  if (!tuya.live || !feed) return null;
  if (feed.refused) {
    return h('div', { class: 'notice warn' }, `Tuya refused ${feed.refused} of the app's subscriptions, so some devices can't be heard. The Log tab lists them.`);
  }
  if (!feed.messages) {
    return h('div', { class: 'notice info' }, 'No messages from Tuya yet, from any device. Switch something on or off in the Smart Life app. ',
      'If this still says nothing, Tuya isn\'t sending the app anything.');
  }
  return h('p', { class: 'hint' },
    `Tuya has sent ${feed.messages} message${feed.messages === 1 ? '' : 's'} since the app connected `,
    `(${feed.lock_messages} from your locks), the last at ${fmtTime(feed.last_message_at)}.`);
}

function feedCard(status) {
  const feed = S.feedAll ? status.all_feed : status.feed;
  const items = feed.map((entry) => {
    let result = null;
    if (entry.result) {
      const cls = entry.ok === false ? 'err' : entry.ok ? 'ok' : 'muted';
      result = h('div', { class: `result ${cls}` }, entry.result);
    }
    return h('li', { class: entry.doorbell ? 'doorbell' : entry.watched === false ? 'other' : '' },
      h('span', { class: 'icon' }, entry.doorbell ? '🔔' : '•'),
      h('span', { class: 'what' }, `${entry.lock_name}: `, code(entry.code), ' = ', h('span', { class: 'mono' }, fmtValue(entry.value)),
        entry.dp_id != null ? h('span', { class: 'sub' }, ` (DP ${entry.dp_id})`) : ''),
      h('span', { class: 'time' }, fmtTime(entry.ts)),
      result);
  });
  const example = feed.find((e) => !e.doorbell);
  const tab = (label, all) => h('button', {
    type: 'button', class: `seg-btn${S.feedAll === all ? ' on' : ''}`, 'aria-pressed': S.feedAll === all ? 'true' : 'false',
    onclick: () => { S.feedAll = all; render(); },
  }, label);
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h3', {}, 'Live activity'),
      h('div', { class: 'seg', role: 'group', 'aria-label': 'Show reports from' }, tab('Locks', false), tab('All devices', true))),
    h('div', { class: 'sub' }, S.feedAll
      ? 'Every report from every device on your Smart Life account. Use it to see which device reports something at the moment you press the doorbell.'
      : 'Everything your locks report, as it happens. Press the doorbell button to try it. A battery lock can take a few seconds to wake up and report.'),
    feedStatus(status.tuya),
    items.length ? h('ul', { class: 'rows feed' }, items) : h('div', { class: 'empty' }, 'Nothing reported since the app started.'),
    S.feedAll
      ? h('p', { class: 'hint' }, 'Was it a device other than your lock (e.g. the camera of a video lock)? Add that device\'s name under ',
        h('b', {}, 'Locks'), ' and its code under ', h('b', {}, 'Doorbell codes'), ' on the Configuration tab, then restart the app.')
      : h('p', { class: 'hint' }, 'Pressed the button but nothing got a 🔔? Find the report that appeared at that moment and add its code',
        example ? [' (e.g. ', code(example.code), ')'] : '',
        ' under ', h('b', {}, 'Doorbell codes'), ' on the Configuration tab, then restart the app. Use ', code('code=value'),
        ' to match just one value. Nothing at all? Switch to ', h('b', {}, 'All devices'), ' and press it again.'));
}

function ringsCard(rings) {
  if (!rings.length) return null;
  return h('div', { class: 'card' },
    h('h3', {}, 'Recent rings'),
    h('ul', { class: 'rows feed' }, rings.map((ring) => h('li', {},
      h('span', { class: 'icon' }, ring.test ? '🧪' : '🔔'),
      h('span', { class: 'what' }, ring.test ? 'Test' : ring.lock_name),
      h('span', { class: 'time' }, fmtTime(ring.ts)),
      h('div', { class: `result ${ring.ok ? 'ok' : 'err'}` }, ring.result)))));
}

function otherDevicesCard(devices) {
  const others = devices.filter((d) => !d.watched && !d.reason);
  if (!others.length) return null;
  return h('div', { class: 'card' },
    h('details', { class: 'more', open: S.showOther, ontoggle: (e) => { S.showOther = e.target.open; } },
      h('summary', {}, `Other Smart Life devices (${others.length})`),
      h('p', { class: 'hint' }, 'Not listened to. If your lock is here, add its name or ID under ', h('b', {}, 'Locks'), ' on the Configuration tab.'),
      h('ul', { class: 'rows' }, others.map((d) => h('li', {},
        h('div', { class: 'row-head' }, h('span', { class: 'row-title' }, d.name), h('span', { class: 'sub' }, d.category)),
        h('div', { class: 'sub mono' }, d.id),
        h('div', { class: 'sub' }, lastMessage(d)))))));
}

function accountCard(tuya) {
  const signOut = async () => {
    const ok = await confirmBox({
      title: 'Sign out of Smart Life?',
      text: 'The app stops listening to your lock until you sign in again. Your lock and the Smart Life app aren\'t affected.',
      ok: 'Sign out', danger: true,
    });
    if (ok) act(() => api('api/logout', { body: {} }), 'Signed out');
  };
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, 'Smart Life account'),
        h('div', { class: 'sub' }, tuya.account ? `Signed in as ${tuya.account}` : 'Signed in')),
      button('Sign out', signOut, 'ghost')));
}

function mainView() {
  const { tuya, settings, devices, rings } = S.status;
  if (tuya.state === 'signed_out') return tuya.login_pending ? qrView(tuya) : signInView();
  return [
    connectionNotice(tuya),
    scriptCard(settings),
    locksCard(devices, settings),
    feedCard(S.status),
    ringsCard(rings),
    otherDevicesCard(devices),
    accountCard(tuya),
  ];
}

function render() {
  const root = document.getElementById('app');
  let view;
  if (S.status) view = mainView();
  else if (S.loadError) view = h('div', { class: 'notice err' }, S.loadError);
  else view = h('div', { class: 'boot' }, 'Loading…');
  const focused = document.activeElement && document.activeElement.tagName === 'INPUT';
  root.replaceChildren(...[
    header(),
    S.pollError ? h('div', { class: 'notice err' }, `Lost contact with the app: ${S.pollError}`) : null,
    view,
  ].flat().filter(Boolean));
  if (focused) { const input = root.querySelector('input'); if (input) input.focus(); }
}

document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refresh(); });
refresh().then(() => { setTimeout(pollLoop, POLL_MS); loginLoop(); });
