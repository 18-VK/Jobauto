"""Copy a jobauto cloud database from one server to another.

Written for the Render -> Supabase move, but the direction is arbitrary: it
works between any two URLs SQLAlchemy can reach, including SQLite, so you can
also pull production down to a local file to inspect it.

It copies through the ORM rather than shelling out to pg_dump, which means no
version-matching between client and server, and it can count and compare rows
on both sides when it is done.

    python scripts/migrate_db.py --from "<source url>" --to "<target url>"
    python scripts/migrate_db.py --from "<url>" --to "<url>" --dry-run
    python scripts/migrate_db.py --from "<url>" --to "<url>" --wipe-target

Read the source with a *direct* connection where possible. For Supabase as the
target from a laptop, the session pooler string works fine.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, func, select, text          # noqa: E402
from sqlalchemy.orm import sessionmaker                           # noqa: E402

from jobauto.cloud.db import (Agent, Application, Base, CloudJob,  # noqa: E402
                              PasswordReset, Task, User,
                              is_transaction_pooler)

# Order matters: users own everything else, so they must land first or the
# foreign keys fail.
TABLES = [User, Agent, PasswordReset, CloudJob, Application, Task]


def normalise(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def make_engine(url: str, label: str):
    url = normalise(url)
    kwargs: dict[str, Any] = {"future": True}
    if not url.startswith("sqlite"):
        # Without a timeout a wrong host just hangs with no output, which on a
        # migration is the worst possible failure mode -- you cannot tell a
        # slow copy from a dead connection.
        connect_args: dict[str, Any] = {"connect_timeout": 10}
        if is_transaction_pooler(url):
            connect_args["prepare_threshold"] = None
        kwargs["connect_args"] = connect_args
    try:
        engine = create_engine(url, **kwargs)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise SystemExit(
            f"\n  cannot connect to the {label} database:\n"
            f"    {type(exc).__name__}: {exc}\n\n"
            f"  If this is Supabase, check you used the *session pooler* string\n"
            f"  (pooler.supabase.com:5432). The direct db.<ref>.supabase.co host\n"
            f"  is IPv6-only and unreachable from most networks.\n")
    return engine


def present_tables(engine) -> set[str]:
    """Which jobauto tables actually exist. A database that has never run the
    app has none, and asking it for a row count just raises UndefinedTable."""
    from sqlalchemy import inspect
    try:
        return set(inspect(engine).get_table_names())
    except Exception:
        return set()


def row_counts(session_factory, engine) -> dict[str, int | None]:
    """Rows per table. None means the table is absent rather than empty --
    a distinction that matters when working out which database is which."""
    existing = present_tables(engine)
    out: dict[str, int | None] = {}
    with session_factory() as s:
        for model in TABLES:
            name = model.__tablename__
            if name not in existing:
                out[name] = None
                continue
            try:
                out[name] = int(s.scalar(select(func.count()).select_from(model)) or 0)
            except Exception:
                s.rollback()
                out[name] = None
    return out


def fmt(value: int | None) -> str:
    return "  --" if value is None else f"{value:>4}"


def copy_table(model, src_factory, dst_factory, batch: int = 500) -> int:
    """Copy one table, preserving primary keys so foreign keys stay valid."""
    copied = 0
    with src_factory() as src, dst_factory() as dst:
        existing = {row[0] for row in dst.execute(select(model.id))}
        rows = src.scalars(select(model).order_by(model.id)).all()

        pending = []
        for row in rows:
            if row.id in existing:
                continue                 # resumable: skip what is already there
            data = {c.name: getattr(row, c.name)
                    for c in model.__table__.columns}
            pending.append(data)
            if len(pending) >= batch:
                dst.execute(model.__table__.insert(), pending)
                dst.commit()
                copied += len(pending)
                pending = []

        if pending:
            dst.execute(model.__table__.insert(), pending)
            dst.commit()
            copied += len(pending)
    return copied


def reset_sequences(engine) -> None:
    """Rows were inserted with explicit ids, which leaves each SERIAL sequence
    still at 1 -- the next signup would collide. Fast-forward them."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for model in TABLES:
            table = model.__tablename__
            conn.execute(text(f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table}), 1),
                    (SELECT MAX(id) IS NOT NULL FROM {table})
                )"""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="source", required=True, help="source URL")
    ap.add_argument("--to", dest="target", required=True, help="target URL")
    ap.add_argument("--dry-run", action="store_true",
                    help="report both sides and copy nothing")
    ap.add_argument("--wipe-target", action="store_true",
                    help="delete all rows in the target first")
    args = ap.parse_args()

    print("\n  connecting...")
    src_engine = make_engine(args.source, "source")
    dst_engine = make_engine(args.target, "target")
    SrcSession = sessionmaker(bind=src_engine, future=True)
    DstSession = sessionmaker(bind=dst_engine, future=True)

    # Read the source before touching the target, so a mistaken --from is
    # reported without having created anything anywhere.
    before_src = row_counts(SrcSession, src_engine)

    if all(v is None for v in before_src.values()):
        print("\n  The SOURCE database has none of the jobauto tables.\n")
        print("  It has never run the app, so there is nothing to copy. Usually"
              " this means:\n")
        print("    - --from and --to are the wrong way round (--from is the OLD"
              " database,")
        print("      --to is the NEW empty one), or")
        print("    - --from points at a fresh database you just created\n")
        print("  Nothing was written to either database.\n")
        return 1

    print("  creating any missing tables on the target")
    Base.metadata.create_all(dst_engine)
    before_dst = row_counts(DstSession, dst_engine)

    print("\n  table                 source    target")
    print("  " + "-" * 40)
    for table in before_src:
        print(f"  {table:<22}{fmt(before_src[table])}  ->{fmt(before_dst[table])}")
    if any(v is None for v in before_src.values()):
        print("\n  (-- means the table does not exist on that side)")

    total = sum(v for v in before_src.values() if v)
    if args.dry_run:
        print(f"\n  dry run: {total} row(s) would be copied. Nothing changed.\n")
        return 0

    if not total:
        print("\n  the source has the tables but no rows -- nothing to migrate.\n")
        return 1

    if args.wipe_target and any(before_dst.values()):
        print("\n  wiping target...")
        with DstSession() as dst:
            for model in reversed(TABLES):      # children before parents
                dst.execute(model.__table__.delete())
            dst.commit()

    print("\n  copying...")
    for model in TABLES:
        copied = copy_table(model, SrcSession, DstSession)
        print(f"    {model.__tablename__:<22}{copied:>6} row(s)")

    print("\n  resetting id sequences")
    reset_sequences(dst_engine)

    after_dst = row_counts(DstSession, dst_engine)
    print("\n  verifying")
    ok = True
    for table, expected in before_src.items():
        got = after_dst[table] or 0
        want = expected or 0
        mark = "ok" if got >= want else "MISMATCH"
        if got < want:
            ok = False
        print(f"    {table:<22}{want:>6} expected, {got:>6} present   {mark}")

    if not ok:
        print("\n  Some rows did not make it. The target was not cleaned up --\n"
              "  fix the cause and run again; copying skips rows already there.\n")
        return 1

    print("\n  Migration complete.\n")
    print("  Next:")
    print("    1. set DATABASE_URL on your host to the new (target) URL")
    print("    2. redeploy, and sign in to confirm your data is there")
    print("    3. only then delete the old database\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
