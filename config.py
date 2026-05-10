"""
╔══════════════════════════════════════════════════════════════╗
║  CONFIGURATION — Multi-Provider Model Routing               ║
║                                                              ║
║  Architecture:                                               ║
║  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐    ║
║  │  Anthropic   │  │   Google AI   │  │  Moonshot (Kimi)  │   ║
║  │ Claude Sonnet│  │Gemini 2.5    │  │  Kimi k2          │   ║
║  │ Claude Haiku │  │Flash / Pro   │  │  Multimodal       │   ║
║  └──────┬──────┘  └──────┬───────┘  └────────┬─────────┘    ║
║         └────────────────┼───────────────────┘               ║
║                    ┌─────┴─────┐                             ║
║                    │  Config   │                             ║
║                    │  Router   │                             ║
║                    └───────────┘                             ║
╚══════════════════════════════════════════════════════════════╝

Each agent in the pipeline is routed to its optimal LLM provider
based on the task requirements. This file centralizes all model
configuration and API key management.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


# ── Model Definitions ────────────────────────────────────────

@dataclass(frozen=True)
class ModelSpec:
    """Specification for a single LLM model."""
    provider: str       # "anthropic" | "google" | "moonshot"
    model_name: str     # e.g. "claude-sonnet-4-20250514"
    temperature: float = 0.3
    max_tokens: int = 4096


# ── Agent-to-Model Mapping ───────────────────────────────────
# This is the core routing table. Each agent role maps to the
# optimal model based on the task characteristics described in
# the research paper.

AGENT_MODELS: dict[str, ModelSpec] = {
    # ── Orchestrator ──
    # Coordinates all phases, needs strong reasoning
    "orchestrator": ModelSpec(
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        temperature=0.2,
        max_tokens=4096,
    ),

    # ── Phase 0: Clarification ──
    # Must deeply understand user intent and ask targeted questions
    "clarification": ModelSpec(
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        temperature=0.4,
        max_tokens=2048,
    ),

    # ── Phase 1: Research ──
    # Fast, cheap, excellent at synthesizing web search results
    "research": ModelSpec(
        provider="google",
        model_name="gemini-2.5-flash",
        temperature=0.3,
        max_tokens=8192,
    ),

    # ── Phase 1: Embeddings for Knowledge Base ──
    "embeddings": ModelSpec(
        provider="google",
        model_name="text-embedding-004",
        temperature=0.0,
        max_tokens=0,  # N/A for embeddings
    ),

    # ── Phase 2: Architecture ──
    # Best at structured system design and PRD generation
    "architect": ModelSpec(
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        temperature=0.3,
        max_tokens=8192,
    ),

    # ── Phase 2: Planning ──
    # Precise task decomposition with dependency tracking
    "planner": ModelSpec(
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        temperature=0.2,
        max_tokens=8192,
    ),

    # ── Phase 3: Coding ──
    # Top-tier code generation — Claude Sonnet 4 is the best coder
    "coder": ModelSpec(
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        temperature=0.2,
        max_tokens=16384,
    ),

    # ── Phase 3: Code Review ──
    # Massive context window for reviewing entire codebases
    "reviewer": ModelSpec(
        provider="google",
        model_name="gemini-2.5-pro",
        temperature=0.1,
        max_tokens=8192,
    ),

    # ── Phase 3: Deployment ──
    # Simple deployment tasks — fast and cheap model suffices
    "deployer": ModelSpec(
        provider="anthropic",
        model_name="claude-haiku-3-5-20241022",
        temperature=0.1,
        max_tokens=4096,
    ),

    # ── Phase 4: Pitch Writing ──
    # Best at persuasive, structured narrative writing
    "pitch": ModelSpec(
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        temperature=0.5,
        max_tokens=8192,
    ),

    # ── Phase 4: Presentation Design ──
    # Multimodal understanding for visual content descriptions
    "presentation": ModelSpec(
        provider="moonshot",
        model_name="kimi-k2",
        temperature=0.4,
        max_tokens=4096,
    ),

    # ── Phase 3.5: De-Sloppify Cleanup ──
    # Fast + cheap model for code cleanup pass (ECC De-Sloppify pattern)
    "deslopify": ModelSpec(
        provider="anthropic",
        model_name="claude-haiku-3-5-20241022",
        temperature=0.1,
        max_tokens=16384,
    ),

    # ── Phase 3.5: Security Review ──
    # Deep reasoning for OWASP-based security audit
    "security": ModelSpec(
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        temperature=0.1,
        max_tokens=4096,
    ),
}


# ── API Keys ─────────────────────────────────────────────────

@dataclass
class APIKeys:
    """All API keys loaded from environment."""
    anthropic: str = ""
    google: str = ""
    moonshot: str = ""
    gamma: str = ""
    railway: str = ""
    exa: str = ""
    firecrawl: str = ""

    @classmethod
    def from_env(cls) -> "APIKeys":
        return cls(
            anthropic=os.getenv("ANTHROPIC_API_KEY", ""),
            google=os.getenv("GOOGLE_API_KEY", ""),
            moonshot=os.getenv("MOONSHOT_API_KEY", ""),
            gamma=os.getenv("GAMMA_API_KEY", ""),
            railway=os.getenv("RAILWAY_API_TOKEN", ""),
            exa=os.getenv("EXA_API_KEY", ""),
            firecrawl=os.getenv("FIRECRAWL_API_KEY", ""),
        )

    def validate(self) -> list[str]:
        """Return list of missing but required keys."""
        missing = []
        if not self.anthropic:
            missing.append("ANTHROPIC_API_KEY")
        if not self.google:
            missing.append("GOOGLE_API_KEY")
        if not self.moonshot:
            missing.append("MOONSHOT_API_KEY")
        if not self.gamma:
            missing.append("GAMMA_API_KEY")
        if not self.railway:
            missing.append("RAILWAY_API_TOKEN")
        return missing


# ── Sandbox Configuration ────────────────────────────────────

@dataclass
class SandboxConfig:
    """Docker sandbox settings."""
    image: str = "python:3.12-slim"
    timeout: int = 120  # seconds
    memory_limit: str = "512m"
    cpu_count: int = 1
    network_disabled: bool = False  # Need network for pip install

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        return cls(
            image=os.getenv("DOCKER_SANDBOX_IMAGE", "python:3.12-slim"),
            timeout=int(os.getenv("DOCKER_SANDBOX_TIMEOUT", "120")),
        )


# ── Workspace Configuration ──────────────────────────────────

@dataclass
class WorkspaceConfig:
    """Shared Intelligence Workspace directories."""
    root: Path = field(default_factory=lambda: Path(
        os.getenv("WORKSPACE_ROOT", "./workspace")
    ))

    @property
    def briefs_dir(self) -> Path:
        """Phase 1 output: research dossiers."""
        return self.root / "briefs"

    @property
    def specs_dir(self) -> Path:
        """Phase 2 output: PRD and task specs."""
        return self.root / "specs"

    @property
    def tasks_dir(self) -> Path:
        """Phase 2 output: decomposed task queue."""
        return self.root / "tasks"

    @property
    def src_dir(self) -> Path:
        """Phase 3 output: generated source code."""
        return self.root / "src"

    @property
    def output_dir(self) -> Path:
        """Phase 4 output: pitch deck and final deliverables."""
        return self.root / "output"

    def ensure_dirs(self) -> None:
        """Create all workspace directories if they don't exist."""
        for d in [self.briefs_dir, self.specs_dir, self.tasks_dir,
                  self.src_dir, self.output_dir]:
            d.mkdir(parents=True, exist_ok=True)


