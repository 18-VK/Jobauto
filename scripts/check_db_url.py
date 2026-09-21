"""Explain a database URL before you use it, without revealing the password.

    python scripts/check_db_url.py "postgresql://..."
    python scripts/check_db_url.py "postgresql://..." --connect

Prints each component, says which Supabase connection mode it is, and names
anything that will fail. The password is never printed -- only its length and
whether it contains characters that need escaping.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Characters that terminate a URL component and so must be percent-encoded.
NEEDS_ESCAPING = "@/#?:[] "


def prescan(raw: str) -> list[str]:
    """Catch the two cases that break urlsplit itself, before it is called.

    Both come straight from copy-pasting the Supabase string, so they must
    produce a clear message rather than a parser traceback or -- worse -- a
    confidently wrong reading of a mangled URL.
    """
    problems: list[str] = []

    if "[" in raw or "]" in raw:
        problems.append(
            "The URL still contains [ ] -- that is Supabase's placeholder.\n"
            "       Replace [YOUR-PASSWORD] (brackets included) with your real "
            "database password.")

    body = raw.split("://", 1)[1] if "://" in raw else raw
    if body.count("@") > 1:
        problems.append(
            "There is more than one @ in the URL, so your password almost "
            "certainly contains one.\n"
            "       Percent-encode it (@ becomes %40) or reset the password to "
            "letters and digits.")

    if "#" in body:
        problems.append(
            "There is a # in the URL. Everything after it is discarded as a "
            "URL fragment,\n"
            "       so a password containing # silently loses its tail. "
            "Percent-encode it (%23)\n"
            "       or reset the password to letters and digits.")

    return problems


def describe(raw: str, try_connect: bool = False) -> int:
    raw = raw.strip()

    fatal = prescan(raw)
    if fatal:
        print("\n  problems")
        for p in fatal:
            print(f"    !  {p}")
        print("\n  Fix these first -- the URL cannot be parsed reliably as it "
              "stands.\n")
        return 1

    try:
        parts = urlsplit(raw)
        parts.port          # port parsing is lazy and can raise on its own
    except ValueError as exc:
        print(f"\n  this is not a usable URL: {exc}\n")
        return 1

    if parts.scheme.startswith("sqlite"):
        print(f"\n  sqlite file: {parts.path.lstrip('/')}\n")
        return 0

    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    host = parts.hostname or ""
    port = parts.port
    database = (parts.path or "").lstrip("/")

    print("\n  parsed as")
    print(f"    scheme     {parts.scheme}")
    print(f"    user       {user or '(none)'}")
    print(f"    password   {'*' * min(len(password), 12)}  ({len(password)} chars)"
          if password else "    password   (none)")
    print(f"    host       {host or '(none)'}")
    print(f"    port       {port or '(default 5432)'}")
    print(f"    database   {database or '(none)'}")

    problems: list[str] = []
    notes: list[str] = []

    # ------------------------------------------------ Supabase specifics
    supabase_direct = host.startswith("db.") and host.endswith(".supabase.co")
    supabase_pooler = host.endswith("pooler.supabase.com")

    if supabase_direct:
        ref = host[len("db."):-len(".supabase.co")]
        print(f"\n  Supabase DIRECT connection (project {ref})")
        problems.append(
            "Direct connections are IPv6-only on new Supabase projects, so "
            "Render and most home networks cannot reach them.\n"
            f"       Use instead: postgresql://postgres.{ref}:<password>"
            f"@aws-0-<region>.pooler.supabase.com:5432/postgres")

    elif supabase_pooler:
        mode = "TRANSACTION pooler" if port == 6543 else "SESSION pooler"
        print(f"\n  Supabase {mode}  (host {host})")

        # This is the mistake that produces
        # 'password authentication failed for user "postgres"'.
        if user == "postgres":
            problems.append(
                'The pooler needs the username "postgres.<project-ref>", not '
                'plain "postgres".\n'
                "       A bare `postgres` is what the DIRECT string uses, so "
                "this usually means\n"
                "       the host was changed but the username was left behind.\n"
                "       The server reports this as: password authentication "
                'failed for user "postgres"')
        elif not user.startswith("postgres."):
            problems.append(
                f'Expected a username like "postgres.<project-ref>", got "{user}".')
        else:
            ref = user.split(".", 1)[1]
            notes.append(f"project ref looks like {ref}")

        if port == 6543:
            notes.append("transaction pooler works -- the app disables prepared "
                         "statements for it -- but :5432 (session) is simpler")
        elif port not in (5432, None):
            problems.append(f"Unexpected pooler port {port}; use 5432 or 6543.")

    # ------------------------------------------------------ generic checks
    if not password:
        problems.append("No password in the URL.")
    elif password.upper() in ("[YOUR-PASSWORD]", "YOUR-PASSWORD",
                              "YOURPASSWORD", "PASSWORD"):
        problems.append("The password is still the placeholder text. Replace it "
                        "with your real database password.")
    else:
        risky = [c for c in NEEDS_ESCAPING if c in password]
        if risky:
            shown = " ".join(repr(c) for c in risky)
            problems.append(
                f"The password contains {shown}, which breaks URL parsing.\n"
                "       Percent-encode it (@ becomes %40), or reset the password "
                "to letters and digits\n"
                "       in Supabase: Settings -> Database -> Reset database password.")

    if not database:
        problems.append("No database name. Supabase uses /postgres at the end.")

    for note in notes:
        print(f"    note: {note}")

    if problems:
        print("\n  problems")
        for p in problems:
            print(f"    !  {p}")
    else:
        print("\n  looks well-formed")

    if try_connect:
        print("\n  connecting...")
        from sqlalchemy import create_engine, text
        from jobauto.cloud.db import is_transaction_pooler

        url = raw.strip()
        for old, new in (("postgres://", "postgresql+psycopg://"),
                         ("postgresql://", "postgresql+psycopg://")):
            if url.startswith(old):
                url = url.replace(old, new, 1)
                break
        connect_args = {"connect_timeout": 10}
        if is_transaction_pooler(url):
            connect_args["prepare_threshold"] = None
        try:
            engine = create_engine(url, connect_args=connect_args)
            with engine.connect() as conn:
                version = conn.execute(text("SHOW server_version")).scalar()
                tables = conn.execute(text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'")).scalar()
            print(f"    connected. Postgres {version}, "
                  f"{tables} table(s) in the public schema.\n")
            return 0
        except Exception as exc:
            print(f"    failed: {type(exc).__name__}")
            print(f"    {str(exc).splitlines()[0]}\n")
            return 1

    print()
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--connect", action="store_true",
                    help="also try connecting")
    args = ap.parse_args()
    return describe(args.url, args.connect)


if __name__ == "__main__":
    raise SystemExit(main())
