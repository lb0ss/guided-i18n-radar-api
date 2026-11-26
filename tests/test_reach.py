"""Reach: real readings, the change with its time span, release and post markers."""

import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from radar.db import Run, Stat, session
from radar.reach import posts, reach, readings, releases


def check(at: datetime, openvsx: float, marketplace: float):
    with session() as db:
        r = Run(job="checks", started_at=at.astimezone(UTC))
        db.add(r)
        db.commit()
        db.refresh(r)
        db.add_all([Stat(run_id=r.id, source="openvsx", metric="downloads", value=openvsx),
                    Stat(run_id=r.id, source="marketplace", metric="downloads", value=marketplace)])
        db.commit()


@pytest.fixture
def local_noon():
    t = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    return t - timedelta(days=5)


def test_near_duplicate_readings_count_once(s, local_noon):
    check(local_noon, 100, 10)
    check(local_noon + timedelta(minutes=1), 100, 10)
    check(local_noon + timedelta(days=1), 124, 16)
    assert len(readings()) == 2


def test_the_change_says_over_how_long(s, local_noon):
    check(local_noon, 900, 160)
    check(local_noon + timedelta(hours=48), 942, 171)
    r = reach()
    ovsx = r["stores"][0]["change"]
    assert ovsx["delta"] == 42 and ovsx["hours"] == 48.0 and ovsx["per_day"] == 21.0 and ovsx["pct"] == 4.7
    assert r["total"]["value"] == 1113 and r["total"]["change"]["delta"] == 53


def test_posts_come_from_the_marketing_log(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/MARKETING.md").write_text(
        "| Date | What | Notes |\n|---|---|---|\n"
        "| 2026-11-14 | Posted on r/webdev (Showoff Saturday): https://reddit.com/x | ok |\n"
        "| 2026-11-16 | Asked the mods; posted on r/SideProject (story angle) | ok |\n"
        "| 2026-11-18 | X account set up; launch post: AI-agent story | ok |\n"
        "| 2026-11-19 | X day 2 post: plurals | ok |\n"
        "| 2026-11-18 | This Week in React #306 checked | not a post |\n")
    assert [(e["day"], e["label"]) for e in posts(tmp_path)] == [
        ("2026-11-14", "r/webdev"), ("2026-11-16", "r/SideProject"), ("2026-11-18", "X #1"), ("2026-11-19", "X #2")]


def test_releases_come_from_version_tags(tmp_path):
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True,
                                    env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                                         "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    run("init", "-q")
    run("commit", "-q", "--allow-empty", "-m", "x")
    run("tag", "-a", "v0.1.4", "-m", "release")
    run("tag", "notarelease")
    assert [e["label"] for e in releases(tmp_path)] == ["0.1.4"]
    assert releases(tmp_path / "missing") == []



def test_points_are_the_real_readings_and_add_up(s, local_noon):
    check(local_noon, 857, 148)
    check(local_noon + timedelta(days=1), 936, 166)
    check(local_noon + timedelta(days=3), 978, 177)
    r = reach()
    assert [(p["openvsx"], p["openvsx_change"]) for p in r["points"]] == [(857, None), (936, 79), (978, 42)]
    ovsx = r["stores"][0]
    assert ovsx["change"]["delta"] == 42 and ovsx["since_first"]["delta"] == 121 == 79 + 42  # every number adds up
    assert r["total"]["since_first"]["delta"] == 121 + 29
