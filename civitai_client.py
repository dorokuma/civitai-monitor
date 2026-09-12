"""Civitai HTTP session, retries, and page fetch."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config_io import HttpConfig

log = logging.getLogger("civitai-monitor")

# Page-size safety: Civitai's /images endpoint silently caps or rejects
# out-of-range `limit` values, so we clamp before sending. 200 is the highest
# value the API reliably accepts.
MAX_API_PAGE_LIMIT = 200
MIN_API_PAGE_LIMIT = 1

HTTP_REQUEST_TIMEOUT = 30

# Default 429 wait when the Retry-After header is missing or unusable.
DEFAULT_RETRY_AFTER_SECONDS = 30
# Upper bound (seconds) for a single 429 wait.  tenacity sleeps for exactly the
# value returned by _rate_limit_wait, so a huge/bogus Retry-After (or an
# HTTP-date far in the future) would otherwise stall the monitor for hours
# while it holds .monitor.lock.  Anything above the cap waits the cap instead.
MAX_RATE_LIMIT_WAIT_SECONDS = 120

# ---------------------------------------------------------------------------
# Global HTTP session (enforces Referer + User-Agent on every request)
# ---------------------------------------------------------------------------

session = requests.Session()
session.timeout = 30
session.verify = True


def init_session(http_cfg: HttpConfig) -> None:
    """Apply the user-configured headers to the global session."""
    session.headers.update({
        "User-Agent": http_cfg.user_agent,
        "Referer": http_cfg.referer,
        "Accept": "*/*",
    })
    if http_cfg.extra_headers:
        session.headers.update(http_cfg.extra_headers)

    # Load Civitai cookies (needed for video CDN and NSFW API auth)
    if http_cfg.cookies_file:
        cookies_path = Path(http_cfg.cookies_file)
        if cookies_path.exists():
            import http.cookiejar
            cj = http.cookiejar.MozillaCookieJar(str(cookies_path))
            cj.load(ignore_expires=True, ignore_discard=True)
            session.cookies.update(cj)
            log.info("Loaded %d cookies from %s", len(cj), cookies_path)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _parse_retry_after(raw: str | None) -> int:
    """Best-effort parse of a Retry-After header value (RFC 7231 section 7.1.3).

    The header may be either delay-seconds ("30") or an HTTP-date
    ("Wed, 21 Oct 2015 07:28:00 GMT") — both are legal, but plain
    ``int("Wed, ...")`` raises ValueError mid-construction and nobody catches
    it, crashing the whole scan round.  Unparseable values and values <= 0
    (e.g. an HTTP-date already in the past) fall back to
    DEFAULT_RETRY_AFTER_SECONDS instead.
    """
    if raw is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    text = str(raw).strip()
    if not text:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        seconds = int(text)
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return DEFAULT_RETRY_AFTER_SECONDS
        if dt is None:
            return DEFAULT_RETRY_AFTER_SECONDS
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = int((dt - datetime.now(timezone.utc)).total_seconds())
    return seconds if seconds > 0 else DEFAULT_RETRY_AFTER_SECONDS


class RateLimitError(requests.RequestException):
    """Raised when the API returns 429 Too Many Requests."""
    def __init__(self, response: requests.Response) -> None:
        self.retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        super().__init__(f"429 Rate Limited, retry after {self.retry_after}s")


class FetchPageError(Exception):
    """Hard failure fetching a Civitai gallery page (network / HTTP error).

    Distinct from a true empty page, which still returns ``([], "")``.
    Callers that must not silently skip failures should let this propagate.
    """


# ---------------------------------------------------------------------------
# Tenacity-retried GET
# ---------------------------------------------------------------------------


def _rate_limit_wait(retry_state) -> float:
    """Respect Retry-After header when rate-limited, fall back to exponential backoff.

    The Retry-After wait is capped at MAX_RATE_LIMIT_WAIT_SECONDS: tenacity
    sleeps for exactly the returned value, so a bogus/huge upstream value must
    never hang the monitor (it holds .monitor.lock while scanning).
    """
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError):
        wait = exc.retry_after + random.uniform(0, 2)
        return min(wait, MAX_RATE_LIMIT_WAIT_SECONDS)
    return wait_exponential(multiplier=1, min=2, max=30)(retry_state)


def _should_retry_api(exc: BaseException) -> bool:
    """Retry transient / 5xx / network errors; never retry 4xx client errors."""
    if isinstance(exc, RateLimitError):
        return True  # 429 → Retry-After honored by _rate_limit_wait
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        return resp is not None and 500 <= resp.status_code < 600  # 4xx → client error, do not retry
    return isinstance(exc, requests.RequestException)


@retry(
    stop=stop_after_attempt(5),
    wait=_rate_limit_wait,
    retry=retry_if_exception(_should_retry_api),
    reraise=True,
)
def safe_get(url: str, **kwargs) -> requests.Response:
    """HTTP GET with tenacity retry + exponential backoff.

    * 5xx and network errors are retried with exponential backoff (min 2s, max 30s).
    * 429 honors the upstream ``Retry-After`` header (plus a little jitter).
    * 4xx are never retried — they are client errors that won't succeed on retry.
    """
    resp = session.get(url, timeout=kwargs.pop("timeout", 30), **kwargs)
    if resp.status_code == 429:
        raise RateLimitError(resp)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Civitai API
# ---------------------------------------------------------------------------


def fetch_page(
    username: str,
    base_url: str = "https://civitai.com/api/v1",
    limit: int = 100,
    cursor: str = "",
    nsfw: bool | None = None,
    sort: str | None = "Newest",
) -> tuple[list[dict[str, Any]], str]:
    """Fetch one page of images for a user.

    Uses cursor-based pagination (Civitai API page parameter is broken).
    Returns (items, next_cursor); next_cursor is empty when there are no more
    pages. An empty page may still return a non-empty next_cursor from the API
    metadata, so callers must keep walking on ``([], next_cursor)`` rather than
    treating an empty page as the end of the gallery.

    Tracks:
      * nsfw=False -> SFW track (civitai.com/api/v1/images?nsfw=false)
      * nsfw=True  -> NSFW track (civitai.red/api/v1/images?nsfw=true)
      * nsfw=None  -> ALL track  (civitai.red/api/v1/images?browsingLevel=31)

    Raises:
        FetchPageError: on network / HTTP hard failures after retries.
    """
    # Clamp `limit` to the Civitai-allowed range.
    limit = max(MIN_API_PAGE_LIMIT, min(int(limit), MAX_API_PAGE_LIMIT))
    params: dict[str, Any] = {
        "username": username,
        "limit": limit,
    }
    if cursor:
        params["cursor"] = cursor
    else:
        params["page"] = 1
    if sort is not None:
        params["sort"] = sort

    if nsfw is True:
        params["nsfw"] = "true"
        actual_base = "https://civitai.red/api/v1"
    elif nsfw is False:
        params["nsfw"] = "false"
        actual_base = base_url
    else:
        # ALL track: browsingLevel=31 covers all content levels on civitai.red
        params["browsingLevel"] = 31
        actual_base = "https://civitai.red/api/v1"

    track_label = "NSFW" if nsfw is True else ("SFW" if nsfw is False else "ALL")
    try:
        resp = safe_get(f"{actual_base}/images", params=params)
        resp.raise_for_status()
        data = resp.json()
        # A non-object payload (list/str/number) means the API contract broke:
        # an AttributeError here would escape the FetchPageError wrapper, the
        # scan would exit 0 and the failed page would go unnoticed.
        if not isinstance(data, dict):
            raise FetchPageError(
                f"fetch_page failed for @{username} (track={track_label}): "
                f"unexpected JSON payload type {type(data).__name__}, expected object"
            )
        items = data.get("items", [])
        # A non-list `items` (str/int/None) means the API contract broke: it
        # used to crash downstream (AttributeError/TypeError) outside the
        # FetchPageError classification, silently skipping the creator while
        # the scan still exited 0.
        if not isinstance(items, list):
            raise FetchPageError(
                f"fetch_page failed for @{username} (track={track_label}): "
                f"unexpected items type {type(items).__name__}, expected list"
            )
        # Lenient element guard, mirroring the metadata fallback below: drop
        # non-object entries instead of crashing on a downstream .get(). A
        # whole-payload contract break is already handled by the list-type
        # check above; individual junk entries only shrink the page, so filter
        # them and keep going (same lenient philosophy as the metadata guard).
        items = [i for i in items if isinstance(i, dict)]
        meta = data.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        next_cursor = meta.get("nextCursor", "")
        return items, next_cursor
    except requests.RequestException as e:
        log.warning("Page query failed (track=%s): %s", track_label, e)
        raise FetchPageError(f"fetch_page failed for @{username} (track={track_label}): {e}") from e
