from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from efp.config import Config, ConfigError, RateLimits, find_config, load_config

REPO = Path(__file__).resolve().parents[1]


def test_valid_file_loads(write_config: Callable[..., Path], data_dir: Path) -> None:
    cfg = load_config(write_config())
    assert cfg.project.name == "efp"
    assert cfg.paths.data_dir == data_dir
    assert cfg.rate_limits.domains() == {"example.test": 1.0}
    assert cfg.stages.import_.modules == ["import.pdf"]
    assert cfg.stages.validate_.label_policy == "first_available"


def test_unknown_top_level_key_fails(write_config: Callable[..., Path]) -> None:
    path = write_config(append="\n[surprise]\nkey = 1\n")
    with pytest.raises(ConfigError, match="surprise"):
        load_config(path)


def test_unknown_nested_key_fails(write_config: Callable[..., Path]) -> None:
    path = write_config({"sample_size = 50": "sample_size = 50\nsample_sede = 3"})
    with pytest.raises(ConfigError, match="sample_sede"):
        load_config(path)


def test_rate_limit_key_must_be_a_hostname(write_config: Callable[..., Path]) -> None:
    path = write_config({'"example.test" = 1.0': "not_a_host = 1.0"})
    with pytest.raises(ConfigError, match="not_a_host"):
        load_config(path)


def test_rate_limit_value_must_be_positive(write_config: Callable[..., Path]) -> None:
    path = write_config({'"example.test" = 1.0': '"example.test" = 0'})
    with pytest.raises(ConfigError, match="positive"):
        load_config(path)


def test_temperature_must_be_zero(write_config: Callable[..., Path]) -> None:
    path = write_config({"temperature = 0.0": "temperature = 0.7"})
    with pytest.raises(ConfigError, match="temperature"):
        load_config(path)


def test_unbalanced_user_agent_fails(write_config: Callable[..., Path]) -> None:
    path = write_config({"invalid)": "invalid"})
    with pytest.raises(ConfigError, match="parenthes"):
        load_config(path)


def test_missing_config_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError, match=r"nope\.toml"):
        load_config(missing)


def test_invalid_toml_is_a_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad)


def test_efp_config_env_overrides_discovery(
    write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config()
    monkeypatch.setenv("EFP_CONFIG", str(path))
    assert find_config() == path


def test_repo_config_validates() -> None:
    cfg = load_config(REPO / "config.toml")
    assert cfg.llm.temperature == 0.0
    assert cfg.project.user_agent.endswith(")")


def _is_model(annotation: object) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _diff(model: type[BaseModel], example: dict[str, object], prefix: str) -> list[str]:
    """Differences between a schema model and the keys of config.example.toml."""
    problems: list[str] = []
    fields = {(f.alias or n): f for n, f in model.model_fields.items()}
    allows_extra = model.model_config.get("extra") == "allow"
    for key in example:
        if key not in fields and not allows_extra:
            problems.append(f"{prefix}{key}: in example but not in schema")
    for key, field in fields.items():
        if key not in example:
            problems.append(f"{prefix}{key}: in schema but not in example")
        elif _is_model(field.annotation):
            sub = example[key]
            assert isinstance(sub, dict)
            problems += _diff(field.annotation, sub, f"{prefix}{key}.")  # type: ignore[arg-type]
    return problems


def test_schema_and_example_have_the_same_keys() -> None:
    example = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))
    assert _diff(Config, example, "") == []
    # Per-domain rate entries are the one open-ended part of the schema.
    limits = example["rate_limits"]
    declared = set(RateLimits.model_fields)
    domains = [k for k in limits if k not in declared]
    assert domains
    assert all("." in k for k in domains)


def test_example_config_validates() -> None:
    assert load_config(REPO / "config.example.toml").irb.cleared is False