# ── Master Configuration ─────────────────────────────────────

@dataclass
class PipelineConfig:
    """Top-level configuration for the entire pipeline."""
    keys: APIKeys = field(default_factory=APIKeys.from_env)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig.from_env)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    models: dict[str, ModelSpec] = field(default_factory=lambda: AGENT_MODELS)
    max_code_review_retries: int = 3
    budget_limit: float = float(os.getenv("PIPELINE_BUDGET_LIMIT", "10.00"))

    # Phase-level budget allocation (prevents coding from being starved)
    phase_budgets: dict[str, float] = field(default_factory=lambda: {
        "research": 0.50,
        "architecture": 0.50,
        "planning": 0.30,
        "coding": 5.00,
        "deslopify": 0.30,
        "readme": 0.20,
        "self_critique": 0.20,
        "review": 1.50,
        "security": 0.30,
        "deployment": 0.30,
        "pitch": 0.50,
        "presentation": 0.40,
    })

    def get_model(self, agent_role: str) -> ModelSpec:
        """Get the model spec for a given agent role."""
        if agent_role not in self.models:
            raise ValueError(
                f"Unknown agent role: {agent_role}. "
                f"Valid roles: {list(self.models.keys())}"
            )
        return self.models[agent_role]

    def budget_for_phase(self, phase: str) -> float:
        """Get the budget allocation for a specific phase.
        Returns the phase budget or 1/12th of total if not specified."""
        return self.phase_budgets.get(phase, self.budget_limit / 12.0)

    def reload_keys(self) -> bool:
        """Hot-reload API keys from .env without restarting.
        Useful when keys are added/changed mid-run.
        Returns True if any keys changed."""
        import logging
        logger = logging.getLogger(__name__)

        old_keys = self.keys
        load_dotenv(override=True)
        new_keys = APIKeys.from_env()

        changed = False
        for field_name in ("anthropic", "google", "moonshot", "exa", "gamma", "railway"):
            old_val = getattr(old_keys, field_name, None)
            new_val = getattr(new_keys, field_name, None)
            if old_val != new_val and new_val:
                changed = True
                logger.info(f"[Config] Hot-reloaded key: {field_name}")

        if changed:
            # Replace the keys (PipelineConfig is not frozen)
            object.__setattr__(self, "keys", new_keys)
            logger.info("[Config] API keys hot-reloaded from .env")

        # Also reload budget limit
        new_budget = float(os.getenv("PIPELINE_BUDGET_LIMIT", "10.00"))
        if new_budget != self.budget_limit:
            object.__setattr__(self, "budget_limit", new_budget)
            logger.info(f"[Config] Budget limit updated: ${new_budget:.2f}")
            changed = True

        return changed


def load_config() -> PipelineConfig:
    """Load and return the pipeline configuration."""
    config = PipelineConfig()
    config.workspace.ensure_dirs()
    return config
