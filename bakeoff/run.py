"""Bake-off: which models should write, and which should evaluate?

Every writer drafts each task; each draft is reviewed by both models from the
other provider. Seeded drafts with known mistakes are reviewed by all four.

    uv run python -m bakeoff.run --dry    # rule checks + prompt sizes, no API calls
    uv run python -m bakeoff.run          # the chosen pair: Haiku 5.5 writes, GPT-5.4 evaluates
    uv run python -m bakeoff.run --writers all --evaluators other   # the full cross-check
"""

import argparse
import asyncio
import html
import json
import random
import time
from dataclasses import asdict
from pathlib import Path

from . import rules
from .cases import SEEDED, TASKS, Task
from .llm import CLAUDE, OPENAI, PRICES, Draft, Result, Verdict, call, provider
from .prompts import EVALUATOR_SYSTEM, WRITER_SYSTEM, facts, review_message, task_message

OUT = Path(__file__).parent / "out"
ALL = CLAUDE + OPENAI
WRITER, EVALUATOR = "claude-haiku-5-5", "gpt-5.4"  # chosen after the first bake-off (2026-11-20)
LIMIT = asyncio.Semaphore(4)
WRITERS: list[str] = [WRITER]
EVALUATORS: list[str] | None = [EVALUATOR]  # None: both models of the other provider


def reviewers_for(writer: str) -> list[str]:
    if EVALUATORS is not None:
        return EVALUATORS
    return OPENAI if provider(writer) == "anthropic" else CLAUDE


def seeded_reviewers() -> list[str]:
    return EVALUATORS if EVALUATORS is not None else ALL


async def limited(coro):
    async with LIMIT:
        return await coro


async def main(dry: bool) -> None:
    f = facts()
    writer_system = WRITER_SYSTEM.format(facts=f)
    eval_system = EVALUATOR_SYSTEM.format(facts=f)
    by_id = {t.id: t for t in TASKS}

    if dry:
        print(f"system prompts: writer {len(writer_system):,} chars, evaluator {len(eval_system):,} chars")
        for s in SEEDED:
            print(f"rules on seeded {s.id}: {rules.check(by_id[s.task].channel, s.draft)}")
        for t in TASKS:
            if t.posted:
                print(f"rules on posted {t.id}: {rules.check(t.channel, t.posted)}")
        calls = sum(1 + len(reviewers_for(w)) for w in WRITERS) * len(TASKS) + len(SEEDED) * len(seeded_reviewers())
        print(f"model calls in a real run: {calls}")
        return

    started = time.time()
    spent: list[Result] = []

    # 1. Drafts: every writer, every task.
    jobs = [(t, w) for t in TASKS for w in WRITERS]
    drafted = await asyncio.gather(*(limited(call(w, writer_system, task_message(t), Draft)) for t, w in jobs))
    spent += drafted
    print(f"drafts: {sum(r.output is not None for r in drafted)}/{len(drafted)}")

    # 2. Reviews: each draft by both models of the other provider; seeded drafts by all four.
    review_jobs: list[tuple[str, Task, str, str]] = []  # (subject id, task, draft text, reviewer)
    drafts: list[dict] = []
    for (t, w), r in zip(jobs, drafted):
        text = r.output.text if r.output else ""
        fails, warns = rules.check(t.channel, text) if text else ([], [])
        drafts.append({"id": f"{t.id}/{w}", "task": t.id, "writer": w, "text": text, "error": r.error,
                       "rule_fails": fails, "rule_warns": warns, "cost": r.cost, "reviews": {}})
        if text:
            review_jobs += [(f"{t.id}/{w}", t, text, rv) for rv in reviewers_for(w)]
    seeded: list[dict] = []
    for s in SEEDED:
        t = by_id[s.task]
        fails, warns = rules.check(t.channel, s.draft)
        seeded.append({**asdict(s), "rule_fails": fails, "rule_warns": warns, "reviews": {}})
        review_jobs += [(f"seeded/{s.id}", t, s.draft, rv) for rv in seeded_reviewers()]

    reviewed = await asyncio.gather(
        *(limited(call(rv, eval_system, review_message(t, text), Verdict)) for _, t, text, rv in review_jobs)
    )
    spent += reviewed
    print(f"reviews: {sum(r.output is not None for r in reviewed)}/{len(reviewed)}")

    index = {d["id"]: d for d in drafts} | {f"seeded/{s['id']}": s for s in seeded}
    for (subject, _, _, rv), r in zip(review_jobs, reviewed):
        index[subject]["reviews"][rv] = (
            {"error": r.error} if r.output is None else {**r.output.model_dump(), "cost": r.cost}
        )

    cost_by_model = {m: round(sum(r.cost for r in spent if r.model == m), 4) for m in PRICES}
    summary = {
        "seconds": round(time.time() - started),
        "cost_total": round(sum(r.cost for r in spent), 4),
        "cost_by_model": cost_by_model,
        "calls": len(spent),
        "errors": [r.error for r in spent if r.error],
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "results.json").write_text(json.dumps({"summary": summary, "drafts": drafts, "seeded": seeded}, indent=2))
    (OUT / "report.html").write_text(report(drafts, seeded, summary))
    print(json.dumps(summary, indent=2))
    print(f"report: {OUT / 'report.html'}")


