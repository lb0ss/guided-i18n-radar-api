import asyncio

from sqlmodel import select

from radar.checks import run_checks
from radar.db import Comment, MyPost, session

from .conftest import devto_comment, gh_comment


def run(s, web):
    return asyncio.run(run_checks(s, web.client()))


def test_first_check_records_existing_comments_without_reporting_them(s, web):
    web.devto_comments = [devto_comment("a1", "reader", "Nice post")]
    r = run(s, web)
    assert r["new_comments"] == 0 and r["baseline"] == 1
    assert "First check" in r["text"]
    with session() as db:
        assert db.exec(select(Comment)).one().state == "baseline"
        assert db.exec(select(MyPost)).one().title == "Wrapping strings in t()"


def test_later_checks_report_only_new_comments_by_other_people(s, web):
    web.devto_comments = [devto_comment("a1", "reader", "Nice post")]
    run(s, web)
    web.devto_comments = [devto_comment("a1", "reader", "Nice post", children=[
        devto_comment("a2", "lb0ss", "Thanks!"),                 # your own reply: never reported
        devto_comment("a3", "skeptic", "Does it do <b>plurals</b>?"),
    ])]
    web.github_comments = [gh_comment(7, "someone", "Undo wiped my edits"), gh_comment(8, "lb0ss", "Fixed in 0.1.3")]
    r = run(s, web)
    assert r["new_comments"] == 2
    assert "skeptic" in r["text"] and "Does it do plurals ?" in r["text"] and "someone" in r["text"]
    assert "**lb0ss**" not in r["text"] and "Thanks!" not in r["text"] and "Fixed in 0.1.3" not in r["text"]
    assert run(s, web)["new_comments"] == 0  # reported once only


def test_stats_show_the_change_since_the_last_check(s, web):
    run(s, web)
    web.downloads = 870
    text = run(s, web)["text"]
    assert "| openvsx | downloads | 870 | +13 |" in text


def test_a_failing_source_doesnt_stop_the_others(s, web):
    web.handler_orig = web.handler
    def broken(request):
        return __import__("httpx").Response(500) if "open-vsx.org" in str(request.url) else web.handler_orig(request)
    web.handler = broken
    r = run(s, web)
    assert r["failed"] == ["stats:openvsx"]
    assert "| marketplace | installs | 1 |" in r["text"] and "## Problems" in r["text"]


def test_quiet_days_save_the_digest_but_send_nothing(s, web):
    run(s, web)
    r = run(s, web)
    assert r["notified"] == [] and r["digest"].endswith(".md")


def test_first_comment_after_a_first_check_with_none_is_reported(s, web):
    run(s, web)  # first check: nothing to record
    web.devto_comments = [devto_comment("b1", "newcomer", "First!")]
    assert run(s, web)["new_comments"] == 1


def test_pushover_gets_a_short_phone_digest(s, web, monkeypatch):
    from dataclasses import replace

    from radar import notify

    sent = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, timeout, data: sent.append(data) or type("R", (), {"raise_for_status": lambda self: None})())
    s2 = replace(s, pushover_token="tok", pushover_user="usr")
    asyncio.run(run_checks(s2, web.client()))
    web.downloads = 870
    web.devto_comments = [devto_comment("z1", "reader", "Does it handle <b>plurals</b> in Polish?")]
    r = asyncio.run(run_checks(s2, web.client()))
    assert "pushed" in r["notified"]
    msg = sent[-1]
    assert msg["title"] == "Radar: 1 new comment, stats changed" and msg["html"] == 1
    assert "<b>reader</b> on Wrapping strings in t(): Does it handle plurals in Polish?" in msg["message"]
    assert "Open VSX downloads 870 (+13)" in msg["message"] and "|" not in msg["message"]  # no Markdown table
    assert msg["url"] == "https://dev.to/reader/comment/z1" and len(msg["message"]) <= 1024
