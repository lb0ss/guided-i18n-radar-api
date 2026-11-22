"""GitHub: repo stats, and new issues and issue comments by other people."""

from datetime import datetime

import httpx

from . import FoundComment

API = "https://api.github.com"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)  # Python 3.11+ reads the trailing Z


async def stats(client: httpx.AsyncClient, repo: str) -> dict[str, float]:
    r = await client.get(f"{API}/repos/{repo}")
    r.raise_for_status()
    d = r.json()
    return {"stars": d["stargazers_count"], "forks": d["forks_count"], "open_issues": d["open_issues_count"]}


async def comments(client: httpx.AsyncClient, repo: str, me: str | None) -> list[FoundComment]:
    """Issues opened and comments written by anyone but you (most recent 100 of each)."""
    found = []
    r = await client.get(f"{API}/repos/{repo}/issues", params={"state": "all", "per_page": 100, "sort": "created"})
    r.raise_for_status()
    for i in r.json():
        if "pull_request" in i or i["user"]["login"] == me:
            continue
        found.append(FoundComment(
            platform="github", external_id=f"github:issue:{i['id']}", post_url=i["html_url"],
            post_title=f"#{i['number']} {i['title']}", url=i["html_url"], author=i["user"]["login"],
            body=i.get("body") or "", created_at=_ts(i["created_at"]),
        ))
    r = await client.get(f"{API}/repos/{repo}/issues/comments", params={"per_page": 100, "sort": "created", "direction": "desc"})
    r.raise_for_status()
    for c in r.json():
        if c["user"]["login"] == me:
            continue
        issue_url = c["html_url"].split("#")[0]
        found.append(FoundComment(
            platform="github", external_id=f"github:comment:{c['id']}", post_url=issue_url,
            post_title=f"#{issue_url.rsplit('/', 1)[-1]}", url=c["html_url"], author=c["user"]["login"],
            body=c.get("body") or "", created_at=_ts(c["created_at"]),
        ))
    return found
