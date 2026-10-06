# ORION runtime 1.0.0-rc.10

This is the complete Python service source for the matching ORION workbench. It includes services, workflow and QA MCP entry points, scripts, dependency locks, migrations, templates, resource notices and tests. It does not contain user credentials, business state, a Python virtual environment or external infrastructure.

Use the matching complete RC10 delivery kit and its `install.zh-CN.md` or `web-install.zh-CN.md` for full host/plugin configuration. The helper ZIP in that kit supplies the configuration generator. The official Harness host is 0.2.0-rc.2; the workbench and this runtime must both be 1.0.0-rc.10.

## Prepare Core

Choose absolute, non-symlink paths. Keep the business Profile outside this runtime source. If a Profile already exists, back it up and inspect it; do not overwrite it to repeat installation.

```sh
export ORION_RUNTIME_ROOT=/path/to/dsh-orion-runtime-1.0.0-rc.10
export ORION_PROFILE_ROOT=/path/to/orion-profile
uv venv --python 3.12 "$ORION_PROFILE_ROOT/.venvs/core"
UV_PROJECT_ENVIRONMENT="$ORION_PROFILE_ROOT/.venvs/core" uv sync --project "$ORION_RUNTIME_ROOT" --locked
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B "$ORION_RUNTIME_ROOT/scripts/orion_runtime_manifest.py" --root "$ORION_RUNTIME_ROOT" --check
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B "$ORION_RUNTIME_ROOT/scripts/prepare_orion_runtime.py" --runtime-root "$ORION_RUNTIME_ROOT" --profile-root "$ORION_PROFILE_ROOT" --python "$ORION_PROFILE_ROOT/.venvs/core/bin/python" --port 8091
```

The prepare command creates an explicit path configuration and empty registry; it does not start a service and refuses to overwrite existing configuration. The official workbench manages its own Core subprocess after complete Profile configuration. Do not start a duplicate service on the same port.

Runtime data lives under Profile/state, caches under Profile/cache and the Core environment under Profile/.venvs/core. Configure an actual operator for business writes. Authorization, source scope, stage approval and formal release gates remain required.

## Dependencies and acceptance

Python 3.11–3.13 is declared; 3.12 is the validated deployment choice. Install Wren in a separate Profile/.venvs/wren environment using scripts/requirements-wren.txt. Do not mix its dependency versions into Core.

Formal S5 needs a compatible Protégé/MCP/HermiT setup. The managed S6 entry requires Semantica runtime configuration; default S7 also requires model synchronization. Document publication needs PostgreSQL current-version storage, MinIO and Fuseki. Database-source validation needs an explicitly read-only source and Docker/Ontop. OCR MCP is needed for scanned inputs; the native database selector needs Chat2DB MCP. The custom adapters, image build recipe and service initialization are not all supplied in this archive. Installing their Python libraries does not deploy these services.

Read `dependencies-and-acceptance.zh-CN.md` in the complete delivery kit, or the [repository guide](https://github.com/Striv1/dsh-orion-plugins/blob/main/docs/dependencies-and-acceptance.zh-CN.md), before planning business acceptance. Configure explicit per-Profile runtime-manager and MCP environments, rather than relying on ambient shell exports. Database connections must be explicit; no deployment credentials are supplied. PDF text extraction currently relies on macOS Swift/PDFKit and some workers use POSIX fcntl, so full Windows/Linux support is not claimed.

An empty registry returns NO_PUBLISHED_RUNTIME. This means that no formally published ontology is available, not that a business question has been answered successfully. Actual authorized data, S0–S7 evidence, human approval and release read-back are necessary for business acceptance.

contracts/runtime-source-manifest.json pins the full source/resource fingerprint; backend-source-fingerprint.json must match. Tests are listed separately. The package is a source installation; wheel equivalence and complete remote multi-user server deployment have not been verified. Third-party notices and template licenses are retained; repository visibility does not alter their terms.
