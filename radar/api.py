"""FastAPI app: the scheduler runs inside it; the web app (phase 5) talks to these routes."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlmodel import col, select

from . import scheduler
from .db import Comment, Run, Schedule, Stat, Step, session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.ensure_defaults()
    task = asyncio.create_task(scheduler.loop())
    yield
    task.cancel()


app = FastAPI(title="guided-i18n Radar", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


class ScheduleUpdate(BaseModel):
    interval_hours: float | None = Field(default=None, ge=1, le=24 * 30)
    enabled: bool | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/schedule")
def get_schedule() -> list[dict]:
    with session() as db:
        rows = db.exec(select(Schedule)).all()
    return [{**r.model_dump(), "built": r.job in scheduler.JOBS} for r in rows]


@app.put("/schedule/{job}")
def put_schedule(job: str, update: ScheduleUpdate) -> dict:
    with session() as db:
        s = db.get(Schedule, job)
        if s is None:
            raise HTTPException(404, f"no job {job!r}")
        if update.enabled and job not in scheduler.JOBS:
            raise HTTPException(409, f"{job} isn't built yet")
        if update.interval_hours is not None:
            s.interval_hours = update.interval_hours
        if update.enabled is not None:
            s.enabled = update.enabled
        db.add(s)
        db.commit()
        return s.model_dump()  # the scheduler reads it on its next tick


@app.post("/run/{job}")
async def run_now(job: str) -> dict:
    if job not in scheduler.JOBS:
        raise HTTPException(404, f"{job} isn't built yet")
    result = await scheduler.run_job(job)
    return {k: v for k, v in result.items() if k != "text"} | {"digest_text": result.get("text")}


@app.get("/runs")
def runs(limit: int = 20) -> list[dict]:
    with session() as db:
        rows = db.exec(select(Run).order_by(col(Run.id).desc()).limit(limit)).all()
        steps = db.exec(select(Step).where(col(Step.run_id).in_([r.id for r in rows]))).all()
    return [{**r.model_dump(), "steps": [s.model_dump() for s in steps if s.run_id == r.id]} for r in rows]


@app.get("/stats")
def stats() -> dict:
    with session() as db:
        last = db.exec(select(Stat.run_id).order_by(col(Stat.run_id).desc())).first()
        rows = db.exec(select(Stat).where(Stat.run_id == last)).all() if last else []
    return {"run": last, "stats": [{"source": r.source, "metric": r.metric, "value": r.value, "at": r.at} for r in rows]}


@app.get("/comments")
def comments(state: str = "new") -> list[dict]:
    with session() as db:
        return [c.model_dump() for c in db.exec(select(Comment).where(Comment.state == state).order_by(col(Comment.created_at).desc())).all()]


@app.post("/comments/{comment_id}/dismiss")
def dismiss(comment_id: int) -> dict:
    with session() as db:
        c = db.get(Comment, comment_id)
        if c is None:
            raise HTTPException(404, "no such comment")
        c.state = "dismissed"
        db.add(c)
        db.commit()
        return c.model_dump()
