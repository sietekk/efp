"""The portal probe: one page load per portal, classified into the spec's three modes.

The probe is reading, not scraping. For each portal in ``reference/docket_portals.csv`` it loads
the search entry point once, through the capture layer, and never submits a search. From that page
it records what it can observe (the HTTP answer, a login wall, fee wording, a challenge page, a
search form, whether the page needs JavaScript, the platform, the terms link) and decides a mode by
fixed rules (:func:`classify`):

* ``automated``: reachable and open, an adapter exists for the portal, and the state is in
  ``court.docket_terms_cleared`` (the operator has read its terms).
* ``assisted``: an online search exists that the operator can run in a browser (a challenge page,
  terms not cleared, no adapter, a portal that refuses automated requests): the pipeline constructs
  the query.
* ``manual``: no online docket search by name (as the seed list says, from knowledge) or an entry
  point that is not there. Recorded; nothing is done. Only a verified fact makes a portal
  ``manual``: a page that shows no search form, a login or fee wording is ``assisted`` (the
  operator looks; an account or fee is listed as what the portal wants), never "no search exists".

A transient failure (a 429, a 5xx, a dropped connection) classifies nothing: the portal is simply
probed again next time.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from efp.capture import CaptureError
from efp.court.adapters.ag.base import is_transient
from efp.court.located import STAGE, cap
from efp.types import Context
from efp.version import repo_root

MODES = ("automated", "assisted", "manual")
_RANK = {"automated": 3, "assisted": 2, "manual": 1}

_CHALLENGE = re.compile(
    r"recaptcha|hcaptcha|g-recaptcha|captcha|cf-challenge|just a moment|checking your browser|"
    r"are you a human|verify you are human",
    re.I,
)
_PASSWORD = re.compile(r"""<input[^>]+type=["']?password""", re.I)
_FEE = re.compile(
    r"subscription required|subscription|per[- ]search|pay[- ]per|\bfees? apply|"
    r"\$\s?\d+(?:\.\d\d)?\s*(?:per|/)\s*(?:search|case|document|page|name)",
    re.I,
)
_NAME_FIELD = re.compile(
    r"""<input[^>]+name=["']?[^"'>\s]*(last|first|party|partyname|defendant|lname|fname)""", re.I
)
_TERMS = re.compile(
    r"""<a[^>]+href=["']([^"']+)["'][^>]*>\s*"""
    r"""([^<]*(?:terms|disclaimer|legal notice|user agreement)[^<]*)<""",
    re.I,
)
_FINGERPRINTS = (
    ("odyssey", re.compile(r"odyssey|tylertech|/Portal/", re.I)),
    ("wcca", re.compile(r"wcca|wicourts", re.I)),
    ("mycase", re.compile(r"mycase", re.I)),
    ("casenet", re.compile(r"case\.net|casenet", re.I)),
    ("ujs", re.compile(r"ujsportal|pacourts", re.I)),
)


@dataclass(frozen=True)
class Seed:
    state: str
    name: str
    url: str
    platform: str  # odyssey | wcca | mycase | casenet | ujs | county_clerks | other | none
    scope: str  # statewide | county:<name>
    note: str = ""


@dataclass
class Facts:
    status: int | None = None
    transient: bool = False
    blocked: bool = False
    challenge: bool = False
    account_wall: bool = False
    fee_wall: bool = False
    search_form: bool = False
    js_only: bool = False
    terms_url: str = ""
    fingerprint: str = ""
    error: str = ""


@dataclass
class Verdict:
    seed: Seed
    facts: Facts
    mode: str
    wants: str  # account | fee | challenge | blocked | terms-not-cleared | no-adapter | ...
    reason: str


@dataclass
class Probed:
    """One portal's probe: its verdict, or None when the probe could not tell (transient)."""

    seed: Seed
    facts: Facts
    verdict: Verdict | None = field(default=None)


