# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`jobauto` — a preference-driven job application assistant for Indian job portals
(Naukri, LinkedIn, Indeed, Instahyre, Hirist). It discovers jobs, scores them
against configured preferences, fills the application form, and **stops for a
human to submit**.

## Commands

```bash
pip install -r requirements.txt
python -m playwright install chromium     # one time, for browser automation

python -m pytest -q                       # full suite (~3 min), no browser or network
python -m pytest tests/test_scoring.py -q  # one file
python -m pytest -k fingerprint -q         # tests matching a name

python -m jobauto doctor                  # validate config; shows portal cool-offs. Run first
python -m jobauto doctor --clear-cooldown indeed   # retry a parked portal now
python -m jobauto login --portal naukri   # one-time manual sign-in per portal
python -m jobauto discover                # search + score; applies to nothing
python -m jobauto shortlist --why         # ranked results with scoring reasons
python -m jobauto apply --dry-run         # show what would be applied to
python -m jobauto apply --limit 5         # prepare 5, prompting before each submit
python -m jobauto review                  # anything prepared but never sent
python -m jobauto dump --portal hirist    # save a portal's rendered results page to data/debug/
python -m jobauto web                     # local dashboard on 127.0.0.1:5057

# Cloud mode
python -m jobauto cloud                   # run the hosted app locally (SQLite)
python -m jobauto link --url=U --token=T  # link this PC; also registers autostart
python -m jobauto autostart [--status|--remove]   # keep the agent running via Task Scheduler
python -m jobauto agent                   # run the sync agent in this window
```

Use `--token=VALUE`, never `--token VALUE`: tokens are base64url and can
begin with `-`, which argparse reads as an option.

`pytest.ini` sets `pythonpath = src`, so tests run without installing the
package. Running the CLI from the repo root needs `PYTHONPATH=src`.

**Port 5000 is reserved on Windows** (Hyper-V excluded ranges). Flask appears
to start but never binds. Defaults avoid it.

**Windows helpers at the repo root:** `setup-agent.bat` installs/upgrades the
agent into `~\.jobauto\venv` (a non-editable install — see below), then
registers autostart; `start-agent.bat` does the install check and runs the
agent in the foreground. The installed copy is a **snapshot**: code changes
in this checkout do nothing until `setup-agent.bat` is re-run. A symptom like
`invalid choice: 'autostart'` means the installed copy predates the command.

Never `pip install -e` this repo into that venv. An editable install makes
`_running_from_checkout()` true, and `data_dir()` then moves to `<repo>/data`
— away from `~\.jobauto\data`, where the agent link, logins and history live.

## The invariant that matters

**Nothing is ever submitted without a human keystroke.** Three independent
layers enforce it, and a change that weakens any of them is a change to the
product, not a refactor:

1. `preferences.application.auto_submit` ships `false` — [review.py](src/jobauto/review.py) prompts per application.
2. `risk.force_manual_submit` (LinkedIn) is refused in
   [`PortalAdapter.submit`](src/jobauto/portals/base.py), *not* in the review gate.
3. Any application with escalated (unanswered) screening questions is never
   auto-submitted regardless of settings — `ReviewGate.ask`.

Tests pin all three (`test_auto_submit_ships_off`,
`test_linkedin_forces_manual_submit`, `test_force_manual_submit_cannot_be_overridden`).
In agent mode `ReviewGate(interactive=False)` defers instead of prompting.

## Architecture

Flow: **discover → score → prepare → review → submit**, orchestrated by
[pipeline.py](src/jobauto/pipeline.py).

```
config/preferences.yaml ──┐
config/profile.yaml ──────┼──> config.py (validates) ──> Scorer ──> Database
config/portals/*.yaml ────┘                                 ▲           │
                                                            │           ▼
        browser.py (persistent context) ──> adapters ──> Job ──> ReviewGate ──> submit
```

**Adapters are dumb, the pipeline holds policy.** Daily caps, cooldowns,
active hours, dedupe, portal cool-off, the batch budget and stop-on-challenge
all live in `pipeline.py`. An adapter only knows how to scrape and click.

### Adding a portal

1. Write `config/portals/<name>.yaml` with selectors and an
   `adapter: "jobauto.portals.<name>:<Name>Adapter"` line.
2. Subclass `ConfigDrivenAdapter` in `src/jobauto/portals/<name>.py`.

