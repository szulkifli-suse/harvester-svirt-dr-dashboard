'use strict';

const $ = (id) => document.getElementById(id);
const STATUS_EVERY = 10000;
const JOB_EVERY = 2000;

let status = null;
let job = null;
let jobTimer = null;

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'style') Object.assign(n.style, v);
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

const svg = (tag, attrs) => {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: 'same-origin',
    ...opts,
    headers: { 'X-Requested-With': 'dashboard', 'Content-Type': 'application/json' },
  });
  if (r.status === 401 && path !== '/api/login') {
    showLogin();
    throw new Error('login required');
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

// ---- formatting ----------------------------------------------------------

function since(iso) {
  if (!iso) return '—';
  let s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '');
const gib = (bytes) => (bytes / 1024 ** 3).toFixed(1);

function badge(text, level) { return el('span', { class: `badge ${level || ''}` }, text); }

const STATE_LEVEL = { running: 'ok', stopped: 'down', pending: 'warn', stopping: 'warn', 'shut off': 'down' };

function errorBox(msg) { return el('div', { class: 'notice error' }, msg); }

function dl(rows) {
  return el('dl', {}, rows.filter(Boolean).flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)]));
}

function meter(used, total, label) {
  if (used === null || used === undefined || !total) return el('span', { class: 'muted' }, '—');
  const pct = Math.min(100, (used / total) * 100);
  return el('div', { class: 'meter' },
    el('div', { class: 'track' }, el('div', { class: `fill ${pct > 85 ? 'hot' : ''}`, style: { width: `${pct.toFixed(1)}%` } })),
    el('span', {}, label));
}

function table(headers, rows) {
  return el('div', { class: 'table-wrap' },
    el('table', {}, el('thead', {}, el('tr', {}, headers.map((h) => el('th', {}, h)))),
      el('tbody', {}, rows.map((r) => el('tr', {}, r.map((c) => el('td', {}, c)))))));
}

function sparkline(points) {
  const wrap = el('div', { class: 'spark' });
  if (!points || points.length < 2) return wrap;
  const W = 300, H = 48, max = 100;
  const xy = points.map((p, i) => [(i / (points.length - 1)) * W, H - 2 - (p.v / max) * (H - 4)]);
  const d = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('');
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': 'CPU utilisation, last 3 hours' });
  s.append(svg('path', { class: 'area', d: `${d}L${W},${H}L0,${H}Z` }), svg('path', { class: 'line', d, 'vector-effect': 'non-scaling-stroke' }));
  const last = points[points.length - 1].v;
  wrap.append(el('div', { class: 'spark-label' }, el('span', {}, 'CPU · last 3h'), el('span', {}, `${last}% now · peak ${Math.max(...points.map((p) => p.v))}%`)), s);
  return wrap;
}

// ---- rendering -----------------------------------------------------------

function hostState() { return status?.host?.ok ? status.host.data.state : null; }

function renderHost() {
  const box = $('host');
  const h = status.host;
  if (!h.ok) { box.replaceChildren(errorBox(h.error)); return; }
  const i = h.data;
  box.replaceChildren(
    dl([
      ['State', badge(i.state, STATE_LEVEL[i.state])],
      ['Instance', el('span', { class: 'mono' }, i.id)],
      i.name && ['Name', i.name],
      ['Type', i.type],
      ['Public IP', i.public_ip ? el('span', { class: 'mono' }, i.public_ip) : '—'],
      ['Zone', i.az || '—'],
      i.state === 'running' && ['Up for', since(i.launch_time)],
    ]),
    status.cpu?.ok ? sparkline(status.cpu.data) : null,
  );
}

