"""配置：默认值 + 可选配置文件覆盖。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_DIR = Path.home() / ".clash-ip-tracker"
DATA_FILE = APP_DIR / "table.json"
CONFIG_FILE = APP_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    # DNS 采样
    "dns_servers": ["223.5.5.5", "114.114.114.114"],
    "dns_samples": 6,          # 每轮对每个 DNS 服务器查几次
    "dns_gap_seconds": 0.3,    # 每次查询之间的间隔
    "dns_timeout": 2,          # dig 的单次超时（秒）
    # 端口探测
    "probe_ports": [61750],    # 要探测的端口，可多选
    "probe_timeout": 3.0,      # 单次 TCP 连接超时（秒）
    # 调度
    "recheck_minutes": 10,     # 一个 IP 距上次检测超过多久才重测
    "stale_days": 7,           # 多久没在 DNS 里出现就标记 stale
    # 存储
    "data_file": str(DATA_FILE),
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """浅合并，override 覆盖 base。"""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    """加载配置：默认值 < 配置文件 < 环境变量。"""
    cfg = dict(DEFAULTS)
    cfg_path = path or CONFIG_FILE
    if cfg_path.exists():
        try:
            with cfg_path.open(encoding="utf-8") as fh:
                cfg = _merge(cfg, json.load(fh))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] 配置文件读取失败，使用默认值：{exc}")

    # 环境变量覆盖（方便调试）
    if env := os.environ.get("CLASH_TRACKER_DATA"):
        cfg["data_file"] = env
    if env := os.environ.get("CLASH_TRACKER_DNS"):
        cfg["dns_servers"] = [s.strip() for s in env.split(",") if s.strip()]
    if env := os.environ.get("CLASH_TRACKER_PORTS"):
        cfg["probe_ports"] = [int(s) for s in env.split(",") if s.strip().isdigit()]

    return cfg


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
