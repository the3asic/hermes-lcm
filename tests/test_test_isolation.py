"""Check collection and registration against a hostile ambient environment."""

import os
from pathlib import Path
import subprocess
import sys


def test_collection_and_registration_ignore_ambient_database_and_host(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "conftest.py").write_text(
        (repo / "tests" / "conftest.py").read_text().replace(
            "plugin_dir = Path(__file__).resolve().parent.parent",
            f"plugin_dir = Path({str(repo)!r})",
        ), encoding="utf-8",
    )
    sentinel = tmp_path / "production.db"
    sentinel.write_bytes(b"do not open or modify")
    host = tmp_path / "host"
    (host / "agent").mkdir(parents=True)
    marker = tmp_path / "host-imported"
    (host / "agent" / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "raise RuntimeError('host bootstrap must not run')\n", encoding="utf-8",
    )
    (probe / "test_probe.py").write_text('''
import importlib.util
import os
from pathlib import Path
from hermes_lcm.config import LCMConfig

# Collection must already be isolated, before any test fixture executes.
assert not os.environ.get("LCM_DATABASE_PATH")
assert not os.environ.get("LCM_EMBEDDINGS_ENABLED")
assert os.environ["HERMES_DISABLE_LAZY_INSTALLS"] == "1"


def test_register():
    import hermes_lcm
    spec = importlib.util.spec_from_file_location(
        "lcm_isolation_probe", Path(hermes_lcm.__path__[0]) / "__init__.py",
        submodule_search_locations=hermes_lcm.__path__,
    )
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    class Context:
        def register_context_engine(self, engine):
            self.engine = engine
    ctx = Context()
    module.register(ctx)
    try:
        assert ctx.engine._store.db_path.parent == Path(os.environ["HERMES_HOME"])
        assert LCMConfig.from_env().embeddings_enabled is False
    finally:
        ctx.engine.shutdown()
''', encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "LCM_DATABASE_PATH": str(sentinel), "LCM_EMBEDDINGS_ENABLED": "1",
        "HERMES_HOME": str(tmp_path / "production-home"),
        "PYTHONPATH": str(host), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "child-temp"), str(probe)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.read_bytes() == b"do not open or modify"
    assert not marker.exists()
    assert not (tmp_path / "production-home").exists()
