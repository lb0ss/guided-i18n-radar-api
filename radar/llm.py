"""One structured call per step, to either provider. Code decides everything else."""

from dataclasses import dataclass

import anthropic
import openai
from pydantic import BaseModel

PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
}
CLAUDE = ["claude-sonnet-5-5", "claude-haiku-5-5"]
OPENAI = ["gpt-5.4", "gpt-5.4-mini"]




def provider(model: str) -> str:
    return "anthropic" if model.startswith("claude") else "openai"


@dataclass
class Result:
    model: str
    output: BaseModel | None
    input_tokens: int
    output_tokens: int
    error: str | None = None

    @property
    def cost(self) -> float:
        i, o = PRICES[self.model]
        return (self.input_tokens * i + self.output_tokens * o) / 1_000_000


_anthropic: anthropic.AsyncAnthropic | None = None
_openai: openai.AsyncOpenAI | None = None


async def call(model: str, system: str, user: str, schema: type[BaseModel]) -> Result:
    global _anthropic, _openai
    try:
        if provider(model) == "anthropic":
            _anthropic = _anthropic or anthropic.AsyncAnthropic()
            r = await _anthropic.messages.parse(
                model=model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
            usage = (r.usage.input_tokens, r.usage.output_tokens)
            if r.stop_reason == "refusal":
                return Result(model, None, *usage, error="refused")
            if r.stop_reason == "max_tokens":
                return Result(model, None, *usage, error="ran out of tokens")
            return Result(model, r.parsed_output, *usage)
        _openai = _openai or openai.AsyncOpenAI()
        r = await _openai.responses.parse(
            model=model,
            instructions=system,
            input=user,
            text_format=schema,
            max_output_tokens=16000,
        )
        usage = (r.usage.input_tokens, r.usage.output_tokens) if r.usage else (0, 0)
        if r.output_parsed is None:
            return Result(model, None, *usage, error="no parsed output")
        return Result(model, r.output_parsed, *usage)
    except (anthropic.APIError, openai.APIError) as e:
        return Result(model, None, 0, 0, error=f"{type(e).__name__}: {e}")
