"""Find and merge duplicate jobs and applications in a jobauto cloud database.

    python scripts/dedupe_cloud.py                    # dry run: lists every group
    python scripts/dedupe_cloud.py --yes              # merge them
    python scripts/dedupe_cloud.py --ignore-location  # also fold the same role
                                                      # at the same company across cities

Reads DATABASE_URL, or JOBAUTO_CLOUD_DB for a local SQLite file, or --url.

Two listings are the same job when their title (seniority words aside), their
company (legal form aside) and their city match -- the same rule the
fingerprint uses now. Rows written before the company rule existed carry
different fingerprints for "Acme" and "Acme Pvt Ltd", so the same posting
sits in the jobs list twice; this collapses them. Applications are grouped the
same way, per portal, and the one that survives is re-pointed at the surviving
job so it keeps that job out of the jobs list.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from jobauto.cloud import db as clouddb  # noqa: E402
from jobauto.cloud.db import RETIRES_JOB_STATUSES, Application, CloudJob, User  # noqa: E402
from jobauto.models import loose_key, normalise_company, normalise_title  # noqa: E402

# Which application to keep when several describe one job: the furthest
# along, then the most recent.
_RANK = {"submitted": 5, "prepared": 4, "external": 3, "skipped": 2, "failed": 1}


def _key(title: str, company: str, location: str, ignore_location: bool) -> str:
    if ignore_location:
        return f"{normalise_title(title)}|{normalise_company(company)}"
    return loose_key(title, company, location)


def _job_groups(jobs: list, ignore_location: bool) -> list[list]:
    by: dict[str, list] = defaultdict(list)
    for j in jobs:
        by[_key(j.title, j.company, j.location, ignore_location)].append(j)
    return [g for g in by.values() if len(g) > 1]


def _app_groups(apps: list, ignore_location: bool) -> list[list]:
    by: dict[tuple, list] = defaultdict(list)
    for a in apps:
        # Applications carry no location; group by fingerprint first (exact
        # duplicates), then by title and company within a portal.
        by[(a.portal, f"{normalise_title(a.title)}|{normalise_company(a.company)}")].append(a)
    return [g for g in by.values() if len(g) > 1]


def _pick_job(group: list, acted_on: set[str]):
    """The row to keep: one an application points at, else the queued one,
    else the best-scored, else the oldest."""
    def rank(j):
        return (j.fingerprint in acted_on, j.state == "queued", j.score or 0.0,
                -(j.id or 0))
    return max(group, key=rank)


def _pick_app(group: list):
    return max(group, key=lambda a: (_RANK.get(a.status, 0),
                                      a.updated_at or a.created_at, a.id or 0))


def dedupe(url: str, *, yes: bool = False, ignore_location: bool = False,
           say=print) -> dict[str, int]:
    clouddb.reset_engine()
    clouddb.init_engine(url)
    removed = {"jobs": 0, "applications": 0, "groups": 0}
    with clouddb.session() as s:
        for user in s.scalars(select(User)).all():
            jobs = s.scalars(select(CloudJob).where(CloudJob.user_id == user.id)).all()
            apps = s.scalars(select(Application).where(Application.user_id == user.id)).all()
            acted_on = {a.fingerprint for a in apps if a.status in RETIRES_JOB_STATUSES}

            jgroups = _job_groups(jobs, ignore_location)
            agroups = _app_groups(apps, ignore_location)
            if not jgroups and not agroups:
                continue
            say(f"  {user.email}")
            kept_fp_by_key: dict[str, str] = {}

            for group in jgroups:
                keep = _pick_job(group, acted_on)
                kept_fp_by_key[f"{normalise_title(keep.title)}|{normalise_company(keep.company)}"] = keep.fingerprint
                say(f"    job: {keep.title[:50]} @ {keep.company[:30]}")
                for j in group:
                    mark = "keep  " if j is keep else "remove"
                    say(f"      {mark} [{j.portal:<9}] {j.state:<7} score {j.score:5.1f}  "
                        f"{(j.location or '')[:18]:<18} {j.company[:32]}")
                if yes:
                    if any(j.state == "queued" for j in group) and keep.fingerprint not in acted_on:
                        keep.state = "queued"
                    for j in group:
                        if j is not keep:
                            s.delete(j)
                            removed["jobs"] += 1
                removed["groups"] += 1

            for group in agroups:
                keep = _pick_app(group)
                say(f"    application: {keep.title[:50]} @ {keep.company[:30]} ({keep.portal})")
                for a in group:
                    mark = "keep  " if a is keep else "remove"
                    say(f"      {mark} {a.status:<9} {a.company[:32]}")
                if yes:
                    # Point the survivor at the surviving job, so it keeps
                    # that job out of the jobs list.
                    target = kept_fp_by_key.get(
                        f"{normalise_title(keep.title)}|{normalise_company(keep.company)}")
                    if target:
                        keep.fingerprint = target
                    for a in group:
                        if a is not keep:
                            s.delete(a)
                            removed["applications"] += 1
                removed["groups"] += 1
        if yes:
            s.commit()

    if removed["groups"] == 0:
        say("  no duplicates found")
    elif not yes:
        say(f"\n  dry run -- {removed['groups']} group(s) found, nothing removed. "
            f"Add --yes to merge them.")
    else:
        say(f"\n  merged {removed['groups']} group(s): removed {removed['jobs']} "
            f"job(s) and {removed['applications']} application(s)")
    return removed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default="")
    p.add_argument("--yes", action="store_true", help="merge; otherwise dry run")
    p.add_argument("--ignore-location", action="store_true",
                   help="treat the same role at the same company as one job across cities")
    args = p.parse_args(argv)
    url = args.url or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        local = os.environ.get("JOBAUTO_CLOUD_DB", "").strip()
        url = f"sqlite:///{local}" if local else ""
    if not url:
        print("  No database given. Set DATABASE_URL or pass --url.", file=sys.stderr)
        return 2
    print()
    try:
        dedupe(url, yes=args.yes, ignore_location=args.ignore_location)
    except Exception as exc:
        print(f"\n  could not dedupe: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
