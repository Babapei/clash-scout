#!/usr/bin/env python3
"""顶层入口：不安装也能直接 `python3 scout.py ...`。"""

import sys
from pathlib import Path

# 允许从任意目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from clash_ip_tracker.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
