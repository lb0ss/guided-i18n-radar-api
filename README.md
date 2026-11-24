# guided-i18n Radar (API)

A small marketing assistant for a solo developer, built for
[guided-i18n](https://github.com/lb0ss/guided-i18n), a VS Code extension for React i18n.
Plain code runs every step; AI models only do the language work; a person posts everything.

**Status: all 5 phases built.** Open **http://127.0.0.1:8000** for the review UI.

| Phase | What | State |
|---|---|---|
| 1 | Daily stats, new comments on your posts, digest, schedule API, start at login | ✅ |
| 2 | Model bake-off: which models write and which evaluate (`bakeoff/`) | ✅ done: Claude Haiku 5.5 writes, GPT-5.4 evaluates |
| 3 | Research every 3 days: threads worth a reply, and intel (read, don't reply) | ✅ |
| 4 | Write ⇄ evaluate loop, Draft a Reply (`uv run radar draft …`, `POST /drafts`) | ✅ |
| 5 | Review UI: [guided-i18n-radar-web](https://github.com/lb0ss/guided-i18n-radar-web), served by this API at `/` | ✅ |

## What the daily check does (no AI; free except optional X)

- **Stats** from public APIs: Open VSX downloads, VS Code Marketplace installs and downloads,
  GitHub stars and issues, dev.to reactions and comments (page views with `DEV_API_KEY`).
- **Comments on your posts:** new GitHub issues and issue comments, and comments on your dev.to
  articles. Your own comments are skipped. The first check records what's already there; after
  that, only new comments are reported. Replies are drafted only after you confirm, one comment at
  a time (phase 4), and nothing is ever posted for you.
- **Digest:** saved to `data/digests/`, and emailed or pushed when configured. Quiet days are saved
  but not sent.
- **X (optional, paid):** with `X_BEARER_TOKEN` set, replies to and mentions of your X account
  (`watch.x` in sources.yaml) since the last check. X bills per post returned (about $0.005), so a
  quiet day costs nothing; Radar stops at `X_MONTHLY_POST_LIMIT` posts a month and says so in the
  digest. Research can then also search X (`x` in the brief's `queries_per_source`).
- Reddit isn't watched: it's closing its public API. Use its own notifications, then paste the
  comment into Draft a Reply.

What to track is in `projects/guided-i18n/sources.yaml`.

## Setup

```sh
uv sync
cp .env.example .env          # optional settings: email, push, dev.to views
uv run radar checks           # one check now, prints the digest
uv run pytest
```

### Run in the background (macOS)

```sh
scripts/launch-agent.sh install    # starts at login, restarts if it stops
scripts/launch-agent.sh status
scripts/launch-agent.sh uninstall
```

After pulling or editing either repo, `scripts/update.sh` installs what changed, rebuilds the UI and
restarts the service.

The API listens on `http://127.0.0.1:8000` only. Jobs run while the Mac is awake; a job missed
while it was asleep runs once when it wakes.

| Route | |
|---|---|
| `GET /schedule` | Each job's interval, whether it's on, its last run |
| `PUT /schedule/{job}` | `{"interval_hours": 12, "enabled": true}`; applies on the next tick |
| `POST /run/{job}` | Run now |
| `GET /stats`, `GET /comments?state=new`, `POST /comments/{id}/dismiss`, `GET /runs` | |

Defaults: checks every 24 hours; research every 72 hours (off until phase 3).

## Bake-off (phase 2)

`uv run python -m bakeoff.run --dry` shows the plan; without `--dry` it calls the models (needs
both API keys, about $0.10 for the chosen pair). The real test threads quote other people's Reddit
comments, so they live in a git-ignored `bakeoff/cases_private.py`; without it, a made-up example
runs instead.

## Data and privacy

Everything stays on your machine: `data/radar.db` (SQLite) and the digests are git-ignored, and so
are `.env` and the bake-off's real threads.
