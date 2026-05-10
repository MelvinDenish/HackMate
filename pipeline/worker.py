"""
╔══════════════════════════════════════════════════════════════╗
║  WORKERS — Specialized Agent Workers (Architecture A)         ║
║                                                              ║
║  Each worker processes one type of job from the message bus.  ║
║  Workers are fault-isolated — one crash doesn't kill others. ║
║                                                              ║
║  Workers:                                                    ║
║  - ResearchWorker: Web search + dossier generation           ║
║  - ArchitectWorker: PRD generation                           ║
║  - PlannerWorker: Task decomposition                         ║
║  - CoderWorker: Code generation with tool-use (Arch C)       ║
║  - ReviewerWorker: Code review + security                    ║
║  - DeployWorker: Deploy + pitch + presentation               ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from pipeline.message_bus import Job, MessageBus
from pipeline.cost_tracker import CostTracker
from config import PipelineConfig
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


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

    async def run(self):
        """Main worker loop — pull jobs from bus and process them."""
        self._running = True
        logger.info(f"[Worker:{self.worker_type}] Started")

        while self._running:
            job = await self.bus.get_job(timeout=5.0)
            if job is None:
                continue

            if job.type != self.worker_type:
                # Not our job — put it back
                await self.bus._job_queue.put(job)
                continue

            try:
                result = await self.process(job)
                await self.bus.complete_job(job.id, result)
            except Exception as e:
                logger.error(f"[Worker:{self.worker_type}] Job {job.id} failed: {e}")
                await self.bus.fail_job(job.id, str(e))

    def stop(self):
        self._running = False


class ResearchWorker(BaseWorker):
    """Research phase — web search + dossier generation."""

    worker_type = "research"

    async def process(self, job: Job) -> dict:
        from agents.research_agent import run_research

        brief = job.payload.get("brief", "")
        logger.info(f"[Worker:research] Processing: {brief[:80]}...")

        # Run research (currently sync — wrapped in executor)
        loop = asyncio.get_event_loop()
        dossier_path = await loop.run_in_executor(
            None,
            lambda: run_research(
                brief, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
        )

        # Ingest into knowledge base
        kb_id = ""
        try:
            from agents.knowledge_base import KnowledgeBase
            kb = KnowledgeBase(self.config)
            kb.ingest_file(dossier_path, doc_type="dossier")
            kb_id = kb.collection_name
        except Exception as e:
            logger.warning(f"[Worker:research] KB ingestion failed: {e}")

        return {
            "dossier_path": dossier_path,
            "knowledge_base_id": kb_id,
        }


class ArchitectWorker(BaseWorker):
    """Architecture phase — PRD generation."""

    worker_type = "architect"

    async def process(self, job: Job) -> dict:
        from agents.architect_agent import run_architect

        brief = job.payload.get("brief", "")
        dossier_path = job.payload.get("dossier_path", "")

        loop = asyncio.get_event_loop()
        prd_path = await loop.run_in_executor(
            None,
            lambda: run_architect(
                brief, dossier_path, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
        )

        return {"prd_path": prd_path}


class PlannerWorker(BaseWorker):
    """Planning phase — task decomposition."""

    worker_type = "planner"

    async def process(self, job: Job) -> dict:
        from agents.planner_agent import run_planner

        prd_path = job.payload.get("prd_path", "")

        loop = asyncio.get_event_loop()
        tasks = await loop.run_in_executor(
            None,
            lambda: run_planner(
                prd_path, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
        )

        return {"tasks": tasks}


class CoderWorker(BaseWorker):
    """Coding phase — code generation WITH tool-use (Architecture C).

    Unlike other workers, the coder has access to tools:
    - read_file: Read workspace files
    - write_file: Write workspace files
    - run_command: Execute shell commands
    - list_files: Browse workspace directory

    This lets it self-correct without waiting for the reviewer.
    """

    worker_type = "code"

    async def process(self, job: Job) -> dict:
        from agents.coder_agent import execute_all_tasks, execute_task

        tasks = job.payload.get("tasks", [])
        prd_path = job.payload.get("prd_path", "")
        iteration = job.payload.get("iteration", 0)
        revision_context = job.payload.get("revision_context", "")

        loop = asyncio.get_event_loop()

        if iteration == 0:
            # First pass: execute all tasks (potentially in parallel)
            code_files = await loop.run_in_executor(
                None,
                lambda: execute_all_tasks(
                    tasks, prd_path, self.workspace, self.config,
                    cost_tracker=self.cost_tracker,
                )
            )
        else:
            # Revision pass
            prd_content = self.workspace.read_file(prd_path) if prd_path else ""
            src_tree = self.workspace.get_src_tree()
            code_files = []
            for task in tasks:
                task["status"] = "pending"
                files = await loop.run_in_executor(
                    None,
                    lambda t=task: execute_task(
                        t, prd_content, src_tree,
                        self.workspace, self.config, revision_context,
                        cost_tracker=self.cost_tracker,
                        all_tasks=tasks,
                    )
                )
                code_files.extend(files)

        return {
            "code_files": code_files,
            "src_dir": str(self.workspace.src_dir),
        }


class ReviewerWorker(BaseWorker):
    """Review phase — code review + security audit (merged).

    Combines the old reviewer + security + deslopify into one worker.
    """

    worker_type = "review"

    async def process(self, job: Job) -> dict:
        from agents.reviewer_agent import run_review

        prd_path = job.payload.get("prd_path", "")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: run_review(
                prd_path, self.workspace, self.config,
                cost_tracker=self.cost_tracker,
            )
        )

        # Also run security review
        try:
            from agents.security_agent import run_security_review
            security = await loop.run_in_executor(
                None,
                lambda: run_security_review(
                    self.workspace, self.config,
                    cost_tracker=self.cost_tracker,
                )
            )
            result["security_verdict"] = security.get("verdict", "PASS")
            result["security_findings"] = security.get("findings", "")
        except Exception as e:
            logger.warning(f"[Worker:review] Security review failed: {e}")
            result["security_verdict"] = "SKIP"

        return result


class DeployWorker(BaseWorker):
    """Deploy + Pitch + Presentation — the final mile."""

    worker_type = "deploy"

    async def process(self, job: Job) -> dict:
        from agents.deployer_agent import deploy_to_railway, generate_deploy_config
        from agents.pitch_agent import run_pitch
        from agents.presentation_agent import run_presentation

        prd_path = job.payload.get("prd_path", "")
        loop = asyncio.get_event_loop()
        result = {}

        # Step 1: Generate deploy configs
        try:
            deploy_files = await loop.run_in_executor(
                None,
                lambda: generate_deploy_config(
                    self.workspace, self.config,
                    cost_tracker=self.cost_tracker,
                )
            )
            result["deploy_files"] = deploy_files
        except Exception as e:
            logger.warning(f"[Worker:deploy] Deploy config failed: {e}")

        # Step 2: Deploy to Railway
        try:
            deploy_result = await loop.run_in_executor(
                None,
                lambda: deploy_to_railway(self.workspace, self.config)
            )
            result["deploy_url"] = deploy_result.get("url", "")
        except Exception as e:
            logger.warning(f"[Worker:deploy] Railway deploy failed: {e}")

        # Step 3: Generate pitch
        try:
            pitch_path, slides = await loop.run_in_executor(
                None,
                lambda: run_pitch(
                    prd_path, self.workspace, self.config,
                    deploy_url=result.get("deploy_url", ""),
                    cost_tracker=self.cost_tracker,
                )
            )
            result["pitch_path"] = pitch_path
            result["slides"] = slides
        except Exception as e:
            logger.warning(f"[Worker:deploy] Pitch failed: {e}")

        # Step 4: Generate presentation
        try:
            pres_result = await loop.run_in_executor(
                None,
                lambda: run_presentation(
                    result.get("pitch_path", ""),
                    self.workspace, self.config,
                    cost_tracker=self.cost_tracker,
                )
            )
            result["deck_url"] = pres_result.get("deck_url", "")
        except Exception as e:
            logger.warning(f"[Worker:deploy] Presentation failed: {e}")

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
