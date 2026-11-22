"""Trusted time and fixity: the weekly hash manifest, RFC 3161 tokens, and the BagIt bag.

* **Manifest.** Every ``captures.content_sha256`` added since the last manifest, one line per body
  (``<sha256>  <body_path>``, sorted), under a small header. Written once, read-only, hashed, and
  recorded in ``manifests`` / ``manifest_captures``. One timestamp per week bounds the cost
  instead of one per capture.
* **Tokens.** The manifest's SHA-256 is timestamped by two independent public TSAs
  (``capture.tsa_urls``). Each request goes through :class:`efp.capture.Capture`, so it is
  budgeted, logged and recorded like every other network call. Tokens are stored beside the
  manifest, and ``captures.tsa_token_path`` is set once (migration 0006) for the covered rows.
* **Verification.** A token checks out when the TSA granted it and its message imprint is the
  SHA-256 of the manifest file as it stands. The signature chain is checked too when trust anchors
  for the TSAs are pinned in ``reference/tsa/*.pem``; with none pinned the result says the
  signature was not checked, instead of claiming it was.
* **Bag.** A BagIt (RFC 8493) bag over the content store, with SHA-256 and SHA-512 manifests,
  built from hard links so the store itself is never rearranged.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import bagit
from cryptography import x509
from rfc3161_client import (
    HashAlgorithm,
    PKIStatus,
    TimestampRequestBuilder,
    VerifierBuilder,
    decode_timestamp_response,
)

from efp.capture import Capture, CaptureError
from efp.config import Config
from efp.version import repo_root

HEADER_MAGIC = "# efp-manifest 1"
# The one permitted update to captures for a timestamp: NULL to a value, once, for the covered rows.
TSA_SQL = (
    "UPDATE captures SET tsa_token_path = ? WHERE tsa_token_path IS NULL AND capture_id IN "
    "(SELECT capture_id FROM manifest_captures WHERE manifest_id = ?)"
)
TSA_CONTENT_TYPE = "application/timestamp-query"


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class ManifestResult:
    manifest_id: int
    path: Path
    sha256: str
    entry_count: int
    first_capture_id: int
    last_capture_id: int
    iso_week: str


@dataclass(frozen=True)
class TokenResult:
    tsa_url: str
    path: Path
    gen_time: str | None


@dataclass(frozen=True)
class TimestampOutcome:
    tokens: list[TokenResult]
    errors: list[str]

    @property
    def independent_tsas(self) -> int:
        return len({t.tsa_url for t in self.tokens})


@dataclass(frozen=True)
class Verification:
    tsa_url: str
    token: Path
    granted: bool
    imprint_matches: bool
    gen_time: str | None
    signature: bool | None  # None: not checked (no trust anchors pinned)
    detail: str

    @property
    def ok(self) -> bool:
        return self.granted and self.imprint_matches and self.signature is not False


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- the manifest ---------------------------------------------------------------------------


def _manifest_path(directory: Path, moment: datetime) -> Path:
    base = f"{moment:%Y-%m-%d}"
    candidate = directory / f"{base}.txt"
    n = 1
    while candidate.exists():
        n += 1
        candidate = directory / f"{base}-{n}.txt"
    return candidate


def build_manifest(
    config: Config,
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    week: str | None = None,
) -> ManifestResult | None:
    """Write a manifest of every capture not yet covered by one. None if there is nothing new.

    ``week`` (``YYYY-Www``) limits it to captures retrieved in that ISO week. The selection and the
    ``manifest_captures`` rows are one write transaction, so two runs at once cannot list the same
    capture twice; a failure removes the manifest file it had just written.
    """
    created: list[Path] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _build_manifest(config, conn, now=now, week=week, created=created)
        conn.commit()
        return result
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        for path in created:
            path.chmod(0o644)
            path.unlink(missing_ok=True)
        raise


def _build_manifest(
    config: Config,
    conn: sqlite3.Connection,
    *,
    now: datetime | None,
    week: str | None,
    created: list[Path],
) -> ManifestResult | None:
    moment = _now(now)
    where, params = "capture_id NOT IN (SELECT capture_id FROM manifest_captures)", []
    if week:
        where += " AND strftime('%G-W%V', retrieved_at) = ?"
        params.append(week)
    rows = conn.execute(
        f"SELECT capture_id, content_sha256, body_path FROM captures WHERE {where} "  # noqa: S608
        "ORDER BY capture_id",
        params,
    ).fetchall()
    if not rows:
        return None
    entries = sorted({(str(sha), str(path)) for _, sha, path in rows})
    previous = conn.execute(
        "SELECT sha256 FROM manifests ORDER BY manifest_id DESC LIMIT 1"
    ).fetchone()
    first, last = int(rows[0][0]), int(rows[-1][0])
    iso_week = week or _iso_week(moment)
    lines = [
        HEADER_MAGIC,
        f"# created: {moment.isoformat(timespec='seconds')}",
        f"# iso-week: {iso_week}",
        f"# previous: {previous[0] if previous else 'none'}",
        f"# captures: first={first} last={last}",
        f"# entries: {len(entries)}",
        *(f"{sha}  {path}" for sha, path in entries),
    ]
    body = ("\n".join(lines) + "\n").encode()
    config.paths.manifests.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(config.paths.manifests, moment)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(body)
    os.chmod(tmp, 0o444)
    os.link(tmp, path)  # never replaces an existing manifest
    created.append(path)
    tmp.unlink()
    sha = hashlib.sha256(body).hexdigest()
    cursor = conn.execute(
        "INSERT INTO manifests (path, sha256, iso_week, first_capture_id, last_capture_id, "
        "entry_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (path.name, sha, iso_week, first, last, len(entries), moment.isoformat(timespec="seconds")),
    )
    manifest_id = cursor.lastrowid
    if manifest_id is None:
        raise ManifestError("manifest row was not inserted")
    conn.executemany(
        "INSERT INTO manifest_captures (manifest_id, capture_id) VALUES (?, ?)",
        [(manifest_id, int(r[0])) for r in rows],
    )
    return ManifestResult(manifest_id, path, sha, len(entries), first, last, iso_week)


# --- RFC 3161 -------------------------------------------------------------------------------


class Tsa(Protocol):
    """Sends a DER timestamp request to a TSA and returns the DER response."""

    def post(self, url: str, request_der: bytes) -> bytes: ...


class CaptureTsa:
    """The real transport: the request goes through the capture layer like any other call."""

    def __init__(self, capture: Capture) -> None:
        self._capture = capture

    def post(self, url: str, request_der: bytes) -> bytes:
        record = self._capture.fetch(
            url,
            row_id=None,
            stage="manifest",
            kind="api",
            method="POST",
            retry=True,
            headers={"Content-Type": TSA_CONTENT_TYPE, "Accept": "application/timestamp-reply"},
            data=request_der,
        )
        if not record.ok:
            raise ManifestError(f"{url} answered HTTP {record.http_status}")
        return self._capture.read_body(record)


def _token_name(manifest: Path, tsa_url: str) -> str:
    host = urlsplit(tsa_url).hostname or "tsa"
    return f"{manifest.name}.{host}.tsr"


def timestamp_manifest(
    config: Config,
    conn: sqlite3.Connection,
    manifest_id: int,
    tsa: Tsa,
    *,
    now: datetime | None = None,
) -> TimestampOutcome:
    """Get a token for the manifest from every configured TSA that answers; store them beside it.

    Raises only if no TSA granted one; a TSA that failed while another succeeded is reported in
    the outcome's ``errors`` so the caller can say the timestamp has fewer than two witnesses."""
    row = conn.execute(
        "SELECT path, sha256 FROM manifests WHERE manifest_id = ?", (manifest_id,)
    ).fetchone()
    if row is None:
        raise ManifestError(f"no manifest {manifest_id}")
    manifest = config.paths.manifests / row[0]
    data = manifest.read_bytes()
    if hashlib.sha256(data).hexdigest() != row[1]:
        raise ManifestError(
            f"{manifest.name} no longer matches its recorded hash; not timestamping"
        )
    request = (
        TimestampRequestBuilder()
        .data(data)
        .hash_algorithm(HashAlgorithm.SHA256)
        .nonce(nonce=True)
        .cert_request(cert_request=True)
        .build()
    )
    tokens: list[TokenResult] = []
    errors: list[str] = []
    for url in config.capture.tsa_urls:
        try:
            der = tsa.post(url, request.as_bytes())
            response = decode_timestamp_response(der)
        except (CaptureError, ManifestError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        if response.status not in (PKIStatus.GRANTED, PKIStatus.GRANTED_WITH_MODS):
            errors.append(f"{url}: not granted (status {int(response.status)})")
            continue
        if response.tst_info.message_imprint.message != hashlib.sha256(data).digest():
            errors.append(f"{url}: the token is for different data than the manifest we sent")
            continue
        path = config.paths.manifests / _token_name(manifest, url)
        path.write_bytes(der)
        gen_time = response.tst_info.gen_time.astimezone(UTC).isoformat(timespec="seconds")
        conn.execute(
            "DELETE FROM manifest_tokens WHERE manifest_id = ? AND tsa_url = ?", (manifest_id, url)
        )
        conn.execute(
            "INSERT INTO manifest_tokens (manifest_id, tsa_url, path, token_sha256, "
            "gen_time, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                manifest_id,
                url,
                path.name,
                hashlib.sha256(der).hexdigest(),
                gen_time,
                _now(now).isoformat(timespec="seconds"),
            ),
        )
        tokens.append(TokenResult(url, path, gen_time))
    if tokens:
        # The one permitted update to captures besides wayback_url: NULL to a value, once.
        conn.execute(TSA_SQL, (tokens[0].path.name, manifest_id))
    conn.commit()
    if not tokens:
        raise ManifestError("no TSA granted a token: " + "; ".join(errors))
    return TimestampOutcome(tokens, errors)


