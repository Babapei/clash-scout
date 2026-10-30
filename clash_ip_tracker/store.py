"""数据存储：JSON 表 + 文件锁 + 状态机。"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

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
    return {"version": SCHEMA_VERSION, "domains": {}}


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
        # 损坏时不覆盖用户数据，先备份
        backup = path.with_suffix(path.suffix + ".broken")
        try:
            path.rename(backup)
            print(f"[warn] 表损坏，已备份到 {backup}")
        except OSError:
            pass
        return empty_table()
    if not isinstance(data, dict) or "domains" not in data:
        return empty_table()
    data.setdefault("version", SCHEMA_VERSION)
    return data


def _write(path: Path, table: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(table, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_domain(table: dict[str, Any], domain: str, port: int) -> dict[str, Any]:
    node = table["domains"].setdefault(
        domain,
        {"ports": [], "first_seen": now_iso(), "updated_at": now_iso(), "ips": {}},
    )
    if port not in node["ports"]:
        node["ports"].append(port)
    node["updated_at"] = now_iso()
    return node


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
        # 最近失败过但现在通了 → 判定为 flaky
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


def success_rate(rec: dict[str, Any]) -> float | None:
    total = rec.get("ok", 0) + rec.get("fail", 0)
    if total == 0:
        return None
    return rec["ok"] / total
