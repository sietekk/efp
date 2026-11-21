from __future__ import annotations

from pathlib import Path

import pytest

from efp.secrets import SECRET_NAMES, SecretsError, load_secrets
from tests.conftest import SENTINELS


def test_mode_600_loads(env_file: Path) -> None:
    secrets = load_secrets(env_file)
    assert secrets.get("COURTLISTENER_TOKEN") == SENTINELS["COURTLISTENER_TOKEN"]


def test_missing_file_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "absent.env"
    with pytest.raises(SecretsError, match=r"absent\.env"):
        load_secrets(missing)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_permissive_mode_is_refused(env_file: Path, mode: int) -> None:
    env_file.chmod(mode)
    with pytest.raises(SecretsError) as err:
        load_secrets(env_file)
    message = str(err.value)
    assert str(env_file) in message
    assert f"{mode:04o}" in message
    assert "chmod 600" in message


def test_present_lists_names_only(env_file: Path) -> None:
    secrets = load_secrets(env_file)
    assert set(secrets.present()) == set(SENTINELS)
    assert set(secrets.missing()) == set(SECRET_NAMES) - set(SENTINELS)
    assert not any(v in repr(secrets) or v in str(secrets) for v in SENTINELS.values())


def test_blank_values_are_not_present(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    content = "FEC_API_KEY=\nCONGRESS_API_KEY=real-looking\n"  # pragma: allowlist secret
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    assert load_secrets(path).present() == ["CONGRESS_API_KEY"]


def test_llm_api_key_alias(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("LLM_API_KEY=alias-value\n", encoding="utf-8")
    path.chmod(0o600)
    secrets = load_secrets(path)
    assert secrets.get("ANTHROPIC_API_KEY") == "alias-value"


def test_unknown_names_are_reported_by_name_only(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("SOMETHING_ELSE=do-not-print\n", encoding="utf-8")
    path.chmod(0o600)
    secrets = load_secrets(path)
    assert secrets.unknown_names == ("SOMETHING_ELSE",)
    assert "do-not-print" not in repr(secrets)


def test_loading_does_not_touch_the_environment(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    load_secrets(env_file)
    assert "COURTLISTENER_TOKEN" not in os.environ


def test_bare_pasted_key_is_counted_never_named(tmp_path: Path) -> None:
    """A key pasted without NAME= must not surface anywhere (it would be the secret itself)."""
    path = tmp_path / ".env"
    bare = "sk-ant-api03-FAKEVALUE123"  # pragma: allowlist secret
    path.write_text(f"{bare}\nFEC_API_KEY=x\nlowercase_name=y\n", encoding="utf-8")
    path.chmod(0o600)
    secrets = load_secrets(path)
    assert secrets.unparseable_lines == 2
    assert secrets.unknown_names == ()
    assert bare not in repr(secrets)
    assert secrets.present() == ["FEC_API_KEY"]
