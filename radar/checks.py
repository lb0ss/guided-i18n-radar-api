"""The daily checks job, no LLM: stats from every source, new issues and comments on your posts, one digest."""

import asyncio
import time
from datetime import UTC

import httpx
from sqlmodel import col, select

from . import notify
from .config import Settings, settings
from .db import Comment, MyPost, Run, Stat, Step, now, session
from .sources import FoundComment, devto, github, marketplace, openvsx

USER_AGENT = "guided-i18n-radar (personal stats)"

# Waits before retrying a dropped connection (ReadError, timeout, can't connect). asyncio's clock
# doesn't advance while the Mac sleeps, so a run cut off by sleep retries in the next wake window.
RETRY_DELAYS = (30, 120)


async def _step(run_id: int, name: str, make):
    """Runs make() (a function returning a fresh coroutine each call) and records the step.
    Network errors are retried; anything else (e.g. GitHub saying 401 or 404) fails at once."""
    t = time.monotonic()
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            result = await make()
            status, detail = "ok", ""
            break
        except httpx.TransportError as e:
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            result, status = None, "failed"
            detail = f"{type(e).__name__}: {e} (after {attempt + 1} tries)"[:500]
        except Exception as e:  # noqa: BLE001 - one failing source must not stop the others
            result, status, detail = None, "failed", f"{type(e).__name__}: {e}"[:500]
            break
    with session() as db:
        db.add(Step(run_id=run_id, name=name, status=status, seconds=round(time.monotonic() - t, 2), detail=detail))
        db.commit()
    return result


def _latest(db, source: str, metric: str, before_run: int) -> float | None:
    row = db.exec(select(Stat).where(Stat.source == source, Stat.metric == metric, Stat.run_id < before_run)
                  .order_by(col(Stat.run_id).desc())).first()
    return row.value if row else None


async def run_checks(s: Settings | None = None, client: httpx.AsyncClient | None = None) -> dict:
    s = s or settings()
    src, watch = s.sources.get("stats", {}), s.sources.get("watch", {})
    me = watch.get("me", {})
    headers = {"User-Agent": USER_AGENT}
    if s.github_token:
        headers["Authorization"] = f"Bearer {s.github_token}"
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True)

    with session() as db:
        run = Run(job="checks")
        db.add(run)
        db.commit()
        db.refresh(run)
        # The very first completed check only records what's already there.
        first_check = db.exec(select(Run).where(Run.job == "checks", Run.id < run.id, col(Run.finished_at).is_not(None))).first() is None

    try:
        jobs = {}
        if "openvsx" in src:
            jobs["openvsx"] = lambda: openvsx.stats(client, src["openvsx"])
        if "marketplace" in src:
            jobs["marketplace"] = lambda: marketplace.stats(client, src["marketplace"])
        if "github" in src:
            jobs["github"] = lambda: github.stats(client, src["github"])
        if "devto" in src:
            jobs["devto"] = lambda: devto.stats(client, src["devto"], s.devto_api_key)
        names = list(jobs)
        results = await asyncio.gather(*(_step(run.id, f"stats:{n}", jobs[n]) for n in names))
        stats = {n: r for n, r in zip(names, results) if r is not None}

        watched: list[FoundComment] = []
        if "github" in watch:
            watched += await _step(run.id, "watch:github", lambda: github.comments(client, watch["github"], me.get("github"))) or []
        if "devto" in watch:
            got = await _step(run.id, "watch:devto", lambda: devto.posts_and_comments(client, watch["devto"], me.get("devto")))
            if got:
                posts, comments = got
                watched += comments
                with session() as db:
                    known = set(db.exec(select(MyPost.url)).all())
                    db.add_all(MyPost(platform=p.platform, url=p.url, title=p.title, posted_at=p.posted_at) for p in posts if p.url not in known)
                    db.commit()
    finally:
        if own_client:
            await client.aclose()

    with session() as db:
        for source, metrics in stats.items():
            db.add_all(Stat(run_id=run.id, source=source, metric=m, value=float(v)) for m, v in metrics.items())
        seen = set(db.exec(select(Comment.external_id)).all())
        new = [c for c in watched if c.external_id not in seen]
        for c in new:
            db.add(Comment(platform=c.platform, external_id=c.external_id, post_url=c.post_url, url=c.url, author=c.author,
                           body=c.body, created_at=c.created_at, state="baseline" if first_check else "new"))
        db.commit()
        changes = {
            (source, m): (_latest(db, source, m, run.id), v) for source, metrics in stats.items() for m, v in metrics.items()
        }
        failed = db.exec(select(Step).where(Step.run_id == run.id, Step.status == "failed")).all()

    reported = [] if first_check else new
    text = digest(stats, changes, reported, new if first_check else [], failed)
    when = now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    path = notify.save(s, when, text)
    moved = any(old is not None and old != new_v for old, new_v in changes.values())
    sent = []
    if reported or moved or failed:  # quiet days stay quiet: the digest is still saved
        parts = ([counted(reported)] if reported else []) + (["stats changed"] if moved else []) \
            + ([f"{len(failed)} problem{'s' * (len(failed) != 1)}"] if failed else [])
        first = min(reported, key=lambda c: c.created_at) if reported else None
        sent = notify.send(s, "Radar: " + ", ".join(parts), text, short_digest(reported, changes, failed),
                           url=first.url if first else None,
                           url_title=f"Open the {kind(first).removeprefix('GitHub ').removeprefix('dev.to ')}" if first else None)

    with session() as db:
        r = db.get(Run, run.id)
        r.finished_at = now()
        r.status = "partial" if failed else "ok"
        db.add(r)
        db.commit()
    return {"run": run.id, "new_comments": len(reported), "new_summary": counted(reported) if reported else "nothing new on your posts",
            "baseline": len(new) if first_check else 0,
            "failed": [f.name for f in failed], "digest": str(path), "notified": sent, "text": text}


