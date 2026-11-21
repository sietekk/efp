"""Secret hygiene. Named test_zz_ so it runs after the rest of the suite.

Sentinel secrets are injected through a temporary env file. No log output, console output,
selftest output, exception text or captured test output may contain any of them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp.cli import app
from efp.config import ConfigError, load_config
from efp.logging import setup_logging
from efp.secrets import SecretsError, load_secrets
from tests.conftest import COLLECTED, SENTINELS

runner = CliRunner()


def _detach_pytest_handlers(log: logging.Logger) -> None:
    """pytest attaches its capture handlers to non-propagating loggers ahead of ours, so they
    would record the secrets these tests deliberately log. Production has no such handler;
    redaction protects the handlers efp installs."""
    for target in (log, logging.getLogger()):
        for handler in list(target.handlers):
            if not getattr(handler, "_efp_handler", False):
                target.removeHandler(handler)


def _assert_clean(text: str, where: str) -> None:
    for name, value in SENTINELS.items():
        assert value not in text, f"{name} leaked into {where}"


def test_error_paths_never_carry_secret_values(env_file: Path, tmp_path: Path) -> None:
    texts: list[str] = []

    env_file.chmod(0o644)
    with pytest.raises(SecretsError) as bad_mode:
        load_secrets(env_file)
    texts += [str(bad_mode.value), repr(bad_mode.value)]
    env_file.chmod(0o600)

    with pytest.raises(SecretsError) as missing:
        load_secrets(tmp_path / "gone.env")
    texts += [str(missing.value), repr(missing.value)]

    with pytest.raises(SecretsError) as not_file:
        load_secrets(env_file.parent)
    texts += [str(not_file.value)]

    # A malformed line sitting next to real secrets.
    malformed = tmp_path / "malformed.env"
    malformed.write_text(
        "=novalue\nthis is not a pair\n" + env_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    malformed.chmod(0o600)
    secrets = load_secrets(malformed)
    texts += [repr(secrets), str(secrets), secrets.model_dump_json(), json.dumps(secrets.present())]

    for text in texts:
        _assert_clean(text, "an error or repr")


def test_config_errors_do_not_include_secrets(
    write_config: Callable[..., Path], env_file: Path
) -> None:
    path = write_config(append="\n[nope]\nx = 1\n")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    _assert_clean(str(err.value), "a config error")


def test_logs_redact_secret_values(write_config: Callable[..., Path], data_dir: Path) -> None:
    (data_dir / "logs").mkdir(parents=True)
    cfg = load_config(write_config())
    secrets = load_secrets(cfg.paths.env_file)
    log = setup_logging(cfg, secrets.secret_values())
    _detach_pytest_handlers(log)
    leaked = SENTINELS["COURTLISTENER_TOKEN"]
    log.info("token is %s", leaked)
    try:
        raise RuntimeError(f"request failed with {SENTINELS['PACER_PASS']}")
    except RuntimeError:
        log.exception("while calling %s", SENTINELS["FEC_API_KEY"])
    for handler in log.handlers:
        handler.flush()
    _assert_clean((data_dir / "logs" / "efp.log").read_text(encoding="utf-8"), "efp.log")


def test_console_output_redacts_secret_values(
    write_config: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = load_config(write_config())
    secrets = load_secrets(cfg.paths.env_file)
    log = setup_logging(cfg, secrets.secret_values())
    _detach_pytest_handlers(log)
    log.warning("oops %s", SENTINELS["ANTHROPIC_API_KEY"])
    # The console handler holds the stderr that was current at setup; read both streams.
    captured = capsys.readouterr()
    _assert_clean(captured.out + captured.err, "the console stream")
    logging.getLogger("efp").handlers.clear()


def test_cli_output_never_contains_secret_values(
    write_config: Callable[..., Path], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir.mkdir()
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    for args in (["selftest"], ["selftest", "--strict"], ["status"], ["--version"]):
        result = runner.invoke(app, args)
        _assert_clean(result.output, f"`efp {' '.join(args)}`")


def test_nothing_in_the_whole_suite_output_contains_a_secret() -> None:
    assert COLLECTED, "the collection hook captured nothing; the check would be vacuous"
    for chunk in COLLECTED:
        for name, value in SENTINELS.items():
            if value in chunk:
                at = chunk.index(value)
                context = chunk[max(0, at - 300) : at + 120]
                pytest.fail(f"{name} leaked into captured suite output: ...{context}...")
