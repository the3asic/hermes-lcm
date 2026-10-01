"""Import the standalone plugin without ambient Hermes state or host bootstrap."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types

import pytest


# Collection imports run before fixtures. Isolate them as well as test bodies.
_collection_home = tempfile.TemporaryDirectory(prefix="lcm-pytest-")
_original_env = {
    key: value for key, value in os.environ.items()
    if key.startswith("LCM_") or key in {"HERMES_HOME", "HERMES_DISABLE_LAZY_INSTALLS"}
}
for key in list(os.environ):
    if key.startswith("LCM_"):
        del os.environ[key]
os.environ["HERMES_HOME"] = _collection_home.name
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

# Register the package path without executing its registration entrypoint.
# Normal imports now load only the requested submodules and propagate errors.
if "hermes_lcm" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "hermes_lcm", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    sys.modules["hermes_lcm"] = importlib.util.module_from_spec(spec)

# Do not discover a host by guessing parent directory depth. The standalone
# helper supplies the same base API used by release/benchmark unit runs.
if "agent" not in sys.modules:
    agent = types.ModuleType("agent")
    agent.__path__ = []
    sys.modules["agent"] = agent
from hermes_lcm.benchmarking.standalone import ensure_agent_context_engine_importable

ensure_agent_context_engine_importable()


@pytest.fixture(autouse=True)
def _isolate_lcm_env(monkeypatch, tmp_path):
    """Each test owns its defaults; explicit monkeypatch overrides still work."""
    for key in list(os.environ):
        if key.startswith("LCM_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")


def pytest_sessionfinish(session, exitstatus):
    """Restore the caller's environment after the test session ends."""
    for key in list(os.environ):
        if key.startswith("LCM_") or key in {"HERMES_HOME", "HERMES_DISABLE_LAZY_INSTALLS"}:
            os.environ.pop(key)
    os.environ.update(_original_env)
    _collection_home.cleanup()
