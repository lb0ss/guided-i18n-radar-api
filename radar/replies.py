"""Drafting: resolve what's being answered (or the post's topic), confirm the cost, run the loop, record what was posted."""

import json

import httpx
from sqlmodel import select

from . import posts
from .db import Comment, DraftJob, MyPost, Opportunity, PostPlan, PostTopic, now, session
from .threads import CHANNEL_FOR, from_comment, from_opportunity, from_paste
from .writing import channels, draft, estimate


class NeedsConfirmation(Exception):
    def __init__(self, per_attempt: float, worst: float):
        super().__init__(f"about ${per_attempt:.2f}, up to ${worst:.2f} with retries")
        self.per_attempt, self.worst = per_attempt, worst


async def fill_post_text(p: MyPost) -> MyPost:
    """dev.to posts come in without their text; fetch it once (public API) so replies get context."""
    if p.text or p.platform != "devto":
        return p
    path = p.url.removeprefix("https://dev.to/")
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "guided-i18n-radar"}) as c:
        r = await c.get(f"https://dev.to/api/articles/{path}")
        r.raise_for_status()
    with session() as db:
        q = db.get(MyPost, p.id)
        q.text = r.json().get("body_markdown", "")[:8000]
        db.add(q)
        db.commit()
        db.refresh(q)
        return q


async def start(kind: str, ref: int | None = None, text: str | None = None, channel: str | None = None,
                post_id: int | None = None, confirm: bool = False, llm=None) -> DraftJob:
    """kind: opportunity (ref = Opportunity.id), comment (ref = Comment.id), pasted (text, channel, optional post_id)
    or post: a new X post on a suggested topic (ref = PostTopic.id) or your own (text).
    Raises NeedsConfirmation unless confirm=True: nothing is drafted, or paid for, without it."""
    if kind == "opportunity":
        with session() as db:
            o = db.get(Opportunity, ref)
        if o is None:
            raise ValueError(f"no opportunity {ref}")
        channel = channel or CHANNEL_FOR[o.source]
    elif kind == "comment":
        with session() as db:
            c = db.get(Comment, ref)
            p = db.exec(select(MyPost).where(MyPost.url == c.post_url)).first() if c else None
        if c is None:
            raise ValueError(f"no comment {ref}")
        if p:
            await fill_post_text(p)
        channel = channel or CHANNEL_FOR.get(c.platform, "github-comment")
    elif kind == "pasted":
        if not text or not channel:
            raise ValueError("pasted needs text and channel")
    elif kind == "post":
        topic = None
        if ref is not None:
            with session() as db:
                topic = db.get(PostTopic, ref)
            if topic is None:
                raise ValueError(f"no topic {ref}")
        elif not (text or "").strip():
            raise ValueError("post needs a topic: a suggested one (id) or your own (text)")
        channel = channel or posts.CHANNEL
    else:
        raise ValueError(f"unknown kind {kind!r}")
    if channel not in channels():
        raise ValueError(f"unknown channel {channel!r}; see projects/<name>/channels.yaml")
    if not confirm:
        raise NeedsConfirmation(*estimate())

    kwargs = {"llm": llm} if llm else {}
    if kind == "opportunity":
        job = await draft("opportunity", channel, await from_opportunity(o), source_id=o.id, **kwargs)
        _set_state(Opportunity, o.id, "drafted")
    elif kind == "comment":
        thread, pid = from_comment(c)
        job = await draft("comment", channel, thread, source_id=c.id, post_id=pid, **kwargs)
        _set_state(Comment, c.id, "drafted")
    elif kind == "post":
        extras: dict = {}
        job = await draft("post", channel, posts.thread_for(topic, text), source_id=topic.id if topic else None,
                          instruction=posts.POST_INSTRUCTION, on_text=lambda out: extras.update(visual=out.visual, alt_text=out.alt_text), **kwargs)
        with session() as db:
            db.add(PostPlan(job_id=job.id, topic=topic.title if topic else text.strip(), first_reply=posts.first_reply(), **extras))
            db.commit()
        if topic:
            _set_status(topic.id, "drafted")
    else:
        job = await draft("pasted", channel, from_paste(text, post_id), post_id=post_id, **kwargs)
    return job


def _set_status(topic_id: int, status: str) -> None:
    with session() as db:
        t = db.get(PostTopic, topic_id)
        t.status = status
        db.add(t)
        db.commit()


def _set_state(model, id_: int, state: str) -> None:
    with session() as db:
        row = db.get(model, id_)
        row.state = state
        db.add(row)
        db.commit()


def mark_posted(job_id: int, url: str, posted_text: str) -> DraftJob:
    """You posted it (yourself). Keeps the final text for calibration and adds it to your posts if it's a new post."""
    with session() as db:
        j = db.get(DraftJob, job_id)
        if j is None:
            raise ValueError(f"no draft {job_id}")
        j.posted_url, j.posted_text = url, posted_text
        db.add(j)
        if j.kind == "opportunity" and j.source_id:
            o = db.get(Opportunity, j.source_id)
            o.state = "posted"
            db.add(o)
        if j.kind == "comment" and j.source_id:
            c = db.get(Comment, j.source_id)
            c.state = "posted"
            db.add(c)
        if j.kind == "post" and j.source_id:
            t = db.get(PostTopic, j.source_id)
            t.status = "posted"
            db.add(t)
        if j.channel.endswith("-post") and not db.exec(select(MyPost).where(MyPost.url == url)).first():
            first = posted_text.strip().splitlines()[0][:120] if posted_text.strip() else url
            db.add(MyPost(platform=j.channel.split("-")[0], url=url, title=first, text=posted_text, posted_at=now()))
        db.commit()
        db.refresh(j)
        return j


def summary(job: DraftJob) -> dict:
    from .writing import attempts

    plan = None
    if job.kind == "post":
        with session() as db:
            p = db.exec(select(PostPlan).where(PostPlan.job_id == job.id)).first()
        plan = p.model_dump() if p else None
    return {**job.model_dump(), "plan": plan, "thread": json.loads(job.thread), "attempts": [
        {**a.model_dump(), **{k: json.loads(getattr(a, k) or ("{}" if k == "scores" else "[]"))
                              for k in ("rule_failures", "rule_warnings", "scores", "problems", "unsupported")}}
        for a in attempts(job.id)]}
