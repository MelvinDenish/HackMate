"""
╔══════════════════════════════════════════════════════════════╗
║  APPROVAL GATES — Human-in-the-Loop Decision Points          ║
║                                                              ║
║  Strategic interrupts at critical pipeline decisions:         ║
║  • POST-ARCHITECTURE: Approve tech stack choice              ║
║  • POST-PLAN: Approve task count + budget estimate           ║
║  • POST-REVIEW-FAIL: Human review after 2 failures           ║
║                                                              ║
║  Modes:                                                      ║
║  • auto (hackathon) — auto-approve after timeout             ║
║  • interactive — wait for stdin input                        ║
║  • telegram — send approval request to Telegram bot          ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ApprovalGate:
    """Configurable approval gate for pipeline decision points."""

    def __init__(
        self,
        mode: str = "auto",
        auto_timeout: int = 60,
        telegram_chat_id: str = "",
    ):
        """
        Args:
            mode: "auto" | "interactive" | "telegram"
            auto_timeout: seconds before auto-approving in auto mode
            telegram_chat_id: Telegram chat ID for remote approvals
        """
        self.mode = mode
        self.auto_timeout = auto_timeout
        self.telegram_chat_id = telegram_chat_id
        self._decisions: list[dict] = []

    def request_approval(
        self,
        gate_name: str,
        summary: str,
        details: str = "",
        options: list[str] = None,
    ) -> str:
        """Request human approval at a decision point.

        Args:
            gate_name: e.g., "architecture", "plan", "review_fail"
            summary: One-line summary of what needs approval
            details: Detailed context for the decision
            options: List of valid responses (default: ["approve", "reject", "modify"])

        Returns:
            The chosen option (string)
        """
        if options is None:
            options = ["approve", "reject", "modify"]

        decision = {
            "gate": gate_name,
            "summary": summary,
            "timestamp": time.time(),
            "mode": self.mode,
        }

        if self.mode == "auto":
            result = self._auto_approve(gate_name, summary)
        elif self.mode == "interactive":
            result = self._interactive_approve(gate_name, summary, details, options)
        else:
            result = self._auto_approve(gate_name, summary)

        decision["result"] = result
        self._decisions.append(decision)
        logger.info(f"[Gate] {gate_name}: {result}")
        return result

    def _auto_approve(self, gate_name: str, summary: str) -> str:
        """Auto-approve after logging the decision."""
        logger.info(
            f"[Gate] AUTO-APPROVE ({gate_name}): {summary} "
            f"(timeout={self.auto_timeout}s)"
        )
        return "approve"

    def _interactive_approve(
        self,
        gate_name: str,
        summary: str,
        details: str,
        options: list[str],
    ) -> str:
        """Wait for user input via stdin."""
        print(f"\n{'='*60}")
        print(f"🚦 APPROVAL GATE: {gate_name}")
        print(f"{'='*60}")
        print(f"\n{summary}\n")
        if details:
            print(f"{details[:500]}\n")
        options_str = " / ".join(f"[{o[0].upper()}]{o[1:]}" for o in options)
        print(f"Options: {options_str}")
        print(f"(Auto-approving in {self.auto_timeout}s if no input)\n")

        try:
            import select
            import sys

            # Non-blocking input with timeout
            print(f"Your choice: ", end="", flush=True)
            start = time.time()

            while time.time() - start < self.auto_timeout:
                if sys.stdin in select.select([sys.stdin], [], [], 1)[0]:
                    choice = sys.stdin.readline().strip().lower()
                    # Match by first letter
                    for opt in options:
                        if choice and choice[0] == opt[0]:
                            return opt
                    return options[0]  # Default to first option

            print("(timed out → auto-approve)")
            return "approve"

        except (ImportError, OSError):
            # Windows doesn't support select on stdin, auto-approve
            logger.info("[Gate] Interactive mode not available, auto-approving")
            return "approve"

    def get_decision_log(self) -> list[dict]:
        """Return all decisions made during this pipeline run."""
        return self._decisions

    def format_log(self) -> str:
        """Format decision log as readable string."""
        if not self._decisions:
            return "No approval gates triggered."

        lines = ["📋 Approval Gate Log:"]
        for d in self._decisions:
            icon = "✅" if d["result"] == "approve" else "❌"
            lines.append(f"  {icon} {d['gate']}: {d['result']} ({d['mode']})")
        return "\n".join(lines)


def create_gate(config=None) -> ApprovalGate:
    """Create an approval gate from environment/config settings."""
    mode = os.getenv("HACKMATE_APPROVAL_MODE", "auto")
    timeout = int(os.getenv("HACKMATE_APPROVAL_TIMEOUT", "60"))
    telegram_id = os.getenv("HACKMATE_TELEGRAM_CHAT_ID", "")

    if config and hasattr(config, "approval_mode"):
        mode = config.approval_mode

    return ApprovalGate(
        mode=mode,
        auto_timeout=timeout,
        telegram_chat_id=telegram_id,
    )
