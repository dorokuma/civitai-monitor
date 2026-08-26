"""Civitai HTTP session, retries, and page fetch."""

from __future__ import annotations

import logging
import random
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


class RateLimitError(requests.RequestException):
    """Raised when the API returns 429 Too Many Requests."""
    def __init__(self, response: requests.Response) -> None:
        self.retry_after = int(response.headers.get("Retry-After", 30))
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
    """Respect Retry-After header when rate-limited, fall back to exponential backoff."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError):
        return exc.retry_after + random.uniform(0, 2)
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
    if nsfw is not None:
        params["nsfw"] = "true" if nsfw else "false"
    # NSFW content requires civitai.red + browsingLevel + cookies
    if nsfw is True:
        actual_base = "https://civitai.red/api/v1"
    else:
        actual_base = base_url

    try:
        resp = safe_get(f"{actual_base}/images", params=params)
        resp.raise_for_status()
        items = resp.json().get("items", [])

        # Fallback: if Newest sort returns empty but user exists,
        # retry with default sort (Civitai API bug workaround).
        if not items and sort == "Newest":
            log.warning(
                "%s: sort=Newest returned 0 items (nsfw=%s), retrying with default sort",
                username, nsfw,
            )
            fallback_params = dict(params)
            fallback_params.pop("sort", None)
            fallback_resp = safe_get(f"{actual_base}/images", params=fallback_params)
            fallback_resp.raise_for_status()
            items = fallback_resp.json().get("items", [])
            resp = fallback_resp

        next_cursor = resp.json().get("metadata", {}).get("nextCursor", "")
        return items, next_cursor
    except requests.RequestException as e:
        log.warning("Page query failed (nsfw=%s): %s", nsfw, e)
        raise FetchPageError(f"fetch_page failed for @{username} (nsfw={nsfw}): {e}") from e
