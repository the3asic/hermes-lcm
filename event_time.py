"""Source-event coverage for new summaries, separate from legacy storage windows.

The side table leaves summary_nodes' column order and write-time fields intact
for older plugin versions. Missing side rows are explicitly legacy/unknown;
opening a database never recomputes historical summary coverage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import sqlite3
from typing import Iterable

ROLLUP_EVENT_TIME_TRIGGERS = (
    'lcm_event_time_node_update', 'lcm_event_time_insert', 'lcm_event_time_delete',
)

EVENT_TIME_TABLE_COLUMNS = {
    'summary_event_time': {'node_id', 'earliest_at', 'latest_at', 'known_count', 'unknown_count', 'legacy_count'},
    'lcm_rollup_event_time': {'rollup_id', 'source_fingerprint'},
}


def verify_event_time_columns(conn: sqlite3.Connection) -> bool:
    for table, expected in EVENT_TIME_TABLE_COLUMNS.items():
        actual = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if actual and actual != expected:
            return False
    return True


@dataclass(frozen=True)
class EventTimeCoverage:
    earliest_at: float | None = None
    latest_at: float | None = None
    known_count: int = 0
    unknown_count: int = 0
    legacy_count: int = 0

    @property
    def complete(self) -> bool:
        return self.known_count > 0 and self.unknown_count == 0 and self.legacy_count == 0

    def as_dict(self) -> dict:
        status = 'complete' if self.complete else (
            'mixed' if self.known_count else 'unknown'
        )
        return {**asdict(self), 'basis': 'observed_at', 'status': status}


def combine_coverage(values: Iterable[EventTimeCoverage]) -> EventTimeCoverage:
    values = tuple(values)
    starts = [v.earliest_at for v in values if v.earliest_at is not None]
    ends = [v.latest_at for v in values if v.latest_at is not None]
    return EventTimeCoverage(
        min(starts) if starts else None, max(ends) if ends else None,
        sum(v.known_count for v in values), sum(v.unknown_count for v in values),
        sum(v.legacy_count for v in values),
    )


def ensure_event_time_schema(conn: sqlite3.Connection) -> None:
    """Add isolated metadata tables; never alter or backfill historical rows."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS summary_event_time (
            node_id INTEGER PRIMARY KEY,
            earliest_at REAL,
            latest_at REAL,
            known_count INTEGER NOT NULL,
            unknown_count INTEGER NOT NULL,
            legacy_count INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_summary_event_time_window
            ON summary_event_time(latest_at, earliest_at);
        CREATE TABLE IF NOT EXISTS lcm_rollup_event_time (
            rollup_id INTEGER PRIMARY KEY,
            source_fingerprint TEXT NOT NULL
        );
    """)
    if not verify_event_time_columns(conn):
        raise RuntimeError('Unrecognized event-time schema; refusing to modify its data')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='summary_nodes'").fetchone():
        return
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS lcm_event_time_node_delete
            BEFORE DELETE ON summary_nodes BEGIN
                DELETE FROM summary_event_time WHERE node_id = old.node_id;
            END;
        CREATE TRIGGER IF NOT EXISTS lcm_event_time_node_lineage_update
            BEFORE UPDATE OF source_ids, source_type ON summary_nodes BEGIN
                DELETE FROM summary_event_time WHERE node_id = old.node_id;
            END;
    """)
    # The existing outbox is owned by the rollup schema. Its storage-time
    # invalidations remain useful to old code. Event-time invalidations augment
    # them in the SAME publication transaction, including deletion/lineage edits.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lcm_rollup_invalidations'").fetchone():
        for trigger in ROLLUP_EVENT_TIME_TRIGGERS:
            conn.execute(f'DROP TRIGGER IF EXISTS {trigger}')
    else:
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS lcm_event_time_node_update
            AFTER UPDATE OF session_id, summary, token_count, source_token_count,
                            created_at, earliest_at, latest_at, expand_hint ON summary_nodes
            BEGIN
                INSERT INTO lcm_rollup_invalidations
                    (node_id, scope, covered_start, covered_end, operation)
                SELECT old.node_id, old.session_id, et.earliest_at, et.latest_at, 'update'
                FROM summary_event_time et WHERE et.node_id=old.node_id
                    AND et.earliest_at IS NOT NULL AND et.latest_at IS NOT NULL;
                INSERT INTO lcm_rollup_invalidations
                    (node_id, scope, covered_start, covered_end, operation)
                SELECT new.node_id, new.session_id, et.earliest_at, et.latest_at, 'update'
                FROM summary_event_time et WHERE et.node_id=new.node_id
                    AND et.earliest_at IS NOT NULL AND et.latest_at IS NOT NULL;
            END
        """)
        for operation, timing, ref in (('insert', 'AFTER', 'new'), ('delete', 'BEFORE', 'old')):
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS lcm_event_time_{operation}
                {timing} {operation.upper()} ON summary_event_time
                WHEN {ref}.earliest_at IS NOT NULL AND {ref}.latest_at IS NOT NULL
                BEGIN
                    INSERT INTO lcm_rollup_invalidations
                        (node_id, scope, covered_start, covered_end, operation)
                    SELECT {ref}.node_id, session_id, {ref}.earliest_at,
                           {ref}.latest_at, '{operation}'
                    FROM summary_nodes WHERE node_id = {ref}.node_id;
                END
            """)


