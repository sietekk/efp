from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from efp import db as database
from efp.config import Config, load_config
from efp.ingest import loader, sample
from efp.ingest.loader import IngestRefused
from efp.ingest.reader import read_workbook
from tests.fixtures.make_workbook import make_workbook, write_manifest


def _candidates(path: Path, config: Config) -> list[sample.Candidate]:
    return loader._candidates(read_workbook(path), config)


@pytest.fixture
def bulk(source_dir: Path) -> Path:
    path = source_dir / "bulk.xlsx"
    make_workbook(path, n_rows=400, seed=7)
    write_manifest(path)
    return path


# ---------------------------------------------------------------- classifiers


@pytest.mark.parametrize(
    "text",
    [
        "Prosecuted by the U.S. Attorney's Office.",
        "the US Attorney announced",
        "United States Attorney for the District",
        "Department of Justice press release",
        "a DOJ investigation",
        "https://www.justice.gov/usao-xx/pr/example",
        "https://example.test/usao-example/9",
        "sentenced in U.S. District Court",
        "United States District Court for the Northern District",
        "heard in federal court",
        "federal district court",
    ],
)
def test_federal_markers(text: str) -> None:
    assert sample.jurisdiction(None, text) == "federal"
    assert sample.jurisdiction(text, None) == "federal"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "federal election law was cited",
        "state court sentenced him",
        "the DOJA office",
        "the state attorney general sued",
        "county attorney",
    ],
)
def test_state_otherwise(text: str) -> None:
    assert sample.jurisdiction(text, text) == "state"


def test_year_bands() -> None:
    assert [sample.year_band(y) for y in (2000, 2009, 2010, 2016, 2017, 2025)] == [
        "2000-09", "2000-09", "2010-16", "2010-16", "2017-25", "2017-25",
    ]  # fmt: skip


def _c(i: int, fraud: str | None, year: int = 2010) -> sample.Candidate:
    return sample.Candidate(i, year, fraud, None, None, f"{i:064x}")


def test_top_five_fraud_types_by_frequency_with_alphabetical_ties() -> None:
    pool = (
        [_c(i, "Alpha") for i in range(10)]
        + [_c(100 + i, "Bravo") for i in range(8)]
        + [_c(200 + i, "Charlie") for i in range(5)]
        + [_c(300 + i, "Delta") for i in range(5)]
        + [_c(400 + i, "Echo") for i in range(3)]
        + [_c(500 + i, "Foxtrot") for i in range(3)]
        + [_c(600, None), _c(601, "  "), _c(602, "alpha ")]
    )
    top = sample.top_fraud_types(pool)
    assert top == (
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
    )  # Echo beats Foxtrot alphabetically
    assert sample.BLANK not in top


# ---------------------------------------------------------------- allocation


def _key(i: int) -> sample.Key:
    return ("2000-09", "state", f"f{i}")


def test_allocation_gives_every_stratum_one_and_totals_exactly() -> None:
    sizes = {_key(i): n for i, n in enumerate((300, 120, 40, 7, 3, 1))}
    alloc = sample.allocate(sizes, 50)
    assert sum(alloc.values()) == 50
    assert all(v >= 1 for v in alloc.values()) and set(alloc) == set(sizes)
    assert all(alloc[k] <= sizes[k] for k in sizes)
    assert alloc[_key(0)] > alloc[_key(1)] > alloc[_key(2)]  # proportional


def test_allocation_takes_everything_when_the_sample_is_larger_than_the_population() -> None:
    sizes = {_key(0): 5, _key(1): 3}
    assert sample.allocate(sizes, 50) == sizes


def test_allocation_caps_a_stratum_at_its_size_and_gives_the_surplus_to_others() -> None:
    sizes = {_key(0): 10, _key(1): 10, _key(2): 2}
    alloc = sample.allocate(sizes, 21)
    assert alloc[_key(2)] == 2  # the small stratum is taken whole, never more than it has
    assert alloc[_key(0)] + alloc[_key(1)] == 19
    assert all(alloc[k] <= sizes[k] for k in sizes) and sum(alloc.values()) == 21


def test_allocation_with_fewer_rows_than_strata_covers_the_largest() -> None:
    sizes = {_key(i): n for i, n in enumerate((10, 9, 8, 7))}
    alloc = sample.allocate(sizes, 2)
    assert alloc == {_key(0): 1, _key(1): 1}


# ---------------------------------------------------------------- the draw


def test_the_draw_is_reproducible_and_independent_of_input_order(
    bulk: Path, config: Config
) -> None:
    pool = _candidates(bulk, config)
    first = sample.draw(pool, seed=20261027, size=50)
    shuffled = list(pool)
    random.Random(3).shuffle(shuffled)  # noqa: S311  (test shuffle)
    again = sample.draw(shuffled, seed=20261027, size=50)
    assert first == again and first.sample_sha256 == again.sample_sha256
    other = sample.draw(pool, seed=1, size=50)
    assert other.rows != first.rows
    assert other.strata == first.strata  # the strata do not depend on the seed


def test_the_sample_covers_every_stratum_that_has_rows(bulk: Path, config: Config) -> None:
    pool = _candidates(bulk, config)
    drawn = sample.draw(pool, seed=20261027, size=50)
    assert drawn.size == 50
    non_empty = [
        (k, population, sampled) for k, population, sampled in drawn.strata if population > 0
    ]
    assert len(non_empty) <= 36  # 3 bands x 2 jurisdictions x (5 + other)
    assert all(sampled >= 1 for _, _, sampled in non_empty)
    assert all(sampled <= population for _, population, sampled in non_empty)
    assert sum(s for _, _, s in non_empty) == 50
    assert {k[0] for k, *_ in non_empty} == {"2000-09", "2010-16", "2017-25"}
    assert {k[1] for k, *_ in non_empty} == {"federal", "state"}


