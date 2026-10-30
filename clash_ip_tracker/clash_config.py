"""从 Clash Verge 配置里读出当前订阅的节点域名和端口。

优先用「当前选中的节点」的 server/port，而不是统计最多的那个——
因为订阅里不同节点可能用不同端口，Clash 实际只用选中的那个。
"""

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
        Path(os.environ.get("APPDATA", "") or "~") / APP_DIR_NAME,   # Windows
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ClashConfigError("需要 PyYAML：pip3 install pyyaml") from exc
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except OSError as exc:
        raise ClashConfigError(f"读取失败 {path}：{exc}") from exc
    except Exception as exc:
        raise ClashConfigError(f"YAML 解析失败 {path}：{exc}") from exc


def current_profile(verge_dir: Path) -> tuple[str, Path, set[str]]:
    """返回 (订阅名, profile 路径, 当前选中节点名集合)。"""
    profiles_yaml = verge_dir / "profiles.yaml"
    if not profiles_yaml.exists():
        raise ClashConfigError(f"找不到 {profiles_yaml}")
    data = _load_yaml(profiles_yaml) or {}
    current = data.get("current")
    if not current:
        raise ClashConfigError("profiles.yaml 里没有 current")
    for item in data.get("items") or []:
        if not isinstance(item, dict) or item.get("uid") != current:
            continue
        file_name = item.get("file")
        if not file_name:
            continue
        path = verge_dir / "profiles" / file_name
        if not path.exists():
            continue
        selected = {
            entry.get("now")
            for entry in (item.get("selected") or [])
            if isinstance(entry, dict) and entry.get("now")
        }
        return item.get("name") or current, path, selected
    raise ClashConfigError("profiles.yaml 里找不到当前订阅的 profile 文件")


def extract_target(profile_path: Path, selected_names: set[str] | None = None) -> tuple[str, int, str]:
    """返回 (domain, port, 依据说明)。

    优先使用当前选中节点的 server/port；没有选中信息时退回“出现最多的组合”。
    """
    data = _load_yaml(profile_path) or {}
    proxies = [p for p in (data.get("proxies") or []) if isinstance(p, dict)]

    if selected_names:
        for proxy in proxies:
            if proxy.get("name") in selected_names:
                server, port = proxy.get("server"), proxy.get("port")
                if isinstance(server, str) and isinstance(port, int):
                    return server, port, f"选中节点「{proxy['name']}」"

    counter: Counter[tuple[str, int]] = Counter()
    for proxy in proxies:
        server, port = proxy.get("server"), proxy.get("port")
        if isinstance(server, str) and isinstance(port, int):
            counter[(server, port)] += 1
    if not counter:
        raise ClashConfigError("订阅里没有可用的 server/port")
    (server, port), count = counter.most_common(1)[0]
    return server, port, f"出现最多的组合（{count} 个节点）"


def resolve_target(verge_dir: Path | None = None) -> tuple[str, int, str]:
    """返回 (domain, port, 来源说明)。读不到时抛 ClashConfigError。"""
    vd = verge_dir or find_verge_dir()
    if vd is None:
        raise ClashConfigError("找不到 Clash Verge 配置目录")
    name, path, selected = current_profile(vd)
    domain, port, basis = extract_target(path, selected)
    return domain, port, f"订阅「{name}」，{basis}"
