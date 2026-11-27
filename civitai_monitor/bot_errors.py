"""Error classification, log tail inspection, and formatting helpers for civitai-bot."""

from __future__ import annotations

import os
import re
from http import HTTPStatus
from pathlib import Path

from telegram.error import NetworkError, RetryAfter, TimedOut

_TRANSIENT_TG_ERRORS: tuple[type[BaseException], ...] = (
    NetworkError,
    TimedOut,
    RetryAfter,
)

_CRON_ALERT_LABELS = {"scan": "定时扫描", "reconciliation": "每日对账"}

_SCAN_ERROR_MARKERS = (
    "Page query failed",
    "Fatal page fetch failure",
    "fetch_page failed",
)
_SCAN_ERROR_FALLBACK_MARKERS = (
    "Service Unavailable",
    "Server Error",
    "Too Many Requests",
    "Rate Limited",
    "timed out",
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectionError",
    "Connection refused",
    "Max retries exceeded",
    "Failed to establish",
)
_BARE_EXIT_NOISE = (
    "Scheduled scan failed (exit",
    "Daily reconciliation finished with code",
    "monitor.py 非零退出",
    "reconcile 非零退出",
)
_HTTP_STATUS_CODES = r"401|403|404|429|500|502|503|504"
_HTTP_ERROR_PHRASE = (
    r"Server Error|Client Error|Bad Gateway|Gateway Time-?out|"
    r"Service Unavailable|Rate Limited|Too Many Requests"
)
_HTTP_ADJACENT_RE = re.compile(
    rf"(?:\b(?P<pre>{_HTTP_STATUS_CODES})\b\D{{0,40}}(?:{_HTTP_ERROR_PHRASE})"
    rf"|(?:{_HTTP_ERROR_PHRASE})\D{{0,40}}\b(?P<post>{_HTTP_STATUS_CODES})\b)",
    re.IGNORECASE,
)
_HTTP_EXPLICIT_RE = re.compile(
    rf"(?:HTTP(?:/\d+(?:\.\d+)?)?\s+\b(?P<http>{_HTTP_STATUS_CODES})\b"
    rf"|\bstatus\s*[=:]\s*(?P<st>{_HTTP_STATUS_CODES})\b"
    rf"|\b(?P<aio>{_HTTP_STATUS_CODES})\s*,\s*"
    rf"(?:message\s*=.*?,\s*)?url\s*=)",
    re.IGNORECASE,
)
_TIMEOUT_MARKERS = ("timed out", "timeout", "readtimeout", "connecttimeout")
_CONN_MARKERS = (
    "connectionerror",
    "connection refused",
    "connection reset",
    "max retries exceeded",
    "failed to establish",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
)

_RECON_START_MARKER = "Daily deep reconciliation starting"
_RECON_END_MARKERS = (
    "Daily reconciliation finished with code",
    "Daily reconciliation error:",
    "Daily deep reconciliation completed successfully",
)


def _is_transient_telegram_error(exc: BaseException | None) -> bool:
    """Return True for recoverable Telegram/httpx transport glitches."""
    if exc is None:
        return False
    if isinstance(exc, _TRANSIENT_TG_ERRORS):
        return True
    cause = getattr(exc, "__cause__", None)
    return isinstance(cause, _TRANSIENT_TG_ERRORS)


def _http_error_status(text: str) -> int | None:
    """HTTP status only when the number is adjacent to error evidence."""
    if not text:
        return None
    m = _HTTP_ADJACENT_RE.search(text)
    if m:
        return int(m.group("pre") or m.group("post"))
    m = _HTTP_EXPLICIT_RE.search(text)
    if m:
        for key in ("http", "st", "aio"):
            val = m.group(key)
            if val:
                return int(val)
    return None


def _looks_like_bare_exit(text: str) -> bool:
    """True when the only useful token is a subprocess exit code."""
    if not re.search(r"\bexit\s+\d+\b", text) and "非零退出" not in text:
        return False
    low = text.lower()
    return not (
        _http_error_status(text) is not None
        or any(m in low for m in _TIMEOUT_MARKERS)
        or any(m in low for m in _CONN_MARKERS)
    )


def _humanize_scan_error(reason: str) -> str:
    """Turn a raw log line into a short title fragment (never a bare exit code)."""
    text = (reason or "").strip()
    if not text:
        return ""
    code = _http_error_status(text)
    if code is not None:
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = ""
        if phrase:
            return f"Civitai API {code} {phrase}"
        return f"Civitai API {code}"
    low = text.lower()
    if any(marker in low for marker in _TIMEOUT_MARKERS):
        return "连接超时"
    if any(marker in low for marker in _CONN_MARKERS):
        return "连接错误"
    if _looks_like_bare_exit(text):
        return ""
    cleaned = text
    for sep in ("civitai-monitor: ", "civitai-bot: "):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[-1]
    cleaned = cleaned.strip()
    return cleaned[:160]


