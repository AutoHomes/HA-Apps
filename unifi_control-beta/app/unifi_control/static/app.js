'use strict';

// All URLs are relative: the page is served under Home Assistant's Ingress
// prefix (/api/hassio_ingress/<token>/), never at the site root.

const S = {
  me: null,
  meError: null,
  status: null,
  tab: null,
  ports: null, portsError: null,
  wifi: null, wifiError: null,
  admin: null, adminError: null,
  history: null, historyError: null,
  publicIp: null,
  form: null,     // the one open inline form: {kind: 'grant'|'open'|'wifi', id, ...}
  reveal: {},     // wlan id -> {password, t}
  busy: false,
};

const ACTION_ICONS = {
  grant: '🔓', revoke: '🔒', expire: '⏱️', open: '⚠️', close: '🔒',
  wifi_password: '🔑', manage: '⚙️', unmanage: '⚙️', error: '❗',
};
const PW_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';

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

function chip(label, on, onclick) {
  return h('button', { type: 'button', class: `chip${on ? ' on' : ''}`, 'aria-pressed': on ? 'true' : 'false', onclick }, label);
}

function toggle(label, checked, onchange) {
  return h('label', { class: 'switch' },
    h('input', { type: 'checkbox', checked, disabled: S.busy, onchange: (e) => onchange(e.target.checked) }),
    h('span', { class: 'track' }),
    h('span', {}, label));
}

const can = (feature) => S.me.user.is_admin || !!S.me.user.features[feature];
const isAdmin = () => S.me.user.is_admin;

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const opts = { hour: '2-digit', minute: '2-digit' };
  if (d.toDateString() !== new Date().toDateString()) Object.assign(opts, { weekday: 'short', day: 'numeric', month: 'short' });
  return d.toLocaleString([], opts);
}

function fmtLeft(ts) {
  const secs = Math.max(0, Math.round(ts - Date.now() / 1000));
  if (secs < 60) return 'under a minute';
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return mins % 60 ? `${hrs} h ${mins % 60} min` : `${hrs} h`;
  return `${Math.floor(hrs / 24)} d ${hrs % 24} h`;
}

const timeLeft = (ts) => h('span', { 'data-until': ts }, fmtLeft(ts));
const hoursLabel = (hrs) => (hrs === 1 ? '1 hour' : `${hrs} hours`);

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
        h('div', { class: 'form-actions' },
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
    await fn();
    if (okMessage) toast(okMessage);
    return true;
  } catch (err) {
    toast(err.message, 'err');
    return false;
  } finally {
    S.busy = false;
    render();
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const area = h('textarea', { style: 'position:fixed;opacity:0;top:0' });
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
  toast('Copied');
}

function generatePassword(length) {
  const limit = 256 - (256 % PW_ALPHABET.length); // avoid modulo bias
  const out = [];
  while (out.length < length) {
    const bytes = crypto.getRandomValues(new Uint8Array(length * 2));
    for (const b of bytes) if (b < limit && out.length < length) out.push(PW_ALPHABET[b % PW_ALPHABET.length]);
  }
  return out.join('');
}

function loadPref(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function savePref(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } }

// ---------------------------------------------------------------- data

async function loadStatus() {
  try { S.status = await api('api/status'); } catch (err) { S.status = { ok: false, error: err.message }; }
}

async function loadPorts() {
  try { S.ports = await api('api/ports'); S.portsError = null; } catch (err) { S.portsError = err.message; }
}

async function loadWifi() {
  try { S.wifi = await api('api/wifi'); S.wifiError = null; } catch (err) { S.wifiError = err.message; }
}

async function loadAdmin() {
  try { S.admin = await api('api/admin/users'); S.adminError = null; } catch (err) { S.adminError = err.message; }
}

async function loadHistory() {
  try { S.history = (await api('api/history')).history; S.historyError = null; } catch (err) { S.historyError = err.message; }
}

const LOADERS = { ports: loadPorts, wifi: loadWifi, admin: loadAdmin, history: loadHistory };

async function refreshTab() {
  const loader = LOADERS[S.tab];
  if (loader) await loader();
  render();
}