[hirist.py](src/jobauto/portals/hirist.py) is the reference: no overrides.
`registry.py` resolves the `adapter:` string dynamically. Only override when
the portal genuinely differs — Naukri's chatbot drawer, LinkedIn's Easy Apply
wizard (`fill_application` walks panes), Instahyre's feed-instead-of-search.

### Selectors belong in YAML, never in Python

Every CSS selector lives in `config/portals/*.yaml`, read via `self.sel(...)`.
A portal redesign should be a YAML edit. LinkedIn once carried a Python
fallback list that duplicated the YAML character for character; it was removed
because a second copy is what the next redesign misses.

**Every portal renders results in JavaScript after load.** `_scrape_page`
therefore waits for the *first card* (`CARD_WAIT_MS`), not only the container
— a container selector like `main` matches instantly on any page and used to
hand control back before any card existed. Don't add `main`/`body` as
container fallbacks expecting them to help.

**Selectors cannot be written blind.** Hirist is a Next.js app shell; the
markup a card actually has is only visible in a rendered, signed-in browser.
`jobauto dump --portal X` runs the real scrape, prints the real diagnostic,
and saves HTML + screenshot under `data/debug/`. Write selectors against that.
Selectors for Hirist and Instahyre are currently unverified.

### Bot checks vs account verification

`guard_challenge` raises one of two things, and they want opposite responses:

- `ChallengeDetected` — matched on Cloudflare/PerimeterX URL markers **and
  titles** (`_CHALLENGE_TITLE_MARKERS`). The portal is parked in
  `portal_state` and skipped, before any browser opens, on an escalating
  cool-off (`Database.COOL_OFF_HOURS`: 6h → 24h → 72h). Never retried into.
- `VerificationRequired` — genuine account steps (`verify your email`,
  `/account/verify`). No cool-off; the user does it once.

**Indeed's "Additional Verification Required" is a bot check**, not account
verification, however it is titled — it recurs on every automated visit.
Indeed effectively cannot be searched by this tool; the honest fix is
`enabled: false`. Do not add CAPTCHA solving, proxy rotation, or fingerprint
spoofing — [docs/RISKS.md](docs/RISKS.md) draws that line deliberately. The
browser sends no pinned user agent for the same reason (a pinned UA
contradicts `sec-ch-ua` client hints); a real installed Chrome/Edge is tried
before the bundled Chromium.

### Apply outcomes and what they mean

`AppStatus` values are load-bearing across the local DB, the cloud, and the
scheduler. `_classify(note)` maps an adapter's note to a status via marker
tuples in `pipeline.py`:

| Status | Means | Local retry | Reaches dashboard |
|---|---|---|---|
| `prepared` | form filled, waiting on a submit | terminal | yes (review list) |
| `submitted` / `skipped` | user decided | terminal | yes |
| `external` | handed off to an employer site — "apply by hand" | terminal | yes, as pending action |
| `failed` | automation could not (stale selector, stuck wizard, signed out) | after 24h | yes, as pending action |

Rules that have each been broken before, with tests now:

- A fill that stopped halfway is **not** `prepared`; the pipeline classifies
  from the adapter's note. `prepared` offers a submit button, so it must mean
  a finished form.
- "already applied" → `submitted`; a closed posting → `skipped`. Both terminal,
  or the job returns to the shortlist every morning.
- A stale selector stays `failed` (retryable) — making it terminal would let
  one redesign delete the shortlist for good.
- `Database.pending_review()` is `prepared` only (the review gate).
  `Database.needs_attention()` is `prepared + external + failed` — what the
  agent syncs. Only syncing `prepared` made hand-offs and failures vanish.
- `settle_application` (a dashboard decision arriving on the PC) moves
  `prepared` and `failed` rows, never a real local outcome.

`apply()` always returns the full counts dict; when it could not run it adds
`"reason"` (outside active hours, nothing to apply to). It never returns `{}`.
`summarise()` reads counts only. `done` counts rows *attempted*, not the
budget, so a batch spanning portals fills to its limit. `_paced` closes stray
tabs (an apply button with `target=_blank`) after every application, whatever
the outcome; a new tab after clicking apply means an external hand-off. A
batch is bounded by `APPLY_BUDGET_SECONDS`, and browser timeouts are set on
the **context** so popups inherit them.

`within_active_hours`: equal hours (`[0, 0]`) is an **empty window and blocks**.
"Any hour" is spelled by omitting the key. Shipped default is `[8, 22]`.

