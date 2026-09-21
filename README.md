# jobauto

A job application assistant for Indian job portals — **Naukri, LinkedIn, Indeed,
Instahyre and Hirist**.

It searches the portals, scores every listing against your preferences, fills in
the application form, and then **stops and waits for you to press submit**.

Deploy it once and you get a private website with a login you can open from your
phone or any laptop — **including when your PC is switched off**.

---

## Contents

- [What it actually does](#what-it-actually-does)
- [Why it never submits for you](#why-it-never-submits-for-you)
- [Pick your setup](#pick-your-setup)
- [Setup A — just this PC](#setup-a--just-this-pc) *(15 min)*
- [Setup B — website you can use anywhere](#setup-b--website-you-can-use-anywhere) *(30 min)*
- [Daily use](#daily-use)
- [Configuring what it looks for](#configuring-what-it-looks-for)
- [Command reference](#command-reference)
- [Where your data lives](#where-your-data-lives)
- [Troubleshooting](#troubleshooting)
- [Adding a portal](#adding-a-portal)
- [Known limits](#known-limits)

---

## What it actually does

1. **Searches** each portal for every role you configured, using your logged-in
   browser session
2. **Scores** every result 0–100 against your preferences — title, skills,
   experience, location, salary, company — and tells you *why* it scored that way
3. **Filters out** the obvious nos: excluded keywords, blocked companies, stale
   postings, roles wanting far more experience than you have
4. **Fills in** the application on the portal, answering the screening questions
   it can and leaving blank the ones it can't
5. **Stops.** You check it and press submit yourself
6. **Tracks** everything, so you never apply to the same job twice

What it never does: submit an application, store a portal password, or guess at
a screening question.

## Why it never submits for you

**Account safety.** Every one of these portals prohibits automated access in its
terms. LinkedIn bans permanently, and your LinkedIn account *is* your
professional presence — losing it costs far more than this saves. A human
clicking submit is the single thing that makes the traffic look like a person.

**It works better.** Mass auto-apply converts badly: recruiters and ATS filters
spot generic applications. Twenty reviewed applications beat two hundred sprayed
ones, and the review step is where you catch the job that read well in the
listing and badly in the description.

This takes an application from about six minutes down to about twenty seconds of
your attention. That is the win — not removing you from the loop.

Full reasoning, and what changes if you turn it off: [docs/RISKS.md](docs/RISKS.md).

---

## Pick your setup

| | **A — just this PC** | **B — website, usable anywhere** |
|---|---|---|
| Setup time | ~15 min | ~30 min |
| Accounts needed | none | GitHub + Render + Supabase (all free) |
| Use from phone | no | yes |
| Works with PC off | no | browse and queue work; applying resumes when it's on |
| Where you run it | terminal or `localhost` | any browser, with a login |

Both use the same engine. **B includes A** — you can start with A and move up
later without redoing anything.

---

## Setup A — just this PC

### A1. Install Python

Needs **Python 3.10 or newer**. Check:

```powershell
python --version
```

Nothing, or older than 3.10? Install it — tick **"Add python.exe to PATH"**:

```powershell
winget install Python.Python.3.12
```

### A2. Get the code and install it

```powershell
git clone https://github.com/18-VK/Jobauto.git
cd Jobauto
pip install -e .
python -m playwright install chromium
```

`pip install -e .` puts a `jobauto` command on your PATH. The Playwright step
downloads the browser it drives — about 150 MB, once.

### A3. Fill in your details

```powershell
copy config\profile.yaml config\profile.local.yaml
copy config\preferences.yaml config\preferences.local.yaml
```

Edit **`config\profile.local.yaml`** — name, email, phone, current and expected
CTC, notice period, your skills, and the canned answers to screening questions.

Edit **`config\preferences.local.yaml`** — the roles you want, locations, salary
floor, dealbreakers. [Details below](#configuring-what-it-looks-for).

Both `.local.yaml` files are gitignored, so your real details never get committed.

### A4. Add your resumes

Put them in `resumes\`. One is enough:

```
resumes\base.docx        used when nothing else matches
resumes\backend.docx     picked for backend / .NET / API roles
resumes\fullstack.docx   picked for React / Node / MERN roles
```

Which variant gets used is set under `resume:` in your preferences.

### A5. Check it

```powershell
jobauto doctor
```

Confirms your config parses, scoring weights sum to 1.0, and all five portals
load. Fix anything it reports before continuing.

### A6. Sign in to the portals

```powershell
jobauto login
```

A real browser opens, one portal at a time. **Sign in by hand**, OTP included.
The session is saved, so this is once per portal.

> **No password is ever stored by this project.** It keeps browser cookies,
> exactly like your normal browser. That's also why MFA keeps working.

### A7. Use it

```powershell
jobauto web
```

Opens a dashboard at `http://127.0.0.1:5057`. Or stay in the terminal —
see [Daily use](#daily-use).

**Setup A is done.**

---

## Setup B — website you can use anywhere

Two pieces, split by what each is allowed to hold:

```
   ANY DEVICE                 CLOUD (free host)              YOUR PC
   ──────────                 ─────────────────              ───────
   phone, laptop,             permanent URL                  the agent
   office machine     ──▶     email + password       ◀──     the browser
                              jobs, scores                   your portal logins
                              preferences                    your resumes
                              task queue
                              ↑ never holds a portal login
```

Your portal cookies **never leave your PC**. If the cloud database leaked
tomorrow, nobody would gain access to a single job portal. The agent connects
*outbound*, so nothing is exposed on your home network and no ports are opened.

### B1. Database — Supabase (free, permanent)

Render's own free Postgres **expires after 90 days and takes your data with it**.
Supabase's free tier doesn't.

1. [supabase.com](https://supabase.com) → **New project**
2. Save the database password — it's shown once
3. Region: **Mumbai (ap-south-1)** if you're in India
4. Click **Connect** at the top → **Session pooler** tab → copy that string

It must look like this:

```
postgresql://postgres.abcdefgh:PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
```

> **Three strings are offered and two of them will not work.** The **direct**
> one (`db.<ref>.supabase.co`) is IPv6-only and Render cannot reach it at all.
> The username must be `postgres.` **plus your project ref**, and the host must
> end in `pooler.supabase.com`.
>
> Use letters and digits in the password. An `@` or `#` breaks URL parsing.

Check it before going further — the password stays masked, so the output is safe
to share:

```powershell
python scripts\check_db_url.py "<your-string>" --connect
```

### B2. Push the code to GitHub

```powershell
.\push-to-github.ps1
```

One browser sign-in, then it creates a private repo and pushes.

### B3. Deploy on Render

1. [render.com](https://render.com) → **Sign up with GitHub**. No card needed
2. **New → Blueprint** → pick your repo
3. It asks for `DATABASE_URL` — paste the Supabase session-pooler string
4. **Apply**, then wait about 5 minutes

Render shows **your** URL at the top, something like
`https://jobauto-a1b2.onrender.com`. That address is permanent.

> Free instances sleep after ~15 minutes idle and take ~30 seconds to wake. The
> agent retries automatically, so this costs you a slow first page load.

### B4. Create your account

Open your URL → **Create one** (or go to `/signup`). **The first account on a
fresh deployment is the owner**, and signups close behind it, so nobody who
finds the URL can register.

### B5. Link your PC

Open **Devices** in the dashboard and copy the command. On the machine that will
do the applying:

```powershell
irm https://<YOUR-URL>/install.ps1 | iex
```

That needs **nothing but Python 3.10+** — no repo, no clone, no setup. It
creates a private environment under `~\.jobauto`, installs the agent and a
browser, asks for your token, links the machine, and offers to start at login.

Then, on that machine:

```powershell
jobauto login      # sign in to each portal by hand, once
jobauto agent      # start syncing — leave it running
```

The dot in the dashboard header turns green.

**Setup B is done.** Open your URL from anywhere.

### Optional: password reset emails

Without SMTP, reset links go to your Render log (search `JOBAUTO_RESET_LINK`).
For real emails, add these under Render → **Environment** — Gmail works with an
[app password](https://myaccount.google.com/apppasswords):

| Variable | Example |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `you@gmail.com` |
| `SMTP_PASSWORD` | your 16-character app password |

Locked out entirely? Set a password straight against the database:

```powershell
$env:DATABASE_URL = "<your-supabase-url>"
python scripts\reset_password.py --email you@example.com --password "a-new-one"
```

---

## Daily use

### In the dashboard

**Jobs** — everything found, ranked, with the reasons it scored that way. Filter
by score, portal, or how recently it was posted. **Queue to apply** on anything
you like.

**Applications** — split into *Waiting for you* and *Done*. Each pending one
shows what was auto-answered and what was deliberately left blank. Open it,
check it, submit it on the portal, then mark it here.

**Preferences** — quick filters plus the raw YAML. Validated before saving; a
broken config is rejected, not written.

**Devices** — your agent's status, the install command, and database cleanup.

Buttons relabel themselves when your PC is offline (*Search portals* → *Queue a
search*), so it's never ambiguous what will happen.

### In the terminal

```powershell
jobauto discover          # search every portal, score everything
jobauto shortlist --why   # the ranking, and the reasons behind it
jobauto apply --limit 5   # fill 5 applications, prompting before each
```

`apply` shows you this before anything is sent:

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

Quit whenever — anything already filled is saved, and `jobauto review` lists it
again later.

### A free win

Naukri ranks profiles by recency, so recruiters see recently-updated ones first.

```powershell
jobauto refresh
```

Re-saves your headline unchanged, which Naukri counts as an update. Your own
account acting on itself — no grey area, and it measurably lifts inbound.

---

## Configuring what it looks for

Everything lives in `config\preferences.local.yaml`, or the **Preferences** tab
if you deployed. Nothing about roles or skills is hardcoded.

### Roles

```yaml
search:
  roles:
    - title: "Backend Developer"
      weight: 1.0                      # bias the score for this role
      aliases: ["Backend Engineer", "API Developer"]
    - title: ".NET Developer"
      weight: 1.0
      aliases: ["Dotnet Developer", "C# Developer"]
```

Each role becomes a separate search on every portal. `aliases` catch the same
job under a different name.

### Filters

```yaml
  keywords:
    include: ["REST API", "microservices"]    # boost the score
    exclude: ["intern", "bpo", "night shift"] # drop the job outright

  locations:
    preferred: ["Noida", "Delhi NCR", "Remote"]
    acceptable: ["Bangalore", "Pune"]         # counted, but lower
    blocked: ["Chennai"]
    work_mode: ["remote", "hybrid", "onsite"]

  compensation:
    expected_ctc_lpa: 14.0
    minimum_acceptable_lpa: 11.0              # below this is heavily down-ranked

  experience:
    current_years: 3.5

  posting:
    max_age_days: 7                           # only jobs posted in the last week
```

`max_age_days` is used twice: sent to the portal so stale listings are never
fetched, and applied exactly by the scorer.

### Scoring weights

Must sum to 1.0 — `doctor` tells you if they don't:

```yaml
scoring:
  weights:
    title_match: 0.30
    skill_overlap: 0.25
    experience_fit: 0.15
    location_fit: 0.15
    compensation_fit: 0.10
    company_quality: 0.05

thresholds:
  shortlist: 60      # below this, never shown
  priority: 85       # flagged as a top pick
```

Everything scoring too low? Lower `thresholds.shortlist` rather than inflating
weights.

### Screening answers

In `profile.local.yaml`. Answered once, reused everywhere:

```yaml
screening_answers:
  - match: ["notice period", "when can you join"]
    answer: "60 days (negotiable)"
  - match: ["expected ctc", "expected salary"]
    answer: "14 LPA"

never_auto_answer:          # always escalated to you, whatever else matches
  - "aadhaar"
  - "pan number"
  - "bank"
```

Anything unmatched is **left blank and flagged**, never guessed. A wrong
screening answer on record is worse than no application.

### Running it daily on its own

**Preferences → Run automatically.** One run a day: search the portals, then
work through the shortlist in batches.

```
  discover  ->  apply 5  ->  apply 5  ->  apply 5  ->  apply 5
```

```yaml
schedule:
  enabled: true
  time: "09:00"
  timezone: "Asia/Kolkata"
  days: ["mon", "tue", "wed", "thu", "fri"]
  batch_size: 5            # applications per batch
  max_batches: 4           # so up to 20 a day
```

Each batch is queued only once the previous one reports back, and the chain
stops early when a batch produces nothing — the shortlist is empty, or a daily
cap has been reached.

**Applications are still only prepared.** The schedule does the searching and
the form filling; they land under *Waiting for you* for review exactly as a
manual run would. Nothing is ever submitted for you.

The agent's poll is the clock, because free hosting has no cron. So if your PC
is off at the scheduled time, the run starts when it next comes online rather
than being skipped.

### Automatic cleanup

Keeps a free database small. Runs twice a day:

```yaml
retention:
  jobs_days: 7             # jobs you never acted on
  tasks_days: 7            # finished run history
  applications_days: 0     # 0 = keep your history forever
```

Never removed at any age: applications waiting on you, jobs queued to apply,
tasks still running.

---

## Command reference

| Command | What it does |
|---|---|
| `jobauto doctor` | Validate config. No browser. Run this first, always |
| `jobauto login` | Sign in to each portal by hand, once |
| `jobauto discover` | Search and score. Applies to nothing |
| `jobauto shortlist --why` | Ranked list with reasons |
| `jobauto apply --limit 5` | Fill applications, prompting before each |
| `jobauto apply --dry-run` | Show what it would apply to, touch nothing |
| `jobauto review` | Anything prepared but not sent |
| `jobauto refresh` | Daily Naukri profile touch |
| `jobauto stats` | Application history summary |
| `jobauto web` | Local dashboard on `127.0.0.1:5057` |
| `jobauto link --url U --token T` | Link this PC to your deployment |
| `jobauto agent` | Run the sync agent — keep it running |
| `jobauto cloud` | Run the hosted app locally, to try it |
| `jobauto export-session` | Copy your portal logins to another machine |
| `jobauto import-session --file F` | Restore logins exported elsewhere |

Most accept `--portal naukri` (repeatable) and `--headless`.

### Helper scripts

| Script | Purpose |
|---|---|
| `scripts\check_db_url.py "<url>" --connect` | Explain a database URL; password stays masked |
| `scripts\migrate_db.py --from A --to B` | Copy a database between servers |
| `scripts\reset_password.py --email X --password Y` | Set a password directly |
| `.\push-to-github.ps1` | Create the private repo and push |
| `.\start-dashboard.ps1` | Local dashboard + a public tunnel |

---

## Where your data lives

**From a checkout:**

```
config\*.local.yaml     your details and preferences
data\jobauto.db         every job seen, every application made
data\browser\<portal>\  your logged-in sessions, one per portal
resumes\                your resume files
```

**Installed via the one-liner:** the same, under `~\.jobauto\`.

All of it is gitignored. `data\browser\` holds live session cookies — never
commit or share that folder.

---

## Troubleshooting

**`jobauto` isn't recognised** — the install didn't finish, or PATH hasn't
refreshed. Reopen PowerShell, or use `python -m jobauto` from a checkout.

**Port 5000 fails / nothing listens** — Windows reserves it (Hyper-V). Defaults
here avoid it. Check yours:
`netsh interface ipv4 show excludedportrange protocol=tcp`

**`discover` finds nothing** — run `jobauto doctor`. If a portal says
`LoginRequired`, run `jobauto login` again. Portal selectors change; see
[Known limits](#known-limits).

**Dashboard says "no PC linked"** — the agent isn't running. Start
`jobauto agent` on that machine.

**`agent token rejected`** — it was rotated. Re-run `jobauto link` with the new one.

**Render deploy exits with status 3, no traceback** — the database is
unreachable and the worker was killed on boot timeout. Almost always the
IPv6-only direct Supabase host. Current builds print a `DATABASE UNREACHABLE`
block naming the cause.

**`password authentication failed for user "postgres"`** — the pooler needs
`postgres.<project-ref>`, not bare `postgres`.

**`failed to resolve host '<text>@aws-0-...'`** — your password contains an `@`.
Percent-encode it as `%40`, or reset it to letters and digits.

More: [docs/DATABASE.md](docs/DATABASE.md) and [docs/DEPLOY.md](docs/DEPLOY.md).

---

## Adding a portal

Copy `config\portals\hirist.yaml`, point the selectors at the new site, set
`adapter: "jobauto.portals.yourportal:YourPortalAdapter"`, then:

```python
from .generic import ConfigDrivenAdapter

class YourPortalAdapter(ConfigDrivenAdapter):
    """Standard search/apply shape -- nothing to override."""
```

That's the whole adapter for any portal with the ordinary "search page → result
cards → apply button" layout. Search, pagination, detail fetching, pacing, daily
caps, dedupe and the review gate are all inherited.

Every CSS selector lives in YAML, so a portal redesign is a config edit rather
than a code change. Override a method only where a portal genuinely differs —
see `naukri.py` for a chatbot flow, `linkedin.py` for a multi-step wizard.

---

## Known limits

- **Portal selectors are best-effort and unverified against the live sites.**
  Expect to fix some on first run. They're all in YAML, so it's a one-line edit.
  If `login` can't confirm a sign-in you actually completed, the
  `auth.logged_in_selector` for that portal is stale.
- **Resume tailoring is configured but not implemented.** `pick_resume` selects
  a pre-written variant by keyword; it does not rewrite anything.
- **The daily digest is configured but not implemented.**
- **Instahyre is a feed, not a search**, so role queries don't apply to it —
  your preferences still filter what it returns.

---

## Tests

```powershell
python -m pytest -q      # 370 tests, no browser or network required
```

Covers scoring and parsing, config validation, screening answers, the full
discover→score→dedupe→shortlist flow, the local and cloud APIs, the agent sync
protocol including cross-account isolation, database migration, retention, and
the installer.

## Layout

```
config/
  preferences.yaml       roles, scoring weights, thresholds, caps, retention
  profile.yaml           your details and canned screening answers
  portals/*.yaml         one file per portal: selectors and URLs
src/jobauto/
  scoring.py             preference-driven ranking
  pipeline.py            discover -> score -> prepare -> review -> submit
  review.py              the human gate
  browser.py             persistent logins, no stored passwords
  forms.py               screening answers, resume selection
  portals/               one adapter per portal
  web/                   local dashboard
  cloud/                 hosted app: login, sync API, task queue, installer
  agent/                 local agent: polls the cloud, runs the work
docs/NEW-MACHINE.md      setting up a machine to do the work
docs/SCHEDULING.md       how the daily schedule works, and why
docs/DEPLOY.md           deploying with a permanent URL
docs/DATABASE.md         Supabase migration and connection strings
docs/RISKS.md            account-ban tradeoffs
docs/HOSTING.md          tunnelling the local dashboard instead
```
