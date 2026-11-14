# Source event time and storage time

A raw message's `observed_at` is its validated source/host timestamp.
`observed_at_source` identifies that source. A missing timestamp stays unknown.
`ingested_at` and the historical `timestamp` column record the LCM write time.
They can be days later than the event and never establish an event date.

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
an event date.

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

Before release, prove both readers against an offline candidate: the selected
Core/LCM pair must publish and reopen known, unknown and mixed coverage; an
exported prior source snapshot must still open the candidate and read its raw
messages and summaries. The recorded-host validation scripts accept operator
exports and never create model output or provider usage stamps. Production
migration, backfill and service restart are separate operations.
