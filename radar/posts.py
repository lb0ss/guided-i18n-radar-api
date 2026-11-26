"""Your own X posts: topic suggestions from Radar's data, and drafting through the same write ⇄ evaluate
loop as replies. Nothing is ever posted for you.

The X brief (docs/launch/x.md in the extension repo: strategy, format rules, allowed claims, what's been
posted) is the writer's and evaluator's guide; FACTS.md stays the source of truth for claims."""

import os
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlmodel import col, select

from .config import Settings, settings
from .db import Comment, MyPost, Opportunity, PostTopic, now, session
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

