"""The population prior (spec section 8): prior odds = 1 / N_namesakes.

    N_namesakes = p(surname) x p(given name | sex, birth cohort) x N_adults(state, cohort)

Every component is returned (and stored in ``identity_candidates.fields_json``) so the prior can be
reproduced. Inputs are the ``*_current`` views of the reference tables (build 11):

* ``p(surname)``: the Census 2010 surname proportion; a surname absent from the file takes the floor
  recorded when the file was loaded (half the smallest listed proportion). Counted as ``floor``.
* ``p(given | sex, cohort)``: SSA counts of the name in the cohort's birth years over all births of
  that sex in those years. The sex is the name's SSA majority sex; a name with 10-90% of its births
  in each sex is ambiguous and pooled over both sexes (counted as ``ambiguous``). The by-state file
  is used where the state had at least ``identity.ssa_state_min_births`` births of that sex in the
  cohort and lists the name; otherwise the national file.
* ``N_adults``: the state's population in the age bands the cohort falls in (uniform within a
  band) at the latest vintage year; adults (18 and over) when there is no age or date of birth.

When the Census surname file or the SSA files are not loaded the prior uses a published *ceiling*
instead of a value: the proportion of the most common surname and of the most common given name.
That can only overstate N_namesakes, so the prior can only be too cautious, and the prior is tagged
``ceiling`` so every decision it produces says so and a refit re-scores it when the files arrive.

N_namesakes is never below 1: the person is a namesake of themself.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from efp.identity.normalize import letters

# Published ceilings (docs/identity_model.md, "The ceiling prior").
CEILING_P_SURNAME = 828.19 / 100_000  # Smith, Census 2010 surnames file (2,442,977; 828.19/100k)
CEILING_P_GIVEN = 62.9 / 1_000  # John, 1900: SSA Note 139's highest male rate, 1900-1997
CEILING_N_ADULTS = 258_000_000  # US residents aged 18+, order of magnitude of the 2020 census
MIN_ADULT_AGE = 18
AMBIGUOUS_LOW, AMBIGUOUS_HIGH = 0.10, 0.90


@dataclass
class Prior:
    n_namesakes: float
    components: dict[str, Any] = field(default_factory=dict)

    @property
    def odds(self) -> float:
        return 1.0 / self.n_namesakes

    @property
    def mode(self) -> str:
        """``census`` when every component came from the reference tables, else ``ceiling``."""
        return "ceiling" if self.components.get("ceiling") else "census"

    def as_dict(self) -> dict[str, Any]:
        return {"n_namesakes": self.n_namesakes, "prior_odds": self.odds, **self.components}


class PriorModel:
    """Reads the reference tables once and answers ``prior()`` for any candidate."""

    def __init__(self, conn: sqlite3.Connection, *, ssa_state_min_births: int = 100_000) -> None:
        self.conn = conn
        self.state_min = ssa_state_min_births
        self.floor_prop, self.floor_load = self._floor()
        self.have_surnames = self._count("ref_surname_freq_current") > 0
        self.have_given = (
            self.conn.execute(
                "SELECT 1 FROM ref_given_freq_current WHERE state IS NULL LIMIT 1"
            ).fetchone()
            is not None
        )
        self.pop_year, self.population = self._population()
        self._totals_national: dict[tuple[str, int], int] | None = None
        self._totals_state: dict[str, dict[tuple[str, int], int]] = {}
        self.loads = self._loads()

    @property
    def default_mode(self) -> str:
        """The prior a row with no candidate would have had: ``ceiling`` until the Census and SSA
        files are loaded (so the decision's tag does not claim a census prior it never used)."""
        return "census" if self.have_surnames and self.have_given else "ceiling"

    # ---- what is loaded ---------------------------------------------------------------------

    def _count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608

    def _floor(self) -> tuple[float | None, int | None]:
        found = self.conn.execute(
            "SELECT notes_json, load_id FROM ref_sources WHERE dataset = 'census_surnames' "
            "AND current = 1 AND status = 'loaded' ORDER BY load_id DESC LIMIT 1"
        ).fetchone()
        if found is None or not found[0]:
            return None, None
        try:
            return float(json.loads(found[0])["floor_prop100k"]) / 100_000, int(found[1])
        except (KeyError, ValueError, TypeError):
            return None, int(found[1])

    def _population(self) -> tuple[int | None, dict[tuple[str, str], int]]:
        rows = self.conn.execute(
            "SELECT state, age_band, year, count FROM ref_population_current"
        ).fetchall()
        if not rows:
            return None, {}
        latest = max(int(r[2]) for r in rows)
        return latest, {(str(r[0]), str(r[1])): int(r[3]) for r in rows if int(r[2]) == latest}

    def _loads(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for dataset, load_id in self.conn.execute(
            "SELECT dataset, load_id FROM ref_sources WHERE current = 1 AND status = 'loaded' "
            "AND dataset IN ('census_surnames','ssa_names','census_population') ORDER BY load_id"
        ):
            out.setdefault(str(dataset), []).append(int(load_id))
        return out

    # ---- components -------------------------------------------------------------------------

    @lru_cache(maxsize=65536)  # noqa: B019 (the model lives for one run)
    def p_surname(self, surname: str) -> tuple[float, str]:
        """``(proportion, how)``; how is ``listed``, ``floor`` or ``ceiling``."""
        if not self.have_surnames:
            return CEILING_P_SURNAME, "ceiling"
        found = self.conn.execute(
            "SELECT prop100k FROM ref_surname_freq_current WHERE surname = ? LIMIT 1",
            (letters(surname),),
        ).fetchone()
        if found is not None:
            return float(found[0]) / 100_000, "listed"
        if self.floor_prop is None:  # loaded without its floor note: the smallest listed value
            smallest = self.conn.execute(
                "SELECT min(prop100k) FROM ref_surname_freq_current"
            ).fetchone()[0]
            return float(smallest) / 200_000, "floor"
        return self.floor_prop, "floor"

    def _totals(self, state: str | None) -> dict[tuple[str, int], int]:
        """Births per (sex, year) over every listed name (names given fewer than five times a year
        are not in the files, so these are slightly low and the proportions slightly high)."""
        if state is None:
            if self._totals_national is None:
                self._totals_national = {
                    (str(s), int(y)): int(n)
                    for s, y, n in self.conn.execute(
                        "SELECT sex, year, sum(count) FROM ref_given_freq_current "
                        "WHERE state IS NULL GROUP BY sex, year"
                    )
                }
            return self._totals_national
        if state not in self._totals_state:
            self._totals_state[state] = {
                (str(s), int(y)): int(n)
                for s, y, n in self.conn.execute(
                    "SELECT sex, year, sum(count) FROM ref_given_freq_current "
                    "WHERE state = ? GROUP BY sex, year",
                    (state,),
                )
            }
        return self._totals_state[state]

    def _name_counts(self, name: str, state: str | None) -> dict[tuple[str, int], int]:
        rows = self.conn.execute(
            "SELECT sex, year, sum(count) FROM ref_given_freq_current "
            "WHERE name = ? AND state IS ? GROUP BY sex, year",
            (name.capitalize(), state),
        ).fetchall()
        return {(str(s), int(y)): int(n) for s, y, n in rows}

    def _p_initial(
        self, letter: str, lo: int | None, hi: int | None
    ) -> tuple[float, dict[str, Any]]:
        """An initial stands for every name that starts with the letter: their share of the
        cohort's births, both sexes (an initial says nothing about sex)."""
        c_lo, c_hi = (lo if lo is not None else 1900), (hi if hi is not None else 2010)
        c_lo, c_hi = min(c_lo, c_hi), max(c_lo, c_hi)
        totals = self._totals(None)
        den = sum(n for (_, y), n in totals.items() if c_lo <= y <= c_hi)
        num = self.conn.execute(
            # pinned to the name index: the planner would otherwise scan every national row
            "SELECT coalesce(sum(t.count), 0) FROM ref_given_freq AS t "
            "INDEXED BY idx_ref_given_name JOIN ref_sources s ON s.load_id = t.load_id "
            "WHERE s.current = 1 AND t.state IS NULL AND t.name >= ? AND t.name < ? "
            "AND t.year BETWEEN ? AND ?",
            (letter, chr(ord(letter) + 1), c_lo, c_hi),
        ).fetchone()[0]
        share = (int(num) / den) if den and num else 1.0
        return share, {
            "how": "initial",
            "sex": "F/M",
            "ambiguous": True,
            "cohort": [c_lo, c_hi],
            "cohort_births": den,
        }

    def p_given(
        self, given: str, state: str | None, lo: int | None, hi: int | None
    ) -> tuple[float, dict[str, Any]]:
        """``p(given | sex, cohort)`` and how it was obtained."""
        key = letters(given)
        if not key:  # no given name at all says nothing about which people qualify
            return 1.0, {"how": "no_given_name", "sex": "?"}
        if not self.have_given:
            return CEILING_P_GIVEN, {"how": "ceiling"}
        if len(key) == 1:
            return self._p_initial(key, lo, hi)
        national = self._name_counts(key, None)
        years = [y for _, y in national] or [0]
        c_lo = lo if lo is not None else min([*years, 1900])
        c_hi = hi if hi is not None else 2010
        c_lo, c_hi = min(c_lo, c_hi), max(c_lo, c_hi)
        total_n = {s: sum(n for (sx, _), n in national.items() if sx == s) for s in ("F", "M")}
        grand = total_n["F"] + total_n["M"]
        if grand == 0:
            # Not a listed name: under five births of each sex in every year. The most it can be is
            # four a year and sex, over the cohort's births (never the share of a single name over
            # all years, which would make a misspelling look unique).
            totals = self._totals(None)
            births = sum(n for (_, y), n in totals.items() if c_lo <= y <= c_hi)
            years_n = max(c_hi - c_lo + 1, 1)
            unlisted = min(1.0, 4.0 * 2 * years_n / births) if births else 1.0
            return unlisted, {
                "how": "unlisted",
                "sex": "?",
                "cohort": [c_lo, c_hi],
                "cohort_births": births,
            }
        male_share = total_n["M"] / grand
        ambiguous = AMBIGUOUS_LOW < male_share < AMBIGUOUS_HIGH
        sexes = ["F", "M"] if ambiguous else ["M" if male_share >= 0.5 else "F"]

        def cohort_share(scope: str | None) -> tuple[float, int] | None:
            counts = national if scope is None else self._name_counts(key, scope)
            totals = self._totals(scope)
            num = sum(n for (s, y), n in counts.items() if s in sexes and c_lo <= y <= c_hi)
            den = sum(n for (s, y), n in totals.items() if s in sexes and c_lo <= y <= c_hi)
            return (num / den, den) if den else None

        how, births = "national", 0
        share: float | None = None
        if state is not None:
            by_state = cohort_share(state)
            if by_state is not None and by_state[1] >= self.state_min:
                local = self._name_counts(key, state)
                if local:
                    share, births, how = by_state[0], by_state[1], "state"
        if share is None:
            nat = cohort_share(None)
            share, births = nat if nat else (0.0, 0)
        if share <= 0:  # listed overall but absent in this cohort: one birth in the cohort
            share = 1.0 / max(births, 1)
            how += "+absent_in_cohort"
        return share, {
            "how": how,
            "sex": "F/M" if ambiguous else sexes[0],
            "ambiguous": ambiguous,
            "male_share": round(male_share, 4),
            "cohort": [c_lo, c_hi],
            "cohort_births": births,
        }

    def n_adults(
        self, state: str | None, lo: int | None, hi: int | None
    ) -> tuple[float, dict[str, Any]]:
        if not self.population or self.pop_year is None:
            return float(CEILING_N_ADULTS), {"how": "ceiling"}
        year = self.pop_year
        if lo is None or hi is None:
            age_lo, age_hi = MIN_ADULT_AGE, 120
        else:
            age_lo, age_hi = max(year - max(lo, hi), 0), year - min(lo, hi)
        total = 0.0
        for (st, band), count in self.population.items():
            if state is not None and st != state:
                continue
            b_lo, b_hi = _band_ages(band)
            overlap = min(b_hi, age_hi) - max(b_lo, age_lo) + 1
            if overlap > 0:
                total += count * overlap / (b_hi - b_lo + 1)
        how = (
            "state"
            if state is not None and any(s == state for s, _ in self.population)
            else "national"
        )
        if how == "national" and state is not None:  # a state the table does not carry
            total = sum(
                count
                * max(min(_band_ages(b)[1], age_hi) - max(_band_ages(b)[0], age_lo) + 1, 0)
                / (_band_ages(b)[1] - _band_ages(b)[0] + 1)
                for (_, b), count in self.population.items()
            )
        return max(total, 1.0), {"how": how, "vintage": year, "ages": [age_lo, min(age_hi, 120)]}

    def age_boost_bits(self, inputs: dict[str, Any], prior: Prior) -> float:
        """How much the prior gained from narrowing to the cohort, in bits: log2 of the namesake
        count with no cohort over the count with it. The age evidence in the comparison is the same
        information, so it is credited once (``efp.identity.decide``), see identity_model.md."""
        if inputs.get("birth_lo") is None:
            return 0.0
        free = self.prior(
            surname=inputs["surname"], given=inputs["given"], state=inputs.get("state"),
            birth_lo=None, birth_hi=None,
        )  # fmt: skip
        return max(0.0, math.log2(free.n_namesakes / prior.n_namesakes))

    # ---- the prior --------------------------------------------------------------------------

    def prior(
        self,
        *,
        surname: str,
        given: str,
        state: str | None,
        birth_lo: int | None,
        birth_hi: int | None,
        widen: int = 0,
    ) -> Prior:
        """``widen`` years either side of the birth interval (5 when only an age is known)."""
        lo = None if birth_lo is None else birth_lo - widen
        hi = None if birth_hi is None else birth_hi + widen
        ps, ps_how = self.p_surname(surname)
        pg, pg_info = self.p_given(given, state, lo, hi)
        na, na_info = self.n_adults(state, lo, hi)
        raw = ps * pg * na
        ceiling = ps_how == "ceiling" or pg_info["how"] == "ceiling" or na_info["how"] == "ceiling"
        return Prior(
            max(raw, 1.0),
            {
                "p_surname": ps,
                "p_surname_how": ps_how,
                "p_given": pg,
                "p_given_info": pg_info,
                "n_adults": na,
                "n_adults_info": na_info,
                "raw_namesakes": raw,
                "floored_at_one": raw < 1.0,
                "cohort_free": lo is None,
                "ceiling": ceiling,
                "loads": self.loads,
            },
        )


def _band_ages(band: str) -> tuple[int, int]:
    if band.endswith("+"):
        return int(band[:-1]), 120
    low, _, high = band.partition("-")
    return int(low), int(high)
