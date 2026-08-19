import io

import pyarrow as pa
import pyarrow.parquet as pq

HOURLY_SCHEMA = pa.schema([
    ("hour_utc", pa.string()),
    ("hour_epoch", pa.int64()),
    ("repo", pa.string()),
    ("family", pa.string()),
    ("commits", pa.int64()),
    ("bulk_commits", pa.int64()),
    ("lines_added", pa.int64()),
    ("lines_deleted", pa.int64()),
    ("lines_added_raw", pa.int64()),
    ("lines_deleted_raw", pa.int64()),
    ("files_changed", pa.int64()),
    ("beads_closed", pa.int64()),
    ("beads_closed_bulk", pa.int64()),
    ("beads_claimed", pa.int64()),
    ("beads_released", pa.int64()),
    ("beads_reopened", pa.int64()),
    ("workers_active", pa.int64()),
])

COMMITS_SCHEMA = pa.schema([
    ("sha", pa.string()),
    ("repo", pa.string()),
    ("family", pa.string()),
    ("ts_utc", pa.string()),
    ("hour_utc", pa.string()),
    ("author_email", pa.string()),
    ("subject", pa.string()),
    ("bead_id", pa.string()),
    ("lines_added", pa.int64()),
    ("lines_deleted", pa.int64()),
    ("files_changed", pa.int64()),
    ("lines_added_raw", pa.int64()),
    ("lines_deleted_raw", pa.int64()),
    ("is_bulk", pa.bool_()),
])

BEAD_EVENTS_SCHEMA = pa.schema([
    ("ts_utc", pa.string()),
    ("hour_utc", pa.string()),
    ("repo", pa.string()),
    ("family", pa.string()),
    ("issue_id", pa.string()),
    ("kind", pa.string()),
    ("actor", pa.string()),
    ("is_bulk_import", pa.bool_()),
])


def table_to_parquet_bytes(rows, schema) -> bytes:
    table = pa.Table.from_pylist(rows, schema=schema)
    buf = io.BytesIO()
    # The client reads these with hyparquet, which does not implement every
    # codec; snappy is what the sibling dashboards already serve.
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()