# ---------------------------------------------------------------- report

E = html.escape
CSS = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#14202b;--muted:#5a6878;--line:#d8dfe6;--ok:#0b7a4b;--warn:#a65d00;--bad:#b3261e;--acc:#0b6e99}
@media (prefers-color-scheme:dark){:root{--bg:#0f151b;--card:#161f28;--ink:#e3eaf0;--muted:#93a3b2;--line:#273440;--ok:#4cc38a;--warn:#f0a43a;--bad:#ff7b72;--acc:#5cb8e6}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:2rem max(16px,4vw) 4rem}
main{max-width:1100px;margin:0 auto;display:grid;gap:1.6rem}
h1{margin:0;font-size:1.9rem}h2{margin:0;font-size:1.3rem}h3{margin:0;font-size:1rem}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem 1.2rem;display:grid;gap:.7rem}
.thread{font-size:.9rem;color:var(--muted);border-left:3px solid var(--line);padding-left:.8rem;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:.9rem}
.draft{border:1px solid var(--line);border-radius:8px;padding:.8rem .9rem;display:grid;gap:.5rem;min-width:0}
.text{white-space:pre-wrap;font-size:.95rem}.label{font-weight:700;color:var(--acc)}
.pill{display:inline-block;font-size:.75rem;font-weight:600;padding:.1rem .5rem;border-radius:999px;border:1px solid currentColor;margin-right:.3rem}
.pass{color:var(--ok)}.revise{color:var(--warn)}.skip,.fail,.error{color:var(--bad)}
ul{margin:.2rem 0;padding-left:1.1rem;font-size:.85rem}table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{border-bottom:1px solid var(--line);padding:.5rem .6rem;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}
.scroll{overflow-x:auto}details summary{cursor:pointer;font-weight:600}
"""


def verdict_html(rv: dict) -> str:
    if "error" in rv:
        return f'<span class="pill error">error</span><span class="muted">{E(rv["error"])}</span>'
    s = rv["scores"]
    scores = " · ".join(f"{k.replace('_', ' ')} {v}" for k, v in s.items())
    probs = "".join(f"<li><b>{E(p['criterion'])}</b>: “{E(p['quote'])}” → {E(p['fix'])}</li>" for p in rv["problems"])
    claims = "".join(f"<li>unsupported: {E(c)}</li>" for c in rv["unsupported_claims"])
    return (f'<span class="pill {rv["verdict"]}">{rv["verdict"]}</span><span class="muted">{scores}</span>'
            f'{"<ul>" + probs + claims + "</ul>" if probs or claims else ""}')


def rules_html(item: dict) -> str:
    parts = [f'<span class="pill fail">rule</span>{E(x)}' for x in item["rule_fails"]]
    parts += [f'<span class="pill revise">note</span>{E(x)}' for x in item["rule_warns"]]
    return "<div>" + "<br>".join(parts) + "</div>" if parts else '<div class="muted">Rule checks: clean</div>'


def report(drafts: list[dict], seeded: list[dict], summary: dict) -> str:
    rng = random.Random(306)
    key_rows, sections = [], []
    for t in TASKS:
        items = [d for d in drafts if d["task"] == t.id]
        rng.shuffle(items)
        cards = []
        for n, d in enumerate(items):
            label = "ABCDEFGH"[n]
            key_rows.append(f"<tr><td>{E(t.id)}</td><td>{label}</td><td>{E(d['writer'])}</td>"
                            f"<td>{', '.join(E(r) for r in d['reviews'])}</td></tr>")
            reviews = "".join(f"<div><b>Reviewer {i + 1}</b> {verdict_html(rv)}</div>"
                              for i, rv in enumerate(d["reviews"].values()))
            body = f'<div class="text">{E(d["text"])}</div>' if d["text"] else f'<div class="error">{E(d["error"] or "")}</div>'
            cards.append(f'<div class="draft"><span class="label">Draft {label}</span>{body}{rules_html(d)}{reviews}</div>')
        posted = (f'<details><summary>What was actually posted</summary><div class="text">{E(t.posted)}</div></details>'
                  if t.posted else "")
        thread = "\n\n".join(t.thread[-2:]) if t.thread else ""
        sections.append(
            f'<section class="card"><h2>{E(t.id)} <span class="muted">· {t.channel}</span></h2>'
            f'{f"<div class=thread>{E(thread)}</div>" if thread else ""}<div class="muted">{E(t.ask)}</div>'
            f'<div class="grid">{"".join(cards)}</div>{posted}</section>')

    judges = seeded_reviewers()
    head = "".join(f"<th>{E(m)}</th>" for m in judges)
    rows = []
    for s in seeded:
        cells = []
        for m in judges:
            rv = s["reviews"].get(m, {"error": "missing"})
            cells.append(f"<td>{verdict_html(rv)}</td>")
        rows.append(f"<tr><td><b>{E(s['id'])}</b><div class='muted'>{E(s['planted'])}</div>"
                    f"<div class='text'>{E(s['draft'])}</div>{rules_html(s)}</td>{''.join(cells)}</tr>")
    caught = {m: sum(1 for s in seeded if s["reviews"].get(m, {}).get("verdict") in ("revise", "skip")) for m in judges}
    caught_row = "".join(f"<td><b>{caught[m]}/{len(seeded)}</b> caught</td>" for m in judges)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar bake-off</title><style>{CSS}</style></head><body><main>
<header><h1>Radar bake-off</h1><p class="muted">{summary['calls']} model calls · ${summary['cost_total']:.2f} · {summary['seconds']} s.
Drafts are shown blind: rate them first, then open the key at the bottom.</p></header>
{''.join(sections)}
<section class="card"><h2>Seeded drafts: did each evaluator catch the planted mistake?</h2>
<p class="muted">"Caught" means the verdict was revise or skip. The markdown case is meant for the rule checks.</p>
<div class="scroll"><table><tr><th>Draft</th>{head}</tr>{''.join(rows)}<tr><td></td>{caught_row}</tr></table></div></section>
<section class="card"><details><summary>Key: which model wrote each draft</summary><div class="scroll"><table>
<tr><th>Task</th><th>Draft</th><th>Writer</th><th>Reviewers (1, 2)</th></tr>{''.join(key_rows)}</table></div>
<p class="muted">Cost by model: {E(json.dumps(summary['cost_by_model']))}</p></details></section>
</main></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--writers", default=WRITER, help='comma-separated model IDs, or "all"')
    ap.add_argument("--evaluators", default=EVALUATOR, help='comma-separated model IDs, or "other" (the other provider)')
    a = ap.parse_args()
    WRITERS = ALL if a.writers == "all" else a.writers.split(",")
    EVALUATORS = None if a.evaluators == "other" else a.evaluators.split(",")
    for m in WRITERS + (EVALUATORS or []):
        if m not in PRICES:
            raise SystemExit(f"unknown model {m}; known: {', '.join(PRICES)}")
    asyncio.run(main(a.dry))
