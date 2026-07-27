# Changelog

This file follows the [Keep a Changelog](https://keepachangelog.com/) format, and version numbers follow [SemVer](https://semver.org/).

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
