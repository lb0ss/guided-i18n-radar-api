"""Runs jobs when they're due. A tick every few minutes compares each job's last run with its interval,
so a Mac that was asleep or off catches up with one run when it wakes, never several."""

import asyncio
import logging
from datetime import UTC, timedelta

from sqlmodel import select

from .checks import run_checks
from .db import Schedule, now, session

log = logging.getLogger("radar.scheduler")
TICK_SECONDS = 300
DEFAULTS = {"checks": (24.0, True), "research": (72.0, False)}
JOBS = {"checks": run_checks}  # research arrives in build phase 3
_locks: dict[str, asyncio.Lock] = {}


def ensure_defaults() -> None:
    with session() as db:
        for job, (hours, enabled) in DEFAULTS.items():
            if db.get(Schedule, job) is None:
                db.add(Schedule(job=job, interval_hours=hours, enabled=enabled and job in JOBS))
        db.commit()


def is_due(s: Schedule, at=None) -> bool:
    if not s.enabled or s.job not in JOBS:
        return False
    if s.last_run_at is None:
        return True
    last = s.last_run_at if s.last_run_at.tzinfo else s.last_run_at.replace(tzinfo=UTC)
    return (at or now()) - last >= timedelta(hours=s.interval_hours)


async def run_job(job: str) -> dict:
    lock = _locks.setdefault(job, asyncio.Lock())
    if lock.locked():
        return {"skipped": f"{job} is already running"}
    async with lock:
        with session() as db:
            s = db.get(Schedule, job)
            s.last_run_at = now()  # set first: a failing job waits for its next interval instead of retrying every tick
            db.add(s)
            db.commit()
        return await JOBS[job]()


async def loop() -> None:
    ensure_defaults()
    while True:
        with session() as db:
            due = [s.job for s in db.exec(select(Schedule)).all() if is_due(s)]
        for job in due:
            try:
                result = await run_job(job)
                log.info("%s: %s", job, {k: v for k, v in result.items() if k != "text"})
            except Exception:
                log.exception("%s failed", job)
        await asyncio.sleep(TICK_SECONDS)
