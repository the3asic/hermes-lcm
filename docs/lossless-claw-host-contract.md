# Host prerequisites for further lossless-claw adaptations

This is a proposed integration contract, not an implemented Hermes Core API.
The plugin can validate source snapshots and publish a summary plus its frontier
in one SQLite transaction. It cannot infer durable message identity or obtain an
atomic host cancellation decision from a boolean callback.

## Reviewed versions and existing plugin guarantees

The donor review uses lossless-claw `e05d8d34b2a44fdef556ce95dd90115b46630200`.
The host source reviewed is Hermes Core
`31150a3195fbdafbd5effcbf4885aaa2b6a4fa4d`; source checkout identity does not prove
that an already-running gateway has that version.

The plugin adaptations currently:

- Compare ordered source rows, parent coverage and session/frontier state again
  inside `SummaryDAG.publish_node` before committing the node and frontier.
- Keep each assistant call and its adjacent tool results together when selecting
  request context. Reused tool IDs remain scoped to the concrete exchange.
- Report partial progress when a durable leaf commit precedes a later failure.

These changes do not supply stable event receipts, a background scheduler, or
atomic admission against every host cancellation race.

## Publication admission must share the host fence

At the reviewed Core revision,
[`CompressionCommitFence`](../tests/fixtures/hermes_summary_dispatch_31150a3.py#L32)
(frozen source excerpt, original `agent/conversation_compression.py:448`)
serializes cancellation with host commit admission. Its `begin_commit` holds the
fence until `finish_commit`. The
[`_run_summary_dispatch`](../tests/fixtures/hermes_summary_dispatch_31150a3.py#L246)
excerpt (original `agent/conversation_compression.py:3005`) installs an attempt-owned cancellation check on the compressor. A callback
check followed by a SQLite commit is still two operations: cancellation may
arrive between them.

A future host API must grant the *same attempt* a short publication section that
serializes with cancellation. It must specify whether the publication consumes
host transcript admission or is a separate phase; blindly invoking the existing
`begin_commit` from the plugin may interfere with the later host commit.

Required behavior:

1. Bind admission to canonical profile home, session/lane, and attempt generation.
2. Keep provider calls outside the admission section and outside SQLite writes.
3. Establish one lock order for the host fence and plugin database writer.
4. Revalidate sources while owning the SQLite write transaction.
5. If cancellation wins first, publish nothing. If publication wins first, the
   host must observe its durable receipt before reporting cancellation.
6. Release admission on every exception, including interruption. Late workers
   must retain their old attempt identity and cannot use a new attempt's token.

Acceptance needs deterministic barriers immediately before admission and before
SQLite commit, with both race orders, stale attempts, session switches, source
mutation, exceptions, and a provider that ignores cancellation. Passing the
current cancelled-before-model-return test is insufficient for the exact race.

## Stable event identity and receipts belong to host ingestion

The host must supply a durable, structured event key scoped to profile and
conversation, together with provenance, ordering and payload revision semantics.
A provider response ID can contribute only when its origin and stability are
known. Tool call IDs and model-generated strings are not global event IDs.

The same event replay should return its original receipt. Two deliberate user
messages with identical text must remain two events. Reused tool IDs must not
join different turns. An edited payload under an existing event key must follow
an explicit update/reject policy; it cannot silently overwrite evidence.

A receipt must distinguish raw ingestion from summary publication and host
transcript replacement. Its durable identity and result must commit alongside
the affected rows in the owning store. Cross-database operations need a recovery
contract rather than a claim of single-transaction atomicity.

Acceptance: same-event retry/restart produces one receipt and no duplicate rows;
identical text under different keys produces two rows; conflicting revisions,
reused tool IDs and profile/session collisions remain visible and correctly
scoped. Crash injection must cover the write/receipt boundary.

Do not introduce a global content-hash uniqueness constraint. Unknown legacy
identity remains ambiguous; preserve evidence rather than deleting repeated text.

## Background work must use a host-owned execution boundary

Before moving compaction off the foreground path, the host must define durable
admission, ownership, cancellation, completion and replay semantics. Reuse its
existing scheduler and state owner; this document does not authorize a second
service, queue, cron job or production schema.

A timeout ends the caller's wait, not a non-cooperative worker. Retain the bounded
permit and attempt ownership until work actually exits. Any lease release must
be holder-qualified so an old worker cannot release a new holder's lease.

Acceptance must cover duplicate delivery, restart, expired ownership, a late
model result, bounded live workers and shutdown with active work. No worker may
publish after session/profile ownership or admission has changed.

## Integration decision

Review the concrete Core API and its lock/state ownership before implementing
these prerequisites. Then add capability checks and isolated host integration
fixtures to LCM. Older hosts must preserve current foreground behavior without
claiming the new guarantees. Production migration and activation are separate
from the plugin PRs.
