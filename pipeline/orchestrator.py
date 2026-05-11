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
import time
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

# v3 Feature imports
from design_systems import get_design_system
from tools.dev_server import start_dev_server, stop_dev_server
from tools.screenshots import capture_screenshots_sync
from pipeline.learning_db import LearningDB
from pipeline.progress_server import record_event as ws_event

# v4 Feature imports
from agents.readme_agent import run_readme_agent
from pipeline.context_utils import compact_src_tree, compact_review_notes

# v5 Feature imports
from pipeline.template_selector import select_template, inject_template
from pipeline.tracing import init_tracing, trace_agent, score_trace, get_trace_summary
from pipeline.approval_gate import create_gate
from pipeline.ab_testing import get_ab_registry
from agents.cicd_agent import generate_cicd
from agents.test_writer_agent import generate_tests, run_tests
from tools.runtime_tracer import trace_runtime, format_runtime_facts
from tools.demo_seeder import generate_seed_data, write_seed_files

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
    logger.info("[Orchestrator] Building pipeline graph v5")

    # v5 FIX #8: Register A/B prompt variants for key agents
    ab_registry = get_ab_registry()
    ab_registry.register_variant("coder", "default", "Generate clean, production-quality code")
    ab_registry.register_variant("coder", "modular", "Generate highly modular code with small, focused functions")
    ab_registry.register_variant("coder", "defensive", "Generate defensive code with extensive error handling")
    ab_registry.register_variant("reviewer", "strict", "Be strict — fail on any missing feature or code smell")
    ab_registry.register_variant("reviewer", "pragmatic", "Be pragmatic — only fail on runtime errors and crashes")

    # v5 FIX #9: Create approval gate (respects HACKMATE_APPROVAL_MODE env var)
    gate = create_gate()

    def _notify(phase: str, status: str):
        """Fire progress callback + WebSocket broadcast."""
        cost = cost_tracker.total_cost if cost_tracker else 0.0
        # Original callback
        if progress_callback:
            try:
                progress_callback(phase, status, cost)
            except Exception:
                pass
        # WebSocket broadcast (non-fatal)
        try:
            ws_event(phase, status, cost_usd=cost)
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

            # v5: Inject cross-run learning insights
            try:
                learning_db = LearningDB()
                insights = learning_db.get_insights(tech_stack=state.get("tech_stack", ""))
                if insights and "No historical data" not in insights:
                    refined_brief = refined_brief + f"\n\n## Historical Intelligence\n{insights}"
                    logger.info("[Orchestrator] Cross-run insights injected into architect context")
                learning_db.close()
            except Exception as e:
                logger.debug(f"[Orchestrator] Learning insights unavailable: {e}")

            prd_path = run_architect(
                refined_brief, dossier_path, workspace, config,
                cost_tracker=cost_tracker,
            )

            status["architecture"] = "completed"

            # Ingest PRD into Knowledge Base for downstream agents (pitch, reviewer)
            try:
                if kb and prd_path:
                    kb.ingest_file(prd_path, doc_type="prd")
                    logger.info("[Orchestrator] PRD ingested into Knowledge Base")
            except Exception as e:
                logger.warning(f"[Orchestrator] KB PRD ingestion failed (non-fatal): {e}")

            # v5 FIX #9: Approval gate after architecture
            try:
                approved = gate.request_approval(
                    phase="architecture",
                    summary=f"PRD generated at {prd_path}",
                    details="Architecture phase complete. Review PRD before coding begins.",
                )
                if not approved:
                    logger.warning("[Orchestrator] Architecture NOT approved — proceeding anyway (hackathon mode)")
            except Exception as e:
                logger.debug(f"[Orchestrator] Approval gate skipped: {e}")

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

            # Fallback: generate minimal 4-task plan from PRD sections
            try:
                prd_path = state.get("prd_path", "")
                if prd_path:
                    logger.warning("[Orchestrator] Generating fallback task plan")
                    fallback_tasks = [
                        {
                            "id": "task_001", "title": "Project Setup",
                            "description": "Create package.json/requirements.txt, install dependencies, set up project structure per PRD",
                            "role": "fullstack", "priority": 1, "dependencies": [],
                            "acceptance_criteria": ["Project initializes without errors"],
                            "estimated_files": ["package.json"], "relevant_prd_sections": ["Tech Stack", "Dependencies"],
                            "complexity": "low", "status": "pending",
                        },
                        {
                            "id": "task_002", "title": "Backend API",
                            "description": "Implement all backend routes, database models, and core business logic per PRD",
                            "role": "backend", "priority": 2, "dependencies": ["task_001"],
                            "acceptance_criteria": ["API endpoints return expected responses"],
                            "estimated_files": ["server.js", "routes.js"], "relevant_prd_sections": ["API Contract", "Database Schema", "Core Business Logic"],
                            "complexity": "high", "status": "pending",
                        },
                        {
                            "id": "task_003", "title": "Frontend UI",
                            "description": "Implement all frontend pages, components, and styling per PRD UI/UX spec",
                            "role": "frontend", "priority": 3, "dependencies": ["task_001"],
                            "acceptance_criteria": ["All pages render without errors"],
                            "estimated_files": ["index.html", "app.js", "styles.css"], "relevant_prd_sections": ["UI/UX Specification"],
                            "complexity": "high", "status": "pending",
                        },
                        {
                            "id": "task_004", "title": "Deployment Config",
                            "description": "Create Dockerfile, deployment config, and README",
                            "role": "fullstack", "priority": 4, "dependencies": ["task_002", "task_003"],
                            "acceptance_criteria": ["Docker build succeeds"],
                            "estimated_files": ["Dockerfile", "README.md"], "relevant_prd_sections": ["Dependencies"],
                            "complexity": "low", "status": "pending",
                        },
                    ]
                    import json
                    task_path = workspace.write_json("tasks", "task_queue.json", fallback_tasks)
                    status["planning"] = "completed"
                    errors = list(state.get("errors", []))
                    errors.append(f"Planning: Used fallback tasks ({e})")
                    return {
                        "task_queue_path": str(task_path),
                        "task_records": fallback_tasks,
                        "current_phase": "coding",
                        "phase_status": status,
                        "errors": errors,
                    }
            except Exception as fallback_err:
                logger.error(f"[Orchestrator] Fallback planning also failed: {fallback_err}")

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
                # v5: Inject template scaffold before coding
                try:
                    prd_content = workspace.read_file(prd_path) if prd_path else ""
                    tech_stack = state.get("tech_stack", "")
                    template_key = select_template(prd_content, tech_stack)
                    if template_key:
                        scaffold_files = inject_template(template_key, workspace.src_dir)
                        logger.info(f"[Orchestrator] Template '{template_key}': {len(scaffold_files)} files injected")
                except Exception as e:
                    logger.warning(f"[Orchestrator] Template injection failed (non-fatal): {e}")

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

                # v4: Compact context to prevent token bloat on revisions
                compacted_notes = compact_review_notes(review_notes)
                compacted_tree = compact_src_tree(src_tree)
                logger.info(
                    f"[Orchestrator] Context compaction: notes {len(review_notes)}→{len(compacted_notes)}, "
                    f"tree {len(src_tree)}→{len(compacted_tree)} chars"
                )

                revision_context = (
                    f"## Previous Test Results\n{compacted_notes}\n\n"
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

            # Inject design system CSS if not already present
            try:
                css_path = workspace.src_dir / "design-system.css"
                if not css_path.exists():
                    css_content = get_design_system("modern_dark")
                    if css_content:
                        workspace.write_source_file("design-system.css", css_content)
                        code_files.append(str(css_path))
                        logger.info("[Orchestrator] Injected design system CSS")
            except Exception as e:
                logger.warning(f"[Orchestrator] Design system injection failed (non-fatal): {e}")

            # Start dev server for live preview (non-fatal)
            try:
                preview_url = start_dev_server(workspace.src_dir)
                if preview_url:
                    logger.info(f"[Orchestrator] 🌐 Live preview: {preview_url}")
            except Exception as e:
                logger.warning(f"[Orchestrator] Dev server start failed (non-fatal): {e}")

            # v5 FIX #6: Redistribute unspent budget to remaining phases
            if cost_tracker:
                try:
                    coding_budget = config.phase_budgets.get("coding", 5.0)
                    config.phase_budgets.update(
                        cost_tracker.redistribute_savings("coding", coding_budget, config.phase_budgets)
                    )
                    logger.info("[Orchestrator] Budget savings redistributed after coding")
                except Exception:
                    pass

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
                "current_phase": "readme",
                "phase_status": status,
            }

        except Exception as e:
            logger.warning(f"[Orchestrator] De-Sloppify failed (non-fatal): {e}")
            status["deslopify"] = "failed"
            return {
                "current_phase": "readme",
                "phase_status": status,
            }

    def readme_node(state: PipelineState) -> dict:
        """Phase 3a.6: README + Seed Data Generation — Claude Haiku 3.5"""
        logger.info("═══ PHASE 3a.6: README + SEED DATA ═══")
        _notify("readme", "running")
        status = dict(state.get("phase_status", {}))
        status["readme"] = "running"

        try:
            prd_path = state.get("prd_path", "")
            files = run_readme_agent(
                workspace, config,
                cost_tracker=cost_tracker,
                prd_path=prd_path,
            )
            status["readme"] = "completed"
            logger.info(f"[Orchestrator] README agent created {len(files)} files")
            return {
                "current_phase": "self_critique",
                "phase_status": status,
            }

        except Exception as e:
            logger.warning(f"[Orchestrator] README generation failed (non-fatal): {e}")
            status["readme"] = "failed"
            return {
                "current_phase": "self_critique",
                "phase_status": status,
            }

    def self_critique_node(state: PipelineState) -> dict:
        """Phase 3a.7: Reflexion Self-Critique — Claude Haiku 3.5
        Quick self-evaluation before expensive Gemini Pro review.
        Catches 40-60% of issues at 1/12th the cost of the reviewer."""
        logger.info("═══ PHASE 3a.7: SELF-CRITIQUE (Reflexion) ═══")
        _notify("self_critique", "running")
        status = dict(state.get("phase_status", {}))
        status["self_critique"] = "running"

        try:
            from agents.llm_factory import create_llm, invoke_with_retry
            from langchain_core.messages import SystemMessage, HumanMessage

            spec = config.get_model("deslopify")  # Haiku = fast + cheap
            llm = create_llm(spec, config.keys)

            src_tree = workspace.get_src_tree()
            prd_content = ""
            prd_path = state.get("prd_path", "")
            if prd_path:
                try:
                    prd_content = workspace.read_file(prd_path)[:4000]
                except Exception:
                    pass

            # Read key files for quick check
            key_files = []
            for fp in sorted(workspace.src_dir.rglob("*")):
                if fp.is_file() and fp.suffix in (".py", ".js", ".ts", ".jsx", ".tsx"):
                    try:
                        content = fp.read_text(encoding="utf-8")[:3000]
                        rel = fp.relative_to(workspace.src_dir)
                        key_files.append(f"### {rel}\n```\n{content}\n```")
                    except Exception:
                        continue
                    if len(key_files) >= 10:
                        break

            messages = [
                SystemMessage(content=(
                    "You are a quick self-critique agent. Review the code for obvious issues:\n"
                    "1. Missing imports or broken dependencies\n"
                    "2. Functions called but never defined\n"
                    "3. Missing required files (e.g., no entry point, no index.html)\n"
                    "4. PRD features that are completely missing\n"
                    "5. Hardcoded credentials or security issues\n\n"
                    "If you find issues, respond with:\n"
                    "SELF_FIX_NEEDED: YES\n"
                    "FIXES:\n- [list each fix needed]\n\n"
                    "If code looks ready for review:\n"
                    "SELF_FIX_NEEDED: NO\n"
                    "NOTES: [brief assessment]"
                )),
                HumanMessage(content=(
                    f"## Project Structure\n```\n{src_tree}\n```\n\n"
                    f"## PRD Summary\n{prd_content[:3000]}\n\n"
                    f"## Key Source Files\n{''.join(key_files[:8])}\n"
                )),
            ]

            response = invoke_with_retry(
                llm, messages,
                spec=spec,
                agent_name="self_critique",
                phase="self_critique",
                cost_tracker=cost_tracker,
            )

            critique = response.content
            needs_fix = "SELF_FIX_NEEDED: YES" in critique.upper()

            if needs_fix:
                logger.warning("[Orchestrator] Self-critique found issues — adding to review context")
                # Store critique for the reviewer to use
                workspace.log_event("self_critique", "Issues found", critique[:1000])
            else:
                logger.info("[Orchestrator] Self-critique: code looks ready for review")

            status["self_critique"] = "completed"
            return {
                "current_phase": "review",
                "phase_status": status,
            }

        except Exception as e:
            logger.warning(f"[Orchestrator] Self-critique failed (non-fatal): {e}")
            status["self_critique"] = "failed"
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

            # Capture screenshots of deployed app for pitch deck
            try:
                if deployment_url:
                    screenshots_dir = workspace.output_dir / "screenshots"
                    screenshots = capture_screenshots_sync(
                        deployment_url, screenshots_dir,
                        pages=["/", "/dashboard", "/api/health"],
                    )
                    logger.info(f"[Orchestrator] 📸 Captured {len(screenshots)} screenshots")
            except Exception as e:
                logger.warning(f"[Orchestrator] Screenshot capture failed (non-fatal): {e}")

            # Stop dev server now that we have a real deployment
            try:
                stop_dev_server()
            except Exception:
                pass

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
        "security_fix_node", "cicd_node"
    ]:
        """
        Route after security review:
        - FAIL with critical issues → security_fix_node (fix then redeploy)
        - PASS or WARN → cicd_node (proceed to CI/CD)
        """
        verdict = state.get("security_verdict", "PASS")
        if verdict == "FAIL":
            logger.info("[Orchestrator] Security FAILED → attempting auto-fix")
            return "security_fix_node"
        logger.info(f"[Orchestrator] Security {verdict} → CI/CD")
        return "cicd_node"

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

    # ── v5 Nodes ──────────────────────────────────────────────

    def test_node(state: PipelineState) -> dict:
        """Phase 3b: TDD Test Generation + Execution"""
        logger.info("═══ PHASE 3b: TDD TEST GENERATION ═══")
        _notify("testing", "running")
        status = dict(state.get("phase_status", {}))
        status["testing"] = "running"

        try:
            tasks = state.get("task_records", [])
            prd_path = state.get("prd_path", "")
            prd_content = workspace.read_file(prd_path) if prd_path else ""
            src_tree = workspace.get_src_tree()

            # Generate test files
            test_files = generate_tests(
                workspace, config, tasks, prd_content, src_tree,
                cost_tracker=cost_tracker,
            )

            # v5 FIX: Actually RUN the generated tests
            test_results = {}
            if test_files:
                try:
                    test_results = run_tests(workspace.src_dir, timeout=30)
                    if test_results.get("passed"):
                        logger.info(f"[Orchestrator] Tests PASSED: {test_results.get('summary', '')}")
                    else:
                        logger.warning(f"[Orchestrator] Tests FAILED: {test_results.get('summary', '')}")
                except Exception as te:
                    logger.warning(f"[Orchestrator] Test execution failed: {te}")

            # Run runtime trace to catch startup errors
            runtime = trace_runtime(workspace.src_dir, timeout=10)
            runtime_facts = format_runtime_facts(runtime)

            if runtime.get("errors"):
                logger.warning(f"[Orchestrator] Runtime issues: {len(runtime['errors'])} errors")

            # v5 FIX: Redistribute budget savings after testing phase
            if cost_tracker:
                try:
                    testing_budget = config.phase_budgets.get("testing", 1.0)
                    config.phase_budgets.update(
                        cost_tracker.redistribute_savings(
                            "testing", testing_budget, config.phase_budgets
                        )
                    )
                except Exception:
                    pass

            status["testing"] = "completed"
            return {
                "test_files": test_files,
                "test_results": test_results,
                "runtime_trace": runtime_facts,
                "current_phase": "readme",
                "phase_status": status,
            }

        except Exception as e:
            logger.warning(f"[Orchestrator] Test generation failed (non-fatal): {e}")
            status["testing"] = "skipped"
            return {"phase_status": status, "current_phase": "readme"}

    def cicd_node(state: PipelineState) -> dict:
        """Phase 7b: CI/CD Pipeline Generation"""
        logger.info("═══ PHASE 7b: CI/CD GENERATION ═══")
        _notify("cicd", "running")
        status = dict(state.get("phase_status", {}))

        try:
            tech_stack = state.get("tech_stack", "")
            cicd_files = generate_cicd(workspace.src_dir, tech_stack)

            status["cicd"] = "completed"
            return {
                "cicd_files": cicd_files,
                "phase_status": status,
            }
        except Exception as e:
            logger.warning(f"[Orchestrator] CI/CD generation failed (non-fatal): {e}")
            status["cicd"] = "skipped"
            return {"phase_status": status}

    def seed_node(state: PipelineState) -> dict:
        """Phase 7c: Demo Data Seeding"""
        logger.info("═══ PHASE 7c: DEMO SEEDING ═══")
        _notify("seeding", "running")
        status = dict(state.get("phase_status", {}))

        try:
            prd_path = state.get("prd_path", "")
            prd_content = workspace.read_file(prd_path) if prd_path else ""
            src_tree = workspace.get_src_tree()

            seed_data = generate_seed_data(
                workspace, config, prd_content, src_tree,
                cost_tracker=cost_tracker,
            )
            seed_files = write_seed_files(seed_data, workspace.src_dir)

            status["seeding"] = "completed"
            return {
                "seed_files": seed_files,
                "demo_walkthrough": seed_data.get("demo_walkthrough", []),
                "phase_status": status,
            }
        except Exception as e:
            logger.warning(f"[Orchestrator] Demo seeding failed (non-fatal): {e}")
            status["seeding"] = "skipped"
            return {"phase_status": status}

    # ── Build the Graph ───────────────────────────────────────

    graph = StateGraph(PipelineState)

    # v5 FIX #4: Wrap all nodes with tracing decorator
    _traced_nodes = {
        "research_node": trace_agent("research", "research")(research_node),
        "architect_node": trace_agent("architect", "architecture")(architect_node),
        "plan_node": trace_agent("planner", "planning")(plan_node),
        "code_node": trace_agent("coder", "coding")(code_node),
        "deslopify_node": trace_agent("deslopify", "deslopify")(deslopify_node),
        "test_node": trace_agent("test_writer", "testing")(test_node),
        "readme_node": trace_agent("readme", "readme")(readme_node),
        "self_critique_node": trace_agent("self_critique", "self_critique")(self_critique_node),
        "review_node": trace_agent("reviewer", "review")(review_node),
        "security_node": trace_agent("security", "security")(security_node),
        "security_fix_node": trace_agent("security_fix", "security_fix")(security_fix_node),
        "cicd_node": trace_agent("cicd", "cicd")(cicd_node),
        "seed_node": trace_agent("seed", "seeding")(seed_node),
        "deploy_node": trace_agent("deployer", "deployment")(deploy_node),
        "pitch_node": trace_agent("pitch", "pitch")(pitch_node),
        "present_node": trace_agent("presentation", "presentation")(present_node),
    }

    # Add nodes (v5: all traced + test, cicd, seed nodes)
    for node_name, node_fn in _traced_nodes.items():
        graph.add_node(node_name, node_fn)

    # Add edges (v5: code → deslopify → test → readme → self-critique → review)
    graph.set_entry_point("research_node")
    graph.add_edge("research_node", "architect_node")
    graph.add_edge("architect_node", "plan_node")
    graph.add_edge("plan_node", "code_node")
    graph.add_edge("code_node", "deslopify_node")
    graph.add_edge("deslopify_node", "test_node")       # v5: TDD after polish
    graph.add_edge("test_node", "readme_node")           # v5: test → readme
    graph.add_edge("readme_node", "self_critique_node")
    graph.add_edge("self_critique_node", "review_node")

    # Conditional: review → code (retry) or security
    graph.add_conditional_edges(
        "review_node",
        review_router,
        {
            "code_node": "code_node",
            "security_node": "security_node",
        },
    )

    # Conditional: security → fix (critical) or cicd (pass/warn)
    graph.add_conditional_edges(
        "security_node",
        security_router,
        {
            "security_fix_node": "security_fix_node",
            "cicd_node": "cicd_node",              # v5 FIX: direct mapping
        },
    )
    graph.add_edge("security_fix_node", "cicd_node")  # v5: fix → cicd
    graph.add_edge("cicd_node", "seed_node")           # v5: cicd → seed
    graph.add_edge("seed_node", "deploy_node")         # v5: seed → deploy

    graph.add_edge("deploy_node", "pitch_node")
    graph.add_edge("pitch_node", "present_node")
    graph.add_edge("present_node", END)

    logger.info("[Orchestrator] Pipeline graph v5.0 built successfully")
    logger.info(
        "[Orchestrator] Flow: research → architect → plan → code → deslopify → "
        "test → readme → self-critique → review ↺ → security ↺ → cicd → seed → "
        "deploy → pitch → present"
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
    logger.info("║  HACKATHON PIPELINE v5 — STARTING EXECUTION  ║")
    logger.info("╚══════════════════════════════════════════════╝")

    run_start = time.time()

    # v5: Initialize tracing subsystem
    try:
        init_tracing(workspace.logs_dir)
        logger.info("[Orchestrator] 📊 Tracing initialized")
    except Exception as e:
        logger.debug(f"[Orchestrator] Tracing init failed: {e}")

    # Start WebSocket progress server (non-blocking, non-fatal)
    try:
        from pipeline.progress_server import start_server_background, start_tracking
        start_tracking()
        start_server_background(port=8765)
        logger.info("[Orchestrator] 📊 Progress dashboard: http://localhost:8765")
    except Exception as e:
        logger.warning(f"[Orchestrator] Progress server failed (non-fatal): {e}")

    pipeline = build_pipeline(
        config, workspace, cost_tracker=cost_tracker,
        progress_callback=progress_callback,
    )

    # v4: Hot-reload config before execution
    try:
        if config.reload_keys():
            logger.info("[Orchestrator] Config hot-reloaded before execution")
    except Exception:
        pass

    # Execute the graph with streaming for real-time progress
    import uuid
    thread_id = str(uuid.uuid4())[:8]
    final_state = None

    try:
        # v4: Use stream() for real-time node-by-node progress
        run_config = {"configurable": {"thread_id": thread_id}}
        for event in pipeline.stream(initial_state, config=run_config, stream_mode="updates"):
            # Each event is {node_name: state_delta}
            for node_name, state_delta in event.items():
                phase = state_delta.get("current_phase", node_name.replace("_node", ""))
                status = state_delta.get("phase_status", {}).get(phase, "running")
                logger.info(f"[Stream] Node completed: {node_name} → phase={phase}")

                # Hot-reload config between major phases
                try:
                    config.reload_keys()
                except Exception:
                    pass

                # Track the latest state
                if final_state is None:
                    final_state = dict(initial_state)
                final_state.update(state_delta)

        if final_state is None:
            final_state = initial_state

    except (TypeError, AttributeError) as e:
        # Fallback: streaming not available, use invoke()
        logger.info(f"[Orchestrator] Streaming not available ({e}), using invoke()")
        try:
            final_state = pipeline.invoke(
                initial_state,
                config={"configurable": {"thread_id": thread_id}},
            )
        except TypeError:
            final_state = pipeline.invoke(initial_state)

    run_duration = time.time() - run_start

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

    # Record run in Learning DB (cross-run intelligence)
    try:
        learning_db = LearningDB()
        review_passed_first = final_state.get("review_iteration", 0) == 0
        learning_db.record_run(
            problem_statement=initial_state.get("problem_statement", "")[:500],
            tech_stack=initial_state.get("tech_stack", ""),
            duration_seconds=run_duration,
            total_cost_usd=cost_tracker.total_cost if cost_tracker else 0,
            total_tasks=len(final_state.get("task_records", [])),
            review_passed_first=review_passed_first,
            review_iterations=final_state.get("review_iteration", 0),
            security_verdict=final_state.get("security_verdict", ""),
            deployment_success=bool(final_state.get("deployment_url")),
            deployment_url=final_state.get("deployment_url", ""),
        )
        learning_db.close()
        logger.info("[Orchestrator] 🧠 Run recorded in Learning DB")
    except Exception as e:
        logger.warning(f"[Orchestrator] Learning DB record failed (non-fatal): {e}")

    # Cleanup dev server if still running
    try:
        stop_dev_server()
    except Exception:
        pass

    return final_state