async function lookupPublicIp() {
  if (!S.me.settings.public_ip_lookup) return;
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 5000);
    const res = await fetch('https://api.ipify.org?format=json', { signal: ctrl.signal, credentials: 'omit', cache: 'no-store' });
    const data = await res.json();
    if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(data.ip || '')) return;
    S.publicIp = data.ip;
    if (S.form && S.form.kind === 'grant' && !S.form.ip) S.form.ip = data.ip;
    if (!S.form || S.form.ip === data.ip) render(); // don't re-render under someone's typing
  } catch (_) { /* offline or blocked - manual entry still works */ }
}

// ------------------------------------------------------------- layout

function availableTabs() {
  const tabs = [];
  // Short labels so all four fit on a phone without scrolling.
  if (can('port_grant') || can('port_open_any')) tabs.push(['ports', 'Ports']);
  if (can('wifi_view') || can('wifi_change')) tabs.push(['wifi', 'WiFi']);
  if (isAdmin()) tabs.push(['admin', 'Admin'], ['history', 'History']);
  return tabs;
}

function selectTab(tab) {
  if (S.tab === tab) return;
  S.tab = tab;
  S.form = null;
  savePref('unifi_control.tab', tab);
  render();
  refreshTab();
}

function logo() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('fill', 'currentColor');
  path.setAttribute('d', 'M12 3a14 14 0 0 1 9.9 4.1l-1.4 1.4A12 12 0 0 0 12 5a12 12 0 0 0-8.5 3.5L2.1 7.1A14 14 0 0 1 12 3Zm0 4a10 10 0 0 1 7.1 2.9l-1.4 1.4A8 8 0 0 0 12 9a8 8 0 0 0-5.7 2.3L4.9 9.9A10 10 0 0 1 12 7Zm0 4a6 6 0 0 1 4.2 1.8l-1.4 1.4A4 4 0 0 0 12 13a4 4 0 0 0-2.8 1.2l-1.4-1.4A6 6 0 0 1 12 11Zm-8 6h16a2 2 0 0 1 2 2v1a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-1a2 2 0 0 1 2-2Zm1.5 2a1 1 0 1 0 0 2 1 1 0 0 0 0-2Zm3 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2Z');
  svg.append(path);
  return svg;
}

function header() {
  const user = S.me.user;
  const st = S.status;
  const dotClass = st == null ? '' : st.ok ? 'ok' : 'err';
  const dotTitle = st == null ? 'Checking UniFi…' : st.ok ? 'Connected to UniFi' : 'UniFi unreachable';
  return h('header', { class: 'top' },
    h('div', { class: 'brand' }, logo(), h('span', {}, 'UniFi Control')),
    h('div', { class: 'who' },
      h('span', { class: `dot ${dotClass}`, title: dotTitle, 'aria-label': dotTitle }),
      h('span', { class: 'user' }, user.name),
      user.is_admin && h('span', { class: 'badge neutral' }, 'Admin')));
}

function banner() {
  const st = S.status;
  if (!st || st.ok) return null;
  return h('div', { class: 'notice err banner' },
    h('span', {}, st.error || 'UniFi is unreachable.',
      isAdmin() && !st.configured ? ' Open Settings → Apps → UniFi Control → Configuration to set it up.' : ''));
}

function render() {
  const root = document.getElementById('app');
  if (S.meError) {
    root.replaceChildren(h('div', { class: 'boot' }, S.meError));
    return;
  }
  if (!S.me) return;
  const tabs = availableTabs();
  if (!tabs.length) {
    root.replaceChildren(header(), h('div', { class: 'empty' }, 'Nothing has been enabled for you here yet. Ask a Home Assistant admin to give you access.'));
    return;
  }
  if (!tabs.some(([id]) => id === S.tab)) S.tab = tabs[0][0];
  const nav = h('nav', { class: 'tabs', role: 'tablist' },
    tabs.map(([id, label]) => h('button', {
      type: 'button', role: 'tab', class: `tab${S.tab === id ? ' active' : ''}`,
      'aria-selected': S.tab === id ? 'true' : 'false', onclick: () => selectTab(id),
    }, label)));
  const panel = { ports: portsPanel, wifi: wifiPanel, admin: adminPanel, history: historyPanel }[S.tab]();
  root.replaceChildren(header(), nav, banner() || '', h('main', {}, panel));
}

const loading = () => h('div', { class: 'empty' }, 'Loading…');
const errorBox = (message) => h('div', { class: 'notice err' }, h('span', {}, message), button('Retry', refreshTab, 'small'));

// ---------------------------------------------------------- port access

