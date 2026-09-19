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
    if (tab.dataset.view === 'preferences') loadPrefs();
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
async function loadApps() {
  let d;
  try { d = await api('/api/applications'); } catch { return; }

  const wrap = $('#apps');
  wrap.innerHTML = '';
  $('#apps-empty').hidden = d.applications.length > 0;

  d.applications.forEach((app) => {
    const card = el('div', 'card');

    const h = el('h3');
    const a = el('a', null, app.title);
    a.href = app.url; a.target = '_blank'; a.rel = 'noopener';
    h.append(a);
    card.append(h, el('div', 'meta', `${app.company} · ${app.portal}`));

    const tags = el('div', 'tagrow');
    const cls = { submitted: 'tag good', prepared: 'tag tagq', skipped: 'tag' }[app.status] || 'tag';
    tags.append(el('span', cls, app.status));
    card.append(tags);

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

    if (app.status === 'prepared') {
      const foot = el('div', 'card-foot');
      foot.append(el('span', 'muted', 'filled on your PC, not sent'));

      const open = el('a', 'btn btn-sm', 'Visit job status');
      open.href = app.url; open.target = '_blank'; open.rel = 'noopener';
      foot.append(open);

      ['submitted', 'skip'].forEach((action) => {
        const b = el('button', 'btn btn-sm' + (action === 'submitted' ? ' btn-primary' : ''),
                     action === 'submitted' ? 'Mark submitted' : 'Skip');
        b.onclick = async () => {
          b.disabled = true;
          await api(`/api/applications/${app.id}/${action}`, { method: 'POST' });
          loadApps(); loadSummary();
        };
        foot.append(b);
      });
      card.append(foot);
    }

    wrap.append(card);
  });
}

/* -------------------------------------------------------- preferences */
function parseRolesFromYaml(text) {
  const block = text.match(/search:\s*\n([\s\S]*?)\n\s*scoring:/);
  if (!block) return [];
  const matches = [...block[1].matchAll(/^\s*-\s*title:\s*["']?([^"'\n]+)["']?/gm)];
  return matches.map((m) => m[1].trim()).filter(Boolean);
}

