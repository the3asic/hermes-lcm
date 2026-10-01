"""Source rechecks and summary/frontier visibility use one file-backed commit."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.dag import SummaryDAG, SummaryNode
from hermes_lcm.engine import LCMEngine


@pytest.fixture
def engine(tmp_path):
    home = tmp_path / "home"
    instance = LCMEngine(LCMConfig(database_path=str(tmp_path / "lcm.db")), hermes_home=str(home))
    instance.on_session_start("session", conversation_id="lane")
    instance.ingest([{"role": "user", "content": "original fact"},
                     {"role": "assistant", "content": "confirmed fact"}])
    try:
        yield instance
    finally:
        instance.shutdown()


def prepare(engine):
    ids = [r["store_id"] for r in engine._store.get_session_messages("session")]
    snapshot, validate = engine._prepare_summary_publication(ids, "messages")
    node = SummaryNode(session_id="session", summary="faithful summary", source_ids=ids)
    return node, snapshot, validate


def test_commit_exposes_node_and_frontier_together(engine):
    node, snapshot, validate = prepare(engine)
    engine._dag.publish_node(node, snapshot, frontier_store_id=max(node.source_ids), validate_runtime=validate)
    with sqlite3.connect(engine._store.db_path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM summary_nodes").fetchone()[0] == 1
        assert reader.execute("SELECT current_frontier_store_id FROM lcm_lifecycle_state WHERE conversation_id='lane'").fetchone()[0] == max(node.source_ids)
    assert engine._store.get_session_messages("session")[0]["content"] == "original fact"


@pytest.mark.parametrize("mutation", ["content", "tool_calls", "delete", "frontier", "session"])
def test_changed_source_or_ownership_rejects_all_publication(engine, mutation):
    node, snapshot, validate = prepare(engine)
    with sqlite3.connect(engine._store.db_path) as writer:
        if mutation == "delete":
            writer.execute("DELETE FROM messages WHERE store_id=?", (node.source_ids[0],))
        elif mutation in {"content", "tool_calls"}:
            writer.execute(f"UPDATE messages SET {mutation}=? WHERE store_id=?", ("changed", node.source_ids[0]))
        elif mutation == "frontier":
            writer.execute("UPDATE lcm_lifecycle_state SET current_frontier_store_id=99 WHERE conversation_id='lane'")
        else:
            writer.execute("UPDATE lcm_lifecycle_state SET current_session_id='replacement' WHERE conversation_id='lane'")
    with pytest.raises(RuntimeError, match="changed|disappeared"):
        engine._dag.publish_node(node, snapshot, frontier_store_id=max(node.source_ids), validate_runtime=validate)
    assert engine._dag.get_session_nodes("session") == []


def test_failure_after_insert_rolls_back_node_frontier_and_fts(engine):
    node, snapshot, validate = prepare(engine)
    from hermes_lcm.rollup_store import RollupStore
    RollupStore(engine._store.db_path).close()
    with sqlite3.connect(engine._store.db_path) as conn:
        conn.execute("CREATE TRIGGER fail_frontier BEFORE UPDATE ON lcm_lifecycle_state BEGIN SELECT RAISE(ABORT,'injected frontier failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        engine._dag.publish_node(node, snapshot, frontier_store_id=max(node.source_ids), validate_runtime=validate)
    with sqlite3.connect(engine._store.db_path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM summary_nodes").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH 'faithful'").fetchone()[0] == 0
        assert reader.execute("SELECT current_frontier_store_id FROM lcm_lifecycle_state WHERE conversation_id='lane'").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM lcm_rollup_invalidations").fetchone()[0] == 0
    assert node.node_id == 0


def test_concurrent_writers_publish_same_snapshot_only_once(engine):
    first, snapshot, validate = prepare(engine)
    sibling = SummaryDAG(engine._store.db_path)
    second = SummaryNode(session_id="session", summary="other summary", source_ids=first.source_ids)
    barrier = Barrier(2)
    def publish(dag, node):
        barrier.wait(timeout=5)
        try:
            dag.publish_node(node, snapshot, frontier_store_id=max(node.source_ids), validate_runtime=validate)
            return "committed"
        except RuntimeError:
            return "rejected"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(publish, dag, node) for dag, node in [(engine._dag, first), (sibling, second)]]
            assert sorted(f.result(timeout=10) for f in futures) == ["committed", "rejected"]
        assert len(engine._dag.get_session_nodes("session")) == 1
    finally:
        sibling.close()


def test_runtime_rebind_and_captured_cancellation_reject_late_result(engine):
    cancelled = False
    engine._compression_cancelled_check = lambda: cancelled
    node, snapshot, validate = prepare(engine)
    cancelled = True
    engine._compression_cancelled_check = lambda: False
    with pytest.raises(RuntimeError, match="cancelled"):
        engine._dag.publish_node(node, snapshot, validate_runtime=validate)
    assert engine._dag.get_session_nodes("session") == []


def test_base_exception_after_insert_rolls_back(engine):
    node, snapshot, _ = prepare(engine)
    calls = []
    def interrupt():
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        engine._dag.publish_node(node, snapshot, frontier_store_id=max(node.source_ids), validate_runtime=interrupt)
    assert engine._dag.get_session_nodes("session") == []


def test_actual_condensation_rejects_source_changed_during_model(engine, monkeypatch):
    node, snapshot, validate = prepare(engine)
    engine._dag.publish_node(node, snapshot, validate_runtime=validate)
    import hermes_lcm.engine as module
    def summary(**kwargs):
        with sqlite3.connect(engine._store.db_path) as writer:
            writer.execute("UPDATE summary_nodes SET summary='changed' WHERE node_id=?", (node.node_id,))
        return "stale condensed text", 1
    monkeypatch.setattr(module, "summarize_with_escalation", summary)
    with pytest.raises(RuntimeError, match="changed"):
        engine._condense_summary_nodes([node])
    assert len(engine._dag.get_session_nodes("session")) == 1


def test_actual_leaf_rejects_mutated_source_before_any_node(engine, monkeypatch):
    engine.on_session_start("leaf-session", conversation_id="leaf-lane")
    engine._config.fresh_tail_count = 2
    engine._config.leaf_chunk_tokens = 1
    engine._config.dynamic_leaf_chunk_enabled = False
    messages = [{"role": "user", "content": "source fact " * 100},
                {"role": "assistant", "content": "source reply " * 100},
                {"role": "user", "content": "recent question"},
                {"role": "assistant", "content": "recent reply"}]
    def summarize(chunk, **kwargs):
        ids = engine._get_store_ids_for_messages(chunk)
        with sqlite3.connect(engine._store.db_path) as writer:
            writer.execute("UPDATE messages SET content='changed' WHERE store_id=?", (ids[0],))
        return chunk, 1000, "stale leaf", 1, 0
    monkeypatch.setattr(engine, "_summarize_leaf_chunk_with_rescue", summarize)
    with pytest.raises(RuntimeError, match="changed"):
        engine.compress(messages, current_tokens=100000)
    assert engine._dag.get_session_nodes("session") == []


def test_actual_host_dispatch_cancellation_rejects_late_leaf(engine, monkeypatch):
    import contextlib
    import importlib.util
    from pathlib import Path
    import sys
    from threading import Event
    from types import ModuleType, SimpleNamespace
    path = Path(__file__).parent / "fixtures" / "hermes_summary_dispatch_31150a3.py"
    spec = importlib.util.spec_from_file_location("host_dispatch_fixture", path)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host._mark_compressor_working_attempt = lambda *args: None
    class Cancelled(Exception):
        pass
    host.AuxiliaryExplicitCancellation = Cancelled
    aux = ModuleType("agent.auxiliary_client")
    # The model deliberately ignores cancellation; publication must reject it.
    for name in ("aux_progress_hook", "aux_stream_deadline", "aux_interrupt_protection"):
        setattr(aux, name, lambda *args, **kwargs: contextlib.nullcontext())
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", aux)
    engine.on_session_start("late-leaf", conversation_id="late-lane")
    engine._config.fresh_tail_count = 2
    engine._config.leaf_chunk_tokens = 1
    engine._config.dynamic_leaf_chunk_enabled = False
    entered, release = Event(), Event()
    def summary(chunk, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return chunk, 1000, "late summary", 1, 0
    monkeypatch.setattr(engine, "_summarize_leaf_chunk_with_rescue", summary)
    messages = [{"role": "user", "content": "old source " * 100},
                {"role": "assistant", "content": "old response " * 100},
                {"role": "user", "content": "recent request"},
                {"role": "assistant", "content": "recent response"}]
    fence = host.CompressionCommitFence()
    agent = SimpleNamespace(context_compressor=engine, session_id="late-leaf")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host._run_summary_dispatch, agent, messages, engine.compress,
                             {"current_tokens": 100000}, commit_fence=fence,
                             attempt_generation=1, hard_cancel_event=None)
        assert entered.wait(timeout=10)
        assert fence.cancel_before_commit()
        release.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            future.result(timeout=10)
    assert engine._dag.get_session_nodes("late-leaf") == []
    assert engine._lifecycle.get_by_conversation("late-lane").current_frontier_store_id == 0