function statusBadge(rule) {
  switch (rule.status) {
    case 'restricted': return h('span', { class: 'badge ok', title: 'Only IPs on its list can connect' }, 'Restricted');
    case 'open': return h('span', { class: 'badge warn' }, 'Open to anyone');
    case 'drift': return h('span', { class: 'badge warn' }, 'Changed in UniFi');
    case 'missing': return h('span', { class: 'badge err' }, 'Deleted in UniFi');
    default: return h('span', { class: 'badge neutral' }, 'Not managed');
  }
}

function ipSuggestions() {
  const list = [];
  if (S.publicIp) list.push({ label: 'This device', ip: S.publicIp });
  for (const c of S.me.client_ips) {
    if (!list.some((s) => s.ip === c.ip)) list.push({ label: c.public ? 'Seen by Home Assistant' : 'Local network', ip: c.ip });
  }
  return list;
}

function openForm(form) {
  S.form = form;
  render();
  const input = document.querySelector('.form input[type=text]');
  if (input && !input.value) input.focus();
}

function closeForm() {
  S.form = null;
  render();
}

function portsPanel() {
  if (S.portsError) return errorBox(S.portsError);
  if (!S.ports) return loading();
  const managed = S.ports.rules.filter((r) => r.managed);
  const others = S.ports.rules.filter((r) => !r.managed);
  const out = [];
  if (!managed.length) {
    out.push(h('div', { class: 'empty' }, isAdmin()
      ? 'No port forwards are managed by this app yet. Pick one below to get started.'
      : 'No port forwards have been shared with you yet.'));
  }
  managed.forEach((rule) => out.push(ruleCard(rule)));
  if (isAdmin()) {
    out.push(h('h2', { class: 'section' }, 'Other port forwards in UniFi'));
    if (!others.length) {
      out.push(h('p', { class: 'hint' }, S.ports.rules.length ? 'All of them are managed.' : 'No port forwards exist in UniFi yet - create one there first.'));
    } else {
      out.push(h('p', { class: 'hint' }, 'Managing a rule limits it to an IP list this app controls. Nobody else sees it until you turn on "Visible to users".'));
      others.forEach((rule) => out.push(unmanagedCard(rule)));
    }
  }
  return out;
}

function ruleSubtitle(rule) {
  if (rule.status === 'missing') return 'This port forward no longer exists in UniFi.';
  return `${rule.proto} ${rule.dst_port} → ${rule.fwd}:${rule.fwd_port}${rule.enabled ? '' : ' · disabled in UniFi'}`;
}

function ruleCard(rule) {
  const kids = [
    h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, rule.name), h('div', { class: 'sub' }, ruleSubtitle(rule))),
      statusBadge(rule)),
  ];

  if (rule.status === 'open') {
    kids.push(h('div', { class: 'notice warn' },
      h('span', {}, `Anyone can connect until ${fmtTime(rule.open_until)} (`, timeLeft(rule.open_until), ` left) · opened by ${rule.open_by || 'someone'}`),
      can('port_open_any') && button('Close now', () => closeAny(rule), 'small')));
  }
  if (rule.status === 'drift') {
    kids.push(h('div', { class: 'notice warn' },
      h('span', {}, 'Someone changed this rule in UniFi - it is no longer limited to its IP list.'),
      isAdmin() && button('Repair', () => manage(rule, true), 'small')));
  }

  if (rule.grants && rule.grants.length) {
    kids.push(h('div', { class: 'label' }, isAdmin() ? 'Access granted through this app' : 'Your access'));
    kids.push(h('ul', { class: 'grants' }, rule.grants.map(grantRow)));
  }

  if (rule.status !== 'missing' && !(S.form && S.form.id === rule.id)) {
    const actions = [];
    if (can('port_grant')) {
      const first = ipSuggestions().find((s) => s.label !== 'Local network');
      actions.push(button('Grant access', () => openForm({ kind: 'grant', id: rule.id, ip: first ? first.ip : '', hours: S.me.settings.durations[0], note: '' }), 'primary'));
    }
    if (can('port_open_any') && rule.status !== 'open') {
      actions.push(button('Open to anyone…', () => openForm({ kind: 'open', id: rule.id, hours: S.me.settings.durations[0] })));
    }
    if (actions.length) kids.push(h('div', { class: 'actions' }, actions));
  }
  if (S.form && S.form.id === rule.id) kids.push(S.form.kind === 'grant' ? grantForm(rule) : openAnyForm(rule));

  if (isAdmin()) {
    kids.push(h('div', { class: 'admin-row' },
      rule.status !== 'missing' && toggle('Visible to users', rule.visible, (v) => setPortVisible(rule, v)),
      button('Stop managing', () => unmanage(rule), 'small ghost')));
    if (rule.group) {
      const members = rule.group.members;
      kids.push(h('div', { class: 'sub', style: 'margin-top:8px' },
        `UniFi IP list “${rule.group.name}”: `, members.length ? h('span', { class: 'mono' }, members.join(', ')) : 'empty (nobody can connect)'));
    }
  }
  return h('section', { class: 'card' }, kids);
}

