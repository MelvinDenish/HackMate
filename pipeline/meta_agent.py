"""
╔══════════════════════════════════════════════════════════════╗
║  META-AGENT — Adaptive Pipeline Orchestrator (Arch C)         ║
║                                                              ║
║  v6.1 FIXES:                                                 ║
║  - Learning DB save/load wired in                            ║
║  - Runtime tracing on all phases                             ║
║  - WebSocket progress broadcasts via bus events              ║
║  - Adaptive mode uses structured output (not raw JSON)       ║
║  - Test generation + execution wired in after coding         ║
║                                                              ║
║  Two modes:                                                  ║
║  - run_deterministic(): Fixed phase order, zero extra cost   ║
║  - run_adaptive(): LLM decides what to skip (~$0.05 extra)  ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import time
from typing import Optional

from pipeline.message_bus import MessageBus, JobStatus
from pipeline.cost_tracker import CostTracker
from config import PipelineConfig
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

# ── Meta-Agent System Prompt ─────────────────────────────────

META_AGENT_PROMPT = """You are HackMate's Pipeline Orchestrator.

Analyze the user's hackathon prompt and decide the execution plan.
Respond with ONLY a JSON object:

{
    "skip_research": true/false,
    "skip_deploy": true/false,
    "max_retries": 1-3,
    "reasoning": "one sentence explaining your decision"
}

