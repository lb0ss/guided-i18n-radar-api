"""X (Twitter) through its official pay-per-use API: recent search only (the last 7 days).

Billing is per post returned, so Radar asks for few posts, never pays to look up authors in research,
and stops for the month at X_MONTHLY_POST_LIMIT. Usage is recorded in the XUsage table."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlmodel import col, func, select

from ..db import XUsage, now, session
from . import FoundComment

API = "https://api.x.com/2"
PRICE_PER_POST = 0.005  # USD per post returned (X pay-per-use, Nov 2026); check developer.x.com before relying on it
MIN_RESULTS = 10  # the API's smallest page; you're billed only for posts actually returned


class XLimitReached(Exception):
    """This month's X post budget is used up; X searches wait until next month."""


def _month_start() -> datetime:
    t = now()
    return datetime(t.year, t.month, 1, tzinfo=UTC)


def used_this_month() -> int:
    with session() as db:
        return int(db.exec(select(func.coalesce(func.sum(XUsage.posts), 0)).where(col(XUsage.at) >= _month_start())).one())


def _check_budget(limit: int) -> None:
    used = used_this_month()
    if used + MIN_RESULTS > limit:
        raise XLimitReached(f"monthly X limit reached: {used} of {limit} posts read (X_MONTHLY_POST_LIMIT)")


def cost_of_run(run_id: int) -> float:
    with session() as db:
        return float(db.exec(select(func.coalesce(func.sum(XUsage.cost), 0.0)).where(XUsage.run_id == run_id)).one())


def _record(run_id: int | None, what: str, posts: int) -> float:
    cost = round(posts * PRICE_PER_POST, 4)
    with session() as db:
        db.add(XUsage(run_id=run_id, what=what, posts=posts, cost=cost))
        db.commit()
    return cost


async def _search(client: httpx.AsyncClient, token: str, query: str, since: datetime, n: int, extra: dict | None = None) -> dict:
    earliest = datetime.now(UTC) - timedelta(days=7) + timedelta(minutes=1)  # recent search covers 7 days
    params = {"query": query, "max_results": max(MIN_RESULTS, min(n, 100)),
              "start_time": max(since, earliest).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "tweet.fields": "created_at,public_metrics,conversation_id"} | (extra or {})
    r = await client.get(f"{API}/tweets/search/recent", params=params, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 402:
        raise RuntimeError("X credits depleted: add credit at developer.x.com")
    r.raise_for_status()
    return r.json()


def status_url(post_id: str) -> str:
    return f"https://x.com/i/web/status/{post_id}"


async def search(client: httpx.AsyncClient, token: str, limit: int, run_id: int | None,
                  query: str, since: datetime, n: int, me: str | None = None, after: datetime | None = None) -> list[dict]:
    """Research: other people's recent posts. Code leaves out retweets, non-English posts and your own posts
    (`me`), and starts at `after` (the last research run) so posts already paid for aren't bought again."""
    _check_budget(limit)
    own = f" -from:{me.lstrip('@')}" if me else ""
    d = await _search(client, token, f"({query}) -is:retweet lang:en{own}", max(since, after) if after else since, n)
    posts = d.get("data", [])
    _record(run_id, f"search: {query}", len(posts))
    return posts


async def replies(client: httpx.AsyncClient, token: str, limit: int, run_id: int | None,
                  handle: str, since: datetime) -> tuple[list[FoundComment], list[dict]]:
    """Checks: replies to you and mentions of you since `since`, plus those of the answered posts that are
    yours (saved as your posts, so a drafted reply has its context). Authors are looked up here: few posts,
    and a name matters."""
    _check_budget(limit)
    h = handle.lstrip("@")
    d = await _search(client, token, f"(to:{h} OR @{h}) -from:{h} -is:retweet", since, MIN_RESULTS, {
        "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id", "user.fields": "username"})
    posts = d.get("data", [])
    inc = d.get("includes", {})
    parents = {t["id"]: t for t in inc.get("tweets", [])}
    users = {u["id"]: u["username"] for u in inc.get("users", [])}
    _record(run_id, f"replies to @{h}", len(posts) + len(parents))
    found = []
    for p in posts:
        ref = next((r["id"] for r in p.get("referenced_tweets", []) if r["type"] == "replied_to"), None)
        parent = parents.get(ref) if ref else None
        found.append(FoundComment(
            platform="x", external_id=f"x:{p['id']}",
            post_url=status_url(ref) if ref else status_url(p["id"]),
            post_title=f"your post: {' '.join(parent['text'].split())[:60]}" if parent else "a mention of you",
            url=status_url(p["id"]), author=users.get(p.get("author_id"), p.get("author_id", "someone")),
            body=p.get("text", ""), created_at=datetime.fromisoformat(p["created_at"]),
        ))
    mine = [t | {"url": status_url(t["id"])} for t in parents.values() if users.get(t.get("author_id"), "").lower() == h.lower()]
    return found, mine
