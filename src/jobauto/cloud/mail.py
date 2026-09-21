"""Outbound email, with a deliberate fallback.

A personal deployment on a free tier usually has no mail service, and making
password reset depend on one would mean it simply does not work for most people.

So: if SMTP is configured, the reset link is emailed. If it is not, the link is
printed to the server log instead. On Render that log is visible only to the
account owner, which for a single-user deployment is a reasonable delivery
channel -- and it is honest about what it is doing rather than silently failing.

What is never done: emailing a password. Passwords are not recoverable here,
only resettable, because only their hash is ever stored.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("jobauto.mail")


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"))


def _from_address() -> str:
    return (os.environ.get("SMTP_FROM")
            or os.environ.get("SMTP_USER")
            or "jobauto@localhost")


def send(to: str, subject: str, body: str) -> bool:
    """Returns True if the message was actually handed to an SMTP server.

    False means it was logged instead -- the caller must not treat that as an
    error, but must also not claim an email was sent.
    """
    if not smtp_configured():
        # Render's log viewer searches line by line, so put the actionable part
        # on one greppable line before the readable body.
        link = _first_link(body)
        if link:
            log.warning("JOBAUTO_RESET_LINK %s %s", to, link)
        log.warning(
            "SMTP not configured -- message for %s not emailed.\n"
            "--- %s ---\n%s\n--- end ---", to, subject, body)
        return False

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ.get("SMTP_PASSWORD", "")

    message = EmailMessage()
    message["From"] = _from_address()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
                server.login(user, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls(context=context)
                server.login(user, password)
                server.send_message(message)
        log.info("sent %r to %s", subject, to)
        return True
    except Exception as exc:
        # Never surface SMTP failures to the browser -- that would reveal
        # whether the address exists. Log, and fall back to the log channel.
        log.error("SMTP send to %s failed: %s: %s", to, type(exc).__name__, exc)
        log.warning("--- %s ---\n%s\n--- end ---", subject, body)
        return False


def _first_link(body: str) -> str:
    """The URL out of a message body, for the one-line log marker."""
    import re
    match = re.search(r"https?://\S+", body or "")
    return match.group(0) if match else ""


def send_password_reset(to: str, link: str, minutes: int) -> bool:
    return send(
        to,
        "Reset your jobauto password",
        f"""Someone asked to reset the jobauto password for this address.

Open this link to choose a new password:

  {link}

The link works once and expires in {minutes} minutes.

If this was not you, ignore this message -- nothing has changed, and the link
cannot be used to read your existing password.
""",
    )