function grantRow(grant) {
  const details = [
    grant.expires_at ? ['ends in ', timeLeft(grant.expires_at), ` · ${fmtTime(grant.expires_at)}`] : 'no expiry',
    isAdmin() && !grant.mine ? ` · by ${grant.user_name}` : '',
    grant.note ? ` · ${grant.note}` : '',
  ];
  return h('li', {},
    h('div', {}, h('div', { class: 'ip mono' }, grant.ip), h('div', { class: 'sub' }, details)),
    button('Revoke', () => revoke(grant), 'small'));
}

function grantForm(rule) {
  const f = S.form;
  const suggestions = ipSuggestions();
  const durationChips = S.me.settings.durations.map((hrs) => chip(hoursLabel(hrs), f.hours === hrs, () => { f.hours = hrs; render(); }));
  if (can('port_permanent')) durationChips.push(chip('No expiry', f.hours === null, () => { f.hours = null; render(); }));
  return h('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); submitGrant(rule); } },
    h('label', { class: 'field' },
      h('span', {}, 'IP address that may connect'),
      h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', placeholder: 'e.g. 203.0.113.7', value: f.ip, required: true, oninput: (e) => { f.ip = e.target.value; } })),
    suggestions.length && h('div', { class: 'chips' },
      suggestions.map((s) => chip(`${s.label}: ${s.ip}`, f.ip === s.ip, () => { f.ip = s.ip; render(); }))),
    suggestions.some((s) => s.label === 'Local network') && !suggestions.some((s) => s.label !== 'Local network')
      ? h('div', { class: 'sub' }, "You seem to be at home, so your internet address couldn't be detected - type the address of the place you'll connect from.")
      : null,
    h('div', { class: 'field' }, h('span', {}, 'For how long'), h('div', { class: 'chips' }, durationChips)),
    h('label', { class: 'field' },
      h('span', {}, 'Note (optional)'),
      h('input', { type: 'text', maxlength: 120, placeholder: "e.g. Sam's laptop at the office", value: f.note, oninput: (e) => { f.note = e.target.value; } })),
    h('div', { class: 'form-actions' },
      button('Cancel', closeForm, 'ghost'),
      button('Grant access', null, 'primary', 'submit')));
}

function openAnyForm(rule) {
  const f = S.form;
  return h('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); submitOpen(rule); } },
    h('div', { class: 'notice warn', style: 'margin-top:0' },
      `Anyone on the internet will be able to reach ${rule.name} until the time runs out. Admins are notified.`),
    h('div', { class: 'field' }, h('span', {}, 'For how long'),
      h('div', { class: 'chips' }, S.me.settings.durations.map((hrs) => chip(hoursLabel(hrs), f.hours === hrs, () => { f.hours = hrs; render(); })))),
    h('div', { class: 'form-actions' },
      button('Cancel', closeForm, 'ghost'),
      button('Open to anyone', null, 'danger', 'submit')));
}

function unmanagedCard(rule) {
  return h('section', { class: 'card muted' },
    h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, rule.name), h('div', { class: 'sub' }, `${ruleSubtitle(rule)} · source: ${rule.source}`)),
      statusBadge(rule)),
    h('div', { class: 'actions' }, button('Manage with this app', () => manage(rule, false))));
}

async function submitGrant(rule) {
  const f = S.form;
  await act(async () => {
    await api(`api/ports/${encodeURIComponent(rule.id)}/grant`, { body: { ip: f.ip.trim(), hours: f.hours, note: f.note.trim() } });
    S.form = null;
    await loadPorts();
  }, `Access granted to ${f.ip.trim()}`);
}

