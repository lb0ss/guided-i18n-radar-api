import asyncio

from sqlmodel import select

from radar.db import Opportunity, Run, session
from radar.llm import Result
from radar.research import Plan, Query, Score, Scores, cap, run_research


class FakeModel:
    """Plans a fixed set of queries and scores by keyword, like a well-behaved model would."""

    def __init__(self, plan, extra_ids=()):
        self.plan, self.extra_ids, self.calls = plan, extra_ids, []

    async def __call__(self, model, system, user, schema):
        self.calls.append(schema.__name__)
        if schema is Plan:
            return Result(model, Plan(queries=self.plan), 1000, 200)
        import json
        items = [Score(id=r["id"], relevance=5 if "hardcoded" in r["title"].lower() or "extract" in r["title"].lower() else 2,
                       category="problem", action="reply", angle="Answer first.", reason="test") for r in json.loads(user)]
        items += [Score(id=i, relevance=5, category="problem", action="reply", angle="x", reason="made up") for i in self.extra_ids]
        return Result(model, Scores(items=items), 3000, 500)


def test_planner_queries_are_capped_and_deduplicated():
    plan = Plan(queries=[Query(source="github", query=q, why="") for q in ["a", "b", "A", "c", "d", "e", " "]]
                + [Query(source="hackernews", query="x", why="")])
    assert cap(plan, {"github": 4, "hackernews": 0}) == [("github", "a"), ("github", "b"), ("github", "c"), ("github", "d")]


def test_research_keeps_relevant_threads_and_records_the_rest(s, web):
    web.search_issues = {"translate react": [web._issue(1, "How to move hardcoded strings to react-i18next?"),
                                             web._issue(2, "Dark mode toggle broken")]}
    model = FakeModel([Query(source="github", query="translate react", why=""), Query(source="devto", query="i18n", why="")],
                      extra_ids=["r999"])  # an id the scorer was never given
    r = asyncio.run(run_research(s, web.client(), llm=model))
    assert r["found"] == 2 and r["scored"] == 2 and r["replies"] == 1
    assert "How to move hardcoded strings" in r["text"] and "Dark mode" not in r["text"]
    assert ("devto", "i18n") in web.queries
    assert any("repo:lokalise/i18n-ally" in q for _, q in web.queries)  # competitor repos, every run, no model
    with session() as db:
        states = {o.title: o.state for o in db.exec(select(Opportunity)).all()}
        assert states == {"How to move hardcoded strings to react-i18next?": "new", "Dark mode toggle broken": "low"}
        assert db.exec(select(Run)).one().cost > 0
    # the same threads next run: nothing new, nothing scored again
    r2 = asyncio.run(run_research(s, web.client(), llm=model))
    assert r2["found"] == 2 and r2["scored"] == 0 and r2["replies"] == 0


def test_mentions_of_the_product_are_always_kept(s, web):
    web.search_issues = {'"guided-i18n"': [web._issue(9, "Tried guided-i18n, keys look odd", repo="dev/shop")]}
    r = asyncio.run(run_research(s, web.client(), llm=FakeModel([])))
    assert r["replies"] == 1 and "(mention, github" in r["text"]


def test_a_failing_planner_still_runs_the_fixed_queries(s, web):
    async def broken(model, system, user, schema):
        if schema is Plan:
            return Result(model, None, 0, 0, error="refused")
        return Result(model, Scores(items=[]), 0, 0)
    r = asyncio.run(run_research(s, web.client(), llm=broken))
    assert r["queries"] == 5 and "plan:output" in r["failed"]


def test_never_suggests_replying_on_a_competitors_tracker(s, web):
    web.search_issues = {"repo:i18next/i18next-cli": [web._issue(5, "extract: unions are not extracted", repo="i18next/i18next-cli")]}
    r = asyncio.run(run_research(s, web.client(), llm=FakeModel([])))
    assert r["replies"] == 0 and r["intel"] == 1 and "Intel only, don't reply (1)" in r["text"]
    assert r["notified"] == []  # intel alone sends nothing


def test_latest_replies_and_status_reach_the_scorer(s, web):
    web.search_issues = {"translate react": [web._issue(1, "How to extract hardcoded strings?", state="closed")]}
    web.thread_replies = {"https://api.github.com/repos/someone/app/issues/1/comments": [
        {"user": {"login": "helper"}, "body": "PR opened: #103 moves every string to en.ts"}]}
    model = FakeModel([Query(source="github", query="translate react", why="")])
    seen = []
    async def spy(model_name, system, user, schema):
        if schema is Scores:
            seen.append(user)
        return await model(model_name, system, user, schema)
    asyncio.run(run_research(s, web.client(), llm=spy))
    assert "Latest replies: helper: PR opened: #103" in seen[0] and '"status": "closed"' in seen[0]
