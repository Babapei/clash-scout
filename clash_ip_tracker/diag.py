"""网络诊断：分层测一遍，判断问题出在哪一层。

测的层次：
  网关     → WiFi 到路由器
  国内     → 出网是否正常
  DNS      → 域名解析是否正常
  节点     → 能不能连上机场服务器
  代理     → 代理是否生效、出口在哪
  系统     → 系统代理 / TUN 状态
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from typing import Any


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    """跑一个命令，返回 (是否成功, 输出)。"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)


def detect_gateway() -> str | None:
    if platform.system() != "Darwin":
        return None
    ok, out = _run(["route", "-n", "get", "default"])
    if not ok:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("gateway:"):
            return line.split(":", 1)[1].strip()
    return None


def test_gateway(gateway: str | None) -> dict[str, Any]:
    """网关延迟。"""
    if not gateway:
        return {"ok": False, "error": "找不到网关"}
    ok, out = _run(["ping", "-c", "3", "-W", "1000", gateway], timeout=8)
    if not ok:
        return {"ok": False, "error": "ping 失败"}
    times = [float(m) for m in re.findall(r"time=([\d.]+)", out)]
    if not times:
        return {"ok": False, "error": "没有响应"}
    avg = sum(times) / len(times)
    return {"ok": True, "gateway": gateway, "latency_ms": avg}


def test_domestic() -> dict[str, Any]:
    """国内网站连通性。"""
    targets = ["https://www.baidu.com", "https://www.taobao.com"]
    results = []
    for url in targets:
        ok, out = _run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "-m", "6", url],
            timeout=8,
        )
        parts = out.strip().split()
        if ok and parts and parts[0].isdigit() and parts[0] != "000":
            results.append(
                {"url": url, "ok": True, "code": parts[0], "time_s": float(parts[1])}
            )
        else:
            results.append({"url": url, "ok": False})
    return {"results": results, "ok": all(r["ok"] for r in results)}


def test_dns(domain: str | None) -> dict[str, Any]:
    """解析耗时和结果。"""
    if not domain:
        return {"ok": False, "error": "没有目标域名"}

    started = time.time()
    ok, out = _run(["dig", "+short", "+time=3", "+tries=2", domain], timeout=8)
    elapsed = (time.time() - started) * 1000

    ips = [line.strip() for line in out.splitlines() if line.strip()]
    return {
        "ok": ok and bool(ips),
        "domain": domain,
        "ips": ips[:5],
        "latency_ms": elapsed if ok else None,
    }


def active_node_ips(port: int) -> list[str]:
    """从 netstat 拿 Clash 当前实际连接的节点 IP。"""
    ok, out = _run(["netstat", "-an", "-p", "tcp"])
    if not ok:
        return []
    marker = f".{port}"
    ips: set[str] = set()
    for line in out.splitlines():
        if marker not in line or "ESTABLISHED" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        remote = parts[4]
        if remote.endswith(marker):
            ip = remote[: -len(marker)]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    return sorted(ips)


def test_nodes(ips: list[str], port: int, timeout: float = 3.0) -> list[dict[str, Any]]:
    """对每个 IP 做 TCP 握手。"""
    results = []
    for ip in ips[:5]:
        started = time.time()
        ok, _ = _run(["nc", "-z", "-w", str(int(timeout)), ip, str(port)], timeout=timeout + 2)
        elapsed = (time.time() - started) * 1000
        results.append({"ip": ip, "ok": ok, "latency_ms": elapsed if ok else None})
    return results


def test_proxy(proxy_url: str = "http://127.0.0.1:7897", attempts: int = 3) -> dict[str, Any]:
    """走代理查出口，测成功率和响应时间。

    复用 audit.exit_info：多源 fallback（ip-api → ipinfo → ifconfig）。
    """
    from .audit import exit_info

    successes = 0
    sample: dict[str, Any] | None = None
    times: list[float] = []

    for _ in range(attempts):
        started = time.time()
        data = exit_info(proxy_url)
        elapsed = (time.time() - started) * 1000
        if data and data.get("ip"):
            successes += 1
            times.append(elapsed)
            if sample is None:
                sample = data

    return {
        "ok": successes > 0,
        "success": f"{successes}/{attempts}",
        "latency_ms": (sum(times) / len(times)) if times else None,
        "ip": (sample or {}).get("ip"),
        "city": (sample or {}).get("city"),
        "country": (sample or {}).get("country"),
        "org": (sample or {}).get("org"),
    }


def test_system() -> dict[str, Any]:
    """系统代理 / TUN 状态。"""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "只支持 macOS"}

    ok, out = _run(["scutil", "--proxy"])
    http_enabled = bool(re.search(r"HTTPEnable\s*:\s*1", out))
    socks_enabled = bool(re.search(r"SOCKSEnable\s*:\s*1", out))
    http_port = re.search(r"HTTPPort\s*:\s*(\d+)", out)

    tun_present = False
    ok2, out2 = _run(["ifconfig"])
    if ok2 and re.search(r"198\.1[89]\.", out2):
        tun_present = True

    return {
        "ok": True,
        "system_proxy": http_enabled or socks_enabled,
        "proxy_port": http_port.group(1) if http_port else None,
        "tun": tun_present,
    }


