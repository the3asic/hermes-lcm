"""Host cancellation must not turn durable leaf progress into another paid leaf."""

import copy

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.engine import LCMEngine
from hermes_lcm.tokens import count_messages_tokens


class HostCancellation(BaseException):
    """The host's explicit auxiliary cancellation is outside Exception."""


@pytest.fixture
def engine(tmp_path, monkeypatch):
    instance = LCMEngine(LCMConfig(
        database_path=str(tmp_path / "lcm.db"), fresh_tail_count=2,
        leaf_chunk_tokens=1, dynamic_leaf_chunk_enabled=False,
        extraction_enabled=False, assertion_extraction_enabled=False,
        embeddings_enabled=False, condensation_fanin=100,
    ), hermes_home=str(tmp_path / "home"))
    instance.on_session_start("session", conversation_id="lane", context_length=200000)
    calls = []

    def summary(chunk, **kwargs):
        calls.append(copy.deepcopy(chunk))
        return chunk, count_messages_tokens(chunk), "saved facts about the original request", 1, 0

    monkeypatch.setattr(instance, "_summarize_leaf_chunk_with_rescue", summary)
    instance.summary_calls = calls
    try:
        yield instance
    finally:
        instance.shutdown()


def history():
    return [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": f"original occurrence {index} " * 100, "_row_id": index + 1}
        for index in range(6)
    ]


def raw_view(messages):
    return [{key: message[key] for key in ("role", "content", "_row_id") if key in message} for message in messages]


@pytest.mark.parametrize("failure_type", [TimeoutError, HostCancellation])
@pytest.mark.parametrize("stage", ["condensation", "assembly"])
def test_cancelled_publication_is_reused_without_model_or_condensation(engine, monkeypatch, failure_type, stage):
    original = history()
    error = failure_type("host budget exhausted after publication")

    def fail(*args, **kwargs):
        raise error

    method = "_maybe_condense" if stage == "condensation" else "_assemble_context"
    implementation = getattr(engine, method)
    monkeypatch.setattr(engine, method, fail)
    with pytest.raises(failure_type) as caught:
        engine.compress(copy.deepcopy(original), current_tokens=10000, force=True)
    assert caught.value is error
    assert engine.last_compression_status == "partial"
    assert engine.compression_count == 1
    nodes = engine._dag.get_session_nodes("session")
    assert len(nodes) == 1 and len(nodes[0].source_ids) == 4
    frontier = engine._last_compacted_store_id
    monkeypatch.setattr(engine, method, implementation)
    monkeypatch.setattr(engine, "_maybe_condense", fail)
    result = engine.compress(copy.deepcopy(original), current_tokens=10000, force=True)
    assert len(engine.summary_calls) == 1
    assert engine._dag.get_session_nodes("session") == nodes
    assert engine._last_compacted_store_id == frontier
    assert raw_view(result[-2:]) == raw_view(original[-2:])
    assert any("saved facts" in str(message.get("content")) for message in result)
    assert len(engine._store.get_session_messages("session", limit=100)) == len(original)


def test_published_prefix_recovery_keeps_new_literal_occurrence(engine, monkeypatch):
    original = history()

    def fail(**kwargs):
        raise TimeoutError("post-leaf failure")

    monkeypatch.setattr(engine, "_maybe_condense", fail)
    with pytest.raises(TimeoutError):
        engine.compress(copy.deepcopy(original), current_tokens=10000, force=True)
    new_turn = {**original[0], "_row_id": 7}
    replay = copy.deepcopy(original) + [new_turn, {"role": "assistant", "content": "new answer", "_row_id": 8}]
    result = engine.compress(replay, current_tokens=10000, force=True)
    assert len(engine.summary_calls) == 1
    assert raw_view(result[-4:]) == raw_view(replay[-4:])
    assert len(engine._store.get_session_messages("session", limit=100)) == 8


def test_missing_source_mapping_stops_before_spending_or_publication(engine, monkeypatch):
    monkeypatch.setattr(engine, "_get_store_id_map_for_messages", lambda *args, **kwargs: {})
    original = history()
    result = engine.compress(original, current_tokens=10000, force=True)
    assert result == original
    assert engine.summary_calls == []
    assert engine._dag.get_session_nodes("session") == []
    assert engine._last_compacted_store_id == 0


def test_retry_without_core_ids_keeps_live_occurrence_proof(engine, monkeypatch):
    original = history()
    for message in original:
        message.pop("_row_id")

    def fail(**kwargs):
        raise HostCancellation("cancel after leaf")

    monkeypatch.setattr(engine, "_maybe_condense", fail)
    with pytest.raises(HostCancellation):
        engine.compress(copy.deepcopy(original), current_tokens=10000, force=True)
    result = engine.compress(copy.deepcopy(original), current_tokens=10000, force=True)
    assert len(engine.summary_calls) == 1
    assert raw_view(result[-2:]) == raw_view(original[-2:])
    assert any("saved facts" in str(message.get("content")) for message in result)


def test_cold_reload_recovers_durable_core_occurrences(engine, monkeypatch):
    original = history()

    def fail(*args, **kwargs):
        raise HostCancellation("cancel after leaf")

    monkeypatch.setattr(engine, "_maybe_condense", fail)
    with pytest.raises(HostCancellation):
        engine.compress(copy.deepcopy(original), current_tokens=10000, force=True)
    config, home = engine._config, engine._hermes_home
    engine.shutdown()
    revived = LCMEngine(config, hermes_home=home)
    try:
        revived.on_session_start("session", conversation_id="lane", context_length=200000)
        monkeypatch.setattr(revived, "_summarize_leaf_chunk_with_rescue", fail)
        monkeypatch.setattr(revived, "_maybe_condense", fail)
        result = revived.compress(copy.deepcopy(original), current_tokens=10000, force=True)
        assert any("saved facts" in str(message.get("content")) for message in result)
        assert raw_view(result[-2:]) == raw_view(original[-2:])
        assert len(revived._dag.get_session_nodes("session")) == 1
        assert len(revived._store.get_session_messages("session", limit=100)) == 6
    finally:
        revived.shutdown()


def test_host_timeout_signal_reaches_existing_route_breaker(engine):
    breaker = engine._summary_circuit_breaker
    model = engine._config.summary_model
    breaker.record_success(model)  # A successful leaf in this same pass.
    breaker.record_failure(model)  # Condensation L1 rejected its output.
    assert breaker.allows(model)
    engine.record_timeout_failure("host cancelled L2", failure_kind="stall_interrupted")
    assert not breaker.allows(model)

