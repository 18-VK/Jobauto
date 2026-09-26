"""Wipe a jobauto cloud database for a fresh start.

    python scripts/reset_cloud.py                       # dry run: counts only
    python scripts/reset_cloud.py --yes                 # jobs, applications, tasks
    python scripts/reset_cloud.py --yes --reset-preferences
    python scripts/reset_cloud.py --yes --everything    # accounts and links too

Reads DATABASE_URL (the Supabase/Render string), or JOBAUTO_CLOUD_DB for a
local SQLite file, or --url. Nothing is deleted without --yes.

The default keeps your account, your agent token (so the PC stays linked) and
your preferences, and removes the data the runs produced: jobs, applications,
tasks. --everything removes the accounts and agents as well, after which the
site's signup is open again and every PC has to be linked afresh.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import delete, func, select, update  # noqa: E402

from jobauto.cloud import db as clouddb  # noqa: E402
from jobauto.cloud.db import (Agent, Application, CloudJob, PasswordReset,  # noqa: E402
                              Task, User)

# Children before parents: nothing here relies on the database cascading.
RUN_DATA = (("tasks", Task), ("applications", Application), ("jobs", CloudJob),
            ("password resets", PasswordReset))
ACCOUNTS = (("agents", Agent), ("users", User))


def counts(s) -> dict[str, int]:
    out = {}
    for name, model in RUN_DATA + ACCOUNTS:
        out[name] = s.scalar(select(func.count()).select_from(model)) or 0
    return out


def reset(url: str, *, everything: bool = False, reset_preferences: bool = False,
          yes: bool = False, say=print) -> dict[str, int]:
    """Returns the row counts as they were before anything was removed."""
    clouddb.reset_engine()
    clouddb.init_engine(url)
    with clouddb.session() as s:
        before = counts(s)
        say(f"  database: {_redact(url)}")
        for name, n in before.items():
            say(f"    {name:<18}{n}")
        if not yes:
            say("\n  dry run -- nothing removed. Add --yes to do it.")
            return before

        tables = RUN_DATA + (ACCOUNTS if everything else ())
        for name, model in tables:
            s.execute(delete(model))
        if not everything:
            # A fresh run history: the schedule fires again on the next poll
            # rather than waiting for tomorrow's slot.
            values = {"schedule_last_run": None}
            if reset_preferences:
                # Empty means "regenerate the shipped defaults on the next
                # request"; the agent picks those up on its next poll.
                values["preferences_yaml"] = ""
            s.execute(update(User).values(**values))
        s.commit()

        after = counts(s)
        say("")
        for name, model in tables:
            say(f"    removed {before[name]} {name}")
        if reset_preferences and not everything:
            say("    preferences reset to the shipped defaults")
        if everything:
            say("\n  accounts removed: signup is open again at your site, and every")
            say("  PC needs linking afresh (jobauto link --url=... --token=...)")
        else:
            say("\n  account, agent link and preferences kept. Your PC stays linked;")
            say("  its own history is separate -- on it, run: jobauto reset --yes")
        return before


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://…@{rest.split('@', 1)[1]}"
    return url


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default="",
                   help="database URL (default: DATABASE_URL, else JOBAUTO_CLOUD_DB)")
    p.add_argument("--yes", action="store_true", help="actually remove; otherwise dry run")
    p.add_argument("--everything", action="store_true",
                   help="also remove accounts and agent links")
    p.add_argument("--reset-preferences", action="store_true",
                   help="also put preferences back to the shipped defaults")
    args = p.parse_args(argv)

    url = args.url or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        local = os.environ.get("JOBAUTO_CLOUD_DB", "").strip()
        url = f"sqlite:///{local}" if local else ""
    if not url:
        print("  No database given. Set DATABASE_URL (from your Render/Supabase "
              "settings) or pass --url.", file=sys.stderr)
        return 2
    url = clouddb.normalise_url(url) if hasattr(clouddb, "normalise_url") else url

    print()
    try:
        reset(url, everything=args.everything,
              reset_preferences=args.reset_preferences, yes=args.yes)
    except Exception as exc:
        print(f"\n  could not reset: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
