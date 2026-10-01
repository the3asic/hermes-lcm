"""Absent host tool calls must not break summary serialization."""

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.engine import LCMEngine


@pytest.fixture
def engine(tmp_path):
    instance = LCMEngine(
        LCMConfig(database_path=str(tmp_path / "serialization.db")),
        hermes_home=str(tmp_path / "hermes"),
    )
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.mark.parametrize("tool_calls", [None, [], {}, "", 0, False])
def test_empty_tool_calls_serialize_as_no_calls(engine, tool_calls):
    text = engine._serialize_messages([
        {"role": "assistant", "content": "Hello there.", "tool_calls": tool_calls},
    ])
    assert "[ASSISTANT]: Hello there." in text
    assert "[Tool calls:" not in text


def test_null_calls_do_not_drop_a_following_tool_result(engine):
    text = engine._serialize_messages([
        {"role": "assistant", "content": "No calls.", "tool_calls": None},
        {"role": "tool", "tool_call_id": "call_a", "content": "result body"},
    ])
    assert "[ASSISTANT]: No calls." in text
    assert "[TOOL RESULT call_a]: result body" in text


def test_normal_calls_preserve_function_arguments_and_results(engine):
    text = engine._serialize_messages([
        {"role": "assistant", "content": "Reading.", "tool_calls": [
            {"id": "call_a", "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "file contents"},
    ])
    assert 'read_file({"path": "notes.txt"})' in text
    assert "[TOOL RESULT call_a]: file contents" in text
