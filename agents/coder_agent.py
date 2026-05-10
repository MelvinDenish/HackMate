"""
╔══════════════════════════════════════════════════════════════╗
║  CODER AGENT v2 — Phase 3: Code Synthesis                   ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Sonnet 4                           ║
║  Why: Top-tier code generation — best at understanding       ║
║       complex specs and producing production-quality code    ║
║                                                              ║
║  Improvements over v1:                                       ║
║  ✅ State-machine code parser (replaces fragile regex)       ║
║  ✅ Parallel task execution for independent tasks (asyncio)  ║
║  ✅ Cost tracking via invoke_with_retry                      ║
║  ✅ File validation (warns if parsed < expected)             ║
║  ✅ DAG-layer execution from Ralphinho pattern               ║
║                                                              ║
║  Pattern: Stateless Worker                                   ║
║  Each invocation handles ONE task from the queue.            ║
║  The orchestrator calls this agent repeatedly per task.      ║
║                                                              ║
║  Inputs:  PRD + single task record + existing src tree       ║
║  Outputs: Source code files → /workspace/src/                ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from pipeline.cost_tracker import CostTracker
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

CODER_SYSTEM_PROMPT = """You are a Coder Agent in an autonomous hackathon pipeline. You are a STATELESS worker — you receive a single task specification and produce source code files.

## Your Mandate
- Implement EXACTLY what the task description specifies
- Follow the PRD for architecture decisions and conventions
- Write clean, working, production-quality code
- Include all necessary imports, error handling, and comments
- Do NOT deviate from the specification — no creative additions

## Critical Rules
1. Generate COMPLETE files — never use "// ... rest of code" placeholders
2. Every file must be immediately runnable/importable
3. Follow the tech stack specified in the PRD exactly
4. Include proper error handling and input validation
5. Add brief comments for complex logic
6. Use the exact file paths specified in the task
7. Write tests alongside code when the task mentions testing

## UI Quality Standards (CRITICAL for hackathon judging)
When generating frontend code, the UI MUST look premium and modern:
- Use a curated dark or light color palette with HSL-based colors (not plain red/blue/green)
- Apply CSS variables for consistent theming: --primary, --surface, --text, --accent
- Every interactive element needs: hover state (scale/opacity), active state, focus ring
- Add CSS transitions (200-300ms ease) on ALL interactive elements
- Include loading spinners or skeleton screens for async operations
- Use proper spacing rhythm: 4px / 8px / 16px / 24px / 32px / 48px
- Add subtle box-shadows on cards and elevated elements
- Typography: Import Inter or Roboto from Google Fonts — never use system defaults
- Include micro-animations: fade-in on page load (opacity 0→1 over 400ms)
- Add gradient accents on primary CTAs and hero sections
- Ensure fully responsive: mobile-first with breakpoints at 768px and 1024px
- Every page must look populated — include seed/demo data, never "lorem ipsum"
- Add <meta name="viewport" content="width=device-width, initial-scale=1.0">

## Output Format
For EACH file you create, use this exact format:

```file:path/to/filename.ext
[complete file contents here]
```

Example:
```file:backend/server.js
import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
dotenv.config();

const app = express();
app.use(cors());
app.use(express.json());

// Health check
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// Global error handler
app.use((err, req, res, next) => {
  console.error(`[Error] ${err.message}`);
  res.status(err.status || 500).json({
    error: process.env.NODE_ENV === 'production' ? 'Internal server error' : err.message
  });
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`Server running on port ${PORT}`));
```

Generate ALL files needed for this task. Each file block must contain the COMPLETE file — no truncation, no placeholders."""

REVISION_PROMPT = """The Reviewer Agent found issues with your code. Fix the problems based on the test results below.

## Test Results
{test_results}

## Review Notes
{review_notes}

## Your Previous Code Files
{previous_files}

