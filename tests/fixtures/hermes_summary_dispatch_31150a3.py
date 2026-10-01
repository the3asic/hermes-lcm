# Exact host cancellation/dispatch excerpts, Hermes Core31150a3 (MIT).
from __future__ import annotations
import threading
import time
import contextlib
import contextvars
import logging
from typing import Any, Callable, Optional

# Test loaders inject these host-owned dependencies before dispatch.
def _mark_compressor_working_attempt(*args: Any) -> None:
    raise RuntimeError("host fixture loader must inject attempt ownership")


class AuxiliaryExplicitCancellation(RuntimeError):
    """Placeholder; the loader injects the host cancellation exception."""

logger=logging.getLogger(__name__)
_COMPRESSOR_ATTEMPT_LOCK=threading.RLock()
_COMPRESSOR_ATTEMPT_GENERATION=contextvars.ContextVar("test_attempt",default=0)


def _install_compression_cancelled_check(compressor: Any, check: Any, generation: int) -> None:
    """Install the F4 cancellation consult, stamped with its owner attempt."""
    with _COMPRESSOR_ATTEMPT_LOCK:
        with contextlib.suppress(Exception):
            compressor._compression_cancelled_check = check
            compressor._compression_cancelled_check_owner = generation

def _clear_compression_cancelled_check_if_owner(compressor: Any, generation: int) -> bool:
    """Clear the cancellation consult only when *generation* installed it.
    Prevents a detached late primary from tearing down a newer fallback's callback. Returns True when cleared."""
    with _COMPRESSOR_ATTEMPT_LOCK:
        owner = getattr(compressor, "_compression_cancelled_check_owner", None)
        if owner is not None and generation and owner != generation:
            return False
        with contextlib.suppress(Exception):
            compressor._compression_cancelled_check = None
            compressor._compression_cancelled_check_owner = None
        return True