def _log_tail_candidates(log_path: str | None = None):
    """Yield decoded log-tail lines from the first readable candidate files."""
    candidates: list[Path] = []
    if log_path:
        candidates.append(Path(log_path))
    else:
        try:
            target = os.readlink("/proc/self/fd/1")
            if target.startswith("/"):
                candidates.append(Path(target))
        except OSError:
            pass
        candidates.append(Path("/var/log/civitai-bot.log"))
    for cand in candidates:
        try:
            if not cand.exists() or cand.stat().st_size == 0:
                continue
            with cand.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace")
            yield tail.splitlines()
        except OSError:
            continue


def _strip_log_prefix(reason: str) -> str:
    for sep in ("civitai-monitor: ", "civitai-bot: "):
        if sep in reason:
            reason = reason.split(sep, 1)[-1]
    return reason.strip()[:200]


def _pick_error_line(lines: list[str]) -> str:
    """Most informative page-fetch / transport error from a log-line window."""
    hits = [line for line in lines if any(m in line for m in _SCAN_ERROR_MARKERS)]
    if not hits:
        hits = [
            line for line in lines
            if any(m in line for m in _SCAN_ERROR_FALLBACK_MARKERS)
            and not any(n in line for n in _BARE_EXIT_NOISE)
        ]
    if not hits:
        hits = [
            line for line in lines
            if (
                "[ERROR]" in line
                or "[WARNING]" in line
                or " Error" in line
                or line.endswith("Error")
            )
            and not any(n in line for n in _BARE_EXIT_NOISE)
            and "Scheduled scan starting" not in line
        ]
    if not hits:
        return ""
    return _strip_log_prefix(hits[-1])


def _recon_window_lines(lines: list[str]) -> list[str]:
    """Latest reconcile-subprocess slice; never a preceding/following scan run."""
    start_idx = None
    for i, line in enumerate(lines):
        if _RECON_START_MARKER in line:
            start_idx = i
    if start_idx is None:
        return [ln for ln in lines if "[RECONCILE]" in ln]
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if any(m in lines[i] for m in _RECON_END_MARKERS):
            end_idx = i + 1
            break
    return lines[start_idx:end_idx]


def _last_scan_error_line(log_path: str | None = None) -> str:
    """Best-effort: most recent monitor page-fetch / transport error from the log."""
    for lines in _log_tail_candidates(log_path):
        reason = _pick_error_line(lines)
        if reason:
            return reason
    return ""


def _last_recon_error_line(log_path: str | None = None) -> str:
    """Most recent error from the latest [RECONCILE] / recon-run log window."""
    for lines in _log_tail_candidates(log_path):
        reason = _pick_error_line(_recon_window_lines(lines))
        if reason:
            return reason
    return ""


def _cron_failure_headline(
    job: str,
    detail: str = "",
    *,
    log_path: str | None = None,
    scan_err_fn=None,
    recon_err_fn=None,
) -> str:
    """Title fragment for a failing cron job; recon never borrows scan logs."""
    if job == "reconciliation":
        raw = (recon_err_fn or _last_recon_error_line)(log_path)
        return (
            _humanize_scan_error(raw)
            or _humanize_scan_error(detail)
            or "未能提取失败原因"
        )
    raw = (scan_err_fn or _last_scan_error_line)(log_path)
    return (
        _humanize_scan_error(raw)
        or _humanize_scan_error(detail)
        or "未能从日志提取失败原因"
    )


def _human_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{int(bytes_)}{unit}" if unit == "B" else f"{bytes_:.1f}{unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f}TB"


def _classify_failure(returncode: int, stderr: str) -> str:
    """Classify a monitor.py failure into a user-facing reason string."""
    tail = (stderr or "")[-2000:]
    if returncode == 75:
        return "另一个扫描进程正在运行，请稍后再试"
    if "Service Unavailable" in tail or "503 Server Error" in tail:
        return "Civitai API 暂时不可用（503），稍后可重试"
    if "Rate Limited" in tail:
        return "触发 Civitai 限流，请稍后再试"
    if "Timeout" in tail or "timeout" in tail or "timed out" in tail:
        return "网络请求超时，稍后可重试"
    return "未知错误，详见服务日志"


def _summarise_log(stderr: str) -> str:
    """Extract the most interesting lines from the monitor.py stderr output."""
    lines = stderr.strip().split("\n")
    relevant = []
    for line in lines:
        lower = line.lower()
        if any(
            kw in lower
            for kw in (
                "new images", "no new", "cleaned", "pushed", "new artwork",
                "new:", "complete", "total new", "fetched", "saved",
                "another monitor",
            )
        ):
            if " [" in line:
                line = line.split("] ", 1)[-1] if "] " in line else line
            relevant.append(line)

    if not relevant:
        return "No new images found."

    seen = set()
    unique = []
    for line in relevant:
        if line not in seen:
            seen.add(line)
            unique.append(line)

    safe_lines = [line.replace(chr(96), chr(92) + chr(96)) for line in unique[-15:]]
    return chr(96) * 3 + chr(10) + chr(10).join(safe_lines) + chr(10) + chr(96) * 3