def read_event_time(conn: sqlite3.Connection, node_id: int) -> EventTimeCoverage:
    row = conn.execute(
        'SELECT earliest_at, latest_at, known_count, unknown_count, legacy_count '
        'FROM summary_event_time WHERE node_id = ?', (node_id,),
    ).fetchone()
    return EventTimeCoverage(*row) if row else EventTimeCoverage(legacy_count=1)


def source_event_time(conn: sqlite3.Connection, source_ids: list[int], source_type: str) -> EventTimeCoverage:
    ids = tuple(sorted(set(source_ids)))
    if not ids:
        return EventTimeCoverage(legacy_count=1)
    if source_type == 'messages':
        columns = {row[1] for row in conn.execute('PRAGMA table_info(messages)')}
        if 'observed_at' not in columns:
            return EventTimeCoverage(legacy_count=len(ids))
    values = []
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        marks = ','.join('?' for _ in batch)
        if source_type == 'messages':
            row = conn.execute(
                f'SELECT MIN(observed_at), MAX(observed_at), COUNT(observed_at), '
                f'COUNT(*) - COUNT(observed_at), COUNT(*) FROM messages WHERE store_id IN ({marks})', batch,
            ).fetchone()
            values.append(EventTimeCoverage(*row[:4], legacy_count=len(batch) - row[4]))
        elif source_type == 'nodes':
            rows = conn.execute(
                f'SELECT earliest_at, latest_at, known_count, unknown_count, legacy_count '
                f'FROM summary_event_time WHERE node_id IN ({marks})', batch,
            ).fetchall()
            values.extend(EventTimeCoverage(*row) for row in rows)
            if len(rows) != len(batch):
                values.append(EventTimeCoverage(legacy_count=len(batch) - len(rows)))
        else:
            raise ValueError(f'Unknown summary source type: {source_type}')
    return combine_coverage(values)


def publish_event_time(conn: sqlite3.Connection, node_id: int, source_ids: list[int], source_type: str) -> EventTimeCoverage:
    coverage = source_event_time(conn, source_ids, source_type)
    conn.execute(
        'INSERT INTO summary_event_time VALUES (?, ?, ?, ?, ?, ?)',
        (node_id, coverage.earliest_at, coverage.latest_at, coverage.known_count,
         coverage.unknown_count, coverage.legacy_count),
    )
    return coverage


def event_bound_sql(bound: str, node_alias: str = '') -> str:
    """Correlated PK lookup, usable without changing canonical summary columns."""
    if bound not in {'earliest_at', 'latest_at'} or node_alias not in {'', 'n', 'node', 's', 'sn'}:
        raise ValueError('Unsupported event-time SQL expression')
    key = f'{node_alias}.node_id' if node_alias else 'summary_nodes.node_id'
    return f'(SELECT et.{bound} FROM summary_event_time et WHERE et.node_id = {key})'


def message_time_fields(row: dict) -> dict:
    observed = row.get('observed_at')
    return {
        'timestamp': observed,
        'observed_at': observed,
        'ingested_at': row.get('ingested_at') or row.get('timestamp'),
        'time_basis': 'observed_at',
        'time_status': 'known' if observed is not None else 'unknown',
    }


def scope_time_status(conn: sqlite3.Connection, session_ids: list[str] | None = None) -> dict:
    where = ''
    params = []
    if session_ids is not None:
        if not session_ids:
            return {'basis':'observed_at', 'unknown_summary_nodes':0, 'mixed_summary_nodes':0}
        where = 'WHERE node.session_id IN (' + ','.join('?' for _ in session_ids) + ')'
        params = session_ids
    row = conn.execute(
        'SELECT SUM(CASE WHEN COALESCE(et.known_count,0)=0 THEN 1 ELSE 0 END), '
        'SUM(CASE WHEN et.known_count>0 AND (et.unknown_count>0 OR et.legacy_count>0) '
        'THEN 1 ELSE 0 END) FROM summary_nodes node '
        'LEFT JOIN summary_event_time et ON et.node_id=node.node_id ' + where, params,
    ).fetchone()
    return {'basis':'observed_at', 'unknown_summary_nodes':int(row[0] or 0), 'mixed_summary_nodes':int(row[1] or 0)}
