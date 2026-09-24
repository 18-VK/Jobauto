/* jobauto cloud dashboard.
   Session-cookie auth, so no tokens in JS. Plain JS, no build step. */

async function api(path, options = {}) {
  const opts = { headers: { 'Content-Type': 'application/json' }, ...options };
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = '/login'; return {}; }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const ago = (iso) => {
  if (!iso) return 'never';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

let agentOnline = false;

/* --------------------------------------------------------------- tabs */
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('is-active'));
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('is-active'));
    tab.classList.add('is-active');
    $(`#view-${tab.dataset.view}`).classList.add('is-active');
    if (tab.dataset.view === 'preferences') { loadPrefs(); loadSchedule(); loadPortals(); }
    if (tab.dataset.view === 'devices') loadDevices();
    if (tab.dataset.view === 'applications') loadApps();
  });
});

/* ------------------------------------------------------------ summary */
async function loadSummary() {
  let d;
  try { d = await api('/api/summary'); } catch { return; }

  const c = d.counts;
  const grid = $('#statgrid');
  grid.innerHTML = '';
  [['jobs', c.jobs], ['queued', c.queued], ['filtered out', c.dropped],
   ['submitted', c.submitted], ['pending', c.pending]].forEach(([label, v]) => {
    const box = el('div', 'stat');
    box.append(el('b', null, String(v)), el('span', null, label));
    grid.append(box);
  });

  $('#c-jobs').textContent = c.jobs;
  $('#c-apps').textContent = c.pending;

  agentOnline = d.any_agent_online;
  const dot = $('#agent-dot');
  dot.className = 'dot ' + (agentOnline ? 'online' : 'offline');

  const a = d.agents[0];
  $('#agent-state').textContent = agentOnline
    ? 'PC online'
    : (a ? `PC offline · seen ${ago(a.last_seen)}` : 'no PC linked');

  $('#offline-bar').hidden = agentOnline || !d.agents.length;

  $('#btn-discover').textContent = agentOnline ? 'Search portals' : 'Queue a search';
  $('#btn-apply').textContent = agentOnline ? 'Apply to queued' : 'Queue apply run';
}

/* --------------------------------------------------------------- jobs */
async function loadJobs() {
  const p = new URLSearchParams();
  if ($('#f-min').value) p.set('min_score', $('#f-min').value);
  if ($('#f-portal').value) p.set('portal', $('#f-portal').value);
  if ($('#f-age').value) p.set('max_age_days', $('#f-age').value);

  let d;
  try { d = await api('/api/jobs?' + p); } catch { return; }

  const wrap = $('#jobs');
  wrap.innerHTML = '';
  $('#jobs-empty').hidden = d.jobs.length > 0;

  const portals = [...new Set(d.jobs.map((j) => j.portal))];
  const sel = $('#f-portal');
  if (sel.options.length <= 1) portals.forEach((x) => sel.append(new Option(x, x)));

  d.jobs.forEach((job) => wrap.append(jobCard(job)));
}

function jobCard(job) {
  const card = el('div', 'card');

  const head = el('div', 'card-head');
  const score = el('div', 'score ' + (job.band || ''));
  score.append(el('b', null, Number(job.score).toFixed(0)),
               el('span', null, job.band || 'score'));
  head.append(score);

  const main = el('div');
  const h = el('h3');
  const a = el('a', null, job.title);
  a.href = job.url; a.target = '_blank'; a.rel = 'noopener';
  h.append(a);
  main.append(h);

  const meta = el('div', 'meta');
  [job.company, job.location, job.salary].filter(Boolean).forEach((bit, i) => {
    if (i) meta.append(el('span', 'sep', '·'));
    meta.append(document.createTextNode(bit));
  });
  main.append(meta);

  const tags = el('div', 'tagrow');
  tags.append(el('span', 'tag', job.portal));
  if (job.state === 'queued') tags.append(el('span', 'tag tagq', 'queued to apply'));
  if (job.applied) tags.append(el('span', 'tag good', 'applied'));
  if (!job.salary) tags.append(el('span', 'tag warn', 'salary not disclosed'));
  if (job.age_days === 0) tags.append(el('span', 'tag good', 'posted today'));
  else if (job.age_days != null) tags.append(el('span', 'tag', `posted ${job.age_days}d ago`));
  else tags.append(el('span', 'tag', 'no date given'));
  main.append(tags);

  head.append(main);
  card.append(head);

  if (job.reasons && job.reasons.length) {
    const ul = el('ul', 'reasons');
    job.reasons.slice(0, 4).forEach((r) => ul.append(el('li', null, r)));
    card.append(ul);
  }

  const foot = el('div', 'card-foot');
  foot.append(el('span', 'muted', job.applied ? 'already applied' : ''));

  const open = el('a', 'btn btn-sm', 'Visit job description');
  open.href = job.url; open.target = '_blank'; open.rel = 'noopener';
  foot.append(open);

  if (!job.applied) {
    const queued = job.state === 'queued';
    const btn = el('button', 'btn btn-sm' + (queued ? '' : ' btn-primary'),
                   queued ? 'Remove from queue' : 'Queue to apply');
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api(`/api/jobs/${job.id}/queue`, { method: 'POST' });
        loadJobs(); loadSummary();
      } catch (e) { alert(e.message); btn.disabled = false; }
    };
    foot.append(btn);
  }

  card.append(foot);
  return card;
}

/* ------------------------------------------------------- applications */
/* Anything you have decided on is finished and must not reappear as pending. */
const DONE_STATUSES = ['submitted', 'skipped'];

