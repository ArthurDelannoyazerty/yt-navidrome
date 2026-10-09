import json

import pytest
import requests

from http_policy import ApiDeferred, HttpPolicy, retry_delay


@pytest.fixture
def clock(monkeypatch):
    import http_policy
    current = [1800000000.0]
    monkeypatch.setattr(http_policy.time, "time", lambda: current[0])
    monkeypatch.setattr(
        http_policy.time,
        "sleep",
        lambda delay: current.__setitem__(0, current[0] + delay),
    )
    monkeypatch.setattr(http_policy.random, "random", lambda: 0)
    return current


def response(status, body=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body or {}).encode()
    result._content_consumed = True
    if retry_after:
        result.headers["Retry-After"] = str(retry_after)
    return result


def test_bounded_retry_uses_shared_spacing(db, clock):
    policy = HttpPolicy(db)
    session = requests.Session()
    request = requests.Request(
        "GET", "https://musicbrainz.org/ws/2/recording"
    ).prepare()
    values = iter([response(503), response(503), response(200)])
    calls = []

    def send(session, prepared, **kwargs):
        calls.append(clock[0])
        return next(values)

    assert policy.send(send, session, request).status_code == 200
    assert len(calls) == 3
    assert all(right - left >= 1.1 for left, right in zip(calls, calls[1:]))
    assert session.get_adapter(request.url).max_retries.total == 0


def test_long_retry_after_pauses_instead_of_hammering(db, clock):
    policy = HttpPolicy(db)
    calls = []
    request = requests.Request("GET", "https://lrclib.net/api/get").prepare()

    def send(*args, **kwargs):
        calls.append(clock[0])
        return response(429, retry_after=3600)

    with pytest.raises(ApiDeferred):
        policy.send(send, requests.Session(), request)
    with pytest.raises(ApiDeferred, match="cooldown"):
        policy.send(send, requests.Session(), request)
    assert len(calls) == 1


def test_quota_error_has_no_useless_retries(db, clock):
    calls = []
    request = requests.Request(
        "GET", "https://www.googleapis.com/youtube/v3/playlistItems"
    ).prepare()

    def send(*args, **kwargs):
        calls.append(1)
        return response(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})

    with pytest.raises(ApiDeferred, match="quota"):
        HttpPolicy(db).send(send, requests.Session(), request)
    assert len(calls) == 1
    assert db.one("SELECT next_at FROM api_clock")["next_at"] > 0


def test_application_daily_budget(db, monkeypatch):
    monkeypatch.setenv("YT_DAILY_BUDGET", "2")
    policy = HttpPolicy(db)
    policy._budget()
    policy._budget()
    with pytest.raises(ApiDeferred, match="budget"):
        policy._budget()
    assert db.one("SELECT used FROM quota")["used"] == 2


def test_network_retry_exhaustion(db, clock):
    calls = []
    request = requests.Request("GET", "https://musicbrainz.org/ws/2").prepare()

    def send(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("offline")

    policy = HttpPolicy(db)
    with pytest.raises(ApiDeferred) as exc:
        policy.send(send, requests.Session(), request)
    assert len(calls) == 3
    assert policy.deferred_failures
    assert exc.value.retry_at > clock[0]


def test_retry_after_http_date():
    assert retry_delay("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412420) == 60
    assert retry_delay("123") == 123
    assert retry_delay("invalid") == 0


def test_mutating_adapter_does_not_double_compress_retries(db, clock):
    policy = HttpPolicy(db)
    seen = []
    request = requests.Request(
        "POST", "https://api.acoustid.org/v2/lookup", data="original"
    ).prepare()

    def send(session, prepared, **kwargs):
        seen.append(prepared.body)
        prepared.body = "compressed:" + prepared.body
        prepared.headers["Content-Encoding"] = "gzip"
        return response(503) if len(seen) == 1 else response(200, {"status": "ok"})

    policy.send(send, requests.Session(), request)
    assert seen == ["original", "original"]


def test_acoustid_application_error_is_not_a_successful_no_match(db, clock):
    policy = HttpPolicy(db)
    request = requests.Request(
        "POST", "https://api.acoustid.org/v2/lookup"
    ).prepare()
    policy.send(
        lambda *args, **kwargs: response(
            200, {"status": "error", "error": {"message": "Invalid client"}}
        ),
        requests.Session(),
        request,
    )
    assert policy.failures == ["api.acoustid.org: Invalid client"]
