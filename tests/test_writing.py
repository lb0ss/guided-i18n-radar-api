import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from radar import replies, scheduler
from radar.db import Comment, DraftJob, MyPost, Opportunity, now, session
from radar.llm import Result
from radar.threads import import_reddit_posts
from radar.writing import DraftText, Scores, Verdict, channels, check_rules


@pytest.fixture
def core(tmp_path, monkeypatch):
    """A stand-in i18n-ai-core with FACTS.md, CHANGELOG and the Reddit post log."""
    repo = tmp_path / "core"
    (repo / "docs/launch").mkdir(parents=True)
    (repo / "packages/vscode").mkdir(parents=True)
    (repo / "docs/FACTS.md").write_text("# Facts\nPlural categories come from Intl.PluralRules.")
    (repo / "packages/vscode/CHANGELOG.md").write_text("## 0.1.4\n- Better translations")
    (repo / "docs/launch/reddit.md").write_text(
        "# Reddit\n\n## Post (r/webdev, Showoff Saturday)\n\n**Title:**\n\nI built a thing\n\n**Body** (Markdown):\n\n---\n\nBody text here.\n\n---\n\n"
        "## Title for r/reactjs\n\nnot a post\n")
    (repo / "docs/MARKETING.md").write_text("| x | https://www.reddit.com/r/webdev/comments/abc123/i_built_a_thing/ |")
    monkeypatch.setenv("I18N_CORE_PATH", str(repo))
    return repo


GOOD = Scores(answers_question=5, claims_backed=5, no_overpromising=5, sounds_human=5, fits_channel=5, worth_posting=5)


class Script:
    """Writer and evaluator that follow a script: writer texts in order, evaluator verdicts in order."""

    def __init__(self, texts, verdicts):
        self.texts, self.verdicts, self.calls = list(texts), list(verdicts), []

    async def __call__(self, model, system, user, schema):
        self.calls.append((model, schema.__name__, json.loads(user)))
        if schema is DraftText:
            return Result(model, DraftText(text=self.texts.pop(0)), 6000, 200)
        v = self.verdicts.pop(0)
        return Result(model, Verdict(verdict=v, scores=GOOD, problems=[] if v != "revise" else
                                     [{"criterion": "claims_backed", "quote": "ships next week", "fix": "drop the date"}],
                                     unsupported_claims=[]), 7000, 500)


def opp():
    with session() as db:
        o = Opportunity(run_id=1, url="https://dev.to/someone/how-to-i18n", source="devto", title="How do I translate my React app?",
                        excerpt="I have hardcoded strings everywhere.", created_at=now())
        db.add(o)
        db.commit()
        db.refresh(o)
        return o


def test_nothing_is_drafted_or_spent_without_confirmation(s, core):
    o = opp()
    model = Script(["x"], ["pass"])
    with pytest.raises(replies.NeedsConfirmation) as e:
        asyncio.run(replies.start("opportunity", o.id, llm=model))
    assert e.value.worst > e.value.per_attempt > 0
    assert model.calls == []
    with session() as db:
        assert db.exec(select(DraftJob)).first() is None


def test_passes_first_time(s, core):
    o = opp()
    model = Script(["Wrap them in t() and keep keys per component."], ["pass"])
    job = asyncio.run(replies.start("opportunity", o.id, confirm=True, llm=model))
    assert job.status == "pass" and job.channel == "devto-comment" and job.cost > 0
    assert [c[1] for c in model.calls] == ["DraftText", "Verdict"]
    assert "draft" in model.calls[1][2] and "writer" not in json.dumps(model.calls[1][2])  # never the writer's reasoning
    with session() as db:
        assert db.get(Opportunity, o.id).state == "drafted"


def test_rule_failures_go_back_to_the_writer_without_the_evaluator(s, core):
    with session() as db:
        db.add(MyPost(platform="reddit", url="https://reddit.com/r/x/1", title="My post", text="I built guided-i18n."))
        db.add(Comment(platform="reddit", external_id="r:1", post_url="https://reddit.com/r/x/1", url="u", author="q",
                       body="Polish plurals?", created_at=now()))
        db.commit()
    model = Script(["Yes, Polish gets four forms. " * 30, "Yes, Polish gets four forms from Intl.PluralRules."], ["pass"])
    job = asyncio.run(replies.start("comment", 1, confirm=True, llm=model))
    assert job.status == "pass" and job.channel == "reddit-reply"
    assert [c[1] for c in model.calls] == ["DraftText", "DraftText", "Verdict"]
    assert any("too long" in f for f in model.calls[1][2]["fix_these_problems_from_the_last_attempt"])
    assert model.calls[0][2]["thread"][0].startswith("Your post (reddit): My post")  # your post's text is attached


def test_revise_feeds_problems_back_and_stops_after_three(s, core):
    o = opp()
    model = Script(["a", "b", "c"], ["revise", "revise", "revise"])
    job = asyncio.run(replies.start("opportunity", o.id, confirm=True, llm=model))
    assert job.status == "needs work" and len([c for c in model.calls if c[1] == "DraftText"]) == 3
    assert 'claims_backed: "ships next week" → drop the date' in model.calls[2][2]["fix_these_problems_from_the_last_attempt"]


def test_disclosure_is_required_outside_your_own_posts():
    ch = channels()["github-comment"]
    assert any("built it" in f for f in check_rules(ch, "Try guided-i18n for this.", own_post=False)[0])
    assert check_rules(ch, "I built guided-i18n for this case.", own_post=False)[0] == []
    assert check_rules(ch, "guided-i18n handles it.", own_post=True)[0] == []
    assert any("hashtag" in f for f in check_rules(channels()["x-post"], "#i18n #react", own_post=True)[0])


def test_pasted_thread_with_your_post_attached(s, core):
    import_reddit_posts(core)
    with session() as db:
        p = db.exec(select(MyPost)).one()
    assert p.url.startswith("https://www.reddit.com/r/webdev/comments/abc123") and p.text == "Body text here."
    model = Script(["Thanks, glad it helped."], ["pass"])
    job = asyncio.run(replies.start("pasted", text="commenter: nice tool", channel="reddit-reply", post_id=p.id, confirm=True, llm=model))
    assert json.loads(job.thread) == ["Your post (reddit): r/webdev: I built a thing\n\nBody text here.", "commenter: nice tool"]
    assert import_reddit_posts(core) == []  # imported once


def test_marking_posted_records_your_final_text(s, core):
    o = opp()
    job = asyncio.run(replies.start("opportunity", o.id, confirm=True, llm=Script(["draft"], ["pass"])))
    replies.mark_posted(job.id, "https://dev.to/c/1", "my edited reply")
    with session() as db:
        j = db.get(DraftJob, job.id)
        assert (j.posted_url, j.posted_text) == ("https://dev.to/c/1", "my edited reply")
        assert db.get(Opportunity, o.id).state == "posted"


def test_api_returns_the_estimate_until_confirmed(s, core, monkeypatch):
    monkeypatch.setattr(scheduler, "is_due", lambda *a, **k: False)
    o = opp()
    from radar.api import app

    with TestClient(app) as c:
        r = c.post("/drafts", json={"kind": "opportunity", "id": o.id}).json()
        assert r["needs_confirmation"] and r["worst_case"] > r["estimate"] > 0
        assert c.post("/drafts", json={"kind": "pasted", "text": "hi"}).status_code == 400  # needs a channel
