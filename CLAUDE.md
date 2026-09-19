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

python -m pytest -q                       # full suite, no browser or network needed
python -m pytest tests/test_scoring.py -q  # one file
python -m pytest -k fingerprint -q         # one test by name

python -m jobauto doctor                  # validate config; run this first, always
python -m jobauto login --portal naukri   # one-time manual sign-in per portal
python -m jobauto discover                # search + score; applies to nothing
python -m jobauto shortlist --why         # ranked results with scoring reasons
python -m jobauto apply --dry-run         # show what would be applied to
python -m jobauto apply --limit 5         # prepare 5, prompting before each submit
python -m jobauto review                  # anything prepared but never sent
python -m jobauto web                     # local dashboard on 127.0.0.1:5057

# Cloud mode
python -m jobauto cloud                   # run the hosted app locally (SQLite)
python -m jobauto link --url U --token T  # link this PC to a deployment
python -m jobauto agent                   # sync agent; polls cloud, runs work
```

Note: **port 5000 is reserved on Windows** (Hyper-V/WinNAT excluded ranges).
Flask appears to start but never binds. Defaults avoid it.

`pytest.ini` sets `pythonpath = src`, so tests run without installing the
package. Running the CLI from the repo root needs `PYTHONPATH=src`.

## The invariant that matters

**Nothing is ever submitted without a human keystroke.** This is not a
preference, it is the design. Three independent layers enforce it, and a change
that weakens any of them is a change to the product, not a refactor:

1. `preferences.application.auto_submit` ships `false` — [review.py](src/jobauto/review.py) prompts per application.
2. A portal setting `risk.force_manual_submit` (LinkedIn) is refused in
   [`PortalAdapter.submit`](src/jobauto/portals/base.py), *not* in the review gate — so no CLI flag or
   config edit can route around it.
3. Any application with escalated (unanswered) screening questions is never
   auto-submitted regardless of settings — see `ReviewGate.ask`.

Tests pin all three (`test_auto_submit_ships_off`,
`test_linkedin_forces_manual_submit`, `test_force_manual_submit_cannot_be_overridden`).

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

The important separation: **adapters are dumb, the pipeline holds policy.**
Daily caps, cooldowns, active hours, dedupe and the stop-on-challenge decision
all live in `pipeline.py`. An adapter only knows how to scrape and click.

### Adding a portal

The extensibility path is deliberate and should stay this cheap:

1. Write `config/portals/<name>.yaml` with selectors and an
   `adapter: "jobauto.portals.<name>:<Name>Adapter"` line.
2. Subclass `ConfigDrivenAdapter` in `src/jobauto/portals/<name>.py`.

[hirist.py](src/jobauto/portals/hirist.py) is the reference: a class body with
no overrides at all. Search, pagination, detail fetch, apply, pacing and caps
are all inherited and driven from YAML. `registry.py` resolves the
`adapter:` string dynamically, so **no central list needs editing**.

Only override when the portal genuinely differs — Naukri's chatbot drawer,
LinkedIn's Easy Apply wizard, Indeed's iframe, Instahyre's feed-instead-of-search.

### Selectors belong in YAML, never in Python

Portal DOMs change every few months; this is the main maintenance cost. Every
CSS selector lives in `config/portals/*.yaml` and is read via `self.sel(...)`
dotted paths. A portal redesign should be a YAML edit, never a code change. If
you find yourself typing a CSS selector into a `.py` file, it belongs in the
YAML instead.

### Scoring

[scoring.py](src/jobauto/scoring.py) reads everything from
`preferences.yaml` — roles, weights, penalties, thresholds. Adding a role or
dealbreaker must never require touching this module.

Two stages: **hard filters** drop a job outright (excluded keyword, blocked
company/location, stale posting, wildly over-senior); then six weighted
components produce 0–100, minus penalties. `ScoreBreakdown.reasons` is retained
and shown by `shortlist --why` — a ranking nobody can interrogate gets ignored,
so keep components explainable.

Weights are validated to sum to 1.0 at load time. `config.validate()` fails
loudly at startup by design: a typo in a weight should not surface as a
mysterious ranking 300 jobs later.

### Credentials

There are none, and it should stay that way. `browser.py` uses Playwright's
`launch_persistent_context` with a per-portal profile dir under `data/browser/`.
You sign in by hand once; cookies persist; MFA never has to be automated. **Do
not add password storage, and do not add a headless-login path.**

Per-portal profile directories are deliberate — a cookie reset or a ban on one
portal must not cascade to the others.

### Screening answers

[forms.py](src/jobauto/forms.py) matches live questions against
`profile.screening_answers`. Two rules are load-bearing:

- Anything matching `never_auto_answer` (Aadhaar, PAN, DOB, bank) is **always**
  escalated, even when a canned answer would match.
- An unmatched question is left blank and escalated, **never guessed**. A wrong
  auto-answer is worse than no application.

Match confidence is thresholded at 0.7, and longer phrase matches win — so
"expected ctc" beats "current ctc" on the question "What is your expected CTC?".

### Dedupe

`Job.fingerprint` hashes normalised title + company + city, stripping
seniority words (`senior`, `jr`, `lead`, roman numerals). The same role posted
on four portals collapses to one row; the `sightings` table records each portal
it was seen on. Changing the fingerprint recipe invalidates existing dedupe
state in `data/jobauto.db`.

## Anti-detection posture

Realistic pacing, headed browser by default, per-portal action caps, active-hours
enforcement, and **stop-the-portal-on-challenge** (`ChallengeDetected` is
deliberately not retried — retrying into a bot check is how accounts get banned).

This is about not tripping naive checks. It is not, and should not become, an
attempt to defeat real bot detection — that is what the human-in-the-loop design
is for. Don't add CAPTCHA solving or proxy rotation.

## Testing

121 tests, no browser or network. `tests/test_pipeline.py` uses a `FakeAdapter`
and monkeypatches `pipeline.session` / `registry.build` — that pattern is how to
test flow changes without Playwright. `test_shipped_config_is_valid` and
`test_every_portal_resolves` validate the committed YAML itself, so a broken
selector file or a typo'd adapter path fails the suite. `tests/test_web.py`
uses the Flask test client with `JOBAUTO_DATA_DIR` pointed at a tmp dir, so web
tests never touch real application history. `tests/test_cloud.py` does the same
with `JOBAUTO_CLOUD_DB` and calls `clouddb.reset_engine()` around each test, so
the SQLAlchemy engine does not leak between them.

## Web dashboard

[src/jobauto/web/](src/jobauto/web/) is a Flask app serving one HTML page plus a
JSON API, with vanilla JS and **no build step** — keep it that way; a bundler
would add a compile stage to a tool whose whole point is running locally with
minimal setup.

It reads the same SQLite DB and calls the same `Pipeline`, so it is a second
front-end, never a second implementation. Fix behaviour in `pipeline.py` /
`scoring.py`, not in the API layer.

Three constraints worth preserving:

- **One background task at a time** (`TASK` in `app.py`). Two browser sessions
  against the same portal profile directory corrupt the session.
- **Preference saves validate before committing.** `POST /api/preferences`
  writes, calls `load_config()`, and rolls back to the previous text on
  `ConfigError` — the dashboard must not be able to brick the CLI.
- **Token auth when exposed.** `JOBAUTO_WEB_TOKEN` gates everything except
  `/static`. The page shows phone number, salary and application history, so
  binding beyond localhost without it is a real leak. `serve()` warns; it does
  not prevent.

The dashboard never clicks submit on a portal. `POST /api/pending/<id>/submitted`
only records that *you* did — it is bookkeeping, not an action, which is what
keeps the web UI consistent with the review-gate invariant.

## Cloud mode

Two processes, deliberately split by what they are allowed to hold:

- [src/jobauto/cloud/](src/jobauto/cloud/) — hosted Flask app: login, job list,
  preferences, task queue. Runs on Render/Fly. **Must never hold portal
  cookies, passwords or browser profiles.** If that boundary is ever crossed,
  the security story collapses: the whole point is that a cloud breach exposes
  job listings, not job portal accounts.
- [src/jobauto/agent/](src/jobauto/agent/) — runs on the user's PC, polls the
  cloud **outbound** (no inbound ports, works behind NAT), executes work with
  the same `Pipeline`, pushes results back.

Sync protocol, all under `/api/agent/*` with `X-Agent-Token` auth:

| Endpoint | Direction | Purpose |
|---|---|---|
| `POST /hello` | agent → cloud | heartbeat; drives the online/offline dot |
| `GET /preferences` | cloud → agent | cloud is the source of truth for prefs |
| `GET /work` | cloud → agent | claims one queued task + lists queued jobs |
| `POST /jobs` | agent → cloud | bulk upsert of discovered+scored jobs |
| `POST /applications` | agent → cloud | prepared applications for review |
| `POST /tasks/<id>/result` | agent → cloud | task outcome and log |

Invariants with tests behind them:

- **Signup closes after the first account** unless `JOBAUTO_ALLOW_SIGNUP=1`. A
  deployed URL must not let strangers register.
- **Agent tokens are not browser sessions.** A logged-in cookie cannot call
  `/api/agent/*`, and an agent token cannot call user routes.
- **Rediscovery never clears a queued job** — a user action outranks a sync.
- **Every query is scoped by `user_id`**; cross-account access is tested for
  jobs, tasks and applications.
- **Preferences validate before saving**, same rules as the local validator.

The cloud never submits an application. `POST /api/applications/<id>/submitted`
records that the *user* did — same bookkeeping-not-action rule as the local web
UI, which is what keeps cloud mode consistent with the review-gate invariant.

`db.py` normalises `postgres://` → `postgresql+psycopg://` because Render and
Heroku hand out the old scheme that SQLAlchemy 2 rejects.

## Config layering

`config/<name>.local.yaml` overrides `config/<name>.yaml` when present, for both
top-level configs and portal files. Committed files are templates; real details
go in `.local.yaml`, which is gitignored. `data/` and `resumes/` are gitignored
too — browser profiles hold live session cookies.

## Known limits

- Selectors in `config/portals/*.yaml` are best-effort and unverified against
  the live sites. `login` failing to detect a successful sign-in usually means a
  stale `auth.logged_in_selector`, not a real login failure.
- Resume tailoring is configured in `preferences.resume.tailor` but not yet
  implemented — `pick_resume` currently selects a pre-written variant by keyword.
- `notifications` / daily digest is configured but not implemented.
