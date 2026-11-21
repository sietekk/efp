"""court component. Implemented in build 07."""

from efp.types import Context, Row, StageResult

BUILD = "07"


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    """Process a batch of rows. Not built yet."""
    raise NotImplementedError("court: build 07")


def gate(row: Row) -> bool | str:
    """True if the row may enter this stage, else the reason it may not. Not built yet."""
    raise NotImplementedError("court: build 07")