Regenerate ALL affected files with the fixes applied. Use the same ```file:path``` format.
Fix ONLY the reported issues — do not refactor working code."""


def _extract_prd_sections(prd_content: str, section_names: list[str]) -> str:
    """Extract specific sections from the PRD by their heading names.
    Falls back to full PRD (up to 12000 chars) if no sections matched."""
    if not section_names:
        return prd_content[:12000]

    import re
    extracted = []
    # Split PRD by markdown headings (### N. Title)
    sections = re.split(r'(?=^###?\s+\d*\.?\s*)', prd_content, flags=re.MULTILINE)

    for section in sections:
        for name in section_names:
            if name.lower() in section[:200].lower():
                extracted.append(section.strip())
                break

    if extracted:
        return "\n\n".join(extracted)

    # Fallback: no sections matched, return more of the PRD
    return prd_content[:12000]


def _get_dependency_file_contents(
    task: dict, all_tasks: list[dict], workspace: WorkspaceManager, max_chars: int = 6000
) -> str:
    """Read the content of files created by dependency tasks."""
    dep_ids = set(task.get("dependencies", []))
    if not dep_ids:
        return ""

    dep_files = []
    for t in all_tasks:
        if t.get("id") in dep_ids and t.get("files_created"):
            for fpath in t["files_created"]:
                try:
                    content = workspace.read_file(fpath)
                    rel = fpath.split("src/")[-1] if "src/" in fpath else fpath.split("\\")[-1]
                    dep_files.append(f"### {rel}\n```\n{content[:3000]}\n```")
                except Exception:
                    continue

    if not dep_files:
        return ""

    result = "\n\n".join(dep_files)
    return result[:max_chars]


def execute_task(
    task: dict,
    prd_content: str,
    src_tree: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    revision_context: str = "",
    cost_tracker: CostTracker = None,
    all_tasks: list[dict] = None,
) -> list[str]:
    """
    Execute a single task from the task queue.

    Args:
        task: Task record dict from the planner
        prd_content: Full PRD text for context
        src_tree: Current source tree structure
        workspace: Workspace manager
        config: Pipeline configuration
        revision_context: Optional revision instructions from reviewer
        cost_tracker: Optional cost tracker
        all_tasks: All tasks (for reading dependency file contents)

    Returns:
        List of file paths created/modified
    """
    task_id = task.get("id", "unknown")
    task_title = task.get("title", "Unknown task")
    complexity = task.get("complexity", "medium")
    logger.info(f"[Coder] Executing task {task_id}: {task_title} (complexity: {complexity})")

    # Adaptive model routing: low complexity → Haiku, high → Sonnet
    if complexity == "low":
        spec = config.get_model("deslopify")  # Haiku — fast + cheap for simple tasks
        logger.info(f"[Coder] Using Haiku for low-complexity task {task_id}")
    else:
        spec = config.get_model("coder")  # Sonnet — full power for medium/high
    llm = create_llm(spec, config.keys)

    # Build the prompt with TARGETED PRD sections (not truncated)
    relevant_sections = task.get("relevant_prd_sections", [])
    prd_context = _extract_prd_sections(prd_content, relevant_sections)

    task_spec = (
        f"## Task: {task_title}\n"
        f"**ID**: {task_id}\n"
        f"**Role**: {task.get('role', 'fullstack')}\n\n"
        f"### Description\n{task.get('description', '')}\n\n"
        f"### Acceptance Criteria\n"
    )
    for criterion in task.get("acceptance_criteria", []):
        task_spec += f"- {criterion}\n"

    if task.get("estimated_files"):
        task_spec += f"\n### Expected Files\n"
        for f in task["estimated_files"]:
            task_spec += f"- `{f}`\n"

    context = (
        f"## Current Project Structure\n```\n{src_tree}\n```\n\n"
        f"## PRD (Relevant Sections)\n{prd_context}\n\n"
        f"## Your Task\n{task_spec}"
    )

    # Add dependency file contents so coder can reference previously created code
    if all_tasks:
        dep_content = _get_dependency_file_contents(task, all_tasks, workspace)
        if dep_content:
            context += f"\n\n## Previously Generated Files (from dependency tasks)\n{dep_content}"

    if revision_context:
        context += f"\n\n## REVISION REQUIRED\n{revision_context}"

    messages = [
        SystemMessage(content=CODER_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="coder",
        phase="coding",
        cost_tracker=cost_tracker,
    )
    raw_output = response.content

    # Parse file blocks using shared state-machine parser
    from utils.file_parser import parse_file_blocks, write_parsed_files
    parsed = parse_file_blocks(raw_output)
    files_created = write_parsed_files(parsed, workspace.write_source_file, label="Coder")

    # ── Quick-Validate Inner Loop (Devin pattern) ──────────────
    # Immediately check generated files for syntax errors.
    # If any fail, retry the task with the error — saves a full review cycle.
    syntax_errors = _quick_validate(files_created)
    if syntax_errors and not revision_context:
        # Only retry once to avoid infinite loops
        logger.warning(
            f"[Coder] Task {task_id}: {len(syntax_errors)} syntax errors detected. "
            f"Retrying with corrections..."
        )
        correction_prompt = (
            f"Your previous code has syntax errors. Fix ONLY these errors:\n\n"
            + "\n".join(f"- {e}" for e in syntax_errors)
            + "\n\nRegenerate the affected files with the ```file:path``` format."
        )
        correction_messages = [
            SystemMessage(content=CODER_SYSTEM_PROMPT),
            HumanMessage(content=context + f"\n\n## SYNTAX FIX REQUIRED\n{correction_prompt}"),
        ]
        retry_response = invoke_with_retry(
            llm, correction_messages,
            spec=spec, agent_name="coder", phase="coding_fix",
            cost_tracker=cost_tracker,
        )
        retry_parsed = parse_file_blocks(retry_response.content)
        if retry_parsed:
            retry_files = write_parsed_files(retry_parsed, workspace.write_source_file, label="Coder-Fix")
            # Merge: replace fixed files in our list
            fixed_paths = set(retry_files)
            files_created = [f for f in files_created if f not in fixed_paths] + retry_files
            logger.info(f"[Coder] Task {task_id}: self-correction fixed {len(retry_files)} files")

    # Validate: warn if fewer files than expected
    expected = task.get("estimated_files", [])
    if expected and len(files_created) < len(expected):
        logger.warning(
            f"[Coder] Task {task_id}: expected {len(expected)} files, "
            f"got {len(files_created)}. May need revision."
        )

    logger.info(f"[Coder] Task {task_id}: created {len(files_created)} files")
    return files_created


def _quick_validate(file_paths: list[str]) -> list[str]:
    """Quick syntax validation of generated files. Returns list of error strings.
    Only checks Python and JS — fast, no external tools needed."""
    errors = []
    for fp in file_paths:
        try:
            path = Path(fp) if not isinstance(fp, Path) else fp
            if not path.exists():
                continue

            content = path.read_text(encoding="utf-8")

            if path.suffix == ".py":
                try:
                    compile(content, str(path), "exec")
                except SyntaxError as e:
                    errors.append(f"{path.name}:{e.lineno}: {e.msg}")

            elif path.suffix in (".js", ".ts", ".jsx", ".tsx"):
                # Quick brace/bracket balance check
                if content.count("{") != content.count("}"):
                    errors.append(f"{path.name}: mismatched curly braces")
                if content.count("(") != content.count(")"):
                    errors.append(f"{path.name}: mismatched parentheses")

        except Exception:
            continue

    return errors


def _build_dependency_layers(tasks: list[dict]) -> list[list[dict]]:
    """
    Build DAG layers from task dependencies.
    Tasks in the same layer have no inter-dependencies and can run in parallel.

    Pattern: Ralphinho RFC-Driven DAG (ECC autonomous-loops skill)
    """
    completed_ids: set[str] = set()
    remaining = list(tasks)
    layers = []

    while remaining:
        # Find tasks whose deps are all met
        ready = []
        not_ready = []

        for task in remaining:
            deps = set(task.get("dependencies", []))
            if deps.issubset(completed_ids):
                ready.append(task)
            else:
                not_ready.append(task)

        if not ready:
            # Deadlock: add all remaining to final layer (hackathon mode)
            logger.warning(
                f"[Coder] Dependency deadlock with {len(not_ready)} tasks. "
                f"Proceeding anyway (hackathon mode)."
            )
            layers.append(not_ready)
            break

        layers.append(ready)
        for task in ready:
            completed_ids.add(task.get("id", ""))
        remaining = not_ready

    return layers


def execute_all_tasks(
    tasks: list[dict],
    prd_path: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker = None,
) -> list[str]:
    """
    Execute all tasks using DAG-layer parallelism.

    Independent tasks run concurrently within each layer via asyncio.
    Layers execute sequentially (respecting dependencies).

    Args:
        tasks: List of task records (sorted by priority)
        prd_path: Path to the PRD
        workspace: Workspace manager
        config: Pipeline configuration
        cost_tracker: Optional cost tracker

    Returns:
        List of all file paths created
    """
    prd_content = workspace.read_file(prd_path)
    all_files = []

    # Sort by priority, then build dependency layers
    sorted_tasks = sorted(tasks, key=lambda t: t.get("priority", 99))
    layers = _build_dependency_layers(sorted_tasks)

    logger.info(
        f"[Coder] Task plan: {len(sorted_tasks)} tasks in {len(layers)} layers"
    )

    for layer_idx, layer in enumerate(layers):
        logger.info(
            f"[Coder] Layer {layer_idx + 1}/{len(layers)}: "
            f"{len(layer)} tasks ({', '.join(t.get('id', '?') for t in layer)})"
        )

        if len(layer) == 1:
            # Single task — execute directly
            task = layer[0]
            src_tree = workspace.get_src_tree()
            files = execute_task(
                task, prd_content, src_tree, workspace, config,
                cost_tracker=cost_tracker,
                all_tasks=sorted_tasks,
            )
            all_files.extend(files)
            task["status"] = "completed"
            task["files_created"] = files
        else:
            # Multiple independent tasks — execute with asyncio parallelism
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Already in an async context, run sequentially
                    _execute_layer_sequential(
                        layer, prd_content, workspace, config,
                        cost_tracker, sorted_tasks, all_files,
                    )
                else:
                    asyncio.run(_execute_layer_parallel(
                        layer, prd_content, workspace, config,
                        cost_tracker, sorted_tasks, all_files,
                    ))
            except RuntimeError:
                # No event loop, create one
                asyncio.run(_execute_layer_parallel(
                    layer, prd_content, workspace, config,
                    cost_tracker, sorted_tasks, all_files,
                ))

        # Log layer completion
        for task in layer:
            workspace.log_event(
                "coding",
                f"Task {task.get('id', '')} completed: {task.get('title', '')}",
                f"Files: {', '.join(task.get('files_created', []))}",
            )

    return all_files


async def _execute_layer_parallel(
    layer: list[dict],
    prd_content: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker,
    all_tasks: list[dict],
    all_files: list[str],
):
    """Execute independent tasks in parallel using asyncio.to_thread."""
    async def _run_task(task):
        src_tree = workspace.get_src_tree()
        return await asyncio.to_thread(
            execute_task, task, prd_content, src_tree, workspace, config,
            "", cost_tracker, all_tasks,
        )

    results = await asyncio.gather(*[_run_task(t) for t in layer])
    for task, files in zip(layer, results):
        all_files.extend(files)
        task["status"] = "completed"
        task["files_created"] = files


def _execute_layer_sequential(
    layer: list[dict],
    prd_content: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker,
    all_tasks: list[dict],
    all_files: list[str],
):
    """Fallback: execute tasks sequentially when async isn't available."""
    for task in layer:
        src_tree = workspace.get_src_tree()
        files = execute_task(
            task, prd_content, src_tree, workspace, config,
            cost_tracker=cost_tracker,
            all_tasks=all_tasks,
        )
        all_files.extend(files)
        task["status"] = "completed"
        task["files_created"] = files
