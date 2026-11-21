"""Secrets: read from the env file named by ``paths.env_file``, never from the repository.

The loader refuses a file readable by group or others. It does not touch ``os.environ``. Values
are held as ``SecretStr``; error messages carry paths and modes only, never values.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr

SECRET_NAMES: tuple[str, ...] = (
    "COURTLISTENER_TOKEN",
    "PACER_USER",
    "PACER_PASS",
    "FEC_API_KEY",
    "CONGRESS_API_KEY",
    "OPENSTATES_KEY",
    "IA_ACCESS",
    "IA_SECRET",
    "PERMA_API_KEY",
    "MEDIACLOUD_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_CSE_KEY",
    "GOOGLE_CSE_ID",
)

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

# ANTHROPIC_API_KEY is the spec's LLM_API_KEY (the SDK reads the former). The old name is
# accepted in the file as an alias when the new one is absent.
ALIASES: dict[str, str] = {"LLM_API_KEY": "ANTHROPIC_API_KEY"}


class SecretsError(Exception):
    """The env file is missing, unreadable or too permissive. Never carries secret values."""


class Secrets(BaseModel):
    model_config = ConfigDict(frozen=True)

    values: dict[str, SecretStr] = {}
    unknown_names: tuple[str, ...] = ()
    unparseable_lines: int = 0

    def present(self) -> list[str]:
        """Names of secrets that are set and non-empty. Never values."""
        return [n for n in SECRET_NAMES if n in self.values]

    def missing(self) -> list[str]:
        return [n for n in SECRET_NAMES if n not in self.values]

    def get(self, name: str) -> str | None:
        """The value of one secret, for the code that must use it. Never log the result."""
        secret = self.values.get(name)
        return secret.get_secret_value() if secret is not None else None

    def secret_values(self) -> list[str]:
        """Raw values, only for the log redaction filter."""
        return [s.get_secret_value() for s in self.values.values()]


def check_env_file(path: Path) -> None:
    """Raise :class:`SecretsError` unless the file exists and is mode 600 (no group/other bits)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise SecretsError(f"env file not found: {path}") from None
    except OSError:
        raise SecretsError(f"env file cannot be read: {path}") from None
    if not stat.S_ISREG(st.st_mode):
        raise SecretsError(f"env file is not a regular file: {path}")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise SecretsError(
            f"{path} has mode {mode:04o}; it must not be readable by group or others. "
            f"Run: chmod 600 {path}"
        )


def load_secrets(env_file: Path) -> Secrets:
    check_env_file(env_file)
    try:
        raw = dotenv_values(env_file)
    except OSError:
        raise SecretsError(f"env file cannot be read: {env_file}") from None
    values: dict[str, SecretStr] = {}
    unknown: list[str] = []
    unparseable = 0
    for name, value in raw.items():
        # A line with no "=" (a pasted bare key) comes back as a name with no value, and a
        # name that is not shaped like an env var may itself be a secret. Count, never name.
        if value is None or not _ENV_NAME.match(name):
            unparseable += 1
            continue
        target = ALIASES.get(name, name)
        if target not in SECRET_NAMES:
            unknown.append(name)
            continue
        if not value or (target in values and target != name):
            continue
        values[target] = SecretStr(value)
    return Secrets(values=values, unknown_names=tuple(unknown), unparseable_lines=unparseable)


def env_file_mode(path: Path) -> int | None:
    """Permission bits of the file, or None if it does not exist."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None
