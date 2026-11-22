"""Rule checks in plain code: run before (and regardless of) the model evaluator."""

import re

BANNED = ["next step", "great question", "good question", "game-changer", "game changer", "love this", "i'd be happy to"]
LIMITS = {"reddit-reply": 500, "x-post": 280}


def check(channel: str, text: str) -> tuple[list[str], list[str]]:
    """Returns (failures, warnings)."""
    fails: list[str] = []
    warns: list[str] = []
    if len(text) > LIMITS[channel]:
        fails.append(f"too long: {len(text)} characters (limit {LIMITS[channel]})")
    if channel == "reddit-reply":
        if re.search(r"\*\*|__", text):
            fails.append("Markdown bold in a Reddit reply")
        if re.search(r"(?m)^\s*([-*•]|\d+\.)\s", text):
            fails.append("list formatting in a Reddit reply")
        if re.search(r"(?m)^#", text):
            fails.append("heading in a Reddit reply")
    if channel == "x-post":
        if re.search(r"https?://", text):
            fails.append("link in the post (links go in the first reply)")
        if len(re.findall(r"#\w", text)) > 1:
            fails.append("more than one hashtag")
    low = text.lower()
    fails += [f'banned phrase: "{p}"' for p in BANNED if p in low]
    if "—" in text:
        warns.append("em dash (often reads as AI-written)")
    return fails, warns
