# Changelog

This file follows the [Keep a Changelog](https://keepachangelog.com/) format, and version numbers follow [SemVer](https://semver.org/).

## [1.8.0] - 2026-11-22

### Changed
- **Strict at-most-once Telegram push delivery**: replaced best-effort post-send bookkeeping with lock-protected write-ahead claiming backed by a per-chat global ledger, automatically migrating historical pushed-ID and inflight/pending state files on first use.
- **Fail-closed state reads**: corrupt or unreadable state files now abort the scan with an error instead of overwriting or deleting damaged files.
- **Explicit delivery outcome handling**: removed automatic retries on HTTP `5xx` and generic transport errors (retaining `429` retries capped at 60s), disallowed text fallbacks on `5xx`/`408`/`429`/uncertain failures, split connect/read timeouts per Telegram endpoint, and moved media compression before slot claiming with guaranteed temporary-file cleanup.
- **Eliminated duplicate-push retry loops**: removed transient download failure Telegram notices (warnings are logged and retried on the next scan), disabled automatic pending retries, and isolated per-creator hole windows while keeping cross-subscriber convergence.

## [1.7.0] - 2026-09-20

### Added
- **Pre-upload media compression (lossless first)**: oversized photos (> 10 MiB) and videos (> 2 GiB local Bot API limit) are compressed before upload — stripping metadata or re-deflating PNGs first, then falling back to bounded JPEG quality/downscale for images and faststart remux, CRF 18 H.264/AAC, or duration-based bitrate targeting for videos, with hard timeouts (`timeout_seconds`, default 300) and automatic cleanup of failed artifacts.
- **`compress:` configuration section**: added optional settings (`enabled`, `image_quality_min`, `image_allow_scale`, `image_min_scale`, `video_crf`, `video_preset`, `video_audio_bitrate`, `video_allow_scale`, `video_min_scale`, `timeout_seconds`) with safe defaults when omitted.

### Changed
- **Compress instead of rerouting**: oversized videos now upload exclusively via `sendVideo` and photos are checked/compressed individually before assembling media groups, falling back to plain-text notices only if compression still cannot meet size limits. Compressed artifacts are deleted after upload while original downloads follow normal cache retention. Requires `Pillow` and runtime `ffmpeg`/`ffprobe`.

### Removed
- Removed the legacy document-upload fallback branch for oversized videos in favor of compression.

## [1.6.0] - 2026-09-15

### Changed
- **Newest-first `/backfill` list**: the `/backfill` interactive button panel and pagination now display newly added subscriptions before older ones, without changing storage order, `/list`, or `/remove`.

## [1.5.2] - 2026-09-14

### Fixed
- **More accurate HTTP status alert titles**: status codes in alert titles now require explicit HTTP forms or nearby error phrases, preventing unrelated numbers (such as timeout milliseconds or item counts) from being misclassified as API `500`/`503` errors while still matching rate-limit and explicit `HTTP N` formats.
- **Job-scoped reconciliation alert titles**: reconciliation failure alerts now summarize only the reconciliation log window instead of borrowing the last scan error.
- **Rate-limited alert-state persistence warnings**: repeated state persistence failures are suppressed for 24 hours after the first warning and log a recovery notice once persistence succeeds again.

## [1.5.1] - 2026-09-14

### Changed
- **24-hour cron failure alert gate**: scheduled `scan` and `reconciliation` jobs notify admins only after 24 continuous hours without success (followed by a daily digest and a recovery notice if an alert was sent), persisted across restarts. Manual `/scan` continues to report failures immediately.

### Fixed
- **Informative failure alert titles**: HTTP status codes (`502`/`503`/`429`), timeouts, connection errors, or the most relevant tail log line now appear directly in failure alert titles instead of bare exit codes.

## [1.5.0] - 2026-09-13

### Added
- **Weekly Telegram report** (Sundays 21:00 UTC to admin chats): summarizes weekly scan counts, per-subscription push counts and last-push dates (flagging subscriptions silent for 14+ days), and the last reconciliation date.
- **Push history tracking**: confirmed pushes record timestamps with 35-day rolling retention to power weekly reports without failing pushes if history writes fail.

### Fixed
- **Backup archive exclusions**: moved `--exclude` options before operands in the `tar` command and excluded `.venv`, preventing downloaded media from bloating backup archives and shrinking archives from ~2.7 GiB to ~0.5 MB.

## [1.4.0] - 2026-09-13

### Fixed
- **Media-retry lifecycle**: pending items now track media upload failures separately so failed uploads are actually retried (and off-page items stay pending until they reappear) instead of being prematurely promoted as pushed.
- **Stale inflight recovery**: adopting stale inflight entries preserves the earliest timestamp and media-failure flag so crashes mid-retry do not reset the confirmation window.
- **Empty download rejection**: 0-byte downloads are rejected before caching or sending.
- **API response guards**: non-list `items` payloads raise a fetch error instead of silently skipping a creator, and non-dict items are filtered out.
- **Process and lock hygiene**: `/scan` registers its process so `/stop` can terminate it; backfill lock files are no longer unlinked on release; off-page promotion handles exceptions per item.

## [1.3.0] - 2026-09-12

### Fixed
- **Prevent silent media loss**: upload retries rewind file handles, reject zero-byte files, and park failed media uploads as pending for the next scan instead of marking them pushed via text fallback.
- **Message length limits**: timestamps are capped and captions/messages are clipped to Telegram limits (`1000` for media captions, `4000` for text) to prevent caption overflow failures.
- **API response guards**: non-dict JSON payloads raise a fetch error instead of silently failing, and malformed item metadata no longer crashes every scan.
- **Cron resilience**: invalid reconciliation times fall back to `03:30`, malformed YAML configs fall back safely to minimal defaults, and corrupted interval state no longer breaks the scan loop.
- **State hygiene**: fixed `active_backfills.json` ownership, atomic status writes, and monitor lock handling.

### Security
- Restricted weekly backup archive permissions (`0600`/`0700`) since archives contain the bot token and Civitai cookies.

## [1.2.0] - 2026-09-12

### Added
- **Scan and reconciliation failure alerts**: admin chats receive deduplicated failure and recovery notifications in Telegram.

### Fixed
- **Download path traversal protection**: API item IDs are validated as integers, extensions are whitelisted per media type, and resolved paths are constrained inside the output directory.
- **Per-item and per-creator error isolation**: a single malformed item no longer aborts the scan for remaining subscriptions.
- **`Retry-After` hardening**: HTTP-date and invalid headers no longer crash rate-limit handling, and waits are capped at 120s.
- **Log token sanitization**: Telegram bot tokens are masked in transport error logs.
- **Graceful disk-full handling**: disk write errors convert into clean state-write errors with temporary file cleanup.
- **Backfill heartbeat race**: generation guards prevent stale heartbeats from blocking scheduled scans indefinitely.
- **Download and cleanup guards**: image downloads are capped at 30 MB and `/cleanup` rejects day counts below 1.

## [1.1.5] - 2026-08-26

### Added
- Cursor-loop detection and a per-track page cap (`500`) in full scans to prevent infinite pagination loops from holding the process lock.

### Fixed
- Full scans now preserve `nextCursor` across empty pages and stop after 3 consecutive empty pages instead of dropping the cursor or looping endlessly.

## [1.1.4] - 2026-08-26

### Fixed
- Incremental scans request `sort=Newest` from Civitai so newly published works appear on the first page.

## [1.1.3] - 2026-08-08

### Added
- Modularized codebase layout with backwards-compatible imports.
- Hard HTTP/network fetch failures now raise and exit with status code `2` instead of returning empty results silently.
- Limited `429 Retry-After` retries on Telegram sends and Markdown escaping for dynamic usernames.
- Paginated keyboard UI shared between `/remove` and `/backfill`.

### Fixed
- Config writes atomically and redacts environment-injected `telegram.bot_token` values.
- Corrupt seen/pushed JSON state files degrade to empty sets with a warning.
- Streaming video downloads without `Content-Length` enforce the configured size cap (default `1024 MB`).
- Interrupted `/status` snapshots are preserved instead of immediately deleted.
- Updated backfill idle-timeout message to `1800s` (30 minutes), clarified that `/mode` and `/nsfw` are global settings, and unified `/cleanup` cache limits.

### Changed
- Removed unused `pydantic-settings` dependency and updated documentation for multi-user subscriptions and environment token handling.

## [1.1.2] - 2026-08-05

### Fixed
- Handled non-numeric `Content-Length` headers gracefully during video downloads instead of crashing.
- Handled corrupt JSON metadata files in `/status` without crashing the command.
- Enabled virtual memory limits (`1400 MB` soft / `1500 MB` hard) for backfill processes and aligned main service memory limit documentation (`MemoryMax=4G`).

## [1.1.1] - 2026-07-29

### Fixed
- Admin bot no longer pages admins for transient Telegram polling transport errors automatically retried by the client library.
- Admin bot now uses the configured local Bot API Server (`telegram.api_base_url`) for polling and replies, matching media pushes.
- Increased HTTP polling timeouts to reduce spurious read errors under load.

## [1.1.0] - 2026-07-27

### Added
- Global Telegram error handler with 5-minute per-root-cause alert deduplication.
- Exponential backoff retries on API `5xx` and `429` responses to reduce missed pushes.
- Automatic processing of pending off-page entries after pagination.
- Graceful shutdown for background tasks.
- Logrotate configuration and ownership self-heal script.

### Fixed
- Fixed `CLOSE-WAIT` connection pile-up, `asyncio` task leaks, and permanently stalled pending pushes.
- Optimized cache cleanup complexity and unified cookie path configuration across users/instances.

### Security
- Removed `bot_token` from `config.yaml`; tokens are now injected exclusively via environment variables.

### Changed
- Log timestamps now include year and timezone information.
- Moved process memory limits from in-app enforcement to static systemd configuration.

## [1.0.0] - 2026-06-07

First tagged release. Subsequent untagged updates before `v1.1.0` added local Bot API Server support for large videos, duplicate download/push prevention, rate limiting, externalized config, stale-lock detection, security hardening, and systemd-managed memory limits.
