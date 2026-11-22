"""The research job (every 3 days): a planner model proposes searches, code runs them on free public
APIs, a scorer model rates each result, code keeps the good ones. Models never decide what runs."""

import asyncio
import json
import time
from datetime import timedelta
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, Field
from sqlmodel import col, select

from . import notify
from .checks import USER_AGENT
from .config import ROOT, Settings, settings
from .db import Opportunity, Run, Step, now, session
from .llm import call
from .sources.search import SEARCH, SYNTAX, Found, github_latest_replies, window

MODEL = "claude-haiku-5-5"  # planner and scorer; cheap, and the bake-off showed Haiku is accurate
SCORE_BATCH = 25
MAX_TO_SCORE = 100
MAX_THREAD_FETCHES = 20  # GitHub threads with replies, read before scoring (free, within the hourly API limit)

Source = Literal["github", "devto", "stackoverflow", "hackernews"]
Category = Literal["problem", "competitor", "request", "mention", "other"]
Action = Literal["reply", "intel", "skip"]


class Query(BaseModel):
    source: Source
    query: str
    why: str = Field(description="One short sentence: which item of looking_for this targets.")


class Plan(BaseModel):
    queries: list[Query]


class Score(BaseModel):
    id: str
    relevance: int = Field(ge=1, le=5)
    category: Category
    action: Action = Field(description="reply: a reply is welcome here. intel: worth knowing, don't reply. skip: neither.")
    angle: str = Field(description="For reply: how to help, answering first. For intel: what it tells the developer.")
    reason: str = Field(description="One short sentence: why this score.")


class Scores(BaseModel):
    items: list[Score]


PLANNER = """\
You plan searches for a solo developer's product, to find recent threads where a helpful reply is
welcome. Product and what to look for:

{brief}

Write search queries for these sources (query syntax per source):
{syntax}

At most {caps} queries per source. Prefer the words people actually write when stuck ("hardcoded
strings", "react-i18next plural", "translate my react app"), not marketing words. Vary from the
queries used before: {previous}"""

SCORER = """\
You rate search results for a solo developer, who may reply to a thread when guided-i18n genuinely
helps. Product, what's relevant and what isn't:

{brief}

For every result, by id:
- relevance 5: asks for exactly what the product does, and a reply would clearly help
- 4: a closely related problem where mentioning the product would feel natural
- 3: i18n, but not React or react-i18next, or the product would only help a little
- 2: tangential; 1: irrelevant, spam, a job post, or someone promoting their own product
- category: problem (someone stuck), competitor (about i18n Ally, i18next-cli, i18next-parser, Babelize,
  Lingo.dev…), request (wants Vue, Next.js App Router, a CLI, another editor…), mention (names guided-i18n),
  other
- action, separately from relevance: would a reply from an outsider be welcome?
  reply: a person asks a question, is stuck, or explicitly invites suggestions or tools, and a
         helpful answer fits the place
  intel: worth knowing but not replying to: issues on a competitor's own tracker (never promote
         another tool there), a maintainer's own to-do or feature issue in their repo that asks
         nobody for help, roadmap signals, articles people published
  skip: neither
  A thread that is closed, already answered, or has a pull request or finished fix linked in its
  latest replies is not "reply": use intel (if it still tells something) or skip.
- angle: for reply, how to help, answering their question first (mention the product only if it
  truly fits); for intel, what it tells the developer
Return every id you were given, and only those."""


def load_brief(s: Settings) -> dict:
    return yaml.safe_load((ROOT / "projects" / s.project / "brief.yaml").read_text())


def _brief_text(b: dict) -> str:
    return yaml.safe_dump({k: b[k] for k in ("product", "looking_for", "not_relevant") if k in b}, sort_keys=False, allow_unicode=True)


async def _step(run_id: int, name: str, coro):
    t = time.monotonic()
    try:
        result, status, detail = await coro, "ok", ""
    except Exception as e:  # noqa: BLE001 - one failing search must not stop the run
        result, status, detail = None, "failed", f"{type(e).__name__}: {e}"[:500]
    with session() as db:
        db.add(Step(run_id=run_id, name=name, status=status, seconds=round(time.monotonic() - t, 2), detail=detail))
        db.commit()
    return result


def fixed_queries(brief: dict) -> list[tuple[str, str]]:
    """Free, exact queries every run: mentions of the product, new issues on competitors' repos."""
    a = brief.get("always", {})
    out = []
    if name := a.get("mentions"):
        out += [("github", f'"{name}"'), ("hackernews", name)]
    out += [("github", f"repo:{r}") for r in a.get("competitor_repos", [])]
    return out


