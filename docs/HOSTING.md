# Hosting the dashboard

Short version: **the dashboard can be reachable from anywhere for free, but the
automation engine has to stay on your machine.** Use a Cloudflare Tunnel.

## Quick start

```powershell
.\start-dashboard.ps1
```

That generates an access token (once, reused after that), starts the dashboard,
opens a free Cloudflare Tunnel and prints a public HTTPS URL. Ctrl-C stops both.

```powershell
.\start-dashboard.ps1 -LocalOnly      # no public URL
.\start-dashboard.ps1 -Port 5080      # if the default port is taken
.\start-dashboard.ps1 -NewToken       # rotate the token
```

**A note on ports.** Windows reserves blocks of TCP ports for Hyper-V and WinNAT,
and **5000 is very commonly one of them** — Flask appears to start, logs
`An attempt was made to access a socket in a way forbidden by its access
permissions`, and nothing listens. That is why the default here is 5057. To see
the reserved ranges on your machine:

```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
```

## Why not Vercel, Netlify or Render

Vercel is the one people ask about most, so specifically: **the dashboard would
deploy, the engine would not.** Vercel runs serverless functions with an
ephemeral filesystem and a 10–60 second execution limit, and no way to run a real
Chromium window.

- The browser profiles under `data/browser/` *are* the login mechanism. An
  ephemeral filesystem throws them away on every cold start.
- An application takes minutes, not seconds. Function timeouts kill it mid-form.
- Playwright headed Chromium needs a display server that serverless containers
  do not have.

Netlify and Cloudflare Pages have the same shape of problem. Render and Railway
can at least run a long-lived process, but the objections below still apply.

## Why not a free container host either

Render, Railway and Fly can run a persistent process, so they clear the first
hurdle. They still fail on the rest:

**Your session cookies would have to go with it.** The engine drives a browser
using your logged-in Naukri and LinkedIn sessions. Deploying it means copying
those cookies onto someone else's server. Anyone with access to that host — or
to a leaked build log, or a misconfigured env var — has your accounts, no
password needed.

**Datacenter IPs get flagged.** Your Naukri profile says Noida. Applications
suddenly arriving from an AWS range in Oregon, at 3am IST, is close to a textbook
bot signature. This is the single fastest way to get an account restricted.

**Free tiers sleep and have no persistent disk.** The browser profile directories
under `data/browser/` are the whole login mechanism. An ephemeral filesystem
means you re-authenticate every cold start, which defeats the point. Sleeping
instances also kill a browser session mid-application.

**Headed Chromium needs a display.** Headless is significantly more detectable,
and free containers have no display server. You would be forced into the more
detectable mode on the more hostile IP.

## What to do instead: Cloudflare Tunnel

The app keeps running on your machine. Cloudflare gives you a public HTTPS URL
that forwards to it. Free, no credit card, no ports opened on your router.

**1. Set a token first.** The dashboard shows your phone number, salary
expectations and full application history. Do not skip this.

```bash
# PowerShell
$env:JOBAUTO_WEB_TOKEN = -join ((48..57) + (97..122) | Get-Random -Count 32 | % {[char]$_})
echo $env:JOBAUTO_WEB_TOKEN     # save this somewhere
```

**2. Start the dashboard.**

```bash
python -m jobauto web
```

**3. Install cloudflared and open the tunnel.**

```bash
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:5000
```

It prints a URL like `https://random-words-here.trycloudflare.com`. Open it with
your token appended:

```
https://random-words-here.trycloudflare.com/?token=YOUR_TOKEN
```

The quick-tunnel URL changes each restart. For a stable address, put a domain on
Cloudflare (free plan) and run a named tunnel:

```bash
cloudflared tunnel login
cloudflared tunnel create jobauto
cloudflared tunnel route dns jobauto jobs.yourdomain.com
cloudflared tunnel run --url http://127.0.0.1:5000 jobauto
```

## Alternative: Tailscale

If you only want access from your own devices — your phone, your laptop
elsewhere — Tailscale is simpler and never exposes anything publicly.

```bash
winget install tailscale.tailscale
tailscale up
python -m jobauto web --host 0.0.0.0
```

Then browse to `http://<your-pc-name>:5000` from any device on your tailnet.
Free for personal use, up to 100 devices. Still set the token — a shared tailnet
is not the same as nobody.

## If you host the UI anyway

There is one split that does work, if you want it later: host the **dashboard
and database** in the cloud, keep the **browser engine** local, and have the
local engine poll the cloud API for work. The cloud side never holds a session
cookie and never touches a portal.

That is a real piece of work — an auth layer, a job queue, a local agent process
and a sync protocol — and it buys you very little over a tunnel. Worth it only if
several people share one instance.

## Whatever you do

- Never bind `--host 0.0.0.0` without `JOBAUTO_WEB_TOKEN` set. The app warns you,
  but it will not stop you.
- Never commit `data/` — browser profiles contain live session cookies. It is
  gitignored; keep it that way.
- Token auth is a single shared secret, appropriate for one person behind a
  tunnel. If more than one person needs access, that is the point to build real
  authentication rather than stretching this.
