"""Screening-question answering.

Portals ask the same dozen questions forever. `profile.screening_answers` holds
your canned replies; this module matches a live question against them.

Two rules that matter:
  - anything matching `never_auto_answer` is ALWAYS escalated to you, even if a
    canned answer would otherwise match;
  - an unmatched question is left blank and escalated, never guessed. A wrong
    auto-answer on a screening question is worse than no application.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AnswerResult:
    answered: dict[str, str] = field(default_factory=dict)
    escalated: list[str] = field(default_factory=list)

    @property
    def needs_you(self) -> bool:
        return bool(self.escalated)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


class ScreeningAnswerer:
    def __init__(self, profile: dict[str, Any]):
        self.rules = profile.get("screening_answers", []) or []
        self.blocklist = [b.lower() for b in profile.get("never_auto_answer", []) or []]

    def is_sensitive(self, question: str) -> bool:
        q = (question or "").lower()
        return any(b in q for b in self.blocklist)

    def answer_for(self, question: str) -> str | None:
        """Best canned answer, or None if nothing matches confidently."""
        if self.is_sensitive(question):
            return None

        q = _norm(question)
        if not q:
            return None

        best_answer, best_score = None, 0.0
        for rule in self.rules:
            answer = (rule.get("answer") or "").strip()
            if not answer:
                continue            # blank answers are opt-outs, not matches
            for phrase in rule.get("match", []):
                p = _norm(phrase)
                if not p:
                    continue
                if p in q:
                    # Longer phrase match wins -- "expected ctc" should beat "ctc".
                    score = len(p) / max(len(q), 1) + 1.0
                else:
                    want = set(p.split())
                    got = set(q.split())
                    if not want:
                        continue
                    score = len(want & got) / len(want)
                if score > best_score:
                    best_answer, best_score = answer, score

        # Below this, the match is a coincidence rather than a real hit.
        return best_answer if best_score >= 0.7 else None

    def answer_all(self, questions: list[str]) -> AnswerResult:
        out = AnswerResult()
        for q in questions:
            q = (q or "").strip()
            if not q:
                continue
            if self.is_sensitive(q):
                out.escalated.append(q)
                continue
            answer = self.answer_for(q)
            if answer is None:
                out.escalated.append(q)
            else:
                out.answered[q] = answer
        return out


def pick_resume(job_text: str, resume_cfg: dict[str, Any]) -> str:
    """First variant whose keywords hit the job text; else the default."""
    blob = (job_text or "").lower()
    for variant in resume_cfg.get("variants", []) or []:
        for kw in variant.get("match_keywords", []):
            if kw.lower() in blob:
                return variant.get("file", "")
    return resume_cfg.get("default", "")
