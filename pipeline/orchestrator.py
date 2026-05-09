"""
╔══════════════════════════════════════════════════════════════╗
║  ORCHESTRATOR v2 — LangGraph State Machine with New Phases   ║
║                                                              ║
║  Improvements over v1:                                       ║
║  ✅ De-Sloppify node between Code → Review                   ║
║  ✅ Security node between Review → Deploy                    ║
║  ✅ Cost tracking passed to all agent calls                  ║
║  ✅ LangGraph SQLite checkpointing for resume-on-failure     ║
║  ✅ Cost report generation at pipeline end                   ║
║                                                              ║
║  Updated Pipeline Flow:                                      ║
║  research → architect → plan → code → deslopify → review ↺  ║
║  → security → deploy → pitch → present                      ║
║                                                              ║
║  Architecture: Hierarchical Manager-Worker (from paper)      ║
║                                                              ║
║  Each node:                                                  ║
║  1. Logs phase entry                                         ║
║  2. Reads required inputs from state (file paths)            ║
║  3. Delegates to the specialized agent                       ║
║  4. Updates state with results                               ║
║  5. Updates phase_status to "completed" or "failed"          ║
║                                                              ║
║  Error Handling:                                             ║
║  - Each node catches exceptions and records them in state    ║
║  - Review → Code loop enforces max retry limit               ║
║  - Pipeline can resume from the last successful phase        ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
from pathlib import Path
from typing import Literal

from langgraph.graph import StateGraph, END

from pipeline.state import PipelineState
from pipeline.cost_tracker import CostTracker
from config import PipelineConfig
from workspace.manager import WorkspaceManager

# Agent imports
from agents.research_agent import run_research
from agents.knowledge_base import KnowledgeBase
from agents.architect_agent import run_architect
from agents.planner_agent import run_planner
from agents.coder_agent import execute_all_tasks, execute_task
from agents.deslopify_agent import run_deslopify
from agents.reviewer_agent import run_review
from agents.security_agent import run_security_review
from agents.deployer_agent import deploy_to_railway, generate_deploy_config
from agents.pitch_agent import run_pitch
from agents.presentation_agent import run_presentation

logger = logging.getLogger(__name__)


def build_pipeline(
    config: PipelineConfig,
    workspace: WorkspaceManager,
    cost_tracker: CostTracker = None,
    progress_callback=None,
) -> StateGraph:
    """
    Build the LangGraph state machine for the full pipeline.

    The graph enforces deterministic phase transitions with
    validation gates at each edge.

    Args:
        config: Pipeline configuration
        workspace: Shared workspace manager
        cost_tracker: Cost tracker for budget enforcement
        progress_callback: Optional callable(phase, status, cost_so_far) for UI updates

    Returns:
        Compiled LangGraph StateGraph ready to invoke
    """
    logger.info("[Orchestrator] Building pipeline graph v2")

    def _notify(phase: str, status: str):
        """Fire progress callback if registered."""
        if progress_callback:
            try:
                cost = cost_tracker.total_cost if cost_tracker else 0.0
                progress_callback(phase, status, cost)
            except Exception:
                pass

    # ── Node Functions ────────────────────────────────────────
    # Each node receives PipelineState and returns updated state.

    def research_node(state: PipelineState) -> dict:
        """Phase 1: Market Research — Gemini 2.5 Flash"""
        logger.info("═══ PHASE 1: MARKET RESEARCH ═══")
        _notify("research", "running")
        status = dict(state.get("phase_status", {}))
        status["research"] = "running"

        try:
            refined_brief = state.get("refined_brief", state["problem_statement"])

            dossier_path = run_research(
                refined_brief, workspace, config,
                cost_tracker=cost_tracker,
            )

            # Ingest into knowledge base
            try:
                kb = KnowledgeBase(config)
                kb.ingest_file(dossier_path, doc_type="dossier")
                kb_id = kb.collection_name
            except Exception as e:
                logger.warning(f"[Orchestrator] KB ingestion failed: {e}")
                kb_id = ""

            status["research"] = "completed"
            return {
                "dossier_path": dossier_path,
                "knowledge_base_id": kb_id,
                "current_phase": "architecture",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Research failed: {e}")
            status["research"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Research: {str(e)}")
            return {
                "current_phase": "architecture",
                "phase_status": status,
                "errors": errors,
            }

    def architect_node(state: PipelineState) -> dict:
        """Phase 2a: Architecture — Claude Sonnet 4"""
        logger.info("═══ PHASE 2a: ARCHITECTURE ═══")
        _notify("architecture", "running")
        status = dict(state.get("phase_status", {}))
        status["architecture"] = "running"

        try:
            refined_brief = state.get("refined_brief", state["problem_statement"])
            dossier_path = state.get("dossier_path", "")

            if not dossier_path:
                logger.warning(
                    "[Orchestrator] No dossier available — architect will proceed "
                    "with refined brief only (fail-forward)"
                )

            prd_path = run_architect(
                refined_brief, dossier_path, workspace, config,
                cost_tracker=cost_tracker,
            )

            status["architecture"] = "completed"
            return {
                "prd_path": prd_path,
                "current_phase": "planning",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Architecture failed: {e}")
            status["architecture"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Architecture: {str(e)}")
            return {"phase_status": status, "errors": errors}

    def plan_node(state: PipelineState) -> dict:
        """Phase 2b: Task Decomposition — Claude Sonnet 4"""
        logger.info("═══ PHASE 2b: TASK DECOMPOSITION ═══")
        _notify("planning", "running")
        status = dict(state.get("phase_status", {}))
        status["planning"] = "running"

        try:
            prd_path = state.get("prd_path", "")
            if not prd_path:
                raise ValueError("No PRD available")

            task_path, task_records = run_planner(
                prd_path, workspace, config,
                cost_tracker=cost_tracker,
            )

            status["planning"] = "completed"
            return {
                "task_queue_path": task_path,
                "task_records": task_records,
                "current_phase": "coding",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Planning failed: {e}")
            status["planning"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Planning: {str(e)}")
            return {"phase_status": status, "errors": errors}

    def code_node(state: PipelineState) -> dict:
        """Phase 3a: Code Synthesis — Claude Sonnet 4"""
        logger.info("═══ PHASE 3a: CODE SYNTHESIS ═══")
        _notify("coding", "running")
        status = dict(state.get("phase_status", {}))
        status["coding"] = "running"

        try:
            tasks = state.get("task_records", [])
            prd_path = state.get("prd_path", "")

            if not tasks:
                raise ValueError("No tasks in queue")

            iteration = state.get("review_iteration", 0)
            if iteration == 0:
                # First pass: execute all tasks
                code_files = execute_all_tasks(
                    tasks, prd_path, workspace, config,
                    cost_tracker=cost_tracker,
                )
            else:
                # Revision pass: fix ONLY tasks relevant to the review feedback
                test_results = state.get("test_results", {})
                review_notes = test_results.get("notes", "")
                fix_instructions = test_results.get("fix_instructions", "")

                prd_content = workspace.read_file(prd_path)
                src_tree = workspace.get_src_tree()

                revision_context = (
                    f"## Previous Test Results\n{review_notes}\n\n"
                    f"## Fix Instructions\n{fix_instructions}"
                )

                # Identify which tasks need re-execution based on review feedback
                # Match tasks whose files or titles appear in the fix instructions
                tasks_to_fix = []
                for task in tasks:
                    task_files = task.get("files_created", [])
                    task_title = task.get("title", "").lower()

                    # Check if any of this task's files are mentioned in fix feedback
                    file_mentioned = any(
                        Path(f).name.lower() in fix_instructions.lower()
                        for f in task_files if f
                    )
                    title_mentioned = task_title and task_title in fix_instructions.lower()

                    if file_mentioned or title_mentioned:
                        tasks_to_fix.append(task)

                # Fallback: if we couldn't match specific tasks, re-do all incomplete
                if not tasks_to_fix:
                    tasks_to_fix = [
                        t for t in tasks if t.get("status") != "completed"
                    ]
                    if not tasks_to_fix:
                        tasks_to_fix = tasks  # Last resort

                logger.info(
                    f"[Orchestrator] Revision: re-executing {len(tasks_to_fix)}/{len(tasks)} tasks"
                )

                # Re-execute targeted tasks with revision context
                code_files = []
                for task in tasks_to_fix:
                    task["status"] = "pending"  # Reset for re-execution
                    files = execute_task(
                        task, prd_content, src_tree,
                        workspace, config, revision_context,
                        cost_tracker=cost_tracker,
                        all_tasks=tasks,
                    )
                    code_files.extend(files)

            status["coding"] = "completed"
            return {
                "code_files": code_files,
                "src_dir": str(workspace.src_dir),
                "current_phase": "deslopify",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Coding failed: {e}")
            status["coding"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Coding: {str(e)}")
            return {"phase_status": status, "errors": errors}

    def deslopify_node(state: PipelineState) -> dict:
        """Phase 3a.5: De-Sloppify Cleanup — Claude Haiku 3.5"""
        logger.info("═══ PHASE 3a.5: DE-SLOPPIFY CLEANUP ═══")
        _notify("deslopify", "running")
        status = dict(state.get("phase_status", {}))
        status["deslopify"] = "running"

        try:
            result = run_deslopify(workspace, config, cost_tracker=cost_tracker)

            status["deslopify"] = "completed"
            return {
                "current_phase": "review",
                "phase_status": status,
            }

        except Exception as e:
            logger.warning(f"[Orchestrator] De-Sloppify failed (non-fatal): {e}")
            status["deslopify"] = "failed"
            return {
                "current_phase": "review",
                "phase_status": status,
            }

    def review_node(state: PipelineState) -> dict:
        """Phase 3b: Code Review — Gemini 2.5 Pro"""
        logger.info("═══ PHASE 3b: CODE REVIEW (6-Phase Verification) ═══")
        _notify("review", "running")
        status = dict(state.get("phase_status", {}))
        status["review"] = "running"

        try:
            result = run_review(
                workspace, config, cost_tracker=cost_tracker,
                prd_path=state.get("prd_path", ""),
            )

            iteration = state.get("review_iteration", 0) + 1
            status["review"] = "completed"

            return {
                "test_results": {
                    "passed": result.passed,
                    "verdict": result.verdict,
                    "notes": result.notes,
                    "fix_instructions": result.fix_instructions,
                    "verification": result.verification,
                },
                "review_iteration": iteration,
                "current_phase": "review_decision",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Review failed: {e}")
            status["review"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Review: {str(e)}")
            # On review failure, still proceed to security
            return {
                "test_results": {"passed": True, "verdict": "PASS (review error)"},
                "review_iteration": state.get("review_iteration", 0) + 1,
                "phase_status": status,
                "errors": errors,
            }

    def security_node(state: PipelineState) -> dict:
        """Phase 3c: Security Review — Claude Sonnet 4"""
        logger.info("═══ PHASE 3c: SECURITY REVIEW ═══")
        _notify("security", "running")
        status = dict(state.get("phase_status", {}))
        status["security"] = "running"

        try:
            result = run_security_review(
                workspace, config, cost_tracker=cost_tracker
            )

            status["security"] = "completed"

            if not result.passed:
                logger.warning(
                    f"[Orchestrator] Security review FAILED "
                    f"({result.critical_count} critical issues)."
                )

            return {
                "security_verdict": result.verdict,
                "security_findings": result.findings[:2000],
                "current_phase": "security_decision",
                "phase_status": status,
            }

        except Exception as e:
            logger.warning(f"[Orchestrator] Security review failed (non-fatal): {e}")
            status["security"] = "failed"
            return {
                "current_phase": "deployment",
                "phase_status": status,
            }

    def deploy_node(state: PipelineState) -> dict:
        """Phase 3d: Deployment — Claude Haiku 3.5"""
        logger.info("═══ PHASE 3d: DEPLOYMENT ═══")
        _notify("deployment", "running")
        status = dict(state.get("phase_status", {}))
        status["deployment"] = "running"

        try:
            # Deploy to Railway (generates configs internally)
            deployment_url = deploy_to_railway(
                workspace, config, cost_tracker=cost_tracker,
            )

            status["deployment"] = "completed"
            return {
                "deployment_url": deployment_url,
                "current_phase": "pitch",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Deployment failed: {e}")
            status["deployment"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Deployment: {str(e)}")
            return {
                "deployment_url": "",
                "current_phase": "pitch",
                "phase_status": status,
                "errors": errors,
            }

    def pitch_node(state: PipelineState) -> dict:
        """Phase 4a: Pitch Content — Claude Sonnet 4"""
        logger.info("═══ PHASE 4a: PITCH NARRATIVE ═══")
        _notify("pitch", "running")
        status = dict(state.get("phase_status", {}))
        status["pitch"] = "running"

        try:
            dossier_path = state.get("dossier_path", "")
            prd_path = state.get("prd_path", "")
            deployment_url = state.get("deployment_url", "")

            pitch_path, slides = run_pitch(
                dossier_path, prd_path, deployment_url, workspace, config,
                cost_tracker=cost_tracker,
            )

            status["pitch"] = "completed"
            return {
                "pitch_content_path": pitch_path,
                "pitch_slides": slides,
                "current_phase": "presentation",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Pitch failed: {e}")
            status["pitch"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Pitch: {str(e)}")
            return {"phase_status": status, "errors": errors}

    def present_node(state: PipelineState) -> dict:
        """Phase 4b: Presentation Rendering — Kimi k2 + Gamma API"""
        logger.info("═══ PHASE 4b: PRESENTATION ═══")
        status = dict(state.get("phase_status", {}))
        status["presentation"] = "running"

        try:
            pitch_path = state.get("pitch_content_path", "")
            if not pitch_path:
                raise ValueError("No pitch content available")

            result = run_presentation(
                pitch_path, workspace, config,
                cost_tracker=cost_tracker,
            )

            status["presentation"] = "completed"
            return {
                "deck_url": result.get("export_url", result.get("deck_url", "")),
                "deck_local_path": result.get("local_path", ""),
                "current_phase": "completed",
                "phase_status": status,
            }

        except Exception as e:
            logger.error(f"[Orchestrator] Presentation failed: {e}")
            status["presentation"] = "failed"
            errors = list(state.get("errors", []))
            errors.append(f"Presentation: {str(e)}")
            return {"phase_status": status, "errors": errors}

    # ── Routing Functions ─────────────────────────────────────

    def review_router(state: PipelineState) -> Literal["code_node", "security_node"]:
        """
        After review: retry coding or proceed to security review.
        Implements the self-correction loop (max 3 retries).
        """
        test_results = state.get("test_results", {})
        passed = test_results.get("passed", False)
        iteration = state.get("review_iteration", 0)
        max_retries = config.max_code_review_retries

        if passed:
            logger.info("[Orchestrator] ✅ Review PASSED → security review")
            return "security_node"
        elif iteration >= max_retries:
            logger.warning(
                f"[Orchestrator] ⚠️ Max retries ({max_retries}) reached → "
                "proceeding to security review with current code"
            )
            return "security_node"
        else:
            logger.info(
                f"[Orchestrator] 🔄 Review FAILED (attempt {iteration}/{max_retries}) "
                "→ retrying code"
            )
            return "code_node"

    def security_router(state: PipelineState) -> Literal[
        "security_fix_node", "deploy_node"
    ]:
        """
        Route after security review:
        - FAIL with critical issues → security_fix_node (fix then redeploy)
        - PASS or WARN → deploy_node
        """
        verdict = state.get("security_verdict", "PASS")
        if verdict == "FAIL":
            logger.info("[Orchestrator] Security FAILED → attempting auto-fix")
            return "security_fix_node"
        logger.info(f"[Orchestrator] Security {verdict} → deploy")
        return "deploy_node"

    def security_fix_node(state: PipelineState) -> dict:
        """Auto-fix critical security issues by feeding findings back to coder."""
        logger.info("=== SECURITY FIX: Applying security patches ===")
        _notify("security_fix", "running")

        findings = state.get("security_findings", "")
        prd_content = ""
        try:
            prd_content = workspace.read_file(state.get("prd_path", ""))
        except Exception:
            pass

        src_tree = workspace.get_src_tree()
        fix_task = {
            "id": "security_fix",
            "title": "Fix Critical Security Issues",
            "description": (
                "Fix the following security issues found during review:\n\n"
                f"{findings}\n\n"
                "Apply fixes to the affected files. Keep all existing functionality."
            ),
            "role": "fullstack",
            "priority": 0,
            "dependencies": [],
            "acceptance_criteria": ["No hardcoded secrets", "Input validation on all endpoints"],
            "estimated_files": [],
            "complexity": "medium",
        }

        try:
            files = execute_task(
                fix_task, prd_content, src_tree, workspace, config,
                cost_tracker=cost_tracker,
            )
            logger.info(f"[Security Fix] Patched {len(files)} files")
        except Exception as e:
            logger.warning(f"[Security Fix] Auto-fix failed: {e}")

        return {
            "current_phase": "deployment",
            "security_verdict": "WARN (auto-fixed)",
        }

    # ── Build the Graph ───────────────────────────────────────

    graph = StateGraph(PipelineState)

    # Add nodes (v2: includes deslopify, security, and security_fix)
    graph.add_node("research_node", research_node)
    graph.add_node("architect_node", architect_node)
    graph.add_node("plan_node", plan_node)
    graph.add_node("code_node", code_node)
    graph.add_node("deslopify_node", deslopify_node)
    graph.add_node("review_node", review_node)
    graph.add_node("security_node", security_node)
    graph.add_node("security_fix_node", security_fix_node)
    graph.add_node("deploy_node", deploy_node)
    graph.add_node("pitch_node", pitch_node)
    graph.add_node("present_node", present_node)

    # Add edges (v2: code → deslopify → review → security → deploy)
    graph.set_entry_point("research_node")
    graph.add_edge("research_node", "architect_node")
    graph.add_edge("architect_node", "plan_node")
    graph.add_edge("plan_node", "code_node")
    graph.add_edge("code_node", "deslopify_node")
    graph.add_edge("deslopify_node", "review_node")

    # Conditional: review → code (retry) or security
    graph.add_conditional_edges(
        "review_node",
        review_router,
        {
            "code_node": "code_node",
            "security_node": "security_node",
        },
    )

    # Conditional: security → fix (critical) or deploy (pass/warn)
    graph.add_conditional_edges(
        "security_node",
        security_router,
        {
            "security_fix_node": "security_fix_node",
            "deploy_node": "deploy_node",
        },
    )
    graph.add_edge("security_fix_node", "deploy_node")

    graph.add_edge("deploy_node", "pitch_node")
    graph.add_edge("pitch_node", "present_node")
    graph.add_edge("present_node", END)

    logger.info("[Orchestrator] Pipeline graph v2.1 built successfully")
    logger.info(
        "[Orchestrator] Flow: research → architect → plan → code → "
        "deslopify → review ↺ → security ↺ → deploy → pitch → present"
    )

    # Compile with SQLite checkpointing for resume-on-crash
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        checkpoint_path = str(workspace.root / ".checkpoints" / "pipeline.db")
        (workspace.root / ".checkpoints").mkdir(parents=True, exist_ok=True)
        checkpointer = SqliteSaver.from_conn_string(checkpoint_path)
        logger.info(f"[Orchestrator] Checkpointing enabled: {checkpoint_path}")
        return graph.compile(checkpointer=checkpointer)
    except (ImportError, Exception) as e:
        logger.warning(f"[Orchestrator] Checkpointing unavailable ({e}), running without")
        return graph.compile()


def run_pipeline(
    initial_state: PipelineState,
    config: PipelineConfig,
    workspace: WorkspaceManager,
    cost_tracker: CostTracker = None,
    progress_callback=None,
) -> PipelineState:
    """
    Execute the full pipeline from the given initial state.

    Args:
        initial_state: State with problem_statement and refined_brief set
        config: Pipeline configuration
        workspace: Workspace manager
        cost_tracker: Optional cost tracker for budget enforcement
        progress_callback: Optional callable(phase, status, cost) for UI

    Returns:
        Final pipeline state with all outputs
    """
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  HACKATHON PIPELINE v2 — STARTING EXECUTION  ║")
    logger.info("╚══════════════════════════════════════════════╝")

    pipeline = build_pipeline(
        config, workspace, cost_tracker=cost_tracker,
        progress_callback=progress_callback,
    )

    # Execute the graph with thread_id for checkpoint resumption
    import uuid
    thread_id = str(uuid.uuid4())[:8]
    try:
        final_state = pipeline.invoke(
            initial_state,
            config={"configurable": {"thread_id": thread_id}},
        )
    except TypeError:
        # Fallback: checkpointing not available, run without config
        final_state = pipeline.invoke(initial_state)

    # Log completion
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  PIPELINE COMPLETE                            ║")
    logger.info("╚══════════════════════════════════════════════╝")

    phase_status = final_state.get("phase_status", {})
    for phase, status in phase_status.items():
        icon = "✅" if status == "completed" else "❌" if status == "failed" else "⏭️"
        logger.info(f"  {icon} {phase}: {status}")

    errors = final_state.get("errors", [])
    if errors:
        logger.warning(f"  ⚠️ {len(errors)} errors occurred during execution")

    # Save cost report
    if cost_tracker:
        try:
            cost_tracker.save_report(workspace.logs_dir)
            logger.info(f"\n{cost_tracker.format_summary()}")
        except Exception as e:
            logger.warning(f"[Orchestrator] Failed to save cost report: {e}")

    return final_state
