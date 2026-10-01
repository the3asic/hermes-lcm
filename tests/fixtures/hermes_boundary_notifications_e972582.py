# Exact lifecycle notification excerpts, Hermes Core e972582 (MIT).
# Tests inject the host observer dependencies; the notification bodies are unchanged.
from __future__ import annotations
from typing import Any, Optional
import contextlib
import logging

logger = logging.getLogger(__name__)

def _quietly(fn):
    fn()


def _swallow(message, *, exc_info=False):
    return contextlib.suppress(Exception)

def _notify_context_engine_session_end(agent: Any, messages: Optional[list]) -> None:
    """Tell the context engine the session ended (flush DAG, close DBs) at the same lifecycle moment as the
    memory manager, so per-session engine state never leaks into the next session."""
    engine = getattr(agent, "context_compressor", None)
    if engine:
        _quietly(lambda: engine.on_session_end(agent.session_id or "", messages or []))

def _notify_context_engine_compression_complete(agent: Any, *, new_session_id: str, old_session_id: str) -> bool:
    """Notify the active context engine after a durable compression commit."""
    # Opt-in relay session-span segmentation. Observer semantics — failure must
    # never undo or delay the committed compression.
    with _swallow('relay segment rotation notification failed', exc_info=True):
        from agent import relay_runtime
        relay_runtime.SESSION_COORDINATOR.notify_session_compacted(
            profile_key=relay_runtime.current_profile_key(), session_id=new_session_id, old_session_id=old_session_id
        )
    callback = getattr(agent.context_compressor, "on_session_start", None)
    if not callable(callback):
        return False
    try:
        callback(
            new_session_id, boundary_reason="compression", old_session_id=old_session_id,
            platform=getattr(agent, "platform", None) or "cli",
            conversation_id=getattr(agent, "_gateway_session_key", None),
        )
        return True
    except Exception:
        # Context-engine hooks are observers. A callback failure must not undo
        # history that the core or an outer host transaction already committed.
        logger.debug("context engine on_session_start (compression) failed", exc_info=True)
        return False