LABEL = {"openvsx": "Open VSX", "marketplace": "Marketplace", "github": "GitHub", "devto": "dev.to"}
KINDS = ("GitHub issue", "GitHub issue comment", "dev.to comment")  # the order they're listed in


def kind(c: FoundComment) -> str:
    """What a watched item is: a new issue on your repo, a comment on one of its issues, or a dev.to comment."""
    if c.external_id.startswith("github:issue:"):
        return "GitHub issue"
    if c.external_id.startswith("github:comment:"):
        return "GitHub issue comment"
    return f"{LABEL.get(c.platform, c.platform)} comment"


def counted(items: list[FoundComment]) -> str:
    """'1 new GitHub issue, 2 new dev.to comments': each kind named, none lumped together."""
    n = {k: 0 for k in KINDS}
    for c in items:
        n[kind(c)] = n.get(kind(c), 0) + 1
    return ", ".join(f"{v} new {k}{'s' * (v != 1)}" for k, v in n.items() if v)


def short_digest(new: list[FoundComment], changes: dict, failed: list[Step]) -> str:
    """The phone version: who commented, what moved, what broke. Pushover reads a little HTML."""
    from html import escape

    lines = []
    for c in sorted(new, key=lambda c: c.created_at)[:3]:
        body = " ".join(c.body.split())
        lines.append(f"<b>{escape(c.author)}</b> ({kind(c)}) on {escape(c.post_title[:40])}: {escape(body[:110])}{'…' if len(body) > 110 else ''}")
    if len(new) > 3:
        lines.append(f"…and {len(new) - 3} more in Radar")
    moved = [f"{LABEL.get(src, src)} {m.replace('_', ' ')} {v:g} ({'+' if v > old else ''}{v - old:g})"
             for (src, m), (old, v) in changes.items() if old is not None and old != v]
    if moved:
        lines.append("Stats: " + ", ".join(moved))
    if failed:
        lines.append(f"{len(failed)} problem{'s' * (len(failed) != 1)}: " + ", ".join(f.name for f in failed))
    return "\n".join(lines) or "Nothing new."


def digest(stats: dict, changes: dict, new: list[FoundComment], baseline: list[FoundComment], failed: list[Step]) -> str:
    lines = ["# Radar digest", ""]
    if new:
        lines += [f"## {counted(new)}", ""]
        for c in sorted(new, key=lambda c: (KINDS.index(kind(c)) if kind(c) in KINDS else 99, c.created_at)):
            body = " ".join(c.body.split())
            lines += [f"- {kind(c)} · **{c.author}** on {c.post_title} ({c.created_at.astimezone(UTC):%b %d})",
                      f"  {body[:280]}{'…' if len(body) > 280 else ''}", f"  {c.url}"]
        lines += ["", "Open Radar to confirm a drafted reply for any of them.", ""]
    elif baseline:
        lines += [f"First check: {len(baseline)} existing issues and comments recorded as already seen. From now on only new ones are reported.", ""]
    else:
        lines += ["No new GitHub issues, issue comments or dev.to comments.", ""]
    lines += ["## Stats", "", "| Source | Metric | Now | Change |", "|---|---|---|---|"]
    for (source, m), (old, v) in changes.items():
        delta = "" if old is None else (f"+{v - old:g}" if v > old else (f"{v - old:g}" if v < old else "–"))
        lines.append(f"| {source} | {m} | {v:g} | {delta} |")
    if failed:
        lines += ["", "## Problems", ""] + [f"- {f.name}: {f.detail}" for f in failed]
    return "\n".join(lines) + "\n"