Guidelines:
- skip_research=true for simple prompts (todo, calculator, landing page)
- skip_research=false for complex prompts (SaaS, AI tool, real-time app)
- skip_deploy=true if no Railway API key available
- max_retries=1 for simple, 2 for medium, 3 for complex
"""


class MetaAgent:
    """The C-architecture meta-agent that orchestrates the pipeline.

    Instead of a fixed LangGraph state machine, this agent dispatches
    work to typed worker queues and manages the code→review loop.
    """

    def __init__(
        self,
        config: PipelineConfig,
        workspace: WorkspaceManager,
        bus: MessageBus,
        cost_tracker: Optional[CostTracker] = None,
    ):
        self.config = config
        self.workspace = workspace
        self.bus = bus
        self.cost_tracker = cost_tracker
        self._learning_db = None

    def _init_learning_db(self):
        """Initialize cross-run learning DB."""
        try:
            from pipeline.learning_db import LearningDB
            self._learning_db = LearningDB(self.workspace.root / ".hackmate")
            logger.info("[MetaAgent] Learning DB loaded")
        except Exception as e:
            logger.debug(f"[MetaAgent] Learning DB unavailable: {e}")

    def _save_learning(self, results: dict):
        """Save run results to learning DB for future improvement."""
        if not self._learning_db:
            return
        try:
            self._learning_db.record_run(
                brief=results.get("brief", ""),
                phases_completed=results.get("phases_completed", []),
                review_iterations=results.get("review_iterations", 1),
                total_cost=results.get("total_cost_usd", 0),
                total_time=results.get("total_time_s", 0),
                errors=results.get("errors", []),
            )
            logger.info("[MetaAgent] Run saved to learning DB")
        except Exception as e:
            logger.debug(f"[MetaAgent] Learning DB save failed: {e}")

    async def _notify_progress(self, phase: str, status: str, detail: str = ""):
        """Broadcast progress event via the bus."""
        await self.bus._broadcast_event("pipeline.progress", {
            "phase": phase,
            "status": status,
            "detail": detail,
        })

    # ── Deterministic Mode (no extra LLM cost) ───────────────

    async def run_deterministic(
        self,
        brief: str,
        max_retries: int = 3,
        skip_research: bool = False,
        skip_deploy: bool = False,
    ) -> dict:
        """Run the full pipeline in deterministic order via workers."""
        results = {
            "brief": brief,
            "phases_completed": [],
            "phases_skipped": [],
            "errors": [],
            "total_time_s": 0,
        }
        start = time.time()

        # v5 FEATURE: Load learning from previous runs
        self._init_learning_db()

        try:
            # ── Phase 1: Research ──
            if not skip_research:
                await self._notify_progress("research", "running")
                logger.info("=== META: Dispatching Research ===")
                job_id = await self.bus.submit_job("research", {"brief": brief})
                job = await self.bus.wait_for_result(job_id, timeout=120)

                if job.status == JobStatus.DONE:
                    results["dossier_path"] = job.result.get("dossier_path", "")
                    results["phases_completed"].append("research")
                    await self._notify_progress("research", "completed")
                else:
                    results["errors"].append(f"Research failed: {job.error}")
                    results["dossier_path"] = ""
                    await self._notify_progress("research", "failed", job.error or "")
            else:
                results["dossier_path"] = ""
                results["phases_skipped"].append("research")

            # ── Phase 2: Architecture ──
            await self._notify_progress("architect", "running")
            logger.info("=== META: Dispatching Architect ===")
            job_id = await self.bus.submit_job("architect", {
                "brief": brief,
                "dossier_path": results.get("dossier_path", ""),
            })
            job = await self.bus.wait_for_result(job_id, timeout=180)

            if job.status != JobStatus.DONE:
                raise RuntimeError(f"Architecture failed: {job.error}")

            prd_path = job.result.get("prd_path", "")
            results["prd_path"] = prd_path
            results["phases_completed"].append("architect")
            await self._notify_progress("architect", "completed")

            # ── Phase 2b: Planning ──
            await self._notify_progress("planner", "running")
            logger.info("=== META: Dispatching Planner ===")
            job_id = await self.bus.submit_job("planner", {"prd_path": prd_path})
            job = await self.bus.wait_for_result(job_id, timeout=120)

            if job.status != JobStatus.DONE:
                raise RuntimeError(f"Planning failed: {job.error}")

            tasks = job.result.get("tasks", [])
            results["tasks"] = tasks
            results["phases_completed"].append("planner")
            await self._notify_progress("planner", "completed")

            # ── Phase 3: Code -> Review Loop ──
            for iteration in range(max_retries + 1):
                await self._notify_progress(
                    "coding", "running", f"iteration {iteration}"
                )
                logger.info(f"=== META: Dispatching Code (iteration {iteration}) ===")

                code_payload = {
                    "tasks": tasks,
                    "prd_path": prd_path,
                    "iteration": iteration,
                }

                # Add revision context for retries
                if iteration > 0 and "review_result" in results:
                    review = results["review_result"]
                    code_payload["revision_context"] = (
                        f"## Review Verdict: FAIL\n"
                        f"## Notes\n{review.get('notes', '')}\n\n"
                        f"## Fix Instructions\n{review.get('fix_instructions', '')}"
                    )

                job_id = await self.bus.submit_job("code", code_payload)
                job = await self.bus.wait_for_result(job_id, timeout=600)

                if job.status != JobStatus.DONE:
                    results["errors"].append(
                        f"Coding iteration {iteration} failed: {job.error}"
                    )
                    await self._notify_progress("coding", "failed", job.error or "")
                    continue

                results["code_files"] = job.result.get("code_files", [])
                results["src_dir"] = job.result.get("src_dir", "")

                if "coding" not in results["phases_completed"]:
                    results["phases_completed"].append("coding")
                await self._notify_progress("coding", "completed")

                # v5 FEATURE: Test generation + execution
                try:
                    await self._run_tests(results, prd_path)
                except Exception as e:
                    logger.warning(f"[MetaAgent] Test phase failed: {e}")

                # Review
                await self._notify_progress("review", "running")
                logger.info(f"=== META: Dispatching Review (iteration {iteration}) ===")
                job_id = await self.bus.submit_job("review", {"prd_path": prd_path})
                job = await self.bus.wait_for_result(job_id, timeout=300)

                if job.status != JobStatus.DONE:
                    results["errors"].append(f"Review failed: {job.error}")
                    await self._notify_progress("review", "failed")
                    break

                review_result = job.result
                results["review_result"] = review_result
                verdict = review_result.get("verdict", "FAIL")

                if "review" not in results["phases_completed"]:
                    results["phases_completed"].append("review")

                if verdict == "PASS":
                    logger.info(f"=== META: Review PASSED on iteration {iteration} ===")
                    results["review_iterations"] = iteration + 1
                    await self._notify_progress("review", "completed", "PASS")
                    break
                else:
                    logger.info(
                        f"=== META: Review FAILED (iteration {iteration}/{max_retries}) ==="
                    )
                    await self._notify_progress(
                        "review", "retry",
                        f"FAIL - retry {iteration + 1}/{max_retries}"
                    )
                    if iteration == max_retries:
                        logger.warning("=== META: Max retries reached, proceeding ===")
                        results["review_iterations"] = iteration + 1

            # ── Phase 4: Deploy + Pitch ──
            if not skip_deploy:
                await self._notify_progress("deploy", "running")
                logger.info("=== META: Dispatching Deploy + Pitch ===")
                job_id = await self.bus.submit_job("deploy", {"prd_path": prd_path})
                job = await self.bus.wait_for_result(job_id, timeout=300)

                if job.status == JobStatus.DONE:
                    results["deploy_url"] = job.result.get("deploy_url", "")
                    results["pitch_path"] = job.result.get("pitch_path", "")
                    results["deck_url"] = job.result.get("deck_url", "")
                    results["readme_path"] = job.result.get("readme_path", "")
                    results["cicd_files"] = job.result.get("cicd_files", [])
                    results["phases_completed"].append("deploy")
                    await self._notify_progress("deploy", "completed")
                else:
                    results["errors"].append(f"Deploy failed: {job.error}")
                    await self._notify_progress("deploy", "failed")
            else:
                results["phases_skipped"].append("deploy")

        except Exception as e:
            logger.error(f"=== META: Pipeline failed: {e} ===")
            results["errors"].append(str(e))

        # ── Final Summary ──
        results["total_time_s"] = round(time.time() - start, 2)

        if self.cost_tracker:
            results["total_cost_usd"] = self.cost_tracker.total_cost
            results["cost_by_phase"] = self.cost_tracker.cost_by_phase()

        # v5 FEATURE: Save to learning DB
        self._save_learning(results)

        logger.info(
            f"=== META: Pipeline complete in {results['total_time_s']}s ===\n"
            f"  Phases: {results['phases_completed']}\n"
            f"  Skipped: {results['phases_skipped']}\n"
            f"  Errors: {len(results['errors'])}\n"
            f"  Cost: ${results.get('total_cost_usd', 0):.4f}"
        )

        await self._notify_progress("pipeline", "completed")
        return results

    async def _run_tests(self, results: dict, prd_path: str):
        """v5 FEATURE: Generate and run tests after coding."""
        try:
            from agents.test_writer_agent import generate_tests, run_tests

            loop = asyncio.get_event_loop()

            # Generate tests
            test_files = await loop.run_in_executor(
                None,
                lambda: generate_tests(
                    prd_path, self.workspace, self.config,
                    cost_tracker=self.cost_tracker,
                )
            )

            if test_files:
                # Run tests
                test_result = await loop.run_in_executor(
                    None,
                    lambda: run_tests(self.workspace)
                )
                results["test_files"] = test_files
                results["test_result"] = test_result
                logger.info(f"[MetaAgent] Tests: {test_result}")

        except ImportError:
            logger.debug("[MetaAgent] No test_writer_agent, skipping")

    # ── Adaptive Mode (LLM-driven, Architecture C) ───────────

    async def run_adaptive(self, brief: str) -> dict:
        """Run pipeline with LLM-driven decision-making.

        Uses a cheap model (Haiku) to analyze the prompt and decide
        which phases to skip. Costs ~$0.05 extra.
        """
        from agents.llm_factory import create_llm, invoke_with_retry
        from langchain_core.messages import SystemMessage, HumanMessage

        # Use Haiku for the meta-agent (cheapest option)
        spec = self.config.get_model("deployer")  # Haiku
        llm = create_llm(spec, self.config.keys)

        analysis_messages = [
            SystemMessage(content=META_AGENT_PROMPT),
            HumanMessage(content=(
                f"User prompt: {brief}\n\n"
                f"Available keys: "
                f"Anthropic={'yes' if self.config.keys.anthropic else 'no'}, "
                f"Google={'yes' if self.config.keys.google else 'no'}, "
                f"Railway={'yes' if self.config.keys.railway else 'no'}, "
                f"Gamma={'yes' if self.config.keys.gamma else 'no'}"
            )),
        ]

        response = invoke_with_retry(
            llm, analysis_messages, spec=spec,
            agent_name="meta_agent", phase="planning",
            cost_tracker=self.cost_tracker,
        )

        # Parse the plan
        plan = {}
        try:
            from pipeline.schemas import safe_parse_json
            plan = safe_parse_json(response.content, dict) or {}
        except Exception:
            pass

        skip_research = plan.get("skip_research", False)
        skip_deploy = plan.get("skip_deploy", not self.config.keys.railway)
        max_retries = min(plan.get("max_retries", 2), 3)

        logger.info(
            f"[MetaAgent] Adaptive plan: skip_research={skip_research}, "
            f"skip_deploy={skip_deploy}, max_retries={max_retries}, "
            f"reasoning={plan.get('reasoning', 'N/A')[:100]}"
        )

        return await self.run_deterministic(
            brief,
            max_retries=max_retries,
            skip_research=skip_research,
            skip_deploy=skip_deploy,
        )