def cap(plan: Plan, caps: dict[str, int]) -> list[tuple[str, str]]:
    seen, out, count = set(), [], {}
    for q in plan.queries:
        key = (q.source, q.query.strip().lower())
        if not q.query.strip() or key in seen or count.get(q.source, 0) >= caps.get(q.source, 0):
            continue
        seen.add(key)
        count[q.source] = count.get(q.source, 0) + 1
        out.append((q.source, q.query.strip()))
    return out


async def run_research(s: Settings | None = None, client: httpx.AsyncClient | None = None, llm=call) -> dict:
    s = s or settings()
    brief = load_brief(s)
    limits = brief.get("limits", {})
    caps = limits.get("queries_per_source", {})
    per_query = limits.get("results_per_query", 15)
    keep = limits.get("keep_relevance", 4)
    since = window(brief.get("freshness_days", 7))
    brief_text = _brief_text(brief)

    with session() as db:
        run = Run(job="research")
        db.add(run)
        db.commit()
        db.refresh(run)
        previous = sorted({o.query for o in db.exec(select(Opportunity).where(Opportunity.found_at > now() - timedelta(days=30))).all() if o.query})
    cost = 0.0
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        # 1. plan (model), then cap (code)
        syntax = "\n".join(f"- {k}: {v}" for k, v in SYNTAX.items())
        planner = PLANNER.format(brief=brief_text, syntax=syntax, caps=json.dumps(caps), previous=", ".join(previous[-30:]) or "none yet")
        r = await _step(run.id, "plan", llm(MODEL, planner, "Plan this run's searches.", Plan))
        queries = []
        if r is not None:
            cost += r.cost
            if r.output is None:
                await _step(run.id, "plan:output", _fail(r.error or "no plan"))
            else:
                queries = cap(r.output, caps)
        queries += [q for q in fixed_queries(brief) if q not in queries]

        # 2. search every source in parallel (code; no model decides whether to)
        async def one(source: str, q: str):
            return await _step(run.id, f"search:{source}:{q}"[:120], SEARCH[source](client, q, since, per_query))

        found: dict[str, Found] = {}
        for res in await asyncio.gather(*(one(src, q) for src, q in queries)):
            for f in res or []:
                found.setdefault(f.url, f)
    finally:
        if own_client:
            await client.aclose()

    # 3. drop what's been seen before, newest first, capped
    with session() as db:
        seen = set(db.exec(select(Opportunity.url).where(col(Opportunity.url).in_(list(found)))).all())
    fresh = sorted((f for u, f in found.items() if u not in seen), key=lambda f: f.created_at, reverse=True)[:MAX_TO_SCORE]

    # 3b. for GitHub threads that have replies, read the latest ones (code): a thread that's already
    # answered, or has a pull request open for it, shouldn't come out as "worth a reply"
    to_read = [f for f in fresh if f.source == "github" and f.replies > 0][:MAX_THREAD_FETCHES]
    if to_read:
        own = own_client  # the search client is closed by now when research made its own
        c2 = httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT} | (
            {"Authorization": f"Bearer {s.github_token}"} if s.github_token else {})) if own else client
        try:
            latest = await asyncio.gather(*(github_latest_replies(c2, f.url) for f in to_read), return_exceptions=True)
        finally:
            if own:
                await c2.aclose()
        for f, text in zip(to_read, latest):
            if isinstance(text, str) and text:
                f.excerpt = f"{f.excerpt}\nLatest replies: {text}"

    # 4. score (model) in parallel batches; ids the scorer wasn't given are rejected
    ids = {f"r{i}": f for i, f in enumerate(fresh, 1)}
    batches = [list(ids.items())[i:i + SCORE_BATCH] for i in range(0, len(ids), SCORE_BATCH)]
    scorer = SCORER.format(brief=brief_text)

    async def score(batch):
        user = json.dumps([{"id": i, "source": f.source, "title": f.title, "excerpt": f.excerpt, "replies": f.replies,
                            "status": f.status, "date": f"{f.created_at:%Y-%m-%d}"} for i, f in batch], ensure_ascii=False)
        return await _step(run.id, f"score:{batch[0][0]}-{batch[-1][0]}", llm(MODEL, scorer, user, Scores))

    scored: dict[str, Score] = {}
    for r in await asyncio.gather(*(score(b) for b in batches)):
        if r is None:
            continue
        cost += r.cost
        for sc in (r.output.items if r.output else []):
            if sc.id in ids:
                scored[sc.id] = sc
    mention = (brief.get("always", {}).get("mentions") or "").lower()

    # 5. save (code); unscored results aren't saved, so a later run can score them
    kept = []
    with session() as db:
        for i, sc in scored.items():
            f = ids[i]
            is_mention = bool(mention) and mention in f"{f.title} {f.excerpt}".lower()
            rel = 5 if is_mention else sc.relevance
            action = "intel" if f.query.startswith("repo:") and sc.action == "reply" else sc.action  # never promote on a competitor's tracker
            state = "low" if rel < keep or action == "skip" else ("new" if action == "reply" or is_mention else "intel")
            o = Opportunity(run_id=run.id, url=f.url, source=f.source, title=f.title, excerpt=f.excerpt, created_at=f.created_at,
                            replies=f.replies, query=f.query, relevance=rel, category="mention" if is_mention else sc.category,
                            angle=sc.angle, reason=sc.reason, state=state)
            db.add(o)
            if state in ("new", "intel"):
                kept.append(o)
        db.commit()
        failed = db.exec(select(Step).where(Step.run_id == run.id, Step.status == "failed")).all()
        rr = db.get(Run, run.id)
        rr.finished_at, rr.cost = now(), round(cost, 4)
        rr.status = "failed" if failed and not scored and not found else ("partial" if failed else "ok")
        db.add(rr)
        db.commit()

    text = digest(kept, len(queries), len(found), len(fresh), len(scored), cost, failed)
    path = notify.save(s, f"{now().astimezone():%Y-%m-%d-%H%M%S}-research", text)
    sent = []
    n_reply = sum(o.state == "new" for o in kept)
    if n_reply or failed:  # intel alone waits for the next digest you open
        subject = f"Radar research: {n_reply} thread{'s' * (n_reply != 1)} worth a reply"
        replies = sorted((o for o in kept if o.state == "new"), key=lambda o: -o.relevance)
        sent = notify.send(s, subject, text, short_digest(replies, failed),
                           url=replies[0].url if replies else None, url_title="Open the top thread" if replies else None)
    return {"run": run.id, "queries": len(queries), "found": len(found), "scored": len(scored),
            "replies": sum(o.state == "new" for o in kept), "intel": sum(o.state == "intel" for o in kept),
            "cost": round(cost, 4), "failed": [f.name for f in failed], "digest": str(path), "notified": sent, "text": text}


