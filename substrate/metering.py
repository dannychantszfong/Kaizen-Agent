"""substrate.metering - real provider cost, metered in the trusted layer.

In a live run the body's own `spine.provider.complete` is bypassed: the runner
injects a metered `complete()` that wraps the real provider call, prices the
*actual* model that ran off the *raw* response, accumulates spend in a `CostMeter`
the runner owns, and only then normalizes to a spine `Completion`. Metering lives
here, outside the editable body, so the agent cannot under-report its own spend -
the budget cap (enforced by the watchdog) is only trustworthy if the meter is.

Provider-agnostic by design. The model string lives in the body (`agent/`), so the
agent may switch models - and providers - across generations. The meter therefore
assumes nothing about provider: it prices whatever model the response says ran, and
sums all spend, across all providers, into one running total against the one $cap.

Circuit breaker. If litellm genuinely cannot price the model that ran - it is not in
litellm's cost map, so the authoritative `cost_per_token` lookup raises - the meter is
blind, and a blind meter is a broken budget cap. Rather than fly blind, it flags the
generation unpriceable and raises, so the runner HALTS the lineage. A $0 cost from a
KNOWN model (a free model, or zero usage) is NOT a blind spot and does not trip it.

Testability is deliberate: both the underlying call and the cost function are
injectable, so the whole real-agent path can be proven with a canned response that
carries a `usage` block - no network, no SDK, no spend.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# Mask anything shaped like a provider API key (sk-..., sk-ant-..., sk-or-...) so a
# stray `echo $DEEPSEEK_API_KEY` in a tool result can't leak into the logs.
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{12,}")


def redact(text: str) -> str:
    return _SECRET_RE.sub("sk-***REDACTED***", text)


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())  # collapse whitespace/newlines for a 1-line log
    if len(text) <= limit:
        return text
    return text[:limit] + f"…(+{len(text) - limit} chars)"


def args_summary(args: dict | None, *, per_value: int = 140, total: int = 400) -> str:
    parts = []
    for k, v in (args or {}).items():
        sv = v if isinstance(v, str) else json.dumps(v)
        parts.append(f"{k}={clip(sv, per_value)!r}")
    return clip(", ".join(parts), total)


class UnpriceableModelError(RuntimeError):
    """Raised when a model that actually ran cannot be priced. Stops the generation
    immediately so the runner can halt the lineage rather than bill blind $0."""

    def __init__(self, model: str) -> None:
        super().__init__(f"cannot price model {model!r} - meter would be blind")
        self.model = model


@dataclass
class CostMeter:
    """Accumulates real spend across one generation's provider calls, regardless of
    which providers/models were used."""

    total_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)
    had_unpriceable: bool = False
    unpriceable_models: list[str] = field(default_factory=list)

    def record(
        self,
        *,
        cost_usd: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str = "?",
    ) -> None:
        self.total_usd += max(0.0, cost_usd)
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0.0) + max(0.0, cost_usd)

    def mark_unpriceable(self, model: str) -> None:
        self.had_unpriceable = True
        if model not in self.unpriceable_models:
            self.unpriceable_models.append(model)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _emit(log: Callable[[str], None] | None, msg: str) -> None:
    """Log without ever breaking the loop. A metered call has already recorded its
    spend by the time we log it; a cosmetic logging failure (e.g. a non-UTF-8
    console choking on `·`) must not turn a priced call into a crash."""
    if log is None:
        return
    try:
        log(msg)
    except Exception:  # noqa: BLE001 - logging is never load-bearing
        pass


def _price_with_litellm(response: Any, model: str, ptok: int, ctok: int):
    """Return (cost_usd, priced). `priced` is True iff litellm KNOWS how to price the
    model. Two sources, in order: completion_cost (which also accounts for hidden
    response_cost / prompt caching when given a real response), then the authoritative
    cost-map lookup cost_per_token (which raises only for a genuinely-unknown model).
    A known model with token usage always yields a real figure here, so a blind $0
    cannot masquerade as a priced call; an unknown model returns (0.0, False) -> halt.
    """
    try:
        import litellm
    except Exception:  # noqa: BLE001 - no SDK: cannot price -> let the breaker trip
        return 0.0, False

    try:
        c = float(litellm.completion_cost(completion_response=response))
        if math.isfinite(c) and c > 0:
            return c, True
    except Exception:  # noqa: BLE001 - fall through to the cost map
        pass

    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model, prompt_tokens=ptok, completion_tokens=ctok
        )
        c = float(prompt_cost) + float(completion_cost)
        if math.isfinite(c):
            return c, True  # priced (may legitimately be 0.0 for a free model)
    except Exception:  # noqa: BLE001 - model unknown to litellm's cost map
        pass

    return 0.0, False


def _provider_of(model: str) -> str:
    """Best-effort provider name for logging. Asks litellm if it's importable, else
    falls back to the `provider/model` prefix convention. Never raises."""
    try:
        import litellm

        return str(litellm.get_llm_provider(model)[1])
    except Exception:  # noqa: BLE001 - logging nicety; litellm may be absent in tests
        return model.split("/", 1)[0] if "/" in model else "?"


def _normalize(response: Any):
    """Raw litellm/OpenAI-shaped response -> spine `Completion`.

    Mirrors `spine.provider`'s normalization but lives here so metering does not
    depend on body-editable parsing. Imports the body's `Completion`/`ToolCall`
    types because the loop consumes exactly those.
    """
    from spine.provider import Completion, ToolCall

    message = response.choices[0].message
    tool_calls: list[ToolCall] = []
    for tc in getattr(message, "tool_calls", None) or []:
        try:
            arguments = json.loads(tc.function.arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            arguments = {"__raw__": tc.function.arguments}
        tool_calls.append(
            ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
        )
    return Completion(content=getattr(message, "content", None), tool_calls=tool_calls)


def _est_msg_tokens(m: dict) -> int:
    """Conservative chars->tokens estimate for one chat message (over-estimates so
    trimming stays safely under the real limit; no tokenizer/SDK needed)."""
    c = m.get("content")
    text = c if isinstance(c, str) else json.dumps(c or "")
    total = len(text) // 3 + 4
    for tc in m.get("tool_calls") or []:
        total += len(json.dumps(tc)) // 3
    return total


def _context_limit(model: str) -> int:
    """Best-effort max INPUT tokens for the model, with a conservative default for an
    unknown model (so an exotic small-context model never overflows)."""
    try:
        import litellm

        info = litellm.get_model_info(model) or {}
        m = info.get("max_input_tokens") or info.get("max_tokens")
        if m:
            return int(m)
    except Exception:  # noqa: BLE001 - unknown model / no SDK
        pass
    return 30000


def fit_context(model: str, messages, *, log=None):  # noqa: ANN001
    """Return `messages` trimmed to fit the model's context window: keep the system
    message and the MOST RECENT turns, dropping the oldest, so a long-running
    generation can't blow past the input limit and crash. Pairing-safe: never leaves a
    `tool` result without the assistant turn that called it. The agent's own message
    history is untouched; only the API call sees the window (durable memory is
    MEMORY.md). Reserves room for the response + estimate error."""
    if not messages:
        return messages
    budget = max(2000, int(_context_limit(model) * 0.7))
    counts = [_est_msg_tokens(m) for m in messages]
    if sum(counts) <= budget:
        return messages
    system, rest = messages[:1], list(messages[1:])
    rest_counts = counts[1:]
    running = counts[0] + sum(rest_counts)
    while rest and running > budget:
        running -= rest_counts.pop(0)
        rest.pop(0)
    while rest and rest[0].get("role") == "tool":  # don't orphan a tool result
        rest.pop(0)
    trimmed = system + rest
    if log and len(trimmed) < len(messages):
        log(
            f"  · context trim: {len(messages)} -> {len(trimmed)} msgs "
            f"(~{budget} tok budget) to fit {model}"
        )
    return trimmed


_TRANSIENT_ERRORS = (
    "RateLimit",
    "Timeout",
    "APIConnection",
    "ServiceUnavailable",
    "InternalServer",
    "Overloaded",
    "APIError",
)


def _call_with_retry(raw, model, messages, tools, *, attempts=3, log=None):  # noqa: ANN001
    """Call the provider, retrying clearly-transient errors with backoff, and logging
    any provider error legibly (one line, not litellm's multi-line footer) before
    raising. A non-transient error (or exhausted retries) propagates so the runner's
    dirty-death/rollback handles it."""
    import time as _time

    for i in range(attempts):
        try:
            return raw(
                model=model,
                messages=messages,
                tools=tools or None,
                tool_choice="auto" if tools else None,
            )
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            transient = any(t in name for t in _TRANSIENT_ERRORS)
            retrying = transient and i + 1 < attempts
            _emit(
                log,
                f"[meter] provider error ({name})"
                + (" — retrying" if retrying else "")
                + f": {clip(str(e), 200)}",
            )
            if not retrying:
                raise
            _time.sleep(min(8.0, 2.0**i))


def make_metered_complete(
    meter: CostMeter,
    *,
    model: str | None = None,
    cap_usd: float | None = None,
    prior_spent_usd: float = 0.0,
    log: Callable[[str], None] | None = None,
    on_progress: Callable[[], None] | None = None,
    transcript: bool = False,
    raw_complete: Callable[..., Any] | None = None,
    cost_fn: Callable[[Any], float] | None = None,
):
    """Build a `complete(model, messages, tools)` that meters real, provider-agnostic
    usage and trips a circuit breaker on anything it cannot price.

    `raw_complete` defaults to `litellm.completion`; `cost_fn` defaults to
    `litellm.completion_cost`. Both are injectable so a test can supply a canned
    response + cost with no SDK and no network. `cap_usd`/`prior_spent_usd`/`log`
    drive the per-call line: `provider · model · $cost · lineage $running / $cap`.
    `on_progress` is the substrate's last-progress stamp: an LLM call returning is an
    observed unit of work, so it refreshes liveness (called only by this injected
    closure, never by the agent).
    """

    def complete(model_arg: str, messages, tools=None):  # noqa: ANN001
        effective_model = model_arg or model or "?"
        # Keep the prompt within the model's context window so a long-running
        # generation can't overflow and crash; retry transient provider errors.
        messages = fit_context(
            effective_model, messages, log=log if transcript else None
        )
        _raw = raw_complete
        if _raw is None:
            import litellm

            _raw = litellm.completion

        response = _call_with_retry(_raw, effective_model, messages, tools, log=log)

        usage = getattr(response, "usage", None)
        ptok = int(getattr(usage, "prompt_tokens", 0) or 0)
        ctok = int(getattr(usage, "completion_tokens", 0) or 0)
        provider = _provider_of(effective_model)

        # Price the model that ACTUALLY ran. `priced` means litellm KNOWS this model;
        # the cost itself may be 0 (a genuinely free model) or >0. It stays False only
        # when the model is unknown to litellm (its cost map can't price it).
        if cost_fn is not None:
            try:
                cost = float(cost_fn(response))
                priced = math.isfinite(cost)
            except Exception:  # noqa: BLE001
                cost, priced = 0.0, False
        else:
            cost, priced = _price_with_litellm(response, effective_model, ptok, ctok)

        # Circuit breaker: only if litellm genuinely cannot price this model. We do NOT
        # trip on a $0 result from a KNOWN model (free, or zero-usage) — that caused a
        # false trip; the authoritative cost-map lookup (cost_per_token) returns a real
        # nonzero figure for any priced model with usage, so a blind $0 can't slip by.
        if not priced:
            meter.mark_unpriceable(effective_model)
            _emit(
                log,
                f"[meter] {provider} · {effective_model} · UNPRICEABLE "
                f"({ptok + ctok} tokens) — HALTING (broken circuit breaker)",
            )
            raise UnpriceableModelError(effective_model)

        meter.record(
            cost_usd=cost,
            prompt_tokens=ptok,
            completion_tokens=ctok,
            model=effective_model,
        )
        running = prior_spent_usd + meter.total_usd
        cap = f" / ${cap_usd:.2f}" if cap_usd is not None else ""
        _emit(
            log,
            f"[meter] {provider} · {effective_model} · ${cost:.4f} · "
            f"lineage ${running:.4f}{cap}",
        )
        if on_progress is not None:  # an LLM call returned => observed progress
            try:
                on_progress()
            except Exception:  # noqa: BLE001 - liveness stamp is never load-bearing
                pass

        out = _normalize(response)
        if log and transcript:  # the agent's "said" + what it's about to "do"
            try:
                if out.content and out.content.strip():
                    log(f"  · says: {redact(clip(out.content, 1000))}")
                for tc in out.tool_calls:
                    log(f"  · calls {tc.name}({redact(args_summary(tc.arguments))})")
            except Exception:  # noqa: BLE001 - observability is never load-bearing
                pass
        return out

    return complete