### Scoring

[scoring.py](src/jobauto/scoring.py) reads everything from `preferences.yaml`.
Hard filters drop a job outright; six weighted components produce 0–100 minus
penalties. `ScoreBreakdown.reasons` is shown by `shortlist --why` — keep
components explainable. Weights must sum to 1.0; `config.validate()` fails
loudly at startup by design.

### Credentials

There are none. `browser.py` uses `launch_persistent_context` with a
per-portal profile dir under `data/browser/`. Sign in by hand once; cookies
persist. **Do not add password storage or a headless-login path.** Per-portal
profile directories are deliberate — `_cleanup_stale_browser_session` kills
only a Chrome holding *that* profile, never others.

### Screening answers

[forms.py](src/jobauto/forms.py): anything matching `never_auto_answer`
(Aadhaar, PAN, DOB, bank) is always escalated; an unmatched question is left
blank and escalated, never guessed. Match confidence threshold 0.7; longer
phrase matches win.

### Dedupe

`Job.fingerprint` hashes normalised title + company + city, stripping
seniority words. Changing the recipe invalidates dedupe state in
`data/jobauto.db`. Test data gotcha: "Backend Developer" and "Senior Backend
Developer" at the same company collapse to one row.

## Testing

557 tests across 20 files, no browser or network. Patterns worth copying:

- `tests/test_pipeline.py` — `FakeAdapter`, `run_discover()`, `_fake_portal()`
  monkeypatch `pipeline.session` / `registry.build`; `_diag_adapter(config,
  search)` builds a `ConfigDrivenAdapter` for diagnostics.
- `tests/test_cloud.py` — Flask test client with `JOBAUTO_CLOUD_DB` on a tmp
  path and `clouddb.reset_engine()` around each test; `signup()`,
  `agent_token()`, `H()` helpers. Other cloud test files import these.
- `tests/test_schedule.py` — `enable_schedule(client, **overrides)` makes a
  run due now; `_chain_to_first_batch()` walks discover → batch 1.
- `test_shipped_config_is_valid` and `test_every_portal_resolves` validate the
  committed YAML, so a broken selector file fails the suite.
- Tests that need to sleep monkeypatch `time.sleep` and assert the requested
  duration; never sleep for real.

When editing via shell heredocs on this Windows setup, backslash escapes in
the *content* (`\n`, `\\U`) get mangled into real characters — even inside
comments. Use the Edit tool for anything containing a backslash.

## Web dashboard

[src/jobauto/web/](src/jobauto/web/) is Flask + vanilla JS, **no build step**.
It calls the same `Pipeline`; fix behaviour in `pipeline.py` / `scoring.py`,
not the API layer. One background task at a time (`TASK`); preference saves
validate before committing; `JOBAUTO_WEB_TOKEN` gates everything except
`/static`. `POST /api/pending/<id>/submitted` records that *you* submitted —
bookkeeping, not an action.

## Cloud mode

Two processes, split by what they may hold:

- [src/jobauto/cloud/](src/jobauto/cloud/) — hosted Flask app. **Must never
  hold portal cookies, passwords or browser profiles.**
- [src/jobauto/agent/](src/jobauto/agent/) — runs on the user's PC, polls the
  cloud **outbound**, runs work with the same `Pipeline`, pushes results.

Sync under `/api/agent/*` with `X-Agent-Token`: `POST /hello` (heartbeat),
`GET /preferences` (cloud is source of truth), `GET /work` (claims one task,
lists queued jobs, returns `decided` for the agent to settle locally),
`POST /jobs`, `POST /applications`, `POST /tasks/<id>/result`.

Invariants with tests:

- Signup closes after the first account unless `JOBAUTO_ALLOW_SIGNUP=1`.
- Agent tokens are not browser sessions, and vice versa.
- Every query is scoped by `user_id`.
- **Rediscovery never resets a state the user chose** (`_USER_CHOSEN_STATES`:
  queued, applied, skipped, external) — the portal keeps listing a job long
  after you are done with it.
- **`RETIRES_JOB_STATUSES`** (in `cloud/db.py`, imported by both `app.py` and
  `schedule.py`): any application at all — `prepared` included — removes the
  job from the Jobs list and from the scheduler's backlog count. Jobs is
  "things I have not acted on yet". One rule in one place; the two consumers
  drifted before and the chain queued batches for a backlog the PC said was
  empty.
