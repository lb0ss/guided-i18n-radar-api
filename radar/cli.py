"""Command line. Drafting always asks before spending money.

  uv run radar checks | research | serve
  uv run radar posts [import]            your posts (Reply on which post? uses these ids)
  uv run radar opportunities             research threads worth a reply
  uv run radar comments                  new comments on your posts
  uv run radar draft opportunity ID      draft a reply to a research thread
  uv run radar draft comment ID          draft a reply to a comment on your post
  uv run radar draft paste --channel reddit-reply [--post ID] < thread.txt
  uv run radar show DRAFT_ID
"""

import argparse
import asyncio
import sys

from sqlmodel import col, select


def _print_result(result: dict) -> None:
    print(result["text"])
    print({k: v for k, v in result.items() if k != "text"})


def _show(job) -> None:
    from .replies import summary

    s = summary(job)
    print(f"Draft {s['id']} · {s['channel']} · {s['status']} · ${s['cost']:.3f}")
    for a in s["attempts"]:
        print(f"\n--- attempt {a['attempt']}: {a['verdict'] or 'rule checks failed'}")
        for f in a["rule_failures"]:
            print(f"  rule: {f}")
        for p in a["problems"]:
            print(f"  {p['criterion']}: \"{p['quote']}\" → {p['fix']}")
        for u in a["unsupported"]:
            print(f"  unsupported: {u}")
        for w in a["rule_warnings"]:
            print(f"  warning: {w}")
    final = next((a for a in s["attempts"] if a["id"] == s["final_draft_id"]), None)
    if final:
        print(f"\n=== {'ready to post' if s['status'] == 'pass' else s['status']} ===\n{final['text']}\n")


def main() -> None:
    ap = argparse.ArgumentParser(prog="radar")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("checks")
    sub.add_parser("research")
    sub.add_parser("serve")
    pp = sub.add_parser("posts")
    pp.add_argument("action", nargs="?", choices=["import"])
    sub.add_parser("opportunities")
    sub.add_parser("comments")
    dp = sub.add_parser("draft")
    dp.add_argument("kind", choices=["opportunity", "comment", "paste"])
    dp.add_argument("id", nargs="?", type=int)
    dp.add_argument("--channel")
    dp.add_argument("--post", type=int, help="your post this replies under (see `radar posts`)")
    dp.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    sp = sub.add_parser("show")
    sp.add_argument("id", type=int)
    a = ap.parse_args()

    from .db import Comment, DraftJob, MyPost, Opportunity, session

    if a.cmd == "checks":
        from .checks import run_checks

        _print_result(asyncio.run(run_checks()))
    elif a.cmd == "research":
        from .research import run_research

        _print_result(asyncio.run(run_research()))
    elif a.cmd == "serve":
        import uvicorn

        uvicorn.run("radar.api:app", host="127.0.0.1", port=8000)
    elif a.cmd == "posts":
        if a.action == "import":
            from .threads import import_reddit_posts

            print(f"imported {len(import_reddit_posts())} posts")
        with session() as db:
            for p in db.exec(select(MyPost).order_by(MyPost.id)).all():
                print(f"{p.id:3}  {p.platform:7} {p.title[:70]}")
    elif a.cmd == "opportunities":
        with session() as db:
            for o in db.exec(select(Opportunity).where(Opportunity.state == "new").order_by(col(Opportunity.relevance).desc())).all():
                print(f"{o.id:4}  [{o.relevance}] {o.source:8} {o.title[:70]}\n      {o.angle[:110]}\n      {o.url}")
    elif a.cmd == "comments":
        with session() as db:
            for c in db.exec(select(Comment).where(Comment.state == "new")).all():
                print(f"{c.id:4}  {c.author} on {c.post_url}\n      {' '.join(c.body.split())[:160]}")
    elif a.cmd == "draft":
        from .replies import NeedsConfirmation, start

        kind = "pasted" if a.kind == "paste" else a.kind
        text = sys.stdin.read() if kind == "pasted" else None
        args = {"kind": kind, "ref": a.id, "text": text, "channel": a.channel, "post_id": a.post}
        try:
            job = asyncio.run(start(**args, confirm=a.yes))
        except NeedsConfirmation as e:
            # stdin may already hold the pasted thread, so ask on the terminal
            try:
                with open("/dev/tty") as tty:
                    print(f"Drafting costs {e}. Continue? [y/N] ", end="", flush=True)
                    ok = tty.readline().strip().lower() == "y"
            except OSError:
                ok = False
            if not ok:
                print("Not drafted (nothing spent). Add --yes to skip this question.")
                return
            job = asyncio.run(start(**args, confirm=True))
        _show(job)
    elif a.cmd == "show":
        with session() as db:
            job = db.get(DraftJob, a.id)
        if job is None:
            print(f"no draft {a.id}")
            return
        _show(job)
    else:
        ap.print_help()
