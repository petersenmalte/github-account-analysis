# Durable analysis data

`state/analytics.duckdb` is the local persistent state database. `derived/`
contains reproducible Parquet snapshots and `current-status.json`; both are
intended to be committed by the scheduled workflow after a validated update.
`declared_attributions.parquet` keeps classification facts keyed by the active
event-selection identity, separate from globally deduplicated SHA facts.

`inbox/` is intentionally ignored because GH Archive inputs can be large.
Provenance remains reproducible through the source URL, source digest, UTC
window, configuration digest, parser rule version, and row counts recorded in
the state and manifests. Do not treat a source digest as a retained raw-data
copy.
