"""A bot check must outlive the run that hit it.

Stopping a portal "for this run" is no protection at all when the run is on a
daily schedule: the next one walks straight back into the same challenge, and
repeatedly failing challenges is what turns a soft check into a hard block.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from jobauto.config import PortalConfig
from jobauto.db import Database
from jobauto.pipeline import Pipeline, _cooling_note
from .test_parallel_discover import make_config


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


# ------------------------------------------------------------- persistence
def test_challenge_is_remembered_after_the_run_ends(db):
    db.note_challenge("indeed", "served a bot check")
    assert db.cooling_until("indeed") is not None


def test_an_untouched_portal_is_not_cooling_off(db):
    assert db.cooling_until("naukri") is None


def test_repeated_challenges_back_off_further_each_time(db):
    """The second bot check in a row means the first cool-off was not enough."""
    first = db.note_challenge("indeed", "")
    second = db.note_challenge("indeed", "")
    third = db.note_challenge("indeed", "")
    assert first < second < third


def test_backoff_stops_growing_at_the_longest_step(db):
    """Escalation must not run off the end of COOL_OFF_HOURS."""
    for _ in range(10):
        until = db.note_challenge("indeed", "")
    longest = datetime.now() + timedelta(hours=db.COOL_OFF_HOURS[-1])
    assert until <= longest + timedelta(minutes=1)


def test_expired_cooldown_lets_the_portal_through(db):
    db.note_challenge("indeed", "")
    with db.tx() as c:
        c.execute("UPDATE portal_state SET cooling_until = ? WHERE portal = ?",
                  ((datetime.now() - timedelta(hours=1)).isoformat(), "indeed"))
    assert db.cooling_until("indeed") is None


def test_expiry_does_not_forget_the_strikes(db):
    """Otherwise every check would restart at the shortest cool-off and the
    escalation would never actually escalate."""
    db.note_challenge("indeed", "")
    with db.tx() as c:
        c.execute("UPDATE portal_state SET cooling_until = ? WHERE portal = ?",
                  ((datetime.now() - timedelta(hours=1)).isoformat(), "indeed"))
    assert db.cooling_until("indeed") is None
    second = db.note_challenge("indeed", "")
    expected = datetime.now() + timedelta(hours=db.COOL_OFF_HOURS[1])
    assert second > expected - timedelta(minutes=1)


def test_a_clean_run_clears_the_record(db):
    db.note_challenge("indeed", "")
    db.clear_challenge("indeed")
    assert db.cooling_until("indeed") is None
    assert db.portal_states() == []


def test_cooldown_is_per_portal(db):
    db.note_challenge("indeed", "")
    assert db.cooling_until("linkedin") is None


def test_corrupt_timestamp_does_not_park_a_portal_forever(db):
    """A row we cannot parse must fail open, not lock the portal out."""
    db.note_challenge("indeed", "")
    with db.tx() as c:
        c.execute("UPDATE portal_state SET cooling_until = 'nonsense'")
    assert db.cooling_until("indeed") is None


# ------------------------------------------------------------ wiring
def _pipeline(db, lines, portal_ids):
    cfg = make_config()
    cfg.portals = {
        pid: PortalConfig(id=pid, name=pid.title(), enabled=True,
                          base_url="", adapter="x:Y")
        for pid in portal_ids
    }
    return Pipeline(cfg, db, log=lines.append)


def test_discover_does_not_open_a_browser_for_a_parked_portal(db, monkeypatch):
    """Opening the browser at all is what the portal counts against us, so the
    check has to happen before the session, not inside the adapter."""
    db.note_challenge("indeed", "bot check")
    opened = []

    def spy(portal, roles, headless):
        opened.append(portal.id)
        return portal.id, [], "", False

    lines = []
    pipe = _pipeline(db, lines, ["indeed", "naukri"])
    monkeypatch.setattr(pipe, "_search_portal", spy)
    pipe.discover(parallel=False)

    assert "indeed" not in opened
    assert "naukri" in opened


def test_a_challenge_during_discover_parks_the_portal(db, monkeypatch):
    lines = []
    pipe = _pipeline(db, lines, ["indeed"])
    monkeypatch.setattr(pipe, "_search_portal",
                        lambda p, r, h: (p.id, [], "bot check", True))
    pipe.discover(parallel=False)
    assert db.cooling_until("indeed") is not None


def test_an_empty_result_does_not_park_the_portal(db, monkeypatch):
    """Stale selectors are not a bot check; parking on them would hide a bug
    behind a six hour silence."""
    lines = []
    pipe = _pipeline(db, lines, ["hirist"])
    monkeypatch.setattr(pipe, "_search_portal",
                        lambda p, r, h: (p.id, [], "selector is stale", False))
    pipe.discover(parallel=False)
    assert db.cooling_until("hirist") is None


def test_skipping_a_parked_portal_says_when_it_returns(db):
    """Silence here reads as a broken install."""
    db.note_challenge("indeed", "bot check")
    lines = []
    pipe = _pipeline(db, lines, ["indeed"])
    pipe.discover(parallel=False)
    text = " ".join(lines).lower()
    assert "bot check" in text and "trying again in" in text


# ------------------------------------------------------------ wording
def test_cooling_note_gives_both_a_duration_and_a_clock_time():
    """"in 5h" answers "is it broken"; "at 14:30" answers "when do I retry"."""
    until = datetime.now() + timedelta(hours=5, minutes=30)
    note = _cooling_note("Indeed", until)
    assert "5h" in note
    assert until.strftime("%H:%M") in note


def test_cooling_note_reads_as_minutes_under_an_hour():
    note = _cooling_note("Indeed", datetime.now() + timedelta(minutes=20))
    assert "20m" in note and "0h" not in note


def test_cooling_note_never_shows_negative_time():
    note = _cooling_note("Indeed", datetime.now() - timedelta(hours=3))
    assert "in 0m" in note
    assert not re.search(r"-\d", note)


# --------------------------------------------------- browser identity
def _options(tmp=None):
    from jobauto.browser import BrowserSession
    portal = PortalConfig(id="indeed", name="Indeed", enabled=True,
                          base_url="", adapter="x:Y")
    return BrowserSession(portal, make_config())._launch_options()


def test_browser_does_not_claim_a_user_agent_it_cannot_back_up():
    """Playwright's user_agent option rewrites navigator.userAgent and the
    User-Agent header, but not the sec-ch-ua client hints, which keep
    reporting the browser's real version. A pinned string therefore claims one
    Chrome version in one header and a different one in the next -- something
    no real browser does, and cheap for a bot check to spot."""
    assert "user_agent" not in _options()


def test_user_agent_can_still_be_forced(monkeypatch):
    """An escape hatch for debugging, off by default."""
    monkeypatch.setenv("JOBAUTO_USER_AGENT", "Mozilla/5.0 Custom")
    assert _options()["user_agent"] == "Mozilla/5.0 Custom"
