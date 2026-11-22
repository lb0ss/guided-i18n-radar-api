"""open-vsx.org: downloads and reviews."""

import httpx


async def stats(client: httpx.AsyncClient, extension: str) -> dict[str, float]:
    r = await client.get(f"https://open-vsx.org/api/{extension}")
    r.raise_for_status()
    d = r.json()
    return {"downloads": d.get("downloadCount") or 0, "reviews": d.get("reviewCount") or 0}
