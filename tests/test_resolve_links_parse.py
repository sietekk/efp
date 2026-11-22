from __future__ import annotations

import pytest

from efp.resolve_links import classify
from efp.resolve_links.parse import parse_source_cell


def _raws(text: str | None) -> list[str]:
    return [u.raw for u in parse_source_cell(text)]


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("https://www.justice.gov/opa/pr/x", ["https://www.justice.gov/opa/pr/x"]),
        (
            "See https://a.test/p. Also http://b.test/q, and more",
            ["https://a.test/p", "http://b.test/q"],
        ),
        ("herit.ag/abc123", ["herit.ag/abc123"]),  # a bare shortener, no scheme
        ("bit.ly/Xy9.", ["bit.ly/Xy9"]),  # trailing period
        ("www.news.com/story/2012/a-b.html", ["www.news.com/story/2012/a-b.html"]),
        (
            "herit.ag/zzz,http://b.org/x;http://c.net/y",
            ["herit.ag/zzz", "http://b.org/x", "http://c.net/y"],
        ),
        ("(https://a.test/p)", ["https://a.test/p"]),  # surrounding parentheses
        ("http://a.test/wiki/Foo_(bar).", ["http://a.test/wiki/Foo_(bar)"]),  # balanced one is kept
        ("https://a.test/p\nhttps://b.test/q", ["https://a.test/p", "https://b.test/q"]),
        ("http://a.test/x,http://a.test/x/", ["http://a.test/x"]),  # the same URL twice
        ("https://a.test/p?x=1&y=2;", ["https://a.test/p?x=1&y=2"]),
        ("\u201chttps://ex.com/q\u201d", ["https://ex.com/q"]),  # curly quotes
        ("see https://ex.com/q\u2026", ["https://ex.com/q"]),  # an ellipsis
        ("http://a.com/x|http://b.com/y", ["http://a.com/x", "http://b.com/y"]),
        ("http://a.com/xhttp://b.com/y", ["http://a.com/x", "http://b.com/y"]),  # glued together
    ],
)
def test_urls_are_extracted_from_messy_cells(cell: str, expected: list[str]) -> None:
    assert _raws(cell) == expected


@pytest.mark.parametrize(
    "cell",
    [
        None,
        "",
        "   ",
        "Heritage Foundation research notes",
        "U.S. Attorney e.g. Smith Inc. report.pdf",
        "see cnn.com for details",  # a bare host with no path in prose is not a link
        "reported via bit.ly and ow.ly",  # a shortener's name is not a short link
        "3.5 million votes",
    ],
)
def test_cells_without_a_url_yield_nothing(cell: str | None) -> None:
    assert parse_source_cell(cell) == []


def test_scheme_less_urls_are_fetched_as_https_and_flagged() -> None:
    (bare,) = parse_source_cell("herit.ag/abc")
    assert (bare.fetch_url, bare.has_scheme, bare.is_shortener) == (
        "https://herit.ag/abc",
        False,
        True,
    )
    (full,) = parse_source_cell("http://news.test/a")
    assert (full.fetch_url, full.has_scheme, full.is_shortener) == (
        "http://news.test/a",
        True,
        False,
    )


def test_the_key_normalises_case_www_slash_and_fragment() -> None:
    a = parse_source_cell("http://WWW.News.test/story/")[0]
    b = parse_source_cell("https://news.test/story")[0]
    assert a.key == b.key


# --- classification --------------------------------------------------------------------------

REAL = "x" * 600


@pytest.mark.parametrize(
    ("kwargs", "ok", "reason"),
    [
        ({"http_status": 200, "text": REAL}, True, "ok"),
        ({"http_status": 404, "text": REAL}, False, "HTTP 404"),
        ({"http_status": 503, "text": REAL}, False, "HTTP 503"),
        ({"http_status": None, "text": None}, False, "no response"),
        ({"http_status": 200, "text": "short"}, False, "trivial body"),
        ({"http_status": 200, "text": "This domain is for sale. " + REAL}, False, "parked"),
        ({"http_status": 200, "text": "Link not found. " + REAL}, False, "shortener error"),
        (
            {
                "http_status": 200,
                "text": REAL,
                "original": "http://a.test/deep/story",
                "final": "http://a.test/",
            },
            False,
            "site root",
        ),
        (  # a deep URL that stays deep, and a root URL that stays at the root, are both fine
            {
                "http_status": 200,
                "text": REAL,
                "original": "http://a.test/",
                "final": "http://a.test/",
            },
            True,
            "ok",
        ),
        ({"http_status": 200, "content_type": "application/pdf", "body_len": 5000}, True, "ok"),
        (
            {"http_status": 200, "content_type": "application/pdf", "body_len": 40},
            False,
            "trivial body",
        ),
    ],
)
def test_classification_table(kwargs: dict[str, object], ok: bool, reason: str) -> None:
    verdict = classify.judge(
        http_status=kwargs["http_status"],  # type: ignore[arg-type]
        content_type=kwargs.get("content_type", "text/html"),  # type: ignore[arg-type]
        body_len=kwargs.get("body_len", 1000),  # type: ignore[arg-type]
        text=kwargs.get("text"),  # type: ignore[arg-type]
        original_url=str(kwargs.get("original", "http://a.test/deep/story")),
        final_url=kwargs.get("final", "http://a.test/deep/story"),  # type: ignore[arg-type]
    )
    assert verdict.ok is ok and reason in verdict.reason


def test_judge_does_not_flag_a_long_article_for_error_words_in_its_headline() -> None:
    from efp.resolve_links import classify

    body = "word " * 1000
    ok = classify.judge(
        http_status=200, content_type="text/html", body_len=9999, text=body,
        original_url="https://a.example/n/1", final_url="https://a.example/n/1",
        title="Ballots not found in county audit - Local News",
    )  # fmt: skip
    assert ok.ok
    bad = classify.judge(
        http_status=200, content_type="text/html", body_len=9999, text=body,
        original_url="https://a.example/n/1", final_url="https://a.example/n/1",
        title="404 Not Found",
    )  # fmt: skip
    assert not bad.ok


def test_soft_404_ignores_canonicalising_redirects_to_the_root() -> None:
    from efp.resolve_links import classify

    assert not classify.is_soft_404("https://a.example/index.html", "https://a.example/")
    assert not classify.is_soft_404("https://a.example/en", "https://a.example/")
    assert classify.is_soft_404("https://a.example/news/2019/story", "https://a.example/")


def test_bitly_info_url_keeps_the_plus_off_the_query_string() -> None:
    from urllib.parse import urlsplit

    parts = urlsplit("https://bit.ly/abc?utm=1")
    assert (
        parts._replace(path=parts.path.rstrip("/") + "+", query="").geturl()
        == "https://bit.ly/abc+"
    )
