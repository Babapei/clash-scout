"""从 Clash Verge 配置里读出当前订阅的节点域名和端口。"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

APP_DIR_NAME = "io.github.clash-verge-rev.clash-verge-rev"


class ClashConfigError(RuntimeError):
    """读不到 Verge 配置。"""


def find_verge_dir() -> Path | None:
    """跨平台找 Clash Verge 的配置目录。"""
    candidates = [
        Path.home() / "Library/Application Support" / APP_DIR_NAME,  # macOS
        Path.home() / ".config" / APP_DIR_NAME,                      # Linux
        Path(os.environ.get("APPDATA", "")) / APP_DIR_NAME,          # Windows
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise ClashConfigError("需要 PyYAML：pip3 install pyyaml") from exc
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except OSError as exc:
        raise ClashConfigError(f"读取失败 {path}：{exc}") from exc
    except Exception as exc:  # yaml 解析错误
        raise ClashConfigError(f"YAML 解析失败 {path}：{exc}") from exc


def current_profile(verge_dir: Path) -> tuple[str, Path] | None:
    """返回 (订阅名, profile 文件路径)。"""
    profiles_yaml = verge_dir / "profiles.yaml"
    if not profiles_yaml.exists():
        return None
    data = _load_yaml(profiles_yaml) or {}
    current = data.get("current")
    if not current:
        return None
    for item in data.get("items") or []:
        if not isinstance(item, dict) or item.get("uid") != current:
            continue
        file_name = item.get("file")
        if not file_name:
            continue
        path = verge_dir / "profiles" / file_name
        if path.exists():
            return item.get("name") or current, path
    return None


def extract_target(profile_path: Path) -> tuple[str, int] | None:
    """统计节点里出现最多的 (server, port) 组合。"""
    data = _load_yaml(profile_path) or {}
    counter: Counter[tuple[str, int]] = Counter()
    for proxy in data.get("proxies") or []:
        if not isinstance(proxy, dict):
            continue
        server, port = proxy.get("server"), proxy.get("port")
        if isinstance(server, str) and isinstance(port, int):
            counter[(server, port)] += 1
    if not counter:
        return None
    (server, port), _ = counter.most_common(1)[0]
    return server, port


def resolve_target(verge_dir: Path | None = None) -> tuple[str, int, str]:
    """返回 (domain, port, 订阅名)。

    读不到时抛 ClashConfigError。
    """
    vd = verge_dir or find_verge_dir()
    if vd is None:
        raise ClashConfigError("找不到 Clash Verge 配置目录")
    found = current_profile(vd)
    if found is None:
        raise ClashConfigError("profiles.yaml 里没有当前订阅")
    name, path = found
    target = extract_target(path)
    if target is None:
        raise ClashConfigError(f"订阅 {name} 里没有可用的 server/port")
    return target[0], target[1], name
