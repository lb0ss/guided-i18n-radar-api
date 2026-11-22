"""FastAPI app: the scheduler runs inside it; the web app (phase 5) talks to these routes."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlmodel import col, select

from . import replies, scheduler
from .db import Comment, DraftJob, MyPost, Opportunity, Run, Schedule, Stat, Step, session

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
def stats(points: int = 30) -> dict:
    """Latest value of every metric, the change since the check before, and its recent history for a trend line."""
    with session() as db:
        rows = db.exec(select(Stat).order_by(col(Stat.run_id))).all()
    series: dict[tuple[str, str], list[Stat]] = {}
    for r in rows:
        series.setdefault((r.source, r.metric), []).append(r)
    out = []
    for (source, metric), vals in series.items():
        last, prev = vals[-1], (vals[-2] if len(vals) > 1 else None)
        out.append({"source": source, "metric": metric, "value": last.value, "at": last.at,
                    "change": None if prev is None else last.value - prev.value,
                    "history": [{"at": v.at, "value": v.value} for v in vals[-points:]]})
    return {"stats": out}


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


@app.get("/posts")
def posts() -> list[dict]:
    with session() as db:
        return [p.model_dump() for p in db.exec(select(MyPost).order_by(MyPost.id)).all()]


@app.get("/opportunities")
def opportunities(state: str = "new") -> list[dict]:
    with session() as db:
        rows = db.exec(select(Opportunity).where(Opportunity.state == state).order_by(col(Opportunity.relevance).desc())).all()
        return [o.model_dump() for o in rows]


@app.post("/opportunities/{opp_id}/skip")
def skip_opportunity(opp_id: int) -> dict:
    with session() as db:
        o = db.get(Opportunity, opp_id)
        if o is None:
            raise HTTPException(404, "no such opportunity")
        o.state = "skipped"
        db.add(o)
        db.commit()
        return o.model_dump()


def _brief_path():
    from .config import ROOT, settings

    return ROOT / "projects" / settings().project / "brief.yaml"


@app.get("/brief")
def get_brief() -> dict:
    return {"text": _brief_path().read_text()}


class BriefText(BaseModel):
    text: str


@app.put("/brief")
def put_brief(body: BriefText) -> dict:
    import yaml

    try:
        data = yaml.safe_load(body.text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"not valid YAML: {e}") from e
    if not isinstance(data, dict) or not data.get("product") or not data.get("looking_for"):
        raise HTTPException(400, "the brief needs at least product and looking_for")
    _brief_path().write_text(body.text)
    return {"text": body.text}


class DraftRequest(BaseModel):
    kind: str = Field(description="opportunity | comment | pasted")
    id: int | None = None
    text: str | None = None
    channel: str | None = None
    post_id: int | None = None
    confirm: bool = False


@app.post("/drafts")
async def create_draft(req: DraftRequest) -> dict:
    """Without confirm=true this only returns the estimate; nothing is drafted or spent."""
    try:
        job = await replies.start(req.kind, req.id, req.text, req.channel, req.post_id, confirm=req.confirm)
    except replies.NeedsConfirmation as e:
        return {"needs_confirmation": True, "estimate": round(e.per_attempt, 3), "worst_case": round(e.worst, 3)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return replies.summary(job)


@app.get("/drafts/{job_id}")
def get_draft(job_id: int) -> dict:
    with session() as db:
        job = db.get(DraftJob, job_id)
    if job is None:
        raise HTTPException(404, "no such draft")
    return replies.summary(job)


class Posted(BaseModel):
    url: str
    text: str = Field(description="What you actually posted, after your edits")


@app.post("/drafts/{job_id}/posted")
def posted(job_id: int, body: Posted) -> dict:
    try:
        return replies.summary(replies.mark_posted(job_id, body.url, body.text))
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# The built web app (guided-i18n-radar-web), served from here so one service is enough. API routes above win.
def _mount_web() -> None:
    import os
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    from .config import ROOT

    dist = Path(os.environ.get("RADAR_WEB_DIST", ROOT.parent / "guided-i18n-radar-web" / "dist")).expanduser()
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")


_mount_web()
