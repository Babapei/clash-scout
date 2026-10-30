"""端口探测：只做 TCP 握手，轻量、低风险。"""

from __future__ import annotations

import socket
import time


def tcp_probe(ip: str, port: int, timeout: float) -> tuple[bool, float | None]:
    """尝试建立 TCP 连接。

    返回 (是否成功, 握手耗时毫秒)。
    只做三次握手，不发任何业务数据，对目标负载极低。
    """
    start = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, (time.monotonic() - start) * 1000
    except (OSError, socket.timeout):
        return False, None


def tls_probe(ip: str, port: int, server_name: str, timeout: float) -> tuple[bool, float | None]:
    """可选的 TLS 握手探测（未来扩展用）。

    目前未在 CLI 中使用：TCP 够用，TLS 握手更贵也更容易触发防护。
    """
    import ssl

    start = time.monotonic()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=server_name):
                return True, (time.monotonic() - start) * 1000
    except (OSError, ssl.SSLError, socket.timeout):
        return False, None
