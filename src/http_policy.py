"""Conservative request spacing, bounded retries, Retry-After and daily quota handling.

Installed only in provider/metadata processes, never in the working yt-dlp downloader.
"""
from __future__ import annotations

import os
import random
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from store import Store


class ApiDeferred(requests.RequestException):
    def __init__(self, message: str, *, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


def retry_delay(value: str | None, now=None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return 0


class HttpPolicy:
    def __init__(self, store: Store, report=None):
        self.store = store
        self.report = report or (lambda level, text: store.event(level, text))
        self.failures = []
        self.deferred_failures = []
        self.deferred_until = 0.0

    def _defer(self, message: str, retry_at: float | None = None):
        retry_at = float(retry_at or (time.time() + 60))
        self.deferred_failures.append(message)
        self.deferred_until = max(self.deferred_until, retry_at)
        return ApiDeferred(message, retry_at=retry_at)

    def _budget(self):
        pacific = datetime.now(ZoneInfo("America/Los_Angeles"))
        day = pacific.date().isoformat()
        limit = int(os.getenv("YT_DAILY_BUDGET", "9000"))
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT used FROM quota WHERE day=?", (day,)).fetchone()
            if row and row[0] >= limit:
                reset = (pacific + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                raise self._defer(
                    f"Local daily API budget ({limit} units) exhausted; "
                    "retry after Pacific midnight",
                    reset.timestamp(),
                )
            con.execute("INSERT INTO quota VALUES (?,1) ON CONFLICT(day) DO UPDATE SET used=used+1", (day,))

    def send(self, original, session, request, **kwargs):
        host = urlsplit(request.url).hostname or "unknown"
        interval = 0.4 if host == "api.acoustid.org" else 1.1
        kwargs.setdefault("timeout", (10, 30))
        # Some plugin adapters compress/mutate the prepared request in send().
        # Restore the original payload for each retry rather than compressing twice.
        body, headers = request.body, request.headers.copy()
        for attempt in range(3):
            wait = self.store.reserve_api(host, interval)
            # Normal API spacing (1.1s/0.4s) can wait inline. Longer shared
            # cooldowns should release the single ingestion worker to other jobs.
            if wait > 5:
                message = f"{host}: waiting for provider cooldown ({wait:.0f}s remaining); job deferred"
                raise self._defer(message, time.time() + wait)
            if wait:
                time.sleep(wait)
            if host == "www.googleapis.com":
                self._budget()
            # Each network attempt must pass through this policy, not a hidden adapter retry.
            from urllib3.util.retry import Retry
            session.get_adapter(request.url).max_retries = Retry(total=0)
            request.body = body
            request.headers = headers.copy()
            try:
                response = original(session, request, **kwargs)
            except requests.RequestException as exc:
                self.report("ERROR", f"{host}: request failed ({type(exc).__name__}), attempt {attempt + 1}/3")
                if attempt == 2:
                    message = f"{host}: connection failed after three attempts"
                    raise self._defer(message, time.time() + 60) from exc
                time.sleep(2 ** (attempt + 1) + random.random())
                continue
            status = response.status_code
            reason = ""
            if host == "api.acoustid.org" and status == 200:
                try:
                    data = response.json()
                    if data.get("status") == "error":
                        reason = data.get("error", {}).get("message", "AcoustID lookup failed")
                        self.failures.append(f"{host}: {reason}")
                        self.report("ERROR", f"{host}: {reason}")
                except (ValueError, AttributeError):
                    pass
            if host == "www.googleapis.com" and status >= 400:
                try:
                    errors = response.json().get("error", {}).get("errors", [])
                    reason = errors[0].get("reason", "") if errors else ""
                except (ValueError, AttributeError):
                    pass
            if reason in {"quotaExceeded", "dailyLimitExceeded"}:
                now = datetime.now(ZoneInfo("America/Los_Angeles"))
                reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                self.store.defer_api(host, reset.timestamp())
                response.close()
                raise self._defer(
                    "Provider daily quota exhausted; paused until the next Pacific midnight",
                    reset.timestamp(),
                )
            transient = status in {429, 500, 502, 503, 504} or reason in {"rateLimitExceeded", "userRateLimitExceeded"}
            if not transient:
                if status >= 400 and status != 404:
                    self.report("ERROR", f"{host}: HTTP {status} {reason}".rstrip())
                    self.failures.append(f"{host}: HTTP {status}")
                return response
            delay = max(retry_delay(response.headers.get("Retry-After")), 2 ** (attempt + 1) + random.random())
            self.store.defer_api(host, time.time() + delay)
            self.report("ERROR", f"{host}: HTTP {status}; attempt {attempt + 1}/3, cooldown {delay:.1f}s")
            response.close()
            if attempt == 2 or delay > 60:
                message = f"{host}: HTTP {status}; automatic attempts exhausted or long provider cooldown"
                retry_at = time.time() + max(delay, 60)
                self.store.defer_api(host, retry_at)
                raise self._defer(message, retry_at)
        raise AssertionError("Unreachable")

    def install(self):
        # Scoped to the short-lived beets process. All its requests-based plugins share
        # the persistent host clock; internal adapter retries are disabled below.
        original = requests.sessions.Session.send
        policy = self

        def guarded(session, request, **kwargs):
            return policy.send(original, session, request, **kwargs)

        requests.sessions.Session.send = guarded

    def get_json(self, endpoint, *, params, headers):
        session = requests.Session()
        request = requests.Request("GET", endpoint, params=params, headers=headers)
        prepared = session.prepare_request(request)
        try:
            response = self.send(requests.sessions.Session.send, session, prepared, timeout=(10, 30))
            if not response.ok:
                try:
                    detail = response.json().get("error", {}).get("message", "Request rejected")
                except (ValueError, AttributeError):
                    detail = "Request rejected"
                raise ApiDeferred(f"Source API HTTP {response.status_code}: {detail}")
            return response.json()
        finally:
            session.close()
