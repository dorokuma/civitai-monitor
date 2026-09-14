# Changelog

This file follows the [Keep a Changelog](https://keepachangelog.com/) format, and version numbers follow [SemVer](https://semver.org/).

## [1.5.1] - 2026-09-14

### Changed
- **Cron failure alerts wait 24 hours without a success**: Telegram pages
  admins only after a `scan` / `reconciliation` job has gone 24 hours with
  no success (one alert per continuous failure episode, then one daily
  digest). A recovery notice is sent only if an alert was actually fired.
  Gate state is persisted in `cron_alert_state.json` so a process restart
  does not reset the 24h clock; a fresh process with no history uses the
  current time as the baseline to avoid a startup false alarm. Manual
  `/scan` still reports failures immediately.

### Fixed
- **Failure alert titles name the error**: HTTP status codes (502/503/429),
  timeouts, and connection errors go in the title (e.g.
  `❌ 定时扫描已连续 24 小时失败：Civitai API 503 Service Unavailable`).
  When the log has no structured hit, the most informative tail line is
  used; a bare exit code is no longer the only alert content.

## [1.5.0] - 2026-09-13

### Added
- **Weekly Telegram report** (Sundays 21:00 UTC to admin chats): scan success/failure/skip counts for the week, per-subscription push counts and last-push dates (subscriptions silent for 14+ days are flagged for manual review), and the last reconciliation date. Guards against the silent-failure mode where everything looks green while subscriptions quietly stop producing.
- **Push history**: every confirmed push records a timestamp (35-day rolling retention) powering the report; write failures never fail confirmed pushes.
- **CodeQL workflow** (python, SHA-pinned actions) alongside lint/test.

### Fixed
- **Backup archives were including everything**: the tar command's `--exclude` flags were positional arguments after the file list, so GNU tar ignored them - `downloads` (11 GB) was never excluded and the 90-day cleanup never ran. Excludes moved before operands, `.venv` excluded, and the archive shrank from 2.69 GiB to ~0.5 MB. Restore procedure documented in README (including venv recreation).

### Changed
- coverage baseline: monitor 75%, state_store 82%, civitai_client 83%, config_io 84%, telegram_media 78% (TOTAL 78%) with new tests for the push-history paths.

## [1.4.0] - 2026-09-13

### Fixed
- **Media-retry lifecycle actually works now**: pending entries carry a `media_failed` flag (backward-compatible with the old `(ts, retries)` on-disk format). A media upload that certainly failed is no longer swallowed by the 30-minute fresh-promotion branches (production logs showed 110 silent promotions, 0 real retries): on-page items with retries left are re-sent unconditionally, off-page items are kept pending until they reappear, and exhausted items are promoted with a warning.
- **adopt_stale_inflight no longer refreshes the pending timestamp** (merges with min(ts) and ORs media_failed), so a crash mid-retry cannot reset the confirmation window.
- **Downloads reject empty bodies** (0-byte "success" files are deleted and retried later) and the "Already exists" short-circuit re-downloads a zero-byte existing file instead of freezing the loss.
- **API guards completed**: `items` that is not a list raises `FetchPageError` (was a silently skipped creator with exit 0); non-dict items are filtered.
- **Cron/process hygiene**: /scan now registers its process so /stop can kill it; backfill lock files are no longer unlinked (removes the flock+unlink double-acquire window); the off-page promotion loop is per-item exception isolated; `reconciliation_status.json` and backfill lock files are gitignored.
- telegram_media docstrings now document the timeout -> full-resend policy (duplicates preferred over loss).

## [1.3.0] - 2026-09-12

### Fixed
- **Media can no longer be silently lost**: upload retries rewind file handles (a retried upload no longer sends 0 bytes), zero-byte files are rejected before sending, and when the media exists but its upload ultimately fails the item is parked as pending (retried next scan) instead of being marked pushed by the text fallback.
- **Message length budgets**: createdAt is capped and captions/messages are clipped to Telegram limits (1000 media / 4000 text), so a tampered `createdAt` cannot push media over the caption limit into the same silent-loss path.
- **API response guards**: non-dict JSON payloads raise `FetchPageError` (alerted scan failure) instead of a silently swallowed AttributeError; a tampered `meta` field is treated as missing instead of crashing the item every scan.
- **Cron robustness**: an invalid `reconciliation.time` falls back to 03:30 instead of killing the reconciliation loop; `load_config` survives `yaml.YAMLError` (falls back to the minimal config); `_load_interval` rejects non-dict/non-int payloads instead of killing the scan loop.
- **State hygiene**: `active_backfills.json` ownership corrected (service user can resume backfills again); `monitor_status.json` is written atomically; the monitor lock file is no longer unlinked (removes the classic flock+unlink double-acquire window); `interval.json` is no longer tracked in git.

### Security
- Weekly backup archives are now 0600/0700 (they contain the bot token and Civitai cookies; they were world-readable).

## [1.2.0] - 2026-09-12

### Fixed
- **Download path traversal**: API-provided item ids are coerced to `int`, file extensions are whitelisted per media type, and the final download path is resolve-checked to stay inside the output directory; a tampered API response can no longer overwrite arbitrary files.
- **Per-item and per-creator exception isolation**: one malformed API item no longer crashes the scan process and silently skips every later subscription; scans now log, continue, and exit with a stable signal.
- **Retry-After hardening**: HTTP-date and non-numeric headers no longer crash `RateLimitError` construction, and the wait is capped at 120s so a hostile header cannot pin the monitor lock indefinitely.
- **Bot token never reaches log files**: Telegram transport errors are sanitized (`/bot<token>` -> `/bot***`) before logging.
- **State writes degrade gracefully on disk-full**: OSError in `_atomic_write` is wrapped as `StateWriteError` (with tmp cleanup), matching the existing lock-timeout recovery path.
- **Backfill heartbeat race**: generation guard prevents a self-rescheduling heartbeat from keeping `active_backfills.json` alive forever, which permanently suppressed scheduled scans until restart.
- **Image downloads capped** (30MB, streamed abort) and `/cleanup` rejects day counts below 1.
- **CI**: GitHub Actions pinned to commit SHAs.

### Added
- **Scan/reconciliation failure alerts in Telegram**: admin chats are notified on success->failure transitions, with continuous-failure dedup and a recovery notice; silent missed scans are now visible.

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
