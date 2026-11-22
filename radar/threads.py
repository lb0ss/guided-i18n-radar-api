"""What a draft answers. Builds the thread (oldest first, the last entry is what the reply answers) from a
research opportunity, a comment on your post, or text you paste; and imports your existing posts."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlmodel import select

from .db import Comment, MyPost, Opportunity, session

CHANNEL_FOR = {"github": "github-comment", "devto": "devto-comment", "hackernews": "hn-comment",
               "stackoverflow": "so-answer", "reddit": "reddit-reply", "x": "x-post"}


def _post_context(p: MyPost) -> str:
    return f"Your post ({p.platform}): {p.title}\n\n{p.text}".strip()


async def from_opportunity(o: Opportunity, client: httpx.AsyncClient | None = None) -> list[str]:
    """GitHub issues are fetched in full (body and comments, free API); other sources use the stored excerpt."""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", o.url)
    if not m:
        return [f"{o.title}\n\n{o.excerpt}"]
    repo, num = m.groups()
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, headers={"User-Agent": "guided-i18n-radar"})
    try:
        issue = (await client.get(f"https://api.github.com/repos/{repo}/issues/{num}")).raise_for_status().json()
        comments = (await client.get(f"https://api.github.com/repos/{repo}/issues/{num}/comments", params={"per_page": 30})).raise_for_status().json()
    finally:
        if own:
            await client.aclose()
    thread = [f"Issue by {issue['user']['login']} in {repo}: {issue['title']}\n\n{(issue.get('body') or '')[:4000]}"]
    thread += [f"{c['user']['login']}: {(c.get('body') or '')[:2000]}" for c in comments]
    return thread


def from_comment(c: Comment) -> tuple[list[str], int | None]:
    with session() as db:
        p = db.exec(select(MyPost).where(MyPost.url == c.post_url)).first()
    thread = [_post_context(p)] if p else [f"Your post: {c.post_url}"]
    return thread + [f"{c.author}: {c.body}"], (p.id if p else None)


def from_paste(text: str, post_id: int | None) -> list[str]:
    """Pasted text is the conversation so far; with post_id, your post's text goes first."""
    thread = []
    if post_id is not None:
        with session() as db:
            p = db.get(MyPost, post_id)
        if p is None:
            raise ValueError(f"no post {post_id}; see `radar posts`")
        thread.append(_post_context(p))
    return thread + [text.strip()]


def import_reddit_posts(repo: Path | None = None) -> list[MyPost]:
    """One-time import of posts written before Radar: docs/launch/reddit.md (titles and bodies) and
    docs/MARKETING.md (URLs, where logged). Posts already imported are left alone."""
    repo = repo or Path(os.environ.get("I18N_CORE_PATH", "~/repos/i18n-ai-core")).expanduser()
    md = (repo / "docs/launch/reddit.md").read_text()
    log = (repo / "docs/MARKETING.md").read_text() if (repo / "docs/MARKETING.md").exists() else ""
    urls = {m.group(1).lower(): m.group(0) for m in re.finditer(r"https://www\.reddit\.com/r/(\w+)/comments/\w+/\w+/?", log)}
    found = []
    for sec in re.split(r"(?m)^## ", md)[1:]:
        head, _, body = sec.partition("\n")
        m = re.match(r"Post \(r/(\w+)", head)
        if not m:
            continue
        sub = m.group(1)
        t = re.search(r"\*\*Title:\*\*\s*\n+(.+)", body)
        b = re.search(r"\*\*Body\*\*[^\n]*\n+---\n(.*?)\n---", body, re.DOTALL)
        if not (t and b):
            continue
        found.append(MyPost(platform="reddit", url=urls.get(sub.lower(), f"reddit:r/{sub}"), title=f"r/{sub}: {t.group(1).strip()}",
                            text=b.group(1).strip(), posted_at=None))
    added = []
    with session() as db:
        known = set(db.exec(select(MyPost.url)).all())
        for p in found:
            if p.url not in known:
                db.add(p)
                added.append(p)
        db.commit()
    return added


def now() -> datetime:
    return datetime.now(UTC)