- Marking an application submitted/skipped moves the `CloudJob` too. They
  are separate rows joined by fingerprint.

### The scheduled run

There is no cron; **the agent's poll is the clock** (`schedule.maybe_start`).
A run is a chain — discover → apply batch 1 → batch 2 … — and each link is
queued only when the previous one reports back (`next_step`). Stop rules, in
order: the batch `failed`; the PC gave a `reason` (that is the truth about
its backlog, and asking again won't change it); the batch *attempted* nothing
and `_backlog()` is empty. A batch that tried jobs and had every one fail
keeps the chain going. The stop reason is written onto the last task's log.
`apply_interval_minutes` works by dating the next task's `created_at` in the
future; `/work` only claims `created_at <= now`.

Timezones: `tzdata` is an explicit dependency. `python:3.11-slim` ships no
zone database and psycopg only pulls `tzdata` on Windows, so without it every
zone silently became UTC and a 09:00 IST run fired at 14:30. `_zone()` still
falls back to UTC but logs, and `/api/schedule` returns `timezone_ok`.
Unquoted `time: 14:00` in YAML is the integer 840 (sexagesimal);
`_parse_time` recovers it.

### Keeping the agent alive

`agent/autostart.py` registers one Task Scheduler task with two triggers: at
logon, and **every 15 minutes forever** with `MultipleInstancesPolicy=IgnoreNew`
— the heartbeat fires whether or not the agent is up; Windows discards the
duplicate when it is and restarts it when it is not. `register()` refuses a
command that cannot start (probing with `PYTHONPATH` stripped, since the task
inherits neither that nor cwd). `jobauto link` registers it; the hosted
installer does too. `autostart --status` translates exit codes and reports
whether the cloud schedule is even switched on.

Stuck-task reapers in `cloud/app.py` (`_reap_orphaned_tasks`,
`_reap_stale_tasks`, `_expire_queued_tasks`) only touch `running` tasks or
`queued` ones older than 24h, so a future-dated interval batch is safe.

`db.py` normalises `postgres://` → `postgresql+psycopg://`.

### Sign-in mid-run

`Pipeline.login_hook: Callable[[portal], bool] | None`. When a search or an
apply batch raises `LoginRequired`, the pipeline calls it (one window at a
time, `_login_lock`), and on True tries that portal **once** more — outside
the browser session, since two browsers on one profile corrupt it. The agent
sets it to `_login_window` (`interactive_login`, `RUN_SIGNIN_MINUTES`, status
pushed to the dashboard) and the CLI to `_attach_login_window`, both only
when headed. `None` keeps the old "run `jobauto login`" message.

### Selector hygiene in detection

`_MINTED` covers `css-`/`sc-`/`mui-style-`/`chakra-` prefixes, hex runs,
and `-<digit><alnum>{3,}` / `__…<digit>…` suffixes; `_TEST_ATTRS`
(`data-testid` etc.) and `role=listitem` beat any class. A card selector
that matches >1.5× the card count on the page is narrowed with
`:has(<title>)`; `_scrape_cards` then de-duplicates by URL, because the
narrowed selector also matches the list and inner boxes.

### The preferences file changes from both ends

The PC writes into `preferences_yaml` too (`/api/agent/portals/<id>/detected`),
so the dashboard must never save text it loaded earlier. Block saves
(schedule, portals) call `freshPrefsText()` and re-base on the server's
current text; the raw editor save sends `expected_updated` and the server
returns **409** if the stamp moved (`api_save_prefs`). While the Preferences
tab is open, `refreshPreferencesView()` re-polls the Portals list and the
editor text (only when untouched) every 15 s — before that, a portal added
from the dashboard showed "waiting for your PC" until a page reload.

## Switching portals off

`preferences.portals.disabled: [indeed, hirist]` switches portals off
everywhere -- search, apply, login. It lives in preferences because that is
the one file the cloud syncs, so the dashboard's *Portals* section reaches
the PC. `apply_portal_preferences()` in `config.py` flips `portal.enabled`
at load, so every existing check works unchanged; it can only turn portals
off, never override a portal's own `enabled: false`. Pausing *every* portal
this way is a legitimate state, not a `ConfigError` -- otherwise the agent
would reject the whole synced file. `doctor` says which of the two disabled a
portal. `GET /api/portals` lists the shipped portal files (selectors only,
nothing secret) with the current switch.

## Custom portals from the dashboard

`preferences.portals.custom.<id>` holds the same mapping a
`config/portals/<id>.yaml` file does. `_materialise_custom_portals()` turns
each into a `PortalConfig` at load, default adapter
`jobauto.portals.generic:ConfigDrivenAdapter` -- so a portal added from the
dashboard is YAML-only, reaches the PC through the existing preference sync,
and never touches disk. It can only **add**: an id matching a shipped portal
is refused on both sides (`_CUSTOM_ID`, `_CUSTOM_REQUIRED` are shared by
`config.py` and the cloud validator). The cloud refuses a bad one at save
time with a reason; the PC skips it and `doctor` says why, because a rejected
sync would undo every other edit in the file. The dashboard keeps one
`portals:` block (`disabled` + `custom`) in `portalState` and re-serialises
it whole on every save, so the two forms cannot drop each other's data.

### Detection: a name and a URL is enough

`portals/detect.py` finds a results page's card without being told what it
looks like: the element that repeats most, with a job link and at least one
other short text in each copy (`MIN_CARDS`, richness check), preferring the
outermost such repeat. Fields are read relative to it -- class-name hints
(`_HINTS`) first, document order second -- and minted class names
(`_MINTED`) are never used. `tokenise_search_url` turns the typed role/city
into `{keywords}`/`{location}` or the slug tokens. Pure functions over HTML;
`tests/test_detect.py` runs them on a synthetic board page with nav and
sidebar noise.

Flow: dashboard saves `portals.custom.<id>` with `detect: true` (validator
requires only name, base_url, url_template). `load_config` materialises it
**disabled** with `raw["pending"]` so `login --portal <id>` works but no
search runs. `LocalAgent.detect_pending_portals` runs at the top of every
`tick`, once an hour per portal, in that portal's own profile; it also checks
the login page (given, or the first sign-in link found) and reports `checks`.
Given only a front page, `_detect_one` signs in first (`looks_signed_out`,
`find_login_link`, a headed window via `_wait_for_signin`), then `_explore`
finds the search box (`find_search_form`, or one `find_jobs_link` away),
types the first role/city, submits, and tokenises the landing URL.
`_look_at` classifies the page (jobs / challenge / login / empty), polling up
to `DETECT_WAIT_SECONDS`; on `empty`/`login` a headed agent opens the
sign-in page in the portal's profile, waits via `wait_for_login`, and looks
again. The page seen is saved to `data/debug/<id>.html`.
`POST /api/agent/portals/<id>/detected` merges the result into the YAML
using `_replace_yaml_block` (same routine as the dashboard), sets
`detect: false`, and the next sync switches the portal on. `jobauto detect`
is the local-only equivalent.

### Self-healing selectors

When a shipped portal's `result_card` matches nothing, `_scrape_detected()`
runs the same detection on the page, uses what it finds for that run, and
appends a note to `adapter.notes` (drained into the run log by
`_search_portal`). `dump` prints `suggested_yaml()` -- the block to paste
into the portal file. It never runs on a login/bot-check landing, and when
it also finds nothing `why_no_results` says the page is probably not a
results page. Fixing the YAML for good is still the right end state; this
keeps results flowing until then.

## Config layering

`config/<name>.local.yaml` overrides `config/<name>.yaml` when present.
Committed files are templates; real details go in `.local.yaml`, which is
gitignored. `data/` and `resumes/` are gitignored too — browser profiles hold
live session cookies, `data/debug/` holds signed-in page dumps.

## Docs

`docs/ADDING-A-PORTAL.md` (the user-facing walkthrough),
`docs/SCHEDULING.md` (the chain and autostart), `docs/RISKS.md`
(anti-detection posture, cool-off), `docs/NEW-MACHINE.md`, `docs/DEPLOY.md`,
`docs/DATABASE.md`, `docs/HOSTING.md`. The README is the user-facing setup
guide; keep its command table in sync with `cli.py`.

## Known limits

- Hirist and Instahyre selectors are unverified against the live DOM; use
  `dump` and write them from real markup.
- Indeed is behind Cloudflare and cannot be searched; recommend
  `enabled: false`.
- Resume tailoring (`preferences.resume.tailor`) is configured but not
  implemented — `pick_resume` selects a pre-written variant by keyword.
- `notifications` / daily digest is configured but not implemented.
- The hosted installer serves `agent.zip` built from the **deployed** commit,
  so a Render deploy must precede installing on a new machine.
