"""Summary acceptance must meet size caps as well as existing quality floors."""
import pytest
from hermes_lcm import escalation
from hermes_lcm.tokens import count_tokens


def test_shorter_than_source_but_over_budget_echo_escalates(monkeypatch):
    echo = "oversized echo " * 200
    calls = []
    def reply(prompt, max_tokens, model="", timeout=None):
        calls.append(max_tokens)
        return echo
    monkeypatch.setattr(escalation, "_call_llm_for_summary", reply)
    result, level = escalation.summarize_with_escalation(
        "source detail " * 2000, source_tokens=10000, token_budget=100,
        l3_truncate_tokens=512,
    )
    assert level == 3
    assert count_tokens(result) <= 100
    assert len(calls) == 2


def test_over_budget_primary_can_use_valid_fallback(monkeypatch):
    monkeypatch.setattr(escalation, "_call_llm_for_summary", lambda prompt, max_tokens, model="", timeout=None:
                        "echo " * 300 if model == "primary" else "valid short summary")
    result, level = escalation.summarize_with_escalation(
        "source " * 1000, source_tokens=1000, token_budget=100,
        model="primary", fallback_models=["fallback"],
    )
    assert level == 1
    assert result == "valid short summary"


def test_large_source_thin_result_still_rejected_when_within_cap(monkeypatch):
    monkeypatch.setattr(escalation, "_call_llm_for_summary", lambda *args, **kwargs: "tiny")
    _, level = escalation.summarize_with_escalation(
        "large source " * 2000, source_tokens=100000, token_budget=1000,
    )
    assert level == 3


def test_l2_enforces_its_smaller_budget(monkeypatch):
    calls = []
    def reply(prompt, max_tokens, **kwargs):
        calls.append(max_tokens)
        return "" if len(calls) == 1 else "medium echo " * 150
    monkeypatch.setattr(escalation, "_call_llm_for_summary", reply)
    result, level = escalation.summarize_with_escalation(
        "source " * 5000, source_tokens=5000, token_budget=400,
    )
    assert level == 3
    assert count_tokens(result) <= 400
