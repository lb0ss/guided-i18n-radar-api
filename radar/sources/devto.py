"""dev.to: your articles, their reactions and comments. Page views need an API key (DEV_API_KEY)."""

import html
import re
from datetime import datetime

import httpx

from . import FoundComment, FoundPost

API = "https://dev.to/api"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)  # Python 3.11+ reads the trailing Z


def _text(body_html: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", body_html or "")).strip()


async def articles(client: httpx.AsyncClient, username: str) -> list[dict]:
    r = await client.get(f"{API}/articles", params={"username": username, "per_page": 100})
    r.raise_for_status()
    return r.json()


async def stats(client: httpx.AsyncClient, username: str, api_key: str | None = None) -> dict[str, float]:
    arts = await articles(client, username)
    out = {
        "articles": len(arts),
        "reactions": sum(a.get("public_reactions_count", 0) for a in arts),
        "comments": sum(a.get("comments_count", 0) for a in arts),
    }
    if api_key:  # views are only in your own article list
        r = await client.get(f"{API}/articles/me/published", headers={"api-key": api_key}, params={"per_page": 100})
        r.raise_for_status()
        out["views"] = sum(a.get("page_views_count", 0) for a in r.json())
    return out


async def posts_and_comments(client: httpx.AsyncClient, username: str, me: str | None) -> tuple[list[FoundPost], list[FoundComment]]:
    posts, found = [], []
    for a in await articles(client, username):
        posts.append(FoundPost(platform="devto", url=a["url"], title=a["title"], posted_at=_ts(a["published_at"]) if a.get("published_at") else None))
        r = await client.get(f"{API}/comments", params={"a_id": a["id"]})
        r.raise_for_status()
        stack = list(r.json())
        while stack:  # comments come as a tree; replies sit in "children"
            c = stack.pop()
            stack.extend(c.get("children", []))
            user = (c.get("user") or {}).get("username")
            if not user or user == me:
                continue
            found.append(FoundComment(
                platform="devto", external_id=f"devto:{c['id_code']}", post_url=a["url"], post_title=a["title"],
                url=f"https://dev.to/{user}/comment/{c['id_code']}", author=user,
                body=_text(c.get("body_html", "")), created_at=_ts(c["created_at"]),
            ))
    return posts, found
