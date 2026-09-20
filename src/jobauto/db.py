"""SQLite tracker.

Holds every job ever seen and every application ever made. Two jobs it answers
that matter operationally:
  - have I already applied to this, or to this company recently?
  - how many applications have I made on this portal today? (daily caps)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import data_dir
from .models import AppStatus, Job, ScoreBreakdown

# A job that reached any of these is finished with and must never be reopened:
# it went through, it is waiting on you, you declined it, or it genuinely lives
# on an employer site. Leaving `external` out of this list is what put the same
# job back in the shortlist on every single run.
TERMINAL_STATUSES = ("submitted", "prepared", "skipped", "external")

# Narrower: "there is already an application on this job". `skipped` is absent
# on purpose -- you declined it, so it is allowed back after the cooldown.
APPLIED_STATUSES = ("submitted", "prepared", "external")

# `failed` is deliberately NOT terminal. A failure is usually ours -- a stale
# selector, an expired session -- and making it permanent would mean a portal
# redesign silently deletes every job from the shortlist for good. Retry, but
# once a day rather than on every run.
RETRY_FAILED_AFTER_HOURS = 24

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint     TEXT PRIMARY KEY,
    portal          TEXT NOT NULL,
    portal_job_id   TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    url             TEXT NOT NULL,
    location        TEXT,
    work_mode       TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_known    INTEGER DEFAULT 0,
    exp_min         REAL,
    exp_max         REAL,
    posted_date     TEXT,
    summary         TEXT,
    description     TEXT,
    skills          TEXT,
    is_easy_apply   INTEGER DEFAULT 0,
    discovered_at   TEXT NOT NULL,
    raw             TEXT
);

-- One job can be seen on several portals; this records each sighting so the
-- digest can say "also on LinkedIn" and so we apply on the best one.
CREATE TABLE IF NOT EXISTS sightings (
    fingerprint     TEXT NOT NULL,
    portal          TEXT NOT NULL,
    portal_job_id   TEXT NOT NULL,
    url             TEXT NOT NULL,
    seen_at         TEXT NOT NULL,
    PRIMARY KEY (fingerprint, portal, portal_job_id)
);

CREATE TABLE IF NOT EXISTS scores (
    fingerprint     TEXT PRIMARY KEY,
    total           REAL NOT NULL,
    band            TEXT,
    components      TEXT,
    penalties       TEXT,
    reasons         TEXT,
    dropped         INTEGER DEFAULT 0,
    drop_reason     TEXT,
    scored_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL,
    portal          TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    status          TEXT NOT NULL,
    resume_path     TEXT,
    cover_letter    TEXT,
    answered        TEXT,
    escalated       TEXT,
    error           TEXT,
    prepared_at     TEXT,
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    command         TEXT,
    portals         TEXT,
    found           INTEGER DEFAULT 0,
    shortlisted     INTEGER DEFAULT 0,
    prepared        INTEGER DEFAULT 0,
    submitted       INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_app_status    ON applications(status);
CREATE INDEX IF NOT EXISTS idx_app_company   ON applications(company);
CREATE INDEX IF NOT EXISTS idx_app_submitted ON applications(submitted_at);
CREATE INDEX IF NOT EXISTS idx_scores_total  ON scores(total);
"""


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "jobauto.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Repair rows written before apply outcomes were classified properly.

        Everything that was not an instant apply used to be filed as
        `external`, including our own failures to drive the page -- a stale
        selector, a modal that never opened. Now that `external` is terminal
        those rows retire the job for good and leave an empty shortlist. A
        genuine redirect always says "apply by hand"; nothing else did.
        """
        stale = (datetime.now()
                 - timedelta(hours=RETRY_FAILED_AFTER_HOURS + 1)
                 ).isoformat(timespec="seconds")
        with self.tx() as c:
            c.execute(
                """UPDATE applications
                      SET status = 'failed', updated_at = ?
                    WHERE status = 'external'
                      AND COALESCE(error, '') NOT LIKE '%apply by hand%'""",
                (stale,))

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------ jobs
    def upsert_job(self, job: Job) -> str:
        fp = job.fingerprint
        now = datetime.now().isoformat(timespec="seconds")
        with self.tx() as c:
            c.execute(
                """INSERT INTO jobs (fingerprint, portal, portal_job_id, title,
                       company, url, location, work_mode, salary_min, salary_max,
                       salary_known, exp_min, exp_max, posted_date, summary,
                       description, skills, is_easy_apply, discovered_at, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       description = COALESCE(NULLIF(excluded.description, ''), jobs.description),
                       summary     = COALESCE(NULLIF(excluded.summary, ''), jobs.summary),
                       skills      = COALESCE(NULLIF(excluded.skills, '[]'), jobs.skills)""",
                (fp, job.portal, job.portal_job_id, job.title, job.company, job.url,
                 job.location, job.work_mode.value, job.salary.min_lpa, job.salary.max_lpa,
                 int(job.salary.disclosed), job.experience.min_years, job.experience.max_years,
                 job.posted_date.isoformat() if job.posted_date else None,
                 job.summary, job.description, json.dumps(job.skills),
                 int(job.is_easy_apply), now, json.dumps(job.raw, default=str)),
            )
            c.execute(
                """INSERT OR REPLACE INTO sightings
                       (fingerprint, portal, portal_job_id, url, seen_at)
                   VALUES (?,?,?,?,?)""",
                (fp, job.portal, job.portal_job_id, job.url, now),
            )
        return fp

    def job_exists(self, fingerprint: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM jobs WHERE fingerprint = ?", (fingerprint,))
        return cur.fetchone() is not None

    def sightings(self, fingerprint: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sightings WHERE fingerprint = ?", (fingerprint,)).fetchall()

    # ---------------------------------------------------------- scores
    def save_score(self, fingerprint: str, score: ScoreBreakdown, band: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO scores (fingerprint, total, band,
                       components, penalties, reasons, dropped, drop_reason, scored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (fingerprint, score.total, band, json.dumps(score.components),
                 json.dumps(score.penalties), json.dumps(score.reasons),
                 int(score.dropped), score.drop_reason,
                 datetime.now().isoformat(timespec="seconds")),
            )

    def shortlist(self, min_score: float = 0, limit: int = 50,
                  exclude_applied: bool = True) -> list[sqlite3.Row]:
        sql = """
            SELECT j.*, s.total, s.band, s.reasons, s.components
            FROM jobs j JOIN scores s ON s.fingerprint = j.fingerprint
            WHERE s.dropped = 0 AND s.total >= ?
        """
        params: list[Any] = [min_score]
        if exclude_applied:
            marks = ",".join("?" for _ in TERMINAL_STATUSES)
            retry_cutoff = (datetime.now()
                            - timedelta(hours=RETRY_FAILED_AFTER_HOURS)).isoformat()
            sql += f""" AND j.fingerprint NOT IN (
                         SELECT fingerprint FROM applications
                         WHERE status IN ({marks}))
                    AND j.fingerprint NOT IN (
                         SELECT fingerprint FROM applications
                         WHERE status = 'failed' AND updated_at >= ?)"""
            params.extend(TERMINAL_STATUSES)
            params.append(retry_cutoff)
        sql += " ORDER BY s.total DESC LIMIT ?"
        params.append(limit)
        return self._conn.execute(sql, params).fetchall()

    # --------------------------------------------------- applications
    def record_application(self, job: Job, status: AppStatus, *,
                           resume_path: str = "", cover_letter: str = "",
                           answered: dict[str, str] | None = None,
                           escalated: list[str] | None = None,
                           error: str = "") -> int:
        now = datetime.now().isoformat(timespec="seconds")
        prepared = now if status in (AppStatus.PREPARED, AppStatus.SUBMITTED) else None
        submitted = now if status == AppStatus.SUBMITTED else None
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO applications (fingerprint, portal, company, title,
                       url, status, resume_path, cover_letter, answered, escalated,
                       error, prepared_at, submitted_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.fingerprint, job.portal, job.company, job.title, job.url,
                 status.value, resume_path, cover_letter,
                 json.dumps(answered or {}), json.dumps(escalated or []),
                 error, prepared, submitted, now),
            )
            return int(cur.lastrowid)

    def set_status(self, app_id: int, status: AppStatus, error: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.tx() as c:
            c.execute(
                """UPDATE applications
                      SET status = ?, error = ?, updated_at = ?,
                          submitted_at = CASE WHEN ? = 'submitted'
                                              THEN ? ELSE submitted_at END
                    WHERE id = ?""",
                (status.value, error, now, status.value, now, app_id),
            )

    def already_applied(self, fingerprint: str, cooldown_days: int = 3650) -> bool:
        cutoff = (datetime.now() - timedelta(days=cooldown_days)).isoformat()
        marks = ",".join("?" for _ in APPLIED_STATUSES)
        cur = self._conn.execute(
            f"""SELECT 1 FROM applications
                 WHERE fingerprint = ? AND status IN ({marks})
                   AND updated_at >= ?""",
            (fingerprint, *APPLIED_STATUSES, cutoff))
        return cur.fetchone() is not None

    def application_status(self, fingerprint: str) -> str | None:
        """The most recent outcome for this job, or None if never attempted."""
        row = self._conn.execute(
            """SELECT status FROM applications WHERE fingerprint = ?
                ORDER BY updated_at DESC LIMIT 1""", (fingerprint,)).fetchone()
        return row["status"] if row else None

    def companies_applied_since(self, days: int) -> set[str]:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """SELECT DISTINCT LOWER(company) AS c FROM applications
                WHERE status = 'submitted' AND submitted_at >= ?""",
            (cutoff,)).fetchall()
        return {r["c"] for r in rows if r["c"]}

    def count_today(self, portal: str,
                    statuses: tuple[str, ...] = ("submitted", "prepared")) -> int:
        today = date.today().isoformat()
        marks = ",".join("?" for _ in statuses)
        cur = self._conn.execute(
            f"""SELECT COUNT(*) AS n FROM applications
                 WHERE portal = ? AND status IN ({marks})
                   AND DATE(updated_at) = ?""",
            (portal, *statuses, today))
        return int(cur.fetchone()["n"])

    def pending_review(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT a.*, s.total FROM applications a
               LEFT JOIN scores s ON s.fingerprint = a.fingerprint
               WHERE a.status = 'prepared' ORDER BY s.total DESC""").fetchall()

    # ------------------------------------------------------------- runs
    def start_run(self, command: str, portals: list[str]) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs (started_at, command, portals) VALUES (?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), command,
                 ",".join(portals)))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, **counts: Any) -> None:
        fields = ", ".join(f"{k} = ?" for k in counts)
        with self.tx() as c:
            c.execute(
                f"UPDATE runs SET finished_at = ?{', ' + fields if fields else ''} "
                f"WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), *counts.values(), run_id))

    def shortlist_breakdown(self, min_score: float) -> dict[str, int]:
        """"Nothing shortlisted" has three very different causes and only one
        of them is "run discover". Say which."""
        q = lambda sql, *a: int(self._conn.execute(sql, a).fetchone()[0])
        marks = ",".join("?" for _ in TERMINAL_STATUSES)
        cutoff = (datetime.now()
                  - timedelta(hours=RETRY_FAILED_AFTER_HOURS)).isoformat()
        return {
            "scored": q("SELECT COUNT(*) FROM scores WHERE dropped = 0"),
            "below_threshold": q(
                "SELECT COUNT(*) FROM scores WHERE dropped = 0 AND total < ?",
                min_score),
            "already_handled": q(
                f"""SELECT COUNT(DISTINCT fingerprint) FROM applications
                     WHERE status IN ({marks})""", *TERMINAL_STATUSES),
            "recently_failed": q(
                """SELECT COUNT(DISTINCT fingerprint) FROM applications
                    WHERE status = 'failed' AND updated_at >= ?""", cutoff),
        }

    def stats(self) -> dict[str, Any]:
        q = lambda sql, *a: self._conn.execute(sql, a).fetchone()[0]
        return {
            "jobs_seen": q("SELECT COUNT(*) FROM jobs"),
            "scored": q("SELECT COUNT(*) FROM scores"),
            "dropped": q("SELECT COUNT(*) FROM scores WHERE dropped = 1"),
            "submitted": q("SELECT COUNT(*) FROM applications WHERE status='submitted'"),
            "prepared": q("SELECT COUNT(*) FROM applications WHERE status='prepared'"),
            "skipped": q("SELECT COUNT(*) FROM applications WHERE status='skipped'"),
            "external": q("SELECT COUNT(*) FROM applications WHERE status='external'"),
            "failed": q("SELECT COUNT(*) FROM applications WHERE status='failed'"),
            "companies": q("SELECT COUNT(DISTINCT company) FROM applications "
                           "WHERE status='submitted'"),
        }
