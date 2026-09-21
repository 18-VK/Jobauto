# Moving the database to Supabase

**Why bother:** Render's free Postgres **expires 90 days after you create it**
and the data goes with it. Supabase's free tier does not expire. If you deployed
on Render's managed database, you are on a clock.

Supabase free gives you 500 MB, which is enormous for this — a few thousand job
rows is well under a megabyte.

> **One Supabase quirk:** free projects pause after 7 days with no connections.
> The agent polls every 30 seconds, so as long as it runs occasionally you will
> never hit that. A paused project is restored from the dashboard with no data
> loss.

---

## 1. Create the Supabase project

1. [supabase.com](https://supabase.com) → sign up → **New project**
2. Pick a strong database password and **save it** — it appears once
3. Region: **Mumbai (ap-south-1)** if you are in India
4. Wait ~2 minutes for provisioning

## 2. Get the right connection string

This is the step that catches people out. Supabase offers three, and **two of
them will not work on Render**.

Click **Connect** at the top of the dashboard (next to the project name), then
choose the **Session pooler** tab. Older Supabase builds put this under
**Project Settings → Database → Connection string** instead.

```
postgresql://postgres.abcdefgh:YOUR-PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
```

| Mode | Host / port | Use it? |
|---|---|---|
| **Session pooler** | `pooler.supabase.com:5432` | **Yes.** IPv4, behaves like normal Postgres |
| Transaction pooler | `pooler.supabase.com:6543` | Works, but pgBouncer disables prepared statements — the app detects this and adapts, but session mode is simpler |
| Direct | `db.<ref>.supabase.co:5432` | **No.** IPv6-only on new projects, and Render has no IPv6 egress. It will just time out |

Replace `YOUR-PASSWORD` with the password from step 1. If it contains `@`, `/`
or `#`, percent-encode those (`@` → `%40`) or reset it to something
alphanumeric.

### Telling the three apart

The **direct** string is the one Supabase shows most prominently, and it is the
wrong one. Compare:

```
direct   postgresql://postgres:PASS@db.<ref>.supabase.co:5432/postgres
                      ^^^^^^^^        ^^^^^^^^^^^^^^^^^^^^^^^^
session  postgresql://postgres.<ref>:PASS@aws-0-<region>.pooler.supabase.com:5432/postgres
                      ^^^^^^^^^^^^^^      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

Two tells: the pooler username is `postgres.` **plus your project ref**, and the
host ends in `pooler.supabase.com`. If your string says `db.<ref>.supabase.co`,
it is the direct one and Render cannot reach it.

### Building it by hand

You do not need the UI at all — the format is fixed:

```
postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
```

- **PROJECT_REF** — the id in your dashboard URL,
  `supabase.com/dashboard/project/`**`<this part>`**
- **REGION** — what you chose at creation; Mumbai is `ap-south-1`. Also under
  **Settings → General → Region**
- **PASSWORD** — set at creation; resettable under **Settings → Database**

### Passwords with symbols in them

A URL gives `@ # / : [ ]` structural meaning, so a password containing any of
them silently changes what the URL means:

| Password contains | Symptom | Fix |
|---|---|---|
| `@` | `failed to resolve host 'sometext@aws-0-...'` — the text after your `@` became the hostname | write `%40` |
| `#` | authentication fails against a password that looks right — everything after `#` was dropped as a URL fragment | write `%23` |
| `/` | the database name is wrong or missing | write `%2F` |
| `[` `]` | `does not appear to be an IPv4 or IPv6 address` — usually the `[YOUR-PASSWORD]` placeholder left in | replace it with the real password |

Simplest answer is to avoid the problem: **Settings → Database → Reset database
password**, letters and digits only.

### Check it before going further

```powershell
python scripts/check_db_url.py "<your-string>"
python scripts/check_db_url.py "<your-string>" --connect
```

Prints every component with the password masked, identifies which Supabase mode
the string is, and names anything that will fail. **It never prints the
password**, so its output is safe to paste into a chat or an issue.

Then a read-only test of the whole path:

```powershell
python scripts/migrate_db.py --from "<your-string>" --to "sqlite:///throwaway.db" --dry-run
```

A table of zeroes means the string works on a fresh project. A wrong string
fails within ten seconds with a named error rather than hanging.

## 3. Copy your data across

Get your current Render database URL from the Render dashboard → your database →
**External Database URL**.

Check what would move, touching nothing:

```powershell
cd D:\Data\Claude
$env:PYTHONPATH="src"
python scripts/migrate_db.py --from "<render-url>" --to "<supabase-url>" --dry-run
```

It prints a row count per table for both sides. If the source looks right, run
it for real by dropping `--dry-run`:

```powershell
python scripts/migrate_db.py --from "<render-url>" --to "<supabase-url>"
```

The script creates any missing tables, copies in foreign-key order, fixes the id
sequences afterwards, and verifies the row counts match. **It is safe to re-run**
— rows already present are skipped, so an interrupted migration just needs the
same command again.

## 4. Point the app at Supabase

Render → your web service → **Environment** → set `DATABASE_URL` to the Supabase
session-pooler string → **Save**. Render redeploys automatically.

## 5. Check before you delete anything

Sign in to your site. Your account, jobs and applications should all be there,
**with the same password** — the hash moves as-is, so nothing needs resetting.
Your agent token also survives, so your PC keeps syncing without re-linking.

Only once you have confirmed that: Render → your database → **Delete**. Keep it
around a few days if you want a safety net; a free database costs nothing until
it expires.

---

## Verifying from the command line

```powershell
python scripts/migrate_db.py --from "<supabase-url>" --to "sqlite:///check.db" --dry-run
```

A dry run against the new database prints its row counts without writing
anything — a quick way to confirm the data really landed.

## Fresh start instead of migrating

If you have nothing worth keeping, skip the script entirely: set `DATABASE_URL`
to the Supabase string and redeploy. The app creates its own tables on boot, and
the first visit to `/signup` makes you the owner again.

## Troubleshooting

**`connection timed out` / `network unreachable`** — you used the direct
`db.<ref>.supabase.co` host. Switch to the session pooler string.

**`password authentication failed for user "postgres"`** — read the username in
that message. The pooler needs `postgres.<project-ref>`; a bare `postgres` is
what the *direct* string uses, so the host was changed and the username left
behind.

**`failed to resolve host '<something>@aws-0-...'`** — your password contains an
`@`. The URL split there, so the rest became the hostname. Percent-encode it as
`%40` or reset the password.

**`password authentication failed`** with a username that looks right — a
special character in the password is being parsed as URL structure. Run
`scripts/check_db_url.py` on the string; it names the offending character.

**`prepared statement "_pg3_0" already exists`** — the transaction pooler
(port 6543) with prepared statements on. The app detects `:6543` and disables
them; if you see this, `DATABASE_URL` probably reached the app from somewhere
that bypassed `database_url()`. Use the session pooler on 5432.

**`duplicate key value violates unique constraint "users_email_key"`** — the
target already holds that account. Either you already migrated, or you signed up
on the new database before copying. Use `--wipe-target` to clear it first, but
be sure that is what you want.

**Everything migrated but the site shows no data** — `DATABASE_URL` on Render
was not actually updated, or the deploy did not restart. Check the Render logs
for the boot line and confirm the variable in the Environment tab.

## Automatic cleanup

The database prunes itself so a free tier stays comfortable. Cleanup runs at
most twice a day, triggered by the agent's poll — free tiers have no cron, and
a sleeping instance runs no background threads, so hanging it off the poll is
what actually works.

Windows live in the `retention:` block of your preferences, and `0` on any of
them means keep forever:

```yaml
retention:
  enabled: true
  jobs_days: 7            # discovered jobs you never acted on
  applied_jobs_days: 7    # job rows for applications already recorded
  tasks_days: 7           # finished run history and its logs
  applications_days: 0    # your application history -- kept by default
  password_resets_days: 1
```

**Three things are never removed at any age**, because losing them costs work
rather than saving space:

- applications still `prepared` — filled in and waiting for *you* to submit
- jobs still queued to apply
- tasks still queued or running

Deleting an applied job row is safe because the application row carries the
title, company and url forward, so the history survives.

**Devices → Database cleanup** shows what the next run will remove, what is
being protected, and a **Clean up now** button. On a realistic database — 147
jobs, 41 tasks — cleanup took it from 278 KB to 94 KB.

> **On Postgres**, deleted rows are reclaimed by autovacuum rather than handed
> straight back to the operating system, so the reported database size may lag
> behind. Supabase runs autovacuum for you; nothing to do.

## Other hosts

The same script works for any pair of SQLAlchemy URLs. Neon, Railway Postgres
and Fly Postgres all behave like Supabase's session pooler — take their
connection string and pass it as `--to`. To pull production down for local
inspection:

```powershell
python scripts/migrate_db.py --from "<cloud-url>" --to "sqlite:///local-copy.db"
```