def test_chosen_rows_belong_to_the_strata_they_are_counted_in(bulk: Path, config: Config) -> None:
    pool = {c.row_id: c for c in _candidates(bulk, config)}
    drawn = sample.draw(list(pool.values()), seed=5, size=50)
    top = set(drawn.top_fraud_types)
    counted: Counter[sample.Key] = Counter()
    for row_id, digest in drawn.rows:
        c = pool[row_id]
        assert c.row_sha256 == digest
        fraud = sample._fraud_key(c.fraud_type)
        counted[
            (
                sample.year_band(c.year),
                sample.jurisdiction(c.source_raw, c.details),
                fraud if fraud in top else sample.OTHER,
            )
        ] += 1
    assert counted == {k: s for k, _, s in drawn.strata if s}


def test_only_in_scope_rows_are_candidates(source_dir: Path, config: Config) -> None:
    path = source_dir / "w.xlsx"
    make_workbook(path)
    pool = _candidates(path, config)
    assert len(pool) == 19  # the 1998 row is excluded
    assert all(2000 <= c.year <= 2025 for c in pool)


def test_a_small_population_is_taken_whole(workbook: Path, config: Config) -> None:
    drawn = sample.draw(_candidates(workbook, config), seed=1, size=50)
    assert drawn.size == 19


# ---------------------------------------------------------------- the frozen file


def test_the_file_holds_ids_and_hashes_only(bulk: Path, config: Config, tmp_path: Path) -> None:
    drawn = sample.draw(_candidates(bulk, config), seed=20261027, size=50)
    path = tmp_path / "out" / "sample_rows.json"
    sample.write_sample(drawn, path)
    text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    assert set(document) == {
        "version",
        "seed",
        "requested_size",
        "size",
        "year_bands",
        "jurisdictions",
        "fraud_type_top",
        "strata",
        "sample_sha256",
        "rows",
    }
    assert all(set(r) == {"row_id", "row_sha256"} for r in document["rows"])
    assert document["seed"] == 20261027 and document["size"] == 50
    for forbidden in ("Person", "Example", "example.test", "Synthetic"):
        assert forbidden not in text, forbidden  # no names, details or URLs


def test_the_file_is_frozen_never_overwritten(bulk: Path, config: Config, tmp_path: Path) -> None:
    drawn = sample.draw(_candidates(bulk, config), seed=1, size=50)
    path = tmp_path / "s.json"
    sample.write_sample(drawn, path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(sample.SampleError, match="frozen"):
        sample.write_sample(sample.draw(_candidates(bulk, config), seed=2, size=50), path)
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".sample-")] == []


def test_compare_and_integrity(bulk: Path, config: Config, tmp_path: Path) -> None:
    drawn = sample.draw(_candidates(bulk, config), seed=1, size=50)
    path = tmp_path / "s.json"
    assert sample.compare_with_file(drawn, path) == "missing"
    sample.write_sample(drawn, path)
    assert sample.compare_with_file(drawn, path) == "same"
    assert sample.verify_file_integrity(path)
    assert (
        sample.compare_with_file(sample.draw(_candidates(bulk, config), seed=2, size=50), path)
        == "differs"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["rows"][0]["row_sha256"] = "f" * 64  # someone edits the frozen file
    path.write_text(json.dumps(document), encoding="utf-8")
    assert not sample.verify_file_integrity(path)
    path.write_text("not json", encoding="utf-8")
    assert sample.compare_with_file(drawn, path) == "differs"
    assert not sample.verify_file_integrity(path)


def test_the_sample_path_resolves_relative_to_the_repository(config: Config) -> None:
    relative = config.model_copy(
        update={
            "scope": config.scope.model_copy(
                update={"sample_file": "tests/fixtures/sample_rows.json"}
            )
        }
    )
    path = sample.resolve_sample_path(relative)
    assert path.is_absolute() and path.parts[-3:] == ("tests", "fixtures", "sample_rows.json")


# ---------------------------------------------------------------- through ingest


def test_ingest_writes_the_sample_once_and_verifies_it_afterwards(
    bulk: Path, config: Config, tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample_rows.json"
    first = loader.ingest(config, loader.prepare(config, bulk))
    assert first.sample_file == "written" and sample_path.exists()
    assert loader.ingest(config, loader.prepare(config, bulk)).sample_file == "verified"
    sample_path.unlink()
    assert loader.ingest(config, loader.prepare(config, bulk)).sample_file == "written"  # restored


def test_a_conflicting_sample_file_stops_ingest_before_any_write(
    bulk: Path, config: Config, tmp_path: Path
) -> None:
    other = sample.draw(_candidates(bulk, config), seed=999, size=50)
    sample.write_sample(other, tmp_path / "sample_rows.json")
    with pytest.raises(IngestRefused, match="never silently redrawn"):
        loader.ingest(config, loader.prepare(config, bulk))
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert conn.execute("SELECT count(*) FROM rows").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_changed_sample_file_is_reported_on_a_no_op_run(
    bulk: Path, config: Config, tmp_path: Path
) -> None:
    loader.ingest(config, loader.prepare(config, bulk))
    path = tmp_path / "sample_rows.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sample_sha256"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IngestRefused, match="never silently redrawn"):
        loader.ingest(config, loader.prepare(config, bulk))


def test_the_seed_comes_from_config(write_config: Callable[..., Path], bulk: Path) -> None:
    a = load_config(write_config({"sample_seed = 1": "sample_seed = 11"}))
    b = load_config(write_config({"sample_seed = 1": "sample_seed = 22"}))
    assert loader.prepare(a, bulk).sample.rows != loader.prepare(b, bulk).sample.rows
