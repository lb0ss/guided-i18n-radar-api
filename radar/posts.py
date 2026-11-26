"""Your own X posts: topic suggestions from Radar's data, drafting through the same write ⇄ evaluate loop
as replies, and the scheduled job that has a draft waiting for you. Nothing is ever posted for you.

The X brief (docs/launch/x.md in the extension repo: strategy, format rules, allowed claims, what's been
posted) is the writer's and evaluator's guide; FACTS.md stays the source of truth for claims."""

import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlmodel import col, select

from . import notify
from .config import Settings, settings
from .db import Comment, MyPost, Opportunity, PostTopic, Run, Step, now, session
from .llm import call
from .writing import facts

PLANNER = "claude-haiku-5-5"
CHANNEL = "x-post"

Source = Literal["release", "feedback", "question", "intel", "candidate", "stats"]


class Topic(BaseModel):
    title: str = Field(description="A short name for the post, e.g. 'Undo that keeps your edits'.")
    angle: str = Field(description="One sentence: the single point the post makes.")
    evidence: str = Field(description="What in the data below supports it, quoted or named (a release, a comment, an intel thread...).")
    source: Source


class Topics(BaseModel):
    items: list[Topic] = Field(description="Three topics, best first.")


SUGGEST = """\
You suggest topics for the next X post by the solo developer of guided-i18n, a VS Code extension for
React i18n. The posts are build-in-public: one concrete thing per post (a fix, a bug, a lesson, a number),
honest, never an ad. Follow the X brief below: its strategy, its allowed claims, and never a topic it
lists as already posted.

Prefer, in order: real user feedback that led to a change; a recent release; a question people actually
asked; something people struggle with (intel); the brief's own candidates. Every topic must be provable
from the facts; nothing about future work. Give exactly three, best first, each with the evidence for it.

<x_brief>
{brief}
</x_brief>

{facts}"""

POST_INSTRUCTION = """\
Write a new post for X, posted by the developer in the Build in Public community. Follow the X brief
(format rules, allowed claims; don't repeat a topic it lists as posted) and make the one point in the topic.
Also describe one image that would make the point at a glance (a before/after card, a code snippet, a
screenshot), and write its alt text. Store links never go in the post: they go in the first reply."""


def repo() -> Path:
    return Path(os.environ.get("I18N_CORE_PATH", "~/repos/i18n-ai-core")).expanduser()


def x_brief() -> str:
    try:
        return (repo() / "docs/launch/x.md").read_text()
    except OSError:
        return "(no X brief found: docs/launch/x.md)"


def first_reply(s: Settings | None = None) -> str:
    """The store links, built from sources.yaml, so they're always right."""
    st = (s or settings()).sources.get("stats", {})
    lines = []
    if m := st.get("marketplace"):
        lines.append(f"VS Code Marketplace: https://marketplace.visualstudio.com/items?itemName={m}")
    if o := st.get("openvsx"):
        lines.append(f"Open VSX (Cursor, Windsurf): https://open-vsx.org/extension/{o}")
    return "\n".join(lines)


def _context() -> str:
    """What Radar knows that a post could be about: recent comments and replies, intel, your past X posts."""
    since = now() - timedelta(days=30)
    with session() as db:
        comments = db.exec(select(Comment).where(Comment.created_at >= since).order_by(col(Comment.created_at).desc()).limit(15)).all()
        intel = db.exec(select(Opportunity).where(Opportunity.state.in_(["intel", "new"]), Opportunity.found_at >= since)
                        .order_by(col(Opportunity.relevance).desc()).limit(15)).all()
        mine = db.exec(select(MyPost).where(MyPost.platform == "x").order_by(col(MyPost.id).desc()).limit(10)).all()
        used = db.exec(select(PostTopic).where(col(PostTopic.status).in_(["drafted", "posted"]))).all()
    parts = ["<comments_on_your_posts_last_30_days>"] + [f"- {c.author} ({c.platform}): {' '.join(c.body.split())[:200]}" for c in comments]
    parts += ["</comments_on_your_posts_last_30_days>", "<intel_and_threads_last_30_days>"]
    parts += [f"- [{o.category}] {o.title}: {o.angle}" for o in intel] + ["</intel_and_threads_last_30_days>", "<your_x_posts_recorded_by_radar>"]
    parts += [f"- {' '.join(p.text.split())[:240]}" for p in mine if p.text] + ["</your_x_posts_recorded_by_radar>", "<topics_already_drafted_or_posted>"]
    parts += [f"- {t.title}" for t in used] + ["</topics_already_drafted_or_posted>"]
    return "\n".join(parts)


