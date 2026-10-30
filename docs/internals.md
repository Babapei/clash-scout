# scout 内部细节

## 数据位置
```
~/.clash-ip-tracker/table.json      # 表
~/.clash-ip-tracker/table.json.lock # 文件锁
```

按「环境 → 域名 → IP」三层组织：

```json
{
  "version": 2,
  "environments": {
    "gw:192.168.30.1": {
      "label": "gw:192.168.30.1",
      "last_seen": "2026-10-30T21:19:00+08:00",
      "domains": {
        "n43ds.chhbuv.cn": {
          "ports": [61750],
          "ips": {
            "47.131.82.134": {
              "first_seen": "...",
              "last_seen": "...",
              "last_check": "...",
              "last_ok": "...",
              "status": "alive",
              "ok": 1,
              "fail": 0,
              "consecutive_fail": 0,
              "latency_ms": 81,
              "stale": false
            }
          }
        }
      }
    }
  }
}
```

旧版（v1，顶层 `domains`）会自动迁移到 `unknown` 环境。

---

## 配置
默认值在 `clash_ip_tracker/config.py`，可用 `~/.clash-ip-tracker/config.json` 覆盖：

```json
{
  "dns_servers": ["223.5.5.5", "114.114.114.114"],
  "dns_samples": 6,
  "dns_gap_seconds": 0.3,
  "probe_ports": [61750],
  "probe_timeout": 3.0,
  "recheck_minutes": 10,
  "stale_days": 7
}
```

环境变量：`CLASH_TRACKER_DNS`、`CLASH_TRACKER_PORTS`、`CLASH_TRACKER_DATA`。

---

## 关于「会不会被拉黑」
设计上刻意保持低频率、低特征：

| 行为 | 频率 | 说明 |
|---|---|---|
| DNS 采样 | 每 30 分钟 | 每次只查几轮 |
| TCP 探测 | 每 5~15 分钟 | 只对「距上次检测超过 N 分钟」的 IP |
| 探测方式 | TCP 三次握手 | 不发业务数据，握手完立即关闭 |

量级参考：15 个 IP × 每 10 分钟一次 ≈ **每小时 90 次握手**，远低于正常浏览产生的连接数。

**不建议**：秒级监控、全端口扫描、并发轰炸。

---

## 当前边界
这些是「现在还没做的」，不是「做不到的」：

1. **完整 IP 池**：只能反复采样逼近。下一步可以拉长采样窗口、接入多个 DNS 源。
2. **自动切换**：现在是手动改 hosts / Script。下一步找 Verge 的可编程入口（IPC、文件监听、或包装重启）。
3. **探测深度**：现在只测 TCP 握手。下一步加 TLS 握手和真实 HTTP 请求，区分「端口开着」和「真能上网」。
4. **根本质量**：如果机场整体差，工具只能帮你选相对好的——但「选得准」本身就能省掉大量盲试。

---

## 扩展方向
- [ ] 自动从 Clash/Verge 配置读取当前订阅的域名和端口
- [ ] 支持 TLS 握手探测（`port_probe.py` 已预留）
- [ ] 导出 CSV
- [ ] 按小时统计成功率曲线，观察池子是否恶化
- [ ] 生成 `hosts` / Verge Script 片段（仍需手动应用）
- [ ] 多机场并行采样

---

## 项目结构
```
clash_ip_tracker/
├── config.py      # 默认配置 + 覆盖
├── store.py       # JSON 表、文件锁、状态机、v1→v2 迁移
├── dns_probe.py   # DNS 采样（调 dig）
├── port_probe.py  # TCP / TLS 探测
├── report.py      # 表格渲染 + 跨环境对比 + 推荐逻辑
└── cli.py         # 命令行
tests/
└── test_store.py  # 12 个单测：状态机、环境隔离、迁移、stale
```

---
