#!/usr/bin/env python3
"""
backfill-memory-wrapper.py
Execs the backfill process into monitor.py; memory is constrained by systemd.
主服务 MemoryMax=4G，Backfill 不再设置虚拟地址空间限制.
"""
import os
import sys

if len(sys.argv) < 3:
    print("Usage: backfill-memory-wrapper.py <python_bin> <monitor_script> <args...>", file=sys.stderr)
    sys.exit(1)

# Now replace ourselves with the real monitor.py
python_bin = sys.argv[1]
script_path = sys.argv[2]
script_args = sys.argv[3:]

os.execv(python_bin, [python_bin, script_path] + script_args)
