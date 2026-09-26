"""A fresh start, both halves.

Cloud: the data the runs produced goes; the account, the agent link and the
preferences stay unless asked. PC: the run history goes; the portal logins
and the dashboard link stay unless asked. Nothing is removed without --yes.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "reset_cloud", ROOT / "scripts" / "reset_cloud.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _populate(client):
    """An account with a linked agent, some jobs, an application, a task,
    a schedule that has run, and edited preferences."""
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": f"f{i}", "portal": "naukri", "title": f"Job {i}",
         "company": "Acme", "url": f"https://x/{i}", "score": 80.0}
        for i in range(3)]})
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "f0", "portal": "naukri", "title": "Job 0", "company": "Acme",
         "url": "https://x/0", "status": "prepared"}]})
    client.post("/api/tasks", json={"kind": "discover"})
    prefs = client.get("/api/preferences").get_json()["yaml"]
    client.post("/api/preferences", json={"yaml": prefs + "\n# mine\n"})
    return tok


def _url(monkeypatch) -> str:
    import os
    return f"sqlite:///{os.environ['JOBAUTO_CLOUD_DB']}"


# ------------------------------------------------------------- the cloud
def test_a_dry_run_counts_and_removes_nothing(client, monkeypatch):
    _populate(client)
    said: list[str] = []
    before = _script().reset(_url(monkeypatch), yes=False, say=said.append)
    assert before["jobs"] == 3 and before["applications"] == 1 and before["tasks"] == 1
    assert any("dry run" in s for s in said)
    # include_applied: one of the three has an application, and the jobs
    # list hides those by design -- the rows are all still there.
    assert len(client.get("/api/jobs?include_applied=1").get_json()["jobs"]) == 3


def test_the_default_keeps_the_account_link_and_preferences(client, monkeypatch):
    tok = _populate(client)
    _script().reset(_url(monkeypatch), yes=True, say=lambda *_: None)

    assert client.get("/api/jobs").get_json()["jobs"] == []
    assert client.get("/api/applications").get_json()["applications"] == []
    assert client.get("/api/tasks").get_json()["tasks"] == []
    # still signed in, still linked, preferences still mine
    assert client.get("/api/summary").status_code == 200
    assert client.get("/api/agent/work", headers=H(tok)).status_code == 200
    assert "# mine" in client.get("/api/preferences").get_json()["yaml"]


def test_the_schedule_fires_again_rather_than_waiting_for_tomorrow(client, monkeypatch):
    from sqlalchemy import select
    from jobauto.cloud.db import User, session, utcnow

    _populate(client)
    with session() as s:
        user = s.scalar(select(User))
        user.schedule_last_run = utcnow()
        s.commit()
    _script().reset(_url(monkeypatch), yes=True, say=lambda *_: None)
    from jobauto.cloud import db as clouddb
    with clouddb.session() as s:
        assert s.scalar(select(User)).schedule_last_run is None


def test_preferences_can_be_put_back_to_the_defaults(client, monkeypatch):
    tok = _populate(client)
    _script().reset(_url(monkeypatch), yes=True, reset_preferences=True,
                    say=lambda *_: None)
    synced = client.get("/api/agent/preferences", headers=H(tok)).get_json()["yaml"]
    assert "# mine" not in synced
    assert "search:" in synced, "the shipped defaults were regenerated"


def test_everything_removes_the_accounts_and_reopens_signup(client, monkeypatch):
    tok = _populate(client)
    _script().reset(_url(monkeypatch), yes=True, everything=True, say=lambda *_: None)
    assert client.get("/api/agent/work", headers=H(tok)).status_code == 401
    client.get("/logout")
    assert signup(client, email="new@b.com").status_code == 302, \
        "with no users left, the first signup must be open again"


def test_the_password_in_the_url_is_never_printed(client, monkeypatch):
    _populate(client)
    said: list[str] = []
    _script().reset("sqlite:///" + __import__("os").environ["JOBAUTO_CLOUD_DB"],
                    yes=False, say=said.append)
    mod = _script()
    assert "s3cret" not in mod._redact("postgresql+psycopg://u:s3cret@host/db")
    assert mod._redact("postgresql+psycopg://u:s3cret@host/db").endswith("@host/db")


# ---------------------------------------------------------------- the PC
def _local(tmp_path, monkeypatch):
    from jobauto import cli, config as config_mod

    data = tmp_path / "data"
    cfg = tmp_path / "config"
    (data / "browser" / "naukri").mkdir(parents=True)
    (data / "debug").mkdir()
    cfg.mkdir()
    for f in ("jobauto.db", "agent.json", "sessions.json"):
        (data / f).write_text("x", encoding="utf-8")
    (data / "browser" / "naukri" / "Cookies").write_text("x", encoding="utf-8")
    (data / "debug" / "hirist.html").write_text("x", encoding="utf-8")
    (cfg / "preferences.local.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(data))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg)
    return cli, data, cfg


def test_local_dry_run_removes_nothing(tmp_path, monkeypatch, capsys):
    cli, data, cfg = _local(tmp_path, monkeypatch)
    assert cli.cmd_reset(argparse.Namespace(yes=False, logins=False, unlink=False)) == 0
    assert (data / "jobauto.db").exists()
    assert "dry run" in capsys.readouterr().out


def test_local_reset_keeps_logins_and_the_link(tmp_path, monkeypatch):
    cli, data, cfg = _local(tmp_path, monkeypatch)
    assert cli.cmd_reset(argparse.Namespace(yes=True, logins=False, unlink=False)) == 0
    assert not (data / "jobauto.db").exists()
    assert not (data / "debug").exists()
    assert not (cfg / "preferences.local.yaml").exists()
    assert not (data / "sessions.json").exists()
    assert (data / "browser" / "naukri" / "Cookies").exists(), "logins take a person to redo"
    assert (data / "agent.json").exists(), "the link takes a token to redo"


def test_local_reset_can_take_the_logins_and_the_link_too(tmp_path, monkeypatch, capsys):
    cli, data, cfg = _local(tmp_path, monkeypatch)
    assert cli.cmd_reset(argparse.Namespace(yes=True, logins=True, unlink=True)) == 0
    assert not (data / "browser").exists()
    assert not (data / "agent.json").exists()
    out = capsys.readouterr().out
    assert "login" in out and "link" in out


def test_a_clean_pc_says_so(tmp_path, monkeypatch, capsys):
    from jobauto import cli, config as config_mod
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "cfg")
    assert cli.cmd_reset(argparse.Namespace(yes=True, logins=False, unlink=False)) == 0
    assert "already clean" in capsys.readouterr().out
