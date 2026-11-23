"""The decision rule (spec section 8): posterior = prior odds x 2^(match weight).

| posterior                 | outcome      |
| >= confirm_threshold      | confirmed    |
| >= review_threshold       | needs_review |
| below                     | unresolved   |

Three conservative additions, each recorded as the reason on the decision:

* ``no_corroborating_evidence``: a candidate whose only agreement is the name (and state, year) is
  never confirmed or reviewed, whatever the arithmetic says. The name levels carry no evidence and
  the prior is at most one half (N_namesakes >= 1), so this cannot trigger at the pre-registered
  thresholds; it is the guarantee made structural rather than numeric.
* ``weak_record``: a candidate from a court record the court stage marked weak is at most reviewed.
* ``competing_candidate``: two candidates from the same source, with different identifiers, both at
  or above the review threshold, go to review; the model does not choose between them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from efp.identity.pairs import Pair
from efp.identity.profile import Person

# comparison -> the levels that are agreement on something other than the name
AGREEING: dict[str, frozenset[str]] = {
    "middle": frozenset({"exact"}),
    "dob": frozenset({"exact", "within_1", "within_3"}),
    "city": frozenset({"exact", "same_county"}),
    "case_number": frozenset({"exact"}),
}
OUTCOMES = ("confirmed", "needs_review", "unresolved")


def posterior(prior_odds: float, bits: float) -> float:
    exponent = max(min(bits, 200.0), -200.0)
    odds = prior_odds * math.pow(2.0, exponent)
    return odds / (1.0 + odds)


@dataclass
class Scored:
    candidate: Person
    pair: Pair
    waterfall: dict[str, Any]
    posterior: float
    candidate_id: int | None = None

    @property
    def agreeing(self) -> list[str]:
        return [
            f"{s['comparison']}:{s['level']}"
            for s in self.waterfall["steps"]
            if s["level"] in AGREEING.get(s["comparison"], frozenset())
        ]


def score_candidate(candidate: Person, pair: Pair, waterfall: dict[str, Any]) -> Scored:
    return Scored(
        candidate, pair, waterfall, posterior(pair.prior.odds, waterfall["evidence_bits"])
    )


@dataclass
class Decision:
    outcome: str
    reason: str
    best: Scored | None = None
    competing: list[Scored] = field(default_factory=list)


def decide(scored: list[Scored], *, confirm: float, review: float) -> Decision:
    if not scored:
        return Decision("unresolved", "no_candidate")
    ranked = sorted(scored, key=lambda s: s.posterior, reverse=True)
    best = ranked[0]
    if best.posterior < review:
        return Decision("unresolved", "below_review_threshold", best)
    if not best.agreeing:
        return Decision("unresolved", "no_corroborating_evidence", best)
    rivals = [
        s for s in ranked[1:]
        if s.posterior >= review
        and s.candidate.source == best.candidate.source
        and s.candidate.source_id != best.candidate.source_id
    ]  # fmt: skip
    if rivals:
        return Decision("needs_review", "competing_candidate", best, rivals)
    if best.posterior >= confirm:
        if best.candidate.extras.get("weak"):
            return Decision("needs_review", "weak_record", best)
        return Decision("confirmed", "at_or_above_confirm_threshold", best)
    return Decision("needs_review", "in_review_band", best)
