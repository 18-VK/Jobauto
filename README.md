# jobauto

A job application assistant for Indian job portals — Naukri, LinkedIn, Indeed,
Instahyre and Hirist.

It searches the portals, scores every listing against your preferences, fills
the application form, and then **stops and waits for you to press submit.**

Deploy it once and you get a private website with a login that you can open from
your phone, your laptop, or anywhere else — **including when your PC is switched
off.**

---

## Using it from anywhere

Two pieces, split deliberately by what each is allowed to hold:

```
   ANY DEVICE                 CLOUD (free host)              YOUR PC
   ──────────                 ─────────────────              ───────
   phone, laptop,             permanent URL                  the agent
   office machine     ──▶     email + password       ◀──     Playwright browser
                              jobs, scores                   your portal logins
                              preferences                    your resumes
                              task queue
                              ↑ never holds a portal login
```

Your job-portal passwords and cookies **never leave your PC**. If the cloud
database leaked tomorrow, nobody would gain access to a single job portal. That
constraint is why the split exists at all.

The agent connects **outbound** to the cloud, so nothing is exposed on your home
network and there are no ports to forward.

### What works with your PC off

| | PC off | PC on |
|---|:---:|:---:|
| Browse ranked jobs with score reasons | ✓ | ✓ |
| Queue jobs to apply to | ✓ *(runs later)* | ✓ *(runs in ~1 min)* |
| Edit preferences — roles, salary, locations | ✓ | ✓ |
| Review and mark applications submitted | ✓ | ✓ |
| Actually fill in applications | — | ✓ |

Queue five jobs from your phone on the train. They're filled in when you get
home and open your laptop.

### Try it before deploying

```bash
python -m jobauto cloud          # http://127.0.0.1:5058
```

Same app, same login, running on your machine. First visit creates your account.

### Then deploy it

```powershell
.\push-to-github.ps1             # private repo, one browser sign-in
```

Then <https://render.com> → **New → Blueprint** → pick the repo → **Apply**.
Render reads [render.yaml](render.yaml), creates the web service and a free
Postgres, and hands you a permanent URL.

Full walkthrough: **[docs/DEPLOY.md](docs/DEPLOY.md)** — about ten minutes.

---

## Why it stops before submitting

Two reasons, both practical.

**Account safety.** Every one of these portals prohibits automated access in its
terms. LinkedIn in particular bans permanently, and your LinkedIn account *is*
your professional presence — losing it costs far more than the time this saves.
A human clicking submit is the single thing that makes the traffic look like a
person using the site.

**It works better.** Mass auto-apply converts badly. Recruiters and ATS filters
spot generic applications. Twenty reviewed applications beat two hundred sprayed
ones, and the review step is where you catch the job that looked great in the
listing and terrible in the JD.

The automation takes an application from roughly six minutes to about twenty
seconds of your attention. That is the win — not removing you from the loop.

See [docs/RISKS.md](docs/RISKS.md) before changing `auto_submit`.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Copy the config templates and fill in your details:

```bash
cp config/profile.yaml config/profile.local.yaml
cp config/preferences.yaml config/preferences.local.yaml
```

Edit `config/profile.local.yaml` — name, email, phone, CTC, notice period,
skills, and your canned screening answers. Then edit
`config/preferences.local.yaml` — the roles you want, locations, salary floor,
and dealbreakers. Both `.local.yaml` files are gitignored.

Drop your resumes in `resumes/` (see `resumes/README.md` for the variants).

Check everything loads:

```bash
PYTHONPATH=src python -m jobauto doctor
```

Sign in to each portal once. A real browser window opens; you log in by hand,
including any OTP. The session is saved, so this is a one-time chore per portal.

```bash
PYTHONPATH=src python -m jobauto login
```

**No passwords are stored anywhere in this project.** It keeps browser cookies,
the same way your normal browser does.

## Three ways to run it

| Mode | Command | Good for |
|---|---|---|
| **Cloud** | deploy + `jobauto agent` | A real website with a login that works when your PC is off. See [docs/DEPLOY.md](docs/DEPLOY.md) |
| **Local dashboard** | `jobauto web` | Browser UI, nothing hosted, no account |
| **Terminal** | `jobauto discover` / `apply` | Scripting and quick runs |

All three drive the same engine. Cloud mode adds a hosted dashboard and a local
agent; the applying always happens on your PC.

## Daily use

Either the dashboard or the terminal — both drive the same engine and the same
database.

### Dashboard

```bash
python -m jobauto web             # http://127.0.0.1:5057
```

Ranked shortlist with the score breakdown on each card, a Pending tab for
applications that are filled but not sent, and a Preferences tab that validates
your YAML before saving (a config that fails validation is rejected, not
written). Discover and Prepare run from the buttons, with live output in
Activity.

