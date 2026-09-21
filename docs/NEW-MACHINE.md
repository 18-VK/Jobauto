# Setting up a machine to do the work

Written for: whoever is sitting at the PC that will run the searches — you, or
someone you have given an account to.

The dashboard runs in the cloud and you can open it anywhere. But the searching
and form filling need a real browser driving your real logged-in sessions, so
one machine has to do that. This is how you set that machine up.

**It needs nothing installed beforehand except Python 3.10 or newer.**

---

## What you actually do

### 1. Get your agent token

On any device, open your jobauto site, sign in, and go to **Devices**. Press
**Copy token**. Keep it on the clipboard — you paste it in a moment.

### 2. Run one command on the new machine

Open **PowerShell** (no admin needed) and run:

```powershell
irm https://jobauto-wrd0.onrender.com/install.ps1 | iex
```

Then answer three prompts. That is the whole setup.

| Prompt | Answer | What happens |
|---|---|---|
| `token` | paste it | Checks it against the server before going further |
| `Start the agent automatically when you log in?` | `Y` | Registers a scheduled task, or a Startup shortcut if the machine won't allow tasks |
| `Sign in to the job portals now?` | `Y` | Opens a real browser per portal — **you sign in by hand**, OTP included |
| `Start the agent now?` | `Y` | Starts syncing, and leaves the output on screen |

### 3. Turn on the daily schedule

Back in the dashboard: **Preferences → Run automatically**.

Pick a time, pick your days, press **Save schedule**. From then on it runs by
itself:

```
  discover  ->  apply 5  ->  apply 5  ->  apply 5  ->  apply 5
```

**That's it.** Nothing else to schedule, nothing else to open.

---

## What the script does, in order

1. **Finds Python** 3.10+, and tells you how to install it if there is none
2. **Creates a private environment** at `~\.jobauto` — nothing else on the
   machine is touched, and deleting that folder removes everything
3. **Downloads the agent** from your own site, not from PyPI or GitHub
4. **Installs Playwright and a browser** (~150 MB, once)
5. **Verifies your token** against the API, so a bad one gives a plain sentence
   rather than a stack trace
6. **Links the machine**, saving the URL and token to `~\.jobauto\data\agent.json`
7. **Sets up autostart**, preferring a scheduled task:
   - starts a minute after you log in, so the network is up first
   - restarts up to 5 times if it crashes
   - never times out, and keeps running on battery
   - falls back to a Startup-folder shortcut where tasks need admin
8. **Signs you in to the portals**, one real browser at a time
9. **Starts the agent**

---

## Things people expect to need, and don't

**Re-linking.** Once. `agent.json` survives reboots.

**Opening the dashboard on that machine.** The agent talks to the API directly.
The dashboard is for you to look at, from wherever you are.

**A Task Scheduler entry for discover or apply.** The schedule lives in your
preferences and the agent acts on it. One moving part, not three.

**Leaving the PC on.** If it is off at the scheduled time, the run starts when
it next comes online rather than being skipped.

---

## Checking it works

In the dashboard:

- The dot by the logo turns **green** — "PC online"
- **Devices → Live output** streams what the agent is doing while it runs
- **Devices → Recent activity** lists finished runs; click one to see its log

On the machine:

```powershell
$j = "$env:USERPROFILE\.jobauto\venv\Scripts\jobauto.exe"

& $j doctor      # config and portals load
& $j stats       # what has been applied to
& $j agent       # run it in the foreground to watch it
```

---

## If something is wrong

**Dot stays grey, "no PC linked"** — the agent is not running. Start it by hand
with `& $j agent` and read what it says.

**`token rejected`** — it was rotated since you copied it. Devices → Copy token,
then `& $j link --url=<your-url> --token=<new>`.

**Searches find nothing** — the portal sign-ins did not happen or have expired.
Run `& $j login` again.

**`exitCode=1260` when a browser opens** — that machine's security policy is
killing the browser. Common on managed work laptops. Use a personal machine;
see the explanation the command prints.

**The scheduled task would not register** — it needs admin on some machines.
The installer falls back to a Startup shortcut automatically, which works the
same for this purpose. Check it exists:

```powershell
dir "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\jobauto-agent.lnk"
```

---

## Removing it

```powershell
Unregister-ScheduledTask jobauto-agent -Confirm:$false   # if it registered
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\jobauto-agent.lnk" -EA 0
Remove-Item "$env:USERPROFILE\.jobauto" -Recurse -Force
```

That removes the agent, its browser profiles and its local history. Your cloud
account and everything in it is untouched — rotate the agent token in **Devices**
if the machine is no longer yours.
