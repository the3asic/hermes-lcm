"""Run the pinned Hermes live-sync function against a real isolated LCM engine."""
import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from hermes_lcm.config import LCMConfig
from hermes_lcm.engine import LCMEngine


@pytest.fixture
def host(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = Path(__file__).parent / "fixtures" / "hermes_live_compression_31150a3.py"
    spec = importlib.util.spec_from_file_location("host_sync_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.logger = logging.getLogger(__name__)
    module.is_truthy_value = lambda value: str(value).lower() in {"true", "1", "yes"}
    init = ModuleType("agent.agent_init")
    init.set_config_context_length = module.set_config_context_length
    init.config_context_length_for_runtime = lambda agent, cfg: (cfg.get("model") or {}).get("context_length")
    init._resolve_compression_threshold = lambda pct, *args, **kwargs: (pct, None)
    compressor = ModuleType("agent.context_compressor")
    compressor.resolve_model_threshold = module.resolve_model_threshold
    defaults = ModuleType("hermes_cli.config_defaults")
    defaults.DEFAULT_CONFIG = {"compression": {"threshold_tokens": 256000}}
    auxiliary = ModuleType("agent.auxiliary_client")
    auxiliary._compression_threshold_for_model = lambda *args, **kwargs: None
    auxiliary._is_codex_gpt54_or_gpt55 = lambda *args: False
    auxiliary._is_codex_spark = lambda *args: False
    for name, obj in [("agent.agent_init", init), ("agent.context_compressor", compressor),
                      ("hermes_cli.config_defaults", defaults), ("agent.auxiliary_client", auxiliary)]:
        monkeypatch.setitem(sys.modules, name, obj)
    engines = []
    def make(config=None):
        engine = LCMEngine(config=config, hermes_home=str(home))
        engine.update_model("test-model", 100000, provider="test-provider")
        engines.append(engine)
        return engine
    def apply(engine, cfg):
        (home / "config.yaml").write_text(yaml.safe_dump(cfg))
        agent = SimpleNamespace(context_compressor=engine, model=engine.model, provider=engine.provider)
        module._apply_live_compression_config(agent, cfg)
        return agent
    try:
        yield SimpleNamespace(make=make, apply=apply)
    finally:
        for engine in engines:
            engine.shutdown()


def test_real_host_sync_applies_ratio_and_absolute_cap(host):
    engine = host.make()
    host.apply(engine, {"compression": {"threshold": 0.5, "threshold_tokens": 1000}})
    assert engine.threshold_tokens == 1000
    assert engine.context_threshold == 0.5
    assert engine.should_compress(1000)


def test_removed_and_null_caps_restore_distinct_host_defaults(host):
    engine = host.make()
    host.apply(engine, {"compression": {"threshold": 0.3, "threshold_tokens": 1000}})
    host.apply(engine, {"compression": {"threshold": 0.6, "threshold_tokens": None}})
    assert engine.threshold_tokens == 60000
    host.apply(engine, {})
    assert engine.threshold_tokens_cap == 256000
    assert engine.threshold_tokens == 50000


@pytest.mark.parametrize("override", ["env", "yaml", "manual"])
def test_explicit_lcm_ratio_wins_over_live_host_ratio(host, monkeypatch, override):
    if override == "env":
        monkeypatch.setenv("LCM_CONTEXT_THRESHOLD", "0.7")
    config = LCMConfig(context_threshold=0.7) if override == "manual" else None
    engine = host.make(config)
    cfg = {"compression": {"threshold": 0.2, "threshold_tokens": None}}
    if override == "yaml":
        cfg["lcm"] = {"context_threshold": 0.7}
    host.apply(engine, cfg)
    assert engine.context_threshold == 0.7
    assert engine.threshold_tokens == 70000


def test_assembly_cap_remains_stricter_than_host_cap(host):
    engine = host.make(LCMConfig(context_threshold=0.5, max_assembly_tokens=800))
    host.apply(engine, {"compression": {"threshold_tokens": 1000}})
    assert engine.threshold_tokens == 800


def test_model_switch_and_clone_recompute_instead_of_copying_stale_trigger(host):
    engine = host.make()
    cfg = {"compression": {"threshold": 0.5, "threshold_tokens": 90000,
                           "model_thresholds": {"test-model": 0.3, "next-model": 0.7}}}
    host.apply(engine, cfg)
    assert engine.threshold_tokens == 30000
    engine.update_model("next-model", 60000, provider="test-provider")
    assert engine.threshold_tokens == 42000
    clone = engine.clone_for_agent()
    try:
        assert clone.threshold_tokens == 42000
        assert clone.threshold_tokens_cap == 90000
    finally:
        clone.shutdown()


def test_context_pin_removal_restores_original_window(host):
    engine = host.make()
    host.apply(engine, {"model": {"context_length": 40000}, "compression": {"threshold": 0.5}})
    assert engine.raw_context_length == 40000
    assert engine.threshold_tokens == 20000
    host.apply(engine, {"compression": {"threshold": 0.5}})
    assert engine.raw_context_length == 100000
    assert engine.threshold_tokens == 50000


@pytest.mark.parametrize("value, expected", [(None, None), (0, None), (-1, None), (False, None),
                                             (True, None), ("bad", None), (float("inf"), None),
                                             ("1000", 1000), (1000, 1000)])
def test_cap_normalization(value, expected):
    assert LCMEngine._coerce_threshold_tokens_cap(value) == expected


def test_pin_removal_after_model_switch_uses_new_model_window(host):
    engine = host.make()
    host.apply(engine, {"model": {"context_length": 40000}})
    engine.update_model("next-model", 90000, provider="test-provider")
    host.apply(engine, {"compression": {"threshold": 0.5}})
    assert engine.raw_context_length == 90000
    assert engine.threshold_tokens == 45000


def test_same_profile_clone_can_remove_inherited_context_pin(host):
    engine = host.make()
    host.apply(engine, {"model": {"context_length": 40000}})
    clone = engine.clone_for_agent()
    try:
        host.apply(clone, {"compression": {"threshold": 0.5}})
        assert clone.raw_context_length == 100000
        assert clone.threshold_tokens == 50000
    finally:
        clone.shutdown()
