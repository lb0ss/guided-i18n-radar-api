import json
from dataclasses import replace

import httpx
import pytest
from sqlmodel import create_engine

from radar import db
from radar.config import settings


@pytest.fixture
def s(tmp_path):
    """Settings with a temporary data dir and database, no email or push."""
    st = replace(settings(), data_dir=tmp_path, smtp_host=None, pushover_token=None, devto_api_key=None, github_token=None)
    db.use_engine(create_engine(f"sqlite:///{tmp_path / 'radar.db'}"))
    return st


class FakeWeb:
    """Answers the public APIs from in-memory data; tests change `comments` between runs."""

    def __init__(self):
        self.downloads = 857
        self.devto_comments = []
        self.github_comments = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "open-vsx.org" in u:
            return httpx.Response(200, json={"downloadCount": self.downloads, "reviewCount": 0})
        if "marketplace.visualstudio.com" in u:
            stats = [{"statisticName": "install", "value": 1.0}, {"statisticName": "downloadCount", "value": 148.0}]
            return httpx.Response(200, json={"results": [{"extensions": [{"statistics": stats}]}]})
        if u.startswith("https://api.github.com/repos/lb0ss/guided-i18n/issues/comments"):
            return httpx.Response(200, json=self.github_comments)
        if u.startswith("https://api.github.com/repos/lb0ss/guided-i18n/issues"):
            return httpx.Response(200, json=[])
        if u.startswith("https://api.github.com/repos/lb0ss/guided-i18n"):
            return httpx.Response(200, json={"stargazers_count": 0, "forks_count": 0, "open_issues_count": 3})
        if "dev.to/api/articles" in u:
            return httpx.Response(200, json=[{"id": 1, "title": "Wrapping strings in t()", "url": "https://dev.to/lb0ss/a",
                                              "published_at": "2026-11-11T00:00:00Z", "public_reactions_count": 2, "comments_count": 1}])
        if "dev.to/api/comments" in u:
            return httpx.Response(200, json=self.devto_comments)
        return httpx.Response(404, text=json.dumps({"unhandled": u}))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def web():
    return FakeWeb()


def devto_comment(code: str, user: str, text: str, children=()):
    return {"id_code": code, "user": {"username": user}, "body_html": f"<p>{text}</p>",
            "created_at": "2026-11-20T10:00:00Z", "children": list(children)}


def gh_comment(cid: int, user: str, text: str):
    return {"id": cid, "user": {"login": user}, "body": text, "created_at": "2026-11-20T10:00:00Z",
            "html_url": f"https://github.com/lb0ss/guided-i18n/issues/3#issuecomment-{cid}"}
