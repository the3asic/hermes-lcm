# Source event time and storage time

A raw message's `observed_at` is its validated source/host timestamp.
`observed_at_source` identifies that source. A missing timestamp stays unknown.
`ingested_at` records the LCM write time, which can be days after the event.
The historical `timestamp` column retains the owning writer's compatibility
meaning: normal ingest stores write time, while imported records may retain the
export's timestamp or an explicit legacy fallback. Neither field establishes an
event date.

New summaries publish their source-event coverage to `summary_event_time` in
one transaction with the summary and any lifecycle frontier. Leaves aggregate
raw `observed_at` values. Condensation combines the children's persisted event
coverage at every depth. The coverage contains the earliest/latest known event,
known/unknown source contributions and legacy child contributions. A mixed
summary's bounds describe its known portion only; the undated portion cannot be
assigned to that interval. An absent side-table row identifies an older summary
whose event-time coverage has not been established.

The summary's existing `earliest_at`/`latest_at` columns retain their historical
storage-time meaning. `created_at` is the summary creation time. Their column
order and schema version remain compatible with older code. Inspection and
search results expose `event_time` separately and label the old fields as
`storage_time_basis=ingested_at`.

Raw `time_from`/`time_to` filtering and recency ordering use `observed_at`.
Unknown event dates cannot satisfy a date filter and sort after known dates.
Raw result `timestamp` is the event timestamp or null; `ingested_at` remains
available explicitly. Summary/vector time filtering uses event-range overlap;
unknown coverage is excluded. No branch substitutes a write/creation date for
an event date. Ordinary recall leaves the relevance of an unknown-date hit
unchanged; a missing date is neither a recency claim nor an automatic relevance
penalty.

`lcm_recent` selects event ranges that overlap the requested UTC window. Its
response identifies unknown and mixed summary coverage in the requested
conversation scope. Unknown-date summaries cannot be assigned to that window;
mixed summaries show their known interval and retain the coverage warning.
The result does not promise that every statement in a mixed summary happened
inside the window.

UTC day/week/month rollups require complete event coverage throughout their
canonical scope. An unknown or mixed canonical summary blocks publication of a
complete temporal rollup. The read path uses source summaries with explicit
coverage instead. New event-based rollups are certified separately in
`lcm_rollup_event_time`; the certificate matches the source fingerprint.
Existing storage-based rollups remain on disk but cannot serve an event-time
query. Aggregation requires certified constituent dailies and complete source
nodes. Publication, mutation, session moves and deletion enqueue known event
intervals in the existing transactional invalidation outbox. They retain its
bounded UTC-day draining and publication-generation checks.

The additive tables and indexes are installed only when candidate code opens
its own database. Opening does not backfill old `observed_at`, recompute old
summary coverage, rebuild old rollups or upgrade historical permissions.
Coverage recomputation for existing summaries is a separate database operation
requiring its own source-identity, recovery and authorization review.

New Lossless Claw SQLite imports preserve a message's explicit `created_at`
using the source export's UTC convention. A missing or invalid message date
stays unknown even when legacy ordering falls back to a conversation's creation
time. JSONL imports accept the normal host timestamp forms: positive finite Unix
seconds or timezone-aware ISO-8601. Each imported row records its import time
separately and labels the source of any known event date. Imported summaries
publish coverage from their resolved raw-message or child-summary sources in
the same import transaction. Reopening an old import does not certify its
existing rows or regenerate summaries.

Interim schema-stamp remediation preserves recognized event-time side tables.
Their exact shape is checked before any derived-family repair; unknown shapes
remain a refusal. Rebuilding a separate feature cache cannot serve as evidence
that a historical event date has been established.

Before release, prove both readers against an offline candidate: the selected
Core/LCM pair must publish and reopen known, unknown and mixed coverage; an
exported prior source snapshot must still open the candidate and read its raw
messages and summaries. The recorded-host validation scripts accept operator
exports and never create model output or provider usage stamps. Production
migration, backfill and service restart are separate operations.
