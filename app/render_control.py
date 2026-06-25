"""Adaptive render-timeout controller (shared via the render_control table).

The maintenance worker calls evaluate() each cycle; render workers call
get_control() each poll. Policy: if the recent render error rate exceeds the
threshold, escalate the per-render timeout by a step up to a cap. If the error
rate is STILL over threshold at the cap, HALT the fleet (workers stop claiming)
so a human can diagnose instead of grinding. Resume clears the halt.
"""
from __future__ import annotations

import asyncpg

from app.config import Settings


async def get_control(conn: asyncpg.Connection, settings: Settings) -> dict:
    """Return the current control row, creating it (default timeout) if absent."""
    row = await conn.fetchrow(
        "SELECT timeout_seconds, halted, reason, error_rate FROM render_control WHERE id = 1"
    )
    if row is None:
        await conn.execute(
            "INSERT INTO render_control (id, timeout_seconds) VALUES (1, $1) "
            "ON CONFLICT (id) DO NOTHING",
            settings.render_timeout_seconds,
        )
        row = await conn.fetchrow(
            "SELECT timeout_seconds, halted, reason, error_rate FROM render_control WHERE id = 1"
        )
    return dict(row)


async def _update(conn: asyncpg.Connection, *, timeout: int, halted: bool,
                  reason: str | None, error_rate: float | None,
                  window_terminal: int) -> None:
    await conn.execute(
        """
        UPDATE render_control
        SET timeout_seconds = $1, halted = $2, reason = $3,
            error_rate = $4, window_terminal = $5, updated_at = now()
        WHERE id = 1
        """,
        timeout, halted, reason, error_rate, window_terminal,
    )


async def _recent_render_rate(conn: asyncpg.Connection, window_seconds: int) -> tuple[int, int]:
    """(terminal, errors) for recent render work.

    errors  = render rows that FAILED an attempt recently and are not yet done
              (parked 'error' or mid-retry 'queued'/'processing' with a recent
              last_error_at) — so ongoing timeout churn counts immediately rather
              than lagging by max_attempts.
    success = render rows that finished 'done' within the window.
    terminal = errors + success (the denominator).
    """
    row = await conn.fetchrow(
        """
        SELECT
            count(*) FILTER (
                WHERE status <> 'done'
                  AND last_error_at > now() - make_interval(secs => $1)
            ) AS errors,
            count(*) FILTER (
                WHERE status = 'done'
                  AND terminated_at > now() - make_interval(secs => $1)
            ) AS success
        FROM scan_queue
        WHERE tier = 'render'
        """,
        window_seconds,
    )
    errors = row["errors"] or 0
    success = row["success"] or 0
    return errors + success, errors


async def evaluate(conn: asyncpg.Connection, settings: Settings) -> dict:
    """Adjust the control row from the recent render error rate. Returns the new state.

    - already halted -> leave it (manual resume required)
    - too few samples -> record observation, no change
    - rate > threshold, below cap -> raise timeout by step
    - rate > threshold, at cap -> HALT for diagnosis
    - rate <= threshold -> healthy, hold timeout (no decay, avoids oscillation)
    """
    ctrl = await get_control(conn, settings)
    if ctrl["halted"]:
        return ctrl

    terminal, errors = await _recent_render_rate(conn, settings.render_control_window_seconds)
    rate = (errors / terminal) if terminal else None
    cur = ctrl["timeout_seconds"]
    thr = settings.render_error_rate_threshold

    if terminal < settings.render_control_min_sample:
        await _update(conn, timeout=cur, halted=False, reason=ctrl.get("reason"),
                      error_rate=rate, window_terminal=terminal)
        return await get_control(conn, settings)

    if rate is not None and rate > thr:
        if cur >= settings.render_timeout_max_seconds:
            await _update(
                conn, timeout=cur, halted=True,
                reason=(f"render error rate {rate:.1%} > {thr:.0%} at max timeout "
                        f"{cur}s ({errors}/{terminal}) — HALTED for diagnosis"),
                error_rate=rate, window_terminal=terminal)
        else:
            new_timeout = min(settings.render_timeout_max_seconds,
                              cur + settings.render_timeout_step_seconds)
            await _update(
                conn, timeout=new_timeout, halted=False,
                reason=(f"render error rate {rate:.1%} > {thr:.0%} ({errors}/{terminal}); "
                        f"raised timeout {cur}->{new_timeout}s"),
                error_rate=rate, window_terminal=terminal)
    else:
        rate_str = f"{rate:.1%}" if rate is not None else "n/a"
        await _update(conn, timeout=cur, halted=False,
                      reason=f"render error rate {rate_str} ok ({errors}/{terminal})",
                      error_rate=rate, window_terminal=terminal)
    return await get_control(conn, settings)


async def resume(conn: asyncpg.Connection, settings: Settings) -> None:
    """Clear a halt and reset the timeout to the configured start (post-diagnosis)."""
    await conn.execute(
        """
        UPDATE render_control
        SET halted = false, timeout_seconds = $1,
            reason = 'resumed after diagnosis', updated_at = now()
        WHERE id = 1
        """,
        settings.render_timeout_seconds,
    )
