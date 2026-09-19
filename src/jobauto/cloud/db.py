"""Cloud database.

Runs on SQLite locally and Postgres in production, selected by DATABASE_URL.

What this database deliberately does NOT contain: portal passwords, portal
session cookies, or browser profiles. Those exist only on the machine running
the agent. If this database leaks, nobody gains access to any job portal.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, create_engine, func, select,
                        text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        return secrets.token_urlsafe(32)


# ------------------------------------------------------------------- jobs
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

    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                    default=utcnow)
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
    # Render and Heroku hand out postgres:// which SQLAlchemy 2 rejects.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# Arbitrary but fixed: every worker must ask for the same lock for it to work.
_DDL_LOCK_KEY = 0x6A6F6261  # "joba"


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
        return

    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": _DDL_LOCK_KEY})
        Base.metadata.create_all(conn)
        # Lock releases when this transaction commits.


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
    _engine = create_engine(url, **kwargs)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    create_schema(_engine)
    return _engine


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
