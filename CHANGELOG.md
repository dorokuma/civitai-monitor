# Changelog

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/)。

## [1.1.0] - 2026-07-27

### Added
- Telegram 全局错误处理器：捕获未处理异常并向管理员发送告警，对同一根因告警做 5 分钟去抖（debounce）。
- API 5xx / 429 指数退避重试（exponential backoff），降低限流与临时故障导致的漏发。
- pending 离页（off-page）条目自动消化逻辑，避免页面滚动后遗漏。
- 后台任务（background tasks）关闭时的统一回收（graceful shutdown），防止悬挂任务。
- 运维配套：logrotate 配置与属主自愈（ownership self-heal）脚本。

### Fixed
- 流式下载连接未关闭导致的 `CLOSE-WAIT` 连接堆积。
- `asyncio` 任务泄漏（task leak）。
- `pending_push` 条目永久滞留问题。
- `cleanup_old_caches` 时间复杂度由 O(n²) 优化。
- cookies 路径硬编码导致的分叉（多实例/多用户路径不一致）。
- docstring 死代码清理。

### Security
- `bot_token` 从 `config.yaml` 移除，统一改由环境变量注入。

### Changed
- 日志格式增加年份与时区信息。
- 移除 `_apply_memory_limit`，内存控制改由 systemd 静态配置管理。

### Tests
- 修复 2 个预存在的 `MagicMock` 不可 `await` 的测试失败，全套测试 82 passed。

## [1.0.0] - 2026-06-07

首个打标签版本。

自 v1.0.0 之后至 v1.1.0 之前，共有 13 个未打标签提交，概述如下：
- 视频处理：`feat` 支持 >50MB 视频压缩后发送；`feat` 改用本地 Bot API Server 上传大视频；`fix` 对 >50MB 视频改走 `sendDocument` 而非压缩；`chore` 移除未使用的压缩函数。
- 稳定性与防重：`fix` 防止重复 Telegram 下载与重复推送；`fix` 解决假死/崩溃并优化防漏发逻辑、支持频道推送。
- 限流与配置：`fix` 加入限流、配置外置化、超时默认值、锁依赖与服务依赖、类型注解（含一次重复提交修正）。
- 文件锁健壮性：`fix` 为 `FileLock` 超时增加重试；`fix` 在锁文件写入 PID 并新增 `_is_scan_running` 以支持陈旧锁检测。
- 安全加固与异常：`fix` 安全加固（枚举校验、日志脱敏、锁错误处理、环境变量路径回退）；`fix` 内存优化 + 安全加固 + 异常处理改善。
- 内存管理：`fix` 移除虚拟内存限制，改由 systemd 控制物理内存。
