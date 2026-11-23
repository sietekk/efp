"""The population prior inputs (spec section 8): Census 2010 surnames, SSA given names, Census state
population by age band. All three are public domain (U.S. Government works)."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path
from typing import Any, ClassVar

from efp.reference.base import Counter, Dataset, Known, LayoutError, batches
from efp.reference.files import Part
from efp.reference.states import state_code
from efp.types import Context

# The Census file lists surnames occurring 100 or more times. A surname absent from it occurred
# fewer than 100 times, so its proportion is somewhere under the smallest listed one. The floor
# used for such a surname is HALF the smallest listed proportion (count 100 / 2 over the file's
# population), recorded with this rule's name in ref_sources.notes_json and reported (spec
# section 8).
FLOOR_RULE = "half the smallest listed proportion"


def _csv_rows(raw: bytes) -> csv.DictReader[str]:
    return csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))


def _member(archive: zipfile.ZipFile, *patterns: str) -> str:
    names = archive.namelist()
    for pattern in patterns:
        found = next((n for n in names if re.search(pattern, n, re.I)), None)
        if found:
            return found
    raise LayoutError(f"none of {patterns} found in the ZIP (it holds {names[:8]})")


class CensusSurnames(Dataset):
    name = "census_surnames"
    table = "ref_surname_freq"
    license = "Public domain (U.S. Census Bureau)"
    attribution = (
        "Surname frequencies: U.S. Census Bureau, Frequently Occurring Surnames from the 2010 "
        "Census."
    )
    version = "2010"
    known: ClassVar[list[Known]] = [
        Known(
            "SMITH proportion per 100k (Census 2010)",
            "SELECT prop100k FROM ref_surname_freq_current WHERE surname = 'SMITH'",
            (),
            828.19,
            0.01,
        )
    ]

    def parts(self, ctx: Context) -> list[Part]:
        return [Part("", ctx.config.reference.census_surnames_url, "names.zip", "zip")]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        with zipfile.ZipFile(path) as archive:
            raw = archive.read(_member(archive, r"names_2010census\.csv$", r"\.csv$"))
        reader = _csv_rows(raw)
        need = {"name", "rank", "count", "prop100k"}
        if not reader.fieldnames or not need <= {f.strip().lower() for f in reader.fieldnames}:
            raise LayoutError(f"expected columns {sorted(need)}, the file has {reader.fieldnames}")
        rows: list[tuple[Any, ...]] = []
        other = 0
        for rec in reader:
            r = {k.strip().lower(): (v or "").strip() for k, v in rec.items() if k}
            if r["name"].upper() == "ALL OTHER NAMES":
                other = int(r["count"] or 0)
                continue
            rows.append(
                (
                    load_id,
                    r["name"].upper(),
                    int(r["rank"] or 0),
                    int(r["count"]),
                    float(r["prop100k"]),
                )
            )
        if not rows:
            raise LayoutError("no surname rows")
        for batch in batches(iter(rows)):
            conn.executemany("INSERT INTO ref_surname_freq VALUES (?,?,?,?,?)", batch)
        smallest = min(rows, key=lambda r: r[3])
        top = max(rows, key=lambda r: r[3])
        population = round(top[3] * 100_000 / top[4]) if top[4] else sum(r[3] for r in rows) + other
        floor = (smallest[3] / 2) / population * 100_000
        return Counter(
            len(rows),
            {
                "floor_prop100k": round(floor, 6),
                "floor_rule": FLOOR_RULE,
                "smallest_listed_count": smallest[3],
                "population_denominator": population,
                "all_other_names_count": other,
            },
        )


class SsaNames(Dataset):
    name = "ssa_names"
    table = "ref_given_freq"
    license = "Public domain (Social Security Administration)"
    attribution = "Given-name counts: Social Security Administration, Baby Names data."
    version = "1880-latest"
    known: ClassVar[list[Known]] = [
        Known(
            "Mary (F) born 1880, national count",
            "SELECT count FROM ref_given_freq_current "
            "WHERE name = 'Mary' AND sex = 'F' AND year = 1880 AND state IS NULL",
            (),
            7065,
        ),
        Known(
            "Mary (F) born 1950 in Connecticut, state count (read from CT.TXT of namesbystate.zip)",
            "SELECT count FROM ref_given_freq_current "
            "WHERE name = 'Mary' AND sex = 'F' AND year = 1950 AND state = 'CT'",
            (),
            614,
            when="SELECT count(*) > 1000000 FROM ref_given_freq_current WHERE state IS NOT NULL",
        ),
    ]

    def parts(self, ctx: Context) -> list[Part]:
        ref = ctx.config.reference
        return [
            Part("national", ref.ssa_national_url, "names.zip", "zip"),
            Part("state", ref.ssa_state_url, "namesbystate.zip", "zip"),
        ]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        total = 0
        years: set[int] = set()
        with zipfile.ZipFile(path) as archive:
            members = [
                n
                for n in archive.namelist()
                if (part.key == "national" and re.search(r"yob\d{4}\.txt$", n, re.I))
                or (part.key == "state" and re.search(r"(^|/)[A-Z]{2}\.TXT$", n, re.I))
            ]
            if not members:
                raise LayoutError(f"no {part.key} name files in the ZIP ({archive.namelist()[:6]})")

            def rows() -> Any:
                nonlocal total
                for member in sorted(members):
                    year_hint = re.search(r"yob(\d{4})", member, re.I)
                    for line in archive.read(member).decode("utf-8", errors="replace").splitlines():
                        cells = line.strip().split(",")
                        if part.key == "national" and len(cells) == 3 and year_hint:
                            name, sex, count = cells
                            year, state = int(year_hint.group(1)), None
                        elif part.key == "state" and len(cells) == 5:
                            state, sex, year_text, name, count = cells
                            year = int(year_text)
                        else:
                            continue
                        years.add(year)
                        total += 1
                        yield (load_id, name, sex, year, state, int(count))

            for batch in batches(rows()):
                conn.executemany("INSERT INTO ref_given_freq VALUES (?,?,?,?,?,?)", batch)
        if not total:
            raise LayoutError("no name rows")
        return Counter(
            total, {"first_year": min(years), "last_year": max(years), "files": len(members)}
        )


def age_band(age: int) -> str:
    if age >= 85:
        return "85+"
    low = (age // 5) * 5
    return f"{low}-{low + 4}"


class CensusPopulation(Dataset):
    name = "census_population"
    table = "ref_population"
    license = "Public domain (U.S. Census Bureau)"
    attribution = (
        "State population by age: U.S. Census Bureau, Population Estimates Program, "
        "State Population by Characteristics."
    )
    version = "2020-2023"
    known: ClassVar[list[Known]] = [
        Known(
            "Connecticut civilian population, 2023 (about 3.6 million)",
            "SELECT sum(count) FROM ref_population_current WHERE state = 'CT' AND year = 2023",
            (),
            3_600_000,
            200_000,
        ),
        Known(
            "jurisdictions in the state file (50 states and DC)",
            "SELECT count(DISTINCT state) FROM ref_population_current",
            (),
            51,
        ),
    ]

    def parts(self, ctx: Context) -> list[Part]:
        return [Part("", ctx.config.reference.census_population_url, "state_agesex.csv", "csv")]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        raw = path.read_bytes()
        reader = _csv_rows(raw)
        names = {f.strip().upper(): f for f in (reader.fieldnames or [])}
        year_cols = {
            int(m.group(1)): original
            for key, original in names.items()
            if (m := re.fullmatch(r"POP(?:ESTIMATE|EST)(\d{4})(?:_CIV)?", key))
        }
        if not {"NAME", "SEX", "AGE"} <= set(names) or not year_cols:
            raise LayoutError(
                f"expected NAME, SEX, AGE and POPESTIMATE<year>; got {reader.fieldnames}"
            )
        sums: dict[tuple[str, str, int], int] = {}
        for rec in reader:
            if str(rec[names["SEX"]]).strip() != "0":  # 0 = both sexes
                continue
            code = state_code(rec[names["NAME"]])
            age_text = str(rec[names["AGE"]]).strip()
            if code is None or not age_text.isdigit() or int(age_text) > 85:
                continue  # the national row, a total row, or an age summary
            band = age_band(int(age_text))
            for year, column in year_cols.items():
                key = (code, band, year)
                sums[key] = sums.get(key, 0) + int(float(rec[column] or 0))
        if not sums:
            raise LayoutError("no state rows with SEX = 0")
        for batch in batches((load_id, s, b, y, n) for (s, b, y), n in sorted(sums.items())):
            conn.executemany("INSERT INTO ref_population VALUES (?,?,?,?,?)", batch)
        return Counter(len(sums), {"years": sorted(year_cols), "states": len({k[0] for k in sums})})