function parseSearchProfileFromYaml(text) {
  const block = text.match(/search:\s*\n([\s\S]*?)\n\s*scoring:/);
  if (!block) return {};
  const src = block[1];
  const get = (re) => {
    const m = src.match(re);
    return m ? m[1].trim() : '';
  };

  const applyBlock = text.match(/application:\s*\n([\s\S]*?)\n\s*resume:/) || text.match(/application:\s*\n([\s\S]*?)\n\s*$/);
  const appSrc = applyBlock ? applyBlock[1] : '';
  const activeMatch = appSrc.match(/active_hours:\s*\[(\d+)\s*,\s*(\d+)\]/);

  return {
    roles: parseRolesFromYaml(text),
    location: get(/\n\s*preferred:\s*\[(.*?)\]/s) || get(/\n\s*preferred:\s*\[([^\]]+)\]/),
    workModes: [...src.matchAll(/\bwork_mode:\s*\[(.*?)\]/gs)].flatMap((m) => (m[1].match(/['\"]?([A-Za-z-]+)['\"]?/g) || []).map((s) => s.replace(/[\"'\s]/g, ''))),
    expMin: get(/\n\s*min_years:\s*(\d+)/) || '2',
    expMax: get(/\n\s*max_years:\s*(\d+)/) || '8',
    salaryMin: get(/\n\s*minimum_acceptable_lpa:\s*(\d+(?:\.\d+)?)/) || '10',
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
    const trimmed = lines[i].trim();
    if (!trimmed) continue;
    if (/^[A-Za-z0-9_-]+:\s*(?:#.*)?$/.test(trimmed) && trimmed !== `${sectionName}:`) {
      endIndex = i;
      break;
    }
  }

  const before = lines.slice(0, startIndex).join('\n').trimEnd();
  const after = lines.slice(endIndex).join('\n').trimStart();
  const bits = [before, replacement.trim(), after].filter((part) => part && part.trim());
  return bits.join('\n\n');
}

function makeSearchYamlFromForm() {
  const roleChecklist = [...document.querySelectorAll('#role-list input:checked')].map((el) => el.value.trim()).filter(Boolean);
  const customRole = $('#custom-role').value.trim();
  const roles = [...new Set([...roleChecklist, ...(customRole ? [customRole] : [])])];
  const locations = $('#pref-location').value.split(',').map((x) => x.trim()).filter(Boolean);
  const modes = [...document.querySelectorAll('.work-mode-check:checked')].map((el) => el.value);
  const include = $('#keywords-include').value.split(',').map((x) => x.trim()).filter(Boolean);
  const exclude = $('#keywords-exclude').value.split(',').map((x) => x.trim()).filter(Boolean);

  const roleYaml = roles.map((role) => `    - title: "${role.replace(/"/g, '\\"')}"\n      weight: 1.0\n      aliases: ["${role.replace(/"/g, '\\"')}" ]`).join('\n');
  const start = Number($('#active-hours-start').value || 8);
  const end = Number($('#active-hours-end').value || 22);

  return {
    search: `search:\n  roles:\n${roleYaml}\n  keywords:\n    include: [${include.map((x) => `"${x.replace(/"/g, '\\"')}"`).join(', ')}]\n    exclude: [${exclude.map((x) => `"${x.replace(/"/g, '\\"')}"`).join(', ')}]\n  experience:\n    min_years: ${Number($('#exp-min').value || 2)}\n    max_years: ${Number($('#exp-max').value || 8)}\n    current_years: ${(Number($('#exp-min').value || 2) + Number($('#exp-max').value || 8)) / 2}\n  locations:\n    preferred: [${locations.map((x) => `"${x.replace(/"/g, '\\"')}"`).join(', ')}]\n    acceptable: []\n    blocked: []\n    work_mode: [${modes.map((x) => `"${x}"`).join(', ')}]\n    relocate: false\n  compensation:\n    currency: "INR"\n    current_ctc_lpa: ${(Number($('#salary-min').value || 10))}\n    expected_ctc_lpa: ${(Number($('#salary-min').value || 10) + 4)}\n    minimum_acceptable_lpa: ${Number($('#salary-min').value || 10)}\n    negotiable: true\n  company:\n    blocked: []\n    preferred: []\n    exclude_staffing_agencies: false\n    min_employee_rating: 3.0\n  posting:\n    max_age_days: 21\n    require_salary_disclosed: false`,
    application: `application:\n  pacing:\n    active_hours: [${start}, ${end}]`
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
  updated = replaceYamlSection(updated, 'application', form.application);
  $('#pref-yaml').value = updated;
  prefMsg('Quick filters applied to the YAML editor.', true);
};

$('#btn-pref-save').onclick = async () => {
  const btn = $('#btn-pref-save');
  btn.disabled = true;
  try {
    await api('/api/preferences', {
      method: 'POST', body: JSON.stringify({ yaml: $('#pref-yaml').value }),
    });
    prefMsg(agentOnline
      ? 'Saved. Your PC will pick this up within a minute.'
      : 'Saved. Your PC will pick this up when it next comes online.', true);
    loadPrefs();
  } catch (e) { prefMsg(e.message, false); } finally { btn.disabled = false; }
};
$('#btn-pref-reload').onclick = loadPrefs;

/* ------------------------------------------------------------ devices */
async function loadDevices() {
  let d, t;
  try {
    d = await api('/api/agents');
    t = await api('/api/tasks');
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
  });

  if (d.agents.length) {
    const ag = d.agents[0];
    $('#link-cmd').textContent =
      `python -m jobauto link --url ${location.origin} --token ${ag.token}\n` +
      `python -m jobauto agent`;
  }

  const tasks = $('#tasks');
  tasks.innerHTML = '';
  (t.tasks || []).forEach((task) => {
    const row = el('div', 'task-row');
    row.append(el('span', 'state ' + task.status, task.status));
    row.append(el('span', null, task.kind));
    row.append(el('span', 'muted', ago(task.created_at)));
    tasks.append(row);
  });
}

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
    await api('/api/tasks', { method: 'POST', body: JSON.stringify({ kind, payload }) });
    alert(agentOnline
      ? `Queued. Your PC will start within a minute — results appear here.`
      : `Queued. It will run as soon as your PC comes online.`);
    loadSummary();
  } catch (e) { alert(e.message); }
}

$('#btn-discover').onclick = () => queueTask('discover', {});
$('#btn-apply').onclick = () => queueTask('apply', { limit: 5 });
$('#f-min').addEventListener('change', loadJobs);
$('#f-portal').addEventListener('change', loadJobs);

/* --------------------------------------------------------------- boot */
loadSummary();
loadJobs();
setInterval(() => { loadSummary(); }, 15000);
