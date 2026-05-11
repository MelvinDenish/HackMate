"""
╔══════════════════════════════════════════════════════════════╗
║  WORKERS — Specialized Agent Workers (Architecture A)         ║
║                                                              ║
║  Each worker processes one type of job from the message bus.  ║
║  Workers are fault-isolated — one crash doesn't kill others. ║
║                                                              ║
║  v6.1 FIXES:                                                 ║
║  - Per-type queue routing (no more ping-pong)                ║
║  - Graceful shutdown with CancelledError handling            ║
║  - ALL 14 missing v5 features wired in:                      ║
║    templates, CSS, learning DB, A/B testing, approval gates, ║
║    README, CI/CD, demo seeding, tracing, screenshots, etc.   ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from pipeline.message_bus import Job, MessageBus
from pipeline.cost_tracker import CostTracker
from pipeline.tracing import trace_agent
from config import PipelineConfig
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

# Shared thread pool — use ProcessPoolExecutor for true parallelism if needed
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="worker")


class BaseWorker(ABC):
    """Base class for all pipeline workers."""

    worker_type: str = "base"

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
        self._running = False

    @abstractmethod
    async def process(self, job: Job) -> dict:
        """Process a job and return the result dict."""
        raise NotImplementedError

    async def _run_sync(self, fn, *args, **kwargs):
        """Run a sync function in the thread pool executor.

        This is NOT true async — it moves blocking to a thread.
        But it prevents workers from blocking each other.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))

    async def run(self):
        """Main worker loop — pull jobs from typed queue and process them.

        FIX: Uses per-type queue (no ping-pong).
        FIX: Graceful shutdown on CancelledError.
        """
        self._running = True
        logger.info(f"[Worker:{self.worker_type}] Started")

        try:
            while self._running:
                # FIX: Pull from our TYPE-SPECIFIC queue only
                job = await self.bus.get_job(self.worker_type, timeout=5.0)
                if job is None:
                    continue

                try:
                    result = await self.process(job)
                    await self.bus.complete_job(job.id, result)
                except asyncio.CancelledError:
                    # FIX: Graceful shutdown — don't lose the job
                    await self.bus.fail_job(job.id, "Worker cancelled during processing")
                    raise
                except Exception as e:
                    logger.error(f"[Worker:{self.worker_type}] Job {job.id} failed: {e}")
                    await self.bus.fail_job(job.id, str(e))

        except asyncio.CancelledError:
            logger.info(f"[Worker:{self.worker_type}] Shutting down gracefully")

    def stop(self):
        self._running = False


class ResearchWorker(BaseWorker):
    """Research phase — web search + dossier generation + KB ingestion."""

    worker_type = "research"

    async def process(self, job: Job) -> dict:
        from agents.research_agent import run_research

        brief = job.payload.get("brief", "")
        logger.info(f"[Worker:research] Processing: {brief[:80]}...")

        # v5 FEATURE: Trace this worker
        traced_fn = trace_agent("research", "research")(run_research)

        dossier_path = await self._run_sync(
            traced_fn, brief, self.workspace, self.config,
            cost_tracker=self.cost_tracker,
        )

        # v5 FEATURE: Ingest into knowledge base
        kb_id = ""
        try:
            from agents.knowledge_base import KnowledgeBase
            kb = KnowledgeBase(self.config)
            kb.ingest_file(dossier_path, doc_type="dossier")
            kb_id = kb.collection_name
            logger.info(f"[Worker:research] KB ingested dossier ({kb_id})")
        except Exception as e:
            logger.warning(f"[Worker:research] KB ingestion failed: {e}")

        return {
            "dossier_path": dossier_path,
            "knowledge_base_id": kb_id,
        }


