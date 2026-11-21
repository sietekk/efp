"""code component. Implemented in build 18."""

from efp.types import Context, Row, StageResult

BUILD = "18"


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    """Process a batch of rows. Not built yet."""
    raise NotImplementedError("code: build 18")


def gate(row: Row) -> bool | str:
    """True if the row may enter this stage, else the reason it may not. Not built yet."""
    raise NotImplementedError("code: build 18")
