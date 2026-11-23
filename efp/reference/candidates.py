"""The candidate universe loaders: FEC Candidate Master, congress-legislators, OpenStates people,
Harvard local elections, DIME, FollowTheMoney. Each fills one ``ref_*`` table with the name split at
load; ``efp.reference.index`` unifies them in ``ref_candidates_all``.

Party fields are identity signals for public figures only. They are never evidence of motive
(build 18 enforces it).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

from efp.capture import CaptureError
from efp.reference import files
from efp.reference.base import Counter, Dataset, Known, LayoutError, batches
from efp.reference.files import Fetched, Part
from efp.reference.names import Split, split_name, split_parts
from efp.reference.states import state_code
from efp.types import Context


def _yaml_load(raw: bytes) -> Any:
    import yaml

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return yaml.load(raw, Loader=loader)  # noqa: S506 - a safe loader


def _year(text: object) -> int | None:
    match = re.match(r"\s*(\d{4})", str(text or ""))
    return int(match.group(1)) if match else None


def _insert(
    conn: sqlite3.Connection, table: str, width: int, rows: Iterator[tuple[Any, ...]]
) -> int:
    marks = ",".join("?" * width)
    count = 0
    for batch in batches(rows):
        conn.executemany(f"INSERT INTO {table} VALUES ({marks})", batch)  # noqa: S608 (constants)
        count += len(batch)
    return count


def _cols(split: Split) -> tuple[str, str, str, str]:
    return split.surname, split.given, split.middle, split.key


# ---- FEC ---------------------------------------------------------


class FecCandidates(Dataset):
    name = "fec_candidates"
    table = "ref_fec_candidates"
    license = "Public domain (Federal Election Commission)"
    attribution = "Federal candidates: Federal Election Commission, Candidate Master bulk files."
    version = "cycles"
    known: ClassVar[list[Known]] = [
        Known(
            "FEC candidate P80001571, 2016 cycle: party REP",
            "SELECT party FROM ref_fec_candidates_current "
            "WHERE cand_id = 'P80001571' AND cycle = 2016",
            (),
            "REP",
        ),
        Known(
            "FEC candidate P80000722, 2020 cycle: party DEM",
            "SELECT party FROM ref_fec_candidates_current "
            "WHERE cand_id = 'P80000722' AND cycle = 2020",
            (),
            "DEM",
        ),
    ]

    def parts(self, ctx: Context) -> list[Part]:
        ref = ctx.config.reference
        return [
            Part(str(year), ref.fec_url_template.format(year=year, yy=f"{year % 100:02d}"),
                 f"cn{year % 100:02d}.zip", "zip")
            for year in range(ref.fec_first_cycle, ref.fec_last_cycle + 1, 2)
        ]  # fmt: skip

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        with zipfile.ZipFile(path) as archive:
            member = next((n for n in archive.namelist() if n.lower().endswith("cn.txt")), None)
            if member is None:
                raise LayoutError(f"no cn.txt in the ZIP ({archive.namelist()})")
            text = archive.read(member).decode("latin-1")

        skipped: list[int] = []

        def rows() -> Iterator[tuple[Any, ...]]:
            # split on "\n" only: str.splitlines also breaks on U+0085, U+000B and the like, which
            # latin-1 produces from a stray byte inside a name and would cut a record in two
            for number, raw in enumerate(text.split("\n"), start=1):
                line = raw.rstrip("\r")
                if not line.strip():
                    continue
                cells = line.split("|")
                if len(cells) < 9 or not cells[0].strip():
                    skipped.append(number)  # counted, never dropped silently
                    continue
                name = cells[1].strip()
                yield (
                    load_id, cells[0].strip(), int(part.key), name, *_cols(split_name(name)),
                    cells[2].strip(), _year(cells[3]),
                    state_code(cells[4]) or cells[4].strip() or None,
                    cells[5].strip() or None, cells[6].strip() or None,
                    cells[7].strip() or None, cells[8].strip() or None,
                )  # fmt: skip

        n = _insert(conn, self.table, 15, rows())
        if not n:
            raise LayoutError("no candidate rows in cn.txt")
        return Counter(
            n,
            {
                "cycle": int(part.key),
                "skipped_malformed_lines": len(skipped),
                "first_skipped_lines": skipped[:10],
            },
        )


# ---- congress-legislators ----------------------------------------


class Congress(Dataset):
    name = "congress"
    table = "ref_congress"
    license = "CC0 1.0 (congress-legislators, unitedstates project)"
    attribution = "Federal officeholders and ID crosswalk: the congress-legislators project (CC0)."
    version = "current+historical"
    known: ClassVar[list[Known]] = [
        Known(
            "bioguide B000944: given name Sherrod",
            "SELECT given FROM ref_congress_current WHERE bioguide = 'B000944'",
            (),
            "Sherrod",
        )
    ]

    def parts(self, ctx: Context) -> list[Part]:
        ref = ctx.config.reference
        return [
            Part("current", ref.congress_current_url, "legislators-current.yaml", "yaml"),
            Part("historical", ref.congress_historical_url, "legislators-historical.yaml", "yaml"),
        ]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        data = _yaml_load(path.read_bytes())
        if not isinstance(data, list) or not data or "id" not in data[0]:
            raise LayoutError("expected a YAML list of legislators with an id block")

        def rows() -> Iterator[tuple[Any, ...]]:
            for person in data:
                ids, name = person.get("id") or {}, person.get("name") or {}
                bioguide = ids.get("bioguide")
                if not bioguide:
                    continue
                terms = person.get("terms") or []
                last = terms[-1] if terms else {}
                years = [
                    y for t in terms for y in (_year(t.get("start")), _year(t.get("end"))) if y
                ]
                full = name.get("official_full") or " ".join(
                    p for p in (name.get("first"), name.get("middle"), name.get("last")) if p
                )
                split = split_parts(name.get("last"), name.get("first"), name.get("middle"))
                yield (
                    load_id, bioguide, json.dumps(ids.get("fec") or []), ids.get("opensecrets"),
                    str(ids["icpsr"]) if ids.get("icpsr") else None,
                    str(ids["govtrack"]) if ids.get("govtrack") else None,
                    full, *_cols(split),
                    str((person.get("bio") or {}).get("birthday") or "") or None,
                    last.get("state"), last.get("party"),
                    min(years) if years else None, max(years) if years else None,
                    json.dumps(
                        [
                            {k: t.get(k) for k in ("type", "start", "end", "state", "party")}
                            for t in terms
                        ]
                    ),
                )  # fmt: skip

        n = _insert(conn, self.table, 17, rows())
        if not n:
            raise LayoutError("no legislators with a bioguide id")
        return Counter(n, {"part": part.key})


# ---- OpenStates people -------------------------------------------

_OS_MEMBER = re.compile(r"(?:^|/)data/([a-z]{2})/(legislature|retired)/[^/]+\.ya?ml$", re.I)


class OpenStates(Dataset):
    name = "openstates"
    table = "ref_state_leg"
    license = "CC0 1.0 (OpenStates people repository)"
    attribution = "State legislators, current and retired: the OpenStates people repository (CC0)."
    version = "main"

    def parts(self, ctx: Context) -> list[Part]:
        return [Part("", ctx.config.reference.openstates_archive_url, "people-main.zip", "zip")]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        with zipfile.ZipFile(path) as archive:
            return self._read_archive(conn, archive, load_id)

    def _read_archive(
        self, conn: sqlite3.Connection, archive: zipfile.ZipFile, load_id: int
    ) -> Counter:
        members = [n for n in archive.namelist() if _OS_MEMBER.search(n)]
        if not members:
            raise LayoutError(
                f"no data/<state>/legislature|retired files ({archive.namelist()[:5]})"
            )

        def rows() -> Iterator[tuple[Any, ...]]:
            for member in members:
                match = _OS_MEMBER.search(member)
                assert match is not None  # noqa: S101
                state, folder = match.group(1).upper(), match.group(2).lower()
                person = _yaml_load(archive.read(member))
                if not isinstance(person, dict) or not person.get("id"):
                    continue
                roles = person.get("roles") or []
                last = roles[-1] if roles else {}
                years = [
                    y
                    for r in roles
                    for y in (_year(r.get("start_date")), _year(r.get("end_date")))
                    if y
                ]
                parties = person.get("party") or []
                party = (parties[-1] or {}).get("name") if parties else None
                name = str(person.get("name") or "")
                if person.get("family_name") or person.get("given_name"):
                    split = split_parts(person.get("family_name"), person.get("given_name"), "")
                else:
                    split = split_name(name)
                yield (
                    load_id, str(person["id"]), name, *_cols(split), state, last.get("type"),
                    str(last.get("district") or "") or None, party,
                    min(years) if years else None, max(years) if years else None,
                    1 if folder == "retired" else 0,
                )  # fmt: skip

        n = _insert(conn, self.table, 14, rows())
        if not n:
            raise LayoutError("no state legislators read")
        return Counter(n, {"files": len(members)})


# ---- generic CSV / SQLite candidate files ------------------------


def _pick(row: dict[str, Any], *names: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items() if k}
    for name in names:
        value = lowered.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _csv_dicts(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        yield from csv.DictReader(handle)


_NOT_A_PARTY = {"", "NA", "N/A", "NAN", "NULL", "NONE", "."}


def _party_label(value: str) -> str:
    """A party label, or "" when the cell is a missing-value marker ("NA") or a number (a modelled
    probability is not a party)."""
    text = value.strip()
    if text.upper() in _NOT_A_PARTY or re.fullmatch(r"-?\d+(\.\d+)?", text):
        return ""
    return text


def _split_from_row(row: dict[str, Any], *, full_when_no_last: bool = False) -> tuple[str, Split]:
    """``full_when_no_last``: a row with a first name but no surname part (DIME has 1,816 such
    candidate rows) is split from its full-name cell instead of being dropped."""
    full = _pick(row, "name", "candidate", "candidate_name", "cand_name", "full_name")
    last = _pick(row, "lname", "lastname", "last_name", "last", "surname", "family_name")
    first = _pick(row, "fname", "firstname", "first_name", "first", "given_name")
    middle = _pick(row, "mname", "middle_name", "middle")
    if full_when_no_last and not last and full:
        return full, split_name(full)
    if last or first:
        return full or f"{first} {middle} {last}".replace("  ", " ").strip(), split_parts(
            last, first, middle
        )
    return full, split_name(full)


MAX_OSF_PAGES = 50


def _osf_files(capture: Any, url: str, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Every file of an OSF storage listing, following its pages and its folders (to depth 3)."""
    for _page in range(MAX_OSF_PAGES):
        listing = capture.fetch(url, row_id=None, stage="reference", kind="api")
        payload = json.loads(capture.read_body(listing).decode("utf-8"))
        for entry in payload.get("data", []):
            attrs = entry.get("attributes") or {}
            if attrs.get("kind") == "folder" and depth < 3:
                link = ((entry.get("relationships") or {}).get("files") or {}).get("links") or {}
                href = (link.get("related") or {}).get("href")
                if href:
                    yield from _osf_files(capture, href, depth + 1)
            elif attrs.get("kind") == "file":
                yield entry
        url = (payload.get("links") or {}).get("next") or ""
        if not url:
            return
    raise CaptureError(f"the OSF listing is longer than {MAX_OSF_PAGES} pages: not read in full")


