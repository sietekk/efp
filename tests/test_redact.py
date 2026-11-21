from __future__ import annotations

import pytest

from efp.redact import REDACTED, redact_args


def test_ordinary_arguments_pass_through() -> None:
    args = ["run", "court", "--limit", "50", "--state", "CT", "--year-from", "2010", "--dry-run"]
    assert redact_args(args) == args
    assert redact_args(["identity", "explain", "12"]) == ["identity", "explain", "12"]
    assert redact_args(["import", "/tmp/a.pdf", "--row", "3", "--source", "westlaw"]) == [
        "import", "/tmp/a.pdf", "--row", "3", "--source", "westlaw",
    ]  # fmt: skip


def test_known_secret_values_are_removed_anywhere() -> None:
    out = redact_args(
        ["run", "court", "--note", "see sekret-value-123 now", "sekret-value-123"],
        ["sekret-value-123"],
    )
    assert "sekret-value-123" not in " ".join(out)
    assert REDACTED in out[-1]


def test_secret_looking_flag_redacts_its_value() -> None:
    assert redact_args(["export", "--api-key", "abc"]) == ["export", "--api-key", REDACTED]
    assert redact_args(["export", "--token=abc"]) == ["export", f"--token={REDACTED}"]
    assert redact_args(["export", "--db-password", "p", "--limit", "5"]) == [
        "export", "--db-password", REDACTED, "--limit", "5",
    ]  # fmt: skip


def test_name_value_assignments_with_secret_names() -> None:
    assert redact_args(["FEC_API_KEY=abc123"]) == [f"FEC_API_KEY={REDACTED}"]
    assert redact_args(["--env", "COURTLISTENER_TOKEN=zzz"]) == [
        "--env",
        f"COURTLISTENER_TOKEN={REDACTED}",
    ]
    assert redact_args(["--state=CT"]) == ["--state=CT"]


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-api03-abcdefghijklmnop",  # pragma: allowlist secret
        "ghp_abcdefghijklmnopqrstuvwx",  # pragma: allowlist secret
        "AKIAABCDEFGHIJKLMNOP",  # pragma: allowlist secret
        "xoxb-1234567890-abcdefgh",  # pragma: allowlist secret
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij",  # pragma: allowlist secret
        "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ",  # pragma: allowlist secret
    ],
)
def test_key_shapes_are_redacted(token: str) -> None:
    assert redact_args(["run", token]) == ["run", REDACTED]
    assert token not in " ".join(redact_args(["x", token]))


def test_short_ids_and_paths_are_not_mistaken_for_secrets() -> None:
    args = ["ingest", "/Users/someone/data/heritage-voter-fraud-map-search.xlsx", "20261027", "CT"]
    assert redact_args(args) == args


def test_credentials_embedded_in_urls_are_removed() -> None:
    assert redact_args(["selftest", "https://x/?token=NOTINENV99"]) == [
        "selftest", f"https://x/?token={REDACTED}",
    ]  # fmt: skip
    url = "https://user:pw@host/p?a=1&api_key=zzz&b=2"  # pragma: allowlist secret
    assert redact_args([url]) == [f"https://{REDACTED}@host/p?a=1&api_key={REDACTED}&b=2"]


def test_header_shaped_values_are_removed() -> None:
    assert redact_args(["Authorization: Bearer abc12345def"]) == [f"Authorization: {REDACTED}"]
    assert redact_args(["X-Api-Key: abc"]) == [f"X-Api-Key: {REDACTED}"]
    assert redact_args(["Bearer abcdefgh12345"]) == [f"Bearer {REDACTED}"]


def test_dash_p_takes_a_secret_value() -> None:
    assert redact_args(["run", "-p", "hunter2", "--limit", "5"]) == [
        "run", "-p", REDACTED, "--limit", "5",
    ]  # fmt: skip


def test_ordinary_prose_is_left_alone() -> None:
    args = ["--note", "basic information about the case", "--state", "CT"]
    assert redact_args(args) == args
