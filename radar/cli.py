"""uv run radar checks · uv run radar research · uv run radar serve"""

import asyncio
import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "checks":
        from .checks import run_checks

        result = asyncio.run(run_checks())
        print(result["text"])
        print({k: v for k, v in result.items() if k != "text"})
    elif cmd == "research":
        from .research import run_research

        result = asyncio.run(run_research())
        print(result["text"])
        print({k: v for k, v in result.items() if k != "text"})
    elif cmd == "serve":
        import uvicorn

        uvicorn.run("radar.api:app", host="127.0.0.1", port=8000)
    else:
        print("usage: radar checks | radar research | radar serve")
