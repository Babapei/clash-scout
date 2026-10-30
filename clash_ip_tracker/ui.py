"""Textual 终端界面：scout ui。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Static

from . import clash_config, config, store
from .report import STATUS_LABEL, display_width, relative_age


def detect_gateway() -> str | None:
    """读默认网关 IP，作为网络环境标识。"""
    try:
        result = subprocess.run(
            ["route", "-n", "get", "default"], capture_output=True, text=True, timeout=3
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("gateway:"):
            return line.split(":", 1)[1].strip()
    return None


def current_connections(port: int) -> set[str]:
    """从 netstat 读当前到某端口的已建立连接的对端 IP。"""
    try:
        result = subprocess.run(
            ["netstat", "-an", "-p", "tcp"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    marker = f".{port}"
    ips: set[str] = set()
    for line in result.stdout.splitlines():
        if marker not in line or "ESTABLISHED" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        remote = parts[4]
        if remote.endswith(marker):
            ips.add(remote[: -len(marker)])
    return ips


class ScoutApp(App):
    """scout 的终端仪表盘。"""

    CSS = """
    Screen { layout: vertical; }
    #status {
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #table { height: 1fr; }
    """

    BINDINGS = [
        ("r", "refresh", "刷新"),
        ("c", "check", "检查一轮"),
        ("q", "quit", "退出"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = config.load_config()
        self.domain: str | None = None
        self.port: int | None = None
        self.env_id: str = "unknown"
        self.env_label: str = "unknown"
        self.last_error: str | None = None

    # ---------------------------------------------------------------- 布局

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("正在读取…", id="status")
            yield DataTable(id="table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.add_columns("IP", "状态", "成功/失败", "成功率", "延迟", "最后检测", "最后可用")
        self.refresh_data()
        self.set_interval(30, self.refresh_data)

    # ---------------------------------------------------------------- 数据

    def _resolve_target(self) -> bool:
        """读 Verge 配置；失败时记录错误。"""
        try:
            self.domain, self.port, _ = clash_config.resolve_target()
            return True
        except clash_config.ClashConfigError as exc:
            self.last_error = str(exc)
            return False

    def _resolve_env(self) -> None:
        gateway = detect_gateway()
        if gateway:
            self.env_id = f"gw:{gateway}"
            self.env_label = f"gw:{gateway}"
        else:
            self.env_id = "unknown"
            self.env_label = "unknown"

    def refresh_data(self) -> None:
        """读表并重绘（同步，几十毫秒）。"""
        self._resolve_env()
        table = self.query_one("#table", DataTable)
        status = self.query_one("#status", Static)

        if not self._resolve_target() or not self.domain or not self.port:
            status.update(
                f"[b red]读不到 Clash Verge 配置[/b red]\n{self.last_error or ''}"
            )
            table.clear()
            return

        path = Path(self.cfg["data_file"]).expanduser()
        node: dict[str, Any] | None = None
        if path.exists():
            with store.locked(path) as data:
                node = store.find_domain(data, self.env_id, self.domain)

        conns = current_connections(self.port)
        ips = list((node or {}).get("ips", {}).items())

        # 排序：当前连接优先，然后 alive/flaky/dead
        ips.sort(
            key=lambda kv: (
                0 if kv[0] in conns else 1,
                0 if kv[1].get("status") == store.ALIVE else 1
                if kv[1].get("status") == store.FLAKY else 2,
                -(store.success_rate(kv[1]) or 0.0),
            )
        )

        alive = sum(1 for _, r in ips if r.get("status") == store.ALIVE and not r.get("stale"))
        flaky = sum(1 for _, r in ips if r.get("status") == store.FLAKY and not r.get("stale"))
        dead = sum(1 for _, r in ips if r.get("status") == store.DEAD and not r.get("stale"))

        best = None
        for ip, rec in ips:
            if rec.get("status") in (store.ALIVE, store.FLAKY) and not rec.get("stale"):
                best = ip
                break

        conn_desc = "、".join(sorted(conns)) if conns else "无活动连接"
        lines = [
            f"[b]环境[/b] {self.env_label}    [b]机场[/b] {self.domain}:{self.port}",
            f"[b]当前连接[/b] {conn_desc}",
            f"[b]推荐 IP[/b] {best or '[red]无可用[/red]'}    "
            f"[b]统计[/b] 活 {alive} / 抖 {flaky} / 死 {dead} / 共 {len(ips)}",
        ]
        if best and best in conns:
            lines.append("[green]✓ 当前连的正是推荐 IP[/green]")
        elif best:
            lines.append(f"[yellow]⚠ 当前没连推荐 IP，可以考虑切到 {best}[/yellow]")
        status.update("\n".join(lines))

        table.clear()
        for ip, rec in ips[:50]:
            mark = "●" if ip in conns else " "
            latency = f"{rec['latency_ms']:.0f}ms" if rec.get("latency_ms") else "-"
            rate = store.success_rate(rec)
            table.add_row(
                f"{mark} {ip}",
                STATUS_LABEL.get(rec.get("status"), "?"),
                f"{rec.get('ok', 0)}/{rec.get('fail', 0)}",
                f"{rate * 100:.0f}%" if rate is not None else "-",
                latency,
                relative_age(rec.get("last_check")) or "刚刚",
                relative_age(rec.get("last_ok")) or "刚刚",
            )

    # ---------------------------------------------------------------- 动作

    def action_refresh(self) -> None:
        self.refresh_data()
        self.notify("已刷新")

    def action_check(self) -> None:
        """跑一次探测（同步，可能卡几秒）。"""
        self.notify("正在检查，请稍候…")
        if not self._resolve_target() or not self.domain or not self.port:
            self.refresh_data()
            return

        path = Path(self.cfg["data_file"]).expanduser()
        timeout = self.cfg["probe_timeout"]
        with store.locked(path) as data:
            env = store.get_environment(data, self.env_id, self.env_label)
            node = store.get_domain(env, self.domain, self.port)
            for ip, rec in list(node["ips"].items()):
                if rec.get("stale"):
                    continue
                from .port_probe import tcp_probe

                ok, latency = tcp_probe(ip, self.port, timeout)
                store.apply_probe(rec, ok=ok, latency_ms=latency)
        self.refresh_data()
        self.notify("检查完成")


def run() -> None:
    ScoutApp().run()
