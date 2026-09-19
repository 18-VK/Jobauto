"""Scoring and parsing tests. No browser, no network."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from jobauto.models import ExperienceRange, Job, SalaryRange
from jobauto.scoring import Scorer

PREFS = {
    "search": {
        "roles": [
            {"title": "Backend Developer", "weight": 1.0,
             "aliases": ["Backend Engineer"]},
            {"title": "Full Stack Developer", "weight": 0.8, "aliases": []},
        ],
        "keywords": {"include": ["REST API"], "exclude": ["intern", "bpo"]},
        "experience": {"min_years": 2, "max_years": 6, "current_years": 3.5},
        "locations": {"preferred": ["Noida", "Remote"], "acceptable": ["Pune"],
                      "blocked": ["Chennai"], "work_mode": ["remote", "hybrid"],
                      "relocate": False},
        "compensation": {"expected_ctc_lpa": 14.0, "minimum_acceptable_lpa": 11.0},
        "company": {"blocked": ["BadCorp"], "preferred": [],
                    "exclude_staffing_agencies": False},
        "posting": {"max_age_days": 21, "require_salary_disclosed": False},
    },
    "scoring": {
        "weights": {"title_match": 0.30, "skill_overlap": 0.25,
                    "experience_fit": 0.15, "location_fit": 0.15,
                    "compensation_fit": 0.10, "company_quality": 0.05},
        "penalties": {"stale_posting": 10, "no_salary_disclosed": 3,
                      "overqualified": 8, "already_applied_company": 15},
    },
    "thresholds": {"shortlist": 60, "auto_tailor": 70, "priority": 85},
}

PROFILE = {
    "skills": {
        "primary": [{"name": "C#", "level": 5}, {"name": ".NET Core", "level": 5},
                    {"name": "SQL Server", "level": 4}],
        "secondary": [{"name": "React", "level": 3}, {"name": "Docker", "level": 2}],
    }
}


@pytest.fixture
def scorer() -> Scorer:
    return Scorer(PREFS, PROFILE)


def make_job(**kw) -> Job:
    base = dict(portal="naukri", portal_job_id="1", title="Backend Developer",
                company="Acme", url="https://x/1", location="Noida")
    base.update(kw)
    salary = base.pop("salary", None)
    experience = base.pop("experience", None)
    job = Job(**base)
    if salary is not None:
        job.salary = SalaryRange.parse(salary)
    if experience is not None:
        job.experience = ExperienceRange.parse(experience)
    return job


# ------------------------------------------------------------- hard filters
def test_excluded_keyword_drops_job(scorer):
    job = make_job(title="Backend Developer Intern")
    result = scorer.score(job)
    assert result.dropped
    assert "intern" in result.drop_reason


def test_blocked_company_drops_job(scorer):
    result = scorer.score(make_job(company="BadCorp Pvt Ltd"))
    assert result.dropped
    assert "BadCorp" in result.drop_reason


def test_blocked_location_drops_job(scorer):
    result = scorer.score(make_job(location="Chennai"))
    assert result.dropped


def test_stale_posting_drops_job(scorer):
    job = make_job()
    job.posted_date = date.today() - timedelta(days=40)
    result = scorer.score(job)
    assert result.dropped
    assert "40d old" in result.drop_reason


def test_far_too_senior_drops_job(scorer):
    """You have 3.5y; a 10y minimum is not a near miss."""
    result = scorer.score(make_job(experience="10-15 years"))
    assert result.dropped
    assert "minimum" in result.drop_reason


# ----------------------------------------------------------------- scoring
def test_strong_match_scores_high(scorer):
    job = make_job(
        title="Backend Developer",
        location="Noida",
        salary="12-18 Lacs PA",
        experience="3-6 years",
        description="Looking for C# .NET Core SQL Server REST API experience",
    )
    result = scorer.score(job)
    assert not result.dropped
    assert result.total >= 80, result.components


def test_weak_match_scores_low(scorer):
    job = make_job(title="Graphic Designer", company="Acme",
                   location="Mumbai", description="Photoshop and Illustrator")
    result = scorer.score(job)
    assert result.total < 50


def test_alias_matches_title(scorer):
    aliased = scorer.score(make_job(title="Backend Engineer"))
    exact = scorer.score(make_job(title="Backend Developer"))
    assert aliased.components["title_match"] == exact.components["title_match"]


def test_role_weight_is_applied(scorer):
    """Full Stack has weight 0.8, so it must score below Backend at 1.0."""
    backend = scorer.score(make_job(title="Backend Developer"))
    fullstack = scorer.score(make_job(title="Full Stack Developer"))
    assert fullstack.components["title_match"] < backend.components["title_match"]


def test_remote_beats_unlisted_city(scorer):
    remote = scorer.score(make_job(location="Remote", description="work from home"))
    other = scorer.score(make_job(location="Kolkata"))
    assert remote.components["location_fit"] > other.components["location_fit"]


def test_salary_below_floor_is_penalised(scorer):
    low = scorer.score(make_job(salary="5-8 Lacs PA"))
    high = scorer.score(make_job(salary="14-20 Lacs PA"))
    assert low.components["compensation_fit"] < high.components["compensation_fit"]


def test_applied_company_penalty(scorer):
    plain = scorer.score(make_job())
    repeat = scorer.score(make_job(), applied_companies={"acme"})
    assert repeat.total < plain.total
    assert repeat.penalties["already_applied_company"] == 15


def test_score_is_bounded(scorer):
    for job in (make_job(), make_job(title="x", location="Kolkata")):
        result = scorer.score(job)
        assert 0 <= result.total <= 100


def test_reasons_are_populated(scorer):
    result = scorer.score(make_job(location="Noida"))
    assert any("Noida" in r for r in result.reasons)


# ------------------------------------------------------------------ bands
@pytest.mark.parametrize("value,expected", [
    (95, "priority"), (85, "priority"), (75, "tailor"),
    (70, "tailor"), (62, "shortlist"), (60, "shortlist"), (40, "below"),
])
def test_bands(scorer, value, expected):
    assert scorer.band(value) == expected


# ----------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,lo,hi", [
    ("12-18 Lacs PA", 12, 18),
    ("8 Lacs PA", 8, 8),
    ("3,50,000 - 6,00,000", 3.5, 6.0),
])
def test_salary_parsing(text, lo, hi):
    s = SalaryRange.parse(text)
    assert s.disclosed
    assert s.min_lpa == pytest.approx(lo, abs=0.1)
    assert s.max_lpa == pytest.approx(hi, abs=0.1)


@pytest.mark.parametrize("text", ["Not disclosed", "", None, "Competitive"])
def test_salary_undisclosed(text):
    assert not SalaryRange.parse(text).disclosed


@pytest.mark.parametrize("text,lo,hi", [
    ("3-6 years", 3, 6), ("5+ years", 5, None), ("2 Yrs", 2, 2),
])
def test_experience_parsing(text, lo, hi):
    e = ExperienceRange.parse(text)
    assert e.min_years == lo
    assert e.max_years == hi


def test_fingerprint_collapses_seniority_and_portal():
    """Same role at same company on two portals must dedupe to one row."""
    a = Job(portal="naukri", portal_job_id="1", title="Senior Backend Developer",
            company="Acme Corp", url="u", location="Noida, India")
    b = Job(portal="linkedin", portal_job_id="2", title="Backend Developer",
            company="Acme  Corp", url="v", location="Noida")
    assert a.fingerprint == b.fingerprint


def test_fingerprint_separates_different_companies():
    a = Job(portal="naukri", portal_job_id="1", title="Backend Developer",
            company="Acme", url="u", location="Noida")
    b = Job(portal="naukri", portal_job_id="2", title="Backend Developer",
            company="Globex", url="v", location="Noida")
    assert a.fingerprint != b.fingerprint
