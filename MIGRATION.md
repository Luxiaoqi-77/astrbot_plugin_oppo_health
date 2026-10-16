# OPPO Health period care integration

Recorded 2026-10-10. This source update adds confirmed-period evidence,
confirmation-gated inquiry delivery and deduplication, plus the verified
Calendar grid geometry correction.

The update does not replace or rewrite `data/plugin_data/oppo_health/care/`.
Existing rule enable flags and scheduling fields are retained by the schema
upgrade; the new post-period rule is added closed. Existing scheduler state,
weight opt-in state, and poll interval are preserved. No cycle is seeded and no
real care message or model call is made during installation or validation.

Validation is performed through the production plugin import path. The
candidate includes 18 period end-to-end tests, the 9 care source-state tests,
and a synthetic grid-geometry regression without screenshot fixtures. A
read-only device check verified both adjacent Calendar months with complete
classification and restored the OPPO Health home. Private diagnostic
screenshots remain outside the plugin source tree and are excluded from this
migration.

After reviewed installation, reload the AstrBot plugin `oppo_health` to load
the Python and dashboard changes. The local capture adapter is launched as a
short-lived subprocess for each read; there is no separate persistent
collector service to restart. No reload is performed by this migration.
