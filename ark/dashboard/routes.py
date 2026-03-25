"""FastAPI routes — REST endpoints + SSE stream."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import data, watcher
from .models import ProjectDetail, ProjectSummary

router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"


@router.get("/", response_class=HTMLResponse)
async def index():
    html_file = STATIC_DIR / "dashboard.html"
    return HTMLResponse(html_file.read_text())


@router.get("/api/projects")
async def list_projects() -> list[dict]:
    names = data.list_project_names()
    return [data.read_project_summary(n).model_dump() for n in names]


@router.get("/api/projects/{name}")
async def get_project(name: str) -> dict:
    names = data.list_project_names()
    if name not in names:
        raise HTTPException(404, f"Project '{name}' not found")
    return data.read_project_detail(name).model_dump()


@router.get("/api/projects/{name}/review")
async def get_review(name: str) -> dict:
    names = data.list_project_names()
    if name not in names:
        raise HTTPException(404, f"Project '{name}' not found")
    detail = data.read_project_detail(name)
    return {"markdown": detail.latest_review_md}


@router.get("/api/projects/{name}/log")
async def get_log(name: str, lines: int = Query(default=100, ge=1, le=5000)) -> dict:
    names = data.list_project_names()
    if name not in names:
        raise HTTPException(404, f"Project '{name}' not found")
    log_lines, log_file = data.read_log_lines(name, lines)
    return {"lines": log_lines, "log_file": log_file}


@router.get("/api/projects/{name}/pdf")
async def get_pdf(name: str):
    pdf_path = data.get_pdf_path(name)
    if not pdf_path or not pdf_path.exists():
        raise HTTPException(404, "No PDF found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)


@router.get("/api/events")
async def sse_stream(request: Request):
    queue = await watcher.subscribe()

    async def event_generator():
        try:
            # Send initial state
            names = data.list_project_names()
            for n in names:
                try:
                    summary = data.read_project_summary(n)
                    import json
                    msg = f"event: project_update\ndata: {json.dumps({'project': n, 'summary': summary.model_dump()})}\n\n"
                    yield msg
                except Exception:
                    pass

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield msg
                except asyncio.TimeoutError:
                    continue
        finally:
            watcher.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
