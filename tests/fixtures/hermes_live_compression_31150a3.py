# Exact host function excerpts for deterministic integration tests.
# Source: NousResearch/hermes-agent at 31150a3195fbdafbd5effcbf4885aaa2b6a4fa4d (MIT).
from __future__ import annotations
import contextlib

# tui_gateway/session_compression.py
def _compressor_ctor_default(name: str, fallback: Any) -> Any:
    """Default read off ContextCompressor.__init__'s REAL signature, so unset-key restoration uses the
    construction path's derivation instead of a hardcoded copy that could drift.

    Unset restoration must go through the same derivation the construction path uses (#94724 review finding
    on #95980) — pulling the default off ``ContextCompressor.__init__`` itself instead of hardcoding copies
    keeps the two from drifting.
    """
    try:
        import inspect
        from agent.context_compressor import ContextCompressor
        default = inspect.signature(ContextCompressor.__init__).parameters[name].default
        return fallback if default is inspect.Parameter.empty else default
    except Exception:
        return fallback

# tui_gateway/session_compression.py
def _default_threshold_tokens_cap():
    """The cap a fresh agent build installs when the key is absent: DEFAULT_CONFIG's
    ``compression.threshold_tokens``. agent_init reads the MERGED config, so "no key in
    config.yaml" still installs the 256K default at construction; key removal here must
    restore that same value. ``None`` instead would re-derive the uncapped ratio trigger
    (500K on a 1M-window model) and the default cap would be gone after the first turn
    (#117093). An explicit ``threshold_tokens: null`` stays ratio-only — the key is present,
    so ``.get`` returns it untouched."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    return (DEFAULT_CONFIG.get("compression") or {}).get("threshold_tokens")

# tui_gateway/session_compression.py
def _derived_default_threshold_percent(agent: Any, compression: dict) -> float:
    """Default compaction threshold when ``compression.threshold`` is unset. Mirrors agent_init: ctor
    global default, then per-model resolution (Codex autoraise etc.) via the SAME
    ``_resolve_compression_threshold`` — removing the key restores the model-derived value."""
    try:
        pct = float(_compressor_ctor_default("threshold_percent", 0.50))
    except (TypeError, ValueError):
        pct = 0.50
    try:
        from agent.agent_init import _resolve_compression_threshold
        from agent.auxiliary_client import _compression_threshold_for_model, _is_codex_gpt54_or_gpt55, _is_codex_spark
        model, provider = getattr(agent, "model", "") or "", getattr(agent, "provider", "") or ""
        autoraise_enabled = str(compression.get("codex_gpt55_autoraise", True)).lower() in {"true", "1", "yes"}
        pct, _notice = _resolve_compression_threshold(
            pct, _compression_threshold_for_model(model, provider, allow_codex_gpt55_autoraise=autoraise_enabled),
            model=model, is_codex_autoraise=_is_codex_gpt54_or_gpt55(model, provider) or _is_codex_spark(model, provider),
        )
    except Exception:
        pass
    return pct

# tui_gateway/session_compression.py
_COMPRESSION_INT_KEYS = (
    ("proactive_prune_tokens", 0, 0),
    ("proactive_prune_min_result_chars", 8000, 0),
    ("proactive_prune_min_reclaim_tokens", 4096, 0),
    ("protect_last_n", 20, 0),
    ("min_tail_user_messages", 1, 1),
)

# tui_gateway/session_compression.py
def _apply_live_compression_config(agent: Any, cfg: dict | None) -> None:
    """Update a live session's compressor in place from config.yaml. Every adopted key has UNSET semantics:
    a removed key restores the normalized default (or model-derived value) through the construction
    path's own derivation — acting only on PRESENT keys would leave stale values active forever.

    Every adopted key has UNSET semantics (#94724 review finding on the merged #95980): removing a key from
    config.yaml restores the normalized default — or the model-derived value — on the next turn, through the
    same derivation the construction path uses (ContextCompressor ctor defaults read off its real signature,
    the Codex threshold autoraise via ``_resolve_compression_threshold``, context-length re-inference via
    the deferred ``get_model_context_length`` resolution).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    compression = cfg.get("compression") if isinstance(cfg.get("compression"), dict) else {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    from agent.agent_init import config_context_length_for_runtime, set_config_context_length
    enabled_raw = compression.get("enabled", True)
    agent.compression_enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).lower() in {"true", "1", "yes"}
    agent.codex_responses_native_compaction = is_truthy_value(compression.get("codex_responses_native", False))
    native_threshold_raw = compression.get("codex_responses_compact_threshold", 200_000)
    try:
        if isinstance(native_threshold_raw, bool) or (native_threshold := int(native_threshold_raw)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("Invalid compression.codex_responses_compact_threshold=%r; using 200000.", native_threshold_raw)
        native_threshold = 200_000
    agent.codex_responses_compact_threshold = native_threshold
    # Absence restores the agent_init/config default (0 = disabled).
    with contextlib.suppress(TypeError, ValueError):
        agent.compression_idle_compact_after_seconds = max(0, int(compression.get("idle_compact_after_seconds", 0) or 0))
    cc = getattr(agent, "context_compressor", None)
    if cc is None:
        return
    # tail_mode: unknown/absent values land on the ctor default ("lean"), matching agent_init.
    default_tail = str(_compressor_ctor_default("tail_mode", "lean"))
    mode = str(compression.get("tail_mode", default_tail) or default_tail).strip().lower()
    cc.tail_mode = mode if mode in ("legacy", "lean") else default_tail
    for key, fallback, min_value in _COMPRESSION_INT_KEYS:
        default = int(_compressor_ctor_default(key, fallback))
        raw = compression.get(key, default)
        with contextlib.suppress(TypeError, ValueError):
            setattr(cc, key, max(min_value, default if raw is None else int(raw)))
    with contextlib.suppress(TypeError, ValueError):
        ratio_raw = compression.get("target_ratio", _compressor_ctor_default("summary_target_ratio", 0.20))
        cc.summary_target_ratio = max(0.10, min(float(ratio_raw), 0.80))
    # Absent or invalid shape (agent_init treats both as empty): stale overrides must stop steering.
    raw_thresholds = compression.get("model_thresholds")
    cc.model_thresholds = {
        str(k): float(v) for k, v in raw_thresholds.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
    } if isinstance(raw_thresholds, dict) else {}
    # threshold: present value wins; absence derives via the agent_init resolution (default + autoraise).
    # resolve_model_threshold returns ``pct`` unchanged when model_thresholds is empty.
    from agent.context_compressor import resolve_model_threshold
    pct: float | None = None
    if "threshold" in compression:
        with contextlib.suppress(TypeError, ValueError):
            pct = float(compression["threshold"])
    if pct is None:
        pct = _derived_default_threshold_percent(agent, compression)
    cc._config_threshold_percent = cc._configured_threshold_percent = pct
    base = cc._base_threshold_percent = resolve_model_threshold(
        getattr(agent, "model", "") or "", cc.model_thresholds, pct, getattr(agent, "provider", "") or "",
    )
    try:
        cc.threshold_percent = cc._effective_threshold_percent(cc.context_length, base)
    except Exception:
        cc.threshold_percent = pct
    # Same scoping rule as construction and the switch path: the pin describes the configured default
    # route, so a session that /model-switched elsewhere must not have it re-applied on a config save
    # (None = absent, invalid, or scoped out).
    new_ctx = config_context_length_for_runtime(agent, cfg)
    if new_ctx is not None:
        # Both cached copies: the compressor's (its own re-resolution) and the agent's
        # (switch/fallback + every display surface). Writing one left the other stale, so the
        # session showed a pinned ceiling while compressing against a different window (#116467).
        set_config_context_length(agent, new_ctx)
        with contextlib.suppress(Exception):
            cc.context_length = new_ctx
    elif getattr(cc, "_config_context_length", None) is not None:
        # model.context_length removed: drop the override and force re-inference from model metadata on
        # next access (construction's deferred resolution); re-applies the small-context floor too.
        set_config_context_length(agent, None)
        cc._resolved_context_length = None
    cc.threshold_tokens_cap = cc._coerce_threshold_tokens_cap(
        compression.get("threshold_tokens", _default_threshold_tokens_cap())
    )
    # Invalidate the cached trigger so the next preflight re-derives from percent/window, then the cap.
    cc._threshold_tokens = cc._tail_token_budget = None

# agent/agent_init.py
def set_config_context_length(agent, value: Optional[int]) -> None:
    """Store the durable ``model.context_length`` pin on EVERY cached copy of it.

    The pin is read from config exactly once, at construction, then cached twice: on
    ``agent._config_context_length`` (switch/fallback resolution plus every display and ``/usage``
    surface) and on ``context_compressor._config_context_length`` (the compressor's own
    re-resolution). Live paths that updated only one copy left the other stale, so a session could
    report a pinned ceiling while compressing against a different window (#116467).
    """
    agent._config_context_length = value
    _compressor = getattr(agent, "context_compressor", None)
    if _compressor is not None:
        _compressor._config_context_length = value

# agent/context_compressor.py
def _model_threshold_key_rank(key: str, model: str, provider: str) -> "tuple[int, int] | None":
    """Match rank for one ``model_thresholds`` key, or None when it does not apply.
    ``"<provider>:<substr>"`` keys apply only on that provider; bare keys apply on every route.
    The same slug means different windows on different routes (Codex caps Astra at 272K; OpenRouter
    serves the full window), so a bare ``astra: 0.85`` written for Codex silently leaks everywhere.
    Rank = (substring length, scoped): the most specific model match wins, scope breaks ties."""
    scope, sep, substr = key.partition(":")
    if not sep:
        return (len(key), 0) if key in model else None
    return (len(substr), 1) if scope.strip().lower() == provider and substr in model else None

# agent/context_compressor.py
def resolve_model_threshold(
    model: str, model_thresholds: dict[str, float] | None, default: float, provider: str = "",
) -> float:
    """Per-model threshold: longest matching ``model_thresholds`` key wins, else ``default``.
    Keys are substrings of the model name, optionally provider-scoped as ``"<provider>:<substr>"``
    (a scoped key outranks a bare one of the same substring). Module-level so plugin context
    engines can reuse it."""
    if not model_thresholds or not model:
        return default
    provider = (provider or "").strip().lower()
    ranked = ((_model_threshold_key_rank(key, model, provider), key) for key in model_thresholds)
    best = max(((rank, key) for rank, key in ranked if rank is not None), default=None)
    return float(model_thresholds[best[1]]) if best else default