def _trust_anchors() -> list[x509.Certificate]:
    root = repo_root()
    folder = (root / "reference" / "tsa") if root else None
    anchors: list[x509.Certificate] = []
    if folder is not None and folder.is_dir():
        for pem in sorted(folder.glob("*.pem")):
            with contextlib.suppress(ValueError):
                anchors.extend(x509.load_pem_x509_certificates(pem.read_bytes()))
    return anchors


def verify_manifest(
    config: Config, conn: sqlite3.Connection, manifest_id: int
) -> list[Verification]:
    """Re-check every stored token of a manifest against the manifest file as it stands now."""
    row = conn.execute(
        "SELECT path, sha256 FROM manifests WHERE manifest_id = ?", (manifest_id,)
    ).fetchone()
    if row is None:
        raise ManifestError(f"no manifest {manifest_id}")
    manifest = config.paths.manifests / row[0]
    data = manifest.read_bytes()
    current = hashlib.sha256(data).digest()
    anchors = _trust_anchors()
    out: list[Verification] = []
    tokens = conn.execute(
        "SELECT tsa_url, path FROM manifest_tokens WHERE manifest_id = ? ORDER BY token_id",
        (manifest_id,),
    ).fetchall()
    for tsa_url, name in tokens:
        path = config.paths.manifests / name
        try:
            response = decode_timestamp_response(path.read_bytes())
        except (OSError, ValueError) as exc:
            out.append(Verification(tsa_url, path, False, False, None, None, f"unreadable: {exc}"))
            continue
        granted = response.status in (PKIStatus.GRANTED, PKIStatus.GRANTED_WITH_MODS)
        imprint = response.tst_info.message_imprint.message == current
        gen_time = response.tst_info.gen_time.astimezone(UTC).isoformat(timespec="seconds")
        signature: bool | None = None
        detail = "imprint matches the manifest" if imprint else "the manifest has changed since"
        if granted and imprint and anchors:
            try:
                builder = VerifierBuilder()
                for anchor in anchors:
                    builder = builder.add_root_certificate(anchor)
                signature = bool(builder.build().verify_message(response, data))
                detail += "; signature chain verified"
            except Exception as exc:  # the library raises its own VerificationError
                signature = False
                detail += f"; signature check failed: {type(exc).__name__}"
        elif granted and imprint:
            detail += "; signature NOT checked (pin TSA roots in reference/tsa/*.pem)"
        out.append(Verification(tsa_url, path, granted, imprint, gen_time, signature, detail))
    if not out:
        raise ManifestError(f"manifest {manifest_id} has no stored tokens")
    return out


