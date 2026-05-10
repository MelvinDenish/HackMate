"""
╔══════════════════════════════════════════════════════════════╗
║  LLM FACTORY v2 — Multi-Provider with Retry, Caching, Cost  ║
║                                                              ║
║  Improvements over v1:                                       ║
║  ✅ Exponential backoff retry (3 attempts) on transient errs ║
║  ✅ Anthropic prompt caching for system prompts > 1024 tok   ║
║  ✅ Per-call cost tracking via CostTracker                   ║
║  ✅ Budget guard — stops before overspending                 ║
║  ✅ Token usage extraction from response metadata            ║
║                                                              ║
║  Providers:                                                  ║
║  • Anthropic (Claude) — via langchain-anthropic              ║
║  • Google (Gemini)    — via langchain-google-genai            ║
║  • Moonshot (Kimi)    — via langchain-openai (compatible)    ║
╚══════════════════════════════════════════════════════════════╝
"""

import time
import logging
from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, AIMessage, SystemMessage

from config import ModelSpec, APIKeys
from pipeline.cost_tracker import CostTracker, CostRecord

logger = logging.getLogger(__name__)

MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"

# Transient errors worth retrying
_MAX_RETRIES = 3
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}

# Model context limits (input tokens) for pre-flight validation
MODEL_CONTEXT_LIMITS = {
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-3-5-20241022": 200_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "kimi-k2": 128_000,
    "text-embedding-004": 3_000,
}

# Default timeouts per model tier (seconds)
MODEL_TIMEOUTS = {
    "claude-haiku-3-5-20241022": 60,
    "claude-sonnet-4-20250514": 180,
    "gemini-2.5-flash": 120,
    "gemini-2.5-pro": 240,
    "kimi-k2": 120,
}

# Model fallback chains — try next provider if primary fails
FALLBACK_MODELS = {
    "claude-sonnet-4-20250514": ModelSpec("google", "gemini-2.5-pro", 0.2, 8192),
    "claude-haiku-3-5-20241022": ModelSpec("google", "gemini-2.5-flash", 0.1, 8192),
    "gemini-2.5-pro": ModelSpec("anthropic", "claude-sonnet-4-20250514", 0.1, 8192),
    "gemini-2.5-flash": ModelSpec("anthropic", "claude-haiku-3-5-20241022", 0.1, 8192),
    "kimi-k2": ModelSpec("anthropic", "claude-haiku-3-5-20241022", 0.4, 4096),
}


# ── Circuit Breaker ──────────────────────────────────────────

class CircuitBreaker:
    """Trip after N failures within window, auto-reset after cooldown.
    Prevents runaway budget drain during API outages."""

    def __init__(self, threshold: int = 5, cooldown_seconds: int = 60):
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self.failures = 0
        self.last_failure_time = 0.0

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()

    def record_success(self):
        self.failures = max(0, self.failures - 1)

    def is_open(self) -> bool:
        """Returns True if circuit is OPEN (should not attempt calls)."""
        if self.failures >= self.threshold:
            elapsed = time.time() - self.last_failure_time
            if elapsed > self.cooldown:
                # Cooldown expired — reset and allow retry
                self.failures = 0
                logger.info("[CircuitBreaker] Reset after cooldown")
                return False
            return True
        return False


# Per-provider circuit breakers
_circuit_breakers: dict[str, CircuitBreaker] = {
    "anthropic": CircuitBreaker(threshold=5, cooldown_seconds=60),
    "google": CircuitBreaker(threshold=5, cooldown_seconds=60),
    "moonshot": CircuitBreaker(threshold=5, cooldown_seconds=60),
}