async function submitOpen(rule) {
  const f = S.form;
  await act(async () => {
    await api(`api/ports/${encodeURIComponent(rule.id)}/open`, { body: { hours: f.hours } });
    S.form = null;
    await loadPorts();
  }, `${rule.name} is open to anyone for ${hoursLabel(f.hours)}`);
}

async function closeAny(rule) {
  await act(async () => {
    await api(`api/ports/${encodeURIComponent(rule.id)}/close`, { body: {} });
    await loadPorts();
  }, `${rule.name} is restricted again`);
}

async function revoke(grant) {
  const yes = await confirmBox({ title: `Revoke ${grant.ip}?`, text: 'It will stop being able to connect right away.', ok: 'Revoke', danger: true });
  if (!yes) return;
  await act(async () => {
    await api(`api/grants/${encodeURIComponent(grant.id)}/revoke`, { body: {} });
    await loadPorts();
  }, `${grant.ip} revoked`);
}

async function manage(rule, repair) {
  const text = repair
    ? 'This puts the IP-list restriction back on the rule. Anyone not on the list is cut off.'
    : `The rule will be limited to an IP list this app controls (source right now: ${rule.source}). `
      + 'If it was open to anyone, only IPs you or your users grant will be able to connect from now on.';
  const yes = await confirmBox({ title: repair ? `Repair ${rule.name}?` : `Manage ${rule.name}?`, text, ok: repair ? 'Repair' : 'Manage' });
  if (!yes) return;
  await act(async () => {
    await api(`api/admin/ports/${encodeURIComponent(rule.id)}/manage`, { body: {} });
    await loadPorts();
  }, repair ? 'Restriction re-applied' : `${rule.name} is now managed`);
}

async function unmanage(rule) {
  const yes = await confirmBox({
    title: `Stop managing ${rule.name}?`,
    text: 'Access granted through this app ends now and an open window is closed. The rule stays limited to its IP list in UniFi.',
    ok: 'Stop managing', danger: true,
  });
  if (!yes) return;
  await act(async () => {
    await api(`api/admin/ports/${encodeURIComponent(rule.id)}/unmanage`, { body: {} });
    await loadPorts();
  }, `${rule.name} is no longer managed`);
}

async function setPortVisible(rule, visible) {
  await act(async () => {
    await api(`api/admin/ports/${encodeURIComponent(rule.id)}/visibility`, { body: { visible } });
    await loadPorts();
  }, visible ? `${rule.name} is visible to users` : `${rule.name} is hidden from users`);
}

// ----------------------------------------------------------------- wifi

function wifiPanel() {
  if (S.wifiError) return errorBox(S.wifiError);
  if (!S.wifi) return loading();
  const nets = S.wifi.networks;
  const out = [];
  if (isAdmin()) out.push(h('p', { class: 'hint' }, 'Turn on "Visible to users" for each network household members should see. Admins always see all of them.'));
  if (!nets.length) out.push(h('div', { class: 'empty' }, isAdmin() ? 'No WiFi networks found in UniFi.' : 'No WiFi networks have been shared with you yet.'));
  nets.forEach((net) => out.push(wifiCard(net)));
  return out;
}

function wifiCard(net) {
  const facts = [net.security, net.enabled ? '' : 'turned off', net.guest ? 'guest network' : '', net.hidden_ssid ? 'hidden name' : ''].filter(Boolean).join(' · ');
  const kids = [
    h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, net.ssid), h('div', { class: 'sub' }, facts)),
      net.enabled ? h('span', { class: 'badge ok' }, 'On') : h('span', { class: 'badge neutral' }, 'Off')),
  ];
  const editing = S.form && S.form.kind === 'wifi' && S.form.id === net.id;
  const revealed = S.reveal[net.id];

  if (!net.has_password) {
    kids.push(h('div', { class: 'sub', style: 'margin-top:10px' }, "This network doesn't use a single shared password, so there's nothing to show or change here."));
  } else if (!editing) {
    const actions = [];
    if (can('wifi_view')) actions.push(button(revealed ? 'Hide password' : 'Show password', () => toggleReveal(net)));
    if (can('wifi_change')) actions.push(button('Change password', () => openForm({ kind: 'wifi', id: net.id, password: '' }), 'primary'));
    if (actions.length) kids.push(h('div', { class: 'actions' }, actions));
  }

  if (revealed && !editing) {
    kids.push(h('div', { class: 'reveal' },
      h('div', { class: 'pw-row' }, h('code', { class: 'pw' }, revealed.password), button('Copy', () => copyText(revealed.password), 'small')),
      h('img', { class: 'qr', src: `api/wifi/${encodeURIComponent(net.id)}/qr.svg?t=${revealed.t}`, alt: `QR code to join ${net.ssid}`, width: 180, height: 180 }),
      h('div', { class: 'sub' }, 'Point a phone camera at the code to join.')));
  }
  if (editing) kids.push(wifiForm(net));

  if (isAdmin()) kids.push(h('div', { class: 'admin-row' }, toggle('Visible to users', net.visible, (v) => setWifiVisible(net, v))));
  return h('section', { class: 'card' }, kids);
}

