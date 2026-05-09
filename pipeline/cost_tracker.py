"""
╔══════════════════════════════════════════════════════════════╗
║  COST TRACKER — Immutable Per-Call Cost Accounting            ║
║                                                              ║
║  Pattern: cost-aware-llm-pipeline (ECC skill)                ║
║                                                              ║
║  Every LLM API call returns a CostRecord. Records            ║
║  accumulate in a frozen CostTracker that can never be        ║
║  silently mutated. Budget guards stop execution before       ║
║  overspending.                                               ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import threading

logger = logging.getLogger(__name__)

# ── Pricing Table (per 1M tokens, May 2026) ──────────────────

PRICING = {
    # Anthropic
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-3-5-20241022": {"input": 0.80, "output": 4.00},
    # Google
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "models/text-embedding-004": {"input": 0.00, "output": 0.00},
    # Moonshot
    "kimi-k2": {"input": 0.95, "output": 2.00},
}

# Fallback for unknown models
DEFAULT_PRICING = {"input": 3.00, "output": 15.00}


@dataclass(frozen=True, slots=True)
class CostRecord:
    """Immutable record of a single LLM API call."""

    model: str
    agent: str
    phase: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached_input_tokens: int = 0
    timestamp: str = ""

    @staticmethod
    def compute(
        model: str,
        agent: str,
        phase: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> "CostRecord":
        """Compute cost from token counts."""
        pricing = PRICING.get(model, DEFAULT_PRICING)
        billable_input = max(0, input_tokens - cached_input_tokens)
        cached_cost = cached_input_tokens * (pricing["input"] * 0.1 / 1_000_000)
        input_cost = billable_input * (pricing["input"] / 1_000_000)
        output_cost = output_tokens * (pricing["output"] / 1_000_000)
        total = input_cost + output_cost + cached_cost

        return CostRecord(
            model=model,
            agent=agent,
            phase=phase,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(total, 6),
            cached_input_tokens=cached_input_tokens,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


class BudgetExceededError(Exception):
    """Raised when cumulative cost exceeds the configured budget."""

    def __init__(self, current: float, limit: float):
        self.current = current
        self.limit = limit
        super().__init__(
            f"Budget exceeded: ${current:.4f} spent, limit is ${limit:.2f}"
        )


class CostTracker:
    """
    Thread-safe, append-only cost tracker.

    Records every LLM call and enforces budget limits.
    Produces a cost report at pipeline end.
    """

    def __init__(self, budget_limit: float = 10.00):
        self.budget_limit = budget_limit
        self._records: list[CostRecord] = []
        self._lock = threading.Lock()

    def record(self, rec: CostRecord) -> None:
        """Record a cost entry. Raises BudgetExceededError if over limit."""
        with self._lock:
            self._records.append(rec)
            total = sum(r.cost_usd for r in self._records)
        logger.info(
            f"[Cost] {rec.agent}/{rec.phase}: "
            f"{rec.input_tokens}in/{rec.output_tokens}out "
            f"= ${rec.cost_usd:.4f} "
            f"(total: ${total:.4f}/{self.budget_limit:.2f})"
        )
        if total > self.budget_limit:
            raise BudgetExceededError(total, self.budget_limit)

    @property
    def total_cost(self) -> float:
        with self._lock:
            return sum(r.cost_usd for r in self._records)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self._records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self._records)

    @property
    def total_cached_tokens(self) -> int:
        return sum(r.cached_input_tokens for r in self._records)

    @property
    def call_count(self) -> int:
        return len(self._records)

    def cost_by_phase(self) -> dict[str, float]:
        """Aggregate cost per pipeline phase."""
        by_phase: dict[str, float] = {}
        for r in self._records:
            by_phase[r.phase] = by_phase.get(r.phase, 0) + r.cost_usd
        return by_phase

    def cost_by_provider(self) -> dict[str, float]:
        """Aggregate cost per LLM provider."""
        by_provider: dict[str, float] = {}
        for r in self._records:
            provider = _model_to_provider(r.model)
            by_provider[provider] = by_provider.get(provider, 0) + r.cost_usd
        return by_provider

    def cost_by_agent(self) -> dict[str, float]:
        """Aggregate cost per agent role."""
        by_agent: dict[str, float] = {}
        for r in self._records:
            by_agent[r.agent] = by_agent.get(r.agent, 0) + r.cost_usd
        return by_agent

    def generate_report(self) -> dict:
        """Generate a full cost report as a dict."""
        return {
            "summary": {
                "total_cost_usd": round(self.total_cost, 4),
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_cached_tokens": self.total_cached_tokens,
                "total_api_calls": self.call_count,
                "budget_limit_usd": self.budget_limit,
                "budget_remaining_usd": round(
                    self.budget_limit - self.total_cost, 4
                ),
            },
            "by_phase": self.cost_by_phase(),
            "by_provider": self.cost_by_provider(),
            "by_agent": self.cost_by_agent(),
            "records": [
                {
                    "model": r.model,
                    "agent": r.agent,
                    "phase": r.phase,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cached_tokens": r.cached_input_tokens,
                    "cost_usd": r.cost_usd,
                    "timestamp": r.timestamp,
                }
                for r in self._records
            ],
        }

    def save_report(self, output_dir: Path) -> Path:
        """Save cost report to a JSON file."""
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "cost_report.json"
        report = self.generate_report()
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(f"[Cost] Report saved: {report_path}")
        return report_path

    def format_summary(self) -> str:
        """Human-readable cost summary."""
        lines = [
            f"💰 Total Cost: ${self.total_cost:.4f} / ${self.budget_limit:.2f} budget",
            f"📊 API Calls: {self.call_count}",
            f"📥 Input: {self.total_input_tokens:,} tokens",
            f"📤 Output: {self.total_output_tokens:,} tokens",
            f"💾 Cached: {self.total_cached_tokens:,} tokens",
        ]
        if self.cost_by_phase():
            lines.append("\nCost by Phase:")
            for phase, cost in sorted(
                self.cost_by_phase().items(), key=lambda x: -x[1]
            ):
                lines.append(f"  {phase}: ${cost:.4f}")
        return "\n".join(lines)


def _model_to_provider(model: str) -> str:
    """Map model name to provider."""
    if "claude" in model:
        return "anthropic"
    elif "gemini" in model or "text-embedding" in model:
        return "google"
    elif "kimi" in model:
        return "moonshot"
    return "unknown"