function renderHarvester() {
  const box = $('harvester');
  if (hostState() !== 'running') { box.replaceChildren(el('p', { class: 'empty' }, 'Host is not running.')); return; }
  const ip = status.host.data.public_ip;
  const nodes = status.nodes, vms = status.vms, vol = status.volumes;
  const ready = nodes?.ok ? `${nodes.data.filter((n) => n.ready).length} / ${nodes.data.length}` : '—';
  const running = vms?.ok ? `${vms.data.filter((v) => v.status === 'Running').length} / ${vms.data.length}` : '—';
  let volumes = '—';
  if (vol?.ok) {
    const v = vol.data;
    volumes = el('span', {}, badge(`${v.healthy || 0} healthy`, 'ok'), ' ',
      v.degraded ? badge(`${v.degraded} degraded`, 'warn') : null, ' ',
      v.faulted ? badge(`${v.faulted} faulted`, 'down') : null);
  }
  const apiErr = nodes && !nodes.ok ? nodes.error : null;
  box.replaceChildren(
    dl([
      ['API', status.ping?.data ? badge('answering', 'ok') : badge('not answering', 'down')],
      ['Version', status.version?.ok ? status.version.data : '—'],
      ['Nodes Ready', ready],
      ['VMs running', running],
      ['Volumes', volumes],
      ['Console', el('a', { href: `https://${ip}/`, target: '_blank', rel: 'noopener noreferrer' }, 'Open Harvester UI ↗')],
    ]),
    apiErr ? el('div', { class: 'notice error', style: { marginTop: '12px' } }, `Kubernetes API: ${apiErr}`) : null,
  );
}

function renderKvm() {
  const box = $('kvm');
  if (hostState() !== 'running') { box.replaceChildren(el('p', { class: 'empty' }, 'Host is not running.')); return; }
  const d = status.domains;
  if (!d) { box.replaceChildren(el('p', { class: 'empty' }, 'No data.')); return; }
  if (!d.ok) {
    const ip = status.egress_ip;
    box.replaceChildren(el('div', { class: 'notice' },
      el('strong', {}, 'SSH to the host failed. '), d.error, el('br'),
      ip ? `Allow ${ip}/32 on port 22 in the host security group (Terraform variable public_ip_source_addresses). Start, Stop and Recover need SSH.`
         : 'Start, Stop and Recover need SSH from the dashboard to the host on port 22.'));
    return;
  }
  if (!d.data.length) {
    box.replaceChildren(el('div', { class: 'notice' }, 'No KVM node instances are defined on the host. Run Recover to re-create them.'));
    return;
  }
  box.replaceChildren(el('ul', { class: 'kvm-list' }, d.data.map((x) =>
    el('li', {}, el('span', { class: 'mono' }, x.name), badge(x.state, STATE_LEVEL[x.state] || 'warn')))));
}

function renderNodes() {
  const box = $('nodes');
  const n = status.nodes;
  if (!n) { box.replaceChildren(el('p', { class: 'empty' }, hostState() === 'running' ? 'No data.' : 'Host is not running.')); return; }
  if (!n.ok) { box.replaceChildren(errorBox(n.error)); return; }
  box.replaceChildren(table(['Node', 'Status', 'Roles', 'CPU', 'Memory'], n.data.map((x) => [
    el('span', { class: 'mono' }, x.name),
    el('span', {}, badge(x.ready ? 'Ready' : 'NotReady', x.ready ? 'ok' : 'down'),
      x.maintenance ? [' ', badge('maintenance', 'warn')] : null,
      x.unschedulable && !x.maintenance ? [' ', badge('cordoned', 'warn')] : null),
    x.roles.join(', ') || '—',
    meter(x.cpu_used, x.cpu_alloc, x.cpu_used === null ? '' : `${x.cpu_used.toFixed(1)} / ${x.cpu_alloc} cores`),
    meter(x.mem_used, x.mem_alloc, x.mem_used === null ? '' : `${gib(x.mem_used)} / ${gib(x.mem_alloc)} GiB`),
  ])));
}

const VM_LEVEL = { Running: 'ok', Stopped: 'down', Starting: 'info', Stopping: 'warn', Paused: 'warn' };

