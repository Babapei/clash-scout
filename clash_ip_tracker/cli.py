"""命令行入口。"""

from __future__ import annotations

import argparse
import platform
import re
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

    优先级：--domain > 自动读 Verge > 配置文件 > 报错

    默认就尝试从 Verge 读，不用加 --from-clash。
    """
    if args.domain:
        port = args.port or (cfg["probe_ports"][0] if cfg.get("probe_ports") else 61750)
        return args.domain, int(port)

    # 默认尝试从 Verge 当前订阅读取
    try:
        domain, port, info = clash_config.resolve_target()
        print(f"从 Verge 读取：{info} → {domain}:{port}", file=sys.stderr)
        return domain, int(args.port or port)
    except clash_config.ClashConfigError as exc:
        # 读不到再退回配置文件
        if cfg.get("domain"):
            port = args.port or (cfg["probe_ports"][0] if cfg.get("probe_ports") else 61750)
            return cfg["domain"], int(port)
        print(f"错误：{exc}", file=sys.stderr)
        print("可以用 --domain 和 --port 手动指定", file=sys.stderr)
        raise SystemExit(2)


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

    try:
        age_info = clash_config.subscription_age_days()
    except clash_config.ClashConfigError:
        age_info = None
    if age_info and age_info[1] >= 7:
        name, days = age_info
        print(f"⚠️ 订阅「{name}」已 {days:.0f} 天未更新；如果大量失败，先到 Verge 里更新订阅")

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


def _find_domain_by_name(target: str, table: dict) -> str | None:
    """按别名或域名在表里找机场。"""
    for env in table["environments"].values():
        for domain, node in env["domains"].items():
            if node.get("alias") == target or domain == target:
                return domain
    return None


def _detect_domain_change(
    table: dict, domain: str, sub: dict[str, str] | None
) -> list[str]:
    """找同一个订阅下、除当前域名外的其他旧域名。"""
    if not sub or not sub.get("uid"):
        return []
    found: set[str] = set()
    for env in table["environments"].values():
        for old_domain, node in env["domains"].items():
            if old_domain == domain:
                continue
            if (node.get("subscription") or {}).get("uid") == sub["uid"]:
                found.add(old_domain)
    return sorted(found)


def _check_all(args: argparse.Namespace, cfg: dict) -> int:
    """测表里记录过的所有机场（不影响当前正在用的订阅）。"""
    env_id, label = resolve_env(args)
    recheck = args.recheck if args.recheck is not None else cfg["recheck_minutes"]
    timeout = cfg["probe_timeout"]
    samples = args.samples or 2  # 测多个时默认少采样，控制总耗时
    path = _data_path(cfg)

    # 先收集所有域名和端口
    with store.locked(path) as table:
        targets: list[tuple[str, int]] = []
        seen: set[str] = set()

        # ① 表里已经采集过的
        for env in table["environments"].values():
            for domain, node in env["domains"].items():
                if domain in seen:
                    continue
                seen.add(domain)
                if getattr(args, "only_enabled", False) and not store.is_airport_auto(table, domain):
                    continue
                port = (node.get("ports") or [61750])[0]
                targets.append((domain, int(port)))

        # ② 从 Verge 订阅补充还没采集过的（否则新加的机场永远测不到）
        try:
            for sub_info in clash_config.list_all_subscriptions():
                for server, port in sub_info["domains"]:
                    if server in seen:
                        continue
                    seen.add(server)
                    if getattr(args, "only_enabled", False) and not store.is_airport_auto(table, server):
                        continue
                    targets.append((server, int(port or 61750)))
        except clash_config.ClashConfigError:
            pass

    if not targets:
        print("表里还没有任何机场记录，先跑一次 check。")
        return 1

    total = len(targets)
    print(f"共 {total} 个机场，当前环境：{label}")

    try:
        age_info = clash_config.subscription_age_days()
    except clash_config.ClashConfigError:
        age_info = None
    if age_info and age_info[1] >= 7:
        name, days = age_info
        print(f"⚠️ 订阅「{name}」已 {days:.0f} 天未更新；如果大量失败，先到 Verge 里更新订阅")

    for idx, (domain, port) in enumerate(targets, 1):
        print(f"\n【{idx}/{total}】{domain}:{port}")
        counts = dns_probe.sample(
            domain,
            cfg["dns_servers"],
            samples,
            cfg["dns_gap_seconds"],
            cfg["dns_timeout"],
        )
        if not counts:
            print("  DNS 没返回结果，跳过")
            continue

        with store.locked(path) as table:
            env = store.get_environment(table, env_id, label)
            # 找这个域名属于哪个订阅：先看表里已有记录，再去 Verge 订阅里匹配
            sub = None
            for existing_env in table["environments"].values():
                existing = existing_env["domains"].get(domain)
                if existing and existing.get("subscription"):
                    sub = existing["subscription"]
                    break
            if sub is None:
                try:
                    for sub_info in clash_config.list_all_subscriptions():
                        if any(d == domain for d, _ in sub_info["domains"]):
                            sub = {"uid": sub_info["uid"], "name": sub_info["name"]}
                            break
                except clash_config.ClashConfigError:
                    pass
            node = store.get_domain(env, domain, port, subscription=sub)
            for ip in counts:
                store.upsert_seen(node, ip)

            pending = [
                ip for ip, rec in node["ips"].items()
                if not rec.get("stale") and store.needs_check(rec, recheck)
            ]
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


def cmd_check(args: argparse.Namespace, cfg: dict) -> int:
    """采样 + 探测过期 IP + 更新表 + 输出报告。"""
    if getattr(args, "all", False):
        return _check_all(args, cfg)

    if getattr(args, "airport", None):
        with store.locked(_data_path(cfg)) as table:
            resolved = _find_domain_by_name(args.airport, table)
        if not resolved:
            print(f"找不到机场「{args.airport}」。用 scout airports 看有哪些。")
            return 1
        args.domain = resolved
        print(f"指定机场：{args.airport} → {resolved}", file=sys.stderr)

    domain, port = _target(args, cfg)
    counts = _sample(args, cfg, domain)
    if counts is None:
        return 1

    recheck = args.recheck if args.recheck is not None else cfg["recheck_minutes"]
    timeout = cfg["probe_timeout"]
    env_id, label = resolve_env(args)
    try:
        sub = clash_config.current_subscription()
    except clash_config.ClashConfigError:
        sub = None

    with store.locked(_data_path(cfg)) as table:
        # 旧数据可能没有 subscription 字段：用当前订阅回填，
        # 这样才认得出来「同一个订阅下换过域名」。
        if sub:
            for env_node in table["environments"].values():
                for old_domain, old_node in env_node["domains"].items():
                    if not old_node.get("subscription"):
                        old_node["subscription"] = sub

        old_domains = _detect_domain_change(table, domain, sub)
        if old_domains:
            print(f"⚠️ 检测到域名变更（同一订阅）：{'、'.join(old_domains)} → {domain}")
            print("   旧域名的数据保留，但可能已失效。")

        env = store.get_environment(table, env_id, label)
        node = store.get_domain(env, domain, port, subscription=sub)

        # 新域名自动继承旧域名的显示名，并同步到所有环境下的同名域名
        if old_domains and not node.get("alias"):
            inherited = None
            for old_env in table["environments"].values():
                for old_domain in old_domains:
                    old_node = old_env["domains"].get(old_domain)
                    if old_node and old_node.get("alias"):
                        inherited = old_node["alias"]
                        break
                if inherited:
                    break
            if inherited:
                for env_node in table["environments"].values():
                    target = env_node["domains"].get(domain)
                    if target is not None:
                        target["alias"] = inherited
                print(f"   已继承显示名：「{inherited}」")
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


def cmd_auto(args: argparse.Namespace, cfg: dict) -> int:
    """查看 / 开启 / 关闭自动记录。"""
    if args.action == "status":
        return cmd_auto_status(cfg)
    if args.action == "off":
        return cmd_uninstall_scheduler(args, cfg)
    return cmd_install_scheduler(args, cfg)


def cmd_auto_status(cfg: dict) -> int:
    """显示定时任务的当前状态。"""
    plist, wrapper, app_dir, log_dir = _scheduler_paths()

    if not plist.exists():
        print("自动记录：未开启")
        print("  开启：scout auto on")
        return 0

    print("自动记录：已开启")

    # 是否被 launchd 加载
    try:
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
        loaded = SCHEDULER_LABEL in listing.stdout
    except (subprocess.TimeoutExpired, OSError):
        loaded = False
    print(f"  状态：{'已加载' if loaded else '未加载（可能被系统卸载，重跑 scout auto on）'}")

    # 间隔
    try:
        text = plist.read_text()
        match = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", text)
        if match:
            print(f"  间隔：每 {int(match.group(1)) // 60} 分钟")
    except OSError:
        pass

    # 上次运行
    log = log_dir / "scheduler.log"
    if log.exists():
        try:
            lines = [ln for ln in log.read_text().splitlines() if "开始记录" in ln or "跳过" in ln]
            if lines:
                print(f"  上次运行：{lines[-1]}")
        except OSError:
            pass

    print("  范围：所有勾选的机场（在 UI 机场 Tab 或 scout airport --auto 里调整）")

    print(f"  日志：{log}")
    print(f"  运行副本：{app_dir}")
    print("  关闭：scout auto off")
    return 0


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
        f'PYTHONPATH="{app_dir}" "{sys.executable}" "{app_dir}/scout.py" check --all --only-enabled 2>&1 | tail -8\n'
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

    print(f"✅ 已安装：每 {interval} 分钟自动检测所有勾选的机场")
    print(f"   日志：{log_dir}/scheduler.log")
    print("   关闭：scout auto off")
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


def cmd_forget(args: argparse.Namespace, cfg: dict) -> int:
    """删除表里的环境 / 域名 / IP。"""
    if not (args.env or args.ip or args.domain):
        print("至少指定 --env / --ip / --domain 之一")
        print("例：scout forget --env hotspot")
        print("    scout forget --ip 1.2.3.4")
        print("    scout forget --domain n43ds.chhbuv.cn")
        return 1

    path = _data_path(cfg)
    removed: list[str] = []

    with store.locked(path) as table:
        envs = table["environments"]

        # 定位目标环境
        if args.env:
            targets = [
                k for k, v in envs.items()
                if k == args.env or k == f"name:{args.env}" or v.get("label") == args.env
            ]
            if not targets:
                print(f"找不到环境「{args.env}」。现有环境：{', '.join(envs) or '（空）'}")
                return 1
        else:
            targets = list(envs.keys())

        for env_id in targets:
            env = envs[env_id]

            if args.domain:
                if args.domain in env["domains"]:
                    del env["domains"][args.domain]
                    removed.append(f"环境 {env_id} / 域名 {args.domain}")
            elif args.ip:
                for domain, node in env["domains"].items():
                    if args.ip in node["ips"]:
                        del node["ips"][args.ip]
                        removed.append(f"环境 {env_id} / {domain} / IP {args.ip}")
            else:
                del envs[env_id]
                removed.append(f"环境 {env_id}")

        # 清理空环境（没有域名或域名为空）
        for env_id in [k for k, v in envs.items() if not v["domains"]]:
            del envs[env_id]

        if not removed:
            print("没有匹配到要删除的内容。")
            return 0

        print("已删除：")
        for item in removed:
            print(f"  - {item}")

    return 0


def cmd_env(args: argparse.Namespace, cfg: dict) -> int:
    """给网络环境设置显示名。"""
    path = _data_path(cfg)
    with store.locked(path) as table:
        env_id = args.id
        if not env_id:
            gateway = detect_gateway()
            env_id = f"gw:{gateway}" if gateway else "unknown"
        env = table["environments"].get(env_id)

        if args.clear:
            if env is None:
                print(f"表里没有环境 {env_id}")
                return 1
            env.pop("label", None)
            print(f"✅ 环境 {env_id} 已恢复原始显示")
            return 0

        if not args.name:
            if env is None:
                print(f"当前环境 {env_id} 还没记录过数据")
                return 0
            print(f"当前环境: {env_id} → 显示为「{env.get('label') or env_id}」")
            return 0

        if env is None:
            known = ", ".join(table["environments"].keys()) or "（空）"
            print(f"表里没有环境 {env_id}。现有：{known}")
            print("用 --id 指定，或者先跑一次 check。")
            return 1
        store.set_env_label(env, args.name)
        print(f"✅ 环境 {env_id} → 显示为「{args.name}」")
    return 0


def cmd_airport(args: argparse.Namespace, cfg: dict) -> int:
    """给机场设置显示名。"""
    domain = args.id
    if not domain:
        try:
            domain, _, _ = clash_config.resolve_target()
        except clash_config.ClashConfigError as exc:
            print(f"错误：{exc}")
            print("用 --id 指定域名")
            return 1

    path = _data_path(cfg)
    with store.locked(path) as table:
        nodes = []
        for env in table["environments"].values():
            node = env["domains"].get(domain)
            if node is not None:
                nodes.append(node)

        if args.auto:
            enabled = args.auto == "on"
            store.set_airport_auto(table, domain, enabled)
            print(f"✅ 机场 {domain} 自动检测：{'开启' if enabled else '关闭'}")
            return 0

        if args.clear:
            if not nodes:
                print(f"表里没有域名 {domain}")
                return 1
            for node in nodes:
                node.pop("alias", None)
            print(f"✅ 机场 {domain} 已恢复原始显示")
            return 0

        if not args.name:
            if not nodes:
                print(f"表里没有域名 {domain}，先跑一次 check")
            else:
                node = nodes[0]
                auto_state = "开启" if store.is_airport_auto(table, domain) else "关闭"
                print(
                    f"当前机场: {domain} → 显示为「{node.get('alias') or domain}」"
                    f"    自动检测: {auto_state}"
                )
            return 0

        if not nodes:
            print(f"表里没有域名 {domain}，先跑一次 check")
            return 1
        for node in nodes:
            store.set_domain_alias(node, args.name)
        print(f"✅ 机场 {domain} → 显示为「{args.name}」")
    return 0


def cmd_alias(args: argparse.Namespace, cfg: dict) -> int:
    """列出所有别名。"""
    path = _data_path(cfg)
    with store.locked(path) as table:
        rows = store.list_aliases(table)
        if not rows:
            print("表里还没有数据。")
            return 0
        header = f"{'类型':<16} {'内部标识':<26} {'显示名':<16} 已改名"
        print(header)
        print("-" * len(header))
        for kind, ident, label, renamed in rows:
            print(f"{kind:<16} {ident:<26} {label:<16} {renamed}")
    return 0


def cmd_audit(args: argparse.Namespace, cfg: dict) -> int:
    """节点体检：逐个切换节点查出口 IP / 机房属性。"""
    from . import audit as audit_module

    def progress(index: int, total: int, node: str, result: dict | None) -> None:
        status = "OK" if result else "失败"
        print(f"  [{index}/{total}] {node}  {status}", flush=True)

    print(f"开始体检（代理组：{args.group}，会短暂断网，结束自动恢复）")
    try:
        results = audit_module.audit(
            group=args.group,
            limit=args.limit,
            delay=args.delay,
            on_progress=progress,
        )
    except RuntimeError as exc:
        print(f"错误：{exc}")
        return 1

    print()
    print(audit_module.format_results(results))
    return 0


def cmd_ui(args: argparse.Namespace, cfg: dict) -> int:
    """打开终端界面（需要 textual）。"""
    try:
        from .ui import run
    except ImportError:
        print("需要 Textual：python3 -m pip install textual -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1
    run()
    return 0


def cmd_airports(args: argparse.Namespace, cfg: dict) -> int:
    """列出表里记录过的所有机场，按订阅分组。"""
    path = _data_path(cfg)
    if not path.exists():
        print(f"表还不存在：{path}\n先跑一次 check。")
        return 1

    with store.locked(path) as table:
        if not table["environments"]:
            print("表里还没有任何数据。")
            return 0

        # 按订阅名分组：{订阅名: {环境id: {域名: node}}}
        groups: dict[str, dict[str, dict[str, dict]]] = {}
        unknown = "未分组（旧数据）"
        for env_id, env in table["environments"].items():
            for domain, node in env["domains"].items():
                sub = node.get("subscription") or {}
                name = sub.get("name") or unknown
                groups.setdefault(name, {}).setdefault(env_id, {})[domain] = node

        # 补充 Verge 里存在、但还没采集过的域名（显示为「未采集」）
        try:
            for sub_info in clash_config.list_all_subscriptions():
                sub_name = sub_info["name"] or "未命名订阅"
                bucket = groups.setdefault(sub_name, {})
                for server, port in sub_info["domains"]:
                    # 找该订阅已采集的环境，没有就放个占位
                    existing = None
                    for env_id in list(bucket):
                        if server in bucket[env_id]:
                            existing = env_id
                            break
                    if existing is None:
                        bucket.setdefault("__pending__", {})[server] = {
                            "_pending": True,
                            "ports": [port] if port else [],
                        }
        except clash_config.ClashConfigError:
            pass

        total = 0
        for sub_name in sorted(groups):
            print(f"机场 {sub_name}")
            env_map = groups[sub_name]
            for env_id in sorted(env_map):
                if env_id == "__pending__":
                    print("  未采集")
                else:
                    env = table["environments"][env_id]
                    label = env.get("label", env_id)
                    print(f"  环境 {label}")
                for domain in sorted(env_map[env_id]):
                    node = env_map[env_id][domain]
                    total += 1
                    if node.get("_pending"):
                        ports = ",".join(str(x) for x in node.get("ports", [])) or "-"
                        print(f"    {domain:<26} 未采集                    端口 {ports}")
                        continue
                    ips = node.get("ips", {})
                    alive = sum(1 for r in ips.values() if r.get("status") == store.ALIVE and not r.get("stale"))
                    flaky = sum(1 for r in ips.values() if r.get("status") == store.FLAKY and not r.get("stale"))
                    dead = sum(1 for r in ips.values() if r.get("status") == store.DEAD and not r.get("stale"))
                    ports = ",".join(str(x) for x in node.get("ports", [])) or "-"
                    print(
                        f"    {domain:<26} 活 {alive:<3} 抖 {flaky:<3} 死 {dead:<3} "
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
        print(report.render_compare(table, domain, show_time=args.time))
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
    p_check.add_argument("--all", action="store_true", help="测表里记录过的所有机场，不管有没有勾选")
    p_check.add_argument("--airport", help="只测指定机场（别名或域名）")
    p_check.add_argument(
        "--only-enabled",
        action="store_true",
        help="只测勾选了「自动检测」的机场（自动任务内部使用）",
    )
    p_check.set_defaults(func=cmd_check)

    p_report = sub.add_parser("report", help="只看表，不联网")
    p_report.set_defaults(func=cmd_report)

    p_best = sub.add_parser("best", help="输出推荐 IP（供脚本使用）")
    p_best.set_defaults(func=cmd_best)

    p_auto = sub.add_parser("auto", help="开启/关闭自动记录（macOS 定时任务）")
    p_auto.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status",
                        help="status 查看状态（默认），on 开启，off 关闭")
    p_auto.add_argument("--every", type=int, default=10, help="间隔分钟数，默认 10")

    p_auto.set_defaults(func=cmd_auto)

    p_forget = sub.add_parser("forget", help="删除表里的环境 / 域名 / IP")
    p_forget.add_argument("--env", help="环境名（如 hotspot 或 gw:192.168.30.1）")
    p_forget.add_argument("--ip", help="要删除的 IP")
    p_forget.add_argument("--domain", help="要删除的域名")
    p_forget.set_defaults(func=cmd_forget)

    p_env = sub.add_parser("env", help="给网络环境设置显示名")
    p_env.add_argument("name", nargs="?", help="新名字；不填则显示当前")
    p_env.add_argument("--clear", action="store_true", help="恢复原始显示")
    p_env.add_argument("--id", help="指定环境 id（如 gw:192.168.30.1）")
    p_env.set_defaults(func=cmd_env)

    p_airport = sub.add_parser("airport", help="给机场设置显示名")
    p_airport.add_argument("name", nargs="?", help="新名字；不填则显示当前")
    p_airport.add_argument("--clear", action="store_true", help="恢复原始显示")
    p_airport.add_argument("--id", help="指定域名")
    p_airport.add_argument("--auto", choices=["on", "off"], help="是否参与自动检测")
    p_airport.set_defaults(func=cmd_airport)

    p_alias = sub.add_parser("alias", help="列出所有别名")
    p_alias.set_defaults(func=cmd_alias)

    p_audit = sub.add_parser("audit", help="节点体检：逐个切换查出口 IP / 是否机房")
    p_audit.add_argument("--limit", type=int, help="只测前 N 个节点")
    p_audit.add_argument("--group", default="🌐 代理", help="代理组名，默认「🌐 代理」")
    p_audit.add_argument("--delay", type=float, default=1.0, help="每个节点等待秒数")
    p_audit.set_defaults(func=cmd_audit)

    p_ui = sub.add_parser("ui", help="打开终端界面（需要 textual）")
    p_ui.set_defaults(func=cmd_ui)

    p_airports = sub.add_parser("airports", help="列出表里所有机场域名和健康概况")
    p_airports.set_defaults(func=cmd_airports)

    p_compare = sub.add_parser("compare", help="对比不同网络环境下同一个域名的 IP 状态")
    p_compare.add_argument("--time", action="store_true", help="显示完整检测时间（默认只标超过 1 小时的）")
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
