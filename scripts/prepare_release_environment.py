#!/usr/bin/env python3
"""Create an LCM test environment from an already PM-built real host graph."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("The selected real Hermes runtime requires Python 3.14")
    core = args.core_root.resolve(strict=True)
    out = args.out.absolute()
    if out.exists() or out.is_symlink():
        raise FileExistsError(out)
    lock = core / "uv.lock"
    lock_bytes = lock.read_bytes()
    # These must come from the host's project/test/dev build, before adding
    # LCM's optional dependencies. A three-package environment fails here.
    required = {name: metadata.version(name) for name in ("pytest", "openai", "anthropic", "httpx")}
    host_source = {relative: hashlib.sha256((core / relative).read_bytes()).hexdigest()
                   for relative in ("pyproject.toml", "pm/build_env.py", "pm/build_operations.py")}
    requirements = sorted({
        f"{distribution.metadata['Name']}=={distribution.version}"
        for distribution in metadata.distributions()
        if distribution.metadata["Name"] not in {"hermes-agent", "hermes-lcm"}
    })
    requirements.extend(("numpy", "PyYAML==6.0.3", "tiktoken>=0.11,<1"))
    out.parent.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    manifest = out.with_name(out.name + "-requirements.txt")
    with manifest.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(requirements) + "\n")
    command = [
        sys.executable, "-m", "pm.build_env", "--requirements", str(manifest),
        "--out", str(out), "--python", sys.executable, "--lock", str(lock),
    ]
    if args.cache is not None:
        command.extend(("--cache", str(args.cache.absolute())))
    if args.offline:
        command.append("--offline")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(core)
    subprocess.run(command, cwd=core, env=env, check=True)
    executable = out / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        raise RuntimeError(f"PM did not produce an interpreter at {executable}")
    receipt = {
        "core_root": str(core),
        "core_sha": subprocess.check_output(["git", "-C", str(core), "rev-parse", "HEAD"], text=True).strip(),
        "core_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "core_dirty_paths": subprocess.check_output(
            ["git", "-C", str(core), "status", "--short"], text=True,
        ).splitlines(),
        "host_source_sha256": host_source,
        "host_python": sys.executable,
        "host_dependency_count": len(requirements) - 3,
        "required_host_distributions": required,
        "requirements": str(manifest),
        "requirements_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "command": command,
        "python": str(executable),
    }
    receipt_path = out.with_name(out.name + "-build.json")
    with receipt_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
