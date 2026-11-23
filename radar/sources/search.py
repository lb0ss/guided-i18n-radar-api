"""Search other people's threads: GitHub issues, dev.to, Stack Overflow, Hacker News. Free public APIs.
Every function returns Found items with the same shape, so the scorer sees one list."""

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx


@dataclass
class Found:
    source: str
    url: str
    title: str
    excerpt: str
    created_at: datetime
    replies: int
    query: str
    status: str = ""  # e.g. "closed" for a GitHub issue
    opened_by: str = ""  # GitHub: "owner", "member" or "collaborator" when the repo's own people opened it


# GitHub's author_association values for the repo's own people; anyone else is an outsider
TEAM = {"OWNER": "owner", "MEMBER": "member", "COLLABORATOR": "collaborator"}


def _who(c: dict) -> str:
    role = TEAM.get(c.get("author_association", ""))
    return f"{c['user']['login']} ({role})" if role else c["user"]["login"]


def _clean(s: str, n: int = 600) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    t = " ".join(t.split())
    return t[:n] + ("…" if len(t) > n else "")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


async def github(client: httpx.AsyncClient, query: str, since: datetime, n: int) -> list[Found]:
    """GitHub issue search syntax, e.g. 'react i18n hardcoded strings' or 'repo:lokalise/i18n-ally'."""
    q = f"{query} is:issue created:>{since:%Y-%m-%d}"
    r = await client.get("https://api.github.com/search/issues", params={"q": q, "per_page": n, "sort": "created"})
    r.raise_for_status()
    return [Found("github", i["html_url"], i["title"], _clean(i.get("body") or ""), _ts(i["created_at"]), i["comments"], query,
                  status=i.get("state", ""), opened_by=TEAM.get(i.get("author_association", ""), ""))
            for i in r.json().get("items", [])]


async def github_latest_replies(client: httpx.AsyncClient, issue_url: str, n: int = 3) -> str:
    """The last few replies on a GitHub issue, so the scorer can tell when it's already answered or solved.
    The repo's own people are labeled, e.g. "truelockmc (owner): already working on this"."""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", issue_url)
    if not m:
        return ""
    r = await client.get(f"https://api.github.com/repos/{m.group(1)}/issues/{m.group(2)}/comments", params={"per_page": 100})
    r.raise_for_status()
    return " | ".join(f"{_who(c)}: {_clean(c.get('body') or '', 300)}" for c in r.json()[-n:])


async def devto(client: httpx.AsyncClient, tag: str, since: datetime, n: int) -> list[Found]:
    """dev.to has no search API: the query is a tag (i18n, react, localization…)."""
    r = await client.get("https://dev.to/api/articles", params={"tag": tag.lstrip("#").lower(), "per_page": n * 2})
    r.raise_for_status()
    out = []
    for a in r.json():
        when = _ts(a["published_at"])
        if when >= since:
            out.append(Found("devto", a["url"], a["title"], _clean(a.get("description") or ""), when, a.get("comments_count", 0), tag))
    return out[:n]


async def stackoverflow(client: httpx.AsyncClient, tags: str, since: datetime, n: int) -> list[Found]:
    """Tags joined by ';' (all must match), e.g. 'reactjs;internationalization'. Unanswered-or-open questions only."""
    r = await client.get("https://api.stackexchange.com/2.3/questions", params={
        "order": "desc", "sort": "creation", "tagged": tags, "site": "stackoverflow",
        "fromdate": int(since.timestamp()), "pagesize": n, "filter": "withbody"})
    r.raise_for_status()
    return [Found("stackoverflow", q["link"], html.unescape(q["title"]), _clean(q.get("body", "")),
                  datetime.fromtimestamp(q["creation_date"], UTC), q["answer_count"], tags)
            for q in r.json().get("items", []) if not q.get("accepted_answer_id")]


async def hackernews(client: httpx.AsyncClient, query: str, since: datetime, n: int) -> list[Found]:
    r = await client.get("https://hn.algolia.com/api/v1/search_by_date", params={
        "query": query, "tags": "(story,comment)", "numericFilters": f"created_at_i>{int(since.timestamp())}", "hitsPerPage": n})
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        title = h.get("title") or f"Comment on: {h.get('story_title') or 'a story'}"
        text = h.get("story_text") or h.get("comment_text") or ""
        out.append(Found("hackernews", f"https://news.ycombinator.com/item?id={h['objectID']}", title, _clean(text),
                         _ts(h["created_at"]), h.get("num_comments") or 0, query))
    return out


SEARCH = {"github": github, "devto": devto, "stackoverflow": stackoverflow, "hackernews": hackernews}

SYNTAX = {
    "github": "GitHub issue search words, optionally repo:owner/name or label:x (is:issue and the date are added by code)",
    "devto": "a single dev.to tag, lowercase, no spaces (dev.to has no text search)",
    "stackoverflow": "Stack Overflow tags joined by ';' (all must match), e.g. reactjs;i18next",
    "hackernews": "plain search words",
}


def window(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)
