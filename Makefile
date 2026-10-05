# Only explicit ORION runtime entry points. No credential discovery or old app launchers.
PYTHON ?= python3
ONTOLOGY_PROJECT_ID ?=
ONTOLOGY_WORKFLOW_HOME ?= $(ORION_WORKFLOW_HOME)
PROTEGE_MCP_SECRET ?=
PROTEGE_CONSTRUCTION_APPLICATION ?=
PROTEGE_ROUTING_FILE ?=
SEMANTICA_CLI ?=
SEMANTICA_PYTHON ?=
SEMANTICA_MCP_COMMAND ?=
SEMANTICA_ACTIVE_RUNTIME_CONFIG ?=
SEMANTICA_SYNC_PYTHON ?= $(PYTHON)
SEMANTICA_SYNC_SCRIPT ?= $(CURDIR)/scripts/sync_published_ontologies_to_semantica.py
export PYTHON SEMANTICA_CLI SEMANTICA_PYTHON SEMANTICA_MCP_COMMAND SEMANTICA_ACTIVE_RUNTIME_CONFIG SEMANTICA_SYNC_PYTHON SEMANTICA_SYNC_SCRIPT

.PHONY: build-manifest-check runtime-check protege-construction-route semantica-runtime ontology-s5 ontology-s6
build-manifest-check runtime-check:
	@"$(PYTHON)" scripts/orion_runtime_manifest.py --check

protege-construction-route: build-manifest-check
	@test -n "$(PROTEGE_CONSTRUCTION_APPLICATION)" -a -n "$(PROTEGE_ROUTING_FILE)" -a -n "$(PROTEGE_MCP_SECRET)" || { echo "Configure the explicit Protege application, routing file and MCP secret path." >&2; exit 2; }
	@test -s "$(PROTEGE_MCP_SECRET)" || { echo "Configured Protege MCP secret is unavailable." >&2; exit 2; }
	@"$(PYTHON)" scripts/protege_role_router.py ensure --role construction --application "$(PROTEGE_CONSTRUCTION_APPLICATION)" --routing-file "$(PROTEGE_ROUTING_FILE)" --secret "$(PROTEGE_MCP_SECRET)"

semantica-runtime: build-manifest-check
	@test -n "$(SEMANTICA_CLI)" -a -n "$(SEMANTICA_PYTHON)" -a -n "$(SEMANTICA_MCP_COMMAND)" -a -n "$(SEMANTICA_ACTIVE_RUNTIME_CONFIG)" || { echo "Configure the external Semantica CLI, Python, MCP command and explicit Profile runtime config." >&2; exit 2; }
	@test -s "$(SEMANTICA_ACTIVE_RUNTIME_CONFIG)" || { echo "Configured Semantica runtime selection is unavailable." >&2; exit 2; }
	@scripts/ensure_semantica_runtime.sh ensure

ontology-s5: protege-construction-route
	@test -n "$(ONTOLOGY_PROJECT_ID)" -a -n "$(ONTOLOGY_WORKFLOW_HOME)" || { echo "A project and Profile workflow home are required." >&2; exit 2; }
	@"$(PYTHON)" scripts/run_protege_build_stage.py "$(ONTOLOGY_PROJECT_ID)" --workflow-home "$(ONTOLOGY_WORKFLOW_HOME)" --secret "$(PROTEGE_MCP_SECRET)" --routing-file "$(PROTEGE_ROUTING_FILE)" --lease-owner orion-platform

ontology-s6: build-manifest-check semantica-runtime
	@test -n "$(ONTOLOGY_PROJECT_ID)" -a -n "$(ONTOLOGY_WORKFLOW_HOME)" || { echo "A project and Profile workflow home are required." >&2; exit 2; }
	@"$(PYTHON)" scripts/run_quality_validation_stage.py "$(ONTOLOGY_PROJECT_ID)" --workflow-home "$(ONTOLOGY_WORKFLOW_HOME)"
