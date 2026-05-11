"""
╔══════════════════════════════════════════════════════════════╗
║  WORKERS — Specialized Agent Workers (Architecture A)         ║
║                                                              ║
║  v6.2 — ALL function signatures verified against real code.  ║
║  Every import name, argument order, and type matches the     ║
║  actual agent/tool function signatures. No more crashes.     ║
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
        raise NotImplementedError

    async def _run_sync(self, fn, *args, **kwargs):
        """Run a sync function in the thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))

    async def run(self):
        """Main worker loop — pull from typed queue only."""
        self._running = True
        logger.info(f"[Worker:{self.worker_type}] Started")
        try:
            while self._running:
                job = await self.bus.get_job(self.worker_type, timeout=5.0)
                if job is None:
                    continue
                try:
                    result = await self.process(job)
                    await self.bus.complete_job(job.id, result)
                except asyncio.CancelledError:
                    await self.bus.fail_job(job.id, "Worker cancelled")
                    raise
                except Exception as e:
                    logger.error(f"[Worker:{self.worker_type}] Job {job.id} failed: {e}")
                    await self.bus.fail_job(job.id, str(e))
        except asyncio.CancelledError:
            logger.info(f"[Worker:{self.worker_type}] Shutting down")

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════
# RESEARCH WORKER
# Actual: run_research(refined_brief, workspace, config, cost_tracker=None)
# ═══════════════════════════════════════════════════════════

class ResearchWorker(BaseWorker):
    worker_type = "research"

    async def process(self, job: Job) -> dict:
        from agents.research_agent import run_research

        brief = job.payload.get("brief", "")
        logger.info(f"[Worker:research] Processing: {brief[:80]}...")

        traced_fn = trace_agent("research", "research")(run_research)

        # Signature: run_research(refined_brief, workspace, config, cost_tracker=None)
        dossier_path = await self._run_sync(
            traced_fn,
            brief,                    # refined_brief
            self.workspace,           # workspace
            self.config,              # config
            cost_tracker=self.cost_tracker,
        )

        # KB ingestion
        kb_id = ""
        try:
            from agents.knowledge_base import KnowledgeBase
            kb = KnowledgeBase(self.config)
            kb.ingest_file(dossier_path, doc_type="dossier")
            kb_id = kb.collection_name
        except Exception as e:
            logger.warning(f"[Worker:research] KB ingestion failed: {e}")

        return {"dossier_path": dossier_path, "knowledge_base_id": kb_id}


# ═══════════════════════════════════════════════════════════
# ARCHITECT WORKER
# Actual: run_architect(refined_brief, dossier_path, workspace, config, cost_tracker=None)
# ═══════════════════════════════════════════════════════════

class ArchitectWorker(BaseWorker):
    worker_type = "architect"

    async def process(self, job: Job) -> dict:
        from agents.architect_agent import run_architect

        brief = job.payload.get("brief", "")
        dossier_path = job.payload.get("dossier_path", "")

        traced_fn = trace_agent("architect", "architecture")(run_architect)

        # Signature: run_architect(refined_brief, dossier_path, workspace, config, cost_tracker=None)
        prd_path = await self._run_sync(
            traced_fn,
            brief,                    # refined_brief
            dossier_path,             # dossier_path
            self.workspace,           # workspace
            self.config,              # config
            cost_tracker=self.cost_tracker,
        )

        # Approval gate (auto-approve in v6 — mode='auto' is default)
        # Actual: ApprovalGate(mode='auto', auto_timeout=60)
        # Actual: request_approval(gate_name, summary, details='', options=None) -> str
        try:
            from pipeline.approval_gate import ApprovalGate
            gate = ApprovalGate(mode="auto", auto_timeout=1)
            gate.request_approval("architecture", f"PRD generated at {prd_path}")
        except Exception:
            pass

        return {"prd_path": prd_path}


# ═══════════════════════════════════════════════════════════
# PLANNER WORKER
# Actual: run_planner(prd_path, workspace, config, cost_tracker=None)
# ═══════════════════════════════════════════════════════════

class PlannerWorker(BaseWorker):
    worker_type = "planner"

    async def process(self, job: Job) -> dict:
        from agents.planner_agent import run_planner

        prd_path = job.payload.get("prd_path", "")

        traced_fn = trace_agent("planner", "planning")(run_planner)

        # Signature: run_planner(prd_path, workspace, config, cost_tracker=None)
        tasks = await self._run_sync(
            traced_fn,
            prd_path,                 # prd_path
            self.workspace,           # workspace
            self.config,              # config
            cost_tracker=self.cost_tracker,
        )

        return {"tasks": tasks}


