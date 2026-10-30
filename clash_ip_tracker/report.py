"""输出：单环境表格、跨环境对比、最佳 IP 选择。

注意：中文和 emoji 在终端里占两个字符宽度，用 len() 补空格会错位，
所以统一用 display_width() / pad() 处理。
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store

STATUS_LABEL = {
    store.ALIVE: "alive",
    store.DEAD: "dead",
    store.FLAKY: "flaky",
    store.UNKNOWN: "unknown",
}

STATUS_MARK = {
    store.ALIVE: "OK",
    store.DEAD: "X",
    store.FLAKY: "~",
    store.UNKNOWN: "?",
}


# ---------------------------------------------------------------- 宽度处理


def display_width(text: str) -> int:
    """按终端显示宽度计算：全角/宽字符算 2，其余算 1。"""
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        elif unicodedata.combining(ch):
            width += 0
        else:
            width += 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补齐（不是按 len）。"""
    padding = width - display_width(text)
    if padding <= 0:
        return text
    if align == "right":
        return " " * padding + text
    return text + " " * padding


# ---------------------------------------------------------------- 单环境


def _fmt_time(value: str | None) -> str:
    dt = store.parse_iso(value)
    if dt is None:
        return "-"
    return dt.strftime("%m-%d %H:%M")


def relative_age(iso_str: str | None, *, show_all: bool = False) -> str:
    """返回数据年龄的简短表示。

    - 1 小时内：返回空字符串（不显示，默认数据是新鲜的）
    - 1 小时 ~ 1 天：返回 "3h前"
    - 超过 1 天：返回 "2d前"
    - show_all=True：返回完整时间 "10-30 22:12"
    """
    dt = store.parse_iso(iso_str)
    if dt is None:
        return ""
    if show_all:
        return dt.strftime("%m-%d %H:%M")
    now = datetime.now(timezone.utc).astimezone()
    seconds = (now - dt).total_seconds()
    if seconds < 3600:
        return ""
    if seconds < 86400:
        return f"{int(seconds // 3600)}h前"
    return f"{(now - dt).days}d前"


def _fmt_rate(rec: dict[str, Any]) -> str:
    rate = store.success_rate(rec)
    if rate is None:
        return "-"
    return f"{rate * 100:.0f}%"


