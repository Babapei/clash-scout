"""节点体检：逐个切换节点，查出口 IP 信息。

依赖：
  - Verge 的 unix socket API（切节点）
  - ipinfo.io（免费的 IP 归属查询）

注意：会短暂中断当前连接，跑完会恢复原节点。
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
from typing import Any, Callable

DEFAULT_SOCK = "/var/run/clash-verge-service/users/501/verge-mihomo.sock"
DEFAULT_GROUP = "🌐 代理"
IPINFO_URL = "https://ipinfo.io/json"

# 机房/云厂商关键词（用来判断是不是 IDC，不是住宅 IP）
IDC_KEYWORDS = (
    "amazon", "aws", "azure", "microsoft", "google", "alibaba", "aliyun",
    "ovh", "hetzner", "digitalocean", "linode", "vultr", "oracle",
    "tencent", "huawei", "cloud", "hosting", "datacenter", "server",
    "colo", "vps", "leaseweb", "choopa", "contabo", "scaleway",
)


def _api(path: str) -> dict[str, Any] | None:
    """调用 Verge 的 unix socket API。"""
    try:
        result = subprocess.run(
            ["curl", "-s", "--unix-socket", DEFAULT_SOCK, f"http://localhost{path}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def get_group(group: str = DEFAULT_GROUP) -> dict[str, Any] | None:
    data = _api("/proxies")
    if not data:
        return None
    return (data.get("proxies") or {}).get(group)


def switch_node(group: str, node: str) -> bool:
    """切换 selector 到指定节点。"""
    url = f"http://localhost/proxies/{urllib.parse.quote(group, safe='')}"
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "PUT",
                "--unix-socket", DEFAULT_SOCK,
                url,
                "-d", json.dumps({"name": node}),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    # 成功时返回空字符串，失败返回错误 JSON
    return result.returncode == 0 and not result.stdout.strip()


def exit_info(proxy_url: str = "http://127.0.0.1:7897", timeout: int = 10) -> dict[str, Any] | None:
    """走代理查当前出口 IP 信息。"""
    try:
        result = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-x", proxy_url, IPINFO_URL],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def is_idc(org: str | None) -> bool:
    """粗判是不是机房/云厂商 IP。"""
    if not org:
        return False
    lowered = org.lower()
    return any(keyword in lowered for keyword in IDC_KEYWORDS)


def audit(
    group: str = DEFAULT_GROUP,
    limit: int | None = None,
    delay: float = 1.0,
    proxy_url: str = "http://127.0.0.1:7897",
    on_progress: Callable[[int, int, str, dict[str, Any] | None], None] | None = None,
) -> list[dict[str, Any]]:
    """遍历节点，查每个节点的出口信息。

    on_progress(index, total, node, result) 用于实时回调。
    """
    info = get_group(group)
    if not info:
        raise RuntimeError(f"找不到代理组「{group}」，或者 Verge 没运行")

    original = info.get("now")

    # 过滤掉非节点项：DIRECT/REJECT、信息节点、自动选择/故障转移这类组内引用
    SKIP_KEYWORDS = (
        "剩余流量", "距离下次", "套餐到期", "官网", "到期", "过期",
        "自动选择", "故障转移", "负载均衡", "DIRECT", "REJECT",
    )
    nodes = [
        n for n in (info.get("all") or [])
        if not any(k in n for k in SKIP_KEYWORDS)
    ]
    if limit:
        nodes = nodes[:limit]

    results: list[dict[str, Any]] = []
    total = len(nodes)

    try:
        for index, node in enumerate(nodes, 1):
            if not switch_node(group, node):
                results.append({"node": node, "ok": False, "error": "切换失败"})
                if on_progress:
                    on_progress(index, total, node, None)
                continue

            time.sleep(delay)  # 等连接生效
            info_out = exit_info(proxy_url)
            entry = {
                "node": node,
                "ok": info_out is not None,
                "ip": (info_out or {}).get("ip"),
                "city": (info_out or {}).get("city"),
                "country": (info_out or {}).get("country"),
                "org": (info_out or {}).get("org"),
                "is_idc": is_idc((info_out or {}).get("org")),
            }
            results.append(entry)
            if on_progress:
                on_progress(index, total, node, entry)
    finally:
        # 无论成功失败，都切回原节点
        if original:
            switch_node(group, original)

    return results


def format_results(results: list[dict[str, Any]]) -> str:
    """把体检结果格式化成对齐的文本表格。"""
    from .report import pad

    header = (
        pad("节点", 24) + "  " + pad("出口 IP", 16) + "  "
        + pad("地区", 8) + "  " + pad("组织", 36) + "  " + pad("机房", 6)
    )
    lines = [header, "-" * 100]

    for r in results:
        if not r.get("ok"):
            lines.append(pad(str(r.get("node"))[:22], 24) + "  " + str(r.get("error") or "查询失败"))
            continue
        location = f"{r.get('city') or '-'}, {r.get('country') or '-'}"
        lines.append(
            pad(str(r.get("node"))[:22], 24) + "  "
            + pad(str(r.get("ip") or "-"), 16) + "  "
            + pad(location[:10], 8) + "  "
            + pad(str(r.get("org") or "-")[:34], 36) + "  "
            + pad("是" if r.get("is_idc") else "否", 6)
        )
    return "\n".join(lines)