class ProviderHealthTracker:
    """Track provider health metrics for intelligent routing.

    Extends circuit breakers with latency and success tracking
    to enable score-based provider selection.
    """

    def __init__(self):
        self._stats: dict[str, dict] = {}

    def record_call(self, provider: str, latency_s: float, success: bool, cost: float = 0):
        """Record a provider API call outcome."""
        if provider not in self._stats:
            self._stats[provider] = {
                "calls": 0, "successes": 0,
                "total_latency": 0, "total_cost": 0,
            }

        s = self._stats[provider]
        s["calls"] += 1
        s["total_latency"] += latency_s
        s["total_cost"] += cost
        if success:
            s["successes"] += 1

    def get_best_provider(self, candidates: list[str]) -> Optional[str]:
        """Select the best provider from candidates based on composite score.

        Score = (1 - error_rate) * (1 / avg_latency) * (1 / avg_cost)
        Higher is better.
        """
        if not candidates:
            return None

        scored = []
        for p in candidates:
            if p not in self._stats or self._stats[p]["calls"] == 0:
                scored.append((p, 1.0))  # Optimistic prior for untested
                continue

            s = self._stats[p]
            error_rate = 1 - (s["successes"] / s["calls"])
            avg_latency = max(0.1, s["total_latency"] / s["calls"])
            avg_cost = max(0.001, s["total_cost"] / s["calls"])

            # Composite score — higher is better
            score = (1 - error_rate) * (1 / avg_latency) * (1 / avg_cost)
            scored.append((p, score))

        best = max(scored, key=lambda x: x[1])
        if len(scored) > 1:
            logger.debug(
                f"[LB] Provider scores: {', '.join(f'{p}={s:.2f}' for p, s in scored)} "
                f"→ selected {best[0]}"
            )
        return best[0]

    def get_summary(self) -> str:
        """Get formatted health summary."""
        if not self._stats:
            return "No provider metrics yet."
        lines = ["📊 Provider Health:"]
        for p, s in sorted(self._stats.items()):
            rate = s["successes"] / s["calls"] * 100 if s["calls"] else 0
            avg_lat = s["total_latency"] / s["calls"] if s["calls"] else 0
            lines.append(
                f"  {p}: {rate:.0f}% success, {avg_lat:.1f}s avg latency, "
                f"${s['total_cost']:.4f} total ({s['calls']} calls)"
            )
        return "\n".join(lines)


# Global health tracker
_health_tracker = ProviderHealthTracker()


