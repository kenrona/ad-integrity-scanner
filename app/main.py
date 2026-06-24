"""FastAPI app: async fire-and-forget scan intake."""
from __future__ import annotations

import pathlib
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import benchmark, datasets, profiling, progress, queue, service
from app.config import get_settings
from app.db import close_pool, get_pool, init_pool
from app.logging_config import configure_logging, get_logger
from app.models import (
    BenchmarkResponse,
    DatasetCreate,
    DatasetRow,
    DatasetRowsResponse,
    DatasetSummary,
    JobStarted,
    JobStatus,
    ProfileResponse,
    ScanAccepted,
    ScanBatchRequest,
    ScanRequest,
    ScanStatus,
)

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(get_settings().log_level)
    await init_pool()
    log.info("api ready")
    yield
    await close_pool()


app = FastAPI(title="Ad Integrity Scanner", version="0.1.0", lifespan=lifespan)

_STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_SOURCES_HTML = _STATIC_DIR / "sources.html"
_PROFILE_HTML = _STATIC_DIR / "profile.html"
_DASHBOARD_HTML = _STATIC_DIR / "dashboard.html"

# Serve static assets (e.g. /static/progress.js used by the new pages).
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Browser GUI: pick a file of URLs and submit them one at a time."""
    return FileResponse(_INDEX_HTML)


@app.get("/sources", include_in_schema=False)
async def sources_page() -> FileResponse:
    """Source-picker GUI: load datasets and trigger dataset scans."""
    return FileResponse(_SOURCES_HTML)


@app.get("/profile", include_in_schema=False)
async def profile_page() -> FileResponse:
    """Data-profiling GUI for a chosen dataset."""
    return FileResponse(_PROFILE_HTML)


@app.get("/dashboard", include_in_schema=False)
async def dashboard_page() -> FileResponse:
    """Baseline-vs-publisher benchmark dashboard GUI."""
    return FileResponse(_DASHBOARD_HTML)


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


# ===========================================================================
# Datasets, ingestion, dataset scans
# ===========================================================================
async def _run_bg(job_id: int, coro) -> None:
    """Background-task wrapper: run coro; on unexpected error mark the job failed.

    The dataset module helpers already finish/fail their own job_runs row; this is
    a safety net so a job is never left 'running' if the coroutine raises before
    its own handler records the failure.
    """
    try:
        await coro
    except Exception as e:  # noqa: BLE001 — record on the job row for the UI poll
        log.warning("background job failed job_id=%s err=%r", job_id, e)
        try:
            async with get_pool().acquire() as conn:
                await progress.fail(conn, job_id, repr(e))
        except Exception:  # noqa: BLE001 — best-effort; original error already logged
            pass


@app.get("/datasets", response_model=list[DatasetSummary])
async def list_datasets_endpoint() -> list[DatasetSummary]:
    async with get_pool().acquire() as conn:
        rows = await datasets.list_datasets(conn)
    return [DatasetSummary(**r) for r in rows]


@app.post("/datasets", response_model=DatasetSummary, status_code=status.HTTP_201_CREATED)
async def create_dataset_endpoint(req: DatasetCreate) -> DatasetSummary:
    """Create an empty dataset (no rows yet)."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await datasets.create_dataset(
                conn, name=req.name, kind=req.kind, notes=req.notes,
            )
            rows = await datasets.list_datasets(conn)
    except asyncpg.UniqueViolationError as e:
        raise HTTPException(status_code=409, detail=f"dataset name already exists: {req.name}") from e
    match = next((r for r in rows if r["name"] == req.name), None)
    if match is None:  # pragma: no cover — just inserted
        raise HTTPException(status_code=500, detail="dataset created but not found")
    return DatasetSummary(**match)


