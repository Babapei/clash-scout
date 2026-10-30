"""命令行入口。"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import clash_config, config, dns_probe, port_probe, report, store


# ---------------------------------------------------------------- 环境识别


def detect_gateway() -> str | None:
    """读默认网关 IP，作为网络环境标识（不需要任何权限）。"""
    try:
        result = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("gateway:"):
            return line.split(":", 1)[1].strip()
    return None


def resolve_env(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (env_id, 展示用 label)。

    优先用 --env 指定的名字，否则用网关 IP。
    """
    if args.env:
        return f"name:{args.env}", args.env
    gateway = detect_gateway()
    if gateway:
        return f"gw:{gateway}", f"gw:{gateway}"
    return "unknown", "unknown"


def current_connections(port: int) -> set[str]:
    """从 netstat 里读出当前到某端口的已建立连接的对端 IP。"""
    try:
        result = subprocess.run(
            ["netstat", "-an", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=5,
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
        if not remote.endswith(marker):
            continue
        ip = remote[: -len(marker)]
        if ip:
            ips.add(ip)
    return ips


# ---------------------------------------------------------------- 参数解析


def _target(args: argparse.Namespace, cfg: dict) -> tuple[str, int]:
    """解析目标域名和端口。

    优先级：--domain > --from-clash > 配置文件 > 报错
    """
    if args.domain:
        port = args.port or (cfg["probe_ports"][0] if cfg.get("probe_ports") else 61750)
        return args.domain, int(port)

    if args.from_clash:
        try:
            domain, port, info = clash_config.resolve_target()
        except clash_config.ClashConfigError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            raise SystemExit(2)
        print(f"从 Verge 读取：{info}")
        print(f"  → {domain}:{port}")
        return domain, int(args.port or port)

    domain = cfg.get("domain")
    if not domain:
        print(
            "错误：需要指定域名。用 --domain n43ds.chhbuv.cn，或 --from-clash 自动读取",
            file=sys.stderr,
        )
        raise SystemExit(2)
    port = args.port or (cfg["probe_ports"][0] if cfg.get("probe_ports") else 61750)
    return domain, int(port)


def _data_path(cfg: dict) -> Path:
    return Path(cfg["data_file"]).expanduser()


def _sample(args: argparse.Namespace, cfg: dict, domain: str) -> dict[str, int] | None:
    try:
        return dns_probe.sample(
            domain,
            cfg["dns_servers"],
            args.samples or cfg["dns_samples"],
            cfg["dns_gap_seconds"],
            cfg["dns_timeout"],
        )
    except dns_probe.DigUnavailable as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 命令


def cmd_sample(args: argparse.Namespace, cfg: dict) -> int:
    """只采样 DNS，把新 IP 记进当前环境的表。"""
    domain, port = _target(args, cfg)
    counts = _sample(args, cfg, domain)
    if counts is None:
        return 1
    if not counts:
        print("警告：DNS 没有返回任何 IP（域名失效或被劫持）")
        return 1

    env_id, label = resolve_env(args)
    with store.locked(_data_path(cfg)) as table:
        env = store.get_environment(table, env_id, label)
        node = store.get_domain(env, domain, port)
        new_ips = [ip for ip in counts if ip not in node["ips"]]
        for ip in counts:
            store.upsert_seen(node, ip)
        store.mark_stale(node, cfg["stale_days"])
        print(f"环境 {label}：采样发现 {len(counts)} 个 IP（新增 {len(new_ips)} 个）")
        if new_ips:
            print("新 IP：" + ", ".join(sorted(new_ips)))
    return 0


def cmd_check(args: argparse.Namespace, cfg: dict) -> int:
    """采样 + 探测过期 IP + 更新表 + 输出报告。"""
    domain, port = _target(args, cfg)
    counts = _sample(args, cfg, domain)
    if counts is None:
        return 1

    recheck = args.recheck if args.recheck is not None else cfg["recheck_minutes"]
    timeout = cfg["probe_timeout"]
    env_id, label = resolve_env(args)

    with store.locked(_data_path(cfg)) as table:
        env = store.get_environment(table, env_id, label)
        node = store.get_domain(env, domain, port)
        for ip in counts:
            store.upsert_seen(node, ip)

        pending = [
            ip
            for ip, rec in node["ips"].items()
            if not rec.get("stale") and store.needs_check(rec, recheck)
        ]
        if pending:
            print(f"环境 {label}：探测 {len(pending)} 个 IP")
        for ip in pending:
            ok, latency = port_probe.tcp_probe(ip, port, timeout)
            store.apply_probe(node["ips"][ip], ok=ok, latency_ms=latency)
            mark = "OK  " if ok else "DEAD"
            ms = f"{latency:.0f}ms" if latency else "-"
            print(f"  {ip:<16} {mark}  {ms}")

        store.mark_stale(node, cfg["stale_days"])
        print()
        print(report.render(env, node, domain, port, sample_counts=counts))
    return 0


def cmd_report(args: argparse.Namespace, cfg: dict) -> int:
    """只读表，不联网。"""
    domain, port = _target(args, cfg)
    env_id, _ = resolve_env(args)
    path = _data_path(cfg)
    if not path.exists():
        print(f"表还不存在：{path}\n先跑一次 sample 或 check。")
        return 1
    with store.locked(path) as table:
        env = table["environments"].get(env_id)
        if not env:
            known = ", ".join(table["environments"].keys()) or "（空）"
            print(f"当前环境 {env_id} 还没有数据。已有环境：{known}")
            return 1
        node = env["domains"].get(domain)
        if not node:
            print(f"环境 {env.get('label')} 里没有域名 {domain}")
            return 1
        print(report.render(env, node, domain, port))
    return 0


def cmd_best(args: argparse.Namespace, cfg: dict) -> int:
    """只输出推荐 IP，方便脚本调用。"""
    domain, _ = _target(args, cfg)
    env_id, _ = resolve_env(args)
    with store.locked(_data_path(cfg)) as table:
        node = store.find_domain(table, env_id, domain)
        best = report.pick_best(node) if node else None
    if not best:
        print("")
        return 1
    print(best[0])
    return 0


SCHEDULER_LABEL = "com.local.scout"


def _scheduler_paths() -> tuple[Path, Path, Path, Path]:
    """返回 (plist, wrapper, app_dir, log_dir)。"""
    home = Path.home()
    log_dir = home / ".clash-ip-tracker"
    return (
        home / "Library" / "LaunchAgents" / f"{SCHEDULER_LABEL}.plist",
        log_dir / "run-scheduled.sh",
        log_dir / "app",
        log_dir,
    )


def cmd_install_scheduler(args: argparse.Namespace, cfg: dict) -> int:
    """安装定时记录任务（macOS launchd）。"""
    if platform.system() != "Darwin":
        print("这个命令只支持 macOS。Linux 用户请用 cron，见 docs/usage.md。")
        return 1

    interval = args.every or 10
    plist, wrapper, app_dir, log_dir = _scheduler_paths()
    project_dir = Path(__file__).resolve().parent.parent

    # macOS 的 launchd 不能访问 ~/Documents（TCC 保护），
    # 所以把运行所需文件复制到 ~/.clash-ip-tracker/app/
    app_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(app_dir / "clash_ip_tracker", ignore_errors=True)
    shutil.copytree(project_dir / "clash_ip_tracker", app_dir / "clash_ip_tracker")
    shutil.copy2(project_dir / "scout.py", app_dir / "scout.py")

    wrapper.write_text(
        "#!/bin/bash\n"
        "# 由 scout install-scheduler 生成\n"
        'if ! pgrep -f "Clash Verge" >/dev/null 2>&1; then\n'
        '  echo "$(date \'+%F %T\') Clash Verge 未运行，跳过"\n'
        "  exit 0\n"
        "fi\n"
        'echo "$(date \'+%F %T\') 开始记录"\n'
        f'PYTHONPATH="{app_dir}" "{sys.executable}" "{app_dir}/scout.py" --from-clash check 2>&1 | tail -5\n'
    )
    wrapper.chmod(0o755)

    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{SCHEDULER_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        "        <string>/bin/bash</string>\n"
        f"        <string>{wrapper}</string>\n"
        "    </array>\n"
        "    <key>StartInterval</key>\n"
        f"    <integer>{interval * 60}</integer>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{log_dir}/scheduler.log</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{log_dir}/scheduler.err</string>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )

    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"加载失败：{result.stderr.strip()}")
        return 1

    print(f"✅ 已安装：每 {interval} 分钟自动记录一次")
    print(f"   日志：{log_dir}/scheduler.log")
    print(f"   卸载：scout uninstall-scheduler")
    return 0


def cmd_uninstall_scheduler(args: argparse.Namespace, cfg: dict) -> int:
    """卸载定时记录任务。"""
    plist, wrapper, app_dir, _ = _scheduler_paths()
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    for path in (plist, wrapper):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(app_dir, ignore_errors=True)
    print("✅ 已卸载定时任务")
    return 0


def cmd_airports(args: argparse.Namespace, cfg: dict) -> int:
    """列出表里记录过的所有机场域名，以及各自的健康概况。"""
    path = _data_path(cfg)
    if not path.exists():
        print(f"表还不存在：{path}\n先跑一次 check。")
        return 1

    with store.locked(path) as table:
        if not table["environments"]:
            print("表里还没有任何数据。")
            return 0

        total = 0
        for env_id, env in table["environments"].items():
            label = env.get("label", env_id)
            print(f"环境 {label}")
            domains = env["domains"]
            if not domains:
                print("  （无数据）")
                print()
                continue
            for domain, node in domains.items():
                total += 1
                ips = node["ips"]
                alive = sum(1 for r in ips.values() if r.get("status") == store.ALIVE and not r.get("stale"))
                flaky = sum(1 for r in ips.values() if r.get("status") == store.FLAKY and not r.get("stale"))
                dead = sum(1 for r in ips.values() if r.get("status") == store.DEAD and not r.get("stale"))
                ports = ",".join(str(x) for x in node.get("ports", [])) or "-"
                print(
                    f"  {domain:<26} 活 {alive:<3} 抖 {flaky:<3} 死 {dead:<3} "
                    f"共 {len(ips):<3} 端口 {ports}"
                )
            print()

        if total == 0:
            print("表里还没有任何机场记录。")
        else:
            print(f"共 {total} 条记录（同一域名在不同环境下分别计数）")
    return 0


def cmd_compare(args: argparse.Namespace, cfg: dict) -> int:
    """对比同一个域名在不同网络环境下的表现。"""
    domain, _ = _target(args, cfg)
    with store.locked(_data_path(cfg)) as table:
        print(report.render_compare(table, domain))
    return 0


def cmd_current(args: argparse.Namespace, cfg: dict) -> int:
    """看 Clash 此刻实际连的是哪个 IP，并对照表里的历史。"""
    domain, port = _target(args, cfg)
    env_id, label = resolve_env(args)
    conns = current_connections(port)

    if not conns:
        print(f"当前没有到端口 {port} 的活动连接。")
        print("可能原因：没在用代理、TUN/系统代理都没开、或端口不是这个。")
        return 1

    path = _data_path(cfg)
    timeout = cfg["probe_timeout"]
    with store.locked(path) as table:
        node = store.find_domain(table, env_id, domain)
        if node is None:
            env = store.get_environment(table, env_id, label)
            node = store.get_domain(env, domain, port)

        print(f"环境: {label}    端口: {port}")
        print(f"Clash 当前建立了 {len(conns)} 个连接，对端 IP：")
        print()

        healthy = 0
        for ip in sorted(conns):
            rec = node["ips"].get(ip)
            if rec is None:
                # 表里没见过：当场探一次，顺手记进表
                ok, latency = port_probe.tcp_probe(ip, port, timeout)
                rec = store.upsert_seen(node, ip)
                store.apply_probe(rec, ok=ok, latency_ms=latency)
                ms = f"{latency:.0f}ms" if latency else "-"
                print(f"  {ip:<16} 新 IP → 实时探测 {'OK' if ok else 'DEAD'}  {ms}")
                if ok:
                    healthy += 1
                continue

            rate = store.success_rate(rec)
            rate_txt = f"{rate * 100:.0f}%" if rate is not None else "-"
            latency = f"{rec['latency_ms']:.0f}ms" if rec.get("latency_ms") else "-"
            status = rec.get("status", "?")
            print(f"  {ip:<16} {status:<8} 成功率 {rate_txt:<5} 延迟 {latency}")
            if status in (store.ALIVE, store.FLAKY):
                healthy += 1

        print()
        if healthy > 0:
            print("结论：当前连的是健康 IP。如果上网还卡，问题不在 IP，去查 WiFi / 线路。")
        else:
            print("结论：当前连的 IP 都不健康。重载配置让它重新解析，或换网络。")
    return 0


# ---------------------------------------------------------------- 组装


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="侦察代理节点域名的 IP 池健康（纯只读，不改任何 Clash 配置）",
    )
    parser.add_argument("--domain", help="节点域名，如 n43ds.chhbuv.cn")
    parser.add_argument("--port", type=int, help="节点端口，默认取配置")
    parser.add_argument("--data", help="自定义表文件路径")
    parser.add_argument(
        "--env",
        help="给当前网络起个名字（如 home/campus/hotspot）。不指定则用网关 IP 自动区分",
    )
    parser.add_argument(
        "--from-clash",
        action="store_true",
        help="从 Clash Verge 当前订阅里自动读取域名和端口（省得手输）",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser("sample", help="只采样 DNS，记录新 IP")
    p_sample.add_argument("--samples", type=int, help="DNS 采样轮数")
    p_sample.set_defaults(func=cmd_sample)

    p_check = sub.add_parser("check", help="采样 + 探测 + 更新表 + 报告")
    p_check.add_argument("--samples", type=int, help="DNS 采样轮数")
    p_check.add_argument("--recheck", type=int, help="距上次检测超过多少分钟才重测")
    p_check.set_defaults(func=cmd_check)

    p_report = sub.add_parser("report", help="只看表，不联网")
    p_report.set_defaults(func=cmd_report)

    p_best = sub.add_parser("best", help="输出推荐 IP（供脚本使用）")
    p_best.set_defaults(func=cmd_best)

    p_install = sub.add_parser("install-scheduler", help="安装定时记录任务（macOS）")
    p_install.add_argument("--every", type=int, default=10, help="间隔分钟数，默认 10")
    p_install.set_defaults(func=cmd_install_scheduler)

    p_uninstall = sub.add_parser("uninstall-scheduler", help="卸载定时记录任务")
    p_uninstall.set_defaults(func=cmd_uninstall_scheduler)

    p_airports = sub.add_parser("airports", help="列出表里所有机场域名和健康概况")
    p_airports.set_defaults(func=cmd_airports)

    p_compare = sub.add_parser("compare", help="对比不同网络环境下同一个域名的 IP 状态")
    p_compare.set_defaults(func=cmd_compare)

    p_current = sub.add_parser("current", help="看 Clash 当前实际连的是哪个 IP，并对照表")
    p_current.set_defaults(func=cmd_current)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config.load_config()
    if args.data:
        cfg["data_file"] = args.data
    config.ensure_app_dir()
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
