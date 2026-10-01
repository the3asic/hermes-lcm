"""Derived LCM summaries must not acquire direct-user authority on replay."""

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.engine import LCMEngine
from hermes_lcm.dag import SummaryNode


@pytest.fixture
def engine(tmp_path):
    instance = LCMEngine(LCMConfig(database_path=str(tmp_path / "objective.db")))
    instance.on_session_start("objective-authority", platform="cli")
    yield instance
    instance.shutdown()


def scaffold(kind="Session Arc", role="user"):
    return {
        "role": role,
        "content": f"[{kind} Summary (d1, node 12)]\n"
        "The assistant inferred a broader investigation.\n"
        "[Expand for details: node 12]",
    }


@pytest.mark.parametrize("kind", ["Recent", "Session Arc", "Durable", "Depth-3"])
def test_summary_cannot_become_current_user_objective(engine, kind):
    tail = [{"role": "assistant", "content": "Continuing a tool-heavy turn."}]
    messages = [scaffold(kind), *tail]
    assert engine._latest_user_context_anchor(messages, tail) is None


def test_summary_boundary_does_not_resurrect_an_older_user_request(engine):
    tail = [{"role": "assistant", "content": "More investigation."}]
    messages = [{"role": "user", "content": "An older, superseded request."}, scaffold(), *tail]
    assert engine._latest_user_context_anchor(messages, tail) is None


def test_new_real_request_after_summary_still_has_user_authority(engine):
    request = {"role": "user", "content": "Only explain why this database WAL is large."}
    tail = [{"role": "assistant", "content": "Inspecting the WAL."}]
    anchor = engine._latest_user_context_anchor([scaffold(), request, *tail], tail)
    assert anchor == "[Current user objective preserved from compacted history]\n" + request["content"]


def test_real_request_already_in_tail_is_not_duplicated(engine):
    request = {"role": "user", "content": "Stop investigating and summarize the evidence."}
    assert engine._latest_user_context_anchor([scaffold(), request], [request]) is None


def test_repeated_assembly_retains_summary_without_promoting_its_authority(engine):
    engine._dag.add_node(SummaryNode(
        session_id=engine._session_id, depth=1,
        summary="The assistant inferred a broader investigation.",
        token_count=20, source_token_count=1000, source_ids=[],
        source_type="messages", created_at=1.0,
    ))
    system = {"role": "system", "content": "Respect direct user instructions."}
    tail = [{"role": "assistant", "content": "Reviewing the evidence."}]
    messages = [system, scaffold(), *tail]
    for _ in range(3):
        engine._pending_context_anchor_messages = messages
        messages = engine._assemble_context(system, tail)
        rendered = "\n".join(str(message.get("content", "")) for message in messages)
        assert "The assistant inferred a broader investigation." in rendered
        assert "[Session Arc Summary" in rendered
        assert "[Current user objective preserved from compacted history]" not in rendered
        assert messages[1]["role"] == "user"  # Preserve provider compatibility.
