"""In-memory analysis job store with background execution and progress events."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.agent.loop import AgentError
from app.agent.pipeline import AnalysisPipeline
from app.api.schemas import AgentEvent, JobPartial, JobView, TripAnalysis

log = logging.getLogger("sakura.jobs")

MAX_JOBS = 200


@dataclass
class Job:
    id: str
    document_name: str
    document_text: str
    travelers: int | None
    depth: Literal["quick", "full"]
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    events: list[AgentEvent] = field(default_factory=list)
    partial: JobPartial = field(default_factory=JobPartial)
    result: TripAnalysis | None = None
    error: str | None = None

    def emit(self, kind: str, title: str, detail: str | None = None) -> None:
        self.events.append(AgentEvent(seq=len(self.events), kind=kind, title=title, detail=detail))

    def view(self) -> JobView:
        return JobView(
            id=self.id, status=self.status, document_name=self.document_name,
            depth=self.depth, created_at=self.created_at, events=self.events,
            partial=self.partial, result=self.result, error=self.error,
        )


class JobStore:
    """Serializes pipeline runs: the local model fits one request at a time."""

    def __init__(self, pipeline: AnalysisPipeline):
        self._pipeline = pipeline
        self._jobs: dict[str, Job] = {}
        self._run_lock = asyncio.Lock()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def submit(
        self,
        document_name: str,
        document_text: str,
        travelers: int | None,
        depth: Literal["quick", "full"],
    ) -> Job:
        if len(self._jobs) >= MAX_JOBS:
            oldest = min(self._jobs.values(), key=lambda j: j.created_at)
            if oldest.status in ("done", "error"):
                del self._jobs[oldest.id]
        job = Job(
            id=uuid.uuid4().hex[:12],
            document_name=document_name,
            document_text=document_text,
            travelers=travelers,
            depth=depth,
        )
        self._jobs[job.id] = job
        asyncio.get_running_loop().create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        async with self._run_lock:
            job.status = "running"
            try:
                job.result = await self._pipeline.run(
                    job.document_text, job.travelers, job.depth, job.emit, job.partial
                )
                job.status = "done"
            except AgentError as exc:
                job.status = "error"
                job.error = str(exc)
                job.emit("error", "Analysis failed", str(exc))
            except Exception:
                log.exception("Unexpected error in job %s", job.id)
                job.status = "error"
                job.error = "Internal error during analysis."
                job.emit("error", "Analysis failed", job.error)