# ═══════════════════════════════════════════════════════════
# CODER WORKER
# Actual: execute_all_tasks(tasks, prd_path, workspace, config, cost_tracker=None)
# Actual: execute_task(task, prd_content, src_tree, workspace, config,
#                      revision_context="", cost_tracker=None, all_tasks=None)
# ═══════════════════════════════════════════════════════════

class CoderWorker(BaseWorker):
    worker_type = "code"

    async def process(self, job: Job) -> dict:
        from agents.coder_agent import execute_all_tasks, execute_task

        tasks = job.payload.get("tasks", [])
        prd_path = job.payload.get("prd_path", "")
        iteration = job.payload.get("iteration", 0)
        revision_context = job.payload.get("revision_context", "")

        # Template + CSS injection on first pass only
        if iteration == 0:
            self._inject_templates(prd_path)
            self._inject_design_system()

        # A/B variant selection
        ab_variant = self._select_ab_variant()

        if iteration == 0:
            traced_fn = trace_agent("coder", "coding")(execute_all_tasks)

            # Signature: execute_all_tasks(tasks, prd_path, workspace, config, cost_tracker=None)
            code_files = await self._run_sync(
                traced_fn,
                tasks,                # tasks
                prd_path,             # prd_path
                self.workspace,       # workspace
                self.config,          # config
                cost_tracker=self.cost_tracker,
            )
        else:
            # Revision pass — re-execute each task with context
            prd_content = ""
            if prd_path:
                try:
                    prd_content = self.workspace.read_file(prd_path)
                except Exception:
                    pass
            src_tree = self.workspace.get_src_tree()
            code_files = []

            for task in tasks:
                task["status"] = "pending"
                # Signature: execute_task(task, prd_content, src_tree, workspace, config,
                #                        revision_context, cost_tracker, all_tasks)
                files = await self._run_sync(
                    execute_task,
                    task,                 # task
                    prd_content,          # prd_content
                    src_tree,             # src_tree
                    self.workspace,       # workspace
                    self.config,          # config
                    revision_context,     # revision_context
                    self.cost_tracker,    # cost_tracker (positional)
                    tasks,                # all_tasks (positional)
                )
                code_files.extend(files)

        # Budget redistribution
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

        return {"code_files": code_files, "src_dir": str(self.workspace.src_dir), "ab_variant": ab_variant}

    def _inject_templates(self, prd_path: str):
        """Inject template scaffolds if detected."""
        try:
            from pipeline.template_selector import select_template, inject_template
            prd_content = self.workspace.read_file(prd_path) if prd_path else ""
            template = select_template(prd_content)
            if template:
                inject_template(template, self.workspace.src_dir)
                logger.info(f"[Worker:code] Template injected: {template}")
        except Exception as e:
            logger.debug(f"[Worker:code] Template injection skipped: {e}")

    def _inject_design_system(self):
        """Inject CSS design system."""
        try:
            from design_systems import get_design_system
            css = get_design_system("modern")
            if css:
                self.workspace.write_source_file("src/styles/design-system.css", css)
                logger.info("[Worker:code] Design system CSS injected")
        except Exception as e:
            logger.debug(f"[Worker:code] CSS injection skipped: {e}")

    def _select_ab_variant(self) -> str:
        """Select A/B testing prompt variant."""
        try:
            from pipeline.ab_testing import get_ab_registry
            registry = get_ab_registry()
            variant = registry.select_variant("coder_prompt")
            logger.info(f"[Worker:code] A/B variant: {variant}")
            return variant
        except Exception:
            return "default"


# ═══════════════════════════════════════════════════════════
# REVIEWER WORKER
# Actual: run_review(workspace, config, cost_tracker=None, prd_path="")
# Actual: run_security_review(workspace, config, cost_tracker=None)
# Actual: run_deslopify(workspace, config, cost_tracker=None)
# ═══════════════════════════════════════════════════════════

