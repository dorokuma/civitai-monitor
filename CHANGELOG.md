# Changelog

This file follows the [Keep a Changelog](https://keepachangelog.com/) format, and version numbers follow [SemVer](https://semver.org/).

## [1.1.5] - 2026-08-26

### Fixed
- `fetch_page` keeps the API `nextCursor` even on empty pages, and `run_full` continues walking on `([], next_cursor)` instead of dropping the cursor and missing later works.
- `run_full` stops after 3 consecutive empty pages, so pathological empty runs cannot loop forever.

### Added
- `run_full` detects API cursor loops via a `visited_cursors` set and breaks out of content-bearing pagination cycles.
- `run_full` hard per-track page cap (`MAX_FULL_PAGES_PER_TRACK = 500`) so an endless track cannot hold the process lock.
- Regression tests covering empty-page cursor preservation, consecutive-empty guard, cursor-loop detection, and the page cap.

## [1.1.4] - 2026-08-26

### Fixed
- Incremental scan requests `sort=Newest` from Civitai so newly published works are found on the first page instead of being missed by the default ordering.

## [1.1.3] - 2026-08-08

### Added
- Module split (compat re-exports from `monitor.py` / bot imports):
  `config_io.py`, `civitai_client.py`, `state_store.py`, `telegram_media.py`, `bot_ui.py`.
- `FetchPageError`: network/HTTP hard failures from `fetch_page` raise instead of returning empty; `main()` exits 2.
- Telegram send path: limited 429 `Retry-After` retries + Markdown escape for dynamic usernames.
- Shared `paginated_user_keyboard` for remove/backfill UI.

### Fixed
- `write_config`: redacts `telegram.bot_token` (env-injected tokens never written back) + atomic `tmp`/`os.replace`.
- `load_seen_ids` / `load_pushed_ids`: corrupt JSON → empty set + warning.
- `download_video`: without Content-Length, stream-count bytes and abort past cap (default 1024 MB).
- `_clear_status(interrupted=True)` keeps the interrupted snapshot (no immediate unlink).
- Backfill timeout user text matches idle 1800s (30 min no output), not “2 hours”.
- `/mode` and `/nsfw` replies warn that settings are **global** (all subscribers).
- `/cleanup` reuses `monitor.cleanup_old_caches` (incl. `max_total_gb`).

### Changed
- Removed unused `pydantic-settings` from requirements.
- Docs: multi-user subscribe vs single channel push; seen vs pushed; env token dual-track.

## [1.1.2] - 2026-08-05

### Fixed
- monitor.py: 视频下载时 Content-Length 头无法解析为数字不再导致崩溃，按未知大小处理并继续下载。
- civitai-bot.py: `/status` 命令扫描文件元数据时捕获 JSONDecodeError（ValueError 子类），损坏的 JSON 缓存不再导致命令崩溃。
- backfill-memory-wrapper.py: 启用 backfill 进程的虚拟内存限制（RLIMIT_AS，软 1400MB / 硬 1500MB），并同步修正主服务内存注释（MemoryMax=4G）。

### Notes
- monitor.py: 补充注释说明 2048MB 的 sendVideo 阈值与本地 telegram-bot-api 服务器（127.0.0.1:8081，上限 2GB）匹配，此值应保持 2048 而非改为官方云 API 的 50MB。

## [1.1.1] - 2026-07-29

### Fixed
- Admin Bot no longer pages admins for transient Telegram transport errors (`NetworkError` / `httpx.ReadError` / `TimedOut` / `RetryAfter`) that PTB already retries during long-polling.
- Admin Bot now uses `telegram.api_base_url` (local Bot API Server at `http://127.0.0.1:8081`) for polling and replies — same endpoint monitor already used for media pushes. This avoids flaky direct connections to `api.telegram.org`.
- Increased HTTP/getUpdates timeouts for the PTB Application to reduce spurious read errors under load.

## [1.1.0] - 2026-07-27

### Added
- Telegram global error handler: catches unhandled exceptions and alerts the admin, with a 5-minute debounce per root cause.
- API 5xx / 429 exponential backoff retries, reducing missed pushes caused by rate limiting and transient failures.
- Automatic digestion of pending off-page entries to avoid omissions after page scrolling.
- Unified graceful shutdown of background tasks to prevent dangling tasks.
- Ops tooling: logrotate configuration and ownership self-heal script.

### Fixed
- `CLOSE-WAIT` connection pile-up caused by unclosed streaming download connections.
- `asyncio` task leak.
- Permanent stall of `pending_push` entries.
- Reduced time complexity of `cleanup_old_caches` from O(n^2).
- Cookie path hardcoding causing divergence (inconsistent paths across multiple instances/users).
- Dead-code cleanup in docstrings.

### Security
- `bot_token` removed from `config.yaml`; now injected uniformly via environment variable.

### Changed
- Log format now includes year and timezone information.
- Removed `_apply_memory_limit`; memory control moved to static systemd configuration.

### Tests
- Fixed 2 pre-existing `MagicMock` not `await`-able test failures; full suite 82 passed.

## [1.0.0] - 2026-06-07

First tagged release.

Between v1.0.0 and v1.1.0 there were 13 untagged commits, summarized below:
- Video processing: `feat` support sending >50MB videos after compression; `feat` switched to a local Bot API Server for uploading large videos; `fix` routed >50MB videos through `sendDocument` instead of compression; `chore` removed the unused compression function.
- Stability and de-duplication: `fix` prevented duplicate Telegram downloads and re-pushes; `fix` resolved hangs/crashes and optimized missed-push prevention logic, added channel push support.
- Rate limiting and config: `fix` added rate limiting, config externalization, timeout defaults, lock deps and service deps, type hints (including one duplicate-commit fix).
- File-lock robustness: `fix` added retries for `FileLock` timeouts; `fix` wrote the PID into the lock file and added `_is_scan_running` to support stale-lock detection.
- Security hardening and exceptions: `fix` security hardening (enum validation, log sanitization, lock error handling, env path fallback); `fix` memory optimization + security hardening + exception handling improvements.
- Memory management: `fix` removed the virtual memory limit, leaving physical memory control to systemd.
