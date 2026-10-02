"""Shared store connections must not mix concurrent query bindings or rows."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hermes_lcm.dag import SummaryDAG
from hermes_lcm.store import MessageStore


@pytest.mark.parametrize("store_type", [MessageStore, SummaryDAG])
def test_shared_connection_preserves_concurrent_query_results(tmp_path, store_type):
    store = store_type(tmp_path / "lcm.db")
    conn = store.connection
    workers = 8
    barrier = threading.Barrier(workers)
    sql = """
        WITH RECURSIVE numbers(n) AS (
            SELECT 0 UNION ALL SELECT n + 1 FROM numbers WHERE n < 15
        ) SELECT ?, n FROM numbers ORDER BY n
    """

    def read(worker):
        barrier.wait(timeout=10)
        for iteration in range(250):
            marker = f"worker-{worker}-query-{iteration}"
            rows = conn.execute(sql, (marker,)).fetchall()
            assert [tuple(row) for row in rows] == [(marker, n) for n in range(16)]

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(read, worker) for worker in range(workers)]
            for future in futures:
                future.result(timeout=30)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()
