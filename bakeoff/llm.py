"""Bake-off schemas; the model calls themselves live in radar/llm.py."""

from typing import Literal

from pydantic import BaseModel, Field

from radar.llm import CLAUDE, OPENAI, PRICES, Result, call, provider  # noqa: F401


class Draft(BaseModel):
    text: str = Field(description="The reply or post, exactly as it would be posted.")


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


