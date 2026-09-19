# Deploy your own jobauto

You end up with a permanent URL, an email-and-password login, and a dashboard
that works whether or not your PC is on. Free, no card.

> **Nothing is deployed until you do step 2.** Any `onrender.com` address in
> this guide is a made-up placeholder — Render generates your real one during
> deployment and it will not exist before then. To see the app working first,
> run `python -m jobauto cloud` and open <http://127.0.0.1:5058>.

Two pieces:

| | Where it runs | What it holds |
|---|---|---|
| **Cloud app** | Render (free) | jobs, scores, applications, preferences |
| **Agent** | Your PC | portal cookies, browser profiles, resumes |

The agent connects **outbound** to the cloud, so nothing is exposed on your home
network. Your portal logins never leave your machine — if the cloud database
leaked tomorrow, nobody would gain access to a single job portal.

## The short version

```powershell
.\push-to-github.ps1      # signs in, creates the private repo, pushes
```

Then open <https://render.com> → sign up with GitHub → **New → Blueprint** →
pick the repo → **Apply**. About ten minutes total, most of it waiting for the
build.

The rest of this page is the same thing with explanations.

---

## 1. Put the code on GitHub

```bash
cd D:\Data\Claude
git add -A
git commit -m "jobauto"
gh repo create jobauto --private --source=. --push
```

No `gh`? Create an empty private repo on github.com, then:

```bash
git remote add origin https://github.com/YOUR_NAME/jobauto.git
git push -u origin main
```

Private is fine — Render reads private repos once you connect your GitHub account.

## 2. Deploy on Render

1. Sign up at [render.com](https://render.com) with GitHub. No card.
2. **New → Blueprint**, pick your `jobauto` repo.
3. Render reads [render.yaml](../render.yaml) and creates a web service plus a
   free Postgres database. Click **Apply**.
4. Wait ~5 minutes for the first build.

Render then shows **your** URL at the top of the service page — something like
`https://jobauto-a1b2.onrender.com`, with a suffix Render picks. That URL is
permanent. Use it everywhere below in place of `<YOUR-URL>`.

> **Free tier naps.** Render free instances sleep after ~15 minutes of no
> traffic and take ~30 seconds to wake. The agent retries with backoff, so a
> sleeping instance costs you nothing but a slow first page load.

## 3. Create your account

Open your URL. The first visit offers signup, and **the first account is the
owner**. After that, signups are closed (`JOBAUTO_ALLOW_SIGNUP=0` in
`render.yaml`), so nobody who finds the URL can create an account.

To let someone else in later, set `JOBAUTO_ALLOW_SIGNUP=1` and
`JOBAUTO_SIGNUP_CODE=some-code` in Render → Environment.

## 4. Link your PC

Open **Devices** in the dashboard and copy the two commands. On your PC:

```powershell
cd D:\Data\Claude
$env:PYTHONPATH="src"
python -m jobauto link --url <YOUR-URL> --token <YOUR-TOKEN>
python -m jobauto agent
```

`link` saves the URL and token to `data/agent.json` (gitignored) and verifies
the connection. `agent` starts polling. The dot in the dashboard header turns
green.

Leave that window open while you want applications to run. Closing it doesn't
break anything — queued work simply waits.

### Start the agent automatically at login

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
             -Argument "-m jobauto agent" -WorkingDirectory "D:\Data\Claude"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "jobauto-agent" -Action $action `
    -Trigger $trigger -Description "jobauto cloud sync agent"
```

You'll want `PYTHONPATH=src` set as a user environment variable for this, or
install the package with `pip install -e .`.

## 5. Sign in to the job portals

Once per portal, on your PC:

```powershell
python -m jobauto login
```

A real browser opens. Sign in by hand, OTP included. Cookies persist in
`data/browser/`. **This never happens in the cloud and no password is ever
stored.**

---

## Using it

### With your PC on

Everything is live. **Search portals** runs discovery within a minute and the
results appear in the dashboard. **Queue to apply** on a job, and the agent
fills that application on your PC and pushes it back as *prepared*.

### With your PC off

The bit you asked for:

- Browse every job found so far, with scores and reasons
- **Queue to apply** on anything that looks good
- Edit preferences — roles, salary floor, locations, weights
- Review and mark applications you've already submitted

Nothing is lost. The moment the agent reconnects it pulls your preferences,
drains the queue, and pushes results back.

### The one thing that still needs you

Applications are filled but **never submitted automatically**. They arrive in
the dashboard as *prepared*, showing what was auto-answered and what was
deliberately left blank. You open the link, check it, submit it, and mark it
done. See [RISKS.md](RISKS.md) for why this matters more than it sounds.

---

## Other hosts

**Fly.io** — [fly.toml](../fly.toml) is included, region set to Mumbai:

```bash
fly launch --no-deploy
fly postgres create
fly postgres attach <db-name>
fly secrets set SECRET_KEY=$(openssl rand -hex 32) JOBAUTO_ALLOW_SIGNUP=0
fly deploy
```

Fly machines suspend rather than cold-start, so wake-ups are faster than Render.

**Railway / Koyeb / any Docker host** — the [Dockerfile](../Dockerfile) is
standard. Set `DATABASE_URL`, `SECRET_KEY`, `JOBAUTO_ALLOW_SIGNUP=0`.

**Local trial first**, before deploying anywhere:

```powershell
python -m jobauto cloud       # http://127.0.0.1:5058, SQLite, signup open
```

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres URL. Absent → SQLite at `cloud.db` |
| `SECRET_KEY` | Signs session cookies. **Rotating it logs everyone out** |
| `JOBAUTO_ALLOW_SIGNUP` | `1` opens signup beyond the first account |
| `JOBAUTO_SIGNUP_CODE` | Invite code required when signup is open |
| `JOBAUTO_HTTPS` | `1` (default) marks cookies HTTPS-only |

## Security notes

- Passwords are hashed with scrypt. The app never stores or sees a portal password.
- The agent token is a separate credential from your login, so it can be rotated
  on its own — **Devices → Rotate token**, which instantly kills the old one.
- Every API route is scoped to the signed-in user; tests cover cross-account
  access on jobs, tasks and applications.
- Set `SECRET_KEY` explicitly in production. Without it the app generates a
  random key at boot, which logs everyone out on every restart.

## If something breaks

**Dashboard says "no PC linked"** — the agent isn't running, or `link` was never
run. Check the agent window for errors.

**"agent token rejected"** — the token was rotated. Re-run `link` with the new one.

**Agent can't reach the cloud** — expected while a free instance is asleep. It
backs off and retries up to 10 minutes. If it persists, check the Render logs.

**Jobs aren't appearing** — run `python -m jobauto doctor` on the PC. Portal
selectors may need updating; see "Known limits" in [CLAUDE.md](../CLAUDE.md).
