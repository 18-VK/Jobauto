/* jobauto dashboard.
   Plain JS, no build step -- the whole app is served from the local Flask
   process so there is nothing to compile or bundle. */

const TOKEN = window.JOBAUTO_TOKEN || '';

async function api(path, options = {}) {
  const opts = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (TOKEN) opts.headers['X-Auth-Token'] = TOKEN;
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

/* ------------------------------------------------------------- tabs */
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('is-active'));
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('is-active'));
    tab.classList.add('is-active');
    $(`#view-${tab.dataset.view}`).classList.add('is-active');
    if (tab.dataset.view === 'preferences') loadPreferences();
    if (tab.dataset.view === 'pending') loadPending();
  });
});

/* -------------------------------------------------------- summary */
async function loadSummary() {
  let data;
  try { data = await api('/api/summary'); } catch { return; }

  const s = data.stats;
  const grid = $('#statgrid');
  grid.innerHTML = '';
  [
    ['jobs seen', s.jobs_seen],
    ['shortlisted', Math.max(s.scored - s.dropped, 0)],
    ['filtered out', s.dropped],
    ['submitted', s.submitted],
    ['pending', s.prepared],
    ['companies', s.companies],
  ].forEach(([label, value]) => {
    const box = el('div', 'stat');
    box.append(el('b', null, String(value)), el('span', null, label));
    grid.append(box);
  });

  $('#count-pending').textContent = s.prepared;

  const note = data.auto_submit ? 'auto-submit ON' : 'review-then-submit';
  $('#gate-note').textContent = note + (data.active_hours_ok ? '' : ' · outside active hours');

  const sel = $('#f-portal');
  if (sel.options.length <= 1) {
    data.portals.filter((p) => p.enabled).forEach((p) => {
      sel.append(new Option(p.name, p.id));
    });
  }

  const caps = $('#portalcaps');
  caps.innerHTML = '';
  data.portals.forEach((p) => {
    const box = el('div', 'cap' + (p.manual_only ? ' manual' : ''));
    box.append(el('b', null, p.name));
    box.append(el('span', 'muted', p.enabled ? `${p.used_today}/${p.cap} today` : 'disabled'));
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = Math.min(100, (p.used_today / Math.max(p.cap, 1)) * 100) + '%';
    bar.append(fill);
    box.append(bar);
    caps.append(box);
  });
}

/* ------------------------------------------------------ shortlist */
async function loadShortlist() {
  const params = new URLSearchParams();
  const min = $('#f-minscore').value;
  const portal = $('#f-portal').value;
  if (min) params.set('min_score', min);
  if (portal) params.set('portal', portal);

  const wrap = $('#shortlist');
  let data;
  try {
    data = await api('/api/shortlist?' + params);
  } catch (err) {
    wrap.innerHTML = '';
    wrap.append(el('div', 'msg bad', err.message));
    return;
  }

  wrap.innerHTML = '';
  $('#count-shortlist').textContent = data.jobs.length;
  $('#shortlist-empty').hidden = data.jobs.length > 0;

  data.jobs.forEach((job) => wrap.append(jobCard(job)));
}

function jobCard(job) {
  const card = el('div', 'card');

  const head = el('div', 'card-head');
  const score = el('div', 'score ' + (job.band || ''));
  score.append(el('b', null, Number(job.score).toFixed(0)), el('span', null, job.band || 'score'));
  head.append(score);

  const main = el('div');
  const title = el('h3');
  const link = el('a', null, job.title);
  link.href = job.url;
  link.target = '_blank';
  link.rel = 'noopener';
  title.append(link);
  main.append(title);

  const meta = el('div', 'meta');
  const bits = [job.company, job.location, job.salary].filter(Boolean);
  bits.forEach((bit, i) => {
    if (i) meta.append(el('span', 'sep', '·'));
    meta.append(document.createTextNode(bit));
  });
  main.append(meta);

  const tags = el('div', 'tagrow');
  tags.append(el('span', 'tag', job.portal));
  const others = (job.sightings || []).filter((p) => p !== job.portal);
  if (others.length) tags.append(el('span', 'tag', 'also on ' + others.join(', ')));
  if (!job.salary) tags.append(el('span', 'tag warn', 'salary not disclosed'));
  main.append(tags);

  head.append(main);
  card.append(head);

  if (job.reasons && job.reasons.length) {
    const list = el('ul', 'reasons');
    job.reasons.slice(0, 5).forEach((r) => list.append(el('li', null, r)));
    card.append(list);
  }

  const foot = el('div', 'card-foot');
  foot.append(el('span', 'muted', 'not applied yet'));
  const open = el('a', 'btn btn-sm', 'Open listing');
  open.href = job.url;
  open.target = '_blank';
  open.rel = 'noopener';
  foot.append(open);
  card.append(foot);

  return card;
}

