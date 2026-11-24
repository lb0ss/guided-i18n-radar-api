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
    assert msg["title"] == "Radar: 1 new dev.to comment, stats changed" and msg["html"] == 1 and msg["url_title"] == "Open the comment"
    assert "<b>reader</b> (dev.to comment) on Wrapping strings in t(): Does it handle plurals in Polish?" in msg["message"]
    assert "Open VSX downloads 870 (+13)" in msg["message"] and "|" not in msg["message"]  # no Markdown table
    assert msg["url"] == "https://dev.to/reader/comment/z1" and len(msg["message"]) <= 1024


def _flaky(web, match, drops):
    """Drops the connection on the first `drops` requests whose URL contains `match`; counts them."""
    import httpx
    orig, calls = web.handler, []
    def handler(request):
        if match in str(request.url):
            calls.append(1)
            if len(calls) <= drops:
                raise httpx.ReadError("connection dropped", request=request)
        return orig(request)
    web.handler = handler
    return calls


def test_a_dropped_connection_is_retried(s, web, monkeypatch):
    monkeypatch.setattr("radar.checks.RETRY_DELAYS", (0, 0))
    run(s, web)  # first check: baseline
    web.github_comments = [gh_comment(5, "someone", "Does it handle plurals?")]
    calls = _flaky(web, "issues/comments", drops=1)  # e.g. the Mac fell asleep mid-request
    r = run(s, web)
    assert r["failed"] == [] and r["new_comments"] == 1 and len(calls) == 2


def test_a_connection_that_keeps_dropping_fails_after_three_tries(s, web, monkeypatch):
    monkeypatch.setattr("radar.checks.RETRY_DELAYS", (0, 0))
    calls = _flaky(web, "issues/comments", drops=99)
    r = run(s, web)
    assert r["failed"] == ["watch:github"] and len(calls) == 3 and "(after 3 tries)" in r["text"]


def test_an_error_response_is_not_retried(s, web, monkeypatch):
    monkeypatch.setattr("radar.checks.RETRY_DELAYS", (0, 0))
    import httpx
    orig, calls = web.handler, []
    def handler(request):
        if "open-vsx.org" in str(request.url):
            calls.append(1)
            return httpx.Response(401)
        return orig(request)
    web.handler = handler
    r = run(s, web)
    assert r["failed"] == ["stats:openvsx"] and len(calls) == 1


def test_issues_and_comments_are_named_separately(s, web):
    run(s, web)  # baseline
    web.github_issues = [{"id": 71, "number": 8, "title": "Vue support?", "html_url": "https://github.com/lb0ss/guided-i18n/issues/8",
                          "user": {"login": "asker"}, "body": "Any plans for Vue?", "created_at": "2026-11-21T09:00:00Z"}]
    web.github_comments = [gh_comment(5, "someone", "Same here"), gh_comment(6, "other", "+1")]
    web.devto_comments = [devto_comment("c9", "reader", "Nice write-up")]
    r = run(s, web)
    summary = "1 new GitHub issue, 2 new GitHub issue comments, 1 new dev.to comment"
    assert r["new_summary"] == summary and f"## {summary}" in r["text"]
    assert "- GitHub issue · **asker** on #8 Vue support?" in r["text"]


def test_a_quiet_day_with_a_problem_says_so_in_the_title(s, web, monkeypatch):
    from dataclasses import replace

    from radar import notify

    sent = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, timeout, data: sent.append(data) or type("R", (), {"raise_for_status": lambda self: None})())
    monkeypatch.setattr("radar.checks.RETRY_DELAYS", ())
    s2 = replace(s, pushover_token="tok", pushover_user="usr")
    asyncio.run(run_checks(s2, web.client()))
    web.downloads = 900
    _flaky(web, "issues/comments", drops=99)
    asyncio.run(run_checks(s2, web.client()))
    assert sent[-1]["title"] == "Radar: stats changed, 1 problem"  # no "0 new comments"
