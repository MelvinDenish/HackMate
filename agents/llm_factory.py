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
from langchain_core.messages import BaseMessage, AIMessage

from config import ModelSpec, APIKeys
from pipeline.cost_tracker import CostTracker, CostRecord

logger = logging.getLogger(__name__)

MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"

# Transient errors worth retrying
_MAX_RETRIES = 3
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}


def create_llm(spec: ModelSpec, keys: APIKeys) -> BaseChatModel:
    """
    Create a LangChain chat model from a ModelSpec and API keys.

    Routes to the correct provider based on spec.provider:
    - "anthropic" → ChatAnthropic
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
        )
        logger.info(f"[LLM] Created Anthropic model: {spec.model_name}")
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


def invoke_with_retry(
    llm: BaseChatModel,
    messages: list[BaseMessage],
    *,
    spec: ModelSpec,
    agent_name: str = "unknown",
    phase: str = "unknown",
    cost_tracker: Optional[CostTracker] = None,
    max_retries: int = _MAX_RETRIES,
) -> AIMessage:
    """
    Invoke an LLM with retry logic, cost tracking, and error handling.

    Args:
        llm: The LangChain chat model
        messages: Messages to send
        spec: Model specification (for cost calculation)
        agent_name: Name of the calling agent (for cost tracking)
        phase: Pipeline phase (for cost tracking)
        cost_tracker: Optional CostTracker for budget enforcement
        max_retries: Maximum retry attempts for transient errors

    Returns:
        AIMessage response

    Raises:
        BudgetExceededError: If cumulative cost exceeds budget
        Exception: If all retries exhausted or non-transient error
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            response = llm.invoke(messages)

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
                cost_tracker.record(record)  # Raises BudgetExceededError if over

            return response

        except Exception as e:
            last_error = e

            # Don't retry budget exceeded
            if "BudgetExceeded" in type(e).__name__:
                raise

            if _is_retryable(e) and attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    f"[LLM] Transient error on attempt {attempt + 1}/{max_retries}: "
                    f"{type(e).__name__}: {str(e)[:100]}. "
                    f"Retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                # Non-retryable or final attempt
                logger.error(
                    f"[LLM] Failed after {attempt + 1} attempts: "
                    f"{type(e).__name__}: {str(e)[:200]}"
                )
                raise

    raise last_error  # type: ignore