function wifiForm(net) {
  const f = S.form;
  const length = f.password.length;
  return h('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); submitWifi(net); } },
    h('label', { class: 'field' },
      h('span', {}, 'New password'),
      h('div', { class: 'input-row' },
        h('input', {
          type: 'text', class: 'mono', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off',
          minlength: 8, maxlength: 63, required: true, value: f.password, placeholder: '8 to 63 characters',
          oninput: (e) => { f.password = e.target.value; },
        }),
        button('Generate', () => { f.password = generatePassword(S.me.settings.wifi_password_length); render(); }, 'small'))),
    h('div', { class: 'sub' }, length
      ? `${length} characters. Every device on ${net.ssid} - this one too, if it's connected - drops off until it's given the new password.`
      : `Type one, or tap Generate for a random ${S.me.settings.wifi_password_length}-character password.`),
    h('div', { class: 'form-actions' },
      button('Cancel', closeForm, 'ghost'),
      button('Save password', null, 'primary', 'submit')));
}

async function toggleReveal(net) {
  if (S.reveal[net.id]) {
    delete S.reveal[net.id];
    render();
    return;
  }
  await act(async () => {
    const data = await api(`api/wifi/${encodeURIComponent(net.id)}/password`);
    S.reveal[net.id] = { password: data.password, t: Date.now() };
  });
}

async function submitWifi(net) {
  const password = S.form.password;
  const yes = await confirmBox({
    title: `Change the ${net.ssid} password?`,
    text: 'Devices using the old password will be disconnected and need the new one to reconnect.',
    ok: 'Change password', danger: true,
  });
  if (!yes) return;
  await act(async () => {
    await api(`api/wifi/${encodeURIComponent(net.id)}/password`, { body: { password } });
    S.form = null;
    if (can('wifi_view')) S.reveal[net.id] = { password, t: Date.now() };
    await loadWifi();
  }, `Password for ${net.ssid} changed`);
}

async function setWifiVisible(net, visible) {
  await act(async () => {
    await api(`api/admin/wifi/${encodeURIComponent(net.id)}/visibility`, { body: { visible } });
    await loadWifi();
  }, visible ? `${net.ssid} is visible to users` : `${net.ssid} is hidden from users`);
}

// ---------------------------------------------------------------- admin

function adminPanel() {
  if (S.adminError) return errorBox(S.adminError);
  if (!S.admin) return loading();
  const st = S.status || {};
  const labels = S.admin.labels;
  const services = S.me.settings.notify_services;

  const connection = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', {}, 'UniFi connection'),
      st.ok ? h('span', { class: 'badge ok' }, 'Connected') : h('span', { class: 'badge err' }, 'Not connected')),
    h('dl', { class: 'kv' },
      h('dt', {}, 'Mode'), h('dd', {}, st.mode === 'cloud' ? 'UniFi cloud' : 'Local'),
      st.controller && [h('dt', {}, 'Controller'), h('dd', {}, st.controller)],
      st.version && [h('dt', {}, 'Network app'), h('dd', {}, st.version)],
      h('dt', {}, 'Site'), h('dd', {}, st.site || 'default'),
      !st.ok && st.error && [h('dt', {}, 'Problem'), h('dd', {}, st.error)]),
    h('p', { class: 'hint', style: 'margin:12px 0 0' }, "Connection details are set on the app's Configuration tab (Settings → Apps → UniFi Control)."));

  const notifications = h('section', { class: 'card' },
    h('h3', {}, 'Admin notifications'),
    h('p', { class: 'hint', style: 'margin:6px 0 10px' }, services.length
      ? ['Sent to: ', h('span', { class: 'mono' }, services.map((s) => `notify.${s}`).join(', '))]
      : 'None set up yet. Add your phone (e.g. mobile_app_my_phone) under "Notify services" on the Configuration tab.'),
    services.length && h('div', { class: 'actions', style: 'margin-top:0' }, button('Send test notification', testNotification)));

  const defaults = h('section', { class: 'card' },
    h('h3', {}, 'Default permissions'),
    h('p', { class: 'hint', style: 'margin:6px 0 0' }, 'What non-admin users can do unless you customise them below.'),
    h('div', { class: 'perm-grid' }, Object.entries(labels).map(([key, label]) =>
      toggle(label, S.admin.defaults[key], (v) => saveDefaults({ ...S.admin.defaults, [key]: v })))));

  const people = h('section', { class: 'card' },
    h('h3', {}, 'Users'),
    S.admin.users.map((user) => userRow(user, labels)));

  return [connection, notifications, defaults, people];
}

