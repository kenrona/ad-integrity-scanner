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
