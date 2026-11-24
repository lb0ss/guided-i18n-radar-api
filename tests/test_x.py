"""X integration against a fake X API: research search, replies in checks, the monthly limit, costs."""

import asyncio
from dataclasses import replace

from sqlmodel import select

from radar.checks import run_checks
from radar.db import Comment, MyPost, Opportunity, Run, XUsage, session
from radar.research import Query, run_research
from radar.sources import x as xapi

from .test_research import FakeModel


def on(s, limit=600):
    return replace(s, x_bearer_token="test-x-token", x_monthly_post_limit=limit)


def post(pid, text, **kw):
    return {"id": pid, "text": text, "created_at": "2026-11-23T10:00:00.000Z", "public_metrics": {"reply_count": 2}} | kw


def test_no_token_no_x(s, web):
    asyncio.run(run_research(s, web.client(), llm=FakeModel([Query(source="x", query="react i18n", why="")])))
    asyncio.run(run_checks(s, web.client()))
    assert not [q for src, q in web.queries if src == "x"]


def test_research_searches_x_and_counts_the_cost(s, web):
    web.x_posts = {"hardcoded": {"data": [post("111", "How do I extract hardcoded strings from my React app?")]}}
    r = asyncio.run(run_research(on(s), web.client(), llm=FakeModel([Query(source="x", query="hardcoded strings react", why="")])))
    xq = [q for src, q in web.queries if src == "x"]
    assert "(hardcoded strings react) -is:retweet lang:en" in xq and '("guided-i18n") -is:retweet lang:en' in xq  # planner + mention
    assert r["replies"] == 1 and "https://x.com/i/web/status/111" in r["text"]
    with session() as db:
        assert db.exec(select(Opportunity)).one().source == "x"
        assert sum(u.posts for u in db.exec(select(XUsage)).all()) == 1
        assert db.exec(select(Run)).one().cost >= xapi.PRICE_PER_POST  # X cost included in the run


def test_checks_report_x_replies_and_save_your_post(s, web):
    asyncio.run(run_checks(on(s), web.client()))  # first check: baseline
    web.x_posts = {"to:terrylife624": {
        "data": [post("222", "@terrylife624 does it work with Next.js?", author_id="9",
                      referenced_tweets=[{"type": "replied_to", "id": "200"}])],
        "includes": {"users": [{"id": "9", "username": "dev_ana"}, {"id": "1", "username": "terrylife624"}],
                     "tweets": [{"id": "200", "text": "My i18n tool's own UI had a translation bug.", "author_id": "1",
                                 "created_at": "2026-11-22T21:12:00.000Z"}]}}}
    r = asyncio.run(run_checks(on(s), web.client()))
    assert r["new_summary"] == "1 new X reply"
    assert "X reply · **dev_ana** on your post: My i18n tool's own UI had a translation bug." in r["text"]
    with session() as db:
        c = db.exec(select(Comment).where(Comment.platform == "x")).one()
        assert c.post_url == "https://x.com/i/web/status/200"
        assert db.exec(select(MyPost).where(MyPost.platform == "x")).one().url == c.post_url  # context for a draft
    q = [q for src, q in web.queries if src == "x"][-1]
    assert q == "(to:terrylife624 OR @terrylife624) -from:terrylife624 -is:retweet"


def test_the_monthly_limit_stops_x_and_says_so(s, web):
    with session() as db:
        db.add(XUsage(what="earlier", posts=595, cost=2.975))
        db.commit()
    r = asyncio.run(run_checks(on(s, limit=600), web.client()))
    assert "watch:x" in r["failed"] and "monthly X limit reached: 595 of 600" in r["text"]
    assert not [q for src, q in web.queries if src == "x"]  # nothing was requested


def test_depleted_credits_are_reported_plainly(s, web):
    web.x_status = 402
    r = asyncio.run(run_checks(on(s), web.client()))
    assert "watch:x" in r["failed"] and "X credits depleted" in r["text"]

