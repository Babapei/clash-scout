"""数据存储：JSON 表 + 文件锁 + 状态机。

表结构（v2，按网络环境分层）：

{
  "version": 2,
  "environments": {
    "gw:192.168.30.1": {
      "label": "home",
      "first_seen": "...",
      "last_seen": "...",
      "domains": {
        "n43ds.chhbuv.cn": {
          "ports": [61750],
          "first_seen": "...",
          "updated_at": "...",
          "ips": { "<ip>": { ...ip record... } }
        }
      }
    }
  }
}
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2

# 状态枚举
UNKNOWN = "unknown"
ALIVE = "alive"
DEAD = "dead"
FLAKY = "flaky"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def empty_table() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "environments": {}}


def new_ip_record() -> dict[str, Any]:
    return {
        "first_seen": now_iso(),
        "last_seen": now_iso(),
        "last_check": None,
        "last_ok": None,
        "last_fail": None,
        "status": UNKNOWN,
        "ok": 0,
        "fail": 0,
        "consecutive_fail": 0,
        "latency_ms": None,
        "stale": False,
    }


# ---------------------------------------------------------------- 迁移


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """把 v1（顶层 domains）迁移到 v2（environment 下的 domains）。"""
    if data.get("version") == SCHEMA_VERSION and "environments" in data:
        return data
    if "domains" in data:
        return {
            "version": SCHEMA_VERSION,
            "environments": {
                "unknown": {
                    "label": "unknown(v1)",
                    "first_seen": now_iso(),
                    "last_seen": now_iso(),
                    "domains": data["domains"],
                }
            },
        }
    return empty_table()


# ---------------------------------------------------------------- 读写


@contextmanager
def locked(path: Path) -> Iterator[dict[str, Any]]:
    """打开表并加排他锁，退出时落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        table = _read(path)
        try:
            yield table
        finally:
            _write(path, table)
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_table()
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(path.suffix + ".broken")
        try:
            path.rename(backup)
            print(f"[warn] 表损坏，已备份到 {backup}")
        except OSError:
            pass
        return empty_table()
    if not isinstance(data, dict):
        return empty_table()
    return migrate(data)


