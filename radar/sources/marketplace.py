"""VS Code Marketplace: installs and downloads, from the public gallery query the Marketplace site uses."""

import httpx

URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"


async def stats(client: httpx.AsyncClient, extension: str) -> dict[str, float]:
    r = await client.post(
        URL,
        json={"filters": [{"criteria": [{"filterType": 7, "value": extension}]}], "flags": 914},
        headers={"Accept": "application/json;api-version=3.0-preview.1"},
    )
    r.raise_for_status()
    ext = r.json()["results"][0]["extensions"][0]
    s = {x["statisticName"]: x["value"] for x in ext.get("statistics", [])}
    return {"installs": s.get("install", 0), "downloads": s.get("downloadCount", 0), "ratings": s.get("ratingcount", 0)}