To reach it from your phone or another machine, see
[docs/HOSTING.md](docs/HOSTING.md) — a Cloudflare Tunnel is free and keeps the
engine on your machine, which matters more than it sounds. Set
`JOBAUTO_WEB_TOKEN` before exposing it; the page shows your phone number, salary
and full application history.

### Terminal

```bash
python -m jobauto discover        # search every portal, score everything
python -m jobauto shortlist --why # see the ranking and why each job ranked there
python -m jobauto apply --limit 5 # fill 5 applications, prompting before each
```

`apply` opens each job, fills the form, answers the screening questions it can,
and then shows you this:

```
========================================================================
  [2/5]  Senior Backend Developer
  Acme Technologies  |  Noida  |  14-22 LPA
  score 87.4  (naukri)
  https://www.naukri.com/job-listings-...

  why it ranked here:
    - title ~ Backend Developer
    - matches C#, .NET Core, SQL Server, REST API
    - you fit the 3-6y band
    - preferred location: Noida
    - up to 22 LPA meets your ask

  resume: resumes/backend.docx

  auto-filled answers:
    What is your notice period?
      -> 60 days (negotiable)
    What is your expected CTC?
      -> 14 LPA

  NEEDS YOU -- left blank on purpose:
    ? Describe your experience with distributed systems
========================================================================

  [s]ubmit  [k]skip  [o]pen in browser  [q]uit >
```

Quit whenever you like — anything already filled is saved, and
`python -m jobauto review` lists it again later.

## Configuring what it looks for

Everything lives in `config/preferences.local.yaml`. Nothing about roles or
skills is hardcoded.

```yaml
search:
  roles:
    - title: "Backend Developer"
      weight: 1.0                    # bias the score for this role
      aliases: ["Backend Engineer", "API Developer"]

  keywords:
    exclude: ["intern", "bpo"]       # these drop a job outright

  locations:
    preferred: ["Noida", "Remote"]
    blocked: ["Chennai"]

  compensation:
    expected_ctc_lpa: 14.0
    minimum_acceptable_lpa: 11.0     # below this gets heavily down-ranked
```

Scoring weights are yours to tune — they must sum to 1.0 and `doctor` will tell
you if they don't:

```yaml
scoring:
  weights:
    title_match: 0.30
    skill_overlap: 0.25
    experience_fit: 0.15
    location_fit: 0.15
    compensation_fit: 0.10
    company_quality: 0.05
```

If everything is scoring too low, lower `thresholds.shortlist` rather than
inflating weights.

## Adding a portal

Two steps. Copy `config/portals/hirist.yaml`, point the selectors at the new
site, and set `adapter: "jobauto.portals.yourportal:YourPortalAdapter"`. Then:

```python
from .generic import ConfigDrivenAdapter

class YourPortalAdapter(ConfigDrivenAdapter):
    """Standard search/apply shape -- nothing to override."""
```

That is genuinely the whole adapter if the portal uses the ordinary
"search page → result cards → apply button" layout. Searching, pagination,
detail fetching, pacing, daily caps, dedupe and the review gate are all
inherited. Override a method only where the portal actually differs — see
`naukri.py` for a chatbot flow or `linkedin.py` for a multi-step wizard.

## A free win

Naukri ranks profiles by recency, so recruiters see recently-updated profiles
first. `python -m jobauto refresh` re-saves your headline unchanged, which
Naukri counts as an update. Your own account acting on itself — no grey area,
and it measurably lifts inbound.

## Tests

```bash
python -m pytest -q      # 198 tests, no browser or network required
```

## Layout

```
config/
  preferences.yaml       roles, scoring weights, thresholds, caps
  profile.yaml           your details and canned screening answers
  portals/*.yaml         one file per portal: selectors and URLs
src/jobauto/
  scoring.py             preference-driven ranking
  pipeline.py            discover -> score -> prepare -> review -> submit
  review.py              the human gate
  browser.py             persistent login sessions, no stored passwords
  forms.py               screening answers, resume selection
  portals/               one adapter per portal
  web/                   local dashboard (Flask + vanilla JS, no build step)
  cloud/                 hosted app: login, sync API, task queue
  agent/                 local agent that polls the cloud and runs the work
data/jobauto.db          every job seen, every application made (gitignored)
docs/RISKS.md            account-ban tradeoffs; read before enabling auto-submit
docs/HOSTING.md          tunnelling the local dashboard, for free
docs/DEPLOY.md           deploying the cloud app with a permanent URL
Dockerfile, render.yaml  one-click deploy on Render's free tier
```
