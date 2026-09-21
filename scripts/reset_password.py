"""Set or reset a cloud account password directly against the database.

The escape hatch for when email is not configured and you would rather not go
digging through deploy logs. Connects with DATABASE_URL, so it needs no running
web service and works whether the app is up, asleep or broken.

    # set a new password straight away
    python scripts/reset_password.py --email you@example.com --password "new-one-here"

    # or mint a reset link to open in the browser
    python scripts/reset_password.py --email you@example.com --link https://your-app.onrender.com

    # see who has an account
    python scripts/reset_password.py --list

Pass --url to override DATABASE_URL.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select                                     # noqa: E402

from jobauto.cloud import auth                                    # noqa: E402
from jobauto.cloud.db import User, init_engine, session           # noqa: E402


def connect(url: str | None) -> None:
    if url:
        os.environ["DATABASE_URL"] = url
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "\n  No database configured. Either set DATABASE_URL or pass --url:\n"
            "    python scripts/reset_password.py --url \"postgresql://...\" --list\n")
    try:
        init_engine()
    except Exception as exc:
        raise SystemExit(f"\n  cannot reach the database:\n    {exc}\n")


def list_users() -> int:
    with session() as s:
        users = s.scalars(select(User).order_by(User.id)).all()
    if not users:
        print("\n  No accounts on this database yet.\n")
        return 1
    print(f"\n  {len(users)} account(s):\n")
    for user in users:
        last = user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else "never"
        print(f"    {user.id:>3}  {user.email:<40} last login: {last}")
    print()
    return 0


def set_password(email: str, password: str) -> int:
    from werkzeug.security import generate_password_hash

    try:
        auth.validate_password(password)
    except auth.AuthError as exc:
        raise SystemExit(f"\n  {exc}\n")

    with session() as s:
        user = s.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            print(f"\n  No account for {email}. Known accounts:")
            list_users()
            return 1
        user.password_hash = generate_password_hash(password)
        s.commit()

    print(f"\n  Password updated for {email}.")
    print("  Sign in with it now -- nothing needs redeploying.\n")
    return 0


def make_link(email: str, base_url: str) -> int:
    issued = auth.begin_password_reset(email)
    if issued is None:
        print(f"\n  No account for {email}. Known accounts:")
        list_users()
        return 1

    token, address = issued
    link = f"{base_url.rstrip('/')}/reset/{token}"
    print(f"\n  Reset link for {address}:\n")
    print(f"    {link}\n")
    print(f"  Single use, expires in {auth.RESET_TTL_MINUTES} minutes.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="database URL; defaults to DATABASE_URL")
    ap.add_argument("--email")
    ap.add_argument("--password", help="set this password directly")
    ap.add_argument("--link", metavar="BASE_URL",
                    help="mint a reset link against this site instead")
    ap.add_argument("--list", action="store_true", help="list accounts")
    args = ap.parse_args()

    connect(args.url)

    if args.list or not args.email:
        return list_users()
    if args.password:
        return set_password(args.email, args.password)
    if args.link:
        return make_link(args.email, args.link)

    ap.error("give --password to set one directly, or --link to mint a link")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
