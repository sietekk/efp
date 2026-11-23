"""Census place-by-county codes (build 12): which county or counties a city or town lies in.

Used only by identity's city comparison ("same county" is a level between "same city" and "same
state"). The 2020 national place-by-county codes file is pipe-delimited, one row per
place-and-county
overlap: ``STATE|STATEFP|COUNTYFP|COUNTYNAME|PLACEFP|PLACENS|PLACENAME|TYPE|CLASSFP|FUNCSTAT``, the
names carrying their legal suffix ("Springfield city", "Hartford County"). Public domain (U.S.
Census Bureau). The 2020 relationship files, which carry land areas, were not found at a stable
URL, so no overlap area is kept.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import ClassVar

from efp.reference.base import Counter, Dataset, Known, LayoutError, batches
from efp.reference.files import Part
from efp.types import Context

# state FIPS code -> USPS code (kept for files that give only the FIPS code)
FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}  # fmt: skip

_PLACE_SUFFIX = re.compile(
    r"\s+(?:city|town|village|borough|township|cdp|municipality|plantation|"
    r"city and borough|consolidated government|metropolitan government|metro government|"
    r"urban county|unified government|comunidad|zona urbana|\(balance\))\s*$",
    re.I,
)
_COUNTY_SUFFIX = re.compile(
    r"\s+(?:county|parish|borough|census area|municipality|municipio)\s*$", re.I
)
_NON_LETTER = re.compile(r"[^A-Z]")


def place_key(name: str) -> str:
    """``"Springfield city"`` -> ``"SPRINGFIELD"`` (also ``"St. Louis"`` -> ``"STLOUIS"``)."""
    text = (name or "").strip()
    for _ in range(3):  # "Nashville-Davidson metropolitan government (balance)"
        stripped = _PLACE_SUFFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    return _NON_LETTER.sub("", text.upper())


def county_key(name: str) -> str:
    return _NON_LETTER.sub("", _COUNTY_SUFFIX.sub("", (name or "").strip()).upper())


class CensusPlaceCounty(Dataset):
    name = "census_place_county"
    table = "ref_place_county"
    license = "Public domain (U.S. Census Bureau)"
    attribution = "Place-by-county codes: U.S. Census Bureau, 2020 Geographic Codes."
    version = "2020"
    known: ClassVar[list[Known]] = [
        Known(
            "Connecticut places listed (169 towns plus census-designated places: at least 120)",
            "SELECT count(DISTINCT place_key) FROM ref_place_county_current WHERE state = 'CT'",
            (),
            200,
            120,
        ),
    ]

    def parts(self, ctx: Context) -> list[Part]:
        return [Part("", ctx.config.reference.census_place_county_url, "place_county.txt", "text")]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="|")
        names = {f.strip().upper(): f for f in (reader.fieldnames or [])}
        need = ("STATE", "COUNTYNAME", "PLACENAME")
        if not all(n in names for n in need):
            raise LayoutError(f"expected {need}; got {reader.fieldnames}")
        seen: set[tuple[str, str, str]] = set()
        for rec in reader:
            state = str(rec[names["STATE"]] or "").strip().upper()
            place = place_key(str(rec[names["PLACENAME"]] or ""))
            county = county_key(str(rec[names["COUNTYNAME"]] or ""))
            if len(state) != 2 or not (place and county):
                continue  # a row without a place or county name
            seen.add((state, place, county))
        if not seen:
            raise LayoutError("no place-and-county rows")
        rows = ((load_id, s, p, c, 0) for s, p, c in sorted(seen))
        for batch in batches(rows):
            conn.executemany("INSERT INTO ref_place_county VALUES (?,?,?,?,?)", batch)
        return Counter(len(seen), {"states": len({k[0] for k in seen})})
