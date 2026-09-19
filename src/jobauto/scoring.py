"""Preference-driven scoring.

Every number here comes from config/preferences.yaml. Adding a new role,
skill or dealbreaker is a YAML edit; this module never needs to change.

Each component returns 0..1, is multiplied by its configured weight, summed
to 0..100, then penalties are subtracted. The breakdown is retained so the
digest can explain a ranking instead of asserting one.
"""
from __future__ import annotations

import re
from typing import Any

from .models import Job, ScoreBreakdown


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if len(t) > 1}


class Scorer:
    def __init__(self, preferences: dict[str, Any], profile: dict[str, Any]):
        self.prefs = preferences
        self.profile = profile
        self.search = preferences.get("search", {})
        self.weights = preferences.get("scoring", {}).get("weights", {})
        self.penalties = preferences.get("scoring", {}).get("penalties", {})
        self.thresholds = preferences.get("thresholds", {})

    # ---------------------------------------------------------------- gates
    def _hard_filters(self, job: Job) -> tuple[bool, str]:
        """Returns (dropped, reason). These are absolute -- no score can
        rescue a job that trips one."""
        blob = job.text_blob()

        for word in self.search.get("keywords", {}).get("exclude", []):
            if word.lower() in blob:
                return True, f"excluded keyword: {word!r}"

        company_l = (job.company or "").lower()
        for blocked in self.search.get("company", {}).get("blocked", []):
            if blocked.lower() in company_l:
                return True, f"blocked company: {blocked}"

        loc_l = (job.location or "").lower()
        for blocked in self.search.get("locations", {}).get("blocked", []):
            if blocked.lower() in loc_l:
                return True, f"blocked location: {blocked}"

        max_age = self.search.get("posting", {}).get("max_age_days")
        if max_age and job.age_days is not None and job.age_days > int(max_age):
            return True, f"posting is {job.age_days}d old (max {max_age})"

        if self.search.get("posting", {}).get("require_salary_disclosed") \
                and not job.salary.disclosed:
            return True, "salary not disclosed"

        # A job demanding far more experience than you have is not a near-miss,
        # it is a wasted application slot.
        cur = self.search.get("experience", {}).get("current_years")
        if cur is not None and job.experience.min_years is not None:
            if job.experience.min_years > float(cur) + 3:
                return True, (
                    f"requires {job.experience.min_years}y minimum, "
                    f"you have {cur}y"
                )

        return False, ""

    # ----------------------------------------------------------- components
    def _title_match(self, job: Job) -> tuple[float, str]:
        """Best match across every configured role and its aliases, scaled by
        that role weight."""
        title = _norm(job.title)
        best, best_name = 0.0, ""
        for role in self.search.get("roles", []):
            weight = float(role.get("weight", 1.0))
            for name in [role.get("title", "")] + list(role.get("aliases") or []):
                n = _norm(name)
                if not n:
                    continue
                if n in title:
                    hit = 1.0
                else:
                    want, got = _tokens(n), _tokens(title)
                    hit = len(want & got) / len(want) if want else 0.0
                value = hit * weight
                if value > best:
                    best, best_name = value, name
        why = f"title ~ {best_name}" if best_name else "no title match"
        return min(best, 1.0), why

    def _skill_overlap(self, job: Job) -> tuple[float, str]:
        """Primary skills count double, secondary single. Include-keywords from
        preferences are folded in at secondary weight."""
        blob = job.text_blob()
        primary = [s["name"] for s in self.profile.get("skills", {}).get("primary", [])]
        secondary = [s["name"] for s in self.profile.get("skills", {}).get("secondary", [])]
        secondary = secondary + list(self.search.get("keywords", {}).get("include", []))

        hit_p = [s for s in primary if s.lower() in blob]
        hit_s = [s for s in secondary if s.lower() in blob]

        earned = len(hit_p) * 2 + len(hit_s)
        possible = len(primary) * 2 + len(secondary)
        if not possible:
            return 0.5, "no skills configured"

        # Full credit well before a perfect match -- no JD lists every skill.
        ratio = min(earned / (possible * 0.45), 1.0)
        matched = hit_p + hit_s
        why = ("matches " + ", ".join(matched[:5])) if matched else "no skill overlap"
        return ratio, why

    def _experience_fit(self, job: Job) -> tuple[float, str]:
        cur = self.search.get("experience", {}).get("current_years")
        if cur is None or job.experience.min_years is None:
            return 0.6, "experience unstated"
        cur = float(cur)
        lo = job.experience.min_years
        hi = job.experience.max_years if job.experience.max_years is not None else lo + 5

        if lo <= cur <= hi:
            return 1.0, f"you fit the {lo:g}-{hi:g}y band"
        gap = (lo - cur) if cur < lo else (cur - hi)
        return max(0.0, 1.0 - gap / 4.0), f"{gap:.1f}y outside the {lo:g}-{hi:g}y band"

    def _location_fit(self, job: Job) -> tuple[float, str]:
        locs = self.search.get("locations", {})
        loc_l = (job.location or "").lower()
        blob = job.text_blob()

        modes = [m.lower() for m in locs.get("work_mode", [])]
        if "remote" in blob or "work from home" in blob:
            if "remote" in modes:
                return 1.0, "remote"
            return 0.3, "remote but you did not list remote"

        for p in locs.get("preferred", []):
            if p.lower() in loc_l or p.lower() in blob:
                return 1.0, f"preferred location: {p}"
        for a in locs.get("acceptable", []):
            if a.lower() in loc_l or a.lower() in blob:
                value = 0.75 if locs.get("relocate") else 0.6
                return value, f"acceptable location: {a}"

        if not loc_l:
            return 0.5, "location unstated"
        return 0.15, f"{job.location} is outside your locations"

    def _compensation_fit(self, job: Job) -> tuple[float, str]:
        comp = self.search.get("compensation", {})
        expected = comp.get("expected_ctc_lpa")
        minimum = comp.get("minimum_acceptable_lpa")
        if expected is None:
            return 0.5, "no expectation configured"
        if not job.salary.disclosed or job.salary.max_lpa is None:
            # Most Indian listings hide salary; neutral, not punitive.
            return 0.5, "salary not disclosed"

        top = job.salary.max_lpa
        floor = float(minimum) if minimum is not None else 0.0
        if minimum is not None and top < floor:
            return 0.05, f"tops out at {top:g} LPA, below your floor of {floor:g}"
        if top >= float(expected):
            return 1.0, f"up to {top:g} LPA meets your ask"
        span = float(expected) - floor
        if span <= 0:
            return 0.5, f"up to {top:g} LPA"
        return max(0.1, (top - floor) / span), f"up to {top:g} LPA, under your ask"

    def _company_quality(self, job: Job) -> tuple[float, str]:
        company_l = (job.company or "").lower()
        for p in self.search.get("company", {}).get("preferred", []):
            if p.lower() in company_l:
                return 1.0, f"preferred company: {p}"

        if self.search.get("company", {}).get("exclude_staffing_agencies"):
            for marker in ("consultanc", "staffing", "recruit", "manpower",
                           "hr solutions", "placement"):
                if marker in company_l:
                    return 0.1, "looks like a staffing agency"

        rating = job.raw.get("company_rating")
        floor = self.search.get("company", {}).get("min_employee_rating")
        if rating is not None and floor is not None:
            r = float(rating)
            if r >= float(floor):
                return 1.0, f"rated {r}"
            return 0.25, f"rated {r}, below your floor of {floor}"
        return 0.6, ""

    # ---------------------------------------------------------------- score
    def score(self, job: Job, applied_companies: set[str] | None = None) -> ScoreBreakdown:
        out = ScoreBreakdown()

        dropped, reason = self._hard_filters(job)
        if dropped:
            out.dropped, out.drop_reason = True, reason
            out.reasons.append(f"DROPPED: {reason}")
            return out

        components = {
            "title_match": self._title_match(job),
            "skill_overlap": self._skill_overlap(job),
            "experience_fit": self._experience_fit(job),
            "location_fit": self._location_fit(job),
            "compensation_fit": self._compensation_fit(job),
            "company_quality": self._company_quality(job),
        }

        total = 0.0
        for key, (value, why) in components.items():
            weight = float(self.weights.get(key, 0.0))
            contribution = value * weight * 100
            out.components[key] = round(contribution, 2)
            total += contribution
            if why:
                out.reasons.append(why)

        # -- penalties ---------------------------------------------------
        max_age = self.search.get("posting", {}).get("max_age_days", 30)
        if job.age_days is not None and job.age_days > int(max_age) / 2:
            p = float(self.penalties.get("stale_posting", 0))
            out.penalties["stale_posting"] = p
            total -= p

        if not job.salary.disclosed:
            p = float(self.penalties.get("no_salary_disclosed", 0))
            out.penalties["no_salary_disclosed"] = p
            total -= p

        cur = self.search.get("experience", {}).get("current_years")
        if cur is not None and job.experience.max_years is not None:
            if job.experience.max_years < float(cur) - 1:
                p = float(self.penalties.get("overqualified", 0))
                out.penalties["overqualified"] = p
                total -= p
                out.reasons.append("you are over the stated experience ceiling")

        if applied_companies and (job.company or "").lower() in applied_companies:
            p = float(self.penalties.get("already_applied_company", 0))
            out.penalties["already_applied_company"] = p
            total -= p
            out.reasons.append("you applied to this company recently")

        out.total = round(max(0.0, min(100.0, total)), 1)
        return out

    # -------------------------------------------------------------- banding
    def band(self, score: float) -> str:
        if score >= float(self.thresholds.get("priority", 85)):
            return "priority"
        if score >= float(self.thresholds.get("auto_tailor", 70)):
            return "tailor"
        if score >= float(self.thresholds.get("shortlist", 60)):
            return "shortlist"
        return "below"
