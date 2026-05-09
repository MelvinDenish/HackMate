"""
╔══════════════════════════════════════════════════════════════╗
║  PIPELINE STATE — LangGraph Shared State Definitions         ║
║                                                              ║
║  This module defines the TypedDict state schema that flows   ║
║  through the entire LangGraph state machine. Each phase      ║
║  reads from and writes to specific state fields.             ║
║                                                              ║
║  Data Flow:                                                  ║
║  User Input → Clarification → Dossier → PRD → Tasks →       ║
║  Code → Review → Deploy → Pitch → Presentation              ║
║                                                              ║
║  Handoff Pattern: "Delegation by Reference"                  ║
║  Agents pass FILE PATHS (not raw content) to preserve        ║
║  context windows. Content lives in /workspace/ dirs.         ║
╚══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations
from typing import TypedDict, Literal, Any
from dataclasses import dataclass, field


# ── Phase Status Tracking ────────────────────────────────────

PhaseStatus = Literal["pending", "running", "completed", "failed", "skipped"]


# ── Task Record (Phase 2 Output) ─────────────────────────────

@dataclass
class TaskRecord:
    """A single decomposed development task from the Planner Agent."""
    id: str                          # e.g. "task_001"
    title: str                       # e.g. "Create Express.js server"
    description: str                 # Detailed implementation instructions
    role: str                        # "frontend" | "backend" | "ai_ml" | "devops"
    priority: int                    # 1 = highest
    dependencies: list[str]          # IDs of tasks that must complete first
    acceptance_criteria: list[str]   # Verifiable conditions for completion
    status: str = "pending"          # "pending" | "in_progress" | "completed" | "failed"
    files_created: list[str] = field(default_factory=list)
    review_notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "acceptance_criteria": self.acceptance_criteria,
            "status": self.status,
            "files_created": self.files_created,
            "review_notes": self.review_notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ── Main Pipeline State ──────────────────────────────────────

class PipelineState(TypedDict, total=False):
    """
    The unified state object that flows through the entire
    LangGraph state machine. Each node reads/writes specific fields.

    ── Immutable Inputs ──
    problem_statement:  Raw user input
    refined_brief:      Clarified + enriched problem description

    ── Phase Outputs (file paths, not content) ──
    dossier_path:       Path to research dossier markdown
    prd_path:           Path to Product Requirements Document
    task_queue_path:    Path to JSON task queue
    src_dir:            Path to generated source code directory
    deployment_url:     Live application URL
    pitch_content_path: Path to pitch narrative JSON
    deck_url:           Path/URL to final presentation file

    ── Operational State ──
    phase_status:       Status of each phase
    review_iteration:   Current code review retry count
    errors:             Accumulated error log
    messages:           LangGraph message history
    """

    # ── User Inputs ──
    problem_statement: str
    refined_brief: str
    user_answers: dict[str, str]  # Q&A from clarification phase
    tech_stack: str               # User's preferred tech stack

    # ── Phase 1: Research Outputs ──
    dossier_path: str
    knowledge_base_id: str        # ChromaDB collection name

    # ── Phase 2: Architecture Outputs ──
    prd_path: str
    task_queue_path: str
    task_records: list[dict]      # Serialized TaskRecord list

    # ── Phase 3: Development Outputs ──
    src_dir: str
    code_files: list[str]         # List of generated file paths
    test_results: dict[str, Any]  # Test execution results
    deployment_url: str
    deployment_config: dict       # Railway config

    # ── Phase 4: Delivery Outputs ──
    pitch_content_path: str
    pitch_slides: list[dict]      # Structured slide content
    deck_url: str                 # Gamma-generated deck URL
    deck_local_path: str          # Local path if downloaded

    # ── Operational ──
    current_phase: str
    phase_status: dict[str, str]  # phase_name -> PhaseStatus
    review_iteration: int
    errors: list[str]
    messages: list[Any]           # LangGraph message history

    # ── v2: Security & Cleanup ──
    security_verdict: str         # PASS, WARN, FAIL
    security_findings: str        # Security review findings summary


def create_initial_state(problem_statement: str) -> PipelineState:
    """Create a fresh pipeline state from a user's problem statement."""
    return PipelineState(
        problem_statement=problem_statement,
        refined_brief="",
        user_answers={},
        tech_stack="",

        dossier_path="",
        knowledge_base_id="",

        prd_path="",
        task_queue_path="",
        task_records=[],

        src_dir="",
        code_files=[],
        test_results={},
        deployment_url="",
        deployment_config={},

        pitch_content_path="",
        pitch_slides=[],
        deck_url="",
        deck_local_path="",

        current_phase="clarification",
        phase_status={
            "clarification": "pending",
            "research": "pending",
            "architecture": "pending",
            "planning": "pending",
            "coding": "pending",
            "deslopify": "pending",
            "review": "pending",
            "security": "pending",
            "deployment": "pending",
            "pitch": "pending",
            "presentation": "pending",
        },
        review_iteration=0,
        errors=[],
        messages=[],

        security_verdict="",
        security_findings="",
    )
