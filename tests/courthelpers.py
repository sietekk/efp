"""Synthetic fixtures for the court tests: rows, DOJ pages, bulk CSVs and a capture client.

Everything here is invented. Names are fictional, docket numbers are made up, and the "bulk" files
follow the layout the loader expects (dockets, courts, parties, party-types), at a few hundred rows.
"""

from __future__ import annotations

import bz2
import csv
import io
import random
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import efp.court as component
from efp.capture import Capture
from efp.config import Config, load_config
from efp.court.modules import register_modules
from efp.modules import Registry
from efp.runner import load_row, make_context
from efp.types import Context, Row
from tests.capturehelpers import make_capture
from tests.dbhelpers import STAMP, ZERO

HOSTS = (
    "www.justice.gov",
    "www.courtlistener.com",
    "storage.courtlistener.com",
    "bulk.example.test",
    "example.test",
)
COURT_CHAIN = (
    'modules = ["court.doj", "court.pin_reports", "court.state_ag", "court.courtlistener_bulk", '
    '"court.courtlistener_api", "court.recap", "court.state_docket"]'
)
ARTICLE = "The county clerk confirmed the filing and the court record shows the plea entered. " * 14


def court_config(write_config: Callable[..., Path], **replace: str) -> Config:
    """The suite's config with the full seven-module court chain (the fixture lists one)."""
    queue = Path(write_config()).parent / "pacer.csv"  # never the repository's own queues/
    return load_config(
        write_config(
            {
                'modules = ["court.doj"]': COURT_CHAIN,
                'pacer_queue = "queues/pacer.csv"': f'pacer_queue = "{queue}"',
                **replace,
            }
        )
    )


def add_court_row(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    name: str = "Alex Example",
    year: int = 2014,
    state: str = "CT",
    details: str | None = None,
    source: str | None = None,
    sha: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO rows (row_id, year, state, name, details, source_raw, in_scope, row_sha256, "
        "ingested_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (row_id, year, state, name, details, source, sha or f"{row_id:064d}", STAMP),
    )
    conn.execute(
        "INSERT INTO case_stage (row_id, stage, status) VALUES (?, 'court', 'pending')", (row_id,)
    )
    conn.commit()


def make_ctx(config: Config, conn: sqlite3.Connection) -> tuple[Context, Capture, Registry]:
    cap = make_capture(config, conn, hosts=HOSTS)
    ctx = make_context(config, conn).model_copy(update={"capture": cap})
    registry = Registry()
    register_modules(registry)
    return ctx, cap, registry


def row_of(conn: sqlite3.Connection, row_id: int) -> Row:
    return load_row(conn, row_id)


def process(config: Config, conn: sqlite3.Connection, row_id: int):  # type: ignore[no-untyped-def]
    ctx, cap, registry = make_ctx(config, conn)
    try:
        return component.process_row(row_of(conn, row_id), ctx, registry)
    finally:
        cap.close()


def release(
    title: str,
    body: str,
    *,
    date: str = "2014-03-05",
    uuid: str = "u1",
    url: str = "https://www.justice.gov/usao-ct/pr/example-release",
) -> dict[str, Any]:
    stamp = int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp())
    return {"uuid": uuid, "title": title, "url": url, "date": str(stamp), "body": f"<p>{body}</p>"}


def doj_page(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "metadata": {"resultset": {"count": len(items), "pagesize": 50}},
        "results": list(items),
    }


def release_html(text: str, title: str = "Press release") -> str:
    return f"<html><head><title>{title}</title></head><body><article><p>{text}</p></article></body></html>"


# --- bulk CSVs --------------------------------------------------------------------------------

COURTS = [
    # id, full_name, short_name, jurisdiction
    ("ctd", "District Court, D. Connecticut", "D. Conn.", "FD"),
    ("nysd", "District Court, S.D. New York", "S.D.N.Y.", "FD"),
    ("ohnd", "District Court, N.D. Ohio", "N.D. Ohio", "FD"),
    ("ca2", "Court of Appeals for the Second Circuit", "2d Cir.", "F"),
    ("ctb", "Bankruptcy Court, D. Connecticut", "Bankr. D. Conn.", "FB"),
    ("conn", "Connecticut Supreme Court", "Conn.", "S"),
]
SURNAMES = [
    "Alder",
    "Birch",
    "Cedar",
    "Dunmore",
    "Ellery",
    "Fairbanks",
    "Garrow",
    "Hollis",
    "Ives",
    "Jarrett",
]
GIVEN = ["Sam", "Pat", "Lee", "Kim", "Jo", "Max", "Ray", "Dale", "Cory", "Drew"]


