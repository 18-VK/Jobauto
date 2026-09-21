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

**Project Settings → Database → Connection string**, then pick **Session
pooler**:

```
postgresql://postgres.abcdefgh:YOUR-PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
```

| Mode | Host / port | Use it? |
|---|---|---|
| **Session pooler** | `pooler.supabase.com:5432` | **Yes.** IPv4, behaves like normal Postgres |
| Transaction pooler | `pooler.supabase.com:6543` | Works, but pgBouncer disables prepared statements — the app detects this and adapts, but session mode is simpler |
| Direct | `db.<ref>.supabase.co:5432` | **No.** IPv6-only on new projects, and Render has no IPv6 egress. It will just time out |

Replace `YOUR-PASSWORD` with the password from step 1. If it contains `@`, `/`
or `#`, percent-encode those (`@` → `%40`).

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

**`password authentication failed`** — a special character in the password is
being parsed as part of the URL. Percent-encode it, or reset to an
alphanumeric password in Supabase → Settings → Database.

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

## Other hosts

The same script works for any pair of SQLAlchemy URLs. Neon, Railway Postgres
and Fly Postgres all behave like Supabase's session pooler — take their
connection string and pass it as `--to`. To pull production down for local
inspection:

```powershell
python scripts/migrate_db.py --from "<cloud-url>" --to "sqlite:///local-copy.db"
```