/* -------------------------------------------------------- pending */
async function loadPending() {
  const wrap = $('#pending');
  let data;
  try { data = await api('/api/pending'); } catch { return; }

  wrap.innerHTML = '';
  $('#count-pending').textContent = data.pending.length;
  $('#pending-empty').hidden = data.pending.length > 0;

  data.pending.forEach((app) => {
    const card = el('div', 'card');

    const head = el('div', 'card-head');
    const score = el('div', 'score');
    score.append(el('b', null, Number(app.score).toFixed(0)), el('span', null, 'score'));
    head.append(score);

    const main = el('div');
    const title = el('h3');
    const link = el('a', null, app.title);
    link.href = app.url;
    link.target = '_blank';
    link.rel = 'noopener';
    title.append(link);
    main.append(title, el('div', 'meta', `${app.company} · ${app.portal}`));
    if (app.resume) {
      const tags = el('div', 'tagrow');
      tags.append(el('span', 'tag', app.resume));
      main.append(tags);
    }
    head.append(main);
    card.append(head);

    const answered = Object.entries(app.answered || {});
    if (answered.length) {
      const dl = el('dl', 'qblock');
      answered.forEach(([q, a]) => {
        dl.append(el('dt', null, q), el('dd', null, a));
      });
      card.append(dl);
    }

    if (app.escalated && app.escalated.length) {
      const dl = el('dl', 'qblock warn');
      dl.append(el('dt', null, 'Left blank on purpose — these need you:'));
      app.escalated.forEach((q) => dl.append(el('dd', null, q)));
      card.append(dl);
    }

    if (app.error) card.append(el('div', 'msg bad', app.error));

    const foot = el('div', 'card-foot');
    foot.append(el('span', 'muted', 'filled, not submitted'));

    const open = el('a', 'btn btn-sm', 'Open & submit');
    open.href = app.url;
    open.target = '_blank';
    open.rel = 'noopener';
    foot.append(open);

    const done = el('button', 'btn btn-sm btn-primary', 'Mark submitted');
    done.onclick = async () => {
      done.disabled = true;
      await api(`/api/pending/${app.id}/submitted`, { method: 'POST' });
      loadPending();
      loadSummary();
    };
    foot.append(done);

    const skip = el('button', 'btn btn-sm', 'Skip');
    skip.onclick = async () => {
      skip.disabled = true;
      await api(`/api/pending/${app.id}/skip`, { method: 'POST' });
      loadPending();
      loadSummary();
    };
    foot.append(skip);

    card.append(foot);
    wrap.append(card);
  });
}

/* ---------------------------------------------------- preferences */
async function loadPreferences() {
  try {
    const data = await api('/api/preferences');
    $('#pref-yaml').value = data.yaml;
    $('#pref-path').textContent = data.path;
    $('#pref-msg').hidden = true;
  } catch (err) {
    showPrefMsg(err.message, false);
  }
}

function showPrefMsg(text, ok) {
  const box = $('#pref-msg');
  box.className = 'msg ' + (ok ? 'ok' : 'bad');
  box.textContent = text;
  box.hidden = false;
}

$('#btn-pref-save').onclick = async () => {
  const btn = $('#btn-pref-save');
  btn.disabled = true;
  try {
    await api('/api/preferences', {
      method: 'POST',
      body: JSON.stringify({ yaml: $('#pref-yaml').value }),
    });
    showPrefMsg('Saved and validated.', true);
    loadSummary();
  } catch (err) {
    showPrefMsg(err.message, false);
  } finally {
    btn.disabled = false;
  }
};

$('#btn-pref-reload').onclick = loadPreferences;

/* --------------------------------------------------------- tasks */
let pollTimer = null;

async function pollTask() {
  let task;
  try { task = await api('/api/task'); } catch { return; }

  const bar = $('#runbar');
  bar.hidden = !task.running;
  $('#btn-discover').disabled = task.running;
  $('#btn-apply').disabled = task.running;

  if (task.running) {
    $('#runbar-name').textContent = task.name;
    $('#runbar-last').textContent = task.lines[task.lines.length - 1] || '';
  }

  const log = $('#log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = task.lines.join('\n');
  if (atBottom) log.scrollTop = log.scrollHeight;

  if (!task.running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
    loadShortlist();
    loadPending();
    loadSummary();
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTask();
  pollTimer = setInterval(pollTask, 1500);
}

$('#btn-discover').onclick = async () => {
  try {
    await api('/api/discover', { method: 'POST', body: '{}' });
    startPolling();
  } catch (err) { alert(err.message); }
};

$('#btn-apply').onclick = async () => {
  const ok = confirm(
    'This opens a real browser and fills up to 5 applications.\n\n' +
    'Nothing is submitted automatically — each one lands in Pending for you ' +
    'to check and send yourself.\n\nContinue?'
  );
  if (!ok) return;
  try {
    await api('/api/apply', { method: 'POST', body: JSON.stringify({ limit: 5 }) });
    startPolling();
  } catch (err) { alert(err.message); }
};

$('#btn-showlog').onclick = () => document.querySelector('[data-view="activity"]').click();
$('#btn-clearlog').onclick = () => { $('#log').textContent = ''; };
$('#btn-reload').onclick = loadShortlist;
$('#f-minscore').addEventListener('change', loadShortlist);
$('#f-portal').addEventListener('change', loadShortlist);

/* ---------------------------------------------------------- boot */
loadSummary();
loadShortlist();
loadPending();
pollTask();
setInterval(loadSummary, 20000);
