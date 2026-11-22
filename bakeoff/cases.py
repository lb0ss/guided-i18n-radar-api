"""Bake-off inputs: real threads to reply to, one X post, and seeded drafts
with known mistakes (to test whether each evaluator catches them)."""

from dataclasses import dataclass, field

@dataclass
class Task:
    id: str
    channel: str  # reddit-reply | x-post
    thread: list[str]  # oldest first; the last entry is what we reply to
    ask: str
    posted: str | None = None  # what the author actually posted, for comparison


@dataclass
class Seeded:
    id: str
    task: str  # id of the Task whose thread it answers
    draft: str
    planted: str  # the known problem an evaluator should catch
    layer: str = "model"  # "model" or "rules" (caught by code)
    extra: dict = field(default_factory=dict)



EXAMPLE_TASKS = [
    Task(
        id="example",
        channel="reddit-reply",
        thread=[
            "Post by the author: a VS Code extension that moves a React app's hardcoded text into react-i18next.",
            "Commenter: Does it handle plurals for languages like Polish?",
        ],
        ask="Reply to the commenter.",
    ),
]
EXAMPLE_SEEDED = [
    Seeded(id="example-overpromise", task="example", planted="promises a release date",
           draft="Polish works, and a smarter plural mode ships next week!"),
]

try:  # the real cases, kept out of git (see cases_private.py)
    from .cases_private import SEEDED, TASKS  # noqa: F401
except ImportError:
    TASKS, SEEDED = EXAMPLE_TASKS, EXAMPLE_SEEDED