function userRow(user, labels) {
  const badges = [
    user.is_admin && h('span', { class: 'badge neutral' }, 'Admin'),
    !user.is_active && h('span', { class: 'badge neutral' }, 'Inactive'),
  ];
  const head = h('div', { class: 'who-line' },
    h('div', {}, h('strong', {}, user.name), user.username && h('span', { class: 'sub' }, ` @${user.username}`), ' ', badges),
    !user.is_admin && (user.custom
      ? h('span', { class: 'sub' }, 'Custom · ', h('button', { type: 'button', class: 'btn link', disabled: S.busy, onclick: () => saveUser(user, null) }, 'use defaults'))
      : h('span', { class: 'sub' }, 'Using defaults')));
  if (user.is_admin) {
    return h('div', { class: 'user-row' }, head, h('div', { class: 'sub', style: 'margin-top:4px' }, 'Home Assistant admins can use everything, including this page.'));
  }
  return h('div', { class: 'user-row' }, head,
    h('div', { class: 'perm-grid' }, Object.entries(labels).map(([key, label]) =>
      toggle(label, user.features[key], (v) => saveUser(user, { ...user.features, [key]: v })))));
}

async function saveUser(user, features) {
  await act(async () => {
    await api(`api/admin/users/${encodeURIComponent(user.id)}`, { body: { features } });
    await loadAdmin();
  }, features ? `Saved for ${user.name}` : `${user.name} now uses the defaults`);
}

async function saveDefaults(features) {
  await act(async () => {
    await api('api/admin/defaults', { body: { features } });
    await loadAdmin();
  }, 'Defaults saved');
}

async function testNotification() {
  await act(() => api('api/admin/test-notification', { body: {} }), 'Test notification sent');
}

// -------------------------------------------------------------- history

function historyPanel() {
  if (S.historyError) return errorBox(S.historyError);
  if (!S.history) return loading();
  if (!S.history.length) return h('div', { class: 'empty' }, 'Nothing has happened yet.');
  return h('section', { class: 'card' },
    h('ul', { class: 'history' }, S.history.map((entry) => h('li', {},
      h('span', { class: 'icon', 'aria-hidden': 'true' }, ACTION_ICONS[entry.action] || '•'),
      h('div', {}, h('div', {}, entry.summary), h('div', { class: 'sub' }, fmtTime(entry.ts)))))));
}

// ----------------------------------------------------------------- boot

function tickCountdowns() {
  let expired = false;
  document.querySelectorAll('[data-until]').forEach((el) => {
    const until = Number(el.dataset.until);
    el.textContent = fmtLeft(until);
    if (until <= Date.now() / 1000) expired = true;
  });
  return expired;
}

async function boot() {
  try {
    S.me = await api('api/me');
  } catch (err) {
    S.meError = err.message;
    render();
    return;
  }
  const tabs = availableTabs();
  const saved = loadPref('unifi_control.tab');
  S.tab = tabs.some(([id]) => id === saved) ? saved : (tabs[0] || [null])[0];
  render();
  lookupPublicIp();
  await Promise.all([loadStatus(), refreshTab()]);
  render();

  let ticks = 0;
  setInterval(async () => {
    ticks += 1;
    const expired = tickCountdowns();
    if (document.hidden || S.form || S.busy) return;
    // Refresh soon after something runs out, otherwise every 30 s.
    if (expired || ticks % 6 === 0) {
      if (ticks % 24 === 0) await loadStatus();
      await refreshTab();
    }
  }, 5000);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !S.form && !S.busy) refreshTab();
  });
}

boot();
