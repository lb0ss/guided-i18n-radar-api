"""X integration against a fake X API: research search and its costs."""

import asyncio
from dataclasses import replace

from sqlmodel import select

from radar.checks import run_checks
from radar.db import Opportunity, Run, XUsage, session
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

