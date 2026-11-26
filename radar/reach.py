"""Reach: downloads across both stores, as real readings. Free: computed from stored checks, the
extension repo's release tags, and the posts logged in its docs/MARKETING.md.

Every number is a reading a check actually took, or the difference between two of them, so the page
always adds up and matches the notification's "+N". Nothing is estimated."""

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import col, select

from .db import Run, Stat, session

STORES = {"openvsx": "Open VSX", "marketplace": "VS Code Marketplace"}
SAME_READING_HOURS = 1  # checks closer than this (e.g. setup runs a minute apart) count as one reading


def _local(t: datetime) -> datetime:
    return t.replace(tzinfo=t.tzinfo or UTC).astimezone()  # SQLite drops the zone; stored times are UTC


def readings() -> list[tuple[datetime, dict[str, float]]]:
    """(time, {store: downloads}) per check that read every store, oldest first, near-duplicates merged."""
    with session() as db:
        rows = db.exec(select(Stat, Run.started_at).join(Run, Run.id == Stat.run_id)
                       .where(Stat.metric == "downloads", col(Stat.source).in_(list(STORES))).order_by(col(Stat.run_id))).all()
    by_run: dict[datetime, dict[str, float]] = {}
    for stat, started in rows:
        by_run.setdefault(_local(started), {})[stat.source] = stat.value
    out: list[tuple[datetime, dict[str, float]]] = []
    for t, vals in sorted(by_run.items()):
        if set(vals) != set(STORES):
            continue
        if out and (t - out[-1][0]) < timedelta(hours=SAME_READING_HOURS):
            out[-1] = (t, vals)  # keep the later of two near-identical readings
        else:
            out.append((t, vals))
    return out


def _change(rs, key) -> dict | None:
    if len(rs) < 2:
        return None
    (t0, v0), (t1, v1) = rs[-2], rs[-1]
    get = (lambda v: v[key]) if key else (lambda v: sum(v.values()))
    hours = (t1 - t0).total_seconds() / 3600
    delta = get(v1) - get(v0)
    return {"delta": delta, "hours": round(hours, 1), "since": t0.isoformat(),
            "per_day": round(delta / hours * 24, 1) if hours else None,
            "pct": round(delta / get(v0) * 100, 1) if get(v0) else None}


def releases(repo: Path) -> list[dict]:
    """Release tags (v*) of the extension repo, with their dates."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short) %(creatordate:iso-strict)", "refs/tags/v*"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    events = []
    for line in out.splitlines():
        tag, _, when = line.partition(" ")
        if when:
            events.append({"day": datetime.fromisoformat(when).astimezone().date().isoformat(), "kind": "release", "label": tag.lstrip("v")})
    return events


POST_PATTERNS = [  # (regex on a MARKETING.md log row, label); case-insensitive
    (r"\bposted on (r/\w+)", "{0}"),
    (r"\bpublished the dev\.to article\b", "dev.to article"),
    (r"\bX day (\d+) post\b", "X #{0}"),
    (r"\bX account\b.*\blaunch post\b", "X #1"),
]


def posts(repo: Path) -> list[dict]:
    """Your posts, from the dated rows of docs/MARKETING.md ("Posted on r/...", "X day N post"...)."""
    try:
        text = (repo / "docs/MARKETING.md").read_text()
    except OSError:
        return []
    events = []
    for m in re.finditer(r"^\| (\d{4}-\d{2}-\d{2}) \| (.+?) \|", text, re.MULTILINE):
        day, row = m.groups()
        for pattern, label in POST_PATTERNS:
            for hit in re.finditer(pattern, row, re.IGNORECASE):
                events.append({"day": day, "kind": "post", "label": label.format(*hit.groups())})
    return events


def _since_first(rs, key) -> dict | None:
    """The change from the first reading to the latest: what the whole chart covers."""
    return _change([rs[0], rs[-1]], key) if len(rs) > 1 else None


def reach(repo: Path | None = None) -> dict:
    repo = repo or Path(os.environ.get("I18N_CORE_PATH", "~/repos/i18n-ai-core")).expanduser()
    rs = readings()
    latest = rs[-1][1] if rs else {}
    return {
        "checked_at": rs[-1][0].isoformat() if rs else None,
        "first_at": rs[0][0].isoformat() if rs else None,
        "total": {"value": sum(latest.values()), "change": _change(rs, None), "since_first": _since_first(rs, None)},
        "stores": [{"source": s, "name": n, "value": latest.get(s), "change": _change(rs, s), "since_first": _since_first(rs, s)}
                   for s, n in STORES.items()],
        # one point per check, as read; each with its change since the check before
        "points": [{"at": t.isoformat(), **v, **{f"{s}_change": (v[s] - rs[i - 1][1][s]) if i else None for s in STORES}}
                   for i, (t, v) in enumerate(rs)],
        "events": sorted(releases(repo) + posts(repo), key=lambda e: e["day"]),
    }
