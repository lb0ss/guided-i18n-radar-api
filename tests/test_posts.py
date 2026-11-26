"""Your own X posts: topic suggestions, drafting through the write ⇄ evaluate loop, marking as posted.
A scripted model stands in for the real ones."""

import asyncio
import json

import pytest
from sqlmodel import select

from radar import posts, replies
from radar.db import Comment, MyPost, PostPlan, PostTopic, now, session
from radar.llm import Result
from radar.writing import PostText, Verdict

from .test_writing import GOOD, core  # noqa: F401 - core is a fixture

BRIEF = "# X brief\n\n## Posted\n\n| 4 | 2026-11-22 | Spanish UI bug |\n"


@pytest.fixture
def xcore(core):  # noqa: F811
    (core / "docs/launch/x.md").write_text(BRIEF)
    return core


class Model:
    """Topics, post texts and verdicts in order; records every call."""

    def __init__(self, texts=("Undo used to wipe your edits. Since 0.1.3 it checks first.",), verdicts=("pass",)):
        self.texts, self.verdicts, self.calls = list(texts), list(verdicts), []

    async def __call__(self, model, system, user, schema):
        self.calls.append((model, schema.__name__, system, user))
        if schema is posts.Topics:
            return Result(model, posts.Topics(items=[
                posts.Topic(title="Undo that keeps your edits", angle="Undo checks before it reverts.", evidence="r/SideProject comment", source="feedback"),
                posts.Topic(title="Plurals", angle="Russian needs four forms.", evidence="FACTS.md", source="candidate"),
                posts.Topic(title="0.1.4 translations", angle="109 strings improved.", evidence="CHANGELOG 0.1.4", source="release")]), 9000, 400)
        if schema is PostText:
            return Result(model, PostText(text=self.texts.pop(0), visual="The Undo prompt", alt_text="A VS Code prompt offering Undo"), 7000, 300)
        return Result(model, Verdict(verdict=self.verdicts.pop(0), scores=GOOD, problems=[], unsupported_claims=[]), 8000, 400)


def test_topic_suggestions_use_radars_data_and_skip_what_was_posted(s, xcore):
    with session() as db:
        db.add(Comment(platform="devto", external_id="devto:1", post_url="u", url="u", author="reader", body="Does Undo keep my edits?", created_at=now()))
        db.add(PostTopic(title="Same word, different meaning", angle="", evidence="", source="feedback", status="posted"))
        db.commit()
    m = Model()
    rows, cost = asyncio.run(posts.suggest_topics(m))
    assert [t.title for t in rows] == ["Undo that keeps your edits", "Plurals", "0.1.4 translations"] and cost > 0
    _, schema, system, user = m.calls[0]
    assert schema == "Topics" and "<x_brief>" in system and "Spanish UI bug" in system  # the brief, with what's posted
    assert "Does Undo keep my edits?" in user and "Same word, different meaning" in user  # real questions; used topics


def test_a_post_is_only_drafted_after_confirmation(s, xcore):
    t = asyncio.run(posts.suggest_topics(Model()))[0][0]
    with pytest.raises(replies.NeedsConfirmation):
        asyncio.run(replies.start("post", t.id))


def test_drafting_a_post_keeps_the_image_idea_alt_text_and_links(s, xcore):
    t = asyncio.run(posts.suggest_topics(Model()))[0][0]
    m = Model(texts=["x" * 300, "Undo used to wipe your edits. Since 0.1.3 it checks first."])  # first try too long for X
    job = asyncio.run(replies.start("post", t.id, confirm=True, llm=m))
    assert job.kind == "post" and job.channel == "x-post" and job.status == "pass"
    assert [c[1] for c in m.calls] == ["PostText", "PostText", "Verdict"]  # the rule failure went back without the evaluator
    task = json.loads(m.calls[0][3])
    assert "<x_brief>" in task["thread"][0] and "Topic: Undo that keeps your edits" in task["thread"][1]
    assert task["you_are_the_original_poster"] is True  # your own post: no "I built it" needed
    with session() as db:
        plan = db.exec(select(PostPlan)).one()
        assert plan.visual == "The Undo prompt" and plan.alt_text.startswith("A VS Code prompt")
        assert "marketplace.visualstudio.com/items?itemName=BosiTerryLin.guided-i18n" in plan.first_reply
        assert "open-vsx.org/extension/BosiTerryLin/guided-i18n" in plan.first_reply
        assert db.get(PostTopic, t.id).status == "drafted"
    assert replies.summary(job)["plan"]["topic"] == "Undo that keeps your edits"


def test_your_own_topic_works_too(s, xcore):
    job = asyncio.run(replies.start("post", text="Why the extension never sends whole files", confirm=True, llm=Model()))
    assert "Topic: Why the extension never sends whole files" in json.loads(job.thread)[1]


def test_marking_a_post_as_posted_records_it_as_your_x_post(s, xcore):
    t = asyncio.run(posts.suggest_topics(Model()))[0][0]
    job = asyncio.run(replies.start("post", t.id, confirm=True, llm=Model()))
    replies.mark_posted(job.id, "https://x.com/terrylife624/status/1", "Undo used to wipe your edits.")
    with session() as db:
        p = db.exec(select(MyPost)).one()
        assert p.platform == "x" and p.posted_at is not None  # its replies get context; a ◆ on the downloads chart
        assert db.get(PostTopic, t.id).status == "posted"