class HarvardLocal(Dataset):
    name = "harvard_local"
    table = "ref_local"
    license = "CC-BY 4.0 (Harvard local elections database)"
    attribution = (
        "Local candidates 1989-2021: Harvard Local Elections Database (Kuriwaki et al.), "
        "doi:10.17605/OSF.IO/MV5E6, CC-BY 4.0. Party is imputed where the race is nonpartisan."
    )
    version = "1989-2021"
    loader_version: ClassVar[int] = 2  # 2: "NA" and numeric scores are not parties

    def parts(self, ctx: Context) -> list[Part]:
        return [Part("", "", "local_elections.csv", "csv")]

    def download(self, ctx: Context, *, refresh: bool = False) -> list[Fetched]:
        """The OSF project lists its files through its API (a capture); the largest CSV is the
        database, and is downloaded from the link the listing gives."""
        part = self.parts(ctx)[0]
        capture: Any = ctx.capture
        try:
            root = (
                f"https://api.osf.io/v2/nodes/{ctx.config.reference.harvard_osf_node}"
                "/files/osfstorage/"
            )
            csvs = [
                d
                for d in _osf_files(capture, root)
                if str((d.get("attributes") or {}).get("name", "")).lower().endswith(".csv")
            ]
            if not csvs:
                raise CaptureError("the OSF listing has no CSV file")
            # the candidate-level database; failing that, the largest CSV
            named = [d for d in csvs if "candidatelevel" in d["attributes"]["name"].lower()]
            best = (named or [max(csvs, key=lambda d: int(d["attributes"].get("size") or 0))])[0]
            link = (best.get("links") or {}).get("download")
            if not link:
                raise CaptureError("the OSF listing gives no download link")
            fetched = files.fetch(
                ctx, self.name, Part("", link, part.filename, "csv"), refresh=refresh
            )
        except (CaptureError, ValueError, KeyError) as exc:
            fetched = Fetched(part, None, "gated", f"the OSF listing could not be read: {exc}")
        if fetched.status != "ok":
            self._record_refusal(ctx, part, fetched)
        return [fetched]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        notes: dict[str, Any] = {}
        seen_columns: list[str] = []

        def rows() -> Iterator[tuple[Any, ...]]:
            for index, row in enumerate(_csv_dicts(path)):
                if not seen_columns:
                    seen_columns.extend(str(k) for k in row)
                full, split = _split_from_row(row)
                if not split.surname:
                    continue
                recorded = _party_label(_pick(row, "party", "party_detailed", "party_clean"))
                estimated = "" if recorded else _party_label(_pick(row, "pid_est"))  # modelled ID
                party: str | None = (recorded or estimated) or None
                flagged = _pick(row, "party_imputed", "nonpartisan", "is_nonpartisan").lower()
                # Party is IMPUTED, flagged and never asserted: where the race is nonpartisan, and
                # always when it is the database's own estimate (pid_est), not a recorded party.
                imputed = (
                    1
                    if flagged in ("1", "true", "yes", "t", "y") or estimated or party is None
                    else 0
                )
                yield (
                    load_id,
                    _pick(row, "id", "ledb_candid", "candidate_id", "record_id") or str(index),
                    full,
                    *_cols(split),
                    state_code(_pick(row, "state", "state_abb", "state_abbrev", "st")) or None,
                    _pick(row, "place", "city", "county", "jurisdiction", "geo_name") or None,
                    _pick(row, "office", "office_consolidated", "office_type") or None,
                    party, imputed,
                    _year(_pick(row, "year", "election_year")),
                )  # fmt: skip

        n = _insert(conn, self.table, 13, rows())
        if not n:
            raise LayoutError(f"no candidate rows; the file's columns are {seen_columns[:20]}")
        notes["columns"] = seen_columns[:40]
        return Counter(n, notes)


