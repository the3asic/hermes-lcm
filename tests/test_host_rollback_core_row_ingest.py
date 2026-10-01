"""A rejected host commit can replay the original Core rows after LCM rebases its cursor."""
import copy

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.engine import LCMEngine
from hermes_lcm.tokens import count_messages_tokens


@pytest.fixture
def engine(tmp_path, monkeypatch):
    instance = LCMEngine(LCMConfig(
        database_path=str(tmp_path / "lcm.db"), fresh_tail_count=2,
        leaf_chunk_tokens=1, dynamic_leaf_chunk_enabled=False,
        extraction_enabled=False, assertion_extraction_enabled=False,
        embeddings_enabled=False, condensation_fanin=100,
    ), hermes_home=str(tmp_path / "home"))
    instance.on_session_start("session", conversation_id="lane", platform="telegram", context_length=200000)
    def summary(chunk, **kwargs):
        return chunk, count_messages_tokens(chunk), "faithful bounded summary", 1, 0
    monkeypatch.setattr(instance, "_summarize_leaf_chunk_with_rescue", summary)
    try:
        yield instance
    finally:
        instance.shutdown()


def history():
    return [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": ("repeatable original request " if index % 2 == 0 else f"original answer {index} ") * 100,
         "timestamp": 100 + index, "_row_id": index + 1}
        for index in range(6)
    ]


def rows(engine):
    return engine._store.get_session_messages("session", limit=100)


def compress_then_host_rollback(engine):
    original = history()
    engine.ingest(original)
    before = rows(engine)
    result = engine.compress(original, current_tokens=10000, force=True)
    assert len(result) == 3
    assert engine._ingest_cursor == 3
    assert rows(engine) == before
    # Both Core anti-growth refusal and archive failure return this original history.
    return original, before


def test_rollback_then_next_leaf_appends_only_new_core_occurrences(engine):
    original, before = compress_then_host_rollback(engine)
    replay = copy.deepcopy(original)
    replay += [
        {**copy.deepcopy(original[0]), "_row_id": 7},
        {"role": "assistant", "content": "new answer", "timestamp": 107, "_row_id": 8},
    ]
    result = engine.compress(replay, current_tokens=10000, force=True)
    assert result != replay
    after = rows(engine)
    assert after[:len(before)] == before
    assert len(after) == len(before) + 2
    assert sum(row["content"] == original[0]["content"] for row in after) == 4
    assert after[-2]["content"] == original[0]["content"]
    assert after[-1]["content"] == "new answer"
    assert len(engine._dag.get_session_nodes("session")) == 2
    assert engine._lifecycle.get_by_conversation("lane").current_session_id == "session"


def test_rollback_with_no_new_rows_never_appends_original_suffix(engine):
    original, before = compress_then_host_rollback(engine)
    engine.ingest(copy.deepcopy(original))
    assert rows(engine) == before
    assert engine._ingest_cursor == len(original)


@pytest.mark.parametrize("mutation", ["content", "foreign_session", "source_digest", "new_row_id"])
def test_unproved_core_occurrence_is_not_filtered(engine, mutation):
    original, before = compress_then_host_rollback(engine)
    replay = copy.deepcopy(original)
    if mutation == "content":
        replay[3]["content"] = "modified Core output projection"
    elif mutation == "foreign_session":
        engine._store._conn.execute("UPDATE messages SET session_id='other' WHERE store_id=?", (before[3]["store_id"],))
        engine._store._conn.commit()
    elif mutation == "source_digest":
        engine._store._conn.execute("UPDATE messages SET content='changed durable source' WHERE store_id=?", (before[3]["store_id"],))
        engine._store._conn.commit()
    else:
        replay[3]["_row_id"] = 70
    engine.ingest(replay)
    after = rows(engine)
    expected_existing = len(before) - int(mutation == "foreign_session")
    assert len(after) == expected_existing + 1
    assert after[-1]["content"] == replay[3]["content"]


def test_no_core_identity_is_not_content_deduplicated(engine):
    original, before = compress_then_host_rollback(engine)
    replay = copy.deepcopy(original)
    for message in replay:
        message.pop("_row_id")
    engine.ingest(replay)
    assert len(rows(engine)) == len(before) + 3