@app.post("/datasets/load", response_model=JobStarted, status_code=status.HTTP_202_ACCEPTED)
async def load_dataset_endpoint(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    name: str = Form(...),
    kind: str = Form(...),
    url_column: str | None = Form(None),
) -> JobStarted:
    """Create-or-reuse a dataset by name, parse the uploaded file, ingest rows in
    the background, and return a job_id the UI polls."""
    if kind not in ("baseline", "publisher"):
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind!r}")
    url_col = url_column or None  # empty form field -> None

    data = await file.read()
    filename = file.filename or "upload"
    try:
        parsed = datasets.parse_source(filename, data, url_column=url_col)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"could not parse source: {e}") from e

    pool = get_pool()
    # Resolve dataset: reuse by name if it exists, else create.
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM datasets WHERE name = $1", name)
        if existing is not None:
            dataset_id = existing["id"]
        else:
            try:
                dataset_id = await datasets.create_dataset(
                    conn, name=name, kind=kind, source_file=filename,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        job_id = await progress.create_job(
            conn, kind="ingest", total=len(parsed.urls),
            message="queued for ingestion", dataset_id=dataset_id,
        )

    batch_size = get_settings().ingest_batch_size
    background.add_task(
        _run_bg, job_id,
        datasets.ingest(pool, dataset_id=dataset_id, parsed=parsed,
                        job_id=job_id, batch_size=batch_size),
    )
    return JobStarted(job_id=job_id, dataset_id=dataset_id)


@app.get("/datasets/{dataset_id}/rows", response_model=DatasetRowsResponse)
async def dataset_rows_endpoint(
    dataset_id: int, limit: int | None = None, offset: int = 0,
) -> DatasetRowsResponse:
    page = limit if limit is not None else get_settings().dataset_rows_page_size
    async with get_pool().acquire() as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM dataset_rows WHERE dataset_id = $1", dataset_id,
        )
        rows = await datasets.get_dataset_rows(conn, dataset_id, limit=page, offset=offset)
    return DatasetRowsResponse(
        dataset_id=dataset_id,
        total=total or 0,
        rows=[DatasetRow(**r) for r in rows],
    )


@app.post("/datasets/{dataset_id}/scan", response_model=JobStarted,
          status_code=status.HTTP_202_ACCEPTED)
async def scan_dataset_endpoint(
    dataset_id: int, background: BackgroundTasks, req: ScanBatchRequest | None = None,
) -> JobStarted:
    """Kick off scans for a dataset (optionally a sampled subset) in the background."""
    settings = get_settings()
    throttle = (
        req.throttle_ms if req is not None and req.throttle_ms is not None
        else settings.scan_batch_throttle_ms
    )
    sample_rate = req.sample_rate if req is not None else 1.0
    pool = get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM datasets WHERE id = $1", dataset_id)
        if not exists:
            raise HTTPException(status_code=404, detail=f"no dataset {dataset_id}")
        job_id = await progress.create_job(
            conn, kind="scan_batch",
            message=f"queued scan batch (sample {sample_rate:.0%})", dataset_id=dataset_id,
        )
    background.add_task(
        _run_bg, job_id,
        datasets.scan_dataset(pool, dataset_id=dataset_id, job_id=job_id,
                              throttle_ms=throttle, sample_rate=sample_rate),
    )
    return JobStarted(job_id=job_id, dataset_id=dataset_id)


# ===========================================================================
# Profiling, benchmark, jobs
# ===========================================================================
@app.get("/datasets/{dataset_id}/profile", response_model=ProfileResponse)
async def profile_dataset_endpoint(dataset_id: int) -> ProfileResponse:
    """Synchronous per-metric profile of a dataset (bounded sizes)."""
    async with get_pool().acquire() as conn:
        result = await profiling.profile_dataset(conn, dataset_id)
    return ProfileResponse(**result)


@app.get("/benchmark", response_model=BenchmarkResponse)
async def benchmark_endpoint(
    publisher_dataset_id: int, baseline_name: str = "baseline",
) -> BenchmarkResponse:
    """Baseline-vs-publisher comparison from the benchmark matview."""
    try:
        async with get_pool().acquire() as conn:
            result = await benchmark.compare(
                conn, publisher_dataset_id=publisher_dataset_id,
                baseline_name=baseline_name,
            )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return BenchmarkResponse(**result)


@app.post("/benchmark/refresh", response_model=JobStarted,
          status_code=status.HTTP_202_ACCEPTED)
async def benchmark_refresh_endpoint(background: BackgroundTasks) -> JobStarted:
    """Refresh the benchmark matview in the background; return a job_id to poll."""
    pool = get_pool()
    async with pool.acquire() as conn:
        job_id = await progress.create_job(
            conn, kind="refresh", message="refreshing benchmark matview",
        )

    async def _refresh() -> None:
        async with pool.acquire() as conn:
            try:
                await benchmark.refresh_benchmark(conn)
            except Exception as e:  # noqa: BLE001 — record + re-raise for the wrapper
                await progress.fail(conn, job_id, repr(e))
                raise
            await progress.finish(conn, job_id, message="benchmark refreshed")

    background.add_task(_run_bg, job_id, _refresh())
    return JobStarted(job_id=job_id)


@app.get("/jobs/{job_id}", response_model=JobStatus)
async def job_status_endpoint(job_id: int) -> JobStatus:
    async with get_pool().acquire() as conn:
        job = await progress.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return JobStatus(**job)
