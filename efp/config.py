"""Configuration: ``config.toml`` validated by Pydantic.

Every key in ``config.example.toml`` is a field here; unknown keys are an error. Paths have no
defaults: every location comes from the file, so no home-directory path is hard-coded in code.
Secrets are not configuration; see :mod:`efp.secrets`.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

Policy = Literal["first_hit", "all", "first_available", "ladder"]

_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class ConfigError(Exception):
    """The configuration could not be found or is invalid. Never carries secret values."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Project(_Section):
    name: str
    user_agent: str
    contact: str

    @field_validator("user_agent")
    @classmethod
    def _user_agent_balanced(cls, v: str) -> str:
        if v.count("(") != v.count(")"):
            raise ValueError("user_agent has unbalanced parentheses")
        return v

    @model_validator(mode="after")
    def _user_agent_carries_contact(self) -> Project:
        if self.contact not in self.user_agent:
            raise ValueError("user_agent must include the contact address (project rule)")
        return self


class Paths(_Section):
    data_dir: Path
    source_workbook: Path
    database: Path
    store: Path
    bulk: Path
    manifests: Path
    bags: Path
    logs: Path
    exports: Path
    import_inbox: Path
    env_file: Path

    @field_validator("*", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


class Scope(_Section):
    population: Literal["all", "convictions"]
    year_from: int
    year_to: int
    sample_size: int = Field(gt=0)
    sample_seed: int


class FirstPass(_Section):
    online_only: bool
    social_enabled: bool
    voter_file_enrichment: bool
    people_search_enabled: bool
    google_search_enabled: bool


class RateLimits(BaseModel):
    """Fixed keys plus one ``"host" = requests_per_second`` entry per domain."""

    model_config = ConfigDict(extra="allow", frozen=True)

    default_rps: float = Field(gt=0, le=0.5)  # ceiling: 1 request per 2 seconds (spec 10)
    honor_robots_txt: bool
    retry_attempts: int = Field(ge=0)
    retry_backoff_seconds: float = Field(ge=0)
    daily_caps: dict[str, int]

    @model_validator(mode="after")
    def _domains_valid(self) -> RateLimits:
        for host, rps in (self.model_extra or {}).items():
            if not _HOSTNAME.match(host):
                raise ValueError(f"unknown key rate_limits.{host!r} (not a hostname)")
            if isinstance(rps, bool) or not isinstance(rps, int | float) or rps <= 0:
                raise ValueError(f"rate_limits.{host!r} must be a positive number")
        for host, cap in self.daily_caps.items():
            if not _HOSTNAME.match(host):
                raise ValueError(f"unknown key rate_limits.daily_caps.{host!r} (not a hostname)")
            if cap < 0:
                raise ValueError(f"rate_limits.daily_caps.{host!r} must not be negative")
        return self

    def domains(self) -> dict[str, float]:
        return {h: float(v) for h, v in (self.model_extra or {}).items()}


class Capture(_Section):
    timeout_seconds: float = Field(gt=0)
    max_redirects: int = Field(ge=0)
    robots_cache_hours: float = Field(ge=0)
    spn_per_minute: int = Field(gt=0)
    tsa_urls: list[str]


class Runner(_Section):
    batch_size: int = Field(gt=0)
    max_attempts_per_stage: int = Field(gt=0)
    stages: list[str]


class _Chain(_Section):
    modules: list[str]
    policy: Policy


class ExtractStage(_Chain):
    require_sample_approval: bool


class IdentityStage(_Section):
    candidate_modules: list[str]
    candidate_policy: Policy
    parameter_modules: list[str]
    parameter_policy: Policy


class SocialStage(_Section):
    enumerate_modules: list[str]
    enumerate_policy: Policy
    retrieve_modules: list[str]
    retrieve_policy: Policy
    allowlist_path: str


class ValidateStage(_Section):
    label_modules: list[str]
    label_policy: Policy


class Stages(_Section):
    resolve_links: _Chain
    court: _Chain
    documents: _Chain
    identity: IdentityStage
    news: _Chain
    # "import" and "validate" are a keyword and a BaseModel attribute; map them by alias.
    import_: _Chain = Field(alias="import")
    social: SocialStage
    extract: ExtractStage
    archive: _Chain
    validate_: ValidateStage = Field(alias="validate")


class Irb(_Section):
    cleared: bool
    determination: str
    date: str = Field(pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    reference: str
    scope_notes: str


class IdentityCfg(_Section):
    confirm_threshold: float = Field(ge=0, le=1)
    review_threshold: float = Field(ge=0, le=1)
    labeled_pairs_required: int = Field(ge=0)
    audit_sample: int = Field(ge=0)


class DocumentsCfg(_Section):
    ocr_quality_threshold: float = Field(ge=0, le=1)
    llm_transcription_fallback: bool


class Llm(_Section):
    provider: Literal["anthropic"]
    model: str
    transcription_model: str
    temperature: float
    max_monthly_usd: float = Field(ge=0)
    sample_runs: int = Field(gt=0)

    @field_validator("temperature")
    @classmethod
    def _temperature_zero(cls, v: float) -> float:
        if v != 0.0:
            raise ValueError("llm.temperature must be 0.0 (project rule)")
        return v


class Search(_Section):
    google_cse_daily_cap: int = Field(ge=0)


class Export(_Section):
    provisional_prefix: str
    redacted_csv: bool
    independent_means: Literal["unaffiliated", "centrist", "nonpartisan_direction"]


class LoggingCfg(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Config(_Section):
    project: Project
    paths: Paths
    scope: Scope
    first_pass: FirstPass
    rate_limits: RateLimits
    capture: Capture
    runner: Runner
    stages: Stages
    irb: Irb
    identity: IdentityCfg
    documents: DocumentsCfg
    llm: Llm
    search: Search
    export: Export
    logging: LoggingCfg


def find_config() -> Path:
    """Locate config.toml: ``EFP_CONFIG``, else the repo root found from the CWD or package."""
    override = os.environ.get("EFP_CONFIG")
    if override:
        return Path(override)
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for directory in (start, *start.parents):
            if (directory / "config.toml").is_file() and (directory / "pyproject.toml").is_file():
                return directory / "config.toml"
    raise ConfigError("config.toml not found: run from the repository or set EFP_CONFIG")


def _format_errors(path: Path, err: ValidationError) -> str:
    lines = [f"invalid configuration in {path}:"]
    for item in err.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in item["loc"]) or "<root>"
        lines.append(f"  {loc}: {item['msg']}")
    return "\n".join(lines)


def load_config(path: Path | None = None) -> Config:
    """Load and validate configuration. Raises :class:`ConfigError` with a clear message."""
    cfg_path = path if path is not None else find_config()
    try:
        raw: dict[str, Any] = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {cfg_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{cfg_path} is not valid TOML: {exc}") from None
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_errors(cfg_path, exc)) from None