async def suggest_topics(llm=call) -> tuple[list[PostTopic], float]:
    """One planner call (Claude Haiku, under a cent): three topics with their evidence, saved as new."""
    r = await llm(PLANNER, SUGGEST.format(brief=x_brief(), facts=facts()), _context(), Topics)
    if r.output is None:
        raise RuntimeError(f"topic suggestions failed: {r.error}")
    each = round(r.cost / max(len(r.output.items), 1), 5)
    rows = [PostTopic(title=t.title, angle=t.angle, evidence=t.evidence, source=t.source, cost=each) for t in r.output.items[:3]]
    with session() as db:
        db.add_all(rows)
        db.commit()
        for row in rows:
            db.refresh(row)
    return rows, r.cost


def thread_for(topic: PostTopic | None, text: str | None) -> list[str]:
    """What the writer gets in place of a thread: the topic and the X brief."""
    what = f"Topic: {topic.title}\nAngle: {topic.angle}\nEvidence: {topic.evidence}" if topic else f"Topic: {text}"
    return [f"<x_brief>\n{x_brief()}\n</x_brief>", what]


async def run_posts(s: Settings | None = None, llm=call) -> dict:
    """The scheduled job: suggest topics, draft the best one, and tell you a draft is waiting."""
    from . import replies

    s = s or settings()
    with session() as db:
        run = Run(job="posts")
        db.add(run)
        db.commit()
        db.refresh(run)

    def step(name: str, t: float, status: str = "ok", detail: str = "") -> None:
        with session() as db:
            db.add(Step(run_id=run.id, name=name, status=status, seconds=round(time.monotonic() - t, 2), detail=detail[:500]))
            db.commit()

    cost, job, top, failed = 0.0, None, None, []
    t = time.monotonic()
    try:
        topics, cost = await suggest_topics(llm)
        top = topics[0]
        step("suggest topics", t)
    except Exception as e:  # noqa: BLE001 - reported in the run and the notification
        step("suggest topics", t, "failed", f"{type(e).__name__}: {e}")
        failed.append("suggest topics")
    if top:
        t = time.monotonic()
        try:
            job = await replies.start("post", top.id, confirm=True, llm=llm)  # scheduled: the cost is the schedule's
            cost += job.cost
            step(f"draft: {top.title}"[:120], t, "ok" if job.status == "pass" else "failed",
                 "" if job.status == "pass" else f"draft ended as '{job.status}': see Drafts")
        except Exception as e:  # noqa: BLE001
            step(f"draft: {top.title}"[:120], t, "failed", f"{type(e).__name__}: {e}")
            failed.append("draft")

    text = "# Radar: X post draft\n\n" + (
        f"Topic: **{top.title}**: {top.angle}\n\nEvidence: {top.evidence}\n\nDraft status: {job.status if job else 'not drafted'}. "
        "Review it under Drafts in Radar; nothing is posted for you.\n" if top else "No topics this time.\n")
    if failed:
        text += "\n## Problems\n\n" + "\n".join(f"- {f}" for f in failed) + "\n"
    notify.save(s, now().astimezone().strftime("%Y-%m-%d-%H%M%S") + "-post", text)
    short = f"Topic: {top.title}. Draft {job.status if job else 'failed'}: review it in Radar." if top else "Topic suggestions failed."
    sent = notify.send(s, f"Radar: X post drafted ({top.title})" if job else "Radar: X post draft failed", text, short)

    with session() as db:
        r = db.get(Run, run.id)
        r.finished_at, r.cost, r.status = now(), round(cost, 4), ("partial" if failed or (job and job.status != "pass") else "ok")
        db.add(r)
        db.commit()
    return {"run": run.id, "topic": top.title if top else None, "draft": job.id if job else None,
            "status": job.status if job else "failed", "cost": round(cost, 4), "failed": failed, "notified": sent, "text": text}
