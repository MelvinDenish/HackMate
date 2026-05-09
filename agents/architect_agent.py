"""
╔══════════════════════════════════════════════════════════════╗
║  ARCHITECT AGENT — Phase 2: PRD Generation                   ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Sonnet 4                           ║
║  Why: Best at structured system design, reasoning about      ║
║       data flows, API contracts, and component architecture  ║
║                                                              ║
║  Inputs:  Reads dossier from /workspace/briefs/              ║
║  Outputs: PRD document → /workspace/specs/prd_*.md           ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
from datetime import datetime, timezone

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from workspace.manager import WorkspaceManager

try:
    from agents.knowledge_base import KnowledgeBase
except ImportError:
    KnowledgeBase = None

logger = logging.getLogger(__name__)

ARCHITECT_SYSTEM_PROMPT = """You are the Architect Agent in an autonomous hackathon pipeline. You produce a comprehensive Product Requirements Document (PRD) that fully specifies what the coding agents must build.

## Your Mandate
- Translate the research dossier and refined brief into a BUILDABLE specification
- Every decision must be explicit — no ambiguity for downstream agents
- Design for a hackathon MVP — ship fast, impress judges, skip enterprise features
- The coding agents will follow this PRD EXACTLY — if you don't specify it, it won't get built

## PRD Structure (REQUIRED)

### 1. Project Overview
- One-paragraph summary of what this application does
- Target audience
- Core value proposition

### 2. Tech Stack Specification
- Frontend: framework, UI library, key packages
- Backend: language, framework, key packages  
- Database: type and schema overview
- AI/ML: models, APIs, libraries (if applicable)
- Deployment: target platform configuration

### 3. System Architecture
- High-level component diagram (describe in text)
- Data flow between frontend ↔ backend ↔ database
- External API integrations
- Authentication flow (if applicable)

### 4. Database Schema
- Table/collection definitions with field types
- Relationships between entities
- Sample seed data for demo

### 5. API Contract
- Each endpoint: method, path, request body, response body
- Error response format
- Authentication headers (if applicable)

### 6. UI/UX Specification
- Screen-by-screen breakdown
- Each screen: layout, components, interactions
- Navigation flow
- Responsive design requirements
- Color palette and design tokens (use HSL, not hex)
- Typography: specify Google Font (Inter/Roboto/Outfit)
- Dark mode / light mode preference
- Loading states for all async operations
- Micro-animations: fade-in, hover scale, transition timing

### 7. Core Business Logic
- Key algorithms or processing pipelines
- Validation rules
- Edge cases to handle

### 8. Demo Script & Seed Data (CRITICAL FOR JUDGING)
- EXACT seed data to pre-populate (5-10 realistic records with specific names, dates, values)
- The app MUST look "lived-in" when judges first open it — NEVER an empty dashboard
- Step-by-step demo walkthrough (click X → see Y → click Z → wow moment)
- Identify the single most impressive feature: the "hero moment"
- What data appears on the landing/home page by default?
- Include demo user credentials if auth is needed (e.g. demo@app.com / password123)

### 9. File Structure
- Exact directory and file layout for the project
- What each file contains

### 10. Dependencies
- Exact package names and versions
- package.json or requirements.txt content

### 11. README Specification
- Project name and one-line tagline
- Key features (3-5 bullets)
- Tech stack list with emojis
- Quick start commands (install, dev, build)
- Architecture overview (brief mermaid diagram description)

Be SPECIFIC. Use exact file names, exact endpoint paths, exact component names. The coding agents are stateless — they only see the PRD and their assigned task."""


def run_architect(
    refined_brief: str,
    dossier_path: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker=None,
) -> str:
    """
    Generate the Product Requirements Document (PRD).

    Reads the research dossier and refined brief, then produces
    a comprehensive PRD that fully specifies the application.

    Args:
        refined_brief: The Refined Brief from Phase 0
        dossier_path: Path to the research dossier
        workspace: Workspace manager
        config: Pipeline configuration

    Returns:
        Path to the saved PRD file
    """
    logger.info("[Architect] Starting PRD generation")

    # Read the dossier (fail-forward: proceed without it if missing)
    dossier_content = ""
    try:
        if dossier_path:
            dossier_content = workspace.read_file(dossier_path)
    except Exception as e:
        logger.warning(f"[Architect] Dossier unavailable ({e}), proceeding with brief only")

    # Query Knowledge Base for additional context (RAG)
    kb_context = ""
    try:
        if KnowledgeBase is not None:
            kb = KnowledgeBase(config)
            kb_context = kb.get_context_for_prompt(
                f"tech stack architecture for: {refined_brief[:500]}",
                n_results=5,
                max_chars=4000,
            )
            if kb_context and "No relevant context" not in kb_context:
                logger.info(f"[Architect] KB returned context ({len(kb_context)} chars)")
            else:
                kb_context = ""
    except Exception as e:
        logger.warning(f"[Architect] KB query failed ({e}), continuing without RAG")

    # Create the LLM
    spec = config.get_model("architect")
    llm = create_llm(spec, config.keys)

    prompt_parts = [f"## Refined Project Brief\n\n{refined_brief}\n\n"]
    if dossier_content:
        prompt_parts.append(f"## Market Research Dossier\n\n{dossier_content}\n\n")
    if kb_context:
        prompt_parts.append(f"## Knowledge Base Context (from research)\n\n{kb_context}\n\n")
    prompt_parts.append(
        "Generate a comprehensive Product Requirements Document (PRD) "
        "that fully specifies the hackathon application. Be extremely "
        "specific — the coding agents will build EXACTLY what you specify."
    )

    messages = [
        SystemMessage(content=ARCHITECT_SYSTEM_PROMPT),
        HumanMessage(content="".join(prompt_parts)),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="architect",
        phase="architecture",
        cost_tracker=cost_tracker,
    )
    prd_content = response.content

    # Add metadata header
    timestamp = datetime.now(timezone.utc).isoformat()
    header = (
        f"# Product Requirements Document\n\n"
        f"> Generated: {timestamp}\n"
        f"> Architect: Claude Sonnet 4 (Anthropic)\n\n---\n\n"
    )

    full_prd = header + prd_content

    # Save to workspace
    prd_path = workspace.write_file("specs", "prd_main.md", full_prd)
    workspace.log_event("architecture", "PRD generated", str(prd_path))

    logger.info(f"[Architect] PRD saved to: {prd_path}")
    return str(prd_path)
