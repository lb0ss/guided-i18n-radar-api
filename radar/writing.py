"""Write ⇄ evaluate. Code runs the loop: the writer (Claude Haiku 5.5) drafts, rule checks in code catch
format slips without a model, the evaluator (GPT-5.4, another provider) judges against FACTS.md and the
CHANGELOG. Revise sends the problems back; at most 3 attempts. Nothing is ever posted."""

import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .config import ROOT, Settings, settings
from .db import Draft, DraftJob, session
from .llm import PRICES, call

WRITER, EVALUATOR = "claude-haiku-5-5", "gpt-5.4"  # chosen in the bake-off (2026-11-20)
MAX_ATTEMPTS = 3
BANNED = ["next step", "great question", "good question", "game-changer", "game changer", "love this",
          "i'd be happy to", "hope this helps"]
DISCLOSURE = re.compile(r"\b(I built|I made|I wrote|I'm the (author|developer|dev)|my (extension|tool|project)|I maintain)\b", re.IGNORECASE)


class DraftText(BaseModel):
    text: str = Field(description="The reply or post, exactly as it would be posted.")


class PostText(DraftText):
    """A new post (not a reply): the text plus what goes with it."""
    visual: str = Field(description="One image that makes the point at a glance: what it shows, concretely.")
    alt_text: str = Field(description="Alt text for that image: what it shows, for someone who can't see it.")


class Scores(BaseModel):
    answers_question: int
    claims_backed: int
    no_overpromising: int
    sounds_human: int
    fits_channel: int
    worth_posting: int


class Problem(BaseModel):
    criterion: str
    quote: str
    fix: str


class Verdict(BaseModel):
    verdict: Literal["pass", "revise", "skip"]
    scores: Scores
    problems: list[Problem]
    unsupported_claims: list[str]


def channels(s: Settings | None = None) -> dict:
    s = s or settings()
    return yaml.safe_load((ROOT / "projects" / s.project / "channels.yaml").read_text())


def facts() -> str:
    repo = Path(os.environ.get("I18N_CORE_PATH", "~/repos/i18n-ai-core")).expanduser()
    f = (repo / "docs/FACTS.md").read_text()
    c = (repo / "packages/vscode/CHANGELOG.md").read_text()
    return f"<facts>\n{f}\n</facts>\n\n<changelog>\n{c}\n</changelog>"


def check_rules(ch: dict, text: str, own_post: bool) -> tuple[list[str], list[str]]:
    """Returns (failures, warnings). Failures go back to the writer without asking the evaluator."""
    fails, warns = [], []
    if len(text) > ch["max_chars"]:
        fails.append(f"too long: {len(text)} characters (limit {ch['max_chars']})")
    if ch["format"] == "plain":
        if re.search(r"\*\*|__|`", text):
            fails.append("Markdown (bold or code) where plain text is required")
        if re.search(r"(?m)^\s*([-*•]|\d+\.)\s", text):
            fails.append("list formatting where plain text is required")
        if re.search(r"(?m)^#", text):
            fails.append("a heading where plain text is required")
    links = len(re.findall(r"https?://", text))
    if links > ch.get("max_links", 1):
        fails.append(f"{links} links (at most {ch.get('max_links', 1)})")
    if "max_hashtags" in ch and len(re.findall(r"(?<!\w)#\w", text)) > ch["max_hashtags"]:
        fails.append(f"more than {ch['max_hashtags']} hashtag")
    low = text.lower()
    fails += [f'banned phrase: "{p}"' for p in BANNED if p in low]
    if "guided-i18n" in low and not own_post and not DISCLOSURE.search(text):
        fails.append('mentions guided-i18n without saying you built it (e.g. "I built …")')
    if "—" in text:
        warns.append("em dash (often reads as AI-written)")
    return fails, warns


WRITER_SYSTEM = """\
You draft replies and posts for the solo developer of guided-i18n, a VS Code extension for React i18n.
The developer reviews, edits and posts every draft themselves.

Write like the developer would: plain, specific, friendly, American English. No marketing tone, no hype
words, no stock openers ("Great question!", "Love this!"), no em dashes. Help with the person's actual
problem first; mention guided-i18n only where it genuinely helps, and then say you built it
{disclosure}. Never claim anything the facts below don't support, never present open issues as shipped,
and never promise future work or dates. If the honest answer is "not yet" or "no", say so plainly.

Facts about the product (the only source of truth):

{facts}"""

EVALUATOR_SYSTEM = """\
You review a draft reply or post before the developer of guided-i18n publishes it. Be strict: a draft that
would embarrass the developer in public must not pass.

Score each criterion from 1 (bad) to 5 (good):
- answers_question: responds to what the person actually said or asked, before anything else (for a post: makes one clear point)
- claims_backed: every claim about the product is supported by the facts below; nothing from the open issues is presented as shipped; numbers match
- no_overpromising: no commitments to future work, releases or dates
- sounds_human: reads like a person wrote it, not a press release or an AI assistant
- fits_channel: follows the channel's rules
- worth_posting: adds something; not just self-promotion

Stating a limitation or an open issue honestly (including its issue number) is good, not overpromising, as
long as no date or commitment is made. Don't penalize a claim the facts support just because it's specific.

Verdict: "pass" if it can be posted as is, "revise" if it has fixable problems, "skip" if the thread isn't
worth replying to. For every problem, quote the exact sentence and say how to fix it. List each claim the
facts don't support in unsupported_claims.

Facts about the product (the only source of truth):

{facts}"""