def pick_best(node: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """挑最值得用的 IP。

    优先级：非 stale > alive > flaky > 成功率高 > 延迟低
    """
    candidates = [
        (ip, rec)
        for ip, rec in node["ips"].items()
        if not rec.get("stale") and rec.get("status") in (store.ALIVE, store.FLAKY)
    ]
    if not candidates:
        return None

    def score(item: tuple[str, dict[str, Any]]) -> tuple:
        _, rec = item
        rate = store.success_rate(rec) or 0.0
        alive_bonus = 1 if rec.get("status") == store.ALIVE else 0
        latency = rec.get("latency_ms") or 9999
        return (alive_bonus, rate, -latency)

    return sorted(candidates, key=score, reverse=True)[0]


COLUMNS = [
    ("IP", 16, "left"),
    ("状态", 8, "left"),
    ("成功/失败", 10, "right"),
    ("成功率", 8, "right"),
    ("延迟", 8, "right"),
    ("最后检测", 13, "left"),
    ("最后可用", 13, "left"),
]


def _row(cells: list[str], aligns: list[str] | None = None) -> str:
    aligns = aligns or ["left"] * len(cells)
    return "  ".join(pad(c, w, a) for c, (_, w, _), a in zip(cells, COLUMNS, aligns))


def render(
    env: dict[str, Any],
    node: dict[str, Any],
    domain: str,
    port: int,
    *,
    sample_counts: dict[str, int] | None = None,
    limit: int = 30,
) -> str:
    ips = list(node["ips"].items())
    ips.sort(
        key=lambda kv: (
            0 if kv[1].get("status") == store.ALIVE else 1 if kv[1].get("status") == store.FLAKY else 2,
            -(store.success_rate(kv[1]) or 0.0),
            kv[0],
        )
    )

    lines: list[str] = []
    lines.append(f"环境: {env.get('label', '?')}    域名: {domain}    端口: {port}")
    if sample_counts is not None:
        lines.append(f"本轮 DNS 采样发现 {len(sample_counts)} 个 IP，本环境共记录 {len(node['ips'])} 个")
    alive = sum(1 for _, r in ips if r.get("status") == store.ALIVE and not r.get("stale"))
    flaky = sum(1 for _, r in ips if r.get("status") == store.FLAKY and not r.get("stale"))
    dead = sum(1 for _, r in ips if r.get("status") == store.DEAD and not r.get("stale"))
    stale = sum(1 for _, r in ips if r.get("stale"))
    lines.append(f"状态统计: 活 {alive} / 时好时坏 {flaky} / 死 {dead} / 陈旧 {stale}")
    lines.append("")

    header_line = _row([c[0] for c in COLUMNS], [c[2] for c in COLUMNS])
    lines.append(header_line)
    lines.append("-" * display_width(header_line))
    for ip, rec in ips[:limit]:
        latency = f"{rec['latency_ms']:.0f}ms" if rec.get("latency_ms") else "-"
        status = STATUS_LABEL.get(rec.get("status"), "?") + ("*" if rec.get("stale") else "")
        lines.append(
            _row(
                [
                    ip,
                    status,
                    f"{rec.get('ok', 0)}/{rec.get('fail', 0)}",
                    _fmt_rate(rec),
                    latency,
                    _fmt_time(rec.get("last_check")),
                    _fmt_time(rec.get("last_ok")),
                ],
                ["left", "left", "right", "right", "right", "left", "left"],
            )
        )

    if len(ips) > limit:
        lines.append(f"... 还有 {len(ips) - limit} 条未显示")

    best = pick_best(node)
    lines.append("")
    if best:
        ip, rec = best
        lines.append(f"推荐 IP: {ip}  (成功率 {_fmt_rate(rec)}，延迟 {rec.get('latency_ms') or '-'}ms)")
    else:
        lines.append("推荐 IP: 无（当前环境没有可用 IP）")

    return "\n".join(lines)


# ---------------------------------------------------------------- 跨环境对比


def render_compare(table: dict[str, Any], domain: str, *, show_time: bool = False) -> str:
    """跨环境对比同一个域名的 IP 状态。"""
    envs = list(table["environments"].items())
    if not envs:
        return "表里还没有任何环境数据。"

    ip_to_envs: dict[str, dict[str, dict[str, Any]]] = {}
    for env_id, env in envs:
        node = env["domains"].get(domain)
        if not node:
            continue
        for ip, rec in node["ips"].items():
            ip_to_envs.setdefault(ip, {})[env_id] = rec

    if not ip_to_envs:
        return f"没有任何环境记录过域名 {domain}。"

    env_cols = [(env_id, env.get("label", env_id)) for env_id, env in envs]

    # 先把每一行的单元格和结论算好，再根据内容决定列宽
    rows: list[tuple[str, list[str], str]] = []
    for ip in sorted(ip_to_envs):
        env_map = ip_to_envs[ip]
        cells: list[str] = []
        marks: list[str | None] = []
        for env_id, _ in env_cols:
            rec = env_map.get(env_id)
            if rec is None:
                cells.append("-")
                marks.append(None)
            else:
                marks.append(rec.get("status"))
                base = STATUS_MARK.get(rec.get("status"), "?")
                age = relative_age(rec.get("last_check"), show_all=show_time)
                cells.append(f"{base} ({age})" if age else base)

        known = [m for m in marks if m is not None]
        if known and all(m == store.ALIVE for m in known):
            verdict = "健康"
        elif known and all(m == store.DEAD for m in known):
            verdict = "机场死 IP"
        elif known and any(m in (store.ALIVE, store.FLAKY) for m in known):
            verdict = "部分环境被封"
        else:
            verdict = "待观察"
        rows.append((ip, cells, verdict))

    # 列宽 = max(表头宽度, 该列所有单元格宽度)
    widths: list[int] = []
    for idx, (_, label) in enumerate(env_cols):
        content = max(display_width(r[1][idx]) for r in rows)
        widths.append(max(display_width(label), content, 8))

    lines: list[str] = []
    lines.append(f"域名: {domain}    对比 {len(env_cols)} 个环境")
    lines.append("")

    header = pad("IP", 18) + "  " + "  ".join(
        pad(label, w) for (_, label), w in zip(env_cols, widths)
    ) + "  结论"
    lines.append(header)
    lines.append("-" * display_width(header))

    for ip, cells, verdict in rows:
        lines.append(
            pad(ip, 18) + "  " + "  ".join(
                pad(c, w) for c, w in zip(cells, widths)
            ) + f"  {verdict}"
        )

    lines.append("")
    lines.append("符号: OK=alive  X=dead  ~=flaky  ?=unknown  -=该环境没见过")
    lines.append("括号内是数据年龄，1 小时内的不显示（默认视为新鲜）")
    return "\n".join(lines)
