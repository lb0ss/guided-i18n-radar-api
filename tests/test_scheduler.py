from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from radar import scheduler
from radar.db import Schedule


def test_due_after_the_interval_and_on_first_run():
    t = datetime(2026, 11, 22, 12, tzinfo=UTC)
    assert scheduler.is_due(Schedule(job="checks", interval_hours=24), t)
    assert not scheduler.is_due(Schedule(job="checks", interval_hours=24, last_run_at=t - timedelta(hours=23)), t)
    assert scheduler.is_due(Schedule(job="checks", interval_hours=24, last_run_at=t - timedelta(days=5)), t)  # one catch-up run


def test_jobs_that_arent_built_never_run():
    assert not scheduler.is_due(Schedule(job="drafting", interval_hours=72, enabled=True))


def test_schedule_api(s, monkeypatch):
    monkeypatch.setattr(scheduler, "TICK_SECONDS", 3600)
    monkeypatch.setattr(scheduler, "JOBS", {"checks": lambda: None})  # the loop must not hit the network
    monkeypatch.setattr(scheduler, "is_due", lambda *a, **k: False)
    from radar.api import app

    with TestClient(app) as c:
        jobs = {j["job"]: j for j in c.get("/schedule").json()}
        assert jobs["checks"]["interval_hours"] == 24 and jobs["checks"]["enabled"]
        assert jobs["research"]["interval_hours"] == 72
        assert c.put("/schedule/checks", json={"interval_hours": 12}).json()["interval_hours"] == 12
        assert c.put("/schedule/nope", json={"enabled": True}).status_code == 404
        assert c.put("/schedule/checks", json={"interval_hours": 0.1}).status_code == 422
