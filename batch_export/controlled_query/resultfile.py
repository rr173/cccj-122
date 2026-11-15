"""Machine-readable query results, published atomically.

The result document is one JSON object::

    {
      "format": "controlled-query-result",
      "format_version": 1,
      "queried_at": "...",
      "identity": "alice",                 # the registered identity used
      "relations": ["fact_lines", ...],    # public relations the query used
      "sql": "SELECT ...",
      "columns": ["fact id", "concept", ...],
      "rows": [[{"t": "int", "v": "1"}, ...], ...],
      "row_count": 2
    }

Values use the same typed encoding as ``provenance_query``:

* ``{"t": "null"}``
* ``{"t": "int",  "v": "<decimal text>"}``  -- 64-bit integers never pass
  through a JSON number, so no precision is lost;
* ``{"t": "real", "v": <json number>}`` (non-finite values as text);
* ``{"t": "text", "v": "..."}``;
* ``{"t": "blob", "v": "<base64>"}``      -- BLOBs round-trip exactly.

The document names the identity and the public relations used; it never
contains hidden original values, entity names the caller may not see, or
anyone's grant details.

The file is written to a temporary sibling and ``os.replace``\ d into place
only after the whole query succeeded; a failed query removes the temporary
file and leaves no readable partial result.  Existing files are not
overwritten unless ``overwrite`` is set.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from .errors import InvocationError

FORMAT_NAME = "controlled-query-result"
FORMAT_VERSION = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_document(report) -> dict:
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "queried_at": utcnow(),
        "identity": report.identity,
        "relations": list(report.relations),
        "sql": report.sql,
        "columns": list(report.columns),
        "rows": [
            [value.encode() for value in row]
            for row in report.rows
        ],
        "row_count": len(report.rows),
    }


def publish_result(target_path: str, document: dict, *, overwrite: bool) -> str:
    """Write the JSON document atomically; return the final path."""
    target = os.path.abspath(target_path)
    if os.path.exists(target) and not overwrite:
        raise InvocationError(
            f"result file {target_path} already exists; refusing to overwrite"
            " (pass --overwrite to replace it)"
        )
    directory = os.path.dirname(target) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".controlled-query-", suffix=".json", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target
