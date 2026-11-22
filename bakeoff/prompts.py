"""Writer and evaluator prompts, and the facts both of them get."""

import json
import os
from pathlib import Path

from .cases import Task

# The extension's repo, for docs/FACTS.md and the CHANGELOG.
REPO = Path(os.environ.get("I18N_CORE_PATH", "~/repos/i18n-ai-core")).expanduser()

CHANNEL_RULES = {
    "reddit-reply": (
        "A Reddit comment reply in the author's own thread (they are OP, so no need to say they built it). "
        "Plain text only: no Markdown, no bold, no bullet lists, no headings. 2 to 4 sentences, at most 500 "
        "characters. Answer the person's actual point or question first."
    ),
    "x-post": (
        "One post on X, at most 280 characters. No links (they go in the first reply), at most one hashtag, "
        "no emoji walls. Short lines are fine."
    ),
}


def facts() -> str:
    """docs/FACTS.md (checked against the code each release) plus the CHANGELOG."""
    facts_md = (REPO / "docs/FACTS.md").read_text()
    changelog = (REPO / "packages/vscode/CHANGELOG.md").read_text()
    return f"<facts>\n{facts_md}\n</facts>\n\n<changelog>\n{changelog}\n</changelog>"


WRITER_SYSTEM = """\
You draft replies and posts for the solo developer of guided-i18n, a VS Code extension for React i18n.
The developer reviews, edits and posts every draft themselves.

Write like the developer would: plain, specific, friendly, American English. No marketing tone, no hype
words, no stock openers ("Great question!", "Love this!"), no em dashes. Never claim anything the facts
below don't support, never present open issues as shipped, and never promise future work or dates. If the
honest answer is "not yet" or "no", say so plainly.

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


def task_message(task: Task) -> str:
    return json.dumps(
        {
            "channel": task.channel,
            "channel_rules": CHANNEL_RULES[task.channel],
            "thread": task.thread,
            "instruction": task.ask,
        },
        indent=2,
    )


def review_message(task: Task, draft: str) -> str:
    # The evaluator sees the thread, the rules and the draft; never the writer's reasoning.
    return json.dumps(
        {
            "channel": task.channel,
            "channel_rules": CHANNEL_RULES[task.channel],
            "thread": task.thread,
            "instruction_given_to_writer": task.ask,
            "draft": draft,
        },
        indent=2,
    )
