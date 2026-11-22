"""Public APIs Radar reads. Each function takes an httpx.AsyncClient so tests can fake the responses."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class FoundComment:
    platform: str
    external_id: str
    post_url: str
    post_title: str
    url: str
    author: str
    body: str
    created_at: datetime


@dataclass
class FoundPost:
    platform: str
    url: str
    title: str
    posted_at: datetime | None
