"""WhatsMyName ``wmn-data.json``: pinned, not loaded into a table (build 15 reads the file).

The file lives at ``paths.bulk/wmn/wmn-data.json``; its SHA-256 is recorded in ``ref_sources`` and
through ``efp.instruments`` so every capture that used it can name the version. CC BY-SA 4.0:
attribute Micah Hoffman, and a published allowlist derived from it is share-alike.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from efp import instruments
from efp.config import Config
from efp.reference.base import Counter, Dataset, LayoutError
from efp.reference.files import Part, dataset_dir, sha256_file
from efp.selftest import Check
from efp.types import Context


def wmn_path(config: Config) -> Path:
    return dataset_dir(config, "wmn") / "wmn-data.json"


def pin(config: Config) -> instruments.Instrument:
    """The pinned file's instrument record (hash included) for builds 12, 15, 16 and 18."""
    return instruments.pin(str(wmn_path(config)))


class Wmn(Dataset):
    name = "wmn"
    table = ""
    license = "CC BY-SA 4.0 (WhatsMyName, Micah Hoffman)"
    attribution = (
        "Site definitions: WhatsMyName project, Micah Hoffman (WebBreacher), CC BY-SA 4.0; "
        "any list derived from it is shared under the same license."
    )
    version = "main"

    def parts(self, ctx: Context) -> list[Part]:
        return [Part("", ctx.config.reference.wmn_url, "wmn-data.json", "json")]

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        sites = data.get("sites") if isinstance(data, dict) else None
        if not isinstance(sites, list) or not sites:
            raise LayoutError("expected a JSON object with a non-empty `sites` list")
        instruments.override("wmn_data", str(path))
        return Counter(len(sites), {"license_entries": len(data.get("license", []) or [])})

    def verify(self, ctx: Context) -> list[Check]:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        row = conn.execute(
            "SELECT file_sha256, row_count FROM ref_sources WHERE dataset = 'wmn' "
            "AND status = 'loaded' AND current = 1"
        ).fetchone()
        if row is None:
            return [Check("reference.wmn", False, "not loaded", required=False)]
        path = wmn_path(ctx.config)
        ok = path.is_file() and sha256_file(path) == row[0]
        return [Check("reference.wmn", ok, f"{row[1]:,} sites, sha256 {str(row[0])[:12]}")]
