"""
╔══════════════════════════════════════════════════════════════╗
║  MESSAGE BUS — In-Process Async Job Queue                     ║
║                                                              ║
║  Architecture A: Event-driven communication between the      ║
║  meta-agent (orchestrator) and specialized workers.          ║
║                                                              ║
║  FIX: Uses per-type queues to prevent job ping-pong.         ║
║  Currently uses asyncio.Queue (single-process).              ║
║  Drop-in replaceable with Redis Streams for multi-process.   ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    """A unit of work dispatched to a worker."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: str = ""           # "research", "architect", "code", "review", etc.
    payload: dict = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    duration_s: float = 0.0

    def complete(self, result: dict):
        self.status = JobStatus.DONE
        self.result = result
        self.completed_at = time.time()
        self.duration_s = round(self.completed_at - self.created_at, 2)

    def fail(self, error: str):
        self.status = JobStatus.FAILED
        self.error = error
        self.completed_at = time.time()
        self.duration_s = round(self.completed_at - self.created_at, 2)


class MessageBus:
    """In-process async message bus for job dispatch and results.

    FIX: Uses per-type queues so workers only see their own jobs.
    No more ping-pong where a research job bounces between coder/reviewer.

    Supports:
    - submit_job(): Enqueue work for a specific worker type
    - wait_for_result(): Block until a specific job completes
    - get_job(worker_type): Workers pull ONLY their own jobs
    - complete_job(): Workers report results
    - broadcast(): Emit events (progress, errors) to all listeners

    Upgradeable to Redis Streams by implementing the same interface.
    """

    def __init__(self):
        # FIX: Per-type queues instead of single shared queue
        self._typed_queues: dict[str, asyncio.Queue[Job]] = {}
        self._jobs: dict[str, Job] = {}
        self._results: dict[str, asyncio.Event] = {}
        self._listeners: list[asyncio.Queue] = []
        self._history: list[dict] = []

    def _get_queue(self, job_type: str) -> asyncio.Queue:
        """Get or create the queue for a specific job type."""
        if job_type not in self._typed_queues:
            self._typed_queues[job_type] = asyncio.Queue()
        return self._typed_queues[job_type]

    async def submit_job(self, job_type: str, payload: dict) -> str:
        """Submit a job to the correct worker type's queue."""
        job = Job(type=job_type, payload=payload)
        self._jobs[job.id] = job
        self._results[job.id] = asyncio.Event()
        await self._get_queue(job_type).put(job)
        logger.info(f"[Bus] Job submitted: {job.id} ({job_type})")
        await self._broadcast_event("job.submitted", {"job_id": job.id, "type": job_type})
        return job.id

    async def get_job(self, worker_type: str, timeout: float = 30.0) -> Optional[Job]:
        """Workers call this to pull ONLY jobs matching their type.

        FIX: Each worker type has its own queue — no cross-contamination.
        """
        queue = self._get_queue(worker_type)
        try:
            job = await asyncio.wait_for(queue.get(), timeout=timeout)
            job.status = JobStatus.RUNNING
            logger.info(f"[Bus] Job picked up: {job.id} ({job.type}) by {worker_type}")
            await self._broadcast_event("job.started", {"job_id": job.id, "type": job.type})
            return job
        except asyncio.TimeoutError:
            return None

    async def complete_job(self, job_id: str, result: dict):
        """Workers call this to report job completion."""
        if job_id not in self._jobs:
            logger.warning(f"[Bus] Unknown job ID: {job_id}")
            return
        job = self._jobs[job_id]
        job.complete(result)
        self._results[job_id].set()
        logger.info(f"[Bus] Job completed: {job_id} ({job.duration_s}s)")
        await self._broadcast_event("job.done", {
            "job_id": job_id, "type": job.type, "duration_s": job.duration_s
        })

    async def fail_job(self, job_id: str, error: str):
        """Workers call this to report job failure."""
        if job_id not in self._jobs:
            return
        job = self._jobs[job_id]
        job.fail(error)
        self._results[job_id].set()
        logger.warning(f"[Bus] Job failed: {job_id} -- {error[:100]}")
        await self._broadcast_event("job.failed", {
            "job_id": job_id, "type": job.type, "error": error[:200]
        })

    async def wait_for_result(self, job_id: str, timeout: float = 600.0) -> Job:
        """Block until a job completes (success or failure)."""
        if job_id not in self._results:
            raise ValueError(f"Unknown job: {job_id}")
        try:
            await asyncio.wait_for(self._results[job_id].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._jobs[job_id].fail(f"Timeout after {timeout}s")
            self._results[job_id].set()
        return self._jobs[job_id]

    def get_job_status(self, job_id: str) -> Optional[Job]:
        """Check current status of a job."""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[Job]:
        """Get all jobs (for dashboard/debugging)."""
        return list(self._jobs.values())

    @property
    def pending_counts(self) -> dict[str, int]:
        """Number of pending jobs per worker type."""
        return {
            wtype: q.qsize()
            for wtype, q in self._typed_queues.items()
        }

    # ── Event Broadcasting ────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to bus events (for UI, logging, etc)."""
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.append(q)
        return q

    async def _broadcast_event(self, event_type: str, data: dict):
        """Broadcast an event to all listeners."""
        event = {"type": event_type, "data": data, "timestamp": time.time()}
        self._history.append(event)
        for listener in self._listeners:
            try:
                listener.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop events for slow listeners

    @property
    def event_history(self) -> list[dict]:
        return self._history.copy()
