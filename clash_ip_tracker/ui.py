"""Textual 终端界面：scout ui。

三个 Tab：
  1. 当前     —— 当前网络 + 当前机场的 IP 明细
  2. 环境对比 —— 同一域名在不同网络下的状态
  3. 机场     —— 表里所有机场域名的健康概览
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from . import clash_config, config, store
from .port_probe import tcp_probe
from .report import STATUS_LABEL, STATUS_MARK, relative_age


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




class EditAliasScreen(ModalScreen[str]):
    """编辑显示名的弹窗。返回新名字，取消返回 None。"""

    CSS = """
    EditAliasScreen {
        align: center middle;
    }
    #dialog {
        width: 60;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #alias-input { margin: 1 0; }
    """

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, kind: str, ident: str, current: str) -> None:
        super().__init__()
        self.kind = kind
        self.ident = ident
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"[b]{self.kind}[/b]  内部标识: {self.ident}")
            yield Static(f"当前显示名: {self.current}")
            yield Input(value=self.current, placeholder="新名字（留空恢复原始值）", id="alias-input")
            yield Static("[dim]Enter 保存   Esc 取消[/dim]")

    def on_mount(self) -> None:
        self.query_one("#alias-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)

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
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        ("1", "switch_tab('tab-current')", "当前"),
        ("2", "switch_tab('tab-compare')", "对比"),
        ("3", "switch_tab('tab-airports')", "机场"),
        ("4", "switch_tab('tab-alias')", "别名"),
        ("e", "edit_alias", "改名"),
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
        with TabbedContent(initial="tab-current"):
            with TabPane("当前", id="tab-current"):
                yield Static("正在读取…", id="status")
                yield DataTable(id="table-current", zebra_stripes=True)
            with TabPane("环境对比", id="tab-compare"):
                yield DataTable(id="table-compare", zebra_stripes=True)
            with TabPane("机场", id="tab-airports"):
                yield DataTable(id="table-airports", zebra_stripes=True)
            with TabPane("别名", id="tab-alias"):
                yield Static("选中一行按 e 改名；新名字留空表示恢复原始值。", id="alias-hint")
                yield DataTable(id="table-alias", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#table-current", DataTable).add_columns(
            "IP", "状态", "成功/失败", "成功率", "延迟", "最后检测", "最后可用"
        )
        self.query_one("#table-compare", DataTable)
        self.query_one("#table-airports", DataTable)
        self.query_one("#table-alias", DataTable).add_columns("类型", "内部标识", "显示名")
        self.refresh_data()
        self.set_interval(30, self.refresh_data)

    # ---------------------------------------------------------------- 数据

    def _resolve_target(self) -> bool:
        try:
            self.domain, self.port, _ = clash_config.resolve_target()
            return True
        except clash_config.ClashConfigError as exc:
            self.last_error = str(exc)
            return False

    def _resolve_env(self) -> None:
        gateway = detect_gateway()
        self.env_id = f"gw:{gateway}" if gateway else "unknown"
        self.env_label = self.env_id

    def _data_path(self) -> Path:
        return Path(self.cfg["data_file"]).expanduser()

    def refresh_data(self) -> None:
        """读一次表，更新三个 Tab。"""
        self._resolve_env()
        path = self._data_path()
        snapshot: dict[str, Any] = {"environments": {}}
        if path.exists():
            with store.locked(path) as data:
                snapshot = data

        self._render_current(snapshot)
        self._render_compare(snapshot)
        self._render_airports(snapshot)
        self._render_aliases(snapshot)

    # ---------------------------------------------------------------- Tab 1

    def _render_current(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#table-current", DataTable)
        status = self.query_one("#status", Static)
        table.clear()

        if not self._resolve_target() or not self.domain or not self.port:
            status.update(f"[b red]读不到 Clash Verge 配置[/b red]\n{self.last_error or ''}")
            return

        node = snapshot.get("environments", {}).get(self.env_id, {}).get("domains", {}).get(self.domain)
        conns = current_connections(self.port)
        ips = list((node or {}).get("ips", {}).items())

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

        best = next(
            (ip for ip, rec in ips
             if rec.get("status") in (store.ALIVE, store.FLAKY) and not rec.get("stale")),
            None,
        )

        conn_desc = "、".join(sorted(conns)) if conns else "无活动连接"
        status.update(
            "\n".join(
                [
                    f"[b]环境[/b] {self.env_label}    [b]机场[/b] {store.domain_display(node or {}, self.domain)}:{self.port}",
                    f"[b]当前连接[/b] {conn_desc}",
                    f"[b]推荐 IP[/b] {best or '[red]无可用[/red]'}    "
                    f"[b]统计[/b] 活 {alive} / 抖 {flaky} / 死 {dead} / 共 {len(ips)}",
                    "[green]✓ 当前连的正是推荐 IP[/green]"
                    if best and best in conns
                    else (f"[yellow]⚠ 当前没连推荐 IP，可以考虑切到 {best}[/yellow]" if best else ""),
                ]
            )
        )

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

    # ---------------------------------------------------------------- Tab 2

    def _render_compare(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#table-compare", DataTable)
        table.clear(columns=True)

        envs = list(snapshot.get("environments", {}).items())
        if not self.domain or not envs:
            table.add_columns("提示")
            table.add_row("还没有足够的环境数据（至少要在两个网络下各跑一次 check）")
            return

        env_cols = [(env_id, env.get("label", env_id)) for env_id, env in envs]
        table.add_columns("IP", *[label for _, label in env_cols], "结论")

        ip_to_envs: dict[str, dict[str, dict[str, Any]]] = {}
        for env_id, env in envs:
            node = env.get("domains", {}).get(self.domain)
            if not node:
                continue
            for ip, rec in node.get("ips", {}).items():
                ip_to_envs.setdefault(ip, {})[env_id] = rec

        if not ip_to_envs:
            table.add_row("该域名在任何环境都没有记录", *["-"] * len(env_cols), "-")
            return

        for ip in sorted(ip_to_envs):
            env_map = ip_to_envs[ip]
            cells = []
            marks = []
            for env_id, _ in env_cols:
                rec = env_map.get(env_id)
                if rec is None:
                    cells.append("-")
                    marks.append(None)
                else:
                    marks.append(rec.get("status"))
                    base = STATUS_MARK.get(rec.get("status"), "?")
                    age = relative_age(rec.get("last_check"))
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

            table.add_row(ip, *cells, verdict)

    # ---------------------------------------------------------------- Tab 3

    def _render_airports(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#table-airports", DataTable)
        table.clear(columns=True)
        table.add_columns("环境", "机场域名", "活", "抖", "死", "共", "端口")

        envs = snapshot.get("environments", {})
        if not envs:
            table.add_row("-", "表里还没有数据", "-", "-", "-", "-", "-")
            return

        for env_id, env in envs.items():
            label = env.get("label", env_id)
            domains = env.get("domains", {})
            if not domains:
                table.add_row(label, "（无）", "-", "-", "-", "-", "-")
                continue
            for domain, node in domains.items():
                display = store.domain_display(node, domain)
                ips = node.get("ips", {})
                alive = sum(1 for r in ips.values() if r.get("status") == store.ALIVE and not r.get("stale"))
                flaky = sum(1 for r in ips.values() if r.get("status") == store.FLAKY and not r.get("stale"))
                dead = sum(1 for r in ips.values() if r.get("status") == store.DEAD and not r.get("stale"))
                ports = ",".join(str(p) for p in node.get("ports", [])) or "-"
                table.add_row(label, display, str(alive), str(flaky), str(dead), str(len(ips)), ports)


    # ---------------------------------------------------------------- Tab 4

    def _render_aliases(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#table-alias", DataTable)
        table.clear()
        self.alias_rows: list[tuple[str, str, str]] = []

        for env_id, env in snapshot.get("environments", {}).items():
            label = env.get("label") or env_id
            table.add_row("环境", env_id, label)
            self.alias_rows.append(("环境", env_id, label))
            for domain, node in env.get("domains", {}).items():
                alias = node.get("alias") or domain
                table.add_row("机场", domain, alias)
                self.alias_rows.append(("机场", domain, alias))

        if not self.alias_rows:
            table.add_row("-", "表里还没有数据", "-")

    def action_edit_alias(self) -> None:
        table = self.query_one("#table-alias", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(getattr(self, "alias_rows", [])):
            self.notify("先选一行（切到别名 Tab，用 ↑↓ 选）", severity="warning")
            return

        kind, ident, current = self.alias_rows[row]
        self.push_screen(EditAliasScreen(kind, ident, current), self._apply_alias_edit)

    def _apply_alias_edit(self, new_name: str | None) -> None:
        if new_name is None:
            return  # 取消

        table = self.query_one("#table-alias", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(getattr(self, "alias_rows", [])):
            return
        kind, ident, _ = self.alias_rows[row]

        path = self._data_path()
        with store.locked(path) as data:
            if kind == "环境":
                env = data["environments"].get(ident)
                if env is not None:
                    if new_name.strip():
                        store.set_env_label(env, new_name.strip())
                    else:
                        env.pop("label", None)
            else:
                for env in data["environments"].values():
                    node = env["domains"].get(ident)
                    if node is not None:
                        if new_name.strip():
                            store.set_domain_alias(node, new_name.strip())
                        else:
                            node.pop("alias", None)

        self.refresh_data()
        self.notify(f"已更新：{new_name.strip() or '（恢复原始值）'}")

    # ---------------------------------------------------------------- 动作

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_refresh(self) -> None:
        self.refresh_data()
        self.notify("已刷新")

    def action_check(self) -> None:
        """跑一次全量探测（同步，可能卡几秒）。"""
        if not self._resolve_target() or not self.domain or not self.port:
            self.refresh_data()
            return

        self.notify("正在检查，请稍候…")
        path = self._data_path()
        timeout = self.cfg["probe_timeout"]
        with store.locked(path) as data:
            env = store.get_environment(data, self.env_id, self.env_label)
            node = store.get_domain(env, self.domain, self.port)
            for ip, rec in list(node["ips"].items()):
                if rec.get("stale"):
                    continue
                ok, latency = tcp_probe(ip, self.port, timeout)
                store.apply_probe(rec, ok=ok, latency_ms=latency)
        self.refresh_data()
        self.notify("检查完成")


def run() -> None:
    ScoutApp().run()
