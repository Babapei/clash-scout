"""DNS 采样：用系统 dig 反复查询，收集出现过的 IP。"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Iterable

IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class DigUnavailable(RuntimeError):
    """系统找不到 dig 命令。"""


def _dig_once(domain: str, server: str, timeout: int) -> list[str]:
    try:
        result = subprocess.run(
            ["dig", "+short", f"+time={timeout}", "+tries=1", f"@{server}", domain],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if IPV4_RE.match(line.strip())]


def sample(domain: str, servers: Iterable[str], samples: int, gap: float, timeout: int) -> dict[str, int]:
    """返回 {ip: 出现次数}。

    对每个 DNS 服务器重复查询 samples 次，统计每个 IP 被返回的频率。
    """
    if shutil.which("dig") is None:
        raise DigUnavailable("系统里没有 dig 命令（macOS 通常自带，可用 brew install bind 补）")

    counts: dict[str, int] = {}
    for round_index in range(samples):
        for server in servers:
            for ip in _dig_once(domain, server, timeout):
                counts[ip] = counts.get(ip, 0) + 1
        if round_index < samples - 1:
            time.sleep(gap)
    return counts