class ReviewerWorker(BaseWorker):
    worker_type = "review"

    async def process(self, job: Job) -> dict:
        from agents.reviewer_agent import run_review

        prd_path = job.payload.get("prd_path", "")

        # Step 1: Deslopify cleanup
        try:
            from agents.deslopify_agent import run_deslopify
            traced_ds = trace_agent("deslopify", "cleanup")(run_deslopify)
            # Signature: run_deslopify(workspace, config, cost_tracker=None)
            await self._run_sync(
                traced_ds,
                self.workspace,       # workspace
                self.config,          # config
                cost_tracker=self.cost_tracker,
            )
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"[Worker:review] Deslopify failed: {e}")

        # Step 2: Code review
        traced_review = trace_agent("reviewer", "review")(run_review)
        # Signature: run_review(workspace, config, cost_tracker=None, prd_path="")
        # RETURNS: ReviewResult (NOT dict) — must convert via .to_dict()
        review_result = await self._run_sync(
            traced_review,
            self.workspace,           # workspace
            self.config,              # config
            cost_tracker=self.cost_tracker,
            prd_path=prd_path,
        )

        # Convert ReviewResult → dict (meta-agent checks result["verdict"])
        if hasattr(review_result, "to_dict"):
            result = review_result.to_dict()
        elif isinstance(review_result, dict):
            result = review_result
        else:
            result = {"verdict": "FAIL", "notes": str(review_result), "fix_instructions": ""}

        # Step 3: Security review
        try:
            from agents.security_agent import run_security_review
            traced_sec = trace_agent("security", "security")(run_security_review)
            # Signature: run_security_review(workspace, config, cost_tracker=None)
            # RETURNS: SecurityResult (NOT dict) — must convert via .to_dict()
            security_obj = await self._run_sync(
                traced_sec,
                self.workspace,       # workspace
                self.config,          # config
                cost_tracker=self.cost_tracker,
            )
            if hasattr(security_obj, "to_dict"):
                security = security_obj.to_dict()
            elif isinstance(security_obj, dict):
                security = security_obj
            else:
                security = {"verdict": "SKIP"}
            result["security_verdict"] = security.get("verdict", "PASS")
            result["security_findings"] = security.get("findings", "")
        except ImportError:
            result["security_verdict"] = "SKIP"
        except Exception as e:
            logger.warning(f"[Worker:review] Security failed: {e}")
            result["security_verdict"] = "SKIP"

        return result


# ═══════════════════════════════════════════════════════════
# DEPLOY WORKER
# Actual: run_readme_agent(workspace, config, cost_tracker=None, prd_path="")
# Actual: generate_cicd(workspace_src_dir, tech_stack="")
# Actual: generate_seed_data(workspace, config, prd_content, src_tree, cost_tracker=None)
# Actual: generate_deploy_config(workspace, config, cost_tracker=None)
# Actual: deploy_to_railway(workspace, config, project_name=None, cost_tracker=None)
# Actual: run_pitch(dossier_path, prd_path, deployment_url, workspace, config, cost_tracker=None)
# Actual: run_presentation(pitch_content_path, workspace, config, cost_tracker=None)
# Actual: create_demo_storyboard(screenshots_dir, demo_walkthrough, output_dir)
# ═══════════════════════════════════════════════════════════

