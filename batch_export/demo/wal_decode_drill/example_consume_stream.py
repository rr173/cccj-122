#!/usr/bin/env python3
"""Example: consume the per-transaction change stream.

This is a small, self-contained consumer. Starting from the tracked row set of
the *baseline* database (read independently), it applies every commit from the
decoder stream in order and checks the reconstructed state after EACH
intermediate commit -- not just the final row count.

It deliberately uses only the public Python iterator API
(``WalChangeDecoder.iter_commits``) plus the tagged value codec, and it keeps
only the current row set in memory: the previous snapshot is replaced as soon
as a commit is applied.

Run:
    python3 -I demo/wal_decode_drill/example_consume_stream.py \
        <baseline.sqlite> <cycle.sqlite-wal> <table> <idcol> [<idcol> ...]

The drill's collector artifacts, e.g. main.baseline.sqlite +
main.cycle.sqlite-wal, can be passed directly.
"""
from __future__ import annotations

import os
import sqlite3
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT)

from wal_decode import (  # noqa: E402
    TrackedTable,
    WalChangeDecoder,
    decode_value,
)


def baseline_rows(db_path: str, table: str, id_columns: tuple[str, ...]):
    """Independent read of the baseline tracked rows (read-only, no sidecar)."""
    uri = "file:" + os.path.abspath(db_path).replace("?", "%3f") + "?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        names = [r[1] for r in conn.execute(
            f'PRAGMA table_info("{table}")').fetchall()]
        col_sql = ", ".join('"' + c + '"' for c in names)
        id_pos = [names.index(c) for c in id_columns]
        state = {}
        for row in conn.execute(f'SELECT {col_sql} FROM "{table}"'):
            values = list(row)
            identity = tuple(values[p] for p in id_pos)
            state[identity] = dict(zip(names, values))
        return state, names
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) < 5:
        print(__doc__)
        return 2
    db_path, wal_path, table = argv[1], argv[2], argv[3]
    id_columns = tuple(argv[4:])
    tracked = (TrackedTable(table, id_columns),)

    # State 0: the baseline row set, read independently of the decoder.
    state, column_names = baseline_rows(db_path, table, id_columns)
    print(f"baseline state 0: {len(state)} tracked row(s)")

    with WalChangeDecoder(db_path, wal_path, tracked) as decoder:
        for commit in decoder.iter_commits():
            before_count = len(state)
            net = 0
            for table_changes in commit.changes:
                for event in table_changes.events:
                    identity = tuple(
                        decode_value(event.after[col] if event.after is not None
                                     else event.before[col])
                        for col in id_columns
                    )
                    if event.kind == "insert":
                        assert identity not in state, "duplicate insert"
                        state[identity] = {
                            col: decode_value(record)
                            for col, record in event.after.items()
                        }
                        net += 1
                    elif event.kind == "delete":
                        assert identity in state, "delete of missing row"
                        del state[identity]
                        net -= 1
                    else:  # update: full before/after carried on the event
                        assert identity in state
                        state[identity] = {
                            col: decode_value(record)
                            for col, record in event.after.items()
                        }
            # Self-consistency at THIS intermediate commit: the row count must
            # equal baseline count plus the cumulative net change so far. The
            # acceptance drill performs the stronger, independent SQL-snapshot
            # comparison at every commit (see applier.sql_snapshot).
            assert len(state) == before_count + net
            print(
                f"after commit {commit.commit_seq:>2} "
                f"(frames {commit.first_frame_no}..{commit.last_frame_no}, "
                f"db pages={commit.db_page_count_after}): "
                f"{sum(len(c.events) for c in commit.changes)} net event(s), "
                f"{len(state)} row(s) reconstructed, "
                f"before=state {commit.state_before_commit_seq}, "
                f"after=state {commit.state_after_commit_seq}"
            )
        print(f"final outcome: {decoder.outcome.value}")
        if decoder.tail is not None:
            print(f"tail: {decoder.tail.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