async function loadApps() {
  let d;
  try { d = await api('/api/applications'); } catch { return; }

  const pending = d.applications.filter((a) => !DONE_STATUSES.includes(a.status));
  const done = d.applications.filter((a) => DONE_STATUSES.includes(a.status));

  const wrap = $('#apps');
  wrap.innerHTML = '';
  $('#apps-empty').hidden = pending.length > 0;
  $('#c-apps-pending').textContent = pending.length;
  pending.forEach((app) => wrap.append(appCard(app)));

  const doneWrap = $('#apps-done');
  doneWrap.innerHTML = '';
  $('#c-apps-done').textContent = done.length;
  $('#apps-done-empty').hidden = done.length > 0 || doneWrap.hidden;
  done.forEach((app) => doneWrap.append(appCard(app)));
}

function appCard(app) {
  const card = el('div', 'card');
  const isDone = DONE_STATUSES.includes(app.status);
  if (isDone) card.classList.add('is-done');

  const h = el('h3');
  const a = el('a', null, app.title);
  a.href = app.url; a.target = '_blank'; a.rel = 'noopener';
  h.append(a);
  card.append(h, el('div', 'meta', `${app.company} · ${app.portal}`));

  const tags = el('div', 'tagrow');
  const cls = { submitted: 'tag good', prepared: 'tag tagq', skipped: 'tag',
                external: 'tag warn', failed: 'tag warn' }[app.status] || 'tag';
  // "external" names the mechanism, not what you have to do about it.
  const label = { external: 'apply on the employer site',
                  failed: 'could not be filled' }[app.status] || app.status;
  tags.append(el('span', cls, label));
  if (app.updated_at) tags.append(el('span', 'tag', ago(app.updated_at)));
  card.append(tags);

  // A finished application does not need its answers re-read every time.
  if (!isDone) {
    if (Object.keys(app.answered || {}).length) {
      const dl = el('dl', 'qblock');
      Object.entries(app.answered).forEach(([q, ans]) => {
        dl.append(el('dt', null, q), el('dd', null, ans));
      });
      card.append(dl);
    }

    if (app.escalated && app.escalated.length) {
      const dl = el('dl', 'qblock warn');
      dl.append(el('dt', null, 'Left blank on purpose — these need you:'));
      app.escalated.forEach((q) => dl.append(el('dd', null, q)));
      card.append(dl);
    }
  }

  // Why it stopped. Held in the database and sent to the browser all along,
  // and then never displayed -- so the one sentence explaining what happened
  // to this application only ever existed in a log on the PC.
  if (!isDone && app.note) {
    card.append(el('p', 'note', app.note));
  }

  const foot = el('div', 'card-foot');
  if (isDone) {
    foot.append(el('span', 'muted',
      app.status === 'submitted' ? 'you marked this submitted' : 'you skipped this'));
    const open = el('a', 'btn btn-sm', 'Open listing');
    open.href = app.url; open.target = '_blank'; open.rel = 'noopener';
    foot.append(open);

    // Reversible, because "Skip" is easy to hit by accident.
    const undo = el('button', 'btn btn-sm', 'Move back to pending');
    undo.onclick = async () => {
      undo.disabled = true;
      try {
        await api(`/api/applications/${app.id}/reopen`, { method: 'POST' });
        loadApps(); loadSummary();
      } catch (e) { alert(e.message); undo.disabled = false; }
    };
    foot.append(undo);
  } else {
    // These three reached very different points, and "filled on your PC, not
    // sent" is only true of one of them. Saying it of all three sends you
    // looking for a filled form that was never filled.
    foot.append(el('span', 'muted', {
      external: 'not filled — this one is on the employer’s own site',
      failed: 'the form could not be completed automatically',
    }[app.status] || 'filled on your PC, not sent'));

    const needsYou = app.status === 'external' || app.status === 'failed';
    const open = el('a', 'btn btn-sm' + (needsYou ? ' btn-primary' : ''),
                    app.status === 'external' ? 'Apply on the employer site'
                    : app.status === 'failed' ? `Apply on ${app.portal} yourself`
                    : 'Visit job status');
    open.href = app.url; open.target = '_blank'; open.rel = 'noopener';
    foot.append(open);

    ['submitted', 'skip'].forEach((action) => {
      const b = el('button', 'btn btn-sm' + (action === 'submitted' ? ' btn-primary' : ''),
                   action === 'submitted' ? 'Mark submitted' : 'Skip');
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api(`/api/applications/${app.id}/${action}`, { method: 'POST' });
          loadApps(); loadSummary();
        } catch (e) { alert(e.message); b.disabled = false; }
      };
      foot.append(b);
    });
  }
  card.append(foot);
  return card;
}

/* -------------------------------------------------------- preferences */
function parseRolesFromYaml(text) {
  const block = text.match(/search:\s*\n([\s\S]*?)\n\s*scoring:/);
  if (!block) return [];
  const matches = [...block[1].matchAll(/^\s*-\s*title:\s*["']?([^"'\n]+)["']?/gm)];
  return matches.map((m) => m[1].trim()).filter(Boolean);
}

function extractYamlSection(text, sectionName) {
  const lines = text.split('\n');
  let start = -1;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() === `${sectionName}:`) {
      start = i + 1;
      break;
    }
  }
  if (start < 0) return '';

  const body = [];
  for (let i = start; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^[A-Za-z0-9_-]+:\s*(?:#.*)?$/.test(line) && line === line.trim()) {
      break;
    }
    body.push(line);
  }
  return body.join('\n');
}

