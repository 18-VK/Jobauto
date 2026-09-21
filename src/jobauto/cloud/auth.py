"""Accounts: signup, login, agent tokens.

Passwords are hashed with werkzeug's scrypt (its current default). Agent tokens
are random 32-byte URL-safe strings, compared in constant time.

Signup is closed by default. JOBAUTO_ALLOW_SIGNUP=1 opens it, and
JOBAUTO_SIGNUP_CODE gates it with an invite code -- otherwise anyone who finds
your URL can create an account on your deployment.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from flask import g, jsonify, redirect, request, session as flask_session, url_for
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from .db import Agent, PasswordReset, User, session, utcnow

RESET_TTL_MINUTES = 60

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10


class AuthError(Exception):
    pass


# ------------------------------------------------------------- policies
def signup_open() -> bool:
    return os.environ.get("JOBAUTO_ALLOW_SIGNUP", "").strip() in ("1", "true", "yes")


def signup_code() -> str:
    return os.environ.get("JOBAUTO_SIGNUP_CODE", "").strip()


def validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
    if password.isdigit() or password.isalpha():
        raise AuthError("Use a mix of letters, numbers or symbols.")


# ---------------------------------------------------------------- users
def user_count() -> int:
    with session() as s:
        return int(s.scalar(select(User).with_only_columns(
            __import__("sqlalchemy").func.count(User.id))) or 0)


def create_user(email: str, password: str, code: str = "") -> User:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("That does not look like an email address.")
    validate_password(password)

    # The very first account is always allowed -- that is the owner claiming a
    # fresh deployment. Everything after that obeys the signup policy.
    with session() as s:
        existing = s.scalar(select(User).where(User.email == email))
        if existing:
            raise AuthError("An account with that email already exists.")

        first = s.scalar(select(User).limit(1)) is None
        if not first:
            if not signup_open():
                raise AuthError("Signups are closed on this deployment.")
            required = signup_code()
            if required and (code or "").strip() != required:
                raise AuthError("Invalid signup code.")

        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            preferences_yaml=_default_preferences(),
        )
        s.add(user)
        s.commit()

        # Every new account gets one agent token so the PC can be linked
        # immediately without a second step.
        agent = Agent(user_id=user.id, name="my-pc", token=Agent.new_token())
        s.add(agent)
        s.commit()
        s.refresh(user)
        return user


def authenticate(email: str, password: str) -> User:
    email = (email or "").strip().lower()
    with session() as s:
        user = s.scalar(select(User).where(User.email == email))
        # Hash even on a miss so timing does not reveal whether the email exists.
        if user is None:
            generate_password_hash(password or "x")
            raise AuthError("Wrong email or password.")
        if not check_password_hash(user.password_hash, password or ""):
            raise AuthError("Wrong email or password.")
        user.last_login = utcnow()
        s.commit()
        s.refresh(user)
        return user


def change_password(user_id: int, current: str, new: str) -> None:
    validate_password(new)
    with session() as s:
        user = s.get(User, user_id)
        if user is None:
            raise AuthError("No such account.")
        if not check_password_hash(user.password_hash, current or ""):
            raise AuthError("Current password is wrong.")
        user.password_hash = generate_password_hash(new)
        s.commit()


# ------------------------------------------------------- password reset
def _hash_token(token: str) -> str:
    """SHA-256 is right here, unlike for passwords: the token is 32 bytes of
    real entropy, so there is nothing to brute-force and no need to slow it
    down. It just must not be stored in a directly usable form."""
    return hashlib.sha256(token.encode()).hexdigest()


def begin_password_reset(email: str) -> tuple[str, str] | None:
    """Issue a reset token. Returns (token, email), or None if no such account.

    The caller must NOT tell the browser which it was -- that would let anyone
    test whether an address has an account here.
    """
    email = (email or "").strip().lower()
    with session() as s:
        user = s.scalar(select(User).where(User.email == email))
        if user is None:
            return None

        # Any earlier outstanding link stops working the moment a new one is
        # issued, so a forwarded or leaked old email cannot be replayed.
        for old in s.scalars(select(PasswordReset).where(
                PasswordReset.user_id == user.id,
                PasswordReset.used_at.is_(None))).all():
            old.used_at = utcnow()

        token = secrets.token_urlsafe(32)
        s.add(PasswordReset(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=utcnow() + timedelta(minutes=RESET_TTL_MINUTES),
        ))
        s.commit()
        return token, user.email


def check_reset_token(token: str) -> User | None:
    """The user behind a still-valid token, or None. Does not consume it --
    the reset form needs to render before anything is spent."""
    if not token:
        return None
    with session() as s:
        record = s.scalar(select(PasswordReset).where(
            PasswordReset.token_hash == _hash_token(token)))
        if record is None or not record.valid:
            return None
        return s.get(User, record.user_id)


def complete_password_reset(token: str, new_password: str) -> User:
    validate_password(new_password)
    with session() as s:
        record = s.scalar(select(PasswordReset).where(
            PasswordReset.token_hash == _hash_token(token)))
        if record is None or not record.valid:
            raise AuthError(
                "That reset link has expired or was already used. "
                "Request a new one.")

        user = s.get(User, record.user_id)
        if user is None:
            raise AuthError("No such account.")

        user.password_hash = generate_password_hash(new_password)
        record.used_at = utcnow()          # single use, enforced in the DB row
        s.commit()
        s.refresh(user)
        return user


def purge_expired_resets() -> int:
    """Housekeeping; cheap enough to call on each reset request."""
    with session() as s:
        stale = s.scalars(select(PasswordReset).where(
            PasswordReset.expires_at < utcnow())).all()
        for record in stale:
            s.delete(record)
        s.commit()
        return len(stale)


def _default_preferences() -> str:
    """Seed a new account from the shipped template so the dashboard is usable
    the moment you sign up."""
    from pathlib import Path
    template = Path(__file__).resolve().parents[3] / "config" / "preferences.yaml"
    try:
        return template.read_text(encoding="utf-8")
    except Exception:
        return "search:\n  roles:\n    - title: \"Software Developer\"\n"


# --------------------------------------------------------------- agents
def agent_from_token(token: str) -> Agent | None:
    if not token:
        return None
    with session() as s:
        agent = s.scalar(select(Agent).where(Agent.token == token))
        if agent is None or agent.revoked:
            return None
        return agent


def touch_agent(agent_id: int, status: str = "") -> None:
    with session() as s:
        agent = s.get(Agent, agent_id)
        if agent:
            agent.last_seen = utcnow()
            if status:
                agent.last_status = status[:255]
            s.commit()


def rotate_agent_token(user_id: int, agent_id: int) -> str:
    with session() as s:
        agent = s.get(Agent, agent_id)
        if agent is None or agent.user_id != user_id:
            raise AuthError("No such agent.")
        agent.token = Agent.new_token()
        agent.revoked = False
        s.commit()
        return agent.token


# ----------------------------------------------------------- decorators
def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        uid = flask_session.get("user_id")
        if not uid:
            if request.path.startswith("/api/"):
                return jsonify({"error": "not signed in"}), 401
            return redirect(url_for("login", next=request.path))
        with session() as s:
            user = s.get(User, uid)
        if user is None:
            flask_session.clear()
            return redirect(url_for("login"))
        g.user = user
        return view(*args, **kwargs)
    return wrapper


def agent_required(view: Callable) -> Callable:
    """Agent endpoints authenticate with X-Agent-Token, never a browser
    session -- the agent is a daemon, not a logged-in user."""
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        token = (request.headers.get("X-Agent-Token")
                 or request.args.get("agent_token", ""))
        agent = agent_from_token(token.strip())
        if agent is None:
            return jsonify({"error": "invalid agent token"}), 401
        g.agent = agent
        return view(*args, **kwargs)
    return wrapper
