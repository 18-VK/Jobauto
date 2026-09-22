"""Core domain types.

`Job` is the normalised shape every portal adapter must produce. Portal-specific
raw text lives in `raw` so an adapter never has to lose information it could not
parse -- the scorer only ever reads the normalised fields.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class WorkMode(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class AppStatus(str, Enum):
    DISCOVERED = "discovered"      # seen in search results
    SCORED = "scored"              # run through the scorer
    SHORTLISTED = "shortlisted"    # above threshold, queued for review
    PREPARED = "prepared"          # form filled, waiting on you to submit
    SUBMITTED = "submitted"        # you clicked submit
    SKIPPED = "skipped"            # you declined at review
    FAILED = "failed"              # automation error
    EXTERNAL = "external"          # redirects off-portal, handed to you


@dataclass
class SalaryRange:
    min_lpa: float | None = None
    max_lpa: float | None = None
    disclosed: bool = False

    # "12-18 Lacs PA" / "₹8,00,000 - ₹12,00,000" / "Not disclosed"
    _RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)")
    _SINGLE = re.compile(r"(\d+(?:\.\d+)?)")

    @classmethod
    def parse(cls, text: str | None) -> SalaryRange:
        if not text:
            return cls()
        t = text.lower().replace(",", "")
        if "not disclosed" in t or "unpaid" in t or not any(c.isdigit() for c in t):
            return cls()

        # Normalise absolute rupee figures to lakhs before matching.
        scale = 1.0
        if "lac" in t or "lakh" in t or "lpa" in t:
            scale = 1.0
        elif re.search(r"\d{6,}", t):      # 800000 style
            scale = 1e-5

        m = cls._RANGE.search(t)
        if m:
            return cls(float(m.group(1)) * scale, float(m.group(2)) * scale, True)
        m = cls._SINGLE.search(t)
        if m:
            v = float(m.group(1)) * scale
            return cls(v, v, True)
        return cls()


@dataclass
class ExperienceRange:
    min_years: float | None = None
    max_years: float | None = None

    _RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)")
    _SINGLE = re.compile(r"(\d+(?:\.\d+)?)")

    @classmethod
    def parse(cls, text: str | None) -> ExperienceRange:
        if not text:
            return cls()
        t = text.lower()
        m = cls._RANGE.search(t)
        if m:
            return cls(float(m.group(1)), float(m.group(2)))
        m = cls._SINGLE.search(t)
        if m:
            v = float(m.group(1))
            # "5+ years" has no upper bound; "fresher" pins to zero.
            return cls(v, None if "+" in t else v)
        if "fresher" in t:
            return cls(0.0, 0.0)
        return cls()


@dataclass
class Job:
    """A normalised job posting. `fingerprint` is what dedupe keys on."""
    portal: str
    portal_job_id: str
    title: str
    company: str
    url: str

    location: str = ""
    work_mode: WorkMode = WorkMode.UNKNOWN
    salary: SalaryRange = field(default_factory=SalaryRange)
    experience: ExperienceRange = field(default_factory=ExperienceRange)
    posted_date: date | None = None
    summary: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)
    is_easy_apply: bool = False
    discovered_at: datetime = field(default_factory=datetime.now)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable cross-portal identity: same role at same company collapses
        into one row even when four portals list it."""
        norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
        title = re.sub(r"\b(sr|senior|jr|junior|lead|i{1,3}|\d+)\b", "",
                       (self.title or "").lower())
        city = (self.location or "").split(",")[0]
        return hashlib.sha256(
            f"{norm(title)}|{norm(self.company)}|{norm(city)}".encode()
        ).hexdigest()[:16]

    @property
    def age_days(self) -> int | None:
        if self.posted_date is None:
            return None
        return (date.today() - self.posted_date).days

    def text_blob(self) -> str:
        """Everything searchable, lowercased -- used for keyword matching.

        The normalised location field is part of the searchable blob so
        location-based preferences and blocked-location checks still work when
        the portal exposes the city/country only in the listing text rather than
        as a dedicated `location` field.
        """
        return " ".join([
            self.title, self.company, self.location, self.summary,
            self.description, " ".join(self.skills),
        ]).lower()


@dataclass
class ScoreBreakdown:
    """Kept per-component so the digest can explain *why* a job ranked where
    it did. An opaque number nobody trusts gets ignored."""
    total: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    dropped: bool = False
    drop_reason: str = ""


@dataclass
class Application:
    job: Job
    score: ScoreBreakdown
    status: AppStatus = AppStatus.DISCOVERED
    resume_path: str = ""
    cover_letter: str = ""
    answered: dict[str, str] = field(default_factory=dict)
    escalated: list[str] = field(default_factory=list)   # questions needing you
    error: str = ""
    prepared_at: datetime | None = None
    submitted_at: datetime | None = None
