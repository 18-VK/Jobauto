"""Cloud database.

Runs on SQLite locally and Postgres in production, selected by DATABASE_URL.

What this database deliberately does NOT contain: portal passwords, portal
session cookies, or browser profiles. Those exist only on the machine running
the agent. If this database leaks, nobody gains access to any job portal.
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, create_engine, func, select,
                        text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Application statuses that mean a job has been acted on. One rule, used in
# two places that must agree: the jobs list hides these (they belong to
# Applications now), and the scheduled apply chain does not count them as
# backlog. When the two disagreed, the chain kept queueing batches for a
# backlog that the jobs list -- and the PC's own shortlist -- said was empty.
RETIRES_JOB_STATUSES = ("submitted", "skipped", "external", "failed",
                        "prepared")


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- account
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)

    # Preferences live in the cloud so they can be edited with the PC off.
    # The agent pulls them at the start of every run.
    preferences_yaml: Mapped[str] = mapped_column(Text, default="")
    preferences_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)

    # When the daily schedule last fired. Nullable: most accounts never enable
    # it, and a null simply means "has not run yet".
    schedule_last_run: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    agents: Mapped[list[Agent]] = relationship(back_populates="user",
                                               cascade="all, delete-orphan")


class Agent(Base):
    """One registered machine. The token is how the agent authenticates; it is
    separate from the user password so it can be revoked on its own."""
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="my-pc")
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                       nullable=True)
    last_status: Mapped[str] = mapped_column(String(255), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="agents")

    @property
    def online(self) -> bool:
        """Agents heartbeat while polling; two missed intervals means offline."""
        if not self.last_seen:
            return False
        seen = self.last_seen
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return (utcnow() - seen) < timedelta(minutes=3)

    @staticmethod
    def new_token() -> str:
        """URL-safe, and never starting with '-'.

        token_urlsafe uses the base64url alphabet, so about one token in sixty
        begins with a hyphen -- and a command line argument beginning with '-'
        is read as an option name, not a value. Callers should still pass it as
        --token=VALUE, but not minting the problem in the first place is free.
        """
        while True:
            token = secrets.token_urlsafe(32)
            if not token.startswith("-"):
                return token


# ------------------------------------------------------------------- jobs
class PasswordReset(Base):
    """A one-time password reset link.

    Only the SHA-256 of the token is stored, so this table is useless to anyone
    who reads it -- the same reason the password column holds a hash and not the
    password. The plaintext token exists only inside the emailed link.
    """
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                     nullable=True)

    @property
    def valid(self) -> bool:
        if self.used_at is not None:
            return False
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return utcnow() < expiry


class CloudJob(Base):
    """A job discovered by the agent and pushed up so it is readable with the
    PC off."""
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint",
                                       name="uq_job_user_fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)

    portal: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(String(200), default="")
    salary_text: Mapped[str] = mapped_column(String(80), default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    band: Mapped[str] = mapped_column(String(20), default="")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    dropped: Mapped[bool] = mapped_column(Boolean, default=False)

    # When the agent first saw it, which is not the same thing as when the
    # employer posted it -- a listing can be three weeks old the first time a
    # search surfaces it.
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                    default=utcnow)
    # What the portal said. Nullable: plenty of listings do not state one.
    posted_date: Mapped[date | None] = mapped_column(Date, nullable=True,
                                                     index=True)
    # queued -> the user asked for it; the agent picks it up next poll
    state: Mapped[str] = mapped_column(String(20), default="new", index=True)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)

    portal: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), index=True)
    answered_json: Mapped[str] = mapped_column(Text, default="{}")
    escalated_json: Mapped[str] = mapped_column(Text, default="[]")
    resume_path: Mapped[str] = mapped_column(String(300), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


# ------------------------------------------------------------------ queue
class Task(Base):
    """Work the user queued from the browser for the agent to run later.

    This is the whole point of the split: you queue with the PC off, the agent
    drains the queue when it next comes online.
    """
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    kind: Mapped[str] = mapped_column(String(20))        # discover|apply|refresh
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    log: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)


# ---------------------------------------------------------------- engine
_engine = None
_Session = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        path = os.environ.get("JOBAUTO_CLOUD_DB", "cloud.db")
        return f"sqlite:///{path}"
    # Render, Heroku and Supabase all hand out postgres:// or postgresql://,
    # and SQLAlchemy 2 rejects the former outright.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def is_transaction_pooler(url: str) -> bool:
    """Supabase offers three connection strings and they are not interchangeable.

      - direct (db.<ref>.supabase.co:5432) is IPv6-only on new projects, so it
        simply cannot be reached from an IPv4-only host like Render
      - session pooler (pooler.supabase.com:5432) is IPv4 and behaves like an
        ordinary Postgres connection -- this is the one to use
      - transaction pooler (pooler.supabase.com:6543) is pgBouncer in
        transaction mode, which does NOT support prepared statements

    psycopg3 prepares statements automatically after a few executions, so on
    the transaction pooler you get 'prepared statement "_pg3_0" already exists'
    once traffic warms up. Detect it and turn preparation off.
    """
    return ":6543" in url or "pgbouncer=true" in url.lower()


# Arbitrary but fixed: every worker must ask for the same lock for it to work.
_DDL_LOCK_KEY = 0x6A6F6261  # "joba"


def ensure_columns(engine) -> list[str]:
    """Add columns the models declare but the live tables lack.

    `create_all` creates missing tables and nothing else, so adding a field to
    an existing model silently does nothing on a deployment that already has
    the table -- and then every query naming that column fails. This closes the
    gap for plain nullable additions, which is all this project needs.
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateColumn

    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue                      # create_all will make it in full
        live = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in live:
                continue
            if not column.nullable and column.default is None                     and column.server_default is None:
                # Backfilling a NOT NULL column needs a decision this cannot
                # make safely, so leave it and let the error be visible.
                continue
            ddl = CreateColumn(column).compile(engine)
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
            added.append(f"{table.name}.{column.name}")

        # ADD COLUMN does not bring the column's index with it, and an index
        # that silently never exists turns a filter into a full table scan.
        live_indexes = {i["name"] for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in live_indexes:
                continue
            try:
                index.create(bind=engine)
                added.append(f"index {index.name}")
            except Exception:
                # A concurrent worker may have won the race; harmless.
                pass

    return added


def create_schema(engine) -> None:
    """Create missing tables, safely when several workers boot at once.

    `create_all` is check-then-create with no locking, so concurrent gunicorn
    workers against an empty database all see "no tables", all issue CREATE
    TABLE, and every worker but one dies with a UniqueViolation on
    pg_type_typname_nsp_index. That took down the first Render deploy.

    A transaction-scoped advisory lock serialises it: the first worker creates
    the tables and commits, the rest wait, then find everything already there.
    """
    if engine.dialect.name != "postgresql":
        # SQLite deployments are single-process; its own file locking suffices.
        Base.metadata.create_all(engine)
        _log_added(ensure_columns(engine))
        return

    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": _DDL_LOCK_KEY})
        Base.metadata.create_all(conn)
        # Lock releases when this transaction commits.

    # Same serialisation reason as create_all: concurrent workers must not
    # both try to add the same column.
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": _DDL_LOCK_KEY})
    _log_added(ensure_columns(engine))


def _log_added(added: list[str]) -> None:
    if added:
        import logging
        logging.getLogger("jobauto.db").info(
            "added missing columns: %s", ", ".join(added))


def init_engine(url: str | None = None, echo: bool = False):
    global _engine, _Session
    url = url or database_url()
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Free Postgres tiers cap connections hard; keep the pool small and
        # recycle before the provider's idle timeout kills a connection.
        kwargs.update(pool_size=3, max_overflow=2, pool_pre_ping=True,
                      pool_recycle=280)
        # Without a timeout an unreachable host (the IPv6-only Supabase direct
        # endpoint is the classic one) makes the worker hang until gunicorn
        # kills it on boot timeout -- which logs no traceback at all, just
        # "Exited with status 3". Fail in ten seconds with a reason instead.
        connect_args: dict[str, Any] = {"connect_timeout": 10}
        if is_transaction_pooler(url):
            # pgBouncer in transaction mode hands you a different backend per
            # transaction, so a prepared statement from one is meaningless to
            # the next. Turning preparation off is the supported fix.
            connect_args["prepare_threshold"] = None
            kwargs["pool_recycle"] = 120
        kwargs["connect_args"] = connect_args
    _engine = create_engine(url, **kwargs)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    try:
        create_schema(_engine)
    except Exception as exc:
        # Say which database could not be reached and why. Left to gunicorn
        # this surfaces as a bare "Exited with status 3" with no cause.
        raise DatabaseUnreachable(explain_connection_failure(url, exc)) from exc
    return _engine


class DatabaseUnreachable(RuntimeError):
    pass


def explain_connection_failure(url: str, exc: Exception) -> str:
    """Turn a driver error into something actionable in a deploy log."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host, port, user = parts.hostname or "?", parts.port or 5432, parts.username or "?"
    except Exception:
        host, port, user = "?", "?", "?"

    lines = [
        "",
        "=" * 68,
        "  DATABASE UNREACHABLE -- the app cannot start.",
        "=" * 68,
        f"  host   {host}",
        f"  port   {port}",
        f"  user   {user}",
        f"  error  {type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
        "",
    ]

    text = str(exc).lower()
    if host.startswith("db.") and host.endswith(".supabase.co"):
        ref = host[len("db."):-len(".supabase.co")]
        lines += [
            "  This is the Supabase DIRECT endpoint, which is IPv6-only on new",
            "  projects. Render has no IPv6 egress, so it can never be reached",
            "  from here -- it will hang rather than refuse.",
            "",
            "  Use the session pooler instead:",
            f"    postgresql://postgres.{ref}:<password>"
            f"@aws-0-<region>.pooler.supabase.com:5432/postgres",
        ]
    elif "pooler.supabase.com" in host and user == "postgres":
        lines += [
            "  The Supabase pooler needs the username 'postgres.<project-ref>',",
            "  not a bare 'postgres' -- that form belongs to the direct string.",
        ]
    elif "timeout" in text or "timed out" in text:
        lines += [
            "  The host did not answer within 10 seconds. Usually a wrong",
            "  hostname, or an endpoint this network cannot route to.",
        ]
    elif "password authentication failed" in text:
        lines += [
            "  The host answered but rejected the credentials. Check the",
            "  username and password in DATABASE_URL; a '@' or '#' in the",
            "  password must be percent-encoded (%40, %23).",
        ]
    elif "does not exist" in text:
        lines += ["  The server is reachable but that database name is wrong."]

    lines += ["", "  Checked with: python scripts/check_db_url.py \"<url>\" --connect",
              "=" * 68, ""]
    return "\n".join(lines)


def session():
    if _Session is None:
        init_engine()
    return _Session()


def reset_engine() -> None:
    """Tests only."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine, _Session = None, None
