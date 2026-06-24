"""API request/response schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    # Cap length to bound memory / abuse; 2048 is a common practical URL limit.
    url: str = Field(..., min_length=1, max_length=2048, description="Complete page URL to scan.")


class ScanAccepted(BaseModel):
    scan_id: uuid.UUID
    url: str
    url_hash: str
    domain: str
    # queued = new work enqueued; fresh = served from a non-expired prior scan;
    # inflight = an identical scan is already queued/processing.
    status: Literal["queued", "fresh", "inflight"]


class ScanStatus(BaseModel):
    scan_id: uuid.UUID
    url_hash: str
    url: str
    domain: str
    state: Literal["queued", "processing", "done", "error", "unknown"]
    scan_tier: str | None = None
    integrity_score: float | None = None
    confidence: float | None = None
    sub_scores: dict[str, Any] | None = None
    score_breakdown: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    scanned_at: dt.datetime | None = None
    last_error: str | None = None


# ---------------------------------------------------------------------------
# Datasets / jobs / profiling / benchmark
# ---------------------------------------------------------------------------
class DatasetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    kind: Literal["baseline", "publisher"]
    notes: str | None = Field(None, max_length=2000)


class DatasetSummary(BaseModel):
    id: int
    name: str
    kind: Literal["baseline", "publisher"]
    source_file: str | None = None
    row_count: int = 0
    scanned_count: int = 0
    created_at: dt.datetime


class JobStarted(BaseModel):
    job_id: int
    dataset_id: int | None = None


class JobStatus(BaseModel):
    id: int
    kind: str
    status: Literal["running", "done", "error"]
    total: int
    done: int
    message: str | None = None
    dataset_id: int | None = None
    error: str | None = None
    started_at: dt.datetime
    finished_at: dt.datetime | None = None


class ScanBatchRequest(BaseModel):
    throttle_ms: int | None = Field(None, ge=0, le=60000,
        description="Override per-URL submit delay; defaults to settings.scan_batch_throttle_ms.")
    sample_rate: float = Field(1.0, gt=0.0, le=1.0,
        description="Fraction of the dataset's rows to scan (deterministic by url_hash). 0.1 = 10% sample.")


class DatasetRow(BaseModel):
    url: str
    domain: str
    integrity_score: float | None = None
    scan_tier: str | None = None
    scanned_at: dt.datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class DatasetRowsResponse(BaseModel):
    dataset_id: int
    total: int
    rows: list[DatasetRow]


class NumericStat(BaseModel):
    metric: str
    count: int
    null_pct: float
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    distinct: int


class CategoryCount(BaseModel):
    value: str | None = None
    n: int


class CategoricalStat(BaseModel):
    metric: str
    distribution: list[CategoryCount]


class HistogramBucket(BaseModel):
    bucket: str
    n: int


class ProfileResponse(BaseModel):
    dataset_id: int
    row_count: int
    scanned_count: int
    numeric: list[NumericStat]
    categorical: list[CategoricalStat]
    integrity_histogram: list[HistogramBucket]


class BenchmarkMetric(BaseModel):
    metric: str
    baseline_mean: float | None = None
    baseline_median: float | None = None
    publisher_mean: float | None = None
    publisher_median: float | None = None
    delta_mean: float | None = None
    delta_median: float | None = None
    pct_of_baseline: float | None = None


class BenchmarkResponse(BaseModel):
    publisher: dict[str, Any]
    baseline: dict[str, Any]
    metrics: list[BenchmarkMetric]
    distributions: dict[str, dict[str, list[HistogramBucket]]]
