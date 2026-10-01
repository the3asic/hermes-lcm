"""A host's end/start compression boundary retains durable publication ownership."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.dag import SummaryNode
from hermes_lcm.engine import LCMEngine
from hermes_lcm.lifecycle_state import LifecycleStateStore


@pytest.fixture
def engine(tmp_path):
    instance = LCMEngine(
        LCMConfig(database_path=str(tmp_path / "lcm.db")),
        hermes_home=str(tmp_path / "home"),
    )
    instance.on_session_start("session", conversation_id="lane", platform="telegram")
    try:
        yield instance
    finally:
        instance.shutdown()


def boundary(engine):
    engine.on_session_start(
        "session", boundary_reason="compression", old_session_id="session",
        platform="telegram", conversation_id="lane",
    )


def source_rows(engine):
    return engine._store.get_session_messages("session", limit=100)


def prepare_node(engine):
    ids = [row["store_id"] for row in source_rows(engine)]
    snapshot, validate = engine._prepare_summary_publication(ids, "messages")
    return SummaryNode(session_id="session", summary="next faithful leaf", source_ids=ids), snapshot, validate


def test_core_end_same_start_then_next_leaf_can_publish(engine):
    path = Path(__file__).parent / "fixtures" / "hermes_boundary_notifications_e972582.py"
    spec = importlib.util.spec_from_file_location("host_boundary_notifications", path)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    agent = SimpleNamespace(
        context_compressor=engine, session_id="session", platform="telegram", _gateway_session_key="lane",
    )
    messages = [
        {"role": "user", "content": "old original fact"},
        {"role": "assistant", "content": "old original reply"},
    ]
    engine.ingest(messages)
    originals = source_rows(engine)
    first, snapshot, validate = prepare_node(engine)
    first.summary = "first faithful leaf"
    frontier = max(first.source_ids)
    engine._dag.publish_node(first, snapshot, frontier_store_id=frontier, validate_runtime=validate)
    engine._last_compacted_store_id = frontier
    engine._ingest_cursor = len(messages)
    engine._ingest_cursor_needs_reconcile = False
    before_cursor = engine._ingest_cursor
    engine.compression_count = 1

    # Core commit_memory_session forwards this actual end notification before its successful boundary.
    host._notify_context_engine_session_end(agent, messages)
    finalized = engine._lifecycle.get_by_conversation("lane")
    assert finalized.current_session_id is None
    assert finalized.last_finalized_session_id == "session"
    assert host._notify_context_engine_compression_complete(agent, new_session_id="session", old_session_id="session")

    # This is the first protected operation of the next compaction, using real rows/SQLite publication.
    engine.ingest(messages + [{"role": "user", "content": "genuinely new fact"}])
    second, snapshot, validate = prepare_node(engine)
    engine._dag.publish_node(second, snapshot, frontier_store_id=max(second.source_ids), validate_runtime=validate)
    state = engine._lifecycle.get_by_conversation("lane")
    assert state.current_session_id == "session"
    assert state.current_frontier_store_id == max(second.source_ids)
    assert len(engine._dag.get_session_nodes("session")) == 2
    assert source_rows(engine)[:len(originals)] == originals
    assert len(source_rows(engine)) == len(originals) + 1
    assert before_cursor == len(messages)
    assert engine._ingest_cursor == len(messages) + 1
    assert engine.compression_count == 1


def test_finalized_boundary_restores_exact_frontier_without_replaying_prefix(engine):
    messages = [{"role": "user", "content": "repeatable request"}]
    engine.ingest(messages)
    frontier = source_rows(engine)[0]["store_id"]
    engine._last_compacted_store_id = frontier
    engine._lifecycle.advance_frontier("lane", "session", frontier)
    engine.compression_count = 3
    engine.on_session_end("session", messages)
    boundary(engine)
    state = engine._lifecycle.get_by_conversation("lane")
    assert state.current_session_id == "session"
    assert state.current_frontier_store_id == frontier
    assert engine._last_compacted_store_id == frontier
    assert engine._ingest_cursor == 1
    assert engine._ingest_cursor_needs_reconcile is False
    assert engine.compression_count == 3
    engine.ingest(messages + [{"role": "user", "content": "repeatable request"}])
    assert len(source_rows(engine)) == 2
    assert all(row["content"] == "repeatable request" for row in source_rows(engine))


def test_actual_leaf_compression_after_end_start_preserves_raw_rows(engine, monkeypatch):
    engine._config.fresh_tail_count = 2
    engine._config.leaf_chunk_tokens = 1
    engine._config.dynamic_leaf_chunk_enabled = False
    engine._config.extraction_enabled = False
    messages = [
        {"role": "user", "content": "old source fact " * 100},
        {"role": "assistant", "content": "old source answer " * 100},
        {"role": "user", "content": "recent request"},
        {"role": "assistant", "content": "recent answer"},
    ]
    engine.ingest(messages)
    original = source_rows(engine)
    engine.on_session_end("session", messages)
    boundary(engine)
    calls = []
    def summary(chunk, **kwargs):
        calls.append(chunk)
        return chunk, 1000, "faithful bounded summary", 1, 0
    monkeypatch.setattr(engine, "_summarize_leaf_chunk_with_rescue", summary)
    result = engine.compress(messages, current_tokens=10000, force=True)
    assert calls
    assert result != messages
    assert len(engine._dag.get_session_nodes("session")) == 1
    assert source_rows(engine) == original
    state = engine._lifecycle.get_by_conversation("lane")
    assert state.current_session_id == "session"
    assert state.current_frontier_store_id == original[1]["store_id"]


def test_already_owned_boundary_is_idempotent_and_preserves_debt(engine):
    engine._ingest_cursor = 7
    engine._last_compacted_store_id = 23
    engine._lifecycle.advance_frontier("lane", "session", 23)
    engine._lifecycle.record_debt("lane", kind="raw_backlog", size_estimate=42)
    before = engine._lifecycle.get_by_conversation("lane")
    boundary(engine)
    boundary(engine)
    assert engine._lifecycle.get_by_conversation("lane") == before
    assert engine._ingest_cursor == 7
    assert engine._last_compacted_store_id == 23


def test_reopen_preserves_debt_and_does_not_copy_other_session_frontier(engine):
    engine._lifecycle.record_debt("lane", kind="raw_backlog", size_estimate=42)
    engine._lifecycle._conn.execute(
        "UPDATE lcm_lifecycle_state SET last_finalized_frontier_store_id=100 WHERE conversation_id='lane'",
    )
    engine._last_compacted_store_id = 7
    engine._ingest_cursor = 3
    engine.on_session_end("session", [])
    finalized = engine._lifecycle.get_by_conversation("lane")
    boundary(engine)
    state = engine._lifecycle.get_by_conversation("lane")
    assert state.current_frontier_store_id == 7
    assert state.last_finalized_frontier_store_id == 100
    assert state.debt_kind == finalized.debt_kind == "raw_backlog"
    assert state.debt_size_estimate == finalized.debt_size_estimate == 42
    assert state.debt_updated_at == finalized.debt_updated_at
    assert state.last_finalized_at == finalized.last_finalized_at
    assert engine._ingest_cursor == 3


def test_failed_reopen_rolls_back_and_reports_error(engine):
    engine.on_session_end("session", [])
    before = engine._lifecycle.get_by_conversation("lane")
    engine._lifecycle._conn.execute(
        "CREATE TRIGGER fail_resume BEFORE UPDATE ON lcm_lifecycle_state "
        "BEGIN SELECT RAISE(ABORT, 'injected resume failure'); END",
    )
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="injected resume failure"):
        boundary(engine)
    assert engine._lifecycle.get_by_conversation("lane") == before
    assert engine._lifecycle._conn.in_transaction is False


def test_missing_lifecycle_row_does_not_get_created_by_boundary(engine):
    engine._lifecycle._conn.execute("DELETE FROM lcm_lifecycle_state WHERE conversation_id='lane'")
    with pytest.raises(RuntimeError, match="ownership"):
        boundary(engine)
    assert engine._lifecycle.get_by_conversation("lane") is None


@pytest.mark.parametrize("replacement_finalized", [False, True])
def test_stale_boundary_cannot_reclaim_replacement_owner(engine, replacement_finalized):
    engine.ingest([{"role": "user", "content": "old source"}])
    engine.on_session_end("session", [])
    sibling = LifecycleStateStore(engine._store.db_path)
    try:
        sibling.bind_session("replacement", conversation_id="lane")
        if replacement_finalized:
            sibling.finalize_session("lane", "replacement")
        before = sibling.get_by_conversation("lane")
        before_cursor = engine._ingest_cursor
        with pytest.raises(RuntimeError, match="ownership"):
            boundary(engine)
        assert sibling.get_by_conversation("lane") == before
        assert engine._ingest_cursor == before_cursor
        node, snapshot, validate = prepare_node(engine)
        with pytest.raises(RuntimeError, match="ownership"):
            engine._dag.publish_node(node, snapshot, validate_runtime=validate)
        assert engine._dag.get_session_nodes("session") == []
    finally:
        sibling.close()


def test_replacement_during_model_still_rejects_publication(engine):
    engine.ingest([{"role": "user", "content": "old source"}])
    engine.on_session_end("session", [])
    boundary(engine)
    node, snapshot, validate = prepare_node(engine)
    sibling = LifecycleStateStore(engine._store.db_path)
    try:
        sibling.bind_session("replacement", conversation_id="lane")
        with pytest.raises(RuntimeError, match="changed"):
            engine._dag.publish_node(node, snapshot, validate_runtime=validate)
        assert engine._dag.get_session_nodes("session") == []
        assert sibling.get_by_conversation("lane").current_session_id == "replacement"
    finally:
        sibling.close()


def test_cross_connection_reopen_does_not_overwrite_replacement(engine):
    engine.on_session_end("session", [])
    sibling = LifecycleStateStore(engine._store.db_path)
    barrier = Barrier(2)
    def resume():
        barrier.wait(timeout=5)
        try:
            boundary(engine)
            return "resumed"
        except RuntimeError:
            return "refused"
    def replace():
        barrier.wait(timeout=5)
        sibling.bind_session("replacement", conversation_id="lane")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(resume), pool.submit(replace)
            assert first.result(timeout=10) in {"resumed", "refused"}
            second.result(timeout=10)
        assert sibling.get_by_conversation("lane").current_session_id == "replacement"
    finally:
        sibling.close()
