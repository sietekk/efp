"""identity component. Implemented in build 12."""

from efp.types import Context, Row, StageResult

BUILD = "12"


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    """Process a batch of rows. Not built yet."""
    raise NotImplementedError("identity: build 12")


def gate(row: Row) -> bool | str:
    """True if the row may enter this stage, else the reason it may not. Not built yet."""
    raise NotImplementedError("identity: build 12")
