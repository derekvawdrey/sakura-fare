"""HTTP API: submit a document for analysis, poll job progress/result."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.api.schemas import CreateJobResponse, JobView
from app.core.config import settings
from app.services.documents import DocumentError, extract_text

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request) -> dict:
    llm_ok = await request.app.state.llm.healthy()
    search = await request.app.state.search.status()
    return {
        "status": "ok" if llm_ok else "degraded",
        "llm": {"reachable": llm_ok, "base_url": settings.llm_base_url, "model": settings.llm_model},
        "web_search": search,
        "fares_dataset": request.app.state.fares.version,
    }


@router.post("/analyses", response_model=CreateJobResponse, status_code=202)
async def create_analysis(
    request: Request,
    file: UploadFile | None = File(None),
    text: str | None = Form(None),
    travelers: int | None = Form(None),
    depth: str = Form("full"),
) -> CreateJobResponse:
    if travelers is not None and not (1 <= travelers <= 50):
        raise HTTPException(422, "travelers must be between 1 and 50")
    if depth not in ("quick", "full"):
        raise HTTPException(422, "depth must be 'quick' or 'full'")

    if file is not None and file.filename:
        data = await file.read()
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, "File too large (max 15 MB).")
        try:
            document_text = extract_text(file.filename, data)
        except DocumentError as exc:
            raise HTTPException(422, str(exc)) from exc
        name = file.filename
    elif text and text.strip():
        document_text = text.strip()
        name = "pasted text"
    else:
        raise HTTPException(422, "Provide a document file or pasted text.")

    job = request.app.state.jobs.submit(name, document_text, travelers, depth)
    return CreateJobResponse(id=job.id)


@router.get("/analyses/{job_id}", response_model=JobView)
async def get_analysis(request: Request, job_id: str) -> JobView:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown analysis id.")
    return job.view()