DIME_PARTY = {"100": "DEM", "200": "REP", "328": "IND"}  # DIME codebook, in the FEC vocabulary
_DIME_OUTCOME = {"W": 1, "L": 0}


def _present(value: str) -> str:
    """A cell's text, or "" when it is a missing-value marker (``NA``, ``NULL``, ...)."""
    return "" if value.strip().upper() in _NOT_A_PARTY else value.strip()


def _dime_party(code: str) -> str | None:
    return DIME_PARTY.get(code.strip())


class Dime(Dataset):
    """DIME v4 recipients. The file has one row per recipient per cycle, committees included.

    Candidates only (``recipient.type`` = ``cand``) are kept; when a build has no such column every
    row is kept. ``party`` is the mapped label and ``party_code`` the raw DIME code.
    """

    name = "dime"
    table = "ref_dime"
    license = "ODC-BY 1.0 (DIME, Stanford)"
    attribution = (
        "Candidates and CFscores: Bonica, Adam. Database on Ideology, Money in Politics, and "
        "Elections (DIME), v4.0, Stanford University Libraries, ODC-BY 1.0."
    )
    version = "4.0"
    # 2: candidates only; mapped party and the raw code; suffix, gender, outcome, status, fecyear;
    #    a candidate with no surname part is split from the full name
    loader_version: ClassVar[int] = 2
    needs_url: ClassVar[str | None] = "dime_url"
    known: ClassVar[list[Known]] = [
        Known(
            "DIME candidate rows for a U.S. House seat in the 2012 cycle (committees excluded)",
            "SELECT count(*) FROM ref_dime_current WHERE cycle = 2012 AND office = 'federal:house'",
            (),
            1823,
            when="SELECT count(*) > 100000 FROM ref_dime_current",
        )
    ]

    def parts(self, ctx: Context) -> list[Part]:
        url = ctx.config.reference.dime_url
        directory = files.dataset_dir(ctx.config, self.name)
        existing = sorted(
            p
            for p in directory.glob("*")
            if p.suffix.lower() in (".sqlite", ".sqlite3", ".db", ".csv")
        ) if directory.is_dir() else []  # fmt: skip
        name = existing[0].name if existing else (url.rsplit("/", 1)[-1] if url else "dime.sqlite")
        kind = "sqlite" if name.lower().endswith((".sqlite", ".sqlite3", ".db")) else "csv"
        return [Part("", url, name or "dime.sqlite", kind)]

    def import_part(self, ctx: Context, source: Path, key: str | None) -> Part:
        kind = "sqlite" if source.suffix.lower() in (".sqlite", ".sqlite3", ".db") else "csv"
        return Part("", "", source.name, kind)

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        seen: list[str] = []
        skipped = {"committees": 0, "no_id_or_surname": 0}

        def source() -> Iterator[dict[str, Any]]:
            if part.kind == "csv":
                yield from _csv_dicts(path)
                return
            lite = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            lite.row_factory = sqlite3.Row
            try:
                tables = [
                    r[0] for r in lite.execute("SELECT name FROM sqlite_master WHERE type='table'")
                ]
                table = next((t for t in tables if "recipient" in t.lower()), None)
                if table is None:
                    raise LayoutError(
                        f"no recipients table in the SQLite file (tables: {tables[:8]})"
                    )
                for rec in lite.execute(f'SELECT * FROM "{table}"'):  # noqa: S608 (a name read above)
                    yield dict(rec)
            finally:
                lite.close()

        def rows() -> Iterator[tuple[Any, ...]]:
            for rec in source():
                if not seen:
                    seen.extend(str(k) for k in rec)
                kind = _pick(rec, "recipient.type", "recipient_type").lower()
                if kind and kind != "cand":
                    skipped["committees"] += 1
                    continue
                rid = _pick(rec, "bonica.rid", "bonica_rid", "bonica rid", "rid")
                full, split = _split_from_row(rec, full_when_no_last=True)
                if not rid or not split.surname:
                    skipped["no_id_or_surname"] += 1
                    continue
                score = _pick(rec, "recipient.cfscore", "cfscore", "recipient_cfscore")
                code = _pick(rec, "party")
                suffix = _present(_pick(rec, "suffix")).split(" ")[0].upper()
                gender = _pick(rec, "cand.gender", "gender").upper()
                yield (
                    load_id, rid, _year(_pick(rec, "cycle")), full, *_cols(split),
                    state_code(_pick(rec, "state")) or None, _pick(rec, "district") or None,
                    _pick(rec, "seat", "office") or None, _dime_party(code),
                    float(score) if re.fullmatch(r"-?\d+(\.\d+)?", score or "") else None,
                    code or None, suffix or None, gender if gender in ("M", "F") else None,
                    _DIME_OUTCOME.get(_pick(rec, "gwinner").upper()),
                    _DIME_OUTCOME.get(_pick(rec, "pwinner").upper()),
                    _present(_pick(rec, "ico.status", "ico_status")).upper() or None,
                    _year(_pick(rec, "fecyear")), _present(_pick(rec, "nname")) or None,
                )  # fmt: skip

        n = _insert(conn, self.table, 21, rows())
        if not n:
            raise LayoutError(f"no recipient rows; the file's columns are {seen[:25]}")
        return Counter(n, {"columns": seen[:80], "excluded": skipped, "kept": "candidates only"})