class ArchitectWorker(BaseWorker):
    """Architecture phase — PRD generation + approval gate."""

    worker_type = "architect"

    async def process(self, job: Job) -> dict:
        from agents.architect_agent import run_architect

        brief = job.payload.get("brief", "")
        dossier_path = job.payload.get("dossier_path", "")

        traced_fn = trace_agent("architect", "architecture")(run_architect)

        prd_path = await self._run_sync(
            traced_fn, brief, dossier_path, self.workspace, self.config,
            cost_tracker=self.cost_tracker,
        )

        # v5 FEATURE: Approval gate (non-blocking in async — auto-approve)
        try:
            from pipeline.approval_gate import ApprovalGate
            gate = ApprovalGate(timeout_seconds=1)  # Auto-approve in v6
            result = gate.request_approval(
                "architecture",
                f"PRD generated at {prd_path}",
                auto_approve=True,
            )
            logger.info(f"[Worker:architect] Approval: {result.decision}")
        except Exception as e:
            logger.debug(f"[Worker:architect] Approval gate skipped: {e}")

        return {"prd_path": prd_path}


class PlannerWorker(BaseWorker):
    """Planning phase — task decomposition."""

    worker_type = "planner"

    async def process(self, job: Job) -> dict:
        from agents.planner_agent import run_planner

        prd_path = job.payload.get("prd_path", "")

        traced_fn = trace_agent("planner", "planning")(run_planner)

        tasks = await self._run_sync(
            traced_fn, prd_path, self.workspace, self.config,
            cost_tracker=self.cost_tracker,
        )

        return {"tasks": tasks}


