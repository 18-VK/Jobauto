# Risks

Read this before changing `application.auto_submit`.

## Terms of service

Naukri, LinkedIn, Indeed, Instahyre and Hirist all prohibit automated access in
their terms. Using your own account with your own data is not a legal problem —
it is a **contractual** one, and the enforcement mechanism is account
termination, not a lawsuit.

Tolerance varies a lot:

| Portal | Detection | What happens when caught |
|---|---|---|
| LinkedIn | Aggressive, sophisticated | Permanent ban, appeals rarely succeed |
| Indeed | Cloudflare + behavioural | Temporary blocks, captcha walls |
| Naukri | Moderate | Rate limiting, occasional soft blocks |
| Instahyre | Light | Rarely enforced |
| Hirist | Light | Rarely enforced |

LinkedIn is the one that genuinely matters. For most people, their LinkedIn
account *is* their professional presence — years of connections, recommendations
and recruiter history. Losing it costs far more than this tool saves. That is
why [linkedin.yaml](../config/portals/linkedin.yaml) sets
`risk.force_manual_submit: true`, and why that flag is enforced inside
`PortalAdapter.submit` where no CLI flag or config edit can route around it.

## The effectiveness argument

Separate from bans, mass auto-apply simply works badly:

- Recruiters recognise generic applications, and volume applicants get pattern-
  matched and deprioritised.
- ATS systems score keyword-matched resumes; an untailored one ranks poorly no
  matter how many you send.
- Screening questions answered wrongly by a bot are worse than not applying —
  a bad answer on record is harder to recover from than silence.

Twenty reviewed applications beat two hundred sprayed ones. The review step is
also where you catch the job that read well in the listing and badly in the JD.

## What this tool does about it

1. **`auto_submit: false` by default.** Every application stops for you.
2. **`force_manual_submit` per portal.** LinkedIn can never auto-submit,
   whatever the global setting says.
3. **Escalated questions block auto-submit.** Anything the answerer could not
   confidently fill is left blank and flagged, and that application will not be
   sent automatically even with `auto_submit: true`.
4. **Sensitive fields are never auto-filled.** Aadhaar, PAN, DOB, bank details
   — see `never_auto_answer` in `profile.yaml`.
5. **Daily caps per portal**, defaulting well below what would look unusual.
6. **Active hours.** Applications at 3am are a strong automation signal.
7. **Randomised human-scale pacing**, multiplied further on risky portals.
8. **Stop on challenge.** A captcha aborts that portal for the whole run.
   Retrying into a bot check is how accounts get banned.

## If you turn auto_submit on anyway

It is your account and your call. If you do:

- Leave LinkedIn on manual. It is not worth it.
- Lower the daily caps rather than raising them.
- Raise `thresholds.shortlist` so only strong matches go out unattended.
- Watch the first few runs. A selector drift can mean you are submitting forms
  with fields you did not intend to leave blank.
- Check `python -m jobauto stats` regularly against what the portals actually
  show as applied. A divergence means something is silently failing.

## What this tool deliberately does not do

- **No CAPTCHA solving.** Not integrated, and should not be. A captcha means the
  portal has decided you look automated; the correct response is to stop.
- **No proxy or IP rotation.** Applying from an IP that does not match your
  stated location is a stronger signal than the traffic pattern it hides.
- **No stored passwords.** Persistent browser profiles only. MFA keeps working
  because you do the login yourself.
- **No headless-by-default.** Headless Chromium is considerably more detectable.

These are not missing features. Adding them would convert a tool that saves you
time into one that is actively trying to defeat detection — which changes both
the risk profile and what the thing actually is.

## What happens after a bot check

A challenge is treated as a signal, not an obstacle. When a portal serves one,
that portal is parked in the `portal_state` table and skipped entirely — no
browser is even opened for it — until the cool-off expires:

| Bot checks in a row | Left alone for |
|---|---|
| 1st | 6 hours |
| 2nd | 24 hours |
| 3rd and after | 72 hours |

The gap widens because repeatedly failing challenges is itself what escalates a
soft check into a hard block. A clean run clears the record, so an unrelated
check months later starts from the shortest gap again.

This matters most on a schedule. Stopping a portal "for this run" is no
protection when the next run is tomorrow at 08:00 — it walks straight back into
the same challenge, every day, which is exactly the pattern that gets an account
restricted.

```bash
python -m jobauto doctor                          # shows what is parked, and until when
python -m jobauto doctor --clear-cooldown indeed  # try it again now, at your own risk
```

To stop searching a portal altogether, set `enabled: false` in
`config/portals/<portal>.yaml`. For a portal that challenges every single time,
that is the honest answer — the tool has no way to satisfy it and should not
acquire one.

### Why the browser no longer pins a user agent

Playwright's `user_agent` option rewrites `navigator.userAgent` and the
`User-Agent` header, but **not** the `sec-ch-ua` client hints, which keep
reporting the browser's real version. A pinned string therefore claims one
Chrome version in one header and a different one in the next — a contradiction
no genuine browser produces, and a cheap one to check for. The browser now
introduces itself, and a real installed Chrome or Edge is preferred over
Playwright's bundled Chromium, which is a stripped build with a distinctive
codec set. Set `JOBAUTO_USER_AGENT` to override.

Note this is about not looking broken, not about looking human. It does not
make a determined bot check pass, and is not meant to.
