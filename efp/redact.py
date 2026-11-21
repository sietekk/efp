"""Redaction of command-line arguments before they are stored in ``audit_log``.

No CLI flag takes a secret today, so this is a guard against a future flag or a pasted value.
It errs toward redacting: a false positive costs a ``***`` in a log, a false negative stores a
credential in an append-only table that cannot be edited.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

REDACTED = "***"

# Flags whose value is a secret: --token abc, --api-key=abc, --db-password abc ...
_SECRET_FLAG = re.compile(
    r"^--?[\w-]*(token|key|pass|passwd|password|secret|credential|auth)[\w-]*$", re.I
)
# NAME=value where NAME looks secret: FEC_API_KEY=abc
_SECRET_ASSIGNMENT = re.compile(
    r"^([\w.-]*(token|key|pass|passwd|password|secret|credential|auth)[\w.-]*)=(.+)$", re.I | re.S
)
# Shapes of well-known credentials, anywhere in a token.
_KEY_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
_LONG_MIXED = re.compile(r"^[A-Za-z0-9_\-+/=]{28,}$")
_SECRET_WORDS = r"token|key|pass|passwd|password|secret|credential|auth|sig|signature"  # noqa: S105  (regex words to match, not a password)
# user:pass@host in a URL
_URL_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")
# ?api_key=... &token=... in a URL or query string
_QUERY_SECRET = re.compile(rf"(?i)([?&;])([\w.%-]*(?:{_SECRET_WORDS})[\w.%-]*)=([^&#\s]*)")
# Header-shaped values: "Authorization: Bearer abc", "X-Api-Key: abc"
_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|x-auth-token|set-cookie|cookie)"
    r"\s*[:=]\s*\S.*"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}|\b(basic)\s+[A-Za-z0-9+/=]{16,}")
_SHORT_SECRET_FLAGS = {"-p", "-P"}  # -p <password> in many tools; no efp flag uses them


def _looks_like_secret(token: str) -> bool:
    if _KEY_SHAPES.search(token):
        return True
    if _LONG_MIXED.match(token):
        has_digit = any(c.isdigit() for c in token)
        has_alpha = any(c.isalpha() for c in token)
        return has_digit and has_alpha
    return False


def _scrub(token: str) -> str:
    """Remove credentials embedded in a URL, query string or header-shaped value."""
    token = _URL_USERINFO.sub(f"{REDACTED}@", token)
    token = _QUERY_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}={REDACTED}", token)
    token = _HEADER.sub(lambda m: f"{m.group(1)}: {REDACTED}", token)
    return _BEARER.sub(lambda m: f"{m.group(1) or m.group(2)} {REDACTED}", token)


def redact_args(args: Sequence[str], secret_values: Iterable[str] = ()) -> list[str]:
    """Return ``args`` with anything that looks like a secret replaced by ``***``."""
    known = sorted({v for v in secret_values if v}, key=len, reverse=True)
    out: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        cleaned = arg
        for value in known:
            cleaned = cleaned.replace(value, REDACTED)
        cleaned = _scrub(cleaned)
        if cleaned == arg:
            assignment = _SECRET_ASSIGNMENT.match(arg)
            if assignment:
                cleaned = f"{assignment.group(1)}={REDACTED}"
            elif arg.startswith("-") and "=" in arg and _SECRET_FLAG.match(arg.split("=", 1)[0]):
                cleaned = f"{arg.split('=', 1)[0]}={REDACTED}"
            elif (
                arg.startswith("-")
                and "=" not in arg
                and (_SECRET_FLAG.match(arg) or arg in _SHORT_SECRET_FLAGS)
            ):
                redact_next = True
            elif _looks_like_secret(arg):
                cleaned = REDACTED
        out.append(cleaned)
    return out
