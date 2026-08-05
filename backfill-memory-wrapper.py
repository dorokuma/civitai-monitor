#!/usr/bin/env python3
"""
backfill-memory-wrapper.py
Sets a stricter memory limit (1400 MB) for the backfill process, then execs into monitor.py.
主服务 MemoryMax=4G，Backfill 更严格 = 1400MB.
"""
import resource
import sys
import os

if len(sys.argv) < 3:
    print("Usage: backfill-memory-wrapper.py <python_bin> <monitor_script> <args...>", file=sys.stderr)
    sys.exit(1)

SOFT_LIMIT_BYTES = 1400 * 1024 * 1024   # 1400 MB - soft limit (warn if exceeded)
HARD_LIMIT_BYTES = 1500 * 1024 * 1024   # 1500 MB - hard limit (SIGKILL if exceeded)

def set_memory_limit():
    try:
        # Set virtual memory (address space) limit
        resource.setrlimit(resource.RLIMIT_AS, (SOFT_LIMIT_BYTES, HARD_LIMIT_BYTES))
        print(f"[backfill-wrapper] Set RLIMIT_AS: soft={SOFT_LIMIT_BYTES // (1024*1024)} MB, hard={HARD_LIMIT_BYTES // (1024*1024)} MB", file=sys.stderr)
    except (ValueError, OSError) as e:
        print(f"[backfill-wrapper] Warning: could not set RLIMIT_AS: {e}", file=sys.stderr)

set_memory_limit()

# Now replace ourselves with the real monitor.py
python_bin = sys.argv[1]
script_path = sys.argv[2]
script_args = sys.argv[3:]

os.execv(python_bin, [python_bin, script_path] + script_args)
