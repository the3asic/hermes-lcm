# Packaging and distribution posture

## Current decision

`hermes-lcm` intentionally remains a clone-or-symlink Hermes user plugin for now.
For this fork, use `the3asic/hermes-lcm` `main` under the
[fork release policy](../CONTRIBUTING.md#fork-release-policy). The supported install path is:

```bash
git clone --branch main https://github.com/the3asic/hermes-lcm \
  ~/.hermes/plugins/hermes-lcm
```

For profile-specific installs, clone under `~/.hermes/profiles/<profile>/plugins/hermes-lcm`. For development checkouts, `scripts/install.sh` creates a profile-aware symlink into the active Hermes plugin directory and refuses to overwrite an existing checkout or unrelated symlink.

## Source metadata and discovery

The repository is a Hermes plugin, not a standalone Python application. Runtime discovery currently depends on:

- `plugin.yaml` declaring the plugin name and registered tools
- the repo root containing `__init__.py` for Hermes plugin registration
- the operator placing or symlinking the checkout into Hermes' plugin search path
- no plugin-resolved third-party runtime dependencies beyond the host APIs, with optional features such as `tiktoken`, `regex`, NumPy, and FastEmbed supplied by the host environment

`pyproject.toml` declares a metadata-only member for Hermes PM's uv workspace: the plugin name, a `0.0.0` placeholder version, its Python requirement, and an empty dependency list. It also owns lint settings. The project has no build backend; Hermes discovers the source plugin through `plugin.yaml` and `__init__.py`. Hermes PM owns the Python environment and lock, including the required host APIs in `dependency-contract.json`.

## Host-owned dependency assurance

[`dependency-contract.json`](../dependency-contract.json) is the authoritative,
versioned host-owned dependency boundary. It records supported Hermes Agent and
Python versions, the update owner, and every external import observed in shipped
plugin, operator-script, and benchmarking sources. Validate it with:

```bash
python scripts/validate_dependency_contract.py --report-environment
```

The validator fails when runtime imports and the contract drift. Its local
availability/version report is not a lock, SBOM, or vulnerability scan; those
remain the responsibility of the resolved Hermes Agent host environment. See
[Dependency assurance](dependency-assurance.md) for update and scanner evidence
requirements.

## Next packaging step

Make packaging a separate implementation lane only when one of these is true:

1. Hermes Agent documents a stable pip/distribution entrypoint for plugins.
2. Users need version-pinned installs without direct git checkouts.
3. Release automation needs packaged artifacts beyond GitHub tags/releases.

A packaged distribution would need a build and discovery entrypoint plus real installation tests proving that `hermes plugins` discovers `hermes-lcm`, context engine `lcm`, and the tools declared by `plugin.yaml`. The current supported path remains a source checkout or development symlink.

## Current install and update references

- Quickstart: [README](../README.md)
- Detailed install/update/verify: [Operator guide](operator-guide.md)
- Standalone install script contract: [`tests/test_packaging_install.py`](../tests/test_packaging_install.py)