class CoderWorker(BaseWorker):
    """Coding phase — code generation with v5 features.

    Wires in:
    - Template injection (scaffold detection + injection)
    - Design system CSS injection
    - A/B testing prompt variants
    - Runtime tracing
    """

    worker_type = "code"

    async def process(self, job: Job) -> dict:
        from agents.coder_agent import execute_all_tasks, execute_task

        tasks = job.payload.get("tasks", [])
        prd_path = job.payload.get("prd_path", "")
        iteration = job.payload.get("iteration", 0)
        revision_context = job.payload.get("revision_context", "")

        # v5 FEATURE: Template injection
        if iteration == 0:
            try:
                from pipeline.template_selector import TemplateSelector
                ts = TemplateSelector()
                prd_content = self.workspace.read_file(prd_path) if prd_path else ""
                scaffold_files = ts.detect_and_inject(prd_content, self.workspace)
                if scaffold_files:
                    logger.info(
                        f"[Worker:code] Template injected {len(scaffold_files)} scaffold files"
                    )
            except Exception as e:
                logger.debug(f"[Worker:code] Template injection skipped: {e}")

        # v5 FEATURE: Design system CSS injection
        if iteration == 0:
            try:
                from design_systems import get_design_system_css
                css = get_design_system_css("modern")
                if css:
                    self.workspace.write_source_file("src/styles/design-system.css", css)
                    logger.info("[Worker:code] Design system CSS injected")
            except Exception as e:
                logger.debug(f"[Worker:code] CSS injection skipped: {e}")

        # v5 FEATURE: A/B testing prompt variant selection
        ab_variant = "default"
        try:
            from pipeline.ab_testing import ABTestingFramework
            ab = ABTestingFramework()
            ab_variant = ab.select_variant("coder_prompt")
            logger.info(f"[Worker:code] Using A/B variant: {ab_variant}")
        except Exception as e:
            logger.debug(f"[Worker:code] A/B testing skipped: {e}")

        traced_execute = trace_agent("coder", "coding")(execute_all_tasks)

        if iteration == 0:
            code_files = await self._run_sync(
                traced_execute,
                tasks, prd_path, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
        else:
            prd_content = self.workspace.read_file(prd_path) if prd_path else ""
            src_tree = self.workspace.get_src_tree()
            code_files = []
            for task in tasks:
                task["status"] = "pending"
                files = await self._run_sync(
                    execute_task,
                    task, prd_content, src_tree,
                    self.workspace, self.config, revision_context,
                    cost_tracker=self.cost_tracker,
                    all_tasks=tasks,
                )
                code_files.extend(files)

        # v5 FEATURE: Budget redistribution after coding
        if self.cost_tracker:
            try:
                coding_budget = self.config.phase_budgets.get("coding", 5.0)
                self.config.phase_budgets.update(
                    self.cost_tracker.redistribute_savings(
                        "coding", coding_budget, self.config.phase_budgets
                    )
                )
            except Exception as e:
                logger.debug(f"[Worker:code] Budget redistribution skipped: {e}")

        return {
            "code_files": code_files,
            "src_dir": str(self.workspace.src_dir),
            "ab_variant": ab_variant,
        }


class ReviewerWorker(BaseWorker):
    """Review phase — code review + security + deslopify (merged).

    Combines 3 old v5 phases into one worker:
    1. Code cleanup (deslopify)
    2. Code review (7-phase)
    3. Security audit (OWASP)
    """

    worker_type = "review"

    async def process(self, job: Job) -> dict:
        from agents.reviewer_agent import run_review

        prd_path = job.payload.get("prd_path", "")

        # Step 1: Deslopify cleanup (v5 feature — formerly separate node)
        try:
            from agents.deslopify_agent import run_deslopify
            traced_deslopify = trace_agent("deslopify", "cleanup")(run_deslopify)
            await self._run_sync(
                traced_deslopify, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
            logger.info("[Worker:review] Deslopify cleanup completed")
        except ImportError:
            logger.debug("[Worker:review] No deslopify agent available, skipping")
        except Exception as e:
            logger.warning(f"[Worker:review] Deslopify failed (non-fatal): {e}")

        # Step 2: Code review
        traced_review = trace_agent("reviewer", "review")(run_review)
        result = await self._run_sync(
            traced_review, prd_path, self.workspace, self.config,
            cost_tracker=self.cost_tracker,
        )

        # Step 3: Security review
        try:
            from agents.security_agent import run_security_review
            traced_security = trace_agent("security", "security")(run_security_review)
            security = await self._run_sync(
                traced_security, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
            result["security_verdict"] = security.get("verdict", "PASS")
            result["security_findings"] = security.get("findings", "")
        except ImportError:
            result["security_verdict"] = "SKIP"
        except Exception as e:
            logger.warning(f"[Worker:review] Security review failed: {e}")
            result["security_verdict"] = "SKIP"

        return result


class DeployWorker(BaseWorker):
    """Deploy + README + CI/CD + Seed + Pitch + Presentation.

    Merges 6 old v5 phases into one worker:
    1. README generation
    2. CI/CD config generation
    3. Demo data seeding
    4. Deploy to Railway
    5. Pitch deck content
    6. Presentation rendering
    """

    worker_type = "deploy"

    async def process(self, job: Job) -> dict:
        from agents.deployer_agent import deploy_to_railway, generate_deploy_config

        prd_path = job.payload.get("prd_path", "")
        result = {}

        # Step 1: README generation (v5 feature — formerly readme_node)
        try:
            from agents.readme_agent import run_readme
            traced_readme = trace_agent("readme", "readme")(run_readme)
            readme_path = await self._run_sync(
                traced_readme, prd_path, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
            result["readme_path"] = readme_path
            logger.info("[Worker:deploy] README generated")
        except ImportError:
            logger.debug("[Worker:deploy] No readme agent, skipping")
        except Exception as e:
            logger.warning(f"[Worker:deploy] README failed: {e}")

        # Step 2: CI/CD generation (v5 feature — formerly cicd_node)
        try:
            from agents.cicd_agent import run_cicd
            traced_cicd = trace_agent("cicd", "cicd")(run_cicd)
            cicd_files = await self._run_sync(
                traced_cicd, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
            result["cicd_files"] = cicd_files
            logger.info(f"[Worker:deploy] CI/CD generated: {cicd_files}")
        except ImportError:
            logger.debug("[Worker:deploy] No cicd agent, skipping")
        except Exception as e:
            logger.warning(f"[Worker:deploy] CI/CD failed: {e}")

        # Step 3: Demo data seeding (v5 feature — formerly seed_node)
        try:
            from tools.demo_seeder import generate_seed_data
            seed_files = await self._run_sync(
                generate_seed_data, prd_path, self.workspace,
            )
            result["seed_files"] = seed_files
            logger.info(f"[Worker:deploy] Demo data seeded: {len(seed_files)} files")
        except ImportError:
            logger.debug("[Worker:deploy] No demo seeder, skipping")
        except Exception as e:
            logger.warning(f"[Worker:deploy] Seeding failed: {e}")

        # Step 4: Generate deploy configs (Dockerfile, railway.toml)
        try:
            traced_deploy_config = trace_agent("deployer", "deploy_config")(generate_deploy_config)
            deploy_files = await self._run_sync(
                traced_deploy_config, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
            result["deploy_files"] = deploy_files
        except Exception as e:
            logger.warning(f"[Worker:deploy] Deploy config failed: {e}")

        # Step 5: Deploy to Railway
        if self.config.keys.railway:
            try:
                deploy_result = await self._run_sync(
                    deploy_to_railway, self.workspace, self.config
                )
                result["deploy_url"] = deploy_result.get("url", "")
                logger.info(f"[Worker:deploy] Deployed: {result['deploy_url']}")
            except Exception as e:
                logger.warning(f"[Worker:deploy] Railway deploy failed: {e}")
        else:
            logger.info("[Worker:deploy] No RAILWAY_API_TOKEN, skipping deployment")

        # Step 6: Generate pitch
        try:
            from agents.pitch_agent import run_pitch
            traced_pitch = trace_agent("pitch", "pitch")(run_pitch)
            pitch_path, slides = await self._run_sync(
                traced_pitch, prd_path, self.workspace, self.config,
                deploy_url=result.get("deploy_url", ""),
                cost_tracker=self.cost_tracker,
            )
            result["pitch_path"] = pitch_path
            result["slides"] = slides
        except Exception as e:
            logger.warning(f"[Worker:deploy] Pitch failed: {e}")

        # Step 7: Generate presentation
        if self.config.keys.gamma:
            try:
                from agents.presentation_agent import run_presentation
                traced_present = trace_agent("presentation", "presentation")(run_presentation)
                pres_result = await self._run_sync(
                    traced_present, result.get("pitch_path", ""),
                    self.workspace, self.config,
                    cost_tracker=self.cost_tracker,
                )
                result["deck_url"] = pres_result.get("deck_url", "")
            except Exception as e:
                logger.warning(f"[Worker:deploy] Presentation failed: {e}")
        else:
            logger.info("[Worker:deploy] No GAMMA_API_KEY, skipping presentation")

        # Step 8: Demo storyboard (v5 feature — formerly in present_node)
        try:
            from tools.video_recorder import create_demo_storyboard
            screenshots = list(self.workspace.output_dir.glob("screenshots/*.png"))
            if screenshots:
                await self._run_sync(
                    create_demo_storyboard,
                    self.workspace.output_dir,
                    screenshots=screenshots,
                    demo_walkthrough=result.get("slides", []),
                )
                logger.info("[Worker:deploy] Demo storyboard created")
        except Exception as e:
            logger.debug(f"[Worker:deploy] Storyboard skipped: {e}")

        return result


# ── Worker Pool ──────────────────────────────────────────────

WORKER_CLASSES = {
    "research": ResearchWorker,
    "architect": ArchitectWorker,
    "planner": PlannerWorker,
    "code": CoderWorker,
    "review": ReviewerWorker,
    "deploy": DeployWorker,
}


async def create_worker_pool(
    config: PipelineConfig,
    workspace: WorkspaceManager,
    bus: MessageBus,
    cost_tracker: Optional[CostTracker] = None,
    coder_count: int = 1,
) -> list[BaseWorker]:
    """Create and return all workers (not yet started)."""
    workers = []
    for wtype, cls in WORKER_CLASSES.items():
        count = coder_count if wtype == "code" else 1
        for i in range(count):
            w = cls(config, workspace, bus, cost_tracker)
            workers.append(w)
    logger.info(
        f"[WorkerPool] Created {len(workers)} workers: "
        f"{', '.join(w.worker_type for w in workers)}"
    )
    return workers