function renderVms() {
  const box = $('vms');
  const v = status.vms;
  if (!v) { box.replaceChildren(el('p', { class: 'empty' }, hostState() === 'running' ? 'No data.' : 'Host is not running.')); return; }
  if (!v.ok) { box.replaceChildren(errorBox(v.error)); return; }
  if (!v.data.length) { box.replaceChildren(el('p', { class: 'empty' }, 'No virtual machines.')); return; }
  box.replaceChildren(table(['Name', 'Status', 'Node', 'IP', 'vCPU', 'Memory'], v.data.map((x) => [
    el('span', {}, el('span', { class: 'muted' }, `${x.namespace}/`), x.name),
    el('span', {}, badge(x.status, VM_LEVEL[x.status] || 'warn'),
      x.restore_pending ? [' ', badge('restart on Start', 'info')] : null),
    x.node || '—',
    x.ip ? el('span', { class: 'mono' }, x.ip) : '—',
    x.vcpus,
    x.memory || '—',
  ])));
}

function renderPower() {
  const st = hostState();
  const busy = job?.status === 'running';
  const healthy = status?.summary?.level === 'ok';
  $('btn-start').disabled = busy || !(st === 'stopped' || (st === 'running' && !healthy));
  $('btn-stop').disabled = busy || st !== 'running';
  $('btn-recover').disabled = busy || st !== 'running';
  $('btn-start').textContent = st === 'running' ? 'Bring up' : 'Start';
  let hint = 'Checking…';
  if (busy) hint = `${cap(job.action)} in progress. See Activity below.`;
  else if (st === 'stopped') hint = 'The EC2 host is stopped. Start boots it, recovers the KVM nodes and restarts your VMs.';
  else if (st === 'running' && healthy) hint = 'Everything is up. Stop shuts down VMs and nodes cleanly before stopping the host.';
  else if (st === 'running') hint = 'The host is up but Harvester is not healthy. Try Recover KVM nodes, or Bring up to wait and restore VMs.';
  else if (st) hint = `The EC2 host is ${st}.`;
  $('power-hint').textContent = hint;
}

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function renderSummary() {
  const s = status.summary;
  const pill = $('summary');
  pill.className = `pill ${s.level}`;
  pill.textContent = s.text;
  $('updated').textContent = `Updated ${clock(status.time)}`;
}

function render() {
  if (!status) return;
  renderSummary();
  renderHost();
  renderHarvester();
  renderKvm();
  renderNodes();
  renderVms();
  renderPower();
}

function renderJob() {
  const box = $('job');
  if (!job) { box.replaceChildren(el('p', { class: 'empty' }, 'No operations since the dashboard started.')); return; }
  const level = { running: 'info', succeeded: 'ok', failed: 'down' }[job.status];
  const log = el('pre', { class: 'log mono' }, job.lines.map((l) =>
    el('div', { class: /FAILED|ERROR/.test(l.msg) ? 'bad' : /WARNING/.test(l.msg) ? 'warn' : '' },
      el('span', { class: 't' }, clock(l.t)), l.msg)));
  box.replaceChildren(
    el('div', { class: 'job-head' }, el('strong', {}, cap(job.action)), badge(job.status, level),
      el('span', { class: 'muted' }, `started ${clock(job.started)}${job.finished ? ` · finished ${clock(job.finished)}` : ''}`)),
    log);
  log.scrollTop = log.scrollHeight;
  renderPower();
}

// ---- data loading --------------------------------------------------------

async function refresh() {
  try {
    status = await api('/api/status');
    render();
  } catch (e) {
    if (e.message !== 'login required') $('updated').textContent = `Refresh failed: ${e.message}`;
  }
}

async function pollJob() {
  clearTimeout(jobTimer);
  if (!job) return;
  try {
    const next = await api(`/api/jobs/${job.id}?since=${job.lines.length}`);
    job = { ...next, lines: job.lines.concat(next.lines) };
    renderJob();
  } catch (e) { /* keep the last view; try again */ }
  if (job.status === 'running') jobTimer = setTimeout(pollJob, JOB_EVERY);
  else refresh();
}

async function loadLatestJob() {
  const jobs = await api('/api/jobs');
  if (!jobs.length) { job = null; renderJob(); return; }
  job = { ...jobs[0], lines: [] };
  await pollJob();
}

// ---- actions -------------------------------------------------------------

