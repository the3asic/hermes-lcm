"""Publish inherited dated regression fixtures through normal source APIs.

Callers must supply the fixture's event dates explicitly. Summary creation and
storage timestamps never establish a source event date. This helper creates no
provider output and does not certify a legacy node in place.
"""
from __future__ import annotations

from hermes_lcm.dag import SummaryDAG, SummaryNode
from hermes_lcm.store import MessageStore


def publish_dated_leaf(
    dag: SummaryDAG,
    node: SummaryNode,
    *,
    event_times: tuple[float, ...],
    store: MessageStore | None = None,
) -> int:
    if node.source_type != "messages" or not event_times:
        raise ValueError("A dated leaf needs explicit source-message event times")
    owned = store is None
    source_store = MessageStore(dag.db_path) if owned else store
    try:
        node.source_ids = source_store.append_batch(
            node.session_id,
            [{"role": "user", "content": node.summary, "timestamp": value}
             for value in event_times],
        )
        rows = source_store.get_batch(node.source_ids)
        if tuple(rows[source_id]["observed_at"] for source_id in node.source_ids) != event_times:
            raise ValueError("The fixture event dates were rejected by the source contract")
        snapshot = dag.publication_snapshot(node.source_ids, "messages")
        return dag.publish_node(node, snapshot)
    finally:
        if owned:
            source_store.close()
