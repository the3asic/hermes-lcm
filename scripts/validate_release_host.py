#!/usr/bin/env python3
"""Verify release prerequisites against the explicitly selected real Core tree."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-root", type=Path, required=True)
    args = parser.parse_args()
    core = args.core_root.resolve(strict=True)
    sys.path.insert(0, str(core))
    attempts = []

    def audit(event, arguments):
        if event == "socket.connect":
            attempts.append(str(arguments[1]))
            raise RuntimeError("Release host validation requires cached offline dependencies")

    sys.addaudithook(audit)
    from agent.context_engine import ContextEngine
    from agent.message_sanitization import native_anthropic_row_costs
    from agent.replayed_reasoning import codex_reasoning_replay_tokens
    import numpy
    import tiktoken

    symbols = {
        "agent/context_engine.py": ContextEngine,
        "agent/message_sanitization.py": native_anthropic_row_costs,
        "agent/replayed_reasoning.py": codex_reasoning_replay_tokens,
    }
    for relative, symbol in symbols.items():
        expected = core / relative
        if Path(inspect.getfile(symbol)).resolve() != expected or not callable(symbol):
            raise RuntimeError(f"Release host API did not come from {expected}")
    encoding = tiktoken.get_encoding("cl100k_base")
    if attempts:
        raise RuntimeError("Release host validation attempted a network connection")
    print(json.dumps({
        "core_root": str(core),
        "core_sha": subprocess.check_output(
            ["git", "-C", str(core), "rev-parse", "HEAD"], text=True,
        ).strip(),
        "core_dirty_paths": subprocess.check_output(
            ["git", "-C", str(core), "status", "--short"], text=True,
        ).splitlines(),
        "host_source_sha256": {
            relative: hashlib.sha256((core / relative).read_bytes()).hexdigest()
            for relative in symbols
        },
        "python": sys.version,
        "executable": sys.executable,
        "numpy": {
            "version": importlib.metadata.version("numpy"),
            "module": inspect.getfile(numpy),
        },
        "tiktoken": {
            "version": importlib.metadata.version("tiktoken"),
            "module": inspect.getfile(tiktoken),
            "encoding": encoding.name,
        },
        "network_connect_attempts": len(attempts),
        "scope": "host identity and dependency preflight; no recording or provider acceptance",
    }, indent=2))


if __name__ == "__main__":
    main()
