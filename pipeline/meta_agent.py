"""
╔══════════════════════════════════════════════════════════════╗
║  META-AGENT — Adaptive Pipeline Orchestrator (Arch C)         ║
║                                                              ║
║  Instead of a hardcoded LangGraph state machine, this uses   ║
║  a ReAct-style agent that DECIDES what to do next based on   ║
║  current state.                                              ║
║                                                              ║
║  The meta-agent has tools to:                                ║
║  - dispatch_research(query): Start research                  ║
║  - dispatch_architect(brief, dossier): Generate PRD          ║
║  - dispatch_code(tasks, prd): Start coding                   ║
║  - dispatch_review(prd): Review code                         ║
║  - dispatch_deploy(prd): Deploy + pitch + present            ║
║  - check_status(job_id): Check if a job is done              ║
║  - read_result(job_id): Get job results                      ║
║  - get_cost_report(): Check budget spent so far              ║
║                                                              ║
║  This lets the agent adapt:                                  ║
║  - Skip research for simple prompts                          ║
║  - Retry coding only for failed tasks                        ║
║  - Skip deployment if no Railway key                         ║
║  - Decide when code is "good enough" vs needs more review    ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import time
from typing import Optional

from pipeline.message_bus import MessageBus, JobStatus
from pipeline.cost_tracker import CostTracker
from pipeline.tracing import trace_agent
from config import PipelineConfig
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

# ── Meta-Agent System Prompt ─────────────────────────────────

META_AGENT_PROMPT = """You are HackMate's Pipeline Orchestrator — a meta-agent that builds complete hackathon projects.

## Your Role
You coordinate specialized workers to turn a user's idea into a deployed, polished hackathon submission.
You DECIDE what to do next based on results — you're not following a fixed script.

## Available Workers (via tools)
1. **research** — Web search + competitive analysis → produces a research dossier
2. **architect** — Reads dossier + brief → produces a detailed PRD
3. **planner** — Reads PRD → decomposes into coding tasks with dependencies
4. **code** — Executes coding tasks → produces source files
5. **review** — Reviews code quality + security → PASS/FAIL with notes
6. **deploy** — Deploy to Railway + generate pitch deck + presentation

## Decision Guidelines
- For SIMPLE prompts (todo app, calculator): skip research, go straight to architect
- For COMPLEX prompts (SaaS, AI tool): always do research first
- After review FAIL: re-dispatch code with fix instructions (max 3 retries)
- After review PASS: proceed to deploy
- If budget > 80% spent: skip presentation, just deploy
- If no RAILWAY_API_TOKEN: skip deployment, still do pitch

## Workflow
1. Analyze the user's prompt
2. Dispatch research (if needed)
3. Wait for research → dispatch architect
4. Wait for PRD → dispatch planner
5. Wait for tasks → dispatch code
6. Wait for code → dispatch review
7. If FAIL → dispatch code with fixes (up to 3 retries)
8. If PASS → dispatch deploy
9. Report final results

## Rules
- ALWAYS wait for a job to complete before dispatching the next dependent job
- Use check_status() to poll, or wait_for_result() to block
- Call get_cost_report() periodically to monitor budget
- Log your reasoning for skipping phases

