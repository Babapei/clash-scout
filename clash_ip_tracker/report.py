"""输出：表格、最佳 IP 选择。"""

from __future__ import annotations

from typing import Any

from . import store

STATUS_LABEL = {
    store.ALIVE: "alive",
    store.DEAD: "dead",
    store.FLAKY: "flaky",
    store.UNKNOWN: "unknown",
}


def _fmt_time(value: str | None) -> str:
    dt = store.parse_iso(value)
    if dt is None:
        return "-"
    return dt.strftime("%m-%d %H:%M")


def _fmt_rate(rec: dict[str, Any]) -> str:
    rate = store.success_rate(rec)
    if rate is None:
        return "-"
    return f"{rate * 100:.0f}%"


def pick_best(node: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """挑最值得用的 IP。

    排序优先级：
      1. 不是 stale
      2. status 为 alive（flaky 次之，dead/unknown 排除）
      3. 成功率高的优先
      4. 延迟低的优先
    """
    candidates = [
        (ip, rec)
        for ip, rec in node["ips"].items()
        if not rec.get("stale") and rec.get("status") in (store.ALIVE, store.FLAKY)
    ]
    if not candidates:
        return None

    def score(item: tuple[str, dict[str, Any]]) -> tuple:
        ip, rec = item
        rate = store.success_rate(rec) or 0.0
        alive_bonus = 1 if rec.get("status") == store.ALIVE else 0
        latency = rec.get("latency_ms") or 9999
        return (alive_bonus, rate, -latency)

    return sorted(candidates, key=score, reverse=True)[0]


def render(node: dict[str, Any], domain: str, port: int, *, sample_counts: dict[str, int] | None = None, limit: int = 30) -> str:
    ips = list(node["ips"].items())
    # 排序：活的在前、成功率高的在前
    ips.sort(
        key=lambda kv: (
            0 if kv[1].get("status") == store.ALIVE else 1 if kv[1].get("status") == store.FLAKY else 2,
            -(store.success_rate(kv[1]) or 0.0),
            kv[0],
        )
    )

    lines: list[str] = []
    lines.append(f"域名: {domain}    端口: {port}")
    if sample_counts is not None:
        lines.append(
            f"本轮 DNS 采样发现 {len(sample_counts)} 个 IP，表内共 {len(node['ips'])} 个"
        )
    alive = sum(1 for _, r in ips if r.get("status") == store.ALIVE and not r.get("stale"))
    flaky = sum(1 for _, r in ips if r.get("status") == store.FLAKY and not r.get("stale"))
    dead = sum(1 for _, r in ips if r.get("status") == store.DEAD and not r.get("stale"))
    stale = sum(1 for _, r in ips if r.get("stale"))
    lines.append(f"状态统计: 活 {alive} / 时好时坏 {flaky} / 死 {dead} / 陈旧 {stale}")
    lines.append("")

    header = f"{'IP':<16} {'状态':<8} {'成功/失败':<10} {'成功率':<7} {'延迟':<8} {'最后检测':<12} {'最后可用':<12}"
    lines.append(header)
    lines.append("-" * len(header))
    for ip, rec in ips[:limit]:
        latency = f"{rec['latency_ms']:.0f}ms" if rec.get("latency_ms") else "-"
        note = " [stale]" if rec.get("stale") else ""
        lines.append(
            f"{ip:<16} {STATUS_LABEL.get(rec.get('status'), '?'):<8} "
            f"{rec.get('ok', 0)}/{rec.get('fail', 0):<8} {_fmt_rate(rec):<7} "
            f"{latency:<8} {_fmt_time(rec.get('last_check')):<12} {_fmt_time(rec.get('last_ok')):<12}{note}"
        )

    if len(ips) > limit:
        lines.append(f"... 还有 {len(ips) - limit} 条未显示")

    best = pick_best(node)
    lines.append("")
    if best:
        ip, rec = best
        lines.append(f"推荐 IP: {ip}  (成功率 {_fmt_rate(rec)}，延迟 {rec.get('latency_ms') or '-'}ms)")
    else:
        lines.append("推荐 IP: 无（当前没有可用 IP）")

    return "\n".join(lines)