def verdict(
    gateway: dict[str, Any],
    domestic: dict[str, Any],
    dns: dict[str, Any],
    nodes: list[dict[str, Any]],
    proxy: dict[str, Any],
    system: dict[str, Any],
) -> tuple[str, str]:
    """返回 (结论, 严重程度)。严重程度：ok / warn / error。"""
    if not domestic.get("ok"):
        return "国内网站都打不开 —— WiFi / 运营商的问题", "error"

    if not system.get("system_proxy") and not system.get("tun"):
        return "系统代理和 TUN 都没开 —— 浏览器在直连", "error"

    if nodes and not any(n.get("ok") for n in nodes):
        return "所有节点端口都不通 —— 节点挂了，或者线路被拦", "error"

    if not proxy.get("ok"):
        return "节点能连，但代理出不去 —— 检查订阅是否过期、节点是否可用", "error"

    if not dns.get("ok"):
        return "代理正常，但域名解析失败 —— 查 DNS 设置", "warn"

    if domestic.get("ok") and proxy.get("ok"):
        return "链路正常。还卡的话，可能是 DNS、浏览器或目标网站的问题", "ok"

    return "部分异常，看上面的明细", "warn"


def diagnose(domain: str | None, port: int, ips: list[str], proxy_url: str = "http://127.0.0.1:7897") -> dict[str, Any]:
    """跑完整诊断。"""
    gateway = detect_gateway()
    dns_result = test_dns(domain)

    # 节点 IP 的选取顺序：
    #   ① Clash 实际连着的（最准）
    #   ② DNS 刚解析出来的
    #   ③ 表里记录的
    node_ips = active_node_ips(port)
    node_ip_source = "实际连接"
    if not node_ips:
        node_ips = dns_result.get("ips") or []
        node_ip_source = "DNS 解析"
    if not node_ips:
        node_ips = ips
        node_ip_source = "表记录"

    result: dict[str, Any] = {
        "gateway": test_gateway(gateway),
        "domestic": test_domestic(),
        "dns": dns_result,
        "nodes": test_nodes(node_ips, port),
        "node_ip_source": node_ip_source,
        "proxy": test_proxy(proxy_url),
        "system": test_system(),
    }

    text, level = verdict(
        result["gateway"], result["domestic"], result["dns"],
        result["nodes"], result["proxy"], result["system"],
    )
    result["verdict"] = text
    result["level"] = level
    return result


def format_report(result: dict[str, Any]) -> str:
    """把诊断结果格式化成文本（命令行用）。"""
    from .report import pad

    lines: list[str] = []

    gw = result["gateway"]
    dom = result["domestic"]
    dns = result["dns"]
    nodes = result["nodes"]
    proxy = result["proxy"]
    system = result["system"]

    # 链路概览
    gw_mark = "OK" if gw.get("ok") else "X"
    dns_mark = "OK" if dns.get("ok") else "X"
    node_mark = "OK" if any(n.get("ok") for n in nodes) else "X"
    proxy_mark = "OK" if proxy.get("ok") else "X"
    lines.append("链路")
    lines.append(f"  本机 --{gw_mark}-- 网关 --{node_mark}-- 节点 --{proxy_mark}-- 出口")
    lines.append("")

    lines.append("网络")
    if gw.get("ok"):
        lines.append(f"  网关    {gw['gateway']}    {gw['latency_ms']:.0f}ms")
    else:
        lines.append(f"  网关    {gw.get('error') or '不可达'}")
    for item in dom.get("results", []):
        if item["ok"]:
            lines.append(f"  {item['url']:<28} {item['code']}  {item['time_s']:.2f}s")
        else:
            lines.append(f"  {item['url']:<28} 失败")
    lines.append("")

    lines.append("DNS")
    if dns.get("ok"):
        lines.append(f"  {dns['domain']} → {', '.join(dns['ips'])}    {dns['latency_ms']:.0f}ms")
    else:
        lines.append(f"  {dns.get('error') or '解析失败'}")
    lines.append("")

    lines.append(f"节点（来源：{result.get('node_ip_source') or '-'}）")
    for node in nodes:
        if node["ok"]:
            lines.append(f"  {node['ip']:<18} TCP {node['latency_ms']:.0f}ms")
        else:
            lines.append(f"  {node['ip']:<18} 不通")
    lines.append("")

    lines.append("代理")
    if proxy.get("ok"):
        lines.append(f"  出口    {proxy.get('ip')}  ({proxy.get('city')}, {proxy.get('country')})")
        lines.append(f"  组织    {proxy.get('org')}")
        lines.append(f"  响应    {proxy.get('latency_ms'):.0f}ms    连续 {proxy.get('success')} 成功")
    else:
        lines.append(f"  不通    连续 {proxy.get('success')} 成功")
    lines.append("")

    lines.append("系统")
    lines.append(f"  系统代理  {'开启' if system.get('system_proxy') else '关闭'}    TUN {'开启' if system.get('tun') else '关闭'}")
    lines.append("")
    lines.append(f"结论：{result['verdict']}")

    return "\n".join(lines)
