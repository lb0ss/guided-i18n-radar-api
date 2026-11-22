"""SQLite through SQLModel. One file, data/radar.db (git-ignored)."""

from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Field, Session, SQLModel, create_engine

_engine: Engine | None = None


def now() -> datetime:
    return datetime.now(UTC)


class Run(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job: str
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    status: str = "running"  # running | ok | partial | failed
    cost: float = 0.0


class Step(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    name: str
    status: str  # ok | failed
    seconds: float
    detail: str = ""


class Stat(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    at: datetime = Field(default_factory=now)
    source: str = Field(index=True)  # openvsx | marketplace | github | devto
    metric: str
    value: float


class MyPost(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    platform: str  # devto | github | reddit | x
    url: str = Field(unique=True)
    title: str
    posted_at: datetime | None = None
    text: str = ""  # your final text: context for drafting replies later


class Comment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    platform: str
    external_id: str = Field(unique=True)  # platform-prefixed, e.g. "devto:2kl3", "github:6888446213"
    post_url: str
    url: str
    author: str
    body: str
    created_at: datetime
    found_at: datetime = Field(default_factory=now)
    # baseline: already there on the first check (never reported) · new · dismissed · drafted
    state: str = Field(default="new", index=True)


class Opportunity(SQLModel, table=True):
    """A thread research found. Low scores are kept too, so they're never scored twice."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    url: str = Field(unique=True)
    source: str
    title: str
    excerpt: str = ""
    created_at: datetime
    replies: int = 0
    query: str = ""
    found_at: datetime = Field(default_factory=now)
    relevance: int = 0  # 1-5 from the scorer
    category: str = ""  # problem | competitor | request | mention | other
    angle: str = ""  # how a reply could help
    reason: str = ""
    # new · low (below the brief's keep_relevance) · drafted · posted · skipped
    state: str = Field(default="new", index=True)


class Schedule(SQLModel, table=True):
    job: str = Field(primary_key=True)  # checks | research
    interval_hours: float
    enabled: bool = True
    last_run_at: datetime | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        from .config import settings

        _engine = create_engine(settings().db_url, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(_engine)
    return _engine


def use_engine(e: Engine) -> None:
    """Tests: point everything at another database."""
    global _engine
    _engine = e
    SQLModel.metadata.create_all(e)


def session() -> Session:
    return Session(engine(), expire_on_commit=False)