class CompressionCommitFence:
    """Fence timeout cancellation against post-summary session mutation.
    The sync worker thread cannot be killed; the fence makes the commit boundary deterministic: cancellation
    wins before mutation starts, or waits for an already-started commit to finish completely."""

    def __init__(self, total_ceiling_seconds: float | None = None) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._commit_started = False
        # Readable WITHOUT the lock (begin_commit holds it until finish_commit): hosts see a hung commit.
        # Lock-free commit-phase marker (#76354 review F1). ``begin_commit`` RETAINS ``self._lock`` until
        # ``finish_commit``, so any host-side observation that needs the lock (``try_cancel_before_commit``)
        # blocks/space-outs for the whole commit. This Event is set inside ``begin_commit`` while the lock
        # is held but is READABLE WITHOUT the lock, so a host can observe "a commit was admitted and may be
        # in flight" even while the commit itself is hung — which is exactly when the overrun warning must
        # be able to fire.
        self._commit_phase = threading.Event()
        # Set on ANY host unwind without the fence lock so FUTURE commits are blocked; bool store is atomic.
        # Lock-free admission revocation (#76354 review F2). Set by :meth:`revoke_commit_admission` on ANY
        # host unwind (KeyboardInterrupt, cancellation, unexpected exception) without touching the fence
        # lock, so a host that cannot afford to block behind an in-flight commit can still guarantee no
        # FUTURE commit is admitted.
        self._admission_revoked = False
        # Holder-scoped release published by the worker once it owns the durable lock (no ABA on a NEW holder).
        # Holder-qualified durable-lock release hook (#76354 review F4; transplanted from PR #71569 by
        # @ciabata-git). The worker publishes an idempotent, holder-scoped release callable once it owns the
        # durable compression lock; a timed-out host invokes it to free the lease without racing a NEW
        # holder (DB release is holder-qualified, so a stale release can never delete a replacement's row —
        # no ABA).
        self._lock_release_guard = threading.Lock()
        self._cancelled_lock_release: Optional[Callable[[], None]] = None
        self._cancelled_lock_release_requested = False
        # Touched per streamed token so waiters tell SLOW-but-alive from HUNG (no fixed wall-clock kill).
        self._last_progress = time.monotonic()
        self._progress_observed = False
        self._deadline: float | None = None
        self._retain_cancelled_lock_until_worker_done = False
        # Set once the active-row watermark is captured: later rows survive as tail, so hosts may keep admission.
        self._commit_watermark_fenced = False
        if total_ceiling_seconds is not None:
            self.set_total_ceiling_seconds(total_ceiling_seconds)

    def set_total_ceiling_seconds(self, seconds: float) -> None:
        """Arm the wall-clock deadline shared by the host and worker."""
        seconds = float(seconds)
        if seconds <= 0:
            raise ValueError("total compression ceiling must be positive")
        self._deadline = time.monotonic() + seconds

    def touch_progress(self) -> None:
        """Record forward progress (a streamed token); a bare float store is atomic, so no lock."""
        self._last_progress = time.monotonic()
        self._progress_observed = True

    @property
    def progress_observed(self) -> bool:
        """Whether semantic provider progress was reported for this attempt."""
        return self._progress_observed

    @property
    def deadline_exceeded(self) -> bool:
        deadline = self._deadline
        return deadline is not None and time.monotonic() >= deadline

    @property
    def deadline_monotonic(self) -> float | None:
        """Armed deadline (absolute monotonic); the worker's stream consumer stops when the host stops waiting.

        :meth:`set_total_ceiling_seconds` documents this deadline as "shared by the host and worker", but
        until #99692 only the host could read it — ``deadline_exceeded`` answers "is it past?" for a caller
        that is already polling, which is useless to a worker blocked inside a provider stream. Publishing
        the instant itself lets the worker's stream consumer stop at exactly the moment the host stops
        waiting (see ``auxiliary_client.aux_stream_deadline``).
        """
        return self._deadline

    def seconds_since_progress(self) -> float:
        """Seconds since the worker last reported forward progress."""
        return max(0.0, time.monotonic() - self._last_progress)

    def cancel_before_commit(self, cancel_event: Any = None) -> bool:
        """Cancel a pending commit (``True``), or block until an active commit finishes (``False``)."""
        with self._lock:
            if not self._commit_started:
                self._cancelled = True
            if cancel_event is not None:
                cancel_event.set()
            return not self._commit_started

    def try_cancel_before_commit(self) -> Optional[bool]:
        """Non-blocking :meth:`cancel_before_commit`; ``None`` while an active commit owns the fence."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if not self._commit_started:
                self._cancelled = True
            return not self._commit_started
        finally:
            self._lock.release()

    def begin_commit(self, cancel_event: Any = None) -> bool:
        """Atomically admit commit unless a hard cancellation already won."""
        self._lock.acquire()
        if self.is_cancelled or self._admission_revoked or (cancel_event is not None and bool(cancel_event.is_set())):
            self._cancelled = True
            self._lock.release()
            if self._admission_revoked:
                # A revoke that lost the fence-lock race deferred its lease release; commit refused: release now.
                self.release_cancelled_compression_lock()
            return False
        self._commit_started = True
        # Set under the fence lock so commit_in_flight is never True for a commit that lost to cancellation.
        self._commit_phase.set()
        return True

    def finish_commit(self) -> None:
        """Leave a commit boundary entered by :meth:`begin_commit`."""
        self._commit_phase.clear()
        self._lock.release()
        if self._admission_revoked:
            # A revoke during THIS commit deferred its lease release (never free mid-mutation); release now.
            self.release_cancelled_compression_lock()

    @property
    def commit_in_flight(self) -> bool:
        """Lock-free read: an admitted commit is in progress (hosts reach the overrun loop on a hung commit)."""
        return self._commit_phase.is_set()

    @property
    def is_cancelled(self) -> bool:
        """True after cancellation won before the commit boundary."""
        return self._cancelled or self._admission_revoked or self.deadline_exceeded

    def retain_compression_lock_until_worker_done(self) -> None:
        """Prevent a timed-out live worker from overlapping a retry."""
        self._retain_cancelled_lock_until_worker_done = True

    def mark_commit_watermark_fenced(self) -> None:
        """Record a watermark-bounded commit (later rows survive as tail); a detached worker may keep admission.

        Called by the compression worker right after it captures ``get_active_message_watermark()`` under
        the durable compression lock (#75316/#87484). A watermark-fenced commit archives ONLY rows at or
        below the watermark; rows appended later — e.g. the user turn the host released at the turn-hold
        boundary (#97963) — are cloned as live concurrent tail. That is exactly the property a host needs
        before letting a detached worker keep its commit admission.
        """
        self._commit_watermark_fenced = True

    @property
    def commit_watermark_fenced(self) -> bool:
        """Lock-free read: the worker's commit is watermark-bounded."""
        return self._commit_watermark_fenced

    def allow_cancelled_lock_release(self) -> None:
        """Undo :meth:`retain_compression_lock_until_worker_done` once a bounded join proved the worker exited."""
        self._retain_cancelled_lock_until_worker_done = False

    def revoke_commit_admission(self) -> None:
        """Revoke FUTURE commit admission without blocking on the fence lock.
        An in-flight commit is never abandoned (``begin_commit`` re-checks the flag under the lock). The lease
        release must not run mid-commit: released now if the lock is free, else deferred to
        ``finish_commit``/refusal (holder-qualified)."""
        self._admission_revoked = True
        if self._lock.acquire(blocking=False):
            try:
                self.release_cancelled_compression_lock()
            finally:
                self._lock.release()

    # ── Holder-qualified durable-lease cancellation: release is DELETE WHERE
    # holder = ?, so a stale release can never free a NEW holder's lease (no ABA).

    # ── Holder-qualified durable-lease cancellation (#76354 F4) ────────── Transplanted from PR #71569
    # (@ciabata-git): the worker publishes an idempotent, holder-scoped release hook once it owns the
    # durable compression lock, and the host invokes it after winning cancellation. ABA safety comes from
    # SessionDB.release_compression_lock being holder-qualified (DELETE ... WHERE holder = ?), so a stale
    # release can never free a NEW holder's lease.
    def begin_lock_setup(self) -> bool:
        """Hold the fence across lock acquisition + release-hook publication so a timeout cannot win between."""
        self._lock.acquire()
        if self.is_cancelled or self._admission_revoked:
            self._lock.release()
            return False
        return True

    def finish_lock_setup(self) -> None:
        """Leave a lock setup boundary entered by :meth:`begin_lock_setup`."""
        self._lock.release()

    def register_cancelled_lock_release(self, release: Callable[[], None]) -> bool:
        """Publish the worker's holder-qualified release; if cleanup was already requested, run it and return True."""
        with self._lock_release_guard:
            self._cancelled_lock_release = release
            requested = self._cancelled_lock_release_requested
        if requested:
            release()
        return requested

    def clear_cancelled_lock_release(self, release: Callable[[], None]) -> None:
        """Forget ``release`` after the worker's normal cleanup finishes."""
        with self._lock_release_guard:
            if self._cancelled_lock_release is release:
                self._cancelled_lock_release = None

    def release_cancelled_compression_lock(self) -> None:
        """After cancellation won: release the worker's lock (a request ahead of hook publication is retained)."""
        if self._retain_cancelled_lock_until_worker_done:
            return
        with self._lock_release_guard:
            self._cancelled_lock_release_requested = True
            release = self._cancelled_lock_release
        if release is not None:
            release()

def _run_summary_dispatch(
    agent: Any, messages: list, compress_fn: Callable[..., Any], compress_kwargs: dict[str, Any], *,
    commit_fence: Optional[CompressionCommitFence], attempt_generation: Any, hard_cancel_event: Any,
) -> list:
    """Run the compressor under the fence's progress hook, deadline and interrupt guard."""
    # Publish progress to the commit fence so hosts extend deadlines while tokens
    # flow. Any active hook (even no-op) selects the streamed path: the timeout is
    # inactivity-based and a byte-trickling provider hits the stream total ceiling.
    from agent.auxiliary_client import aux_interrupt_protection, aux_progress_hook, aux_stream_deadline
    _progress_hook = commit_fence.touch_progress if commit_fence is not None else (lambda: None)
    # Return leg: cancel frees the owner but the provider daemon streams on to its
    # own larger ceiling; share the host deadline so orphan streams stop with it.
    _host_stream_deadline = commit_fence.deadline_monotonic if commit_fence is not None else None
    # A LATE successful summary must not undo the host's timeout cooldown: the
    # compressor checks cancellation before clearing; removed in finally (no leak).
    if commit_fence is not None:
        # Install a cancellation check the compressor consults BEFORE clearing the failure cooldown; removed
        # in the finally below so it cannot leak into later attempts (e.g. a manual /compress force-clear).
        # See #76354.
        _install_compression_cancelled_check(
            agent.context_compressor, lambda: commit_fence.is_cancelled, attempt_generation
        )

    def _compression_cancel_requested() -> bool:
        return bool(
            (hard_cancel_event is not None and hard_cancel_event.is_set())
            or (commit_fence is not None and commit_fence.is_cancelled)
        )

    _attempt_ctx_token = _COMPRESSOR_ATTEMPT_GENERATION.set(attempt_generation)
    try:
        # F6: never start expensive summary work for an already-cancelled
        # fence (a stale queued job admitted after host departure).
        if commit_fence is not None and commit_fence.is_cancelled:
            logger.info(
                "Compression cancelled before summary dispatch (session=%s) — skipping summary work.",
                agent.session_id or "none",
            )
            compressed = messages
        else:
            with (
                aux_progress_hook(_progress_hook), aux_stream_deadline(_host_stream_deadline),
                aux_interrupt_protection(cancel_check=_compression_cancel_requested),
            ):
                # This attempt is now doing real summary work: publish it as the working attempt so later
                # no-op entry claims (lock sit-outs, gates, the cancelled-fence skip above) cannot supersede
                # the candidate this run produces (#112482).
                _mark_compressor_working_attempt(agent.context_compressor, attempt_generation)
                compressed = compress_fn(messages, **compress_kwargs)
                # Freeze a hard stop that arrived after the last provider attempt but before session state rotates.
                if hard_cancel_event is not None and hard_cancel_event.is_set():
                    raise AuxiliaryExplicitCancellation()
    finally:
        _COMPRESSOR_ATTEMPT_GENERATION.reset(_attempt_ctx_token)
        if commit_fence is not None:
            _clear_compression_cancelled_check_if_owner(agent.context_compressor, attempt_generation)
    return compressed