function parseSearchProfileFromYaml(text) {
  const block = text.match(/search:\s*\n([\s\S]*?)\n\s*scoring:/);
  if (!block) return {};
  const src = block[1];
  const get = (re) => {
    const m = src.match(re);
    return m ? m[1].trim() : '';
  };

  const appSrc = extractYamlSection(text, 'application');
  const activeMatch = appSrc.match(/active_hours:\s*\[(\d+)\s*,\s*(\d+)\]/);
  const thresholdSrc = extractYamlSection(text, 'thresholds');
  const shortlistMatch = thresholdSrc.match(/shortlist:\s*(\d+(?:\.\d+)?)/);

  return {
    roles: parseRolesFromYaml(text),
    location: get(/\n\s*preferred:\s*\[(.*?)\]/s) || get(/\n\s*preferred:\s*\[([^\]]+)\]/),
    workModes: [...src.matchAll(/\bwork_mode:\s*\[(.*?)\]/gs)].flatMap((m) => (m[1].match(/['\"]?([A-Za-z-]+)['\"]?/g) || []).map((s) => s.replace(/[\"'\s]/g, ''))),
    expMin: get(/\n\s*min_years:\s*(\d+)/) || '2',
    expMax: get(/\n\s*max_years:\s*(\d+)/) || '8',
    salaryMin: get(/\n\s*minimum_acceptable_lpa:\s*(\d+(?:\.\d+)?)/) || '10',
    maxAgeDays: get(/\n\s*max_age_days:\s*(\d+)/) || '21',
    shortlist: shortlistMatch ? Number(shortlistMatch[1]) : 60,
    activeStart: activeMatch ? activeMatch[1] : '8',
    activeEnd: activeMatch ? activeMatch[2] : '22',
    include: get(/\n\s*include:\s*\[(.*?)\]/s) || '',
    exclude: get(/\n\s*exclude:\s*\[(.*?)\]/s) || '',
  };
}

function fillQuickFilterEditorFromYaml(text) {
  const data = parseSearchProfileFromYaml(text || '');
  const roles = [...new Set(((data.roles && data.roles.length) ? data.roles : ['Software Developer', 'Backend Developer']))];
  const rolesWrap = $('#role-list');
  rolesWrap.innerHTML = '';
  roles.forEach((role) => {
    const label = el('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = role;
    input.checked = true;
    label.append(input, document.createTextNode(' ' + role));
    rolesWrap.append(label);
  });

  $('#custom-role').value = '';
  $('#pref-location').value = (data.location || 'Noida, Delhi NCR, Remote').replace(/\[|\]|\"/g, '').replace(/,/g, ', ');
  $('#exp-min').value = data.expMin || '2';
  $('#exp-max').value = data.expMax || '8';
  $('#salary-min').value = data.salaryMin || '10';
  $('#threshold-shortlist').value = String(data.shortlist || 60);
  $('#posting-max-age').value = data.maxAgeDays || '21';
  $('#active-hours-start').value = data.activeStart || '8';
  $('#active-hours-end').value = data.activeEnd || '22';
  const selectedModes = (data.workModes && data.workModes.length ? data.workModes : ['remote', 'hybrid', 'onsite']);
  document.querySelectorAll('.work-mode-check').forEach((cb) => {
    cb.checked = selectedModes.includes(cb.value);
  });
  $('#keywords-include').value = (data.include || 'REST API, SQL Server, .NET').replace(/['\"]/g, '').replace(/\s*,\s*/g, ', ');
  $('#keywords-exclude').value = (data.exclude || 'intern, contract, night shift').replace(/['\"]/g, '').replace(/\s*,\s*/g, ', ');
}

function replaceYamlSection(text, sectionName, replacement) {
  const lines = text.split('\n');
  const startIndex = lines.findIndex((line) => line.trim() === `${sectionName}:`);
  if (startIndex < 0) {
    return `${text.trim()}\n\n${replacement.trim()}\n`;
  }

  let endIndex = lines.length;
  for (let i = startIndex + 1; i < lines.length; i += 1) {
    const raw = lines[i];
    const trimmed = raw.trim();
    if (!trimmed) continue;
    if (/^[A-Za-z0-9_-]+:\s*(?:#.*)?$/.test(raw) && raw === raw.trim()) {
      endIndex = i;
      break;
    }
  }

  const before = lines.slice(0, startIndex).join('\n').trimEnd();
  const after = lines.slice(endIndex).join('\n').trimStart();
  const bits = [before, replacement.trim(), after].filter((part) => part && part.trim());
  return bits.join('\n\n');
}

function normalizeHour(value, fallback) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  if (num < 0) return 0;
  if (num > 24) return 24;
  return num;
}

function makeSearchYamlFromForm() {
  const roleChecklist = [...document.querySelectorAll('#role-list input:checked')].map((el) => el.value.trim()).filter(Boolean);
  const customRole = $('#custom-role').value.trim();
  let roles = [...new Set([...roleChecklist, ...(customRole ? [customRole] : [])])];
  if (!roles.length) {
    roles = ['Software Developer', 'Backend Developer', 'Full Stack Developer', '.NET Developer'];
  }
  const locations = $('#pref-location').value.split(',').map((x) => x.trim()).filter(Boolean);
  const modes = [...document.querySelectorAll('.work-mode-check:checked')].map((el) => el.value);
  if (!modes.length) {
    modes.push('remote', 'hybrid', 'onsite');
  }
  const include = $('#keywords-include').value.split(',').map((x) => x.trim()).filter(Boolean);
  const exclude = $('#keywords-exclude').value.split(',').map((x) => x.trim()).filter(Boolean);

  const roleYaml = roles.map((role) => `    - title: "${role.replace(/"/g, '\\"')}"\n      weight: 1.0\n      aliases: ["${role.replace(/"/g, '\\"')}" ]`).join('\n');
  const start = normalizeHour($('#active-hours-start').value, 8);
  const end = normalizeHour($('#active-hours-end').value, 22);

  const shortlist = Math.max(0, Math.min(100, Number($('#threshold-shortlist').value || 60)));

  return {
    search: `search:\n  roles:\n${roleYaml}\n  keywords:\n    include: [${include.map((x) => `"${x.replace(/"/g, '\\"')}"`).join(', ')}]\n    exclude: [${exclude.map((x) => `"${x.replace(/"/g, '\\"')}"`).join(', ')}]\n  experience:\n    min_years: ${Number($('#exp-min').value || 2)}\n    max_years: ${Number($('#exp-max').value || 8)}\n    current_years: ${(Number($('#exp-min').value || 2) + Number($('#exp-max').value || 8)) / 2}\n  locations:\n    preferred: [${locations.map((x) => `"${x.replace(/"/g, '\\"')}"`).join(', ')}]\n    acceptable: []\n    blocked: []\n    work_mode: [${modes.map((x) => `"${x}"`).join(', ')}]\n    relocate: false\n  compensation:\n    currency: "INR"\n    current_ctc_lpa: ${(Number($('#salary-min').value || 10))}\n    expected_ctc_lpa: ${(Number($('#salary-min').value || 10) + 4)}\n    minimum_acceptable_lpa: ${Number($('#salary-min').value || 10)}\n    negotiable: true\n  company:\n    blocked: []\n    preferred: []\n    exclude_staffing_agencies: false\n    min_employee_rating: 3.0\n  posting:\n    max_age_days: ${Math.max(1, Number($('#posting-max-age').value || 21))}\n    require_salary_disclosed: false`,
    scoring: `scoring:\n  weights:\n    title_match: 0.30\n    skill_overlap: 0.25\n    experience_fit: 0.15\n    location_fit: 0.15\n    compensation_fit: 0.10\n    company_quality: 0.05`,
    thresholds: `thresholds:\n  shortlist: ${shortlist}\n  auto_tailor: ${Math.max(shortlist + 10, 70)}\n  priority: ${Math.max(shortlist + 25, 85)}`,
    application: `application:\n  auto_submit: false\n  daily_caps:\n    naukri: 25\n    linkedin: 15\n    indeed: 20\n    instahyre: 15\n    hirist: 15\n  pacing:\n    between_actions: [1.5, 4.0]\n    between_applications: [20, 75]\n    active_hours: [${start}, ${end}]\n  cooldown_days:\n    same_job: 3650\n    same_company: 30`
  };
}

async function loadPrefs() {
  try {
    const d = await api('/api/preferences');
    $('#pref-yaml').value = d.yaml;
    fillQuickFilterEditorFromYaml(d.yaml);
    $('#pref-updated').textContent = 'last saved ' + ago(d.updated);
    $('#pref-msg').hidden = true;
  } catch (e) { prefMsg(e.message, false); }
}

function prefMsg(text, ok) {
  const b = $('#pref-msg');
  b.className = 'msg ' + (ok ? 'ok' : 'bad');
  b.textContent = text;
  b.hidden = false;
}

$('#btn-pref-apply').onclick = () => {
  const yaml = $('#pref-yaml').value;
  const form = makeSearchYamlFromForm();
  let updated = replaceYamlSection(yaml, 'search', form.search);
  updated = replaceYamlSection(updated, 'scoring', form.scoring);
  updated = replaceYamlSection(updated, 'thresholds', form.thresholds);
  updated = replaceYamlSection(updated, 'application', form.application);
  $('#pref-yaml').value = updated;
  prefMsg('Quick filters applied to the YAML editor.', true);
};

$('#btn-pref-save').onclick = async () => {
  const btn = $('#btn-pref-save');
  const yaml = $('#pref-yaml').value;
  btn.disabled = true;
  try {
    const saved = await api('/api/preferences', {
      method: 'POST', body: JSON.stringify({ yaml }),
    });
    $('#pref-yaml').value = saved.yaml || yaml;
    $('#pref-updated').textContent = 'last saved ' + ago(saved.updated || new Date().toISOString());
    prefMsg(agentOnline
      ? 'Saved to the cloud DB. Your PC will pick this up within a minute.'
      : 'Saved to the cloud DB. Your PC will pick this up when it next comes online.', true);
    await loadPrefs();
  } catch (e) { prefMsg(e.message, false); } finally { btn.disabled = false; }
};

/* A top-level YAML block and its indented body. Done by line rather
   than by regex: a non-greedy pattern here matched only the header and
   left the old body behind, producing duplicate keys where the stale
   value won. */
function replaceYamlBlock(text, name, block) {
  const out = [];
  let skipping = false;
  for (const line of (text || '').split('\n')) {
    if (skipping) {
      if (line.trim() && !/^\s/.test(line)) skipping = false;
      else continue;
    }
    if (line.startsWith(name + ':')) { skipping = true; continue; }
    out.push(line);
  }
  return out.join('\n').replace(/\n+$/, '')
    + '\n\n' + block.replace(/\n+$/, '') + '\n';
}

/* ----------------------------------------------------------- schedule */
const DAY_LABELS = [['mon','Mon'],['tue','Tue'],['wed','Wed'],['thu','Thu'],
                    ['fri','Fri'],['sat','Sat'],['sun','Sun']];

async function loadSchedule() {
  let d;
  try { d = await api('/api/schedule'); } catch { return; }
  const s = d.settings || {};

  $('#sched-enabled').checked = !!s.enabled;
  $('#sched-time').value = s.time || '09:00';
  $('#sched-batch').value = s.batch_size || 5;
  $('#sched-batches').value = s.max_batches || 4;
  $('#sched-interval').value = s.apply_interval_minutes || 0;

  const wrap = $('#sched-days');
  wrap.innerHTML = '';
  const chosen = (s.days || []).map((x) => String(x).toLowerCase().slice(0, 3));
  DAY_LABELS.forEach(([key, label]) => {
    const l = el('label');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.value = key;
    box.className = 'sched-day';
    box.checked = chosen.includes(key);
    l.append(box, document.createTextNode(' ' + label));
    wrap.append(l);
  });

  updateScheduleTotal();
  $('#sched-next').textContent = d.next_run
    ? `next run ${new Date(d.next_run).toLocaleString()}`
    : 'not scheduled';

  // Without the timezone database the server treats your time as UTC, so a
  // 09:00 schedule fires at 14:30 IST. Saying so beats letting someone stare
  // at a next-run time they never chose.
  if (d.timezone_ok === false) {
    $('#sched-next').textContent +=
      `  —  warning: the server does not know "${d.settings.timezone}", `
      + 'so your time is being read as UTC. The times above are wrong.';
    $('#sched-next').classList.add('warn');
  } else {
    $('#sched-next').classList.remove('warn');
  }
}

function updateScheduleTotal() {
  const total = Number($('#sched-batch').value || 0) * Number($('#sched-batches').value || 0);
  $('#sched-total').textContent = `up to ${total} applications a day`;
}

$('#sched-batch').addEventListener('input', updateScheduleTotal);
$('#sched-batches').addEventListener('input', updateScheduleTotal);

$('#btn-sched-save').onclick = async () => {
  const btn = $('#btn-sched-save');
  const days = [...document.querySelectorAll('.sched-day')]
    .filter((c) => c.checked).map((c) => c.value);
  if ($('#sched-enabled').checked && !days.length) {
    return showSchedMsg('Pick at least one day, or turn the schedule off.', false);
  }

  // Edit the schedule block inside the existing YAML rather than rewriting the
  // whole file -- everything else in there is the user's.
  let text = $('#pref-yaml').value || '';
  const block = [
    'schedule:',
    `  enabled: ${$('#sched-enabled').checked}`,
    `  time: "${$('#sched-time').value || '09:00'}"`,
    `  timezone: "${Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Kolkata'}"`,
    `  days: [${days.map((d) => `"${d}"`).join(', ')}]`,
    '  discover: true',
    '  apply: true',
    `  batch_size: ${Number($('#sched-batch').value || 5)}`,
    `  max_batches: ${Number($('#sched-batches').value || 4)}`,
    `  apply_interval_minutes: ${Number($('#sched-interval').value || 0)}`,
  ].join('\n');

  text = replaceYamlBlock(text, 'schedule', block);

  btn.disabled = true;
  try {
    await api('/api/preferences', { method: 'POST', body: JSON.stringify({ yaml: text }) });
    $('#pref-yaml').value = text;
    showSchedMsg(agentOnline
      ? 'Saved. Your PC will pick this up within a minute.'
      : 'Saved. It starts when your PC next comes online.', true);
    loadSchedule();
  } catch (e) { showSchedMsg(e.message, false); } finally { btn.disabled = false; }
};

/* ------------------------------------------------------------ portals */
/* Everything about portals rides in one `portals:` block of the preferences
   YAML -- `disabled` (switched off) and `custom` (added here) -- because
   preferences are the one thing the cloud already syncs down. Both forms
   below rewrite that whole block from shared state, so saving one never
   drops what the other holds. */
const portalState = { disabled: [], custom: {} };

/* The block is a nested mapping of strings, so a serialiser this small is
   correct for it: every scalar is a JSON string, which is valid YAML. */
function yamlLines(obj, indent) {
  const pad = ' '.repeat(indent);
  const out = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v)) {
      out.push(`${pad}${k}: [${v.map((x) => JSON.stringify(String(x))).join(', ')}]`);
    } else if (typeof v === 'object') {
      const inner = yamlLines(v, indent + 2);
      if (inner.length) { out.push(`${pad}${k}:`); out.push(...inner); }
    } else if (typeof v === 'number' || typeof v === 'boolean') {
      out.push(`${pad}${k}: ${v}`);
    } else {
      out.push(`${pad}${k}: ${JSON.stringify(String(v))}`);
    }
  }
  return out;
}

function portalsBlock() {
  const lines = ['portals:',
    `  disabled: [${portalState.disabled.map((d) => JSON.stringify(d)).join(', ')}]`];
  if (Object.keys(portalState.custom).length) {
    lines.push('  custom:');
    lines.push(...yamlLines(portalState.custom, 4));
  }
  return lines.join('\n');
}

async function savePortalsBlock() {
  let text = $('#pref-yaml').value || '';
  text = replaceYamlBlock(text, 'portals', portalsBlock());
  await api('/api/preferences', { method: 'POST', body: JSON.stringify({ yaml: text }) });
  $('#pref-yaml').value = text;
}

async function loadPortals() {
  let d;
  try { d = await api('/api/portals'); } catch { return; }
  portalState.disabled = d.portals.filter((p) => !p.enabled).map((p) => p.id);
  portalState.custom = {};
  d.portals.filter((p) => p.custom).forEach((p) => { portalState.custom[p.id] = p.definition; });

  const wrap = $('#portal-list');
  wrap.innerHTML = '';
  d.portals.forEach((p) => {
    const l = el('label');
    const box = el('input');
    box.type = 'checkbox'; box.className = 'portal-box'; box.value = p.id;
    box.checked = !!p.enabled;
    l.append(box, document.createTextNode(' ' + p.name));
    if (p.custom) {
      l.append(el('span', 'tag', 'custom'));
      const edit = el('button', 'btn btn-sm btn-quiet', 'Edit');
      edit.type = 'button';
      edit.onclick = () => fillCustomForm(p.id, p.definition);
      const del = el('button', 'btn btn-sm btn-quiet', 'Remove');
      del.type = 'button';
      del.onclick = async () => {
        if (!confirm(`Remove ${p.name}? Its saved logins on the PC are kept.`)) return;
        delete portalState.custom[p.id];
        portalState.disabled = portalState.disabled.filter((x) => x !== p.id);
        try { await savePortalsBlock(); showPortalsMsg(`${p.name} removed.`, true); loadPortals(); }
        catch (e) { showPortalsMsg(e.message, false); }
      };
      l.append(document.createTextNode(' '), edit, document.createTextNode(' '), del);
    }
    wrap.append(l);
  });
  const off = d.portals.filter((p) => !p.enabled).map((p) => p.name);
  $('#portal-note').textContent = off.length
    ? `switched off: ${off.join(', ')}` : 'all portals on';
}

$('#btn-portals-save').onclick = async () => {
  const btn = $('#btn-portals-save');
  portalState.disabled = [...document.querySelectorAll('.portal-box')]
    .filter((c) => !c.checked).map((c) => c.value);
  const all = document.querySelectorAll('.portal-box').length;

  btn.disabled = true;
  try {
    await savePortalsBlock();
    showPortalsMsg(portalState.disabled.length === all
      ? 'Saved. Every portal is off -- nothing will run until you turn one back on.'
      : (agentOnline ? 'Saved. Your PC will pick this up within a minute.'
                     : 'Saved. It applies when your PC next comes online.'), true);
    loadPortals();
  } catch (e) { showPortalsMsg(e.message, false); } finally { btn.disabled = false; }
};

/* --------------------------------------------------- add a portal */
const CP_FIELDS = {
  id: 'cp-id', name: 'cp-name', base: 'cp-base', login: 'cp-login',
  marker: 'cp-marker', url: 'cp-url', container: 'cp-container', card: 'cp-card',
  title: 'cp-title', link: 'cp-link', company: 'cp-company', location: 'cp-location',
  salary: 'cp-salary', posted: 'cp-posted', next: 'cp-next', pages: 'cp-pages',
  jd: 'cp-jd', apply: 'cp-apply',
};
const cpVal = (k) => ($('#' + CP_FIELDS[k]).value || '').trim();

function fillCustomForm(id, def) {
  const s = def.search || {}, f = s.fields || {}, a = def.auth || {};
  $('#cp-id').value = id;
  $('#cp-id').disabled = true;             // the id is the key; rename = remove + add
  $('#cp-name').value = def.name || '';
  $('#cp-base').value = def.base_url || '';
  $('#cp-login').value = a.login_url || '';
  $('#cp-marker').value = a.logged_in_selector || '';
  $('#cp-url').value = s.url_template || '';
  $('#cp-container').value = s.results_container || '';
  $('#cp-card').value = s.result_card || '';
  $('#cp-title').value = f.title || '';
  $('#cp-link').value = (f.url && f.url.selector) || '';
  $('#cp-company').value = f.company || '';
  $('#cp-location').value = f.location || '';
  $('#cp-salary').value = f.salary || '';
  $('#cp-posted').value = f.posted || '';
  $('#cp-next').value = (s.pagination && s.pagination.next) || '';
  $('#cp-pages').value = (s.pagination && s.pagination.max_pages) || 3;
  $('#cp-jd').value = (def.detail && def.detail.jd_body) || '';
  $('#cp-apply').value = (def.apply && def.apply.instant_button) || '';
  $('#cp-note').textContent = `editing ${id}`;
  $('#custom-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function clearCustomForm() {
  Object.values(CP_FIELDS).forEach((i) => { $('#' + i).value = ''; });
  $('#cp-pages').value = 3;
  $('#cp-id').disabled = false;
  $('#cp-note').textContent = '';
}
$('#btn-cp-clear').onclick = clearCustomForm;

function customDefinitionFromForm() {
  return {
    name: cpVal('name'),
    base_url: cpVal('base'),
    auth: { mode: 'persistent_profile', login_url: cpVal('login'),
            logged_in_selector: cpVal('marker') },
    search: {
      url_template: cpVal('url'),
      results_container: cpVal('container'),
      result_card: cpVal('card'),
      fields: {
        title: cpVal('title'),
        url: cpVal('link') ? { selector: cpVal('link'), attr: 'href' } : '',
        company: cpVal('company'), location: cpVal('location'),
        salary: cpVal('salary'), posted: cpVal('posted'),
      },
      pagination: { next: cpVal('next'), max_pages: Number(cpVal('pages') || 3) },
    },
    detail: { jd_body: cpVal('jd') },
    apply: { instant_button: cpVal('apply') },
  };
}

$('#btn-cp-save').onclick = async () => {
  const btn = $('#btn-cp-save');
  const id = cpVal('id').toLowerCase();
  const need = { id: 'an id', name: 'a name', base: 'the site URL', url: 'a search URL',
                 card: 'a result-card selector', title: 'a title selector', link: 'a link selector' };
  for (const [k, what] of Object.entries(need)) {
    if (!cpVal(k)) return showCpMsg(`Needs ${what}.`, false);
  }
  if (!/^[a-z][a-z0-9_-]{1,30}$/.test(id)) {
    return showCpMsg('Id: lowercase letters, digits, - or _ only.', false);
  }

  portalState.custom[id] = customDefinitionFromForm();
  btn.disabled = true;
  try {
    await savePortalsBlock();               // the server validates and says why if not
    showCpMsg(`Saved ${id}. On your PC: jobauto login --portal ${id}, then jobauto dump --portal ${id} to check the selectors match.`, true);
    clearCustomForm();
    loadPortals();
  } catch (e) {
    delete portalState.custom[id];
    showCpMsg(e.message, false);
  } finally { btn.disabled = false; }
};

function showCpMsg(text, ok) {
  const box = $('#cp-msg');
  box.className = 'msg ' + (ok ? 'ok' : 'bad');
  box.textContent = text;
  box.hidden = false;
}

function showPortalsMsg(text, ok) {
  const box = $('#portals-msg');
  box.className = 'msg ' + (ok ? 'ok' : 'bad');
  box.textContent = text;
  box.hidden = false;
}

function showSchedMsg(text, ok) {
  const box = $('#sched-msg');
  box.className = 'msg ' + (ok ? 'ok' : 'bad');
  box.textContent = text;
  box.hidden = false;
}

$('#btn-pref-reload').onclick = loadPrefs;

/* ------------------------------------------------------------ devices */
function tokenPanel(agent) {
  const box = el('div', 'token-panel');
  box.append(el('div', 'token-label', 'Agent token'));

  const masked = '•'.repeat(Math.min(agent.token.length, 44));
  const value = el('code', 'token-value', masked);
  value.dataset.shown = 'no';
  box.append(value);

  const actions = el('div', 'token-actions');

  // Hidden by default: this panel is often open while screen-sharing or
  // while someone is standing behind you, and the token is a credential.
  const toggle = el('button', 'btn btn-sm', 'Show');
  toggle.onclick = () => {
    const shown = value.dataset.shown === 'yes';
    value.textContent = shown ? masked : agent.token;
    value.dataset.shown = shown ? 'no' : 'yes';
    toggle.textContent = shown ? 'Show' : 'Hide';
  };
  actions.append(toggle);

  // Copy works without revealing it, which is what you usually want.
  const copy = el('button', 'btn btn-sm btn-primary', 'Copy token');
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(agent.token);
      copy.textContent = 'Copied';
      setTimeout(() => (copy.textContent = 'Copy token'), 1500);
    } catch {
      value.textContent = agent.token;
      value.dataset.shown = 'yes';
      toggle.textContent = 'Hide';
      alert('Copy failed — the token is now shown, select it manually.');
    }
  };
  actions.append(copy);
  box.append(actions);

  box.append(el('div', 'token-hint',
    'Paste this when the installer asks for it. Treat it like a password: '
    + 'anyone holding it can push data into this account. Rotate it if it leaks.'));
  return box;
}

let liveTimer = null;

function startLiveRefresh() {
  if (liveTimer) return;
  liveTimer = setInterval(() => {
    if ($('#view-devices').classList.contains('is-active')) loadDevices();
    else stopLiveRefresh();
  }, 4000);
}

function stopLiveRefresh() {
  if (!liveTimer) return;
  clearInterval(liveTimer);
  liveTimer = null;
}

async function loadDevices() {
  let d, t;
  try {
    d = await api('/api/agents');
    t = await api('/api/tasks');
    loadRetention();
  } catch { return; }

  const wrap = $('#devices');
  wrap.innerHTML = '';
  d.agents.forEach((ag) => {
    const row = el('div', 'device');
    row.append(el('span', 'status' + (ag.online ? ' on' : '')));
    const info = el('div', 'grow');
    info.append(el('b', null, ag.name),
                el('span', 'muted', ag.online ? 'online now' : `last seen ${ago(ag.last_seen)}`
                   + (ag.last_status ? ` · ${ag.last_status}` : '')));
    row.append(info);

    const rot = el('button', 'btn btn-sm', 'Rotate token');
    rot.onclick = async () => {
      if (!confirm('Rotate the token? The PC will stop syncing until you re-link it.')) return;
      await api(`/api/agents/${ag.id}/rotate`, { method: 'POST' });
      loadDevices();
    };
    row.append(rot);
    wrap.append(row);

    // The installer asks for this by hand, so it has to be readable and
    // copyable here -- not buried in a command block.
    wrap.append(tokenPanel(ag));
  });

  if (d.agents.length) {
    const ag = d.agents[0];
    $('#install-cmd').textContent = `irm ${location.origin}/install.ps1 | iex`;
    $('#link-cmd').textContent =
      `python -m jobauto link --url ${location.origin} --token ${ag.token}\n` +
      `python -m jobauto agent`;
  }

  const active = (t.tasks || []).find(
    (x) => x.status === 'running' || x.status === 'queued');
  const live = $('#live-log');
  const note = $('#live-note');
  if (active) {
    let label;
    if (active.status === 'queued') {
      label = agentOnline
        ? `${active.kind} is queued — your PC should pick it up within a minute`
        : `${active.kind} is queued, but your PC is OFFLINE so nothing will run it. `
          + `Start the agent on that machine: jobauto agent`;
    } else {
      label = `${active.kind} is running on your PC`;
    }
    note.className = (active.status === 'queued' && !agentOnline) ? 'msg bad' : 'muted';
    note.textContent = label;
    live.hidden = !active.log;
    if (active.log) {
      const atBottom = live.scrollTop + live.clientHeight >= live.scrollHeight - 40;
      live.textContent = active.log;
      if (atBottom) live.scrollTop = live.scrollHeight;
    }
    startLiveRefresh();
  } else {
    note.textContent = 'Nothing running.';
    live.hidden = true;
    stopLiveRefresh();
  }

  const tasks = $('#tasks');
  tasks.innerHTML = '';
  (t.tasks || []).forEach((task) => {
    const row = el('div', 'task-row');
    row.append(el('span', 'state ' + task.status, task.status));
    row.append(el('span', null, task.kind));
    row.append(el('span', 'muted', ago(task.created_at)));

    // Anything unfinished can be given up on. A run whose agent stopped would
    // otherwise sit here looking active until it ages out.
    if (task.log) {
      row.style.cursor = 'pointer';
      row.title = 'click to show output';
      row.onclick = (e) => {
        if (e.target.tagName === 'BUTTON') return;
        const existing = row.nextElementSibling;
        if (existing && existing.classList.contains('task-log')) {
          existing.remove();
          return;
        }
        const pre = el('pre', 'task-log log', task.log);
        row.after(pre);
      };
    }

    if (task.status === 'running' || task.status === 'queued') {
      const stop = el('button', 'btn btn-sm', 'Cancel');
      stop.onclick = async () => {
        if (!confirm(`Cancel this ${task.kind} run?`)) return;
        stop.disabled = true;
        try {
          await api(`/api/tasks/${task.id}/cancel`, { method: 'POST' });
          loadDevices(); loadSummary();
        } catch (e) { alert(e.message); stop.disabled = false; }
      };
      row.append(stop);
    }
    tasks.append(row);
  });
}

async function loadRetention() {
  let d;
  try { d = await api('/api/retention'); } catch { return; }

  const wrap = $('#retention');
  wrap.innerHTML = '';
  const card = el('div', 'card');

  const pending = Object.entries(d.would_delete || {});
  if (pending.length) {
    card.append(el('div', 'meta', 'next cleanup will remove:'));
    const tags = el('div', 'tagrow');
    pending.forEach(([what, n]) => tags.append(el('span', 'tag warn', `${n} ${what}`)));
    card.append(tags);
  } else {
    card.append(el('div', 'meta', 'nothing old enough to remove'));
  }

  const kept = Object.entries(d.protected || {});
  if (kept.length) {
    const tags = el('div', 'tagrow');
    kept.forEach(([what, n]) => tags.append(el('span', 'tag good', `keeping ${n} ${what}`)));
    card.append(tags);
  }

  const s = d.settings || {};
  const windows = [
    ['unapplied jobs', s.jobs_days], ['applied job rows', s.applied_jobs_days],
    ['finished tasks', s.tasks_days], ['applications', s.applications_days],
  ].map(([label, days]) => `${label}: ${days ? days + 'd' : 'kept forever'}`);
  card.append(el('div', 'meta', windows.join('  ·  ')));
  card.append(el('div', 'meta',
    `runs automatically every ${d.every_hours}h` +
    (d.last_run ? ` · last run ${ago(d.last_run)}` : ' · not run yet this session')));

  wrap.append(card);
}

$('#btn-purge').onclick = async () => {
  const btn = $('#btn-purge');
  btn.disabled = true;
  try {
    const r = await api('/api/retention/purge', { method: 'POST' });
    alert(r.total ? r.summary : 'Nothing old enough to remove.');
    loadRetention(); loadSummary(); loadJobs();
  } catch (e) { alert(e.message); } finally { btn.disabled = false; }
};

async function copyFrom(selector, button) {
  try {
    await navigator.clipboard.writeText($(selector).textContent);
    const original = button.textContent;
    button.textContent = 'Copied';
    setTimeout(() => (button.textContent = original), 1500);
  } catch { alert('Copy failed — select the text manually.'); }
}

$('#btn-copy-install').onclick = (e) => copyFrom('#install-cmd', e.target);

$('#btn-copy').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('#link-cmd').textContent);
    $('#btn-copy').textContent = 'Copied';
    setTimeout(() => ($('#btn-copy').textContent = 'Copy'), 1500);
  } catch { alert('Copy failed — select the text manually.'); }
};

/* -------------------------------------------------------------- tasks */
async function queueTask(kind, payload) {
  try {
    const res = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ kind, payload }) });
    // Saying "Queued." when nothing was queued is how a wedged task queue
    // looks like a dead button.
    if (res.already_pending) {
      alert(`A ${kind} run is already queued or still going - see Activity below.`);
    } else {
      alert(agentOnline
        ? `Queued. Your PC will start within a minute - results appear here.`
        : `Queued. It will run as soon as your PC comes online.`);
    }
    loadSummary();
  } catch (e) { alert(e.message); }
}

$('#btn-discover').onclick = () => queueTask('discover', {});
$('#btn-apply').onclick = () => queueTask('apply', { limit: 5 });
$('#btn-toggle-done').onclick = () => {
  const wrap = $('#apps-done');
  wrap.hidden = !wrap.hidden;
  $('#btn-toggle-done').textContent = wrap.hidden ? 'show' : 'hide';
  $('#apps-done-empty').hidden = wrap.hidden || wrap.children.length > 0;
};

$('#f-min').addEventListener('change', loadJobs);
$('#f-portal').addEventListener('change', loadJobs);
$('#f-age').addEventListener('change', loadJobs);

/* --------------------------------------------------------------- boot */
loadSummary();
loadJobs();
setInterval(() => { loadSummary(); }, 15000);