def bulk_rows(seed: int = 7, extra: int = 300) -> dict[str, list[list[str]]]:
    """Invented dockets, parties and links, with decoys around two planted targets."""
    rnd = random.Random(seed)  # noqa: S311  (test data, not crypto)
    dockets: list[list[str]] = []
    parties: dict[int, str] = {}
    links: list[list[str]] = []
    next_party = 1000

    def add(
        did: int, court: str, number: str, name: str, filed: str, ended: str, party: str | None
    ) -> None:
        nonlocal next_party
        dockets.append(
            [
                str(did),
                court,
                number,
                name,
                filed,
                ended,
                f"{did}",
                f"/docket/{did}/{name.lower().replace(' ', '-')}/",
            ]
        )
        if party:
            parties[next_party] = party
            links.append([str(did), str(next_party), "Defendant"])
            next_party += 1

    # the planted targets
    add(
        1,
        "ctd",
        "3:13-cr-00045",
        "United States v. Example",
        "2013-06-01",
        "2015-02-01",
        "Alex Example",
    )
    add(
        2,
        "ohnd",
        "1:12-cr-00210",
        "United States v. Rowan",
        "2012-04-10",
        "2014-09-09",
        "Chris Rowan",
    )
    # decoys: same surname in another state; a civil docket; a bankruptcy court; a pre-1999 case
    add(
        3,
        "nysd",
        "1:13-cr-00777",
        "United States v. Example",
        "2013-05-05",
        "2014-11-11",
        "Alex Example",
    )
    add(
        4,
        "ctd",
        "3:13-cv-00046",
        "Example v. Widgets Inc.",
        "2013-06-02",
        "2014-01-01",
        "Alex Example",
    )
    add(
        5,
        "ctb",
        "3:13-cr-00047",
        "United States v. Example",
        "2013-06-03",
        "2014-01-01",
        "Alex Example",
    )
    add(
        6,
        "ctd",
        "3:95-cr-00001",
        "United States v. Example",
        "1995-01-01",
        "1996-01-01",
        "Alex Example",
    )
    add(
        7,
        "ctd",
        "3:13-cr-00048",
        "United States v. Example",
        "2013-06-04",
        "2015-02-02",
        "Bo Example",
    )
    for i in range(extra):
        court = rnd.choice(["ctd", "nysd", "ohnd", "ctd", "ca2"])
        number = f"{rnd.randint(1, 3)}:{rnd.randint(10, 20)}-cr-{rnd.randint(1, 999):05d}"
        surname, given = rnd.choice(SURNAMES), rnd.choice(GIVEN)
        add(
            100 + i,
            court,
            number,
            f"United States v. {surname}",
            f"20{rnd.randint(10, 20)}-03-01",
            "",
            f"{given} {surname}",
        )
    party_rows = [[str(pid), name] for pid, name in parties.items()]
    court_rows = [list(c) for c in COURTS]
    return {"dockets": dockets, "courts": court_rows, "parties": party_rows, "party-types": links}


HEADERS = {
    "dockets": [
        "id",
        "court_id",
        "docket_number",
        "case_name",
        "date_filed",
        "date_terminated",
        "pacer_case_id",
        "absolute_url",
    ],
    "courts": ["id", "full_name", "short_name", "jurisdiction"],
    "parties": ["id", "name"],
    "party-types": ["docket_id", "party_id", "name"],
}


def write_bulk_files(
    folder: Path,
    *,
    date: str = "2026-09-30",
    compress: bool = True,
    headers: dict[str, list[str]] | None = None,
) -> dict[str, Path]:
    """Write the four CSVs the loader reads (bz2 by default, like the real ones)."""
    folder.mkdir(parents=True, exist_ok=True)
    data = bulk_rows()
    paths: dict[str, Path] = {}
    for kind, rows in data.items():
        header = (headers or HEADERS)[kind]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        writer.writerows(rows)
        path = folder / (f"{kind}-{date}.csv" + (".bz2" if compress else ""))
        if compress:
            path.write_bytes(bz2.compress(buffer.getvalue().encode()))
        else:
            path.write_text(buffer.getvalue())
        paths[kind] = path
    return paths


def s3_listing(entries: list[tuple[str, int]], *, token: str | None = None) -> str:
    items = "".join(
        f"<Contents><Key>{key}</Key><LastModified>2026-10-01T00:00:00.000Z</LastModified>"
        f"<Size>{size}</Size></Contents>"
        for key, size in entries
    )
    more = (
        f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>"
        if token
        else "<IsTruncated>false</IsTruncated>"
    )
    return f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}{more}</ListBucketResult>'


def record_manifests(config: Any, conn: Any, paths: dict[str, Path]) -> None:
    """Record a bulk_download manifest capture for each file, as ``Capture.download`` would."""
    import hashlib
    import json as _json

    from tests.capturehelpers import make_capture

    capture = make_capture(config, conn)
    try:
        for path in paths.values():
            if not path.is_file():
                continue
            body = path.read_bytes()
            manifest = {
                "url": f"https://bulk.example.test/bulk-data/{path.name}",
                "path": str(path),
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            capture.store_bytes(
                _json.dumps(manifest, sort_keys=True).encode(),
                row_id=None,
                stage="bulk",
                url_requested=manifest["url"],
                tool="bulk_download",
                headers=manifest,
            )
    finally:
        capture.close()


__all__ = ["ZERO"]
