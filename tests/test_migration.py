"""Database migration between two servers. Uses two SQLite files, so the
copy/verify/resume logic is exercised without needing a Postgres pair."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jobauto.cloud.db import (Agent, Application, Base, CloudJob,  # noqa: E402
                              PasswordReset, Task, User)


def build_source(path: Path) -> dict[str, int]:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        user = User(email="a@b.com", password_hash="scrypt:32768:8:1$fake$hash",
                    preferences_yaml="search:\n  roles:\n    - title: X\n")
        s.add(user)
        s.commit()

        s.add(Agent(user_id=user.id, name="my-pc", token="tok-123"))
        s.add(PasswordReset(user_id=user.id, token_hash="h" * 64,
                            expires_at=__import__("datetime").datetime.now(
                                __import__("datetime").timezone.utc)))
        for i in range(12):
            s.add(CloudJob(user_id=user.id, fingerprint=f"fp{i}", portal="naukri",
                           title=f"Role {i}", company=f"Co {i}", url=f"u{i}",
                           score=float(90 - i), band="priority",
                           reasons_json=json.dumps(["title match"])))
        for i in range(4):
            s.add(Application(user_id=user.id, fingerprint=f"fp{i}",
                              portal="naukri", title=f"Role {i}",
                              company=f"Co {i}", url=f"u{i}",
                              status="submitted" if i < 2 else "prepared"))
        s.add(Task(user_id=user.id, kind="discover", status="done"))
        s.commit()
    engine.dispose()
    return {"users": 1, "agents": 1, "password_resets": 1,
            "jobs": 12, "applications": 4, "tasks": 1}


def counts(path: Path) -> dict[str, int]:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    out = {}
    with Session() as s:
        for model in (User, Agent, PasswordReset, CloudJob, Application, Task):
            out[model.__tablename__] = int(
                s.scalar(select(func.count()).select_from(model)) or 0)
    engine.dispose()
    return out


def run_migration(src: Path, dst: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_db.py"),
         "--from", f"sqlite:///{src}", "--to", f"sqlite:///{dst}", *extra],
        capture_output=True, text=True, cwd=ROOT)


def test_dry_run_changes_nothing(tmp_path):
    src, dst = tmp_path / "s.db", tmp_path / "d.db"
    build_source(src)
    res = run_migration(src, dst, "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "dry run" in res.stdout
    assert all(v == 0 for v in counts(dst).values())


def test_migration_copies_every_table(tmp_path):
    src, dst = tmp_path / "s.db", tmp_path / "d.db"
    expected = build_source(src)
    res = run_migration(src, dst)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Migration complete" in res.stdout
    assert counts(dst) == expected


def test_migration_preserves_ids_and_relationships(tmp_path):
    """Foreign keys are only valid if the primary keys survive the copy."""
    src, dst = tmp_path / "s.db", tmp_path / "d.db"
    build_source(src)
    run_migration(src, dst)

    engine = create_engine(f"sqlite:///{dst}", future=True)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        user = s.scalar(select(User))
        agent = s.scalar(select(Agent))
        jobs = s.scalars(select(CloudJob)).all()
        assert agent.user_id == user.id
        assert agent.token == "tok-123"            # agent keeps working
        assert user.password_hash.startswith("scrypt:")   # no re-login needed
        assert all(j.user_id == user.id for j in jobs)
        assert json.loads(jobs[0].reasons_json) == ["title match"]
    engine.dispose()


def test_application_statuses_survive(tmp_path):
    src, dst = tmp_path / "s.db", tmp_path / "d.db"
    build_source(src)
    run_migration(src, dst)

    engine = create_engine(f"sqlite:///{dst}", future=True)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        statuses = sorted(a.status for a in s.scalars(select(Application)).all())
    engine.dispose()
    assert statuses == ["prepared", "prepared", "submitted", "submitted"]


def test_rerunning_is_safe(tmp_path):
    """An interrupted migration must be resumable by just running it again."""
    src, dst = tmp_path / "s.db", tmp_path / "d.db"
    expected = build_source(src)

    run_migration(src, dst)
    second = run_migration(src, dst)

    assert second.returncode == 0
    assert counts(dst) == expected            # no duplicates
    body = second.stdout.split("copying...")[1].split("resetting")[0]
    assert all(line.split()[-2] == "0" for line in body.strip().splitlines())


def test_empty_source_is_reported_not_silently_ok(tmp_path):
    src, dst = tmp_path / "s.db", tmp_path / "d.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{src}", future=True))
    res = run_migration(src, dst)
    assert res.returncode == 1
    assert "source is empty" in res.stdout


def test_wipe_target_clears_before_copying(tmp_path):
    src, dst = tmp_path / "s.db", tmp_path / "d.db"
    expected = build_source(src)
    build_source(dst)                 # target already has a different account

    res = run_migration(src, dst, "--wipe-target")
    assert res.returncode == 0
    assert counts(dst) == expected


def test_unreachable_target_fails_loudly(tmp_path):
    src = tmp_path / "s.db"
    build_source(src)
    res = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_db.py"),
         "--from", f"sqlite:///{src}",
         "--to", "postgresql://u:p@127.0.0.1:1/nope"],
        capture_output=True, text=True, cwd=ROOT)
    assert res.returncode != 0
    assert "cannot connect to the target database" in res.stdout + res.stderr


# ================================================= connection URL diagnostics
def check_url(url: str):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_db_url.py"), url],
        capture_output=True, text=True, cwd=ROOT)


REF = "absysmpotwbawjtpjffu"
POOLER = f"aws-0-ap-south-1.pooler.supabase.com"


def test_good_session_pooler_url_passes():
    res = check_url(f"postgresql://postgres.{REF}:Secret123@{POOLER}:5432/postgres")
    assert res.returncode == 0
    assert "looks well-formed" in res.stdout
    assert "SESSION pooler" in res.stdout
    assert REF in res.stdout


def test_bare_postgres_user_on_pooler_is_caught():
    """The actual failure: 'password authentication failed for user postgres'
    means the username lost its project ref."""
    res = check_url(f"postgresql://postgres:Secret123@{POOLER}:5432/postgres")
    assert res.returncode == 1
    assert 'postgres.<project-ref>' in res.stdout
    assert "password authentication failed" in res.stdout


def test_direct_host_is_flagged_as_unreachable():
    res = check_url(f"postgresql://postgres:Secret123@db.{REF}.supabase.co:5432/postgres")
    assert res.returncode == 1
    assert "IPv6-only" in res.stdout
    assert f"postgres.{REF}" in res.stdout          # suggests the fix


def test_transaction_pooler_is_allowed_with_a_note():
    res = check_url(f"postgresql://postgres.{REF}:Secret123@{POOLER}:6543/postgres")
    assert res.returncode == 0
    assert "TRANSACTION pooler" in res.stdout


def test_placeholder_password_is_caught_not_crashed():
    """Supabase ships [YOUR-PASSWORD]; brackets make urlsplit raise on its own."""
    res = check_url(f"postgresql://postgres.{REF}:[YOUR-PASSWORD]@{POOLER}:5432/postgres")
    assert res.returncode == 1
    assert "placeholder" in res.stdout
    assert "Traceback" not in res.stdout + res.stderr


def test_password_with_at_sign_is_caught():
    res = check_url(f"postgresql://postgres.{REF}:pa@ssword@{POOLER}:5432/postgres")
    assert res.returncode == 1
    assert "more than one @" in res.stdout


def test_password_with_hash_is_caught():
    """A # truncates the URL silently, which is the nastiest of these."""
    res = check_url(f"postgresql://postgres.{REF}:pass#word@{POOLER}:5432/postgres")
    assert res.returncode == 1
    assert "#" in res.stdout and "fragment" in res.stdout


def test_password_is_never_printed():
    secret = "SuperSecret12345"
    res = check_url(f"postgresql://postgres.{REF}:{secret}@{POOLER}:5432/postgres")
    assert secret not in res.stdout
    assert "16 chars" in res.stdout


def test_sqlite_url_is_described_simply():
    res = check_url("sqlite:///cloud.db")
    assert res.returncode == 0
    assert "sqlite file" in res.stdout
