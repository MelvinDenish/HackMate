"""
╔══════════════════════════════════════════════════════════════╗
║  DESIGN SYSTEMS — Pre-Built CSS Templates for UI Quality     ║
║                                                              ║
║  Inspired by: Lovable.dev, v0 (Vercel)                       ║
║                                                              ║
║  These design systems are injected into the first coding     ║
║  task so all components use consistent, professional tokens. ║
║                                                              ║
║  Usage:                                                      ║
║  - Architect PRD picks a design_system name                  ║
║  - Planner includes design CSS in task_001                   ║
║  - Coder references var(--primary) etc                       ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DESIGN_SYSTEMS_DIR = Path(__file__).parent


def get_design_system(name: str = "modern_dark") -> str:
    """Load a pre-built design system CSS template.

    Args:
        name: Design system name (modern_dark, ocean_light, purple_glass, minimal_clean)

    Returns:
        Complete CSS content for the design system
    """
    css_file = DESIGN_SYSTEMS_DIR / f"{name}.css"
    if not css_file.exists():
        # Fallback to modern_dark
        css_file = DESIGN_SYSTEMS_DIR / "modern_dark.css"
        if not css_file.exists():
            logger.warning(f"[DesignSystem] No design system found: {name}")
            return ""

    content = css_file.read_text(encoding="utf-8")
    logger.info(f"[DesignSystem] Loaded '{name}' ({len(content)} chars)")
    return content


def list_design_systems() -> list[str]:
    """List available design system names."""
    return [
        f.stem for f in DESIGN_SYSTEMS_DIR.glob("*.css")
    ]