class Ftm(Dataset):
    name = "ftm"
    table = "ref_ftm"
    license = "Attribution (FollowTheMoney / National Institute on Money in Politics)"
    attribution = (
        "State candidates: FollowTheMoney, National Institute on Money in Politics "
        "(followthemoney.org); the site is no longer maintained."
    )
    version = "through 2024"
    needs_url: ClassVar[str | None] = "ftm_url"

    def parts(self, ctx: Context) -> list[Part]:
        return [Part("", ctx.config.reference.ftm_url, "ftm_candidates.csv", "csv")]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        seen: list[str] = []

        def rows() -> Iterator[tuple[Any, ...]]:
            for index, rec in enumerate(_csv_dicts(path)):
                if not seen:
                    seen.extend(str(k) for k in rec)
                full, split = _split_from_row(rec)
                if not split.surname:
                    continue
                state = state_code(_pick(rec, "state", "candidate_state", "election_state")) or None
                office = _pick(rec, "office", "candidate_office", "office_sought") or None
                year = _year(_pick(rec, "year", "election_year"))
                ident = (
                    _pick(rec, "id", "candidate_id", "eid", "ftm_id")
                    or hashlib.sha1(  # noqa: S324
                        f"{full}|{state}|{office}|{year}|{index}".encode()
                    ).hexdigest()[:16]
                )
                won = _pick(
                    rec, "won", "winner", "status", "candidate_status", "election_status"
                ).lower()
                yield (
                    load_id, ident, full, *_cols(split), state, office,
                    _pick(rec, "party", "candidate_party", "general_party") or None, year,
                    1 if won in ("1", "true", "yes", "won", "winner", "w") else 0,
                )  # fmt: skip

        n = _insert(conn, self.table, 12, rows())
        if not n:
            raise LayoutError(f"no candidate rows; the file's columns are {seen[:25]}")
        return Counter(n, {"columns": seen[:40]})


_ = io  # (kept for loaders that read in-memory members)
