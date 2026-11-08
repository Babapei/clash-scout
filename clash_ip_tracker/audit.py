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
IPINFO_URL = "http://ip-api.com/json"

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
    return None


def find_group(preferred: str = DEFAULT_GROUP) -> str | None:
    """找一个可用的代理组。

    优先用 preferred（比如「🌐 代理」），找不到就用当前订阅同名的组，
    再找不到就用选项最多的那个 Selector（排除 GLOBAL）。
    """
    data = _api("/proxies")
    if not data:
        return None
    proxies = data.get("proxies") or {}

    # ① 优先用指定名字
    if preferred in proxies and proxies[preferred].get("type") == "Selector":
        return preferred

    selectors = [
        (name, info) for name, info in proxies.items()
        if info.get("type") == "Selector" and name != "GLOBAL"
    ]
    if not selectors:
        return None

    # ② 当前订阅同名组（Verge 切订阅后组名会变成订阅名）
    try:
        from .clash_config import current_subscription
        sub = current_subscription()
        sub_name = sub.get("name")
        if sub_name:
            for name, _ in selectors:
                if name == sub_name:
                    return name
    except Exception:  # noqa: BLE001
        pass

    # ③ 选项最多的
    selectors.sort(key=lambda item: len(item[1].get("all") or []), reverse=True)
    return selectors[0][0]


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
    actual_group = find_group(group)
    if not actual_group:
        raise RuntimeError("找不到可用的代理组，或者 Verge 没运行")
    group = actual_group
    info = get_group(group)
    if not info:
        raise RuntimeError(f"代理组「{group}」读取失败")

    original = info.get("now")

    # 从当前订阅读节点协议，纯本地读取，不影响连接
    try:
        from .clash_config import get_node_protocols
        protocols = get_node_protocols()
    except Exception:  # noqa: BLE001
        protocols = {}

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
            started = time.time()
            info_out = exit_info(proxy_url)
            latency = (time.time() - started) * 1000 if info_out else None

            entry = {
                "node": node,
                "ok": info_out is not None,
                "ip": (info_out or {}).get("ip"),
                "city": (info_out or {}).get("city"),
                "country": (info_out or {}).get("country"),
                "org": (info_out or {}).get("org"),
                "is_idc": is_idc((info_out or {}).get("org")),
                "latency_ms": latency,
                "protocol": protocols.get(node),
            }
            results.append(entry)
            if on_progress:
                on_progress(index, total, node, entry)
    finally:
        # 无论成功失败，都切回原节点
        if original:
            switch_node(group, original)

    return results


def pick_recommended(
    nodes: dict[str, dict[str, Any]],
    limit: int = 3,
    max_age_days: float = 7.0,
) -> list[tuple[str, dict[str, Any]]]:
    """从体检结果里挑推荐节点。

    条件：有出口 IP、住宅（非机房）、体检不超过 7 天。
    排序：延迟低的优先；同一国家/同一出口 IP 只取一个。
    """
    now = time.time()
    candidates: list[tuple[str, dict[str, Any]]] = []

    for name, info in nodes.items():
        if not info.get("ip"):
            continue
        if info.get("is_idc"):
            continue

        audited = info.get("audited_at")
        if audited:
            # audited_at 是 ISO 字符串，粗算一下年龄
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(audited)
                age_days = (now - dt.timestamp()) / 86400
                if age_days > max_age_days:
                    continue
            except (ValueError, TypeError):
                pass

        candidates.append((name, info))

    candidates.sort(key=lambda x: x[1].get("latency_ms") or float("inf"))

    seen_countries: set[str] = set()
    seen_ips: set[str] = set()
    result: list[tuple[str, dict[str, Any]]] = []

    for name, info in candidates:
        country = str(info.get("country") or "-")
        ip = str(info.get("ip"))
        if country in seen_countries or ip in seen_ips:
            continue
        seen_countries.add(country)
        seen_ips.add(ip)
        result.append((name, info))
        if len(result) >= limit:
            break

    return result


def format_results(results: list[dict[str, Any]]) -> str:
    """把体检结果格式化成对齐的文本表格。"""
    from .report import pad

    header = (
        pad("节点", 22) + "  " + pad("出口 IP", 16) + "  " + pad("协议", 10) + "  "
        + pad("地区", 12) + "  " + pad("组织", 32) + "  "
        + pad("类型", 6) + "  " + pad("延迟", 7)
    )
    lines = [header, "-" * 100]

    for r in results:
        if not r.get("ok"):
            lines.append(pad(str(r.get("node"))[:22], 24) + "  " + str(r.get("error") or "查询失败"))
            continue
        location = f"{r.get('city') or '-'}, {r.get('country') or '-'}"
        latency = r.get("latency_ms")
        lines.append(
            pad(str(r.get("node"))[:20], 22) + "  "
            + pad(str(r.get("ip") or "-"), 16) + "  "
            + pad(str(r.get("protocol") or "-")[:8], 10) + "  "
            + pad(location[:10], 12) + "  "
            + pad(str(r.get("org") or "-")[:30], 32) + "  "
            + pad("机房" if r.get("is_idc") else "住宅", 6) + "  "
            + pad(f"{latency:.0f}ms" if latency else "-", 7)
        )
    return "\n".join(lines)
