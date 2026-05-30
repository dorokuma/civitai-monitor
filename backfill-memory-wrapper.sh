#!/bin/bash
# Backfill Memory Limit Wrapper
# Usage: backfill-memory-wrapper.sh <python_path> <monitor_script> <args...>
# Starts the backfill in a transient systemd scope with stricter MemoryMax (1400 MB).
# 主服务 MemoryMax=1800MB，Backfill 更严格设为 1400MB.

set -euo pipefail

PYTHON_BIN="${1:?Usage: $0 <python_bin> <monitor_script> <args...>}"
shift

SCRIPT_PATH="${1:?}"
shift

# Stricter limit for backfill: 1400 MB (主服务上限 1800MB)
BACKFILL_MEMORY_MAX=1400M

# Build args string for systemd-run
# systemd-run accepts: systemd-run --scope -p MemoryMax=1400M /path/to/script arg1 arg2 ...
exec systemd-run --scope -p "MemoryMax=${BACKFILL_MEMORY_MAX}" \
    "${PYTHON_BIN}" "${SCRIPT_PATH}" "$@"