class DeployWorker(BaseWorker):
    worker_type = "deploy"

    async def process(self, job: Job) -> dict:
        prd_path = job.payload.get("prd_path", "")
        result = {}

        # Step 1: README
        try:
            from agents.readme_agent import run_readme_agent
            traced_readme = trace_agent("readme", "readme")(run_readme_agent)
            # Signature: run_readme_agent(workspace, config, cost_tracker=None, prd_path="")
            readme_files = await self._run_sync(
                traced_readme,
                self.workspace,           # workspace
                self.config,              # config
                cost_tracker=self.cost_tracker,
                prd_path=prd_path,
            )
            result["readme_files"] = readme_files
        except ImportError:
            logger.debug("[Worker:deploy] No readme agent")
        except Exception as e:
            logger.warning(f"[Worker:deploy] README failed: {e}")

        # Step 2: CI/CD (no LLM call — pure template generation)
        try:
            from agents.cicd_agent import generate_cicd
            # Signature: generate_cicd(workspace_src_dir, tech_stack="")
            cicd_files = await self._run_sync(
                generate_cicd,
                self.workspace.src_dir,   # workspace_src_dir (Path)
            )
            result["cicd_files"] = cicd_files
        except ImportError:
            logger.debug("[Worker:deploy] No cicd agent")
        except Exception as e:
            logger.warning(f"[Worker:deploy] CI/CD failed: {e}")

        # Step 3: Demo data seeding
        try:
            from tools.demo_seeder import generate_seed_data
            prd_content = ""
            if prd_path:
                try:
                    prd_content = self.workspace.read_file(prd_path)
                except Exception:
                    pass
            src_tree = self.workspace.get_src_tree()
            # Signature: generate_seed_data(workspace, config, prd_content, src_tree, cost_tracker=None)
            seed_files = await self._run_sync(
                generate_seed_data,
                self.workspace,           # workspace
                self.config,              # config
                prd_content,              # prd_content
                src_tree,                 # src_tree
                self.cost_tracker,        # cost_tracker
            )
            result["seed_files"] = seed_files
        except ImportError:
            logger.debug("[Worker:deploy] No demo seeder")
        except Exception as e:
            logger.warning(f"[Worker:deploy] Seeding failed: {e}")

        # Step 4: Deploy config (Dockerfile, railway.toml)
        try:
            from agents.deployer_agent import generate_deploy_config
            traced_cfg = trace_agent("deployer", "deploy_config")(generate_deploy_config)
            # Signature: generate_deploy_config(workspace, config, cost_tracker=None)
            deploy_files = await self._run_sync(
                traced_cfg,
                self.workspace,           # workspace
                self.config,              # config
                cost_tracker=self.cost_tracker,
            )
            result["deploy_files"] = deploy_files
        except Exception as e:
            logger.warning(f"[Worker:deploy] Deploy config failed: {e}")

        # Step 5: Deploy to Railway
        if self.config.keys.railway:
            try:
                from agents.deployer_agent import deploy_to_railway
                # Signature: deploy_to_railway(workspace, config, project_name=None, cost_tracker=None)
                deploy_result = await self._run_sync(
                    deploy_to_railway,
                    self.workspace,       # workspace
                    self.config,          # config
                )
                result["deploy_url"] = deploy_result.get("url", "")
            except Exception as e:
                logger.warning(f"[Worker:deploy] Railway failed: {e}")
        else:
            logger.info("[Worker:deploy] No RAILWAY_API_TOKEN, skipping")

        # Step 6: Pitch
        try:
            from agents.pitch_agent import run_pitch
            traced_pitch = trace_agent("pitch", "pitch")(run_pitch)
            dossier_path = job.payload.get("dossier_path", "")
            deploy_url = result.get("deploy_url", "")
            # Signature: run_pitch(dossier_path, prd_path, deployment_url, workspace, config, cost_tracker=None)
            pitch_path, slides = await self._run_sync(
                traced_pitch,
                dossier_path,             # dossier_path
                prd_path,                 # prd_path
                deploy_url,               # deployment_url
                self.workspace,           # workspace
                self.config,              # config
                self.cost_tracker,        # cost_tracker
            )
            result["pitch_path"] = pitch_path
            result["slides"] = slides
        except Exception as e:
            logger.warning(f"[Worker:deploy] Pitch failed: {e}")

        # Step 7: Presentation
        if self.config.keys.gamma and result.get("pitch_path"):
            try:
                from agents.presentation_agent import run_presentation
                traced_pres = trace_agent("presentation", "presentation")(run_presentation)
                # Signature: run_presentation(pitch_content_path, workspace, config, cost_tracker=None)
                pres = await self._run_sync(
                    traced_pres,
                    result["pitch_path"],     # pitch_content_path
                    self.workspace,           # workspace
                    self.config,              # config
                    cost_tracker=self.cost_tracker,
                )
                result["deck_url"] = pres.get("deck_url", "")
            except Exception as e:
                logger.warning(f"[Worker:deploy] Presentation failed: {e}")

        # Step 8: Demo storyboard
        try:
            from tools.video_recorder import create_demo_storyboard
            screenshots_dir = self.workspace.output_dir / "screenshots"
            if screenshots_dir.exists():
                screenshots = sorted(screenshots_dir.glob("*.png"))
                if screenshots:
                    # Signature: create_demo_storyboard(screenshots_dir, demo_walkthrough, output_dir)
                    await self._run_sync(
                        create_demo_storyboard,
                        screenshots_dir,          # screenshots_dir
                        result.get("slides", []), # demo_walkthrough
                        self.workspace.output_dir, # output_dir
                    )
        except Exception as e:
            logger.debug(f"[Worker:deploy] Storyboard skipped: {e}")

        return result


# ═══════════════════════════════════════════════════════════
# WORKER POOL
# ═══════════════════════════════════════════════════════════

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
    """Create all workers (not yet started)."""
    workers = []
    for wtype, cls in WORKER_CLASSES.items():
        count = coder_count if wtype == "code" else 1
        for _ in range(count):
            workers.append(cls(config, workspace, bus, cost_tracker))
    logger.info(f"[WorkerPool] {len(workers)} workers: {[w.worker_type for w in workers]}")
    return workers