async def _fail(msg: str):
    raise RuntimeError(msg)


def short_digest(replies: list[Opportunity], failed: list[Step]) -> str:
    from html import escape

    lines = [f"• {escape(o.title[:90])} <i>({o.source})</i>" for o in replies[:3]]
    if len(replies) > 3:
        lines.append(f"…and {len(replies) - 3} more")
    if replies:
        lines.append("Draft replies in Radar on your Mac.")
    if failed:
        lines.append(f"{len(failed)} problem{'s' * (len(failed) != 1)} in the run")
    return "\n".join(lines) or "Nothing new."


def digest(kept: list[Opportunity], n_queries: int, n_found: int, n_fresh: int, n_scored: int, cost: float, failed: list[Step]) -> str:
    replies = [o for o in kept if o.state == "new"]
    intel = [o for o in kept if o.state == "intel"]
    summary = (f"{n_queries} searches, {n_found} results, {n_fresh} not seen before, {n_scored} scored: "
               f"**{len(replies)} worth a reply**, {len(intel)} intel. Cost ${cost:.3f}.")
    lines = ["# Radar research", "", summary, ""]
    for title, group in ((f"Worth a reply ({len(replies)})", replies), (f"Intel only, don't reply ({len(intel)})", intel)):
        if not group:
            continue
        lines += [f"## {title}", ""]
        for o in sorted(group, key=lambda o: (o.category, -o.relevance)):
            lines += [f"- **{o.title}** ({o.category}, {o.source}, {o.created_at:%b %d}, {o.replies} replies, relevance {o.relevance})",
                      f"  {'Angle' if o.state == 'new' else 'Why it matters'}: {o.angle}", f"  {o.url}"]
        lines.append("")
    if failed:
        lines += ["## Problems", ""] + [f"- {f.name}: {f.detail}" for f in failed]
    return "\n".join(lines) + "\n"