def seeds_path() -> Path:
    root = repo_root()
    return (root or Path.cwd()) / "reference" / "docket_portals.csv"


def load_seeds(path: Path | None = None) -> list[Seed]:
    with (path or seeds_path()).open(newline="", encoding="utf-8") as handle:
        return [
            Seed(
                r["state"].strip().upper(),
                r["portal"].strip(),
                r["url"].strip(),
                r["platform"].strip(),
                r["scope"].strip(),
                r.get("note", "").strip(),
            )
            for r in csv.DictReader(handle)
            if r.get("state")
        ]


def analyse(html: str, status: int | None, base_url: str) -> Facts:
    """What the entry page shows, from its status and text. Nothing is submitted."""
    facts = Facts(status=status)
    if status in (401, 403):
        facts.blocked = True
    if status is None or status >= 400:
        return facts
    text = html or ""
    facts.challenge = bool(_CHALLENGE.search(text))
    facts.search_form = bool(_NAME_FIELD.search(text))
    facts.account_wall = bool(_PASSWORD.search(text)) and not facts.search_form
    facts.fee_wall = bool(_FEE.search(text)) and not facts.search_form
    visible = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", text, flags=re.S | re.I)
    facts.js_only = len(visible.split()) < 40 and len(re.findall(r"<script", text, re.I)) >= 2
    for name, pattern in _FINGERPRINTS:
        if pattern.search(text):
            facts.fingerprint = name
            break
    for found in _TERMS.finditer(text):
        link = found.group(1).strip()
        if link.lower().startswith(("javascript:", "#", "mailto:")):
            continue  # a script handler or an anchor is not a terms page
        facts.terms_url = urljoin(base_url, link)
        break
    return facts


def classify(
    seed: Seed, facts: Facts, *, adapter_exists: bool, cleared: bool
) -> tuple[str, str, str] | None:
    """(mode, wants, reason), or None when a transient failure means the portal cannot be judged."""
    if seed.platform == "none":
        return "manual", "no-online-search", f"no online docket search by name ({seed.note})"
    if facts.transient:
        return None
    if facts.blocked:
        return (
            "assisted",
            "blocked",
            f"the portal answers HTTP {facts.status} to automated requests; the operator searches "
            "in a browser",
        )
    if facts.status is None or facts.status >= 400:
        return (
            "manual",
            "unreachable",
            f"the entry point answered {facts.error or f'HTTP {facts.status}'}: check "
            "reference/docket_portals.csv",
        )
    if seed.note.startswith("human-check:"):
        # known from the portal's own client code and wording, not from this page: the search sits
        # behind a check that says it is for humans and against automated programs
        return "assisted", "challenge", seed.note.removeprefix("human-check:").strip()
    # A landing page that shows a login form or fee wording does not prove there is no public
    # search behind it, so these are for the operator to check (assisted), listed as gated with
    # what they want; the pipeline never creates an account or pays.
    if facts.account_wall:
        return (
            "assisted",
            "account",
            "the entry point shows a login and no search form; the search may need an account the "
            "pipeline will not create: the operator checks in a browser",
        )
    if facts.fee_wall:
        return (
            "assisted",
            "fee",
            "the entry point asks for a subscription or a fee; the pipeline will not pay: the "
            "operator checks in a browser",
        )
    if facts.challenge:
        return (
            "assisted",
            "challenge",
            "a challenge page stands in front of the search; the operator searches in a browser",
        )
    if facts.js_only:
        if adapter_exists and cleared:
            return (
                "automated",
                "js-only",
                "the search renders only in a browser; the adapter captures it with Browsertrix "
                "(Docker); terms cleared",
            )
        if adapter_exists:
            return (
                "assisted",
                "terms-not-cleared",
                "an adapter exists but the state is not in court.docket_terms_cleared (the "
                "portal's terms have not been read); the search renders only in a browser",
            )
        return (
            "assisted",
            "js-only",
            "the search renders only in a browser (nothing is visible to a plain page load); the "
            "operator searches in a browser",
        )
    searchable = facts.search_form or seed.platform not in ("other", "county_clerks")
    if adapter_exists and cleared and searchable:
        return "automated", "", "open search by name and year; an adapter exists; terms cleared"
    if adapter_exists:
        return (
            "assisted",
            "terms-not-cleared",
            "an adapter exists but the state is not in court.docket_terms_cleared (the portal's "
            "terms have not been read)",
        )
    if searchable:
        return (
            "assisted",
            "no-adapter",
            "an online search exists and no adapter is built for this platform: the operator "
            "searches in a browser",
        )
    # Nothing at the entry page proves there is no online search (the form may sit behind a menu or
    # render by script), so this is for the operator to look at, not a "no search exists".
    return (
        "assisted",
        "search-not-found",
        "no name-search form is visible at the entry point (it may sit behind a menu); the "
        "operator looks for the search in a browser",
    )