def estimate() -> tuple[float, float]:
    """Rough cost of one attempt and of the worst case, from the prompt size."""
    tokens_in = (len(WRITER_SYSTEM) + len(facts())) / 3.5 + 800
    one = (tokens_in * PRICES[WRITER][0] + 400 * PRICES[WRITER][1]
           + (tokens_in + 400) * PRICES[EVALUATOR][0] + 900 * PRICES[EVALUATOR][1]) / 1e6
    return one, one * MAX_ATTEMPTS


async def draft(kind: str, channel: str, thread: list[str], source_id: int | None = None, post_id: int | None = None,
                instruction: str | None = None, llm=call, on_text=None) -> DraftJob:
    """on_text(output) is called with the writer's last output (for a post: its image idea and alt text)."""
    ch = channels()[channel]
    own_post = post_id is not None or kind in ("comment", "post")  # a post of your own needs no "I built it"
    schema = PostText if kind == "post" else DraftText
    f = facts()
    writer_sys = WRITER_SYSTEM.format(facts=f, disclosure="" if own_post else '(e.g. "I built …"); you are not the original poster here')
    if own_post:
        writer_sys = writer_sys.replace("and then say you built it\n", "(you are the original poster, so no need to say you built it)\n")
    evaluator_sys = EVALUATOR_SYSTEM.format(facts=f)
    task = {"channel": channel, "channel_rules": ch["guide"], "you_are_the_original_poster": own_post,
            "thread": thread, "instruction": instruction or "Draft a reply to the last message in the thread."}
    with session() as db:
        job = DraftJob(kind=kind, source_id=source_id, post_id=post_id, channel=channel, thread=json.dumps(thread))
        db.add(job)
        db.commit()
        db.refresh(job)

    feedback: list[str] = []
    last = None
    status = "failed"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user = json.dumps(task | ({"fix_these_problems_from_the_last_attempt": feedback} if feedback else {}), ensure_ascii=False, indent=1)
        w = await llm(WRITER, writer_sys, user, schema)
        d = Draft(job_id=job.id, attempt=attempt, text=w.output.text if w.output else "", cost=w.cost)
        if w.output is not None and on_text:
            on_text(w.output)
        if w.output is None:
            d.rule_failures = json.dumps([f"writer failed: {w.error}"])
            _save(d)
            break
        fails, warns = check_rules(ch, d.text, own_post)
        d.rule_failures, d.rule_warnings = json.dumps(fails), json.dumps(warns)
        if fails:  # code catches these: back to the writer, no evaluator call
            feedback = fails
            last = _save(d)
            status = "needs work"
            continue
        review = json.dumps({"channel": channel, "channel_rules": ch["guide"], "you_are_the_original_poster": own_post,
                             "thread": thread, "draft": d.text}, ensure_ascii=False, indent=1)  # never the writer's reasoning
        e = await llm(EVALUATOR, evaluator_sys, review, Verdict)
        d.cost += e.cost
        if e.output is None:
            d.verdict = "error"
            d.problems = json.dumps([{"criterion": "evaluator", "quote": "", "fix": e.error or "no verdict"}])
            last = _save(d)
            status = "needs work"
            break
        v = e.output
        d.verdict, d.scores = v.verdict, v.scores.model_dump_json()
        d.problems = json.dumps([p.model_dump() for p in v.problems], ensure_ascii=False)
        d.unsupported = json.dumps(v.unsupported_claims, ensure_ascii=False)
        last = _save(d)
        if v.verdict in ("pass", "skip"):
            status = v.verdict
            break
        status = "needs work"
        feedback = [f'{p.criterion}: "{p.quote}" → {p.fix}' for p in v.problems] + [f"unsupported: {c}" for c in v.unsupported_claims]

    with session() as db:
        j = db.get(DraftJob, job.id)
        j.status = status
        j.final_draft_id = last.id if last else None
        j.cost = round(sum(x.cost for x in db.exec(_drafts_of(job.id)).all()), 4)
        db.add(j)
        db.commit()
        db.refresh(j)
        return j


def _drafts_of(job_id: int):
    from sqlmodel import select

    return select(Draft).where(Draft.job_id == job_id).order_by(Draft.attempt)


def _save(d: Draft) -> Draft:
    with session() as db:
        db.add(d)
        db.commit()
        db.refresh(d)
        return d


def attempts(job_id: int) -> list[Draft]:
    with session() as db:
        return list(db.exec(_drafts_of(job_id)).all())
