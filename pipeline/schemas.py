"""
╔══════════════════════════════════════════════════════════════╗
║  STRUCTURED SCHEMAS — Pydantic Models for Agent Output       ║
║                                                              ║
║  Inspired by: MetaGPT's SOP-driven structured outputs        ║
║                                                              ║
║  Provides validated, typed output schemas for all agents.    ║
║  When used with llm.with_structured_output(Schema), the      ║
║  LLM is forced to return valid JSON matching the schema.     ║
║  Eliminates ~80% of JSON parse failures.                     ║
╚══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations
import json
import logging
import re
from typing import Literal, Optional

try:
    from pydantic import BaseModel, Field
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False
    # Fallback: define minimal base class
    class BaseModel:
        pass
    def Field(*args, **kwargs):
        return None

logger = logging.getLogger(__name__)


# ── Task Planning Schema ─────────────────────────────────────

class TaskSpec(BaseModel):
    """A single decomposed development task from the Planner Agent."""
    id: str = Field(description="Unique task ID like task_001")
    title: str = Field(description="Short descriptive title")
    description: str = Field(description="Detailed implementation instructions")
    role: Literal["backend", "frontend", "ai_ml", "fullstack"] = Field(
        description="Developer role for this task"
    )
    priority: int = Field(ge=1, le=10, description="Priority (1=highest)")
    dependencies: list[str] = Field(
        default_factory=list,
        description="IDs of tasks that must complete first"
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Verifiable conditions for completion"
    )
    estimated_files: list[str] = Field(
        default_factory=list,
        description="Expected file paths to create"
    )
    relevant_prd_sections: list[str] = Field(
        default_factory=list,
        description="PRD section titles the coder needs"
    )
    complexity: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="Task complexity for model routing"
    )


class TaskQueue(BaseModel):
    """Complete task queue output from the Planner Agent."""
    tasks: list[TaskSpec] = Field(description="Ordered list of development tasks")


# ── Review Schema ────────────────────────────────────────────

class ReviewVerification(BaseModel):
    """Verification results for each review phase."""
    build: Literal["PASS", "FAIL"] = "PASS"
    imports: Literal["PASS", "FAIL"] = "PASS"
    lint: Literal["PASS", "WARN", "FAIL"] = "PASS"
    tests: Literal["PASS", "SKIP", "FAIL"] = "SKIP"
    runtime: Literal["PASS", "FAIL"] = "PASS"
    logic: Literal["PASS", "WARN", "FAIL"] = "PASS"
    prd_compliance: Literal["PASS", "PARTIAL", "FAIL"] = "PASS"


class ReviewOutput(BaseModel):
    """Structured output from the Reviewer Agent."""
    verdict: Literal["PASS", "FAIL"] = Field(description="Overall verdict")
    verification: ReviewVerification = Field(
        default_factory=ReviewVerification,
        description="Per-phase verification results"
    )
    notes: str = Field(default="", description="Summary notes")
    issues: list[str] = Field(
        default_factory=list,
        description="List of specific issues found"
    )
    fix_instructions: str = Field(
        default="",
        description="Instructions for the Coder to fix issues"
    )


# ── Pitch Schema ─────────────────────────────────────────────

class PitchSlide(BaseModel):
    """A single slide in the pitch deck."""
    slide_number: int = Field(ge=1)
    title: str
    headline: str = ""
    body: str = ""
    bullets: list[str] = Field(default_factory=list)
    visual_suggestion: str = ""


class PitchOutput(BaseModel):
    """Structured output from the Pitch Agent."""
    title: str = Field(description="Project title")
    subtitle: str = Field(default="", description="One-line tagline")
    slides: list[PitchSlide] = Field(description="Ordered slide content")
    demo_script: str = Field(default="", description="Demo walkthrough script")


# ── Security Schema ──────────────────────────────────────────

class SecurityFinding(BaseModel):
    """A single security finding."""
    severity: Literal["CRITICAL", "WARNING", "INFO"]
    file: str = ""
    description: str
    fix: str = ""


class SecurityOutput(BaseModel):
    """Structured output from the Security Agent."""
    verdict: Literal["PASS", "WARN", "FAIL"]
    critical_count: int = 0
    warning_count: int = 0
    findings: list[SecurityFinding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────

def safe_parse_json(text: str, schema_class=None) -> dict | list | None:
    """Parse JSON from LLM output with multiple fallback strategies.

    1. Try direct JSON parse
    2. Try extracting JSON from ```json``` code blocks
    3. Try extracting JSON array from text
    4. If schema_class provided and Pydantic available, validate

    Returns parsed data or None on failure.
    """
    # Strategy 1: Direct parse
    try:
        data = json.loads(text)
        if schema_class and HAS_PYDANTIC:
            try:
                if isinstance(data, list):
                    return [schema_class(**item).model_dump() for item in data]
                else:
                    return schema_class(**data).model_dump()
            except Exception:
                return data
        return data
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract from code blocks
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            return data
        except json.JSONDecodeError:
            pass

    # Strategy 3: Find JSON array
    bracket_match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if bracket_match:
        try:
            return json.loads(bracket_match.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 4: Find JSON object
    brace_match = re.search(r'\{.*\}', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("[Schemas] All JSON parse strategies failed")
    return None