def latest_manifest_id(conn: sqlite3.Connection) -> int | None:
    found = conn.execute("SELECT max(manifest_id) FROM manifests").fetchone()
    return None if found is None or found[0] is None else int(found[0])


# --- BagIt ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BagResult:
    path: Path
    files: int
    created: bool
    valid: bool
    detail: str


_WARC_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.warc\.gz$")


def _open_warc(path: Path, warc_root: Path, today: date) -> bool:
    """True for a WARC file that can still grow: today's (UTC) or any later day's.

    Each day's WARC is appended to all day; a bag holding hard links to it would see it change, so a
    day's file is bagged only once that day is over.
    """
    if not path.is_relative_to(warc_root):
        return False
    found = _WARC_DAY.match(path.name)
    return found is not None and date.fromisoformat(found.group(1)) >= today


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:  # a different filesystem: copy instead
        shutil.copy2(source, target)


def build_bag(config: Config, *, now: datetime | None = None) -> BagResult:
    """Create or update the BagIt bag over ``paths.store`` and validate it.

    The payload is a tree of hard links to the store's files, so the store keeps its
    content-addressed layout and nothing in it moves. The bag holds SHA-256 and SHA-512
    manifests. Validation re-reads every payload file. An update validates the existing bag first
    and stops, leaving its manifests alone, if anything in it has changed.
    """
    store = config.paths.store
    if not store.is_dir():
        raise ManifestError(f"{store} does not exist: nothing to bag")
    bag_dir = config.paths.bags / "store"
    today = _now(now).date()
    sources = sorted(
        p
        for p in store.rglob("*")
        if p.is_file()
        and not p.name.startswith(".")
        and not p.name.endswith(".lock")
        and not _open_warc(p, config.paths.warc, today)
    )
    created = not (bag_dir / "bagit.txt").exists()
    info = {
        "Source-Organization": "University of New Haven",
        "Contact-Email": config.project.contact,
        "External-Description": "Content-addressed capture store of the efp pipeline",
        "Bagging-Date": _now(now).strftime("%Y-%m-%d"),
    }
    if created:
        staging = config.paths.bags / ".staging-store"
        if staging.exists():
            shutil.rmtree(staging)
        for source in sources:
            _link_or_copy(source, staging / source.relative_to(store))
        if not sources:
            staging.mkdir(parents=True)
        bagit.make_bag(str(staging), info, checksums=["sha256", "sha512"])
        staging.rename(bag_dir)
        bag = bagit.Bag(str(bag_dir))
    else:
        # Check the bag as it stands before touching it: regenerating the manifests would
        # otherwise bless a body that has changed since it was bagged.
        existing = bagit.Bag(str(bag_dir))
        try:
            existing.validate()
        except bagit.BagValidationError as exc:
            return BagResult(bag_dir, len(sources), False, False, f"existing bag is invalid: {exc}")
        data = bag_dir / "data"
        for source in sources:
            target = data / source.relative_to(store)
            if not target.exists():
                _link_or_copy(source, target)
        bag = bagit.Bag(str(bag_dir))
        for key, value in info.items():
            bag.info[key] = value
        bag.save(manifests=True)
    try:
        bag.validate()
    except bagit.BagValidationError as exc:
        return BagResult(bag_dir, len(sources), created, False, str(exc))
    return BagResult(bag_dir, len(sources), created, True, "bag is valid")
