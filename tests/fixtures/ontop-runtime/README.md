# Isolated Ontop runtime fixtures

Minimal, domain-neutral ontology, mapping and query used only by tests. The integration test substitutes `__TEST_ENTITY_TABLE__` with a unique identifier and creates that table only after validating the explicitly configured disposable PostgreSQL/Fuseki environment. No production business tables or archived SupplyGuard files are required.
