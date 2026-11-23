"""Common and rare surnames, for the label queue, the F3 audit and ``efp status``.

A surname is *common* when its Census 2010 rank is at most ``identity.common_surname_rank``. Until
the Census file is imported there is no rank, so the stratum falls back to the surname's frequency
among the candidate universe (``ref_candidates_all``) and the workbook's own rows; the fallback is
reported as ``provisional`` wherever a stratum is shown.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from efp.identity.normalize import letters


class Strata:
    def __init__(self, conn: sqlite3.Connection, common_rank: int) -> None:
        self.common_rank = common_rank
        self.provisional = (
            conn.execute("SELECT 1 FROM ref_surname_freq_current LIMIT 1").fetchone() is None
        )
        self._ranks: dict[str, int] = {}
        if not self.provisional:
            self._ranks = {
                str(s): int(r)
                for s, r in conn.execute(
                    "SELECT surname, rank FROM ref_surname_freq_current WHERE rank IS NOT NULL"
                )
            }
        else:
            counts: Counter[str] = Counter()
            for (key,) in conn.execute("SELECT surname_key FROM ref_candidates_all"):
                if key:
                    counts[str(key)] += 1
            for (name,) in conn.execute("SELECT name FROM rows WHERE in_scope = 1"):
                from efp.court.names import parse_name

                counts[letters(parse_name(name).surname)] += 1
            self._ranks = {s: i for i, (s, _) in enumerate(counts.most_common(), 1)}

    def rank(self, surname: str | None) -> int | None:
        return self._ranks.get(letters(surname))

    def is_common(self, surname: str | None) -> bool:
        rank = self.rank(surname)
        return rank is not None and rank <= self.common_rank

    def label(self, surname: str | None) -> str:
        return "common" if self.is_common(surname) else "rare"