function confirmAction(action) {
  const i = status?.host?.data || {};
  const running = (status?.vms?.ok ? status.vms.data : []).filter((v) => v.status === 'Running');
  const body = $('c-body');
  const ok = $('c-ok');
  $('c-force').checked = false;
  $('c-force-wrap').hidden = action !== 'stop';
  ok.className = action === 'stop' ? 'btn danger solid' : 'btn primary';

  if (action === 'start') {
    $('c-title').textContent = i.state === 'running' ? 'Bring Harvester up?' : 'Start Harvester?';
    body.replaceChildren(
      el('p', {}, i.state === 'running' ? 'The host is already running. The dashboard will:' : `Start EC2 host ${i.id}, then:`),
      el('ul', {},
        el('li', {}, 'Wait for the boot-time recovery to re-create the KVM nodes. If Harvester does not answer within 5 minutes, run the recovery script over SSH.'),
        el('li', {}, 'Wait for all Harvester nodes to be Ready.'),
        el('li', {}, 'Restart the VMs that the dashboard stopped.')),
      el('p', { class: 'muted' }, 'This usually takes 10–20 minutes.'));
    ok.textContent = i.state === 'running' ? 'Bring up' : 'Start';
  } else if (action === 'stop') {
    $('c-title').textContent = 'Stop Harvester?';
    body.replaceChildren(
      el('p', {}, 'The dashboard will:'),
      el('ul', {},
        el('li', {}, !status?.vms?.ok
          ? 'Stop all running VMs cleanly. The VM list is unavailable right now, so without Force the stop is refused before anything changes.'
          : running.length
            ? `Stop ${running.length} running VM${running.length > 1 ? 's' : ''} cleanly: ${running.map((v) => v.name).join(', ')}. They restart on the next Start.`
            : 'Stop any running VMs cleanly. None are running right now.'),
        el('li', {}, 'Shut down the KVM node instances.'),
        el('li', {}, `Stop EC2 host ${i.id}.`)),
      el('p', { class: 'muted' }, 'Compute billing stops. EBS volumes and the Elastic IP are still billed.'));
    ok.textContent = 'Stop';
  } else {
    $('c-title').textContent = 'Recover KVM node instances?';
    body.replaceChildren(
      el('p', {}, 'Runs recover-harvester-vms.sh on the host. It:'),
      el('ul', {},
        el('li', {}, 'Mounts the data disks and re-creates the vlan1 network.'),
        el('li', {}, 'Re-defines and boots harvester-node-N from the existing disks. No reinstall.'),
        el('li', {}, 'Restarts the socat proxy for ports 443, 6443 and 8443.')),
      el('p', { class: 'muted' }, 'Nodes that are already running are left alone. Afterwards the dashboard waits for all nodes to be Ready.'));
    ok.textContent = 'Recover';
  }

  const dlg = $('confirm');
  dlg.returnValue = '';
  dlg.onclose = async () => {
    if (dlg.returnValue !== 'ok') return;
    try {
      const started = await api(`/api/actions/${action}`, { method: 'POST', body: JSON.stringify({ force: $('c-force').checked }) });
      job = { ...started, lines: [] };
      renderJob();
      pollJob();
    } catch (e) {
      alert(`Could not ${action}: ${e.message}`);
    }
  };
  dlg.showModal();
}

// ---- auth ----------------------------------------------------------------

function showLogin() {
  $('app').hidden = true;
  $('login').hidden = false;
  $('password').focus();
}

async function boot() {
  $('login').hidden = true;
  $('app').hidden = false;
  await refresh();
  await loadLatestJob().catch(() => {});
}

$('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('login-error').textContent = '';
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ password: $('password').value }) });
    $('password').value = '';
    boot();
  } catch (e) {
    $('login-error').textContent = e.message;
  }
});

$('logout').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' }).catch(() => {});
  showLogin();
});
$('refresh').addEventListener('click', refresh);
$('btn-start').addEventListener('click', () => confirmAction('start'));
$('btn-stop').addEventListener('click', () => confirmAction('stop'));
$('btn-recover').addEventListener('click', () => confirmAction('recover'));

setInterval(() => { if (!document.hidden && !$('app').hidden) refresh(); }, STATUS_EVERY);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('app').hidden) refresh(); });

api('/api/status').then(boot).catch(() => {});
