"""Textual 终端界面：scout ui。

三个 Tab：
  1. 当前     —— 当前网络 + 当前机场的 IP 明细
  2. 环境对比 —— 同一域名在不同网络下的状态
  3. 机场     —— 表里所有机场域名的健康概览
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
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
    #status, #compare-title {
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("1", "switch_tab('tab-current')", "当前"),
        Binding("2", "switch_tab('tab-compare')", "对比"),
        Binding("3", "switch_tab('tab-airports')", "机场"),
        Binding("4", "switch_tab('tab-alias')", "别名"),
        Binding("5", "switch_tab('tab-settings')", "设置"),
        Binding("r", "refresh", "刷新"),
        Binding("c", "check", "检查"),
        Binding("q", "quit", "退出"),
        # 以下只在对应 Tab 里用，底部不显示（各 Tab 内已有文字提示）
        Binding("a", "toggle_auto_task", "自动任务", show=False),
        Binding("plus,equal", "interval_up", "间隔+", show=False),
        Binding("minus", "interval_down", "间隔-", show=False),
        Binding("e", "edit_alias", "改名", show=False),
        Binding("space", "toggle_auto", "勾选自动", show=False),
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
                yield Static("", id="compare-title")
                yield DataTable(id="table-compare", zebra_stripes=True)
            with TabPane("机场", id="tab-airports"):
                yield DataTable(id="table-airports", zebra_stripes=True)
            with TabPane("别名", id="tab-alias"):
                yield Static("选中一行按 e 改名；新名字留空表示恢复原始值。", id="alias-hint")
                yield DataTable(id="table-alias", zebra_stripes=True)
            with TabPane("设置", id="tab-settings"):
                yield Static("正在读取…", id="settings")
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

        # 每个 Tab 独立刷新：一个出错不拖累其他
        for name, fn in (
            ("当前", self._render_current),
            ("环境对比", self._render_compare),
            ("机场", self._render_airports),
            ("别名", self._render_aliases),
        ):
            try:
                fn(snapshot)
            except Exception as exc:  # noqa: BLE001
                self.notify(f"{name} Tab 刷新失败：{exc}", severity="error")

        try:
            self._render_settings()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"设置 Tab 刷新失败：{exc}", severity="error")

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

        env_node = snapshot.get("environments", {}).get(self.env_id) or {}
        env_display = env_node.get("label") or self.env_label

        lines = [
            f"[b]环境[/b] {env_display}    [b]机场[/b] {store.domain_display(node or {}, self.domain)}:{self.port}",
            f"[b]当前连接[/b] {conn_desc}",
            f"[b]推荐 IP[/b] {best or '[red]无可用[/red]'}    "
            f"[b]统计[/b] 活 {alive} / 抖 {flaky} / 死 {dead} / 共 {len(ips)}",
        ]

        if best and best in conns:
            lines.append("[green]✓ 当前连的正是推荐 IP[/green]")
        elif best:
            lines.append(f"[yellow]⚠ 当前没连推荐 IP，可以考虑切到 {best}[/yellow]")

        # 全是死 IP：很可能是订阅没更新（机场换了域名/入口）
        if ips and alive == 0 and flaky == 0 and dead > 0:
            lines.append(
                "[red]⚠️ 所有 IP 都不通，可能订阅需要更新[/red]"
                "[dim]（Verge → 订阅 → 更新）[/dim]"
            )

        status.update("\n".join(lines))

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
        title = self.query_one("#compare-title", Static)
        display_node = None
        for _, env_node in envs:
            node = env_node.get("domains", {}).get(self.domain)
            if node is not None:
                display_node = node
                break
        display = store.domain_display(display_node or {}, self.domain)
        title.update(f"[b]当前机场[/b] {display}（{self.domain}）")

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

        envs = snapshot.get("environments", {})
        env_cols = [(env_id, env.get("label", env_id)) for env_id, env in envs.items()]

        # ① 表里记录过的机场
        domains: dict[str, dict[str, dict[str, Any]]] = {}
        domain_sub: dict[str, dict[str, str]] = {}
        for env_id, env in envs.items():
            for domain, node in env.get("domains", {}).items():
                domains.setdefault(domain, {})[env_id] = node
                if node.get("subscription"):
                    domain_sub[domain] = node["subscription"]

        # ② 补充 Verge 里存在、但还没采集过的机场
        verge_ports: dict[str, str] = {}
        try:
            for sub in clash_config.list_all_subscriptions():
                for server, port in sub["domains"]:
                    domains.setdefault(server, {})
                    domain_sub.setdefault(
                        server, {"uid": sub["uid"], "name": sub["name"]}
                    )
                    if port:
                        verge_ports.setdefault(server, str(port))
        except Exception:  # noqa: BLE001
            pass

        if not domains:
            table.add_columns("提示")
            table.add_row("表里还没有数据，也没读到 Verge 订阅")
            self.airport_rows = []
            return

        table.add_columns(
            "机场", "域名", "端口", *[label for _, label in env_cols],
            "唯一IP", "最后检测", "最后可用", "自动"
        )
        self.airport_rows = []

        for domain in sorted(domains):
            env_map = domains[domain]
            sub_name = (domain_sub.get(domain) or {}).get("name") or "-"

            if env_map:
                ports: set[str] = set()
                for node in env_map.values():
                    ports.update(str(p) for p in node.get("ports", []))
                port_text = ",".join(sorted(ports)) or verge_ports.get(domain, "-")
            else:
                port_text = verge_ports.get(domain, "-")

            cells: list[str] = []
            all_ips: set[str] = set()
            for env_id, _ in env_cols:
                node = env_map.get(env_id)
                if node is None:
                    cells.append("-")
                    continue
                ips = node.get("ips", {})
                all_ips.update(ips)
                alive = sum(1 for r in ips.values() if r.get("status") == store.ALIVE and not r.get("stale"))
                flaky = sum(1 for r in ips.values() if r.get("status") == store.FLAKY and not r.get("stale"))
                dead = sum(1 for r in ips.values() if r.get("status") == store.DEAD and not r.get("stale"))
                cells.append(f"{alive}/{flaky}/{dead}")

            unique = str(len(all_ips)) if env_map else "未采集"

            # 最后检测 / 最后可用：要从 IP 记录里取（域名节点本身没这两个字段）
            check_times: list[str] = []
            ok_times: list[str] = []
            for node in env_map.values():
                for rec in node.get("ips", {}).values():
                    if rec.get("last_check"):
                        check_times.append(rec["last_check"])
                    if rec.get("last_ok"):
                        ok_times.append(rec["last_ok"])
            latest_check = max(check_times) if check_times else None
            latest_ok = max(ok_times) if ok_times else None

            auto = "✓" if store.is_airport_auto(snapshot, domain) else "✗"
            table.add_row(
                sub_name,
                domain,
                port_text,
                *cells,
                unique,
                relative_age(latest_check) or "刚刚" if latest_check else "-",
                relative_age(latest_ok) or "刚刚" if latest_ok else "-",
                auto,
            )
            self.airport_rows.append(domain)

    def action_toggle_auto(self) -> None:
        """在机场 Tab 里按空格切换是否参与自动检测。"""
        table = self.query_one("#table-airports", DataTable)
        row = table.cursor_row
        rows = getattr(self, "airport_rows", [])
        if row < 0 or row >= len(rows):
            self.notify("先切到机场 Tab 并选中一行", severity="warning")
            return

        domain = rows[row]
        path = self._data_path()
        with store.locked(path) as data:
            enabled = not store.is_airport_auto(data, domain)
            store.set_airport_auto(data, domain, enabled)

        self.refresh_data()
        self.notify(f"{domain} 自动检测：{'开启' if enabled else '关闭'}")


    # ---------------------------------------------------------------- Tab 5

    def _scheduler_state(self) -> tuple[bool, int]:
        """返回 (是否开启, 间隔分钟)。"""
        home = Path.home()
        plist = home / "Library" / "LaunchAgents" / "com.local.scout.plist"
        if not plist.exists():
            return False, 10
        try:
            text = plist.read_text()
            match = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", text)
            interval = int(match.group(1)) // 60 if match else 10
        except OSError:
            interval = 10
        return True, interval

    def _render_settings(self) -> None:
        widget = self.query_one("#settings", Static)
        enabled, interval = self._scheduler_state()
        state = "[green]已开启[/green]" if enabled else "[red]已关闭[/red]"
        lines = [
            "[b]自动任务[/b]",
            f"  状态：{state}",
            f"  间隔：每 {interval} 分钟",
            "",
        ]

        # 订阅状态
        try:
            age_info = clash_config.subscription_age_days()
        except clash_config.ClashConfigError:
            age_info = None
        if age_info:
            name, days = age_info
            if days >= 7:
                lines.append(f"[b]订阅[/b] {name}  [red]已 {days:.0f} 天未更新，建议去 Verge 更新[/red]")
            elif days < 1:
                lines.append(f"[b]订阅[/b] {name}  今天更新")
            else:
                lines.append(f"[b]订阅[/b] {name}  {days:.0f} 天前更新")

            # 同订阅下的所有历史域名
            try:
                current_sub = clash_config.current_subscription()
                uid = current_sub.get("uid")
            except clash_config.ClashConfigError:
                uid = None

            if uid and self.domain:
                seen: set[str] = set()
                old_domains: list[str] = []
                with store.locked(self._data_path()) as data:
                    for env_node in data["environments"].values():
                        for d, node in env_node["domains"].items():
                            if d in seen:
                                continue
                            if (node.get("subscription") or {}).get("uid") == uid:
                                seen.add(d)
                                if d != self.domain:
                                    old_domains.append(d)

                lines.append(f"    当前域名：{self.domain}")
                for d in sorted(old_domains):
                    lines.append(f"    [dim]历史域名：{d}（已失效）[/dim]")
            lines.append("")

        lines += [
            "[b]快捷键[/b]",
            "  [b]a[/b]      开启 / 关闭自动任务",
            "  [b]+ / -[/b]  间隔 +5 / -5 分钟",
            "",
            "[dim]删除记录请用命令行：scout forget --env <名字> / --ip <IP> / --domain <域名>[/dim]",
        ]
        widget.update("\n".join(lines))

    def action_toggle_auto_task(self) -> None:
        from . import cli as cli_module

        enabled, interval = self._scheduler_state()
        args = argparse.Namespace(every=interval)
        if enabled:
            cli_module.cmd_uninstall_scheduler(args, self.cfg)
        else:
            cli_module.cmd_install_scheduler(args, self.cfg)
        self._render_settings()

    def action_interval_up(self) -> None:
        self._change_interval(5)

    def action_interval_down(self) -> None:
        self._change_interval(-5)

    def _change_interval(self, delta: int) -> None:
        from . import cli as cli_module

        enabled, interval = self._scheduler_state()
        new_interval = max(1, interval + delta)
        args = argparse.Namespace(every=new_interval)
        if enabled:
            cli_module.cmd_install_scheduler(args, self.cfg)
            self.notify(f"间隔改为 {new_interval} 分钟")
        else:
            self.notify(f"自动任务没开，先按 a 开启（会使用 {new_interval} 分钟）", severity="warning")
        self._render_settings()

    # ---------------------------------------------------------------- Tab 4

    def _render_aliases(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#table-alias", DataTable)
        table.clear()
        self.alias_rows: list[tuple[str, str, str]] = []

        envs = snapshot.get("environments", {})

        for env_id, env in envs.items():
            label = env.get("label") or env_id
            table.add_row("环境", env_id, label)
            self.alias_rows.append(("环境", env_id, label))

        # 机场：先收表里记录过的，再从 Verge 订阅补上没采集过的
        seen_domains: set[str] = set()
        domain_alias: dict[str, str] = {}
        for env in envs.values():
            for domain, node in env.get("domains", {}).items():
                if node.get("alias"):
                    domain_alias.setdefault(domain, node["alias"])

        for env in envs.values():
            for domain in env.get("domains", {}):
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)
                alias = domain_alias.get(domain) or domain
                table.add_row("机场", domain, alias)
                self.alias_rows.append(("机场", domain, alias))

        try:
            for sub in clash_config.list_all_subscriptions():
                for server, _ in sub["domains"]:
                    if server in seen_domains:
                        continue
                    seen_domains.add(server)
                    table.add_row("机场", server, server)
                    self.alias_rows.append(("机场", server, server))
        except Exception:  # noqa: BLE001
            pass

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
        """检查所有勾选的机场（和自动任务一致）。"""
        from . import cli as cli_module

        self.notify("正在检查所有勾选的机场…")
        args = argparse.Namespace(
            all=False,
            airport=None,
            only_enabled=True,
            samples=2,
            recheck=None,
            env=None,
            domain=None,
            port=None,
        )
        try:
            cli_module._check_all(args, self.cfg)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"检查出错：{exc}", severity="error")
        self.refresh_data()
        self.notify("检查完成")


def run() -> None:
    ScoutApp().run()
