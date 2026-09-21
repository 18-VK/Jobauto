"""Self-hosted agent installer.

The point of these endpoints is that a fresh machine needs no repo, no PyPI and
no public mirror -- just the deployment the person already has.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from jobauto.cloud import db as clouddb
from jobauto.cloud import installer


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "i.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("JOBAUTO_HTTPS", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    clouddb.reset_engine()
    from jobauto.cloud.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    clouddb.reset_engine()


# ------------------------------------------------------------- the zip
def zip_names() -> list[str]:
    return zipfile.ZipFile(io.BytesIO(installer.build_agent_zip())).namelist()


def test_zip_has_what_an_install_needs():
    names = set(zip_names())
    for required in (
        "jobauto-agent/pyproject.toml",
        "jobauto-agent/src/jobauto/cli.py",
        "jobauto-agent/src/jobauto/agent/runner.py",
        "jobauto-agent/src/jobauto/pipeline.py",
        "jobauto-agent/src/jobauto/browser.py",
        "jobauto-agent/src/jobauto/scoring.py",
    ):
        assert required in names, required


def test_zip_ships_the_portal_selectors():
    """Without these a fresh install has nothing to search."""
    names = set(zip_names())
    for portal in ("naukri", "linkedin", "indeed", "instahyre", "hirist"):
        assert f"jobauto-agent/src/jobauto/defaults/portals/{portal}.yaml" in names
    assert "jobauto-agent/src/jobauto/defaults/preferences.yaml" in names
    assert "jobauto-agent/src/jobauto/defaults/profile.yaml" in names


def test_zip_excludes_the_server_halves():
    """Cloud and web modules are useless on an agent machine and would drag in
    dependencies it does not need."""
    names = zip_names()
    assert not [n for n in names if "/cloud/" in n]
    assert not [n for n in names if "/web/" in n]
    assert not [n for n in names if "__pycache__" in n]


def test_zip_declares_the_console_entry_point():
    """This is what removes the need for PYTHONPATH and `python -m`."""
    archive = zipfile.ZipFile(io.BytesIO(installer.build_agent_zip()))
    pyproject = archive.read("jobauto-agent/pyproject.toml").decode()
    assert "jobauto = \"jobauto.cli:main\"" in pyproject
    assert "playwright" in pyproject


def test_zip_is_small_enough_to_download_quickly():
    assert len(installer.build_agent_zip()) < 1_000_000


# ------------------------------------------------------- the ps1 script
def test_installer_bakes_in_the_site_url():
    script = installer.installer_script("https://jobauto-abc.onrender.com/")
    assert "__BASE_URL__" not in script
    assert "$Base = 'https://jobauto-abc.onrender.com'" in script
    assert "$Base/agent.zip" in script


def test_installer_checks_for_python_and_says_how_to_get_it():
    script = installer.installer_script("https://x.example")
    assert "Python 3.10 or newer" in script
    assert "python.org/downloads" in script


def test_installer_uses_an_isolated_environment():
    """It must not install into whatever Python happens to be on PATH."""
    script = installer.installer_script("https://x.example")
    assert "-m venv" in script
    assert ".jobauto" in script


def test_installer_prompts_for_the_token_rather_than_embedding_it():
    """A token in the URL would end up in shell history and in any logs."""
    script = installer.installer_script("https://x.example")
    assert "Read-Host" in script
    assert "token" in script.lower()


def test_installer_installs_a_browser():
    script = installer.installer_script("https://x.example")
    assert "playwright install chromium" in script


# --------------------------------------------------------------- routes
def test_endpoints_are_public(client):
    """The installer runs before anyone has signed in, so these cannot require
    a session -- and neither carries a secret."""
    assert client.get("/install.ps1").status_code == 200
    assert client.get("/agent.zip").status_code == 200


def test_served_script_uses_the_requesting_host(client):
    body = client.get("/install.ps1", base_url="https://myjobs.example.com").get_data(as_text=True)
    assert "$Base = 'https://myjobs.example.com'" in body


def test_served_zip_is_a_valid_archive(client):
    res = client.get("/agent.zip")
    assert res.headers["Content-Type"] == "application/zip"
    assert "jobauto-agent.zip" in res.headers["Content-Disposition"]
    archive = zipfile.ZipFile(io.BytesIO(res.get_data()))
    assert archive.testzip() is None
    assert "jobauto-agent/pyproject.toml" in archive.namelist()


def test_script_is_served_as_text_so_iex_can_run_it(client):
    res = client.get("/install.ps1")
    assert res.headers["Content-Type"].startswith("text/plain")


# ------------------------------------------------------ invocation hint
@pytest.mark.parametrize("argv0,expected", [
    (r"C:\Users\x\.jobauto\venv\Scripts\jobauto.exe", "jobauto"),
    ("/usr/local/bin/jobauto", "jobauto"),
    (r"D:\repo\src\jobauto\__main__.py", "python -m jobauto"),
    ("-m", "python -m jobauto"),
])
def test_hints_match_how_it_was_launched(monkeypatch, argv0, expected):
    """Telling an installed user to run `python -m jobauto` sends them down a
    path that does not work for them."""
    import sys
    from jobauto.cli import invocation

    monkeypatch.setattr(sys, "argv", [argv0])
    assert invocation() == expected


# ------------------------------------------- failure handling in the script
def test_installer_never_calls_bare_exit():
    """Run through `irm ... | iex` the script shares the window's session, so
    `exit` closes the window -- taking the error with it, at exactly the moment
    you need to read it."""
    import re
    script = installer.installer_script("https://x.example")
    bare = [l for l in script.splitlines() if re.match(r"^\s*exit \d", l)]
    assert not bare, bare
    assert "function Finish" in script
    assert "Press Enter to close" in script


def test_installer_passes_the_token_with_an_equals_sign():
    """A token starting with '-' is read as an option name when passed as
    `--token VALUE`; `--token=VALUE` is unambiguous."""
    script = installer.installer_script("https://x.example")
    assert '--token="$token"' in script
    assert "--token $token" not in script


def test_installer_validates_the_token_before_linking():
    script = installer.installer_script("https://x.example")
    assert "/api/agent/hello" in script
    assert "401" in script
    assert "sign up first" in script


def test_generated_tokens_never_start_with_a_hyphen():
    """About one token in sixty otherwise would, and that one breaks any
    caller that passes it as a separate argument."""
    from jobauto.cloud.db import Agent
    tokens = [Agent.new_token() for _ in range(2000)]
    assert not any(t.startswith("-") for t in tokens)
    assert all(len(t) > 30 for t in tokens)


@pytest.mark.parametrize("token", ["-leadingdash123", "_leadingunderscore1", "normaltoken123"])
def test_cli_accepts_any_token_via_equals_form(token):
    from jobauto.cli import build_parser
    args = build_parser().parse_args(["link", "--url=https://x", f"--token={token}"])
    assert args.token == token