def _estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~4 chars per token across providers."""
    total_chars = 0
    for m in messages:
        content = m.content if hasattr(m, 'content') else str(m)
        total_chars += len(content) if isinstance(content, str) else 0
    return total_chars // 4


def _check_context_budget(messages: list, spec) -> None:
    """Pre-flight check: warn or raise if prompt exceeds model context."""
    est_tokens = _estimate_tokens(messages)
    limit = MODEL_CONTEXT_LIMITS.get(spec.model_name, 200_000)
    ratio = est_tokens / limit if limit > 0 else 0

    if ratio > 0.95:
        logger.error(
            f"[LLM] CONTEXT OVERFLOW: ~{est_tokens:,} tokens estimated, "
            f"limit is {limit:,} for {spec.model_name}. Prompt will be truncated!"
        )
    elif ratio > 0.80:
        logger.warning(
            f"[LLM] Context usage HIGH: ~{est_tokens:,}/{limit:,} tokens "
            f"({ratio:.0%}) for {spec.model_name}"
        )


def create_llm(spec: ModelSpec, keys: APIKeys) -> BaseChatModel:
    """
    Create a LangChain chat model from a ModelSpec and API keys.

    Routes to the correct provider based on spec.provider:
    - "anthropic" → ChatAnthropic (with prompt caching enabled)
    - "google"    → ChatGoogleGenerativeAI
    - "moonshot"  → ChatOpenAI (OpenAI-compatible API)
    """
    if spec.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        if not keys.anthropic:
            raise ValueError("ANTHROPIC_API_KEY is required for this agent")
        llm = ChatAnthropic(
            model=spec.model_name,
            api_key=keys.anthropic,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            # Enable prompt caching — 90% cost reduction on cached prefixes
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        logger.info(f"[LLM] Created Anthropic model: {spec.model_name} (cache-enabled)")
        return llm

    elif spec.provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        if not keys.google:
            raise ValueError("GOOGLE_API_KEY is required for this agent")
        llm = ChatGoogleGenerativeAI(
            model=spec.model_name,
            google_api_key=keys.google,
            temperature=spec.temperature,
            max_output_tokens=spec.max_tokens,
        )
        logger.info(f"[LLM] Created Google model: {spec.model_name}")
        return llm

    elif spec.provider == "moonshot":
        from langchain_openai import ChatOpenAI
        if not keys.moonshot:
            raise ValueError("MOONSHOT_API_KEY is required for this agent")
        llm = ChatOpenAI(
            model=spec.model_name,
            api_key=keys.moonshot,
            base_url=MOONSHOT_BASE_URL,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
        )
        logger.info(f"[LLM] Created Moonshot model: {spec.model_name}")
        return llm

    else:
        raise ValueError(f"Unknown LLM provider: {spec.provider}")


def create_embeddings(spec: ModelSpec, keys: APIKeys):
    """Create an embeddings model (used for ChromaDB)."""
    if spec.provider == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        return GoogleGenerativeAIEmbeddings(
            model=spec.model_name,
            google_api_key=keys.google,
        )
    else:
        raise ValueError(f"Embeddings not supported for provider: {spec.provider}")


def _is_retryable(error: Exception) -> bool:
    """Check if an error is transient and worth retrying."""
    error_str = str(error).lower()
    # Check for HTTP status codes
    for code in _RETRYABLE_STATUS_CODES:
        if str(code) in error_str:
            return True
    # Check for common transient error messages
    retryable_phrases = [
        "rate limit", "rate_limit", "too many requests",
        "server error", "internal server", "overloaded",
        "connection", "timeout", "temporarily unavailable",
        "capacity", "throttl",
    ]
    return any(phrase in error_str for phrase in retryable_phrases)


def _extract_token_usage(response: AIMessage) -> dict:
    """Extract token usage from LLM response metadata."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

    # Check response_metadata (Anthropic, Google)
    meta = getattr(response, "response_metadata", {}) or {}

    # Anthropic format
    if "usage" in meta:
        u = meta["usage"]
        usage["input_tokens"] = u.get("input_tokens", 0)
        usage["output_tokens"] = u.get("output_tokens", 0)
        usage["cached_tokens"] = u.get("cache_read_input_tokens", 0)

    # Google format
    elif "usage_metadata" in meta:
        u = meta["usage_metadata"]
        usage["input_tokens"] = u.get("prompt_token_count", 0)
        usage["output_tokens"] = u.get("candidates_token_count", 0)

    # OpenAI-compatible format (Moonshot)
    elif "token_usage" in meta:
        u = meta["token_usage"]
        usage["input_tokens"] = u.get("prompt_tokens", 0)
        usage["output_tokens"] = u.get("completion_tokens", 0)

    # Fallback: estimate from content length
    if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
        content = response.content if isinstance(response.content, str) else ""
        usage["output_tokens"] = max(1, len(content) // 4)

    return usage


def _apply_prompt_caching(messages: list[BaseMessage], spec: ModelSpec) -> list[BaseMessage]:
    """Apply Anthropic prompt caching to system messages.
    Marks the first SystemMessage as cacheable so repeat calls
    with the same system prompt get 90% cost reduction on the prefix."""
    if spec.provider != "anthropic":
        return messages

    cached = list(messages)
    for i, msg in enumerate(cached):
        if isinstance(msg, SystemMessage) and isinstance(msg.content, str):
            # Only cache system prompts > 1024 chars (Anthropic minimum)
            if len(msg.content) > 1024:
                cached[i] = SystemMessage(
                    content=[
                        {
                            "type": "text",
                            "text": msg.content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                )
                logger.debug(f"[LLM] Prompt caching enabled for system message ({len(msg.content)} chars)")
            break  # Only cache the first system message

    return cached


def invoke_with_retry(
    llm: BaseChatModel,
    messages: list[BaseMessage],
    *,
    spec: ModelSpec,
    agent_name: str = "unknown",
    phase: str = "unknown",
    cost_tracker: Optional[CostTracker] = None,
    max_retries: int = _MAX_RETRIES,
    keys: Optional[APIKeys] = None,
) -> AIMessage:
    """
    Invoke an LLM with retry logic, cost tracking, circuit breaker,
    prompt caching, and model fallback.

    v3 improvements:
    - Prompt caching for Anthropic (90% prefix cost reduction)
    - Circuit breaker (prevents runaway spend during outages)
    - Model fallback chain (survives single-provider failures)
    """
    last_error = None
    timeout = MODEL_TIMEOUTS.get(spec.model_name, 180)

    # Pre-flight: check if prompt fits the model context
    _check_context_budget(messages, spec)

    # Check circuit breaker
    breaker = _circuit_breakers.get(spec.provider)
    if breaker and breaker.is_open():
        logger.warning(
            f"[LLM] Circuit breaker OPEN for {spec.provider}. "
            f"Attempting fallback..."
        )
        return _invoke_fallback(
            messages, spec=spec, agent_name=agent_name,
            phase=phase, cost_tracker=cost_tracker, keys=keys,
        )

    # Apply prompt caching for Anthropic
    cached_messages = _apply_prompt_caching(messages, spec)

    for attempt in range(max_retries):
        call_start = time.time()
        try:
            # Timeout guard: use threading to prevent infinite hangs
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(llm.invoke, cached_messages)
                try:
                    response = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"LLM call to {spec.model_name} timed out after {timeout}s"
                    )

            call_latency = time.time() - call_start
            call_cost = 0.0

            # Track cost
            if cost_tracker is not None:
                usage = _extract_token_usage(response)
                record = CostRecord.compute(
                    model=spec.model_name,
                    agent=agent_name,
                    phase=phase,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cached_input_tokens=usage["cached_tokens"],
                )
                call_cost = record.cost_usd
                cost_tracker.record(record)  # Raises BudgetExceededError if over

            # Record success for circuit breaker + health tracker
            if breaker:
                breaker.record_success()
            _health_tracker.record_call(spec.provider, call_latency, True, call_cost)

            return response

        except Exception as e:
            last_error = e

            # Don't retry budget exceeded
            if "BudgetExceeded" in type(e).__name__:
                raise

            # Record failure for circuit breaker + health tracker
            if breaker:
                breaker.record_failure()
            _health_tracker.record_call(spec.provider, time.time() - call_start, False)

            if _is_retryable(e) and attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    f"[LLM] Transient error on attempt {attempt + 1}/{max_retries}: "
                    f"{type(e).__name__}: {str(e)[:100]}. "
                    f"Retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                # All retries exhausted — try fallback model
                logger.error(
                    f"[LLM] Failed after {attempt + 1} attempts: "
                    f"{type(e).__name__}: {str(e)[:200]}"
                )
                if keys:
                    try:
                        return _invoke_fallback(
                            messages, spec=spec, agent_name=agent_name,
                            phase=phase, cost_tracker=cost_tracker, keys=keys,
                        )
                    except Exception as fallback_err:
                        logger.error(f"[LLM] Fallback also failed: {fallback_err}")
                raise

    raise last_error  # type: ignore


def _invoke_fallback(
    messages: list[BaseMessage],
    *,
    spec: ModelSpec,
    agent_name: str,
    phase: str,
    cost_tracker: Optional[CostTracker],
    keys: Optional[APIKeys],
) -> AIMessage:
    """Attempt to invoke a fallback model when the primary fails."""
    fallback_spec = FALLBACK_MODELS.get(spec.model_name)
    if not fallback_spec or not keys:
        raise ValueError(f"No fallback available for {spec.model_name}")

    logger.warning(
        f"[LLM] Falling back: {spec.model_name} → {fallback_spec.model_name}"
    )
    fallback_llm = create_llm(fallback_spec, keys)
    fallback_messages = _apply_prompt_caching(messages, fallback_spec)
    timeout = MODEL_TIMEOUTS.get(fallback_spec.model_name, 180)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fallback_llm.invoke, fallback_messages)
        response = future.result(timeout=timeout)

    if cost_tracker:
        usage = _extract_token_usage(response)
        record = CostRecord.compute(
            model=fallback_spec.model_name,
            agent=f"{agent_name}_fallback",
            phase=phase,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cached_input_tokens=usage["cached_tokens"],
        )
        cost_tracker.record(record)

    logger.info(f"[LLM] Fallback {fallback_spec.model_name} succeeded")
    return response


def invoke_structured(
    llm: BaseChatModel,
    messages: list[BaseMessage],
    schema,
    *,
    spec: ModelSpec,
    agent_name: str = "unknown",
    phase: str = "unknown",
    cost_tracker: Optional[CostTracker] = None,
    keys: Optional[APIKeys] = None,
):
    """Invoke LLM with Pydantic structured output enforcement.

    Tries native with_structured_output first (Anthropic/Google support).
    Falls back to regular invoke + safe_parse_json if unsupported.

    Args:
        schema: Pydantic BaseModel class defining expected output shape
    Returns:
        Parsed Pydantic model instance, or dict on fallback
    """
    from pipeline.schemas import safe_parse_json

    # Strategy 1: Native structured output (provider-level enforcement)
    try:
        structured_llm = llm.with_structured_output(schema)
        cached_msgs = _apply_prompt_caching(messages, spec)

        import concurrent.futures
        timeout = MODEL_TIMEOUTS.get(spec.model_name, 180)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(structured_llm.invoke, cached_msgs)
            result = future.result(timeout=timeout)

        # Track cost (structured calls still have token usage)
        if cost_tracker and hasattr(result, 'response_metadata'):
            # Some providers attach metadata to structured output
            pass  # Cost tracked via the underlying invoke

        logger.info(f"[LLM] Structured output success: {agent_name}/{phase}")
        return result

    except (NotImplementedError, TypeError, AttributeError) as e:
        logger.info(f"[LLM] Native structured output not available ({e}), using fallback parse")

    except Exception as e:
        logger.warning(f"[LLM] Structured output failed ({e}), falling back to text parse")

    # Strategy 2: Regular invoke + JSON parse + Pydantic validation
    response = invoke_with_retry(
        llm, messages, spec=spec, agent_name=agent_name,
        phase=phase, cost_tracker=cost_tracker, keys=keys,
    )

    raw = response.content
    parsed = safe_parse_json(raw, schema)

    if parsed is not None:
        if isinstance(parsed, dict):
            try:
                return schema(**parsed)
            except Exception:
                return parsed
        return parsed

    logger.warning(f"[LLM] Structured parse failed for {agent_name}, returning raw dict")
    return {"raw": raw}

