"""
╔══════════════════════════════════════════════════════════════╗
║  PLANNER AGENT — Phase 2: Task Decomposition                 ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Sonnet 4                           ║
║  Why: Precise structured decomposition with dependency       ║
║       tracking. Follows Refine-Plan-Act pattern.             ║
║                                                              ║
║  Inputs:  PRD from /workspace/specs/                         ║
║  Outputs: JSON task queue → /workspace/tasks/task_queue.json ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
from datetime import datetime, timezone

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the Planner Agent in an autonomous hackathon pipeline. Your role is to decompose the PRD into a precise, dependency-aware task queue that stateless coding agents will execute.

## Your Mandate
- Break the PRD into discrete, executable tasks
- Each task must be completable by a SINGLE agent in a SINGLE session
- Define clear dependencies (which tasks must complete before others)
- Assign each task a role: "backend", "frontend", "ai_ml", or "fullstack"
- Write acceptance criteria that a reviewer agent can verify programmatically

## Refine-Plan-Act Pattern
For each task, you must:
1. REFINE: Understand exactly what the PRD requires for this component
2. PLAN: Determine the files to create, the code structure, the tests
3. ACT: Write the task specification so a coder can execute without questions

## Task Priority Rules
- Priority 1: Project setup, configuration files, package manifests
- Priority 2: Database models/schemas, core utilities
- Priority 3: Backend API routes, business logic
- Priority 4: Frontend components, pages, styling
- Priority 5: Integration, testing, deployment config
- Priority 6: Demo data, polish, documentation

## Output Format (STRICT JSON)
Return a JSON array of task objects. Each task:
```json
{
  "id": "task_001",
  "title": "Short descriptive title",
  "description": "Detailed implementation instructions including exact file paths, function signatures, imports needed, and logic to implement",
  "role": "backend|frontend|ai_ml|fullstack",
  "priority": 1,
  "dependencies": ["task_000"],
  "acceptance_criteria": [
    "File server.js exists and exports an Express app",
    "GET /api/health returns 200 with {status: ok}",
    "All imports resolve without errors"
  ],
  "estimated_files": ["src/backend/server.js", "src/backend/routes/health.js"],
  "relevant_prd_sections": ["Tech Stack Specification", "API Contract", "Database Schema"],
  "complexity": "low|medium|high"
}
```

Rules:
- Generate 8-20 tasks (appropriate for a hackathon MVP)
- Task descriptions must be EXTREMELY detailed — the coder sees ONLY this task
- Include exact file paths, exact function names, exact package imports
- First task is ALWAYS project initialization (package.json / requirements.txt)
- Last task is ALWAYS deployment configuration
- For `relevant_prd_sections`: list ONLY the PRD section titles the coder needs for THIS task
- For `complexity`: "low" = config/setup files, "medium" = standard CRUD/UI, "high" = algorithms/auth/AI
- Return ONLY the JSON array — no explanation text"""


def run_planner(
    prd_path: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker=None,
) -> tuple[str, list[dict]]:
    """
    Decompose the PRD into an executable task queue.

    Args:
        prd_path: Path to the PRD document
        workspace: Workspace manager
        config: Pipeline configuration

    Returns:
        Tuple of (task_queue_path, task_records_list)
    """
    logger.info("[Planner] Starting task decomposition")

    # Read the PRD
    prd_content = workspace.read_file(prd_path)

    # Create the LLM
    spec = config.get_model("planner")
    llm = create_llm(spec, config.keys)

    messages = [
        SystemMessage(content=PLANNER_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"## Product Requirements Document\n\n{prd_content}\n\n"
            "Decompose this PRD into a precise, dependency-aware task queue. "
            "Return ONLY a JSON array of task objects."
        )),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="planner",
        phase="planning",
        cost_tracker=cost_tracker,
    )
    content = response.content.strip()

    # Parse JSON from response
    try:
        # Handle markdown code blocks
        if "```" in content:
            # Extract JSON from code block
            parts = content.split("```")
            for part in parts[1:]:
                cleaned = part.strip()
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:].strip()
                try:
                    tasks = json.loads(cleaned)
                    if isinstance(tasks, list):
                        break
                except json.JSONDecodeError:
                    continue
            else:
                tasks = json.loads(content)
        else:
            tasks = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"[Planner] Failed to parse task JSON: {e}")
        logger.error(f"[Planner] Raw response: {content[:500]}")
        # Attempt recovery: ask LLM to fix the JSON
        tasks = _recover_json(content, config)

    if not isinstance(tasks, list):
        tasks = [tasks]

    # Validate and normalize tasks
    validated_tasks = []
    for i, task in enumerate(tasks):
        validated = {
            "id": task.get("id", f"task_{i+1:03d}"),
            "title": task.get("title", f"Task {i+1}"),
            "description": task.get("description", ""),
            "role": task.get("role", "fullstack"),
            "priority": task.get("priority", i + 1),
            "dependencies": task.get("dependencies", []),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "estimated_files": task.get("estimated_files", []),
            "relevant_prd_sections": task.get("relevant_prd_sections", []),
            "complexity": task.get("complexity", "medium"),
            "status": "pending",
        }
        validated_tasks.append(validated)

    # Sort by priority
    validated_tasks.sort(key=lambda t: t["priority"])

    # Save task queue
    task_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_tasks": len(validated_tasks),
        "planner_model": "Claude Sonnet 4 (Anthropic)",
        "tasks": validated_tasks,
    }

    task_path = workspace.write_json("tasks", "task_queue.json", task_data)
    workspace.log_event("planning", f"Task queue: {len(validated_tasks)} tasks", str(task_path))

    logger.info(f"[Planner] Created {len(validated_tasks)} tasks → {task_path}")
    return str(task_path), validated_tasks


def _recover_json(raw: str, config: PipelineConfig, cost_tracker=None) -> list[dict]:
    """Attempt to recover malformed JSON using the LLM."""
    logger.info("[Planner] Attempting JSON recovery...")
    spec = config.get_model("planner")
    llm = create_llm(spec, config.keys)

    messages = [
        HumanMessage(content=(
            "The following text was supposed to be a JSON array of task "
            "objects but has syntax errors. Fix ONLY the JSON syntax and "
            "return a valid JSON array. No explanation.\n\n"
            f"{raw[:6000]}"
        )),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="planner",
        phase="planning",
        cost_tracker=cost_tracker,
    )
    content = response.content.strip()
    if "```" in content:
        parts = content.split("```")
        for part in parts[1:]:
            cleaned = part.strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                continue

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error("[Planner] JSON recovery failed, returning empty task list")
        return []
