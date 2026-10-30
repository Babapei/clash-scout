"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, dns_probe, port_probe, report, store


def _target(args: argparse.Namespace, cfg: dict) -> tuple[str, int]:
    domain = args.domain or cfg.get("domain")
    if not domain:
        print("错误：需要指定域名。例如 --domain n43ds.chhbuv.cn", file=sys.stderr)
        raise SystemExit(2)
    port = args.port or (cfg["probe_ports"][0] if cfg.get("probe_ports") else 61750)
    return domain, int(port)


def _data_path(cfg: dict) -> Path:
    return Path(cfg["data_file"]).expanduser()


def cmd_sample(args: argparse.Namespace, cfg: dict) -> int:
    """只采样 DNS，把新 IP 记进表，不做端口探测。"""
    domain, port = _target(args, cfg)
    try:
        counts = dns_probe.sample(
            domain,
            cfg["dns_servers"],
            args.samples or cfg["dns_samples"],
            cfg["dns_gap_seconds"],
            cfg["dns_timeout"],
        )
    except dns_probe.DigUnavailable as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if not counts:
        print("警告：DNS 没有返回任何 IP（域名失效或被劫持）")
        return 1

    with store.locked(_data_path(cfg)) as table:
        node = store.get_domain(table, domain, port)
        new_ips = [ip for ip in counts if ip not in node["ips"]]
        for ip in counts:
            store.upsert_seen(node, ip)
        store.mark_stale(node, cfg["stale_days"])
        print(f"采样完成：发现 {len(counts)} 个 IP（新增 {len(new_ips)} 个）")
        if new_ips:
            print("新 IP：" + ", ".join(sorted(new_ips)))
    return 0


def cmd_check(args: argparse.Namespace, cfg: dict) -> int:
    """采样 + 探测过期 IP + 更新表 + 输出报告。"""
    domain, port = _target(args, cfg)
    try:
        counts = dns_probe.sample(
            domain,
            cfg["dns_servers"],
            args.samples or cfg["dns_samples"],
            cfg["dns_gap_seconds"],
            cfg["dns_timeout"],
        )
    except dns_probe.DigUnavailable as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    recheck = args.recheck or cfg["recheck_minutes"]
    timeout = cfg["probe_timeout"]

    with store.locked(_data_path(cfg)) as table:
        node = store.get_domain(table, domain, port)
        for ip in counts:
            store.upsert_seen(node, ip)

        # 只探测"距上次检测超过 recheck 分钟"的 IP，避免高频
        pending = [
            ip
            for ip, rec in node["ips"].items()
            if not rec.get("stale") and store.needs_check(rec, recheck)
        ]
        for ip in pending:
            ok, latency = port_probe.tcp_probe(ip, port, timeout)
            store.apply_probe(node["ips"][ip], ok=ok, latency_ms=latency)
            mark = "OK " if ok else "DEAD"
            ms = f"{latency:.0f}ms" if latency else "-"
            print(f"  探测 {ip:<16} {mark}  {ms}")

        store.mark_stale(node, cfg["stale_days"])
        print()
        print(report.render(node, domain, port, sample_counts=counts))
    return 0


def cmd_report(args: argparse.Namespace, cfg: dict) -> int:
    """只读表，不联网。"""
    domain, port = _target(args, cfg)
    path = _data_path(cfg)
    if not path.exists():
        print(f"表还不存在：{path}\n先跑一次 sample 或 check。")
        return 1
    with store.locked(path) as table:
        node = table["domains"].get(domain)
        if not node:
            print(f"表里没有这个域名：{domain}")
            return 1
        print(report.render(node, domain, port))
    return 0


def cmd_best(args: argparse.Namespace, cfg: dict) -> int:
    """只输出推荐 IP，方便脚本调用。"""
    domain, port = _target(args, cfg)
    path = _data_path(cfg)
    with store.locked(path) as table:
        node = table["domains"].get(domain)
        best = report.pick_best(node) if node else None
    if not best:
        print("")
        return 1
    print(best[0])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clash-ip-tracker",
        description="记录代理节点域名的 IP 池健康状态（纯只读，不改任何 Clash 配置）",
    )
    parser.add_argument("--domain", help="节点域名，如 n43ds.chhbuv.cn")
    parser.add_argument("--port", type=int, help="节点端口，默认取配置")
    parser.add_argument("--data", help="自定义表文件路径")
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
