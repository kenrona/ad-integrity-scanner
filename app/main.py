"""FastAPI app: async fire-and-forget scan intake."""
from __future__ import annotations

import pathlib
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse

from app import queue, service
from app.config import get_settings
from app.db import close_pool, get_pool, init_pool
from app.logging_config import configure_logging, get_logger
from app.models import ScanAccepted, ScanRequest, ScanStatus

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(get_settings().log_level)
    await init_pool()
    log.info("api ready")
    yield
    await close_pool()


app = FastAPI(title="Ad Integrity Scanner", version="0.1.0", lifespan=lifespan)

_INDEX_HTML = pathlib.Path(__file__).resolve().parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Browser GUI: pick a file of URLs and submit them one at a time."""
    return FileResponse(_INDEX_HTML)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/scan", response_model=ScanAccepted, status_code=status.HTTP_202_ACCEPTED)
async def scan(req: ScanRequest, response: Response) -> ScanAccepted:
    """Accept a URL, dedup/enqueue, return 202 immediately (fire-and-forget)."""
    try:
        accepted = await service.submit_scan(get_pool(), req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid url: {e}") from e
    # A 'fresh' hit is a cached read, not new work — reflect that in the status code.
    if accepted.status == "fresh":
        response.status_code = status.HTTP_200_OK
    return accepted


@app.get("/scan/{scan_id}", response_model=ScanStatus)
async def scan_status(scan_id: uuid.UUID) -> ScanStatus:
    return await service.get_status(get_pool(), scan_id)


@app.get("/stats")
async def stats() -> dict:
    """Queue depth by tier/status + total completed results."""
    async with get_pool().acquire() as conn:
        return await queue.get_stats(conn)
