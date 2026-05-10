"""
╔══════════════════════════════════════════════════════════════╗
║  A/B TESTING — Prompt Evolution Framework                    ║
║                                                              ║
║  Epsilon-greedy prompt selection:                            ║
║  • 90% of the time: use the best-performing prompt variant   ║
║  • 10% of the time: explore a random variant                 ║
║                                                              ║
║  Tracks win rates per variant via Learning DB.               ║
║  System gets better with every pipeline run.                 ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PromptVariant:
    """A single prompt variant for A/B testing."""
    id: str
    agent: str
    prompt: str
    trials: int = 0
    wins: int = 0

    @property
    def win_rate(self) -> float:
        if self.trials == 0:
            return 0.5  # Optimistic prior for untested variants
        return self.wins / self.trials

    def record_result(self, success: bool):
        self.trials += 1
        if success:
            self.wins += 1


class ABRegistry:
    """Registry for managing prompt A/B tests across agents."""

    def __init__(self, storage_path: Optional[Path] = None):
        self._variants: dict[str, list[PromptVariant]] = {}
        self._active: dict[str, str] = {}  # agent → currently selected variant id
        self._storage = storage_path

        if storage_path and storage_path.exists():
            self._load()

    def register_variant(self, agent: str, variant_id: str, prompt: str):
        """Register a new prompt variant for an agent."""
        if agent not in self._variants:
            self._variants[agent] = []

        # Don't register duplicates
        if any(v.id == variant_id for v in self._variants[agent]):
            return

        self._variants[agent].append(
            PromptVariant(id=variant_id, agent=agent, prompt=prompt)
        )
        logger.debug(f"[A/B] Registered variant {variant_id} for {agent}")

    def select_variant(self, agent: str, epsilon: float = 0.1) -> PromptVariant:
        """Select a prompt variant using epsilon-greedy strategy.

        Args:
            agent: Agent name
            epsilon: Exploration rate (0.1 = 10% random)

        Returns:
            Selected PromptVariant
        """
        variants = self._variants.get(agent, [])
        if not variants:
            return None

        if len(variants) == 1:
            self._active[agent] = variants[0].id
            return variants[0]

        # Epsilon-greedy selection
        if random.random() < epsilon:
            # Explore: random variant
            selected = random.choice(variants)
            logger.info(f"[A/B] EXPLORE: {agent} → {selected.id} (random)")
        else:
            # Exploit: best win rate
            selected = max(variants, key=lambda v: v.win_rate)
            logger.info(
                f"[A/B] EXPLOIT: {agent} → {selected.id} "
                f"(win_rate={selected.win_rate:.1%}, {selected.trials} trials)"
            )

        self._active[agent] = selected.id
        return selected

    def record_result(self, agent: str, success: bool):
        """Record the result of the currently active variant."""
        variant_id = self._active.get(agent)
        if not variant_id:
            return

        variants = self._variants.get(agent, [])
        for v in variants:
            if v.id == variant_id:
                v.record_result(success)
                logger.info(
                    f"[A/B] Result: {agent}/{variant_id} → "
                    f"{'WIN' if success else 'LOSS'} "
                    f"(rate={v.win_rate:.1%}, n={v.trials})"
                )
                break

        self._save()

    def get_stats(self) -> str:
        """Get formatted A/B test statistics."""
        lines = ["📊 A/B Test Statistics:"]
        for agent, variants in sorted(self._variants.items()):
            lines.append(f"\n  {agent}:")
            for v in sorted(variants, key=lambda x: x.win_rate, reverse=True):
                active = " ◀ active" if self._active.get(agent) == v.id else ""
                lines.append(
                    f"    {v.id}: {v.win_rate:.0%} "
                    f"({v.wins}/{v.trials}){active}"
                )
        return "\n".join(lines)

    def _save(self):
        """Persist variant stats to disk."""
        if not self._storage:
            return
        try:
            data = {}
            for agent, variants in self._variants.items():
                data[agent] = [
                    {"id": v.id, "trials": v.trials, "wins": v.wins}
                    for v in variants
                ]
            self._storage.parent.mkdir(parents=True, exist_ok=True)
            self._storage.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[A/B] Save failed: {e}")

    def _load(self):
        """Load variant stats from disk."""
        try:
            data = json.loads(self._storage.read_text(encoding="utf-8"))
            for agent, variants in data.items():
                for v in variants:
                    if agent not in self._variants:
                        self._variants[agent] = []
                    existing = next(
                        (x for x in self._variants[agent] if x.id == v["id"]), None
                    )
                    if existing:
                        existing.trials = v["trials"]
                        existing.wins = v["wins"]
                    else:
                        self._variants[agent].append(
                            PromptVariant(
                                id=v["id"], agent=agent, prompt="",
                                trials=v["trials"], wins=v["wins"],
                            )
                        )
            logger.info(f"[A/B] Loaded stats for {len(self._variants)} agents")
        except Exception as e:
            logger.debug(f"[A/B] Load failed: {e}")


# ── Singleton Registry ───────────────────────────────────────

_registry: Optional[ABRegistry] = None


def get_ab_registry(storage_dir: Optional[Path] = None) -> ABRegistry:
    """Get or create the global A/B testing registry."""
    global _registry
    if _registry is None:
        storage_path = (storage_dir / "ab_stats.json") if storage_dir else None
        _registry = ABRegistry(storage_path=storage_path)
    return _registry