Respond with your plan first, then execute step by step using tools."""


class MetaAgent:
    """The C-architecture meta-agent that orchestrates the pipeline.

    Instead of a fixed LangGraph state machine, this agent uses
    an LLM to decide what to do next based on current state and results.

    For deterministic mode (no extra LLM cost), use run_deterministic().
    For adaptive mode (LLM decides), use run_adaptive().
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
        self._state: dict = {}

    # ── Deterministic Mode (no extra LLM cost) ───────────────
    # This follows a fixed pipeline but uses the bus/worker architecture.
    # Recommended for most runs. Adaptive mode is for complex prompts.

    async def run_deterministic(
        self,
        brief: str,
        max_retries: int = 3,
        skip_research: bool = False,
        skip_deploy: bool = False,
    ) -> dict:
        """Run the full pipeline in deterministic order via workers.

        This gives you Architecture A (workers + bus) without the
        extra cost of Architecture C (LLM decision-making).
        """
        results = {
            "brief": brief,
            "phases_completed": [],
            "phases_skipped": [],
            "errors": [],
            "total_time_s": 0,
        }
        start = time.time()

        try:
            # ── Phase 1: Research ──
            if not skip_research:
                logger.info("═══ META: Dispatching Research ═══")
                job_id = await self.bus.submit_job("research", {"brief": brief})
                job = await self.bus.wait_for_result(job_id, timeout=120)

                if job.status == JobStatus.DONE:
                    results["dossier_path"] = job.result.get("dossier_path", "")
                    results["phases_completed"].append("research")
                else:
                    results["errors"].append(f"Research failed: {job.error}")
                    results["dossier_path"] = ""
            else:
                results["dossier_path"] = ""
                results["phases_skipped"].append("research")

            # ── Phase 2: Architecture ──
            logger.info("═══ META: Dispatching Architect ═══")
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

            # ── Phase 2b: Planning ──
            logger.info("═══ META: Dispatching Planner ═══")
            job_id = await self.bus.submit_job("planner", {"prd_path": prd_path})
            job = await self.bus.wait_for_result(job_id, timeout=120)

            if job.status != JobStatus.DONE:
                raise RuntimeError(f"Planning failed: {job.error}")

            tasks = job.result.get("tasks", [])
            results["tasks"] = tasks
            results["phases_completed"].append("planner")

            # ── Phase 3: Code → Review Loop ──
            for iteration in range(max_retries + 1):
                logger.info(f"═══ META: Dispatching Code (iteration {iteration}) ═══")

                code_payload = {
                    "tasks": tasks,
                    "prd_path": prd_path,
                    "iteration": iteration,
                }

                # Add revision context for retries
                if iteration > 0 and "review_result" in results:
                    code_payload["revision_context"] = (
                        f"## Review Notes\n{results['review_result'].get('notes', '')}\n\n"
                        f"## Fix Instructions\n{results['review_result'].get('fix_instructions', '')}"
                    )

                job_id = await self.bus.submit_job("code", code_payload)
                job = await self.bus.wait_for_result(job_id, timeout=600)

                if job.status != JobStatus.DONE:
                    results["errors"].append(f"Coding iteration {iteration} failed: {job.error}")
                    continue

                results["code_files"] = job.result.get("code_files", [])
                results["src_dir"] = job.result.get("src_dir", "")

                if "coding" not in results["phases_completed"]:
                    results["phases_completed"].append("coding")

                # Review
                logger.info(f"═══ META: Dispatching Review (iteration {iteration}) ═══")
                job_id = await self.bus.submit_job("review", {"prd_path": prd_path})
                job = await self.bus.wait_for_result(job_id, timeout=300)

                if job.status != JobStatus.DONE:
                    results["errors"].append(f"Review failed: {job.error}")
                    break

                review_result = job.result
                results["review_result"] = review_result
                verdict = review_result.get("verdict", "FAIL")

                if "review" not in results["phases_completed"]:
                    results["phases_completed"].append("review")

                if verdict == "PASS":
                    logger.info(f"═══ META: Review PASSED on iteration {iteration} ═══")
                    results["review_iterations"] = iteration + 1
                    break
                else:
                    logger.info(
                        f"═══ META: Review FAILED (iteration {iteration}/{max_retries}) ═══"
                    )
                    if iteration == max_retries:
                        logger.warning("═══ META: Max retries reached, proceeding anyway ═══")
                        results["review_iterations"] = iteration + 1

            # ── Phase 4: Deploy + Pitch ──
            if not skip_deploy:
                logger.info("═══ META: Dispatching Deploy + Pitch ═══")
                job_id = await self.bus.submit_job("deploy", {"prd_path": prd_path})
                job = await self.bus.wait_for_result(job_id, timeout=300)

                if job.status == JobStatus.DONE:
                    results["deploy_url"] = job.result.get("deploy_url", "")
                    results["pitch_path"] = job.result.get("pitch_path", "")
                    results["deck_url"] = job.result.get("deck_url", "")
                    results["phases_completed"].append("deploy")
                else:
                    results["errors"].append(f"Deploy failed: {job.error}")
            else:
                results["phases_skipped"].append("deploy")

        except Exception as e:
            logger.error(f"═══ META: Pipeline failed: {e} ═══")
            results["errors"].append(str(e))

        # ── Final Summary ──
        results["total_time_s"] = round(time.time() - start, 2)

        if self.cost_tracker:
            results["total_cost_usd"] = self.cost_tracker.total_cost
            results["cost_by_phase"] = self.cost_tracker.cost_by_phase()

        logger.info(
            f"═══ META: Pipeline complete in {results['total_time_s']}s ═══\n"
            f"  Phases: {results['phases_completed']}\n"
            f"  Skipped: {results['phases_skipped']}\n"
            f"  Errors: {len(results['errors'])}\n"
            f"  Cost: ${results.get('total_cost_usd', 0):.4f}"
        )

        return results

    # ── Adaptive Mode (LLM-driven, Architecture C) ───────────
    # Uses an LLM to decide what to do next. Costs ~$0.10 extra.

    async def run_adaptive(self, brief: str) -> dict:
        """Run pipeline with LLM-driven decision-making.

        The meta-agent LLM decides:
        - Whether to skip research
        - How many retries for code review
        - Whether to deploy or just pitch
        - When to stop

        Costs ~$0.10 extra for the meta-agent LLM calls.
        Uses claude-haiku-3.5 for cost efficiency.
        """
        from agents.llm_factory import create_llm, invoke_with_retry
        from langchain_core.messages import SystemMessage, HumanMessage

        spec = self.config.get_model("orchestrator")
        llm = create_llm(spec, self.config.keys)

        # Ask meta-agent to analyze the prompt and create a plan
        analysis_messages = [
            SystemMessage(content=META_AGENT_PROMPT),
            HumanMessage(content=(
                f"## User's Hackathon Idea\n{brief}\n\n"
                f"## Available Budget\n${self.config.budget_limit_usd:.2f}\n\n"
                f"## Available API Keys\n"
                f"- Anthropic: {'✅' if self.config.keys.anthropic else '❌'}\n"
                f"- Google: {'✅' if self.config.keys.google else '❌'}\n"
                f"- Railway: {'✅' if self.config.keys.railway else '❌'}\n"
                f"- Gamma: {'✅' if self.config.keys.gamma else '❌'}\n\n"
                f"Analyze this prompt and respond with a JSON plan:\n"
                f'{{"skip_research": bool, "skip_deploy": bool, "max_retries": int, "reasoning": str}}'
            )),
        ]

        response = invoke_with_retry(
            llm, analysis_messages, spec=spec,
            agent_name="meta_agent", phase="planning",
            cost_tracker=self.cost_tracker,
        )

        # Parse the plan
        try:
            from pipeline.schemas import safe_parse_json
            plan = safe_parse_json(response.content, dict) or {}
        except Exception:
            plan = {}

        skip_research = plan.get("skip_research", False)
        skip_deploy = plan.get("skip_deploy", not self.config.keys.railway)
        max_retries = min(plan.get("max_retries", 3), 3)

        logger.info(
            f"[MetaAgent] Adaptive plan: skip_research={skip_research}, "
            f"skip_deploy={skip_deploy}, max_retries={max_retries}, "
            f"reasoning={plan.get('reasoning', 'N/A')[:100]}"
        )

        # Execute using deterministic mode with the LLM's decisions
        return await self.run_deterministic(
            brief,
            max_retries=max_retries,
            skip_research=skip_research,
            skip_deploy=skip_deploy,
        )
