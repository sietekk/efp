"""news component. Implemented in build 13."""

from efp.types import Context, Row, StageResult

BUILD = "13"


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    """Process a batch of rows. Not built yet."""
    raise NotImplementedError("news: build 13")


def gate(row: Row) -> bool | str:
    """True if the row may enter this stage, else the reason it may not. Not built yet."""
    raise NotImplementedError("news: build 13")
