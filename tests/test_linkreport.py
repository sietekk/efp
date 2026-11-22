from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import linkreport
from efp.cli import app
from efp.config import Config
from tests.linkhelpers import add_row

runner = CliRunner()


def _link(
    conn: sqlite3.Connection,
    row_id: int,
    raw: str,
    status: str,
    expanded: str | None = None,
    *,
    done: bool = True,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO links (row_id, url_raw, url_expanded, status, resolved_at, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row_id, raw, expanded, status, "2026-01-01T00:00:00+00:00" if done else None, note),
    )
    conn.commit()


@pytest.fixture
def populated(config: Config, conn: sqlite3.Connection) -> sqlite3.Connection:
    sha = lambda n: f"{n:064d}"  # noqa: E731
    add_row(conn, 1, year=2005, source="x", sha=sha(1))
    add_row(conn, 2, year=2012, source="x", sha=sha(2))
    add_row(conn, 3, year=2012, source="x", sha=sha(2))  # an exact duplicate of row 2
    add_row(conn, 4, year=2020, source="x", sha=sha(4))
    add_row(conn, 5, year=2020, source="x", sha=sha(5))
    add_row(conn, 6, year=2020, source="x", sha=sha(6))
    add_row(conn, 7, year=2020, source="x", sha=sha(7))
    add_row(conn, 8, year=2020, source="x", sha=sha(8))  # never reached by the stage
    _link(conn, 1, "https://www.news.test/a", "live", "https://www.news.test/a")
    _link(conn, 2, "https://news.test/b", "archived", "https://news.test/b")
    _link(conn, 2, "https://news.test/c", "dead", "https://news.test/c")
    _link(conn, 3, "https://news.test/b", "archived", "https://news.test/b")
    _link(conn, 4, "herit.ag/zz", "unresolved", None)
    _link(conn, 5, "herit.ag/ok", "live", "https://justice.test/pr/1")
    _link(conn, 6, "", "unresolved", None, note="no_url")
    _link(conn, 7, "https://justice.test/pr/2", "unresolved", None, done=False)
    return conn


def test_a_rows_status_is_its_best_link_and_unprocessed_rows_are_pending(
    config: Config, populated: sqlite3.Connection
) -> None:
    report = linkreport.build(populated, config)
    assert report.population == 8 and report.rows_no_url == 1 and report.rows_not_yet_seen == 1
    assert report.rows_with_url == 6
    assert report.row_status == {"live": 2, "archived": 2, "dead": 0, "unresolved": 1, "pending": 1}
    # row 2 has an archived and a dead link: it counts once, at archived
    assert sum(report.row_status.values()) == report.rows_with_url


def test_survival_by_year_band_sums_to_the_overall_figure(
    config: Config, populated: sqlite3.Connection
) -> None:
    report = linkreport.build(populated, config)
    assert report.row_status_by_band["2000-09"]["live"] == 1
    assert report.row_status_by_band["2010-16"]["archived"] == 2
    assert report.row_status_by_band["2017-25"] == {
        "live": 1, "archived": 0, "dead": 0, "unresolved": 1, "pending": 1,
    }  # fmt: skip
    for status in linkreport.ROW_STATUSES:
        assert (
            sum(b[status] for b in report.row_status_by_band.values()) == report.row_status[status]
        )


def test_link_counts_and_the_domain_table_sum_to_the_links_table(
    config: Config, populated: sqlite3.Connection
) -> None:
    report = linkreport.build(populated, config)
    in_table = populated.execute(
        "SELECT count(*) FROM links WHERE note IS NOT 'no_url'"
    ).fetchone()[0]
    assert report.total_links == in_table == 7
    assert sum(d.links for d in report.domains) == report.total_links
    for status in linkreport.ROW_STATUSES:
        assert sum(d.counts[status] for d in report.domains) == report.link_status[status]


def test_domains_are_ranked_by_rows_with_www_removed_and_unexpanded_shorteners_kept_apart(
    config: Config, populated: sqlite3.Connection
) -> None:
    report = linkreport.build(populated, config)
    ranked = [(d.domain, len(d.rows), len(d.hashes), d.links) for d in report.domains]
    assert ranked[0] == ("news.test", 3, 2, 4)  # rows 1, 2, 3; rows 2 and 3 are one distinct row
    assert ("justice.test", 2, 2, 2) in ranked
    assert ("(unexpanded) herit.ag", 1, 1, 1) in ranked


def test_the_shortener_share_counts_links_by_raw_host(
    config: Config, populated: sqlite3.Connection
) -> None:
    report = linkreport.build(populated, config)
    assert report.shortener_links["herit.ag"]["unresolved"] == 1
    assert report.shortener_links["herit.ag"]["live"] == 1
    text = linkreport.render_domains(report)
    assert "2 of 7 links (28.6%) are shortener URLs" in text


def test_the_rendered_report_states_the_measured_rot_against_the_expected_range(
    config: Config, populated: sqlite3.Connection
) -> None:
    text = linkreport.render_md(linkreport.build(populated, config))
    assert "With a source URL: 6" in text and "With no URL in the Source cell: 1" in text
    assert "Not yet processed by `resolve_links`: 1" in text
    assert "The spec expects 20-40% rot" in text and "2000-09" in text and "2017-25" in text
    assert "| news.test | 3 | 2 | 4 |" in text


def test_an_empty_database_reports_zeros_without_dividing_by_zero(
    config: Config, conn: sqlite3.Connection
) -> None:
    report = linkreport.build(conn, config)
    assert report.rows_with_url == 0 and report.pct(0, 0) == "n/a"
    assert "No shortener URLs." in linkreport.render_md(report)


def test_status_links_and_links_report_through_the_cli(
    config: Config,
    populated: sqlite3.Connection,
    write_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    populated.close()
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    shown = runner.invoke(app, ["status", "--links"])
    assert shown.exit_code == 0, shown.output
    assert "Link survival" in shown.output and "news.test" in shown.output
    printed = runner.invoke(app, ["links", "report", "--print"])
    assert printed.exit_code == 0 and "# Source domains and link survival" in printed.output


def test_a_row_with_a_dead_link_and_a_link_still_to_do_is_pending_not_dead(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_row(conn, 1, year=2012, source="x")
    _link(conn, 1, "https://news.test/a", "dead", "https://news.test/a")
    _link(conn, 1, "https://news.test/b", "unresolved", None, done=False)
    report = linkreport.build(conn, config)
    assert report.row_status["pending"] == 1 and report.row_status["dead"] == 0


def test_links_blocked_by_robots_txt_are_counted_as_policy_not_rot(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_row(conn, 1, year=2012, source="x", sha="1" * 64)
    add_row(conn, 2, year=2012, source="x", sha="2" * 64)
    _link(
        conn,
        1,
        "https://news.test/p",
        "dead",
        "https://news.test/p",
        note="robots.txt disallows news.test: unreachable by policy",
    )
    _link(conn, 2, "https://news.test/q", "dead", "https://news.test/q", note="live page: HTTP 404")
    report = linkreport.build(conn, config)
    assert (report.rows_blocked, report.links_blocked) == (1, 1)
    assert report.row_status["dead"] == 2  # still not live; the split is reported, not hidden
    assert "forbids fetching" in linkreport.render_survival(report)
