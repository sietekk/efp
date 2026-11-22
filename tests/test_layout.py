from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from efp.types import Context, Row

REPO = Path(__file__).resolve().parents[1]

COMPONENTS = [
    "ingest",
    "resolve_links",
    "court",
    "identity",
    "news",
    "documents",
    "social",
    "extract",
    "code",
    "export",
]

# Built from pieces so this file does not itself contain the strings it forbids elsewhere.
FORBIDDEN = re.compile("efp" + "-data|" + r"\.config/" + "efp")

# Excluded tools (union of the spec, the prompt and the tool register).
EXCLUDED = [
    "newspaper3k",
    "snscrape",
    "twscrape",
    "waybackpy",
    "abydos",
    "dedupe",
    "recordlinkage",
    "name-match",
    "namematch",
    "court-scraper",
    "nougat",
    "nougat-ocr",
    "pymupdf",
    "blackbird",
    "spiderfoot",
    "recon-ng",
    "aleph",
]


@pytest.mark.parametrize("name", COMPONENTS)
def test_component_exposes_run_and_gate(name: str) -> None:
    module = importlib.import_module(f"efp.{name}")
    assert callable(module.run)
    assert callable(module.gate)


# ingest, resolve_links and court are built; the rest are still stubs.
BUILT = ("ingest", "resolve_links", "court")


@pytest.mark.parametrize("name", [c for c in COMPONENTS if c not in BUILT])
def test_component_stubs_raise_not_implemented(name: str, write_config: object) -> None:
    module = importlib.import_module(f"efp.{name}")
    with pytest.raises(NotImplementedError, match=name):
        module.gate(Row(row_id=1))


def test_court_adapter_packages_exist() -> None:
    importlib.import_module("efp.court.adapters.ag")
    importlib.import_module("efp.court.adapters.docket")


def test_top_level_modules_exist() -> None:
    for name in ("cli", "db", "runner", "capture", "config", "secrets", "types", "logging"):
        importlib.import_module(f"efp.{name}")
    assert Context.model_fields["capture"].default is None


@pytest.mark.parametrize(
    "path",
    [
        "codebook",
        "prompts",
        "reference",
        "tests/fixtures",
        "tests/cassettes",
        "docs",
        "reports",
        "queues",
        "migrations",
    ],
)
def test_layout_directories_exist(path: str) -> None:
    assert (REPO / path).is_dir()


def test_prompts_readme_says_extraction_only() -> None:
    assert "extraction" in (REPO / "prompts" / "README.md").read_text(encoding="utf-8")


def test_allowlist_is_an_empty_versioned_list() -> None:
    data = json.loads((REPO / "allowlist.json").read_text(encoding="utf-8"))
    assert data == {"version": 1, "platforms": []}


def test_lockfile_has_no_excluded_tools() -> None:
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    names = set(re.findall(r'^name = "([^"]+)"', lock, flags=re.MULTILINE))
    assert not names & set(EXCLUDED), sorted(names & set(EXCLUDED))


def test_no_home_paths_in_code_tests_or_workflows() -> None:
    """Everything this build writes reaches those locations only through config.toml."""
    roots = [REPO / "efp", REPO / "tests", REPO / ".github", REPO / "scripts"]
    singles = [REPO / "pyproject.toml", REPO / ".pre-commit-config.yaml"]
    files = [p for r in roots if r.is_dir() for p in r.rglob("*") if p.is_file()]
    files += [p for p in singles if p.is_file()]
    offenders = [
        str(p.relative_to(REPO))
        for p in files
        if p.suffix not in {".pyc"}
        and "__pycache__" not in p.parts
        and FORBIDDEN.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == []


def test_shell_scripts_are_bash() -> None:
    for script in (REPO / "scripts").glob("*.sh"):
        lines = script.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "#!/usr/bin/env bash", script.name
        assert "set -euo pipefail" in lines, script.name


def test_ingest_is_a_command_not_a_stage() -> None:
    """Built in 03: the component interface stays, but there is no per-row stage work."""
    import efp.ingest as ingest

    assert ingest.gate(Row(row_id=1)) is True
    assert ingest.run([], None) == []  # type: ignore[arg-type]
    assert ingest.BUILD == "03"