def probe_seed(ctx: Context, seed: Seed) -> Facts:
    """One page load of the entry point (never a search)."""
    if seed.platform == "none":
        return Facts()
    try:
        record = cap(ctx).fetch(seed.url, row_id=None, stage=STAGE, kind="page")
    except CaptureError as exc:
        facts = Facts(error=str(exc)[:120])
        facts.transient = is_transient(exc)
        return facts
    body = (
        cap(ctx).read_body(record).decode("utf-8", errors="replace") if record.http_status else ""
    )
    facts = analyse(body, record.http_status, record.url_final or seed.url)
    if record.http_status == 429 or (record.http_status or 0) >= 500:
        facts.transient = True
    return facts


def probe_all(
    ctx: Context, seeds: list[Seed], *, cleared: list[str], adapter_states: set[str]
) -> list[Probed]:
    out = []
    for seed in seeds:
        facts = probe_seed(ctx, seed)
        decided = classify(
            seed, facts, adapter_exists=seed.state in adapter_states, cleared=seed.state in cleared
        )
        verdict = Verdict(seed, facts, *decided) if decided else None
        out.append(Probed(seed, facts, verdict))
    return out


def best_per_state(probed: list[Probed]) -> dict[str, Verdict]:
    """The state's recorded portal: the most automatable one, a statewide portal before a county."""
    best: dict[str, Verdict] = {}
    for item in probed:
        v = item.verdict
        if v is None:
            continue
        key = (_RANK[v.mode], v.seed.scope == "statewide")
        held = best.get(v.seed.state)
        if held is None or key > (_RANK[held.mode], held.seed.scope == "statewide"):
            best[v.seed.state] = v
    return best


def reason_text(v: Verdict, others: int) -> str:
    head = f"{v.wants}: {v.reason}" if v.wants else v.reason
    return head + (f" (+{others} other portal(s) probed in this state)" if others else "")


def is_gated(verdict: Verdict) -> bool:
    """A portal the pipeline cannot search today: anything not ``automated``. The list of such
    portals (``portal_accounts.csv``) says what each one wants."""
    return verdict.mode != "automated"


def snapshot_rows(probed: list[Probed]) -> list[dict[str, Any]]:
    stamp = datetime.now(UTC).date().isoformat()
    rows = []
    for p in probed:
        v = p.verdict
        rows.append(
            {
                "state": p.seed.state,
                "portal": p.seed.name,
                "url": p.seed.url,
                "scope": p.seed.scope,
                "platform": p.seed.platform,
                "http_status": "" if p.facts.status is None else p.facts.status,
                "mode": v.mode if v else "",
                "wants": v.wants if v else "",
                "reason": v.reason if v else f"not judged (transient: {p.facts.error or 'HTTP'})",
                "fingerprint": p.facts.fingerprint,
                "terms_url": p.facts.terms_url,
                "probed_at": stamp,
            }
        )
    return rows