def _write(path: Path, table: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(table, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 分层访问


def get_environment(table: dict[str, Any], env_id: str, label: str | None = None) -> dict[str, Any]:
    """取（或创建）一个网络环境节点。

    注意：已有 label 时不会覆盖——那是用户通过 `scout env` 设置的显示名，
    不能被自动任务的网关字符串冲掉。
    """
    env = table["environments"].get(env_id)
    if env is None:
        env = {
            "label": label or env_id,
            "first_seen": now_iso(),
            "last_seen": now_iso(),
            "domains": {},
        }
        table["environments"][env_id] = env
    env["last_seen"] = now_iso()
    if not env.get("label"):
        env["label"] = label or env_id
    return env


def get_domain(
    env: dict[str, Any],
    domain: str,
    port: int,
    subscription: dict[str, str] | None = None,
) -> dict[str, Any]:
    """取（或创建）某个环境下的域名节点。

    subscription 是 {"uid": ..., "name": ...}，用来区分「同一机场换域名」和「新机场」。
    """
    node = env["domains"].setdefault(
        domain,
        {"ports": [], "first_seen": now_iso(), "updated_at": now_iso(), "ips": {}},
    )
    if port not in node["ports"]:
        node["ports"].append(port)
    if subscription:
        node["subscription"] = subscription
    node["updated_at"] = now_iso()
    return node


def find_domain(table: dict[str, Any], env_id: str, domain: str) -> dict[str, Any] | None:
    env = table["environments"].get(env_id)
    if not env:
        return None
    return env["domains"].get(domain)


def all_domains(table: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for env in table["environments"].values():
        for domain in env["domains"]:
            if domain not in seen:
                seen.append(domain)
    return seen


# ---------------------------------------------------------------- IP 记录


def upsert_seen(node: dict[str, Any], ip: str) -> dict[str, Any]:
    """DNS 里出现了某个 IP：新增或刷新 last_seen。"""
    rec = node["ips"].get(ip)
    if rec is None:
        rec = new_ip_record()
        node["ips"][ip] = rec
    else:
        rec["last_seen"] = now_iso()
        rec["stale"] = False
    return rec


def apply_probe(
    rec: dict[str, Any],
    *,
    ok: bool,
    latency_ms: float | None,
    flaky_window_minutes: int = 30,
) -> None:
    """记录一次探测结果并推进状态机。"""
    rec["last_check"] = now_iso()
    if ok:
        rec["ok"] += 1
        rec["consecutive_fail"] = 0
        rec["last_ok"] = now_iso()
        rec["latency_ms"] = round(latency_ms, 1) if latency_ms is not None else rec.get("latency_ms")
        last_fail = parse_iso(rec.get("last_fail"))
        window = timedelta(minutes=flaky_window_minutes)
        if last_fail and datetime.now(timezone.utc).astimezone() - last_fail <= window:
            rec["status"] = FLAKY
        else:
            rec["status"] = ALIVE
    else:
        rec["fail"] += 1
        rec["consecutive_fail"] += 1
        rec["last_fail"] = now_iso()
        rec["status"] = DEAD if rec["consecutive_fail"] >= 2 else FLAKY


def mark_stale(node: dict[str, Any], stale_days: int) -> int:
    """超过 N 天没在 DNS 出现过的 IP 标记 stale（不删除）。"""
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(days=stale_days)
    count = 0
    for rec in node["ips"].values():
        last_seen = parse_iso(rec.get("last_seen"))
        if last_seen and last_seen < cutoff and not rec.get("stale"):
            rec["stale"] = True
            count += 1
    return count


def needs_check(rec: dict[str, Any], recheck_minutes: int) -> bool:
    """距上次检测超过 N 分钟才需要重测。"""
    last = parse_iso(rec.get("last_check"))
    if last is None:
        return True
    return datetime.now(timezone.utc).astimezone() - last >= timedelta(minutes=recheck_minutes)




# ---------------------------------------------------------------- 节点体检


def save_audit(table: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """把体检结果合并进表里的 nodes 字段。"""
    nodes = table.setdefault("nodes", {})
    stamp = now_iso()
    for item in results:
        if not item.get("ok"):
            continue
        name = str(item.get("node") or "")
        if not name:
            continue
        nodes[name] = {
            "ip": item.get("ip"),
            "city": item.get("city"),
            "country": item.get("country"),
            "org": item.get("org"),
            "is_idc": bool(item.get("is_idc")),
            "audited_at": stamp,
        }


def get_audit_nodes(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return table.get("nodes") or {}

# ---------------------------------------------------------------- 别名


def set_env_label(env: dict[str, Any], label: str) -> None:
    """给环境设置显示名（只影响显示，不影响内部 key）。"""
    env["label"] = label


def set_domain_alias(node: dict[str, Any], alias: str) -> None:
    """给机场域名设置显示名。"""
    node["alias"] = alias



def is_airport_auto(table: dict[str, Any], domain: str) -> bool:
    """这个机场是否参与自动检测。

    优先级：节点的 auto 字段 > 表级 airport_prefs > 默认 True。
    这样即使还没有任何采集数据，也能通过 airport_prefs 记住勾选状态。
    """
    for env in table["environments"].values():
        node = env["domains"].get(domain)
        if node is not None and "auto" in node:
            return bool(node["auto"])
    prefs = table.get("airport_prefs") or {}
    return bool(prefs.get(domain, {}).get("auto", True))


def set_airport_auto(table: dict[str, Any], domain: str, enabled: bool) -> None:
    """写入勾选状态：同时更新表级 prefs 和已存在节点上的 auto 字段。"""
    prefs = table.setdefault("airport_prefs", {})
    prefs.setdefault(domain, {})["auto"] = enabled
    for env in table["environments"].values():
        node = env["domains"].get(domain)
        if node is not None:
            node["auto"] = enabled

def env_display(env: dict[str, Any], env_id: str) -> str:
    return env.get("label") or env_id


def domain_display(node: dict[str, Any], domain: str) -> str:
    return node.get("alias") or domain


def list_aliases(table: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """返回 [(类型, 内部标识, 显示名, 是否已改名)]。

    环境按 env_id 唯一；机场（域名）跨环境去重，只列一次。
    """
    rows: list[tuple[str, str, str, str]] = []
    seen_domains: set[str] = set()

    for env_id, env in table["environments"].items():
        label = env.get("label") or env_id
        rows.append(("环境", env_id, label, "是" if label != env_id else "否"))

    # 同一个域名可能在多个环境下存在，优先取带别名的那个
    domain_alias: dict[str, str] = {}
    for env in table["environments"].values():
        for domain, node in env["domains"].items():
            if node.get("alias"):
                domain_alias.setdefault(domain, node["alias"])

    for env in table["environments"].values():
        for domain, node in env["domains"].items():
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            alias = domain_alias.get(domain) or node.get("alias") or domain
            rows.append(("机场", domain, alias, "是" if alias != domain else "否"))
    return rows

def success_rate(rec: dict[str, Any]) -> float | None:
    total = rec.get("ok", 0) + rec.get("fail", 0)
    if total == 0:
        return None
    return rec["ok"] / total
